"""The REAL Claude Agent SDK driver client, driven against a wheel-shaped fake.

EXE-02: the driver runs in the execution lane next to its runner — these
tests pin that its Protocol surface maps onto the VENDOR's actual shapes.
The ``claude-agent-sdk`` package is not installed here (CI has no vendor
packages), so the fakes mirror the ``claude-agent-sdk==0.2.157`` surface
verified in ``docs/research/claude-sdk-python.md``: the interactive
``ClaudeSDKClient`` method shapes (connect / query / receive_messages /
interrupt / disconnect), the ``ClaudeAgentOptions`` field names, and the
message dataclasses (``AssistantMessage.content`` DIRECT, ``ResultMessage``
carrying ``session_id`` / ``terminal_reason``). Fakes are injected through
the driver's ``sdk=`` import seam — no monkeypatching of internals the
tests do not own.

The ported hacks (``docs/research/forge-harness-hacks.md``) are pinned
here: ephemeral per-session ``CLAUDE_CONFIG_DIR`` (no cross-run memory
bleed), ``setting_sources`` isolation, the mechanical deny that beats
every permission mode, one-rule-per-literal allowlists (A09), gateway and
proxy env passthrough, and the interrupt path that must never hang even
when the SDK never acks (#1094).
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import forge.adaptive.drivers.claude_sdk as claude_sdk
from forge.adaptive.adapters import ClaudeSDKAdapter
from forge.adaptive.drivers.claude_sdk import (
    ClaudeSDKDriverClient,
    claude_sdk_client_from_env,
)


# ---------------------------------------------------------------------------
# Fakes mirroring the claude-agent-sdk==0.2.157 wheel surface (research doc §4/§5)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FakeTextBlock:
    text: str


@dataclasses.dataclass
class FakeAssistantMessage:
    # content is DIRECT (list of blocks) plus .model — no nested .message
    # wrapper; that is the wheel shape the drained dicts must preserve.
    content: list
    model: str
    parent_tool_use_id: str | None = None
    error: Any = None
    usage: dict | None = None
    message_id: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    uuid: str | None = None


@dataclasses.dataclass
class FakeUserMessage:
    content: str | list
    uuid: str | None = None
    parent_tool_use_id: str | None = None
    tool_use_result: dict | None = None
    origin: str | None = None


@dataclasses.dataclass
class FakeSystemMessage:
    subtype: str
    data: dict


@dataclasses.dataclass
class FakeResultMessage:
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
    """The ClaudeAgentOptions fields the driver sets (research doc §4),
    with the wheel's documented defaults."""

    allowed_tools: list = dataclasses.field(default_factory=list)
    disallowed_tools: list = dataclasses.field(default_factory=list)
    setting_sources: list | None = None
    permission_mode: str | None = None
    continue_conversation: bool = False
    resume: str | None = None
    session_id: str | None = None
    max_turns: int | None = None
    model: str | None = None
    fallback_model: str | None = None
    cwd: str | None = None
    env: dict = dataclasses.field(default_factory=dict)
    extra_args: dict = dataclasses.field(default_factory=dict)
    can_use_tool: Any = None
    hooks: dict | None = None
    user: str | None = None
    stderr: Any = None
    include_partial_messages: bool = False
    enable_file_checkpointing: bool = False
    mcp_servers: dict = dataclasses.field(default_factory=dict)
    strict_mcp_config: bool = False


class FakeCLIConnectionError(Exception):
    """Mirrors claude_agent_sdk.CLIConnectionError (research doc §5)."""


