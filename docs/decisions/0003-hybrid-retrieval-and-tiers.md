# ADR 3: Hybrid retrieval with guaranteed structural tiers

**Problem.** Embeddings alone miss exact identifiers and structural relations (callers, tests).
**Choice.** Deterministic lookups (containing symbol, imports/calls, callers, tests, config, docs) fill
tiers 1-4 and 6; lexical + vector results are fused with Reciprocal Rank Fusion plus bounded boosts and fill
the RAG tier. Budget is split per tier and unused budget rolls down.
**Reason.** RRF avoids adding incomparable scores; structural tiers make the evidence the reviewer must
have non-negotiable. The ablation in `evals/results/retrieval.md` measures each part (full 92% vs hybrid-only
69% vs diff-only 0% required-context hit rate on the benchmark).
**Tradeoff.** Name-based call resolution is approximate (no type information).
