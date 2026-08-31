"""Approval-gated guest export import into the workspace."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath

from ..approval import ApprovalBinding
from ..errors import ToolError
from ..sandbox_approval import GuestExportImportBinding
from ..sandbox_export import GuestExportManifest
from ..schemas import ToolCall, ToolDefinition
from .filesystem import FileSystemTools
from .registry import ToolRegistry

_MANIFEST_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAGING_DIRECTORY = Path(".neil-agent") / "guest-export-staging"


class GuestExportImportTools:
    """Stage certified exports and apply them after a second explicit approval."""

    def __init__(self, filesystem: FileSystemTools) -> None:
        self._filesystem = filesystem
        self._staged: dict[str, tuple[GuestExportManifest, dict[str, bytes]]] = {}
        self._latest_preview: str | None = None
        self._latest_manifest_sha256: str | None = None

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            IMPORT_GUEST_EXPORT,
            self.import_guest_export,
            requires_approval=True,
            preview_handler=self.preview_import_guest_export,
            binding_resolver=self.resolve_approval_binding,
        )

    def stage(
        self,
        manifest: GuestExportManifest,
        file_contents: dict[str, bytes],
    ) -> str:
        """Retain one export candidate until import is previewed or applied."""

        digest = manifest.manifest_sha256
        staging_root = self._staging_root() / digest
        if staging_root.exists():
            shutil.rmtree(staging_root)
        files_root = staging_root / "files"
        files_root.mkdir(parents=True)
        for entry in manifest.files:
            payload = file_contents[entry.path]
            target = self._staging_file_path(files_root, entry.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            FileSystemTools._write_bytes_no_follow(target, payload, exclusive=True)
        manifest_path = staging_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        self._staged[digest] = (manifest, file_contents)
        return digest

    def preview_import_guest_export(self, manifest_sha256: str) -> str:
        manifest, contents = self._require_staged(manifest_sha256)
        prepared = self._filesystem.prepare_guest_export_import(manifest, contents)
        self._latest_preview = prepared.preview
        self._latest_manifest_sha256 = manifest.manifest_sha256
        return prepared.preview

    def import_guest_export(self, manifest_sha256: str) -> str:
        manifest, contents = self._require_staged(manifest_sha256)
        prepared = self._filesystem.prepare_guest_export_import(manifest, contents)
        if (
            self._latest_preview is None
            or self._latest_manifest_sha256 != manifest.manifest_sha256
            or self._latest_preview != prepared.preview
        ):
            raise ToolError("guest export 导入预览已变化，请重新预览并批准。")
        try:
            return self._filesystem.apply_guest_export_import(manifest, prepared)
        finally:
            self._latest_preview = None
            self._latest_manifest_sha256 = None
            self._clear_staged(manifest.manifest_sha256)

    def resolve_approval_binding(
        self,
        call: ToolCall,
        _preview: str,
    ) -> ApprovalBinding:
        manifest_sha256 = call.arguments.get("manifest_sha256")
        if not isinstance(manifest_sha256, str) or not _MANIFEST_SHA256.fullmatch(
            manifest_sha256
        ):
            raise ToolError("manifest_sha256 无效。")
        manifest, _contents = self._require_staged(manifest_sha256)
        return GuestExportImportBinding.from_manifest(manifest).approval_binding

    def _require_staged(
        self,
        manifest_sha256: str,
    ) -> tuple[GuestExportManifest, dict[str, bytes]]:
        if not isinstance(manifest_sha256, str) or not _MANIFEST_SHA256.fullmatch(
            manifest_sha256
        ):
            raise ToolError("manifest_sha256 无效。")
        cached = self._staged.get(manifest_sha256)
        if cached is not None:
            return cached
        loaded = self._load_staged_from_disk(manifest_sha256)
        self._staged[manifest_sha256] = loaded
        return loaded

    def _staging_root(self) -> Path:
        root = (self._filesystem.root / _STAGING_DIRECTORY).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ToolError("guest export 暂存目录不可用。")
        try:
            root.relative_to(self._filesystem.root.resolve())
        except ValueError as error:
            raise ToolError("guest export 暂存目录越界。") from error
        return root

    def _load_staged_from_disk(
        self,
        manifest_sha256: str,
    ) -> tuple[GuestExportManifest, dict[str, bytes]]:
        staging_root = self._staging_root() / manifest_sha256
        manifest_path = staging_root / "manifest.json"
        if not manifest_path.is_file():
            raise ToolError("guest export 导入候选不存在或已过期，请重新导出。")
        try:
            manifest = GuestExportManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except ValueError as error:
            raise ToolError("guest export 暂存 manifest 无效。") from error
        if manifest.manifest_sha256 != manifest_sha256:
            raise ToolError("guest export 暂存 manifest 摘要不匹配。")
        files_root = staging_root / "files"
        contents: dict[str, bytes] = {}
        for entry in manifest.files:
            path = self._staging_file_path(files_root, entry.path)
            if not path.is_file():
                raise ToolError("guest export 暂存文件不完整。")
            contents[entry.path] = path.read_bytes()
        return manifest, contents

    def _clear_staged(self, manifest_sha256: str) -> None:
        self._staged.pop(manifest_sha256, None)
        staging_root = self._staging_root() / manifest_sha256
        if staging_root.exists():
            shutil.rmtree(staging_root)

    @staticmethod
    def _staging_file_path(files_root: Path, relative_path: str) -> Path:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ToolError("guest export 暂存路径无效。")
        target = (files_root / Path(*relative.parts)).resolve()
        try:
            target.relative_to(files_root.resolve())
        except ValueError as error:
            raise ToolError("guest export 暂存路径越界。") from error
        return target


IMPORT_GUEST_EXPORT = ToolDefinition(
    name="import_guest_export",
    description=(
        "Import one certified guest export manifest into the workspace after "
        "explicit second approval. Requires a prior staged export bound to "
        "manifest_sha256."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "manifest_sha256": {
                "type": "string",
                "description": "SHA-256 digest of the staged guest export manifest.",
            }
        },
        "required": ["manifest_sha256"],
        "additionalProperties": False,
    },
)
