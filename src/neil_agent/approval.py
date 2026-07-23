"""Bound, one-use approvals for explicitly enabled non-interactive mutations."""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal
from unicodedata import category

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import ApprovalError
from .schemas import ToolCall

APPROVAL_DIRECTORY = Path(".neil-agent") / "approvals"
PENDING_DIRECTORY = "pending"
CONSUMED_DIRECTORY = "consumed"
APPROVAL_RECORD_VERSION: Literal[1] = 1
APPROVAL_TTL = timedelta(minutes=15)
MAX_APPROVAL_PREVIEW_CHARS = 30_000
MAX_APPROVAL_RECORD_BYTES = 64_000
MAX_PENDING_APPROVALS = 100
ApprovalMode = Literal["request", "approve"]
ApprovalRequestHandler = Callable[["ApprovalRequest"], None]
InstructionProvider = Callable[[], str]


class ApprovalRequest(BaseModel):
    """Persisted metadata for one exact operation preview."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = APPROVAL_RECORD_VERSION
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: datetime
    expires_at: datetime
    workspace: str = Field(min_length=1, max_length=4_096)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instructions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_name: str = Field(min_length=1, max_length=128)
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview: str = Field(min_length=1, max_length=MAX_APPROVAL_PREVIEW_CHARS)

    @property
    def approval_id(self) -> str:
        """Bind the caller-visible ID to the exact canonical record."""

        return f"{self.request_id}.{_request_digest(self)}"

    @field_validator("preview")
    @classmethod
    def validate_preview_characters(cls, value: str) -> str:
        """Reject terminal-spoofing control and format characters."""

        if any(
            category(character).startswith("C") and character not in {"\n", "\r", "\t"}
            for character in value
        ):
            raise ValueError("approval preview contains unsafe characters")
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> ApprovalRequest:
        """Require timezone-aware, increasing timestamps."""

        for value in (self.created_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("approval timestamps must include a timezone")
        if self.expires_at <= self.created_at:
            raise ValueError("approval expiry must be after creation")
        return self


class ApprovalStore:
    """Create and atomically consume workspace-local approval requests."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ApprovalError("审批工作区不是有效目录。")
        self._workspace_root = root
        self._approval_root = root / APPROVAL_DIRECTORY
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create(
        self,
        call: ToolCall,
        preview: str,
        *,
        prompt: str,
        instructions: str,
    ) -> ApprovalRequest:
        """Persist a bounded request without storing prompt or instruction text."""

        if not preview or len(preview) > MAX_APPROVAL_PREVIEW_CHARS:
            raise ApprovalError(
                f"审批预览必须为 1–{MAX_APPROVAL_PREVIEW_CHARS} 个字符。"
            )
        pending_root, _ = self._resolved_roots()
        self._prune_expired_pending(pending_root)
        if self._record_count(pending_root) >= MAX_PENDING_APPROVALS:
            raise ApprovalError(
                f"待审批请求已达到 {MAX_PENDING_APPROVALS} 项上限，请先清理。"
            )
        created_at = self._now()
        try:
            request = ApprovalRequest(
                request_id=secrets.token_hex(16),
                created_at=created_at,
                expires_at=created_at + APPROVAL_TTL,
                workspace=str(self._workspace_root),
                prompt_sha256=_text_digest(prompt),
                instructions_sha256=_text_digest(instructions),
                tool_name=call.name,
                arguments_sha256=_arguments_digest(call),
                preview_sha256=_text_digest(preview),
                preview=preview,
            )
        except (ValidationError, ValueError) as error:
            raise ApprovalError("审批预览或工具元数据格式无效。") from error
        payload = (request.model_dump_json() + "\n").encode("utf-8")
        if len(payload) > MAX_APPROVAL_RECORD_BYTES:
            raise ApprovalError("审批请求超过记录大小上限。")
        self._write_exclusive(
            pending_root / f"{request.request_id}.json",
            payload,
        )
        return request

    def preflight(
        self,
        approval_id: str,
        *,
        prompt: str,
    ) -> ApprovalRequest:
        """Load an unused request and bind it to this workspace and prompt."""

        request = self.load(approval_id)
        if request.workspace != str(self._workspace_root):
            raise ApprovalError("审批请求不属于当前工作区。")
        if request.prompt_sha256 != _text_digest(prompt):
            raise ApprovalError("审批请求与当前 prompt 不匹配。")
        return request

    def load(self, approval_id: str) -> ApprovalRequest:
        """Load one pending request after rejecting replay and unsafe paths."""

        normalized_id, expected_digest = _normalize_approval_id(approval_id)
        pending_root, consumed_root = self._resolved_roots()
        consumed_path = consumed_root / f"{normalized_id}.json"
        if self._regular_file_size(consumed_path):
            raise ApprovalError("审批请求已经使用，不能重放。")
        pending_path = pending_root / f"{normalized_id}.json"
        size = self._regular_file_size(pending_path)
        if size == 0:
            raise ApprovalError("审批请求不存在。")
        if size > MAX_APPROVAL_RECORD_BYTES:
            raise ApprovalError("审批请求文件超过大小上限。")
        try:
            payload = self._read_regular_file(pending_path)
            request = ApprovalRequest.model_validate_json(payload)
        except (OSError, ValidationError, ValueError) as error:
            raise ApprovalError("审批请求格式无效。") from error
        if request.request_id != normalized_id:
            raise ApprovalError("审批请求 ID 与文件名不匹配。")
        if _request_digest(request) != expected_digest:
            raise ApprovalError("审批记录与用户确认的 approval ID 不匹配。")
        if self._now() >= request.expires_at:
            raise ApprovalError("审批请求已经过期，请重新生成预览。")
        return request

    def matches(
        self,
        request: ApprovalRequest,
        call: ToolCall,
        preview: str,
        *,
        prompt: str,
        instructions: str,
    ) -> bool:
        """Return whether the current operation is exactly the approved preview."""

        return (
            request.workspace == str(self._workspace_root)
            and request.prompt_sha256 == _text_digest(prompt)
            and request.instructions_sha256 == _text_digest(instructions)
            and request.tool_name == call.name
            and request.arguments_sha256 == _arguments_digest(call)
            and request.preview_sha256 == _text_digest(preview)
            and request.preview == preview
        )

    def consume(
        self,
        request: ApprovalRequest,
        call: ToolCall,
        preview: str,
        *,
        prompt: str,
        instructions: str,
    ) -> None:
        """Atomically burn one matching request before the mutation executes."""

        current = self.load(request.approval_id)
        if current != request:
            raise ApprovalError("审批请求在加载后发生变化。")
        if not self.matches(
            current,
            call,
            preview,
            prompt=prompt,
            instructions=instructions,
        ):
            raise ApprovalError("当前操作与已批准预览不匹配。")
        pending_root, consumed_root = self._resolved_roots()
        marker = {
            "version": APPROVAL_RECORD_VERSION,
            "request_id": request.request_id,
            "consumed_at": self._now().isoformat(),
        }
        payload = (
            json.dumps(marker, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self._write_exclusive(
            consumed_root / f"{request.request_id}.json",
            payload,
        )
        try:
            (pending_root / f"{request.request_id}.json").unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ApprovalError(
                "审批已消费，但待审批记录清理失败；请求仍不可重放。"
            ) from error

    def fingerprint(
        self,
        call: ToolCall,
        preview: str,
        *,
        instructions: str,
    ) -> str:
        """Build an in-process de-duplication key without storing argument values."""

        return _text_digest(
            "\0".join(
                (
                    call.name,
                    _arguments_digest(call),
                    _text_digest(preview),
                    _text_digest(instructions),
                )
            )
        )

    def _resolved_roots(self) -> tuple[Path, Path]:
        approval_root = self._resolved_directory(self._approval_root)
        pending_root = self._resolved_directory(approval_root / PENDING_DIRECTORY)
        consumed_root = self._resolved_directory(approval_root / CONSUMED_DIRECTORY)
        return pending_root, consumed_root

    def _prune_expired_pending(self, pending_root: Path) -> None:
        try:
            entries = tuple(pending_root.iterdir())
        except OSError as error:
            raise ApprovalError("无法检查待审批请求目录。") from error
        now = self._now()
        for entry in entries:
            size = self._regular_file_size(entry)
            if size == 0:
                continue
            if size > MAX_APPROVAL_RECORD_BYTES:
                raise ApprovalError("审批请求文件超过大小上限。")
            try:
                request = ApprovalRequest.model_validate_json(
                    self._read_regular_file(entry)
                )
            except (ValidationError, ValueError) as error:
                raise ApprovalError("审批请求格式无效。") from error
            if entry.name != f"{request.request_id}.json":
                raise ApprovalError("审批请求 ID 与文件名不匹配。")
            if now < request.expires_at:
                continue
            try:
                entry.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ApprovalError("过期审批请求清理失败。") from error

    def _resolved_directory(self, directory: Path) -> Path:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            resolved = directory.resolve(strict=True)
        except OSError as error:
            raise ApprovalError("无法创建或访问审批目录。") from error
        if resolved != directory or not resolved.is_dir():
            raise ApprovalError("审批目录必须是工作区内的真实目录。")
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as error:
            raise ApprovalError("审批目录越过工作区边界。") from error
        return resolved

    @staticmethod
    def _record_count(directory: Path) -> int:
        count = 0
        try:
            entries = tuple(directory.iterdir())
        except OSError as error:
            raise ApprovalError("无法检查待审批请求目录。") from error
        for entry in entries:
            size = ApprovalStore._regular_file_size(entry)
            if size:
                count += 1
        return count

    @staticmethod
    def _regular_file_size(path: Path) -> int:
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return 0
        except OSError as error:
            raise ApprovalError("无法检查审批记录。") from error
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            raise ApprovalError("审批记录必须是真实普通文件。")
        return file_stat.st_size

    @staticmethod
    def _write_exclusive(target: Path, payload: bytes) -> None:
        descriptor = -1
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ApprovalError("审批记录目标必须是真实普通文件。")
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = -1
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError as error:
            raise ApprovalError("审批请求已经存在或已被消费。") from error
        except ApprovalError:
            raise
        except OSError as error:
            raise ApprovalError("审批记录写入失败。") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    @staticmethod
    def _read_regular_file(path: Path) -> bytes:
        descriptor = -1
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ApprovalError("审批记录必须是真实普通文件。")
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = -1
                return source.read(MAX_APPROVAL_RECORD_BYTES + 1)
        except ApprovalError:
            raise
        except OSError as error:
            raise ApprovalError("审批请求读取失败。") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalError("审批时间必须包含时区。")
        return value.astimezone(timezone.utc)


class NoninteractiveApprovalBroker:
    """Capture previews or consume one exact approval during an Agent run."""

    def __init__(
        self,
        store: ApprovalStore,
        *,
        mode: ApprovalMode,
        prompt: str,
        instructions: InstructionProvider,
        request_handler: ApprovalRequestHandler,
        approval_id: str | None = None,
    ) -> None:
        if mode == "request" and approval_id is not None:
            raise ApprovalError("生成审批请求时不能同时提供 approval ID。")
        if mode == "approve" and approval_id is None:
            raise ApprovalError("批准模式必须提供 approval ID。")
        self._store = store
        self._mode = mode
        self._prompt = prompt
        self._instructions = instructions
        self._request_handler = request_handler
        self._expected = (
            store.preflight(approval_id, prompt=prompt)
            if approval_id is not None
            else None
        )
        self._requests: dict[str, ApprovalRequest] = {}
        self._consumed_request_id: str | None = None

    @property
    def requests(self) -> tuple[ApprovalRequest, ...]:
        return tuple(self._requests.values())

    @property
    def consumed_request_id(self) -> str | None:
        return self._consumed_request_id

    def __call__(self, call: ToolCall, preview: str) -> bool:
        instructions = self._instructions()
        expected = self._expected
        if (
            self._mode == "approve"
            and expected is not None
            and self._consumed_request_id is None
            and self._store.matches(
                expected,
                call,
                preview,
                prompt=self._prompt,
                instructions=instructions,
            )
        ):
            self._store.consume(
                expected,
                call,
                preview,
                prompt=self._prompt,
                instructions=instructions,
            )
            self._consumed_request_id = expected.approval_id
            return True

        fingerprint = self._store.fingerprint(
            call,
            preview,
            instructions=instructions,
        )
        if fingerprint not in self._requests:
            request = self._store.create(
                call,
                preview,
                prompt=self._prompt,
                instructions=instructions,
            )
            self._requests[fingerprint] = request
            self._request_handler(request)
        return False


def _normalize_approval_id(value: str) -> tuple[str, str]:
    normalized = value.strip().lower()
    parts = normalized.split(".")
    if (
        len(parts) != 2
        or len(parts[0]) != 32
        or len(parts[1]) != 64
        or any(character not in "0123456789abcdef" for character in parts[0] + parts[1])
    ):
        raise ApprovalError("approval ID 格式无效。")
    return parts[0], parts[1]


def _arguments_digest(call: ToolCall) -> str:
    try:
        payload = json.dumps(
            call.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ApprovalError("工具参数无法生成稳定审批摘要。") from error
    return _text_digest(payload)


def _text_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _request_digest(request: ApprovalRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_digest(payload)
