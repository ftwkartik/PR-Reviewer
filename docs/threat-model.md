# Threat model

| Threat | Mitigation |
|---|---|
| Prompt injection (PR text, code comments, docs, analyzer output) | Untrusted content in nonce-delimited blocks; **instruction-like lines are removed from the model input and reported as a finding (small models obey injected text); summary discarded if an attempt was detected**; schema-constrained output; model has no write/network tools; application-side validation; sanitized templated comments |
| Malicious tarballs / huge files | Safe extraction (no `..`, absolute paths, symlinks); size/file-count caps; parse timeouts; binary sniffing |
| Untrusted PR code | **Never executed on the host.** Static analysis only. Future execution: ephemeral container, no network, read-only FS, CPU/mem/time limits, no secrets, no Docker socket |
| Webhook spoofing / replay | HMAC-SHA256 constant-time check; body-size cap; delivery-ID idempotency; ignore unknown installations |
| Secret leakage | Env/secret manager only; log redaction; pre-send secret scanning of context; tokens never in prompts; code never logged |
| Path traversal | Normalized paths must be in the PR file set or snapshot manifest; resolved-path containment |
| SSRF | No user-supplied URLs fetched; GitHub base URL fixed in config |
| API abuse | API-key auth, rate limits, per-repo budgets, hard `MAX_*` caps |
| Cross-tenant retrieval | Every query filtered by `repository_id`; isolation tests |
