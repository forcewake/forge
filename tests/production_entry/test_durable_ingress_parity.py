"""R41-02 parity — the durable-ingress kill matrix for GitHub and Azure.

The follow-up to #357: the GitLab gateway landed the durable-acceptance
contract (``tests/production_entry/test_durable_ingress.py``); this module
proves the SAME traces on the TWO legacy gateways the contract was ported
to — ``forge.gateway.github_webhook`` and ``forge.gateway.azure_webhook``.
Everything is shared with the GitLab matrix: the REAL ASGI gateway
subprocess (uvicorn serving ``forge.main.create_app`` over the trace's own
durable database, SIGKILLed at the acknowledgement boundaries), the REAL
Redis for the marker arms, and the INSTALLED ``run_step_worker`` loop over
a fresh engine as the recovering worker.

The per-gateway arms (the #357 acceptance items, restated):

- **P02 schedule** (real Redis): the marker exists, the DB transaction
  fails → the honest 503; the retries (same delivery, then a manual
  redelivery) land ONE durable command — never a successful empty
  duplicate.
- **Orphaned marker**: a marker whose record never landed proceeds as a
  FIRST delivery (the authoritative lookup decides, the cache never
  suppresses).
- **After-response kill**: the acknowledged ``/pause`` is killed before
  the routing — the committed ``adaptive_control`` step IS the command, a
  fresh worker recovers it with NO resend (mailbox row, pause fence, ONE
  operator reply).
- **Exact replay**: the same delivery twice → ONE logical command, ONE
  operator reply.

Provider surfaces: GitHub writes travel the REAL ``GitHubClient`` to the
fake native server (github mode); Azure DevOps replies travel the REAL
``AzureDevOpsClient`` to a local work-item-comments sink (the only Azure
endpoint the adaptive reply channel needs — the sink accepts the documented
``POST /{project}/_apis/wit/workItems/{id}/comments`` shape).

This module is env-clean once (the module-scoped scrub).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

import forge.adaptive.mailbox_db  # noqa: F401 — control_commands joins the schema
import forge.adaptive.pause_fence  # noqa: F401 — pause_fences joins the schema
from forge.adaptive.command_router import reset_shared_control_service
from forge.config import Settings
from forge.database import reset_engine

from .conftest import gl_settings
from .test_durable_ingress import (
    DI_WEBHOOK_SECRET,
    Gateway,
    _await_truth,
    _command_rows,
    _control_rows,
    _pause_fence_rows,
    _resume_worker,
    _wait_for_ready,
)
from .test_durable_ingress import redis_server as _gitlab_redis_server  # noqa: F401 — a fixture


@pytest.fixture()
def redis_server(request: pytest.FixtureRequest):
    """The real-redis fixture from the GitLab matrix, re-exported for the
    marker arms (resolved by name so the re-export stays ruff-clean)."""
    return request.getfixturevalue("_gitlab_redis_server")


pytestmark = pytest.mark.production_entry

HARNESS = Path(__file__).parent / "durable_ingress_server.py"

#: The GitHub connection the traces' deliveries address.
GH_REPO = "acme/forge-pe"
GH_INSTALLATION = 42
GH_REPO_ID = 70055
GH_ISSUE_NUMBER = 51
GH_AUTHOR = "alice"

#: The Azure DevOps connection the traces' deliveries address.
AZ_ORG_URL = "https://dev.azure.com/fabrikam"
AZ_PROJECT = "Fabrikam"
AZ_PROJECT_GUID = "3b2a1c0d-0000-0000-0000-0000000000aa"
AZ_WORK_ITEM = 142
AZ_AUTHOR = "dev@fabrikam.example"
AZ_HOOK_USER = "forge-hooks"  # noqa: S105 — fixture value
AZ_PAT = "di-azdo-pat"  # noqa: S105 — fixture value
GH_TOKEN = "di-gh-token"  # noqa: S105 — fixture value


@pytest.fixture(autouse=True, scope="module")
def _env_clean_once():
    """Scrub the provider/forge environment ONCE for the whole module."""
    prefixes = ("FORGE_", "GITLAB_", "GITHUB_", "AZURE_")
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
# The Azure DevOps work-item comment sink (the reply channel's endpoint)
# ----------------------------------------------------------------------


class AzDoSink:
    """A local HTTP surface for ``POST /{project}/_apis/wit/workItems/{id}/comments``."""

    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread
        self.comments: list[str] = []
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def bodies(self) -> list[str]:
        return list(self.comments)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture()
def azdo_sink():
    sink_holder: dict[str, AzDoSink] = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "azdo-sink/1"

        def do_POST(self) -> None:  # noqa: N802 — http.server spelling
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                text = str(json.loads(raw).get("text") or "")
            except ValueError:
                text = ""
            sink = sink_holder.get("sink")
            if sink is not None:
                sink.comments.append(text)
            body = json.dumps({"commentId": 1, "text": text}).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # silence the test log
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    sink = AzDoSink(server, threading.Thread(target=server.serve_forever, daemon=True))
    sink_holder["sink"] = sink
    sink._thread.start()  # noqa: SLF001 — the fixture owns the thread
    try:
        yield sink
    finally:
        sink.stop()


# ----------------------------------------------------------------------
# The REAL ASGI gateway subprocess (github / azure_devops ingress)
# ----------------------------------------------------------------------


def start_parity_gateway(
    tmp_path: Path,
    *,
    db_url: str,
    gateway: str,
    reply_url: str,
    kill: str | None = None,
    db_failures: int = 0,
    redis_url: str | None = None,
    name: str = "gw",
    no_nudge: bool | None = None,
) -> Gateway:
    """Launch the shared harness subprocess serving the REAL app, with the
    named gateway's ingress enabled (the env the child's Settings read).

    ``no_nudge`` (default: every mode except ``kill="after-response"``)
    pins the gateway to ingress-only — the after-response in-process
    execution nudge is disabled so the step behind the 202 stays
    ``scheduled`` for this module's recovering fresh worker. Without the
    pin, a machine with no ambient localhost redis (the CI runner) has the
    gateway execute the adaptive step itself: the routing lands on the
    gateway's IN-MEMORY control mailbox (no durable ``ControlCommandRow``,
    no mailbox-shaped observable) or dies mid-nudge under ``stop()`` with
    the lease stranded — the "outcome drifted" CI failure. The
    ``after-response`` kill boundary IS the nudge; there the real symbol
    stays."""
    if no_nudge is None:
        no_nudge = kill != "after-response"
    ready = tmp_path / f"{name}-ready.json"
    env = dict(os.environ)
    env.update(
        {
            # Required Settings fields for every provider slice; the GitLab
            # transport itself is never used by these traces.
            "GITLAB_URL": "https://gitlab.test",
            "GITLAB_TOKEN": "di-gitlab-token",  # noqa: S106 — fixture value
            "GITLAB_WEBHOOK_SECRET": DI_WEBHOOK_SECRET,
            "DATABASE_URL": db_url,
            "FORGE_ADAPTIVE_COMMANDS_ENABLED": "1",
            "FORGE_CAPTURE_DIR": "",
        }
    )
    if gateway == "github":
        env.update(
            {
                "FORGE_GITHUB_ENABLED": "1",
                "FORGE_GITHUB_WEBHOOK_SECRET": DI_WEBHOOK_SECRET,
                "FORGE_GITHUB_API_URL": reply_url,
                "FORGE_GITHUB_TOKEN": GH_TOKEN,
                "FORGE_GITHUB_APPROVERS": "",
                "FORGE_APPROVERS": GH_AUTHOR,
            }
        )
    elif gateway == "azure_devops":
        env.update(
            {
                "FORGE_AZDO_ENABLED": "1",
                "FORGE_AZDO_WEBHOOK_USERNAME": AZ_HOOK_USER,
                "FORGE_AZDO_WEBHOOK_PASSWORD": DI_WEBHOOK_SECRET,
                "FORGE_AZDO_ORG_URL": reply_url,
                "FORGE_AZDO_PAT": AZ_PAT,
                "FORGE_AZDO_APPROVERS": AZ_AUTHOR,
                "FORGE_AZDO_BOT_NAME": "",
                "FORGE_APPROVERS": AZ_AUTHOR,
            }
        )
    else:  # pragma: no cover — the two parity gateways only
        raise ValueError(f"unknown gateway: {gateway!r}")
    if redis_url:
        env["REDIS_URL"] = redis_url
    command = [sys.executable, str(HARNESS), "--ready-file", str(ready), "--db-url", db_url]
    if kill:
        command += ["--kill", kill]
    if db_failures:
        command += ["--db-failures", str(db_failures)]
    if no_nudge:
        command += ["--no-nudge"]
    process = subprocess.Popen(
        command,
        cwd=tmp_path,  # no repo .env: the parity env above is the whole config
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port = _wait_for_ready(process, ready)
    return Gateway(process, port)


# ----------------------------------------------------------------------
# Payloads and posting
# ----------------------------------------------------------------------


def _github_issue_note(comment_id: int, text: str) -> dict:
    """An ``issue_comment`` ``created`` delivery, the shape GitHub POSTs."""
    return {
        "action": "created",
        "issue": {
            "id": 9000 + GH_ISSUE_NUMBER,
            "number": GH_ISSUE_NUMBER,
            "title": "Parity for the durable acceptance",
            "body": "The kill-matrix issue.",
        },
        "comment": {
            "id": comment_id,
            "body": text,
            "user": {"login": GH_AUTHOR, "id": 11},
        },
        "repository": {
            "id": GH_REPO_ID,
            "name": "forge-pe",
            "full_name": GH_REPO,
        },
        "installation": {"id": GH_INSTALLATION},
        "sender": {"login": GH_AUTHOR},
    }


def _azure_workitem_note(rev: int, text: str) -> dict:
    """A ``workitem.commented`` delivery, the shape Azure DevOps POSTs."""
    return {
        "eventType": "workitem.commented",
        "id": f"di-ado-{rev}",
        "resource": {
            "id": AZ_WORK_ITEM,
            "rev": rev,
            "fields": {
                "System.History": text,
                "System.TeamProject": AZ_PROJECT,
                "System.ChangedBy": {
                    "displayName": "Dev User",
                    "uniqueName": AZ_AUTHOR,
                },
            },
        },
        "resourceContainers": {
            "project": {"id": AZ_PROJECT_GUID},
            "collection": {"baseUrl": AZ_ORG_URL},
        },
    }


async def _post_github(gateway: Gateway, comment_id: int, text: str):
    """POST one GitHub comment webhook; a SIGKILLed child surfaces as None."""
    body = json.dumps(_github_issue_note(comment_id, text)).encode()
    signature = "sha256=" + hmac.new(DI_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": f"di-parity-{comment_id}",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            return await client.post(
                gateway.base_url + "/webhook/github", content=body, headers=headers
            )
        except httpx.TransportError:
            return None


async def _post_azure(gateway: Gateway, rev: int, text: str):
    """POST one Azure DevOps comment webhook; a SIGKILLed child → None."""
    body = json.dumps(_azure_workitem_note(rev, text)).encode()
    token = base64.b64encode(f"{AZ_HOOK_USER}:{DI_WEBHOOK_SECRET}".encode()).decode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {token}",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            return await client.post(
                gateway.base_url + "/webhook/azure_devops", content=body, headers=headers
            )
        except httpx.TransportError:
            return None


# ----------------------------------------------------------------------
# The fresh worker (restarted), durable reads, the seeded active run
# ----------------------------------------------------------------------


def _github_worker_settings(reply_url: str) -> Settings:
    return gl_settings(
        FORGE_APPROVERS=GH_AUTHOR,
        FORGE_GITHUB_APPROVERS="",  # the shared list is authoritative here
        REDIS_URL=None,  # the fresh worker recovers from SQL alone
        FORGE_GITHUB_API_URL=reply_url,
        FORGE_GITHUB_TOKEN=SecretStr(GH_TOKEN),
        # PAT mode, deterministically: a developer .env App key must not
        # redirect the reply channel at api.github.com (an explicit None
        # wins over the env_file in pydantic-settings).
        FORGE_GITHUB_PRIVATE_KEY=None,
    )


def _azure_worker_settings(reply_url: str) -> Settings:
    return gl_settings(
        FORGE_APPROVERS=AZ_AUTHOR,
        # The connection-scoped list is what admission reads for
        # azure_devops — pin it so a developer .env cannot refuse the
        # trace's author.
        FORGE_AZDO_APPROVERS=AZ_AUTHOR,
        FORGE_AZDO_BOT_NAME="",
        REDIS_URL=None,  # the fresh worker recovers from SQL alone
        FORGE_AZDO_ORG_URL=reply_url,
        FORGE_AZDO_PAT=SecretStr(AZ_PAT),
    )


async def _seed_active_run(pe_db, run_id: str, *, provider: str, project_id: int, issue: int):
    """An active (non-terminal) run on the trace's subject for /pause to target."""
    from forge.durable.models import FlowRun

    factory = pe_db.worker_factory()
    async with factory() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=project_id,
                issue_iid=issue,
                provider=provider,
                status="waiting_approval",
            )
        )
        await session.commit()


