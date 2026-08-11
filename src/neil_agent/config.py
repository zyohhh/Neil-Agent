"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .providers.base import ProviderId

DEFAULT_SYSTEM_PROMPT = """You are Neil Agent, a helpful local coding assistant.
Give accurate, practical, and concise answers. Explain unfamiliar programming
concepts clearly, and say when you are uncertain instead of inventing facts."""
DEFAULT_DEEPSEEK_BASE_URL = AnyHttpUrl("https://api.deepseek.com/anthropic")
DEFAULT_CLAUDE_BASE_URL = AnyHttpUrl("https://api.anthropic.com")
DEFAULT_OLLAMA_BASE_URL = AnyHttpUrl("http://localhost:11434/v1")
DEFAULT_VLLM_BASE_URL = AnyHttpUrl("http://localhost:8000/v1")


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

    llm_provider: ProviderId = Field(
        default=ProviderId.DEEPSEEK,
        description="Selected model provider; defaults to the legacy DeepSeek path.",
    )
    llm_model: str | None = Field(
        default=None,
        min_length=1,
        description="Provider model identifier; overrides the legacy DeepSeek model.",
    )
    llm_base_url: AnyHttpUrl | None = Field(
        default=None,
        description="Optional endpoint override for the selected provider.",
    )
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        description="API key created in the DeepSeek platform.",
    )
    deepseek_base_url: AnyHttpUrl = Field(
        default=DEFAULT_DEEPSEEK_BASE_URL,
        description="DeepSeek Anthropic-compatible API endpoint.",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        min_length=1,
        description="DeepSeek model identifier.",
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        description="API key used by the Claude provider.",
    )
    openai_api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        description="API key used by the OpenAI provider.",
    )
    system_prompt: str = Field(
        default=DEFAULT_SYSTEM_PROMPT,
        min_length=1,
        description="System instruction sent with every model request.",
    )
    thinking_enabled: bool = Field(
        default=False,
        description="Whether provider reasoning mode is enabled when supported.",
    )
    claude_thinking_mode: Literal["adaptive", "enabled"] = Field(
        default="adaptive",
        description="Claude reasoning mode: adaptive or a fixed manual budget.",
    )
    claude_thinking_budget_tokens: int = Field(
        default=1024,
        description="Manual Claude thinking budget; ignored in adaptive mode.",
    )
    max_tokens: int = Field(
        default=8192,
        ge=1,
        description="Maximum number of tokens generated in one model response.",
    )
    max_rounds: int = Field(
        default=20,
        ge=1,
        description="Maximum number of conversation rounds retained in history.",
    )
    max_context_chars: int = Field(
        default=120_000,
        ge=1_000,
        description="Approximate request character budget for model context.",
    )
    max_context_tokens: int | None = Field(
        default=None,
        ge=1_000,
        description=(
            "Optional approximate request token budget; character budgeting "
            "remains active as a fallback."
        ),
    )
    max_tool_rounds: int = Field(
        default=5,
        ge=1,
        description="Maximum tool-use cycles allowed for one user request.",
    )
    workspace_root: Path = Field(
        default=Path("."),
        description="Directory boundary for local project tools.",
    )
    request_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Model request timeout in seconds.",
    )
    max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Maximum retries for one transient model request failure.",
    )
    retry_base_delay: float = Field(
        default=1.0,
        ge=0,
        le=60,
        description="Initial model retry delay in seconds.",
    )
    retry_max_delay: float = Field(
        default=8.0,
        gt=0,
        le=60,
        description="Maximum delay before one model request retry.",
    )
    command_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Timeout in seconds for an approved local command.",
    )
    max_command_output_chars: int = Field(
        default=20_000,
        ge=1_000,
        description="Maximum command output returned to the model.",
    )
    sandbox_backend: Literal["disabled", "windows-sandbox"] = Field(
        default="disabled",
        description=(
            "Fail-closed OS sandbox backend for explicitly enabled general commands."
        ),
    )
    sandbox_certification_root: Path | None = Field(
        default=None,
        description="Absolute root of a reviewed Windows Sandbox evidence bundle.",
    )
    sandbox_trusted_reviewer: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Out-of-band pinned independent reviewer identity.",
    )
    sandbox_trusted_review_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Out-of-band pinned independent review SHA-256.",
    )
    audit_log_enabled: bool = Field(
        default=False,
        description="Write metadata-only lifecycle events to a local JSONL log.",
    )
    audit_log_max_bytes: int = Field(
        default=1_000_000,
        ge=10_000,
        le=10_000_000,
        description="Maximum audit JSONL size before one-file rotation.",
    )

    @field_validator("system_prompt")
    @classmethod
    def system_prompt_must_not_be_blank(cls, value: str) -> str:
        """Reject prompts that contain only whitespace."""

        if not value.strip():
            raise ValueError("system prompt must not be blank")
        return value

    @field_validator("llm_model", "deepseek_model")
    @classmethod
    def model_name_must_not_be_blank(cls, value: str | None) -> str | None:
        """Reject model identifiers containing only whitespace."""

        if value is not None and not value.strip():
            raise ValueError("model identifier must not be blank")
        return value

    @model_validator(mode="after")
    def validate_provider_and_retry_settings(self) -> Self:
        """Conditionally validate only the selected provider and retry policy."""

        if self.retry_base_delay > self.retry_max_delay:
            raise ValueError("retry base delay cannot exceed retry max delay")
        required_key = {
            ProviderId.DEEPSEEK: ("DEEPSEEK_API_KEY", self.deepseek_api_key),
            ProviderId.CLAUDE: ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            ProviderId.OPENAI: ("OPENAI_API_KEY", self.openai_api_key),
        }.get(self.llm_provider)
        if required_key is not None and required_key[1] is None:
            raise ValueError(
                f"{required_key[0]} is required when "
                f"LLM_PROVIDER={self.llm_provider.value}"
            )
        if self.llm_provider is not ProviderId.DEEPSEEK and self.llm_model is None:
            raise ValueError(
                f"LLM_MODEL is required when LLM_PROVIDER={self.llm_provider.value}"
            )
        if (
            self.llm_provider is ProviderId.CLAUDE
            and self.thinking_enabled
            and self.claude_thinking_mode == "enabled"
        ):
            if self.claude_thinking_budget_tokens < 1024:
                raise ValueError("Claude thinking budget must be at least 1024 tokens")
            if self.claude_thinking_budget_tokens >= self.max_tokens:
                raise ValueError("Claude thinking budget must be less than max tokens")
        return self

    @property
    def selected_model(self) -> str:
        """Return the model identifier for the selected provider."""

        if self.llm_model is not None:
            return self.llm_model
        return self.deepseek_model

    @property
    def selected_base_url(self) -> AnyHttpUrl | None:
        """Return an explicit or compatibility endpoint for the selected provider."""

        if self.llm_base_url is not None:
            return self.llm_base_url
        if self.llm_provider is ProviderId.DEEPSEEK:
            return self.deepseek_base_url
        if self.llm_provider is ProviderId.CLAUDE:
            return DEFAULT_CLAUDE_BASE_URL
        if self.llm_provider is ProviderId.OLLAMA:
            return DEFAULT_OLLAMA_BASE_URL
        if self.llm_provider is ProviderId.VLLM:
            return DEFAULT_VLLM_BASE_URL
        return None

    @property
    def selected_api_key(self) -> SecretStr | None:
        """Return only the selected provider key, never an unrelated secret."""

        return {
            ProviderId.DEEPSEEK: self.deepseek_api_key,
            ProviderId.CLAUDE: self.anthropic_api_key,
            ProviderId.OPENAI: self.openai_api_key,
            ProviderId.OLLAMA: None,
            ProviderId.VLLM: None,
        }[self.llm_provider]

    @property
    def selected_api_key_required(self) -> bool:
        """Return whether the selected provider needs a configured API key."""

        return self.llm_provider in {
            ProviderId.DEEPSEEK,
            ProviderId.CLAUDE,
            ProviderId.OPENAI,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache application settings on first use."""

    return Settings()
