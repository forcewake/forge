"""Retained contrib lessons on the two supported native lane drivers.

Codex/OpenCode/Grok implementations and activation flags are retired. Historical
test names preserve the isolation, exact identity, terminal and quiescence
lessons, now exercised on actual Claude SDK and Copilot ACP boundaries.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from forge_lane.cli import load_driver
from forge_lane.client import LaneClient
from forge_lane.driver import DriverEnv
from forge_lane.drivers.claude_policy import FilePolicy, child_environment
from forge_lane.errors import LaneFailure
from forge_protocol import ModelKeyGrant
from tests.native_driver_fixture import Case, FakeACP, Result, collect, config_path, until


async def test_private_environment_and_grant_identity(tmp_path, monkeypatch):
    case = Case(tmp_path)
    for variable in ("GITHUB_TOKEN", "CI_JOB_TOKEN", "OPENAI_API_KEY", "CODEX_HOME", "XAI_API_KEY"):
        monkeypatch.setenv(variable, "ambient-must-not-propagate")
    child = child_environment(tmp_path / "private", "https://gateway.test", "attempt-only")
    assert "ambient-must-not-propagate" not in child.values()
    assert child["HOME"] != str(case.workspace) and child["ANTHROPIC_AUTH_TOKEN"] == "attempt-only"
    for mutation in (
        {"generation": 1},
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        {"model": "other"},
    ):
        grant = ModelKeyGrant(
            **case.dispatch.model_dump(include={"forge_run_id", "attempt", "generation"}),
            gateway_url="https://gateway.test",
            key="attempt-only",
            model="test-model",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        ).model_copy(update=mutation)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=grant.model_dump(mode="json"))
            )
        ) as http:
            with pytest.raises(LaneFailure):
                await LaneClient(case.dispatch, http).model_key()


async def test_file_tools_refuse_shell_authority_paths_and_symlinks(tmp_path):
    case = Case(tmp_path)
    policy = FilePolicy(case.workspace, case.sdk, scope=case.brief.frozen_spec.write_policy)
    (case.workspace / "escape").symlink_to(tmp_path)
    (case.workspace / "authority").symlink_to(case.workspace / ".git")
    for path in (
        ".git/config",
        ".env",
        ".github/workflows/test.yml",
        "escape/outside",
        "authority/config",
        str(tmp_path / "outside"),
    ):
        assert not policy.permitted("Read", {"file_path": path})
        assert not policy.permitted("Write", {"file_path": path})
    assert not policy.permitted("Bash", {"command": "git push"})
    assert policy.permitted("Write", {"file_path": "src/result.txt"})
    assert (
        await policy.permission("Write", {"file_path": "src/result.txt"}, None)
    ).behavior == "allow"
    assert (await policy.permission("Write", {"file_path": ".env"}, None)).behavior == "deny"


async def test_codex_handshake_file_tools_completion_and_steer(tmp_path):
    case = Case(tmp_path)
    session = await case.claude()
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    assert not task.done()
    await session.steer("bounded correction")
    client = case.sdk.clients[0]
    assert (
        client.prompts[1]
        == "Operator guidance; approval and permissions are unchanged:\nbounded correction"
    )
    client.messages.put_nowait(Result(uuid="first"))
    await asyncio.sleep(0)
    assert not task.done()
    client.messages.put_nowait(Result(uuid="second"))
    result = await asyncio.wait_for(task, 2)
    assert result[-1].kind == "completed" and result[-1].usage is None
    assert client.disconnected and not config_path(session).exists()
    assert (
        client.options.tools == ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
        and "WebFetch" in client.options.disallowed_tools
    )


async def test_codex_interrupt_is_quiescent(tmp_path):
    case = Case(tmp_path)
    session = await case.claude()
    await session.interrupt()
    assert session.done.is_set() and session.task.done() and case.sdk.clients[0].disconnected
    assert not config_path(session).exists() and await collect(session) == []


async def test_codex_refuses_effective_shell_and_foreign_turn_tool(tmp_path, monkeypatch):
    case = Case(tmp_path, driver="copilot_cli")
    wire = FakeACP(automatic=False)
    session = await case.copilot(wire, monkeypatch)
    await until(lambda: wire.prompt_id is not None)
    wire.frames.put_nowait(
        {
            "jsonrpc": "2.0",
            "id": 900,
            "method": "session/request_permission",
            "params": {
                "sessionId": "foreign-session",
                "toolCall": {"kind": "edit", "rawInput": {"path": "must-not-exist"}},
                "options": [],
            },
        }
    )
    with pytest.raises(LaneFailure) as failure:
        await asyncio.wait_for(collect(session), 2)
    assert failure.value.classification == "driver_protocol_error"
    assert not (case.workspace / "must-not-exist").exists() and wire.closed


async def test_opencode_sets_model_permissions_and_waits_for_terminal_transcript(
    tmp_path, monkeypatch
):
    case = Case(tmp_path, driver="copilot_cli")
    wire = FakeACP(automatic=False)
    session = await case.copilot(wire, monkeypatch)
    await until(lambda: wire.prompt_id is not None)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    assert not task.done() and case.received[0].model == "test-model"
    assert wire.sent[0]["params"]["clientCapabilities"]["terminal"] is False
    assert wire.sent[1]["params"]["mcpServers"] == []
    wire.respond(wire.prompt_id, {"stopReason": "end_turn"})
    result = await asyncio.wait_for(task, 2)
    assert result[-1].kind == "completed" and result[-1].usage is None and wire.closed


async def test_opencode_rejects_shell_permission_and_unconfirmed_rules(tmp_path, monkeypatch):
    case = Case(tmp_path, driver="copilot_cli")
    wire = FakeACP(
        permission={
            "toolCall": {"kind": "execute", "rawInput": {"command": "git push"}},
            "options": [{"kind": "allow_once", "optionId": "yes"}],
        }
    )
    session = await case.copilot(wire, monkeypatch)
    with pytest.raises(LaneFailure) as failure:
        await collect(session)
    assert failure.value.classification == "tool_policy_refused" and wire.closed
    response = next(frame for frame in wire.sent if frame.get("id") == 1000)
    assert response["result"]["outcome"] == {"outcome": "cancelled"}


async def test_grok_has_restricted_tools_explicit_auth_and_unknown_usage(tmp_path, monkeypatch):
    case = Case(tmp_path, driver="copilot_cli")
    monkeypatch.setenv("GITHUB_TOKEN", "repository-token-must-not-authorize")
    monkeypatch.setenv("XAI_API_KEY", "ambient-key-must-not-win")
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    from forge_lane.drivers.copilot_cli import CopilotCLIDriver

    with pytest.raises(LaneFailure) as failure:
        await CopilotCLIDriver().start(case.brief, case.workspace, DriverEnv("test-model"))
    assert failure.value.classification == "copilot_token_required"
    wire = FakeACP()
    session = await case.copilot(wire, monkeypatch)
    result = await collect(session)
    assert result[-1].kind == "completed" and result[-1].usage is None and wire.closed
    assert case.received[0].token == "synthetic-copilot-only" and not config_path(session).exists()


async def test_grok_eof_does_not_invent_success(tmp_path, monkeypatch):
    case = Case(tmp_path, driver="copilot_cli")
    wire = FakeACP(automatic=False)
    session = await case.copilot(wire, monkeypatch)
    await until(lambda: wire.prompt_id is not None)
    wire.frames.put_nowait(None)
    with pytest.raises(LaneFailure) as failure:
        await asyncio.wait_for(collect(session), 2)
    assert failure.value.classification == "driver_process_error" and wire.closed


def test_contrib_requires_explicit_runtime_activation(monkeypatch):
    # No activation setting can enlarge the sole two-driver runtime.
    monkeypatch.setenv("FORGE_LANE_ENABLE_CONTRIB", "1")
    for name in ("codex", "opencode", "grok", "claude-sdk-lane", "copilot-sdk-lane"):
        with pytest.raises(LaneFailure) as failure:
            load_driver(name)
        assert failure.value.classification == "driver_unavailable"
    assert load_driver("claude_code").name == "claude_code"
    assert load_driver("copilot_cli").name == "copilot_cli"


async def test_opencode_grants_only_one_confined_file_operation(tmp_path, monkeypatch):
    case = Case(tmp_path, driver="copilot_cli")
    wire = FakeACP(
        permission={
            "toolCall": {
                "kind": "edit",
                "rawInput": {"path": "result.py"},
                "locations": [{"path": "result.py"}],
            },
            "options": [
                {"kind": "allow_once", "optionId": "once"},
                {"kind": "allow_always", "optionId": "always"},
            ],
        }
    )
    session = await case.copilot(wire, monkeypatch)
    result = await collect(session)
    response = next(frame for frame in wire.sent if frame.get("id") == 1000)
    assert response["result"]["outcome"] == {"outcome": "selected", "optionId": "once"}
    assert result[-1].kind == "completed" and wire.closed


@pytest.mark.parametrize("driver", ["claude_code", "copilot_cli"])
async def test_interrupt_does_not_swallow_process_cleanup_failure(tmp_path, monkeypatch, driver):
    case = Case(tmp_path, driver=driver)

    async def cannot_stop():
        raise RuntimeError("synthetic process did not stop")

    if driver == "claude_code":
        session = await case.claude()
        monkeypatch.setattr(session.owned, "terminate", cannot_stop)
    else:
        wire = FakeACP(automatic=False)
        session = await case.copilot(wire, monkeypatch)
        wire.close = cannot_stop
    for _ in range(2):
        with pytest.raises(LaneFailure) as failure:
            await session.interrupt()
        assert failure.value.classification == "driver_cleanup_failed"
    assert not config_path(session).exists()


@pytest.mark.parametrize("consume", [False, True])
async def test_codex_completion_reports_cleanup_failure_without_success(
    tmp_path, monkeypatch, consume
):
    case = Case(tmp_path)
    session = await case.claude()
    close_calls = []

    async def cannot_stop():
        close_calls.append(True)
        raise RuntimeError("synthetic process did not stop")

    monkeypatch.setattr(session.owned, "terminate", cannot_stop)
    case.sdk.clients[0].messages.put_nowait(Result())
    await asyncio.wait_for(session.done.wait(), 2)
    assert session.task.exception() is None
    if consume:
        with pytest.raises(LaneFailure) as failure:
            await collect(session)
        assert failure.value.classification == "driver_cleanup_failed"
        assert session.queue.get_nowait() is None and session.queue.empty()
    else:
        result = [session.queue.get_nowait(), session.queue.get_nowait()]
        assert (
            isinstance(result[0], LaneFailure)
            and result[0].classification == "driver_cleanup_failed"
        )
        assert result[1] is None and session.queue.empty()
    for _ in range(2):
        with pytest.raises(LaneFailure):
            await session.interrupt()
    assert close_calls == [True] and not config_path(session).exists()


async def test_codex_cancelled_interrupt_does_not_cancel_owned_cleanup(tmp_path, monkeypatch):
    case = Case(tmp_path)
    session = await case.claude()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_close():
        calls.append(True)
        entered.set()
        await release.wait()

    monkeypatch.setattr(session.owned, "terminate", slow_close)
    stopping = asyncio.create_task(session.interrupt())
    await asyncio.wait_for(entered.wait(), 2)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert not session.task.done() and not session.task.cancelled()
    release.set()
    await asyncio.wait_for(session.interrupt(), 2)
    assert calls == [True] and session.task.done() and case.sdk.clients[0].disconnected
    assert await collect(session) == [] and not config_path(session).exists()


async def test_shared_stream_finalizer_never_swallows_cancellation(tmp_path):
    case = Case(tmp_path)
    session = await case.claude()
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.interrupt()
    assert session.done.is_set() and session.task.done() and not config_path(session).exists()
