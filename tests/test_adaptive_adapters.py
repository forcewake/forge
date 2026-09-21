"""EXE-02/04/06/07/08: interactive drivers, control channel, tool isolation.

Adapters are tested over INJECTED FAKES — the whole point of the
duck-typed contract is that no vendor SDK is importable here. These
tests pin: the session/thread/event round trips per driver, the
capability sets (claude answers questions, codex does not), the
outbound channel's monotonic sequences and broker-ref auth (never a
token value), the matrix's fail-closed behavior for untested combos,
and the EXE-08 isolation — egress and tools per role, and the docker
socket belonging to the trusted test executor alone.
"""

from __future__ import annotations

import dataclasses

import pytest

from forge.adaptive.adapters import (
    ClaudeSDKAdapter,
    CodexAppAdapter,
    DriverMatrix,
    InteractiveDriver,
    OpenCodeAdapter,
    OutboundControlChannel,
    egress_policy,
    privileged_ok,
    tool_allowlist,
)


class FakeClaudeClient:
    """The ClaudeSDKClient duck type: records what the adapter asked of it."""

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.sent: list[tuple[str, str]] = []
        self.interrupted: list[str] = []
        self.messages: dict[str, list[dict]] = {}
        self._count = 0

    async def start_session(self, task: str) -> str:
        self._count += 1
        session_id = f"claude-session-{self._count}"
        self.sessions[session_id] = task
        self.messages[session_id] = [{"role": "assistant", "text": f"on it: {task}"}]
        return session_id

    async def send(self, session_id: str, text: str) -> None:
        self.sent.append((session_id, text))
        self.messages.setdefault(session_id, []).append({"role": "user", "text": text})

    async def interrupt(self, session_id: str) -> None:
        self.interrupted.append(session_id)

    async def query(self, session_id: str) -> list[dict]:
        return list(self.messages.get(session_id, []))


class FakeCodexClient:
    """The CodexAppClient duck type, with turns and steering kept APART."""

    def __init__(self):
        self.threads: dict[str, str] = {}
        self.turns: list[tuple[str, str]] = []
        self.steers: list[tuple[str, str]] = []
        self.interrupted: list[str] = []
        self._count = 0

    async def start_thread(self, task: str) -> str:
        self._count += 1
        thread_id = f"codex-thread-{self._count}"
        self.threads[thread_id] = task
        return thread_id

    async def send_turn(self, thread_id: str, text: str) -> None:
        self.turns.append((thread_id, text))

    async def steer_active_turn(self, thread_id: str, text: str) -> None:
        self.steers.append((thread_id, text))

    async def interrupt(self, thread_id: str) -> None:
        self.interrupted.append(thread_id)


class FakeOpenCodeClient:
    """The OpenCodeClient duck type over a server event stream."""

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.prompts: list[tuple[str, str]] = []
        self.aborted: list[str] = []
        self.stream: dict[str, list[dict]] = {}
        self._count = 0

    async def start_session(self, task: str) -> str:
        self._count += 1
        session_id = f"oc-session-{self._count}"
        self.sessions[session_id] = task
        self.stream[session_id] = [{"type": "session.started"}]
        return session_id

    async def prompt(self, session_id: str, text: str) -> None:
        self.prompts.append((session_id, text))
        self.stream.setdefault(session_id, []).append({"type": "message", "text": text})

    async def events(self, session_id: str) -> list[dict]:
        return list(self.stream.get(session_id, []))

    async def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)


class TestInteractiveDriver:
    def test_the_profile_is_frozen(self):
        driver = InteractiveDriver("x-lane", "claude-sdk", frozenset({"interrupt"}))
        with pytest.raises(dataclasses.FrozenInstanceError):
            driver.profile_id = "other-lane"

    def test_an_sdk_outside_the_closed_vocabulary_is_refused(self):
        with pytest.raises(ValueError, match="sdk must be one of"):
            InteractiveDriver("x-lane", "vscode-plugin", frozenset())

    def test_a_capability_outside_the_reviewed_vocabulary_is_refused(self):
        with pytest.raises(ValueError, match="outside the reviewed vocabulary"):
            InteractiveDriver("x-lane", "claude-sdk", frozenset({"shell_as_root"}))

    def test_supports_answers_from_the_declared_set(self):
        driver = InteractiveDriver("x-lane", "claude-sdk", frozenset({"interrupt"}))
        assert driver.supports("interrupt") is True
        assert driver.supports("questions") is False


