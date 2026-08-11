"""Fail-closed construction of configured ChatModel providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Protocol, cast

from ..agent import ChatModel
from ..config import Settings
from ..schemas import ActivityEvent
from .base import ProviderId
from .errors import ProviderNotImplementedError

RetryHandler = Callable[[ActivityEvent], None]


class ProviderBuilder(Protocol):
    """Constructor shape registered in ProviderFactory."""

    def __call__(
        self,
        settings: Settings,
        *,
        retry_handler: RetryHandler | None = None,
    ) -> ChatModel: ...


class ProviderFactory:
    """Build only explicitly registered providers without implicit fallback."""

    def __init__(self, builders: Mapping[ProviderId, ProviderBuilder]) -> None:
        self._builders = MappingProxyType(dict(builders))

    @property
    def registered_providers(self) -> tuple[ProviderId, ...]:
        """Return registered providers in stable identifier order."""

        return tuple(sorted(self._builders, key=str))

    def create(
        self,
        settings: Settings,
        *,
        retry_handler: RetryHandler | None = None,
    ) -> ChatModel:
        """Construct the selected provider or reject it before network access."""

        provider = settings.llm_provider
        builder = self._builders.get(provider)
        if builder is None:
            raise ProviderNotImplementedError(
                f"Provider '{provider.value}' 的协议适配器尚未实现。",
                provider=provider,
            )
        return builder(settings, retry_handler=retry_handler)


def create_provider(
    settings: Settings,
    *,
    retry_handler: RetryHandler | None = None,
    deepseek_builder: ProviderBuilder | None = None,
    claude_builder: ProviderBuilder | None = None,
    openai_builder: ProviderBuilder | None = None,
) -> ChatModel:
    """Create the configured runtime using the shipped provider adapters."""

    if deepseek_builder is None:
        from ..llm import LLMClient

        deepseek_builder = cast(ProviderBuilder, LLMClient)
    if claude_builder is None:
        from .claude import ClaudeProvider

        claude_builder = cast(ProviderBuilder, ClaudeProvider)
    if openai_builder is None:
        from .openai import OpenAIProvider

        openai_builder = cast(ProviderBuilder, OpenAIProvider)
    factory = ProviderFactory(
        {
            ProviderId.DEEPSEEK: deepseek_builder,
            ProviderId.CLAUDE: claude_builder,
            ProviderId.OPENAI: openai_builder,
        }
    )
    return factory.create(settings, retry_handler=retry_handler)
