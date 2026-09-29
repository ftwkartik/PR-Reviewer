# ADR 2: Index the PR base; overlay the head in memory

**Problem.** Indexing every PR head would embed code nobody merges yet.
**Choice.** Index the base commit. Files touched by the PR are chunked from the head SHA in memory
(never persisted or embedded), and their base versions are excluded from retrieval.
**Reason.** The reviewer sees the new code for changed files and the real repository for everything else,
never a mixture of old and new versions of the code under review.
**Tradeoff.** Overlay chunks are not semantically searchable (lexical/structural only) - acceptable, since
the changed code is always included directly.
