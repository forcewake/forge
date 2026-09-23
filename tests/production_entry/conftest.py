"""Shared production-entry fixtures (Q35-09).

The layer's discipline: every collaborator a CUSTOMER drives is REAL —

- provider writes travel REAL HTTP from the REAL
  :class:`forge.integrations.github.GitHubClient` to the fake NATIVE
  server (a separate process whose state survives worker death);
- durable state lives in a REAL database (an aiosqlite FILE so a
  "restarted worker" is a genuinely fresh engine/session factory over
  the same rows; real PostgreSQL when ``FORGE_PG_TEST_URL`` is set);
- the lane runs as a REAL subprocess (``python -m forge.lane_driver
  --driver codex``) whose vendor is the controlled fake_vendor
  executable on the REAL codex app-server wire;
- the collector runs as the SHIPPED subprocess
  (``python -m forge.harness_entry --collect-candidate``);
- the control plane (lane control + checkpoint channel routers — the
  same ASGI routers ``forge.main.create_app`` mounts) serves REAL
  HTTP through uvicorn on a loopback port.

Every fixture cleans up its processes, engines and ports; nothing is
suppressed globally.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from uvicorn import Config, Server

from forge.config import ForgeConfig, Settings
from forge.integrations.github import GitHubClient
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.runs.github_service import GitHubRunService
from forge.runs.stubs import StubImplementer, StubPlanner

# ----------------------------------------------------------------------
# The layer's shared vocabulary
# ----------------------------------------------------------------------

#: The repository identity every PE trace runs against.
PE_REPO = "acme/forge-pe"
PE_OWNER, PE_REPO_NAME = PE_REPO.split("/", 1)
PE_BASE_BRANCH = "main"
PE_BASE_SHA = "b" * 40

#: The lane-control shared secret (the HMAC key work-scoped lane tokens
#: and the checkpoint channel bearer derive from).
PE_LANE_SECRET = "pe-lane-secret"  # noqa: S105 — a test fixture value

#: The workflow filename the dispatches name (``ci_harness`` backend).
PE_WORKFLOW = "forge-harness.github.yml"
PE_HARNESS_MODEL = "glm-5.3-flash[1m]"

#: A migration window far in the future: the LEGACY work-scoped token
#: (the spelling the lane fixtures mint) verifies inside it.
PE_LEGACY_DEADLINE = "2099-01-01T00:00:00+00:00"

PACKAGE_DIR = Path(__file__).parent
FAKE_NATIVE_SERVER = PACKAGE_DIR / "fake_native_server.py"
FAKE_VENDOR = PACKAGE_DIR / "fake_vendor.py"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ----------------------------------------------------------------------
# The fake native server (a separate PROCESS; state survives worker death)
# ----------------------------------------------------------------------


class FakeNative:
    """The typed control client over the fake native server's ``/__ctl``."""

    def __init__(self, process: subprocess.Popen, port: int) -> None:  # noqa: SIM115
        self._process = process
        self.base_url = f"http://127.0.0.1:{port}"
        self._http = httpx.Client(timeout=10.0)

    # -- reads -----------------------------------------------------------

    def state(self) -> dict:
        response = self._http.get(f"{self.base_url}/__ctl/state")
        response.raise_for_status()
        return response.json()

    def dispatches(self) -> list[dict]:
        return list(self.state()["dispatches"])

    def dispatch_inputs(self, *, ref: str) -> list[dict]:
        """The recorded dispatch INPUTS for one factory branch."""
        return [entry["inputs"] for entry in self.dispatches() if entry["ref"] == ref]

    def runs(self) -> list[dict]:
        return list(self.state()["runs"])

    def cancels(self) -> list[dict]:
        return list(self.state()["cancels"])

    def comments(self) -> list[str]:
        return [entry["body"] for entry in self.state()["comments"]]

    def unknown_paths(self) -> list[str]:
        return list(self.state()["unknown_paths"])

    # -- control ---------------------------------------------------------

    def configure(self, **kwargs: str) -> None:
        response = self._http.post(f"{self.base_url}/__ctl/config", json=kwargs)
        response.raise_for_status()

    def mark_terminal(self, run_id: int, conclusion: str = "success") -> None:
        response = self._http.post(
            f"{self.base_url}/__ctl/mark_terminal",
            json={"run_id": run_id, "conclusion": conclusion},
        )
        response.raise_for_status()

    def seed_issue(self, number: int, title: str, body: str) -> None:
        response = self._http.post(
            f"{self.base_url}/__ctl/seed_issue",
            json={"number": number, "title": title, "body": body},
        )
        response.raise_for_status()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover — a wedged child
            self._process.kill()
            self._process.wait(timeout=10)


