"""Test-suite hygiene: tier markers and leaked-process detection (DESIGN §7)."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from collections.abc import Collection

from tests.invariants.registry import INVARIANT_FILES

SLOW_PREFIXES = ("tests/production_entry/", "tests/fi_os/")
SLOW_FILES = frozenset({"tests/test_failure_injection_os.py"})


def tiers_for(nodeid: str) -> set[str]:
    """The tier markers a collected test carries, from its file path."""
    path = nodeid.split("::", 1)[0]
    tiers = set()
    if path.startswith(SLOW_PREFIXES) or path in SLOW_FILES:
        tiers.add("slow")
    if path.startswith("tests/invariants/") or path in INVARIANT_FILES:
        tiers.add("invariant")
    return tiers


def child_pids(pid: int | None = None) -> list[int]:
    """Direct children of ``pid`` (default: this process); empty without ``pgrep``."""
    if shutil.which("pgrep") is None:
        return []
    result = subprocess.run(
        ["pgrep", "-P", str(pid or os.getpid())], capture_output=True, text=True, check=False
    )
    return [int(p) for p in result.stdout.split()]


def command_lines(pids: Collection[int]) -> dict[int, str]:
    """``pid → command line`` for running processes, read before anything is killed."""
    if not pids:
        return {}
    result = subprocess.run(
        ["ps", "-o", "pid=,args=", "-p", ",".join(str(p) for p in pids)],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = (line.strip().split(maxsplit=1) for line in result.stdout.splitlines())
    return {int(parts[0]): (parts[1] if len(parts) > 1 else "") for parts in lines if parts}


def reap_and_kill_leaks(only: Collection[int] | None = None) -> dict[int, str]:
    """Reap finished children; kill and reap the running ones.

    Returns ``pid → command line`` of the killed processes. ``only`` limits the
    check to those pids, so a unit test never touches another test's children.
    """
    running = []
    for pid in child_pids():
        if only is not None and pid not in only:
            continue
        try:
            finished, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if finished == 0:
            running.append(pid)
    leaked = command_lines(running)
    for pid in running:
        with contextlib.suppress(ProcessLookupError, ChildProcessError):
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        leaked.setdefault(pid, "")
    return leaked
