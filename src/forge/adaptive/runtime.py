"""The adaptive runtime substrate (review 05868e9, EXE epic core).

Three stories, one honesty rule: never pretend a batch harness is
interactive, and never pretend a conversation is a filesystem.

- EXE-01 — the role-aware :class:`HarnessRuntime` protocol. Adapters
  DECLARE capabilities (``"interrupt"``, ``"live_input"``,
  ``"checkpoint_export"``, ``"questions"``); consumers MUST degrade per
  capability and never assume uniform interactivity.
  :class:`CapabilityMatrix` is the registry that makes the degradation
  decision explicit before dispatch (``supports`` fails closed for
  unknown profiles; ``checkpoint_only`` refuses to advertise a
  checkpoint-only CLI as natively interactive).
  :class:`CheckpointRestartRuntime` is the honest batch reference
  implementation — control applies at checkpoint boundaries, never
  mid-turn.
- EXE-03 — portable workspace checkpoints. Filesystem persistence and
  conversational session persistence are DIFFERENT things:
  :func:`portable_checkpoint` bundles the workspace and metadata with an
  OPTIONAL ``native_session`` rider; :class:`SessionRestorer` decides
  whether a state restores natively (pinned interactive profile only)
  or must be RECONSTRUCTED from durable artifacts;
  :func:`rehydrate` materializes a checkpoint into a fresh scratch
  directory and refuses path traversal before writing anything.
- EXE-05 — :class:`BatchController` drives a
  :class:`CheckpointRestartRuntime` across interruptions in bounded
  episodes, keeping the SAME external honesty as a live adapter:
  what's supported, when control applies, and what was saved.

Pure stdlib and typing-only — every adapter imports this without cycle
or dependency risk (the same posture as ``forge.adaptive.read_guards``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "CHECKPOINT_RESTART_STATE",
    "KNOWN_CAPABILITIES",
    "NATIVE_SESSION_PROFILES",
    "PORTABLE_CHECKPOINT_SCHEMA",
    "BatchController",
    "CapabilityMatrix",
    "CheckpointRestartRuntime",
    "ControlOp",
    "HarnessRuntime",
    "RuntimeEvent",
    "SessionRestorer",
    "portable_checkpoint",
    "rehydrate",
]

#: The closed capability vocabulary. An adapter may only declare what
#: consumers already know how to degrade for; anything else fails at
#: registration instead of silently making ``supports()`` a lie.
KNOWN_CAPABILITIES = frozenset({"interrupt", "live_input", "checkpoint_export", "questions"})

#: Profiles whose NATIVE sessions may resume in place — the interactive
#: adapter set (EXE-02 claude-sdk, EXE-06 codex-app, EXE-07
#: opencode-server). Every other profile restores by RECONSTRUCTION from
#: durable artifacts, never by claiming a native resume.
NATIVE_SESSION_PROFILES = frozenset({"claude-sdk", "codex-app", "opencode-server"})

#: Schema tag of a portable checkpoint bundle (EXE-03).
PORTABLE_CHECKPOINT_SCHEMA = "forge.checkpoint.portable/1"

#: The ``state`` marker inside a CheckpointRestartRuntime checkpoint;
#: ``restore`` refuses any dict that does not carry it.
CHECKPOINT_RESTART_STATE = "checkpoint-restart"

#: The ONLY control ops a ControlOp may carry (validated in
#: ``__post_init__``) and the ONLY kinds a RuntimeEvent may carry.
_OPS = ("pause", "steer", "interrupt", "none")
_EVENT_KINDS = (
    "turn_started",
    "turn_finished",
    "tool_started",
    "tool_finished",
    "question_raised",
    "checkpoint_ready",
    "usage",
)


@dataclass(frozen=True)
class ControlOp:
    """One operator control intent as handed to a runtime adapter.

    ``"none"`` is the idle control (nothing queued). The other three are
    intents whose APPLICATION TIMING is capability-bound — an
    interactive runtime may apply ``pause``/``interrupt`` mid-turn; a
    batch runtime can only queue them for the next checkpoint boundary.
    """

    op: Literal["pause", "steer", "interrupt", "none"]
    payload: str = ""

    def __post_init__(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"unknown control op: {self.op!r}")
        if not isinstance(self.payload, str):
            raise ValueError("control payload must be a string")


@dataclass(frozen=True)
class RuntimeEvent:
    """One normalized runtime event, capability-neutral by shape.

    The kind vocabulary is closed; native session/turn correlation ids
    ride inside ``payload`` and stay OPAQUE — they are never forge
    run/attempt ids (EXE-01 keeps the two namespaces separate).
    """

    kind: Literal[
        "turn_started",
        "turn_finished",
        "tool_started",
        "tool_finished",
        "question_raised",
        "checkpoint_ready",
        "usage",
    ]
    payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _EVENT_KINDS:
            raise ValueError(f"unknown event kind: {self.kind!r}")


@runtime_checkable
class HarnessRuntime(Protocol):
    """The role-aware runtime contract every harness adapter satisfies.

    Adapters DECLARE capabilities — ``"interrupt"``, ``"live_input"``,
    ``"checkpoint_export"``, ``"questions"`` — and consumers MUST
    degrade per capability: a plan step that needs live steering checks
    ``"interrupt"`` BEFORE dispatch and negotiates an explicit
    checkpoint-restart downgrade; it never assumes uniform
    interactivity across profiles. Planning, implementation and review
    reuse this integration without inheriting the same permissions —
    the role's profile, not the adapter, decides the tool surface.
    """

    async def start(self, task: str, *, profile_id: str) -> None:
        """Begin a session for *task* under *profile_id*'s capability set."""

    async def send_control(self, op: ControlOp) -> None:
        """Hand one control intent to the adapter.

        WHEN it applies is capability-bound: interactive adapters apply
        control mid-turn; checkpoint-restart adapters queue it for the
        next boundary. The adapter's events say which happened.
        """

    async def events(self) -> list[RuntimeEvent]:
        """Drain the events accumulated since the last drain."""

    async def checkpoint(self) -> dict:
        """Export portable state a later ``restore`` accepts."""

    async def restore(self, state: dict) -> None:
        """Adopt a previously exported ``checkpoint`` state."""

    @property
    def capabilities(self) -> frozenset[str]:
        """The capability names this adapter truthfully supports."""
        ...


