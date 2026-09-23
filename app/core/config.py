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

    llm_provider: Literal["anthropic", "openai", "fake"] = "anthropic"
    llm_model: str = ""
    llm_timeout_s: float = 120.0
    llm_max_retries: int = Field(4, ge=0, le=10)
    llm_max_output_tokens: int = 8000
    llm_effort: str = ""  # optional: low|medium|high|xhigh|max (omit for model default)
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")

    embedding_provider: Literal["voyage", "openai", "hash"] = "hash"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
