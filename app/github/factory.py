from collections.abc import Awaitable, Callable

import httpx

from app.core.config import Settings
from app.github.auth import InstallationTokenProvider
from app.github.client import GitHubClient

Closer = Callable[[], Awaitable[None]]
GitHubClientFactory = Callable[[int], tuple[GitHubClient, Closer]]


def make_github_client_factory(settings: Settings) -> GitHubClientFactory:
    """Build a factory producing a per-installation client plus its cleanup callback."""

    def factory(installation_id: int) -> tuple[GitHubClient, Closer]:
        http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        tokens = InstallationTokenProvider(
            settings.github_app_id,
            settings.github_private_key.get_secret_value().replace("\\n", "\n"),
            settings.github_api_url,
            http,
        )
        client = GitHubClient(http, tokens, installation_id, settings.github_api_url)
        return client, http.aclose

    return factory
