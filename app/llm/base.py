"""Provider-neutral LLM interface.

The orchestrator depends only on `LLMProvider.generate`. Concrete providers implement the thin
`_complete` call; retry/backoff, JSON parsing, Pydantic validation and the single repair attempt
are shared here so every provider behaves identically.
"""

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol

import structlog
from pydantic import BaseModel, ValidationError

from app.core.errors import PermanentError
from app.core.retry import retry_transient

log = structlog.get_logger()


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    def add(self, other: "LLMUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls


@dataclass
class LLMRequest[T: BaseModel]:
    system: str
    user: str
    schema: type[T]
    purpose: str = "review"  # review | synthesis
    max_output_tokens: int = 8000


@dataclass
class LLMResult[T: BaseModel]:
    parsed: T
    usage: LLMUsage
    model: str
    request_ids: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    repaired: bool = False


@dataclass
class RawCompletion:
    text: str
    usage: LLMUsage
    request_id: str | None = None
    stop_reason: str | None = None


@dataclass
class Turn:
    role: str  # user | assistant
    content: str


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]: ...


class StructuredProvider(ABC):
    """Base class: subclasses implement `_complete`; everything else is shared."""

    name = "base"
    model = ""
    max_attempts = 4
    backoff_base = 1.0

    @abstractmethod
    async def _complete(
        self, system: str, turns: list[Turn], schema: dict[str, object], max_tokens: int
    ) -> RawCompletion:
        """One API call. Must raise TransientError / PermanentError; never return a refusal."""

    async def generate[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]:
        from anthropic import transform_schema

        schema = transform_schema(request.schema)
        turns = [Turn("user", request.user)]
        usage = LLMUsage()
        ids: list[str] = []
        started = time.monotonic()

        async def call(ts: list[Turn]) -> RawCompletion:
            raw = await retry_transient(
                lambda: self._complete(request.system, ts, schema, request.max_output_tokens),
                max_attempts=self.max_attempts,
                base=self.backoff_base,
                operation=f"{self.name}.{request.purpose}",
            )
            usage.add(raw.usage)
            if raw.request_id:
                ids.append(raw.request_id)
            log.info(
                "llm_call", provider=self.name, model=self.model, purpose=request.purpose,
                provider_request_id=raw.request_id, input_tokens=raw.usage.input_tokens,
                output_tokens=raw.usage.output_tokens, stop_reason=raw.stop_reason,
            )  # fmt: skip
            return raw

        raw = await call(turns)
        parsed, error = _parse(raw, request.schema)
        repaired = False
        if parsed is None:
            # One repair attempt: show the model exactly what was wrong with its output.
            repaired = True
            turns += [
                Turn("assistant", raw.text[:20000]),
                Turn("user", f"Your previous reply was invalid: {error}\n"
                             "Reply again with only a corrected object matching the schema."),
            ]  # fmt: skip
            raw = await call(turns)
            parsed, error = _parse(raw, request.schema)
            if parsed is None:
                raise PermanentError(f"model output failed validation after repair: {error}",
                                     code="llm_malformed_output")  # fmt: skip
        return LLMResult(parsed, usage, self.model, ids, time.monotonic() - started, repaired)


def _parse[T: BaseModel](raw: RawCompletion, schema: type[T]) -> tuple[T | None, str]:
    if raw.stop_reason in {"max_tokens", "length"}:
        return None, "output was truncated (hit the token limit); return fewer, shorter findings"
    try:
        return schema.model_validate_json(_strip_fences(raw.text)), ""
    except ValidationError as exc:
        first = exc.errors()[0]
        return None, f"{first['type']} at {'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"not valid JSON ({exc})"


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
