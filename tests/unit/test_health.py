from fastapi.testclient import TestClient

from app.main import create_app


def test_health() -> None:
    with TestClient(create_app()) as c:
        r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_reports_unavailable_when_deps_down(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from app.core import config
    from app.db import session

    config.get_settings.cache_clear()
    session._engine = None
    with TestClient(create_app()) as c:
        r = c.get("/ready")
    config.get_settings.cache_clear()
    session._engine = None
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": "unavailable", "redis": "unavailable"}
