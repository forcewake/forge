"""R41-02 (#357) — the durable-ingress kill matrix (production entry).

The review's bar, restated as traces: a REAL ASGI gateway subprocess
(uvicorn serving ``forge.main.create_app`` over the same durable database
and fake native GitLab the trace owns), SIGKILLed at each
acknowledgement/commit boundary — before the SQL commit / after the commit
/ after the HTTP response / before the mailbox routing — after which a
FRESH worker (the installed ``run_step_worker`` loop over a fresh engine)
must recover the command WITHOUT a manual webhook resend. A TestClient
waiting for background tasks is not evidence: the process actually dies.

The matrix:

- **DI-1 classic kills** (``/status`` — a read-only classic run command
  with a journaled native reply): before-commit → the redelivery lands
  exactly once; after-commit and after-response → recovered with NO
  resend at all.
- **DI-2 adaptive kills** (``/pause`` on a seeded active run): the SAME
  boundaries; recovery is the durable control mailbox row, the pause
  fence and the ONE operator reply note.
- **DI-3 the P02 schedule** (real Redis): SET-NX "succeeds", the DB
  transaction fails, retries arrive with the SAME and a DIFFERENT
  delivery UUID → ONE durable command, never a successful empty
  duplicate. An orphaned marker (a pre-commit leftover) proceeds as a
  first delivery.
- **DI-4 Redis stopped after the first accept** → the next command still
  lands and BOTH recover from SQL alone (latency only).
- **DI-5 replay vs. decision**: an exact replay is ONE logical command
  with no repeated native effect; two distinct steering messages stay
  distinct.
- **DI-6 two API replicas** over one database: the unique constraint
  arbitrates the same command to ONE row; different commands both land.
- **DI-7 the mutation arms**: (a) the pre-#357 pre-commit cache success
  restored → BOTH the classic and the adaptive P02 traces end in the
  successful empty duplicate; (b) the pre-#357 BackgroundTask-only
  mailbox write restored → the adaptive kill trace loses the
  acknowledged /pause outright. Each arm asserts the DEFECT's
  observable — the baseline traces' assertions fail under the patch.

This module is env-clean once (the module-scoped scrub).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import forge.adaptive.mailbox_db  # noqa: F401 — control_commands joins the schema
import forge.adaptive.pause_fence  # noqa: F401 — pause_fences joins the schema
from forge.adaptive.command_router import reset_shared_control_service
from forge.config import ForgeConfig, Settings
from forge.database import reset_engine
from forge.durable import EventInbox, StepRun
from forge.durable.models import FlowRun
from forge.worker.steps import run_step_worker

from .conftest import GL_PROJECT_ID, gl_settings

pytestmark = pytest.mark.production_entry

#: The webhook shared secret the child gateways authenticate with.
DI_WEBHOOK_SECRET = "di-durable-whsec"  # noqa: S105 — fixture value

HARNESS = Path(__file__).parent / "durable_ingress_server.py"

#: Issue iid the traces' notes land on (the fake native's seeded issue).
DI_ISSUE_IID = 41
DI_ISSUE_TITLE = "Harden the ingress acknowledgement"
DI_ISSUE_DESC = "The kill-matrix issue."


@pytest.fixture(autouse=True, scope="module")
def _env_clean_once():
    """Scrub the provider/forge environment ONCE for the whole module."""
    prefixes = ("FORGE_", "GITLAB_", "GITHUB_")
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefixes)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _clean_shared_control():
    """The process-shared control mailbox never leaks across traces."""
    reset_shared_control_service()
    yield
    reset_shared_control_service()
    reset_engine()


# ----------------------------------------------------------------------
# The REAL ASGI gateway subprocess (uvicorn in a separate OS process)
# ----------------------------------------------------------------------


class Gateway:
    """One uvicorn child serving the shipped app; SIGKILLed by the harness."""

    def __init__(self, process: subprocess.Popen, port: int) -> None:  # noqa: SIM115
        self._process = process
        self.base_url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait(timeout=10)  # a killed child is joined, not zombied


def _wait_for_ready(process: subprocess.Popen, ready: Path, timeout: float = 60.0) -> int:
    """Wait for the child's ready file CONTENT, not its existence.

    R41-10 (the fake-native ready file taught this): ``write_text`` exposes
    an empty file before the payload lands, and under a loaded runner the
    existence check races an empty read into ``json.loads('')``. Poll until
    the file holds parseable content; a torn/partial read retries within
    the same deadline (60s — the CI runner spawns these children ~2-3×
    slower than a dev box).
    """
    deadline = time.monotonic() + timeout
    while True:
        if ready.is_file():
            try:
                return int(json.loads(ready.read_text())["port"])
            except (ValueError, KeyError, json.JSONDecodeError):
                # created but not yet written (or torn) — keep polling
                pass
        if process.poll() is not None:
            process.wait(timeout=10)
            raise AssertionError("the gateway child died at startup")
        if time.monotonic() > deadline:
            process.kill()
            process.wait(timeout=10)
            raise AssertionError("the gateway child never became ready")
        time.sleep(0.02)


def start_gateway(
    tmp_path: Path,
    *,
    db_url: str,
    gitlab_url: str,
    kill: str | None = None,
    db_failures: int = 0,
    mutation: str | None = None,
    redis_url: str | None = None,
    name: str = "gw",
    no_nudge: bool | None = None,
) -> Gateway:
    """Launch the harness subprocess: the REAL app, instrumented for kills.

    ``no_nudge`` (default: every mode except ``kill="after-response"``)
    pins the gateway to ingress-only — the after-response in-process
    execution nudge is disabled, so the step behind the 202 stays
    ``scheduled`` for THIS module's recovering fresh worker instead of
    racing it inside the gateway process (the CI-only desynchronization:
    with no ambient localhost redis the shipped gateway falls back to
    executing the command itself, and either completes it under the fresh
    worker's feet or dies mid-nudge under ``stop()`` leaving the lease
    stranded). The ``after-response`` kill boundary IS the nudge — there
    the real symbol must stay.
    """
    if no_nudge is None:
        no_nudge = kill != "after-response"
    ready = tmp_path / f"{name}-ready.json"
    env = dict(os.environ)
    env.update(
        {
            "GITLAB_URL": gitlab_url,
            "GITLAB_TOKEN": "di-gitlab-token",  # noqa: S106 — fixture value
            "GITLAB_WEBHOOK_SECRET": DI_WEBHOOK_SECRET,
            "DATABASE_URL": db_url,
            "FORGE_APPROVERS": "alice",
            "FORGE_ADAPTIVE_COMMANDS_ENABLED": "1",
            "FORGE_CAPTURE_DIR": "",
        }
    )
    if redis_url:
        env["REDIS_URL"] = redis_url
    else:
        env.pop("REDIS_URL", None)
    command = [
        sys.executable,
        str(HARNESS),
        "--ready-file",
        str(ready),
        "--db-url",
        db_url,
    ]
    if kill:
        command += ["--kill", kill]
    if db_failures:
        command += ["--db-failures", str(db_failures)]
    if mutation:
        command += ["--mutation", mutation]
    if no_nudge:
        command += ["--no-nudge"]
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port = _wait_for_ready(process, ready)
    return Gateway(process, port)


# ----------------------------------------------------------------------
# A REAL Redis (skip-classified where redis-server is unavailable)
# ----------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RedisServer:
    def __init__(self, process: subprocess.Popen, url: str) -> None:  # noqa: SIM115
        self._process = process
        self.url = url

    def stop(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover — a wedged child
            self._process.kill()
            self._process.wait(timeout=10)


@pytest.fixture()
def redis_server():
    """A real ``redis-server`` on a loopback port (skip where absent)."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server is not available — the Redis marker arms are skip-classified")
    port = _free_port()
    process = subprocess.Popen(
        [binary, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15.0
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                break
        except OSError:
            if process.poll() is not None or time.monotonic() > deadline:
                process.kill()
                process.wait(timeout=10)
                pytest.fail("the redis-server child never became ready")
            time.sleep(0.05)
    server = RedisServer(process, f"redis://127.0.0.1:{port}")
    try:
        yield server
    finally:
        server.stop()


# ----------------------------------------------------------------------
# Payloads, posting, durable reads, the fresh worker
# ----------------------------------------------------------------------


def _issue_note(note_id: int, text: str, *, author: str = "alice") -> dict:
    """A GitLab Note Hook on the issue, the shape GitLab POSTs."""
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice Approver", "username": author, "email": ""},
        "project": {
            "id": GL_PROJECT_ID,
            "name": "forge-di",
            "path_with_namespace": "acme/forge-di",
            "web_url": "https://gitlab.test/acme/forge-di",
        },
        "object_attributes": {
            "id": note_id,
            "note": text,
            "noteable_type": "Issue",
            "noteable_id": 41,
            "author_id": 11,
            "discussion_id": f"d-{note_id}",
        },
        "issue": {
            "id": 4100,
            "iid": DI_ISSUE_IID,
            "title": DI_ISSUE_TITLE,
            "description": DI_ISSUE_DESC,
        },
    }


async def _post(
    gateway: Gateway,
    note_id: int,
    text: str,
    *,
    delivery: str | None = None,
    author: str = "alice",
):
    """POST one note webhook; a SIGKILLed child surfaces as ``None``."""
    headers = {"X-Gitlab-Token": DI_WEBHOOK_SECRET, "X-Gitlab-Event": "Note Hook"}
    if delivery is not None:
        headers["X-Gitlab-Event-UUID"] = delivery
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            return await client.post(
                gateway.base_url + "/webhook",
                json=_issue_note(note_id, text, author=author),
                headers=headers,
            )
        except httpx.TransportError:
            # The SIGKILLed child tears the socket mid-request/response —
            # exactly the boundary condition the matrix drives.
            return None


async def _await_truth(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _worker_settings(gitlab_url: str) -> Settings:
    return gl_settings(
        GITLAB_URL=gitlab_url,
        GITLAB_TOKEN="di-gitlab-token",  # noqa: S106 — fixture value
        FORGE_APPROVERS="alice",
        REDIS_URL=None,  # the fresh worker recovers from SQL alone
    )


async def _resume_worker(pe_db, settings, until, *, timeout: float = 60.0, expect: bool = True):
    """The INSTALLED step-runtime loop over a FRESH engine — the restarted
    worker (``run_step_worker`` is the exact loop ``worker/app.main``
    gathers; the claim → lease → fence protocol executes the persisted
    step through the REAL dispatch). The 60s deadline is a CI-tolerant
    bound on POLLING (claim + route + the native reply), never a
    behavioral assertion — the predicate still has to become true."""
    shutdown = asyncio.Event()
    loop = asyncio.create_task(
        run_step_worker(
            pe_db.worker_factory(),
            settings,
            ForgeConfig(),
            "di-fresh-worker",
            shutdown,
            None,
            poll_interval=0.05,
        )
    )
    try:
        reached = await _await_truth(until, timeout=timeout)
        assert reached is expect, "the fresh worker's outcome drifted from the expectation"
    finally:
        shutdown.set()
        await asyncio.wait_for(loop, timeout=10)


async def _command_rows(pe_db, command: str):
    """The inbox and step rows for one command name."""
    from sqlalchemy import select

    factory = pe_db.worker_factory()
    async with factory() as session:
        inbox = list((await session.execute(select(EventInbox))).scalars().all())
        steps = list((await session.execute(select(StepRun))).scalars().all())
    return (
        [row for row in inbox if (row.payload or {}).get("command") == command],
        [row for row in steps if row.step_name == command],
    )


def _issue_note_bodies(gitlab_native) -> list[str]:
    return [entry["body"] for entry in gitlab_native.state()["notes"]]


async def _seed_active_run(pe_db, run_id: str) -> None:
    """An active (non-terminal) run on the trace's issue for /pause to target."""
    factory = pe_db.worker_factory()
    async with factory() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=GL_PROJECT_ID,
                issue_iid=DI_ISSUE_IID,
                provider="gitlab",
                status="waiting_approval",
            )
        )
        await session.commit()


