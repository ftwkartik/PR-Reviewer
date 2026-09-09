import asyncio
import uuid

import structlog
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.errors import PermanentError, TransientError
from app.core.locks import LockNotAcquiredError, redis_lock
from app.db.models import Repository
from app.db.repositories import index as repo_index
from app.db.session import make_worker_sessionmaker
from app.github.factory import make_github_client_factory
from app.indexing.service import IndexService
from app.retrieval.embeddings import make_embedder
from app.workers.celery_app import celery_app

log = structlog.get_logger()


async def _index(repo_id: uuid.UUID, commit_sha: str) -> str:
    settings = get_settings()
    engine, sessionmaker = make_worker_sessionmaker()
    redis = Redis.from_url(settings.redis_url)
    try:
        async with sessionmaker() as s:
            repo = await s.get(Repository, repo_id)
            if repo is None or repo.installation_id is None:
                raise PermanentError("repository unknown or not installed", code="repo_missing")
            gh, close = make_github_client_factory(settings)(repo.installation_id)
            try:
                async with redis_lock(redis, f"index:{repo.id}", ttl_s=1800, wait_s=300):
                    snap, stats = await IndexService(s, make_embedder(settings)).ensure_snapshot(
                        repo, commit_sha, gh
                    )
                    await repo_index.gc_snapshots(s, repo.id, keep=3)
            finally:
                await close()
            return f"{snap.status}:{stats.mode}"
    except LockNotAcquiredError as exc:
        raise TransientError("repository index is busy", retry_after=30) from exc
    finally:
        await redis.aclose()
        await engine.dispose()


@celery_app.task(name="app.workers.index_tasks.index_repository", bind=True, max_retries=3,
                 acks_late=True)  # fmt: skip
def index_repository(self, repo_id: str, commit_sha: str) -> str:  # type: ignore[no-untyped-def]
    try:
        return asyncio.run(_index(uuid.UUID(repo_id), commit_sha))
    except TransientError as exc:
        raise self.retry(
            exc=exc, countdown=exc.retry_after or 30 * 2**self.request.retries
        ) from exc
