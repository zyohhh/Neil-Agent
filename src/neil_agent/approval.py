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
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import ApprovalError
from .schemas import ToolCall

APPROVAL_DIRECTORY = Path(".neil-agent") / "approvals"
PENDING_DIRECTORY = "pending"
CONSUMED_DIRECTORY = "consumed"
APPROVAL_RECORD_VERSION: Literal[2] = 2
GENERIC_APPROVAL_BINDING_KIND: Literal["generic-tool"] = "generic-tool"
SANDBOX_APPROVAL_BINDING_KIND: Literal["sandbox-run-command"] = "sandbox-run-command"
GUEST_EXPORT_IMPORT_BINDING_KIND: Literal["guest-export-import"] = "guest-export-import"
GENERIC_APPROVAL_BINDING_VERSION: Literal[1] = 1
APPROVAL_TTL = timedelta(minutes=15)
MAX_APPROVAL_PREVIEW_CHARS = 30_000
MAX_APPROVAL_RECORD_BYTES = 64_000
MAX_PENDING_APPROVALS = 100
MAX_CONSUMED_APPROVALS = 1_000
CONSUMED_APPROVAL_RETENTION = timedelta(days=1)
ApprovalMode = Literal["request", "approve"]
ApprovalBindingKind = Literal[
    "generic-tool",
    "sandbox-run-command",
    "guest-export-import",
]
ApprovalRequestHandler = Callable[["ApprovalRequest"], None]
InstructionProvider = Callable[[], str]


class ApprovalBinding(BaseModel):
    """Stable machine-readable identity for one approval preview."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: ApprovalBindingKind
    version: StrictInt = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


ApprovalBindingResolver = Callable[[ToolCall, str], ApprovalBinding | None]


class ApprovalRequest(BaseModel):
    """Persisted metadata for one exact operation preview."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[2] = APPROVAL_RECORD_VERSION
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: datetime
    expires_at: datetime
    workspace: str = Field(min_length=1, max_length=4_096)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instructions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_name: str = Field(min_length=1, max_length=128)
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_kind: ApprovalBindingKind
    binding_version: StrictInt = Field(ge=1)
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
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


