"""Local Ollama provider (`POST /api/chat`, JSON-schema `format`).

Notes specific to Ollama:
  * The model is loaded with a fixed context window (`num_ctx`). Ollama truncates an over-long
    prompt SILENTLY (and reports the post-truncation token count), which would corrupt a review.
    So we always send `num_ctx`, cap `num_predict`, and refuse up front, with a conservative size
    estimate, any prompt that cannot fit.
  * `temperature` is 0: a reviewer should be as repeatable as the model allows.
  * Local inference is free, so there is no pricing entry (cost is reported as unknown/zero).
"""

import httpx
import structlog

from app.core.errors import PermanentError, TransientError
from app.llm.base import LLMUsage, RawCompletion, StructuredProvider, Turn

log = structlog.get_logger()

# Conservative chars-per-token for code on Qwen-style tokenizers (typically 3-4). Used only for the
# pre-flight overflow check, because Ollama's own counters cannot reveal truncation.
_CHARS_PER_TOKEN = 3.0


class OllamaProvider(StructuredProvider):
    name = "ollama"

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://localhost:11434",
        timeout_s: float = 120.0,
        max_attempts: int = 4,
        num_ctx: int = 16384,
        keep_alive: str = "30m",
        temperature: float = 0.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not model:
            raise PermanentError("LLM_MODEL is not configured", code="llm_config")
        self.model = model
        self.max_attempts = max_attempts
        self.num_ctx, self._keep_alive, self._temperature = num_ctx, keep_alive, temperature
        self.url = f"{base_url.rstrip('/')}/api/chat"
        # Fast connect failure (Ollama down / firewall) but a generous read: cold model loads and
        # long prefills on a laptop GPU can take a while before the first byte.
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=timeout_s, write=30.0, pool=5.0)
        )

    async def _complete(
        self, system: str, turns: list[Turn], schema: dict[str, object], max_tokens: int
    ) -> RawCompletion:
        num_predict = min(max_tokens, max(256, self.num_ctx // 4))
        prompt_chars = len(system) + sum(len(t.content) for t in turns)
        if prompt_chars / _CHARS_PER_TOKEN + num_predict > self.num_ctx:
            # Ollama would truncate silently (and report a misleading prompt_eval_count).
            raise PermanentError(
                f"prompt (~{int(prompt_chars / _CHARS_PER_TOKEN)} tokens) plus {num_predict} "
                f"output tokens exceeds the {self.num_ctx}-token context window; "
                "lower MAX_CONTEXT_TOKENS or raise OLLAMA_NUM_CTX",
                code="llm_context_overflow",
            )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                *[{"role": t.role, "content": t.content} for t in turns],
            ],
            "stream": False,
            "format": schema,
            "keep_alive": self._keep_alive,
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": num_predict,
                "temperature": self._temperature,
            },
        }
        try:
            resp = await self._http.post(self.url, json=body)
        except httpx.TransportError as exc:  # includes connect/read timeouts
            raise TransientError(f"ollama connection error: {exc!r}") from exc

        if resp.status_code == 404:
            raise PermanentError(
                f"ollama has no model '{self.model}' (run `ollama pull {self.model}`)",
                code="llm_model_missing",
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"ollama {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(
                f"ollama rejected the request ({resp.status_code}): {_error_text(resp)}",
                code="llm_bad_request",
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise PermanentError(
                "ollama returned a non-JSON body", code="llm_bad_response"
            ) from exc
        if not isinstance(data, dict):
            raise PermanentError("ollama returned an unexpected body", code="llm_bad_response")
        if data.get("error"):
            raise PermanentError(
                f"ollama error: {str(data['error'])[:300]}", code="llm_bad_request"
            )

        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        message = data.get("message") or {}
        return RawCompletion(
            text=message.get("content") or "",
            usage=LLMUsage(
                input_tokens=prompt_tokens,
                output_tokens=int(data.get("eval_count") or 0),
                calls=1,
            ),
            request_id=None,  # Ollama has no request ids
            stop_reason=data.get("done_reason"),
        )


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])[:300]
    except ValueError:
        pass
    return resp.text[:300]