class FakeClaudeSDKClient:
    """Stand-in for the SDK's interactive ``ClaudeSDKClient`` (§2.2/§3).

    connect() emits the init ``SystemMessage`` carrying the session id;
    query() appends one full turn of messages ending in a ``ResultMessage``
    with the SAME vendor session id every turn (§6.2 — in-process session
    continuity); interrupt() acks, or hangs forever when
    ``hang_on_interrupt`` is set (the #1094 shape), optionally emitting
    the aborted turn's ``ResultMessage`` first (§7.2 — the aborted turn
    still produces one even when the ack never comes back).
    """

    def __init__(
        self,
        options: FakeClaudeAgentOptions | None = None,
        transport: Any = None,
    ) -> None:
        self.options = options if options is not None else FakeClaudeAgentOptions()
        self.transport = transport
        self.connected = False
        self.disconnected = False
        self._session_id = f"fake-session-{uuid.uuid4()}"
        self.queries: list[str] = []
        self.stream_labels: list[str] = []
        self.interrupt_calls = 0
        self.hang_on_interrupt = False
        self.emit_aborted_result = False
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()

    async def connect(self, prompt: str | None = None) -> None:
        self.connected = True
        await self._inbox.put(
            FakeSystemMessage(subtype="init", data={"session_id": self._session_id})
        )

    async def query(self, prompt: str, session_id: str = "default") -> None:
        if not self.connected:
            raise FakeCLIConnectionError("Not connected. Call connect() first.")
        self.queries.append(prompt)
        self.stream_labels.append(session_id)
        await self._inbox.put(FakeUserMessage(content=prompt))
        await self._inbox.put(
            FakeAssistantMessage(
                content=[FakeTextBlock(text=f"ack: {prompt}")],
                model="claude-sonnet-4-5-20250929",
                session_id=self._session_id,
            )
        )
        await self._inbox.put(self._result(subtype="success", result="done"))

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        if self.emit_aborted_result:
            await self._inbox.put(
                self._result(subtype="success", result=None, terminal_reason="aborted_tools")
            )
        if self.hang_on_interrupt:
            await asyncio.Event().wait()  # the CLI never acks (#1094)

    async def receive_messages(self):
        while True:
            yield await self._inbox.get()

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnected = True

    def _result(
        self, *, subtype: str, result: str | None, terminal_reason: str | None = None
    ) -> FakeResultMessage:
        return FakeResultMessage(
            subtype=subtype,
            duration_ms=5,
            duration_api_ms=4,
            is_error=False,
            num_turns=1,
            session_id=self._session_id,
            result=result,
            terminal_reason=terminal_reason,
        )


class FakeSDK:
    """A claude_agent_sdk module stand-in, injected via the sdk= seam."""

    def __init__(self) -> None:
        self.clients: list[FakeClaudeSDKClient] = []
        self.module = self._build_module()

    def _build_module(self) -> ModuleType:
        registry = self

        class RegisteredClient(FakeClaudeSDKClient):
            def __init__(
                self,
                options: FakeClaudeAgentOptions | None = None,
                transport: Any = None,
            ) -> None:
                super().__init__(options=options, transport=transport)
                registry.clients.append(self)

        module = ModuleType("claude_agent_sdk")
        module.ClaudeAgentOptions = FakeClaudeAgentOptions
        module.ClaudeSDKClient = RegisteredClient
        module.ResultMessage = FakeResultMessage
        return module

    @property
    def sole_client(self) -> FakeClaudeSDKClient:
        assert len(self.clients) == 1, f"expected exactly one client, got {len(self.clients)}"
        return self.clients[0]


async def drain_until(
    driver: ClaudeSDKDriverClient,
    session_id: str,
    predicate,
    timeout: float = 2.0,
) -> list[dict]:
    """Poll-drain until *predicate* holds over the accumulated messages.

    The receive pump consumes asynchronously and the Protocol's drain
    contract is "so far", so counts are asserted once the pump caught up.
    """
    seen: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        seen.extend(await driver.query(session_id))
        if predicate(seen):
            break
        await asyncio.sleep(0.01)
    return seen


def is_result(message: dict) -> bool:
    """A drained ResultMessage dict (is_error + num_turns is its fingerprint)."""
    return "is_error" in message and "num_turns" in message


@pytest.fixture
async def lane():
    """A driver over the fake SDK module, torn down after the test."""
    registry = FakeSDK()
    driver = ClaudeSDKDriverClient(sdk=registry.module)
    yield driver, registry
    await driver.aclose()


