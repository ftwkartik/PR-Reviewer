from celery import Celery

from app.core.config import get_settings

_s = get_settings()

celery_app = Celery("pr_review_agent", broker=_s.redis_url, backend=None,
                    include=["app.workers.review_tasks", "app.workers.index_tasks"])  # fmt: skip
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="review",
    task_routes={
        "app.workers.review_tasks.run_review": {"queue": "review"},
        "app.workers.index_tasks.index_repository": {"queue": "index"},
    },
    broker_transport_options={"visibility_timeout": 3600},
    task_soft_time_limit=1500,
    task_time_limit=1800,
    beat_schedule={
        "requeue-orphaned-jobs": {
            "task": "app.workers.review_tasks.requeue_orphaned_jobs",
            "schedule": 120.0,
        }
    },
    timezone="UTC",
)
