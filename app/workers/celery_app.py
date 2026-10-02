import os

from celery import Celery
from celery.signals import worker_ready
from prometheus_client import CollectorRegistry, multiprocess, start_http_server

from app.core.config import get_settings

# Must exist before any metric is created (prometheus_client writes per-process files there).
if _mp_dir := os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
    os.makedirs(_mp_dir, exist_ok=True)

_s = get_settings()

celery_app = Celery("pr_review_agent", broker=_s.redis_url, backend=None,
                    include=[
        "app.workers.review_tasks", "app.workers.index_tasks", "app.workers.maintenance",
    ])  # fmt: skip
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
        },
        "purge-expired-findings": {
            "task": "app.workers.maintenance.purge_expired_findings",
            "schedule": 86400.0,
        },
    },
    timezone="UTC",
)


@worker_ready.connect
def _start_metrics_server(**_: object) -> None:
    """Expose worker metrics on WORKER_METRICS_PORT.

    Tasks run in forked pool children, whose counters live in their own memory. With
    PROMETHEUS_MULTIPROC_DIR set (docker-compose does) the exporter aggregates every process's
    metric files; without it only the parent's (task-free) metrics would be visible.
    """
    port = get_settings().worker_metrics_port
    if port <= 0:
        return
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        start_http_server(port, registry=registry)
    else:
        start_http_server(port)
