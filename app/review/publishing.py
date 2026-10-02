"""Turn persisted, validated findings into a GitHub review and record the outcome."""

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermanentError
from app.db.models import Repository, ReviewJob
from app.db.models import ReviewFinding as Row
from app.db.models.base import utcnow
from app.github.client import GitHubClient
from app.github.publisher import PublishResult, publish_review
from app.observability.metrics import PUBLISHED_COMMENTS
from app.review.summary import CommentData, SummaryData

log = structlog.get_logger()
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _comment_data(row: Row) -> CommentData:
    refs = list(row.context_refs or [])  # stored as "path:start-end" labels at validation time
    return CommentData(
        path=row.path, line_start=row.line_start, line_end=row.line_end, side=row.side,
        severity=row.severity, category=row.category, title=row.title, explanation=row.explanation,
        suggested_fix=row.suggested_fix or "", replacement_code=row.replacement_code,
        fingerprint=row.fingerprint, evidence_refs=refs, confidence=row.confidence,
    )  # fmt: skip


def build_summary(job: ReviewJob, head_sha: str) -> SummaryData:
    sc = job.scope or {}
    return SummaryData(
        head_sha=head_sha,
        summary=sc.get("summary", ""),
        overall_risk=sc.get("overall_risk", "none"),
        reviewed_files=sc.get("reviewed_files", 0),
        total_files=sc.get("total_files", 0),
        reviewed_lines=sc.get("reviewed_changed_lines", 0),
        total_lines=sc.get("total_changed_lines", 0),
        skipped=[(s["path"], s["reason"]) for s in sc.get("skipped", [])],
        cross_file=sc.get("cross_file", []),
        context_paths=sc.get("context_paths", []),
        below_threshold=sc.get("below_threshold", 0),
        injection_attempts=sc.get("injection_attempts", 0),
        degraded=bool(sc.get("degraded")),
        failed_batches=[b["paths"] for b in sc.get("failed_batches", [])],
    )


async def publish_job(
    session: AsyncSession, gh: GitHubClient, job: ReviewJob, *, force: bool = False
) -> PublishResult | None:
    """Publish a job's accepted findings. Returns None when publishing is intentionally skipped."""
    if job.dry_run and not force:
        log.info("publish_skipped_dry_run")
        return None
    repo = await session.get(Repository, job.repository_id)
    if repo is None:  # pragma: no cover
        raise PermanentError("repository missing", code="repo_missing")

    rows = list(
        (
            await session.execute(
                select(Row).where(Row.review_job_id == job.id, Row.status == "accepted")
            )
        ).scalars()
    )
    rows.sort(key=lambda r: (SEV_ORDER[r.severity], -r.confidence, r.path, r.line_start))
    comments = [_comment_data(r) for r in rows]
    by_fp = {c.fingerprint: r for c, r in zip(comments, rows, strict=True)}

    summary = build_summary(job, job.head_sha)
    result = await publish_review(
        gh, repo.owner, repo.name, job.pull_number, str(job.id), job.head_sha, comments, summary
    )

    failed = {fp for fp, _ in result.failed}
    for fp, row in by_fp.items():
        if fp in failed:
            row.status = "publish_failed"
            row.reject_reason = "GitHub rejected the inline comment; included in summary instead"
        else:
            row.status = "published"
            row.github_comment_id = result.posted.get(fp) or result.already_posted.get(fp)
    if result.review_id:
        job.github_review_id = result.review_id
    job.scope = {
        **job.scope,
        "published_at": utcnow().isoformat(),
        "published_comments": len(comments) - len(failed),
    }
    await session.commit()
    PUBLISHED_COMMENTS.inc(len(comments) - len(failed))
    log.info("review_published", review_github_id=result.review_id, comments=len(comments),
             failed=len(failed), already_posted=len(result.already_posted))  # fmt: skip
    return result
