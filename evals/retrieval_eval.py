"""Retrieval evaluation, independent of any LLM.

Question answered: did the context builder surface the code a reviewer needs in order to reason
about each seeded bug (e.g. the `Session.is_expired` definition, the test that pins the behaviour,
the contributing guideline)? Every benchmark finding lists `required_context` symbols; a hit means
a retrieved chunk with that qualified name is in the final, budgeted bundle.

Ablations show what each retrieval component contributes (see MODES).
"""

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import review_jobs as jobs
from app.domain.retrieval import ContextBundle, Tier
from app.indexing.service import IndexService
from app.retrieval.context_builder import ContextBuilder, ContextRequest
from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.overlay import build_overlay
from app.retrieval.store import ChunkStore
from evals.cases import BenchmarkCase
from evals.local_gh import BASE_SHA, HEAD_SHA, LocalGH

ALL_SOURCES = frozenset({"lexical", "vector", "exact"})
_RAG = frozenset({Tier.IMMEDIATE, Tier.RAG})
MODES: dict[str, dict[str, object]] = {
    "diff_only": {"tiers": frozenset({Tier.IMMEDIATE})},
    "vector_only": {"tiers": _RAG, "rag_sources": frozenset({"vector"})},
    "lexical_only": {"tiers": _RAG, "rag_sources": frozenset({"lexical"})},
    "hybrid_rag": {"tiers": _RAG, "rag_sources": ALL_SOURCES},
    "full": {},  # structural tiers 2-4, hybrid RAG and conventions
}


@dataclass
class CaseRetrieval:
    case_id: str
    required: list[str]
    hits: list[str]
    tokens: int
    items: int
    extra_items: int  # items beyond Tier 1

    @property
    def missed(self) -> list[str]:
        return [r for r in self.required if r not in self.hits]


@dataclass
class ModeReport:
    mode: str
    cases: list[CaseRetrieval] = field(default_factory=list)

    @property
    def required_total(self) -> int:
        return sum(len(c.required) for c in self.cases)

    @property
    def hit_rate(self) -> float:
        total = self.required_total
        return sum(len(c.hits) for c in self.cases) / total if total else 0.0

    @property
    def fully_covered(self) -> float:
        scored = [c for c in self.cases if c.required]
        return sum(1 for c in scored if not c.missed) / len(scored) if scored else 0.0

    @property
    def mean_tokens(self) -> float:
        return sum(c.tokens for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def context_precision(self) -> float:
        """Required hits per retrieved non-Tier-1 item: how much of the extra context was needed."""
        extra = sum(c.extra_items for c in self.cases)
        return sum(len(c.hits) for c in self.cases) / extra if extra else 0.0


def _hit(bundle: ContextBundle, symbol: str) -> bool:
    return any(i.chunk.qualified_name == symbol or i.chunk.symbol == symbol for i in bundle.items)


async def evaluate_retrieval(
    cases: list[BenchmarkCase],
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingProvider,
    budget: int = 2500,
    modes: list[str] | None = None,
) -> dict[str, ModeReport]:
    base = cases[0].base_files
    async with sessionmaker() as s:
        repo = await jobs.upsert_repository(
            s, github_repo_id=424242, owner="eval", name="app", installation_id=None,
            default_branch="main", private=True,
        )  # fmt: skip
        await s.commit()
        snap, _ = await IndexService(s, embedder).ensure_snapshot(
            repo,
            BASE_SHA,
            LocalGH(base, base),  # type: ignore[arg-type]
        )

    reports: dict[str, ModeReport] = {}
    for mode in modes or list(MODES):
        rep = reports[mode] = ModeReport(mode)
        for case in (c for c in cases if c.expected):
            gh = LocalGH(case.base_files, case.head_files)

            async def fetch(path: str, gh: LocalGH = gh) -> bytes | None:
                return await gh.get_file_content("eval", "app", path, HEAD_SHA)

            overlay = await build_overlay(case.changed, fetch)
            async with sessionmaker() as s:
                store = ChunkStore(s, repo.id, snap.id, embedder.model)
                builder = ContextBuilder(store, embedder, **MODES[mode])  # type: ignore[arg-type]
                bundle = await builder.build(
                    ContextRequest(case.changed, overlay, case.changed_paths, case.title, budget)
                )
            required = [sym for e in case.expected for sym in e.required_context]
            rep.cases.append(CaseRetrieval(
                case.id, required, [r for r in required if _hit(bundle, r)], bundle.tokens_used,
                len(bundle.items), sum(1 for i in bundle.items if i.tier != Tier.IMMEDIATE),
            ))  # fmt: skip
    return reports
