import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_key
from app.db.repositories import review_jobs as jobs
from app.db.session import get_session

router = APIRouter(
    prefix="/api/v1/reviews", tags=["reviews"], dependencies=[Depends(require_api_key)]
)


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
