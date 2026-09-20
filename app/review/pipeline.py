"""Default stage list. Real stages are registered here as milestones land."""

from app.core.config import Settings
from app.domain.states import ReviewStatus
from app.github.factory import make_github_client_factory
from app.retrieval.embeddings import make_embedder
from app.review.orchestrator import ReviewContext, Stage
from app.review.stages.fetch import FetchPRStage
from app.review.stages.index import IndexStage
from app.review.stages.retrieve import RetrieveStage


class NoopStage:
    def __init__(self, status: ReviewStatus) -> None:
        self.status = status

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        return None


def default_stages(settings: Settings | None = None) -> list[Stage]:
    """Stage list for a worker run. Without settings (unit tests) the fetch stage is a no-op."""
    fetch: Stage = (
        FetchPRStage(settings, make_github_client_factory(settings))
        if settings is not None
        else NoopStage(ReviewStatus.FETCHING_PR)
    )
    index: Stage = (
        IndexStage(settings, make_embedder(settings))
        if settings is not None
        else NoopStage(ReviewStatus.INDEXING)
    )
    retrieve: Stage = (
        RetrieveStage(settings, make_embedder(settings))
        if settings is not None
        else NoopStage(ReviewStatus.RETRIEVING_CONTEXT)
    )
    return [
        fetch,
        index,
        retrieve,
        *[
            NoopStage(s)
            for s in (
                ReviewStatus.ANALYZING, ReviewStatus.VALIDATING, ReviewStatus.PUBLISHING,
            )
        ],
    ]  # fmt: skip
