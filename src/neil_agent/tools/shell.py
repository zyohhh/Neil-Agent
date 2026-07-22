"""Fixed, workspace-scoped command tools without arbitrary shell access."""

from __future__ import annotations

import os
import subprocess
import sys
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path

from ..errors import ToolError
from ..schemas import ToolDefinition
from .registry import ToolRegistry

QUALITY_COMMANDS: dict[str, tuple[str, ...]] = {
    "eval": ("-m", "neil_agent.evals", "--format", "json"),
    "pytest": ("-m", "pytest", "-q"),
    "ruff": ("-m", "ruff", "check", "."),
    "mypy": ("-m", "mypy", "src"),
}
MAX_GIT_PATHS = 50
MAX_COMMIT_MESSAGE_CHARS = 200
STATUS_TIMEOUT_SECONDS = 5.0
BLOCKED_GIT_DIRECTORIES = frozenset(
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
BLOCKED_GIT_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
BLOCKED_GIT_FILE_NAMES = frozenset({".git-credentials", ".netrc", ".npmrc", ".pypirc"})

SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WINDIR",
    }
)


class ShellTools:
    """Run only predefined quality checks and read-only Git commands."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        timeout: float = 120.0,
        max_output_chars: int = 20_000,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        if timeout <= 0:
            raise ValueError("command timeout must be greater than zero")
        if max_output_chars < 1_000:
            raise ValueError("max command output must be at least 1000 characters")

        self.root = root
        self.timeout = timeout
        self.max_output_chars = max_output_chars

    def register(self, registry: ToolRegistry) -> None:
        """Register approved quality checks and read-only Git inspection."""

        registry.register(
            RUN_QUALITY_CHECK,
            self.run_quality_check,
            requires_approval=True,
            preview_handler=self.preview_quality_check,
        )
        self.register_read_only(registry)
        registry.register(
            GIT_STAGE,
            self.git_stage,
            requires_approval=True,
            preview_handler=self.preview_git_stage,
        )
        registry.register(
            GIT_COMMIT,
            self.git_commit,
            requires_approval=True,
            preview_handler=self.preview_git_commit,
        )

    def register_read_only(self, registry: ToolRegistry) -> None:
        """Register only non-mutating Git inspection commands."""

        registry.register(GIT_STATUS, self.git_status)
        registry.register(GIT_DIFF, self.git_diff)

    def preview_quality_check(self, check: str) -> str:
        """Describe the exact fixed command that would be executed."""

        command = self._quality_command(check)
        return (
            "将执行项目代码检查；检查器配置或插件可能运行项目代码。\n"
            f"工作目录：{self.root}\n"
            f"命令：{subprocess.list2cmdline(command)}\n"
            f"超时：{self.timeout:g} 秒"
        )

    def run_quality_check(self, check: str) -> str:
        """Run one allowlisted project quality command."""

        return self._run(self._quality_command(check))

    def git_status(self) -> str:
        """Return concise working-tree status without modifying Git state."""

        return self._run(self._git_status_command())

    def git_status_snapshot(self) -> str:
        """Return raw concise Git status for the local ``/status`` command."""

        return self._capture(
            self._git_status_command(),
            timeout=min(self.timeout, STATUS_TIMEOUT_SECONDS),
        )

    def git_diff(self, staged: bool = False) -> str:
        """Return unstaged or staged Git diff without modifying Git state."""

        if not isinstance(staged, bool):
            raise ToolError("staged 必须是布尔值。")
        command = self._git_command(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
        )
        if staged:
            command.append("--cached")
        return self._run(command)

    def preview_git_stage(self, paths: list[str]) -> str:
        """Preview the exact explicit paths and content that would be staged."""

        normalized_paths = self._normalize_git_paths(paths)
        pathspecs = self._literal_pathspecs(normalized_paths)
        status = self._capture(
            self._git_command(
                "status",
                "--short",
                "--untracked-files=all",
                "--",
                *pathspecs,
            ),
            truncate=False,
        )
        if not status:
            raise ToolError("所选路径没有可暂存的变更。")

        cached_diff = self._capture(
            self._git_command(
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--",
                *pathspecs,
            ),
            truncate=False,
        )
        working_diff = self._capture(
            self._git_command(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--",
                *pathspecs,
            ),
            truncate=False,
        )
        untracked_diffs = [
            self._preview_untracked_file(path)
            for path in normalized_paths
            if not self._is_tracked(path)
        ]
        change_id = self._git_stage_change_id(
            normalized_paths,
            status,
            cached_diff,
            working_diff,
        )
        sections = [f"当前状态：\n{self._truncate(status)}"]
        if cached_diff:
            sections.append(f"已经暂存的内容：\n{self._truncate(cached_diff)}")
        if working_diff:
            sections.append(f"将加入暂存区的内容：\n{self._truncate(working_diff)}")
        if untracked_diffs:
            sections.append("未跟踪文件：\n" + "\n".join(untracked_diffs))

        command = self._git_command("add", "--", *pathspecs)
        preview = (
            "将只暂存下面列出的明确路径，不会暂存整个工作区。\n"
            "Git clean filter 可能运行仓库配置的外部程序。\n"
            f"工作目录：{self.root}\n"
            f"命令：{subprocess.list2cmdline(command)}\n\n"
            + "\n\n".join(sections)
            + f"\n\nChange-ID: {change_id}"
        )
        return self._truncate(preview)

    def git_stage(self, paths: list[str]) -> str:
        """Stage only validated, explicit workspace file paths."""

        normalized_paths = self._normalize_git_paths(paths)
        command = self._git_command(
            "add",
            "--",
            *self._literal_pathspecs(normalized_paths),
        )
        result = self._run(command)
        staged_stat = self._capture(
            self._git_command(
                "diff",
                "--cached",
                "--stat",
                "--no-ext-diff",
                "--no-textconv",
            )
        )
        return f"{result}\nStaged summary:\n{staged_stat or '(no staged changes)'}"

    def preview_git_commit(self, message: str) -> str:
        """Preview a local commit message and the complete staged diff."""

        commit_message = self._validate_commit_message(message)
        staged_diff = self._capture(
            self._git_command(
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
            ),
            truncate=False,
        )
        if not staged_diff:
            raise ToolError("暂存区为空，无法创建提交。")
        staged_stat = self._capture(
            self._git_command(
                "diff",
                "--cached",
                "--stat",
                "--no-ext-diff",
                "--no-textconv",
            )
        )
        command = self._git_commit_command(commit_message)
        change_id = sha256(staged_diff.encode("utf-8")).hexdigest()[:16]
        preview = (
            "将创建本地 Git 提交；不会推送到远端。\n"
            f"工作目录：{self.root}\n"
            f"提交消息：{commit_message}\n"
            f"命令：{subprocess.list2cmdline(command)}\n\n"
            f"暂存区统计：\n{staged_stat or '(no stat output)'}\n\n"
            f"暂存区 diff：\n{self._truncate(staged_diff)}\n\n"
            f"Change-ID: {change_id}"
        )
        return self._truncate(preview)

    def git_commit(self, message: str) -> str:
        """Create one local commit from the current staged changes."""

        commit_message = self._validate_commit_message(message)
        return self._run(self._git_commit_command(commit_message))

    @staticmethod
    def _quality_command(check: str) -> list[str]:
        if not isinstance(check, str):
            raise ToolError("check 必须是字符串。")
        arguments = QUALITY_COMMANDS.get(check)
        if arguments is None:
            allowed = ", ".join(QUALITY_COMMANDS)
            raise ToolError(f"不允许的代码检查：{check}。可选值：{allowed}")
        return [sys.executable, *arguments]

    def _run(self, command: list[str]) -> str:
        completed = self._run_process(command)
        result = self._format_process_result(command, completed)
        if completed.returncode != 0:
            raise ToolError(result)
        return result

    def _capture(
        self,
        command: list[str],
        *,
        truncate: bool = True,
        timeout: float | None = None,
    ) -> str:
        completed = self._run_process(command, timeout=timeout)
        if completed.returncode != 0:
            raise ToolError(self._format_process_result(command, completed))
        return self._combined_output(
            completed.stdout,
            completed.stderr,
            truncate=truncate,
        )

    def _run_process(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            return subprocess.run(
                command,
                cwd=self.root,
                env=self._safe_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                check=False,
                shell=False,
                creationflags=self._creation_flags(),
            )
        except subprocess.TimeoutExpired as error:
            output = self._timeout_output(error)
            message = (
                f"Command: {subprocess.list2cmdline(command)}\n"
                f"Working directory: {self.root}\n"
                f"命令执行超过 {effective_timeout:g} 秒，已停止。"
            )
            if output:
                message += f"\n{output}"
            raise ToolError(message) from error
        except FileNotFoundError as error:
            raise ToolError(f"找不到命令：{command[0]}") from error
        except OSError as error:
            raise ToolError("命令启动失败。") from error

    def _format_process_result(
        self,
        command: list[str],
        completed: subprocess.CompletedProcess[str],
    ) -> str:
        output = self._combined_output(completed.stdout, completed.stderr)
        return (
            f"Command: {subprocess.list2cmdline(command)}\n"
            f"Working directory: {self.root}\n"
            f"Exit code: {completed.returncode}\n"
            f"Output:\n{output or '(no output)'}"
        )

    @staticmethod
    def _git_command(*arguments: str) -> list[str]:
        return [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            *arguments,
        ]

    def _git_status_command(self) -> list[str]:
        return self._git_command(
            "status",
            "--short",
            "--branch",
            "--ignore-submodules=all",
        )

    def _git_commit_command(self, message: str) -> list[str]:
        return [
            *self._git_command(),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            message,
        ]

    def _normalize_git_paths(self, paths: list[str]) -> tuple[str, ...]:
        if not isinstance(paths, list) or not paths:
            raise ToolError("paths 必须是非空字符串列表。")
        if len(paths) > MAX_GIT_PATHS:
            raise ToolError(f"一次最多暂存 {MAX_GIT_PATHS} 个路径。")

        normalized: list[str] = []
        for path in paths:
            if not isinstance(path, str) or not path.strip():
                raise ToolError("paths 必须是非空字符串列表。")
            requested = Path(path)
            if requested.is_absolute():
                raise ToolError("Git 暂存只接受工作区相对路径。")
            candidate = (self.root / requested).resolve()
            try:
                relative = candidate.relative_to(self.root)
            except ValueError as error:
                raise ToolError("拒绝暂存工作区之外的路径。") from error
            if not relative.parts:
                raise ToolError("必须明确列出要暂存的文件，不能暂存整个工作区。")
            if self._is_sensitive_git_path(relative):
                raise ToolError("拒绝暂存受保护目录或敏感文件。")
            if candidate.exists() and not candidate.is_file():
                raise ToolError(f"只能暂存明确的文件路径：{path}")
            display = relative.as_posix()
            if display not in normalized:
                normalized.append(display)
        return tuple(normalized)

    @staticmethod
    def _literal_pathspecs(paths: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(f":(literal){path}" for path in paths)

    @staticmethod
    def _is_sensitive_git_path(relative: Path) -> bool:
        lowered_parts = tuple(part.lower() for part in relative.parts)
        if any(part in BLOCKED_GIT_DIRECTORIES for part in lowered_parts):
            return True
        name = relative.name.lower()
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return True
        if name in BLOCKED_GIT_FILE_NAMES:
            return True
        return relative.suffix.lower() in BLOCKED_GIT_SUFFIXES

    def _is_tracked(self, path: str) -> bool:
        command = self._git_command(
            "ls-files",
            "--error-unmatch",
            "--",
            f":(literal){path}",
        )
        completed = self._run_process(command)
        if completed.returncode not in {0, 1}:
            raise ToolError(self._format_process_result(command, completed))
        return completed.returncode == 0

    def _preview_untracked_file(self, path: str) -> str:
        file_path = self.root / path
        if not file_path.is_file():
            return f"未跟踪路径不存在或不可读取：{path}"
        read_limit = self.max_output_chars + 1
        try:
            with file_path.open("rb") as file:
                data = file.read(read_limit)
        except OSError as error:
            raise ToolError(f"无法读取未跟踪文件：{path}") from error
        if b"\0" in data:
            return f"二进制文件：{path} ({file_path.stat().st_size} bytes)"
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"非 UTF-8 文件：{path} ({file_path.stat().st_size} bytes)"
        preview = "".join(
            unified_diff(
                [],
                content.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{path}",
                lineterm="\n",
            )
        )
        if len(data) == read_limit:
            preview += "\n... 未跟踪文件预览已截断。"
        return preview or f"空文件：{path}"

    def _git_stage_change_id(
        self,
        paths: tuple[str, ...],
        status: str,
        cached_diff: str,
        working_diff: str,
    ) -> str:
        digest = sha256()
        for value in (status, cached_diff, working_diff):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        for path in paths:
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            file_path = self.root / path
            if not file_path.is_file():
                digest.update(b"<missing>")
                continue
            try:
                with file_path.open("rb") as file:
                    for chunk in iter(lambda: file.read(65_536), b""):
                        digest.update(chunk)
            except OSError as error:
                raise ToolError(f"无法读取待暂存文件：{path}") from error
            digest.update(b"\0")
        return digest.hexdigest()[:16]

    @staticmethod
    def _validate_commit_message(message: str) -> str:
        if not isinstance(message, str):
            raise ToolError("提交消息必须是字符串。")
        normalized = message.strip()
        if not normalized:
            raise ToolError("提交消息不能为空。")
        if "\n" in normalized or "\r" in normalized or "\0" in normalized:
            raise ToolError("提交消息必须是单行文本。")
        if len(normalized) > MAX_COMMIT_MESSAGE_CHARS:
            raise ToolError(f"提交消息不能超过 {MAX_COMMIT_MESSAGE_CHARS} 个字符。")
        return normalized

    def _combined_output(
        self,
        stdout: str,
        stderr: str,
        *,
        truncate: bool = True,
    ) -> str:
        sections: list[str] = []
        if stdout.strip():
            sections.append(stdout.rstrip())
        if stderr.strip():
            sections.append(stderr.rstrip())
        output = "\n".join(sections)
        return self._truncate(output) if truncate else output

    def _truncate(self, output: str) -> str:
        if len(output) <= self.max_output_chars:
            return output
        half = self.max_output_chars // 2
        omitted = len(output) - self.max_output_chars
        return output[:half] + f"\n... 已省略 {omitted} 个字符 ...\n" + output[-half:]

    def _timeout_output(self, error: subprocess.TimeoutExpired) -> str:
        stdout = self._decode_timeout_stream(error.stdout)
        stderr = self._decode_timeout_stream(error.stderr)
        return self._combined_output(stdout, stderr)

    @staticmethod
    def _decode_timeout_stream(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        environment = {
            name.upper(): value
            for name, value in os.environ.items()
            if name.upper() in SAFE_ENVIRONMENT_NAMES
        }
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "NO_COLOR": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONUTF8": "1",
            }
        )
        return environment

    @staticmethod
    def _creation_flags() -> int:
        if os.name != "nt":
            return 0
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


RUN_QUALITY_CHECK = ToolDefinition(
    name="run_quality_check",
    description=(
        "Run one fixed project quality check. Allowed checks are eval, pytest, "
        "ruff, and mypy. The command runs in the workspace with a timeout, no "
        "shell, and requires explicit user approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "check": {
                "type": "string",
                "enum": ["eval", "pytest", "ruff", "mypy"],
                "description": "The fixed quality check to run.",
            }
        },
        "required": ["check"],
        "additionalProperties": False,
    },
)

GIT_STATUS = ToolDefinition(
    name="git_status",
    description="Show concise Git branch and working-tree status without modifying it.",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)

GIT_DIFF = ToolDefinition(
    name="git_diff",
    description="Show unstaged or staged Git diff without modifying repository state.",
    input_schema={
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "Use true to show staged changes; defaults to false.",
            }
        },
        "additionalProperties": False,
    },
)

GIT_STAGE = ToolDefinition(
    name="git_stage",
    description=(
        "Stage only an explicit list of workspace file paths. The preview shows "
        "the selected paths and changes, and execution requires user approval. "
        "Git clean filters may run configured programs. Directories, sensitive "
        "files, and whole-workspace staging are rejected."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": MAX_GIT_PATHS,
                "description": "Explicit workspace-relative file paths to stage.",
            }
        },
        "required": ["paths"],
        "additionalProperties": False,
    },
)

GIT_COMMIT = ToolDefinition(
    name="git_commit",
    description=(
        "Create one local Git commit from the current staged changes. The staged "
        "diff and message are previewed, hooks and signing are disabled, execution "
        "requires user approval, and nothing is pushed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_COMMIT_MESSAGE_CHARS,
                "description": "Single-line local commit message.",
            }
        },
        "required": ["message"],
        "additionalProperties": False,
    },
)
