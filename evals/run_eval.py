"""Run the benchmark.

    python -m evals.run_eval --retrieval-only          # free, deterministic, no API keys
    python -m evals.run_eval --replay perfect|noisy|sloppy   # harness self-check, no API keys
    python -m evals.run_eval                           # real model via LLM_PROVIDER / LLM_MODEL

Needs Postgres with pgvector (DATABASE_URL). The eval schema is created and truncated on start.
"""

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.db.models import Base
from app.db.repositories import review_jobs as jobs
from app.domain.pr import PullRequestContext
from app.domain.states import ReviewStatus
from app.indexing.service import IndexService
from app.llm.base import LLMProvider
from app.llm.factory import make_llm_provider
from app.llm.fake import FakeProvider
from app.retrieval.embeddings import EmbeddingProvider, make_embedder
from app.review.orchestrator import ReviewContext, ReviewOrchestrator
from app.review.stages.analyze import AnalyzeStage
from app.review.stages.retrieve import RetrieveStage
from app.review.stages.validate import ValidateStage
from app.review.triage import triage
from evals.cases import BenchmarkCase, load_all
from evals.local_gh import BASE_SHA, HEAD_SHA, LocalGH
from evals.metrics import CaseScore, aggregate, score_case
from evals.replay import SimulatedReviewer
from evals.report import render
from evals.retrieval_eval import evaluate_retrieval

OUT_DIR = Path(__file__).parent


async def prepare_db(url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {names} CASCADE"))
    return async_sessionmaker(engine, expire_on_commit=False)


class _EvalFetch:
    status = ReviewStatus.FETCHING_PR

    def __init__(self, case: BenchmarkCase, number: int, settings: Settings) -> None:
        self.case, self.number, self.settings = case, number, settings

    async def run(self, ctx: ReviewContext) -> None:
        c = self.case
        ctx.gh = LocalGH(c.base_files, c.head_files)  # type: ignore[assignment]
        ctx.pr = PullRequestContext("eval/app", self.number, c.title, c.body, BASE_SHA, HEAD_SHA,
                                    "eval", None, files=c.changed)  # fmt: skip
        ctx.triage = triage(c.changed, self.settings)
        ctx.job.scope = ctx.triage.as_scope()
        await ctx.session.commit()


class _EvalIndex:
    status = ReviewStatus.INDEXING

    def __init__(self, embedder: EmbeddingProvider) -> None:
        self.embedder = embedder

    async def run(self, ctx: ReviewContext) -> None:
        from app.db.models import Repository

        repo = await ctx.session.get(Repository, ctx.job.repository_id)
        assert repo is not None  # noqa: S101
        snap, _ = await IndexService(ctx.session, self.embedder).ensure_snapshot(
            repo, BASE_SHA, ctx.require_gh()
        )
        ctx.job.snapshot_id = snap.id
        await ctx.session.commit()


class _Capture:
    status = ReviewStatus.PUBLISHING

    def __init__(self, sink: dict[str, Any]) -> None:
        self.sink = sink

    async def run(self, ctx: ReviewContext) -> None:
        self.sink.update(ctx.data)
        self.sink["usage"] = dict(ctx.job.usage or {})


async def run_case(
    sm: async_sessionmaker[AsyncSession], case: BenchmarkCase, number: int,
    provider: LLMProvider, embedder: EmbeddingProvider, settings: Settings,
) -> CaseScore:  # fmt: skip
    async with sm() as s:
        repo = await jobs.upsert_repository(
            s, github_repo_id=424242, owner="eval", name="app", installation_id=None,
            default_branch="main", private=True,
        )  # fmt: skip
        job = await jobs.create_job(s, repository=repo, pull_number=number, head_sha=HEAD_SHA,
                                    base_sha=BASE_SHA, trigger="eval", dry_run=True)  # fmt: skip
        await s.commit()
    assert job is not None  # noqa: S101
    sink: dict[str, Any] = {}
    stages = [
        _EvalFetch(case, number, settings), _EvalIndex(embedder), RetrieveStage(settings, embedder),
        AnalyzeStage(settings, provider), ValidateStage(settings, provider), _Capture(sink),
    ]  # fmt: skip
    started = time.monotonic()
    status = await ReviewOrchestrator(sm, stages).run(job.id)  # type: ignore[arg-type]
    if status != ReviewStatus.COMPLETED:
        raise RuntimeError(f"{case.id}: pipeline ended {status}")
    return score_case(case, sink.get("processed", []), sink.get("summary", ""), sink.get("usage"),
                      time.monotonic() - started)  # fmt: skip


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--replay", choices=["perfect", "noisy", "sloppy"])
    ap.add_argument(
        "--budget", type=int, default=2500, help="context token budget for retrieval eval"
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    settings = get_settings()
    cases = load_all()
    sm = await prepare_db(settings.database_url)
    embedder = make_embedder(settings)
    notes = [f"Embedding model: `{embedder.model}`. "
             + ("The hash embedder is lexical-only: vector retrieval here approximates keyword overlap, "
                "not semantics." if embedder.model.startswith("hash") else "")]  # fmt: skip

    retrieval = await evaluate_retrieval(cases, sm, embedder, args.budget)
    reasoning = scores = None
    title = "Retrieval evaluation"
    if not args.retrieval_only:
        if args.replay:
            provider: LLMProvider = FakeProvider(SimulatedReviewer(cases, args.replay))
            title = f"Harness self-check (simulated `{args.replay}` reviewer, not a real model)"
            notes.append("Review-quality numbers below come from a scripted simulator, NOT a language "
                         "model. They validate the scoring and validation pipeline only.")  # fmt: skip
        else:
            provider = make_llm_provider(settings)
            title = f"Benchmark results: {provider.name}/{provider.model}"
        scores = [
            await run_case(sm, c, i + 1, provider, embedder, settings) for i, c in enumerate(cases)
        ]
        reasoning = aggregate(scores)
    out = args.out or OUT_DIR / ("retrieval_report.md" if args.retrieval_only else "report.md")
    md = render(title, retrieval, reasoning, scores, notes)
    out.write_text(md)
    print(md)


if __name__ == "__main__":
    asyncio.run(main())
