from app.core.config import Settings
from app.core.errors import PermanentError
from app.llm.anthropic import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider
from app.llm.openai import OpenAIProvider

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


def make_llm_provider(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case "anthropic":
            return AnthropicProvider(
                settings.anthropic_api_key.get_secret_value(),
                settings.llm_model or DEFAULT_ANTHROPIC_MODEL,
                timeout_s=settings.llm_timeout_s, max_attempts=settings.llm_max_retries + 1,
                effort=settings.llm_effort,
            )  # fmt: skip
        case "openai":
            return OpenAIProvider(
                settings.openai_api_key.get_secret_value(), settings.llm_model,
                timeout_s=settings.llm_timeout_s, max_attempts=settings.llm_max_retries + 1,
            )  # fmt: skip
        case "ollama":
            return OllamaProvider(
                settings.llm_model,
                base_url=settings.ollama_base_url,
                timeout_s=settings.llm_timeout_s,
                max_attempts=settings.llm_max_retries + 1,
                num_ctx=settings.ollama_num_ctx,
                keep_alive=settings.ollama_keep_alive,
            )
        case "fake":
            raise PermanentError("the fake provider must be constructed explicitly in tests",
                                 code="llm_config")  # fmt: skip
    raise PermanentError("unknown LLM provider", code="llm_config")  # pragma: no cover
