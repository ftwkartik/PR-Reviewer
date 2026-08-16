import uuid

import structlog

log = structlog.get_logger()


def enqueue_review(job_id: uuid.UUID) -> bool:
    """Enqueue a review task. Returns False (never raises) if the broker is unavailable.

    The job row is the source of truth: a QUEUED job whose enqueue failed is picked
    up later by the `requeue_orphaned_jobs` sweeper.
    """
    from app.workers.review_tasks import run_review

    try:
        run_review.delay(str(job_id))
        return True
    except Exception:
        log.warning("enqueue_failed", job_id=str(job_id), exc_info=True)
        return False
