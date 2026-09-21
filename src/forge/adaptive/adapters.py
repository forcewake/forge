"""Interactive driver adapters and the execution-lane trust boundary.

EXE-02/EXE-04/EXE-06/EXE-07/EXE-08 (review 05868e9 backlog). Interactive
steering only counts when it runs OUTSIDE the privileged API process: an
adapter lives in the execution lane next to the runner it drives, and
the API process that owns credentials never hosts driver code. This
module makes that architecture executable as small, testable contracts:

- :class:`InteractiveDriver` — the frozen profile of one interactive
  driver: WHICH sdk it wraps and the capabilities it was wired for. The
  adapter runs in the EXECUTION LANE, never inside the privileged API
  process (EXE-02).
- :class:`ClaudeSDKAdapter` — the Claude SDK driver: session-based
  start/steer/interrupt/drain over an injected client.
- :class:`CodexAppAdapter` — the Codex App Server driver: thread/turn
  based, where steering is bound to the ACTIVE TURN (EXE-06).
- :class:`OpenCodeAdapter` — the OpenCode server driver for BYOK
  profiles; the :class:`DriverMatrix` gates which combos were tested
  (EXE-07).
- :class:`OutboundControlChannel` — the runner's outbound authenticated
  control channel: the RUNNER dials out, there is no inbound listener
  to attack, and the token travels as a broker-owned REFERENCE, never a
  value (EXE-04).
- :class:`DriverMatrix` — which (sdk, provider_route, credential_mode)
  combinations were actually TESTED; an unregistered combination is
  unsupported and fails during onboarding, never mid-run.
- :func:`egress_policy` / :func:`tool_allowlist` /
  :func:`privileged_ok` — the tool/egress isolation of EXE-08: the
  docker socket belongs to the TRUSTED TEST EXECUTOR only, never the
  coding agent.

Every adapter takes an injected, Protocol-shaped client. The CONTRACT
(naming, capabilities, lane) is testable and pinnable without any vendor
package — and the REAL clients that satisfy these Protocols now live in
:mod:`forge.adaptive.drivers` (claude_sdk / codex_app / opencode,
research-verified against the vendor surfaces), so a fake in tests and
the production client present the same duck type to this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from forge.adaptive.capability_profiles import CAPABILITIES

__all__ = [
    "ClaudeSDKAdapter",
    "ClaudeSDKClient",
    "CodexAppAdapter",
    "CodexAppClient",
    "DriverMatrix",
    "InteractiveDriver",
    "OpenCodeAdapter",
    "OpenCodeClient",
    "OutboundControlChannel",
    "SDKS",
    "egress_policy",
    "privileged_ok",
    "tool_allowlist",
]

#: The closed SDK vocabulary: the interactive surfaces adapters exist
#: for. A name outside this tuple is a modelling error, not a silent
#: pass-through — the matrix and the profiles both refuse it.
SDKS: tuple[str, ...] = ("claude-sdk", "codex-app", "opencode-server")

#: The privileged capabilities ONLY the trusted test executor may hold.
#: The docker socket (and testcontainers, which needs it) grant
#: root-adjacent power over the host; a coding agent holding them is
#: the containment failure EXE-08 exists to prevent.
_PRIVILEGED_WANTS: frozenset[str] = frozenset({"docker_socket", "testcontainers"})


@dataclass(frozen=True)
class InteractiveDriver:
    """The frozen profile of one interactive driver in the execution lane.

    ``sdk`` is the closed :data:`SDKS` vocabulary; ``capabilities`` is a
    subset of the reviewed :data:`~forge.adaptive.capability_profiles.CAPABILITIES`
    the driver was WIRED for (never inferred from a method name). The
    adapter this profile describes runs in the EXECUTION LANE, next to
    its runner — never inside the privileged API process that owns
    credentials. Steering text and vendor SDK callbacks stay outside
    the trust boundary by construction.
    """

    profile_id: str
    sdk: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if self.sdk not in SDKS:
            raise ValueError(f"sdk must be one of {SDKS}, got {self.sdk!r}")
        unknown = self.capabilities - frozenset(CAPABILITIES)
        if unknown:
            raise ValueError(f"capabilities outside the reviewed vocabulary: {sorted(unknown)}")

    def supports(self, capability: str) -> bool:
        """Whether THIS driver advertises *capability* (tested-for, not named-for)."""
        return capability in self.capabilities


class ClaudeSDKClient(Protocol):
    """The duck type :class:`ClaudeSDKAdapter` needs from the SDK shim."""

    async def start_session(self, task: str) -> str: ...
    async def send(self, session_id: str, text: str) -> None: ...
    async def interrupt(self, session_id: str) -> None: ...
    async def query(self, session_id: str) -> list[dict]: ...


class CodexAppClient(Protocol):
    """The duck type :class:`CodexAppAdapter` needs from the App Server shim."""

    async def start_thread(self, task: str) -> str: ...
    async def send_turn(self, thread_id: str, text: str) -> None: ...
    async def steer_active_turn(self, thread_id: str, text: str) -> None: ...
    async def interrupt(self, thread_id: str) -> None: ...


class OpenCodeClient(Protocol):
    """The duck type :class:`OpenCodeAdapter` needs from the server shim."""

    async def start_session(self, task: str) -> str: ...
    async def prompt(self, session_id: str, text: str) -> None: ...
    async def events(self, session_id: str) -> list[dict]: ...
    async def abort(self, session_id: str) -> None: ...


_CLAUDE_PROFILE = InteractiveDriver(
    profile_id="claude-sdk-lane",
    sdk="claude-sdk",
    capabilities=frozenset({"interrupt", "live_input", "checkpoint_export", "questions"}),
)
_CODEX_PROFILE = InteractiveDriver(
    profile_id="codex-app-lane",
    sdk="codex-app",
    capabilities=frozenset({"interrupt", "live_input"}),
)
_OPENCODE_PROFILE = InteractiveDriver(
    profile_id="opencode-server-lane",
    sdk="opencode-server",
    capabilities=frozenset({"interrupt"}),
)


@dataclass
class ClaudeSDKAdapter:
    """The Claude SDK interactive driver (EXE-02).

    Wraps an injected :class:`ClaudeSDKClient` — no SDK import, so the
    contract is testable with a fake and pinnable without a vendor
    package.

    Whether the current CLI+BYOK proxy actually surfaces all SDK
    callbacks (interrupt delivery, mid-turn send, checkpoint export) is
    a VERIFY-BLE matrix question — :class:`DriverMatrix` answers it per
    (sdk, provider_route, credential_mode) combination. The adapter
    declares what it NEEDS; the matrix declares what was TESTED; a gap
    between them fails during onboarding, never mid-run.
    """

    client: ClaudeSDKClient

    @property
    def profile(self) -> InteractiveDriver:
        """The lane profile: what this driver was wired for (see the class docstring)."""
        return _CLAUDE_PROFILE

    async def start(self, task: str) -> str:
        """Start a session for *task*; returns the vendor session id."""
        return await self.client.start_session(task)

    async def steer(self, session_id: str, text: str) -> None:
        """Send *text* into a RUNNING session — steering through the SDK's
        continuous interaction, not a queue that waits for the turn to end."""
        await self.client.send(session_id, text)

    async def interrupt(self, session_id: str) -> None:
        """Interrupt the running session."""
        await self.client.interrupt(session_id)

    async def drain(self, session_id: str) -> list[dict]:
        """Drain the messages accumulated for *session_id* so far."""
        return await self.client.query(session_id)


@dataclass
class CodexAppAdapter:
    """The Codex App Server interactive driver (EXE-06).

    Same injected-client pattern as :class:`ClaudeSDKAdapter` — a
    Protocol-shaped async object, no SDK import — but thread/turn based
    (:class:`CodexAppClient`).

    Steering is bound to the ACTIVE TURN and never replaces
    execution-permission changes: mid-turn guidance reaches the model
    through :meth:`steer_active_turn`, while what a turn is allowed to
    DO is decided elsewhere (the work contract and the permission
    ladder). That separation is the point — a steering channel that
    could also grant itself permissions would be a privilege escalation
    path.
    """

    client: CodexAppClient

    @property
    def profile(self) -> InteractiveDriver:
        """The lane profile: interrupt and live_input, nothing else was tested."""
        return _CODEX_PROFILE

    async def start_thread(self, task: str) -> str:
        """Start a thread for *task*; returns the vendor thread id."""
        return await self.client.start_thread(task)

    async def send_turn(self, thread_id: str, text: str) -> None:
        """Queue a NEW turn on *thread_id* — this is conversation, not steering."""
        await self.client.send_turn(thread_id, text)

    async def steer_active_turn(self, thread_id: str, text: str) -> None:
        """Steer the CURRENTLY RUNNING turn — guidance only; execution
        permissions are decided elsewhere and cannot be changed from here."""
        await self.client.steer_active_turn(thread_id, text)

    async def interrupt(self, thread_id: str) -> None:
        """Interrupt the thread's active turn."""
        await self.client.interrupt(thread_id)


