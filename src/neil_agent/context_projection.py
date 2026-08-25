"""Shared ContextTomography projections for cockpit and Web DTOs."""

from __future__ import annotations

from collections.abc import Sequence

from .agent import Agent, COMPACTION_CHECKPOINT_USER
from .config import Settings
from .context import (
    ContextCheckpointState,
    ContextSelection,
    ContextTomography,
    build_context_tomography,
    count_rounds,
    estimate_fixed_chars,
    estimate_fixed_tokens,
    estimate_messages_chars,
    estimate_messages_tokens,
    select_recent_rounds,
    split_rounds,
)
from .host_runtime import HostRuntime
from .schemas import Message, TokenUsage, ToolDefinition


def build_host_context_tomography(
    settings: Settings,
    host_runtime: HostRuntime,
    messages: Sequence[Message],
    *,
    last_server_usage: TokenUsage | None = None,
    current_input: str = "",
) -> ContextTomography:
    """Build one local context estimate using the same rules as Agent.context_tomography()."""

    registry = host_runtime.registry
    project_instructions = host_runtime.instruction_manager.current.prompt_section()
    base_prompt = settings.system_prompt
    if project_instructions.strip():
        base_prompt = base_prompt.rstrip()
    system_prompt_without_project = Agent._with_tool_workflow(base_prompt, registry)
    system_prompt = Agent._with_tool_workflow(
        Agent._with_project_instructions(base_prompt, project_instructions),
        registry,
    )
    current_chain = (
        ()
        if not current_input.strip()
        else (Agent._make_user_message(current_input),)
    )
    selection = _host_context_selection(
        messages,
        system_prompt=system_prompt,
        tools=registry.definitions,
        max_rounds=settings.max_rounds,
        max_context_chars=settings.max_context_chars,
        max_context_tokens=settings.max_context_tokens,
        current_chain=current_chain,
    )
    return build_context_tomography(
        system_prompt_without_project=system_prompt_without_project,
        system_prompt=system_prompt,
        has_project_instructions=bool(project_instructions.strip()),
        tools=registry.definitions,
        selected_history=selection,
        current_chain=current_chain,
        stored_history=messages,
        stored_rounds=count_rounds(messages),
        budget_chars=settings.max_context_chars,
        budget_tokens=settings.max_context_tokens,
        last_server_usage=last_server_usage,
        checkpoint_state=_host_context_checkpoint_state(messages, selection),
    )


def _host_context_selection(
    messages: Sequence[Message],
    *,
    system_prompt: str,
    tools: Sequence[ToolDefinition],
    max_rounds: int,
    max_context_chars: int,
    max_context_tokens: int | None,
    current_chain: Sequence[Message],
) -> ContextSelection:
    fixed_chars = estimate_fixed_chars(system_prompt, tools)
    fixed_tokens = estimate_fixed_tokens(system_prompt, tools)
    return _select_history(
        messages,
        max_rounds=max_rounds - 1,
        max_chars=max(
            max_context_chars
            - fixed_chars
            - estimate_messages_chars(current_chain),
            0,
        ),
        max_tokens=(
            None
            if max_context_tokens is None
            else max(
                max_context_tokens
                - fixed_tokens
                - estimate_messages_tokens(current_chain),
                0,
            )
        ),
    )


def _select_history(
    messages: Sequence[Message],
    *,
    max_rounds: int,
    max_chars: int,
    max_tokens: int | None = None,
) -> ContextSelection:
    rounds = split_rounds(messages)
    if not rounds or not _is_compaction_checkpoint(rounds[0]):
        return select_recent_rounds(
            messages,
            max_rounds=max_rounds,
            max_chars=max_chars,
            max_tokens=max_tokens,
        )
    checkpoint = rounds[0]
    checkpoint_chars = estimate_messages_chars(checkpoint)
    checkpoint_tokens = estimate_messages_tokens(checkpoint)
    if (
        max_rounds < 1
        or checkpoint_chars > max_chars
        or (max_tokens is not None and checkpoint_tokens > max_tokens)
    ):
        return select_recent_rounds(
            messages,
            max_rounds=max_rounds,
            max_chars=max_chars,
            max_tokens=max_tokens,
        )
    recent_messages = tuple(
        message for conversation_round in rounds[1:] for message in conversation_round
    )
    recent = select_recent_rounds(
        recent_messages,
        max_rounds=max_rounds - 1,
        max_chars=max_chars - checkpoint_chars,
        max_tokens=(None if max_tokens is None else max_tokens - checkpoint_tokens),
    )
    return ContextSelection(
        messages=(*checkpoint, *recent.messages),
        round_count=1 + recent.round_count,
        omitted_round_count=len(rounds) - 1 - recent.round_count,
        message_chars=checkpoint_chars + recent.message_chars,
        estimated_tokens=checkpoint_tokens + recent.estimated_tokens,
    )


def _host_context_checkpoint_state(
    messages: Sequence[Message],
    selection: ContextSelection,
) -> ContextCheckpointState:
    rounds = split_rounds(messages)
    if not rounds or not _is_compaction_checkpoint(rounds[0]):
        return "none"
    checkpoint = rounds[0]
    return (
        "kept"
        if tuple(selection.messages[: len(checkpoint)]) == checkpoint
        else "omitted"
    )


def _is_compaction_checkpoint(messages: Sequence[Message]) -> bool:
    return (
        len(messages) == 2
        and messages[0].role == "user"
        and messages[0].content == COMPACTION_CHECKPOINT_USER
        and messages[1].role == "assistant"
        and messages[1].content.startswith("[Compressed conversation summary]\n")
    )
