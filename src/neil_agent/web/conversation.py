"""Explicit, paginated projection of saved user questions and assistant answers."""

from ..agent import COMPACTION_CHECKPOINT_USER
from ..session import SessionSnapshot
from .dto import ConversationPageDto, ConversationPartDto

CONVERSATION_PART_CHARS = 4_000
CONVERSATION_PAGE_PARTS = 32


def project_conversation(
    snapshot: SessionSnapshot | None,
    *,
    session_id: str,
    revision: int,
    cursor: str | None = None,
) -> ConversationPageDto:
    messages = () if snapshot is None else snapshot.messages
    compacted = bool(messages and messages[0].content == COMPACTION_CHECKPOINT_USER)
    visible = [
        message
        for index, message in enumerate(messages)
        if not (compacted and index < 2)
        and not message.tool_results
        and not message.tool_calls
        and message.content.strip()
    ]
    index, offset = 0, 0
    if cursor is not None:
        try:
            message_cursor, separator, character_cursor = cursor.partition(":")
            if not separator:
                raise ValueError
            index, offset = int(message_cursor), int(character_cursor)
        except ValueError as error:
            raise ValueError("Invalid conversation cursor") from error
        if index < 0 or offset < 0 or index > len(visible):
            raise ValueError("Invalid conversation cursor")
        if (index == len(visible) and offset != 0) or (
            index < len(visible) and offset >= len(visible[index].content)
        ):
            raise ValueError("Invalid conversation cursor")
    items: list[ConversationPartDto] = []
    while index < len(visible) and len(items) < CONVERSATION_PAGE_PARTS:
        message = visible[index]
        text = message.content[offset : offset + CONVERSATION_PART_CHARS]
        last = offset + len(text) == len(message.content)
        items.append(
            ConversationPartDto(
                message_index=index,
                role=message.role,
                text=text,
                offset=offset,
                is_last_part=last,
            )
        )
        if last:
            index, offset = index + 1, 0
        else:
            offset += len(text)
    return ConversationPageDto(
        session_id=session_id,
        revision=revision,
        items=tuple(items),
        next_cursor=None if index == len(visible) else f"{index}:{offset}",
        total_messages=len(visible),
        compacted=compacted,
    )
