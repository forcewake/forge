"""Durable human control over an adaptive run (CTL-04..CTL-08, review 05868e9).

The review's separation of powers: a run executes WHAT the approved
contract authorizes, and humans steer it through a durable mailbox of
:class:`ControlCommand` records — never through chat text that happens
to reach the model. This module makes each control invariant executable:

- CTL-04 — every command is a mailbox record with an idempotency key (a
  redelivery must not spend another iteration or grant another
  approval), a strictly increasing per-work sequence, and the state
  ladder ``received -> authorized -> applied -> checkpointed`` (the
  durable Postgres mailbox refines the middle leg into NXT-12's
  ``dispatching -> vendor_accepted | outcome_unknown`` rungs — see
  :mod:`forge.adaptive.mailbox_db`).
  Concurrency control is compare-and-set: a command whose expected plan
  revision / execution epoch no longer matches EXPIRES instead of
  applying against the wrong state.
- CTL-05 — pause is revoke-then-interrupt: ``pause_requested`` is
  persisted BEFORE any interrupt is sent (the ordering IS the
  guarantee), the publication epoch is bumped so grants of the old
  epoch cannot authorize new effects, a cooperative drain captures WIP,
  and a timeout exposes the last RECOVERABLE checkpoint — never a false
  clean pause.
- CTL-06 — resume runs only from a confirmed checkpoint with the
  snapshot available and permissions valid, under a FRESH execution
  epoch; a native session is restored only for a compatible PINNED
  profile, everything else reconstructs from durable artifacts (the
  same behavior on a host with no vendor session files).
- CTL-07 — steering is bounded: guidance within the current work is
  delivered at the next checkpoint boundary, contract constraints are
  promoted to a :class:`ChangeProposal` for the human gate, attempts to
  weaken acceptance are routed to rejection. Steering NEVER grants
  authority.
- CTL-08 — the final cancel contract: cancellation generation covers
  every interactive stage, tool/publication grants are revoked,
  immutable artifacts are retained, effects already accepted by a
  provider are CORRELATED as evidence (never claimed undone), and any
  intentional restart requires a NEW work command.

The state types are frozen value objects — transitions return new
instances and the caller persists them, so a crash between steps leaves
the last durable state, not a half-applied one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final, Literal, Protocol, runtime_checkable

from forge.adaptive.models import ChangeProposal, ControlCommand

__all__ = [
    "Mailbox",
    "MailboxSurface",
    "PauseState",
    "cancel_generation_applies",
    "classify_instruction",
    "deliver_steer",
    "drain_turn",
    "final_cancel",
    "late_effect_outcome",
    "new_execution_epoch",
    "new_publication_epoch",
    "promote_to_proposal",
    "recorded_pause",
    "request_pause",
    "resume_check",
    "send_interrupt",
]

#: The states a command passes through on the happy path. ``rejected`` and
#: ``expired`` are exits, never rungs — the ladder only climbs.
_LADDER: Final = ("received", "authorized", "applied", "checkpointed")


@runtime_checkable
class MailboxSurface(Protocol):
    """The mailbox contract the control plane codes against (CTL-04).

    :class:`Mailbox` (this module) is the in-memory reference
    implementation — tests and reasoning. The durable implementation is
    :class:`forge.adaptive.mailbox_db.PostgresMailbox`: the same surface
    as awaitables (production I/O is async), with the coarse in-memory
    ``apply`` refined into the NXT-12 delivery rungs
    (``dispatch -> vendor_accepted | outcome_unknown -> observe``) — the
    split that keeps "intended to send" distinguishable from "the agent
    applied it". Both satisfy this protocol's names, arguments and
    semantics, so a caller typed against ``MailboxSurface`` can be moved
    from either implementation to the other without touching its logic.
    """

    def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]: ...

    def authorize(
        self, command_id: str, actor_scopes: dict[str, tuple[str, ...]]
    ) -> ControlCommand: ...

    def apply(
        self,
        command_id: str,
        *,
        current_plan_revision: int,
        current_execution_epoch: int,
    ) -> ControlCommand: ...

    def checkpoint(self, command_id: str) -> ControlCommand: ...

    def pending(self, work_id: str) -> list[ControlCommand]: ...


class Mailbox:
    """The in-memory durable command mailbox (CTL-04).

    Commands enter via :meth:`submit`, dedup by idempotency key, climb
    the ladder ``received -> authorized -> applied -> checkpointed``,
    and leave the ladder through ``expired`` (a CAS miss) or
    ``rejected``. Skip-forward transitions are refused: a command that
    was never authorized cannot be applied, and one that was never
    applied cannot be checkpointed — a skipped rung would mean an effect
    the durable record cannot explain.
    """

    def __init__(self) -> None:
        self.commands: dict[str, ControlCommand] = {}
        #: idempotency_key -> command_id — the dedup index.
        self.by_key: dict[str, str] = {}
        #: work_id -> highest sequence accepted so far.
        self._last_sequence: dict[str, int] = {}

    def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]:
        """Enter a command into the mailbox; redelivery is a no-op.

        A REDELIVERED command (same idempotency key) returns the
        existing record with ``created=False`` — it does not spend
        another iteration, grant another approval, or reset a status the
        original already climbed to. A NEW command is stored at
        ``received`` (the ladder starts here whatever the caller
        believed) and its sequence must be strictly greater than every
        prior sequence of the same work — out-of-order delivery is a
        protocol violation, not a queue to reorder.
        """
        existing_id = self.by_key.get(command.idempotency_key)
        if existing_id is not None:
            return self.commands[existing_id], False
        if command.command_id in self.commands:
            raise ValueError(
                f"command_id {command.command_id!r} already exists under a "
                f"different idempotency key"
            )
        last = self._last_sequence.get(command.work_id, 0)
        if command.sequence <= last:
            raise ValueError(
                f"sequence {command.sequence} for work {command.work_id!r} is not "
                f"strictly increasing (last accepted: {last})"
            )
        stored = command.model_copy(update={"status": "received"})
        self.commands[command.command_id] = stored
        self.by_key[command.idempotency_key] = command.command_id
        self._last_sequence[command.work_id] = command.sequence
        return stored, True

    def authorize(
        self, command_id: str, actor_scopes: dict[str, tuple[str, ...]]
    ) -> ControlCommand:
        """Check the actor against its OWN origin's scopes; authorize.

        ``actor_scopes`` maps origin -> the actor refs that origin may
        speak for. An actor listed under a different origin is a
        wrong-origin actor — impersonation, not a typo — so it raises
        :class:`PermissionError` rather than a ladder error.
        """
        command = self._require(command_id)
        self._require_rung(command, "received")
        allowed = actor_scopes.get(command.actor_origin, ())
        if command.actor_ref not in allowed:
            raise PermissionError(
                f"actor {command.actor_ref!r} is not listed under its own origin "
                f"{command.actor_origin!r}"
            )
        return self._advance(command, "authorized")

    def apply(
        self,
        command_id: str,
        *,
        current_plan_revision: int,
        current_execution_epoch: int,
    ) -> ControlCommand:
        """Compare-and-set the command's expectations against the world.

        When ``expected_plan_revision`` / ``expected_execution_epoch``
        are set and differ from the current values, the command EXPIRES —
        it was written against a world that no longer exists, and
        applying it there would be a wrong-state apply. Absent
        expectations never expire (the command never pinned a world).
        Only an authorized command may apply.
        """
        command = self._require(command_id)
        self._require_rung(command, "authorized")
        stale = (
            command.expected_plan_revision is not None
            and command.expected_plan_revision != current_plan_revision
        ) or (
            command.expected_execution_epoch is not None
            and command.expected_execution_epoch != current_execution_epoch
        )
        return self._advance(command, "expired" if stale else "applied")

    def checkpoint(self, command_id: str) -> ControlCommand:
        """Mark an applied command as carried into the durable checkpoint."""
        command = self._require(command_id)
        self._require_rung(command, "applied")
        return self._advance(command, "checkpointed")

    def pending(self, work_id: str) -> list[ControlCommand]:
        """The work's received/authorized commands, in durable sequence order.

        Sequence order is the delivery order — CTL-07 requires two
        steering messages to be applied in the order they were
        sequenced, never in the order they happened to arrive.
        """
        return sorted(
            (
                command
                for command in self.commands.values()
                if command.work_id == work_id and command.status in ("received", "authorized")
            ),
            key=lambda command: command.sequence,
        )

    def _require(self, command_id: str) -> ControlCommand:
        command = self.commands.get(command_id)
        if command is None:
            raise KeyError(f"unknown command_id {command_id!r}")
        return command

    @staticmethod
    def _require_rung(command: ControlCommand, expected: str) -> None:
        if command.status != expected:
            raise ValueError(
                f"command {command.command_id!r} is {command.status!r}; the ladder "
                f"{_LADDER} refuses to skip from anything but {expected!r}"
            )

    def _advance(self, command: ControlCommand, status: str) -> ControlCommand:
        advanced = command.model_copy(update={"status": status})
        self.commands[command.command_id] = advanced
        return advanced


@dataclass(frozen=True)
class PauseState:
    """The durable pause book for one work (CTL-05).

    Value semantics: every transition returns a new instance and the
    caller persists it, so the on-disk record is always a state that
    actually held — never a half-applied one.

    ``publication_epoch`` is the fence CTL-05/CTL-08 share: a grant
    minted under an epoch lower than the current one is dead and can
    never authorize a new effect.
    """

    work_id: str
    publication_epoch: int = 0
    pause_requested: bool = False
    interrupt_sent: bool = False
    checkpoint_captured: bool = False
    last_applied_command_sequence: int = 0
    wip_artifact_id: str | None = None

    @property
    def last_recoverable(self) -> int:
        """The sequence the last RECOVERABLE checkpoint carries.

        On a cooperative drain this is the fresh capture; on a timeout it
        is the PREVIOUS durable checkpoint — either way the caller learns
        what can actually be resumed, not a false clean pause.
        """
        return self.last_applied_command_sequence


def request_pause(state: PauseState) -> PauseState:
    """Set ``pause_requested`` — and nothing else.

    Persist ``pause_requested`` BEFORE sending any interrupt: the
    ordering IS the guarantee. A process that dies between the two
    leaves a pause on record (an unacknowledged-but-paused work), never
    an interrupted runner with no pause behind it.
    """
    return replace(state, pause_requested=True)


def send_interrupt(state: PauseState) -> PauseState:
    """Send the runtime interrupt; refused unless a pause is on record.

    An interrupt without a persisted pause request would be
    revoke-nothing-interrupt-something — the exact inversion CTL-05
    exists to prevent.
    """
    if not state.pause_requested:
        raise ValueError(
            "interrupt refused: pause_requested is not on record — persist the "
            "pause before interrupting"
        )
    return replace(state, interrupt_sent=True)


def drain_turn(
    state: PauseState, *, cooperative: bool, wip_artifact_id: str | None = None
) -> PauseState:
    """Drain the active turn after the interrupt.

    Cooperative: the runner stopped at a boundary, so the WIP is
    captured — ``checkpoint_captured`` set and the artifact recorded.
    Timeout (``cooperative=False``): the runner was terminated and
    ``checkpoint_captured`` is LEFT AS-IS — we expose the last
    RECOVERABLE checkpoint (``state.last_recoverable``), not a false
    clean pause; whether anything from this turn survives is a later
    reconciliation, never an assumption.
    """
    if cooperative:
        artifact = (
            wip_artifact_id if wip_artifact_id is not None else (f"artifact:wip:{state.work_id}")
        )
        return replace(state, checkpoint_captured=True, wip_artifact_id=artifact)
    return state


def new_publication_epoch(state: PauseState) -> PauseState:
    """Bump the publication epoch, closing the old epoch's authorizations.

    Every publication grant carries the epoch it was minted under; after
    the bump, grants of the old epoch cannot authorize a new effect even
    if a slow process presents them — the pause means publication stops,
    not merely that a UI changes state.
    """
    return replace(state, publication_epoch=state.publication_epoch + 1)


def recorded_pause(
    state: PauseState,
    command: ControlCommand,
    submit: Callable[[ControlCommand], tuple[ControlCommand, bool]],
) -> tuple[PauseState, ControlCommand, bool]:
    """NXT-09's dedup-first pause: the command row is durable FIRST.

    ``submit`` runs BEFORE any pause-state mutation. A redelivered pause
    (same work-scoped idempotency key) is refused by the mailbox with
    ``created=False`` and the state comes back UNCHANGED — the
    publication epoch is not bumped a second time and nothing is
    re-recorded (the old order — fence the epoch, then discover the
    duplicate at submit — bumped the fence on a redelivery, spending a
    generation the command never earned). Only a NEW command row
    (``created=True``) sets ``pause_requested`` and bumps the fence; the
    caller persists the returned state and then sends the interrupt
    (:func:`send_interrupt`) — CTL-05's order preserved.

    Works over any :class:`MailboxSurface` submit — the in-memory
    reference mailbox synchronously, the durable Postgres mailbox
    through the same shape (its ``submit`` is awaitable, so the async
    caller awaits it and applies the same ``created`` gate before
    fencing).
    """
    stored, created = submit(command)
    if not created:
        return state, stored, False
    return new_publication_epoch(request_pause(state)), stored, True


def resume_check(
    checkpoint_state: PauseState,
    snapshot_available: bool,
    active_plan_revision: int,
    permissions_valid: bool,
) -> tuple[bool, str]:
    """Gate resume on a confirmed checkpoint, snapshot, and permissions.

    Resumable only when the pause captured a checkpoint AND the snapshot
    set is still available AND the current permissions are valid. The
    plan revision is INFORMATIONAL — recorded in the reason, never
    compared: historical spend and pending remote effects stay attached
    to the same work whatever revision is now active.
    """
    if not checkpoint_state.checkpoint_captured:
        return False, "no confirmed checkpoint: the pause ended without capturing WIP"
    if not snapshot_available:
        return False, "snapshot set unavailable: resume must not run from an unbound source"
    if not permissions_valid:
        return False, "permissions invalid: resume must not inherit stale authorizations"
    return True, (f"resumable: active plan revision {active_plan_revision} recorded, not compared")


#: Profiles whose native (vendor) session files are safe to restore across
#: an epoch bump; anything else reconstructs from durable artifacts.
_PINNED_PROFILES: Final = frozenset({"claude-sdk", "codex-app", "opencode-server"})


def new_execution_epoch(
    attempt_id: str, prior_epoch: int, pinned_profile: str | None = None
) -> dict:
    """Open a fresh execution epoch for a resume (CTL-06).

    The new epoch starts at ``prior_epoch + 1`` so late artifacts from
    the interrupted attempt cannot masquerade as current ones. The
    native session is restored ONLY when a compatible PINNED profile is
    named — an unpinned or unknown profile (or a provider/model change)
    reconstructs from durable task artifacts, which is exactly how a
    resume on another host with no vendor session files must behave.
    """
    native = pinned_profile is not None and pinned_profile in _PINNED_PROFILES
    return {
        "attempt_id": attempt_id,
        "execution_epoch": prior_epoch + 1,
        "native_session_restored": native,
        "reconstruction": "native_session" if native else "durable_artifacts",
    }


#: Acceptance-weakening stems. Checked FIRST: an attempt to weaken the
#: tests is never "just steering" and never "just an amendment".
_ACCEPTANCE_WEAKENING: Final = (
    "skip the test",
    "skip tests",
    "turn off check",
    "turn off test",
    "make tests optional",
    "make the tests optional",
    "disable the test",
    "disable tests",
    "disable check",
    "ignore failing test",
    "ignore test fail",
    "relax the acceptance",
    "weaken the acceptance",
    "drop the acceptance",
)

#: Constraint-introducing phrases — text that dictates HOW the system must
#: be built, as opposed to guidance inside the current work.
_CONSTRAINT_PHRASES: Final = (
    "must not",
    "must use",
    "do not introduce",
    "do not use",
    "use existing",
    "instead of",
)

#: System nouns — the vocabulary of contract-level choices. An amendment
#: is a constraint phrase ABOUT one of these; "use the existing helper"
#: is guidance, "do not introduce a new broker" is a contract change.
_SYSTEM_NOUNS: Final = (
    "broker",
    "database",
    "datastore",
    "queue",
    "message bus",
    "cache",
    "rabbitmq",
    "kafka",
    "postgres",
    "postgresql",
    "mysql",
    "redis",
    "elasticsearch",
    "event store",
    "search engine",
    "orm",
    "service",
)

InstructionClass = Literal["steer", "amend", "acceptance_change"]


def classify_instruction(text: str) -> InstructionClass:
    """Classify operator text: ``steer`` | ``amend`` | ``acceptance_change``.

    ``steer`` — guidance within the current work ("fix the failing
    assertion first", "use the existing helper"): it may change TACTICS,
    never authority.

    ``amend`` — the text introduces a CONSTRAINT that belongs in the
    contract (a constraint phrase about a system noun: "do not introduce
    a new broker", "must use RabbitMQ"); it is promoted to a
    ChangeProposal for the human gate.

    ``acceptance_change`` — the text tries to weaken the tests ("skip
    the tests", "turn off checks", "make tests optional"). This is NOT
    steering: it is an attempt to change acceptance policy, and the
    label lets callers route it to rejection instead of delivering it.
    Bounded by construction: no class here grants authority.
    """
    lowered = text.lower()
    if any(stem in lowered for stem in _ACCEPTANCE_WEAKENING):
        return "acceptance_change"
    if any(phrase in lowered for phrase in _CONSTRAINT_PHRASES) and any(
        noun in lowered for noun in _SYSTEM_NOUNS
    ):
        return "amend"
    return "steer"


def deliver_steer(command: ControlCommand, *, current_turn: int) -> dict:
    """Record delivery of a bounded steering instruction (CTL-07).

    Accepted guidance is delivered at the next checkpoint boundary or
    via the runtime adapter — this record tells the operator at WHICH
    turn the boundary sat. Delivery changes no read/write/tool scope: a
    steering note NEVER grants authority. Text classified as
    ``acceptance_change`` is rejected outright — acceptance policy
    changes require the revision gate, not a steering channel.
    """
    text = str(command.payload.get("text", ""))
    if classify_instruction(text) == "acceptance_change":
        return {
            "command_id": command.command_id,
            "status": "rejected",
            "reason": "acceptance policy change requires the revision gate",
        }
    return {
        "command_id": command.command_id,
        "delivered_at_turn": current_turn,
        "status": "accepted",
    }


def promote_to_proposal(text: str) -> ChangeProposal | None:
    """Promote amend-class text into a material-contract ChangeProposal.

    A constraint that belongs in the contract must travel the SAME road
    as any other material change — the human gate — instead of being
    laundered through the steering channel. Non-amend text promotes to
    nothing (``None``): steering stays steering.
    """
    if classify_instruction(text) != "amend":
        return None
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return ChangeProposal(
        proposal_id=f"auto-{digest}",
        work_id="unknown",
        from_revision=1,
        classification="material_contract",
        rationale=text,
    )


#: Every interactive stage the cancellation generation must cover (CTL-08).
#: Adding pause/resume/question states must not open a stage the cancel
#: fence forgets — the list is the contract this function documents.
_CANCELLATION_STAGES: Final = (
    "discovery",
    "implementation",
    "questions",
    "revisions",
    "verification",
    "child_work_items",
)


def cancel_generation_applies(stage: str) -> list[str]:
    """Confirm ``stage`` is covered; return ALL covered stages.

    Any VALID stage name returns the full list — the generation is one
    fence shared by every interactive stage, not a per-stage opt-in. An
    UNKNOWN name raises :class:`ValueError`: a typo must fail loudly
    rather than silently skipping a stage the cancel contract covers.
    """
    if stage not in _CANCELLATION_STAGES:
        raise ValueError(
            f"unknown interactive stage {stage!r}; cancellation generation applies "
            f"to {list(_CANCELLATION_STAGES)}"
        )
    return list(_CANCELLATION_STAGES)


def final_cancel(state: PauseState, accepted_effects: list[str]) -> dict:
    """The final cancel contract (CTL-08).

    Cancelling revokes FUTURE tool and publication grants, retains the
    immutable artifacts as evidence, and CORRELATES every effect the
    provider already accepted before the boundary — correlated as
    evidence, never claimed undone (a dispatched remote effect is
    reconciled; "we took it back" would be a lie the record cannot
    support). The cancellation generation is the pause state's
    publication epoch — grants minted under any lower epoch are dead.
    Any intentional restart requires a NEW work command: an old answer,
    approval, or replayed command event must not resurrect the work.
    """
    return {
        "cancellation_generation": state.publication_epoch,
        "revoked": ["tool_grants", "publication_grants"],
        "retained_artifacts": True,
        "correlated_effects": list(accepted_effects),
        "restart_requires_new_command": True,
    }


def late_effect_outcome(effect_accepted_before_cancel: bool) -> str:
    """Classify one late effect against the cancel boundary.

    An effect the provider accepted BEFORE the cancel is
    ``superseded`` — it happened, it is correlated, and the new
    generation moves on without it. Anything else is ``forbidden``: the
    boundary forbade it, so it must not land. There is no third outcome
    — a late effect is never silently absorbed.
    """
    return "superseded" if effect_accepted_before_cancel else "forbidden"
