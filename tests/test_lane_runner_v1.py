"""The lane runs against a real Git tree and an in-process wire server."""

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from forge_lane.client import LaneClient
from forge_lane.driver import DriverEnv, DriverEvent
from forge_lane.errors import ExitCode
from forge_lane.runner import Runner
from forge_lane.supervisor import Timings
from forge_protocol import (
    CandidateBundle,
    CheckpointManifest,
    ControlCommand,
    LaneDispatch,
    LaneReport,
    SessionRequest,
    SessionToken,
    RunIdentity,
    approved_input_digest,
    canonical_json,
    digest,
)

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import BudgetLimits
from tests.test_domain_approved_input import spec


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True).stdout


def checkout(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "lane@example.invalid")
    git(path, "config", "user.name", "Lane test")
    (path / "base.txt").write_text("base\n")
    (path / "delete.txt").write_text("remove me\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return git(path, "rev-parse", "HEAD").decode().strip()


def inputs(base, **overrides):
    identity = dict(forge_run_id="run-1", attempt=0, generation=0)
    frozen = spec(
        base_sha=base,
        driver=overrides.get("driver", "fake"),
        metering="copilot-scoped-unmetered"
        if overrides.get("driver") == "copilot_cli"
        else "metered",
        model=overrides.get("model", "test-model"),
        limits=BudgetLimits(
            usd=Decimal("10"),
            calls=20,
            tokens=10000,
            wallclock_seconds=overrides.get("timeout_seconds", 10),
        ),
    )
    approved = ApprovedInput.from_spec(frozen)
    dispatch = LaneDispatch(
        **{
            **identity,
            "server_url": "http://lane.test",
            "provider": "gitlab",
            "repository": "team/repo",
            "ref": "main",
            "base_sha": base,
            "approved_digest": approved.approved_digest,
            "driver": frozen.driver,
            "model": frozen.model,
            "timeout_seconds": frozen.limits.wallclock_seconds,
            **overrides,
        }
    )
    brief = approved.to_brief(
        RunIdentity(**{**identity, "generation": dispatch.generation}), frozen
    )
    return dispatch, brief


class WireServer:
    def __init__(self, dispatch, brief):
        self.dispatch, self.brief = dispatch, brief.model_dump(mode="json")
        self.app = FastAPI()
        self.blobs, self.checkpoints = {}, []
        self.candidates, self.reports, self.acks, self.heartbeats = [], [], [], []
        self.commands, self.calls = [], []
        self.corrupt_blob = False
        self.reject_report = False
        self.model_keys = 0
        self.bad_session = False
        self.app.api_route("/lane/v1/{path:path}", methods=["GET", "POST", "PUT"])(self.handle)

    async def handle(self, request: Request, path: str):
        self.calls.append((request.method, path))
        if path == "session":
            body = SessionRequest.model_validate(await request.json())
            assert body.oidc_token == "ci-oidc"
            return SessionToken(
                forge_run_id=body.forge_run_id,
                attempt=body.attempt,
                generation=body.generation + int(self.bad_session),
                access_token="session-only",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        assert request.headers.get("authorization") == "Bearer session-only"
        if path == "brief":
            return self.brief
        if path == "controls":
            after = int(request.query_params["after"])
            return [command for command in self.commands if command["sequence"] > after]
        if path.startswith("controls/"):
            body = await request.json()
            self.acks.append(body)
            return body
        if path.startswith("blobs/"):
            key = path.split("/")[1]
            if request.method == "PUT":
                data = await request.body()
                assert sha256(data).hexdigest() == key
                self.blobs[key] = data
                return Response(status_code=204)
            return Response(b"corrupt" if self.corrupt_blob else self.blobs[key])
        if path == "checkpoints/latest":
            return self.checkpoints[-1] if self.checkpoints else Response(status_code=404)
        if path.startswith("checkpoints/"):
            body = await request.json()
            manifest = CheckpointManifest.model_validate(body)
            assert path.split("/")[1] == manifest.checkpoint_id
            self.checkpoints.append(body)
            return body
        if path == "candidate":
            body = await request.json()
            bundle = CandidateBundle.model_validate(body)
            assert len(self.blobs[bundle.diff_sha256]) == bundle.diff_size_bytes
            self.candidates.append(bundle)
            return body
        if path == "report":
            if self.reject_report:
                return JSONResponse({"detail": "unavailable"}, status_code=503)
            body = await request.json()
            self.reports.append(LaneReport.model_validate(body))
            return body
        if path == "heartbeat":
            body = await request.json()
            self.heartbeats.append(body)
            return body
        if path == "model-key":
            self.model_keys += 1
            return {
                **self.dispatch.model_dump(include={"forge_run_id", "attempt", "generation"}),
                "gateway_url": "https://gateway.test",
                "key": "scoped-key",
                "model": "test-model",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            }
        return Response(status_code=404)

    def command(self, action, sequence=1, text=None):
        self.commands.append(
            ControlCommand(
                forge_run_id=self.dispatch.forge_run_id,
                attempt=self.dispatch.attempt,
                generation=self.dispatch.generation,
                command_id=f"command-{sequence}",
                sequence=sequence,
                action=action,
                text=text,
            ).model_dump(mode="json")
        )


class FakeSession:
    def __init__(self, driver):
        self.driver = driver
        self.stopped = asyncio.Event()
        self.steered = []

    async def events(self):
        if self.driver.wait:
            yield DriverEvent("message", resume_session="conversation-1")
            await self.stopped.wait()
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.02)
        if self.driver.failure:
            raise RuntimeError("vendor failed with a secret which must not be logged")
        yield DriverEvent("completed", terminal_reason="work complete")

    async def interrupt(self):
        self.driver.interrupted += 1
        self.stopped.set()

    async def steer(self, text):
        self.steered.append(text)


class FakeDriver:
    name = "fake"
    capabilities = frozenset({"interrupt", "steer", "resume_session"})
    requires_model_key = True

    def __init__(self, edit=None, *, wait=False, failure=False):
        self.edit, self.wait, self.failure = edit, wait, failure
        self.starts, self.interrupted = [], 0
        self.session = FakeSession(self)

    async def prepare(self, brief, workspace):
        assert brief.frozen_spec.driver == self.name

    async def start(self, brief, workspace, env):
        assert isinstance(env, DriverEnv)
        self.starts.append((brief, env))
        if self.edit:
            self.edit(workspace, env)
        return self.session


async def execute(dispatch, server, driver, workspace, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app)) as http:
        runner = Runner(
            dispatch,
            LaneClient(dispatch, http),
            driver,
            workspace,
            timings=Timings(controls=0.005, heartbeat=0.005, checkpoint=10),
            **kwargs,
        )
        return await runner.execute("ci-oidc")


