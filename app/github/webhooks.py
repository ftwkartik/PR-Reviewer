from typing import Literal

from pydantic import BaseModel, ConfigDict

SUPPORTED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class GHOwner(_Loose):
    login: str


class GHRepo(_Loose):
    id: int
    name: str
    owner: GHOwner
    private: bool = True
    default_branch: str = "main"


class GHRef(_Loose):
    sha: str


class GHPullRequest(_Loose):
    number: int
    draft: bool = False
    head: GHRef
    base: GHRef


class GHInstallation(_Loose):
    id: int


class PullRequestEvent(_Loose):
    action: str
    number: int
    pull_request: GHPullRequest
    repository: GHRepo
    installation: GHInstallation | None = None


class InstallationRepo(_Loose):
    id: int


class InstallationEvent(_Loose):
    action: str
    installation: GHInstallation
    repositories: list[InstallationRepo] = []  # `installation` events
    repositories_removed: list[InstallationRepo] = []  # `installation_repositories` events


Decision = Literal["review", "ignore"]


def should_review(event: PullRequestEvent) -> tuple[Decision, str]:
    if event.action not in SUPPORTED_ACTIONS:
        return "ignore", f"unsupported_action:{event.action}"
    if event.pull_request.draft:
        return "ignore", "draft"
    return "review", "ok"
