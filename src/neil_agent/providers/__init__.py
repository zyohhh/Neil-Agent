"""Provider-neutral contracts shared by model protocol adapters."""

from .base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderId,
    ProviderTurnState,
    StopReason,
    WireProtocol,
)
from .errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderErrorCategory,
    ProviderInternalError,
    ProviderInvalidRequestError,
    ProviderNotImplementedError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    UnsupportedCapabilityError,
)

__all__ = [
    "ProviderAuthenticationError",
    "ProviderCapabilities",
    "ProviderConnectionError",
    "ProviderContextOverflowError",
    "ProviderDescriptor",
    "ProviderError",
    "ProviderErrorCategory",
    "ProviderId",
    "ProviderInternalError",
    "ProviderInvalidRequestError",
    "ProviderNotImplementedError",
    "ProviderProtocolError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderTurnState",
    "StopReason",
    "UnsupportedCapabilityError",
    "WireProtocol",
]
