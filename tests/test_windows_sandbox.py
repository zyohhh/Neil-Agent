"""Tests for the candidate host-side Windows Sandbox CLI executor."""

from __future__ import annotations

import base64
import io
import json
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

import pytest

from neil_agent.sandbox import CancellationSignal
from neil_agent.errors import SandboxError
from neil_agent.sandbox_approval import RunCommandApprovalBinding
from neil_agent.sandbox_guest import (
    GUEST_PROTOCOL_VERSION,
    GUEST_RUNNER_SECURITY_ASSURANCE,
    GUEST_RUNNER_VERSION,
    SandboxGuestRequest,
    SandboxGuestResult,
)
from neil_agent.sandbox_snapshot import inspect_prepared_snapshot
from neil_agent.sandbox_lease import HandleLease, acquire_bounded_tree_lease
from neil_agent.windows_sandbox import (
    MAX_CLI_STDERR_BYTES,
    MAX_CLI_STDOUT_BYTES,
    MAX_RESULT_JSON_BYTES,
    WSB_EXPORTER_COMMAND,
    WSB_GUEST_CONTROL,
    WSB_GUEST_EXPORT,
    WSB_GUEST_SNAPSHOT,
    WSB_REQUEST_FILENAME,
    WSB_RESULT_FILENAME,
    WSB_RUNNER_COMMAND,
    WSB_RUNNER_FILENAME,
    BoundedSubprocessCliRunner,
    WsbCliCompleted,
    WsbCliRunner,
    WsbExecutionPlan,
    WsbHostExecutionError,
    WsbHostExecutor,
    WsbRawObservation,
)


class _FakeCliRunner:
    def __init__(
        self,
        plan: WsbExecutionPlan,
        *,
        fail_stage: str | None = None,
        terminate_stage: tuple[str, str] | None = None,
        result_updates: Mapping[str, object] | None = None,
        raw_result: bytes | None = None,
        interfere_before_share: bool = False,
        mutate_control_after_start: bool = False,
        mutate_snapshot_stage: str | None = None,
        write_result_stage: str = "exporter",
        stage_statuses: Mapping[str, str] | None = None,
    ) -> None:
        self.plan = plan
        self.fail_stage = fail_stage
        self.terminate_stage = terminate_stage
        self.result_updates = dict(result_updates or {})
        self.raw_result = raw_result
        self.interfere_before_share = interfere_before_share
        self.mutate_control_after_start = mutate_control_after_start
        self.mutate_snapshot_stage = mutate_snapshot_stage
        self.write_result_stage = write_result_stage
        self.stage_statuses = dict(stage_statuses or {})
        self.calls: list[
            tuple[
                tuple[str, ...],
                float,
                int,
                int,
                dict[str, str],
                CancellationSignal | None,
            ]
        ] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        environment: Mapping[str, str],
        cancel: CancellationSignal | None,
    ) -> WsbCliCompleted:
        self.calls.append(
            (
                argv,
                timeout_seconds,
                stdout_limit,
                stderr_limit,
                dict(environment),
                cancel,
            )
        )
        stage = _stage(argv)
        if self.terminate_stage is not None and stage == self.terminate_stage[0]:
            reason = self.terminate_stage[1]
            return WsbCliCompleted(
                returncode=None,
                timed_out=reason == "timeout",
                cancelled=reason == "cancelled",
                output_limited=reason == "output",
            )
        if stage == self.fail_stage:
            return WsbCliCompleted(returncode=1, stdout=b'{"Id":"ignored"}')

        if stage == "start" and self.mutate_control_after_start:
            request_path = self.plan.control_directory / WSB_REQUEST_FILENAME
            request_path.write_bytes(request_path.read_bytes() + b"\n")
        if stage == "runner" and self.interfere_before_share:
            export = self.plan.temporary_root / "export"
            (export / "external.txt").write_text("race", encoding="utf-8")
        if stage == self.mutate_snapshot_stage:
            snapshot_file = self.plan.snapshot_directory / "tool.exe"
            snapshot_file.write_bytes(snapshot_file.read_bytes() + b"changed")
        if stage == self.write_result_stage:
            export = self.plan.temporary_root / "export"
            raw = self.raw_result
            if raw is None:
                raw = _result_bytes(self.plan, self.result_updates)
            (export / WSB_RESULT_FILENAME).write_bytes(raw)
        if stage == "list_after_stop":
            return WsbCliCompleted(returncode=0, stdout=b"[]")

        default_statuses = {
            "start": "Running",
            "runner": "Succeeded",
            "share": "Shared",
            "exporter": "Succeeded",
            "stop": "Stopped",
        }
        payload: dict[str, object] = {
            "Id": str(self.plan.instance_id),
            "Success": True,
            "Status": self.stage_statuses.get(stage, default_statuses[stage]),
        }
        if stage in {"runner", "exporter"}:
            payload["ExitCode"] = 0
        return WsbCliCompleted(
            returncode=0,
            stdout=_canonical_json(payload),
        )


