"""lane_driver tests (EXE-02): the runner-side entry over the sdk seams.

The lane entry (``python -m forge.lane_driver [--driver claude|codex|opencode]``)
drives the REAL interactive driver clients:

- the claude lane over the REAL
  :class:`~forge.adaptive.drivers.claude_sdk.ClaudeSDKDriverClient` — the
  ``claude-agent-sdk`` package is not installed here (CI installs no vendor
  packages), so these contract tests fake the SDK MODULE exactly the way
  ``tests/test_adaptive_driver_claude_sdk.py`` does and run the lane through
  the real client built by :func:`claude_sdk_client_from_env` (via the
  ``sdk=`` seam);
- the codex lane (``CodexAppDriverClient``, ``codex app-server`` over
  stdio) and the opencode lane (``OpenCodeServer`` spawner +
  ``OpenCodeDriverClient`` over HTTP) through fakes installed at the
  MODULE SEAM (``lane_driver.codex_app_client_from_env`` /
  ``opencode_server_from_env`` / ``opencode_client_from_env``) — no
  subprocess, no server, no network, ever.

For every lane: the brief becomes the ONE task, the turn is driven to its
terminal verdict bounded by the budget, and the batch lane's ``.forge/``
artifact contract comes out the other end — meta (the lane's driver id,
exit classification, usage receipt) + usage.json, with unknown usage
staying unknown, never zero.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

import forge.adaptive.drivers.claude_sdk as claude_sdk_module
import forge.lane_driver as lane_driver
from forge.adaptive.drivers.live_registrations import install_pin_of
from forge.adaptive.wiring import OperatorControlService
from forge.lane_driver import (
    EPISODE_PHASE_KEYS,
    LANE_DRIVER_ID,
    NO_COMMIT_ADDENDUM,
    STEERING_ENV,
    build_task,
    classify_result,
    drive_codex_lane,
    drive_lane,
    main,
    run_opencode_lane,
    steering_enabled,
    usage_receipt,
)

TEMPLATE = (
    Path(__file__).resolve().parent.parent / "ci" / "templates" / ("claude-sdk-lane.gitlab-ci.yml")
)

ATTEMPT_BASE = "b" * 40


# ---------------------------------------------------------------------------
# Fakes mirroring the claude-agent-sdk==0.2.157 wheel surface (same style
# as tests/test_adaptive_driver_claude_sdk.py — injected via sdk=, never
# monkeypatching internals the tests do not own).
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FakeTextBlock:
    text: str


@dataclasses.dataclass
class FakeAssistantMessage:
    content: list
    model: str
    session_id: str | None = None


@dataclasses.dataclass
class FakeUserMessage:
    content: str | list


@dataclasses.dataclass
class FakeSystemMessage:
    subtype: str
    data: dict


@dataclasses.dataclass
class FakeResultMessage:
    """The ResultMessage fields the lane reads (research doc §5)."""

    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    stop_reason: str | None = None
    total_cost_usd: float | None = None
    usage: dict | None = None
    result: str | None = None
    structured_output: Any = None
    model_usage: dict | None = None
    permission_denials: list | None = None
    errors: list | None = None
    api_error_status: int | None = None
    uuid: str | None = None
    terminal_reason: str | None = None


@dataclasses.dataclass
class FakeClaudeAgentOptions:
    allowed_tools: list = dataclasses.field(default_factory=list)
    disallowed_tools: list = dataclasses.field(default_factory=list)
    setting_sources: list | None = None
    permission_mode: str | None = None
    resume: str | None = None
    max_turns: int | None = None
    model: str | None = None
    cwd: str | None = None
    env: dict = dataclasses.field(default_factory=dict)
    can_use_tool: Any = None
    stderr: Any = None


class FakeLaneClient:
    """Stand-in for the SDK's interactive ClaudeSDKClient.

    connect() emits the init SystemMessage carrying the session id (the
    driver's start_session waits for it); each query() appends the turn's
    messages ending in the subclass's ResultMessage. ``interrupt_calls``
    records the budget-interrupt path; subclasses shape the turn.
    """

    #: ResultMessage kwargs for the turn's terminal piece.
    result_kwargs: dict = {"terminal_reason": "completed"}

    def __init__(self, options=None, transport=None):
        self.options = options if options is not None else FakeClaudeAgentOptions()
        self.transport = transport
        self.connected = False
        self.disconnected = False
        self._session_id = f"fake-session-{uuid.uuid4()}"
        self.queries: list[str] = []
        self.interrupt_calls = 0
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def connect(self, prompt=None):
        self.connected = True
        await self._inbox.put(
            FakeSystemMessage(subtype="init", data={"session_id": self._session_id})
        )

    async def query(self, prompt, session_id="default"):
        if not self.connected:
            raise RuntimeError("Not connected. Call connect() first.")
        self.queries.append(prompt)
        await self._inbox.put(FakeUserMessage(content=prompt))
        await self._inbox.put(
            FakeAssistantMessage(content=[FakeTextBlock(text="ack")], model="claude-sonnet-4-5")
        )
        await self._inbox.put(self._result_message())

    async def interrupt(self):
        self.interrupt_calls += 1

    async def receive_messages(self):
        while True:
            yield await self._inbox.get()

    async def disconnect(self):
        self.connected = False
        self.disconnected = True

    def _result_message(self) -> FakeResultMessage:
        kwargs = dict(type(self).result_kwargs)
        return FakeResultMessage(
            subtype=kwargs.pop("subtype", "success"),
            duration_ms=5,
            duration_api_ms=4,
            is_error=kwargs.pop("is_error", False),
            num_turns=3,
            session_id=self._session_id,
            **kwargs,
        )


class CompletedTurnClient(FakeLaneClient):
    result_kwargs = {
        "terminal_reason": "completed",
        "usage": {
            "input_tokens": 120,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
            "output_tokens": 80,
        },
        "total_cost_usd": 0.0947,
        "model_usage": {
            "claude-sonnet-4-5": {"input_tokens": 120, "output_tokens": 80, "cost_usd": 0.09}
        },
    }


class AbortedTurnClient(FakeLaneClient):
    result_kwargs = {"terminal_reason": "aborted_tools"}


class ErrorTurnClient(FakeLaneClient):
    result_kwargs = {
        "subtype": "error_during_execution",
        "is_error": True,
        "terminal_reason": "completed",  # the vendor flag says error; the lane must not trust one field
    }


class BareTurnClient(FakeLaneClient):
    """A completed turn carrying NO usage/cost fields at all."""

    result_kwargs = {"terminal_reason": "completed"}


class SilentTurnClient(FakeLaneClient):
    """A turn that never terminates — the budget path's worst case."""

    async def query(self, prompt, session_id="default"):
        if not self.connected:
            raise RuntimeError("Not connected. Call connect() first.")
        self.queries.append(prompt)
        await self._inbox.put(FakeUserMessage(content=prompt))


class InterruptedTurnClient(SilentTurnClient):
    """Silent, but the interrupt produces the aborted turn's ResultMessage
    (§7.2 — completion keys off the terminal state, not the ack)."""

    async def interrupt(self):
        await super().interrupt()
        await self._inbox.put(
            FakeResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=self._session_id,
                terminal_reason="aborted_streaming",
            )
        )


class FakeSDK:
    """A claude_agent_sdk module stand-in over a configurable client."""

    def __init__(self, base: type[FakeLaneClient] | None = None) -> None:
        self.clients: list[FakeLaneClient] = []
        registry = self

        class Registered(base or FakeLaneClient):
            def __init__(self, options=None, transport=None):
                super().__init__(options=options, transport=transport)
                registry.clients.append(self)

        self.module = ModuleType("claude_agent_sdk")
        self.module.ClaudeAgentOptions = FakeClaudeAgentOptions
        self.module.ClaudeSDKClient = Registered
        self.module.ResultMessage = FakeResultMessage

    @property
    def sole_client(self) -> FakeLaneClient:
        assert len(self.clients) == 1, f"expected exactly one client, got {len(self.clients)}"
        return self.clients[0]


