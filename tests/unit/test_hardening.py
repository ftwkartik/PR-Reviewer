import fakeredis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from app.api.ratelimit import get_redis, rate_limit
from app.core.config import Settings, get_settings, is_owner_allowed
from app.main import create_app
from app.observability.metrics import REDACTIONS, timed_stage
from app.review.secrets import redact
from app.review.validator import Corpus, evidence_exists

AWS = "AKIAIOSFODNN7EXAMPLE"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_redacts_common_credentials_but_keeps_context() -> None:
    src = f'aws = "{AWS}"\ntoken = "{GH}"\nDB_PASSWORD = "hunter2hunter2hunter2"\nname = "alice"\n'
    out = redact(src)
    assert AWS not in out and GH not in out and "hunter2" not in out
    assert "DB_PASSWORD" in out and 'name = "alice"' in out and "[REDACTED:" in out


def test_redaction_leaves_ordinary_code_alone() -> None:
    code = 'SECRET = "change-me"\nresult = compute(token_count, max_tokens=3)\npassword_field = form.password\n'
    assert redact(code) == code  # short placeholders and identifiers are not secrets


def test_private_key_and_blobs_redacted() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 100 + "\n-----END RSA PRIVATE KEY-----"
    out = redact(pem)
    assert "BEGIN RSA PRIVATE KEY" not in out and "A" * 50 not in out


def test_redaction_counts_metric() -> None:
    before = REDACTIONS.labels("aws_access_key")._value.get()
    redact(AWS)
    assert REDACTIONS.labels("aws_access_key")._value.get() == before + 1


def test_validator_corpus_matches_redacted_quotes() -> None:
    """A model that saw `[REDACTED]` and quotes it must still pass the evidence check."""
    from tests.helpers import changed_file

    cf = changed_file("a.py", "x = 1\n", f'API_TOKEN = "{GH}"\n')
    corpus = Corpus.build([cf], None)
    quote = redact(f'API_TOKEN = "{GH}"')
    assert "[REDACTED" in quote and evidence_exists(quote, corpus)
    assert GH not in corpus.blob


def test_owner_allowlist() -> None:
    s = Settings(_env_file=None, allowed_owners="Acme, widgets")  # type: ignore[call-arg]
    assert (
        is_owner_allowed(s, "acme")
        and is_owner_allowed(s, "WIDGETS")
        and not is_owner_allowed(s, "evil")
    )
    assert is_owner_allowed(Settings(_env_file=None), "anyone")  # type: ignore[call-arg]


def limited_app(limit: int, redis: object) -> FastAPI:
    from fastapi import Depends

    app = FastAPI()

    @app.get("/x", dependencies=[Depends(rate_limit)])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, rate_limit_per_minute=limit
    )  # type: ignore[call-arg]
    app.dependency_overrides[get_redis] = lambda: redis
    return app


def test_rate_limit_blocks_after_limit_per_key() -> None:
    r = fakeredis.FakeAsyncRedis()
    with TestClient(limited_app(3, r)) as c:
        codes = [c.get("/x", headers={"X-API-Key": "a"}).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        blocked = c.get("/x", headers={"X-API-Key": "a"})
        assert blocked.headers["retry-after"].isdigit()
        assert c.get("/x", headers={"X-API-Key": "other"}).status_code == 200  # separate bucket


def test_rate_limit_disabled_and_fails_open() -> None:
    with TestClient(limited_app(0, fakeredis.FakeAsyncRedis())) as c:
        assert all(c.get("/x").status_code == 200 for _ in range(20))

    class Broken:
        async def incr(self, *_: object) -> int:
            raise ConnectionError("redis down")

    with TestClient(limited_app(1, Broken())) as c:
        assert [c.get("/x").status_code for _ in range(3)] == [200, 200, 200]


def test_metrics_endpoint_and_token() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, metrics_token="s3cret"
    )  # type: ignore[call-arg]
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = c.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert (
            ok.status_code == 200
            and "review_jobs_total" in ok.text
            or "review_stage_seconds" in ok.text
        )
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)  # type: ignore[call-arg]
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 200  # open when no token is configured


def test_stage_timer_records_histogram() -> None:
    with timed_stage("UNIT_TEST_STAGE"):
        pass
    assert b'review_stage_seconds_count{stage="UNIT_TEST_STAGE"} 1.0' in generate_latest()
