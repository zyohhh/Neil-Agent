"""Backward-compatible DeepSeek model entry point."""

from __future__ import annotations

from time import sleep
from warnings import warn

from anthropic import Anthropic

from .config import Settings
from .providers.anthropic_runtime import RetryHandler, Sleeper
from .providers.deepseek import DEEPSEEK_DESCRIPTOR, DeepSeekProvider


class LLMClient(DeepSeekProvider):
    """Deprecated 0.1.x facade; use ``DeepSeekProvider`` before version 0.2.0."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Anthropic | None = None,
        retry_handler: RetryHandler | None = None,
        sleeper: Sleeper = sleep,
    ) -> None:
        warn(
            "LLMClient is deprecated; import DeepSeekProvider from "
            "neil_agent.providers.deepseek before Neil Agent 0.2.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(
            settings,
            client=client,
            retry_handler=retry_handler,
            sleeper=sleeper,
            client_factory=Anthropic,
        )


__all__ = ["DEEPSEEK_DESCRIPTOR", "LLMClient"]
