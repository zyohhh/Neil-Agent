"""Workspace-restricted filesystem tools with guarded writes."""

from __future__ import annotations

import os
import stat
import tempfile
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path
from typing import Literal

from ..checkpoint import (
    FileEditCheckpoint,
    FileCheckpointHistory,
    PreparedFileRestore,
    PreparedFileRestoreEntry,
    content_hash,
)
from ..errors import ToolError
from ..execution_budget import check_execution_budget
from ..sandbox_approval import GuestExportImportBinding
from ..sandbox_export import (
    GuestExportError,
    GuestExportManifest,
    PreparedGuestExportImport,
    PreparedGuestExportImportEntry,
    format_guest_export_import_sections,
)
from ..schemas import ToolDefinition
from ..sensitive_paths import is_sensitive_relative_path
from .registry import ToolRegistry

MAX_FILE_SIZE_BYTES = 1_000_000
MAX_SEARCH_RESULTS = 100
MAX_DIFF_PREVIEW_CHARS = 20_000
MAX_TASK_RESTORE_PREVIEW_CHARS = 100_000
MAX_GUEST_EXPORT_IMPORT_PREVIEW_CHARS = 100_000


class FileSystemTools:
    """Expose bounded file access while protecting workspace boundaries."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        checkpoints: FileCheckpointHistory | None = None,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        self.root = root
        self.checkpoints = checkpoints or FileCheckpointHistory()

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

        check_execution_budget()
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

        check_execution_budget()
        file_path = self._resolve(path)
        if not file_path.is_file():
            raise ToolError(f"文件不存在：{path}")
        if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            raise ToolError(f"文件过大，最多允许读取 {MAX_FILE_SIZE_BYTES} 字节。")

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ToolError("只能读取 UTF-8 文本文件。") from error
        check_execution_budget()
        return content

    def search_text(self, query: str, path: str = ".") -> str:
        """Search for case-insensitive text matches within the workspace."""

        check_execution_budget()
        if not query.strip():
            raise ToolError("搜索内容不能为空。")

        target = self._resolve(path)
        files = [target] if target.is_file() else self._walk_files(target)
        matches: list[str] = []
        normalized_query = query.casefold()

        for file_path in files:
            check_execution_budget()
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
        relative = self._relative_display(file_path)
        self.checkpoints.ensure_capacity(relative, current_content, content)
        self._atomic_write(file_path, content)
        checkpoint = self.checkpoints.record(
            relative,
            current_content,
            content,
        )
        return f"已{action}文件：{relative}；任务恢复检查点：{checkpoint.checkpoint_id}"

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
        if current_content == updated_content:
            return f"文件内容没有变化：{self._relative_display(file_path)}"
        relative = self._relative_display(file_path)
        self.checkpoints.ensure_capacity(relative, current_content, updated_content)
        self._atomic_write(file_path, updated_content)
        checkpoint = self.checkpoints.record(
            relative,
            current_content,
            updated_content,
        )
        return (
            f"已在 {relative} 中替换 "
            f"{expected_replacements} 处文本；任务恢复检查点："
            f"{checkpoint.checkpoint_id}"
        )

    def prepare_checkpoint_restore(self, checkpoint_id: str) -> PreparedFileRestore:
        """Preview reversal of one task checkpoint after validating it is still latest."""

        checkpoint = self.checkpoints.latest
        if checkpoint is None:
            raise ToolError("当前进程没有可恢复的文件任务检查点。")
        if checkpoint.checkpoint_id != checkpoint_id:
            raise ToolError(
                "只能恢复最新的任务检查点；较旧的检查点请使用 Git 回退。"
            )
        return self.prepare_latest_restore()

    def prepare_latest_restore(self) -> PreparedFileRestore:
        """Preview reversal of every effective edit in the latest Agent task."""

        checkpoint = self.checkpoints.latest
        if checkpoint is None:
            raise ToolError("当前进程没有可恢复的文件任务检查点。")
        entries: list[PreparedFileRestoreEntry] = []
        previews: list[tuple[str, str, str]] = []
        for edit in checkpoint.edits:
            file_path = self._resolve_checkpoint_file(edit.path)
            current_content = self._read_required_text(file_path, edit.path)
            current_hash = content_hash(current_content)
            if current_hash != edit.resulting_hash:
                raise ToolError(
                    f"任务文件 {edit.path} 在 Agent 编辑后发生外部变化，拒绝恢复。"
                )
            if edit.original_content is None:
                preview = self._format_delete_diff(file_path, current_content)
                action = "删除 Agent 新建文件"
                deletes_created_file = True
            else:
                preview = self._format_diff(
                    file_path,
                    current_content,
                    edit.original_content,
                )
                action = "恢复原内容"
                deletes_created_file = False
            entries.append(
                PreparedFileRestoreEntry(
                    path=edit.path,
                    current_hash=current_hash,
                    current_content=current_content,
                    deletes_created_file=deletes_created_file,
                )
            )
            previews.append((edit.path, action, preview))
        return PreparedFileRestore(
            checkpoint_id=checkpoint.checkpoint_id,
            files=tuple(entries),
            preview=self._format_task_restore_preview(previews),
        )

    def apply_latest_restore(self, prepared: PreparedFileRestore) -> str:
        """Restore one task after full preflight, with in-process failure rollback."""

        checkpoint = self.checkpoints.latest
        if (
            checkpoint is None
            or checkpoint.checkpoint_id != prepared.checkpoint_id
            or tuple(edit.path for edit in checkpoint.edits)
            != tuple(entry.path for entry in prepared.files)
        ):
            raise ToolError("文件任务检查点已变化，请重新预览。")

        prepared_by_path = {entry.path: entry for entry in prepared.files}
        current_hashes: dict[str, str] = {}
        for edit in checkpoint.edits:
            entry = prepared_by_path[edit.path]
            if content_hash(entry.current_content) != entry.current_hash:
                raise ToolError("文件恢复预览无效，请重新预览。")
            file_path = self._resolve_checkpoint_file(edit.path)
            current_content = self._read_required_text(file_path, edit.path)
            current_hash = content_hash(current_content)
            if (
                current_hash != entry.current_hash
                or current_hash != edit.resulting_hash
            ):
                raise ToolError(f"批准后任务文件 {edit.path} 发生变化，拒绝恢复。")
            current_hashes[edit.path] = current_hash

        applied: list[tuple[FileEditCheckpoint, PreparedFileRestoreEntry]] = []
        try:
            for edit in checkpoint.edits:
                entry = prepared_by_path[edit.path]
                file_path = self._resolve_checkpoint_file(edit.path)
                current_content = self._read_required_text(file_path, edit.path)
                if content_hash(current_content) != entry.current_hash:
                    raise ToolError(
                        f"恢复过程中任务文件 {edit.path} 发生变化，拒绝继续。"
                    )
                if edit.original_content is None:
                    try:
                        file_path.unlink()
                    except OSError as error:
                        raise ToolError(
                            f"删除 Agent 新建文件失败：{edit.path}"
                        ) from error
                else:
                    self._atomic_write(file_path, edit.original_content)
                applied.append((edit, entry))
            self.checkpoints.consume(prepared.checkpoint_id, current_hashes)
        except ToolError as error:
            rollback_complete = self._rollback_task_restore(applied)
            if not rollback_complete:
                raise ToolError(
                    "任务恢复失败且自动回滚不完整；检查点已保留，"
                    "请立即检查工作区并使用 Git 恢复。"
                ) from error
            raise ToolError(
                "任务恢复失败，已回滚本次恢复操作；检查点已保留。"
            ) from error

        restored_count = prepared.file_count - prepared.delete_count
        return (
            f"已恢复最近任务检查点：{prepared.file_count} 个文件"
            f"（恢复 {restored_count}，删除 {prepared.delete_count}）"
        )

    def prepare_guest_export_import(
        self,
        manifest: GuestExportManifest,
        file_contents: dict[str, bytes],
    ) -> PreparedGuestExportImport:
        """Preview importing one certified guest export manifest into the workspace."""

        binding = GuestExportImportBinding.from_manifest(manifest)
        manifest_paths = {item.path for item in manifest.files}
        if set(file_contents) != manifest_paths:
            raise ToolError("guest export import 文件集合与 manifest 不一致。")

        entries: list[PreparedGuestExportImportEntry] = []
        previews: list[tuple[str, str, str]] = []
        for file_entry in manifest.files:
            payload = file_contents[file_entry.path]
            if sha256(payload).hexdigest() != file_entry.sha256:
                raise ToolError(
                    f"guest export 文件 {file_entry.path} 内容与 manifest 摘要不一致。"
                )
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ToolError(
                    f"guest export 文件 {file_entry.path} 不是 UTF-8 文本。"
                ) from error
            self._validate_new_content(content)
            _file_path, action, prior_hash, prior_content = (
                self._prepare_guest_import_target(file_entry.path)
            )
            action_label = "新建" if action == "create" else "替换"
            preview = self._format_diff(_file_path, prior_content, content)
            entries.append(
                PreparedGuestExportImportEntry(
                    path=file_entry.path,
                    content=content,
                    action=action,
                    prior_hash=prior_hash,
                    prior_content=prior_content,
                )
            )
            previews.append((file_entry.path, action_label, preview))

        try:
            sections = format_guest_export_import_sections(
                tuple(previews),
                max_chars=MAX_GUEST_EXPORT_IMPORT_PREVIEW_CHARS,
            )
        except GuestExportError as error:
            raise ToolError(str(error)) from error

        return PreparedGuestExportImport(
            manifest_sha256=manifest.manifest_sha256,
            binding_digest=binding.digest,
            files=tuple(entries),
            preview=binding.render_preview(sections),
        )

    def apply_guest_export_import(
        self,
        manifest: GuestExportManifest,
        prepared: PreparedGuestExportImport,
    ) -> str:
        """Apply one guest export import after approval, with in-process rollback."""

        binding = GuestExportImportBinding.from_manifest(manifest)
        if prepared.manifest_sha256 != manifest.manifest_sha256:
            raise ToolError("guest export manifest 已变化，请重新预览。")
        if prepared.binding_digest != binding.digest:
            raise ToolError("guest export 审批绑定无效，请重新预览。")
        manifest_paths = tuple(item.path for item in manifest.files)
        if tuple(entry.path for entry in prepared.files) != manifest_paths:
            raise ToolError("guest export import 文件列表已变化，请重新预览。")

        prepared_by_path = {entry.path: entry for entry in prepared.files}
        for file_entry in manifest.files:
            entry = prepared_by_path[file_entry.path]
            if sha256(entry.content.encode("utf-8")).hexdigest() != file_entry.sha256:
                raise ToolError(
                    f"guest export 预览内容 {file_entry.path} 无效，请重新预览。"
                )
            _file_path, action, prior_hash, prior_content = (
                self._prepare_guest_import_target(file_entry.path)
            )
            if action != entry.action:
                raise ToolError(
                    f"guest export 文件 {file_entry.path} 在批准后发生变化，拒绝导入。"
                )
            if action == "replace" and (
                prior_hash != entry.prior_hash or prior_content != entry.prior_content
            ):
                raise ToolError(
                    f"批准后工作区文件 {file_entry.path} 发生变化，拒绝导入。"
                )

        applied: list[PreparedGuestExportImportEntry] = []
        try:
            for file_entry in manifest.files:
                entry = prepared_by_path[file_entry.path]
                file_path, action, prior_hash, _prior_content = (
                    self._prepare_guest_import_target(file_entry.path)
                )
                if action != entry.action or (
                    action == "replace" and prior_hash != entry.prior_hash
                ):
                    raise ToolError(
                        f"导入过程中工作区文件 {file_entry.path} 发生变化，拒绝继续。"
                    )
                if action == "create":
                    file_path = self._prepare_write_target(file_entry.path)
                else:
                    file_path = self._resolve_checkpoint_file(file_entry.path)
                self._atomic_write(file_path, entry.content)
                applied.append(entry)
        except ToolError as error:
            rollback_complete = self._rollback_guest_export_import(applied)
            if not rollback_complete:
                raise ToolError(
                    "guest export 导入失败且自动回滚不完整；"
                    "请立即检查工作区并使用 Git 恢复。"
                ) from error
            raise ToolError(
                "guest export 导入失败，已回滚本次导入操作。"
            ) from error

        return (
            f"已导入 guest export：{prepared.file_count} 个文件"
            f"（新建 {prepared.create_count}，替换 {prepared.replace_count}）"
        )

    def _rollback_task_restore(
        self,
        applied: list[tuple[FileEditCheckpoint, PreparedFileRestoreEntry]],
    ) -> bool:
        """Best-effort rollback without overwriting changes made during rollback."""

        complete = True
        for edit, entry in reversed(applied):
            original_content = edit.original_content
            try:
                if original_content is None:
                    file_path = self._prepare_write_target(entry.path)
                    if file_path.exists():
                        raise ToolError("恢复回滚期间 Agent 新建文件路径已被外部占用。")
                else:
                    file_path = self._resolve_checkpoint_file(entry.path)
                    restored_content = self._read_required_text(file_path, entry.path)
                    if content_hash(restored_content) != content_hash(original_content):
                        raise ToolError("恢复回滚期间文件发生外部变化。")
                self._atomic_write(file_path, entry.current_content)
            except ToolError:
                complete = False
        return complete

    def _rollback_guest_export_import(
        self,
        applied: list[PreparedGuestExportImportEntry],
    ) -> bool:
        """Best-effort rollback for a partially applied guest export import."""

        complete = True
        for entry in reversed(applied):
            try:
                if entry.action == "create":
                    file_path = self._lexical_import_path(entry.path)
                    if not file_path.exists():
                        continue
                    try:
                        file_stat = file_path.lstat()
                        resolved = file_path.resolve(strict=True)
                    except OSError as error:
                        raise ToolError(
                            f"回滚 guest export 新建文件失败：{entry.path}"
                        ) from error
                    if (
                        file_path.is_symlink()
                        or not stat.S_ISREG(file_stat.st_mode)
                        or resolved != file_path
                    ):
                        raise ToolError(
                            "guest export 回滚期间文件路径发生外部变化。"
                        )
                    current_content = self._read_required_text(file_path, entry.path)
                    if content_hash(current_content) != content_hash(entry.content):
                        raise ToolError(
                            "guest export 回滚期间文件发生外部变化。"
                        )
                    file_path.unlink()
                else:
                    file_path = self._resolve_checkpoint_file(entry.path)
                    current_content = self._read_required_text(file_path, entry.path)
                    if content_hash(current_content) != content_hash(entry.content):
                        raise ToolError(
                            "guest export 回滚期间文件发生外部变化。"
                        )
                    if entry.prior_content is None:
                        raise ToolError("guest export 回滚缺少原始内容。")
                    self._atomic_write(file_path, entry.prior_content)
            except ToolError:
                complete = False
        return complete

    def _prepare_guest_import_target(
        self,
        path: str,
    ) -> tuple[Path, Literal["create", "replace"], str | None, str | None]:
        """Resolve one import target and capture the current workspace state."""

        lexical = self._lexical_import_path(path)
        if not lexical.exists():
            file_path = self._prepare_write_target(path)
            return file_path, "create", None, None
        file_path = self._resolve_checkpoint_file(path)
        prior_content = self._read_required_text(file_path, path)
        return file_path, "replace", content_hash(prior_content), prior_content

    def _lexical_import_path(self, path: str) -> Path:
        requested = Path(path)
        if requested.is_absolute():
            raise ToolError("guest export 路径无效，拒绝导入。")
        lexical = self.root / requested
        try:
            lexical.relative_to(self.root)
        except ValueError as error:
            raise ToolError("拒绝导入工作区之外的路径。") from error
        if not self._is_allowed(lexical):
            raise ToolError("该路径包含受保护的目录或敏感文件。")
        return lexical

    def _resolve_checkpoint_file(self, path: str) -> Path:
        """Resolve a recorded path without following a replacement link."""

        requested = Path(path)
        if requested.is_absolute():
            raise ToolError("文件检查点路径无效，拒绝恢复。")
        lexical = self.root / requested
        try:
            file_stat = lexical.lstat()
            resolved = lexical.resolve(strict=True)
        except FileNotFoundError as error:
            raise ToolError(f"文件不存在：{path}") from error
        except OSError as error:
            raise ToolError("无法检查文件恢复目标。") from error
        if (
            lexical.is_symlink()
            or not stat.S_ISREG(file_stat.st_mode)
            or resolved != lexical
        ):
            raise ToolError("文件路径在 Agent 编辑后发生外部变化，拒绝恢复。")
        try:
            lexical.relative_to(self.root)
        except ValueError as error:
            raise ToolError("拒绝恢复工作区之外的路径。") from error
        if not self._is_allowed(lexical):
            raise ToolError("该路径包含受保护的目录或敏感文件。")
        return lexical

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
        lexical = self._lexical_workspace_path(path)
        self._assert_write_path_safe(lexical)
        if lexical.exists():
            self._require_safe_regular_file(lexical, path)
        elif not lexical.parent.is_dir():
            raise ToolError(f"父目录不存在：{self._relative_display(lexical.parent)}")
        return lexical

    def _lexical_workspace_path(self, path: str) -> Path:
        requested = Path(path)
        if requested.is_absolute():
            raise ToolError("拒绝使用绝对路径写入。")
        if ".." in requested.parts:
            raise ToolError("拒绝访问工作区之外的路径。")
        lexical = self.root.joinpath(*requested.parts)
        try:
            lexical.relative_to(self.root)
        except ValueError as error:
            raise ToolError("拒绝访问工作区之外的路径。") from error
        if not self._is_allowed_lexical(lexical):
            raise ToolError("该路径包含受保护的目录或敏感文件。")
        return lexical

    def _is_allowed_lexical(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return False
        return not is_sensitive_relative_path(relative.parts)

    def _assert_write_path_safe(self, lexical: Path) -> None:
        relative = lexical.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current == lexical:
                break
            if current.exists() and current.is_symlink():
                raise ToolError("路径包含符号链接目录，拒绝写入。")

    @staticmethod
    def _require_safe_regular_file(file_path: Path, display_path: str) -> None:
        try:
            file_stat = file_path.lstat()
            resolved = file_path.resolve(strict=True)
        except FileNotFoundError as error:
            raise ToolError(f"文件不存在：{display_path}") from error
        except OSError as error:
            raise ToolError("无法检查写入目标。") from error
        if (
            file_path.is_symlink()
            or not stat.S_ISREG(file_stat.st_mode)
            or resolved != file_path
        ):
            raise ToolError("拒绝写入符号链接或非常规文件目标。")

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

    @staticmethod
    def _format_task_restore_preview(
        previews: list[tuple[str, str, str]],
    ) -> str:
        """Show every target while bounding the combined reverse diff."""

        if not previews:
            raise ToolError("文件任务检查点不包含可恢复文件。")
        headers = [
            f"=== {path} · {action} ===\n" for path, action, _preview in previews
        ]
        separators_size = max(len(previews) - 1, 0) * 2
        available = (
            MAX_TASK_RESTORE_PREVIEW_CHARS
            - sum(len(header) for header in headers)
            - separators_size
        )
        if available < 0:
            raise ToolError("文件任务恢复范围过大，无法安全生成完整路径预览。")
        per_file_limit = available // len(previews)
        marker = "\n... 该文件的反向 diff 已按任务预览上限截断。"
        sections: list[str] = []
        for header, (_path, _action, preview) in zip(
            headers,
            previews,
            strict=True,
        ):
            if len(preview) > per_file_limit:
                kept_chars = max(per_file_limit - len(marker), 0)
                preview = preview[:kept_chars] + marker[:per_file_limit]
            sections.append(header + preview)
        return "\n\n".join(sections)

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
        full_len = len(diff)
        if full_len > MAX_DIFF_PREVIEW_CHARS:
            diff = (
                diff[:MAX_DIFF_PREVIEW_CHARS]
                + f"\n... diff 预览已截断（完整 {full_len} 字符，上限 {MAX_DIFF_PREVIEW_CHARS}）。"
            )
        return f"{diff}\nChange-ID: {change_id}"

    def _format_delete_diff(self, file_path: Path, current_content: str) -> str:
        relative = self._relative_display(file_path)
        diff = "".join(
            unified_diff(
                current_content.splitlines(keepends=True),
                [],
                fromfile=f"a/{relative}",
                tofile="/dev/null",
                lineterm="\n",
            )
        )
        change_id = sha256(
            current_content.encode("utf-8") + b"\0<deleted>"
        ).hexdigest()[:16]
        full_len = len(diff)
        if full_len > MAX_DIFF_PREVIEW_CHARS:
            diff = (
                diff[:MAX_DIFF_PREVIEW_CHARS]
                + f"\n... diff 预览已截断（完整 {full_len} 字符，上限 {MAX_DIFF_PREVIEW_CHARS}）。"
            )
        return f"{diff}\nChange-ID: {change_id}"

    def _atomic_write(self, file_path: Path, content: str) -> None:
        self._assert_write_path_safe(file_path)
        if file_path.exists():
            self._require_safe_regular_file(
                file_path,
                self._relative_display(file_path),
            )
        payload = content.encode("utf-8")
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if no_follow:
            try:
                if file_path.exists():
                    self._write_bytes_no_follow(file_path, payload, exclusive=False)
                else:
                    self._write_bytes_no_follow(file_path, payload, exclusive=True)
                return
            except OSError as error:
                raise ToolError("写入失败，原文件保持不变。") from error

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
                self._require_safe_regular_file(
                    file_path,
                    self._relative_display(file_path),
                )
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

    @staticmethod
    def _write_bytes_no_follow(
        file_path: Path,
        payload: bytes,
        *,
        exclusive: bool,
    ) -> None:
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if exclusive:
            flags |= os.O_EXCL
        else:
            flags |= os.O_TRUNC
        descriptor = os.open(file_path, flags, 0o644)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _is_allowed(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.root)
        except (OSError, ValueError):
            return False
        if is_sensitive_relative_path(relative.parts):
            return False
        return True

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