@pytest.fixture
def lane_env(tmp_path, monkeypatch):
    """The lane job's env: cwd is the runner's checkout, brief on disk."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "brief.md").write_text("PLAN: modernize the orders service")
    monkeypatch.setenv("FORGE_ISSUE_IID", "42")
    monkeypatch.setenv("FORGE_ATTEMPT_BASE", ATTEMPT_BASE)
    monkeypatch.setenv("FORGE_CLAUDE_MODEL", "glm-5.3-flash[1m]")
    monkeypatch.setenv("FORGE_LANE_POLL_SECONDS", "0.01")
    return tmp_path


def read_meta(tmp_path) -> dict:
    return json.loads((tmp_path / ".forge" / "candidate.meta.json").read_text())


def read_usage(tmp_path):
    return json.loads((tmp_path / ".forge" / "usage.json").read_text())


# ---------------------------------------------------------------------------
# Task construction
# ---------------------------------------------------------------------------


class TestBuildTask:
    def test_task_carries_the_brief_the_issue_and_the_no_commit_posture(self):
        task = build_task("PLAN: do the thing", "42")

        assert "PLAN: do the thing" in task
        assert "issue #42" in task
        assert task.rstrip().endswith(NO_COMMIT_ADDENDUM)
        assert "Do NOT commit and do NOT push" in NO_COMMIT_ADDENDUM

    def test_issue_reference_is_omitted_when_not_dispatched(self):
        assert "issue #" not in build_task("PLAN: x", "")


# ---------------------------------------------------------------------------
# Classification + usage receipt (pure)
# ---------------------------------------------------------------------------


class TestClassifyResult:
    def test_completed_on_the_live_verified_terminal_reason(self):
        assert classify_result(
            {"terminal_reason": "completed", "is_error": False, "subtype": "success"}
        ) == ("completed", "completed")

    def test_missing_terminal_reason_on_a_successful_turn_stays_completed(self):
        # pre-0.2.118 stacks; the pinned floor has the field but the lane
        # never fabricates a failure from its absence.
        assert classify_result({"is_error": False, "subtype": "success"}) == (
            "completed",
            "completed",
        )

    def test_aborted_terminal_reason_classifies_failed_with_the_reason(self):
        assert classify_result(
            {"terminal_reason": "aborted_tools", "is_error": False, "subtype": "success"}
        ) == ("failed", "aborted_tools")

    def test_error_markers_beat_a_completed_terminal_reason(self):
        assert classify_result(
            {"terminal_reason": "completed", "is_error": True, "subtype": "error_during_execution"}
        ) == ("failed", "error_during_execution")


class TestUsageReceipt:
    RESULT = {
        "usage": {
            "input_tokens": 120,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
            "output_tokens": 80,
        },
        "total_cost_usd": 0.0947,
        "model_usage": {"claude-sonnet-4-5": {"cost_usd": 0.09}},
    }

    def test_vendor_counters_map_onto_the_meta_usage_shape(self):
        receipt = usage_receipt(self.RESULT)

        assert receipt["input_tokens"] == 120
        assert receipt["cached_input_tokens"] == 40
        assert receipt["cache_write_tokens"] == 10
        assert receipt["output_tokens"] == 80
        assert receipt["total_cost_usd"] == 0.0947
        assert receipt["model_usage"] == {"claude-sonnet-4-5": {"cost_usd": 0.09}}
        assert receipt["driver"] == LANE_DRIVER_ID
        assert receipt["completeness"] == "aggregate"

    def test_unknown_stays_unknown_never_zero(self):
        # No usage block at all → None, not a zeroed dict.
        assert usage_receipt({"terminal_reason": "completed"}) is None
        # A cost without tokens is still a receipt; missing tokens stay ABSENT.
        receipt = usage_receipt({"total_cost_usd": 0.01})
        assert receipt is not None
        assert "input_tokens" not in receipt
        assert receipt["total_cost_usd"] == 0.01

    def test_malformed_counters_are_dropped_not_clamped(self):
        receipt = usage_receipt(
            {
                "usage": {
                    "input_tokens": -3,  # negative: dropped
                    "output_tokens": True,  # bool is not an int receipt: dropped
                    "cache_read_input_tokens": "70",  # str: dropped
                }
            }
        )

        assert receipt is None  # nothing parseable survived


# ---------------------------------------------------------------------------
# The driven lane over the sdk seam (contract tests)
# ---------------------------------------------------------------------------


class TestMainCompleted:
    def test_completed_turn_writes_the_batch_contract_and_exits_zero(self, lane_env):
        registry = FakeSDK(CompletedTurnClient)

        assert main(sdk=registry.module) == 0

        meta = read_meta(lane_env)
        assert meta["driver"] == LANE_DRIVER_ID
        assert meta["attempt_base"] == ATTEMPT_BASE
        assert meta["model"] == "glm-5.3-flash[1m]"
        assert meta["exit"] == "completed"
        assert meta["terminal_reason"] == "completed"
        receipt = meta["usage"]
        assert receipt["input_tokens"] == 120
        assert receipt["cached_input_tokens"] == 40
        assert receipt["cache_write_tokens"] == 10
        assert receipt["output_tokens"] == 80
        assert receipt["total_cost_usd"] == 0.0947
        assert receipt["completeness"] == "aggregate"
        # usage.json carries the same receipt beside the meta (batch layout).
        assert read_usage(lane_env) == receipt

    def test_the_brief_becomes_the_one_task(self, lane_env):
        registry = FakeSDK(CompletedTurnClient)

        main(sdk=registry.module)

        (task,) = registry.sole_client.queries
        assert "PLAN: modernize the orders service" in task
        assert "issue #42" in task
        assert "Do NOT commit and do NOT push" in task

    def test_the_lane_adds_no_capability_beyond_the_driver(self, lane_env):
        # The mechanical deny is unioned by the driver client whatever the
        # env said; the lane template's FORBIDDEN push URL is the
        # architectural backstop, and this lane adds nothing on top.
        registry = FakeSDK(CompletedTurnClient)

        main(sdk=registry.module)

        disallowed = set(registry.sole_client.options.disallowed_tools)
        assert {"Bash(git commit:*)", "Bash(git push:*)"} <= disallowed

    def test_turn_without_usage_leaves_the_receipt_unknown(self, lane_env):
        registry = FakeSDK(BareTurnClient)

        assert main(sdk=registry.module) == 0

        assert read_meta(lane_env)["usage"] is None
        assert read_usage(lane_env) is None


class TestMainFailures:
    def test_aborted_turn_is_nonzero_with_the_reason_in_meta(self, lane_env, capsys):
        registry = FakeSDK(AbortedTurnClient)

        assert main(sdk=registry.module) == 1

        meta = read_meta(lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "aborted_tools"
        assert "exit=failed reason=aborted_tools" in capsys.readouterr().err

    def test_error_subtype_is_nonzero_even_with_a_completed_terminal_reason(self, lane_env):
        registry = FakeSDK(ErrorTurnClient)

        assert main(sdk=registry.module) == 1

        assert read_meta(lane_env)["exit"] == "failed"
        assert read_meta(lane_env)["terminal_reason"] == "error_during_execution"

    def test_missing_brief_fails_closed_but_still_writes_the_meta(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no .forge/brief.md here
        monkeypatch.setenv("FORGE_ATTEMPT_BASE", ATTEMPT_BASE)

        assert main(sdk=FakeSDK(CompletedTurnClient).module) == 1

        meta = json.loads((tmp_path / ".forge" / "candidate.meta.json").read_text())
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "brief_missing"

    def test_missing_sdk_fails_with_the_actionable_message(self, lane_env, monkeypatch, capsys):
        monkeypatch.setattr(claude_sdk_module, "_claude_agent_sdk", None)

        assert main() == 1

        meta = read_meta(lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "sdk_missing"
        assert "claude-agent-sdk" in meta["error"]
        assert "claude-agent-sdk" in capsys.readouterr().err

    def test_malformed_budget_env_fails_closed(self, lane_env, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "soon")

        assert main(sdk=FakeSDK(CompletedTurnClient).module) == 1

        assert read_meta(lane_env)["terminal_reason"] == "driver_setup_error"


class TestBudgetBounded:
    def test_budget_expiry_interrupts_and_records_budget_exceeded(self, lane_env, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        monkeypatch.setenv("FORGE_LANE_GRACE_SECONDS", "0.05")
        registry = FakeSDK(SilentTurnClient)

        assert main(sdk=registry.module) == 1

        meta = read_meta(lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "budget_exceeded"
        assert registry.sole_client.interrupt_calls == 1

    def test_the_aborted_result_within_grace_wins_over_budget_exceeded(self, lane_env, monkeypatch):
        # §7.2: the interrupted turn still emits a ResultMessage — the lane
        # records the vendor's reason, not its own timeout.
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        monkeypatch.setenv("FORGE_LANE_GRACE_SECONDS", "0.5")
        registry = FakeSDK(InterruptedTurnClient)

        assert main(sdk=registry.module) == 1

        meta = read_meta(lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "aborted_streaming"


# ---------------------------------------------------------------------------
# The codex lane (CodexAppDriverClient faked at the module seam)
# ---------------------------------------------------------------------------


THREAD_ID = "thr_fake"


def _codex_turn(status: str, *, error: dict | None = None) -> dict:
    turn: dict = {"id": "turn_1", "status": status}
    if error is not None:
        turn["error"] = error
    return {"method": "turn/completed", "params": {"threadId": THREAD_ID, "turn": turn}}


class FakeCodexLaneClient:
    """Stand-in for :class:`CodexAppDriverClient` at the lane seam.

    ``events()`` is the non-consuming buffer read the real client offers;
    the turn verdict (and usage) frames are preloaded. ``interrupt``
    mirrors the real contract: completion keys off the appended
    turn/completed notification — the fakes that produce one append it
    here, a silent fake leaves the lane's grace poll empty-handed.
    """

    def __init__(
        self, events: list[dict] | None = None, *, interrupt_events: list[dict] | None = None
    ):
        self._events = list(events or [])
        self._interrupt_events = list(interrupt_events or [])
        self.tasks: list[str] = []
        self.interrupt_calls = 0
        self.closed = False

    async def start_thread(self, task: str) -> str:
        self.tasks.append(task)
        return THREAD_ID

    def events(self) -> list[dict]:
        return [dict(event) for event in self._events]

    async def interrupt(self, thread_id: str) -> None:
        self.interrupt_calls += 1
        self._events.extend(dict(event) for event in self._interrupt_events)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def codex_lane_env(lane_env, monkeypatch):
    monkeypatch.setenv("FORGE_CODEX_MODEL", "gpt-5.3")
    return lane_env


def install_codex_client(monkeypatch, client: FakeCodexLaneClient) -> FakeCodexLaneClient:
    monkeypatch.setattr(lane_driver, "codex_app_client_from_env", lambda: client)
    return client


class TestCodexLane:
    def test_completed_turn_writes_the_lane_contract_and_exits_zero(
        self, codex_lane_env, monkeypatch, capsys
    ):
        client = install_codex_client(
            monkeypatch,
            FakeCodexLaneClient(
                events=[
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {"inputTokens": 90, "cachedInputTokens": 30, "outputTokens": 45},
                    },
                    _codex_turn("completed"),
                ]
            ),
        )

        assert main(["--driver", "codex"]) == 0

        meta = read_meta(codex_lane_env)
        assert meta["driver"] == "codex-sdk-lane"
        assert meta["attempt_base"] == ATTEMPT_BASE
        assert meta["model"] == "gpt-5.3"
        assert meta["exit"] == "completed"
        assert meta["terminal_reason"] == "completed"
        receipt = meta["usage"]
        assert receipt["input_tokens"] == 90
        assert receipt["cached_input_tokens"] == 30
        assert receipt["output_tokens"] == 45
        assert receipt["driver"] == "codex-sdk-lane"
        assert receipt["completeness"] == "aggregate"
        assert receipt["source"] == "codex-app-server"
        assert read_usage(codex_lane_env) == receipt
        assert "lane_driver: codex-sdk-lane exit=completed reason=completed" in (
            capsys.readouterr().err
        )
        assert client.closed

    def test_the_brief_becomes_the_one_task(self, codex_lane_env, monkeypatch):
        client = install_codex_client(
            monkeypatch, FakeCodexLaneClient(events=[_codex_turn("completed")])
        )

        main(["--driver", "codex"])

        (task,) = client.tasks
        assert "PLAN: modernize the orders service" in task
        assert "issue #42" in task
        assert task.rstrip().endswith(NO_COMMIT_ADDENDUM)

    def test_the_env_dispatch_selects_the_lane_without_a_flag(self, codex_lane_env, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_DRIVER", "codex")
        install_codex_client(monkeypatch, FakeCodexLaneClient(events=[_codex_turn("completed")]))

        assert main() == 0

        assert read_meta(codex_lane_env)["driver"] == "codex-sdk-lane"

    def test_failed_turn_is_nonzero_with_the_vendor_status_and_error(
        self, codex_lane_env, monkeypatch
    ):
        install_codex_client(
            monkeypatch,
            FakeCodexLaneClient(
                events=[_codex_turn("failed", error={"message": "UsageLimitExceeded: quota"})]
            ),
        )

        assert main(["--driver", "codex"]) == 1

        meta = read_meta(codex_lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "failed"
        assert "UsageLimitExceeded" in meta["error"]

    def test_a_turn_without_usage_leaves_the_receipt_unknown(self, codex_lane_env, monkeypatch):
        install_codex_client(monkeypatch, FakeCodexLaneClient(events=[_codex_turn("completed")]))

        assert main(["--driver", "codex"]) == 0

        assert read_meta(codex_lane_env)["usage"] is None
        assert read_usage(codex_lane_env) is None

    def test_budget_expiry_interrupts_and_honors_the_interrupted_verdict(
        self, codex_lane_env, monkeypatch
    ):
        # §5.3: completion keys off turn/completed(interrupted) — the lane
        # records the vendor's status, not its own timeout.
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        monkeypatch.setenv("FORGE_LANE_GRACE_SECONDS", "0.5")
        client = install_codex_client(
            monkeypatch, FakeCodexLaneClient(interrupt_events=[_codex_turn("interrupted")])
        )

        assert main(["--driver", "codex"]) == 1

        meta = read_meta(codex_lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "interrupted"
        assert client.interrupt_calls == 1
        assert client.closed

    def test_a_silent_interrupt_ends_as_budget_exceeded(self, codex_lane_env, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        monkeypatch.setenv("FORGE_LANE_GRACE_SECONDS", "0.05")
        client = install_codex_client(monkeypatch, FakeCodexLaneClient())

        assert main(["--driver", "codex"]) == 1

        meta = read_meta(codex_lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "budget_exceeded"
        assert client.interrupt_calls == 1

    def test_another_threads_completion_is_never_taken(self, codex_lane_env, monkeypatch):
        install_codex_client(
            monkeypatch,
            FakeCodexLaneClient(
                events=[
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thr_other",
                            "turn": {"id": "t9", "status": "completed"},
                        },
                    }
                ],
                interrupt_events=[],
            ),
        )
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        monkeypatch.setenv("FORGE_LANE_GRACE_SECONDS", "0.05")

        assert main(["--driver", "codex"]) == 1

        assert read_meta(codex_lane_env)["terminal_reason"] == "budget_exceeded"


class TestCodexClassification:
    def test_completed_requires_the_completed_status(self):
        assert lane_driver.classify_codex_turn(_codex_turn("completed")["params"]) == (
            "completed",
            "completed",
            "",
        )

    def test_interrupted_and_failed_carry_the_vendor_status(self):
        assert lane_driver.classify_codex_turn(_codex_turn("interrupted")["params"])[:2] == (
            "failed",
            "interrupted",
        )
        failed = lane_driver.classify_codex_turn(
            _codex_turn("failed", error={"message": "boom"})["params"]
        )
        assert failed == ("failed", "failed", "boom")

    def test_a_statusless_frame_never_guesses_success(self):
        assert lane_driver.classify_codex_turn({"turn": {}}) == (
            "failed",
            "turn_end_unobserved",
            "",
        )

    def test_usage_receipt_tolerates_both_vendor_spellings(self):
        camel = lane_driver.codex_usage_receipt(
            [
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {"inputTokens": 5, "outputTokens": 6},
                },
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {"usage": {"input_tokens": 90, "output_tokens": 45}},
                },
            ]
        )
        assert camel["input_tokens"] == 90  # the LAST update wins, never a sum
        assert camel["output_tokens"] == 45

    def test_usage_receipt_stays_none_without_the_event(self):
        assert lane_driver.codex_usage_receipt([_codex_turn("completed")]) is None
        assert lane_driver.codex_usage_receipt([]) is None
        # Malformed counters are dropped, never clamped.
        assert (
            lane_driver.codex_usage_receipt(
                [{"method": "thread/tokenUsage/updated", "params": {"inputTokens": -1}}]
            )
            is None
        )


# ---------------------------------------------------------------------------
# The opencode lane (OpenCodeServer + OpenCodeDriverClient faked at the
# module seam — no serve process, no HTTP, no network)
# ---------------------------------------------------------------------------


SESSION_ID = "ses_fake"


def _opencode_event(event_type: str, data: dict | None = None) -> dict:
    return {"id": None, "type": event_type, "data": {"sessionID": SESSION_ID, **(data or {})}}


class FakeOpenCodeServer:
    """Stand-in for :class:`OpenCodeServer` (the async context manager)."""

    def __init__(self) -> None:
        self.url = "http://127.0.0.1:45678"
        self.password = "lane-pw"  # noqa: S105 - fixture
        self.stopped = False

    async def __aenter__(self) -> FakeOpenCodeServer:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.stopped = True


class FakeOpenCodeLaneClient:
    """Stand-in for :class:`OpenCodeDriverClient` at the lane seam."""

    def __init__(self, events: list[dict] | None = None, *, timeout: bool = False):
        self._events = list(events or [])
        self.timeout = timeout
        self.tasks: list[str] = []
        self.aclosed = False

    async def start_session(self, task: str) -> str:
        self.tasks.append(task)
        if self.timeout:
            raise TimeoutError(
                f"opencode session {SESSION_ID} did not reach "
                "session.execution.succeeded/failed within 0.05s"
            )
        return SESSION_ID

    async def events(self, session_id: str) -> list[dict]:
        return [dict(event) for event in self._events]

    async def aclose(self) -> None:
        self.aclosed = True


@pytest.fixture
def opencode_lane_env(lane_env, monkeypatch):
    monkeypatch.setenv("FORGE_OPENCODE_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("OPENCODE_PROVIDER_ID", "zai")
    monkeypatch.setenv("OPENCODE_PROVIDER_API_KEY", "sk-zai-test")
    return lane_env


def install_opencode(monkeypatch, client: FakeOpenCodeLaneClient, server=None):
    server = server or FakeOpenCodeServer()
    captured: dict = {}

    def fake_factory(provider_key=None, *, env=None):
        captured["provider_key"] = provider_key
        captured["env"] = dict(env or {})
        return client

    monkeypatch.setattr(lane_driver, "opencode_server_from_env", lambda env=None: server)
    monkeypatch.setattr(lane_driver, "opencode_client_from_env", fake_factory)
    return server, captured


class TestOpenCodeLane:
    def test_succeeded_turn_writes_the_lane_contract_and_exits_zero(
        self, opencode_lane_env, monkeypatch, capsys
    ):
        client = FakeOpenCodeLaneClient(
            events=[
                _opencode_event("session.usage.updated", {"input": 33, "output": 21}),
                _opencode_event("session.execution.succeeded"),
            ]
        )
        server, _captured = install_opencode(monkeypatch, client)

        assert main(["--driver", "opencode"]) == 0

        meta = read_meta(opencode_lane_env)
        assert meta["driver"] == "opencode-sdk-lane"
        assert meta["attempt_base"] == ATTEMPT_BASE
        assert meta["model"] == "glm-5.3-flash"
        assert meta["exit"] == "completed"
        assert meta["terminal_reason"] == "session.execution.succeeded"
        receipt = meta["usage"]
        assert receipt["input_tokens"] == 33
        assert receipt["output_tokens"] == 21
        # No cost API: the receipt is honest about what it does not know.
        assert receipt["completeness"] == "unknown"
        assert receipt["source"] == "session.usage.updated"
        assert "total_cost_usd" not in receipt
        assert read_usage(opencode_lane_env) == receipt
        assert "lane_driver: opencode-sdk-lane exit=completed" in capsys.readouterr().err
        assert server.stopped
        assert client.aclosed

    def test_the_brief_becomes_the_one_task(self, opencode_lane_env, monkeypatch):
        client = FakeOpenCodeLaneClient(events=[_opencode_event("session.execution.succeeded")])
        install_opencode(monkeypatch, client)

        main(["--driver", "opencode"])

        (task,) = client.tasks
        assert "PLAN: modernize the orders service" in task
        assert "issue #42" in task
        assert task.rstrip().endswith(NO_COMMIT_ADDENDUM)

    def test_the_env_dispatch_selects_the_lane_without_a_flag(self, opencode_lane_env, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_DRIVER", "opencode")
        install_opencode(
            monkeypatch,
            FakeOpenCodeLaneClient(events=[_opencode_event("session.execution.succeeded")]),
        )

        assert main() == 0

        assert read_meta(opencode_lane_env)["driver"] == "opencode-sdk-lane"

    def test_failed_turn_is_nonzero_with_the_vendor_verdict(self, opencode_lane_env, monkeypatch):
        install_opencode(
            monkeypatch, FakeOpenCodeLaneClient([_opencode_event("session.execution.failed")])
        )

        assert main(["--driver", "opencode"]) == 1

        meta = read_meta(opencode_lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "session.execution.failed"

    def test_usage_stays_unknown_without_the_usage_event(self, opencode_lane_env, monkeypatch):
        install_opencode(
            monkeypatch, FakeOpenCodeLaneClient([_opencode_event("session.execution.succeeded")])
        )

        assert main(["--driver", "opencode"]) == 0

        assert read_meta(opencode_lane_env)["usage"] is None
        assert read_usage(opencode_lane_env) is None

    def test_budget_timeout_classifies_budget_exceeded_and_tears_down(
        self, opencode_lane_env, monkeypatch
    ):
        # start_session yields the session id only at turn completion — a
        # timed-out turn cannot be aborted by id; the server teardown
        # bounds the orphaned turn (the honest outcome, never a guess).
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        client = FakeOpenCodeLaneClient(timeout=True)
        server, _captured = install_opencode(monkeypatch, client)

        assert main(["--driver", "opencode"]) == 1

        meta = read_meta(opencode_lane_env)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "budget_exceeded"
        assert server.stopped
        assert client.aclosed

    def test_the_client_env_pins_the_server_and_the_lane_posture(
        self, opencode_lane_env, monkeypatch
    ):
        server, captured = install_opencode(
            monkeypatch, FakeOpenCodeLaneClient([_opencode_event("session.execution.succeeded")])
        )

        main(["--driver", "opencode"])

        client_env = captured["env"]
        assert client_env["OPENCODE_SERVER_URL"] == server.url
        assert client_env["OPENCODE_SERVER_PASSWORD"] == server.password
        # LIVE-found: the factory default "reject" starves tools — the lane
        # defaults to "once" unless the operator said otherwise.
        assert client_env["OPENCODE_PERMISSION_RESPONSE"] == "once"
        assert client_env["OPENCODE_PROVIDER_ID"] == "zai"
        assert captured["provider_key"] == "sk-zai-test"
        # The client's own turn-wait budget IS the lane budget.
        assert client_env["OPENCODE_PROMPT_TIMEOUT"] == "1800.0"

    def test_an_explicit_permission_response_override_wins(self, opencode_lane_env, monkeypatch):
        monkeypatch.setenv("OPENCODE_PERMISSION_RESPONSE", "reject")
        _server, captured = install_opencode(
            monkeypatch, FakeOpenCodeLaneClient([_opencode_event("session.execution.succeeded")])
        )

        main(["--driver", "opencode"])

        assert captured["env"]["OPENCODE_PERMISSION_RESPONSE"] == "reject"

    def test_the_transcript_fallback_classifies_when_the_stream_was_uncovered(
        self, opencode_lane_env, monkeypatch
    ):
        install_opencode(
            monkeypatch,
            FakeOpenCodeLaneClient(
                [
                    {
                        "id": None,
                        "type": "transcript.reconciled",
                        "data": {
                            "sessionID": SESSION_ID,
                            "messages": [
                                {"id": "m1", "type": "assistant", "finish": "tool-calls"},
                                {"id": "m2", "type": "assistant", "finish": "stop"},
                            ],
                        },
                    }
                ]
            ),
        )

        assert main(["--driver", "opencode"]) == 0

        meta = read_meta(opencode_lane_env)
        assert meta["exit"] == "completed"
        assert meta["terminal_reason"] == "transcript.stop"


class TestOpenCodeClassification:
    def test_the_last_verdict_for_the_session_wins(self):
        events = [
            _opencode_event("session.execution.failed"),
            _opencode_event("session.execution.succeeded"),
            # another session's verdict is never taken
            {"type": "session.execution.failed", "data": {"sessionID": "ses_other"}},
        ]
        assert lane_driver.classify_opencode_events(events, SESSION_ID) == (
            "completed",
            "session.execution.succeeded",
        )

    def test_an_error_finish_in_the_transcript_classifies_failed(self):
        events = [
            {
                "type": "transcript.reconciled",
                "data": {
                    "sessionID": SESSION_ID,
                    "messages": [{"type": "assistant", "finish": "error"}],
                },
            }
        ]
        assert lane_driver.classify_opencode_events(events, SESSION_ID) == (
            "failed",
            "transcript.error",
        )

    def test_nothing_observable_stays_unobserved(self):
        assert lane_driver.classify_opencode_events([], SESSION_ID) == (
            "failed",
            "turn_end_unobserved",
        )

    def test_usage_receipt_reads_nested_token_shapes_and_stays_unknown(self):
        receipt = lane_driver.opencode_usage_receipt(
            [_opencode_event("session.usage.updated", {"tokens": {"inputTokens": 8, "output": 9}})]
        )
        assert receipt["input_tokens"] == 8
        assert receipt["output_tokens"] == 9
        assert receipt["completeness"] == "unknown"
        assert lane_driver.opencode_usage_receipt([]) is None
        assert (
            lane_driver.opencode_usage_receipt(
                [_opencode_event("session.usage.updated", {"context": "no tokens here"})]
            )
            is None
        )


# ---------------------------------------------------------------------------
# Registration + the lane template contract
# ---------------------------------------------------------------------------


class TestLaneRegistration:
    def test_the_lane_id_is_a_shipped_driver_with_the_claude_code_recipe(self):
        from forge.runs.harness_selection import (
            DRIVER_CREDENTIAL_VARS,
            DRIVER_OPTIONAL_CREDENTIAL_VARS,
            SHIPPED_DRIVERS,
        )

        assert LANE_DRIVER_ID in SHIPPED_DRIVERS
        assert DRIVER_CREDENTIAL_VARS[LANE_DRIVER_ID] == DRIVER_CREDENTIAL_VARS["claude-code"]
        assert (
            DRIVER_OPTIONAL_CREDENTIAL_VARS[LANE_DRIVER_ID]
            == DRIVER_OPTIONAL_CREDENTIAL_VARS["claude-code"]
        )

    def test_the_lane_driver_constant_matches_the_registered_id(self):
        assert lane_driver.LANE_DRIVER_ID == "claude-sdk-lane"

    def test_the_codex_and_opencode_lane_constants_are_their_registered_ids(self):
        # The meta ``driver`` ids for the two new SDK lanes, keyed by the
        # ``--driver`` / ``FORGE_LANE_DRIVER`` short key. The
        # SHIPPED_DRIVERS / DRIVER_CREDENTIAL_VARS registration rides with
        # the harness_selection + workflow arms (their exact-set pins in
        # test_harness_selection / test_templates stay green unchanged
        # until that registration lands beside them).
        assert lane_driver.CODEX_LANE_DRIVER_ID == "codex-sdk-lane"
        assert lane_driver.OPENCODE_LANE_DRIVER_ID == "opencode-sdk-lane"
        assert lane_driver.LANE_DRIVER_IDS == {
            "claude": "claude-sdk-lane",
            "codex": "codex-sdk-lane",
            "opencode": "opencode-sdk-lane",
        }

    def test_an_unknown_driver_fails_closed_but_still_writes_the_meta(self, lane_env, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_DRIVER", "warp")

        assert main() == 1

        meta = read_meta(lane_env)
        assert meta["driver"] == "warp"
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "unknown_driver"


class TestLaneTemplate:
    def test_rules_filter_on_the_run_id_and_the_lane_driver(self):
        doc = yaml.safe_load(TEMPLATE.read_text())
        lane_key = next(k for k in doc if k.startswith("forge-agent"))
        assert doc[lane_key]["rules"] == [
            {
                "if": (
                    '$FORGE_RUN_ID && ($FORGE_HARNESS_DRIVER == "" '
                    '|| $FORGE_HARNESS_DRIVER == "claude-sdk-lane")'
                )
            }
        ]

    def test_the_brief_is_written_into_the_repo_never_tmp(self):
        text = TEMPLATE.read_text()

        assert "printf '%s\\n' \"$FORGE_PLAN\" > .forge/brief.md" in text
        # The sandbox lesson: run context never lives in ephemeral scratch.
        assert not any("/tmp" in line and "brief" in line for line in text.splitlines())

    def test_the_lane_installs_forge_interactive_from_a_pinnable_ref(self):
        text = TEMPLATE.read_text()

        assert "forge[interactive]" in text
        assert "git+https://github.com/forcewake/forge@${FORGE_LANE_REF}" in text
        assert 'FORGE_LANE_REF: "main"' in text
        assert "python -m forge.lane_driver" in text

    def test_the_candidate_contract_is_byte_compatible_with_the_batch_lane(self):
        text = TEMPLATE.read_text()
        doc = yaml.safe_load(TEMPLATE.read_text())
        lane_key = next(k for k in doc if k.startswith("forge-agent"))
        doc = doc[lane_key]

        assert 'git checkout --detach "$FORGE_ATTEMPT_BASE"' in text
        assert (
            'git diff --cached --binary --full-index "$FORGE_ATTEMPT_BASE" '
            "> .forge/candidate.diff" in text
        )
        assert "git remote set-url --push origin FORBIDDEN" in text
        assert 'IS_SANDBOX: "1"' in text
        assert set(doc["artifacts"]["paths"]) == {
            ".forge/candidate.diff",
            ".forge/candidate.meta.json",
        }
        assert doc["artifacts"]["when"] == "always"

    def test_a_driver_failure_fails_the_job_but_uploads_artifacts(self):
        # USER-directed 2026-09-22: a nonzero driver rc FAILS the job
        # (batch pipefail parity); artifacts still upload via when:always.
        text = TEMPLATE.read_text()

        assert "python -m forge.lane_driver" in text
        assert '|| FORGE_DRIVER_EXIT="failed"' not in text
        assert 'exit "$_driver_rc"' in text


# ---------------------------------------------------------------------------
# The codex + opencode lane templates (the claude-sdk-lane skeleton with
# their own driver ids and CLI preambles)
# ---------------------------------------------------------------------------

CODEX_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "ci" / "templates" / "codex-sdk-lane.gitlab-ci.yml"
)
OPENCODE_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "ci" / "templates" / "opencode-sdk-lane.gitlab-ci.yml"
)

SDK_LANE_TEMPLATES = {
    CODEX_TEMPLATE: ("codex-sdk-lane", "codex"),
    OPENCODE_TEMPLATE: ("opencode-sdk-lane", "opencode"),
}


@pytest.mark.parametrize("template", list(SDK_LANE_TEMPLATES), ids=["codex", "opencode"])
class TestSdkLaneTemplates:
    def _text(self, template: Path) -> str:
        return template.read_text()

    def test_rules_filter_on_the_run_id_and_the_lane_driver(self, template):
        driver_id, _key = SDK_LANE_TEMPLATES[template]
        doc = yaml.safe_load(template.read_text())
        lane_key = next(k for k in doc if k.startswith("forge-agent"))
        assert doc[lane_key]["rules"] == [
            {
                "if": (
                    '$FORGE_RUN_ID && ($FORGE_HARNESS_DRIVER == "" '
                    f'|| $FORGE_HARNESS_DRIVER == "{driver_id}")'
                )
            }
        ]

    def test_the_template_registers_the_shipped_lane_id(self, template):
        # The id the rules filter on IS the lane_driver meta's driver id —
        # the worker dispatches what the lane reports.
        driver_id, _key = SDK_LANE_TEMPLATES[template]
        assert driver_id in lane_driver.LANE_DRIVER_IDS.values()

    def test_the_brief_is_written_into_the_repo_never_tmp(self, template):
        text = self._text(template)

        assert "printf '%s\\n' \"$FORGE_PLAN\" > .forge/brief.md" in text
        assert not any("/tmp" in line and "brief" in line for line in text.splitlines())

    def test_the_lane_installs_forge_interactive_and_runs_the_lane_driver(self, template):
        text = self._text(template)
        _driver_id, key = SDK_LANE_TEMPLATES[template]

        assert "forge[interactive]" in text
        assert "git+https://github.com/forcewake/forge@${FORGE_LANE_REF}" in text
        assert 'FORGE_LANE_REF: "main"' in text
        assert "uv python install 3.13" in text
        assert "python -m forge.lane_driver --driver " + key in text

    def test_the_cli_installs_over_npm_with_retries(self, template):
        text = self._text(template)

        if "opencode" in str(template):
            # The OFFICIAL installer, not npm (LIVE-found: the npm
            # opencode-ai build differs; the v2 prompt route 400s under it).
            assert "https://opencode.ai/install" in text
        else:
            assert "npm install -g --no-fund --no-audit" in text
        assert "for attempt in 1 2 3" in text
        assert "--version" in text

    def test_the_candidate_contract_is_byte_compatible_with_the_batch_lane(self, template):
        text = self._text(template)
        parsed = yaml.safe_load(template.read_text())
        doc = parsed[next(k for k in parsed if k.startswith("forge-agent"))]

        assert 'git checkout --detach "$FORGE_ATTEMPT_BASE"' in text
        assert (
            'git diff --cached --binary --full-index "$FORGE_ATTEMPT_BASE" '
            "> .forge/candidate.diff" in text
        )
        assert "git remote set-url --push origin FORBIDDEN" in text
        assert 'echo ".forge/" >> .git/info/exclude' in text
        assert set(doc["artifacts"]["paths"]) == {
            ".forge/candidate.diff",
            ".forge/candidate.meta.json",
        }
        assert doc["artifacts"]["when"] == "always"
        # The claude CLI's root-in-sandbox escape hatch is irrelevant here
        # — these lanes run no vendor TUI that checks it.
        assert "IS_SANDBOX" not in text

    def test_a_driver_failure_fails_the_job_but_uploads_artifacts(self, template):
        text = self._text(template)
        driver_id, _key = SDK_LANE_TEMPLATES[template]

        assert '|| FORGE_DRIVER_EXIT="failed"' not in text
        assert 'exit "$_driver_rc"' in text
        # The defensive meta floor names THIS lane's driver id.
        assert f'\\"driver\\": \\"{driver_id}\\"' in text

    def test_the_agent_is_told_not_to_commit_or_push(self, template):
        assert "Do NOT commit and do NOT push" in self._text(template)


class TestCodexLaneTemplateDetails:
    def test_the_cli_is_openai_codex_over_npm(self):
        text = CODEX_TEMPLATE.read_text()

        # NXT-27: the npm pin defaults to the LIVE-verified version, not
        # a floating latest (a vendor release must not silently change
        # lane behavior).
        assert f'"@openai/codex@${{FORGE_CODEX_VERSION:-{install_pin_of("codex-app")}}}"' in text
        assert f'FORGE_CODEX_VERSION: "{install_pin_of("codex-app")}"' in text
        assert "codex --version" in text

    def test_the_optional_provider_native_auth_lands_owner_only(self):
        text = CODEX_TEMPLATE.read_text()

        assert 'if [ -n "$FORGE_CODEX_AUTH" ]; then' in text
        assert 'printf "%s" "$FORGE_CODEX_AUTH" > ~/.codex/auth.json' in text
        assert "chmod 600 ~/.codex/auth.json" in text

    def test_the_model_route_and_cwd_exports(self):
        text = CODEX_TEMPLATE.read_text()

        assert "export FORGE_CODEX_MODEL=" in text
        assert 'export CODEX_CWD="$PWD"' in text


class TestOpenCodeLaneTemplateDetails:
    def test_the_cli_installs_via_the_official_installer(self):
        text = OPENCODE_TEMPLATE.read_text()

        # The official installer (LIVE-found 2026-09-21: the npm
        # opencode-ai build differs from the release channel — the v2
        # prompt route 400s under it).
        assert "https://opencode.ai/install" in text
        assert "opencode --version" in text

    def test_the_mechanical_deny_rides_the_serve_config(self):
        text = OPENCODE_TEMPLATE.read_text()

        assert '"git commit *": "deny"' in text
        assert '"git push *": "deny"' in text
        assert '"external_directory": "allow"' in text
        assert '"doom_loop": "allow"' in text

    def test_permission_prompts_are_answered_once(self):
        # LIVE-found: "reject" (the factory default) starves every tool
        # call in a task lane.
        text = OPENCODE_TEMPLATE.read_text()

        assert 'OPENCODE_PERMISSION_RESPONSE: "once"' in text

    def test_the_zai_provider_route_is_configured(self):
        text = OPENCODE_TEMPLATE.read_text()

        assert 'OPENCODE_PROVIDER_ID: "zai"' in text
        assert "{env:ZAI_API_KEY}" in text

    def test_the_lane_spawns_its_own_server_no_manual_serve(self):
        # The template only puts the CLI on PATH — lane_driver owns the
        # OpenCodeServer lifecycle; no manual port or serve management (the
        # header comment may NAME the spawned command, a script line never
        # runs it).
        text = OPENCODE_TEMPLATE.read_text()

        assert not any(line.lstrip().startswith("opencode serve") for line in text.splitlines())
        assert 'export OPENCODE_SERVE_CWD="$PWD"' in text
        assert "export FORGE_OPENCODE_MODEL=" in text


# ---------------------------------------------------------------------------
# The steering attach (NXT-11) — the driven turn + the concurrent mailbox
# drain, over the SAME client object, gated by FORGE_STEERING_ENABLED
# ---------------------------------------------------------------------------


class TestSteeringGate:
    def test_the_flag_defaults_off_and_parses_only_truthy_spellings(self):
        assert steering_enabled({}) is False
        assert steering_enabled({STEERING_ENV: "0"}) is False
        assert steering_enabled({STEERING_ENV: "off"}) is False
        assert steering_enabled({STEERING_ENV: "yes please"}) is False  # fails CLOSED
        for value in ("1", "true", "YES", " On "):
            assert steering_enabled({STEERING_ENV: value}) is True

    def test_main_without_the_flag_writes_no_steering_journal(self, lane_env):
        assert main(sdk=FakeSDK(CompletedTurnClient).module) == 0

        assert "steering_journal" not in read_meta(lane_env)

    def test_main_with_the_flag_carries_the_honest_empty_journal(self, lane_env, monkeypatch):
        # Attached, nothing routed: the lane-local mailbox is empty until
        # NXT-10's remote leg lands — the journal SAYS that (an empty list),
        # it never fabricates activity.
        monkeypatch.setenv(STEERING_ENV, "1")
        monkeypatch.setenv("FORGE_RUN_ID", "run-7")

        assert main(sdk=FakeSDK(CompletedTurnClient).module) == 0

        assert read_meta(lane_env)["steering_journal"] == []

    def test_the_flag_without_any_lane_identity_stays_detached(self, lane_env, monkeypatch):
        monkeypatch.setenv(STEERING_ENV, "1")
        # no FORGE_RUN_ID / FORGE_WORK_ID: nothing to scope a mailbox to —
        # the lane runs the old path rather than attaching under a guess

        assert main(sdk=FakeSDK(CompletedTurnClient).module) == 0

        assert "steering_journal" not in read_meta(lane_env)


class SteerableLaneClient:
    """Duck-types BOTH surfaces the claude lane needs at once: the lane's
    own client contract (start/query/interrupt/close) and the steering
    adapter's ClaudeSDKClient protocol (send/interrupt/query) — ONE object,
    the SAME-client rule the bridge documents. The turn's ResultMessage
    appears only once a steer has landed, so the concurrent drain is proven
    deterministically: the lane cannot finish before the mailbox won."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.steered = asyncio.Event()

    async def start_session(self, task: str) -> str:
        self.calls.append(("start_session", task))
        return "sess-1"

    async def send(self, session_id: str, text: str) -> None:
        self.calls.append(("steer", session_id, text))
        self.steered.set()  # the turn ends only after the guidance landed

    async def interrupt(self, session_id: str) -> None:
        self.calls.append(("interrupt", session_id))

    async def query(self, session_id: str) -> list[dict]:
        if self.steered.is_set():
            return [{"is_error": False, "num_turns": 2, "terminal_reason": "completed"}]
        return []

    async def close(self, session_id: str) -> None:
        self.calls.append(("close", session_id))