async def _control_rows(pe_db, kind: str):
    from sqlalchemy import select

    from forge.adaptive.mailbox_db import ControlCommandRow

    factory = pe_db.worker_factory()
    async with factory() as session:
        return list(
            (await session.execute(select(ControlCommandRow).where(ControlCommandRow.kind == kind)))
            .scalars()
            .all()
        )


async def _pause_fence_rows(pe_db, run_id: str):
    from sqlalchemy import select

    from forge.adaptive.pause_fence import PauseFenceRow

    factory = pe_db.worker_factory()
    async with factory() as session:
        return list(
            (await session.execute(select(PauseFenceRow).where(PauseFenceRow.work_id == run_id)))
            .scalars()
            .all()
        )


def _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path) -> None:
    """The worker's control-plane mailbox is the durable Postgres one over
    the trace's own database — the mailbox rows are assertable state."""
    monkeypatch.setenv("DATABASE_URL", pe_db.url)
    monkeypatch.setenv("FORGE_CONTROL_MAILBOX", "postgres")
    monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "di-checkpoints"))
    reset_shared_control_service()


async def _seeded_issue(gitlab_native) -> None:
    gitlab_native.seed_issue(DI_ISSUE_IID, DI_ISSUE_TITLE, DI_ISSUE_DESC)


