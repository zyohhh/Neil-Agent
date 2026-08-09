"""Fail-closed conversion of reviewed evidence into runtime capability.

Only this module maps a fully replayed, Sigstore-verified bundle into the
runtime certification type consumed by :mod:`neil_agent.sandbox`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import SandboxError
from .sandbox import (
    WINDOWS_SANDBOX_BACKEND,
    SandboxCertification as RuntimeSandboxCertification,
)
from .sandbox_evidence import (
    BACKEND_POLICY_VERSION,
    IndependentSecurityReview,
    ReviewTrustPins,
    REQUIRED_SECURITY_GATE_IDS,
    SandboxCertification,
    SandboxEvidenceError,
    VerifiedEvidenceBundle,
    _hash_identity_file,
    _load_json_model,
    collect_windows_platform_fingerprint,
    verify_certification,
    verify_evidence_bundle,
    verify_evidence_subject_checkout,
)
from .sandbox_guest import (
    GUEST_PROTOCOL_VERSION,
    find_dotnet_framework_csc,
)

SANDBOX_CERTIFICATION_ROOT_ENV = "SANDBOX_CERTIFICATION_ROOT"
SANDBOX_TRUSTED_REVIEWER_ENV = "SANDBOX_TRUSTED_REVIEWER"
SANDBOX_TRUSTED_REVIEW_SHA256_ENV = "SANDBOX_TRUSTED_REVIEW_SHA256"

_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class SandboxRuntimeCertificationError(SandboxError):
    """A configured runtime certification failed closed."""


class SandboxRuntimeCertificationUnavailable(SandboxRuntimeCertificationError):
    """No runtime certification was configured."""


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeCertification:
    """Opaque verifier output used by the backend for one execution attempt."""

    certification: RuntimeSandboxCertification
    runner_binary_path: Path
    expires_at: datetime


RuntimeAttestationVerifier = Callable[[Path, Path, object], None]


def load_runtime_certification(
    wsb_executable: Path,
    *,
    certification_root: Path | None = None,
    repository_root: Path | None = None,
    trust_pins: ReviewTrustPins | None = None,
    now: datetime | None = None,
    attestation_verifier: RuntimeAttestationVerifier | None = None,
) -> VerifiedRuntimeCertification:
    """Revalidate raw evidence and bind it to this checkout and Windows host."""

    configured_root = certification_root
    if configured_root is None:
        root_value = os.environ.get(SANDBOX_CERTIFICATION_ROOT_ENV, "")
        if not root_value:
            raise SandboxRuntimeCertificationUnavailable(
                "no Windows Sandbox certification bundle is configured"
            )
        configured_root = Path(root_value)
    if not configured_root.is_absolute():
        raise SandboxRuntimeCertificationError(
            "the Windows Sandbox certification root must be absolute"
        )

    effective_pins = trust_pins or _environment_trust_pins()
    current_time = now or datetime.now(timezone.utc)
    checkout = repository_root or _discover_repository_root()
    try:
        kwargs: dict[str, object] = {}
        if attestation_verifier is not None:
            kwargs["attestation_verifier"] = attestation_verifier
        bundle = verify_evidence_bundle(configured_root, **kwargs)  # type: ignore[arg-type]
        review = _load_json_model(
            bundle.root / "independent-review.json",
            IndependentSecurityReview,
            require_canonical=True,
        )
        certification = _load_json_model(
            bundle.root / "certification.json",
            SandboxCertification,
            require_canonical=True,
        )
        verify_certification(
            certification,
            bundle.aggregate,
            review,
            trust_pins=effective_pins,
            now=current_time,
        )
        _verify_current_host(
            wsb_executable,
            checkout,
            bundle,
        )
    except (SandboxEvidenceError, OSError, ValueError) as error:
        raise SandboxRuntimeCertificationError(
            "Windows Sandbox certification did not match the current runtime"
        ) from error

    subject = bundle.aggregate.subject
    return VerifiedRuntimeCertification(
        certification=RuntimeSandboxCertification(
            backend=WINDOWS_SANDBOX_BACKEND,
            git_commit_sha=subject.git_commit_sha,
            evidence_sha256=bundle.aggregate.aggregate_sha256,
            provenance_sha256=bundle.attestation_sha256,
            independent_review_sha256=review.review_sha256,
            certification_sha256=certification.certification_sha256,
            executable_sha256=bundle.aggregate.platform.wsb_sha256,
            runner_source_sha256=subject.runner_source_sha256,
            runner_binary_sha256=subject.runner_binary_sha256,
            policy_version=subject.backend_policy_version,
            protocol_version=subject.guest_protocol_version,
            required_gate_ids=REQUIRED_SECURITY_GATE_IDS,
        ),
        runner_binary_path=bundle.runner_binary_path,
        expires_at=certification.expires_at,
    )


def _environment_trust_pins() -> ReviewTrustPins:
    reviewer = os.environ.get(SANDBOX_TRUSTED_REVIEWER_ENV, "")
    review_sha256 = os.environ.get(SANDBOX_TRUSTED_REVIEW_SHA256_ENV, "")
    if not reviewer or not review_sha256:
        raise SandboxRuntimeCertificationUnavailable(
            "independent review trust pins are not configured"
        )
    try:
        return ReviewTrustPins(
            reviewer_ids=frozenset({reviewer}),
            review_sha256s=frozenset({review_sha256}),
        )
    except ValueError as error:
        raise SandboxRuntimeCertificationError(
            "independent review trust pins are invalid"
        ) from error


def _discover_repository_root() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    if not (candidate / "pyproject.toml").is_file():
        raise SandboxRuntimeCertificationError(
            "a reviewed source checkout is required for runtime certification"
        )
    return candidate


def _verify_current_host(
    wsb_executable: Path,
    repository_root: Path,
    bundle: VerifiedEvidenceBundle,
) -> None:
    platform = collect_windows_platform_fingerprint(wsb_executable)
    if platform != bundle.aggregate.platform:
        raise SandboxEvidenceError(
            "current Windows/WSB fingerprint does not match evidence"
        )
    commit_sha = _current_git_commit(repository_root)
    verify_evidence_subject_checkout(
        bundle.aggregate.subject,
        repository_root,
        commit_sha,
    )
    compiler = find_dotnet_framework_csc()
    if compiler is None:
        raise SandboxEvidenceError("the certified C# compiler is unavailable")
    subject = bundle.aggregate.subject
    if (
        _hash_identity_file(
            compiler,
            label="current C# compiler",
            allow_hardlinks=True,
        )
        != subject.compiler_sha256
        or _hash_identity_file(
            compiler.parent / "System.Web.Extensions.dll",
            label="current .NET Framework reference",
            allow_hardlinks=True,
        )
        != subject.framework_reference_sha256
        or subject.backend_policy_version != BACKEND_POLICY_VERSION
        or subject.guest_protocol_version != GUEST_PROTOCOL_VERSION
    ):
        raise SandboxEvidenceError(
            "current compiler, framework, policy, or protocol does not match evidence"
        )


def _current_git_commit(repository_root: Path) -> str:
    located = shutil.which("git")
    if not located:
        raise SandboxEvidenceError("Git is required to bind the current revision")
    git = Path(located)
    _hash_identity_file(git, label="Git executable", allow_hardlinks=True)
    environment = {
        name: value
        for name in ("SystemRoot", "WINDIR", "TEMP", "TMP")
        if (value := os.environ.get(name))
    }
    environment.update(
        {
            "PATH": str(git.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "NUL" if os.name == "nt" else "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    try:
        completed = subprocess.run(
            (str(git), "-C", str(repository_root), "rev-parse", "--verify", "HEAD"),
            shell=False,
            cwd=repository_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SandboxEvidenceError(
            "current Git revision could not be verified"
        ) from error
    value = completed.stdout.decode("ascii", errors="strict").strip()
    if (
        completed.returncode != 0
        or completed.stderr.strip()
        or not _GIT_OBJECT_ID.fullmatch(value)
    ):
        raise SandboxEvidenceError("current Git revision is not canonical")
    return value