def _stage(argv: tuple[str, ...]) -> str:
    command = argv[1]
    if command == "list":
        return "list_after_stop"
    if command != "exec":
        return command
    guest_command = argv[argv.index("--command") + 1]
    if guest_command == WSB_RUNNER_COMMAND:
        return "runner"
    if guest_command == WSB_EXPORTER_COMMAND:
        return "exporter"
    raise AssertionError(f"unexpected guest command: {guest_command}")


def _make_plan(
    tmp_path: Path,
) -> tuple[Path, WsbExecutionPlan, SandboxGuestRequest]:
    wsb = tmp_path / "wsb.exe"
    wsb.write_bytes(b"fixed wsb test executable")
    snapshot = tmp_path / "snapshot"
    control = tmp_path / "control"
    temporary_root = tmp_path / "temporary"
    snapshot.mkdir()
    control.mkdir()
    temporary_root.mkdir()
    (snapshot / "tool.exe").write_bytes(b"fixed test executable")
    snapshot_manifest = inspect_prepared_snapshot(snapshot.resolve())
    runner = control / WSB_RUNNER_FILENAME
    runner.write_bytes(b"trusted guest runner")
    runner_sha256 = sha256(runner.read_bytes()).hexdigest()
    instance_id = uuid4()
    run_id = uuid4()
    approval_binding = RunCommandApprovalBinding(
        executable="tool.exe",
        argv=("--version",),
        snapshot_manifest_sha256=snapshot_manifest.digest,
        certification_sha256="c" * 64,
        runner_source_sha256="d" * 64,
        runner_binary_sha256=runner_sha256,
        timeout_ms=30_000,
        max_output_bytes=128_000,
        active_process_limit=4,
        process_memory_bytes=64 * 1024 * 1024,
        job_memory_bytes=128 * 1024 * 1024,
    )
    request = SandboxGuestRequest.create(
        run_id=run_id.hex,
        instance_id=instance_id.hex,
        snapshot_manifest_sha256=snapshot_manifest.digest,
        runner_source_sha256="d" * 64,
        approval_binding_sha256=approval_binding.digest,
        executable="tool.exe",
        argv=("--version",),
        timeout_ms=30_000,
        max_output_bytes=128_000,
        active_process_limit=4,
        process_memory_bytes=64 * 1024 * 1024,
        job_memory_bytes=128 * 1024 * 1024,
    )
    (control / WSB_REQUEST_FILENAME).write_bytes(request.canonical_bytes())
    plan = WsbExecutionPlan(
        instance_id=instance_id,
        run_id=run_id,
        request_hash=request.request_hash,
        snapshot_directory=snapshot.resolve(),
        control_directory=control.resolve(),
        temporary_root=temporary_root.resolve(),
        snapshot_manifest_sha256=snapshot_manifest.digest,
        certification_sha256=approval_binding.certification_sha256,
        runner_source_sha256=request.runner_source_sha256,
        runner_sha256=runner_sha256,
        approval_binding_version=request.approval_binding_version,
        approval_binding_sha256=approval_binding.digest,
        timeout_seconds=30,
    )
    return wsb, plan, request