async def test_success_collects_untracked_binary_and_ignores_real_index(tmp_path):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server = WireServer(dispatch, brief)
    original_index = b"deliberately not a git index"
    binary = b"\x00\xff\x01binary content"

    def edit(path, env):
        (path / "base.txt").write_text("changed\n")
        (path / "binary.bin").write_bytes(binary)
        (path / ".forge").mkdir()
        (path / ".forge" / "secret").write_text("must stay private")
        (path / ".git" / "index").write_bytes(original_index)

    driver = FakeDriver(edit)
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code == ExitCode.COMPLETED
    assert result.report_accepted and result.report.status == "completed"
    candidate = server.candidates[0]
    diff = server.blobs[candidate.diff_sha256]
    assert b"GIT binary patch" in diff and b"base.txt" in diff
    assert b".forge" not in diff and b"must stay private" not in diff
    assert (workspace / ".git" / "index").read_bytes() == original_index
    assert server.heartbeats and server.model_keys == 1
    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", "-q", str(workspace), str(fresh))
    subprocess.run(["git", "-C", str(fresh), "apply", "--binary", "-"], input=diff, check=True)
    assert (fresh / "binary.bin").read_bytes() == binary
    assert (fresh / "base.txt").read_text() == "changed\n"


async def test_pause_exits_then_new_generation_restores_exact_files_before_start(tmp_path):
    workspace = tmp_path / "first"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server = WireServer(dispatch, brief)
    binary = b"\x00\xffWIP"

    def edit(path, env):
        (path / "base.txt").write_text("work in progress\n")
        (path / "delete.txt").unlink()
        (path / "new.bin").write_bytes(binary)
        (path / "new.bin").chmod(0o755)

    server.command("pause")
    driver = FakeDriver(edit, wait=True)
    first = await execute(dispatch, server, driver, workspace)
    assert first.exit_code == ExitCode.PAUSED and first.report_accepted
    assert driver.interrupted == 1 and not server.candidates
    checkpoint = CheckpointManifest.model_validate(server.checkpoints[-1])
    assert first.report.checkpoint_id == checkpoint.checkpoint_id
    assert {entry.state for entry in checkpoint.entries} == {"new", "modified", "deleted"}
    assert server.acks[0]["checkpoint_id"] == checkpoint.checkpoint_id
    fresh = tmp_path / "resumed"
    git(tmp_path, "clone", "-q", str(workspace), str(fresh))
    resumed, resumed_brief = inputs(base, generation=1, checkpoint_id=checkpoint.checkpoint_id)
    second_server = WireServer(resumed, resumed_brief)
    second_server.checkpoints = server.checkpoints
    second_server.blobs = server.blobs

    def inspect(path, env):
        assert (path / "base.txt").read_text() == "work in progress\n"
        assert not (path / "delete.txt").exists()
        assert (path / "new.bin").read_bytes() == binary
        assert (path / "new.bin").stat().st_mode & 0o111
        assert "modified: base.txt" in env.resume_note

    second_driver = FakeDriver(inspect)
    second = await execute(resumed, second_server, second_driver, fresh)
    assert second.exit_code == ExitCode.COMPLETED and second.report_accepted
    assert second_server.candidates[0].generation == 1


