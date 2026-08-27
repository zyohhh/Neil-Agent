"""Bounded, read-only projection service for Web Workbench P1."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal

from ..config import Settings
from ..context_projection import build_host_context_tomography
from ..errors import SessionError, ToolError
from ..host_runtime import HostMode, build_host_runtime, observe_host_security
from ..providers.factory import describe_provider
from ..runtime_models import runtime_model_catalog
from ..sensitive_paths import is_sensitive_entry_name, is_sensitive_relative_path
from ..session import (
    SESSION_DIRECTORY,
    SESSION_STATE_DIRECTORY,
    SessionSnapshot,
    SessionStore,
)
from ..tools.registry import ToolRegistry
from ..tools.shell import ShellTools
from .dto import (
    ContextDto,
    CostEstimateDto,
    FileNodeDto,
    FileTreeDto,
    GitDto,
    GitDiffDto,
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
from .pricing import ProviderRateTable, load_rate_table

MAX_TREE_NODES = 300
MAX_TREE_DEPTH = 4
MAX_REVIEW_FILES = 100
MAX_WEB_DIFF_CHARS = 40_000
ReviewState = Literal["empty", "passed", "failed", "stale", "unavailable"]


@dataclass(frozen=True, slots=True)
class _GitStatusEntry:
    status: str
    path: str
    previous_path: str | None = None


class WorkbenchSnapshotService:
    """Project local metadata without exposing content or mutation capabilities."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
        rate_table: ProviderRateTable | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        root = settings.workspace_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Web Workbench workspace must be a directory")
        self.settings = settings
        self.root = root
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rate_table = rate_table or load_rate_table(settings.web_rate_table)
        if (
            session_store is not None
            and session_store.root != root / SESSION_STATE_DIRECTORY / SESSION_DIRECTORY
        ):
            raise ValueError("Web Workbench session store must belong to the workspace")
        self._sessions = session_store or SessionStore(root)
        self._shell = ShellTools(
            root,
            timeout=min(settings.command_timeout, 5.0),
            max_output_chars=max(settings.max_command_output_chars, 1_000),
        )
        self._host_runtime = build_host_runtime(settings, mode=HostMode.WEB)

    @property
    def session_store(self) -> SessionStore:
        """Return the validated workspace-local store shared by the controller."""

        return self._sessions

    @property
    def registry(self) -> ToolRegistry:
        """Return the shared tool registry for approval binding resolution."""

        return self._host_runtime.registry

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
                    runtime_provider=(
                        None
                        if item.runtime_provider is None
                        else item.runtime_provider.value
                    ),
                    runtime_model=item.runtime_model,
                )
                for item in index.sessions
            ),
            invalid_count=index.invalid_count,
            total_count=index.valid_count,
        )

    def files(
        self,
        relative_path: str = "",
        *,
        depth: int = 2,
        revision: str | None = None,
    ) -> FileTreeDto:
        """Return a bounded tree without following links or reading file contents."""

        if depth < 0 or depth > MAX_TREE_DEPTH:
            raise ValueError(f"file tree depth must be between 0 and {MAX_TREE_DEPTH}")
        requested = self._resolve_relative_directory(relative_path)
        budget = [MAX_TREE_NODES]
        items = self._list_directory(requested, depth=depth, budget=budget)
        current_revision = self._file_tree_revision(requested, items)
        return FileTreeDto(
            root=requested.relative_to(self.root).as_posix()
            if requested != self.root
            else "",
            items=() if revision == current_revision else items,
            truncated=budget[0] == 0,
            revision=current_revision,
            unchanged=revision == current_revision,
        )

    def git(self) -> GitDto:
        """Return concise Git metadata through the existing fixed command boundary."""

        try:
            raw = self._shell.git_review_status_snapshot()
        except (ToolError, OSError):
            return GitDto(available=False)
        try:
            numstat = self._shell.git_numstat_snapshot()
        except (ToolError, OSError):
            numstat = None
        return self._parse_git_status(raw, numstat)

    def diff(self, path: str, *, revision: str) -> GitDiffDto:
        """Return one bounded diff only for a current, visible Git status entry."""

        git = self.git()
        if not git.available or git.revision is None:
            return GitDiffDto(
                path=self._bounded_requested_path(path),
                revision="0" * 16,
                available=False,
                reason="unavailable",
            )
        if revision != git.revision:
            return GitDiffDto(
                path=self._bounded_requested_path(path),
                revision=git.revision,
                available=False,
                reason="stale",
            )
        requested = next((item for item in git.files if item.path == path), None)
        if requested is None:
            raise ValueError("diff path is not a current visible Git change")
        if not requested.diff_available:
            reason = requested.diff_reason
            return GitDiffDto(
                path=requested.path,
                previous_path=requested.previous_path,
                revision=git.revision,
                available=False,
                reason=reason,
            )
        paths = [requested.path]
        if requested.previous_path is not None:
            paths.insert(0, requested.previous_path)
        try:
            snapshot = self._shell.git_file_diff_snapshot(
                paths, max_chars=MAX_WEB_DIFF_CHARS
            )
        except (ToolError, OSError):
            return GitDiffDto(
                path=requested.path,
                previous_path=requested.previous_path,
                revision=git.revision,
                available=False,
                reason="unavailable",
            )
        if (
            "GIT binary patch" in snapshot.content
            or "Binary files " in snapshot.content
        ):
            return GitDiffDto(
                path=requested.path,
                previous_path=requested.previous_path,
                revision=git.revision,
                available=False,
                reason="binary",
            )
        return GitDiffDto(
            path=requested.path,
            previous_path=requested.previous_path,
            revision=git.revision,
            available=bool(snapshot.content),
            reason="available" if snapshot.content else "empty",
            content=snapshot.content,
            truncated=snapshot.truncated,
        )

    def review(
        self,
        session: SessionSnapshot | None = None,
        *,
        fallback_to_latest: bool = True,
        runtime_settings: Settings | None = None,
    ) -> ReviewDto:
        """Combine Git metadata with one explicitly selected session when supplied."""

        selected = session
        if selected is None and fallback_to_latest:
            selected = self._latest_session()
        settings = runtime_settings or self.settings
        return self._review_from(self.git(), selected, settings)

    def snapshot(
        self,
        session: SessionSnapshot | None = None,
        *,
        fallback_to_latest: bool = True,
        runtime_settings: Settings | None = None,
    ) -> WorkbenchSnapshotDto:
        """Build one internally consistent, versioned first-screen snapshot."""

        settings = runtime_settings or self.settings
        sessions = self.sessions()
        selected = session
        if selected is None and fallback_to_latest:
            selected = self._latest_session(sessions)
        git = self.git()
        review = self._review_from(git, selected, settings)
        provider = describe_provider(settings)
        model_catalog = runtime_model_catalog(settings)
        capabilities = provider.capabilities
        security = observe_host_security(
            settings,
            self._host_runtime.registry,
            audit_probe=(
                self._host_runtime.audit_sink.inspect
                if self._host_runtime.audit_sink is not None
                else None
            ),
        )
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
                model=settings.selected_model,
                available_models=model_catalog.models,
                wire_protocol=provider.wire_protocol.value,
                thinking_enabled=settings.thinking_enabled,
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
            task=self._task(selected),
            context=self.context_dto(selected, settings),
            review=review,
            security=SecurityDto.from_security_shield(
                security,
                sandbox_backend=settings.sandbox_backend,
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

    def context_dto(
        self,
        session: SessionSnapshot | None,
        settings: Settings,
    ) -> ContextDto:
        messages = () if session is None else session.messages
        usage = None if session is None else session.last_usage
        tomography = build_host_context_tomography(
            settings,
            self._host_runtime,
            messages,
            last_server_usage=usage,
        )
        return ContextDto.from_tomography(tomography)

    def _review_from(
        self,
        git: GitDto,
        latest: SessionSnapshot | None,
        settings: Settings,
    ) -> ReviewDto:
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
        cost = self._cost(latest, settings)
        return ReviewDto(
            state=state,
            git=git,
            quality_check=quality,
            quality_checks=() if quality is None else (quality,),
            cost=cost,
            cost_available=cost.source == "versioned_rate_table",
        )

    def _cost(
        self,
        latest: SessionSnapshot | None,
        settings: Settings,
    ) -> CostEstimateDto:
        table = self._rate_table
        if table is None:
            return CostEstimateDto(source="unavailable", reason="no_rate_table")
        if table.effective_date > self._clock().date():
            return CostEstimateDto(
                source="unavailable",
                rate_table_version=table.version,
                rate_effective_date=table.effective_date.isoformat(),
                model=settings.selected_model,
                reason="rate_not_effective",
            )
        usage = None if latest is None else latest.last_usage
        if usage is None:
            return CostEstimateDto(
                source="unavailable",
                rate_table_version=table.version,
                rate_effective_date=table.effective_date.isoformat(),
                model=settings.selected_model,
                reason="no_saved_usage",
            )
        rate = table.find(settings.llm_provider.value, settings.selected_model)
        if rate is None:
            return CostEstimateDto(
                source="unavailable",
                rate_table_version=table.version,
                rate_effective_date=table.effective_date.isoformat(),
                model=settings.selected_model,
                reason="model_not_listed",
            )
        estimate = rate.estimate(usage)
        if estimate is None:
            return CostEstimateDto(
                source="unavailable",
                rate_table_version=table.version,
                rate_effective_date=table.effective_date.isoformat(),
                model=settings.selected_model,
                reason="cache_rate_missing",
            )
        rounded = estimate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return CostEstimateDto(
            source="versioned_rate_table",
            estimated_usd=f"{rounded:.6f}",
            rate_table_version=table.version,
            rate_effective_date=table.effective_date.isoformat(),
            model=settings.selected_model,
            reason="estimated",
        )

    def _resolve_relative_directory(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or "\x00" in relative_path:
            raise ValueError("file tree path is invalid")
        normalized = relative_path.replace("\\", "/").strip("/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            if normalized:
                raise ValueError("file tree path must be workspace-relative")
        if any(is_sensitive_entry_name(part) for part in pure.parts):
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
            if is_sensitive_entry_name(entry.name):
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
    def _file_tree_revision(root: Path, items: tuple[FileNodeDto, ...]) -> str:
        def walk(nodes: tuple[FileNodeDto, ...]) -> list[str]:
            values: list[str] = []
            for node in nodes:
                values.append(f"{node.kind}:{node.path}")
                values.extend(walk(node.children))
            return values

        identity = "\0".join([root.name, *walk(items)])
        return sha256(identity.encode("utf-8")).hexdigest()[:16]

    def _parse_git_status(self, raw: str, numstat: str | None) -> GitDto:
        records = raw.split("\0")
        branch = None
        if records and records[0].startswith("## "):
            branch_text = records.pop(0)[3:].strip()
            branch = branch_text.split("...", 1)[0].strip()
            if branch.startswith("No commits yet on "):
                branch = branch.removeprefix("No commits yet on ")
        entries: list[_GitStatusEntry] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4 or record[2] != " ":
                continue
            status = record[:2]
            path = record[3:]
            previous_path = None
            if "R" in status or "C" in status:
                if index >= len(records):
                    continue
                previous_path = records[index]
                index += 1
            entries.append(
                _GitStatusEntry(
                    status=status,
                    path=path,
                    previous_path=previous_path,
                )
            )
        stats = self._parse_numstat(numstat or "")
        files: list[GitFileDto] = []
        total_changes = 0
        for entry in entries:
            if not self._safe_git_path(entry.path):
                continue
            if entry.previous_path is not None and not self._safe_git_path(
                entry.previous_path
            ):
                continue
            total_changes += 1
            if len(files) >= MAX_REVIEW_FILES:
                continue
            status_code = entry.status
            additions, deletions = self._combined_numstat(entry, stats)
            kind = self._git_kind(status_code)
            binary = (additions, deletions) == (None, None) and any(
                stats.get(path) == (None, None)
                for path in (entry.path, entry.previous_path)
                if path is not None
            )
            diff_available = (
                numstat is not None
                and kind not in {"untracked", "conflict"}
                and not binary
            )
            reason: Literal[
                "available", "untracked", "binary", "conflict", "unavailable"
            ]
            if kind == "untracked":
                reason = "untracked"
            elif kind == "conflict":
                reason = "conflict"
            elif binary:
                reason = "binary"
            elif diff_available:
                reason = "available"
            else:
                reason = "unavailable"
            files.append(
                GitFileDto(
                    path=entry.path[:4_096],
                    previous_path=(
                        entry.previous_path[:4_096]
                        if entry.previous_path is not None
                        else None
                    ),
                    status=status_code.strip() or "?",
                    kind=kind,
                    additions=additions,
                    deletions=deletions,
                    diff_available=diff_available,
                    diff_reason=reason,
                )
            )
        return GitDto(
            available=True,
            branch=(branch or "detached")[:512],
            revision=self._git_revision(raw, numstat or "", entries),
            change_count=min(total_changes, 10_000),
            files=tuple(files),
            truncated=total_changes > len(files),
        )

    def _git_revision(
        self,
        status: str,
        numstat: str,
        entries: list[_GitStatusEntry],
    ) -> str:
        digest = sha256()
        digest.update(status.encode("utf-8"))
        digest.update(numstat.encode("utf-8"))
        for entry in entries[:MAX_REVIEW_FILES]:
            if not self._safe_git_path(entry.path):
                continue
            candidate = self.root.joinpath(*PurePosixPath(entry.path).parts)
            try:
                info = candidate.lstat()
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self.root)
            except (OSError, ValueError):
                digest.update(f"missing:{entry.path}".encode("utf-8"))
                continue
            if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
                digest.update(f"blocked:{entry.path}".encode("utf-8"))
                continue
            digest.update(
                f"file:{entry.path}:{info.st_size}:{info.st_mtime_ns}".encode("utf-8")
            )
        return digest.hexdigest()[:16]

    def _parse_numstat(self, raw: str) -> dict[str, tuple[int | None, int | None]]:
        stats: dict[str, tuple[int | None, int | None]] = {}
        for record in raw.split("\0"):
            if not record:
                continue
            parts = record.split("\t", 2)
            if len(parts) != 3 or not self._safe_git_path(parts[2]):
                continue
            if parts[0] == "-" or parts[1] == "-":
                stats[parts[2]] = (None, None)
                continue
            try:
                stats[parts[2]] = (int(parts[0]), int(parts[1]))
            except ValueError:
                continue
        return stats

    @staticmethod
    def _combined_numstat(
        entry: _GitStatusEntry,
        stats: dict[str, tuple[int | None, int | None]],
    ) -> tuple[int | None, int | None]:
        values = [
            stats[path]
            for path in (entry.path, entry.previous_path)
            if path is not None and path in stats
        ]
        if not values or any(value == (None, None) for value in values):
            return None, None
        return sum(value[0] or 0 for value in values), sum(
            value[1] or 0 for value in values
        )

    def _bounded_requested_path(self, value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4_096
            or "\x00" in value
            or not self._safe_git_path(value)
        ):
            raise ValueError("diff path must be a safe workspace-relative path")
        return value

    def _safe_git_path(self, value: str) -> bool:
        pure = PurePosixPath(value.replace("\\", "/"))
        return (
            not pure.is_absolute()
            and all(part not in {"", ".", ".."} for part in pure.parts)
            and not is_sensitive_relative_path(pure.parts)
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