class TestStartSession:
    async def test_returns_the_vendor_session_id_and_passes_task_and_options(self, lane):
        driver, registry = lane
        session_id = await driver.start_session("modernize the orders service")

        client = registry.sole_client
        assert client.connected is True
        assert client.queries == ["modernize the orders service"]

        options = client.options
        assert options.permission_mode == "bypassPermissions"
        assert options.setting_sources == []  # the --setting-sources '' isolation
        assert options.max_turns == 200  # the runaway bound
        assert options.resume is None
        assert options.stderr is not None  # subprocess stderr wired to logs

        config_dir = options.env["CLAUDE_CONFIG_DIR"]
        assert Path(config_dir).is_dir()  # ephemeral, per session
        assert config_dir != str(Path.home() / ".claude")

        drained = await driver.query(session_id)
        init = next(m for m in drained if m.get("subtype") == "init")
        assert init["data"]["session_id"] == session_id

    async def test_each_session_gets_its_own_ephemeral_config_dir(self, lane):
        driver, registry = lane
        first = await driver.start_session("task one")
        second = await driver.start_session("task two")

        assert first != second
        dirs = [c.options.env["CLAUDE_CONFIG_DIR"] for c in registry.clients]
        # e2adf02: agent-written memory must never bleed into another
        # session — never share a config dir.
        assert dirs[0] != dirs[1]

        await driver.aclose()
        assert not Path(dirs[0]).exists()
        assert not Path(dirs[1]).exists()

    async def test_operator_env_cannot_relocate_the_config_dir(self):
        registry = FakeSDK()
        driver = ClaudeSDKDriverClient(
            sdk=registry.module,
            extra_env={"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude")},
        )
        session_id = await driver.start_session("task")
        config_dir = registry.sole_client.options.env["CLAUDE_CONFIG_DIR"]
        # The isolation mechanism is forced last in the env merge: the
        # client must not silently widen into a shared config dir.
        assert config_dir != str(Path.home() / ".claude")
        assert Path(config_dir).is_dir()  # real and ephemeral while the session lives
        await driver.close(session_id)
        assert not Path(config_dir).exists()  # and gone once it ends

    async def test_the_mechanical_deny_survives_operator_disallowed_tools(self):
        registry = FakeSDK()
        driver = ClaudeSDKDriverClient(sdk=registry.module, disallowed_tools=["Bash(rm:*)"])
        try:
            await driver.start_session("task")
        finally:
            await driver.aclose()
        disallowed = set(registry.sole_client.options.disallowed_tools)
        # Deny beats EVERY permission mode, including bypass — the one
        # CLI-level write boundary, unreachable from steering or config.
        assert {"Bash(git commit:*)", "Bash(git push:*)", "Bash(rm:*)"} <= disallowed


class TestSend:
    async def test_send_continues_the_same_live_session(self, lane):
        driver, registry = lane
        session_id = await driver.start_session("first task")
        await driver.send(session_id, "prefer incremental steps")

        client = registry.sole_client
        assert client.queries == ["first task", "prefer incremental steps"]
        # In-process continuation on the SAME client — not a new session.
        assert len(registry.clients) == 1

        seen = await drain_until(
            driver, session_id, lambda m: len([x for x in m if is_result(x)]) == 2
        )
        results = [m for m in seen if is_result(m)]
        assert len(results) == 2
        # §7.3 discipline: the follow-up was sent only after the previous
        # turn's ResultMessage — and every turn carries the SAME vendor id.
        assert all(r["session_id"] == session_id for r in results)

    async def test_unknown_session_fails_closed(self, lane):
        driver, _ = lane
        with pytest.raises(KeyError, match="unknown claude session"):
            await driver.send("nope", "text")
        with pytest.raises(KeyError, match="unknown claude session"):
            await driver.interrupt("nope")
        with pytest.raises(KeyError, match="unknown claude session"):
            await driver.query("nope")


class TestInterrupt:
    async def test_interrupt_is_delivered_and_acked(self, lane):
        driver, registry = lane
        session_id = await driver.start_session("count to one hundred slowly")
        await driver.interrupt(session_id)

        assert registry.sole_client.interrupt_calls == 1
        assert driver._live[session_id].interrupt_acked is True

    async def test_interrupt_returns_when_the_sdk_never_acks(self):
        # #1094 / PR #1095: an interrupt awaiting a control ack can hang.
        # The driver bounds it well under the SDK's 60s; the test bounds
        # it again so a regression cannot stall the suite.
        registry = FakeSDK()
        driver = ClaudeSDKDriverClient(sdk=registry.module, interrupt_timeout=0.05)
        try:
            session_id = await driver.start_session("count slowly")
            client = registry.sole_client
            client.hang_on_interrupt = True
            client.emit_aborted_result = True

            await asyncio.wait_for(driver.interrupt(session_id), timeout=2.0)

            assert client.interrupt_calls == 1
            assert driver._live[session_id].interrupt_acked is False
        finally:
            await driver.aclose()

    async def test_completion_keys_off_the_terminal_state_not_the_ack(self):
        registry = FakeSDK()
        driver = ClaudeSDKDriverClient(sdk=registry.module, interrupt_timeout=0.05)
        try:
            session_id = await driver.start_session("long turn")
            client = registry.sole_client
            client.hang_on_interrupt = True
            client.emit_aborted_result = True  # §7.2: the aborted turn still results

            await asyncio.wait_for(driver.interrupt(session_id), timeout=2.0)

            seen = await drain_until(
                driver,
                session_id,
                lambda m: any(x.get("terminal_reason") == "aborted_tools" for x in m),
            )
            assert any(m.get("terminal_reason") == "aborted_tools" for m in seen)
        finally:
            await driver.aclose()