@pytest.fixture()
def native(tmp_path: Path) -> FakeNative:
    """The fake native server as a REAL separate process on a loopback port."""
    ready = tmp_path / "native-ready.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(FAKE_NATIVE_SERVER),
            "--ready-file",
            str(ready),
            "--repo",
            PE_REPO,
            "--base-branch",
            PE_BASE_BRANCH,
            "--base-sha",
            PE_BASE_SHA,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20.0
    while not ready.is_file():
        if process.poll() is not None:
            raise AssertionError("the fake native server died at startup")
        if time.monotonic() > deadline:
            process.kill()
            raise AssertionError("the fake native server never became ready")
        time.sleep(0.02)
    port = int(json.loads(ready.read_text())["port"])
    server = FakeNative(process, port)
    try:
        yield server
    finally:
        server.close()


# ----------------------------------------------------------------------
# Real durable state — file-backed aiosqlite (or real PostgreSQL)
# ----------------------------------------------------------------------


class PEDatabase:
    """One database FILE (or PG URL) with per-worker engines.

    ``worker_factory()`` mints a FRESH engine + session factory — the
    "restarted worker": new process shape, same durable rows.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._engines: list[object] = []

    def worker_factory(self) -> async_sessionmaker[AsyncSession]:
        engine = create_async_engine(
            self.url, connect_args={"timeout": 30} if self.url.startswith("sqlite") else {}
        )
        self._engines.append(engine)
        return async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        """The full schema, from scratch (the FI convention: a PG lab URL is
        DISPOSABLE — its rows are dropped so no test inherits another's
        leases, commands or checkpoints)."""
        # Import every module owning tables BEFORE create_all — SQLAlchemy
        # only creates the tables whose modules have been imported.
        import forge.adaptive.mailbox_db  # noqa: F401 — control_commands
        import forge.durable.models  # noqa: F401 — the run/step/action ledger
        from forge import api_checkpoint_channel  # noqa: F401 — checkpoint_metadata

        assert forge.adaptive.mailbox_db and forge.durable.models and api_checkpoint_channel
        engine = create_async_engine(self.url)
        try:
            async with engine.begin() as conn:
                if not self.url.startswith("sqlite"):
                    from sqlalchemy import text

                    names = (
                        (
                            await conn.execute(
                                text("select tablename from pg_tables where schemaname = 'public'")
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for name in names:
                        await conn.execute(text(f'drop table if exists "{name}" cascade'))
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    async def dispose(self) -> None:
        for engine in self._engines:
            await engine.dispose()
        self._engines.clear()


@pytest.fixture()
async def pe_db(tmp_path: Path):
    """A real durable database whose rows survive a worker restart."""
    url = os.environ.get("FORGE_PG_TEST_URL") or f"sqlite+aiosqlite:///{tmp_path / 'pe-state.db'}"
    if url.startswith("sqlite"):
        Path(url.split("///")[-1]).parent.mkdir(parents=True, exist_ok=True)
    database = PEDatabase(url)
    await database.create_schema()
    try:
        yield database
    finally:
        await database.dispose()


# ----------------------------------------------------------------------
# The control plane over real HTTP (the same routers create_app mounts)
# ----------------------------------------------------------------------


class ControlPlane:
    """A real-HTTP lane-control + checkpoint-channel server instance.

    Owns its OWN engine over the target database — a separate process
    shape, exactly like the deployed control plane: asyncpg pools are
    event-loop-bound, so the server thread's pool is never the pytest
    loop's pool (and never the operator-side service's).
    """

    def __init__(
        self, server: Server, thread: threading.Thread, port: int, session_factory
    ) -> None:
        self._server = server
        self._thread = thread
        self._session_factory = session_factory
        self.base_url = f"http://127.0.0.1:{port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def stop(self) -> None:
        # The engine runs on a NullPool (see start_control_plane): every
        # connection is closed by the request that opened it, so stopping
        # the server leaks nothing to dispose across the (now closed)
        # server loop.
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _control_plane_app(session_factory, secret: str) -> FastAPI:
    from forge.api_checkpoint_channel import checkpoint_channel_router
    from forge.api_lane_control import lane_control_router

    application = FastAPI()
    application.state.settings = Settings(FORGE_LANE_CONTROL_SECRET=SecretStr(secret))
    application.state.session_factory = session_factory
    application.include_router(lane_control_router)
    application.include_router(checkpoint_channel_router)
    return application


async def start_control_plane(db_url: str, *, secret: str = PE_LANE_SECRET) -> ControlPlane:
    """Serve the two control routers over real HTTP on a loopback port.

    The server owns its OWN engine over *db_url* on a NullPool — the
    deployed control plane's shape (its pool is never a caller's pool;
    asyncpg pools are event-loop-bound and the server runs on its own
    thread's loop).
    """
    application = _control_plane_app(
        async_sessionmaker(create_async_engine(db_url, poolclass=NullPool), expire_on_commit=False),
        secret,
    )
    server = Server(Config(application, host="127.0.0.1", port=_free_port(), log_level="error"))
    thread = threading.Thread(target=server.run, name="pe-control-plane", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20.0
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            raise AssertionError("the control-plane server never started")
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    return ControlPlane(server, thread, int(port), application.state.session_factory)


# ----------------------------------------------------------------------
# The REAL provider transport: GitHubClient over HTTP to the fake native
# ----------------------------------------------------------------------


class _StaticTokens:
    """The token provider the real client asks before every request."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self) -> str:
        return self._token

    async def invalidate(self) -> None:  # the client's 401 re-auth hook
        return None


