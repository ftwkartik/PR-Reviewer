"""Repository indexing: commit SHA -> snapshot (manifest of blobs) + content-addressed chunks.

Two build paths, same result:
  * incremental: parent snapshot + GitHub compare API -> only changed files are fetched
  * full: tarball at the commit, safely extracted
In both, a blob already chunked+embedded for this repository (same git blob SHA and chunker
version) is reused, so a full rebuild after a one-file commit still embeds a single file.
"""

import hashlib
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermanentError, TransientError
from app.db.models import Repository, RepositorySnapshot
from app.db.repositories import index as repo_index
from app.domain.code import CHUNKER_VERSION, CodeChunk
from app.github import classify
from app.github.client import GitHubClient
from app.github.tarball import safe_extract
from app.indexing.chunker import chunk_file
from app.indexing.ignore import IgnoreRules, should_index
from app.retrieval.embeddings import EmbeddingProvider

log = structlog.get_logger()

MAX_INCREMENTAL_FILES = 200
MAX_CHUNKS_PER_EMBED_CALL = 256


def git_blob_sha(data: bytes) -> str:
    """Same SHA-1 git (and GitHub's trees/compare API) assigns to a file's contents."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()  # noqa: S324


class FileSource(Protocol):
    def paths(self) -> list[str]: ...
    def read(self, path: str) -> bytes: ...


class DirSource:
    def __init__(self, root: Path, paths: list[str]) -> None:
        self._root, self._paths = root, paths

    def paths(self) -> list[str]:
        return self._paths

    def read(self, path: str) -> bytes:
        return (self._root / path).read_bytes()


class MemorySource:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def paths(self) -> list[str]:
        return list(self._files)

    def read(self, path: str) -> bytes:
        return self._files[path]


@dataclass
class IndexStats:
    mode: str = "cached"  # cached | incremental | full
    files_indexed: int = 0
    files_skipped: int = 0
    new_blobs: int = 0
    new_chunks: int = 0
    embeddings_computed: int = 0
    embeddings_cached: int = 0
    embed_tokens: int = 0
    duration_s: float = 0.0


class IndexService:
    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider) -> None:
        self._s = session
        self._embedder = embedder

    async def ensure_snapshot(
        self, repo: Repository, commit_sha: str, gh: GitHubClient
    ) -> tuple[RepositorySnapshot, IndexStats]:
        started = time.monotonic()
        model = self._embedder.model
        existing = await repo_index.get_snapshot(
            self._s, repo.id, commit_sha, model, CHUNKER_VERSION
        )
        if existing is not None and existing.status == "ready":
            return existing, IndexStats(mode="cached")

        parent = await repo_index.latest_ready_snapshot(
            self._s, repo.id, model, CHUNKER_VERSION, commit_sha
        )
        snap = await repo_index.get_or_create_snapshot(
            self._s, repo.id, commit_sha, model, CHUNKER_VERSION, parent.id if parent else None
        )
        if snap.status != "building":  # retry of a failed build
            snap.status = "building"
            await self._s.commit()
        stats = IndexStats()
        try:
            manifest = await self._build(repo, parent, commit_sha, gh, stats)
            await repo_index.write_manifest(self._s, snap.id, manifest)
            chunk_count = await repo_index.count_snapshot_chunks(self._s, snap.id)
            await repo_index.finalize_snapshot(
                self._s, snap, file_count=len(manifest), chunk_count=chunk_count
            )
        except (TransientError, PermanentError) as exc:
            await repo_index.fail_snapshot(self._s, snap, f"{exc.code}: {exc}")
            raise
        except Exception as exc:
            await repo_index.fail_snapshot(self._s, snap, repr(exc))
            raise
        stats.duration_s = time.monotonic() - started
        log.info("snapshot_ready", snapshot_id=str(snap.id), commit_sha=commit_sha, **vars(stats))
        return snap, stats

    async def _build(
        self,
        repo: Repository,
        parent: RepositorySnapshot | None,
        sha: str,
        gh: GitHubClient,
        stats: IndexStats,
    ) -> repo_index.Manifest:
        if parent is not None:
            plan = await self._plan_incremental(repo, parent, sha, gh)
            if plan is not None:
                stats.mode = "incremental"
                manifest, mem = plan
                await self._index_blobs(repo, manifest, mem, stats)
                return manifest
        stats.mode = "full"
        with tempfile.TemporaryDirectory(prefix="idx-") as tmp:
            manifest, src = await self._plan_full(repo, sha, gh, Path(tmp), stats)
            await self._index_blobs(repo, manifest, src, stats)
        return manifest

    # -- planning ------------------------------------------------------------------------
    async def _plan_incremental(
        self, repo: Repository, parent: RepositorySnapshot, sha: str, gh: GitHubClient
    ) -> tuple[repo_index.Manifest, MemorySource] | None:
        try:
            cmp = await gh.compare(repo.owner, repo.name, parent.commit_sha, sha)
        except PermanentError:
            return None  # e.g. force-pushed away parent: fall back to a full build
        files = cmp.get("files", [])
        if cmp.get("status") not in {"ahead", "identical"} or len(files) >= MAX_INCREMENTAL_FILES:
            return None
        manifest = await repo_index.load_manifest(self._s, parent.id)
        gitignore = await gh.get_file_content(repo.owner, repo.name, ".gitignore", sha)
        rules = IgnoreRules(gitignore.decode("utf-8", "ignore") if gitignore else None)
        loaded: dict[str, bytes] = {}
        for f in files:
            path, status = f["filename"], f["status"]
            if f.get("previous_filename"):
                manifest.pop(f["previous_filename"], None)
            if status == "removed":
                manifest.pop(path, None)
                continue
            manifest.pop(path, None)
            if rules.is_ignored(path):
                continue
            data = await gh.get_file_content(repo.owner, repo.name, path, sha)
            if data is None:
                continue
            ok, _ = should_index(path, data, rules)
            if ok:
                loaded[path] = data
                manifest[path] = (git_blob_sha(data), classify.detect_language(path))
        return manifest, MemorySource(loaded)

    async def _plan_full(
        self, repo: Repository, sha: str, gh: GitHubClient, tmp: Path, stats: IndexStats
    ) -> tuple[repo_index.Manifest, DirSource]:
        archive = await gh.download_tarball(repo.owner, repo.name, sha, tmp / "repo.tar.gz")
        root = tmp / "src"
        extracted = safe_extract(archive, root)
        archive.unlink(missing_ok=True)
        gi = root / ".gitignore"
        rules = IgnoreRules(gi.read_text("utf-8", "ignore") if gi.exists() else None)
        manifest: repo_index.Manifest = {}
        for path in extracted:
            data = (root / path).read_bytes()
            ok, _ = should_index(path, data, rules)
            if not ok:
                stats.files_skipped += 1
                continue
            manifest[path] = (git_blob_sha(data), classify.detect_language(path))
        return manifest, DirSource(root, list(manifest))

    # -- chunk + embed -----------------------------------------------------------------
    async def _index_blobs(
        self, repo: Repository, manifest: repo_index.Manifest, source: FileSource, stats: IndexStats
    ) -> None:
        stats.files_indexed = len(manifest)
        all_blobs = sorted({b for b, _ in manifest.values()})
        have = await repo_index.existing_blob_shas(self._s, repo.id, CHUNKER_VERSION, all_blobs)
        todo: dict[str, tuple[str, str]] = {}  # blob -> (path, language)
        available = set(source.paths())  # incremental sources only hold changed files
        for path, (blob, lang) in manifest.items():
            if path in available and blob not in have and blob not in todo and lang:
                todo[blob] = (path, lang)
        stats.new_blobs = len(todo)
        batch: list[CodeChunk] = []
        for blob, (path, lang) in todo.items():
            text = source.read(path).decode("utf-8", errors="replace")
            batch += chunk_file(path, lang, text, blob)
            if len(batch) >= MAX_CHUNKS_PER_EMBED_CALL:
                await self._embed_and_store(repo, batch, stats)
                batch = []
        if batch:
            await self._embed_and_store(repo, batch, stats)

    async def _embed_and_store(
        self, repo: Repository, chunks: list[CodeChunk], stats: IndexStats
    ) -> None:
        model = self._embedder.model
        texts = [c.embedding_text() for c in chunks]
        keys = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
        cached = await repo_index.cached_embeddings(self._s, list(set(keys)), model)
        missing = sorted({k for k in keys if k not in cached})
        if missing:
            by_key = dict(zip(keys, texts, strict=True))
            result = await self._embedder.embed_documents([by_key[k] for k in missing])
            fresh = dict(zip(missing, result.vectors, strict=True))
            await repo_index.store_embedding_cache(self._s, fresh, model)
            cached.update(fresh)
            stats.embeddings_computed += len(missing)
            stats.embed_tokens += result.tokens
        stats.embeddings_cached += len(keys) - len(missing)
        pairs = [(c, cached.get(k)) for c, k in zip(chunks, keys, strict=True)]
        stats.new_chunks += await repo_index.insert_chunks(
            self._s, repo.id, CHUNKER_VERSION, model, pairs
        )
        await self._s.commit()
