import asyncio
import uuid
from datetime import timedelta

import structlog
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.errors import TransientError
from app.core.locks import LockNotAcquiredError, redis_lock
from app.db.repositories import review_jobs as jobs
from app.db.session import make_worker_sessionmaker
from app.review.orchestrator import ReviewOrchestrator
from app.review.pipeline import default_stages
from app.workers.celery_app import celery_app

log = structlog.get_logger()


async def _run(job_id: uuid.UUID) -> str:
    engine, sessionmaker = make_worker_sessionmaker()
    redis = Redis.from_url(get_settings().redis_url)
    try:
        async with sessionmaker() as s:
            job = await jobs.get_job(s, job_id)
            if job is None:
                return "job_not_found"
            lock_key = f"review:{job.repository_id}:{job.pull_number}"
        try:
            async with redis_lock(redis, lock_key, ttl_s=1800):
                status = await ReviewOrchestrator(sessionmaker, default_stages()).run(job_id)
        except LockNotAcquiredError as exc:
            raise TransientError("another review of this PR is running", retry_after=30) from exc
        return status.value
    finally:
        await redis.aclose()
        await engine.dispose()


@celery_app.task(
    name="app.workers.review_tasks.run_review",
    bind=True,
    max_retries=3,
    acks_late=True,
)
def run_review(self, job_id: str) -> str:  # type: ignore[no-untyped-def]
    try:
        return asyncio.run(_run(uuid.UUID(job_id)))
    except TransientError as exc:
        delay = exc.retry_after or min(300, 15 * 2**self.request.retries)
        raise self.retry(exc=exc, countdown=delay) from exc


@celery_app.task(name="app.workers.review_tasks.requeue_orphaned_jobs")
def requeue_orphaned_jobs() -> int:
    """Outbox sweeper: QUEUED jobs whose enqueue was lost (broker down, worker crash)."""

    async def _sweep() -> list[uuid.UUID]:
        engine, sessionmaker = make_worker_sessionmaker()
        try:
            async with sessionmaker() as s:
                return await jobs.stale_queued_jobs(s, timedelta(minutes=5))
        finally:
            await engine.dispose()

    ids = asyncio.run(_sweep())
    for job_id in ids:
        run_review.delay(str(job_id))
    return len(ids)
