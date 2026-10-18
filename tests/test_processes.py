"""A command must not leave descendants running after its execution boundary."""

import sys
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from neil_agent.errors import ToolError
from neil_agent.execution_budget import execution_budget_scope
from neil_agent.process_tree import ProcessTree
from neil_agent.processes import run_bounded
from neil_agent.tools.shell import ShellTools


def _wait_for(path: Path) -> None:
    deadline = monotonic() + 10
    while not path.exists():
        if monotonic() >= deadline:
            raise RuntimeError("Owned child did not start")
        sleep(0.01)


@pytest.mark.parametrize("stop", ["completed", "timeout", "output_limit", "cancel"])
def test_entire_tree_stops_on_every_exit(tmp_path: Path, stop: str) -> None:
    marker = tmp_path / "heartbeat"
    child = tmp_path / "child.py"
    child.write_text(
        "import time\nfrom pathlib import Path\n"
        f"marker = Path({str(marker)!r})\n"
        "for count in range(200):\n"
        "    marker.write_text(str(count))\n"
        "    time.sleep(0.03)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-I', '-S', {str(child)!r}])\n"
        f"while not Path({str(marker)!r}).exists(): time.sleep(0.01)\n"
        + ("print('x' * 100000, flush=True)\n" if stop == "output_limit" else "")
        + ("time.sleep(10)\n" if stop != "completed" else ""),
        encoding="utf-8",
    )
    cancel = Event()
    thread = None
    if stop == "cancel":
        def request_cancel():
            _wait_for(marker)
            cancel.set()

        thread = Thread(target=request_cancel)
        thread.start()
    try:
        with execution_budget_scope(cancel=cancel, cancelled_message="owned test cancelled"):
            if stop == "cancel":
                with pytest.raises(ToolError, match="owned test cancelled"):
                    run_bounded((sys.executable, "-I", str(parent)), cwd=tmp_path, timeout=5)
            else:
                result = run_bounded(
                    (sys.executable, "-I", str(parent)), cwd=tmp_path,
                    timeout=1.5 if stop == "timeout" else 5, max_output_bytes=1024,
                )
                assert result.reason == stop
        assert marker.exists()
        sleep(0.08)
        stopped_at = marker.read_text()
        sleep(0.15)
        assert marker.read_text() == stopped_at
    finally:
        if thread is not None:
            thread.join(10)


def test_assignment_failure_never_starts_requested_command(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "must-not-exist"

    def fail_assignment(*_):
        raise OSError("test assignment rejection")

    monkeypatch.setattr(ProcessTree, "attach", fail_assignment)
    result = run_bounded(
        (sys.executable, "-I", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
        cwd=tmp_path, timeout=5,
    )
    assert result.reason == "unavailable"
    # Only Windows needs an assignment gate; POSIX starts directly in its group.
    if sys.platform == "win32":
        assert not marker.exists()


def test_cancelled_budget_does_not_start_a_command(tmp_path: Path) -> None:
    cancel = Event()
    cancel.set()
    marker = tmp_path / "must-not-exist"
    with execution_budget_scope(cancel=cancel):
        with pytest.raises(ToolError):
            run_bounded(
                (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
                cwd=tmp_path, timeout=5,
            )
    assert not marker.exists()


def test_stdout_and_stderr_share_a_retention_limit(tmp_path: Path) -> None:
    result = run_bounded(
        (sys.executable, "-I", "-c", "import sys; print('o'*800); print('e'*800, file=sys.stderr)"),
        cwd=tmp_path, timeout=5, max_output_bytes=1024, merge_stderr=False,
    )
    assert result.reason == "output_limit"
    assert len((result.output + result.stderr).encode()) <= 1024


def test_quality_check_observes_execution_cancel(tmp_path: Path, monkeypatch) -> None:
    cancel = Event()
    marker = tmp_path / "started"
    command = [sys.executable, "-I", "-c", f"import time; from pathlib import Path; Path({str(marker)!r}).touch(); time.sleep(10)"]
    monkeypatch.setattr(ShellTools, "_quality_command", staticmethod(lambda _: command))

    def request_cancel():
        _wait_for(marker)
        cancel.set()

    thread = Thread(target=request_cancel)
    thread.start()
    try:
        with execution_budget_scope(cancel=cancel, cancelled_message="quality check cancelled"):
            with pytest.raises(ToolError, match="quality check cancelled"):
                ShellTools(tmp_path).run_quality_check("pytest")
    finally:
        thread.join(10)


def test_cleanup_error_does_not_hide_cancellation(tmp_path: Path, monkeypatch) -> None:
    cancel = Event()
    marker = tmp_path / "started"
    original_close = ProcessTree.close

    def close(tree):
        original_close(tree)
        raise OSError("synthetic cleanup failure after stopping owned processes")

    monkeypatch.setattr(ProcessTree, "close", close)

    def request_cancel():
        _wait_for(marker)
        cancel.set()

    thread = Thread(target=request_cancel)
    thread.start()
    try:
        with execution_budget_scope(cancel=cancel, cancelled_message="original cancellation"):
            with pytest.raises(ToolError, match="original cancellation") as error:
                run_bounded(
                    (sys.executable, "-I", "-c", f"import time; from pathlib import Path; Path({str(marker)!r}).touch(); time.sleep(10)"),
                    cwd=tmp_path, timeout=5,
                )
        assert "命令进程树清理失败。" in error.value.__notes__
    finally:
        thread.join(10)
