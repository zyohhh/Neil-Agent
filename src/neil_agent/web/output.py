"""Bounded output storage that retains assistant text ahead of activity logs."""

from collections.abc import Iterator

from .dto import OutputEntryDto

MAX_OUTPUT_ENTRIES = 200
MAX_OUTPUT_ENTRY_CHARS = 4_000


class OutputBuffer:
    def __init__(self, *, max_entries: int = MAX_OUTPUT_ENTRIES) -> None:
        self._entries: list[OutputEntryDto] = []
        self._max_entries = max_entries
        self.truncated = False

    def __iter__(self) -> Iterator[OutputEntryDto]:
        return iter(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self.truncated = False

    def append(self, entry: OutputEntryDto) -> None:
        text = entry.text
        if entry.kind == "assistant" and self._entries:
            previous = self._entries[-1]
            if previous.kind == "assistant":
                count = min(len(text), MAX_OUTPUT_ENTRY_CHARS - len(previous.text))
                self._entries[-1] = previous.model_copy(
                    update={"text": previous.text + text[:count]}
                )
                text = text[count:]
        if not text:
            return
        if len(self._entries) == self._max_entries:
            removable = next(
                (i for i, item in enumerate(self._entries) if item.kind != "assistant"),
                None,
            )
            if removable is None:
                self.truncated |= entry.kind == "assistant"
                return
            self._entries.pop(removable)
        self._entries.append(entry.model_copy(update={"text": text}))
