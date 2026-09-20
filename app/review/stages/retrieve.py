"""RETRIEVING_CONTEXT stage: head overlay, batching, and a tiered context bundle per batch."""

import structlog

from app.core.config import Settings
from app.db.models import Repository
from app.domain.states import ReviewStatus
from app.retrieval.context_builder import ContextBuilder, ContextRequest
from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.overlay import build_overlay
from app.retrieval.store import ChunkStore
from app.review.batching import context_budget, make_batches
from app.review.orchestrator import ReviewContext

log = structlog.get_logger()


class RetrieveStage:
    status = ReviewStatus.RETRIEVING_CONTEXT

    def __init__(self, settings: Settings, embedder: EmbeddingProvider) -> None:
        self._settings, self._embedder = settings, embedder

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        job, pr, triage, gh = ctx.job, ctx.require_pr(), ctx.triage, ctx.require_gh()
        if triage is None:
            raise RuntimeError("triage result missing")
        repo = await ctx.session.get(Repository, job.repository_id)
        if repo is None:  # pragma: no cover
            raise RuntimeError("repository missing")

        # Overlay covers every selected file so cross-file dependencies inside the PR resolve
        # to the head version even when the files land in different batches.
        async def fetch_head(path: str) -> bytes | None:
            return await gh.get_file_content(repo.owner, repo.name, path, pr.head_sha)

        overlay = await build_overlay(triage.selected, fetch_head)
        changed_paths = {f.path for f in pr.files} | {
            f.previous_path for f in pr.files if f.previous_path
        }

        batches, skipped = make_batches(
            triage.selected, self._settings.max_context_tokens, self._settings.max_model_calls
        )
        for path, reason in skipped:
            triage.skipped.append((path, reason))
            triage.degraded = True

        store = (
            ChunkStore(ctx.session, repo.id, job.snapshot_id, self._embedder.model)
            if job.snapshot_id is not None
            else None
        )
        builder = ContextBuilder(store, self._embedder)
        for batch in batches:
            batch.bundle = await builder.build(
                ContextRequest(
                    files=batch.files,
                    overlay=overlay,
                    changed_paths=changed_paths,
                    pr_title=pr.title,
                    budget_tokens=context_budget(batch, self._settings.max_context_tokens),
                )  # fmt: skip
            )
        ctx.batches = batches
        ctx.overlay = overlay
        job.scope = {
            **triage.as_scope(),
            **{k: v for k, v in job.scope.items() if k in {"no_index_reason"}},
            "batches": len(batches),
            "context_paths": sorted({p for b in batches if b.bundle for p in b.bundle.paths()}),
        }
        await ctx.session.commit()
        log.info(
            "context_ready", batches=len(batches), skipped=len(skipped), indexed=store is not None
        )
        return None