# ----------------------------------------------------------------------
# DI-1 — the classic kill matrix (/status: read-only, one journaled reply)
# ----------------------------------------------------------------------


class TestClassicKillMatrix:
    async def test_kill_before_commit_the_redelivery_lands_exactly_once(
        self, pe_db, gitlab_native, tmp_path
    ):
        """SIGKILL after ``ingest_event`` ran but before the commit: nothing
        is durable, the provider-style redelivery hits a clean slate and
        lands exactly once."""
        await _seeded_issue(gitlab_native)
        db_url = pe_db.url
        gateway = start_gateway(
            tmp_path, db_url=db_url, gitlab_url=gitlab_native.base_url, kill="before-commit"
        )
        try:
            response = await _post(gateway, 6101, "@forge /status", delivery="di1-pre")
            assert response is None  # the process died inside the request
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "status")
        assert inbox == [] and steps == []  # the uncommitted work is gone

        # the provider's redelivery (a fresh gateway over the same database)
        retry_gateway = start_gateway(
            tmp_path, db_url=db_url, gitlab_url=gitlab_native.base_url, name="gw-retry"
        )
        try:
            response = await _post(retry_gateway, 6101, "@forge /status", delivery="di1-pre")
            assert response is not None and response.status_code == 202
            assert response.json()["run_command"] is True
        finally:
            retry_gateway.stop()

        async def _replied() -> bool:
            return any("Forge — status" in body for body in _issue_note_bodies(gitlab_native))

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _replied)
        inbox, steps = await _command_rows(pe_db, "status")
        assert len(inbox) == 1 and len(steps) == 1  # ONE durable command
        assert steps[0].status != "scheduled"  # the fresh worker executed it
        replies = [b for b in _issue_note_bodies(gitlab_native) if "Forge — status" in b]
        assert len(replies) == 1  # no repeated native effect

    async def test_kill_after_commit_recovers_without_any_resend(
        self, pe_db, gitlab_native, tmp_path
    ):
        """SIGKILL after the commit but before the response: the inbox row
        and the scheduled step ARE durable — NO redelivery, the fresh
        worker recovers the command alone."""
        await _seeded_issue(gitlab_native)
        gateway = start_gateway(
            tmp_path, db_url=pe_db.url, gitlab_url=gitlab_native.base_url, kill="after-commit"
        )
        try:
            response = await _post(gateway, 6102, "@forge /status", delivery="di1-post")
            assert response is None  # the acknowledgement never left
        finally:
            gateway.stop()

        async def _rows_landed() -> bool:
            inbox, steps = await _command_rows(pe_db, "status")
            return bool(inbox) and bool(steps)

        assert await _await_truth(_rows_landed, timeout=10), "the commit did not survive"
        inbox, steps = await _command_rows(pe_db, "status")
        assert steps[0].status == "scheduled"  # committed, never executed

        async def _replied() -> bool:
            return any("Forge — status" in body for body in _issue_note_bodies(gitlab_native))

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _replied)
        replies = [b for b in _issue_note_bodies(gitlab_native) if "Forge — status" in b]
        assert len(replies) == 1

    async def test_kill_after_response_recovers_without_any_resend(
        self, pe_db, gitlab_native, tmp_path
    ):
        """The acknowledgement LEFT, then the process died before the
        after-response work: the committed step is the durability — the
        fresh worker recovers the command without a resend."""
        await _seeded_issue(gitlab_native)
        gateway = start_gateway(
            tmp_path, db_url=pe_db.url, gitlab_url=gitlab_native.base_url, kill="after-response"
        )
        try:
            response = await _post(gateway, 6103, "@forge /status", delivery="di1-resp")
            # The client may hold the 202 or lose the socket mid-teardown —
            # either way the command must be durable.
            assert response is None or response.status_code == 202
        finally:
            gateway.stop()

        async def _rows_landed() -> bool:
            inbox, steps = await _command_rows(pe_db, "status")
            return bool(inbox) and bool(steps)

        assert await _await_truth(_rows_landed, timeout=10)

        async def _replied() -> bool:
            return any("Forge — status" in body for body in _issue_note_bodies(gitlab_native))

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _replied)
        replies = [b for b in _issue_note_bodies(gitlab_native) if "Forge — status" in b]
        assert len(replies) == 1


