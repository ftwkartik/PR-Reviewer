# ADR 1: Content-addressed chunks and snapshot manifests

**Problem.** Re-embedding a repository on every commit is slow and costly; serving chunks from an old
commit makes the reviewer reason about code that no longer exists.
**Options.** (a) rebuild per commit; (b) mutable "current" index with deletes; (c) chunks keyed by git
blob SHA + per-commit manifest.
**Choice.** (c). `code_chunks` belong to a blob; a `repository_snapshot` is a `path -> blob_sha` manifest.
**Reason.** Unchanged files cost nothing; a full rebuild after a one-file change still embeds one file;
retrieval reaches chunks only through the snapshot's manifest, so stale chunks cannot leak in.
**Tradeoff.** Manifest rows per snapshot and a GC step; chunk `path` is advisory (the manifest is truth).
