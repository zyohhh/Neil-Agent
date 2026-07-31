"""Tests for filtered, deterministic sandbox workspace snapshots."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest

from neil_agent import sandbox_snapshot as snapshot_module
from neil_agent.errors import SandboxError
from neil_agent.sandbox_snapshot import (
    SnapshotLimits,
    inspect_prepared_snapshot,
    prepare_snapshot,
)

SENSITIVE_DIRECTORIES = (
    ".aws",
    ".azure",
    ".docker",
    ".gnupg",
    ".kube",
    ".ssh",
    "AppData",
)
SENSITIVE_FILES = (
    "credentials",
    "credentials.json",
    "application_default_credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
)


def test_snapshot_filters_sensitive_paths_and_builds_canonical_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "b.txt").write_text("beta", encoding="utf-8")
    package = source / "src"
    package.mkdir()
    (package / "a.py").write_text("alpha\n", encoding="utf-8")
    (source / ".env").write_text("DEEPSEEK_API_KEY=secret", encoding="utf-8")
    (source / "private.pem").write_text("secret", encoding="utf-8")
    git = source / ".git"
    git.mkdir()
    (git / "config").write_text("credential", encoding="utf-8")
    destination = tmp_path / "snapshot"

    prepared = prepare_snapshot(source.resolve(), destination.resolve())

    assert (prepared.root / "b.txt").read_text(encoding="utf-8") == "beta"
    assert (prepared.root / "src" / "a.py").read_text(encoding="utf-8") == "alpha\n"
    assert not (prepared.root / ".env").exists()
    assert not (prepared.root / "private.pem").exists()
    assert not (prepared.root / ".git").exists()
    assert [entry.path for entry in prepared.manifest.entries] == [
        "b.txt",
        "src",
        "src/a.py",
    ]
    assert prepared.manifest.file_count == 2
    assert prepared.manifest.total_bytes == 11
    assert (
        prepared.manifest.digest
        == hashlib.sha256(prepared.manifest.canonical_json.encode("utf-8")).hexdigest()
    )
    assert "secret" not in prepared.manifest.canonical_json
    with pytest.raises(FrozenInstanceError):
        prepared.manifest.digest = "changed"  # type: ignore[misc]
    prepared.close()


def test_manifest_digest_is_stable_across_destinations_and_creation_order(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "first-source"
    second_source = tmp_path / "second-source"
    first_source.mkdir()
    second_source.mkdir()
    for source, names in (
        (first_source, ("z.txt", "a.txt")),
        (second_source, ("a.txt", "z.txt")),
    ):
        for name in names:
            (source / name).write_text(name, encoding="utf-8")

    first = prepare_snapshot(first_source.resolve(), (tmp_path / "one").resolve())
    second = prepare_snapshot(second_source.resolve(), (tmp_path / "two").resolve())

    try:
        assert first.manifest.canonical_json == second.manifest.canonical_json
        assert first.manifest.digest == second.manifest.digest
    finally:
        first.close()
        second.close()


def test_prepared_snapshot_reinspection_reproduces_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "README.md").write_text("snapshot\n", encoding="utf-8")
    (source / "src" / "app.py").write_text("print('safe')\n", encoding="utf-8")

    prepared = prepare_snapshot(
        source.resolve(),
        (tmp_path / "snapshot").resolve(),
    )
    try:
        inspected = inspect_prepared_snapshot(prepared.root)
        assert inspected == prepared.manifest
        assert inspected.canonical_json == prepared.manifest.canonical_json
        assert inspected.digest == prepared.manifest.digest
    finally:
        prepared.close()


def test_prepared_snapshot_reinspection_observes_content_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("before\n", encoding="utf-8")

    prepared = prepare_snapshot(
        source.resolve(),
        (tmp_path / "snapshot").resolve(),
    )
    try:
        (prepared.root / "app.py").write_text("after\n", encoding="utf-8")
        inspected = inspect_prepared_snapshot(prepared.root)
        assert inspected.digest != prepared.manifest.digest
    finally:
        prepared.close()


def test_prepared_snapshot_reinspection_rejects_late_sensitive_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("safe\n", encoding="utf-8")

    prepared = prepare_snapshot(
        source.resolve(),
        (tmp_path / "snapshot").resolve(),
    )
    try:
        (prepared.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        with pytest.raises(SandboxError, match="敏感"):
            inspect_prepared_snapshot(prepared.root)
    finally:
        prepared.close()


@pytest.mark.parametrize("directory_name", SENSITIVE_DIRECTORIES)
def test_snapshot_filters_sensitive_directory_union(
    tmp_path: Path,
    directory_name: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sensitive = source / directory_name
    sensitive.mkdir()
    (sensitive / "secret.txt").write_text("PRIVATE", encoding="utf-8")
    (source / "safe.txt").write_text("safe", encoding="utf-8")

    with prepare_snapshot(
        source.resolve(),
        (tmp_path / "snapshot").resolve(),
    ) as prepared:
        manifest_paths = {entry.path.casefold() for entry in prepared.manifest.entries}
        assert not (prepared.root / directory_name).exists()
        assert directory_name.casefold() not in manifest_paths
        assert (prepared.root / "safe.txt").read_text(encoding="utf-8") == "safe"
        assert "PRIVATE" not in prepared.manifest.canonical_json


@pytest.mark.parametrize("file_name", SENSITIVE_FILES)
def test_snapshot_filters_sensitive_file_union(
    tmp_path: Path,
    file_name: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / file_name).write_text("PRIVATE", encoding="utf-8")
    (source / ".env.example").write_text("SAFE_EXAMPLE=1\n", encoding="utf-8")

    with prepare_snapshot(
        source.resolve(),
        (tmp_path / "snapshot").resolve(),
    ) as prepared:
        manifest_paths = {entry.path.casefold() for entry in prepared.manifest.entries}
        assert not (prepared.root / file_name).exists()
        assert file_name.casefold() not in manifest_paths
        assert (prepared.root / ".env.example").read_text(encoding="utf-8") == (
            "SAFE_EXAMPLE=1\n"
        )
        assert ".env.example" in manifest_paths
        assert "PRIVATE" not in prepared.manifest.canonical_json


def test_source_changes_after_preparation_do_not_change_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "app.py"
    target.write_text("before\n", encoding="utf-8")

    with prepare_snapshot(
        source.resolve(),
        (tmp_path / "snapshot").resolve(),
    ) as prepared:
        original_digest = prepared.manifest.digest
        target.write_text("after\n", encoding="utf-8")

        assert (prepared.root / "app.py").read_text(encoding="utf-8") == "before\n"
        assert prepared.manifest.digest == original_digest


def test_context_manager_and_explicit_close_remove_owned_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("content", encoding="utf-8")
    first_target = tmp_path / "first"

    with prepare_snapshot(source.resolve(), first_target.resolve()) as prepared:
        assert first_target.is_dir()
        assert prepared.closed is False

    assert not first_target.exists()
    assert prepared.closed is True
    prepared.close()


def test_cleanup_refuses_new_symlink_without_following_it(tmp_path: Path) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("snapshot", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "must-remain.txt"
    outside_file.write_text("outside", encoding="utf-8")
    prepared = prepare_snapshot(
        source.resolve(),
        (tmp_path / "snapshot").resolve(),
    )
    snapshot_nested = prepared.root / "nested"
    (snapshot_nested / "file.txt").unlink()
    snapshot_nested.rmdir()
    try:
        snapshot_nested.symlink_to(outside, target_is_directory=True)
    except OSError:
        prepared.close()
        pytest.skip("creating directory symlinks is unavailable")

    with pytest.raises(SandboxError, match="重解析点"):
        prepared.close()

    assert outside_file.read_text(encoding="utf-8") == "outside"
    assert prepared.closed is False
    snapshot_nested.unlink()
    prepared.close()
    assert not prepared.root.exists()


def test_cleanup_fail_closed_on_observed_reparse_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("snapshot", encoding="utf-8")
    prepared = prepare_snapshot(
        source.resolve(),
        (tmp_path / "snapshot").resolve(),
    )
    snapshot_nested = prepared.root / "nested"
    nested_identity = snapshot_nested.lstat()
    original_check = snapshot_module._metadata_is_reparse

    def mark_nested_as_reparse(metadata: os.stat_result) -> bool:
        return original_check(metadata) or os.path.samestat(metadata, nested_identity)

    with monkeypatch.context() as patch:
        patch.setattr(
            snapshot_module,
            "_metadata_is_reparse",
            mark_nested_as_reparse,
        )
        with pytest.raises(SandboxError, match="重解析点"):
            prepared.close()

    assert (snapshot_nested / "file.txt").read_text(encoding="utf-8") == "snapshot"
    assert prepared.closed is False
    prepared.close()
    assert not prepared.root.exists()


def test_destination_must_be_exclusive_and_outside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    existing = tmp_path / "existing"
    existing.mkdir()

    with pytest.raises(SandboxError, match="必须不存在"):
        prepare_snapshot(source.resolve(), existing.resolve())
    with pytest.raises(SandboxError, match="不能位于源根内部"):
        prepare_snapshot(source.resolve(), (source / "snapshot").resolve())


@pytest.mark.parametrize(
    ("limits", "files", "message"),
    [
        (
            SnapshotLimits(max_entries=1, max_file_bytes=10, max_total_bytes=20),
            (("a.txt", 1), ("b.txt", 1)),
            "条目数量",
        ),
        (
            SnapshotLimits(max_entries=10, max_file_bytes=2, max_total_bytes=10),
            (("large.txt", 3),),
            "单文件上限",
        ),
        (
            SnapshotLimits(max_entries=10, max_file_bytes=3, max_total_bytes=4),
            (("a.txt", 3), ("b.txt", 2)),
            "累计字节",
        ),
    ],
)
def test_capacity_failures_remove_partial_destination(
    tmp_path: Path,
    limits: SnapshotLimits,
    files: tuple[tuple[str, int], ...],
    message: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name, size in files:
        (source / name).write_bytes(b"x" * size)
    destination = tmp_path / "snapshot"

    with pytest.raises(SandboxError, match=message):
        prepare_snapshot(source.resolve(), destination.resolve(), limits=limits)

    assert not destination.exists()
    assert {path.name: path.read_bytes() for path in source.iterdir()} == {
        name: b"x" * size for name, size in files
    }


def test_snapshot_rejects_hard_link_and_cleans_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("shared", encoding="utf-8")
    try:
        os.link(outside, source / "linked.txt")
    except OSError:
        pytest.skip("hard links are unavailable")
    destination = tmp_path / "snapshot"

    with pytest.raises(SandboxError, match="硬链接"):
        prepare_snapshot(source.resolve(), destination.resolve())

    assert not destination.exists()
    assert outside.read_text(encoding="utf-8") == "shared"


def test_snapshot_rejects_reparse_entry_and_source_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = source / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(SandboxError, match="重解析点"):
        prepare_snapshot(source.resolve(), (tmp_path / "snapshot").resolve())
    with pytest.raises(SandboxError, match="源根"):
        prepare_snapshot(link.absolute(), (tmp_path / "other").resolve())


def test_concurrent_file_replacement_is_rejected_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "app.py"
    target.write_text("before\n", encoding="utf-8")
    replacement = tmp_path / "replacement.py"
    replacement.write_text("after\n", encoding="utf-8")
    destination = tmp_path / "snapshot"
    original_copy = snapshot_module._copy_file
    replaced = False

    def replace_before_open(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            os.replace(replacement, target)
            replaced = True
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_copy_file", replace_before_open)

    with pytest.raises(SandboxError, match="替换或变化"):
        prepare_snapshot(source.resolve(), destination.resolve())

    assert not destination.exists()
    assert target.read_text(encoding="utf-8") == "after\n"


def test_concurrent_directory_addition_is_rejected_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("content\n", encoding="utf-8")
    destination = tmp_path / "snapshot"
    original_copy = snapshot_module._copy_file
    added = False

    def add_after_copy(*args, **kwargs):
        nonlocal added
        result = original_copy(*args, **kwargs)
        if not added:
            (source / "added.py").write_text("new\n", encoding="utf-8")
            added = True
        return result

    monkeypatch.setattr(snapshot_module, "_copy_file", add_after_copy)

    with pytest.raises(SandboxError, match="源目录.*发生变化"):
        prepare_snapshot(source.resolve(), destination.resolve())

    assert not destination.exists()
    assert (source / "added.py").read_text(encoding="utf-8") == "new\n"


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SnapshotLimits(max_entries=0)
    with pytest.raises(ValueError, match="cannot exceed"):
        SnapshotLimits(max_file_bytes=10, max_total_bytes=5)


class _FakeWindowsSnapshotApi:
    def __init__(
        self,
        information: snapshot_module._WindowsHandleInformation,
        *,
        query_sequence: tuple[
            snapshot_module._WindowsHandleInformation,
            ...,
        ] = (),
        fail_open: bool = False,
        fail_query_calls: frozenset[int] = frozenset(),
        fail_close: bool = False,
        fail_to_fd: bool = False,
    ) -> None:
        self.information = information
        self.query_sequence = query_sequence
        self.fail_open = fail_open
        self.fail_query_calls = fail_query_calls
        self.fail_close = fail_close
        self.fail_to_fd = fail_to_fd
        self.events: list[tuple[object, ...]] = []
        self.live_handles: set[int] = set()
        self.handle_paths: dict[int, Path] = {}
        self.handle_share_modes: dict[int, int] = {}
        self.max_live_handles = 0
        self.open_count = 0
        self.query_count = 0

    def open_path(
        self,
        path: Path,
        *,
        share_mode: int,
        flags: int,
    ) -> int:
        self.events.append(("open", path, share_mode, flags))
        if self.fail_open:
            raise OSError("injected open failure")
        self.open_count += 1
        handle = self.open_count
        self.live_handles.add(handle)
        self.handle_paths[handle] = path
        self.handle_share_modes[handle] = share_mode
        self.max_live_handles = max(
            self.max_live_handles,
            len(self.live_handles),
        )
        return handle

    def query(
        self,
        handle: int,
    ) -> snapshot_module._WindowsHandleInformation:
        assert handle in self.live_handles
        self.query_count += 1
        self.events.append(("query", handle))
        if self.query_count in self.fail_query_calls:
            raise OSError("injected query failure")
        if self.query_sequence:
            index = min(self.query_count - 1, len(self.query_sequence) - 1)
            return self.query_sequence[index]
        return self.information

    def to_fd(self, handle: int) -> int:
        assert handle in self.live_handles
        self.events.append(("to_fd", handle))
        if self.fail_to_fd:
            raise OSError("injected descriptor conversion failure")
        self.live_handles.remove(handle)
        del self.handle_paths[handle]
        del self.handle_share_modes[handle]
        return 101

    def close(self, handle: int) -> None:
        assert handle in self.live_handles
        self.events.append(("close", handle))
        if self.fail_close:
            raise OSError("injected close failure")
        self.live_handles.remove(handle)
        del self.handle_paths[handle]
        del self.handle_share_modes[handle]

    def replace_path(self, path: Path) -> None:
        for handle in self.live_handles:
            if self.handle_paths[handle] == path and not (
                self.handle_share_modes[handle]
                & snapshot_module._WINDOWS_FILE_SHARE_DELETE
            ):
                raise PermissionError("injected Windows sharing violation")


def _directory_handle_information(
    *,
    attributes: int = snapshot_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY,
    link_count: int = 1,
    volume_serial: int = 17,
    file_index: int = 23,
) -> snapshot_module._WindowsHandleInformation:
    return snapshot_module._WindowsHandleInformation(
        file_attributes=attributes,
        link_count=link_count,
        volume_serial=volume_serial,
        file_index=file_index,
    )


def test_windows_directory_guards_cover_nested_traversal_without_delete_share(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    destination = tmp_path / "snapshot"
    destination.mkdir()
    api = _FakeWindowsSnapshotApi(_directory_handle_information())
    state = snapshot_module._BuildState(
        limits=SnapshotLimits(),
        entries=[],
    )

    snapshot_module._copy_directory(
        source,
        destination,
        PurePosixPath("."),
        state,
        windows_api=api,
    )

    open_events = [event for event in api.events if event[0] == "open"]
    assert len(open_events) == 2
    assert api.max_live_handles == 2
    assert not api.live_handles
    for _, _, share_mode, flags in open_events:
        assert isinstance(share_mode, int)
        assert isinstance(flags, int)
        assert share_mode & snapshot_module._WINDOWS_FILE_SHARE_DELETE == 0
        assert flags & snapshot_module._WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
        assert flags & snapshot_module._WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT


def test_windows_directory_guard_keeps_handle_until_scope_exit(
    tmp_path: Path,
) -> None:
    api = _FakeWindowsSnapshotApi(_directory_handle_information())
    guard = snapshot_module._WindowsDirectoryGuard(tmp_path, api=api)

    with guard:
        assert api.live_handles
        assert [event[0] for event in api.events] == ["open", "query"]

    assert not api.live_handles
    assert [event[0] for event in api.events] == [
        "open",
        "query",
        "query",
        "close",
    ]


def test_windows_directory_guard_rejects_open_failure(tmp_path: Path) -> None:
    api = _FakeWindowsSnapshotApi(
        _directory_handle_information(),
        fail_open=True,
    )

    with pytest.raises(SandboxError, match="锁定"):
        with snapshot_module._WindowsDirectoryGuard(tmp_path, api=api):
            pytest.fail("directory guard entered after an injected open failure")

    assert not api.live_handles


@pytest.mark.parametrize(
    ("fail_query_calls", "message"),
    [
        (frozenset({1}), "验证"),
        (frozenset({2}), "查询"),
    ],
)
def test_windows_directory_guard_rejects_query_failure_and_closes(
    tmp_path: Path,
    fail_query_calls: frozenset[int],
    message: str,
) -> None:
    api = _FakeWindowsSnapshotApi(
        _directory_handle_information(),
        fail_query_calls=fail_query_calls,
    )

    with pytest.raises(SandboxError, match=message):
        with snapshot_module._WindowsDirectoryGuard(tmp_path, api=api):
            pass

    assert not api.live_handles
    assert any(event[0] == "close" for event in api.events)


def test_windows_directory_guard_rejects_close_failure(tmp_path: Path) -> None:
    api = _FakeWindowsSnapshotApi(
        _directory_handle_information(),
        fail_close=True,
    )

    with pytest.raises(SandboxError, match="关闭"):
        with snapshot_module._WindowsDirectoryGuard(tmp_path, api=api):
            pass

    assert api.live_handles


@pytest.mark.parametrize(
    "information",
    [
        _directory_handle_information(attributes=0),
        _directory_handle_information(
            attributes=(
                snapshot_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY
                | snapshot_module._REPARSE_POINT_ATTRIBUTE
            )
        ),
        _directory_handle_information(link_count=0),
        _directory_handle_information(file_index=0),
    ],
    ids=("not-directory", "reparse", "invalid-link", "invalid-identity"),
)
def test_windows_directory_guard_rejects_invalid_handle_metadata(
    tmp_path: Path,
    information: snapshot_module._WindowsHandleInformation,
) -> None:
    api = _FakeWindowsSnapshotApi(information)

    with pytest.raises(SandboxError, match="类型、链接或身份无效"):
        with snapshot_module._WindowsDirectoryGuard(tmp_path, api=api):
            pytest.fail("directory guard accepted invalid metadata")

    assert not api.live_handles


def test_windows_directory_guard_rejects_identity_change(
    tmp_path: Path,
) -> None:
    before = _directory_handle_information()
    after = _directory_handle_information(file_index=before.file_index + 1)
    api = _FakeWindowsSnapshotApi(
        before,
        query_sequence=(before, after),
    )

    with pytest.raises(SandboxError, match="发生变化"):
        with snapshot_module._WindowsDirectoryGuard(tmp_path, api=api):
            pass

    assert not api.live_handles


def test_windows_file_no_follow_handle_disables_delete_share(
    tmp_path: Path,
) -> None:
    information = snapshot_module._WindowsHandleInformation(
        file_attributes=0,
        link_count=1,
        volume_serial=17,
        file_index=31,
    )
    api = _FakeWindowsSnapshotApi(information)

    descriptor = snapshot_module._open_windows_source_no_follow(
        tmp_path / "file.txt",
        api=api,
    )

    assert descriptor == 101
    assert not api.live_handles
    open_event = api.events[0]
    share_mode = open_event[2]
    flags = open_event[3]
    assert isinstance(share_mode, int)
    assert isinstance(flags, int)
    assert share_mode & snapshot_module._WINDOWS_FILE_SHARE_DELETE == 0
    assert flags & snapshot_module._WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT


def test_windows_file_no_follow_close_failure_is_fail_closed(
    tmp_path: Path,
) -> None:
    information = snapshot_module._WindowsHandleInformation(
        file_attributes=0,
        link_count=2,
        volume_serial=17,
        file_index=31,
    )
    api = _FakeWindowsSnapshotApi(information, fail_close=True)

    with pytest.raises(SandboxError, match="关闭"):
        snapshot_module._open_windows_source_no_follow(
            tmp_path / "file.txt",
            api=api,
        )


def test_windows_cleanup_holds_nested_guards_until_children_are_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    nested = root / "nested"
    nested.mkdir(parents=True)
    snapshot_file = nested / "file.txt"
    snapshot_file.write_text("snapshot", encoding="utf-8")
    root_identity = root.lstat()
    api = _FakeWindowsSnapshotApi(_directory_handle_information())
    original_unlink = snapshot_module.os.unlink
    original_rmdir = snapshot_module.os.rmdir
    replacement_checks: list[Path] = []
    rmdir_handle_counts: list[tuple[Path, int]] = []

    def guarded_unlink(path: Path) -> None:
        assert Path(path) == snapshot_file
        for locked_directory in (root, nested):
            with pytest.raises(PermissionError, match="sharing violation"):
                api.replace_path(locked_directory)
            replacement_checks.append(locked_directory)
        original_unlink(path)

    def tracked_rmdir(path: Path) -> None:
        resolved = Path(path)
        rmdir_handle_counts.append((resolved, len(api.live_handles)))
        original_rmdir(path)

    monkeypatch.setattr(snapshot_module.os, "unlink", guarded_unlink)
    monkeypatch.setattr(snapshot_module.os, "rmdir", tracked_rmdir)

    snapshot_module._remove_owned_directory(
        root,
        root_identity,
        is_root=True,
        windows_api=api,
    )

    assert replacement_checks == [root, nested]
    assert api.max_live_handles == 2
    assert not api.live_handles
    assert rmdir_handle_counts == [(nested, 1), (root, 0)]
    assert not root.exists()
    for event in api.events:
        if event[0] != "open":
            continue
        share_mode = event[2]
        assert isinstance(share_mode, int)
        assert share_mode & snapshot_module._WINDOWS_FILE_SHARE_DELETE == 0


def test_windows_directory_guard_blocks_real_junction_replacement(
    tmp_path: Path,
) -> None:
    required = os.environ.get("SANDBOX_REQUIRED") == "1"
    if os.name != "nt":
        if required:
            pytest.fail("mandatory security runner must use Windows")
        pytest.skip("requires Windows directory sharing")

    source = tmp_path / "source"
    source.mkdir()
    parked = tmp_path / "parked"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "must-remain.txt").write_text("outside", encoding="utf-8")
    junction = tmp_path / "replacement-junction"
    command = [
        os.environ.get("COMSPEC", "cmd.exe"),
        "/d",
        "/c",
        "mklink",
        "/J",
        str(junction),
        str(outside),
    ]
    creation = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if creation.returncode != 0 or not snapshot_module._is_reparse(junction):
        if required:
            pytest.fail("mandatory security runner cannot create a Windows junction")
        pytest.skip("creating a local Windows junction is unavailable")

    rename_succeeded = False
    with snapshot_module._WindowsDirectoryGuard(source):
        try:
            source.rename(parked)
        except OSError:
            pass
        else:
            rename_succeeded = True
            parked.rename(source)
    if rename_succeeded:
        if required:
            pytest.fail(
                "mandatory security runner did not enforce the directory "
                "delete-share lock"
            )
        pytest.skip("filesystem did not enforce the directory delete-share lock")

    source.rename(parked)
    junction.rename(source)
    assert snapshot_module._is_reparse(source)
    source.rename(junction)
    parked.rename(source)
    assert (outside / "must-remain.txt").read_text(encoding="utf-8") == "outside"
