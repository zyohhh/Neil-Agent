"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for Neil Agent.

    Values can be provided through environment variables or a local ``.env``
    file. Environment variables take precedence over values in ``.env``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    deepseek_api_key: SecretStr = Field(
        min_length=1,
        description="API key created in the DeepSeek platform.",
    )
    deepseek_base_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("https://api.deepseek.com/anthropic"),
        description="DeepSeek Anthropic-compatible API endpoint.",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        min_length=1,
        description="DeepSeek model identifier.",
    )
    max_tokens: int = Field(
        default=8192,
        ge=1,
        description="Maximum number of tokens generated in one model response.",
    )
    max_rounds: int = Field(
        default=20,
        ge=1,
        description="Maximum number of conversation or agent-loop rounds.",
    )
    request_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Model request timeout in seconds.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache application settings on first use."""

    # The required API key is supplied by the environment or .env at runtime.
    return Settings()  # type: ignore[call-arg]
