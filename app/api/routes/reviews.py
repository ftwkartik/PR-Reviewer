import re
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import SettingsDep, require_api_key
from app.api.ratelimit import rate_limit
from app.core.config import is_owner_allowed
from app.core.errors import PermanentError, StaleHeadError, TransientError
from app.db.models import ReviewFinding as FindingRow
from app.db.repositories import review_jobs as jobs
from app.db.session import get_session
from app.github.factory import GitHubClientFactory, make_github_client_factory
from app.review.publishing import publish_job
from app.workers.queue import enqueue_review

router = APIRouter(
    prefix="/api/v1/reviews",
    tags=["reviews"],
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class CreateReviewRequest(BaseModel):
    repository: str = Field(description="owner/name")
    pull_request: int = Field(gt=0)
    publish: bool = Field(
        default=True,
        description="false = analyze and validate only; publish later via POST .../publish",
    )
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
    settings: SettingsDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    gh_factory: Annotated[GitHubClientFactory, Depends(get_gh_factory)],
    enqueue: Annotated[Callable[..., bool], Depends(get_enqueue_fn)],
) -> dict[str, Any]:
    """Queue a review for an existing PR without needing a webhook."""
    owner, name = body.repository.split("/", 1)
    if not is_owner_allowed(settings, owner):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "repository owner is not allowed")
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
        dry_run=not body.publish,
    )  # fmt: skip
    if job is None:
        existing = await jobs.find_live_job(session, repo.id, body.pull_request, raw["head"]["sha"])
        await session.commit()
        if existing is None:  # lost a race with a job that just finished as FAILED/STALE
            raise HTTPException(status.HTTP_409_CONFLICT, "retry the request")
        return {"review_id": str(existing.id), "status": existing.status.lower(), "existing": True}
    await session.commit()
    await run_in_threadpool(enqueue, job.id)
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


@router.get("/{review_id}/findings")
async def get_findings(
    review_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    """All findings of a review, including rejected ones with the reason (for debugging/eval)."""
    job = await jobs.get_job(session, review_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "review not found")
    q = select(FindingRow).where(FindingRow.review_job_id == review_id)
    if status_filter:
        q = q.where(FindingRow.status == status_filter)
    rows = (await session.execute(q.order_by(FindingRow.created_at))).scalars().all()
    return {
        "review_id": str(review_id),
        "findings": [
            {
                "id": str(r.id), "path": r.path, "line_start": r.line_start, "line_end": r.line_end,
                "side": r.side, "severity": r.severity, "category": r.category, "title": r.title,
                "explanation": r.explanation, "suggested_fix": r.suggested_fix,
                "replacement_code": r.replacement_code, "confidence": r.confidence,
                "status": r.status, "reject_reason": r.reject_reason,
                "github_comment_id": r.github_comment_id, "context": r.context_refs,
            }
            for r in rows
        ],
    }  # fmt: skip


@router.post("/{review_id}/publish", status_code=status.HTTP_202_ACCEPTED)
async def publish_review_now(
    review_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    gh_factory: Annotated[GitHubClientFactory, Depends(get_gh_factory)],
) -> dict[str, Any]:
    """Publish a completed (typically dry-run) review. Refuses if the PR head has moved."""
    job = await jobs.get_job(session, review_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "review not found")
    if job.status != "COMPLETED":
        raise HTTPException(status.HTTP_409_CONFLICT, f"review is {job.status}, not COMPLETED")
    if job.github_review_id or (job.scope or {}).get("published_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "review was already published")
    if job.installation_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "review has no installation")
    gh, close = gh_factory(job.installation_id)
    try:
        result = await publish_job(session, gh, job, force=True)
    except StaleHeadError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"stale: {exc}") from exc
    except PermanentError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"GitHub rejected publishing: {exc.code}"
        ) from exc
    except TransientError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub temporarily unavailable"
        ) from exc
    finally:
        await close()
    published = len(result.posted) + len(result.already_posted) if result else 0
    return {
        "review_id": str(review_id),
        "github_review_id": result.review_id if result else None,
        "published_comments": published,
    }