async def test_deadline_interrupts_before_reporting_and_never_publishes(tmp_path):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base, timeout_seconds=1)
    server, driver = WireServer(dispatch, brief), FakeDriver(wait=True)
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code == ExitCode.DEADLINE
    assert result.report.status == "timed_out" and driver.interrupted == 1
    assert not server.candidates


@pytest.mark.parametrize(
    "tamper", ["brief_text", "dispatch_digest", "generation", "missing_spec", "null_spec"]
)
async def test_unapproved_or_cross_generation_brief_never_starts_driver(tmp_path, tamper):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server, driver = WireServer(dispatch, brief), FakeDriver()
    if tamper == "dispatch_digest":
        dispatch = dispatch.model_copy(update={"approved_digest": "c" * 64})
    elif tamper == "brief_text":
        server.brief["brief_text"] = "tampered"
    elif tamper == "missing_spec":
        del server.brief["spec_json"]
    elif tamper == "null_spec":
        server.brief["spec_json"] = None
    else:
        server.brief["generation"] += 1
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code in (ExitCode.APPROVAL, ExitCode.TRANSPORT)
    assert not driver.starts and not server.candidates and server.model_keys == 0


@pytest.mark.parametrize("fault", ["unbound", "unknown_spec", "unknown_policy"])
async def test_unbound_or_unknown_spec_refuses_before_driver_or_key(tmp_path, fault):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server, driver = WireServer(dispatch, brief), FakeDriver()
    driver.requires_model_key = True
    document = {
        "schema": "forge.run-spec/2",
        "base_sha": base,
        "revision": 1,
        "plan": {"summary": "Approved plan"},
        "execution_policy": {"schema": "forge.execution-policy/1"},
    }
    if fault == "unknown_spec":
        document["schema"] = "forge.run-spec/999"
    elif fault == "unknown_policy":
        document["execution_policy"]["schema"] = "forge.execution-policy/999"
    server.brief["spec_json"] = canonical_json(document).decode("utf-8")
    if fault != "unbound":
        server.brief["spec_digest"] = digest(document)
        server.brief["plan_digest"] = digest(document["plan"])
        approval = approved_input_digest(
            **{
                key: server.brief[key]
                for key in ("brief_text", "plan_digest", "spec_digest", "base_sha", "revision")
            }
        )
        server.brief["approved_digest"] = approval
        dispatch = dispatch.model_copy(update={"approved_digest": approval})
    assert json.loads(server.brief["spec_json"])["base_sha"] == base
    result = await execute(dispatch, server, driver, workspace)
    assert result.classification == "invalid_brief"
    assert not driver.starts and server.model_keys == 0 and not server.candidates


async def test_empty_diff_is_honest_and_git_failure_is_different(tmp_path):
    workspace = tmp_path / "empty"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server = WireServer(dispatch, brief)
    result = await execute(dispatch, server, FakeDriver(), workspace)
    assert result.exit_code == ExitCode.EMPTY_DIFF and result.classification == "empty_diff"
    assert result.report.status == "failed" and not server.candidates
    broken = tmp_path / "broken"
    broken.mkdir()
    server = WireServer(dispatch, brief)
    result = await execute(dispatch, server, FakeDriver(), broken)
    assert result.exit_code == ExitCode.GIT and result.classification == "git_failed"


async def test_driver_exception_and_report_rejection_never_claim_success(tmp_path):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server, driver = WireServer(dispatch, brief), FakeDriver(failure=True)
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code == ExitCode.DRIVER and driver.interrupted == 1
    assert "secret" not in result.report.terminal_reason
    server = WireServer(dispatch, brief)
    server.reject_report = True
    result = await execute(dispatch, server, FakeDriver(), workspace)
    assert result.exit_code == ExitCode.TRANSPORT and not result.report_accepted


async def test_ordered_controls_apply_steer_checkpoint_and_interrupt_but_refuse_inplace_resume(
    tmp_path,
):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server = WireServer(dispatch, brief)
    server.command("steer", 1, "Use the approved approach")
    server.command("checkpoint", 2)
    server.command("resume", 3)
    server.command("interrupt", 4)
    driver = FakeDriver(lambda path, env: (path / "base.txt").write_text("WIP"), wait=True)
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code == ExitCode.CANCELLED and driver.interrupted == 1
    assert driver.session.steered == ["Use the approved approach"]
    assert [ack["status"] for ack in server.acks] == ["applied", "applied", "refused", "applied"]
    assert [ack["sequence"] for ack in server.acks] == [1, 2, 3, 4]
    assert server.acks[1]["checkpoint_id"] and len(server.checkpoints) == 1
    assert not server.candidates


