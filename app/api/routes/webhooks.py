import hashlib
from collections.abc import Callable
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import SettingsDep
from app.core.security import verify_github_signature
from app.db.repositories import review_jobs as jobs
from app.db.session import get_session
from app.github.webhooks import PullRequestEvent, should_review
from app.workers.queue import enqueue_review

router = APIRouter(tags=["webhooks"])
log = structlog.get_logger()


def get_enqueue() -> Callable[..., bool]:
    return enqueue_review


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    return body


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    settings: SettingsDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    enqueue: Annotated[Callable[..., bool], Depends(get_enqueue)],
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    body = await _read_body(request, settings.max_webhook_body_bytes)
    if not verify_github_signature(
        settings.github_webhook_secret.get_secret_value(), body, x_hub_signature_256
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    if not x_github_event or not x_github_delivery:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing event or delivery header")
    if x_github_event == "ping":
        return {"status": "pong"}
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"unsupported_event:{x_github_event}"}

    try:
        event = PullRequestEvent.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "malformed payload") from exc

    is_new = await jobs.record_delivery(
        session, delivery_id=x_github_delivery, event=x_github_event,
        action=event.action, payload_hash=hashlib.sha256(body).hexdigest(),
    )  # fmt: skip
    if not is_new:
        await session.rollback()
        return {"status": "duplicate"}

    decision, reason = should_review(event)
    if decision == "ignore":
        await session.commit()
        return {"status": "ignored", "reason": reason}

    repo = await jobs.upsert_repository(
        session,
        github_repo_id=event.repository.id,
        owner=event.repository.owner.login,
        name=event.repository.name,
        installation_id=event.installation.id if event.installation else None,
        default_branch=event.repository.default_branch,
        private=event.repository.private,
    )
    job = await jobs.create_job(
        session, repository=repo, pull_number=event.number,
        head_sha=event.pull_request.head.sha, base_sha=event.pull_request.base.sha,
        trigger="webhook", delivery_id=x_github_delivery,
    )  # fmt: skip
    await session.commit()
    if job is None:
        return {"status": "duplicate", "reason": "review_for_head_exists"}

    queued = enqueue(job.id)  # never raises; failure leaves the job for the sweeper
    log.info("review_queued", review_id=str(job.id), delivery_id=x_github_delivery, enqueued=queued)
    return {"status": "queued", "review_id": str(job.id), "enqueued": queued}
