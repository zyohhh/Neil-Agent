"""Bounded, read-only projection service for Web Workbench P1."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal

from ..config import Settings
from ..errors import SessionError, ToolError
from ..providers.factory import describe_provider
from ..session import SessionSnapshot, SessionStore
from ..tools.shell import (
    BLOCKED_GIT_DIRECTORIES,
    BLOCKED_GIT_FILE_NAMES,
    BLOCKED_GIT_SUFFIXES,
    ShellTools,
)
from .dto import (
    ContextDto,
    FileNodeDto,
    FileTreeDto,
    GitDto,
    GitFileDto,
    ProviderCapabilitiesDto,
    ProviderDto,
    QualityCheckDto,
    ReviewDto,
    SecurityDto,
    SessionDto,
    SessionListDto,
    TaskDto,
    TaskStepDto,
    WorkbenchSnapshotDto,
    WorkspaceDto,
    utc_timestamp,
)

MAX_TREE_NODES = 300
MAX_TREE_DEPTH = 4
MAX_REVIEW_FILES = 100
ReviewState = Literal["empty", "passed", "failed", "stale", "unavailable"]
SENSITIVE_NAMES = frozenset(
    {
        *BLOCKED_GIT_DIRECTORIES,
        *BLOCKED_GIT_FILE_NAMES,
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".npm-cache",
        ".uv-cache",
    }
)


class WorkbenchSnapshotService:
    """Project local metadata without exposing content or mutation capabilities."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = settings.workspace_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Web Workbench workspace must be a directory")
        self.settings = settings
        self.root = root
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sessions = SessionStore(root)
        self._shell = ShellTools(
            root,
            timeout=min(settings.command_timeout, 5.0),
            max_output_chars=max(settings.max_command_output_chars, 1_000),
        )

    def health(self) -> dict[str, object]:
        """Return generic liveness facts without workspace metadata."""

        return {
            "status": "ready",
            "service": "neil-agent-web",
            "schema_version": 1,
            "read_only": True,
        }

    def sessions(self) -> SessionListDto:
        """Return bounded summaries; never return stored message bodies."""

        try:
            index = self._sessions.list_sessions(page=1, page_size=20)
        except SessionError:
            return SessionListDto(available=False)
        return SessionListDto(
            available=True,
            items=tuple(
                SessionDto(
                    session_id=item.session_id,
                    title=item.title,
                    updated_at=item.updated_at,
                    round_count=item.round_count,
                    preview=item.preview,
                    has_plan=item.has_plan,
                    failed_check=item.failed_check,
                    has_compaction=item.has_compaction,
                )
                for item in index.sessions
            ),
            invalid_count=index.invalid_count,
            total_count=index.valid_count,
        )

    def files(self, relative_path: str = "", *, depth: int = 2) -> FileTreeDto:
        """Return a bounded tree without following links or reading file contents."""

        if depth < 0 or depth > MAX_TREE_DEPTH:
            raise ValueError(f"file tree depth must be between 0 and {MAX_TREE_DEPTH}")
        requested = self._resolve_relative_directory(relative_path)
        budget = [MAX_TREE_NODES]
        items = self._list_directory(requested, depth=depth, budget=budget)
        return FileTreeDto(
            root=requested.relative_to(self.root).as_posix()
            if requested != self.root
            else "",
            items=items,
            truncated=budget[0] == 0,
        )

    def git(self) -> GitDto:
        """Return concise Git metadata through the existing fixed command boundary."""

        try:
            raw = self._shell.git_status_snapshot()
        except (ToolError, OSError):
            return GitDto(available=False)
        return self._parse_git_status(raw)

    def review(self) -> ReviewDto:
        """Combine read-only Git metadata with the latest persisted quality result."""

        git = self.git()
        latest = self._latest_session()
        quality = None
        if latest is not None and latest.latest_quality_check is not None:
            record = latest.latest_quality_check
            quality = QualityCheckDto(
                check=record.check,
                status=record.status,
                exit_code=record.exit_code,
            )
        if not git.available:
            state: ReviewState = "unavailable"
        elif quality is not None and quality.status == "failed":
            state = "failed"
        elif quality is not None and quality.status == "passed":
            state = "passed" if git.change_count else "empty"
        elif git.change_count:
            state = "stale"
        else:
            state = "empty"
        return ReviewDto(state=state, git=git, quality_check=quality)

    def snapshot(self) -> WorkbenchSnapshotDto:
        """Build one internally consistent, versioned first-screen snapshot."""

        sessions = self.sessions()
        latest = self._latest_session(sessions)
        git = self.git()
        review = self._review_from(git, latest)
        provider = describe_provider(self.settings)
        capabilities = provider.capabilities
        return WorkbenchSnapshotDto(
            generated_at=utc_timestamp(self._clock()),
            workspace=WorkspaceDto(
                name=self.root.name or self.root.drive,
                identity=sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[
                    :16
                ],
            ),
            provider=ProviderDto(
                provider=provider.provider.value,
                display_name=provider.display_name,
                model=self.settings.selected_model,
                wire_protocol=provider.wire_protocol.value,
                thinking_enabled=self.settings.thinking_enabled,
                capabilities=ProviderCapabilitiesDto(
                    streaming=capabilities.streaming,
                    tool_calling=capabilities.tool_calling,
                    parallel_tool_calls=capabilities.parallel_tool_calls,
                    reasoning_state=capabilities.reasoning_state,
                    usage_reporting=capabilities.usage_reporting,
                ),
            ),
            git=git,
            sessions=sessions,
            files=self.files(depth=2),
            task=self._task(latest),
            context=self._context(latest),
            review=review,
            security=SecurityDto(
                sandbox_backend=self.settings.sandbox_backend,
                audit_enabled=self.settings.audit_log_enabled,
            ),
        )

    def _latest_session(
        self, sessions: SessionListDto | None = None
    ) -> SessionSnapshot | None:
        summaries = sessions or self.sessions()
        if not summaries.available or not summaries.items:
            return None
        try:
            return self._sessions.load(summaries.items[0].session_id)
        except SessionError:
            return None

    def _task(self, latest: SessionSnapshot | None) -> TaskDto:
        if latest is None:
            return TaskDto(source="unavailable")
        return TaskDto(
            source="saved_session",
            session_id=latest.session_id,
            steps=tuple(
                TaskStepDto(title=step.title, status=step.status)
                for step in latest.plan
            ),
        )

    def _context(self, latest: SessionSnapshot | None) -> ContextDto:
        usage = None if latest is None else latest.last_usage
        if usage is None:
            return ContextDto(
                source="unavailable", limit_tokens=self.settings.max_context_tokens
            )
        return ContextDto(
            source="server_reported",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            limit_tokens=self.settings.max_context_tokens,
        )

    def _review_from(self, git: GitDto, latest: SessionSnapshot | None) -> ReviewDto:
        quality = None
        if latest is not None and latest.latest_quality_check is not None:
            record = latest.latest_quality_check
            quality = QualityCheckDto(
                check=record.check, status=record.status, exit_code=record.exit_code
            )
        if not git.available:
            state: ReviewState = "unavailable"
        elif quality is not None and quality.status == "failed":
            state = "failed"
        elif quality is not None and quality.status == "passed":
            state = "passed" if git.change_count else "empty"
        elif git.change_count:
            state = "stale"
        else:
            state = "empty"
        return ReviewDto(state=state, git=git, quality_check=quality)

    def _resolve_relative_directory(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or "\x00" in relative_path:
            raise ValueError("file tree path is invalid")
        normalized = relative_path.replace("\\", "/").strip("/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            if normalized:
                raise ValueError("file tree path must be workspace-relative")
        if any(self._is_sensitive_name(part) for part in pure.parts):
            raise ValueError("file tree path is not available")
        candidate = self.root.joinpath(*pure.parts)
        try:
            if candidate.is_symlink():
                raise ValueError("file tree path cannot be a symbolic link")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as error:
            raise ValueError("file tree path is unavailable") from error
        if not resolved.is_dir():
            raise ValueError("file tree path must be a directory")
        return resolved

    def _list_directory(
        self, directory: Path, *, depth: int, budget: list[int]
    ) -> tuple[FileNodeDto, ...]:
        nodes: list[FileNodeDto] = []
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda item: (
                    not item.is_dir(follow_symlinks=False),
                    item.name.casefold(),
                ),
            )
        except OSError:
            return ()
        for entry in entries:
            if budget[0] <= 0:
                break
            if self._is_sensitive_name(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                continue
            path = Path(entry.path)
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.root)
            except (OSError, ValueError):
                continue
            budget[0] -= 1
            is_directory = stat.S_ISDIR(info.st_mode)
            children = (
                self._list_directory(resolved, depth=depth - 1, budget=budget)
                if is_directory and depth > 0 and budget[0] > 0
                else ()
            )
            nodes.append(
                FileNodeDto(
                    name=entry.name,
                    path=resolved.relative_to(self.root).as_posix(),
                    kind="directory" if is_directory else "file",
                    children=children,
                )
            )
        return tuple(nodes)

    @staticmethod
    def _is_sensitive_name(name: str) -> bool:
        lowered = name.casefold()
        return (
            lowered in SENSITIVE_NAMES
            or Path(lowered).suffix in BLOCKED_GIT_SUFFIXES
            or lowered.startswith(".env.")
        )

    def _parse_git_status(self, raw: str) -> GitDto:
        lines = raw.splitlines()
        branch = None
        change_lines = lines
        if lines and lines[0].startswith("## "):
            branch_text = lines[0][3:].strip()
            branch = branch_text.split("...", 1)[0].strip()
            if branch.startswith("No commits yet on "):
                branch = branch.removeprefix("No commits yet on ")
            change_lines = lines[1:]
        files: list[GitFileDto] = []
        total_changes = 0
        for line in change_lines:
            if len(line) < 3:
                continue
            total_changes += 1
            if len(files) >= MAX_REVIEW_FILES:
                continue
            status_code = line[:2]
            path = line[3:].strip().split(" -> ")[-1].strip('"')
            if not path or not self._safe_git_path(path):
                continue
            files.append(
                GitFileDto(
                    path=path[:4_096],
                    status=status_code.strip() or "?",
                    kind=self._git_kind(status_code),
                )
            )
        return GitDto(
            available=True,
            branch=(branch or "detached")[:512],
            change_count=min(total_changes, 10_000),
            files=tuple(files),
            truncated=total_changes > len(files),
        )

    def _safe_git_path(self, value: str) -> bool:
        pure = PurePosixPath(value.replace("\\", "/"))
        return not pure.is_absolute() and all(
            part not in {"", ".", ".."} and not self._is_sensitive_name(part)
            for part in pure.parts
        )

    @staticmethod
    def _git_kind(
        status: str,
    ) -> Literal["modified", "added", "deleted", "renamed", "untracked", "conflict"]:
        if "U" in status or status in {"AA", "DD"}:
            return "conflict"
        if status == "??" or "A" in status:
            return "untracked" if status == "??" else "added"
        if "D" in status:
            return "deleted"
        if "R" in status:
            return "renamed"
        return "modified"
