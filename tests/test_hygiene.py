"""Tier markers by path, and the leaked-process check (tests/_hygiene.py)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest

from tests import _hygiene

needs_pgrep = pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep is unavailable")


def test_process_heavy_suites_are_slow():
    assert "slow" in _hygiene.tiers_for("tests/production_entry/test_x.py::test_a")
    assert "slow" in _hygiene.tiers_for("tests/fi_os/test_y.py::test_b")
    assert "slow" in _hygiene.tiers_for("tests/test_failure_injection_os.py::test_c")


def test_ordinary_tests_have_no_tier():
    assert _hygiene.tiers_for("tests/test_ordinary.py::TestThing::test_a") == set()


@needs_pgrep
def test_a_running_child_is_killed_and_reported():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert child.pid in _hygiene.reap_and_kill_leaks()
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@needs_pgrep
def test_a_finished_child_is_reaped_not_reported():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(1.0)  # let it exit without being waited on (a zombie)
    assert child.pid not in _hygiene.reap_and_kill_leaks()
    child.wait(timeout=10)