@dataclass
class OpenCodeAdapter:
    """The OpenCode server interactive driver (EXE-07).

    Wraps an injected :class:`OpenCodeClient` — no SDK import.

    BYOK profiles only: this driver exists for the bring-your-own-key
    paths the customer plan exercises, and :class:`DriverMatrix` gates
    which (sdk, provider_route, credential_mode) combinations were
    actually tested. An untested combination fails during onboarding —
    the adapter never guesses that it "probably works".
    """

    client: OpenCodeClient

    @property
    def profile(self) -> InteractiveDriver:
        """The lane profile: interrupt only — no live_input was tested here."""
        return _OPENCODE_PROFILE

    async def start_session(self, task: str) -> str:
        """Start a session for *task*; returns the vendor session id."""
        return await self.client.start_session(task)

    async def prompt(self, session_id: str, text: str) -> None:
        """Send a prompt to *session_id*."""
        await self.client.prompt(session_id, text)

    async def events(self, session_id: str) -> list[dict]:
        """The server's event stream for *session_id* so far."""
        return await self.client.events(session_id)

    async def abort(self, session_id: str) -> None:
        """Abort the session — the one control surface this profile claims."""
        await self.client.abort(session_id)


@dataclass
class OutboundControlChannel:
    """The runner's outbound authenticated control channel (EXE-04).

    The RUNNER dials OUT to *endpoint*; there is no inbound listener to
    attack, which is why the channel is a push buffer and not a server.
    ``token_ref`` is a BROKER-OWNED ID, never a value: the broker
    resolves it at dial time, key material never enters the runner's
    memory space, and a ref that LOOKS like a pasted secret is refused
    here — the same X03 boundary
    :class:`~forge.adaptive.capability_profiles.CredentialBinding`
    enforces for profiles.

    Events get a strictly increasing ``sequence`` on enqueue, so the
    receiving side can detect gaps and replays; :meth:`flush` stamps
    each buffered event with the auth-header stub and drains the
    buffer. Flushing twice returns the second batch only — a redelivered
    event is visible as a duplicate sequence, not hidden.
    """

    endpoint: str
    token_ref: str
    _buffer: list[dict] = field(default_factory=list)
    _next_sequence: int = 1

    def __post_init__(self) -> None:
        if not self.endpoint:
            raise ValueError("endpoint must be the control plane the runner dials out to")
        if "=" in self.token_ref or "SECRET" in self.token_ref:
            raise ValueError(
                f"token_ref must be a broker-owned id, never a secret value "
                f"(got {self.token_ref!r})"
            )

    def enqueue(self, event: dict) -> None:
        """Buffer *event* (copied, never aliased) and assign its monotonic sequence."""
        staged = dict(event)
        staged["sequence"] = self._next_sequence
        self._next_sequence += 1
        self._buffer.append(staged)

    def flush(self) -> list[dict]:
        """Return the buffered events, each stamped with the auth-header stub.

        The ``authorization`` value is ``ref:<token_ref>`` — a reference
        the control plane resolves through the broker. No token value
        exists on this side of the wire to leak.
        """
        outbox = [{**event, "authorization": f"ref:{self.token_ref}"} for event in self._buffer]
        self._buffer = []
        return outbox