# ----------------------------------------------------------------------
# DI-2 — the adaptive kill matrix (/pause on a seeded active run)
# ----------------------------------------------------------------------


class TestAdaptiveKillMatrix:
    async def test_kill_after_response_the_pause_recovers_without_resend(
        self, pe_db, gitlab_native, tmp_path, monkeypatch
    ):
        """The #357 headline defect: an acknowledged /pause killed before
        the mailbox routing. Under the durable rule the scheduled step IS
        the command — the fresh worker routes it: mailbox row, pause
        fence, ONE operator reply."""
        await _seeded_issue(gitlab_native)
        run_id = "d" * 31 + "1"
        await _seed_active_run(pe_db, run_id)
        gateway = start_gateway(
            tmp_path, db_url=pe_db.url, gitlab_url=gitlab_native.base_url, kill="after-response"
        )
        try:
            response = await _post(gateway, 6201, "/pause", delivery="di2-pause")
            assert response is None or (
                response.status_code == 202 and response.json()["adaptive_command"] is True
            )
        finally:
            gateway.stop()

        async def _rows_landed() -> bool:
            inbox, steps = await _command_rows(pe_db, "adaptive_control")
            return bool(inbox) and bool(steps)

        assert await _await_truth(_rows_landed, timeout=10)
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert steps[0].status == "scheduled"  # durable, un-routed yet

        _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path)

        async def _pause_recorded() -> bool:
            fences = await _pause_fence_rows(pe_db, run_id)
            return bool(fences)

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _pause_recorded)
        fences = await _pause_fence_rows(pe_db, run_id)
        assert len(fences) == 1  # the durable pause fence stands
        commands = await _control_rows(pe_db, "pause")
        assert len(commands) == 1  # the durable mailbox record
        assert commands[0].work_id == run_id
        pauses = [b for b in _issue_note_bodies(gitlab_native) if "Pause recorded" in b]
        assert len(pauses) == 1  # ONE operator reply

    async def test_kill_before_commit_the_pause_redelivery_lands_once(
        self, pe_db, gitlab_native, tmp_path, monkeypatch
    ):
        await _seeded_issue(gitlab_native)
        run_id = "e" * 31 + "2"
        await _seed_active_run(pe_db, run_id)
        db_url = pe_db.url
        gateway = start_gateway(
            tmp_path, db_url=db_url, gitlab_url=gitlab_native.base_url, kill="before-commit"
        )
        try:
            response = await _post(gateway, 6202, "/pause", delivery="di2-pre")
            assert response is None
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert inbox == [] and steps == []

        retry_gateway = start_gateway(
            tmp_path, db_url=db_url, gitlab_url=gitlab_native.base_url, name="gw-retry"
        )
        try:
            response = await _post(retry_gateway, 6202, "/pause", delivery="di2-pre")
            assert response is not None and response.status_code == 202
            assert response.json()["adaptive_command"] is True
        finally:
            retry_gateway.stop()

        _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path)

        async def _pause_recorded() -> bool:
            fences = await _pause_fence_rows(pe_db, run_id)
            return bool(fences)

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _pause_recorded)
        commands = await _control_rows(pe_db, "pause")
        assert len(commands) == 1  # the redelivered pause landed exactly once