def _result_bytes(
    plan: WsbExecutionPlan,
    updates: Mapping[str, object] | None = None,
) -> bytes:
    stdout = b"runner ok"
    payload: dict[str, object] = {
        "version": GUEST_PROTOCOL_VERSION,
        "runner_version": GUEST_RUNNER_VERSION,
        "security_assurance": GUEST_RUNNER_SECURITY_ASSURANCE,
        "instance_id": plan.instance_id.hex,
        "run_id": plan.run_id.hex,
        "request_hash": plan.request_hash,
        "status": "exited",
        "exit_code": 0,
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": "",
        "stdout_bytes": len(stdout),
        "stderr_bytes": 0,
        "duration_ms": 125,
        "error_code": None,
        "job_terminated": True,
    }
    payload.update(updates or {})
    payload["result_hash"] = sha256(_canonical_json(payload)).hexdigest()
    try:
        return SandboxGuestResult.model_validate(payload).canonical_bytes()
    except ValueError:
        # Invalid variants are intentionally passed to the host parser.
        return _canonical_json(payload)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def test_executor_uses_safe_two_phase_share_and_always_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan)

    result = WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert result.status == "exited"
    assert result.stdout == b"runner ok"
    assert result.job_terminated is True
    assert [_stage(call[0]) for call in runner.calls] == [
        "start",
        "runner",
        "share",
        "exporter",
        "stop",
        "list_after_stop",
    ]
    for argv, _, stdout_limit, stderr_limit, environment, _ in runner.calls:
        assert argv[0] == str(wsb.resolve())
        if _stage(argv) == "list_after_stop":
            assert argv == (str(wsb.resolve()), "list", "--raw")
        else:
            assert argv[argv.index("--id") + 1] == str(plan.instance_id)
        assert argv[-1] == "--raw"
        assert stdout_limit == MAX_CLI_STDOUT_BYTES
        assert stderr_limit == MAX_CLI_STDERR_BYTES
        assert "DEEPSEEK_API_KEY" not in environment
        assert set(environment) <= {"SYSTEMROOT", "WINDIR", "NO_COLOR"}

    start = runner.calls[0][0]
    config = start[start.index("--config") + 1]
    document = ElementTree.fromstring(config)
    mappings = document.findall("MappedFolders/MappedFolder")
    assert len(mappings) == 2
    assert {
        (
            mapping.findtext("HostFolder"),
            mapping.findtext("SandboxFolder"),
            mapping.findtext("ReadOnly"),
        )
        for mapping in mappings
    } == {
        (str(plan.snapshot_directory), WSB_GUEST_SNAPSHOT, "true"),
        (str(plan.control_directory), WSB_GUEST_CONTROL, "true"),
    }
    assert WSB_GUEST_EXPORT not in config
    assert document.find("LogonCommand") is None

    runner_exec = runner.calls[1][0]
    share = runner.calls[2][0]
    exporter_exec = runner.calls[3][0]
    assert runner_exec[runner_exec.index("--command") + 1] == WSB_RUNNER_COMMAND
    assert exporter_exec[exporter_exec.index("--command") + 1] == (WSB_EXPORTER_COMMAND)
    assert runner_exec[runner_exec.index("--run-as") + 1] == "System"
    assert (
        runner_exec[runner_exec.index("--working-directory") + 1] == WSB_GUEST_CONTROL
    )
    assert "--allow-write" in share
    assert share[share.index("--host-path") + 1] == str(plan.temporary_root / "export")
    assert share[share.index("--sandbox-path") + 1] == WSB_GUEST_EXPORT
    assert all(
        "--allow-write" not in call[0]
        for call in (runner.calls[0], runner.calls[1], runner.calls[3])
    )
    assert "tool.exe" not in " ".join((*runner_exec, *exporter_exec))


