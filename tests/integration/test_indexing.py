import io
import tarfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import PermanentError
from app.db.models import CodeChunk, Repository, RepositorySnapshot, SnapshotFile
from app.db.repositories import index as repo_index
from app.db.repositories import review_jobs as jobs
from app.domain.code import CHUNKER_VERSION
from app.indexing.service import IndexService, git_blob_sha
from app.retrieval.embeddings import EmbeddingResult, HashEmbedder

SESSION_PY = '''"""Sessions."""
from .tokens import decode_token


class SessionManager:
    def verify_token(self, raw):
        return decode_token(raw)


def cleanup():
    return 1
'''
TOKENS_PY = "def decode_token(raw):\n    return {'sid': raw}\n"
README = "# Demo\n\n## Conventions\nAll auth changes need tests.\n"

BASE = {
    "app/auth/session.py": SESSION_PY,
    "app/auth/tokens.py": TOKENS_PY,
    "README.md": README,
    "node_modules/x/index.js": "module.exports = 1",
    "logo.png": "\x89PNG\x00\x00",
    ".gitignore": "secrets/\n",
    "secrets/keys.py": "KEY = 'x'\n",
}


class CountingEmbedder(HashEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.documents_embedded = 0
        self.calls = 0

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        self.documents_embedded += len(texts)
        return await super().embed_documents(texts)


class FakeGH:
    """Stands in for GitHubClient: serves a dict of {sha: {path: text}} as repository history."""

    def __init__(self, commits: dict[str, dict[str, str]], compare_status: str = "ahead") -> None:
        self.commits, self.compare_status = commits, compare_status
        self.tarballs = 0
        self.contents_calls = 0

    async def download_tarball(self, owner: str, repo: str, sha: str, dest: Path, **_: Any) -> Path:
        self.tarballs += 1
        with tarfile.open(dest, "w:gz") as tar:
            for path, text in self.commits[sha].items():
                data = text.encode("latin-1")
                info = tarfile.TarInfo(f"{owner}-{repo}-{sha[:7]}/{path}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return dest

    async def compare(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
        old, new = self.commits[base], self.commits[head]
        files = []
        for p in sorted(set(old) | set(new)):
            if p not in new:
                files.append({"filename": p, "status": "removed"})
            elif p not in old:
                files.append(
                    {"filename": p, "status": "added", "sha": git_blob_sha(new[p].encode())}
                )
            elif old[p] != new[p]:
                files.append({"filename": p, "status": "modified"})
        return {"status": self.compare_status, "files": files}

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> bytes | None:
        self.contents_calls += 1
        text = self.commits[ref].get(path)
        return text.encode("latin-1") if text is not None else None


async def make_repo(sm: async_sessionmaker[AsyncSession]) -> Repository:
    async with sm() as s:
        repo = await jobs.upsert_repository(
            s, github_repo_id=1, owner="o", name="r", installation_id=1,
            default_branch="main", private=True,
        )  # fmt: skip
        await s.commit()
        return repo


async def count(sm: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sm() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


async def index(sm, repo, sha, gh, embedder):  # type: ignore[no-untyped-def]
    async with sm() as s:
        return await IndexService(s, embedder).ensure_snapshot(repo, sha, gh)


async def test_full_build_respects_ignore_rules(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()
    gh = FakeGH({"c1": BASE})
    snap, stats = await index(sessionmaker, repo, "c1", gh, emb)
    assert stats.mode == "full" and snap.status == "ready"
    async with sessionmaker() as s:
        paths = set(
            (
                await s.execute(
                    select(SnapshotFile.path).where(SnapshotFile.snapshot_id == snap.id)
                )
            ).scalars()
        )
    assert paths == {"app/auth/session.py", "app/auth/tokens.py", "README.md", ".gitignore"} - {
        ".gitignore"
    }
    assert emb.documents_embedded == stats.embeddings_computed > 0
    assert await count(sessionmaker, CodeChunk) == stats.new_chunks


async def test_second_call_same_commit_is_cached(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()
    gh = FakeGH({"c1": BASE})
    await index(sessionmaker, repo, "c1", gh, emb)
    before = emb.calls
    _, stats = await index(sessionmaker, repo, "c1", gh, emb)
    assert stats.mode == "cached" and emb.calls == before and gh.tarballs == 1


async def test_incremental_embeds_only_changed_file(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()
    changed = {**BASE, "app/auth/tokens.py": TOKENS_PY + "\n\ndef encode_token(x):\n    return x\n"}
    gh = FakeGH({"c1": BASE, "c2": changed})
    await index(sessionmaker, repo, "c1", gh, emb)
    embedded_after_first = emb.documents_embedded
    snap2, stats = await index(sessionmaker, repo, "c2", gh, emb)
    assert stats.mode == "incremental" and gh.tarballs == 1
    assert stats.new_blobs == 1  # only tokens.py
    assert 0 < emb.documents_embedded - embedded_after_first <= 3
    async with sessionmaker() as s:
        man = await repo_index.load_manifest(s, snap2.id)
    assert man["app/auth/tokens.py"][0] == git_blob_sha(changed["app/auth/tokens.py"].encode())
    assert man["app/auth/session.py"][0] == git_blob_sha(SESSION_PY.encode())  # reused


async def test_incremental_handles_delete_and_add(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()
    c2 = {k: v for k, v in BASE.items() if k != "app/auth/tokens.py"} | {
        "app/new.py": "def n():\n    return 1\n"
    }
    gh = FakeGH({"c1": BASE, "c2": c2})
    await index(sessionmaker, repo, "c1", gh, emb)
    snap2, _ = await index(sessionmaker, repo, "c2", gh, emb)
    async with sessionmaker() as s:
        man = await repo_index.load_manifest(s, snap2.id)
    assert "app/auth/tokens.py" not in man and "app/new.py" in man


async def test_diverged_history_falls_back_to_full_build(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()
    gh = FakeGH(
        {"c1": BASE, "c2": {**BASE, "README.md": README + "more\n"}}, compare_status="diverged"
    )
    await index(sessionmaker, repo, "c1", gh, emb)
    embedded = emb.documents_embedded
    _, stats = await index(sessionmaker, repo, "c2", gh, emb)
    assert stats.mode == "full" and gh.tarballs == 2
    assert (
        stats.new_blobs == 1 and emb.documents_embedded - embedded <= 3
    )  # content addressing still saves work


async def test_old_chunks_not_reachable_from_new_snapshot(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    """Stale-embedding guard: a snapshot only sees chunks reachable via its own manifest."""
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()
    new_tokens = "def decode_token_v2(raw):\n    return raw\n"
    gh = FakeGH({"c1": BASE, "c2": {**BASE, "app/auth/tokens.py": new_tokens}})
    await index(sessionmaker, repo, "c1", gh, emb)
    snap2, _ = await index(sessionmaker, repo, "c2", gh, emb)
    async with sessionmaker() as s:
        q = (
            select(CodeChunk.qualified_name)
            .join(SnapshotFile, SnapshotFile.blob_sha == CodeChunk.blob_sha)
            .where(SnapshotFile.snapshot_id == snap2.id)
        )
        names = set((await s.execute(q)).scalars())
    assert "decode_token_v2" in names and "decode_token" not in names


async def test_gc_removes_unreferenced_chunks(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()
    gh = FakeGH({"c1": BASE, "c2": {**BASE, "app/auth/tokens.py": "def other():\n    return 2\n"}})
    await index(sessionmaker, repo, "c1", gh, emb)
    await index(sessionmaker, repo, "c2", gh, emb)
    async with sessionmaker() as s:
        assert await repo_index.gc_snapshots(s, repo.id, keep=3) == 0
        assert await repo_index.gc_snapshots(s, repo.id, keep=1) > 0
    async with sessionmaker() as s:
        names = set((await s.execute(select(CodeChunk.qualified_name))).scalars())
        snaps = (await s.execute(select(func.count()).select_from(RepositorySnapshot))).scalar_one()
    assert "decode_token" not in names and "other" in names and snaps == 1


async def test_failed_build_marked_failed_and_retryable(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()

    class Boom(FakeGH):
        async def download_tarball(self, *a: Any, **k: Any) -> Path:
            raise PermanentError("nope", code="github_forbidden")

    with pytest.raises(PermanentError):
        await index(sessionmaker, repo, "c1", Boom({"c1": BASE}), emb)
    async with sessionmaker() as s:
        snap = await repo_index.get_snapshot(s, repo.id, "c1", emb.model, CHUNKER_VERSION)
        assert snap and snap.status == "failed" and "github_forbidden" in (snap.error or "")
    snap, _ = await index(sessionmaker, repo, "c1", FakeGH({"c1": BASE}), emb)  # retry works
    assert snap.status == "ready"


async def test_delete_repository_data(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, emb = await make_repo(sessionmaker), CountingEmbedder()
    await index(sessionmaker, repo, "c1", FakeGH({"c1": BASE}), emb)
    async with sessionmaker() as s:
        r = await s.get(Repository, repo.id)
        assert r
        await repo_index.delete_repository_data(s, r)
    assert (
        await count(sessionmaker, CodeChunk) == 0
        and await count(sessionmaker, RepositorySnapshot) == 0
    )