# ----------------------------------------------------------------------
# DI-3 — the P02 schedule against a REAL Redis
# ----------------------------------------------------------------------


class TestP02Schedule:
    async def test_marker_ok_db_fail_retries_land_one_command(
        self, pe_db, gitlab_native, tmp_path, redis_server
    ):
        """SET-NX "succeeds", the SQL transaction fails, the retries arrive
        within the TTL with the SAME and a DIFFERENT delivery UUID → ONE
        durable command, never a successful empty duplicate."""
        await _seeded_issue(gitlab_native)
        gateway = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
            db_failures=1,
        )
        try:
            first = await _post(gateway, 6301, "@forge /status", delivery="di3-a")
            assert first is not None and first.status_code == 503  # honest refusal

            retry = await _post(gateway, 6301, "@forge /status", delivery="di3-a")
            assert retry is not None and retry.status_code == 202
            assert retry.json()["run_command"] is True  # NOT an empty duplicate

            manual = await _post(gateway, 6301, "@forge /status", delivery="di3-b")
            assert manual is not None and manual.status_code == 202
            assert manual.json()["deduplicated"] is True
        finally:
            gateway.stop()

        inbox, steps = await _command_rows(pe_db, "status")
        assert len(inbox) == 1 and len(steps) == 1  # ONE durable command

        async def _replied() -> bool:
            return any("Forge — status" in body for body in _issue_note_bodies(gitlab_native))

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _replied)
        replies = [b for b in _issue_note_bodies(gitlab_native) if "Forge — status" in b]
        assert len(replies) == 1

    async def test_an_orphaned_marker_proceeds_as_a_first_delivery(
        self, pe_db, gitlab_native, tmp_path, redis_server
    ):
        """A marker whose record never landed (the pre-commit SET-NX of a
        failed transaction under the old code) must not suppress the
        command — the authoritative lookup misses and the delivery
        ingests."""
        from forge.utils.redis_client import RedisManager
        from forge.worker.queue import DEDUP_PREFIX

        await _seeded_issue(gitlab_native)
        manager = RedisManager(redis_server.url)
        try:
            await manager.set_ex(f"{DEDUP_PREFIX}run:{GL_PROJECT_ID}:6302", "1", 300)
        finally:
            await manager.close()

        gateway = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
        )
        try:
            response = await _post(gateway, 6302, "@forge /status", delivery="di3-orphan")
            assert response is not None and response.status_code == 202
            assert response.json()["run_command"] is True  # first delivery, not deduplicated
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "status")
        assert len(inbox) == 1 and len(steps) == 1