class TestClaudeLaneSteering:
    async def test_the_lane_binds_attaches_and_journals_a_mid_turn_steer(self, monkeypatch):
        monkeypatch.setenv("FORGE_RUN_ID", "run-1")
        monkeypatch.setenv("FORGE_WORK_ID", "wp-1")
        control = OperatorControlService()
        await control.steer("wp-1", "human:op", "tighten the retry bounds", run_id="run-1")
        client = SteerableLaneClient()

        outcome = await drive_lane(
            client, task="do the thing", budget_s=5.0, poll_s=0.01, control=control
        )

        # the turn could only complete THROUGH the drained steer — the
        # mailbox consumer ran concurrently with the driven turn
        assert outcome.exit_status == "completed"
        assert ("steer", "sess-1", "tighten the retry bounds") in client.calls
        assert client.calls[0] == ("start_session", "do the thing")
        assert client.calls[-1] == ("close", "sess-1")  # detached, then closed
        (entry,) = outcome.steering_journal or []
        assert entry["kind"] == "steer"
        assert entry["outcome"] == "applied"
        assert entry["delivery"] == "application_observed"
        assert entry["detail"]["mailbox_status"] == "checkpointed"

    async def test_without_control_the_lane_runs_the_old_path(self):
        client = SteerableLaneClient()

        outcome = await drive_lane(
            client, task="do the thing", budget_s=0.2, grace_s=0.05, poll_s=0.01
        )

        # no session, no drain task: nothing steered the turn, so it ends
        # bounded — and the outcome carries no journal at all
        assert outcome.terminal_reason == "budget_exceeded"
        assert outcome.steering_journal is None
        assert ("interrupt", "sess-1") in client.calls
        assert client.calls[-1] == ("close", "sess-1")


