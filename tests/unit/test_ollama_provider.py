import json

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import PermanentError, TransientError
from app.domain.review import ReviewResult
from app.llm.base import LLMRequest, LLMUsage
from app.llm.factory import make_llm_provider
from app.llm.ollama import OllamaProvider
from app.llm.pricing import estimate_cost_usd

URL = "http://ollama.test:11434/api/chat"
GOOD = ReviewResult(summary="Looks fine.", overall_risk="none", findings=[]).model_dump_json()


def ok_body(content: str = GOOD, prompt: int = 120, out: int = 30, reason: str = "stop") -> dict:  # type: ignore[type-arg]
    return {"message": {"role": "assistant", "content": content}, "done": True, "done_reason": reason,
            "prompt_eval_count": prompt, "eval_count": out}  # fmt: skip


def provider(**kw) -> OllamaProvider:  # type: ignore[no-untyped-def]
    return OllamaProvider(
        "qwen2.5-coder:3b", base_url="http://ollama.test:11434", max_attempts=2, **kw
    )


def req() -> LLMRequest[ReviewResult]:
    return LLMRequest("sys", "user", ReviewResult, max_output_tokens=8000)


async def test_request_shape_and_usage_mapping() -> None:
    with respx.mock() as m:
        route = m.post(URL).respond(200, json=ok_body())
        res = await provider(num_ctx=8192).generate(req())
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "qwen2.5-coder:3b" and body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["format"]["type"] == "object" and "findings" in body["format"]["properties"]
    opts = body["options"]
    assert opts["num_ctx"] == 8192 and opts["temperature"] == 0.0
    assert opts["num_predict"] == 2048  # output is capped to a quarter of the window, not 8000
    assert body["keep_alive"] == "30m"
    assert res.parsed.overall_risk == "none" and res.model == "qwen2.5-coder:3b"
    assert (res.usage.input_tokens, res.usage.output_tokens, res.usage.calls) == (120, 30, 1)
    assert not res.repaired and res.request_ids == []


async def test_cost_is_unknown_for_local_models() -> None:
    assert estimate_cost_usd("qwen2.5-coder:3b", LLMUsage(10, 5)) is None


async def test_truncated_generation_triggers_repair() -> None:
    with respx.mock() as m:
        route = m.post(URL)
        route.side_effect = [httpx.Response(200, json=ok_body('{"summary": "cut', reason="length")),
                             httpx.Response(200, json=ok_body())]  # fmt: skip
        res = await provider().generate(req())
    assert res.repaired and route.call_count == 2


async def test_oversized_prompt_refused_before_sending() -> None:
    """Ollama truncates silently, so an over-long prompt must never be sent."""
    with respx.mock(assert_all_called=False) as m:
        route = m.post(URL).respond(200, json=ok_body())
        big = LLMRequest("sys", "x" * 30_000, ReviewResult, max_output_tokens=1000)
        with pytest.raises(PermanentError) as ei:
            await provider(num_ctx=4096).generate(big)
    assert ei.value.code == "llm_context_overflow" and route.call_count == 0


async def test_prompt_just_inside_the_window_is_sent() -> None:
    with respx.mock() as m:
        m.post(URL).respond(200, json=ok_body())
        fits = LLMRequest("sys", "x" * 6_000, ReviewResult, max_output_tokens=1000)
        assert (await provider(num_ctx=4096).generate(fits)).parsed.summary


async def test_budgeted_prompts_always_pass_preflight() -> None:
    """The app's budget (Settings.context_limit) must never produce a prompt the provider refuses."""
    s = Settings(_env_file=None, llm_provider="ollama", ollama_num_ctx=16384)  # type: ignore[call-arg]
    worst_chars = s.context_limit * 4  # the app's estimator is ~4 chars/token
    with respx.mock() as m:
        m.post(URL).respond(200, json=ok_body())
        p = provider(num_ctx=s.ollama_num_ctx)
        await p.generate(
            LLMRequest("s", "x" * worst_chars, ReviewResult, max_output_tokens=s.llm_output_cap)
        )