# ----------------------------------------------------------------------
# DI-4 — Redis stopped after the first accept
# ----------------------------------------------------------------------


class TestRedisStoppedAfterCommit:
    async def test_redis_death_changes_latency_only(
        self, pe_db, gitlab_native, tmp_path, redis_server
    ):
        """Redis alive for the first command, dead for the second: both land
        durably and the fresh worker (no Redis at all) recovers both from
        SQL alone."""
        await _seeded_issue(gitlab_native)
        gateway = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
        )
        try:
            first = await _post(gateway, 6401, "@forge /status", delivery="di4-a")
            assert first is not None and first.json()["run_command"] is True
        finally:
            gateway.stop()
        redis_server.stop()  # Redis dies AFTER the first commit

        second_gateway = start_gateway(
            tmp_path, db_url=pe_db.url, gitlab_url=gitlab_native.base_url, name="gw-noredis"
        )
        try:
            second = await _post(second_gateway, 6402, "@forge /status", delivery="di4-b")
            assert second is not None and second.status_code == 202
            assert second.json()["run_command"] is True
        finally:
            second_gateway.stop()

        async def _both_replied() -> bool:
            bodies = _issue_note_bodies(gitlab_native)
            return sum("Forge — status" in b for b in bodies) >= 2

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _both_replied)
        inbox, steps = await _command_rows(pe_db, "status")
        assert len(inbox) == 2 and len(steps) == 2


# ----------------------------------------------------------------------
# DI-5 — exact replay vs. genuine repeated decisions
# ----------------------------------------------------------------------


