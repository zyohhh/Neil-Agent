"""Stable, secret-safe error taxonomy for every model provider."""

from __future__ import annotations

from enum import StrEnum
from math import isfinite

from ..errors import LLMError
from .base import ProviderId


class ProviderErrorCategory(StrEnum):
    """Machine-readable provider failure categories."""

    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_OVERFLOW = "context_overflow"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PROTOCOL = "protocol"
    PROVIDER_INTERNAL = "provider_internal"


class ProviderError(LLMError):
    """Base error emitted after an SDK or wire failure is normalized."""

    category = ProviderErrorCategory.PROTOCOL
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: ProviderId,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        if not message.strip():
            raise ValueError("provider error message must not be blank")
        if status_code is not None and not 100 <= status_code <= 599:
            raise ValueError("provider status code must be a valid HTTP status")
        if retry_after is not None and (not isfinite(retry_after) or retry_after < 0):
            raise ValueError("provider retry delay must be finite and non-negative")
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after


class ProviderAuthenticationError(ProviderError):
    category = ProviderErrorCategory.AUTHENTICATION


class ProviderRateLimitError(ProviderError):
    category = ProviderErrorCategory.RATE_LIMIT
    retryable = True


class ProviderTimeoutError(ProviderError):
    category = ProviderErrorCategory.TIMEOUT
    retryable = True


class ProviderConnectionError(ProviderError):
    category = ProviderErrorCategory.CONNECTION
    retryable = True


class ProviderInvalidRequestError(ProviderError):
    category = ProviderErrorCategory.INVALID_REQUEST


class ProviderContextOverflowError(ProviderError):
    category = ProviderErrorCategory.CONTEXT_OVERFLOW


class UnsupportedCapabilityError(ProviderError):
    category = ProviderErrorCategory.UNSUPPORTED_CAPABILITY


class ProviderNotImplementedError(UnsupportedCapabilityError):
    """The provider is configured but its adapter is not registered yet."""


class ProviderProtocolError(ProviderError):
    category = ProviderErrorCategory.PROTOCOL


class ProviderInternalError(ProviderError):
    category = ProviderErrorCategory.PROVIDER_INTERNAL
    retryable = True