async def test_periodic_checkpoint_is_saved_while_driver_is_running(tmp_path):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server = WireServer(dispatch, brief)
    driver = FakeDriver(lambda path, env: (path / "base.txt").write_text("periodic WIP"), wait=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app)) as http:
        runner = Runner(
            dispatch,
            LaneClient(dispatch, http),
            driver,
            workspace,
            timings=Timings(controls=0.005, heartbeat=0.005, checkpoint=0.02),
        )
        task = asyncio.create_task(runner.execute("ci-oidc"))
        try:
            async with asyncio.timeout(3):
                while not server.checkpoints:
                    await asyncio.sleep(0.005)
            assert not task.done()
            server.command("pause")
            result = await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert result.exit_code == ExitCode.PAUSED
    assert len(server.checkpoints) >= 2
    assert server.checkpoints[-1]["sequence"] > server.checkpoints[0]["sequence"]


async def test_corrupt_checkpoint_halts_before_any_key_or_driver_start_and_before_write(tmp_path):
    from forge_lane.checkpoint import Checkpoints
    from forge_lane.workspace import Workspace

    workspace = tmp_path / "first"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server = WireServer(dispatch, brief)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app)) as http:
        client = LaneClient(dispatch, http)
        await client.exchange("ci-oidc")
        (workspace / "base.txt").write_text("WIP")
        manifest = await Checkpoints(dispatch, client, Workspace(workspace, base)).capture()
    fresh = tmp_path / "resumed"
    git(tmp_path, "clone", "-q", str(workspace), str(fresh))
    resumed, brief = inputs(base, generation=1, checkpoint_id=manifest.checkpoint_id)
    server = WireServer(resumed, brief)
    server.checkpoints = [manifest.model_dump(mode="json")]
    server.corrupt_blob = True
    driver = FakeDriver()
    driver.requires_model_key = True
    result = await execute(resumed, server, driver, fresh)
    assert result.exit_code == ExitCode.CHECKPOINT and result.classification == "corrupt_blob"
    assert not driver.starts and server.model_keys == 0
    assert (fresh / "base.txt").read_text() == "base\n"
    assert not server.candidates


async def test_candidate_byte_limit_refuses_without_upload(tmp_path):
    from forge_lane.workspace import Limits

    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server = WireServer(dispatch, brief)
    driver = FakeDriver(lambda path, env: (path / "base.txt").write_text("changed"))
    result = await execute(dispatch, server, driver, workspace, limits=Limits(diff_bytes=1))
    assert result.exit_code == ExitCode.GIT and result.classification == "candidate_too_large"
    assert not server.candidates and not server.blobs


async def test_check_command_never_loads_drivers_or_calls_network(tmp_path, monkeypatch, capsys):
    import argparse
    from forge_lane import cli

    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, _ = inputs(base)
    document = tmp_path / "dispatch.json"
    document.write_text(dispatch.model_dump_json())

    def forbidden(*args, **kwargs):
        raise AssertionError("Configuration check must stay local")

    monkeypatch.setattr(cli, "load_driver", forbidden)
    monkeypatch.setattr(cli.httpx, "AsyncClient", forbidden)
    result = await cli.run(argparse.Namespace(dispatch=document, workspace=workspace, check=True))
    assert result == 0 and '"configuration": "valid"' in capsys.readouterr().out


async def test_github_oidc_audience_uses_dispatch_nonce_without_shared_secret_fallback(
    tmp_path, monkeypatch
):
    from forge_lane.cli import oidc_token

    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, _ = inputs(base, provider="github", nonce="single-use-nonce")
    monkeypatch.delenv("FORGE_OIDC_TOKEN", raising=False)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.test/token?existing=value")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-credential")

    def exchange(request):
        assert request.url.params["audience"] == "http://lane.test/lane/single-use-nonce"
        assert request.url.params["existing"] == "value"
        assert request.headers["authorization"] == "Bearer request-credential"
        return httpx.Response(200, json={"value": "ci-oidc"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as http:
        assert await oidc_token(dispatch, "FORGE_OIDC_TOKEN", http) == "ci-oidc"


async def test_wrong_session_identity_never_crosses_into_authenticated_calls(tmp_path):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = inputs(base)
    server, driver = WireServer(dispatch, brief), FakeDriver()
    server.bad_session = True
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code == ExitCode.TRANSPORT and result.classification == "identity_mismatch"
    assert not result.report_accepted and not driver.starts
    assert server.calls == [("POST", "session")]
