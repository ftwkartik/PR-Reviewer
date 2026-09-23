"""Anthropic provider (Messages API + structured outputs).

Structured output uses `output_config.format` (JSON schema). Forced `tool_choice` is deliberately
not used: it is rejected by the current Opus/Sonnet models. Thinking is left at the model
default; depth is controlled with the optional `effort` setting.
"""

from typing import Any

import anthropic

from app.core.errors import PermanentError, TransientError
from app.llm.base import LLMUsage, RawCompletion, StructuredProvider, Turn


class AnthropicProvider(StructuredProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 120.0,
        max_attempts: int = 4,
        effort: str = "",
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        if not api_key and client is None:
            raise PermanentError("ANTHROPIC_API_KEY is not configured", code="llm_config")
        if not model:
            raise PermanentError("LLM_MODEL is not configured", code="llm_config")
        # SDK retries are off: our retry layer adds jitter, structured logs, one shared policy.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=0
        )
        self.model, self.max_attempts, self._effort = model, max_attempts, effort

    async def _complete(
        self, system: str, turns: list[Turn], schema: dict[str, object], max_tokens: int
    ) -> RawCompletion:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self._effort:
            output_config["effort"] = self._effort
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # Static system prompt first + cache breakpoint: identical across every call.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": t.role, "content": t.content} for t in turns],
            "output_config": output_config,
        }
        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise TransientError("anthropic rate limited", retry_after=_retry_after(exc)) from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise TransientError(f"anthropic connection error: {exc!r}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code in (408, 409, 529):
                raise TransientError(
                    f"anthropic {exc.status_code}", retry_after=_retry_after(exc)
                ) from exc
            if exc.status_code in (401, 403):
                raise PermanentError("anthropic rejected credentials", code="llm_auth") from exc
            raise PermanentError(f"anthropic rejected the request ({exc.status_code}): "
                                 f"{exc.message}",
                                 code="llm_bad_request") from exc  # fmt: skip

        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            raise PermanentError(
                f"model declined this request (category={category})", code="llm_refusal"
            )
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        return RawCompletion(
            text=text,
            usage=LLMUsage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                calls=1,
            ),  # fmt: skip
            request_id=getattr(resp, "_request_id", None),
            stop_reason=resp.stop_reason,
        )


def _retry_after(exc: anthropic.APIStatusError | anthropic.RateLimitError) -> float | None:
    raw = exc.response.headers.get("retry-after")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
