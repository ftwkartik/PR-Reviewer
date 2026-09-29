# ADR 5: Celery + Redis

**Problem.** Reviews take minutes; webhooks must return fast and work must survive crashes.
**Options.** FastAPI BackgroundTasks (dies with the process), `arq` (async-native), RQ, Temporal.
**Choice.** Celery with `acks_late`, retries with backoff, and an outbox-style sweeper for jobs whose
enqueue failed. Tasks run the async pipeline via `asyncio.run` with a per-task engine.
**Reason.** Mature semantics and operational familiarity; the job row in Postgres is the source of truth,
so the queue can be replaced without touching the pipeline.
**Tradeoff.** Sync worker + async code needs the per-task event loop bridge.