@pytest.fixture()
async def native_client(native: FakeNative):
    """The REAL GitHubClient, pointed at the fake native server."""
    from forge.integrations.github import GitHubRepositoryReader

    client = GitHubClient(
        base_url=native.base_url,
        token_provider=_StaticTokens("pe-native-token"),  # noqa: S106
    )
    reader = GitHubRepositoryReader(client, PE_OWNER, PE_REPO_NAME)
    try:
        yield client, reader
    finally:
        await client.aclose()


def pe_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("whsec"),
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_HARNESS_MODEL=PE_HARNESS_MODEL,
        FORGE_GITHUB_HARNESS_WORKFLOW=PE_WORKFLOW,
        FORGE_VERIFICATION_GRACE_SECONDS=0,
        FORGE_CHECKPOINT_MAX_BLOB_BYTES=str(8 * 1024 * 1024),
    )
    values.update(overrides)
    return Settings(**values)


class StubPRReviewer:
    async def review(self, **kwargs):  # pragma: no cover — unused on these traces
        raise AssertionError("no PR review happens on the production-entry traces")


def make_service(
    session_factory,
    client,
    reader,
    *,
    settings: Settings | None = None,
) -> GitHubRunService:
    """A REAL GitHubRunService whose every provider I/O is real HTTP."""
    stack = GitHubAgents(
        client=client,
        reader=reader,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubPRReviewer(),
        flow=GitHubPublishFlow(client, proposer=StubImplementer(), base_branch=PE_BASE_BRANCH),
    )
    return GitHubRunService(
        session_factory,
        settings or pe_settings(),
        ForgeConfig(),
        stack=stack,
        repo_full_name=PE_REPO,
    )
