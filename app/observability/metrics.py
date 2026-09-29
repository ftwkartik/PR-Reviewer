"""Prometheus metrics. One module so names and labels stay consistent across api and worker.

API exposes /metrics; each Celery worker starts its own exporter (WORKER_METRICS_PORT), so
scrape both. Labels are low-cardinality on purpose: never repository names, PR numbers or paths.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from prometheus_client import Counter, Histogram

WEBHOOKS = Counter("webhook_events_total", "Webhook deliveries by outcome", ["event", "outcome"])
REVIEW_JOBS = Counter("review_jobs_total", "Finished review jobs by terminal status", ["status"])
STAGE_SECONDS = Histogram(
    "review_stage_seconds", "Wall time per pipeline stage", ["stage"],
    buckets=(0.05, 0.25, 1, 3, 10, 30, 90, 300, 900),
)  # fmt: skip
GITHUB_CALLS = Counter("github_api_calls_total", "GitHub REST calls", ["method", "status"])
LLM_CALLS = Counter("llm_calls_total", "LLM API calls", ["provider", "purpose", "outcome"])
LLM_TOKENS = Counter("llm_tokens_total", "LLM tokens", ["direction"])
LLM_SECONDS = Histogram(
    "llm_call_seconds", "LLM call latency", ["provider", "purpose"],
    buckets=(0.5, 2, 5, 15, 30, 60, 120, 300),
)  # fmt: skip
EMBED_TOKENS = Counter("embedding_tokens_total", "Tokens sent to the embedding provider")
EMBED_OPERATIONS = Counter("embedding_operations_total", "Chunks embedded (cache misses)")
FINDINGS = Counter("review_findings_total", "Findings by validation outcome", ["status"])
PUBLISHED_COMMENTS = Counter("published_comments_total", "Inline comments posted to GitHub")
REDACTIONS = Counter("secret_redactions_total", "Secrets redacted from model input", ["kind"])
EST_COST = Counter("llm_estimated_cost_usd_total", "Estimated LLM spend in USD")


@contextmanager
def timed_stage(stage: str) -> Iterator[None]:
    start = perf_counter()
    try:
        yield
    finally:
        STAGE_SECONDS.labels(stage).observe(perf_counter() - start)
