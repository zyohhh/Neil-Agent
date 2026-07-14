"""Read-only filesystem tools restricted to one workspace."""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import ToolError
from ..schemas import ToolDefinition
from .registry import ToolRegistry

MAX_FILE_SIZE_BYTES = 1_000_000
MAX_SEARCH_RESULTS = 100
BLOCKED_DIRECTORIES = frozenset(
    {
        ".git",
        ".agents",
        ".codex",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
BLOCKED_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
BLOCKED_FILE_NAMES = frozenset({".git-credentials", ".netrc", ".npmrc", ".pypirc"})


class FileSystemTools:
    """Expose bounded, read-only access to project text files."""

    def __init__(self, workspace_root: str | Path) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        self.root = root

    def register(self, registry: ToolRegistry) -> None:
        """Register all read-only filesystem tools."""

        registry.register(LIST_DIRECTORY, self.list_directory)
        registry.register(READ_FILE, self.read_file)
        registry.register(SEARCH_TEXT, self.search_text)

    def list_directory(self, path: str = ".") -> str:
        """List direct children of a workspace directory."""

        directory = self._resolve(path)
        if not directory.is_dir():
            raise ToolError(f"不是目录：{path}")

        entries: list[str] = []
        for item in sorted(directory.iterdir(), key=lambda value: value.name.lower()):
            if not self._is_allowed(item):
                continue
            relative = self._relative_display(item)
            if item.is_dir():
                entries.append(f"DIR  {relative}/")
            elif item.is_file():
                entries.append(f"FILE {relative} ({item.stat().st_size} bytes)")

        return "\n".join(entries) if entries else "目录为空。"

    def read_file(self, path: str) -> str:
        """Read one UTF-8 text file inside the workspace."""

        file_path = self._resolve(path)
        if not file_path.is_file():
            raise ToolError(f"文件不存在：{path}")
        if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            raise ToolError(f"文件过大，最多允许读取 {MAX_FILE_SIZE_BYTES} 字节。")

        try:
            return file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ToolError("只能读取 UTF-8 文本文件。") from error

    def search_text(self, query: str, path: str = ".") -> str:
        """Search for case-insensitive text matches within the workspace."""

        if not query.strip():
            raise ToolError("搜索内容不能为空。")

        target = self._resolve(path)
        files = [target] if target.is_file() else self._walk_files(target)
        matches: list[str] = []
        normalized_query = query.casefold()

        for file_path in files:
            if len(matches) >= MAX_SEARCH_RESULTS:
                break
            if not self._is_searchable_file(file_path):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue

            relative = self._relative_display(file_path)
            for line_number, line in enumerate(lines, start=1):
                if normalized_query in line.casefold():
                    preview = line.strip()[:500]
                    matches.append(f"{relative}:{line_number}: {preview}")
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        break

        if not matches:
            return "未找到匹配内容。"
        if len(matches) == MAX_SEARCH_RESULTS:
            matches.append(f"结果已限制为前 {MAX_SEARCH_RESULTS} 条。")
        return "\n".join(matches)

    def _resolve(self, path: str) -> Path:
        requested = Path(path).expanduser()
        candidate = (
            requested.resolve()
            if requested.is_absolute()
            else (self.root / requested).resolve()
        )
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ToolError("拒绝访问工作区之外的路径。") from error
        if not self._is_allowed(candidate):
            raise ToolError("该路径包含受保护的目录或敏感文件。")
        return candidate

    def _walk_files(self, directory: Path) -> list[Path]:
        if not directory.is_dir():
            raise ToolError(f"路径不存在：{self._relative_display(directory)}")

        files: list[Path] = []
        for current_path, directory_names, file_names in os.walk(directory):
            current = Path(current_path)
            directory_names[:] = [
                name for name in directory_names if self._is_allowed(current / name)
            ]
            files.extend(
                current / name
                for name in file_names
                if self._is_allowed(current / name)
            )
        return files

    def _is_allowed(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.root)
        except (OSError, ValueError):
            return False
        if any(part.lower() in BLOCKED_DIRECTORIES for part in relative.parts):
            return False
        if self._is_sensitive_name(relative.name):
            return False
        return path.suffix.lower() not in BLOCKED_SUFFIXES

    @staticmethod
    def _is_sensitive_name(name: str) -> bool:
        lowered = name.lower()
        return (
            lowered in BLOCKED_FILE_NAMES
            or lowered == ".env"
            or (lowered.startswith(".env.") and lowered != ".env.example")
        )

    def _is_searchable_file(self, path: Path) -> bool:
        try:
            return (
                path.is_file()
                and self._is_allowed(path)
                and path.stat().st_size <= MAX_FILE_SIZE_BYTES
            )
        except OSError:
            return False

    def _relative_display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix() or "."
        except ValueError:
            return str(path)


LIST_DIRECTORY = ToolDefinition(
    name="list_directory",
    description=(
        "List files and directories directly inside a workspace directory. "
        "Use relative paths and start with '.' when exploring the project."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative directory path; defaults to '.'.",
            }
        },
        "additionalProperties": False,
    },
)

READ_FILE = ToolDefinition(
    name="read_file",
    description="Read one UTF-8 text file inside the workspace.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)

SEARCH_TEXT = ToolDefinition(
    name="search_text",
    description=(
        "Search case-insensitively for text in one file or recursively in a "
        "workspace directory. Returns file paths, line numbers, and previews."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to find."},
            "path": {
                "type": "string",
                "description": "Workspace-relative file or directory; defaults to '.'.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
