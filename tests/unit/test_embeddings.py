import json
import math

import httpx
import pytest
import respx

from app.core.errors import PermanentError, TransientError
from app.retrieval.embeddings import (
    HashEmbedder,
    OpenAIEmbedder,
    VoyageEmbedder,
    identifier_tokens,
)  # fmt: skip


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_identifier_tokens_split_camel_and_snake() -> None:
    toks = identifier_tokens("SessionManager.verify_token")
    assert {"sessionmanager", "session", "manager", "verify_token", "verify", "token"} <= set(toks)


async def test_hash_embedder_deterministic_and_normalised() -> None:
    e = HashEmbedder()
    a = (await e.embed_documents(["def verify_token(raw): pass"])).vectors[0]
    b = await e.embed_query("def verify_token(raw): pass")
    assert a == b and len(a) == 1024
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-6)


async def test_hash_embedder_ranks_lexical_overlap_higher() -> None:
    e = HashEmbedder()
    q = await e.embed_query("verify session token expiry")
    near = (await e.embed_documents(["def verify_token(session): check expiry of token"])).vectors[
        0
    ]
    far = (await e.embed_documents(["def render_chart(data): draw bars"])).vectors[0]
    assert cos(q, near) > cos(q, far)


def ok_body(n: int) -> dict:  # type: ignore[type-arg]
    return {"data": [{"index": i, "embedding": [float(i)] * 4} for i in reversed(range(n))],
            "usage": {"total_tokens": 10 * n}}  # fmt: skip


async def test_voyage_batches_orders_and_sends_input_type() -> None:
    with respx.mock() as m:
        route = m.post("https://api.voyageai.com/v1/embeddings").mock(
            side_effect=lambda req: httpx.Response(
                200,
                json=ok_body(len(json.loads(req.content)["input"])),
            )
        )
        emb = VoyageEmbedder("k", "voyage-code-3", 4)
        emb.batch_size = 2
        res = await emb.embed_documents(["a", "b", "c"])
    assert route.call_count == 2 and res.tokens == 30
    assert [v[0] for v in res.vectors] == [0.0, 1.0, 0.0]  # sorted by index within each batch
    body = json.loads(route.calls[0].request.content)
    assert body["input_type"] == "document" and body["output_dimension"] == 4


async def test_openai_payload_has_dimensions() -> None:
    with respx.mock() as m:
        route = m.post("https://api.openai.com/v1/embeddings").respond(200, json=ok_body(1))
        await OpenAIEmbedder("k", "text-embedding-3-small", 4).embed_query("x")
    assert json.loads(route.calls[0].request.content)["dimensions"] == 4


async def test_transient_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.core.retry.asyncio.sleep", no_sleep)
    with respx.mock() as m:
        route = m.post("https://api.voyageai.com/v1/embeddings").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "0"}),
                httpx.Response(200, json=ok_body(1)),
            ]
        )
        res = await VoyageEmbedder("k", "m", 4).embed_documents(["x"])
    assert route.call_count == 2 and len(res.vectors) == 1


async def test_permanent_error_not_retried() -> None:
    with respx.mock() as m:
        route = m.post("https://api.voyageai.com/v1/embeddings").respond(401)
        with pytest.raises(PermanentError):
            await VoyageEmbedder("k", "m", 4).embed_documents(["x"])
    assert route.call_count == 1


async def test_exhausted_retries_raise_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.core.retry.asyncio.sleep", no_sleep)
    with respx.mock() as m:
        route = m.post("https://api.voyageai.com/v1/embeddings").respond(503)
        with pytest.raises(TransientError):
            await VoyageEmbedder("k", "m", 4).embed_documents(["x"])
    assert route.call_count == 4


def test_missing_api_key_is_config_error() -> None:
    with pytest.raises(PermanentError):
        VoyageEmbedder("", "m", 4)
