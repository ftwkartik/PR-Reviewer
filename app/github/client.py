"""Thin async GitHub REST client with retry, pagination and rate-limit handling."""

import asyncio
import random
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.core.errors import PermanentError, TransientError
from app.github.auth import InstallationTokenProvider

log = structlog.get_logger()

MAX_ATTEMPTS = 4
MAX_FILES_API = 3000  # GitHub caps /pulls/{n}/files at 3000 files


class GitHubClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: InstallationTokenProvider,
        installation_id: int,
        api_url: str = "https://api.github.com",
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._http, self._tokens, self._inst, self._api = http, tokens, installation_id, api_url
        self._sleep = sleep
        self.calls = 0

    async def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        token = await self._tokens.token_for(self._inst)
        return {"Authorization": f"Bearer {token}", "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28"}  # fmt: skip

    async def _request(
        self, method: str, path: str, *, accept: str = "application/vnd.github+json", **kw: Any
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._api}{path}"
        if not url.startswith(self._api) and "codeload.github.com" not in url:
            raise PermanentError(f"refusing request to non-GitHub host: {url}", code="ssrf_blocked")
        for attempt in range(MAX_ATTEMPTS):
            self.calls += 1
            try:
                resp = await self._http.request(
                    method, url, headers=await self._headers(accept), **kw
                )
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise TransientError(f"GitHub network error: {exc!r}") from exc
                await self._sleep(_backoff(attempt))
                continue
            wait = _retry_delay(resp)
            if wait is not None:
                if attempt == MAX_ATTEMPTS - 1 or wait > 120:
                    raise TransientError(f"GitHub {resp.status_code} (rate limited/unavailable)",
                                         retry_after=wait)  # fmt: skip
                await self._sleep(wait)
                continue
            if resp.status_code == 404:
                raise PermanentError(f"GitHub 404 for {method} {path}", code="github_not_found")
            if resp.status_code in (401, 403):
                raise PermanentError(f"GitHub {resp.status_code} for {method} {path}",
                                     code="github_forbidden")  # fmt: skip
            return resp
        raise TransientError("GitHub retries exhausted")  # pragma: no cover

    async def get_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        resp.raise_for_status()
        return dict(resp.json())

    async def list_pull_files(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page = 1
        while len(files) < MAX_FILES_API:
            resp = await self._request(
                "GET", f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )  # fmt: skip
            resp.raise_for_status()
            batch = resp.json()
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    async def get_commit_sha(self, owner: str, repo: str, ref: str) -> str:
        resp = await self._request("GET", f"/repos/{owner}/{repo}/commits/{ref}")
        resp.raise_for_status()
        return str(resp.json()["sha"])

    async def compare(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{owner}/{repo}/compare/{base}...{head}",
                                   params={"per_page": 100})  # fmt: skip
        resp.raise_for_status()
        return dict(resp.json())

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> bytes | None:
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref},
            accept="application/vnd.github.raw+json",
        )  # fmt: skip
        return resp.content if resp.status_code == 200 else None

    async def download_tarball(
        self, owner: str, repo: str, sha: str, dest: Path, max_bytes: int = 200 * 1024 * 1024
    ) -> Path:
        """Stream the repo archive at `sha` to `dest`, enforcing a size cap."""
        token = await self._tokens.token_for(self._inst)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        url = f"{self._api}/repos/{owner}/{repo}/tarball/{sha}"
        total = 0
        self.calls += 1
        try:
            async with self._http.stream(
                "GET", url, headers=headers, follow_redirects=True
            ) as resp:
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise TransientError(f"GitHub tarball {resp.status_code}")
                if resp.status_code in (401, 403, 404):
                    raise PermanentError(f"tarball {resp.status_code}", code="github_forbidden")
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise PermanentError("repository archive exceeds size limit",
                                                 code="repo_too_large")  # fmt: skip
                        fh.write(chunk)
        except httpx.TransportError as exc:
            raise TransientError(f"tarball network error: {exc!r}") from exc
        return dest

    async def create_review(
        self, owner: str, repo: str, number: int, payload: dict[str, Any]
    ) -> httpx.Response:
        """POST a pull request review. Returns the raw response: the publisher handles 422."""
        return await self._request(
            "POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload
        )

    async def list_review_comments(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}/comments",
                                       params={"per_page": 100, "page": page})  # fmt: skip
            resp.raise_for_status()
            batch = resp.json()
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1


def _backoff(attempt: int) -> float:
    return random.uniform(0, min(20.0, 0.5 * 2**attempt))  # noqa: S311 - jitter, not crypto


def _retry_delay(resp: httpx.Response) -> float | None:
    """Seconds to wait if the response is a retryable failure, else None."""
    if resp.status_code >= 500:
        return _backoff(1)
    if resp.status_code in (403, 429):
        if "retry-after" in resp.headers:
            return float(resp.headers["retry-after"])
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = float(resp.headers.get("x-ratelimit-reset", time.time() + 60))
            return max(1.0, reset - time.time())
        if resp.status_code == 429:
            return _backoff(1)
    return None
