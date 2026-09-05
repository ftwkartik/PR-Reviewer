"""Embedding providers behind one interface.

`HashEmbedder` is deterministic and offline (tests, dev, CI retrieval evals). Real providers
are chosen by EMBEDDING_PROVIDER. Dimension is fixed by the DB column (vector(1024)); switching
model means a new `model` string, which versions snapshots and keeps vectors from mixing.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.errors import PermanentError, TransientError
from app.core.retry import retry_transient

EMBEDDING_DIM = 1024
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    tokens: int = 0


class EmbeddingProvider(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult: ...
    async def embed_query(self, text: str) -> list[float]: ...


def identifier_tokens(text: str) -> list[str]:
    """Lowercase sub-tokens: `SessionManager.verify_token` -> session, manager, verify, token."""
    out: list[str] = []
    for ident in _IDENT.findall(text):
        out.append(ident.lower())
        parts = [p.lower() for p in _CAMEL.findall(ident.replace("_", " "))]
        if len(parts) > 1:
            out.extend(parts)
    return out


class HashEmbedder:
    """Signed feature hashing over identifier sub-tokens: lexical overlap, not semantics."""

    model = "hash-v1"

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in identifier_tokens(text):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(
            [self._embed(t) for t in texts], tokens=sum(len(t) // 4 for t in texts)
        )

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class _HTTPEmbedder:
    url = ""
    batch_size = 64

    def __init__(
        self, api_key: str, model: str, dim: int, http: httpx.AsyncClient | None = None
    ) -> None:
        if not api_key:
            raise PermanentError(
                f"{type(self).__name__}: API key not configured", code="embedding_config"
            )
        self._key, self.model, self.dim = api_key, model, dim
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    def _payload(self, inputs: list[str], input_type: str) -> dict[str, object]:
        raise NotImplementedError

    async def _post(self, inputs: list[str], input_type: str) -> tuple[list[list[float]], int]:
        async def call() -> tuple[list[list[float]], int]:
            try:
                resp = await self._http.post(
                    self.url, json=self._payload(inputs, input_type),
                    headers={"Authorization": f"Bearer {self._key}"},
                )  # fmt: skip
            except httpx.TransportError as exc:
                raise TransientError(f"embedding network error: {exc!r}") from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = (
                    float(resp.headers["retry-after"]) if "retry-after" in resp.headers else None
                )
                raise TransientError(
                    f"embedding provider {resp.status_code}", retry_after=retry_after
                )
            if resp.status_code >= 400:
                raise PermanentError(f"embedding provider rejected request ({resp.status_code})",
                                     code="embedding_rejected")  # fmt: skip
            body = resp.json()
            data = sorted(body["data"], key=lambda d: d["index"])
            usage = body.get("usage", {})
            return [d["embedding"] for d in data], int(usage.get("total_tokens", 0))

        return await retry_transient(call, operation="embed")

    async def embed_documents(self, texts: list[str]) -> EmbeddingResult:
        vectors: list[list[float]] = []
        tokens = 0
        for i in range(0, len(texts), self.batch_size):
            vecs, tok = await self._post(texts[i : i + self.batch_size], "document")
            vectors += vecs
            tokens += tok
        return EmbeddingResult(vectors, tokens)

    async def embed_query(self, text: str) -> list[float]:
        vecs, _ = await self._post([text], "query")
        return vecs[0]


class VoyageEmbedder(_HTTPEmbedder):
    url = "https://api.voyageai.com/v1/embeddings"

    def _payload(self, inputs: list[str], input_type: str) -> dict[str, object]:
        return {"input": inputs, "model": self.model, "input_type": input_type,
                "output_dimension": self.dim}  # fmt: skip


class OpenAIEmbedder(_HTTPEmbedder):
    url = "https://api.openai.com/v1/embeddings"

    def _payload(self, inputs: list[str], input_type: str) -> dict[str, object]:
        return {"input": inputs, "model": self.model, "dimensions": self.dim}


def make_embedder(settings: Settings) -> EmbeddingProvider:
    match settings.embedding_provider:
        case "hash":
            return HashEmbedder(settings.embedding_dim)
        case "voyage":
            return VoyageEmbedder(
                settings.voyage_api_key.get_secret_value(),
                settings.embedding_model or "voyage-code-3", settings.embedding_dim,
            )  # fmt: skip
        case "openai":
            return OpenAIEmbedder(
                settings.openai_api_key.get_secret_value(),
                settings.embedding_model or "text-embedding-3-small", settings.embedding_dim,
            )  # fmt: skip
    raise PermanentError("unknown embedding provider", code="embedding_config")  # pragma: no cover