class TestClaudeSDKAdapter:
    def test_the_profile_declares_the_continuous_interaction_surface(self):
        profile = ClaudeSDKAdapter(FakeClaudeClient()).profile
        assert profile.profile_id == "claude-sdk-lane"
        assert profile.sdk == "claude-sdk"
        assert profile.capabilities == frozenset(
            {"interrupt", "live_input", "checkpoint_export", "questions"}
        )

    async def test_start_steer_interrupt_drain_round_trip(self):
        fake = FakeClaudeClient()
        adapter = ClaudeSDKAdapter(fake)

        session_id = await adapter.start("modernize the orders service")
        assert fake.sessions[session_id] == "modernize the orders service"

        await adapter.steer(session_id, "prefer incremental steps")
        assert fake.sent == [(session_id, "prefer incremental steps")]

        await adapter.interrupt(session_id)
        assert fake.interrupted == [session_id]

        drained = await adapter.drain(session_id)
        assert {"role": "assistant", "text": "on it: modernize the orders service"} in drained
        assert {"role": "user", "text": "prefer incremental steps"} in drained

    async def test_two_sessions_stay_separate(self):
        fake = FakeClaudeClient()
        adapter = ClaudeSDKAdapter(fake)
        first = await adapter.start("task one")
        second = await adapter.start("task two")
        await adapter.steer(second, "guidance for two only")
        drained = await adapter.drain(first)
        assert all(msg["text"] != "guidance for two only" for msg in drained)


class TestCodexAppAdapter:
    def test_the_profile_is_interrupt_and_live_input_only(self):
        profile = CodexAppAdapter(FakeCodexClient()).profile
        assert profile.profile_id == "codex-app-lane"
        assert profile.sdk == "codex-app"
        assert profile.capabilities == frozenset({"interrupt", "live_input"})

    async def test_thread_and_turn_round_trip(self):
        fake = FakeCodexClient()
        adapter = CodexAppAdapter(fake)

        thread_id = await adapter.start_thread("add expiry to orders")
        assert fake.threads[thread_id] == "add expiry to orders"

        await adapter.send_turn(thread_id, "first turn")
        await adapter.steer_active_turn(thread_id, "watch the shared schema")
        await adapter.interrupt(thread_id)

        assert fake.turns == [(thread_id, "first turn")]
        assert fake.steers == [(thread_id, "watch the shared schema")]
        assert fake.interrupted == [thread_id]

    async def test_steering_never_travels_through_the_turn_channel(self):
        # The active-turn binding is the point (EXE-06): guidance must
        # reach the running turn, not queue as new conversation.
        fake = FakeCodexClient()
        adapter = CodexAppAdapter(fake)
        thread_id = await adapter.start_thread("task")

        await adapter.steer_active_turn(thread_id, "steer mid-turn")

        assert fake.steers == [(thread_id, "steer mid-turn")]
        assert fake.turns == []


class TestOpenCodeAdapter:
    def test_the_profile_claims_interrupt_only(self):
        profile = OpenCodeAdapter(FakeOpenCodeClient()).profile
        assert profile.profile_id == "opencode-server-lane"
        assert profile.sdk == "opencode-server"
        assert profile.capabilities == frozenset({"interrupt"})

    async def test_session_prompt_events_abort_round_trip(self):
        fake = FakeOpenCodeClient()
        adapter = OpenCodeAdapter(fake)

        session_id = await adapter.start_session("audit the BYOK profiles")
        assert fake.sessions[session_id] == "audit the BYOK profiles"

        await adapter.prompt(session_id, "start from the driver matrix")
        events = await adapter.events(session_id)
        assert {"type": "session.started"} in events
        assert any(e.get("text") == "start from the driver matrix" for e in events)

        await adapter.abort(session_id)
        assert fake.aborted == [session_id]