def _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path) -> None:
    """The worker's control-plane mailbox is the durable Postgres one over
    the trace's own database — the mailbox rows are assertable state."""
    monkeypatch.setenv("DATABASE_URL", pe_db.url)
    monkeypatch.setenv("FORGE_CONTROL_MAILBOX", "postgres")
    monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "di-parity-checkpoints"))
    reset_shared_control_service()


def _pause_landed_predicate(pe_db, run_id: str):
    """The fresh worker's success predicate: the durable pause fence stands."""

    async def _pause_landed() -> bool:
        fences = await _pause_fence_rows(pe_db, run_id)
        return bool(fences)

    return _pause_landed


# ----------------------------------------------------------------------
# The parity arms — GitHub
# ----------------------------------------------------------------------


class TestGitHubDurableIngressParity:
    async def test_p02_schedule_marker_ok_db_fail_retries_land_one_command(
        self, pe_db, native, tmp_path, redis_server, monkeypatch
    ):
        """SET-NX "succeeds", the SQL transaction fails, the retries arrive
        within the TTL → ONE durable command, never a successful empty
        duplicate (the #357 P02 schedule, on the GitHub ingress)."""
        run_id = "1" * 31 + "1"
        await _seed_active_run(
            pe_db, run_id, provider="github", project_id=GH_REPO_ID, issue=GH_ISSUE_NUMBER
        )
        gateway = start_parity_gateway(
            tmp_path,
            db_url=pe_db.url,
            gateway="github",
            reply_url=native.base_url,
            redis_url=redis_server.url,
            db_failures=1,
        )
        try:
            first = await _post_github(gateway, 7101, "/pause")
            assert first is not None and first.status_code == 503  # honest refusal

            retry = await _post_github(gateway, 7101, "/pause")
            assert retry is not None and retry.status_code == 202
            assert retry.json()["adaptive_command"] is True  # NOT an empty duplicate

            manual = await _post_github(gateway, 7101, "/pause")
            assert manual is not None and manual.status_code == 202
            assert manual.json()["deduplicated"] is True
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert len(inbox) == 1 and len(steps) == 1  # ONE durable command

        _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path)
        await _resume_worker(
            pe_db, _github_worker_settings(native.base_url), _pause_landed_predicate(pe_db, run_id)
        )
        commands = await _control_rows(pe_db, "pause")
        assert len(commands) == 1
        pauses = [b for b in native.comments() if "Pause recorded" in b]
        assert len(pauses) == 1  # ONE operator reply

    async def test_an_orphaned_marker_proceeds_as_a_first_delivery(
        self, pe_db, native, tmp_path, redis_server
    ):
        """A marker whose record never landed (the pre-commit SET-NX of a
        failed transaction under the old code) must not suppress the
        command — the authoritative lookup misses and the delivery ingests."""
        from forge.utils.redis_client import RedisManager
        from forge.worker.queue import DEDUP_PREFIX

        manager = RedisManager(redis_server.url)
        try:
            await manager.set_ex(
                f"{DEDUP_PREFIX}run:github:{GH_INSTALLATION}:{GH_REPO}:7102", "1", 300
            )
        finally:
            await manager.close()

        gateway = start_parity_gateway(
            tmp_path,
            db_url=pe_db.url,
            gateway="github",
            reply_url=native.base_url,
            redis_url=redis_server.url,
        )
        try:
            response = await _post_github(gateway, 7102, "/pause")
            assert response is not None and response.status_code == 202
            body = response.json()
            assert body["adaptive_command"] is True  # first delivery, not deduplicated
            assert "deduplicated" not in body
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert len(inbox) == 1 and len(steps) == 1

    async def test_kill_after_response_the_pause_recovers_without_resend(
        self, pe_db, native, tmp_path, monkeypatch
    ):
        """The #357 headline defect, on the GitHub ingress: an acknowledged
        /pause killed before the routing. Under the durable rule the
        scheduled step IS the command — the fresh worker routes it: mailbox
        row, pause fence, ONE operator reply."""
        run_id = "2" * 31 + "2"
        await _seed_active_run(
            pe_db, run_id, provider="github", project_id=GH_REPO_ID, issue=GH_ISSUE_NUMBER
        )
        gateway = start_parity_gateway(
            tmp_path,
            db_url=pe_db.url,
            gateway="github",
            reply_url=native.base_url,
            kill="after-response",
        )
        try:
            response = await _post_github(gateway, 7103, "/pause")
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
        await _resume_worker(
            pe_db, _github_worker_settings(native.base_url), _pause_landed_predicate(pe_db, run_id)
        )
        commands = await _control_rows(pe_db, "pause")
        assert len(commands) == 1  # the durable mailbox record
        assert commands[0].work_id == run_id
        fences = await _pause_fence_rows(pe_db, run_id)
        assert len(fences) == 1  # the durable pause fence stands
        pauses = [b for b in native.comments() if "Pause recorded" in b]
        assert len(pauses) == 1  # ONE operator reply

    async def test_exact_replay_is_one_logical_command(self, pe_db, native, tmp_path, monkeypatch):
        """The same delivery twice → ONE logical command and ONE operator
        reply (the reply journal collapses the re-execution)."""
        run_id = "3" * 31 + "3"
        await _seed_active_run(
            pe_db, run_id, provider="github", project_id=GH_REPO_ID, issue=GH_ISSUE_NUMBER
        )
        gateway = start_parity_gateway(
            tmp_path, db_url=pe_db.url, gateway="github", reply_url=native.base_url
        )
        try:
            first = await _post_github(gateway, 7104, "/pause")
            replay = await _post_github(gateway, 7104, "/pause")
            assert first is not None and first.json()["adaptive_command"] is True
            assert replay is not None and replay.json()["deduplicated"] is True
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert len(inbox) == 1 and len(steps) == 1

        _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path)
        await _resume_worker(
            pe_db, _github_worker_settings(native.base_url), _pause_landed_predicate(pe_db, run_id)
        )
        commands = await _control_rows(pe_db, "pause")
        assert len(commands) == 1
        pauses = [b for b in native.comments() if "Pause recorded" in b]
        assert len(pauses) == 1  # no repeated native effect