class CapabilityMatrix:
    """Registry mapping profile ids to their declared capability sets.

    The dispatch-time degradation oracle: "missing capabilities are
    detected before dispatch" (EXE-01) means asking this matrix, not
    hoping the adapter happens to support what the plan step needs.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, frozenset[str]] = {}

    def register(self, profile_id: str, capabilities: set[str]) -> None:
        """Declare one profile's capabilities.

        Registration validates against the closed vocabulary
        (:data:`KNOWN_CAPABILITIES`) — an unknown capability string
        would make every consumer's ``supports()`` check silently
        meaningless, so it fails HERE, at declaration time.
        """
        unknown = set(capabilities) - KNOWN_CAPABILITIES
        if unknown:
            raise ValueError(
                f"unknown capabilities {sorted(unknown)}; known: {sorted(KNOWN_CAPABILITIES)}"
            )
        self._profiles[profile_id] = frozenset(capabilities)

    def supports(self, profile_id: str, capability: str) -> bool:
        """Whether *profile_id* declared *capability* — fail closed.

        An unknown profile supports NOTHING: absence of a registration
        is never treated as "probably fine".
        """
        return capability in self._profiles.get(profile_id, frozenset())

    def interactive_profiles(self) -> list[str]:
        """Profiles that may receive live control.

        BOTH ``"interrupt"`` AND ``"live_input"`` are required — an
        interrupt without an input channel (or vice versa) cannot carry
        a live steer, so it is not interactive. Sorted for determinism.
        """
        return sorted(
            profile_id
            for profile_id, caps in self._profiles.items()
            if {"interrupt", "live_input"} <= caps
        )

    def checkpoint_only(self, profile_id: str) -> bool:
        """Whether *profile_id* is a checkpoint-restart-only profile.

        True when it has ``"checkpoint_export"`` but NOT ``"interrupt"``:
        a checkpoint-only CLI is never advertised as native interactive.
        This predicate is how a dispatcher picks the checkpoint-restart
        downgrade (EXE-05) instead of pretending the CLI takes keystrokes.
        """
        caps = self._profiles.get(profile_id, frozenset())
        return "checkpoint_export" in caps and "interrupt" not in caps


class CheckpointRestartRuntime:
    """The batch/CLI contract as a reference :class:`HarnessRuntime`.

    Control applies at CHECKPOINT BOUNDARIES, not mid-turn: a batch CLI
    has no live input channel, so ``send_control`` only QUEUES the
    intent, and the NEXT ``checkpoint()`` applies it — a queued
    ``pause`` lands there and is announced with a ``checkpoint_ready``
    event. Turns are bounded episodes (``run_turn``: one
    ``turn_started``/``turn_finished`` pair) with a checkpoint between
    them, exactly EXE-05's bounded episodes.

    The exported checkpoint is the four-key dict ``{"task",
    "pending_control", "profile_id", "state"}`` and ``restore`` accepts
    EXACTLY that shape — a foreign dict is a ValueError, not a
    best-effort guess.
    """

    def __init__(self) -> None:
        self._task: str = ""
        self._profile_id: str = ""
        self._pending: ControlOp = ControlOp(op="none")
        self._events: list[RuntimeEvent] = []
        self._turns: int = 0

    @property
    def capabilities(self) -> frozenset[str]:
        """A batch CLI supports checkpoint export — and honestly nothing else."""
        return frozenset({"checkpoint_export"})

    async def start(self, task: str, *, profile_id: str) -> None:
        """Record the task; a fresh session has nothing queued."""
        self._task = task
        self._profile_id = profile_id
        self._pending = ControlOp(op="none")
        self._events = []
        self._turns = 0

    async def send_control(self, op: ControlOp) -> None:
        """Queue *op* for the next checkpoint boundary — no immediate effect.

        The queue holds the LATEST intent (an operator retry replaces a
        stale one); nothing is applied until ``checkpoint()`` runs.
        """
        self._pending = op

    async def run_turn(self) -> None:
        """Run ONE bounded episode: ``turn_started`` then ``turn_finished``."""
        self._turns += 1
        self._events.append(RuntimeEvent(kind="turn_started", payload={"turn": self._turns}))
        self._events.append(RuntimeEvent(kind="turn_finished", payload={"turn": self._turns}))

    async def events(self) -> list[RuntimeEvent]:
        """Drain accumulated events (turn pairs, plus ``checkpoint_ready``
        after a checkpoint consumed a queued pause)."""
        drained, self._events = self._events, []
        return drained

    async def checkpoint(self) -> dict:
        """Apply queued control at THIS boundary, then export portable state.

        A queued ``pause`` is consumed here — the checkpoint IS the
        pause point — and announced with a ``checkpoint_ready`` event;
        the exported dict therefore carries ``pending_control`` as the
        serialized idle control (``{"op": "none", "payload": ""}``).
        """
        if self._pending.op == "pause":
            self._events.append(RuntimeEvent(kind="checkpoint_ready", payload={"applied": "pause"}))
            self._pending = ControlOp(op="none")
        return {
            "task": self._task,
            "pending_control": asdict(self._pending),
            "profile_id": self._profile_id,
            "state": CHECKPOINT_RESTART_STATE,
        }

    async def restore(self, state: dict) -> None:
        """Adopt a checkpoint exported by ``checkpoint()`` — exactly that shape.

        Refuses foreign dicts with ValueError rather than guessing at a
        half-compatible state: restoring onto an unrelated base is the
        exact corruption class EXE-03 exists to prevent.
        """
        if not isinstance(state, dict) or set(state) != {
            "task",
            "pending_control",
            "profile_id",
            "state",
        }:
            raise ValueError(
                "foreign checkpoint shape: expected exactly "
                "{task, pending_control, profile_id, state}"
            )
        if state["state"] != CHECKPOINT_RESTART_STATE:
            raise ValueError(f"foreign checkpoint state marker: {state['state']!r}")
        pending = state["pending_control"]
        if not isinstance(pending, dict) or set(pending) != {"op", "payload"}:
            raise ValueError(f"malformed pending_control (expected {{op, payload}}): {pending!r}")
        if not isinstance(state["task"], str) or not isinstance(state["profile_id"], str):
            raise ValueError("checkpoint task/profile_id must be strings")
        op = ControlOp(op=pending["op"], payload=pending["payload"])  # validates via __post_init__
        self._task = state["task"]
        self._profile_id = state["profile_id"]
        self._pending = op
        self._events = []
        self._turns = 0


class SessionRestorer:
    """Decides HOW a checkpoint may come back (EXE-03).

    Vendor conversation persistence alone does not restore the
    filesystem, and a native session artifact resumes in place ONLY on
    the pinned interactive profile it was recorded on. Every other case
    still restores — but honestly, as a RECONSTRUCTION from durable
    artifacts (workspace + metadata), never as a claimed native resume.
    """

    def restorable(self, state: dict, *, pinned_profile: str | None) -> tuple[bool, str]:
        """Whether *state* restores under *pinned_profile*, and how.

        Returns ``(True, "native")`` only for a native session artifact
        whose profile is BOTH the pinned one AND interactive
        (:data:`NATIVE_SESSION_PROFILES`); ``(True, "reconstructed")``
        whenever a native artifact exists but cannot resume in place
        (durable artifacts carry the work forward instead); ``(True,
        "durable")`` for a plain checkpoint with no pinned mismatch.
        ``(False, reason)`` for a shape without ``task``/``profile_id``
        or a pinned-profile mismatch on a plain checkpoint — restoring
        onto an unrelated base must fail loudly.
        """
        if not isinstance(state, dict) or "task" not in state or "profile_id" not in state:
            return (False, "state is not a forge checkpoint: task/profile_id missing")
        profile = state["profile_id"]
        if state.get("native_session") is not None:
            if (
                pinned_profile is not None
                and pinned_profile == profile
                and pinned_profile in NATIVE_SESSION_PROFILES
            ):
                return (True, "native")
            return (True, "reconstructed")
        if pinned_profile is None or pinned_profile == profile:
            return (True, "durable")
        return (
            False,
            f"checkpoint profile {profile!r} does not match pinned {pinned_profile!r}",
        )


def portable_checkpoint(
    workspace_files: dict[str, str], metadata: dict, *, native_session: dict | None = None
) -> dict:
    """Bundle a portable checkpoint (EXE-03).

    Filesystem persistence and conversational session persistence are
    DIFFERENT things: the workspace snapshot and metadata are the
    durable, always-present core, while ``native_session`` rides along
    ONLY when the caller has one and is ALWAYS optional — a checkpoint
    without it is complete, not degraded. The inputs are copied so
    later mutation of the caller's dicts cannot rewrite a checkpoint
    already taken.
    """
    return {
        "schema": PORTABLE_CHECKPOINT_SCHEMA,
        "workspace": dict(workspace_files),
        "metadata": dict(metadata),
        "native_session": native_session,
    }


def rehydrate(checkpoint: dict, scratch: Path) -> Path:
    """Materialize *checkpoint*'s workspace under *scratch* (created) and return it.

    Every path is validated BEFORE anything is written: a traversal
    attempt (any path containing ``..`` or absolute) fails the whole
    rehydration with ValueError instead of leaving a half-written
    workspace that looks restored. Validation uses POSIX rules because
    checkpoint paths are portable strings, not host paths.
    """
    workspace = checkpoint.get("workspace") or {}
    for path in workspace:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"checkpoint workspace path escapes scratch: {path!r}")
    scratch.mkdir(parents=True, exist_ok=True)
    for path, content in workspace.items():
        target = scratch / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return scratch


class BatchController:
    """Drives a :class:`CheckpointRestartRuntime` across interruptions (EXE-05).

    Batch harnesses keep the SAME external honesty as interactive ones:
    what's supported (``checkpoint_export`` only — never simulated
    terminal keystrokes), when control applies (at episode/checkpoint
    boundaries), and what was saved (the exported checkpoint).
    """

    async def run_until_pause(
        self, runtime: CheckpointRestartRuntime, max_turns: int = 5
    ) -> list[RuntimeEvent]:
        """Run bounded episodes until a queued pause lands at a boundary.

        Each iteration is one episode (``turn_started``/``turn_finished``)
        followed by a checkpoint — the ONLY place control may apply. The
        loop stops at the first ``checkpoint_ready`` (the pause was
        consumed; later turns do not run) or after *max_turns* episodes,
        so uncheckpointed work is bounded and explicit.
        """
        events: list[RuntimeEvent] = []
        for _ in range(max_turns):
            await runtime.run_turn()
            await runtime.checkpoint()
            drained = await runtime.events()
            events.extend(drained)
            if any(event.kind == "checkpoint_ready" for event in drained):
                break
        return events

    async def resume_with(self, runtime: CheckpointRestartRuntime, state: dict) -> None:
        """Restore *state* into *runtime*, then start again from it."""
        await runtime.restore(state)
        await runtime.start(state["task"], profile_id=state["profile_id"])