class TestCapabilitySetsDiffer:
    def test_claude_answers_questions_and_codex_does_not(self):
        claude = ClaudeSDKAdapter(FakeClaudeClient()).profile
        codex = CodexAppAdapter(FakeCodexClient()).profile
        assert claude.supports("questions") is True
        assert codex.supports("questions") is False

    def test_only_claude_claims_checkpoint_export(self):
        claude = ClaudeSDKAdapter(FakeClaudeClient()).profile
        codex = CodexAppAdapter(FakeCodexClient()).profile
        opencode = OpenCodeAdapter(FakeOpenCodeClient()).profile
        assert claude.supports("checkpoint_export") is True
        assert codex.supports("checkpoint_export") is False
        assert opencode.supports("checkpoint_export") is False

    def test_all_three_interrupt_but_only_opencode_lacks_live_input(self):
        claude = ClaudeSDKAdapter(FakeClaudeClient()).profile
        codex = CodexAppAdapter(FakeCodexClient()).profile
        opencode = OpenCodeAdapter(FakeOpenCodeClient()).profile
        for profile in (claude, codex, opencode):
            assert profile.supports("interrupt") is True
        assert claude.supports("live_input") is True
        assert codex.supports("live_input") is True
        assert opencode.supports("live_input") is False


class TestOutboundControlChannel:
    def test_enqueue_assigns_a_monotonic_sequence(self):
        channel = OutboundControlChannel("https://control.local/ingest", "broker/cred/0192")
        channel.enqueue({"kind": "log", "text": "one"})
        channel.enqueue({"kind": "log", "text": "two"})
        channel.enqueue({"kind": "log", "text": "three"})
        batch = channel.flush()
        assert [event["sequence"] for event in batch] == [1, 2, 3]

    def test_sequences_keep_increasing_across_flushes(self):
        channel = OutboundControlChannel("https://control.local/ingest", "broker/cred/0192")
        channel.enqueue({"kind": "log", "text": "first batch"})
        first = channel.flush()
        channel.enqueue({"kind": "log", "text": "second batch"})
        second = channel.flush()
        assert second[0]["sequence"] == first[-1]["sequence"] + 1

    def test_flush_stamps_the_broker_ref_auth_header(self):
        channel = OutboundControlChannel("https://control.local/ingest", "broker/cred/0192")
        channel.enqueue({"kind": "steer", "text": "hold"})
        batch = channel.flush()
        assert all(event["authorization"] == "ref:broker/cred/0192" for event in batch), (
            "the wire carries the broker-owned reference, never a token value"
        )

    def test_flush_drains_the_buffer(self):
        channel = OutboundControlChannel("https://control.local/ingest", "broker/cred/0192")
        channel.enqueue({"kind": "log", "text": "once"})
        assert len(channel.flush()) == 1
        assert channel.flush() == []

    def test_the_callers_event_dict_is_not_aliased(self):
        channel = OutboundControlChannel("https://control.local/ingest", "broker/cred/0192")
        event = {"kind": "log", "text": "before"}
        channel.enqueue(event)
        event["text"] = "mutated after enqueue"
        assert channel.flush()[0]["text"] == "before"

    def test_a_value_looking_token_ref_is_refused(self):
        with pytest.raises(ValueError, match="broker-owned id"):
            OutboundControlChannel("https://control.local/ingest", "SECRET=ghs_rawtoken")

    def test_no_token_value_shape_exists_anywhere_in_the_outbox(self):
        channel = OutboundControlChannel("https://control.local/ingest", "broker/cred/0192")
        channel.enqueue({"kind": "log", "text": "payload"})
        batch = channel.flush()
        assert all(event["authorization"].startswith("ref:") for event in batch)
        # No secret-shaped value can ride the channel: the constructor
        # refuses it and flush only ever stamps the reference.
        assert "=" not in repr(batch)
        assert "SECRET" not in repr(batch)