# ----------------------------------------------------------------------
# The parity arms — Azure DevOps
# ----------------------------------------------------------------------


class TestAzureDevOpsDurableIngressParity:
    async def test_p02_schedule_marker_ok_db_fail_retries_land_one_command(
        self, pe_db, azdo_sink, tmp_path, redis_server, monkeypatch
    ):
        """The #357 P02 schedule on the Azure DevOps ingress: the honest 503,
        then retries landing ONE durable command."""
        from forge.gateway.azure_webhook import azure_project_key

        run_id = "4" * 31 + "4"
        await _seed_active_run(
            pe_db,
            run_id,
            provider="azure_devops",
            project_id=azure_project_key(AZ_PROJECT_GUID),
            issue=AZ_WORK_ITEM,
        )
        gateway = start_parity_gateway(
            tmp_path,
            db_url=pe_db.url,
            gateway="azure_devops",
            reply_url=azdo_sink.base_url,
            redis_url=redis_server.url,
            db_failures=1,
        )
        try:
            first = await _post_azure(gateway, 21, "/pause")
            assert first is not None and first.status_code == 503  # honest refusal

            retry = await _post_azure(gateway, 21, "/pause")
            assert retry is not None and retry.status_code == 202
            assert retry.json()["adaptive_command"] is True  # NOT an empty duplicate

            manual = await _post_azure(gateway, 21, "/pause")
            assert manual is not None and manual.status_code == 202
            assert manual.json()["deduplicated"] is True
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert len(inbox) == 1 and len(steps) == 1  # ONE durable command

        _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path)
        await _resume_worker(
            pe_db,
            _azure_worker_settings(azdo_sink.base_url),
            _pause_landed_predicate(pe_db, run_id),
        )
        commands = await _control_rows(pe_db, "pause")
        assert len(commands) == 1
        pauses = [b for b in azdo_sink.bodies() if "Pause recorded" in b]
        assert len(pauses) == 1  # ONE operator reply

    async def test_an_orphaned_marker_proceeds_as_a_first_delivery(
        self, pe_db, azdo_sink, tmp_path, redis_server
    ):
        """The orphaned-marker fall-through on the Azure DevOps ingress: the
        authoritative lookup decides, the cache never suppresses."""
        from forge.gateway.azure_webhook import azure_connection_id
        from forge.utils.redis_client import RedisManager
        from forge.worker.queue import DEDUP_PREFIX

        connection = azure_connection_id(AZ_ORG_URL, AZ_PROJECT)
        manager = RedisManager(redis_server.url)
        try:
            await manager.set_ex(
                f"{DEDUP_PREFIX}run:{connection}:workitem:{AZ_WORK_ITEM}:comment:22", "1", 300
            )
        finally:
            await manager.close()

        gateway = start_parity_gateway(
            tmp_path,
            db_url=pe_db.url,
            gateway="azure_devops",
            reply_url=azdo_sink.base_url,
            redis_url=redis_server.url,
        )
        try:
            response = await _post_azure(gateway, 22, "/pause")
            assert response is not None and response.status_code == 202
            body = response.json()
            assert body["adaptive_command"] is True  # first delivery, not deduplicated
            assert "deduplicated" not in body
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert len(inbox) == 1 and len(steps) == 1

    async def test_kill_after_response_the_pause_recovers_without_resend(
        self, pe_db, azdo_sink, tmp_path, monkeypatch
    ):
        """The acknowledged /pause killed before the routing, on the Azure
        DevOps ingress — the committed adaptive step IS the command, the
        fresh worker routes it with NO resend."""
        from forge.gateway.azure_webhook import azure_project_key

        run_id = "5" * 31 + "5"
        await _seed_active_run(
            pe_db,
            run_id,
            provider="azure_devops",
            project_id=azure_project_key(AZ_PROJECT_GUID),
            issue=AZ_WORK_ITEM,
        )
        gateway = start_parity_gateway(
            tmp_path,
            db_url=pe_db.url,
            gateway="azure_devops",
            reply_url=azdo_sink.base_url,
            kill="after-response",
        )
        try:
            response = await _post_azure(gateway, 23, "/pause")
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
        await _resume_worker(
            pe_db,
            _azure_worker_settings(azdo_sink.base_url),
            _pause_landed_predicate(pe_db, run_id),
        )
        commands = await _control_rows(pe_db, "pause")
        assert len(commands) == 1  # the durable mailbox record
        assert commands[0].work_id == run_id
        fences = await _pause_fence_rows(pe_db, run_id)
        assert len(fences) == 1  # the durable pause fence stands
        pauses = [b for b in azdo_sink.bodies() if "Pause recorded" in b]
        assert len(pauses) == 1  # ONE operator reply

    async def test_exact_replay_is_one_logical_command(
        self, pe_db, azdo_sink, tmp_path, monkeypatch
    ):
        """The same delivery twice → ONE logical command and ONE operator
        reply on the Azure DevOps ingress."""
        from forge.gateway.azure_webhook import azure_project_key

        run_id = "6" * 31 + "6"
        await _seed_active_run(
            pe_db,
            run_id,
            provider="azure_devops",
            project_id=azure_project_key(AZ_PROJECT_GUID),
            issue=AZ_WORK_ITEM,
        )
        gateway = start_parity_gateway(
            tmp_path, db_url=pe_db.url, gateway="azure_devops", reply_url=azdo_sink.base_url
        )
        try:
            first = await _post_azure(gateway, 24, "/pause")
            replay = await _post_azure(gateway, 24, "/pause")
            assert first is not None and first.json()["adaptive_command"] is True
            assert replay is not None and replay.json()["deduplicated"] is True
        finally:
            gateway.stop()
        inbox, steps = await _command_rows(pe_db, "adaptive_control")
        assert len(inbox) == 1 and len(steps) == 1

        _enable_durable_control_mailbox(monkeypatch, pe_db, tmp_path)
        await _resume_worker(
            pe_db,
            _azure_worker_settings(azdo_sink.base_url),
            _pause_landed_predicate(pe_db, run_id),
        )
        commands = await _control_rows(pe_db, "pause")
        assert len(commands) == 1
        pauses = [b for b in azdo_sink.bodies() if "Pause recorded" in b]
        assert len(pauses) == 1  # no repeated native effect