class SteerableCodexClient:
    """``start_thread`` answers at turn ACCEPTANCE; the ``turn/completed``
    frame is appended only when a steer lands on the ACTIVE turn — the
    lane's notification poll and the bridge's drain interleave without
    competing for the (non-consuming) buffer."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._events: list[dict] = []

    async def start_thread(self, task: str) -> str:
        self.calls.append(("start_thread", task))
        return THREAD_ID

    def events(self) -> list[dict]:
        return [dict(event) for event in self._events]

    async def steer_active_turn(self, thread_id: str, text: str) -> None:
        self.calls.append(("steer_active_turn", thread_id, text))
        self._events.append(_codex_turn("completed"))

    async def send_turn(self, thread_id: str, text: str) -> None:
        self.calls.append(("send_turn", thread_id, text))

    async def interrupt(self, thread_id: str) -> None:
        self.calls.append(("interrupt", thread_id))

    async def close(self) -> None:
        self.calls.append(("close",))


class TestCodexLaneSteering:
    async def test_the_thread_binds_at_acceptance_and_steers_the_active_turn(self, monkeypatch):
        monkeypatch.setenv("FORGE_RUN_ID", "run-1")
        monkeypatch.setenv("FORGE_WORK_ID", "wp-1")
        control = OperatorControlService()
        await control.steer("wp-1", "human:op", "use the fixture factory", run_id="run-1")
        client = SteerableCodexClient()

        outcome = await drive_codex_lane(
            client, task="do the thing", budget_s=5.0, poll_s=0.01, control=control
        )

        assert outcome.exit_status == "completed"
        assert ("steer_active_turn", THREAD_ID, "use the fixture factory") in client.calls
        assert client.calls[-1] == ("close",)
        (entry,) = outcome.steering_journal or []
        assert entry["outcome"] == "applied"
        assert entry["delivery"] == "application_observed"

    async def test_without_control_the_codex_lane_runs_the_old_path(self):
        client = SteerableCodexClient()  # never steered → never completes

        outcome = await drive_codex_lane(
            client, task="do the thing", budget_s=0.2, grace_s=0.05, poll_s=0.01
        )

        assert outcome.terminal_reason == "budget_exceeded"
        assert outcome.steering_journal is None
        assert ("interrupt", THREAD_ID) in client.calls


class TestOpenCodeLaneSteering:
    async def test_the_session_binds_at_completion_and_journals_the_refusal(
        self, opencode_lane_env, monkeypatch
    ):
        # The opencode client yields the session id only AT completion —
        # the attach binds there, and a queued mid-turn steer is honestly
        # refused (the profile has no live_input), never guessed at.
        control = OperatorControlService()
        await control.steer("wp-1", "human:op", "look at the parser next", run_id="run-1")
        client = FakeOpenCodeLaneClient([_opencode_event("session.execution.succeeded")])
        install_opencode(monkeypatch, client)

        outcome = await run_opencode_lane(
            task="do the thing",
            budget_s=5.0,
            poll_s=0.01,
            env={
                "FORGE_RUN_ID": "run-1",
                "FORGE_WORK_ID": "wp-1",
                "OPENCODE_PROVIDER_ID": "zai",
                "OPENCODE_MODEL_ID": "glm-5.3-flash",
            },
            control=control,
        )

        assert outcome.exit_status == "completed"
        (entry,) = outcome.steering_journal or []
        assert entry["kind"] == "steer"
        assert entry["outcome"] == "refused"
        assert "live_input" in entry["reason"]
        assert entry["delivery"] == ""  # no vendor call was ever attempted

    async def test_without_control_the_opencode_lane_runs_the_old_path(
        self, opencode_lane_env, monkeypatch
    ):
        control_free_client = FakeOpenCodeLaneClient([_opencode_event("session.execution.failed")])
        install_opencode(monkeypatch, control_free_client)

        outcome = await run_opencode_lane(
            task="do the thing",
            budget_s=5.0,
            poll_s=0.01,
            env={"OPENCODE_PROVIDER_ID": "zai"},
        )

        assert outcome.exit_status == "failed"
        assert outcome.steering_journal is None


# ---------------------------------------------------------------------------
# NXT-28 — the episode timing breakdown: every DRIVEN outcome carries it,
# whatever the exit classification; failures before the episode do not
# ---------------------------------------------------------------------------


class TestEpisodeTiming:
    def _assert_keys(self, episode: dict) -> None:
        # presence of every phase key (the meta is written sort_keys=True,
        # so only presence — not order — survives the JSON round-trip)
        assert set(episode) == set(EPISODE_PHASE_KEYS)

    async def test_the_outcomes_episode_keys_are_canonically_ordered(self):
        # In memory the breakdown carries the canonical phase order.
        client = SteerableLaneClient()
        control = OperatorControlService()
        await control.steer("wp", "human:op", "finish now", run_id="run")

        outcome = await drive_lane(
            client, task="do the thing", budget_s=5.0, poll_s=0.01, control=control
        )

        assert outcome.episode is not None
        assert tuple(outcome.episode) == EPISODE_PHASE_KEYS

    # -- claude -----------------------------------------------------------

    def test_completed_claude_turn_carries_all_four_phase_keys(self, lane_env):
        assert main(sdk=FakeSDK(CompletedTurnClient).module) == 0

        episode = read_meta(lane_env)["episode"]
        self._assert_keys(episode)
        assert episode["startup_s"] >= 0.0
        assert episode["turn_s"] >= 0.0
        assert episode["teardown_s"] >= 0.0
        # the turn completed without an interrupt: the phase is honestly
        # absent (None), never a fabricated 0.0
        assert episode["interrupt_grace_s"] is None

    def test_failed_claude_turn_still_carries_the_breakdown(self, lane_env):
        assert main(sdk=FakeSDK(AbortedTurnClient).module) == 1

        episode = read_meta(lane_env)["episode"]
        self._assert_keys(episode)
        assert episode["interrupt_grace_s"] is None

    def test_budget_exceeded_claude_turn_measures_the_interrupt_grace(self, lane_env, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        monkeypatch.setenv("FORGE_LANE_GRACE_SECONDS", "0.05")

        assert main(sdk=FakeSDK(SilentTurnClient).module) == 1

        episode = read_meta(lane_env)["episode"]
        self._assert_keys(episode)
        assert episode["interrupt_grace_s"] is not None
        assert episode["interrupt_grace_s"] >= 0.0
        assert episode["turn_s"] >= 0.0

    def test_a_failure_before_the_episode_carries_no_episode_key(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no .forge/brief.md → nothing was ever driven
        monkeypatch.setenv("FORGE_ATTEMPT_BASE", ATTEMPT_BASE)

        assert main(sdk=FakeSDK(CompletedTurnClient).module) == 1

        meta = json.loads((tmp_path / ".forge" / "candidate.meta.json").read_text())
        assert meta["terminal_reason"] == "brief_missing"
        assert "episode" not in meta

    # -- codex ------------------------------------------------------------

    def test_completed_codex_turn_carries_the_breakdown(self, codex_lane_env, monkeypatch):
        install_codex_client(monkeypatch, FakeCodexLaneClient(events=[_codex_turn("completed")]))

        assert main(["--driver", "codex"]) == 0

        episode = read_meta(codex_lane_env)["episode"]
        self._assert_keys(episode)
        assert episode["startup_s"] >= 0.0
        assert episode["turn_s"] >= 0.0
        assert episode["teardown_s"] >= 0.0
        assert episode["interrupt_grace_s"] is None

    def test_budget_exceeded_codex_turn_measures_the_interrupt_grace(
        self, codex_lane_env, monkeypatch
    ):
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        monkeypatch.setenv("FORGE_LANE_GRACE_SECONDS", "0.05")
        install_codex_client(monkeypatch, FakeCodexLaneClient())

        assert main(["--driver", "codex"]) == 1

        episode = read_meta(codex_lane_env)["episode"]
        self._assert_keys(episode)
        assert episode["interrupt_grace_s"] is not None

    # -- opencode ----------------------------------------------------------

    def test_completed_opencode_turn_carries_the_breakdown(self, opencode_lane_env, monkeypatch):
        install_opencode(
            monkeypatch,
            FakeOpenCodeLaneClient([_opencode_event("session.execution.succeeded")]),
        )

        assert main(["--driver", "opencode"]) == 0

        episode = read_meta(opencode_lane_env)["episode"]
        self._assert_keys(episode)
        assert episode["startup_s"] >= 0.0
        assert episode["turn_s"] >= 0.0
        assert episode["teardown_s"] >= 0.0
        # this lane has no interrupt-by-id phase at all — recorded as the
        # None it is (a timed-out turn never yields its session id)
        assert episode["interrupt_grace_s"] is None

    def test_budget_exceeded_opencode_turn_still_measures_start_and_turn(
        self, opencode_lane_env, monkeypatch
    ):
        monkeypatch.setenv("FORGE_LANE_BUDGET_SECONDS", "0.05")
        client = FakeOpenCodeLaneClient(timeout=True)
        install_opencode(monkeypatch, client)

        assert main(["--driver", "opencode"]) == 1

        episode = read_meta(opencode_lane_env)["episode"]
        self._assert_keys(episode)
        assert episode["turn_s"] >= 0.0
        assert episode["teardown_s"] >= 0.0
        assert episode["interrupt_grace_s"] is None


# ---------------------------------------------------------------------------
# NXT-27 + NXT-29 — the SDK-lane templates' version pins and the opt-in
# hardened-profile block (single-sourced from the live evidence)
# ---------------------------------------------------------------------------


class TestSdkLaneVersionPins:
    """The CLI installs default to the LIVE-verified versions — derived
    from the recorded evidence rows, so a new smoke recording a new
    version must move the pins with it (or they visibly disagree)."""

    def test_claude_pin_defaults_to_the_live_verified_version(self):
        text = TEMPLATE.read_text()
        pin = install_pin_of("claude-sdk")

        assert f'FORGE_CLAUDE_VERSION: "{pin}"' in text
        assert f"@anthropic-ai/claude-code@${{FORGE_CLAUDE_VERSION:-{pin}}}" in text
        assert "claude --version" in text  # the trace records what installed

    def test_codex_pin_defaults_to_the_live_verified_version(self):
        text = CODEX_TEMPLATE.read_text()
        pin = install_pin_of("codex-app")

        assert f'FORGE_CODEX_VERSION: "{pin}"' in text
        assert f'"@openai/codex@${{FORGE_CODEX_VERSION:-{pin}}}"' in text
        assert "codex --version" in text

    def test_opencode_pin_defaults_to_the_live_verified_version(self):
        text = OPENCODE_TEMPLATE.read_text()
        pin = install_pin_of("opencode-server")

        assert f'FORGE_OPENCODE_VERSION: "{pin}"' in text
        # the official installer accepts --version (v-prefix stripped)
        assert f'bash -s -- --version "${{FORGE_OPENCODE_VERSION:-{pin}}}"' in text
        assert "opencode --version" in text

    def test_no_sdk_lane_template_ships_a_floating_latest_install(self):
        for path in (TEMPLATE, CODEX_TEMPLATE, OPENCODE_TEMPLATE):
            text = path.read_text()
            assert ":-latest" not in text, path


class TestSdkLaneProfileBlock:
    """NXT-29: every SDK-lane template declares the (opt-in) hardened
    execution profile — default v1, with the egress hook beside it so a
    dispatch flipping to v2 is never a half-declaration."""

    @pytest.mark.parametrize(
        "template",
        [TEMPLATE, CODEX_TEMPLATE, OPENCODE_TEMPLATE],
        ids=["claude", "codex", "opencode"],
    )
    def test_the_profile_defaults_to_v1_with_the_egress_hook(self, template):
        doc = yaml.safe_load(template.read_text())
        lane_key = next(k for k in doc if k.startswith("forge-agent"))
        variables = doc[lane_key]["variables"]

        assert variables["FORGE_LANE_PROFILE"] == "v1"
        assert "FORGE_EGRESS_ALLOWLIST" in variables
