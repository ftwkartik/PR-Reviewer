import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CodeChunk,
    EmbeddingCache,
    Repository,
    RepositorySnapshot,
    ReviewFinding,
    ReviewJob,
    SnapshotFile,
)  # fmt: skip
from app.db.models.base import utcnow
from app.domain.code import CodeChunk as DomainChunk

Manifest = dict[str, tuple[str, str | None]]  # path -> (blob_sha, language)


async def get_snapshot(
    session: AsyncSession, repo_id: uuid.UUID, sha: str, model: str, chunker_version: str
) -> RepositorySnapshot | None:
    return (
        await session.execute(
            select(RepositorySnapshot).where(
                RepositorySnapshot.repository_id == repo_id,
                RepositorySnapshot.commit_sha == sha,
                RepositorySnapshot.embedding_model == model,
                RepositorySnapshot.chunker_version == chunker_version,
            )
        )
    ).scalar_one_or_none()


async def get_or_create_snapshot(
    session: AsyncSession, repo_id: uuid.UUID, sha: str, model: str, chunker_version: str,
    parent_id: uuid.UUID | None,
) -> RepositorySnapshot:  # fmt: skip
    stmt = (
        insert(RepositorySnapshot)
        .values(
            id=uuid.uuid4(),
            repository_id=repo_id,
            commit_sha=sha,
            embedding_model=model,
            chunker_version=chunker_version,
            parent_snapshot_id=parent_id,
            status="building",
        )  # fmt: skip
        .on_conflict_do_nothing()
    )
    await session.execute(stmt)
    await session.commit()
    snap = await get_snapshot(session, repo_id, sha, model, chunker_version)
    if snap is None:  # pragma: no cover
        raise RuntimeError("snapshot vanished after insert")
    return snap


