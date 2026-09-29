"""A guarded test helper exits when the process that spawned it is killed."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

GUARD_DIR = Path(__file__).resolve().parent / "production_entry"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.05)


def test_a_guarded_child_exits_when_its_parent_is_killed(tmp_path):
    pid_file = tmp_path / "child.pid"
    child_code = (
        f"import os, sys, time; sys.path.insert(0, {str(GUARD_DIR)!r}); "
        "import orphan_guard; orphan_guard.install(0.1); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    child = None
    try:
        _wait_for(lambda: pid_file.is_file() and pid_file.read_text(), 15, "child never started")
        child = int(pid_file.read_text())
        time.sleep(0.3)  # several guard polls: the guard must not exit on its own
        assert _alive(child), "the guard exited while its parent was alive"
        parent.kill()
        parent.wait(timeout=10)
        _wait_for(lambda: not _alive(child), 10, "the orphaned child outlived its parent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
