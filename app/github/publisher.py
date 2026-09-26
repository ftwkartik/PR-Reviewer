"""Publish a review to GitHub: one atomic review, with safe fallbacks and idempotency.

Invariants:
  * nothing is posted unless the PR head is still the SHA that was reviewed (else STALE)
  * comments already posted for the same finding (by hidden fingerprint marker) are skipped, so
    a retried task never double-posts
  * one POST = one notification. If GitHub rejects the batch (422), comments are retried
    individually and any that still fail are folded into the summary instead of being lost
  * the review event is always COMMENT: the agent never approves or blocks a merge
"""

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.core.errors import StaleHeadError
from app.github.client import GitHubClient
from app.review.summary import (
    MARKER_PREFIX,
    CommentData,
    SummaryData,
    render_comment,
    render_summary,
)

log = structlog.get_logger()
_FP_RE = re.compile(rf"<!-- {MARKER_PREFIX}:job=[\w-]+;fp=(\w+) -->")


@dataclass
class PublishResult:
    review_id: int | None = None
    posted: dict[str, int | None] = field(default_factory=dict)  # fingerprint -> comment id
    already_posted: dict[str, int | None] = field(default_factory=dict)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (fingerprint, reason)
    summary_posted: bool = False


def _anchor(c: CommentData) -> dict[str, Any]:
    a: dict[str, Any] = {"path": c.path, "line": c.line_end, "side": c.side}
    if c.line_start != c.line_end:
        a.update(start_line=c.line_start, start_side=c.side)
    return a


async def publish_review(
    gh: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    job_id: str,
    head_sha: str,
    comments: list[CommentData],
    summary: SummaryData,
) -> PublishResult:
    result = PublishResult()

    # 1. Stale-head gate: a review of an old commit must never be posted.
    pr = await gh.get_pull(owner, repo, number)
    if pr["head"]["sha"] != head_sha:
        raise StaleHeadError(f"PR head is now {pr['head']['sha'][:7]}, reviewed {head_sha[:7]}")
    if pr.get("state") != "open":
        raise StaleHeadError(f"pull request is {pr.get('state')}")

    # 2. Idempotency: skip anything a previous attempt already posted.
    existing = await gh.list_review_comments(owner, repo, number)
    posted_fps: dict[str, int | None] = {}
    for ec in existing:
        m = _FP_RE.search(ec.get("body") or "")
        if m:
            posted_fps[m.group(1)] = ec.get("id")
    fresh: list[CommentData] = []
    for c in comments:
        if c.fingerprint in posted_fps:
            result.already_posted[c.fingerprint] = posted_fps[c.fingerprint]
        else:
            fresh.append(c)
    summary.posted = list(comments)  # the summary describes the whole review, old and new

    reviews = await gh.list_reviews(owner, repo, number)
    summary_marker = f"{MARKER_PREFIX}:summary;head={head_sha}"
    summary_exists = any(summary_marker in (r.get("body") or "") for r in reviews)

    if not fresh and summary_exists:
        return result

    # 3. One review with all inline comments.
    body = render_summary(summary)
    payload: dict[str, Any] = {"commit_id": head_sha, "body": body, "event": "COMMENT"}
    payload["comments"] = [{**_anchor(c), "body": render_comment(c, job_id)} for c in fresh]
    if summary_exists:
        payload["body"] = ""  # comments only; the summary review for this head already exists
    resp = await gh.create_review(owner, repo, number, payload)

    if resp.status_code in (200, 201):
        data = resp.json()
        result.review_id = data.get("id")
        result.summary_posted = not summary_exists
        result.posted = await _resolve_comment_ids(gh, owner, repo, number, data.get("id"), fresh)
        return result

    # 4. GitHub rejected the batch (typically one comment's anchor): isolate the bad ones.
    if resp.status_code != 422:
        resp.raise_for_status()
    log.warning("review_batch_rejected", status=resp.status_code, comments=len(fresh))
    survivors: list[CommentData] = []
    for c in fresh:
        single = await gh.create_review_comment(
            owner,
            repo,
            number,
            {**_anchor(c), "body": render_comment(c, job_id), "commit_id": head_sha},
        )
        if single.status_code in (200, 201):
            result.posted[c.fingerprint] = single.json().get("id")
            survivors.append(c)
        else:
            result.failed.append((c.fingerprint, f"github {single.status_code}"))
    failed_fps = {fp for fp, _ in result.failed}
    summary.posted = [c for c in comments if c.fingerprint not in failed_fps]
    summary.demoted = [c for c in comments if c.fingerprint in failed_fps]
    if not summary_exists:
        final = await gh.create_review(
            owner, repo, number,
            {"commit_id": head_sha, "body": render_summary(summary), "event": "COMMENT"},
        )  # fmt: skip
        if final.status_code in (200, 201):
            result.review_id = final.json().get("id")
            result.summary_posted = True
        else:
            final.raise_for_status()
    return result


async def _resolve_comment_ids(
    gh: GitHubClient, owner: str, repo: str, number: int, review_id: int | None,
    sent: list[CommentData],
) -> dict[str, int | None]:  # fmt: skip
    ids: dict[str, int | None] = {c.fingerprint: None for c in sent}
    if not sent or review_id is None:
        return ids
    try:
        for raw in await gh.list_review_comments(owner, repo, number):
            m = _FP_RE.search(raw.get("body") or "")
            if m and m.group(1) in ids and raw.get("pull_request_review_id") == review_id:
                ids[m.group(1)] = raw.get("id")
    except Exception:  # comment ids are bookkeeping only; never fail a posted review over them
        log.warning("comment_id_lookup_failed", exc_info=True)
    return ids
