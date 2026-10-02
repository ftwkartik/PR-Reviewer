import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.core.errors import PermanentError, TransientError
from app.domain.review import ReviewResult
from app.llm.anthropic import AnthropicProvider
from app.llm.base import LLMRequest, LLMUsage, RawCompletion, StructuredProvider, Turn
from app.llm.openai import OpenAIProvider
from app.llm.pricing import estimate_cost_usd

GOOD = ReviewResult(
    summary="Looks fine overall.", overall_risk="none", findings=[]
).model_dump_json()


class Scripted(StructuredProvider):
    name, model = "scripted", "m"
    backoff_base = 0.0

    def __init__(self, replies: list[Any]) -> None:
        self.replies, self.calls = replies, []  # type: ignore[var-annotated]

    async def _complete(self, system, turns, schema, max_tokens):  # type: ignore[no-untyped-def]
        self.calls.append(list(turns))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return RawCompletion(r, LLMUsage(10, 5, calls=1), "req_1", "end_turn")


def req() -> LLMRequest[ReviewResult]:
    return LLMRequest("sys", "user", ReviewResult)


async def test_valid_output_parsed_with_usage_and_request_id() -> None:
    p = Scripted([GOOD])
    res = await p.generate(req())
    assert res.parsed.overall_risk == "none" and res.usage.output_tokens == 5
    assert res.request_ids == ["req_1"] and not res.repaired


async def test_code_fenced_json_tolerated() -> None:
    assert (await Scripted([f"```json\n{GOOD}\n```"]).generate(req())).parsed.summary


async def test_malformed_output_gets_one_repair_with_error_fed_back() -> None:
    p = Scripted(['{"summary": "x"}', GOOD])
    res = await p.generate(req())
    assert res.repaired and res.usage.calls == 2
    repair_turn = p.calls[1][-1]
    assert (
        repair_turn.role == "user"
        and "invalid" in repair_turn.content
        and "findings" in repair_turn.content  # first required field in the schema
    )


async def test_second_malformed_output_is_permanent() -> None:
    p = Scripted(["nope", "still nope"])
    with pytest.raises(PermanentError) as ei:
        await p.generate(req())
    assert ei.value.code == "llm_malformed_output"


async def test_transient_errors_retried_then_succeed() -> None:
    p = Scripted([TransientError("429", retry_after=0), TransientError("503", retry_after=0), GOOD])
    assert (await p.generate(req())).parsed.overall_risk == "none" and not p.replies


async def test_permanent_error_not_retried() -> None:
    p = Scripted([PermanentError("bad", code="llm_auth"), GOOD])
    with pytest.raises(PermanentError):
        await p.generate(req())
    assert len(p.replies) == 1


async def test_truncated_output_triggers_repair_request() -> None:
    class Trunc(Scripted):
        async def _complete(self, system, turns, schema, max_tokens):  # type: ignore[no-untyped-def]
            raw = await super()._complete(system, turns, schema, max_tokens)
            raw.stop_reason = "max_tokens" if len(self.calls) == 1 else "end_turn"
            return raw

    p = Trunc(['{"summary": "cut o', GOOD])
    res = await p.generate(req())
    assert res.repaired and "truncated" in p.calls[1][-1].content


# ---- Anthropic adapter (mocked SDK client) -----------------------------------------------------


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result, self.kwargs = result, {}  # type: ignore[var-annotated]

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def message(text: str, stop: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, _request_id="req_abc",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80,
                              cache_creation_input_tokens=0),
        stop_details=SimpleNamespace(category="cyber"),
    )  # fmt: skip


def anthropic_provider(result: Any, **kw: Any) -> tuple[AnthropicProvider, FakeMessages]:
    fm = FakeMessages(result)
    client = SimpleNamespace(messages=fm)
    return AnthropicProvider("k", "claude-opus-5-5", client=client, **kw), fm  # type: ignore[arg-type]


async def test_anthropic_request_shape_uses_structured_output_not_forced_tool() -> None:
    p, fm = anthropic_provider(message(GOOD), effort="medium")
    res = await p.generate(req())
    kw = fm.kwargs
    assert kw["model"] == "claude-opus-5-5" and "tool_choice" not in kw and "thinking" not in kw
    assert (
        kw["output_config"]["format"]["type"] == "json_schema"
        and kw["output_config"]["effort"] == "medium"
    )
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert res.request_ids == ["req_abc"] and res.usage.cache_read_tokens == 80


