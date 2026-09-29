"""Context builder: for a batch of changed files, assemble tiered, budgeted repository evidence.

    changed hunk -> containing symbol (head version)            Tier 1
                 -> imported / called definitions               Tier 2
                 -> callers of the changed symbols              Tier 3
                 -> tests, config, migrations                   Tier 4
                 -> hybrid lexical + vector search extras       Tier 5 (RAG)
                 -> README / CONTRIBUTING / docs                Tier 6

Structural tiers (1-4) are deterministic lookups: the review's correctness depends on them so
they are not left to similarity ranking. Tier 5 uses RRF + boosts (see scoring.py).
"""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import structlog

from app.domain.code import CodeChunk
from app.domain.pr import ChangedFile
from app.domain.retrieval import ContextBundle, RetrievedContext, Tier
from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.overlay import containing_chunks
from app.retrieval.refs import ImportTarget, all_candidate_paths, resolve_imports
from app.retrieval.scoring import DEFAULT_WEIGHTS, ScoringContext, Weights, score_candidates
from app.retrieval.store import ChunkStore
from app.retrieval.tiers import DEFAULT_SHARES, select_within_budget

log = structlog.get_logger()

CONFIG_LANGS = ["yaml", "toml", "ini", "json", "dockerfile"]
MIGRATION_PATHS = ["%migrat%", "%alembic%", "%schema%"]
MAX_HEADER_TOKENS = 300
MAX_CALLERS_PER_SYMBOL = 3


@dataclass
class ContextRequest:
    files: list[ChangedFile]  # the batch under review
    overlay: dict[str, list[CodeChunk]]  # head-version chunks for ALL files changed in the PR
    changed_paths: set[str]  # every path touched by the PR (base versions are excluded)
    pr_title: str
    budget_tokens: int


@dataclass
class _Refs:
    imports: list[str] = field(default_factory=list)
    called: set[str] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)  # short names of symbols being changed
    qualified: set[str] = field(default_factory=set)
    classes: dict[str, str] = field(default_factory=dict)  # method qname -> parent class
    changed_text: str = ""
    bases: set[str] = field(default_factory=set)
    imported_paths: set[str] = field(default_factory=set)
    signatures: list[str] = field(default_factory=list)


def _item(chunk: CodeChunk, tier: Tier, score: float, *reasons: str) -> RetrievedContext:
    return RetrievedContext(chunk, tier, score, list(reasons))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))


