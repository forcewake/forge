"""Lifecycle for a lane-local ``opencode serve`` process.

The OpenCode driver client (``opencode.py``) assumes a running server —
server ownership belongs to the LANE, not the HTTP client (the research
doc's §10.1 posture). This module is that lane-side ownership:

- :class:`OpenCodeServer` — an async context manager that spawns
  ``opencode serve`` bound to loopback, waits for readiness by polling
  the served spec (``/doc`` — the same surface :class:`SpecProbe`
  reads), and tears the process down deterministically (SIGTERM, then
  SIGKILL after a grace period). The port is chosen by binding a probe
  socket first, so two lanes never race for the default 4096.
- :func:`opencode_server_from_env` — the factory reading ``OPENCODE_BINARY``
  and ``OPENCODE_SERVE_HOSTNAME``.

The process seam is injectable: ``spawn`` is a callable returning an
``asyncio.subprocess.Process``; tests drive a fake. No test ever spawns
a real server.

Auth: opencode v2.0.10 enforces a server password ALWAYS — when
``OPENCODE_SERVER_PASSWORD`` is unset in the child env it GENERATES a
random one and prints it to stdout (LIVE-verified 2026-09-21), which a
DEVNULL lane would lose. The spawner therefore generates a strong
password itself and passes it via the child environment (an explicit
``OPENCODE_SERVER_PASSWORD`` in :attr:`env` is respected — the
operator override), and exposes it as :attr:`password` so the client
factory can authenticate. The listener stays loopback-bound; the
password is the second fence.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx

__all__ = ["OpenCodeServer", "OpenCodeServerError", "opencode_server_from_env"]

#: How long readiness polling may take overall before the spawn fails.
_READY_TIMEOUT_S = 30.0

#: Grace period after SIGTERM before escalating to SIGKILL.
_TERM_GRACE_S = 5.0


class OpenCodeServerError(RuntimeError):
    """The serve process failed to spawn or become ready."""


def _pick_free_port() -> int:
    """A loopback port the OS confirms free (probe-socket release race
    is bounded and the server fails loudly if it loses the port)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class OpenCodeServer:
    """One lane-local ``opencode serve`` process bound to loopback.

    Use as an async context manager::

        async with OpenCodeServer() as server:
            client = opencode_client_from_env(
                env={"OPENCODE_SERVER_URL": server.url, ...}
            )

    ``url`` is only valid inside the context. Readiness is polled on
    the SPEC surface (``/doc``) — the same route :class:`SpecProbe`
    uses — so "ready" means what the client will actually rely on.
    The server never gets the caller's stdio; its output is discarded
    (stderr is the serve diagnostics surface — the lane captures it
    out-of-band if it needs to).
    """

    binary: str = "opencode"
    hostname: str = "127.0.0.1"
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    ready_timeout_s: float = _READY_TIMEOUT_S
    term_grace_s: float = _TERM_GRACE_S
    #: Injectable process seam (tests pass a fake); production default
    #: spawns the real subprocess.
    spawn: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None
    #: Injectable clock for tests.
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    _process: asyncio.subprocess.Process | None = None
    _port: int | None = None
    _started_at: float | None = None
    _password: str | None = None

    @property
    def url(self) -> str:
        """The base URL of the running server (only valid while started)."""
        if self._port is None:
            raise OpenCodeServerError("server is not running — use the async context manager")
        return f"http://{self.hostname}:{self._port}"

    @property
    def password(self) -> str:
        """The server password (operator-supplied or spawner-generated)."""
        if self._password is None:
            raise OpenCodeServerError("server is not running — use the async context manager")
        return self._password

    @property
    def ready_seconds(self) -> float | None:
        """Seconds from start() to readiness (evidence for the lane operator)."""
        if self._started_at is None:
            return None
        return time.monotonic() - self._started_at

    async def __aenter__(self) -> OpenCodeServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Spawn the server and block until it serves its spec."""
        if self._process is not None:
            raise OpenCodeServerError("server already started")
        self._port = _pick_free_port()
        self._started_at = time.monotonic()
        # v2.0.10 always enforces a password; an unset one is randomly
        # generated and printed to stdout the lane cannot read. Own it.
        child_env = dict(self.env) if self.env is not None else {}
        self._password = child_env.setdefault("OPENCODE_SERVER_PASSWORD", secrets.token_urlsafe(24))
        spawn = self.spawn or self._spawn_real
        self._process = await spawn(
            self.binary,
            self._port,
            cwd=self.cwd,
            env=child_env,
        )
        await self._wait_ready()

    async def stop(self) -> None:
        """Terminate the process: SIGTERM, grace, then SIGKILL."""
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self.term_grace_s)
        except TimeoutError:
            process.kill()
            await process.wait()
        self._port = None
        self._password = None

    async def _spawn_real(
        self, binary: str, port: int, *, cwd: str | None, env: dict[str, str] | None
    ) -> asyncio.subprocess.Process:
        # ``env`` carries OVERRIDES (the password among them), not a full
        # replacement — replacing wholesale drops PATH and breaks the
        # binary lookup (LIVE-found: FileNotFoundError on macOS).
        merged = {**os.environ, **(env or {})}
        return await asyncio.create_subprocess_exec(
            binary,
            "serve",
            "--hostname",
            self.hostname,
            "--port",
            str(port),
            cwd=cwd,
            env=merged,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _wait_ready(self) -> None:
        """Poll the spec surface until it answers, within the budget.

        Any HTTP answer — 200 with a spec, a redirect, even an auth
        challenge — proves the listener is up; only transport errors
        keep polling. A process that dies while polling fails fast with
        its exit code.
        """
        deadline = time.monotonic() + self.ready_timeout_s
        url = f"{self.url}/doc"
        last_error: str = "not polled"
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=2.0)) as probe:
            while time.monotonic() < deadline:
                process = self._process
                if process is not None and process.returncode is not None:
                    await self.stop()
                    raise OpenCodeServerError(
                        f"opencode serve exited with {process.returncode} before becoming ready"
                    )
                try:
                    await probe.get(url)
                    return
                except httpx.HTTPError as error:
                    last_error = f"{type(error).__name__}: {error}"
                await self._sleep(0.25)
        await self.stop()
        raise OpenCodeServerError(
            f"opencode serve did not serve {url!r} within {self.ready_timeout_s}s ({last_error})"
        )


def opencode_server_from_env(
    env: Mapping[str, str] | None = None,
) -> OpenCodeServer:
    """Build the server handle from the documented environment.

    - ``OPENCODE_BINARY`` — the opencode executable (default ``opencode``).
    - ``OPENCODE_SERVE_HOSTNAME`` — bind address (default ``127.0.0.1``;
      the lane NEVER binds a public interface).
    - ``OPENCODE_SERVE_CWD`` — the lane working directory for the server
      (sessions inherit it when a session directory is not pinned).
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return OpenCodeServer(
        binary=source.get("OPENCODE_BINARY", "opencode"),
        hostname=source.get("OPENCODE_SERVE_HOSTNAME", "127.0.0.1"),
        cwd=source.get("OPENCODE_SERVE_CWD") or None,
    )
