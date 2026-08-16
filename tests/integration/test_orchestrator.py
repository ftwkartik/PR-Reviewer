import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import StaleHeadError, TransientError
from app.db.models import Repository, ReviewJob
from app.db.repositories import review_jobs as jobs
from app.domain.states import ReviewStatus
from app.review.orchestrator import ReviewContext, ReviewOrchestrator
from app.review.pipeline import NoopStage, default_stages


async def make_job(sm: async_sessionmaker[AsyncSession], head: str = "a" * 40) -> ReviewJob:
    async with sm() as s:
        repo = await jobs.upsert_repository(
            s, github_repo_id=1, owner="o", name="r", installation_id=1,
            default_branch="main", private=True,
        )  # fmt: skip
        job = await jobs.create_job(
            s, repository=repo, pull_number=1, head_sha=head, base_sha="b" * 40, trigger="api"
        )
        await s.commit()
        assert job is not None
        return job


class Boom:
    def __init__(self, status: ReviewStatus, exc: Exception) -> None:
        self.status, self.exc = status, exc

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        raise self.exc


async def test_happy_path_completes(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    result = await ReviewOrchestrator(sessionmaker, default_stages()).run(job.id)
    assert result == ReviewStatus.COMPLETED
    async with sessionmaker() as s:
        j = await jobs.get_job(s, job.id)
        assert j and j.status == "COMPLETED" and j.finished_at and "entered_PUBLISHING" in j.timings


async def test_stale_head_marks_stale(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    stages = [
        NoopStage(ReviewStatus.FETCHING_PR),
        Boom(ReviewStatus.INDEXING, StaleHeadError("moved")),
    ]
    assert await ReviewOrchestrator(sessionmaker, stages).run(job.id) == ReviewStatus.STALE  # type: ignore[arg-type]
    async with sessionmaker() as s:
        j = await jobs.get_job(s, job.id)
        assert j and j.error_code == "stale_head"


async def test_cancel_requested_stops_before_next_stage(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    async with sessionmaker() as s:
        j = await jobs.get_job(s, job.id)
        assert j
        j.cancel_requested = True
        await s.commit()
    assert (
        await ReviewOrchestrator(sessionmaker, default_stages()).run(job.id)
        == ReviewStatus.CANCELLED
    )


async def test_unexpected_error_fails_job(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    stages = [Boom(ReviewStatus.FETCHING_PR, RuntimeError("kaboom"))]
    assert await ReviewOrchestrator(sessionmaker, stages).run(job.id) == ReviewStatus.FAILED  # type: ignore[arg-type]


async def test_transient_error_propagates_and_resumes(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    stages = [
        NoopStage(ReviewStatus.FETCHING_PR),
        Boom(ReviewStatus.INDEXING, TransientError("503")),
    ]
    with pytest.raises(TransientError):
        await ReviewOrchestrator(sessionmaker, stages).run(job.id)  # type: ignore[arg-type]
    async with sessionmaker() as s:
        j = await jobs.get_job(s, job.id)
        assert j and j.status == "INDEXING"  # not failed; the retry resumes here
    # retry succeeds: earlier stages are skipped, job completes
    assert (
        await ReviewOrchestrator(sessionmaker, default_stages()).run(job.id)
        == ReviewStatus.COMPLETED
    )


async def test_terminal_job_is_not_rerun(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    orch = ReviewOrchestrator(sessionmaker, default_stages())
    await orch.run(job.id)
    assert await orch.run(job.id) == ReviewStatus.COMPLETED


async def test_repository_model_roundtrip(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    job = await make_job(sessionmaker)
    async with sessionmaker() as s:
        repo = await s.get(Repository, job.repository_id)
        assert repo and (repo.owner, repo.name) == ("o", "r")
