import re
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import SettingsDep, require_api_key
from app.core.errors import PermanentError, TransientError
from app.db.repositories import review_jobs as jobs
from app.db.session import get_session
from app.github.factory import GitHubClientFactory, make_github_client_factory
from app.workers.queue import enqueue_review

router = APIRouter(
    prefix="/api/v1/reviews", tags=["reviews"], dependencies=[Depends(require_api_key)]
)


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class CreateReviewRequest(BaseModel):
    repository: str = Field(description="owner/name")
    pull_request: int = Field(gt=0)
    installation_id: int | None = Field(
        default=None, description="Required only if the repository is not yet known to the service"
    )

    @field_validator("repository")
    @classmethod
    def _valid_repo(cls, v: str) -> str:
        if not _REPO_RE.match(v) or any(part in {".", ".."} for part in v.split("/")):
            raise ValueError("repository must look like 'owner/name'")
        return v


def get_gh_factory(settings: SettingsDep) -> GitHubClientFactory:
    return make_github_client_factory(settings)


def get_enqueue_fn() -> Callable[..., bool]:
    return enqueue_review


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_review(
    body: CreateReviewRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    gh_factory: Annotated[GitHubClientFactory, Depends(get_gh_factory)],
    enqueue: Annotated[Callable[..., bool], Depends(get_enqueue_fn)],
) -> dict[str, Any]:
    """Queue a review for an existing PR without needing a webhook."""
    owner, name = body.repository.split("/", 1)
    known = await jobs.find_repository(session, owner, name)
    installation_id = body.installation_id or (known.installation_id if known else None)
    if installation_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "installation_id required for unknown repository"
        )
    gh, close = gh_factory(installation_id)
    try:
        raw = await gh.get_pull(owner, name, body.pull_request)
    except PermanentError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"pull request not accessible: {exc.code}"
        ) from exc
    except TransientError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub temporarily unavailable"
        ) from exc
    finally:
        await close()
    base_repo = raw["base"]["repo"]
    repo = await jobs.upsert_repository(
        session, github_repo_id=base_repo["id"], owner=owner, name=name,
        installation_id=installation_id, default_branch=base_repo.get("default_branch", "main"),
        private=base_repo.get("private", True),
    )  # fmt: skip
    job = await jobs.create_job(
        session, repository=repo, pull_number=body.pull_request,
        head_sha=raw["head"]["sha"], base_sha=raw["base"]["sha"], trigger="api",
    )  # fmt: skip
    if job is None:
        existing = await jobs.find_live_job(session, repo.id, body.pull_request, raw["head"]["sha"])
        await session.commit()
        if existing is None:  # lost a race with a job that just finished as FAILED/STALE
            raise HTTPException(status.HTTP_409_CONFLICT, "retry the request")
        return {"review_id": str(existing.id), "status": existing.status.lower(), "existing": True}
    await session.commit()
    enqueue(job.id)
    return {"review_id": str(job.id), "status": "queued"}


@router.get("/{review_id}")
async def get_review(
    review_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    job = await jobs.get_job(session, review_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "review not found")
    return {
        "review_id": str(job.id),
        "status": job.status,
        "status_detail": job.status_detail,
        "pull_number": job.pull_number,
        "head_sha": job.head_sha,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "scope": job.scope,
        "usage": job.usage,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
