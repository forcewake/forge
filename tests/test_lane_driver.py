"""claude-sdk-lane tests (EXE-02): the runner-side entry over the sdk seam.

The lane entry (``python -m forge.lane_driver``) drives the REAL
:class:`~forge.adaptive.drivers.claude_sdk.ClaudeSDKDriverClient` — the
``claude-agent-sdk`` package is not installed here (CI installs no vendor
packages), so these contract tests fake the SDK MODULE exactly the way
``tests/test_adaptive_driver_claude_sdk.py`` does and run the lane through
the real client built by :func:`claude_sdk_client_from_env` (via the
``sdk=`` seam): the brief becomes the ONE task, the turn is poll-drained
to its ResultMessage, and the batch lane's ``.forge/`` artifact contract
comes out the other end — meta (driver id ``claude-sdk-lane``, exit
classification, usage receipt) + usage.json, with unknown usage staying
unknown, never zero.
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
from forge.lane_driver import (
    LANE_DRIVER_ID,
    NO_COMMIT_ADDENDUM,
    build_task,
    classify_result,
    main,
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


class TestLaneTemplate:
    def test_rules_filter_on_the_run_id_and_the_lane_driver(self):
        doc = yaml.safe_load(TEMPLATE.read_text())
        assert doc["forge-agent"]["rules"] == [
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
        doc = yaml.safe_load(TEMPLATE.read_text())["forge-agent"]

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

    def test_a_nonzero_driver_exit_never_aborts_the_audit_trail(self):
        # The batch lane's || swallow: the job stays green, artifacts still
        # upload, the worker classifies from the meta's exit.
        text = TEMPLATE.read_text()

        assert "python -m forge.lane_driver" in text
        assert '|| FORGE_DRIVER_EXIT="failed"' in text