class TestQueryDrains:
    async def test_drains_raw_sdk_message_dicts(self, lane):
        driver, _ = lane
        session_id = await driver.start_session("summarize the auth module")
        seen = await drain_until(driver, session_id, lambda m: any(is_result(x) for x in m))

        # Raw vendor shapes, no normalized schema: SystemMessage carries
        # subtype+data; AssistantMessage.content is DIRECT (list of block
        # dicts) plus model — exactly as the wheel defines them.
        init = next(m for m in seen if m.get("subtype") == "init")
        assert init["data"]["session_id"] == session_id

        assistant = next(m for m in seen if "model" in m and "content" in m)
        assert assistant["model"].startswith("claude-")
        assert assistant["content"][0]["text"].startswith("ack:")

        result = next(m for m in seen if is_result(m))
        assert result["session_id"] == session_id
        assert result["is_error"] is False

    async def test_draining_consumes(self, lane):
        driver, _ = lane
        session_id = await driver.start_session("one")
        await drain_until(driver, session_id, lambda m: any(is_result(x) for x in m))
        assert await driver.query(session_id) == []

        await driver.send(session_id, "two")
        second = await drain_until(driver, session_id, lambda m: any(is_result(x) for x in m))
        # Only the new turn's messages — the earlier drain consumed them.
        assert len([m for m in second if is_result(m)]) == 1


class TestGuardedImport:
    def test_the_module_imports_cleanly_without_the_vendor_package(self):
        # CI installs no vendor packages: this file imports the driver
        # module at the top WITHOUT claude-agent-sdk present — that is
        # the proof. The seam resolves either way at import time.
        module = importlib.import_module("forge.adaptive.drivers.claude_sdk")
        assert module is claude_sdk

    def test_constructing_without_an_sdk_raises_the_actionable_error(self, monkeypatch):
        monkeypatch.setattr(claude_sdk, "_claude_agent_sdk", None)
        with pytest.raises(RuntimeError, match="claude-agent-sdk"):
            ClaudeSDKDriverClient()


class TestFactory:
    async def test_reads_the_lane_environment(self, monkeypatch):
        monkeypatch.setenv("FORGE_CLAUDE_MODEL", "glm-5.3-flash[1m]")  # the [1m] suffix lesson
        monkeypatch.setenv("FORGE_CLAUDE_PERMISSION_MODE", "acceptEdits")
        monkeypatch.setenv("FORGE_CLAUDE_ALLOWED_TOOLS", "Read, Bash(git status:*),,Grep")
        monkeypatch.setenv("FORGE_CLAUDE_DISALLOWED_TOOLS", "Bash(rm:*)")
        monkeypatch.setenv("FORGE_CLAUDE_MAX_TURNS", "50")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.com")

        registry = FakeSDK()
        driver = claude_sdk_client_from_env(sdk=registry.module)
        try:
            await driver.start_session("task")
        finally:
            await driver.aclose()

        options = registry.sole_client.options
        assert options.model == "glm-5.3-flash[1m]"
        assert options.permission_mode == "acceptEdits"
        assert options.allowed_tools == ["Read", "Bash(git status:*)", "Grep"]
        assert "Bash(rm:*)" in options.disallowed_tools
        assert options.max_turns == 50
        assert options.env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
        # The mechanical deny rides along whatever the env said.
        assert {"Bash(git commit:*)", "Bash(git push:*)"} <= set(options.disallowed_tools)

    def test_malformed_numbers_fail_closed(self, monkeypatch):
        monkeypatch.setenv("FORGE_CLAUDE_MAX_TURNS", "many")
        with pytest.raises(ValueError, match="FORGE_CLAUDE_MAX_TURNS"):
            claude_sdk_client_from_env(sdk=FakeSDK().module)

    async def test_resume_with_a_pinned_config_root_is_passed_through(self):
        registry = FakeSDK()
        driver = ClaudeSDKDriverClient(
            sdk=registry.module, config_root="/tmp/forge-claude-root", resume="old-session-id"
        )
        try:
            await driver.start_session("continue where we left off")
        finally:
            await driver.aclose()
        options = registry.sole_client.options
        assert options.resume == "old-session-id"
        assert options.env["CLAUDE_CONFIG_DIR"] == "/tmp/forge-claude-root"

    async def test_resume_without_a_pinned_config_root_is_refused(self):
        # An ephemeral dir cannot hold the transcript being resumed —
        # continuation is an explicit operator choice, never a silent
        # cold start that PRETENDS to resume.
        with pytest.raises(ValueError, match="config_root"):
            ClaudeSDKDriverClient(sdk=FakeSDK().module, resume="old-session-id")


