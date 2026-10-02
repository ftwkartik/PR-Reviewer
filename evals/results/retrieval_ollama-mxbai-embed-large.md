# Retrieval evaluation

_Generated 2026-10-02 11:30 UTC_

> Embedding model: `mxbai-embed-large`. 

## Retrieval (no LLM involved)

| Retrieval mode | Required-context hit rate | PRs fully covered | Mean tokens | Context precision |
|---|---|---|---|---|
| `diff_only` | 0% (0/13) | 0% | 88 | 0.00 |
| `vector_only` | 69% (9/13) | 50% | 443 | 0.10 |
| `lexical_only` | 54% (7/13) | 38% | 432 | 0.08 |
| `hybrid_rag` | 62% (8/13) | 38% | 436 | 0.09 |
| `full` | 85% (11/13) | 75% | 613 | 0.09 |

### Misses in the `full` configuration

| Case | Required context | Missed |
|---|---|---|
| auth_expired_bypass | `Session.is_expired`, `test_expired_session_is_rejected` | `Session.is_expired` |
| breaking_api_change | `test_items_response_shape`, `Contributing` | - |
| injection_hidden | `Session.is_expired`, `test_expired_session_is_rejected` | - |
| missing_transaction | `transaction`, `Contributing` | - |
| missing_validation | `get_items` | - |
| n_plus_one | `items_with_owner_email`, `get_owner` | - |
| sql_injection | `list_items` | `list_items` |
| swallowed_exception | `transfer` | - |
