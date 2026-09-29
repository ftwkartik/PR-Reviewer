# Runbook

| Symptom | Likely cause | Action |
|---|---|---|
| Webhooks return 401 | Secret mismatch | Compare `GITHUB_WEBHOOK_SECRET` with the App; redeliver from the App's "Advanced" tab |
| Job stuck in `QUEUED` | Redis/worker down | The sweeper re-enqueues QUEUED jobs older than 5 min; check worker logs and `/ready` |
| Job `FAILED` `budget_exceeded` | `DAILY_BUDGET_USD` reached | Raise the cap or wait; no model calls were made |
| Job `FAILED` `llm_refusal` | Model declined the content | Inspect `provider_request_ids`; consider a fallback model |
| Job `FAILED` `llm_malformed_output` | Output failed validation after one repair | Check `llm_calls_total{outcome="repaired"}`; try a different model/effort |
| Job `STALE` | PR updated during review | Expected; the newer head has its own job |
| Job `PARTIAL`-path (`scope.degraded`) | Some batches failed or limits hit | `GET /api/v1/reviews/{id}`; scope lists skipped files and reasons |
| GitHub `403` with rate-limit headers | Secondary rate limit | Client waits/retries; lower worker concurrency |
| Retrieval seems poor | Wrong/old embedding model | `GET /api/v1/repositories/{o}/{r}/index/status`; re-index; check `embedding_model` |

## Operations

- Re-index a repository: `POST /api/v1/repositories/{owner}/{repo}/index`.
- Delete everything stored for a repository: `DELETE /api/v1/repositories/{owner}/{repo}`.
- Review without posting, then publish: `POST /api/v1/reviews {"publish": false}` then `POST .../publish`.
- Inspect why a finding was not posted: `GET /api/v1/reviews/{id}/findings` (rejected ones carry `reject_reason`).
- Changing `EMBEDDING_MODEL` or the chunker version creates new snapshots; old ones are garbage-collected.