@pytest.mark.parametrize("stage", ["start", "runner", "share", "exporter"])
def test_every_host_stage_failure_still_stops(
    tmp_path: Path,
    stage: str,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, fail_stage=stage)

    with pytest.raises(WsbHostExecutionError):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    stages = [_stage(call[0]) for call in runner.calls]
    assert stages[-2:] == ["stop", "list_after_stop"]
    assert stages.count("stop") == 1


@pytest.mark.parametrize("reason", ["timeout", "cancelled", "output"])
def test_runner_termination_is_fail_closed_and_stops(
    tmp_path: Path,
    reason: str,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, terminate_stage=("runner", reason))

    with pytest.raises(WsbHostExecutionError):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls] == [
        "start",
        "runner",
        "stop",
        "list_after_stop",
    ]
    assert runner.calls[-1][-1] is None


def test_cancelled_before_start_does_not_launch_or_need_cleanup(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan)
    cancel = Event()
    cancel.set()

    with pytest.raises(WsbHostExecutionError, match="启动前"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan, cancel=cancel)

    assert runner.calls == []


def test_stop_failure_rejects_an_otherwise_valid_result(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, fail_stage="stop")

    with pytest.raises(WsbHostExecutionError, match="stop"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)


def test_handle_lease_close_failure_rejects_an_otherwise_valid_result(
    tmp_path: Path,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan)
    created = 0

    class CloseFailingLease:
        def __init__(self, delegate: HandleLease, fail: bool) -> None:
            self.delegate = delegate
            self.fail = fail

        def __enter__(self) -> CloseFailingLease:
            self.delegate.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self.delegate.close()
            if self.fail:
                raise SandboxError("injected lease close failure")

        def validate(self) -> None:
            self.delegate.validate()

        def read_file(self, relative_path: str, maximum_bytes: int) -> bytes:
            return self.delegate.read_file(relative_path, maximum_bytes)

        def close(self) -> None:
            self.delegate.close()
            if self.fail:
                raise SandboxError("injected lease close failure")

    def factory(
        root: Path,
        *,
        writable_root: bool = False,
        expected_names: frozenset[str] | None = None,
        max_entries: int,
        max_total_bytes: int,
    ) -> HandleLease:
        nonlocal created
        delegate = acquire_bounded_tree_lease(
            root,
            writable_root=writable_root,
            expected_names=expected_names,
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
        )
        result = CloseFailingLease(delegate, fail=created == 0)
        created += 1
        return result

    with pytest.raises(WsbHostExecutionError, match="lease"):
        WsbHostExecutor(
            wsb,
            cli_runner=runner,
            lease_factory=factory,
        ).execute(plan)

    assert [_stage(call[0]) for call in runner.calls][-2:] == [
        "stop",
        "list_after_stop",
    ]


def test_result_must_be_sealed_before_explicit_stop(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, write_result_stage="stop")

    with pytest.raises(WsbHostExecutionError):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls][-3:] == [
        "exporter",
        "stop",
        "list_after_stop",
    ]


def test_stop_confirmation_polls_until_instance_is_absent(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)

    class DelayedListRunner(_FakeCliRunner):
        list_calls = 0

        def run(self, *args: Any, **kwargs: Any) -> WsbCliCompleted:
            completed = super().run(*args, **kwargs)
            if _stage(args[0]) != "list_after_stop":
                return completed
            self.list_calls += 1
            if self.list_calls == 1:
                return WsbCliCompleted(
                    returncode=0,
                    stdout=_canonical_json(
                        [{"Id": str(plan.instance_id), "Status": "Stopped"}]
                    ),
                )
            return completed

    runner = DelayedListRunner(plan)
    result = WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert result.status == "exited"
    assert runner.list_calls == 2