class TestReplayAndDistinct:
    async def test_exact_replay_is_one_logical_command(self, pe_db, gitlab_native, tmp_path):
        await _seeded_issue(gitlab_native)
        gateway = start_gateway(tmp_path, db_url=pe_db.url, gitlab_url=gitlab_native.base_url)
        try:
            first = await _post(gateway, 6501, "@forge /status", delivery="di5-a")
            replay = await _post(gateway, 6501, "@forge /status", delivery="di5-a")
            redelivery = await _post(gateway, 6501, "@forge /status", delivery="di5-b")
            assert first is not None and first.json()["run_command"] is True
            assert replay is not None and replay.json()["deduplicated"] is True
            assert redelivery is not None and redelivery.json()["deduplicated"] is True
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "status")
        assert len(inbox) == 1 and len(steps) == 1

        async def _replied() -> bool:
            return any("Forge — status" in body for body in _issue_note_bodies(gitlab_native))

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _replied)
        replies = [b for b in _issue_note_bodies(gitlab_native) if "Forge — status" in b]
        assert len(replies) == 1  # no repeated native effect

    async def test_two_distinct_steering_messages_stay_distinct(
        self, pe_db, gitlab_native, tmp_path, monkeypatch
    ):
        await _seeded_issue(gitlab_native)
        run_id = "f" * 31 + "3"
        await _seed_active_run(pe_db, run_id)
        gateway = start_gateway(tmp_path, db_url=pe_db.url, gitlab_url=gitlab_native.base_url)
        try:
            first = await _post(
                gateway, 6502, "/steer tighten the error handling", delivery="di5-s1"
            )
            second = await _post(
                gateway, 6503, "/steer prefer the functional style", delivery="di5-s2"
            )
            assert first is not None and first.json()["adaptive_command"] is True
            assert second is not None and second.json()["adaptive_command"] is True
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert len(inbox) == 2 and len(steps) == 2  # two durable commands

        _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path)

        async def _two_steers() -> bool:
            rows = await _control_rows(pe_db, "steer")
            return len(rows) == 2

        await _resume_worker(pe_db, _worker_settings(gitlab_native.base_url), _two_steers)
        rows = await _control_rows(pe_db, "steer")
        texts = {json.dumps(row.payload.get("text") or row.payload, sort_keys=True) for row in rows}
        assert len(texts) == 2  # the two guidances never collapsed
        steers = [b for b in _issue_note_bodies(gitlab_native) if "Steering recorded" in b]
        assert len(steers) == 2


# ----------------------------------------------------------------------
# DI-6 — two API replicas over one database
# ----------------------------------------------------------------------


class TestTwoReplicas:
    async def test_the_same_command_through_both_replicas_is_one_command(
        self, pe_db, gitlab_native, tmp_path, redis_server
    ):
        """Two gateway processes, one database: the DB unique constraint
        arbitrates (Redis only accelerates) — ONE durable command."""
        await _seeded_issue(gitlab_native)
        one = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
            name="gw-replica-1",
        )
        two = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
            name="gw-replica-2",
        )
        try:
            first = await _post(one, 6601, "@forge /status", delivery="di6-a")
            second = await _post(two, 6601, "@forge /status", delivery="di6-a")
            assert first is not None and first.json()["run_command"] is True
            assert second is not None
            assert second.json()["deduplicated"] is True
        finally:
            one.stop()
            two.stop()
        inbox, steps = await _command_rows(pe_db, "status")
        assert len(inbox) == 1 and len(steps) == 1

    async def test_different_commands_through_both_replicas_both_land(
        self, pe_db, gitlab_native, tmp_path, redis_server
    ):
        await _seeded_issue(gitlab_native)
        one = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
            name="gw-replica-1",
        )
        two = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
            name="gw-replica-2",
        )
        try:
            first = await _post(one, 6602, "@forge /status", delivery="di6-b")
            second = await _post(two, 6603, "@forge /status", delivery="di6-c")
            assert first is not None and first.json()["run_command"] is True
            assert second is not None and second.json()["run_command"] is True
        finally:
            one.stop()
            two.stop()
        inbox, steps = await _command_rows(pe_db, "status")
        assert len(inbox) == 2 and len(steps) == 2


