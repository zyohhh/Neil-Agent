"""Versioned, opt-in Provider rate tables for Web Workbench estimates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..schemas import TokenUsage

MAX_RATE_TABLE_BYTES = 256_000


class _RateEntryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    cache_creation_usd_per_million: Decimal | None = Field(default=None, ge=0)
    cache_read_usd_per_million: Decimal | None = Field(default=None, ge=0)
    input_token_accounting: Literal[
        "separate_cache_tokens", "input_includes_cache_tokens"
    ]


class _RateTableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    effective_date: date
    currency: Literal["USD"] = "USD"
    rates: tuple[_RateEntryModel, ...] = Field(max_length=1_000)


@dataclass(frozen=True, slots=True)
class ModelRate:
    provider: str
    model: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    cache_creation_usd_per_million: Decimal | None = None
    cache_read_usd_per_million: Decimal | None = None
    input_token_accounting: Literal[
        "separate_cache_tokens", "input_includes_cache_tokens"
    ] = "separate_cache_tokens"

    def estimate(self, usage: TokenUsage) -> Decimal | None:
        """Estimate one saved usage only when every used category has a rate."""

        if (
            usage.cache_creation_input_tokens
            and self.cache_creation_usd_per_million is None
        ):
            return None
        if usage.cache_read_input_tokens and self.cache_read_usd_per_million is None:
            return None
        input_tokens = usage.input_tokens
        if self.input_token_accounting == "input_includes_cache_tokens":
            input_tokens -= (
                usage.cache_creation_input_tokens + usage.cache_read_input_tokens
            )
            if input_tokens < 0:
                return None
        million = Decimal(1_000_000)
        total = (
            Decimal(input_tokens) * self.input_usd_per_million
            + Decimal(usage.output_tokens) * self.output_usd_per_million
            + Decimal(usage.cache_creation_input_tokens)
            * (self.cache_creation_usd_per_million or Decimal(0))
            + Decimal(usage.cache_read_input_tokens)
            * (self.cache_read_usd_per_million or Decimal(0))
        ) / million
        return total


@dataclass(frozen=True, slots=True)
class ProviderRateTable:
    version: str
    effective_date: date
    rates: tuple[ModelRate, ...]

    def find(self, provider: str, model: str) -> ModelRate | None:
        key = (provider.casefold(), model.casefold())
        return next(
            (
                rate
                for rate in self.rates
                if (rate.provider.casefold(), rate.model.casefold()) == key
            ),
            None,
        )


def load_rate_table(path: Path | None) -> ProviderRateTable | None:
    """Load an explicit local table; absence or invalid data remains unavailable."""

    if path is None:
        return None
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_size > MAX_RATE_TABLE_BYTES:
            return None
        parsed = _RateTableModel.model_validate_json(resolved.read_bytes())
    except (OSError, ValidationError, ValueError, json.JSONDecodeError):
        return None
    return ProviderRateTable(
        version=parsed.version,
        effective_date=parsed.effective_date,
        rates=tuple(
            ModelRate(
                provider=rate.provider,
                model=rate.model,
                input_usd_per_million=rate.input_usd_per_million,
                output_usd_per_million=rate.output_usd_per_million,
                cache_creation_usd_per_million=rate.cache_creation_usd_per_million,
                cache_read_usd_per_million=rate.cache_read_usd_per_million,
                input_token_accounting=rate.input_token_accounting,
            )
            for rate in parsed.rates
        ),
    )
