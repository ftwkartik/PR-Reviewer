"""Review orchestrator: drives a job through the state machine.

Each pipeline step is a `Stage` bound to the status the job is in while it runs. The
orchestrator owns only sequencing, cancellation checks and failure bookkeeping; the
stages (fetch, index, retrieve, analyze, validate, publish) live in their own modules.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import (
    CancelledError,
    PermanentError,
    ReviewError,
    StaleHeadError,
    TransientError,
)
from app.core.logging import bind_review_context
from app.db.models import ReviewJob
from app.db.repositories import review_jobs as jobs
from app.domain.code import CodeChunk
from app.domain.pr import PullRequestContext
from app.domain.states import TERMINAL, ReviewStatus
from app.github.client import GitHubClient
from app.review.batching import ReviewBatch
from app.review.triage import TriageResult

log = structlog.get_logger()


@dataclass
class ReviewContext:
    """Mutable bag passed between stages; typed fields are added as stages are built."""

    job_id: uuid.UUID
    session: AsyncSession
    job: ReviewJob
    gh: GitHubClient | None = None
    pr: PullRequestContext | None = None
    triage: TriageResult | None = None
    batches: list[ReviewBatch] = field(default_factory=list)
    overlay: dict[str, list[CodeChunk]] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    cleanups: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def require_gh(self) -> GitHubClient:
        if self.gh is None:
            raise RuntimeError("GitHub client not initialised (fetch stage must run first)")
        return self.gh

    def require_pr(self) -> PullRequestContext:
        if self.pr is None:
            raise RuntimeError("PR context not loaded (fetch stage must run first)")
        return self.pr


class Stage(Protocol):
    status: ReviewStatus

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        """Return an alternative next status (e.g. PARTIAL), or None for the default."""


class ReviewOrchestrator:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], stages: list[Stage]) -> None:
        self._sessionmaker = sessionmaker
        self._stages = stages

    async def run(self, job_id: uuid.UUID) -> ReviewStatus:
        async with self._sessionmaker() as session:
            job = await jobs.get_job(session, job_id)
            if job is None:
                raise PermanentError(f"job {job_id} not found", code="job_not_found")
            if ReviewStatus(job.status) in TERMINAL:
                return ReviewStatus(job.status)  # idempotent re-delivery
            bind_review_context(review_id=str(job.id), pull_number=job.pull_number,
                                commit_sha=job.head_sha)  # fmt: skip
            ctx = ReviewContext(job_id=job.id, session=session, job=job)
            try:
                return await self._run_stages(ctx)
            except TransientError:
                raise  # task-level retry; job keeps its current status and resumes
            except (StaleHeadError, CancelledError) as exc:
                final = (
                    ReviewStatus.STALE
                    if isinstance(exc, StaleHeadError)
                    else ReviewStatus.CANCELLED
                )
                await self._finish(ctx, final, exc)
                return final
            except ReviewError as exc:
                await self._finish(ctx, ReviewStatus.FAILED, exc)
                return ReviewStatus.FAILED
            except Exception as exc:
                log.exception("review_unexpected_error")
                await self._finish(ctx, ReviewStatus.FAILED, exc)
                return ReviewStatus.FAILED
            finally:
                for cleanup in ctx.cleanups:
                    await cleanup()

    async def _run_stages(self, ctx: ReviewContext) -> ReviewStatus:
        current = ReviewStatus(ctx.job.status)
        for stage in self._stages:
            await self._raise_if_cancelled(ctx)
            if current != stage.status:
                # Resuming after a retry: skip stages at or before the persisted status.
                if current != ReviewStatus.QUEUED and _order(stage.status) <= _order(current):
                    continue
                await jobs.transition(ctx.session, ctx.job, stage.status)
                current = stage.status
            override = await stage.run(ctx)
            if override is not None and override != current:
                await jobs.transition(ctx.session, ctx.job, override)
                current = override
        await jobs.transition(ctx.session, ctx.job, ReviewStatus.COMPLETED)
        return ReviewStatus.COMPLETED

    async def _raise_if_cancelled(self, ctx: ReviewContext) -> None:
        await ctx.session.refresh(ctx.job)
        if ctx.job.cancel_requested:
            raise CancelledError("superseded by a newer review or cancelled by request")

    async def _finish(self, ctx: ReviewContext, status: ReviewStatus, exc: Exception) -> None:
        code = exc.code if isinstance(exc, ReviewError) else "internal_error"
        await ctx.session.rollback()
        await ctx.session.refresh(ctx.job)
        if ReviewStatus(ctx.job.status) in TERMINAL:
            return
        # FAILED/CANCELLED/STALE are reachable from any active state in practice; allow the
        # transition even when the state table is stricter (e.g. PARTIAL -> CANCELLED).
        ctx.job.status = status.value
        ctx.job.error_code = code
        ctx.job.error_message = str(exc)[:2000]
        from app.db.models.base import utcnow

        ctx.job.finished_at = utcnow()
        await ctx.session.commit()
        log.warning("review_finished", status=status.value, error_code=code)


_PIPELINE_ORDER = [
    ReviewStatus.QUEUED, ReviewStatus.FETCHING_PR, ReviewStatus.INDEXING,
    ReviewStatus.RETRIEVING_CONTEXT, ReviewStatus.ANALYZING, ReviewStatus.PARTIAL,
    ReviewStatus.VALIDATING, ReviewStatus.PUBLISHING,
]  # fmt: skip


def _order(status: ReviewStatus) -> int:
    return _PIPELINE_ORDER.index(status)
