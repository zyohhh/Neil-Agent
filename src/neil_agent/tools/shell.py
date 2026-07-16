"""Fixed, workspace-scoped command tools without arbitrary shell access."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..errors import ToolError
from ..schemas import ToolDefinition
from .registry import ToolRegistry

QUALITY_COMMANDS: dict[str, tuple[str, ...]] = {
    "pytest": ("-m", "pytest", "-q"),
    "ruff": ("-m", "ruff", "check", "."),
    "mypy": ("-m", "mypy", "src"),
}

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

        return self._run(
            [
                "git",
                "--no-pager",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--short",
                "--branch",
                "--ignore-submodules=all",
            ]
        )

    def git_diff(self, staged: bool = False) -> str:
        """Return unstaged or staged Git diff without modifying Git state."""

        if not isinstance(staged, bool):
            raise ToolError("staged 必须是布尔值。")
        command = [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
        ]
        if staged:
            command.append("--cached")
        return self._run(command)

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
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                env=self._safe_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
                shell=False,
                creationflags=self._creation_flags(),
            )
        except subprocess.TimeoutExpired as error:
            output = self._timeout_output(error)
            message = f"命令执行超过 {self.timeout:g} 秒，已停止。"
            if output:
                message += f"\n{output}"
            raise ToolError(message) from error
        except FileNotFoundError as error:
            raise ToolError(f"找不到命令：{command[0]}") from error
        except OSError as error:
            raise ToolError("命令启动失败。") from error

        output = self._combined_output(completed.stdout, completed.stderr)
        result = f"Exit code: {completed.returncode}\n{output or '(no output)'}"
        if completed.returncode != 0:
            raise ToolError(result)
        return result

    def _combined_output(self, stdout: str, stderr: str) -> str:
        sections: list[str] = []
        if stdout.strip():
            sections.append(stdout.rstrip())
        if stderr.strip():
            sections.append(stderr.rstrip())
        return self._truncate("\n".join(sections))

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
        "Run one fixed project quality check. Allowed checks are pytest, ruff, "
        "and mypy. The command runs in the workspace with a timeout, no shell, "
        "and requires explicit user approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "check": {
                "type": "string",
                "enum": ["pytest", "ruff", "mypy"],
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
