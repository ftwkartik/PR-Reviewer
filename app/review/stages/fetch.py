"""FETCHING_PR stage: load the PR from GitHub, verify it is still the reviewed head, triage."""

import structlog
from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import CancelledError, PermanentError, StaleHeadError
from app.db.models import Repository
from app.domain.states import ReviewStatus
from app.github.factory import GitHubClientFactory
from app.github.pr_builder import build_pr_context
from app.review.orchestrator import ReviewContext
from app.review.triage import triage

log = structlog.get_logger()


class FetchPRStage:
    status = ReviewStatus.FETCHING_PR

    def __init__(self, settings: Settings, gh_factory: GitHubClientFactory) -> None:
        self._settings = settings
        self._gh_factory = gh_factory

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        job = ctx.job
        repo = (
            await ctx.session.execute(select(Repository).where(Repository.id == job.repository_id))
        ).scalar_one()
        if job.installation_id is None:
            raise PermanentError(
                "repository has no GitHub App installation", code="no_installation"
            )
        gh, closer = self._gh_factory(job.installation_id)
        ctx.gh = gh
        ctx.cleanups.append(closer)

        raw_pr = await gh.get_pull(repo.owner, repo.name, job.pull_number)
        if raw_pr["state"] != "open":
            raise CancelledError(f"pull request is {raw_pr['state']}")
        if raw_pr["head"]["sha"] != job.head_sha:
            raise StaleHeadError(
                f"head moved: reviewed {job.head_sha[:7]}, now {raw_pr['head']['sha'][:7]}"
            )

        raw_files = await gh.list_pull_files(repo.owner, repo.name, job.pull_number)
        pr = build_pr_context(raw_pr, raw_files, self._settings)
        pr.installation_id = job.installation_id
        result = triage(pr.files, self._settings)
        if pr.total_files_reported > len(raw_files):
            result.degraded = True
            result.skipped.append(("(files beyond GitHub API listing limit)", "api_file_cap"))

        job.base_sha = pr.base_sha
        job.scope = result.as_scope()
        await ctx.session.commit()
        ctx.pr, ctx.triage = pr, result
        log.info("pr_fetched", files=len(pr.files), reviewed=len(result.selected),
                 skipped=len(result.skipped), github_calls=gh.calls)  # fmt: skip
        return None
