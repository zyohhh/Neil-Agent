"""Fail-closed preparation for same-provider Web runtime model switching."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .config import Settings, validate_runtime_model_identifier
from .providers.base import ProviderId

RUNTIME_MODEL_SWITCH_VERSION: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class RuntimeModelCatalog:
    """One safe, operator-configured model catalog for the active provider."""

    version: Literal[1]
    provider: ProviderId
    current_model: str
    models: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version != RUNTIME_MODEL_SWITCH_VERSION:
            raise ValueError("unsupported runtime model catalog version")
        validate_runtime_model_identifier(self.current_model)
        if not self.models or self.models[0] != self.current_model:
            raise ValueError("runtime model catalog must start with the current model")
        if len(set(self.models)) != len(self.models):
            raise ValueError("runtime model catalog contains duplicate models")
        for model in self.models:
            validate_runtime_model_identifier(model)


@dataclass(frozen=True, slots=True)
class PreparedRuntimeModelSwitch:
    """Validated settings replacement that performs no network request."""

    version: Literal[1]
    provider: ProviderId
    previous_model: str
    target_model: str
    settings: Settings = field(repr=False, compare=False)

    @property
    def changes_model(self) -> bool:
        return self.previous_model != self.target_model


def runtime_model_catalog(settings: Settings) -> RuntimeModelCatalog:
    """Return the startup model plus explicit same-provider alternatives."""

    if not isinstance(settings, Settings):
        raise TypeError("runtime model catalog requires Settings")
    current = validate_runtime_model_identifier(settings.selected_model)
    models = tuple(dict.fromkeys((current, *settings.web_runtime_model_allowlist)))
    return RuntimeModelCatalog(
        version=RUNTIME_MODEL_SWITCH_VERSION,
        provider=settings.llm_provider,
        current_model=current,
        models=models,
    )


def prepare_runtime_model_switch(
    settings: Settings,
    target_model: str,
) -> PreparedRuntimeModelSwitch:
    """Validate one allowlisted target and rebuild Settings without side effects."""

    catalog = runtime_model_catalog(settings)
    target = validate_runtime_model_identifier(target_model)
    if target not in catalog.models:
        raise ValueError("runtime model target is not allowlisted")
    payload = settings.model_dump()
    payload["llm_model"] = target
    payload["web_runtime_model_allowlist"] = tuple(
        model for model in catalog.models if model != target
    )
    candidate = Settings.model_validate(payload)
    if candidate.llm_provider is not settings.llm_provider:
        raise ValueError("runtime model switching cannot change providers")
    return PreparedRuntimeModelSwitch(
        version=RUNTIME_MODEL_SWITCH_VERSION,
        provider=candidate.llm_provider,
        previous_model=settings.selected_model,
        target_model=candidate.selected_model,
        settings=candidate,
    )
