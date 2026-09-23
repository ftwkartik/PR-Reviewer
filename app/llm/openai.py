"""OpenAI provider (Chat Completions + json_schema response format).

Present to prove the abstraction: nothing outside `app/llm` knows which provider is active.
"""

import httpx

from app.core.errors import PermanentError, TransientError
from app.llm.base import LLMUsage, RawCompletion, StructuredProvider, Turn


class OpenAIProvider(StructuredProvider):
    name = "openai"
    url = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: str, model: str, *, timeout_s: float = 120.0, max_attempts: int = 4,
                 http: httpx.AsyncClient | None = None) -> None:  # fmt: skip
        if not api_key:
            raise PermanentError("OPENAI_API_KEY is not configured", code="llm_config")
        if not model:
            raise PermanentError("LLM_MODEL is not configured", code="llm_config")
        self._key, self.model, self.max_attempts = api_key, model, max_attempts
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def _complete(
        self, system: str, turns: list[Turn], schema: dict[str, object], max_tokens: int
    ) -> RawCompletion:
        body = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                *[{"role": t.role, "content": t.content} for t in turns],
            ],  # fmt: skip
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            },  # fmt: skip
        }
        try:
            resp = await self._http.post(
                self.url, json=body, headers={"Authorization": f"Bearer {self._key}"}
            )
        except httpx.TransportError as exc:
            raise TransientError(f"openai connection error: {exc!r}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            ra = resp.headers.get("retry-after")
            raise TransientError(
                f"openai {resp.status_code}", retry_after=float(ra) if ra else None
            )
        if resp.status_code in (401, 403):
            raise PermanentError("openai rejected credentials", code="llm_auth")
        if resp.status_code >= 400:
            raise PermanentError(
                f"openai rejected the request ({resp.status_code})", code="llm_bad_request"
            )
        data = resp.json()
        choice = data["choices"][0]
        if choice["message"].get("refusal"):
            raise PermanentError("model declined this request", code="llm_refusal")
        usage = data.get("usage", {})
        return RawCompletion(
            text=choice["message"].get("content") or "",
            usage=LLMUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                calls=1,
            ),  # fmt: skip
            request_id=resp.headers.get("x-request-id"),
            stop_reason=choice.get("finish_reason"),
        )
