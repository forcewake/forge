"""Tests for the lane-local ``opencode serve`` spawner.

The process seam is faked (no real server ever spawns); readiness is
driven through pytest-httpx — the spawner polls ``/doc`` with its own
internal client, which the transport-level mock covers. The v2.0.10
password doctrine (always pin one — an unset one is randomly generated
and printed to stdout the DEVNULL lane cannot read) is pinned here.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from forge.adaptive.drivers.opencode_serve import (
    OpenCodeServer,
    OpenCodeServerError,
    opencode_server_from_env,
)


class FakeProcess:
    """An ``asyncio.subprocess.Process`` stand-in with scriptable exit."""

    def __init__(self, *, resist_term: bool = False) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self._resist_term = resist_term
        self._terminated = asyncio.Event()
        self._killed = False

    def terminate(self) -> None:
        self._terminated.set()
        if not self._resist_term:
            self.returncode = 0

    def kill(self) -> None:
        self._killed = True
        self.returncode = 9

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


# Polling tests fire the refusal callback a variable number of times; the
# mock must not assert that every registered response was consumed.
pytestmark = pytest.mark.httpx_mock(assert_all_requests_were_expected=False)


def _refuse(request: httpx.Request) -> httpx.Response:
    """A reusable transport-level refusal (pytest-httpx callbacks are
    unlimited; add_exception is single-shot and trips teardown)."""
    raise httpx.ConnectError("refused")


def _server_with_fake_spawn(
    *, resist_term: bool = False, **kwargs: object
) -> tuple[OpenCodeServer, dict, list[FakeProcess]]:
    """A server whose spawn seam records calls and returns FakeProcesses."""
    spawned: dict = {}
    processes: list[FakeProcess] = []

    async def fake_spawn(binary: str, port: int, *, cwd, env):  # noqa: ANN001, ANN202
        spawned["binary"] = binary
        spawned["port"] = port
        spawned["cwd"] = cwd
        spawned["env"] = dict(env or {})
        process = FakeProcess(resist_term=resist_term)
        processes.append(process)
        return process

    kwargs["spawn"] = fake_spawn
    return OpenCodeServer(**kwargs), spawned, processes


async def _no_sleep(_: float) -> None:
    """Zero-time sleep that still yields — a truly instant return would
    starve the event loop (the poller would never let the test's timer
    run, and the deadline would pass first)."""
    await asyncio.sleep(0)


async def test_start_spawns_with_generated_password_and_becomes_ready(httpx_mock) -> None:
    httpx_mock.add_response(status_code=401)  # auth challenge proves the listener
    server, spawned, _ = _server_with_fake_spawn(binary="opencode", hostname="127.0.0.1")

    await server.start()

    assert spawned["binary"] == "opencode"
    assert spawned["port"] > 0
    assert server.url == f"http://127.0.0.1:{spawned['port']}"
    password = spawned["env"]["OPENCODE_SERVER_PASSWORD"]
    assert password and server.password == password
    assert server.ready_seconds is not None and server.ready_seconds >= 0
    await server.stop()


async def test_operator_supplied_password_is_respected(httpx_mock) -> None:
    httpx_mock.add_response(status_code=401)
    server, spawned, _ = _server_with_fake_spawn(
        binary="opencode", env={"OPENCODE_SERVER_PASSWORD": "operator-pw"}
    )

    await server.start()

    assert spawned["env"]["OPENCODE_SERVER_PASSWORD"] == "operator-pw"
    assert server.password == "operator-pw"
    await server.stop()


async def test_readiness_polls_through_connection_refusals(httpx_mock) -> None:
    httpx_mock.add_callback(_refuse)
    httpx_mock.add_callback(_refuse)
    httpx_mock.add_response(status_code=200)
    server, _, _ = _server_with_fake_spawn(binary="opencode")
    server._sleep = _no_sleep  # noqa: SLF001 — the injectable clock seam

    await server.start()
    await server.stop()


async def test_dead_process_fails_start_with_exit_code(httpx_mock) -> None:
    httpx_mock.add_callback(_refuse)
    server, _, processes = _server_with_fake_spawn(binary="opencode", ready_timeout_s=2.0)
    server._sleep = _no_sleep  # noqa: SLF001

    task = asyncio.create_task(server.start())
    await asyncio.sleep(0.02)
    assert len(processes) == 1  # spawn happened inside start()
    processes[0].returncode = 7  # the server died mid-poll

    with pytest.raises(OpenCodeServerError, match="exited with 7"):
        await task


async def test_never_ready_times_out_fail_closed(httpx_mock) -> None:
    httpx_mock.add_callback(_refuse)
    server, _, _ = _server_with_fake_spawn(binary="opencode", ready_timeout_s=0.05)
    server._sleep = _no_sleep  # noqa: SLF001

    with pytest.raises(OpenCodeServerError, match="did not serve"):
        await server.start()


async def test_stop_escalates_to_kill_after_grace(httpx_mock) -> None:
    httpx_mock.add_response(status_code=401)
    server, _, processes = _server_with_fake_spawn(
        binary="opencode", resist_term=True, term_grace_s=0.05
    )

    await server.start()
    process = processes[0]
    await server.stop()

    assert process._killed is True  # noqa: SLF001
    assert process.returncode == 9


async def test_stop_terminates_cleanly_and_clears_state(httpx_mock) -> None:
    httpx_mock.add_response(status_code=401)
    server, _, processes = _server_with_fake_spawn(binary="opencode")

    await server.start()
    process = processes[0]
    await server.stop()

    assert process.returncode == 0
    with pytest.raises(OpenCodeServerError):
        _ = server.url
    with pytest.raises(OpenCodeServerError):
        _ = server.password
    # Idempotent: a second stop is a no-op.
    await server.stop()


async def test_context_manager_starts_and_stops(httpx_mock) -> None:
    httpx_mock.add_response(status_code=401)
    server, _, processes = _server_with_fake_spawn(binary="opencode")

    async with server:
        assert server.url.startswith("http://127.0.0.1:")

    assert processes[0].returncode == 0


async def test_double_start_is_refused(httpx_mock) -> None:
    httpx_mock.add_response(status_code=401)
    server, _, _ = _server_with_fake_spawn(binary="opencode")

    await server.start()
    with pytest.raises(OpenCodeServerError, match="already started"):
        await server.start()
    await server.stop()


async def test_spawn_receives_env_overrides_with_the_password(httpx_mock) -> None:
    """The observable contract: the spawn seam gets an OVERRIES dict that
    carries at least the password (never None) — the os.environ merge
    happens inside the production seam only."""
    httpx_mock.add_response(status_code=401)
    server, spawned, _ = _server_with_fake_spawn(binary="opencode")

    await server.start()

    assert isinstance(spawned["env"], dict)
    assert "OPENCODE_SERVER_PASSWORD" in spawned["env"]
    await server.stop()


def test_factory_reads_the_documented_environment() -> None:
    server = opencode_server_from_env(
        {
            "OPENCODE_BINARY": "/opt/opencode/bin/opencode",
            "OPENCODE_SERVE_HOSTNAME": "127.0.0.2",
            "OPENCODE_SERVE_CWD": "/lane/work",
        }
    )
    assert server.binary == "/opt/opencode/bin/opencode"
    assert server.hostname == "127.0.0.2"
    assert server.cwd == "/lane/work"

    default = opencode_server_from_env(env={})
    assert default.binary == "opencode"
    assert default.hostname == "127.0.0.1"
    assert default.cwd is None


def test_url_before_start_is_refused() -> None:
    with pytest.raises(OpenCodeServerError, match="not running"):
        _ = OpenCodeServer().url
