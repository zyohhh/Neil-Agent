"""Install acceptance must exercise the artifact, not reuse editable sources."""

import json
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from neil_agent import installation
from neil_agent.evaluation.catalog import MAX_CATALOG_BYTES, _load_task_ids
from neil_agent.evaluation.offline import run_offline_evals
from neil_agent.processes import ProcessResult, acceptance_environment, run_bounded


def _wheel(tmp_path: Path, *, complete: bool = True, name: str = "neil-agent") -> Path:
    target = tmp_path / "neil_agent-0.1.0-py3-none-any.whl"
    with ZipFile(target, "w") as archive:
        archive.writestr(
            "neil_agent-0.1.0.dist-info/METADATA", f"Name: {name}\nVersion: 0.1.0\n"
        )
        if complete:
            for resource in (
                "neil_agent/evaluation/tasks.json",
                "neil_agent/evaluation/coding_tasks.json",
                "neil_agent/web/static/asset-manifest.json",
                "neil_agent/installation_probe.py",
            ):
                archive.writestr(resource, "{}")
    return target


@pytest.mark.parametrize(
    "complete,name", [(False, "neil-agent"), (True, "different-package")]
)
def test_incomplete_or_wrong_wheel_is_rejected_before_install(
    tmp_path: Path, monkeypatch, complete: bool, name: str
) -> None:
    wheel = _wheel(tmp_path, complete=complete, name=name)
    monkeypatch.setattr(
        installation,
        "run_bounded",
        lambda *_args, **_kwargs: pytest.fail("Invalid artifact was executed"),
    )
    report = installation.run_install_acceptance(wheel)
    assert not report.success and report.checks[0].status == "failed"
    assert all(check.status == "skipped" for check in report.checks[1:])


def test_install_failure_keeps_later_checks_skipped_and_hides_raw_output(
    tmp_path: Path, monkeypatch
) -> None:
    wheel = _wheel(tmp_path)
    monkeypatch.setattr(installation.shutil, "which", lambda _: "uv")

    def fail_install(command, **kwargs):
        if "venv" in command:
            return ProcessResult(0, "created")
        return ProcessResult(1, "PRIVATE-INDEX-CREDENTIAL")

    monkeypatch.setattr(installation, "run_bounded", fail_install)
    report = installation.run_install_acceptance(wheel, work_dir=tmp_path)
    assert not report.success
    assert (
        report.checks[2].id == "install_dependencies"
        and report.checks[2].status == "failed"
    )
    assert all(check.status == "skipped" for check in report.checks[3:])
    assert "PRIVATE" not in json.dumps(report.summary())
    assert sorted(path.name for path in tmp_path.iterdir()) == [wheel.name]


def _fake_probe(command, **kwargs):
    if "resources" in command:
        payload = {
            "installed": True,
            "version": "0.1.0",
            "offline_tasks": 6,
            "coding_tasks": 3,
            "static_files": 3,
            "python": "3.13.0",
            "dependencies": {
                name: "1.0"
                for name in (
                    "anthropic",
                    "openai",
                    "pydantic",
                    "fastapi",
                    "uvicorn",
                    "websockets",
                    "textual",
                )
            },
        }
    elif (
        "--real-deepseek" in command
        or "--coding-tasks" in command
        and "--simulate" not in command
    ):
        return ProcessResult(2, "cost gate")
    elif "--simulate" in command:
        payload = {"mode": "simulated", "success": True, "total": 3}
    elif "--format" in command:
        payload = {"success": True, "total": 6}
    else:
        return ProcessResult(0, "done")
    return ProcessResult(0, json.dumps(payload))


def test_install_acceptance_uses_new_environment_and_real_json_results(
    tmp_path: Path, monkeypatch
) -> None:
    wheel = _wheel(tmp_path)
    commands: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(installation.shutil, "which", lambda _: "uv")
    monkeypatch.setattr(installation, "_check_web_loopback", lambda *_: True)

    def probe(command, **kwargs):
        commands.append((list(command), kwargs["cwd"]))
        return _fake_probe(command, **kwargs)

    monkeypatch.setattr(installation, "run_bounded", probe)
    report = installation.run_install_acceptance(wheel, work_dir=tmp_path)
    assert report.success and len(report.checks) == 15
    create = commands[0][0]
    install = commands[1][0]
    assert (
        "--offline" in create and "--no-config" in create and "--no-project" in create
    )
    assert "--no-python-downloads" in create and "--system-site-packages" not in create
    assert "--no-deps" not in install and "--editable" not in install
    installed_python = Path(install[install.index("--python") + 1])
    assert installed_python != Path(sys.executable)
    assert installed_python.parent.parent.name == "environment"
    assert all(path.name == "outside-checkout" for _, path in commands)
    assert all(not path.exists() for _, path in commands)
    assert len(report.wheel_sha256) == 64
    assert report.python_version == "3.13.0" and "openai" in report.dependency_versions


def test_empty_eval_output_cannot_pass_as_install_acceptance(
    tmp_path: Path, monkeypatch
) -> None:
    wheel = _wheel(tmp_path)
    monkeypatch.setattr(installation.shutil, "which", lambda _: "uv")
    monkeypatch.setattr(installation, "_check_web_loopback", lambda *_: True)

    def empty_evals(command, **kwargs):
        if "--format" in command and "--simulate" not in command:
            return ProcessResult(0, "")
        return _fake_probe(command, **kwargs)

    monkeypatch.setattr(installation, "run_bounded", empty_evals)
    report = installation.run_install_acceptance(wheel, work_dir=tmp_path)
    assert not report.success
    assert {check.id for check in report.checks if check.status == "failed"} == {
        "offline_evals",
        "module_evals",
    }


def test_sanitized_children_do_not_inherit_secrets_or_python_injection(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-key")
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/source")
    monkeypatch.setenv("VIRTUAL_ENV", "/active/developer/environment")
    monkeypatch.setenv("UV_INDEX_URL", "https://private-key@example.invalid")
    environment = acceptance_environment()
    assert (
        not {
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "UV_INDEX_URL",
        }
        & environment.keys()
    )
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_process_limits_stop_hanging_checks_and_bound_output(tmp_path: Path) -> None:
    hanging = run_bounded(
        (sys.executable, "-I", "-c", "import time; time.sleep(10)"),
        cwd=tmp_path,
        timeout=0.1,
    )
    assert hanging.reason == "timeout"
    noisy = run_bounded(
        (sys.executable, "-I", "-c", "print('x' * 100000)"),
        cwd=tmp_path,
        timeout=5,
        max_output_bytes=1024,
    )
    assert noisy.reason == "output_limit" and len(noisy.output.encode()) <= 1024


def test_default_catalog_and_module_entry_work_without_checkout_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert len(_load_task_ids()) == 6
    assert all(result.passed for result in run_offline_evals())
    result = run_bounded(
        (sys.executable, "-I", "-m", "neil_agent.evals", "--format", "json"),
        cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 0
    payload = json.loads(result.output)
    assert payload["success"] is True and payload["total"] == 6


@pytest.mark.parametrize(
    "payload",
    [[], [{"id": "same"}, {"id": "same"}], [{"id": "../escape"}], {"id": "task"}],
)
def test_invalid_offline_catalogs_cannot_report_empty_success(
    tmp_path: Path, payload
) -> None:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        _load_task_ids(path)


def test_catalog_size_is_bounded_before_parse(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    path.write_bytes(b" " * (MAX_CATALOG_BYTES + 1))
    with pytest.raises(ValueError, match="上限"):
        _load_task_ids(path)
