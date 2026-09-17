import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.retrieval import Tier
from app.indexing.service import IndexService
from app.retrieval.context_builder import ContextBuilder, ContextRequest
from app.retrieval.embeddings import HashEmbedder
from app.retrieval.overlay import build_overlay
from app.retrieval.store import ChunkStore
from tests.fixtures.mini_repo import FILES, SESSION_BASE, SESSION_HEAD
from tests.helpers import changed_file

from .test_indexing import FakeGH
from .test_orchestrator import make_job  # noqa: F401  (registers nothing; kept for parity)

PATH = "app/auth/session.py"


async def setup(
    sm: async_sessionmaker[AsyncSession], gh_files: dict[str, str] | None = None, repo_id: int = 1
):  # type: ignore[no-untyped-def]
    from app.db.repositories import review_jobs as jobs

    async with sm() as s:
        repo = await jobs.upsert_repository(
            s, github_repo_id=repo_id, owner="o", name=f"r{repo_id}", installation_id=1,
            default_branch="main", private=True,
        )  # fmt: skip
        await s.commit()
    embedder = HashEmbedder()
    async with sm() as s:
        snap, _ = await IndexService(s, embedder).ensure_snapshot(
            repo,
            "base",
            FakeGH({"base": gh_files or FILES}),  # type: ignore[arg-type]
        )
    return repo, snap, embedder


async def build(
    sm, repo, snap, embedder, budget: int = 6000, title: str = "Touch session on verify"
):  # type: ignore[no-untyped-def]
    cf = changed_file(PATH, SESSION_BASE, SESSION_HEAD)

    async def fetch(_: str) -> bytes | None:
        return SESSION_HEAD.encode()

    overlay = await build_overlay([cf], fetch)
    async with sm() as s:
        store = ChunkStore(s, repo.id, snap.id, embedder.model)
        bundle = await ContextBuilder(store, embedder).build(
            ContextRequest([cf], overlay, {PATH}, title, budget)
        )
    return bundle


async def test_tier1_is_head_version_and_base_version_excluded(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, snap, emb = await setup(sessionmaker)
    bundle = await build(sessionmaker, repo, snap, emb)
    tier1 = [i for i in bundle.items if i.tier == Tier.IMMEDIATE]
    verify = next(i for i in tier1 if i.chunk.qualified_name == "SessionManager.verify_token")
    assert verify.chunk.origin == "head" and "session.touch()" in verify.chunk.content
    assert any(i.chunk.qualified_name == "SessionManager" for i in tier1)  # enclosing class
    assert not any(i.chunk.origin == "base" and i.chunk.path == PATH for i in bundle.items)


async def test_dependencies_resolved_from_imports_and_calls(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, snap, emb = await setup(sessionmaker)
    bundle = await build(sessionmaker, repo, snap, emb)
    t2 = {i.chunk.qualified_name: i for i in bundle.items if i.tier == Tier.DEPENDENCY}
    assert "decode_token" in t2 and t2["decode_token"].chunk.path == "app/auth/tokens.py"
    assert "decode_token_legacy" not in t2  # the decoy is not a dependency


async def test_callers_and_tests_found(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, snap, emb = await setup(sessionmaker)
    bundle = await build(sessionmaker, repo, snap, emb)
    usage = [i for i in bundle.items if i.tier == Tier.USAGE]
    assert any(i.chunk.qualified_name == "current_user" for i in usage)
    tests = [i for i in bundle.items if i.tier == Tier.VALIDATION and i.chunk.is_test]
    assert [i.chunk.qualified_name for i in tests] == ["test_expired_session_rejected"]


async def test_model_and_cleanup_evidence_retrieved(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    """The 'sessions expire but stay stored until cleanup' evidence the reviewer needs."""
    repo, snap, emb = await setup(sessionmaker)
    bundle = await build(sessionmaker, repo, snap, emb, title="Session expiry handling")
    names = {i.chunk.qualified_name for i in bundle.items}
    assert "Session.touch" in names  # definition of the method the change calls


async def test_conventions_and_config_included(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, snap, emb = await setup(sessionmaker)
    bundle = await build(sessionmaker, repo, snap, emb)
    assert any(i.tier == Tier.CONVENTIONS and i.chunk.path == "README.md" for i in bundle.items)


async def test_budget_respected_but_changed_code_kept(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, snap, emb = await setup(sessionmaker)
    bundle = await build(sessionmaker, repo, snap, emb, budget=150)
    assert any(i.tier == Tier.IMMEDIATE for i in bundle.items)
    non_t1 = sum(i.chunk.token_count for i in bundle.items if i.tier != Tier.IMMEDIATE)
    t1 = sum(i.chunk.token_count for i in bundle.items if i.tier == Tier.IMMEDIATE)
    assert bundle.tokens_used == t1 + non_t1 and non_t1 <= max(0, 150 - t1) + 1
    assert any(d.reason == "budget" for d in bundle.dropped)


async def test_ctx_ids_unique_and_stable_order(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, snap, emb = await setup(sessionmaker)
    a = await build(sessionmaker, repo, snap, emb)
    b = await build(sessionmaker, repo, snap, emb)
    ids = [i.ctx_id for i in a.items]
    assert len(ids) == len(set(ids)) and ids[0] == "c1"
    assert [(i.chunk.path, i.chunk.start_line) for i in a.items] == [
        (i.chunk.path, i.chunk.start_line) for i in b.items
    ]


async def test_tenant_isolation_same_blobs_in_another_repo(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo_a, snap_a, emb = await setup(sessionmaker, repo_id=1)
    repo_b, snap_b, _ = await setup(
        sessionmaker, repo_id=2
    )  # identical content => identical blob SHAs
    async with sessionmaker() as s:
        store = ChunkStore(s, repo_a.id, snap_a.id, emb.model)
        from sqlalchemy import select

        from app.db.models import CodeChunk

        ids_b = set(
            str(x)
            for x in (
                await s.execute(select(CodeChunk.id).where(CodeChunk.repository_id == repo_b.id))
            ).scalars()
        )
        found = await store.by_names(["decode_token"]) + await store.lexical("decode token")
    assert found and not ({c.chunk_id for c in found} & ids_b)


async def test_lexical_finds_dotted_identifiers(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    repo, snap, emb = await setup(sessionmaker)
    async with sessionmaker() as s:
        store = ChunkStore(s, repo.id, snap.id, emb.model)
        hits = await store.lexical("manager.verify_token")
    assert any(h.qualified_name == "current_user" for h in hits) or any(
        h.path == PATH for h in hits
    )


async def test_degraded_mode_without_index_still_gives_tier1(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    cf = changed_file(PATH, SESSION_BASE, SESSION_HEAD)

    async def fetch(_: str) -> bytes | None:
        return SESSION_HEAD.encode()

    overlay = await build_overlay([cf], fetch)
    bundle = await ContextBuilder(None).build(ContextRequest([cf], overlay, {PATH}, "t", 4000))
    assert bundle.items and all(i.tier <= Tier.DEPENDENCY for i in bundle.items)


@pytest.mark.parametrize("missing", [True, False])
async def test_overlay_handles_unfetchable_and_removed(missing: bool) -> None:
    cf = changed_file(PATH, SESSION_BASE, SESSION_HEAD)
    if not missing:
        cf.status = "removed"

    async def fetch(_: str) -> bytes | None:
        return None if missing else SESSION_HEAD.encode()

    assert (await build_overlay([cf], fetch))[PATH] == []
