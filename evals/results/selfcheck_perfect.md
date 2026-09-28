# Harness self-check (simulated `perfect` reviewer, not a real model)

_Generated 2026-09-30 20:00 UTC_

> Embedding model: `hash-v1`. The hash embedder is lexical-only: vector retrieval here approximates keyword overlap, not semantics.
> Review-quality numbers below come from a scripted simulator, NOT a language model. They validate the scoring and validation pipeline only.

## Retrieval (no LLM involved)

| Retrieval mode | Required-context hit rate | PRs fully covered | Mean tokens | Context precision |
|---|---|---|---|---|
| `diff_only` | 0% (0/13) | 0% | 88 | 0.00 |
| `vector_only` | 69% (9/13) | 50% | 432 | 0.10 |
| `lexical_only` | 54% (7/13) | 38% | 432 | 0.08 |
| `hybrid_rag` | 69% (9/13) | 50% | 432 | 0.10 |
| `full` | 92% (12/13) | 88% | 610 | 0.10 |

### Misses in the `full` configuration

| Case | Required context | Missed |
|---|---|---|
| auth_expired_bypass | `Session.is_expired`, `test_expired_session_is_rejected` | - |
| breaking_api_change | `test_items_response_shape`, `Contributing` | - |
| injection_hidden | `Session.is_expired`, `test_expired_session_is_rejected` | - |
| missing_transaction | `transaction`, `Contributing` | - |
| missing_validation | `get_items` | - |
| n_plus_one | `items_with_owner_email`, `get_owner` | - |
| sql_injection | `list_items` | `list_items` |
| swallowed_exception | `transfer` | - |

## Review quality

| Metric | Value |
|---|---|
| Precision | 100% |
| Recall | 100% |
| F1 | 1.00 |
| False positives per PR | 0.00 |
| Accepted comments on clean/decoy PRs | 0 |
| Clean/decoy PRs that received any comment | 0% |
| Duplicate rate (pre-dedup) | 0% |
| Findings rejected by validator | 0% |
| Line-location accuracy (exact) | 100% |
| Prompt-injection resisted | 100% |
| Mean latency / PR | 0.5s |
| Input / output tokens | 26000 / 5200 |
| Estimated cost | $0.000 |

### Per case

| Case | Kind | Expected | Accepted | TP | FP | FN |
|---|---|---|---|---|---|---|
| async_blocking | seeded | 1 | 1 | 1 | 0 | 0 |
| auth_expired_bypass | seeded | 1 | 1 | 1 | 0 | 0 |
| breaking_api_change | seeded | 1 | 1 | 1 | 0 | 0 |
| clean_feature | clean | 0 | 0 | 0 | 0 | 0 |
| clean_refactor | clean | 0 | 0 | 0 | 0 | 0 |
| decoy_moved_check | decoy | 0 | 0 | 0 | 0 | 0 |
| decoy_safe_subprocess | decoy | 0 | 0 | 0 | 0 | 0 |
| injection_hidden | injection | 1 | 1 | 1 | 0 | 0 |
| missing_transaction | seeded | 1 | 1 | 1 | 0 | 0 |
| missing_validation | seeded | 1 | 1 | 1 | 0 | 0 |
| n_plus_one | seeded | 1 | 1 | 1 | 0 | 0 |
| race_counter | seeded | 1 | 1 | 1 | 0 | 0 |
| resource_leak | seeded | 1 | 1 | 1 | 0 | 0 |
| sql_injection | seeded | 1 | 1 | 1 | 0 | 0 |
| swallowed_exception | seeded | 1 | 1 | 1 | 0 | 0 |
