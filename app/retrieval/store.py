"""Snapshot-scoped chunk queries (vector, lexical, structural).

Every query is constrained to (repository_id, snapshot manifest): a chunk is reachable only
through the snapshot's `snapshot_files`, which is what keeps stale embeddings out of context,
and `repository_id` is checked explicitly so identical blobs in other repos can never leak in.
"""

import re
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Select, and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CodeChunk, SnapshotFile
from app.domain.code import CHUNKER_VERSION
from app.domain.code import CodeChunk as Chunk

_STOP = frozenset(
    [
        "self",
        "cls",
        "none",
        "true",
        "false",
        "return",
        "import",
        "from",
        "class",
        "def",
        "async",
        "await",
        "and",
        "not",
        "for",
        "the",
        "with",
        "else",
        "elif",
        "pass",
        "raise",
        "try",
        "except",
        "finally",
        "lambda",
        "yield",
        "while",
        "break",
        "continue",
        "global",
        "str",
        "int",
        "dict",
        "list",
        "bool",
        "float",
        "set",
        "tuple",
        "any",
        "type",
        "isinstance",
        "len",
        "range",
        "print",
    ]
)


def to_tsquery(text: str, max_terms: int = 24) -> str | None:
    """OR-query of distinct lowercased identifier fragments, safe to pass to to_tsquery().

    Mirrors how the `fts` column is built: dots become spaces and the parser splits on
    underscores, so `verify_token` is indexed as `verify` + `token` and `SessionManager` as
    `sessionmanager`.
    """
    seen: dict[str, None] = {}
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        pieces = ident.split("_") if "_" in ident else [ident]
        for piece in pieces:
            tok = piece.lower()
            if len(tok) > 2 and tok.isalnum() and tok not in _STOP:
                seen[tok] = None
    terms = list(seen)[:max_terms]
    return " | ".join(terms) if terms else None