class TestDriverMatrix:
    def test_an_unregistered_combination_is_not_supported(self):
        assert DriverMatrix().supports("claude-sdk", "github", "byok") is False

    def test_a_registered_combination_is_supported(self):
        matrix = DriverMatrix()
        matrix.register("claude-sdk", "github", "byok")
        assert matrix.supports("claude-sdk", "github", "byok") is True

    def test_registration_does_not_grant_neighbor_combinations(self):
        matrix = DriverMatrix()
        matrix.register("claude-sdk", "github", "byok")
        assert matrix.supports("claude-sdk", "github", "app") is False
        assert matrix.supports("claude-sdk", "gitlab", "byok") is False
        assert matrix.supports("opencode-server", "github", "byok") is False

    def test_unsupported_combos_lists_the_refused_probes_only(self):
        matrix = DriverMatrix()
        matrix.register("opencode-server", "azure", "byok")
        matrix.supports("opencode-server", "azure", "byok")
        matrix.supports("claude-sdk", "github", "byok")
        matrix.supports("codex-app", "github", "app")
        assert matrix.unsupported_combos() == [
            ("claude-sdk", "github", "byok"),
            ("codex-app", "github", "app"),
        ]

    def test_an_sdk_outside_the_vocabulary_cannot_be_registered(self):
        with pytest.raises(ValueError, match="sdk must be one of"):
            DriverMatrix().register("cursor-cli", "github", "byok")


class TestEgressPolicy:
    def test_discovery_has_no_egress_at_all(self):
        assert egress_policy("discovery") == frozenset()

    def test_implementation_may_reach_only_the_package_registry(self):
        assert egress_policy("implementation") == frozenset({"package_registry"})

    def test_only_the_trusted_test_executor_gets_the_docker_socket(self):
        assert egress_policy("trusted_test") == frozenset({"package_registry", "docker_socket"})
        assert "docker_socket" not in egress_policy("implementation")
        assert "docker_socket" not in egress_policy("discovery")

    def test_an_unknown_mode_is_refused_not_guessed(self):
        with pytest.raises(ValueError, match="unknown execution mode"):
            egress_policy("open")


class TestToolAllowlist:
    def test_discovery_is_read_only_navigation(self):
        assert tool_allowlist("discovery") == frozenset(
            {"read_file", "list_paths", "grep", "find_symbol", "find_references"}
        )

    def test_implementation_adds_the_write_and_test_surface(self):
        implementation = tool_allowlist("implementation")
        assert tool_allowlist("discovery") < implementation
        assert {"write_file", "run_tests"} <= implementation

    def test_verification_reads_runs_and_composes_but_never_writes(self):
        assert tool_allowlist("verification") == frozenset(
            {"read_file", "run_tests", "compose_environment"}
        )
        assert "write_file" not in tool_allowlist("verification")

    def test_an_unknown_role_is_refused_not_guessed(self):
        with pytest.raises(ValueError, match="unknown role"):
            tool_allowlist("intern")


class TestPrivilegedOk:
    @pytest.mark.parametrize("role", ["discovery", "implementation", "verification"])
    def test_a_coding_agent_cannot_have_the_docker_socket(self, role):
        ok, reason = privileged_ok(role, "docker_socket")
        assert ok is False
        assert reason == "privileged execution belongs to the trusted test executor"

    def test_testcontainers_is_denied_to_the_coding_agent_too(self):
        ok, reason = privileged_ok("implementation", "testcontainers")
        assert ok is False
        assert reason == "privileged execution belongs to the trusted test executor"

    @pytest.mark.parametrize("wants", ["docker_socket", "testcontainers"])
    def test_the_trusted_test_executor_may_have_privileged_execution(self, wants):
        assert privileged_ok("trusted_test", wants) == (True, "ok")

    def test_unprivileged_wants_are_fine_for_a_coding_agent(self):
        assert privileged_ok("implementation", "package_registry") == (True, "ok")
        assert privileged_ok("discovery", "read_file") == (True, "ok")

    def test_an_unknown_role_fails_closed(self):
        ok, reason = privileged_ok("attacker", "docker_socket")
        assert ok is False
        assert "unknown role" in reason
