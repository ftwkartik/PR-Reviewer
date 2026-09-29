from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import require_api_key
from app.api.ratelimit import rate_limit
from app.api.routes.reviews import get_gh_factory
from app.core.errors import PermanentError, TransientError
from app.db.models import RepositorySnapshot
from app.db.repositories import index as repo_index
from app.db.repositories import review_jobs as jobs
from app.db.session import get_session
from app.github.factory import GitHubClientFactory

router = APIRouter(
    prefix="/api/v1/repositories",
    tags=["repositories"],
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)


class IndexRequest(BaseModel):
    ref: str | None = Field(
        default=None, description="Branch/tag/SHA; defaults to the default branch"
    )


def enqueue_index(repo_id: str, sha: str) -> bool:
    from app.workers.index_tasks import index_repository

    try:
        index_repository.delay(repo_id, sha)
        return True
    except Exception:
        return False


@router.post("/{owner}/{repo}/index", status_code=status.HTTP_202_ACCEPTED)
async def trigger_index(
    owner: str,
    repo: str,
    body: IndexRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    gh_factory: Annotated[GitHubClientFactory, Depends(get_gh_factory)],
) -> dict[str, Any]:
    """Queue indexing of a repository at a ref (default branch HEAD). Incremental when possible."""
    record = await jobs.find_repository(session, owner, repo)
    if record is None or record.installation_id is None or record.deleted_at is not None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "repository not registered (open a PR first)"
        )
    gh, close = gh_factory(record.installation_id)
    try:
        sha = await gh.get_commit_sha(owner, repo, body.ref or record.default_branch)
    except PermanentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"ref not accessible: {exc.code}") from exc
    except TransientError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub temporarily unavailable"
        ) from exc
    finally:
        await close()
    queued = await run_in_threadpool(enqueue_index, str(record.id), sha)
    return {"status": "queued" if queued else "deferred", "commit_sha": sha}


@router.get("/{owner}/{repo}/index/status")
async def index_status(
    owner: str, repo: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    record = await jobs.find_repository(session, owner, repo)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    snaps = (
        await session.execute(
            select(RepositorySnapshot)
            .where(RepositorySnapshot.repository_id == record.id)
            .order_by(RepositorySnapshot.started_at.desc())
            .limit(10)
        )
    ).scalars().all()  # fmt: skip
    return {
        "repository": f"{owner}/{repo}",
        "snapshots": [
            {
                "snapshot_id": str(s.id), "commit_sha": s.commit_sha, "status": s.status,
                "files": s.file_count, "chunks": s.chunk_count,
                "embedding_model": s.embedding_model,
                "started_at": s.started_at, "finished_at": s.finished_at, "error": s.error,
            }
            for s in snaps
        ],
    }  # fmt: skip


@router.delete("/{owner}/{repo}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repository_data(
    owner: str, repo: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> None:
    """Privacy: purge all stored code, embeddings, snapshots, reviews and findings for a repo."""
    record = await jobs.find_repository(session, owner, repo)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    await repo_index.delete_repository_data(session, record)
