"""Safe loading of workspace-root project instructions."""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from unicodedata import category

INSTRUCTIONS_FILENAME = "AGENTS.md"
MAX_INSTRUCTIONS_FILE_BYTES = 32_768
InstructionStatus = Literal["active", "missing", "empty", "invalid"]


@dataclass(frozen=True, slots=True)
class ProjectInstructions:
    """Startup snapshot of one optional root ``AGENTS.md`` file."""

    source: Path
    status: InstructionStatus
    size_bytes: int = 0
    char_count: int = 0
    reason: str = ""
    content: str = field(default="", repr=False)

    @property
    def active(self) -> bool:
        return self.status == "active"

    def prompt_section(self) -> str:
        """Return a clearly delimited system-prompt section when active."""

        if not self.active:
            return ""
        return (
            "Project instructions loaded from the workspace-root AGENTS.md follow.\n"
            "Apply them when they do not conflict with higher-priority system rules.\n"
            "--- BEGIN PROJECT INSTRUCTIONS ---\n"
            f"{self.content}\n"
            "--- END PROJECT INSTRUCTIONS ---"
        )


def load_project_instructions(workspace_root: str | Path) -> ProjectInstructions:
    """Read a bounded regular UTF-8 file without following a symlink."""

    root = Path(workspace_root).expanduser().resolve()
    source = root / INSTRUCTIONS_FILENAME
    try:
        file_stat = source.lstat()
    except FileNotFoundError:
        return ProjectInstructions(source=source, status="missing")
    except OSError:
        return ProjectInstructions(
            source=source,
            status="invalid",
            reason="无法读取项目指令文件元数据。",
        )

    if not stat.S_ISREG(file_stat.st_mode) or source.is_symlink():
        return ProjectInstructions(
            source=source,
            status="invalid",
            size_bytes=file_stat.st_size,
            reason="项目指令必须是工作区根目录中的普通文件，不能是符号链接。",
        )
    try:
        if source.resolve(strict=True) != source:
            raise ValueError("instruction path resolves elsewhere")
    except (OSError, ValueError):
        return ProjectInstructions(
            source=source,
            status="invalid",
            size_bytes=file_stat.st_size,
            reason="项目指令文件越过工作区根目录。",
        )
    if file_stat.st_size > MAX_INSTRUCTIONS_FILE_BYTES:
        return ProjectInstructions(
            source=source,
            status="invalid",
            size_bytes=file_stat.st_size,
            reason=f"项目指令超过 {MAX_INSTRUCTIONS_FILE_BYTES} 字节上限。",
        )

    try:
        payload = source.read_bytes()
        if source.resolve(strict=True) != source:
            raise OSError("instruction path changed")
    except OSError:
        return ProjectInstructions(
            source=source,
            status="invalid",
            size_bytes=file_stat.st_size,
            reason="读取项目指令文件失败。",
        )
    if len(payload) > MAX_INSTRUCTIONS_FILE_BYTES:
        return ProjectInstructions(
            source=source,
            status="invalid",
            size_bytes=len(payload),
            reason=f"项目指令超过 {MAX_INSTRUCTIONS_FILE_BYTES} 字节上限。",
        )
    try:
        content = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ProjectInstructions(
            source=source,
            status="invalid",
            size_bytes=len(payload),
            reason="项目指令必须使用 UTF-8 编码。",
        )
    if any(
        character not in {"\n", "\r", "\t"} and category(character).startswith("C")
        for character in content
    ):
        return ProjectInstructions(
            source=source,
            status="invalid",
            size_bytes=len(payload),
            char_count=len(content),
            reason="项目指令包含不允许的控制或格式字符。",
        )

    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ProjectInstructions(
            source=source,
            status="empty",
            size_bytes=len(payload),
        )
    return ProjectInstructions(
        source=source,
        status="active",
        size_bytes=len(payload),
        char_count=len(normalized),
        content=normalized,
    )
