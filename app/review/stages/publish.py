"""PUBLISHING stage: stale-head check + review posting, all inside review/publishing.py."""

from app.domain.states import ReviewStatus
from app.review.orchestrator import ReviewContext
from app.review.publishing import publish_job


class PublishStage:
    status = ReviewStatus.PUBLISHING

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        await publish_job(ctx.session, ctx.require_gh(), ctx.job)
        return None