def test_list_after_stop_requires_an_audited_collection_schema(
    tmp_path: Path,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)

    class AmbiguousListRunner(_FakeCliRunner):
        def run(self, *args: Any, **kwargs: Any) -> WsbCliCompleted:
            completed = super().run(*args, **kwargs)
            if _stage(args[0]) == "list_after_stop":
                return WsbCliCompleted(
                    returncode=0,
                    stdout=_canonical_json({"Success": True}),
                )
            return completed

    with pytest.raises(WsbHostExecutionError, match="stop"):
        WsbHostExecutor(wsb, cli_runner=AmbiguousListRunner(plan)).execute(plan)


def test_list_after_stop_rejects_ambiguous_or_nonterminal_wrapper(
    tmp_path: Path,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)

    class ContradictoryListRunner(_FakeCliRunner):
        def run(self, *args: Any, **kwargs: Any) -> WsbCliCompleted:
            completed = super().run(*args, **kwargs)
            if _stage(args[0]) == "list_after_stop":
                return WsbCliCompleted(
                    returncode=0,
                    stdout=_canonical_json(
                        {
                            "WindowsSandboxEnvironments": [],
                            "Success": True,
                            "Status": "Running",
                            "State": "Failed",
                        }
                    ),
                )
            return completed

    with pytest.raises(WsbHostExecutionError, match="stop"):
        WsbHostExecutor(wsb, cli_runner=ContradictoryListRunner(plan)).execute(plan)


def test_raw_observer_receives_every_bounded_response_before_parsing(
    tmp_path: Path,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan)
    observations: list[WsbRawObservation] = []

    result = WsbHostExecutor(
        wsb,
        cli_runner=runner,
        raw_observer=observations.append,
    ).execute(plan)

    assert result.status == "exited"
    assert [observation.stage for observation in observations] == [
        "start",
        "runner",
        "share",
        "exporter",
        "stop",
        "list_after_stop",
    ]
    assert [observation.argv for observation in observations] == [
        call[0] for call in runner.calls
    ]
    assert {observation.instance_id for observation in observations} == {
        plan.instance_id
    }
    assert {observation.run_id for observation in observations} == {plan.run_id}
    assert {observation.request_hash for observation in observations} == {
        plan.request_hash
    }
    assert all(observation.completed.stdout for observation in observations[:-1])
    assert observations[-1].completed.stdout == b"[]"


@pytest.mark.parametrize("missing_field", ["Success", "Status"])
def test_raw_schema_requires_explicit_success_and_state(
    tmp_path: Path,
    missing_field: str,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)

    class MissingFieldRunner(_FakeCliRunner):
        def run(self, *args: Any, **kwargs: Any) -> WsbCliCompleted:
            completed = super().run(*args, **kwargs)
            if _stage(args[0]) != "start":
                return completed
            payload = {
                "Id": str(plan.instance_id),
                "Success": True,
                "Status": "Running",
            }
            del payload[missing_field]
            return WsbCliCompleted(returncode=0, stdout=_canonical_json(payload))

    runner = MissingFieldRunner(plan)
    with pytest.raises(WsbHostExecutionError, match="schema"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls] == [
        "start",
        "stop",
        "list_after_stop",
    ]


