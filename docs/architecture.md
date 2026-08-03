# Architecture

## System

```mermaid
flowchart LR
  Dev[Developer] -->|open/update PR| GH[(GitHub)]
  GH -->|webhook + HMAC| API[FastAPI]
  API --> PG[(Postgres + pgvector)]
  API --> RQ[(Redis / Celery)]
  RQ --> W[Review Worker]
  W --> GHC[GitHub client]
  W --> IDX[Indexer]
  W --> RET[Hybrid retrieval]
  W --> CB[Context builder]
  W --> SA[Static analyzers]
  W --> LLM[LLMProvider]
  W --> VAL[Validation + dedup]
  VAL --> PUB[Publisher]
  PUB -->|review @ head SHA| GH
  IDX --> PG
  RET --> PG
```

## PR review sequence

```mermaid
sequenceDiagram
  participant D as Developer
  participant G as GitHub
  participant A as Webhook API
  participant W as Worker
  participant R as Retriever
  participant V as Vector DB
  participant L as LLM
  participant P as Publisher
  D->>G: open / push to PR
  G->>A: pull_request (signed)
  A->>A: verify HMAC, dedupe delivery
  A-->>G: 202
  A->>W: enqueue review job
  W->>G: PR, files, patches
  W->>R: context for each hunk group
  R->>V: hybrid query (snapshot-scoped)
  V-->>R: candidates
  R-->>W: tiered, budgeted context
  W->>L: structured review request
  L-->>W: findings (untrusted)
  W->>W: validate, dedupe, confidence gate
  W->>G: GET PR (head SHA unchanged?)
  W->>P: publish
  P->>G: one review, inline comments + summary
```

## Key decisions
See `docs/decisions/`. Full rationale: the approved planning document (sections 6, 13, 14, 20).

- Index the PR **base** commit; overlay head-version chunks for changed files in memory.
- Chunks are content-addressed by git blob SHA; a snapshot is a `path -> blob_sha` manifest.
- Retrieval: RRF over lexical + vector, plus structural tiers with guaranteed slots.
- The LLM proposes; deterministic validation decides what is published.
