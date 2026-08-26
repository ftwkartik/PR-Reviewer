import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.errors import PermanentError, TransientError
from app.github.auth import InstallationTokenProvider, make_app_jwt
from app.github.client import GitHubClient

API = "https://api.github.com"


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


def test_app_jwt_claims() -> None:
    priv, pub = _keypair()
    token = make_app_jwt("123", priv, now=1_000_000)
    claims = jwt.decode(token, pub, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 10 * 60


def test_missing_credentials_is_permanent() -> None:
    with pytest.raises(PermanentError):
        make_app_jwt("", "")


def _expiry(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def keys() -> tuple[str, str]:
    return _keypair()


async def test_installation_token_cached(keys) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(base_url=API) as m:
        route = m.post("/app/installations/9/access_tokens").respond(
            201, json={"token": "ghs_tok", "expires_at": _expiry(60)}
        )
        async with httpx.AsyncClient() as http:
            p = InstallationTokenProvider("1", keys[0], API, http)
            assert await p.token_for(9) == "ghs_tok"
            assert await p.token_for(9) == "ghs_tok"
        assert route.call_count == 1


async def test_installation_token_refreshed_near_expiry(keys) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(base_url=API) as m:
        route = m.post("/app/installations/9/access_tokens").respond(
            201,
            json={"token": "t", "expires_at": _expiry(2)},  # inside the 5 min skew
        )
        async with httpx.AsyncClient() as http:
            p = InstallationTokenProvider("1", keys[0], API, http)
            await p.token_for(9)
            await p.token_for(9)
        assert route.call_count == 2


async def test_installation_not_found_is_permanent(keys) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(base_url=API) as m:
        m.post("/app/installations/9/access_tokens").respond(404)
        async with httpx.AsyncClient() as http:
            with pytest.raises(PermanentError) as ei:
                await InstallationTokenProvider("1", keys[0], API, http).token_for(9)
        assert ei.value.code == "github_installation_unavailable"


class FakeTokens:
    async def token_for(self, _: int) -> str:
        return "tok"


async def _sleep(_: float) -> None:
    return None


def client(http: httpx.AsyncClient) -> GitHubClient:
    return GitHubClient(http, FakeTokens(), 1, API, sleep=_sleep)  # type: ignore[arg-type]


async def test_pagination_collects_all_files() -> None:
    page1 = [{"filename": f"f{i}.py"} for i in range(100)]
    page2 = [{"filename": "last.py"}]
    with respx.mock(base_url=API) as m:
        m.get("/repos/o/r/pulls/1/files", params={"page": "1"}).respond(200, json=page1)
        m.get("/repos/o/r/pulls/1/files", params={"page": "2"}).respond(200, json=page2)
        async with httpx.AsyncClient() as http:
            files = await client(http).list_pull_files("o", "r", 1)
    assert len(files) == 101


async def test_retries_5xx_then_succeeds() -> None:
    with respx.mock(base_url=API) as m:
        m.get("/repos/o/r/pulls/1").mock(
            side_effect=[httpx.Response(502), httpx.Response(200, json={"number": 1})]
        )
        async with httpx.AsyncClient() as http:
            c = client(http)
            assert (await c.get_pull("o", "r", 1))["number"] == 1
            assert c.calls == 2


async def test_rate_limit_waits_then_raises_transient_when_too_long() -> None:
    reset = str(int(time.time()) + 3600)
    with respx.mock(base_url=API) as m:
        m.get("/repos/o/r/pulls/1").respond(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(TransientError) as ei:
                await client(http).get_pull("o", "r", 1)
    assert ei.value.retry_after and ei.value.retry_after > 120


async def test_404_and_403_are_permanent() -> None:
    with respx.mock(base_url=API) as m:
        m.get("/repos/o/r/pulls/1").respond(404)
        m.get("/repos/o/r/pulls/2").respond(403)
        async with httpx.AsyncClient() as http:
            c = client(http)
            with pytest.raises(PermanentError):
                await c.get_pull("o", "r", 1)
            with pytest.raises(PermanentError):
                await c.get_pull("o", "r", 2)


async def test_network_error_retried_then_transient() -> None:
    with respx.mock(base_url=API) as m:
        m.get("/repos/o/r/pulls/1").mock(side_effect=httpx.ConnectError("boom"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(TransientError):
                await client(http).get_pull("o", "r", 1)


async def test_refuses_non_github_hosts() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(PermanentError) as ei:
            await client(http)._request("GET", "http://169.254.169.254/latest/meta-data")
    assert ei.value.code == "ssrf_blocked"