def test_stage_specific_raw_statuses_accept_consistent_states(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(
        plan,
        stage_statuses={
            "start": "Running",
            "runner": "Succeeded",
            "share": "Shared",
            "exporter": "Running",
            "stop": "Stopped",
        },
    )

    result = WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert result.status == "exited"
    assert [_stage(call[0]) for call in runner.calls] == [
        "start",
        "runner",
        "share",
        "exporter",
        "stop",
        "list_after_stop",
    ]


@pytest.mark.parametrize(
    ("stage", "status"),
    [
        ("start", "Stopped"),
        ("runner", "Stopped"),
        ("share", "Stopped"),
        ("exporter", "Stopped"),
        ("stop", "Running"),
    ],
)
def test_explicitly_contradictory_raw_status_fails_closed(
    tmp_path: Path,
    stage: str,
    status: str,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, stage_statuses={stage: status})

    with pytest.raises(WsbHostExecutionError) as caught:
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    if stage == "stop":
        assert isinstance(caught.value.__cause__, WsbHostExecutionError)
        assert "矛盾" in str(caught.value.__cause__)
    else:
        assert "矛盾" in str(caught.value)
    stages = [_stage(call[0]) for call in runner.calls]
    if stage == "stop":
        assert stages[-2:] == ["stop", "list_after_stop"]
    else:
        assert stages[-2:] == ["stop", "list_after_stop"]
    assert stages.count("stop") == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance_id", "0" * 32),
        ("run_id", "1" * 32),
        ("request_hash", "2" * 64),
        ("job_terminated", False),
    ],
)
def test_exported_result_must_match_every_binding(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, result_updates={field: value})

    with pytest.raises(WsbHostExecutionError):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls][-2:] == [
        "stop",
        "list_after_stop",
    ]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{not-json", id="invalid"),
        pytest.param(
            b'{"version":1,"version":1}',
            id="duplicate-key",
        ),
        pytest.param(
            b"x" * (MAX_RESULT_JSON_BYTES + 1),
            id="oversized",
        ),
    ],
)
def test_invalid_or_oversized_guest_json_is_rejected_and_stopped(
    tmp_path: Path,
    raw: bytes,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, raw_result=raw)

    with pytest.raises(WsbHostExecutionError):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls][-2:] == [
        "stop",
        "list_after_stop",
    ]


def test_writable_export_is_not_shared_if_runner_did_not_leave_it_empty(
    tmp_path: Path,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, interfere_before_share=True)

    with pytest.raises(WsbHostExecutionError, match="必须为空"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls] == [
        "start",
        "runner",
        "stop",
        "list_after_stop",
    ]


