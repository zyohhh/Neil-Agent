"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYSTEM_PROMPT = """You are Neil Agent, a helpful local coding assistant.
Give accurate, practical, and concise answers. Explain unfamiliar programming
concepts clearly, and say when you are uncertain instead of inventing facts."""


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
    system_prompt: str = Field(
        default=DEFAULT_SYSTEM_PROMPT,
        min_length=1,
        description="System instruction sent with every model request.",
    )
    thinking_enabled: bool = Field(
        default=False,
        description="Whether DeepSeek thinking mode is enabled.",
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

    @field_validator("system_prompt")
    @classmethod
    def system_prompt_must_not_be_blank(cls, value: str) -> str:
        """Reject prompts that contain only whitespace."""

        if not value.strip():
            raise ValueError("system prompt must not be blank")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache application settings on first use."""

    # The required API key is supplied by the environment or .env at runtime.
    return Settings()  # type: ignore[call-arg]
