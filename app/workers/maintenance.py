"""Periodic housekeeping: findings retention."""

import asyncio
from datetime import timedelta

import structlog
from sqlalchemy import delete

from app.core.config import get_settings
from app.db.models import ReviewFinding
from app.db.models.base import utcnow
from app.db.session import make_worker_sessionmaker
from app.workers.celery_app import celery_app

log = structlog.get_logger()


async def _purge(days: int) -> int:
    engine, sessionmaker = make_worker_sessionmaker()
    try:
        async with sessionmaker() as s:
            res = await s.execute(
                delete(ReviewFinding).where(
                    ReviewFinding.created_at < utcnow() - timedelta(days=days)
                )
            )
            await s.commit()
            return int(res.rowcount or 0)  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.maintenance.purge_expired_findings")
def purge_expired_findings() -> int:
    """Privacy: finding text can quote private code; keep it only FINDINGS_RETENTION_DAYS."""
    days = get_settings().findings_retention_days
    if days <= 0:
        return 0
    n = asyncio.run(_purge(days))
    log.info("findings_purged", deleted=n, retention_days=days)
    return n