async def test_missing_model_is_permanent_with_pull_hint() -> None:
    with respx.mock() as m:
        route = m.post(URL).respond(404, json={"error": "model 'x' not found"})
        with pytest.raises(PermanentError) as ei:
            await provider().generate(req())
    assert (
        ei.value.code == "llm_model_missing"
        and "ollama pull" in str(ei.value)
        and route.call_count == 1
    )


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_server_errors_retried_then_transient(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.core.retry.asyncio.sleep", no_sleep)
    with respx.mock() as m:
        route = m.post(URL).respond(status)
        with pytest.raises(TransientError):
            await provider().generate(req())
    assert route.call_count == 2  # max_attempts


async def test_connection_refused_and_timeout_are_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.core.retry.asyncio.sleep", no_sleep)
    for exc in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow")):
        with respx.mock() as m:
            m.post(URL).mock(side_effect=exc)
            with pytest.raises(TransientError):
                await provider().generate(req())


async def test_transient_then_success() -> None:
    with respx.mock() as m:
        route = m.post(URL)
        route.side_effect = [httpx.Response(503), httpx.Response(200, json=ok_body())]
        res = await OllamaProvider(
            "m", base_url="http://ollama.test:11434", max_attempts=3
        ).generate(req())
    assert res.parsed.summary == "Looks fine." and route.call_count == 2


@pytest.mark.parametrize(
    "resp",
    [httpx.Response(400, json={"error": "bad option"}), httpx.Response(200, text="<html>"),
     httpx.Response(200, json=["not", "a", "dict"]), httpx.Response(200, json={"error": "llama runner crashed"})],
)  # fmt: skip
async def test_bad_or_error_bodies_are_permanent(resp: httpx.Response) -> None:
    with respx.mock() as m:
        m.post(URL).mock(return_value=resp)
        with pytest.raises(PermanentError):
            await provider().generate(req())


def test_config_error_and_factory_selection() -> None:
    with pytest.raises(PermanentError):
        OllamaProvider("")
    s = Settings(_env_file=None, llm_provider="ollama", llm_model="qwen2.5-coder:3b",  # type: ignore[call-arg]
                 ollama_base_url="http://host.docker.internal:11434", ollama_num_ctx=8192)  # fmt: skip
    p = make_llm_provider(s)
    assert isinstance(p, OllamaProvider) and p.name == "ollama" and p.model == "qwen2.5-coder:3b"
    assert p.url == "http://host.docker.internal:11434/api/chat" and p.num_ctx == 8192


def test_cloud_providers_still_selectable() -> None:
    from app.llm.anthropic import AnthropicProvider
    from app.llm.openai import OpenAIProvider

    a = make_llm_provider(Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="k"))  # type: ignore[call-arg]
    o = make_llm_provider(
        Settings(_env_file=None, llm_provider="openai", openai_api_key="k", llm_model="m")
    )  # type: ignore[call-arg]
    assert isinstance(a, AnthropicProvider) and isinstance(o, OpenAIProvider)


def test_context_limit_respects_the_loaded_window() -> None:
    def limit(**kw) -> int:  # type: ignore[no-untyped-def]
        return Settings(_env_file=None, **kw).context_limit  # type: ignore[call-arg]

    assert limit(llm_provider="anthropic") == 24000 and limit(llm_provider="openai") == 24000
    small = limit(llm_provider="ollama", ollama_num_ctx=8192)
    big = limit(llm_provider="ollama", ollama_num_ctx=16384)
    assert small < 8192 and big < 16384 and small < big  # never exceeds the window
    assert (
        limit(llm_provider="ollama", ollama_num_ctx=131072) == 24000
    )  # still bounded by the app cap
    s = Settings(_env_file=None, llm_provider="ollama", ollama_num_ctx=8192)  # type: ignore[call-arg]
    assert s.llm_output_cap == 1024 and s.context_limit + s.llm_output_cap < s.ollama_num_ctx
