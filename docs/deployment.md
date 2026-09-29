# Deployment

Deliberately small: one codebase, two processes (`api`, `worker`), Postgres (pgvector) and Redis.
No Kubernetes needed; the containers are stateless and configured by environment, so moving to an
orchestrator later is a packaging change, not a redesign.

## Reference topology

```
GitHub ──HTTPS──> Caddy/Traefik (TLS) ──> api (x2)  ──> Postgres (managed, pgvector)
                                              └──────> Redis (managed)
                                         worker (xN) ──> Postgres / Redis / GitHub / LLM / embeddings
```

- Single VM with `docker compose` is sufficient for a small team; scale `worker` by queue depth.
- Use managed Postgres with the `vector` and `pg_trgm` extensions enabled, and managed Redis.
- Run `alembic upgrade head` before rolling out a new `api`/`worker` (the compose `migrate` service does this).

## Configuration checklist

| Variable | Notes |
|---|---|
| `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET` | From the GitHub App. Private key via secret store; `\n` escapes are accepted in env vars |
| `LLM_PROVIDER`, `LLM_MODEL`, provider API key | Model is never hard-coded |
| `EMBEDDING_PROVIDER` (`voyage`/`openai`/`hash`), key | `hash` is for dev/tests only (lexical, not semantic) |
| `API_KEY` | Required for `/api/v1`. Use a long random value |
| `ALLOWED_OWNERS` | Restrict which GitHub owners may trigger reviews (prevents LLM-budget abuse) |
| `DAILY_BUDGET_USD` | Per-repository 24h spend cap |
| `RATE_LIMIT_PER_MINUTE` | Per API key / IP on `/api/v1` |
| `METRICS_TOKEN` | If set, `/metrics` requires a bearer token; otherwise restrict it at the proxy |
| `FINDINGS_RETENTION_DAYS` | Findings can quote private code; purged daily |

## GitHub App setup

1. Create a GitHub App. Webhook URL `https://<host>/webhooks/github`, a random secret.
2. Permissions (minimum): **Contents: read**, **Pull requests: read & write**, **Metadata: read**.
3. Subscribe to events: **Pull request**. (Installation events are delivered automatically; the service
   purges a repository's data when the App is uninstalled or the repository is removed.)
4. Generate a private key; install the App on the repositories to review.
5. Local development: expose the API with a tunnel (smee.io/ngrok) and point the App's webhook at it.

## Observability

- `GET /metrics` on the API and port `WORKER_METRICS_PORT` (9102) on each worker. Key series:
  `review_jobs_total{status}`, `review_stage_seconds`, `llm_calls_total`, `llm_tokens_total`,
  `llm_estimated_cost_usd_total`, `github_api_calls_total{status}`, `review_findings_total{status}`,
  `published_comments_total`, `webhook_events_total{outcome}`, `secret_redactions_total`.
- Logs are JSON with `review_id`, `pull_number`, `commit_sha`, `delivery_id`, `provider_request_id`.
  Source code and secrets are never logged.
- Suggested alerts: failed-job ratio, `review_stage_seconds` p95, `llm_calls_total{outcome="error"}`,
  rising `github_api_calls_total{status="403|429"}`, queue depth.

## Measured webhook latency

`scripts/loadtest_webhook.py` (400 signed deliveries, concurrency 20, worker consuming jobs on the same
development machine): **p50 ≈ 60 ms, p95 ≈ 200-375 ms, p99 ≈ 230-420 ms**, all `202`. (A first run measured
p95 ≈ 1.5 s because the Celery publish was blocking the event loop; it now runs in a thread pool.)
