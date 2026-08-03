# Privacy and data handling

- **Sent to the LLM provider:** diff hunks, selected retrieved code snippets, PR title/body, static-analysis output. Never credentials.
- **Sent to the embedding provider:** chunk text. Use the local embedder to keep it on-box.
- **Persisted:** code chunks and embeddings, findings (with short evidence quotes), usage. Webhook payloads are not stored.
- **Logs:** IDs, hashes, sizes and metrics only. No source code, no secrets.
- **Deletion:** `DELETE /api/v1/repositories/{owner}/{repo}` removes snapshots, chunks, findings and cache rows.
- **Provider choice** is configured by environment variables; prefer zero-retention terms for private repos.