def test_paths_and_control_bundle_are_validated_before_start(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    (plan.temporary_root / "unexpected").write_text("not dedicated", encoding="utf-8")
    runner = _FakeCliRunner(plan)

    with pytest.raises(WsbHostExecutionError, match="temporary root"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert runner.calls == []


def test_changed_runner_is_rejected_before_start(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    (plan.control_directory / WSB_RUNNER_FILENAME).write_bytes(b"replaced")
    runner = _FakeCliRunner(plan)

    with pytest.raises(WsbHostExecutionError, match="SHA-256"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert runner.calls == []


def test_execution_plan_rejects_boolean_approval_versions(tmp_path: Path) -> None:
    _, plan, _ = _make_plan(tmp_path)

    with pytest.raises(ValueError, match="approval binding version"):
        replace(plan, approval_binding_version=True)  # type: ignore[arg-type]


def test_approval_digest_is_recomputed_from_actual_guest_semantics(
    tmp_path: Path,
) -> None:
    wsb, plan, request = _make_plan(tmp_path)
    forged = SandboxGuestRequest.create(
        run_id=request.run_id,
        instance_id=request.instance_id,
        snapshot_manifest_sha256=request.snapshot_manifest_sha256,
        runner_source_sha256=request.runner_source_sha256,
        approval_binding_sha256=request.approval_binding_sha256,
        executable=request.executable,
        argv=("--different-command",),
        cwd=request.cwd,
        environment=request.environment,
        timeout_ms=request.timeout_ms,
        max_output_bytes=request.max_output_bytes,
        active_process_limit=request.active_process_limit,
        process_memory_bytes=request.process_memory_bytes,
        job_memory_bytes=request.job_memory_bytes,
    )
    (plan.control_directory / WSB_REQUEST_FILENAME).write_bytes(
        forged.canonical_bytes()
    )
    forged_plan = replace(plan, request_hash=forged.request_hash)
    runner = _FakeCliRunner(forged_plan)

    with pytest.raises(WsbHostExecutionError, match="binding"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(forged_plan)

    assert runner.calls == []


def test_changed_snapshot_manifest_is_rejected_before_start(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    (plan.snapshot_directory / "tool.exe").write_bytes(b"replaced")
    runner = _FakeCliRunner(plan)

    with pytest.raises(WsbHostExecutionError):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert runner.calls == []


@pytest.mark.parametrize(
    ("stage", "expected_stages"),
    [
        ("runner", ["start", "runner", "stop", "list_after_stop"]),
        (
            "stop",
            [
                "start",
                "runner",
                "share",
                "exporter",
                "stop",
                "list_after_stop",
            ],
        ),
    ],
)
def test_snapshot_mutation_during_execution_is_rejected(
    tmp_path: Path,
    stage: str,
    expected_stages: list[str],
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, mutate_snapshot_stage=stage)

    with pytest.raises(WsbHostExecutionError):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls] == expected_stages


def test_noncanonical_or_changed_request_is_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    runner = _FakeCliRunner(plan, mutate_control_after_start=True)

    with pytest.raises(WsbHostExecutionError):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls] == [
        "start",
        "stop",
        "list_after_stop",
    ]


def test_snapshot_symlink_is_rejected_before_start(tmp_path: Path) -> None:
    wsb, plan, _ = _make_plan(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = plan.snapshot_directory / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks is unavailable")
    runner = _FakeCliRunner(plan)

    with pytest.raises(WsbHostExecutionError, match="重解析点"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert runner.calls == []


def test_cli_response_must_be_bounded_json_bound_to_explicit_instance(
    tmp_path: Path,
) -> None:
    wsb, plan, _ = _make_plan(tmp_path)

    class WrongInstanceRunner(_FakeCliRunner):
        def run(self, *args: Any, **kwargs: Any) -> WsbCliCompleted:
            completed = super().run(*args, **kwargs)
            if _stage(args[0]) == "start":
                return WsbCliCompleted(
                    returncode=0,
                    stdout=_canonical_json(
                        {
                            "Id": str(uuid4()),
                            "Success": True,
                            "Status": "Running",
                        }
                    ),
                )
            return completed

    runner = WrongInstanceRunner(plan)
    with pytest.raises(WsbHostExecutionError, match="实例绑定"):
        WsbHostExecutor(wsb, cli_runner=runner).execute(plan)

    assert [_stage(call[0]) for call in runner.calls] == [
        "start",
        "stop",
        "list_after_stop",
    ]


def test_gui_executable_is_never_accepted_as_the_wsb_cli(tmp_path: Path) -> None:
    gui = tmp_path / "WindowsSandbox.exe"
    gui.write_bytes(b"not the CLI")

    with pytest.raises(ValueError, match="CLI"):
        WsbHostExecutor(gui)


class _ImmediateProcess:
    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.stdout = io.BytesIO(b'{"Id":"00000000-0000-0000-0000-000000000000"}')
        self.stderr = io.BytesIO()
        self.returncode = 0

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def terminate(self) -> None:
        self.returncode = -1

    def kill(self) -> None:
        self.returncode = -9


def test_default_process_boundary_never_uses_a_shell_or_secret_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[_ImmediateProcess] = []

    def make_process(argv: list[str], **kwargs: Any) -> _ImmediateProcess:
        process = _ImmediateProcess(argv, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    runner: WsbCliRunner = BoundedSubprocessCliRunner(
        popen_factory=make_process,  # type: ignore[arg-type]
    )
    result = runner.run(
        (r"C:\Windows\System32\wsb.exe", "list", "--raw"),
        timeout_seconds=1,
        stdout_limit=MAX_CLI_STDOUT_BYTES,
        stderr_limit=MAX_CLI_STDERR_BYTES,
        environment={"NO_COLOR": "1"},
        cancel=Event(),
    )

    assert result.returncode == 0
    assert len(calls) == 1
    assert calls[0].argv == [r"C:\Windows\System32\wsb.exe", "list", "--raw"]
    assert calls[0].kwargs["shell"] is False
    assert calls[0].kwargs["stdin"] == subprocess.DEVNULL
    assert calls[0].kwargs["env"] == {"NO_COLOR": "1"}
    assert "DEEPSEEK_API_KEY" not in calls[0].kwargs["env"]
