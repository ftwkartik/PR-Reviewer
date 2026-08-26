import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes.webhooks import get_enqueue
from app.core.config import Settings, get_settings
from app.db.models import ReviewJob, WebhookDelivery
from app.db.session import get_session
from app.main import create_app

SECRET = "whsec"


def payload(action: str = "opened", sha: str = "a" * 40, draft: bool = False) -> dict[str, Any]:
    return {
        "action": action,
        "number": 7,
        "pull_request": {
            "number": 7,
            "draft": draft,
            "head": {"sha": sha},
            "base": {"sha": "b" * 40},
        },
        "repository": {"id": 99, "name": "demo", "owner": {"login": "acme"}, "private": True},
        "installation": {"id": 555},
    }


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[AsyncClient, list[UUID]]]:
    app = create_app()
    enqueued: list[UUID] = []

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        github_webhook_secret=SECRET,
        api_key="k",  # type: ignore[call-arg]
    )
    app.dependency_overrides[get_enqueue] = lambda: lambda jid: enqueued.append(jid) or True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, enqueued


async def post(
    c: AsyncClient,
    body: dict[str, Any],
    delivery: str,
    event: str = "pull_request",
    signature: str | None = None,
):  # type: ignore[no-untyped-def]
    raw = json.dumps(body).encode()
    return await c.post(
        "/webhooks/github", content=raw,
        headers={"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
                 "X-Hub-Signature-256": signature or sign(raw), "Content-Type": "application/json"},
    )  # fmt: skip


async def test_opened_creates_job_and_enqueues(client, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    c, enqueued = client
    r = await post(c, payload(), "d1")
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert enqueued == [UUID(r.json()["review_id"])]
    async with sessionmaker() as s:
        job = (await s.execute(select(ReviewJob))).scalar_one()
        assert (job.status, job.head_sha, job.pull_number) == ("QUEUED", "a" * 40, 7)


async def test_bad_signature_rejected(client) -> None:  # type: ignore[no-untyped-def]
    c, enqueued = client
    r = await post(c, payload(), "d1", signature="sha256=" + "0" * 64)
    assert r.status_code == 401 and not enqueued


async def test_duplicate_delivery_is_idempotent(client, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    c, enqueued = client
    await post(c, payload(), "same")
    r = await post(c, payload(), "same")
    assert r.json()["status"] == "duplicate" and len(enqueued) == 1
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(ReviewJob))).scalar_one() == 1
        assert (
            await s.execute(select(func.count()).select_from(WebhookDelivery))
        ).scalar_one() == 1


async def test_same_head_different_delivery_does_not_duplicate_job(client) -> None:  # type: ignore[no-untyped-def]
    c, enqueued = client
    await post(c, payload(), "d1")
    r = await post(c, payload(action="reopened"), "d2")
    assert r.json()["status"] == "duplicate" and len(enqueued) == 1


async def test_synchronize_supersedes_older_job(client, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    await post(c, payload(sha="a" * 40), "d1")
    await post(c, payload(action="synchronize", sha="c" * 40), "d2")
    async with sessionmaker() as s:
        jobs = {j.head_sha: j for j in (await s.execute(select(ReviewJob))).scalars()}
    assert jobs["a" * 40].cancel_requested is True
    assert jobs["c" * 40].cancel_requested is False


@pytest.mark.parametrize(
    ("event", "body"),
    [
        ("issues", payload()),
        ("pull_request", payload(action="closed")),
        ("pull_request", payload(draft=True)),
    ],
)
async def test_ignored_events(client, event, body) -> None:  # type: ignore[no-untyped-def]
    c, enqueued = client
    r = await post(c, body, "dx", event=event)
    assert r.status_code == 202 and r.json()["status"] == "ignored" and not enqueued


async def test_ping(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    r = await post(c, {"zen": "x"}, "dp", event="ping")
    assert r.json() == {"status": "pong"}


async def test_get_review_requires_api_key(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    r = await post(c, payload(), "d1")
    rid = r.json()["review_id"]
    assert (await c.get(f"/api/v1/reviews/{rid}")).status_code == 401
    ok = await c.get(f"/api/v1/reviews/{rid}", headers={"X-API-Key": "k"})
    assert ok.status_code == 200 and ok.json()["status"] == "QUEUED"


async def test_manual_review_api(client, sessionmaker) -> None:  # type: ignore[no-untyped-def]
    import httpx
    import respx

    from app.api.routes.reviews import get_enqueue_fn, get_gh_factory
    from app.github.client import GitHubClient

    c, _ = client
    app = c._transport.app  # type: ignore[attr-defined]
    queued: list[UUID] = []

    class T:
        async def token_for(self, _: int) -> str:
            return "t"

    async def close() -> None:
        return None

    http = httpx.AsyncClient()
    app.dependency_overrides[get_gh_factory] = lambda: (
        lambda inst: (GitHubClient(http, T(), inst, "https://api.github.com"), close)  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_enqueue_fn] = lambda: lambda jid: queued.append(jid) or True
    pr = {"number": 4, "head": {"sha": "d" * 40},
          "base": {"sha": "e" * 40, "repo": {"id": 77, "default_branch": "main", "private": False}}}  # fmt: skip
    with respx.mock(base_url="https://api.github.com") as m:
        m.get("/repos/acme/demo/pulls/4").respond(200, json=pr)
        h = {"X-API-Key": "k"}
        body = {"repository": "acme/demo", "pull_request": 4}
        r = await c.post("/api/v1/reviews", json=body, headers=h)
        assert r.status_code == 422  # unknown repo needs installation_id
        r = await c.post("/api/v1/reviews", json=body | {"installation_id": 5}, headers=h)
        assert r.status_code == 202 and r.json()["status"] == "queued" and len(queued) == 1
        again = await c.post("/api/v1/reviews", json=body, headers=h)
        assert again.json()["existing"] is True and len(queued) == 1
        assert (await c.post("/api/v1/reviews", json=body)).status_code == 401
    await http.aclose()
