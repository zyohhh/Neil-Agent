"""Install and exercise a wheel in a fresh environment outside its checkout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic, sleep
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
from zipfile import BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict, Field

from .processes import acceptance_environment, creation_flags, run_bounded

CHECK_IDS = (
    "wheel_metadata",
    "fresh_environment",
    "install_dependencies",
    "dependency_check",
    "cli_help",
    "eval_help",
    "web_help",
    "install_help",
    "installed_resources",
    "offline_evals",
    "module_evals",
    "coding_harness",
    "api_cost_gate",
    "coding_cost_gate",
    "web_loopback",
)


class InstallCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    status: Literal["passed", "failed", "skipped"]
    exit_code: int | None = None


class InstallReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    wheel_sha256: str
    version: str | None
    offline_install: bool
    python_version: str | None = None
    dependency_versions: dict[str, str] = Field(default_factory=dict)
    checks: tuple[InstallCheck, ...]

    @property
    def success(self) -> bool:
        return all(check.status == "passed" for check in self.checks)

    def summary(self) -> dict[str, object]:
        return {**self.model_dump(mode="json"), "success": self.success}


def _wheel_version(wheel: Path) -> str:
    if (
        wheel.suffix != ".whl"
        or not wheel.is_file()
        or wheel.stat().st_size > 50_000_000
    ):
        raise ValueError("A bounded local wheel file is required")
    with ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise ValueError("Wheel metadata is unavailable")
        info = archive.getinfo(metadata_files[0])
        if info.file_size > 262_144:
            raise ValueError("Wheel metadata exceeds limits")
        metadata = BytesParser().parsebytes(archive.read(info))
        if metadata.get("Name") != "neil-agent" or not metadata.get("Version"):
            raise ValueError("Expected a Neil Agent wheel")
        required = {
            "neil_agent/evaluation/tasks.json",
            "neil_agent/evaluation/coding_tasks.json",
            "neil_agent/web/static/asset-manifest.json",
            "neil_agent/installation_probe.py",
        }
        if not required <= set(archive.namelist()):
            raise ValueError("Wheel resources are incomplete")
        return str(metadata["Version"])


def run_install_acceptance(
    wheel: Path,
    *,
    offline: bool = True,
    work_dir: Path | None = None,
    index_url: str | None = None,
) -> InstallReport:
    """Never reuse the active venv or install into a global/user environment."""

    wheel = wheel.expanduser().resolve(strict=True)
    checks = {name: InstallCheck(id=name, status="skipped") for name in CHECK_IDS}
    digest = (
        sha256(wheel.read_bytes()).hexdigest()
        if wheel.stat().st_size <= 50_000_000
        else ""
    )
    package_version: str | None = None
    python_version: str | None = None
    dependency_versions: dict[str, str] = {}

    def report() -> InstallReport:
        return InstallReport(
            wheel_sha256=digest,
            version=package_version,
            offline_install=offline,
            python_version=python_version,
            dependency_versions=dependency_versions,
            checks=tuple(checks.values()),
        )

    def mark(name: str, passed: bool, exit_code: int | None = None) -> bool:
        checks[name] = InstallCheck(
            id=name, status="passed" if passed else "failed", exit_code=exit_code
        )
        return passed

    try:
        package_version = _wheel_version(wheel)
    except (OSError, ValueError, BadZipFile):
        mark("wheel_metadata", False)
        return report()
    mark("wheel_metadata", True)
    uv = shutil.which("uv")
    if uv is None:
        mark("fresh_environment", False)
        return report()
    parent = None if work_dir is None else work_dir.expanduser().resolve(strict=True)
    with TemporaryDirectory(prefix="neil-install-check-", dir=parent) as temporary:
        root = Path(temporary).resolve()
        environment = root / "environment"
        outside = root / "outside-checkout"
        workspace = root / "workspace"
        outside.mkdir()
        workspace.mkdir()
        uv_prefix = [uv, "--no-config", *(["--offline"] if offline else [])]
        commands = (
            (
                "fresh_environment",
                [
                    *uv_prefix,
                    "venv",
                    "--no-project",
                    "--no-python-downloads",
                    "--python",
                    sys.executable,
                    str(environment),
                ],
            ),
        )
        for name, command in commands:
            result = run_bounded(
                command, cwd=outside, timeout=60, max_output_bytes=1_048_576
            )
            if not mark(
                name,
                result.reason == "completed" and result.returncode == 0,
                result.returncode,
            ):
                return report()
        bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        install = [
            *uv_prefix,
            "pip",
            "install",
            "--only-binary",
            ":all:",
            "--python",
            str(python),
        ]
        if index_url is not None:
            install.extend(("--index-url", index_url))
        install.append(str(wheel))
        result = run_bounded(
            install, cwd=outside, timeout=180, max_output_bytes=1_048_576
        )
        if not mark(
            "install_dependencies",
            result.reason == "completed" and result.returncode == 0,
            result.returncode,
        ):
            return report()
        suffix = ".exe" if os.name == "nt" else ""
        cli, evaluate, web = (
            str(bin_dir / (name + suffix))
            for name in ("neil-agent", "neil-agent-eval", "neil-agent-web")
        )
        install_check = str(bin_dir / ("neil-agent-install-check" + suffix))
        probes = (
            (
                "dependency_check",
                [*uv_prefix, "pip", "check", "--python", str(python)],
                0,
            ),
            ("cli_help", [cli, "--help"], 0),
            ("eval_help", [evaluate, "--help"], 0),
            ("web_help", [web, "--help"], 0),
            ("install_help", [install_check, "--help"], 0),
            (
                "installed_resources",
                [str(python), "-I", "-m", "neil_agent.installation_probe", "resources"],
                0,
            ),
            ("offline_evals", [evaluate, "--format", "json"], 0),
            (
                "module_evals",
                [str(python), "-I", "-m", "neil_agent.evals", "--format", "json"],
                0,
            ),
            (
                "coding_harness",
                [evaluate, "--coding-tasks", "--simulate", "--format", "json"],
                0,
            ),
            ("api_cost_gate", [evaluate, "--real-deepseek"], 2),
            ("coding_cost_gate", [evaluate, "--coding-tasks"], 2),
        )
        for name, command, expected_code in probes:
            result = run_bounded(command, cwd=outside, timeout=60)
            passed = result.reason == "completed" and result.returncode == expected_code
            if passed and name in {
                "offline_evals",
                "module_evals",
                "coding_harness",
                "installed_resources",
            }:
                try:
                    payload = json.loads(result.output)
                    if name == "installed_resources":
                        passed = (
                            payload.get("installed") is True
                            and payload.get("version") == package_version
                            and payload.get("offline_tasks", 0) >= 6
                            and payload.get("coding_tasks", 0) >= 3
                            and payload.get("static_files", 0) >= 3
                        )
                        if passed:
                            runtime_python = payload.get("python")
                            dependencies = payload.get("dependencies")
                            required = {
                                "anthropic",
                                "openai",
                                "pydantic",
                                "fastapi",
                                "uvicorn",
                                "websockets",
                                "textual",
                            }
                            passed = (
                                isinstance(runtime_python, str)
                                and len(runtime_python) <= 80
                                and isinstance(dependencies, dict)
                                and dependencies.keys() == required
                                and all(
                                    isinstance(value, str) and len(value) <= 80
                                    for value in dependencies.values()
                                )
                            )
                            if passed:
                                python_version = runtime_python
                                dependency_versions = dependencies
                    else:
                        passed = payload.get("success") is True and payload.get(
                            "total", 0
                        ) >= (3 if name == "coding_harness" else 6)
                        if name == "coding_harness":
                            passed = passed and payload.get("mode") == "simulated"
                except (ValueError, AttributeError, TypeError):
                    passed = False
            mark(name, passed, result.returncode)
        mark("web_loopback", _check_web_loopback(python, workspace, outside))
    return report()


def _check_web_loopback(python: Path, workspace: Path, outside: Path) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    with TemporaryFile() as output:
        process = subprocess.Popen(
            [
                str(python),
                "-I",
                "-m",
                "neil_agent.installation_probe",
                "serve",
                "--workspace",
                str(workspace),
                "--port",
                str(port),
            ],
            cwd=outside,
            env=acceptance_environment(),
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags(),
        )
        healthy = False
        opener = build_opener(ProxyHandler({}))
        try:
            deadline = monotonic() + 15
            base = f"http://127.0.0.1:{port}"
            while monotonic() < deadline and process.poll() is None:
                try:
                    with opener.open(base + "/api/v1/health", timeout=0.5) as response:
                        health = json.loads(response.read(8_192))
                        healthy = (
                            health.get("status") == "ready"
                            and health.get("service") == "neil-agent-web"
                            and health.get("schema_version") == 1
                        )
                    if healthy:
                        break
                    sleep(0.05)
                except (OSError, URLError, ValueError):
                    sleep(0.05)
            if healthy:
                with opener.open(base + "/", timeout=2) as response:
                    healthy = (
                        response.status == 200
                        and "default-src"
                        in response.headers.get("Content-Security-Policy", "")
                    )
                try:
                    with opener.open(base + "/api/v1/snapshot", timeout=2):
                        healthy = False
                except HTTPError as error:
                    healthy = healthy and error.code == 401
            if process.stdin is not None:
                process.stdin.write(b"stop\n")
                process.stdin.close()
            process.wait(timeout=15)
            return healthy and process.returncode == 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install and verify a local wheel in a fresh isolated environment; no model API calls."
    )
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument(
        "--allow-network-install",
        action="store_true",
        help="Allow dependency downloads; model APIs remain disabled.",
    )
    parser.add_argument("--index-url")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    arguments = parser.parse_args()
    try:
        result = run_install_acceptance(
            arguments.wheel,
            offline=not arguments.allow_network_install,
            work_dir=arguments.work_dir,
            index_url=arguments.index_url,
        )
    except (OSError, ValueError):
        parser.exit(
            2,
            "Installation acceptance could not initialize; check the wheel and work directory.\n",
        )
    if arguments.format == "json":
        print(json.dumps(result.summary(), sort_keys=True))
    else:
        for check in result.checks:
            print(f"{check.status}: {check.id}")
    raise SystemExit(0 if result.success else 1)


if __name__ == "__main__":
    main()