class ChunkStore:
    def __init__(self, session: AsyncSession, repository_id: uuid.UUID, snapshot_id: uuid.UUID,
                 embedding_model: str | None = None) -> None:  # fmt: skip
        self._s, self._repo, self._snap, self._model = (
            session,
            repository_id,
            snapshot_id,
            embedding_model,
        )

    # -- helpers -----------------------------------------------------------------------
    def _base(self, *extra: Any, exclude_paths: Iterable[str] = ()) -> Select[Any]:
        q: Any = (
            select(CodeChunk, SnapshotFile.path.label("sf_path"))
            .join(SnapshotFile, SnapshotFile.blob_sha == CodeChunk.blob_sha)
            .where(
                SnapshotFile.snapshot_id == self._snap,
                CodeChunk.repository_id == self._repo,
                CodeChunk.chunker_version == CHUNKER_VERSION,
                *extra,
            )
        )
        excl = list(exclude_paths)
        if excl:
            q = q.where(SnapshotFile.path.notin_(excl))
        result: Select[Any] = q
        return result

    async def _run(self, q: Select[Any]) -> list[Chunk]:
        rows = (await self._s.execute(q)).all()
        return [_to_domain(row[0], row[1]) for row in rows]

    # -- structural --------------------------------------------------------------------
    async def by_names(self, names: Sequence[str], exclude_paths: Iterable[str] = (),
                       limit: int = 30) -> list[Chunk]:  # fmt: skip
        """Definitions whose qualified name or bare symbol equals one of `names`."""
        if not names:
            return []
        q = self._base(
            CodeChunk.symbol_type.in_(["function", "method", "class"]),
            or_(CodeChunk.qualified_name.in_(names), CodeChunk.symbol.in_(names)),
            exclude_paths=exclude_paths,
        ).limit(limit)
        return await self._run(q)

    async def in_paths(self, paths: Sequence[str], symbols: Sequence[str] = (),
                       with_headers: bool = False, limit: int = 40) -> list[Chunk]:  # fmt: skip
        """Chunks of given files, filtered to `symbols` (and optionally module headers)."""
        if not paths:
            return []
        conds: list[Any] = []
        if symbols:
            conds.append(CodeChunk.qualified_name.in_(symbols))
            conds.append(CodeChunk.symbol.in_(symbols))
        if with_headers:
            conds.append(and_(CodeChunk.symbol_type == "module", CodeChunk.start_line == 1))
        if not conds:
            return []
        q = self._base(SnapshotFile.path.in_(paths), or_(*conds)).limit(limit)
        return await self._run(q)

    async def callers(self, names: Sequence[str], exclude_paths: Iterable[str] = (),
                      limit: int = 20) -> list[Chunk]:  # fmt: skip
        if not names:
            return []
        q = self._base(
            CodeChunk.called_names.overlap(list(names)),
            CodeChunk.is_test.is_(False),
            CodeChunk.symbol_type.in_(["function", "method"]),
            exclude_paths=exclude_paths,
        ).limit(limit)
        return await self._run(q)

    async def tests_for(
        self,
        names: Sequence[str],
        stems: Sequence[str],
        exclude_paths: Iterable[str] = (),
        limit: int = 20,
    ) -> list[Chunk]:
        conds: list[Any] = []
        if names:
            conds.append(CodeChunk.called_names.overlap(list(names)))
        # Naming-convention match: only actual test functions, not module headers/imports.
        path_conds = [SnapshotFile.path.ilike(f"%test_{s}%") for s in stems if s]
        path_conds += [SnapshotFile.path.ilike(f"%{s}_test%") for s in stems if s]
        if path_conds:
            conds.append(and_(CodeChunk.symbol_type.in_(["function", "method"]), or_(*path_conds)))
        if not conds:
            return []
        q = self._base(CodeChunk.is_test.is_(True), or_(*conds), exclude_paths=exclude_paths).limit(
            limit
        )
        return await self._run(q)

    async def docs(self, limit: int = 40) -> list[Chunk]:
        q = self._base(
            CodeChunk.language == "markdown",
            or_(*[SnapshotFile.path.ilike(p) for p in (
                "readme%", "contributing%", "docs/%", "%architecture%", "%style%", "%conventions%",
            )]),
        ).limit(limit)  # fmt: skip
        return await self._run(q)

    # -- lexical -----------------------------------------------------------------------
    async def lexical(self, text: str, exclude_paths: Iterable[str] = (), limit: int = 20,
                      languages: Sequence[str] | None = None,
                      path_like: Sequence[str] | None = None) -> list[Chunk]:  # fmt: skip
        """Full-text search over qualified_name + signature + content, ranked by ts_rank_cd."""
        tsq = to_tsquery(text)
        if not tsq:
            return []
        query = func.to_tsquery("simple", tsq)
        extra: list[Any] = [CodeChunk.fts.op("@@")(query)]
        if languages:
            extra.append(CodeChunk.language.in_(list(languages)))
        if path_like:
            extra.append(or_(*[SnapshotFile.path.ilike(p) for p in path_like]))
        q = (
            self._base(*extra, exclude_paths=exclude_paths)
            .order_by(func.ts_rank_cd(CodeChunk.fts, query).desc())
            .limit(limit)
        )
        return await self._run(q)

    async def similar_names(self, name: str, exclude_paths: Iterable[str] = (),
                            limit: int = 5, threshold: float = 0.6) -> list[Chunk]:  # fmt: skip
        """Trigram match on qualified_name (typo/partial identifier tolerance)."""
        sim = func.similarity(CodeChunk.qualified_name, literal(name))
        q = (
            self._base(sim >= threshold, exclude_paths=exclude_paths)
            .order_by(sim.desc())
            .limit(limit)
        )
        return await self._run(q)

    # -- vector ------------------------------------------------------------------------
    async def vector(self, embedding: list[float], exclude_paths: Iterable[str] = (),
                     limit: int = 20) -> list[Chunk]:  # fmt: skip
        """Cosine nearest neighbours among chunks embedded with the *current* model only."""
        if self._model is None:
            return []
        dist = CodeChunk.embedding.cosine_distance(embedding)
        q = (
            self._base(
                CodeChunk.embedding.is_not(None),
                CodeChunk.embedding_model == self._model,
                exclude_paths=exclude_paths,
            )  # fmt: skip
            .order_by(dist)
            .limit(limit)
        )
        return await self._run(q)

    async def existing_paths(self, paths: Sequence[str]) -> set[str]:
        if not paths:
            return set()
        rows = await self._s.execute(
            select(SnapshotFile.path).where(
                SnapshotFile.snapshot_id == self._snap, SnapshotFile.path.in_(paths)
            )  # fmt: skip
        )
        return set(rows.scalars())


def _to_domain(row: CodeChunk, path: str) -> Chunk:
    return Chunk(
        path=path, language=row.language, symbol_type=row.symbol_type,
        start_line=row.start_line, end_line=row.end_line, content=row.content,
        symbol=row.symbol, qualified_name=row.qualified_name, parent_symbol=row.parent_symbol,
        signature=row.signature, imports=list(row.imports or []),
        called_names=list(row.called_names or []), bases=list(row.bases or []),
        is_test=row.is_test, blob_sha=row.blob_sha, chunk_id=str(row.id), origin="base",
    )  # fmt: skip