@dataclass
class DriverMatrix:
    """Which (sdk, provider_route, credential_mode) combinations were TESTED.

    A driver name is not a promise (the FND-04 doctrine, now for
    interactive surfaces): an SDK proxied over a BYOK CLI may or may not
    deliver every callback, and the only honest answer is
    per-combination evidence. ``register`` records a combination that
    WAS tested; :meth:`supports` answers False for anything else —
    unregistered combinations fail during onboarding, never mid-run,
    and the matrix never guesses.

    :meth:`unsupported_combos` lists the combinations onboarding ASKED
    about and the matrix refused: "unregistered" is infinite, so the
    diagnostic surface is the probe log, kept for the operator who has
    to decide what to test next.
    """

    _registered: set[tuple[str, str, str]] = field(default_factory=set)
    _probed: set[tuple[str, str, str]] = field(default_factory=set)

    def register(self, sdk: str, route: str, mode: str) -> None:
        """Record that (sdk, route, mode) was tested and is supported."""
        if sdk not in SDKS:
            raise ValueError(f"sdk must be one of {SDKS}, got {sdk!r}")
        if not route or not mode:
            raise ValueError("provider_route and credential_mode must both be non-empty")
        self._registered.add((sdk, route, mode))

    def supports(self, sdk: str, route: str, mode: str) -> bool:
        """Whether the combination was registered as tested; unregistered is False."""
        combo = (sdk, route, mode)
        self._probed.add(combo)
        return combo in self._registered

    def unsupported_combos(self) -> list[tuple[str, str, str]]:
        """Probed-and-refused combinations, sorted — the what-to-test-next list."""
        return sorted(self._probed - self._registered)


