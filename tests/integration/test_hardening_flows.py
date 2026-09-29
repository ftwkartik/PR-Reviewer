import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes.webhooks import get_enqueue
from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, Repository, ReviewFinding, ReviewJob
from app.db.models.base import utcnow
from app.db.repositories import review_jobs as jobs
from app.db.session import get_session
from app.domain.review import ReviewResult
from app.domain.states import ReviewStatus
from app.indexing.service import IndexService
from app.llm.fake import FakeProvider
from app.main import create_app
from app.retrieval.embeddings import HashEmbedder
from app.review.orchestrator import ReviewOrchestrator
from app.review.pipeline import NoopStage
from app.review.stages.analyze import AnalyzeStage
from app.workers.maintenance import _purge
from tests.fixtures.mini_repo import FILES

from .test_analyze_validate import setup_stage
from .test_indexing import FakeGH
from .test_orchestrator import make_job

SECRET = "s"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[AsyncClient, list[Any], dict[str, Any]]]:
    app = create_app()
    queued: list[Any] = []
    cfg: dict[str, Any] = {"github_webhook_secret": SECRET}

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **cfg)  # type: ignore[call-arg]
    app.dependency_overrides[get_enqueue] = lambda: lambda jid: queued.append(jid) or True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, queued, cfg


async def post(c: AsyncClient, event: str, body: dict[str, Any], delivery: str):  # type: ignore[no-untyped-def]
    raw = json.dumps(body).encode()
    return await c.post("/webhooks/github", content=raw, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sign(raw)})  # fmt: skip


def pr_payload(owner: str = "acme") -> dict[str, Any]:
    return {"action": "opened", "number": 1,
            "pull_request": {"number": 1, "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}},
            "repository": {"id": 5, "name": "r", "owner": {"login": owner}}, "installation": {"id": 9}}  # fmt: skip


async def test_disallowed_owner_is_ignored_and_never_queued(client) -> None:  # type: ignore[no-untyped-def]
    c, queued, cfg = client
    cfg["allowed_owners"] = "acme"
    r = await post(c, "pull_request", pr_payload("stranger"), "d1")
    assert r.json() == {"status": "ignored", "reason": "owner_not_allowed"} and not queued
    assert (await post(c, "pull_request", pr_payload("acme"), "d2")).json()["status"] == "queued"


async def test_uninstall_purges_all_derived_data(client, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    c, queued, _ = client
    await post(c, "pull_request", pr_payload(), "d1")  # registers repo + job
    async with sessionmaker() as s:
        repo = (await s.execute(select(Repository))).scalar_one()
        await IndexService(s, HashEmbedder()).ensure_snapshot(repo, "base", FakeGH({"base": FILES}))  # type: ignore[arg-type]
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(CodeChunk))).scalar_one() > 0

    r = await post(c, "installation", {"action": "deleted", "installation": {"id": 9}}, "d2")
    assert r.json() == {"status": "purged", "repositories": 1}
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(CodeChunk))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(ReviewJob))).scalar_one() == 0
        assert (await s.execute(select(Repository))).scalar_one().deleted_at is not None
    assert (
        await post(c, "installation", {"action": "deleted", "installation": {"id": 9}}, "d2")
    ).json() == {"status": "duplicate"}


async def test_single_repo_removal_only_purges_that_repo(client, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    c, _, _ = client
    async with sessionmaker() as s:
        for rid, name in [(5, "keep"), (6, "drop")]:
            await jobs.upsert_repository(s, github_repo_id=rid, owner="acme", name=name, installation_id=9,
                                         default_branch="main", private=True)  # fmt: skip
        await s.commit()
    body = {"action": "removed", "installation": {"id": 9}, "repositories_removed": [{"id": 6}]}
    assert (await post(c, "installation_repositories", body, "d3")).json()["repositories"] == 1
    async with sessionmaker() as s:
        rows = {r.name: r.deleted_at for r in (await s.execute(select(Repository))).scalars()}
    assert rows["keep"] is None and rows["drop"] is not None


async def test_ignored_installation_actions(client) -> None:  # type: ignore[no-untyped-def]
    c, _, _ = client
    r = await post(c, "installation", {"action": "created", "installation": {"id": 9}}, "d4")
    assert r.json()["status"] == "ignored"


async def run_analyze(sm, job, cap: float):  # type: ignore[no-untyped-def]
    provider = FakeProvider(lambda r: ReviewResult(summary="ok", overall_risk="none", findings=[]))
    s = Settings(_env_file=None, daily_budget_usd=cap)  # type: ignore[call-arg]
    stages = [setup_stage(), NoopStage(ReviewStatus.INDEXING), NoopStage(ReviewStatus.RETRIEVING_CONTEXT),
              AnalyzeStage(s, provider), NoopStage(ReviewStatus.VALIDATING), NoopStage(ReviewStatus.PUBLISHING)]  # fmt: skip
    return await ReviewOrchestrator(sm, stages).run(job.id), provider  # type: ignore[arg-type]


async def test_daily_budget_cap_stops_spend_before_any_model_call(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    old = await make_job(sessionmaker, head="1" * 40)
    async with sessionmaker() as s:
        await s.execute(
            update(ReviewJob).where(ReviewJob.id == old.id).values(usage={"est_cost_usd": 4.5})
        )
        await s.commit()
    job = await make_job(sessionmaker, head="2" * 40)
    status, provider = await run_analyze(sessionmaker, job, cap=4.0)
    assert status == ReviewStatus.FAILED and provider.requests == []
    async with sessionmaker() as s:
        j = await jobs.get_job(s, job.id)
    assert j and j.error_code == "budget_exceeded"


async def test_budget_cap_off_or_not_reached_allows_review(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker, head="3" * 40)
    status, provider = await run_analyze(sessionmaker, job, cap=0)
    assert status == ReviewStatus.COMPLETED and len(provider.requests) == 1


async def test_old_spend_does_not_count(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    old = await make_job(sessionmaker, head="4" * 40)
    async with sessionmaker() as s:
        await s.execute(update(ReviewJob).where(ReviewJob.id == old.id).values(
            usage={"est_cost_usd": 99.0}, created_at=utcnow() - timedelta(hours=30)))  # fmt: skip
        await s.commit()
    job = await make_job(sessionmaker, head="5" * 40)
    assert (await run_analyze(sessionmaker, job, cap=1.0))[0] == ReviewStatus.COMPLETED


async def test_retention_purge_deletes_only_expired_findings(sessionmaker, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    async with sessionmaker() as s:
        for title, age in [("old one", 100), ("recent", 1)]:
            s.add(ReviewFinding(review_job_id=job.id, path="a.py", line_start=1, line_end=1, severity="high",
                                category="correctness", title=title, explanation="e", evidence_quote="q",
                                confidence=0.9, fingerprint=title, created_at=utcnow() - timedelta(days=age)))  # fmt: skip
        await s.commit()
    from app.db import session as dbsession

    monkeypatch.setattr(
        dbsession, "make_worker_sessionmaker", lambda: (sessionmaker.kw["bind"], sessionmaker)
    )
    monkeypatch.setattr(
        "app.workers.maintenance.make_worker_sessionmaker",
        lambda: (sessionmaker.kw["bind"], sessionmaker),
    )
    assert await _purge(90) == 1
    async with sessionmaker() as s:
        assert [f.title for f in (await s.execute(select(ReviewFinding))).scalars()] == ["recent"]
