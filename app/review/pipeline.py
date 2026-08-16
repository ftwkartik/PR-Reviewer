"""Default stage list. Real stages are registered here as milestones land."""

from app.domain.states import ReviewStatus
from app.review.orchestrator import ReviewContext, Stage


class NoopStage:
    def __init__(self, status: ReviewStatus) -> None:
        self.status = status

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        return None


def default_stages() -> list[Stage]:
    return [
        NoopStage(s)
        for s in (
            ReviewStatus.FETCHING_PR, ReviewStatus.INDEXING, ReviewStatus.RETRIEVING_CONTEXT,
            ReviewStatus.ANALYZING, ReviewStatus.VALIDATING, ReviewStatus.PUBLISHING,
        )
    ]  # fmt: skip
