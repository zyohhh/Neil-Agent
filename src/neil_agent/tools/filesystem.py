"""Workspace-restricted filesystem tools with guarded writes."""

from __future__ import annotations

import os
import tempfile
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path

from ..errors import ToolError
from ..schemas import ToolDefinition
from .registry import ToolRegistry

MAX_FILE_SIZE_BYTES = 1_000_000
MAX_SEARCH_RESULTS = 100
MAX_DIFF_PREVIEW_CHARS = 20_000
BLOCKED_DIRECTORIES = frozenset(
    {
        ".git",
        ".neil-agent",
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
    """Expose bounded file access while protecting workspace boundaries."""

    def __init__(self, workspace_root: str | Path) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        self.root = root

    def register(self, registry: ToolRegistry) -> None:
        """Register read tools and approval-required write tools."""

        self.register_read_only(registry)
        registry.register(
            WRITE_FILE,
            self.write_file,
            requires_approval=True,
            preview_handler=self.preview_write_file,
        )
        registry.register(
            REPLACE_TEXT,
            self.replace_text,
            requires_approval=True,
            preview_handler=self.preview_replace_text,
        )

    def register_read_only(self, registry: ToolRegistry) -> None:
        """Register only bounded inspection tools for unattended runs."""

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

    def preview_write_file(self, path: str, content: str) -> str:
        """Preview creating or replacing a UTF-8 text file."""

        file_path = self._prepare_write_target(path)
        self._validate_new_content(content)
        current_content = self._read_optional_text(file_path)
        return self._format_diff(file_path, current_content, content)

    def write_file(self, path: str, content: str) -> str:
        """Atomically create or replace a UTF-8 text file."""

        file_path = self._prepare_write_target(path)
        self._validate_new_content(content)
        current_content = self._read_optional_text(file_path)
        if current_content == content:
            return f"文件内容没有变化：{self._relative_display(file_path)}"

        action = "更新" if current_content is not None else "创建"
        self._atomic_write(file_path, content)
        return f"已{action}文件：{self._relative_display(file_path)}"

    def preview_replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_replacements: int = 1,
    ) -> str:
        """Preview an exact text replacement before approval."""

        file_path = self._resolve(path)
        current_content = self._read_required_text(file_path, path)
        updated_content = self._replace_content(
            current_content,
            old_text,
            new_text,
            expected_replacements,
        )
        self._validate_new_content(updated_content)
        return self._format_diff(file_path, current_content, updated_content)

    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_replacements: int = 1,
    ) -> str:
        """Atomically replace an exact text occurrence in one file."""

        file_path = self._resolve(path)
        current_content = self._read_required_text(file_path, path)
        updated_content = self._replace_content(
            current_content,
            old_text,
            new_text,
            expected_replacements,
        )
        self._validate_new_content(updated_content)
        self._atomic_write(file_path, updated_content)
        return (
            f"已在 {self._relative_display(file_path)} 中替换 "
            f"{expected_replacements} 处文本。"
        )

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

    def _prepare_write_target(self, path: str) -> Path:
        file_path = self._resolve(path)
        if file_path.exists() and not file_path.is_file():
            raise ToolError(f"目标不是文件：{path}")
        if not file_path.parent.is_dir():
            raise ToolError(f"父目录不存在：{self._relative_display(file_path.parent)}")
        return file_path

    def _read_optional_text(self, file_path: Path) -> str | None:
        if not file_path.exists():
            return None
        return self._read_required_text(file_path, self._relative_display(file_path))

    def _read_required_text(self, file_path: Path, display_path: str) -> str:
        if not file_path.is_file():
            raise ToolError(f"文件不存在：{display_path}")
        if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            raise ToolError(f"文件过大，最多允许处理 {MAX_FILE_SIZE_BYTES} 字节。")
        try:
            return file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ToolError("只能处理 UTF-8 文本文件。") from error

    @staticmethod
    def _validate_new_content(content: str) -> None:
        if not isinstance(content, str):
            raise ToolError("文件内容必须是字符串。")
        if len(content.encode("utf-8")) > MAX_FILE_SIZE_BYTES:
            raise ToolError(f"写入内容过大，最多允许 {MAX_FILE_SIZE_BYTES} 字节。")

    @staticmethod
    def _replace_content(
        current_content: str,
        old_text: str,
        new_text: str,
        expected_replacements: int,
    ) -> str:
        if not isinstance(old_text, str) or not old_text:
            raise ToolError("old_text 必须是非空字符串。")
        if not isinstance(new_text, str):
            raise ToolError("new_text 必须是字符串。")
        if old_text == new_text:
            raise ToolError("old_text 与 new_text 相同，没有内容变化。")
        if not isinstance(expected_replacements, int) or expected_replacements < 1:
            raise ToolError("expected_replacements 必须是大于等于 1 的整数。")

        actual_replacements = current_content.count(old_text)
        if actual_replacements != expected_replacements:
            raise ToolError(
                "精确替换数量不匹配："
                f"期望 {expected_replacements} 处，实际 {actual_replacements} 处。"
            )
        return current_content.replace(old_text, new_text, expected_replacements)

    def _format_diff(
        self,
        file_path: Path,
        current_content: str | None,
        new_content: str,
    ) -> str:
        if current_content == new_content:
            diff = "没有内容变化。"
        else:
            relative = self._relative_display(file_path)
            before = "" if current_content is None else current_content
            from_name = "/dev/null" if current_content is None else f"a/{relative}"
            diff = "".join(
                unified_diff(
                    before.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile=from_name,
                    tofile=f"b/{relative}",
                    lineterm="\n",
                )
            )

        before = "" if current_content is None else current_content
        change_id = sha256(
            before.encode("utf-8") + b"\0" + new_content.encode("utf-8")
        ).hexdigest()[:16]
        if len(diff) > MAX_DIFF_PREVIEW_CHARS:
            diff = (
                diff[:MAX_DIFF_PREVIEW_CHARS]
                + f"\n... diff 预览已截断（上限 {MAX_DIFF_PREVIEW_CHARS} 字符）。"
            )
        return f"{diff}\nChange-ID: {change_id}"

    def _atomic_write(self, file_path: Path, content: str) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                delete=False,
                dir=file_path.parent,
                prefix=".neil-agent-",
                suffix=".tmp",
            ) as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
                temporary_path = Path(temporary_file.name)

            if file_path.exists():
                os.chmod(temporary_path, file_path.stat().st_mode)
            os.replace(temporary_path, file_path)
            temporary_path = None
        except OSError as error:
            raise ToolError("写入失败，原文件保持不变。") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

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

WRITE_FILE = ToolDefinition(
    name="write_file",
    description=(
        "Create or replace one UTF-8 text file inside the workspace. "
        "This changes project files and always requires explicit user approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative destination file path.",
            },
            "content": {
                "type": "string",
                "description": "Complete new UTF-8 file content.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)

REPLACE_TEXT = ToolDefinition(
    name="replace_text",
    description=(
        "Replace an exact text fragment in one UTF-8 workspace file. "
        "The match count must equal expected_replacements, and execution always "
        "requires explicit user approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact existing text to replace.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text.",
            },
            "expected_replacements": {
                "type": "integer",
                "description": "Required exact match count; defaults to 1.",
                "minimum": 1,
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    },
)
