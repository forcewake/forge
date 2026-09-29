"""Test-suite hygiene: tier markers and leaked-process detection (DESIGN §7)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess

SLOW_PREFIXES = ("tests/production_entry/", "tests/fi_os/")
SLOW_FILES = frozenset({"tests/test_failure_injection_os.py"})


def tiers_for(nodeid: str) -> set[str]:
    """The tier markers a collected test carries, from its file path."""
    path = nodeid.split("::", 1)[0]
    tiers = set()
    if path.startswith(SLOW_PREFIXES) or path in SLOW_FILES:
        tiers.add("slow")
    return tiers


def child_pids(pid: int | None = None) -> list[int]:
    if shutil.which("pgrep") is None:
        return []
    result = subprocess.run(
        ["pgrep", "-P", str(pid or os.getpid())], capture_output=True, text=True, check=False
    )
    return [int(p) for p in result.stdout.split()]


def reap_and_kill_leaks() -> list[int]:
    """Reap finished children; kill, reap and return the ones still running."""
    leaked = []
    for pid in child_pids():
        try:
            finished, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if finished == 0:
            leaked.append(pid)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    return leaked
