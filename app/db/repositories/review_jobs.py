import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import Float, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Repository, ReviewJob, WebhookDelivery
from app.db.models.base import utcnow
from app.domain.states import TERMINAL, ReviewStatus, check_transition

_NON_TERMINAL = [s.value for s in ReviewStatus if s not in TERMINAL]


async def upsert_repository(
    session: AsyncSession,
    *,
    github_repo_id: int,
    owner: str,
    name: str,
    installation_id: int | None,
    default_branch: str,
    private: bool,
) -> Repository:
    stmt = (
        insert(Repository)
        .values(
            id=uuid.uuid4(),
            github_repo_id=github_repo_id,
            owner=owner,
            name=name,
            installation_id=installation_id,
            default_branch=default_branch,
            private=private,
        )  # fmt: skip
        .on_conflict_do_update(
            index_elements=[Repository.github_repo_id],
            set_={
                "owner": owner,
                "name": name,
                "default_branch": default_branch,
                "private": private,
                "deleted_at": None,
                "installation_id": installation_id or Repository.installation_id,
            },
        )  # fmt: skip
        .returning(Repository)
    )
    return (await session.execute(stmt)).scalar_one()


async def record_delivery(
    session: AsyncSession, *, delivery_id: str, event: str, action: str | None, payload_hash: str
) -> bool:
    """Returns True if this delivery is new, False if it is a retry/duplicate."""
    stmt = (
        insert(WebhookDelivery)
        .values(delivery_id=delivery_id, event=event, action=action, payload_hash=payload_hash)
        .on_conflict_do_nothing(index_elements=[WebhookDelivery.delivery_id])
        .returning(WebhookDelivery.delivery_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def create_job(
    session: AsyncSession,
    *,
    repository: Repository,
    pull_number: int,
    head_sha: str,
    base_sha: str | None,
    trigger: str,
    delivery_id: str | None = None,
    dry_run: bool = False,
) -> ReviewJob | None:
    """Create a QUEUED job; None if a live job already exists for this head SHA.

    Older live jobs for the same PR are flagged cancel_requested (superseded).
    """
    stmt = (
        insert(ReviewJob)
        .values(
            id=uuid.uuid4(),
            repository_id=repository.id,
            pull_number=pull_number,
            head_sha=head_sha,
            base_sha=base_sha,
            installation_id=repository.installation_id,
            trigger=trigger,
            delivery_id=delivery_id,
            status=ReviewStatus.QUEUED.value,
            dry_run=dry_run,
        )  # fmt: skip
        .on_conflict_do_nothing()
        .returning(ReviewJob)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None
    await session.execute(
        update(ReviewJob)
        .where(
            ReviewJob.repository_id == repository.id,
            ReviewJob.pull_number == pull_number,
            ReviewJob.id != job.id,
            ReviewJob.status.in_(_NON_TERMINAL),
        )
        .values(cancel_requested=True)
    )
    return job


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> ReviewJob | None:
    return await session.get(ReviewJob, job_id)


async def transition(
    session: AsyncSession,
    job: ReviewJob,
    new: ReviewStatus,
    *,
    detail: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    check_transition(ReviewStatus(job.status), new)
    now = utcnow()
    timings: dict[str, Any] = dict(job.timings or {})
    timings[f"entered_{new.value}"] = now.isoformat()
    job.timings = timings
    job.status = new.value
    job.status_detail = detail
    if new == ReviewStatus.FETCHING_PR and job.started_at is None:
        job.started_at = now
    if new in TERMINAL:
        job.finished_at = now
    if error_code:
        job.error_code = error_code
        job.error_message = (error_message or "")[:2000]
    await session.commit()


async def stale_queued_jobs(session: AsyncSession, older_than: timedelta) -> list[uuid.UUID]:
    cutoff = utcnow() - older_than
    rows = await session.execute(
        select(ReviewJob.id).where(
            ReviewJob.status == ReviewStatus.QUEUED.value, ReviewJob.created_at < cutoff
        )
    )
    return list(rows.scalars())


async def spend_last_24h(
    session: AsyncSession, repository_id: uuid.UUID, exclude: uuid.UUID
) -> float:
    """Estimated LLM spend (USD) on a repository over the last 24 hours, excluding one job."""
    cost = cast(ReviewJob.usage["est_cost_usd"].astext, Float)
    total = await session.scalar(
        select(func.coalesce(func.sum(cost), 0.0)).where(
            ReviewJob.repository_id == repository_id,
            ReviewJob.id != exclude,
            ReviewJob.created_at > utcnow() - timedelta(hours=24),
        )
    )
    return float(total or 0.0)


async def find_live_job(
    session: AsyncSession, repository_id: uuid.UUID, pull_number: int, head_sha: str
) -> ReviewJob | None:
    return (
        await session.execute(
            select(ReviewJob).where(
                ReviewJob.repository_id == repository_id,
                ReviewJob.pull_number == pull_number,
                ReviewJob.head_sha == head_sha,
                ReviewJob.status.notin_(["FAILED", "CANCELLED", "STALE"]),
            )
        )
    ).scalar_one_or_none()


async def find_repository(session: AsyncSession, owner: str, name: str) -> Repository | None:
    return (
        await session.execute(
            select(Repository).where(Repository.owner == owner, Repository.name == name)
        )
    ).scalar_one_or_none()