class TestEnvPassthrough:
    async def test_ambient_proxy_vars_and_r5_budgets_reach_the_subprocess(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://fast-proxy.corp:3128")
        registry = FakeSDK()
        driver = ClaudeSDKDriverClient(sdk=registry.module)
        try:
            await driver.start_session("task")
        finally:
            await driver.aclose()
        env = registry.sole_client.options.env
        assert env["HTTPS_PROXY"] == "http://fast-proxy.corp:3128"
        assert env["API_TIMEOUT_MS"] == "3000000"
        assert env["BASH_DEFAULT_TIMEOUT_MS"] == "300000"
        assert env["BASH_MAX_TIMEOUT_MS"] == "600000"

    async def test_the_gateway_base_url_overrides_and_credentials_survive(self):
        registry = FakeSDK()
        driver = ClaudeSDKDriverClient(
            sdk=registry.module,
            base_url="https://litellm.internal:4000",
            extra_env={"ANTHROPIC_AUTH_TOKEN": "sk-gateway-token"},
        )
        try:
            await driver.start_session("task")
        finally:
            await driver.aclose()
        env = registry.sole_client.options.env
        assert env["ANTHROPIC_BASE_URL"] == "https://litellm.internal:4000"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-gateway-token"


class TestAllowlistDiscipline:
    def test_default_rules_are_one_rule_per_literal(self):
        # A09: adjacent string literals once glued into a single
        # unmatchable rule. One rule per literal, and the glued
        # signatures appear NOWHERE.
        for rule in claude_sdk._DEFAULT_ALLOWED_TOOLS:
            assert rule and rule.strip() == rule
            assert rule.count("Bash(") <= 1
        joined = repr(list(claude_sdk._DEFAULT_ALLOWED_TOOLS))
        assert "*)Bash(" not in joined

    def test_commas_are_the_only_rule_boundary(self):
        assert claude_sdk._split_rules("Read,Bash(git status:*),,Grep") == [
            "Read",
            "Bash(git status:*)",
            "Grep",
        ]

    async def test_the_allowlist_travels_as_explicit_rules(self, lane):
        driver, registry = lane
        await driver.start_session("task")
        allowed = registry.sole_client.options.allowed_tools
        assert allowed == list(claude_sdk._DEFAULT_ALLOWED_TOOLS)
        assert all("," not in rule for rule in allowed)


class TestAdapterIntegration:
    async def test_the_driver_satisfies_the_frozen_adapter_contract(self, lane):
        # The point of the Protocol: the REAL client is the same duck
        # type the adapter was tested against with fakes (EXE-02 — the
        # adapter itself never imports the SDK).
        driver, _ = lane
        adapter = ClaudeSDKAdapter(client=driver)
        assert adapter.profile.supports("interrupt")

        session_id = await adapter.start("modernize the orders service")
        await adapter.steer(session_id, "prefer incremental steps")
        await adapter.interrupt(session_id)
        drained = await adapter.drain(session_id)
        assert drained, "raw vendor dicts flowed through the frozen contract"