async def test_anthropic_refusal_is_permanent() -> None:
    p, _ = anthropic_provider(message("", stop="refusal"))
    with pytest.raises(PermanentError) as ei:
        await p.generate(req())
    assert ei.value.code == "llm_refusal"


def status_error(cls: str, code: int, headers: dict[str, str] | None = None) -> Exception:
    import anthropic

    resp = httpx.Response(code, headers=headers or {}, request=httpx.Request("POST", "https://x"))
    return getattr(anthropic, cls)(message="boom", response=resp, body=None)


@pytest.mark.parametrize(
    ("cls", "code", "kind"),
    [("RateLimitError", 429, TransientError), ("InternalServerError", 500, TransientError),
     ("AuthenticationError", 401, PermanentError), ("BadRequestError", 400, PermanentError)],
)  # fmt: skip
async def test_anthropic_error_mapping(cls: str, code: int, kind: type[Exception]) -> None:
    p, _ = anthropic_provider(status_error(cls, code, {"retry-after": "0"}), max_attempts=1)
    with pytest.raises(kind):
        await p.generate(req())


async def test_anthropic_config_errors() -> None:
    with pytest.raises(PermanentError):
        AnthropicProvider("", "claude-opus-5-5")
    with pytest.raises(PermanentError):
        AnthropicProvider("k", "")


# ---- OpenAI adapter: same contract through the same base class ------------------------------------


async def test_openai_adapter_satisfies_same_contract() -> None:
    with respx.mock() as m:
        route = m.post("https://api.openai.com/v1/chat/completions").respond(
            200, headers={"x-request-id": "oa_1"},
            json={"choices": [{"message": {"content": GOOD}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )  # fmt: skip
        res = await OpenAIProvider("k", "gpt-x").generate(req())
    assert (
        res.parsed.overall_risk == "none"
        and res.request_ids == ["oa_1"]
        and res.usage.input_tokens == 7
    )
    body = json.loads(route.calls[0].request.content)
    assert (
        body["response_format"]["type"] == "json_schema" and body["messages"][0]["role"] == "system"
    )


async def test_openai_refusal_and_rate_limit() -> None:
    with respx.mock() as m:
        m.post("https://api.openai.com/v1/chat/completions").respond(
            200,
            json={
                "choices": [
                    {"message": {"content": None, "refusal": "no"}, "finish_reason": "stop"}
                ]
            },
        )
        with pytest.raises(PermanentError):
            await OpenAIProvider("k", "gpt-x").generate(req())
    with respx.mock() as m:
        m.post("https://api.openai.com/v1/chat/completions").respond(
            429, headers={"retry-after": "0"}
        )
        with pytest.raises(TransientError):
            await OpenAIProvider("k", "gpt-x", max_attempts=1).generate(req())


def test_cost_estimate() -> None:
    u = LLMUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)
    assert estimate_cost_usd("claude-opus-5-5", u) == pytest.approx(4.0 + 2.0 + 0.2)
    assert estimate_cost_usd("unknown-model", u) is None


def test_schema_roundtrip_is_strict() -> None:
    class Extra(BaseModel):
        x: int

    assert ReviewResult.model_json_schema()["additionalProperties"] is False
    with pytest.raises(ValueError):
        ReviewResult.model_validate_json(
            '{"summary":"s","overall_risk":"none","findings":[],"bogus":1}'
        )
    _ = (Extra, Turn)


def test_schema_puts_findings_before_summary_and_risk() -> None:
    """Regression: with summary/risk first, small models answer 'no issues' and emit no findings."""
    from anthropic import transform_schema

    from app.domain.review import SynthesisResult

    keys = list(transform_schema(ReviewResult)["properties"])
    assert keys.index("findings") < keys.index("overall_risk") < keys.index("summary")
    skeys = list(transform_schema(SynthesisResult)["properties"])
    assert skeys.index("verdicts") < skeys.index("summary")
