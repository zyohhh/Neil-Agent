"""Backward-compatible DeepSeek model entry point."""

from __future__ import annotations

from time import sleep

from anthropic import Anthropic

from .config import Settings
from .providers.anthropic_runtime import RetryHandler, Sleeper
from .providers.deepseek import DEEPSEEK_DESCRIPTOR, DeepSeekProvider


class LLMClient(DeepSeekProvider):
    """Compatibility facade retained for existing DeepSeek integrations."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Anthropic | None = None,
        retry_handler: RetryHandler | None = None,
        sleeper: Sleeper = sleep,
    ) -> None:
        super().__init__(
            settings,
            client=client,
            retry_handler=retry_handler,
            sleeper=sleeper,
            client_factory=Anthropic,
        )


__all__ = ["DEEPSEEK_DESCRIPTOR", "LLMClient"]
