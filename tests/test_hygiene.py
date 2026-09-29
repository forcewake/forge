"""Tier markers by path, and the leaked-process check (tests/_hygiene.py)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from tests import _hygiene

needs_pgrep = pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep is unavailable")
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_process_heavy_suites_are_slow():
    assert "slow" in _hygiene.tiers_for("tests/production_entry/test_x.py::test_a")
    assert "slow" in _hygiene.tiers_for("tests/fi_os/test_y.py::test_b")
    assert "slow" in _hygiene.tiers_for("tests/test_failure_injection_os.py::test_c")


def test_ordinary_tests_have_no_tier():
    assert _hygiene.tiers_for("tests/test_ordinary.py::TestThing::test_a") == set()


@needs_pgrep
def test_a_running_child_is_killed_and_reported_with_its_command():
    child = subprocess.Popen(SLEEPER)
    try:
        leaked = _hygiene.reap_and_kill_leaks(only={child.pid})
        assert list(leaked) == [child.pid]
        assert "time.sleep(60)" in leaked[child.pid]
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@needs_pgrep
def test_a_finished_child_is_reaped_not_reported():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)  # exited, not yet reaped
    assert _hygiene.reap_and_kill_leaks(only={child.pid}) == {}
    child.wait(timeout=10)


@needs_pgrep
def test_children_outside_the_filter_are_left_alone():
    bystander = subprocess.Popen(SLEEPER)
    try:
        assert _hygiene.reap_and_kill_leaks(only=set()) == {}
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(timeout=10)