class ContextBuilder:
    def __init__(
        self,
        store: ChunkStore | None,
        embedder: EmbeddingProvider | None = None,
        weights: Weights = DEFAULT_WEIGHTS,
        *,
        tiers: frozenset[Tier] | None = None,
        rag_sources: frozenset[str] = frozenset({"lexical", "vector", "exact"}),
    ) -> None:
        """`tiers` and `rag_sources` exist for ablation studies; production uses the defaults."""
        self._store, self._embedder, self._weights = store, embedder, weights
        self._tiers = tiers if tiers is not None else frozenset(Tier)
        self._rag_sources = rag_sources

    async def build(self, req: ContextRequest) -> ContextBundle:
        refs = _Refs()
        cands: list[RetrievedContext] = []
        on = self._tiers
        cands += self._tier1(req, refs)  # always computed: it also fills refs for later tiers
        cands += await self._tier2(req, refs) if Tier.DEPENDENCY in on else []
        if self._store is not None:
            excl = req.changed_paths
            cands += await self._tier3(refs, excl) if Tier.USAGE in on else []
            cands += await self._tier4(req, refs, excl) if Tier.VALIDATION in on else []
            cands += await self._tier_rag(req, refs, excl) if Tier.RAG in on else []
            cands += await self._tier_docs(refs, excl) if Tier.CONVENTIONS in on else []
        cands = [c for c in cands if c.tier in on]
        bundle = select_within_budget(cands, req.budget_tokens, DEFAULT_SHARES)
        tiers = {t.label: sum(1 for i in bundle.items if i.tier == t) for t in Tier}
        log.info("context_built", items=len(bundle.items), tokens=bundle.tokens_used,
                 budget=bundle.budget, dropped=len(bundle.dropped), tiers=tiers)  # fmt: skip
        return bundle

    # -- Tier 1: the code under review (head version) ---------------------------------------
    def _tier1(self, req: ContextRequest, refs: _Refs) -> list[RetrievedContext]:
        out: list[RetrievedContext] = []
        for f in req.files:
            chunks = req.overlay.get(f.path, [])
            if not chunks:
                continue
            headers = [c for c in chunks if c.symbol_type == "module" and c.start_line == 1]
            for h in f.hunks:
                lines = h.added_lines or h.right_lines
                refs.changed_text += "\n".join(ln.text for ln in h.lines if ln.kind == "add") + "\n"
                for c in containing_chunks(chunks, lines):
                    out.append(_item(c, Tier.IMMEDIATE, 10.0, "changed_symbol"))
                    if c.symbol_type in {"function", "method", "class"} and c.symbol:
                        refs.symbols.add(c.symbol)
                        refs.qualified.add(c.qualified_name or c.symbol)
                        if c.signature:
                            refs.signatures.append(c.signature[:200])
                    refs.called.update(c.called_names)
                    refs.imports.extend(c.imports)
                    refs.bases.update(c.bases)
                    if c.parent_symbol:
                        parent = next((p for p in chunks if p.symbol_type == "class"
                                       and p.qualified_name == c.parent_symbol), None)  # fmt: skip
                        if parent is not None:
                            out.append(_item(parent, Tier.IMMEDIATE, 9.0, "enclosing_class"))
                            refs.bases.update(parent.bases)
            for hdr in headers[:1]:
                refs.imports.extend(hdr.imports)
                if hdr.token_count <= MAX_HEADER_TOKENS and any(
                    i.chunk.path == f.path for i in out
                ):
                    out.append(_item(hdr, Tier.IMMEDIATE, 5.0, "module_header"))
        return out

    # -- Tier 2: dependencies -----------------------------------------------------------------
    async def _tier2(self, req: ContextRequest, refs: _Refs) -> list[RetrievedContext]:
        out: list[RetrievedContext] = []
        used_words = (
            _words(refs.changed_text) | refs.called | {c.split(".")[0] for c in refs.called}
        )
        # sibling methods reached through self.<name>
        for f in req.files:
            chunks = req.overlay.get(f.path, [])
            for call in refs.called:
                if call.startswith(("self.", "cls.")) and call.count(".") == 1:
                    name = call.split(".", 1)[1]
                    for c in chunks:
                        if c.symbol_type == "method" and c.symbol == name:
                            out.append(_item(c, Tier.DEPENDENCY, 6.0, "sibling_method"))
        # bare calls resolved to definitions elsewhere in the PR's own (head-version) files
        bare = {c for c in refs.called if "." not in c}
        for chunks in req.overlay.values():
            for c in chunks:
                is_def = c.symbol_type in {"function", "class"} and c.qualified_name in bare
                if is_def and c.qualified_name not in refs.symbols:  # the changed symbol is Tier 1
                    out.append(_item(c, Tier.DEPENDENCY, 7.0, "same_pr_definition"))
        imports = [i for i in dict.fromkeys(refs.imports) if i.rsplit(".", 1)[-1] in used_words]
        imports = imports[:24]
        overlay_paths = set(req.overlay)
        existing = set(overlay_paths & set(all_candidate_paths(imports)))
        if self._store is not None:
            existing |= await self._store.existing_paths(all_candidate_paths(imports))
        targets = resolve_imports(imports, existing)
        refs.imported_paths = {t.path for t in targets}
        by_path: dict[str, list[ImportTarget]] = {}
        for t in targets:
            by_path.setdefault(t.path, []).append(t)
        for path, tlist in by_path.items():
            symbols: set[str] = set()
            for t in tlist:
                if t.symbol:
                    symbols.add(t.symbol)
                else:  # module import: pick attributes used as `alias.attr(...)`
                    symbols |= {c.split(".")[1] for c in refs.called
                                if c.startswith(f"{t.alias}.") and c.count(".") >= 1}  # fmt: skip
            if not symbols:
                continue
            if path in overlay_paths:
                found = [c for c in req.overlay[path]
                         if c.qualified_name in symbols or c.symbol in symbols]  # fmt: skip
            elif self._store is not None:
                found = await self._store.in_paths([path], sorted(symbols))
            else:
                found = []
            for c in found[:4]:
                out.append(_item(c, Tier.DEPENDENCY, 8.0, "imported_definition"))
        if self._store is not None and refs.bases:
            for c in (await self._store.by_names(sorted(refs.bases), req.changed_paths, limit=4))[
                :2
            ]:
                out.append(_item(c, Tier.DEPENDENCY, 5.0, "base_class"))
        return out

    # -- Tier 3: usage ------------------------------------------------------------------------
    async def _tier3(self, refs: _Refs, exclude: set[str]) -> list[RetrievedContext]:
        assert self._store is not None  # noqa: S101 - guarded by caller
        out: list[RetrievedContext] = []
        if not refs.symbols:
            return out
        callers = await self._store.callers(sorted(refs.symbols), exclude, limit=30)
        per_symbol: dict[str, int] = {}
        imported = refs.imported_paths
        for c in sorted(callers, key=lambda c: (c.path not in imported, c.path, c.start_line)):
            hit = next((s for s in sorted(refs.symbols) if s in c.called_names), None)
            if hit is None or per_symbol.get(hit, 0) >= MAX_CALLERS_PER_SYMBOL:
                continue
            per_symbol[hit] = per_symbol.get(hit, 0) + 1
            out.append(_item(c, Tier.USAGE, 5.0, f"calls:{hit}"))
        return out

    # -- Tier 4: tests, config, migrations ------------------------------------------------------
    async def _tier4(
        self, req: ContextRequest, refs: _Refs, exclude: set[str]
    ) -> list[RetrievedContext]:
        assert self._store is not None  # noqa: S101
        out: list[RetrievedContext] = []
        stems = [PurePosixPath(f.path).stem for f in req.files if not f.is_test]
        tests = await self._store.tests_for(sorted(refs.symbols), stems, exclude, limit=20)
        for c in tests[:3]:
            out.append(_item(c, Tier.VALIDATION, 6.0, "test_for_changed_symbol"))
        query = " ".join(sorted(refs.symbols | refs.called))
        if query:
            for c in (await self._store.lexical(query, exclude, limit=6, languages=CONFIG_LANGS))[
                :2
            ]:
                out.append(_item(c, Tier.VALIDATION, 4.0, "config_match"))
            for c in (
                await self._store.lexical(query, exclude, limit=6, path_like=MIGRATION_PATHS)
            )[:2]:
                out.append(_item(c, Tier.VALIDATION, 4.0, "migration_or_schema"))
        return out

    # -- Tier 5: hybrid search extras ---------------------------------------------------------
    async def _tier_rag(
        self, req: ContextRequest, refs: _Refs, exclude: set[str]
    ) -> list[RetrievedContext]:
        assert self._store is not None  # noqa: S101
        signatures = " ".join(refs.signatures) or " ".join(sorted(refs.qualified))
        query_text = f"{req.pr_title}\n{signatures}\n{refs.changed_text[:2000]}"
        lexical = await self._store.lexical(query_text, exclude, limit=20)
        ranked: dict[str, list[CodeChunk]] = {}
        if "lexical" in self._rag_sources:
            ranked["lexical"] = lexical
        if self._embedder is not None and "vector" in self._rag_sources:
            vec = await self._embedder.embed_query(query_text[:6000])
            ranked["vector"] = await self._store.vector(vec, exclude, limit=20)
        names = sorted(refs.qualified | refs.called)
        if names and "exact" in self._rag_sources:
            ranked["exact"] = await self._store.by_names(names, exclude, limit=10)
        sctx = ScoringContext(
            referenced_names=frozenset(refs.called | {c.rsplit(".", 1)[-1] for c in refs.called}),
            imported_paths=frozenset(refs.imported_paths),
            called_names=frozenset(refs.called | {c.rsplit(".", 1)[-1] for c in refs.called}),
            changed_dirs=frozenset(str(PurePosixPath(f.path).parent) for f in req.files),
            changed_symbols=frozenset(refs.symbols),
        )
        scored = score_candidates(ranked, sctx, self._weights)
        return [_item(s.chunk, Tier.RAG, s.score, *s.reasons) for s in scored[:8]]

    # -- Tier 6: conventions ----------------------------------------------------------------
    async def _tier_docs(self, refs: _Refs, exclude: set[str]) -> list[RetrievedContext]:
        assert self._store is not None  # noqa: S101
        docs = [
            d for d in await self._store.docs() if d.path not in exclude and d.token_count <= 400
        ]
        terms = _words(" ".join(sorted(refs.symbols | refs.called))).union(
            {w.lower() for w in _words(refs.changed_text)}
        )
        ranked = sorted(
            docs,
            key=lambda d: (
                -len(terms & {w.lower() for w in _words(d.content)}),
                d.path,
                d.start_line,
            ),
        )
        return [_item(d, Tier.CONVENTIONS, 2.0, "repository_guidelines") for d in ranked[:2]]
