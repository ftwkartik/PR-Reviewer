"""GitHub App authentication: App JWT -> installation access token (cached)."""

import time
from dataclasses import dataclass

import httpx
import jwt

from app.core.errors import PermanentError, TransientError


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # epoch seconds


def make_app_jwt(app_id: str, private_key_pem: str, *, now: float | None = None) -> str:
    """Short-lived RS256 JWT identifying the App (max 10 min; we use 9 with clock-skew slack)."""
    if not app_id or not private_key_pem:
        raise PermanentError("GitHub App credentials are not configured", code="github_auth_config")
    issued = int(now if now is not None else time.time())
    claims = {"iat": issued - 30, "exp": issued + 9 * 60, "iss": app_id}
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


class InstallationTokenProvider:
    """Mints and caches installation tokens. Tokens never leave this process' memory."""

    SKEW_S = 300

    def __init__(
        self, app_id: str, private_key: str, api_url: str, http: httpx.AsyncClient
    ) -> None:
        self._app_id, self._key, self._api, self._http = app_id, private_key, api_url, http
        self._cache: dict[int, _CachedToken] = {}

    async def token_for(self, installation_id: int) -> str:
        cached = self._cache.get(installation_id)
        if cached and cached.expires_at - self.SKEW_S > time.time():
            return cached.token
        app_jwt = make_app_jwt(self._app_id, self._key)
        try:
            resp = await self._http.post(
                f"{self._api}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                },
            )
        except httpx.TransportError as exc:
            raise TransientError(f"GitHub auth network error: {exc!r}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientError(f"GitHub auth {resp.status_code}")
        if resp.status_code in (401, 403, 404):
            raise PermanentError(
                f"installation {installation_id} token request rejected ({resp.status_code})",
                code="github_installation_unavailable",
            )
        resp.raise_for_status()
        data = resp.json()
        expires = _parse_expiry(data["expires_at"])
        self._cache[installation_id] = _CachedToken(data["token"], expires)
        return str(data["token"])


def _parse_expiry(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