# ----------------------------------------------------------------------
# DI-7 — the mutation arms (the mutants the baseline traces kill)
# ----------------------------------------------------------------------


class TestMutationArms:
    async def test_pre_commit_cache_restored_empties_the_classic_duplicate(
        self, pe_db, gitlab_native, tmp_path, redis_server
    ):
        """Seed the #357 defect back (``_ingest_run_command`` reverted to the
        pre-commit SET-NX marker deciding) and run the SAME P02 schedule:
        the retry within the TTL answers a SUCCESSFUL EMPTY DUPLICATE —
        the baseline trace's assertions (503 then ONE durable command)
        both fail under this patch."""
        await _seeded_issue(gitlab_native)
        gateway = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
            db_failures=1,
            mutation="pre-commit-cache",
        )
        try:
            first = await _post(gateway, 6701, "@forge /status", delivery="di7-a")
            assert first is not None and first.status_code == 500  # the old unhandled outage

            retry = await _post(gateway, 6701, "@forge /status", delivery="di7-a")
            assert retry is not None and retry.status_code == 202
            # THE DEFECT'S OBSERVABLE — the successful empty duplicate:
            assert retry.json()["deduplicated"] is True
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "status")
        assert inbox == [] and steps == []  # acknowledged, and nothing is durable

    async def test_pre_commit_cache_restored_empties_the_adaptive_duplicate(
        self, pe_db, gitlab_native, tmp_path, redis_server
    ):
        """The same regression on the adaptive leg: the /pause P02 schedule
        under the reverted ingest answers a successful empty duplicate —
        the adaptive baseline's assertions fail identically."""
        await _seeded_issue(gitlab_native)
        run_id = "a" * 31 + "7"
        await _seed_active_run(pe_db, run_id)
        gateway = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            redis_url=redis_server.url,
            db_failures=1,
            mutation="pre-commit-cache",
        )
        try:
            first = await _post(gateway, 6702, "/pause", delivery="di7-p")
            assert first is not None and first.status_code == 500

            retry = await _post(gateway, 6702, "/pause", delivery="di7-p")
            assert retry is not None and retry.status_code == 202
            # THE DEFECT'S OBSERVABLE — adaptive_command acknowledged as a
            # duplicate with NOTHING durable behind it:
            assert retry.json()["deduplicated"] is True
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert inbox == [] and steps == []
        assert await _control_rows(pe_db, "pause") == []
        assert await _pause_fence_rows(pe_db, run_id) == []

    async def test_background_mailbox_restored_loses_the_acknowledged_pause(
        self, pe_db, gitlab_native, tmp_path, monkeypatch
    ):
        """Seed the #357 defect back (``_ingest_adaptive_control`` reverted
        to the immediate 202 + after-response BackgroundTask mailbox write)
        and kill the process at exactly the boundary the review named —
        after the response, before the mailbox routing: the acknowledged
        /pause is LOST. The adaptive baseline's recovery assertions (step,
        fence, mailbox row, reply) all fail under this patch."""
        await _seeded_issue(gitlab_native)
        run_id = "b" * 31 + "7"
        await _seed_active_run(pe_db, run_id)
        gateway = start_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            kill="after-response",
            mutation="background-mailbox",
        )
        try:
            response = await _post(gateway, 6703, "/pause", delivery="di7-bg")
            # The OLD code's immediate 202 (then the process died in the
            # background task, before any mailbox write):
            assert response is None or (
                response.status_code == 202 and response.json()["adaptive_command"] is True
            )
        finally:
            gateway.stop()

        # THE DEFECT'S OBSERVABLE — nothing durable exists for the command:
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert inbox == [] and steps == []

        # ...and the fresh worker genuinely has nothing to recover:
        _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path)

        async def _never() -> bool:
            return False

        await _resume_worker(
            pe_db, _worker_settings(gitlab_native.base_url), _never, timeout=1.0, expect=False
        )
        assert await _control_rows(pe_db, "pause") == []
        assert await _pause_fence_rows(pe_db, run_id) == []
        assert not any("Pause recorded" in b for b in _issue_note_bodies(gitlab_native))
