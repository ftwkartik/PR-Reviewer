"""Hybrid candidate scoring: Reciprocal Rank Fusion + bounded, explainable structural boosts.

Why RRF: BM25-style `ts_rank_cd` scores and cosine similarities live on different scales and
can't be added honestly; ranks can. Why boosts: for code, *where* a chunk sits relative to the
change (same directory, imported by it, tests it) carries signal neither text retriever sees.
Each boost is a named constant so the weights can be tuned against the retrieval benchmark.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.domain.code import CodeChunk

RRF_K = 60


@dataclass(frozen=True)
class Weights:
    exact_name: float = 1.00  # chunk's qualified name/symbol is literally referenced by the change
    import_edge: float = 0.30  # chunk lives in a module the changed code imports
    called_edge: float = 0.30  # chunk defines something the changed code calls
    same_dir: float = 0.15
    test_relation: float = 0.25  # a test that references a changed symbol
    generic_penalty: float = -0.20  # tiny / boilerplate chunks (dunder methods, one-liners)
    module_penalty: float = -0.15  # import-header / module-level sections rarely answer a question


DEFAULT_WEIGHTS = Weights()
_GENERIC = frozenset(
    {"__init__", "__repr__", "__str__", "__eq__", "__hash__", "__enter__", "__exit__"}
)
RRF_SCALE = 60.0  # maps 1/(K+rank) (~0.016 at rank 1) into the same order of magnitude as boosts


@dataclass
class ScoringContext:
    referenced_names: frozenset[str] = (
        frozenset()
    )  # symbols named in changed code (exact lookup set)
    imported_paths: frozenset[str] = frozenset()  # repo paths of imported modules
    called_names: frozenset[str] = frozenset()
    changed_dirs: frozenset[str] = frozenset()
    changed_symbols: frozenset[str] = frozenset()  # short names of symbols the PR modified


@dataclass
class Scored:
    chunk: CodeChunk
    score: float
    reasons: list[str] = field(default_factory=list)


def rrf(ranked_lists: dict[str, list[CodeChunk]]) -> dict[str, tuple[float, list[str]]]:
    """chunk_id -> (fused score, which retrievers found it and at what rank)."""
    fused: dict[str, float] = defaultdict(float)
    why: dict[str, list[str]] = defaultdict(list)
    for source, chunks in ranked_lists.items():
        for rank, c in enumerate(chunks, start=1):
            fused[c.chunk_id] += 1.0 / (RRF_K + rank)
            why[c.chunk_id].append(f"{source}#{rank}")
    return {cid: (fused[cid] * RRF_SCALE, why[cid]) for cid in fused}


def score_candidates(
    ranked_lists: dict[str, list[CodeChunk]],
    ctx: ScoringContext,
    weights: Weights = DEFAULT_WEIGHTS,
) -> list[Scored]:
    base = rrf(ranked_lists)
    by_id = {c.chunk_id: c for chunks in ranked_lists.values() for c in chunks}
    out: list[Scored] = []
    for cid, (score, reasons) in base.items():
        c = by_id[cid]
        reasons = list(reasons)
        names = {n for n in (c.qualified_name, c.symbol) if n}
        if names & ctx.referenced_names:
            score += weights.exact_name
            reasons.append("exact_symbol")
        if c.path in ctx.imported_paths:
            score += weights.import_edge
            reasons.append("imported_module")
        if c.symbol and c.symbol in ctx.called_names:
            score += weights.called_edge
            reasons.append("called_by_change")
        if str(PurePosixPath(c.path).parent) in ctx.changed_dirs:
            score += weights.same_dir
            reasons.append("same_dir")
        if c.is_test and (set(c.called_names) & ctx.changed_symbols):
            score += weights.test_relation
            reasons.append("tests_changed_symbol")
        if c.symbol_type == "module" and "exact_symbol" not in reasons:
            score += weights.module_penalty
            reasons.append("module_section")
        if c.token_count < 15 or (c.symbol in _GENERIC):
            score += weights.generic_penalty
            reasons.append("generic")
        out.append(Scored(c, score, reasons))
    out.sort(key=lambda s: (-s.score, s.chunk.path, s.chunk.start_line))  # deterministic ties
    return out
