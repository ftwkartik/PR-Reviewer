"""INDEXING stage: make sure the PR's base commit has a ready snapshot."""

import structlog
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.errors import PermanentError, TransientError
from app.core.locks import LockNotAcquiredError, redis_lock
from app.db.models import Repository
from app.db.repositories import index as repo_index
from app.domain.states import ReviewStatus
from app.indexing.service import IndexService
from app.retrieval.embeddings import EmbeddingProvider
from app.review.orchestrator import ReviewContext

log = structlog.get_logger()


class IndexStage:
    status = ReviewStatus.INDEXING

    def __init__(self, settings: Settings, embedder: EmbeddingProvider) -> None:
        self._settings = settings
        self._embedder = embedder

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        job = ctx.job
        pr = ctx.require_pr()
        repo = await ctx.session.get(Repository, job.repository_id)
        if repo is None:  # pragma: no cover - FK guarantees it
            raise PermanentError("repository missing", code="repo_missing")
        redis = Redis.from_url(self._settings.redis_url)
        try:
            async with redis_lock(redis, f"index:{repo.id}", ttl_s=1800, wait_s=300):
                snap, stats = await IndexService(ctx.session, self._embedder).ensure_snapshot(
                    repo, pr.base_sha, ctx.require_gh()
                )
                if stats.mode != "cached":
                    await repo_index.gc_snapshots(ctx.session, repo.id, keep=3)
        except LockNotAcquiredError as exc:
            raise TransientError("repository index is busy", retry_after=30) from exc
        except PermanentError as exc:
            if exc.code != "repo_too_large":
                raise
            # Too large to index: continue with diff + head-overlay context only, and say so.
            await ctx.session.refresh(job)  # the failed build rolled the shared session back
            job.scope = {**job.scope, "degraded": True, "no_index_reason": exc.code}
            await ctx.session.commit()
            log.warning("index_skipped_repo_too_large")
            return None
        finally:
            await redis.aclose()
        job.snapshot_id = snap.id
        job.usage = {
            **(job.usage or {}),
            "embedding_operations": stats.embeddings_computed,
            "embedding_tokens": stats.embed_tokens,
            "index_mode": stats.mode,
        }
        await ctx.session.commit()
        ctx.data["index_stats"] = stats
        return None
