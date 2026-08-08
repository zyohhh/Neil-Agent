"""Tests for strict, non-enabling Windows Sandbox evidence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

import pytest

from neil_agent.sandbox_evidence import (
    CERTIFIED_SECURITY_ASSURANCE,
    MINIMUM_EVIDENCE_REPEATS,
    REQUIRED_CLI_SCHEMA_STAGES,
    REQUIRED_WINDOWS_SANDBOX_TESTS,
    CliExecutionIdentity,
    CliSchemaEntry,
    CliSchemaField,
    CliSchemaReport,
    EvidenceSubject,
    IndependentSecurityReview,
    PlatformFingerprint,
    RawObservationRecorder,
    ReviewTrustPins,
    SandboxCertification,
    SandboxEvidenceAggregate,
    SandboxEvidenceError,
    SandboxEvidenceRun,
    TestOutcomeSummary as OutcomeSummary,
    build_cli_schema_report,
    collect_evidence_run,
    issue_certification,
    main,
    required_test_manifest,
    verify_certification,
    verify_evidence_runs,
)
from neil_agent.windows_sandbox import (
    WSB_EXPORTER_COMMAND,
    WSB_GUEST_CONTROL,
    WSB_GUEST_EXPORT,
    WSB_RUNNER_COMMAND,
)

_START = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)


def _digest(character: str) -> str:
    return character * 64


def _platform(*, ubr: int = 1) -> PlatformFingerprint:
    return PlatformFingerprint(
        os_product_name="Windows 11 Pro",
        edition_id="Professional",
        display_version="24H2",
        build_number=26100,
        ubr=ubr,
        architecture="AMD64",
        sandbox_feature_state="Enabled",
        wsb_executable_name="wsb.exe",
        wsb_file_version="10.0.26100.1",
        wsb_product_version="10.0.26100.1",
        wsb_sha256=_digest("1"),
        authenticode_status="Valid",
        signer_thumbprint="2" * 40,
    )


def _subject(
    *,
    assurance: str = "candidate-restricted-low-integrity-job-not-certified",
    runner_binary_sha256: str | None = None,
) -> EvidenceSubject:
    return EvidenceSubject(
        git_commit_sha="a" * 40,
        source_manifest_sha256=_digest("2"),
        wheel_sha256=_digest("3"),
        uv_lock_sha256=_digest("4"),
        workflow_sha256=_digest("5"),
        runner_source_sha256=_digest("6"),
        runner_binary_sha256=runner_binary_sha256 or _digest("7"),
        compiler_sha256=_digest("8"),
        framework_reference_sha256=_digest("9"),
        probe_source_sha256=_digest("a"),
        probe_binary_sha256=_digest("b"),
        required_test_manifest_sha256=required_test_manifest().manifest_sha256,
        backend_policy_version=1,
        guest_protocol_version=1,
        runner_version=1,
        security_assurance=assurance,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("edition_id", "Core"),
        ("build_number", 26099),
    ],
)
def test_platform_fingerprint_requires_windows_11_pro_enterprise_24h2(
    field: str,
    value: object,
) -> None:
    payload = _platform().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValueError, match="Pro or Enterprise|24H2"):
        PlatformFingerprint.model_validate(payload)


def _identity(index: int) -> CliExecutionIdentity:
    return CliExecutionIdentity(
        instance_id=f"00000000-0000-0000-0000-{index + 1:012x}",
        run_id=f"10000000-0000-0000-0000-{index + 1:012x}",
        request_hash=_digest(format((index + 1) % 16, "x")),
    )


def _schema(
    index: int = 0,
    *,
    repeat_id: str | None = None,
    nonce: str | None = None,
    raw_character: str = "c",
    start_status: str = "Running",
    identity: CliExecutionIdentity | None = None,
    transcript_sha256: str | None = None,
) -> CliSchemaReport:
    status_by_stage = {
        "start": (start_status,),
        "runner": ("Succeeded",),
        "share": ("Shared",),
        "exporter": ("Succeeded",),
        "stop": ("Stopped",),
        "list_after_stop": (),
    }
    entries: list[CliSchemaEntry] = []
    for entry_index, stage in enumerate(REQUIRED_CLI_SCHEMA_STAGES):
        if stage == "list_after_stop":
            fields: tuple[CliSchemaField, ...] = ()
            root_type = "array"
        else:
            field_definitions = (
                (
                    ("ExitCode", "integer"),
                    ("Id", "string"),
                    ("Status", "string"),
                    ("Success", "boolean"),
                )
                if stage in {"runner", "exporter"}
                else (
                    ("Id", "string"),
                    ("Status", "string"),
                    ("Success", "boolean"),
                )
            )
            fields = tuple(
                CliSchemaField(
                    name=name,
                    value_type=value_type,
                )
                for name, value_type in field_definitions
            )
            root_type = "object"
        raw_digest_character = format(
            (int(raw_character, 16) + entry_index) % 16,
            "x",
        )
        entries.append(
            CliSchemaEntry(
                stage=stage,
                root_type=root_type,
                fields=fields,
                observed_statuses=status_by_stage[stage],
                normalized_shape_sha256=_digest(format((entry_index + 1) % 16, "x")),
                raw_response_sha256s=(_digest(raw_digest_character),),
            )
        )
    return CliSchemaReport(
        repeat_id=repeat_id or f"repeat-{index}",
        execution_nonce=nonce or f"{index:032x}",
        execution_identities=(identity or _identity(index),),
        transcript_sha256=transcript_sha256 or _digest(format((index + 8) % 16, "x")),
        entries=tuple(entries),
    )


def _summary(
    *,
    outcome: str | None = None,
) -> OutcomeSummary:
    counts = {
        "passed": len(REQUIRED_WINDOWS_SANDBOX_TESTS),
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    if outcome is not None:
        counts["passed"] -= 1
        counts[outcome] = 1
    return OutcomeSummary(
        nodeids=REQUIRED_WINDOWS_SANDBOX_TESTS,
        junit_sha256=_digest("d"),
        **counts,
    )


def _run(
    index: int,
    *,
    platform: PlatformFingerprint | None = None,
    subject: EvidenceSubject | None = None,
    schema: CliSchemaReport | None = None,
    summary: OutcomeSummary | None = None,
    repeat_id: str | None = None,
    nonce: str | None = None,
    workflow_run_id: int = 100,
) -> SandboxEvidenceRun:
    bound_repeat_id = repeat_id or f"repeat-{index}"
    bound_nonce = nonce or f"{index:032x}"
    report = schema or _schema(
        index,
        repeat_id=bound_repeat_id,
        nonce=bound_nonce,
        raw_character=format(10 + index, "x"),
    )
    return SandboxEvidenceRun.create(
        repeat_id=bound_repeat_id,
        execution_nonce=bound_nonce,
        producer_id="github-actions:owner/repository",
        workflow_run_id=workflow_run_id,
        workflow_attempt=1,
        pytest_exit_code=0,
        started_at=_START + timedelta(minutes=index),
        finished_at=_START + timedelta(minutes=index, seconds=30),
        platform=platform or _platform(),
        subject=subject or _subject(),
        cli_schema=report,
        tests=summary or _summary(),
    )


def _aggregate(
    *,
    assurance: str = "candidate-restricted-low-integrity-job-not-certified",
) -> SandboxEvidenceAggregate:
    subject = _subject(assurance=assurance)
    return verify_evidence_runs(
        tuple(_run(index, subject=subject) for index in range(3))
    )


def _review(
    aggregate: SandboxEvidenceAggregate,
    *,
    reviewer_id: str = "security-reviewer",
    disposition: str = "approved",
    open_findings: int = 0,
    reviewed_at: datetime | None = None,
) -> IndependentSecurityReview:
    assert disposition in {"approved", "rejected"}
    return IndependentSecurityReview.create(
        review_id="e" * 32,
        reviewer_id=reviewer_id,
        aggregate_sha256=aggregate.aggregate_sha256,
        disposition=disposition,  # type: ignore[arg-type]
        open_findings=open_findings,
        closed_finding_ids=("SEC-1", "SEC-2"),
        reviewed_at=reviewed_at or (_START + timedelta(hours=1)),
    )


def _write_json(path: Path, value: object, *, indent: int | None = None) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )


def _write_junit(
    path: Path,
    *,
    outcome_nodeid: str | None = None,
    outcome_xml: str = "",
) -> None:
    cases = []
    for nodeid in REQUIRED_WINDOWS_SANDBOX_TESTS:
        module, name = nodeid.split("::", 1)
        classname = module.removesuffix(".py").replace("/", ".")
        outcome = outcome_xml if nodeid == outcome_nodeid else ""
        cases.append(
            f"<testcase classname={quoteattr(classname)} "
            f"name={quoteattr(name)}>{outcome}</testcase>"
        )
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite tests="{len(cases)}" '
        f'failures="{int("<failure" in outcome_xml)}" '
        f'errors="{int("<error" in outcome_xml)}" '
        f'skipped="{int("<skipped" in outcome_xml)}">'
        f"{''.join(cases)}</testsuite></testsuites>",
        encoding="utf-8",
    )


def _collect_junit(junit_path: Path) -> OutcomeSummary:
    return collect_evidence_run(
        repeat_id="repeat-0",
        execution_nonce="0" * 32,
        producer_id="github-actions:owner/repository",
        workflow_run_id=100,
        workflow_attempt=1,
        pytest_exit_code=0,
        started_at=_START,
        finished_at=_START + timedelta(seconds=30),
        platform=_platform(),
        subject=_subject(),
        cli_schema=_schema(0),
        junit_path=junit_path,
    ).tests


def test_required_manifest_is_fixed_sorted_and_self_hashed() -> None:
    manifest = required_test_manifest()

    assert manifest.nodeids == REQUIRED_WINDOWS_SANDBOX_TESTS
    assert len(manifest.nodeids) == 14
    assert manifest.canonical_bytes() == required_test_manifest().canonical_bytes()


def _stage_argv(stage: str, instance_id: str) -> tuple[str, ...]:
    executable = r"C:\Windows\System32\wsb.exe"
    if stage == "start":
        return (
            executable,
            "start",
            "--id",
            instance_id,
            "--config",
            "<Configuration></Configuration>",
            "--raw",
        )
    if stage in {"runner", "exporter"}:
        return (
            executable,
            "exec",
            "--id",
            instance_id,
            "--command",
            WSB_RUNNER_COMMAND if stage == "runner" else WSB_EXPORTER_COMMAND,
            "--run-as",
            "System",
            "--working-directory",
            WSB_GUEST_CONTROL,
            "--raw",
        )
    if stage == "share":
        return (
            executable,
            "share",
            "--id",
            instance_id,
            "--host-path",
            r"C:\evidence\output",
            "--sandbox-path",
            WSB_GUEST_EXPORT,
            "--allow-write",
            "--raw",
        )
    if stage == "stop":
        return (executable, "stop", "--id", instance_id, "--raw")
    return (executable, "list", "--raw")


def _record_observation(
    recorder: RawObservationRecorder,
    *,
    stage: str,
    raw: bytes,
    identity: CliExecutionIdentity,
    returncode: int | None = 0,
    timed_out: bool = False,
    cancelled: bool = False,
    output_limited: bool = False,
) -> None:
    recorder.record(
        stage,
        raw,
        argv=_stage_argv(stage, identity.instance_id),
        instance_id=identity.instance_id,
        run_id=identity.run_id,
        request_hash=identity.request_hash,
        returncode=returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        output_limited=output_limited,
    )


def _raw_response(stage: str, identity: CliExecutionIdentity) -> bytes:
    status = {
        "start": "Running",
        "runner": "Succeeded",
        "share": "Shared",
        "exporter": "Succeeded",
        "stop": "Stopped",
    }[stage]
    exit_code = '"ExitCode":0,' if stage in {"runner", "exporter"} else ""
    return (
        f'{{{exit_code}"Id":"{identity.instance_id}",'
        f'"Status":"{status}","Success":true}}'
    ).encode()


def _record_complete_raw_cli_run(
    path: Path,
    *,
    index: int = 0,
    list_responses: tuple[bytes, ...] = (b"[]",),
) -> None:
    identity = _identity(index)
    with RawObservationRecorder(
        path,
        repeat_id=f"repeat-{index}",
        execution_nonce=f"{index:032x}",
    ) as recorder:
        for stage in REQUIRED_CLI_SCHEMA_STAGES[:-1]:
            _record_observation(
                recorder,
                stage=stage,
                raw=_raw_response(stage, identity),
                identity=identity,
            )
        for response in list_responses:
            _record_observation(
                recorder,
                stage="list_after_stop",
                raw=response,
                identity=identity,
            )


def test_raw_recorder_derives_schema_from_exact_bounded_jsonl(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "cli-raw.jsonl"
    _record_complete_raw_cli_run(raw_path)

    report = build_cli_schema_report(raw_path)

    assert tuple(entry.stage for entry in report.entries) == (
        REQUIRED_CLI_SCHEMA_STAGES
    )
    assert report.entries[0].observed_statuses == ("Running",)
    assert report.entries[-1].root_type == "array"
    assert report.entries[-1].raw_response_sha256s
    assert report.execution_identities == (_identity(0),)
    assert report.repeat_id == "repeat-0"
    assert report.execution_nonce == "0" * 32
    assert report.transcript_sha256 == sha256(raw_path.read_bytes()).hexdigest()


def test_list_poll_content_changes_do_not_change_the_schema_profile(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    first_identity = _identity(0)
    first_present = json.dumps(
        [{"Id": first_identity.instance_id, "State": "Running"}],
        separators=(",", ":"),
    ).encode()
    _record_complete_raw_cli_run(
        first_path,
        list_responses=(first_present, b"[]"),
    )
    _record_complete_raw_cli_run(second_path, index=1)

    first = build_cli_schema_report(first_path)
    second = build_cli_schema_report(second_path)

    assert first.profile_payload() == second.profile_payload()
    assert first.entries[-1].normalized_shape_sha256 == (
        second.entries[-1].normalized_shape_sha256
    )
    assert len(first.entries[-1].raw_response_sha256s) == 2


def test_exact_executor_list_object_schema_is_accepted(tmp_path: Path) -> None:
    raw_path = tmp_path / "cli-raw.jsonl"
    _record_complete_raw_cli_run(
        raw_path,
        list_responses=(
            b'{"WindowsSandboxEnvironments":[],"Success":true,"Status":"Stopped"}',
        ),
    )

    report = build_cli_schema_report(raw_path)

    assert report.entries[-1].root_type == "object"
    assert tuple(field.name for field in report.entries[-1].fields) == (
        "Status",
        "Success",
        "WindowsSandboxEnvironments",
    )


def test_schema_cli_builds_report_but_missing_list_after_stop_fails(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "cli-raw.jsonl"
    identity = _identity(0)
    with RawObservationRecorder(
        raw_path,
        repeat_id="repeat-0",
        execution_nonce="0" * 32,
    ) as recorder:
        for stage in REQUIRED_CLI_SCHEMA_STAGES[:-1]:
            _record_observation(
                recorder,
                stage=stage,
                raw=_raw_response(stage, identity),
                identity=identity,
            )
    schema_path = tmp_path / "schema.json"

    assert (
        main(
            (
                "schema",
                "--raw-jsonl",
                str(raw_path),
                "--output",
                str(schema_path),
            )
        )
        == 2
    )
    assert not schema_path.exists()


def test_schema_cli_writes_canonical_report_from_recorder_output(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "cli-raw.jsonl"
    schema_path = tmp_path / "schema.json"
    _record_complete_raw_cli_run(raw_path)

    assert (
        main(
            (
                "schema",
                "--raw-jsonl",
                str(raw_path),
                "--output",
                str(schema_path),
            )
        )
        == 0
    )
    parsed = CliSchemaReport.model_validate_json(schema_path.read_bytes())
    assert (
        schema_path.read_bytes()
        == json.dumps(
            parsed.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def test_raw_recorder_rejects_invalid_or_duplicate_key_json(tmp_path: Path) -> None:
    raw_path = tmp_path / "cli-raw.jsonl"
    identity = _identity(0)
    with RawObservationRecorder(
        raw_path,
        repeat_id="repeat-0",
        execution_nonce="0" * 32,
    ) as recorder:
        with pytest.raises(SandboxEvidenceError):
            _record_observation(
                recorder,
                stage="start",
                raw=b'{"Id":"one","Id":"two"}',
                identity=identity,
            )

    assert raw_path.read_bytes() == b""


def test_transcript_records_empty_cancelled_runner_and_validates_state_machine(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "cli-raw.jsonl"
    completed = _identity(0)
    cancelled = _identity(1)
    with RawObservationRecorder(
        raw_path,
        repeat_id="repeat-0",
        execution_nonce="0" * 32,
    ) as recorder:
        for stage in REQUIRED_CLI_SCHEMA_STAGES:
            _record_observation(
                recorder,
                stage=stage,
                raw=(
                    b"[]"
                    if stage == "list_after_stop"
                    else _raw_response(stage, completed)
                ),
                identity=completed,
            )
        _record_observation(
            recorder,
            stage="start",
            raw=_raw_response("start", cancelled),
            identity=cancelled,
        )
        _record_observation(
            recorder,
            stage="runner",
            raw=b"",
            identity=cancelled,
            returncode=None,
            cancelled=True,
        )
        _record_observation(
            recorder,
            stage="stop",
            raw=_raw_response("stop", cancelled),
            identity=cancelled,
        )
        _record_observation(
            recorder,
            stage="list_after_stop",
            raw=b"[]",
            identity=cancelled,
        )

    report = build_cli_schema_report(raw_path)

    assert report.execution_identities == (completed, cancelled)
    assert b'"cancelled":true' in raw_path.read_bytes()
    assert b'"raw_b64":""' in raw_path.read_bytes()


def test_transcript_rejects_unbound_argv_and_stage_reordering(
    tmp_path: Path,
) -> None:
    identity = _identity(0)
    invalid_argv = tmp_path / "invalid-argv.jsonl"
    with RawObservationRecorder(
        invalid_argv,
        repeat_id="repeat-0",
        execution_nonce="0" * 32,
    ) as recorder:
        with pytest.raises(SandboxEvidenceError, match="observation is invalid"):
            recorder.record(
                "start",
                _raw_response("start", identity),
                argv=(
                    r"C:\Windows\System32\wsb.exe",
                    "stop",
                    "--id",
                    identity.instance_id,
                    "--raw",
                ),
                instance_id=identity.instance_id,
                run_id=identity.run_id,
                request_hash=identity.request_hash,
                returncode=0,
                timed_out=False,
                cancelled=False,
                output_limited=False,
            )

    reordered = tmp_path / "reordered.jsonl"
    with RawObservationRecorder(
        reordered,
        repeat_id="repeat-0",
        execution_nonce="0" * 32,
    ) as recorder:
        for stage in ("start", "share", "runner", "exporter", "stop"):
            _record_observation(
                recorder,
                stage=stage,
                raw=_raw_response(stage, identity),
                identity=identity,
            )
        _record_observation(
            recorder,
            stage="list_after_stop",
            raw=b"[]",
            identity=identity,
        )

    with pytest.raises(SandboxEvidenceError, match="state machine"):
        build_cli_schema_report(reordered)


@pytest.mark.parametrize(
    ("stage", "payload"),
    (
        ("start", {"Id": "INSTANCE", "Status": "Running"}),
        (
            "start",
            {
                "Id": "INSTANCE",
                "Status": "Running",
                "Success": False,
            },
        ),
        (
            "start",
            {
                "Id": "INSTANCE",
                "Status": "Stopped",
                "Success": True,
            },
        ),
        (
            "start",
            {
                "Id": "INSTANCE",
                "Status": "Running",
                "State": "Running",
                "Success": True,
            },
        ),
        (
            "runner",
            {
                "ExitCode": 1,
                "Id": "INSTANCE",
                "Status": "Succeeded",
                "Success": True,
            },
        ),
        (
            "exporter",
            {
                "Id": "INSTANCE",
                "Status": "Succeeded",
                "Success": True,
            },
        ),
        (
            "list_after_stop",
            {
                "WindowsSandboxEnvironments": [],
                "Success": True,
                "Status": "Running",
            },
        ),
        (
            "list_after_stop",
            {
                "Instances": [],
                "Success": True,
                "Status": "Stopped",
            },
        ),
    ),
)
def test_successful_observation_reuses_executor_raw_semantics(
    tmp_path: Path,
    stage: str,
    payload: dict[str, object],
) -> None:
    raw_path = tmp_path / "invalid.jsonl"
    identity = _identity(0)
    bound_payload = json.loads(
        json.dumps(payload).replace("INSTANCE", identity.instance_id)
    )
    raw = json.dumps(bound_payload, separators=(",", ":")).encode()

    with RawObservationRecorder(
        raw_path,
        repeat_id="repeat-0",
        execution_nonce="0" * 32,
    ) as recorder:
        with pytest.raises(SandboxEvidenceError, match="observation is invalid"):
            _record_observation(
                recorder,
                stage=stage,
                raw=raw,
                identity=identity,
            )

    assert raw_path.read_bytes() == b""


def test_three_unique_consistent_passes_create_non_certifying_aggregate() -> None:
    runs = tuple(_run(index) for index in range(MINIMUM_EVIDENCE_REPEATS))

    aggregate = verify_evidence_runs(runs)

    assert aggregate.repeat_ids == ("repeat-0", "repeat-1", "repeat-2")
    assert aggregate.subject.security_assurance == (
        "candidate-restricted-low-integrity-job-not-certified"
    )
    assert aggregate.evidence_started_at == _START
    assert aggregate.evidence_finished_at == _START + timedelta(
        minutes=2,
        seconds=30,
    )
    assert aggregate.evidence_run_sha256s == tuple(
        sorted(run.evidence_sha256 for run in runs)
    )


@pytest.mark.parametrize(
    "outcome",
    ["failed", "errors", "skipped", "xfailed", "xpassed"],
)
def test_any_non_pass_outcome_rejects_the_entire_aggregate(outcome: str) -> None:
    runs = [
        _run(0),
        _run(1, summary=_summary(outcome=outcome)),
        _run(2),
    ]

    with pytest.raises(SandboxEvidenceError, match="zero failures"):
        verify_evidence_runs(runs)


def test_fewer_than_three_repeats_are_rejected() -> None:
    with pytest.raises(SandboxEvidenceError, match="at least 3"):
        verify_evidence_runs((_run(0), _run(1)))


@pytest.mark.parametrize("duplicate", ["repeat", "nonce"])
def test_repeat_id_and_execution_nonce_must_each_be_unique(duplicate: str) -> None:
    first = _run(0)
    runs = [
        first,
        _run(
            1,
            repeat_id=first.repeat_id if duplicate == "repeat" else None,
            nonce=first.execution_nonce if duplicate == "nonce" else None,
        ),
        _run(2),
    ]

    with pytest.raises(SandboxEvidenceError, match="must be unique"):
        verify_evidence_runs(runs)


def test_repeats_must_come_from_one_workflow_attempt() -> None:
    runs = (_run(0), _run(1, workflow_run_id=101), _run(2))

    with pytest.raises(SandboxEvidenceError, match="one workflow attempt"):
        verify_evidence_runs(runs)


def test_fixed_test_manifest_cannot_be_replaced_by_a_smaller_green_suite() -> None:
    nodeids = REQUIRED_WINDOWS_SANDBOX_TESTS[:-1]
    smaller = OutcomeSummary(
        nodeids=nodeids,
        passed=len(nodeids),
        failed=0,
        errors=0,
        skipped=0,
        xfailed=0,
        xpassed=0,
        junit_sha256=_digest("d"),
    )

    with pytest.raises(SandboxEvidenceError, match="fixed required test manifest"):
        verify_evidence_runs((_run(0), _run(1, summary=smaller), _run(2)))


@pytest.mark.parametrize("changed", ["platform", "subject", "schema"])
def test_platform_subject_and_schema_must_match_between_repeats(
    changed: str,
) -> None:
    keyword: dict[str, object]
    if changed == "platform":
        keyword = {"platform": _platform(ubr=2)}
    elif changed == "subject":
        keyword = {
            "subject": _subject(runner_binary_sha256=_digest("e")),
        }
    else:
        keyword = {"schema": _schema(1, start_status="Started")}
    runs = [_run(0), _run(1, **keyword), _run(2)]  # type: ignore[arg-type]

    with pytest.raises(SandboxEvidenceError, match=changed):
        verify_evidence_runs(runs)


def test_raw_cli_hashes_may_change_when_normalized_schema_stays_identical() -> None:
    runs = (
        _run(0, schema=_schema(0, raw_character="1")),
        _run(1, schema=_schema(1, raw_character="4")),
        _run(2, schema=_schema(2, raw_character="8")),
    )

    aggregate = verify_evidence_runs(runs)

    assert aggregate.schema_profile_sha256 == runs[0].schema_profile_sha256


def test_copied_transcript_cannot_be_relabelled_as_three_repeats() -> None:
    original = _schema(0)
    runs: list[SandboxEvidenceRun] = []
    for index in range(3):
        payload = original.model_dump(mode="python")
        payload["repeat_id"] = f"repeat-{index}"
        payload["execution_nonce"] = f"{index:032x}"
        relabelled = CliSchemaReport.model_validate(payload)
        runs.append(_run(index, schema=relabelled))

    with pytest.raises(SandboxEvidenceError, match="distinct execution transcripts"):
        verify_evidence_runs(runs)


def test_repeat_execution_identity_sets_must_not_overlap() -> None:
    shared_identity = _identity(0)
    runs = tuple(
        _run(
            index,
            schema=_schema(
                index,
                identity=shared_identity,
                transcript_sha256=_digest(format(index + 8, "x")),
            ),
        )
        for index in range(3)
    )

    with pytest.raises(SandboxEvidenceError, match="must not overlap"):
        verify_evidence_runs(runs)


def test_empty_trust_pins_can_never_issue_a_certification() -> None:
    aggregate = _aggregate(assurance=CERTIFIED_SECURITY_ASSURANCE)
    review = _review(aggregate)

    with pytest.raises(SandboxEvidenceError, match="no independent review trust pins"):
        issue_certification(aggregate, review)


def test_candidate_assurance_cannot_be_certified_even_with_trusted_review() -> None:
    aggregate = _aggregate()
    review = _review(aggregate)
    pins = ReviewTrustPins(
        reviewer_ids=frozenset({review.reviewer_id}),
        review_sha256s=frozenset({review.review_sha256}),
    )

    with pytest.raises(SandboxEvidenceError, match="candidate"):
        issue_certification(aggregate, review, trust_pins=pins)


def test_evidence_producer_cannot_approve_its_own_aggregate() -> None:
    aggregate = _aggregate(assurance=CERTIFIED_SECURITY_ASSURANCE)
    review = _review(
        aggregate,
        reviewer_id=aggregate.producer_ids[0],
    )
    pins = ReviewTrustPins(
        reviewer_ids=frozenset({review.reviewer_id}),
        review_sha256s=frozenset({review.review_sha256}),
    )

    with pytest.raises(SandboxEvidenceError, match="cannot independently approve"):
        issue_certification(aggregate, review, trust_pins=pins)


def test_exact_pinned_independent_review_can_issue_and_verify() -> None:
    aggregate = _aggregate(assurance=CERTIFIED_SECURITY_ASSURANCE)
    review = _review(aggregate)
    pins = ReviewTrustPins(
        reviewer_ids=frozenset({review.reviewer_id}),
        review_sha256s=frozenset({review.review_sha256}),
    )
    issued_at = _START + timedelta(hours=2)
    expires_at = issued_at + timedelta(days=30)

    certification = issue_certification(
        aggregate,
        review,
        trust_pins=pins,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    verify_certification(
        certification,
        aggregate,
        review,
        trust_pins=pins,
        now=issued_at + timedelta(days=1),
    )

    assert certification.aggregate_sha256 == aggregate.aggregate_sha256
    assert certification.review_sha256 == review.review_sha256


def test_certification_rejects_expiry_and_empty_pins_during_verification() -> None:
    aggregate = _aggregate(assurance=CERTIFIED_SECURITY_ASSURANCE)
    review = _review(aggregate)
    pins = ReviewTrustPins(
        reviewer_ids=frozenset({review.reviewer_id}),
        review_sha256s=frozenset({review.review_sha256}),
    )
    issued_at = _START + timedelta(hours=2)
    certification = issue_certification(
        aggregate,
        review,
        trust_pins=pins,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(days=1),
    )

    with pytest.raises(SandboxEvidenceError, match="no independent review"):
        verify_certification(certification, aggregate, review)
    with pytest.raises(SandboxEvidenceError, match="not currently valid"):
        verify_certification(
            certification,
            aggregate,
            review,
            trust_pins=pins,
            now=issued_at + timedelta(days=2),
        )


def test_certification_cannot_predate_the_independent_review() -> None:
    aggregate = _aggregate(assurance=CERTIFIED_SECURITY_ASSURANCE)
    review = _review(aggregate)
    pins = ReviewTrustPins(
        reviewer_ids=frozenset({review.reviewer_id}),
        review_sha256s=frozenset({review.review_sha256}),
    )

    with pytest.raises(SandboxEvidenceError, match="before the independent review"):
        issue_certification(
            aggregate,
            review,
            trust_pins=pins,
            issued_at=_START,
            expires_at=_START + timedelta(days=1),
        )


def test_independent_review_cannot_predate_completed_evidence() -> None:
    aggregate = _aggregate(assurance=CERTIFIED_SECURITY_ASSURANCE)
    review = _review(
        aggregate,
        reviewed_at=aggregate.evidence_finished_at - timedelta(seconds=1),
    )
    pins = ReviewTrustPins(
        reviewer_ids=frozenset({review.reviewer_id}),
        review_sha256s=frozenset({review.review_sha256}),
    )

    with pytest.raises(SandboxEvidenceError, match="predate completed evidence"):
        issue_certification(
            aggregate,
            review,
            trust_pins=pins,
            issued_at=aggregate.evidence_finished_at + timedelta(hours=1),
            expires_at=aggregate.evidence_finished_at + timedelta(days=1),
        )


def test_junit_collection_distinguishes_skip_xfail_and_xpass(tmp_path: Path) -> None:
    platform_path = tmp_path / "platform.json"
    subject_path = tmp_path / "subject.json"
    _write_json(platform_path, _platform().model_dump(mode="json"), indent=2)
    _write_json(subject_path, _subject().model_dump(mode="json"), indent=2)

    cases = (
        ("skip", '<skipped type="pytest.skip"/>', "skipped"),
        ("xfail", '<skipped type="pytest.xfail"/>', "xfailed"),
        ("xpass", '<failure message="XPASS(strict)"/>', "xpassed"),
    )
    for index, (_, outcome_xml, expected) in enumerate(cases):
        schema_path = tmp_path / f"{expected}-schema.json"
        _write_json(
            schema_path,
            _schema(
                index,
                repeat_id=f"repeat-{index}",
                nonce=f"{index + 1:032x}",
            ).model_dump(mode="json"),
            indent=2,
        )
        junit = tmp_path / f"{expected}.xml"
        output = tmp_path / f"{expected}.json"
        _write_junit(
            junit,
            outcome_nodeid=REQUIRED_WINDOWS_SANDBOX_TESTS[0],
            outcome_xml=outcome_xml,
        )

        exit_code = main(
            (
                "collect",
                "--repeat-id",
                f"repeat-{index}",
                "--execution-nonce",
                f"{index + 1:032x}",
                "--producer-id",
                "github-actions:owner/repository",
                "--workflow-run-id",
                "100",
                "--workflow-attempt",
                "1",
                "--pytest-exit-code",
                "0",
                "--started-at",
                "2026-07-30T01:00:00Z",
                "--finished-at",
                "2026-07-30T01:01:00Z",
                "--platform",
                str(platform_path),
                "--subject",
                str(subject_path),
                "--schema",
                str(schema_path),
                "--junit",
                str(junit),
                "--output",
                str(output),
            )
        )

        assert exit_code == 0
        parsed = SandboxEvidenceRun.model_validate_json(output.read_bytes())
        assert getattr(parsed.tests, expected) == 1


def test_collect_rejects_nonzero_pytest_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    platform_path = tmp_path / "platform.json"
    subject_path = tmp_path / "subject.json"
    schema_path = tmp_path / "schema.json"
    junit_path = tmp_path / "junit.xml"
    output = tmp_path / "run.json"
    _write_json(platform_path, _platform().model_dump(mode="json"), indent=2)
    _write_json(subject_path, _subject().model_dump(mode="json"), indent=2)
    _write_json(
        schema_path,
        _schema(0, nonce="1" * 32).model_dump(mode="json"),
        indent=2,
    )
    _write_junit(junit_path)

    exit_code = main(
        (
            "collect",
            "--repeat-id",
            "repeat-0",
            "--execution-nonce",
            "1" * 32,
            "--producer-id",
            "github-actions:owner/repository",
            "--workflow-run-id",
            "100",
            "--workflow-attempt",
            "1",
            "--pytest-exit-code",
            "1",
            "--started-at",
            "2026-07-30T01:00:00Z",
            "--finished-at",
            "2026-07-30T01:00:30Z",
            "--platform",
            str(platform_path),
            "--subject",
            str(subject_path),
            "--schema",
            str(schema_path),
            "--junit",
            str(junit_path),
            "--output",
            str(output),
        )
    )

    assert exit_code == 2
    assert not output.exists()
    assert "pytest exit code must be zero" in capsys.readouterr().err


def test_junit_suite_counters_must_match_testcases(tmp_path: Path) -> None:
    junit_path = tmp_path / "junit.xml"
    _write_junit(junit_path)
    junit_path.write_text(
        junit_path.read_text(encoding="utf-8").replace(
            f'tests="{len(REQUIRED_WINDOWS_SANDBOX_TESTS)}"',
            'tests="7"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(SandboxEvidenceError, match="counter does not match"):
        _collect_junit(junit_path)


@pytest.mark.parametrize(
    "variant",
    ("root_testsuite", "root_testcase", "multiple_suites", "nested_wrapper"),
)
def test_junit_requires_one_pytest_suite_with_direct_testcases(
    tmp_path: Path,
    variant: str,
) -> None:
    junit_path = tmp_path / "junit.xml"
    _write_junit(junit_path)
    root = ElementTree.fromstring(junit_path.read_bytes())
    suite = root.find("testsuite")
    assert suite is not None
    if variant == "root_testsuite":
        output_root = suite
    elif variant == "root_testcase":
        case = suite.find("testcase")
        assert case is not None
        suite.remove(case)
        suite.set("tests", str(len(REQUIRED_WINDOWS_SANDBOX_TESTS) - 1))
        root.append(case)
        output_root = root
    elif variant == "multiple_suites":
        root.append(
            ElementTree.Element(
                "testsuite",
                tests="0",
                failures="0",
                errors="0",
                skipped="0",
            )
        )
        output_root = root
    else:
        case = suite.find("testcase")
        assert case is not None
        suite.remove(case)
        wrapper = ElementTree.SubElement(suite, "wrapper")
        wrapper.append(case)
        output_root = root
    junit_path.write_bytes(ElementTree.tostring(output_root, encoding="utf-8"))

    with pytest.raises(SandboxEvidenceError, match="JUnit"):
        _collect_junit(junit_path)


@pytest.mark.parametrize(
    "classname",
    (
        "",
        "tests.not_the_required_module",
        "tests.test_sandbox_guest.ForgedClass",
    ),
)
def test_junit_always_binds_classname_to_required_module(
    tmp_path: Path,
    classname: str,
) -> None:
    junit_path = tmp_path / "junit.xml"
    _write_junit(junit_path)
    root = ElementTree.fromstring(junit_path.read_bytes())
    case = root.find("./testsuite/testcase")
    assert case is not None
    case.set("classname", classname)
    junit_path.write_bytes(ElementTree.tostring(root, encoding="utf-8"))

    with pytest.raises(
        SandboxEvidenceError,
        match="invalid name or classname|mapped uniquely",
    ):
        _collect_junit(junit_path)


def test_cli_collects_three_runs_and_verifies_a_canonical_aggregate(
    tmp_path: Path,
) -> None:
    platform_path = tmp_path / "platform.json"
    subject_path = tmp_path / "subject.json"
    junit_path = tmp_path / "junit.xml"
    _write_json(platform_path, _platform().model_dump(mode="json"), indent=2)
    _write_json(subject_path, _subject().model_dump(mode="json"), indent=2)
    _write_junit(junit_path)
    outputs: list[Path] = []
    for index in range(3):
        schema_path = tmp_path / f"schema-{index}.json"
        _write_json(
            schema_path,
            _schema(
                index,
                repeat_id=f"repeat-{index}",
                nonce=f"{index + 1:032x}",
            ).model_dump(mode="json"),
            indent=2,
        )
        output = tmp_path / f"run-{index}.json"
        outputs.append(output)
        assert (
            main(
                (
                    "collect",
                    "--repeat-id",
                    f"repeat-{index}",
                    "--execution-nonce",
                    f"{index + 1:032x}",
                    "--producer-id",
                    "github-actions:owner/repository",
                    "--workflow-run-id",
                    "100",
                    "--workflow-attempt",
                    "1",
                    "--pytest-exit-code",
                    "0",
                    "--started-at",
                    f"2026-07-30T01:0{index}:00Z",
                    "--finished-at",
                    f"2026-07-30T01:0{index}:30Z",
                    "--platform",
                    str(platform_path),
                    "--subject",
                    str(subject_path),
                    "--schema",
                    str(schema_path),
                    "--junit",
                    str(junit_path),
                    "--output",
                    str(output),
                )
            )
            == 0
        )

    aggregate_path = tmp_path / "aggregate.json"
    arguments = ["verify"]
    for output in outputs:
        arguments.extend(("--run", str(output)))
    arguments.extend(("--output", str(aggregate_path)))

    assert main(arguments) == 0
    aggregate_raw = aggregate_path.read_bytes()
    aggregate = SandboxEvidenceAggregate.model_validate_json(aggregate_raw)
    assert aggregate.canonical_bytes() == aggregate_raw


def test_cli_verify_rejects_noncanonical_run_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths: list[Path] = []
    for index in range(3):
        path = tmp_path / f"run-{index}.json"
        path.write_bytes(_run(index).canonical_bytes() + b"\n")
        paths.append(path)
    output = tmp_path / "aggregate.json"
    arguments = ["verify"]
    for path in paths:
        arguments.extend(("--run", str(path)))
    arguments.extend(("--output", str(output)))

    assert main(arguments) == 2
    assert not output.exists()
    assert "not canonical" in capsys.readouterr().err


def test_models_reject_unknown_fields_and_tampered_self_hashes() -> None:
    run = _run(0)
    payload = run.model_dump(mode="json")
    payload["unknown"] = True
    with pytest.raises(ValueError):
        SandboxEvidenceRun.model_validate_json(json.dumps(payload))

    payload.pop("unknown")
    payload["evidence_sha256"] = _digest("f")
    with pytest.raises(ValueError, match="digest does not match"):
        SandboxEvidenceRun.model_validate_json(json.dumps(payload))


def test_certification_model_rejects_a_lifetime_over_ninety_days() -> None:
    aggregate = _aggregate(assurance=CERTIFIED_SECURITY_ASSURANCE)
    review = _review(aggregate)

    with pytest.raises(ValueError, match="lifetime"):
        SandboxCertification.create(
            aggregate=aggregate,
            review=review,
            issued_at=_START,
            expires_at=_START + timedelta(days=91),
        )


def test_security_workflow_repeats_derives_schema_and_always_uploads() -> None:
    workflow = (
        Path(__file__).parents[1]
        / ".github"
        / "workflows"
        / "windows-sandbox-security.yml"
    ).read_text(encoding="utf-8")

    assert "foreach ($repeat in 1..3)" in workflow
    assert "sandbox_evidence schema" in workflow
    assert "sandbox_evidence collect" in workflow
    assert "$verifyArguments" in workflow
    assert '"neil_agent.sandbox_evidence",' in workflow
    assert '"verify"' in workflow
    assert "SANDBOX_EVIDENCE_RAW_JSONL" in workflow
    assert "if: always()" in workflow
    assert (
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f" in workflow
    )
    assert "sandbox_evidence certify" not in workflow
