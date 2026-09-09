from collections.abc import AsyncIterator
from typing import Any

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes.reviews import get_gh_factory
from app.core.config import Settings, get_settings
from app.core.errors import PermanentError
from app.db.models import CodeChunk
from app.db.repositories import review_jobs as jobs
from app.db.session import get_session
from app.domain.states import ReviewStatus
from app.main import create_app
from app.retrieval.embeddings import HashEmbedder
from app.review.orchestrator import ReviewOrchestrator
from app.review.pipeline import NoopStage
from app.review.stages.index import IndexStage

from .test_indexing import BASE, FakeGH, count
from .test_orchestrator import make_job


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    server = fakeredis.FakeServer()

    def from_url(*_: Any, **__: Any) -> fakeredis.FakeAsyncRedis:
        return fakeredis.FakeAsyncRedis(server=server)

    monkeypatch.setattr("app.review.stages.index.Redis.from_url", from_url)


class FetchStub:
    """Stands in for FetchPRStage: installs a fake GitHub client and PR context."""

    status = ReviewStatus.FETCHING_PR

    def __init__(self, gh: FakeGH) -> None:
        self.gh = gh

    async def run(self, ctx: Any) -> None:
        from app.domain.pr import PullRequestContext

        ctx.gh = self.gh
        ctx.pr = PullRequestContext("o/r", 1, "t", "b", base_sha="c1", head_sha="h", author="a",
                                    installation_id=1)  # fmt: skip


async def test_index_stage_builds_snapshot_for_base_sha(sessionmaker, fake_redis) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    gh = FakeGH({"c1": BASE})
    stages = [FetchStub(gh), IndexStage(Settings(_env_file=None), HashEmbedder()),  # type: ignore[call-arg]
              NoopStage(ReviewStatus.RETRIEVING_CONTEXT), NoopStage(ReviewStatus.ANALYZING),
              NoopStage(ReviewStatus.VALIDATING), NoopStage(ReviewStatus.PUBLISHING)]  # fmt: skip
    assert await ReviewOrchestrator(sessionmaker, stages).run(job.id) == ReviewStatus.COMPLETED  # type: ignore[arg-type]
    async with sessionmaker() as s:
        j = await jobs.get_job(s, job.id)
    assert j and j.snapshot_id is not None
    assert j.usage["index_mode"] == "full" and j.usage["embedding_operations"] > 0
    assert await count(sessionmaker, CodeChunk) > 0


async def test_repo_too_large_degrades_instead_of_failing(sessionmaker, fake_redis) -> None:  # type: ignore[no-untyped-def]
    class Huge(FakeGH):
        async def download_tarball(self, *a: Any, **k: Any):  # type: ignore[no-untyped-def]
            raise PermanentError("too big", code="repo_too_large")

    job = await make_job(sessionmaker)
    stages = [FetchStub(Huge({"c1": BASE})), IndexStage(Settings(_env_file=None), HashEmbedder()),  # type: ignore[call-arg]
              NoopStage(ReviewStatus.RETRIEVING_CONTEXT), NoopStage(ReviewStatus.ANALYZING),
              NoopStage(ReviewStatus.VALIDATING), NoopStage(ReviewStatus.PUBLISHING)]  # fmt: skip
    assert await ReviewOrchestrator(sessionmaker, stages).run(job.id) == ReviewStatus.COMPLETED  # type: ignore[arg-type]
    async with sessionmaker() as s:
        j = await jobs.get_job(s, job.id)
    assert j and j.snapshot_id is None and j.scope["degraded"] is True
    assert j.scope["no_index_reason"] == "repo_too_large"


@pytest_asyncio.fixture
async def api(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as s:
            yield s

    class GH:
        async def get_commit_sha(self, *_: Any) -> str:
            return "c1"

    async def close() -> None:
        return None

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, api_key="k")  # type: ignore[call-arg]
    app.dependency_overrides[get_gh_factory] = lambda: lambda _i: (GH(), close)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           headers={"X-API-Key": "k"}) as c:  # fmt: skip
        yield c


async def test_index_routes_and_delete(api, sessionmaker, fake_redis, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.indexing.service import IndexService

    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.api.routes.repositories.enqueue_index", lambda r, s: queued.append((r, s)) or True
    )
    assert (
        await api.post("/api/v1/repositories/o/r/index", json={})
    ).status_code == 404  # unknown repo

    await make_job(sessionmaker)  # registers repository o/r
    r = await api.post("/api/v1/repositories/o/r/index", json={})
    assert r.status_code == 202 and r.json()["commit_sha"] == "c1" and len(queued) == 1

    async with sessionmaker() as s:
        repo = await jobs.find_repository(s, "o", "r")
        assert repo
        await IndexService(s, HashEmbedder()).ensure_snapshot(repo, "c1", FakeGH({"c1": BASE}))  # type: ignore[arg-type]
    st = (await api.get("/api/v1/repositories/o/r/index/status")).json()
    assert st["snapshots"][0]["status"] == "ready" and st["snapshots"][0]["chunks"] > 0

    assert (await api.delete("/api/v1/repositories/o/r")).status_code == 204
    assert await count(sessionmaker, CodeChunk) == 0
    assert (await api.get("/api/v1/repositories/o/r/index/status")).json()["snapshots"] == []


async def test_repository_routes_require_api_key(api) -> None:  # type: ignore[no-untyped-def]
    api.headers.pop("X-API-Key")
    assert (await api.get("/api/v1/repositories/o/r/index/status")).status_code == 401