async def latest_ready_snapshot(
    session: AsyncSession, repo_id: uuid.UUID, model: str, chunker_version: str, exclude_sha: str
) -> RepositorySnapshot | None:
    return (
        await session.execute(
            select(RepositorySnapshot)
            .where(
                RepositorySnapshot.repository_id == repo_id,
                RepositorySnapshot.embedding_model == model,
                RepositorySnapshot.chunker_version == chunker_version,
                RepositorySnapshot.status == "ready",
                RepositorySnapshot.commit_sha != exclude_sha,
            )
            .order_by(RepositorySnapshot.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def load_manifest(session: AsyncSession, snapshot_id: uuid.UUID) -> Manifest:
    rows = await session.execute(
        select(SnapshotFile.path, SnapshotFile.blob_sha, SnapshotFile.language).where(
            SnapshotFile.snapshot_id == snapshot_id
        )
    )
    return {p: (b, lang) for p, b, lang in rows}


async def existing_blob_shas(
    session: AsyncSession, repo_id: uuid.UUID, chunker_version: str, blob_shas: list[str]
) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(blob_shas), 5000):
        rows = await session.execute(
            select(CodeChunk.blob_sha)
            .where(
                CodeChunk.repository_id == repo_id,
                CodeChunk.chunker_version == chunker_version,
                CodeChunk.blob_sha.in_(blob_shas[i : i + 5000]),
            )
            .distinct()
        )
        found.update(rows.scalars())
    return found


async def cached_embeddings(
    session: AsyncSession, hashes: list[str], model: str
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for i in range(0, len(hashes), 2000):
        rows = await session.execute(
            select(EmbeddingCache.content_hash, EmbeddingCache.embedding).where(
                EmbeddingCache.embedding_model == model,
                EmbeddingCache.content_hash.in_(hashes[i : i + 2000]),
            )
        )
        out.update({h: [float(x) for x in v] for h, v in rows})
    return out


async def store_embedding_cache(
    session: AsyncSession, items: dict[str, list[float]], model: str
) -> None:
    rows = [{"content_hash": h, "embedding_model": model, "embedding": v} for h, v in items.items()]
    for i in range(0, len(rows), 500):
        await session.execute(
            insert(EmbeddingCache).values(rows[i : i + 500]).on_conflict_do_nothing()
        )


async def insert_chunks(
    session: AsyncSession, repo_id: uuid.UUID, chunker_version: str, model: str,
    chunks: list[tuple[DomainChunk, list[float] | None]],
) -> int:  # fmt: skip
    rows: list[dict[str, Any]] = [
        {
            "id": uuid.uuid4(), "repository_id": repo_id, "blob_sha": c.blob_sha,
            "chunker_version": chunker_version, "path": c.path, "language": c.language,
            "symbol": c.symbol, "qualified_name": c.qualified_name, "symbol_type": c.symbol_type,
            "parent_symbol": c.parent_symbol, "start_line": c.start_line, "end_line": c.end_line,
            "signature": c.signature, "imports": c.imports, "called_names": c.called_names,
            "bases": c.bases, "is_test": c.is_test, "content": c.content,
            "content_hash": c.content_hash, "token_count": c.token_count,
            "embedding": vec, "embedding_model": model if vec is not None else None,
        }
        for c, vec in chunks
    ]  # fmt: skip
    n = 0
    for i in range(0, len(rows), 200):
        res = await session.execute(
            insert(CodeChunk)
            .values(rows[i : i + 200])
            .on_conflict_do_nothing(constraint="uq_code_chunks_identity")
        )
        n += cast(CursorResult[Any], res).rowcount or 0
    return n


async def write_manifest(session: AsyncSession, snapshot_id: uuid.UUID, manifest: Manifest) -> None:
    await session.execute(delete(SnapshotFile).where(SnapshotFile.snapshot_id == snapshot_id))
    rows = [{"snapshot_id": snapshot_id, "path": p, "blob_sha": b, "language": lang}
            for p, (b, lang) in manifest.items()]  # fmt: skip
    for i in range(0, len(rows), 2000):
        await session.execute(insert(SnapshotFile).values(rows[i : i + 2000]))


async def finalize_snapshot(
    session: AsyncSession, snap: RepositorySnapshot, *, file_count: int, chunk_count: int
) -> None:
    snap.status, snap.file_count, snap.chunk_count = "ready", file_count, chunk_count
    snap.finished_at, snap.error = utcnow(), None
    await session.commit()


async def fail_snapshot(session: AsyncSession, snap: RepositorySnapshot, error: str) -> None:
    await session.rollback()
    snap.status, snap.error, snap.finished_at = "failed", error[:2000], utcnow()
    await session.commit()


async def count_snapshot_chunks(session: AsyncSession, snapshot_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(CodeChunk)
                .join(SnapshotFile, SnapshotFile.blob_sha == CodeChunk.blob_sha)
                .where(SnapshotFile.snapshot_id == snapshot_id)
            )
        ).scalar_one()
    )


async def gc_snapshots(session: AsyncSession, repo_id: uuid.UUID, keep: int = 3) -> int:
    """Drop all but the `keep` newest ready snapshots (and stale failed/building ones), then
    delete chunks no retained snapshot references. Returns deleted chunk count."""
    ready = (
        await session.execute(
            select(RepositorySnapshot.id)
            .where(
                RepositorySnapshot.repository_id == repo_id,
                RepositorySnapshot.status == "ready",
            )
            .order_by(RepositorySnapshot.finished_at.desc())
        )
    ).scalars().all()  # fmt: skip
    # Never drop a snapshot an unfinished review job is still using.
    in_use = set(
        (
            await session.execute(
                select(ReviewJob.snapshot_id).where(
                    ReviewJob.repository_id == repo_id,
                    ReviewJob.snapshot_id.is_not(None),
                    ReviewJob.status.notin_(["COMPLETED", "FAILED", "CANCELLED", "STALE"]),
                )
            )
        ).scalars()
    )
    drop = [sid for sid in ready[keep:] if sid not in in_use]
    cutoff = utcnow() - timedelta(hours=6)
    stale = (
        await session.execute(
            select(RepositorySnapshot.id).where(
                RepositorySnapshot.repository_id == repo_id,
                RepositorySnapshot.status != "ready",
                RepositorySnapshot.started_at < cutoff,
            )
        )
    ).scalars().all()  # fmt: skip
    drop += list(stale)
    if drop:
        await session.execute(
            update(ReviewJob).where(ReviewJob.snapshot_id.in_(drop)).values(snapshot_id=None)
        )
        await session.execute(delete(RepositorySnapshot).where(RepositorySnapshot.id.in_(drop)))
    referenced = select(SnapshotFile.blob_sha).join(
        RepositorySnapshot, RepositorySnapshot.id == SnapshotFile.snapshot_id
    ).where(RepositorySnapshot.repository_id == repo_id)  # fmt: skip
    res = await session.execute(
        delete(CodeChunk).where(
            CodeChunk.repository_id == repo_id, CodeChunk.blob_sha.notin_(referenced)
        )
    )
    await session.commit()
    return cast(CursorResult[Any], res).rowcount or 0


async def repositories_for_installation(
    session: AsyncSession, installation_id: int, github_repo_ids: list[int] | None = None
) -> list[Repository]:
    q = select(Repository).where(Repository.installation_id == installation_id)
    if github_repo_ids is not None:
        q = q.where(Repository.github_repo_id.in_(github_repo_ids))
    return list((await session.execute(q)).scalars())


async def delete_repository_data(session: AsyncSession, repo: Repository) -> None:
    """Privacy: remove every stored artefact derived from the repository's source."""
    job_ids = select(ReviewJob.id).where(ReviewJob.repository_id == repo.id)
    await session.execute(delete(ReviewFinding).where(ReviewFinding.review_job_id.in_(job_ids)))
    await session.execute(delete(ReviewJob).where(ReviewJob.repository_id == repo.id))
    await session.execute(delete(CodeChunk).where(CodeChunk.repository_id == repo.id))
    await session.execute(
        delete(RepositorySnapshot).where(RepositorySnapshot.repository_id == repo.id)
    )
    repo.deleted_at = utcnow()
    await session.commit()