#: Egress per execution mode (EXE-08). Discovery needs NO network at
#: all; implementation may reach package registries (dependency
#: resolution is part of building); the docker socket belongs to the
#: TRUSTED TEST EXECUTOR only — a coding agent with the socket can
#: reach the host, so it never appears in an agent's policy.
_EGRESS: dict[str, frozenset[str]] = {
    "discovery": frozenset(),
    "implementation": frozenset({"package_registry"}),
    "trusted_test": frozenset({"package_registry", "docker_socket"}),
}

#: Tools per role (EXE-08). Discovery is read-only navigation;
#: implementation adds the write and test-run surface; verification can
#: read, run tests and compose an environment but NOT write files — a
#: verifier that could change what it verifies verifies nothing.
_TOOLS: dict[str, frozenset[str]] = {
    "discovery": frozenset({"read_file", "list_paths", "grep", "find_symbol", "find_references"}),
    "implementation": frozenset(
        {
            "read_file",
            "list_paths",
            "grep",
            "find_symbol",
            "find_references",
            "write_file",
            "run_tests",
        }
    ),
    "verification": frozenset({"read_file", "run_tests", "compose_environment"}),
}


def egress_policy(mode: str) -> frozenset[str]:
    """The egress destinations *mode* may reach; unknown modes are refused.

    Fail closed: an unrecognized mode raising here is caught at lane
    construction, whereas defaulting it to something permissive would
    hand an unreviewed mode live network access.
    """
    try:
        return _EGRESS[mode]
    except KeyError:
        raise ValueError(
            f"unknown execution mode {mode!r}; egress is never guessed — "
            f"known modes: {sorted(_EGRESS)}"
        ) from None


def tool_allowlist(role: str) -> frozenset[str]:
    """The closed tool allowlist for *role*; unknown roles are refused.

    The docker socket is NOT a tool any role gets here — privileged
    execution is asked about through :func:`privileged_ok`, and only the
    trusted test executor's answer can be yes.
    """
    try:
        return _TOOLS[role]
    except KeyError:
        raise ValueError(
            f"unknown role {role!r}; a tool allowlist is never guessed — "
            f"known roles: {sorted(_TOOLS)}"
        ) from None


def privileged_ok(role: str, wants: str) -> tuple[bool, str]:
    """Whether *role* may hold the privileged capability *wants*; ``(ok, reason)``.

    ``docker_socket`` and ``testcontainers`` are answered True ONLY for
    the trusted test executor; a coding agent asking for either gets a
    refusal that says where privileged execution belongs. Unknown roles
    are refused outright — fail closed, never defaulted.
    """
    if role not in ("discovery", "implementation", "verification", "trusted_test"):
        return False, f"unknown role {role!r}"
    if wants in _PRIVILEGED_WANTS and role != "trusted_test":
        return False, "privileged execution belongs to the trusted test executor"
    return True, "ok"
