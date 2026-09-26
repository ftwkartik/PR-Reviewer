"""Stage list for a review run.

With no settings (unit tests of the orchestrator itself) every stage is a no-op; with settings
the real stages are wired. The LLM provider is injectable so tests can substitute a fake.
"""

from app.core.config import Settings
from app.domain.states import ReviewStatus
from app.github.factory import make_github_client_factory
from app.llm.base import LLMProvider
from app.llm.factory import make_llm_provider
from app.retrieval.embeddings import make_embedder
from app.review.orchestrator import ReviewContext, Stage
from app.review.stages.analyze import AnalyzeStage
from app.review.stages.fetch import FetchPRStage
from app.review.stages.index import IndexStage
from app.review.stages.publish import PublishStage
from app.review.stages.retrieve import RetrieveStage
from app.review.stages.validate import ValidateStage


class NoopStage:
    def __init__(self, status: ReviewStatus) -> None:
        self.status = status

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        return None


def default_stages(
    settings: Settings | None = None, provider: LLMProvider | None = None
) -> list[Stage]:
    if settings is None:
        return [
            NoopStage(s)
            for s in (
                ReviewStatus.FETCHING_PR, ReviewStatus.INDEXING, ReviewStatus.RETRIEVING_CONTEXT,
                ReviewStatus.ANALYZING, ReviewStatus.VALIDATING, ReviewStatus.PUBLISHING,
            )
        ]  # fmt: skip
    embedder = make_embedder(settings)
    llm = provider or make_llm_provider(settings)
    return [
        FetchPRStage(settings, make_github_client_factory(settings)),
        IndexStage(settings, embedder),
        RetrieveStage(settings, embedder),
        AnalyzeStage(settings, llm),
        ValidateStage(settings, llm),
        PublishStage(),
    ]
