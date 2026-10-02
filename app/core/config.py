from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://review:review@localhost:5432/review"
    redis_url: str = "redis://localhost:6379/0"

    github_app_id: str = ""
    github_private_key: SecretStr = SecretStr("")
    github_webhook_secret: SecretStr = SecretStr("")
    github_api_url: str = "https://api.github.com"

    llm_provider: Literal["anthropic", "openai", "ollama", "fake"] = "anthropic"
    llm_model: str = ""
    llm_timeout_s: float = 120.0
    llm_max_retries: int = Field(4, ge=0, le=10)
    llm_max_output_tokens: int = 8000
    llm_effort: str = ""  # optional: low|medium|high|xhigh|max (omit for model default)
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    ollama_base_url: str = "http://localhost:11434"
    # Ollama silently truncates prompts past num_ctx, so the app budgets to it (see context_limit).
    ollama_num_ctx: int = Field(16384, ge=2048)
    ollama_keep_alive: str = "30m"

    embedding_provider: Literal["voyage", "openai", "ollama", "hash"] = "hash"
    embedding_model: str = ""
    embedding_dim: int = 1024
    voyage_api_key: SecretStr = SecretStr("")

    api_key: SecretStr = SecretStr("")

    review_confidence_threshold: float = Field(0.75, ge=0, le=1)
    max_changed_lines: int = Field(3000, gt=0)
    max_files_per_review: int = Field(60, gt=0)
    max_patch_size_bytes: int = 200_000
    max_context_tokens: int = Field(24_000, gt=0)
    max_model_calls: int = Field(12, gt=0)
    max_webhook_body_bytes: int = 5_000_000
    token_estimate_safety: float = Field(0.7, gt=0, le=1)  # scale for local-model tokenizers
    review_synthesis: bool = (
        True  # PR-level LLM pass that may drop findings; weak local models hurt precision
    )
    webhook_dry_run: bool = (
        False  # True: webhook-triggered reviews are analysed but never published
    )
    rate_limit_per_minute: int = 60  # per API key / IP on /api/v1; 0 disables
    allowed_owners: str = ""  # comma-separated GitHub owners; empty = allow all installed repos
    daily_budget_usd: float = 0.0  # per-repository LLM spend cap over 24h; 0 disables
    findings_retention_days: int = 90
    metrics_token: SecretStr = SecretStr(
        ""
    )  # if set, /metrics requires 'Authorization: Bearer <token>'
    worker_metrics_port: int = 9102  # 0 disables the worker's Prometheus endpoint
    static_analyzers: str = "ruff,bandit,semgrep"  # comma list; '' disables
    analyzer_timeout_s: float = 30.0

    @property
    def llm_output_cap(self) -> int:
        """Max output tokens per call. Local models share their context window with the prompt."""
        if self.llm_provider == "ollama":
            return min(self.llm_max_output_tokens, self.ollama_num_ctx // 8)
        return self.llm_max_output_tokens

    @property
    def context_limit(self) -> int:
        """Token budget for ONE prompt (diff + context + overhead), in the app's token estimate.

        Cloud providers: MAX_CONTEXT_TOKENS. Ollama: also bounded by the model's loaded context
        window (num_ctx) minus the output reserve, scaled by a safety factor because the app's
        ~4 chars/token estimate undercounts code tokens for local tokenizers.
        """
        if self.llm_provider != "ollama":
            return self.max_context_tokens
        room = self.ollama_num_ctx - self.llm_output_cap
        return max(1024, min(self.max_context_tokens, int(room * self.token_estimate_safety)))


def is_owner_allowed(settings: Settings, owner: str) -> bool:
    allowed = {o.strip().lower() for o in settings.allowed_owners.split(",") if o.strip()}
    return not allowed or owner.lower() in allowed


@lru_cache
def get_settings() -> Settings:
    return Settings()
