"""Provider-neutral bounded retry policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from .errors import ProviderError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Decide retries without depending on one provider SDK."""

    max_retries: int
    base_delay: float
    max_delay: float

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("maximum retries cannot be negative")
        if not isfinite(self.base_delay) or self.base_delay < 0:
            raise ValueError("retry base delay must be finite and non-negative")
        if not isfinite(self.max_delay) or self.max_delay <= 0:
            raise ValueError("retry maximum delay must be finite and positive")
        if self.base_delay > self.max_delay:
            raise ValueError("retry base delay cannot exceed maximum delay")

    def can_retry(
        self,
        error: ProviderError,
        retries_done: int,
        *,
        output_started: bool = False,
    ) -> bool:
        """Return whether another attempt is safe and remains within budget."""

        return (
            not output_started
            and retries_done < self.max_retries
            and error.retryable
        )

    def delay(self, error: ProviderError, retry_number: int) -> float:
        """Return a bounded server-directed or exponential delay."""

        if retry_number < 1:
            raise ValueError("retry number must be positive")
        if error.retry_after is not None:
            return min(error.retry_after, self.max_delay)
        exponential = self.base_delay * (2 ** (retry_number - 1))
        return min(exponential, self.max_delay)


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse supported Retry-After headers without accepting dates or infinity."""

    normalized = {key.lower(): value for key, value in headers.items()}
    for header, divisor in (("retry-after-ms", 1_000), ("retry-after", 1)):
        raw_value = normalized.get(header)
        if raw_value is None:
            continue
        try:
            value = float(raw_value) / divisor
        except ValueError:
            continue
        if isfinite(value) and value >= 0:
            return value
    return None
