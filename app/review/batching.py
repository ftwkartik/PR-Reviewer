"""Group changed files into review batches that each fit one model call."""

from dataclasses import dataclass, field, replace

from app.domain.pr import ChangedFile, DiffHunk
from app.domain.retrieval import ContextBundle
from app.retrieval.tokens import estimate_tokens
from app.review.diff_render import render_file_diff, render_hunk

PROMPT_OVERHEAD_TOKENS = 2500  # system prompt, schema, PR metadata, static signals
MIN_CONTEXT_TOKENS = 2000
DIFF_SHARE = 0.40  # of MAX_CONTEXT_TOKENS a batch's diff may occupy


@dataclass
class ReviewBatch:
    index: int
    files: list[ChangedFile]
    diff_tokens: int
    bundle: ContextBundle | None = None
    summary_hint: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return sorted({f.path for f in self.files})


def split_by_hunks(f: ChangedFile, max_tokens: int) -> list[ChangedFile]:
    """A file whose diff alone exceeds a batch is reviewed hunk-range by hunk-range."""
    if estimate_tokens(render_file_diff(f)) <= max_tokens or len(f.hunks) <= 1:
        return [f]
    parts: list[ChangedFile] = []
    cur: list[DiffHunk] = []
    used = 0
    for h in f.hunks:
        t = estimate_tokens(render_hunk(h))
        if cur and used + t > max_tokens:
            parts.append(replace(f, hunks=cur))
            cur, used = [], 0
        cur.append(h)
        used += t
    if cur:
        parts.append(replace(f, hunks=cur))
    return parts


def make_batches(
    files: list[ChangedFile], max_context_tokens: int, max_model_calls: int
) -> tuple[list[ReviewBatch], list[tuple[str, str]]]:
    """Returns (batches, skipped). `files` must already be in priority order (see triage)."""
    diff_budget = max(1000, int(max_context_tokens * DIFF_SHARE))
    units: list[ChangedFile] = []
    for f in files:
        units += split_by_hunks(f, diff_budget)
    batches: list[ReviewBatch] = []
    cur: list[ChangedFile] = []
    used = 0
    for u in units:
        t = estimate_tokens(render_file_diff(u))
        if cur and used + t > diff_budget:
            batches.append(ReviewBatch(len(batches), cur, used))
            cur, used = [], 0
        cur.append(u)
        used += t
    if cur:
        batches.append(ReviewBatch(len(batches), cur, used))

    allowed = max(1, max_model_calls - 1)  # one call is reserved for the synthesis pass
    skipped: list[tuple[str, str]] = []
    for extra in batches[allowed:]:
        skipped += [(p, "model_call_limit") for p in extra.paths]
    return batches[:allowed], skipped


def context_budget(batch: ReviewBatch, max_context_tokens: int) -> int:
    return max(MIN_CONTEXT_TOKENS, max_context_tokens - batch.diff_tokens - PROMPT_OVERHEAD_TOKENS)
