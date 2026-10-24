"""Shared fixtures use the sole runtime; process and warning gates stay global."""

import logging
import warnings

import pytest

from tests import _hygiene
from tests.operator_native_fixture import operator_native as operator_native
from tests.runtime_fixture import WEBHOOK_SECRET

for name in ("aiosqlite", "LiteLLM", "litellm", "asyncio"):
    logging.getLogger(name).setLevel(logging.WARNING)

TEST_WEBHOOK_SECRET = WEBHOOK_SECRET


@pytest.fixture(autouse=True)
def _fail_on_unawaited_coroutines():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "error", message=r"coroutine .* was never awaited", category=RuntimeWarning
        )
        yield


@pytest.fixture
def test_settings(operator_native):
    return operator_native.config


@pytest.fixture
async def app(operator_native):
    yield operator_native.app


@pytest.fixture
async def client(operator_native):
    yield operator_native.client


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark tiers by path: ``slow`` (real processes) and ``invariant`` (DESIGN §4)."""
    for item in items:
        for tier in sorted(_hygiene.tiers_for(item.nodeid)):
            item.add_marker(tier)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """After fixture teardown, kill leaked child processes; fail an otherwise green run."""
    leaked = _hygiene.reap_and_kill_leaks()
    if not leaked:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_sep("!", "test hygiene: killed leaked child processes", red=True)
        for pid, command in leaked.items():
            reporter.write_line(f"  {pid} {command}", red=True)
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