class ApprovalClaimMarker(BaseModel):
    """Bounded terminal marker retained past the pending request lifetime."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    version: Literal[2] = APPROVAL_RECORD_VERSION
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claimed_at: datetime

    @field_validator("claimed_at")
    @classmethod
    def claimed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval claim timestamp must include a timezone")
        return value.astimezone(timezone.utc)


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
        binding: ApprovalBinding | None = None,
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
        resolved_binding = _resolve_binding(call, preview, binding)
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
                binding_kind=resolved_binding.kind,
                binding_version=resolved_binding.version,
                binding_sha256=resolved_binding.sha256,
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

    def claim(
        self,
        approval_id: str,
        *,
        prompt: str,
    ) -> ApprovalRequest:
        """Irreversibly claim one approval before an approve-mode model call."""

        normalized_id, expected_digest = _normalize_approval_id(approval_id)
        pending_root, consumed_root = self._resolved_roots()
        self._prune_expired_consumed(consumed_root)
        if self._record_count(consumed_root) >= MAX_CONSUMED_APPROVALS:
            raise ApprovalError("已消费审批记录达到安全上限，请等待旧记录过期后重试。")
        marker = {
            "version": APPROVAL_RECORD_VERSION,
            "request_id": normalized_id,
            "request_sha256": expected_digest,
            "claimed_at": self._now().isoformat(),
        }
        payload = (
            json.dumps(
                marker,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        consumed_path = consumed_root / f"{normalized_id}.json"
        if self._regular_file_size(consumed_path):
            raise ApprovalError("审批请求已经使用，不能重放。")
        try:
            self._write_exclusive(consumed_path, payload)
        except ApprovalError as error:
            if self._regular_file_size(consumed_path):
                raise ApprovalError("审批请求已经使用，不能重放。") from error
            raise
        request: ApprovalRequest | None = None
        primary_error: BaseException | None = None
        try:
            request = self._load_pending_record(
                normalized_id,
                expected_digest,
                pending_root,
            )
        except BaseException as error:
            primary_error = error
        try:
            (pending_root / f"{normalized_id}.json").unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ApprovalError(
                "审批已认领，但待审批记录清理失败；请求仍不可重放。"
            ) from error
        if primary_error is not None:
            raise primary_error
        if request is None:  # pragma: no cover - defensive type/runtime guard.
            raise ApprovalError("审批已认领，但审批记录无法读取。")
        if request.workspace != str(self._workspace_root):
            raise ApprovalError("审批请求不属于当前工作区，且已永久失效。")
        if request.prompt_sha256 != _text_digest(prompt):
            raise ApprovalError("审批请求与当前 prompt 不匹配，且已永久失效。")
        return request

    def load(self, approval_id: str) -> ApprovalRequest:
        """Load one pending request after rejecting replay and unsafe paths."""

        normalized_id, expected_digest = _normalize_approval_id(approval_id)
        pending_root, consumed_root = self._resolved_roots()
        consumed_path = consumed_root / f"{normalized_id}.json"
        if self._regular_file_size(consumed_path):
            raise ApprovalError("审批请求已经使用，不能重放。")
        return self._load_pending_record(
            normalized_id,
            expected_digest,
            pending_root,
        )

    def _load_pending_record(
        self,
        normalized_id: str,
        expected_digest: str,
        pending_root: Path,
    ) -> ApprovalRequest:
        pending_path = pending_root / f"{normalized_id}.json"
        size = self._regular_file_size(pending_path)
        if size == 0:
            raise ApprovalError("审批请求不存在。")
        if size > MAX_APPROVAL_RECORD_BYTES:
            raise ApprovalError("审批请求文件超过大小上限。")
        try:
            payload = self._read_regular_file(pending_path)
            version = _record_version(payload)
            if version == 1:
                raise ApprovalError("旧版审批请求已经失效，请重新生成预览。")
            if version != APPROVAL_RECORD_VERSION:
                raise ApprovalError("审批请求使用了不受支持的记录版本。")
            request = ApprovalRequest.model_validate_json(payload)
        except ApprovalError:
            raise
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
        binding: ApprovalBinding | None = None,
    ) -> bool:
        """Return whether the current operation is exactly the approved preview."""

        resolved_binding = _resolve_binding(call, preview, binding)
        return (
            request.workspace == str(self._workspace_root)
            and request.prompt_sha256 == _text_digest(prompt)
            and request.instructions_sha256 == _text_digest(instructions)
            and request.tool_name == call.name
            and request.arguments_sha256 == _arguments_digest(call)
            and request.preview_sha256 == _text_digest(preview)
            and request.preview == preview
            and request.binding_kind == resolved_binding.kind
            and request.binding_version == resolved_binding.version
            and request.binding_sha256 == resolved_binding.sha256
        )

    def consume(
        self,
        request: ApprovalRequest,
        call: ToolCall,
        preview: str,
        *,
        prompt: str,
        instructions: str,
        binding: ApprovalBinding | None = None,
    ) -> None:
        """Atomically burn one matching request before the mutation executes."""

        current = self.claim(request.approval_id, prompt=prompt)
        if current != request:
            raise ApprovalError("审批请求在加载后发生变化。")
        if not self.matches(
            current,
            call,
            preview,
            prompt=prompt,
            instructions=instructions,
            binding=binding,
        ):
            raise ApprovalError("当前操作与已批准预览不匹配，审批已永久失效。")

    def fingerprint(
        self,
        call: ToolCall,
        preview: str,
        *,
        instructions: str,
        binding: ApprovalBinding | None = None,
    ) -> str:
        """Build an in-process de-duplication key without storing argument values."""

        resolved_binding = _resolve_binding(call, preview, binding)
        return _text_digest(
            "\0".join(
                (
                    call.name,
                    _arguments_digest(call),
                    _text_digest(preview),
                    _text_digest(instructions),
                    resolved_binding.kind,
                    str(resolved_binding.version),
                    resolved_binding.sha256,
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
                payload = self._read_regular_file(entry)
                version = _record_version(payload)
                if version == 1:
                    self._unlink_pending_record(entry, "旧版审批请求清理失败。")
                    continue
                if version != APPROVAL_RECORD_VERSION:
                    raise ApprovalError("审批请求使用了不受支持的记录版本。")
                request = ApprovalRequest.model_validate_json(payload)
            except ApprovalError:
                raise
            except (ValidationError, ValueError) as error:
                raise ApprovalError("审批请求格式无效。") from error
            if entry.name != f"{request.request_id}.json":
                raise ApprovalError("审批请求 ID 与文件名不匹配。")
            if now < request.expires_at:
                continue
            self._unlink_pending_record(entry, "过期审批请求清理失败。")

    def _prune_expired_consumed(self, consumed_root: Path) -> None:
        try:
            entries = tuple(consumed_root.iterdir())
        except OSError as error:
            raise ApprovalError("无法检查已消费审批请求目录。") from error
        now = self._now()
        for entry in entries:
            size = self._regular_file_size(entry)
            if size == 0:
                continue
            if size > MAX_APPROVAL_RECORD_BYTES:
                raise ApprovalError("已消费审批记录超过大小上限。")
            try:
                payload = self._read_regular_file(entry)
                if _record_version(payload) != APPROVAL_RECORD_VERSION:
                    raise ApprovalError("已消费审批记录版本无效。")
                marker = ApprovalClaimMarker.model_validate_json(payload)
            except ApprovalError:
                raise
            except (ValidationError, ValueError) as error:
                raise ApprovalError("已消费审批记录格式无效。") from error
            if entry.name != f"{marker.request_id}.json":
                raise ApprovalError("已消费审批记录 ID 与文件名不匹配。")
            if now - marker.claimed_at < CONSUMED_APPROVAL_RETENTION:
                continue
            self._unlink_pending_record(entry, "过期已消费审批记录清理失败。")

    @staticmethod
    def _unlink_pending_record(entry: Path, message: str) -> None:
        try:
            entry.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            raise ApprovalError(message) from error

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
        binding_resolver: ApprovalBindingResolver | None = None,
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
        self._binding_resolver = binding_resolver
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
        binding = (
            self._binding_resolver(call, preview)
            if self._binding_resolver is not None
            else None
        )
        expected = self._expected
        if (
            self._mode == "approve"
            and expected is not None
            and self._consumed_request_id is None
        ):
            if self._store.matches(
                expected,
                call,
                preview,
                prompt=self._prompt,
                instructions=instructions,
                binding=binding,
            ):
                self._store.consume(
                    expected,
                    call,
                    preview,
                    prompt=self._prompt,
                    instructions=instructions,
                    binding=binding,
                )
                self._consumed_request_id = expected.approval_id
                return True
            fingerprint = self._store.fingerprint(
                call,
                preview,
                instructions=instructions,
                binding=binding,
            )
            if fingerprint not in self._requests:
                request = self._store.create(
                    call,
                    preview,
                    prompt=self._prompt,
                    instructions=instructions,
                    binding=binding,
                )
                self._requests[fingerprint] = request
                self._request_handler(request)
            return False

        if self._mode == "approve":
            return False

        fingerprint = self._store.fingerprint(
            call,
            preview,
            instructions=instructions,
            binding=binding,
        )
        if fingerprint not in self._requests:
            request = self._store.create(
                call,
                preview,
                prompt=self._prompt,
                instructions=instructions,
                binding=binding,
            )
            self._requests[fingerprint] = request
            self._request_handler(request)
        return False


def approval_context_digest(value: str) -> str:
    """Return a stable SHA-256 digest for approval-bound prompt or instruction text."""

    return _text_digest(value)


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


def _resolve_binding(
    call: ToolCall,
    preview: str,
    binding: ApprovalBinding | None,
) -> ApprovalBinding:
    if binding is not None:
        if not isinstance(binding, ApprovalBinding):
            raise ApprovalError("审批绑定格式无效。")
        return binding
    payload = {
        "arguments_sha256": _arguments_digest(call),
        "preview_sha256": _text_digest(preview),
        "tool_name": call.name,
        "version": GENERIC_APPROVAL_BINDING_VERSION,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = sha256(b"neil-agent:generic-tool-approval:v1\0" + canonical).hexdigest()
    return ApprovalBinding(
        kind=GENERIC_APPROVAL_BINDING_KIND,
        version=GENERIC_APPROVAL_BINDING_VERSION,
        sha256=digest,
    )


def _record_version(payload: bytes) -> int:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ApprovalError("审批请求格式无效。") from error
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise ApprovalError("审批请求缺少有效的记录版本。")
    return value["version"]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate approval record key")
        result[name] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _request_digest(request: ApprovalRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_digest(payload)
