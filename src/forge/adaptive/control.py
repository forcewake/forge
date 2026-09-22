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
  epoch cannot authorize new effects, a cooperative drain captures WIP
  ONLY through a REAL capture capability (NXT-15: the drain runs the
  capture — serialize the working tree, upload the blobs, VERIFY the
  digests by reading them back, commit the manifest as the checkpoint
  reference — :mod:`forge.adaptive.checkpointing` implements it), and
  a missing capability or failed capture lands the pause as
  ``paused_partial`` / ``paused_failed`` naming the last REAL
  recoverable checkpoint — never a fabricated artifact id, never a
  false clean pause.
- CTL-06 — resume runs only from a confirmed checkpoint with the
  snapshot available and permissions valid, under a FRESH execution
  epoch; the strict gate
  (:func:`resume_check` ``require_verified_checkpoint=True`` /
  :func:`forge.adaptive.checkpointing.resume_from_checkpoint`)
  re-verifies the CURRENT authorization and the checkpoint BYTES now —
  no constructor-default booleans — and a native session is restored
  only for a compatible PINNED profile WITH verified restore evidence
  (a profile name alone claims nothing), everything else reconstructs
  from durable artifacts (the same behavior on a host with no vendor
  session files).
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
    "BROADCAST_SCOPE",
    "BroadcastCommand",
    "BroadcastMailbox",
    "BroadcastStatus",
    "CaptureResult",
    "Mailbox",
    "MailboxSurface",
    "PauseState",
    "PauseStatus",
    "RecipientAckStatus",
    "RecipientAcknowledgement",
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

        Work-wide (broadcast) parents are deliberately ABSENT from this
        single-consumer view (NXT-13): one command with one status is
        exactly the defect that let the first lane to checkpoint a
        pause remove it from every other lane's queue. A broadcast is
        delivered ONLY through its per-recipient views
        (:meth:`BroadcastMailbox.pending_for`), each lane consuming its
        own acknowledgement row independently.
        """
        return sorted(
            (
                command
                for command in self.commands.values()
                if command.work_id == work_id
                and command.status in ("received", "authorized")
                and command.payload.get("scope") != BROADCAST_SCOPE
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


# ---------------------------------------------------------------------------
# NXT-13 — work-wide controls fanned out with per-recipient acknowledgements
# ---------------------------------------------------------------------------

#: The payload marker that makes a command a work-wide BROADCAST parent.
#: A parent carrying ``{"scope": "work", "recipients": [...]}`` is never
#: delivered through the single-consumer ``pending()`` view — only through
#: its per-recipient acknowledgement rows (one lane consuming it says
#: nothing about the other lanes' consumption).
BROADCAST_SCOPE: Final = "work"

#: The kinds whose work-wide delivery fences NEW child starts while the
#: broadcast is still pending: a pause that not every recipient has
#: acknowledged must not be raced by a lane that started after the
#: snapshot was taken.
_FENCE_KINDS: Final = frozenset({"pause"})

#: The per-recipient acknowledgement ladder (NXT-13). Each recipient
#: climbs INDEPENDENTLY: ``pending`` (the row exists, the lane has not
#: spoken), ``acknowledged`` (this lane checkpointed/applied the command
#: — the only rung that satisfies the parent barrier), ``uncertain``
#: (this lane's outcome could NOT be proven — the honest state a
#: reconciliation surfaces instead of guessing; individually visible at
#: the parent, never hidden behind another lane's success).
RecipientAckStatus = Literal["pending", "acknowledged", "uncertain"]

#: The parent barrier's derived status. ``completed`` means EVERY
#: recipient acknowledged; ``completed_with_uncertain`` means every
#: recipient is decided with at least one explicitly ``uncertain`` — the
#: review's "all children quiescent OR an explicitly listed uncertain
#: subset"; anything undecided is ``pending``.
BroadcastStatus = Literal["pending", "completed", "completed_with_uncertain"]


@dataclass(frozen=True)
class RecipientAcknowledgement:
    """One recipient's delivery row: parent intent seen from ONE lane.

    ``note`` carries the lane's own evidence — the checkpoint receipt
    address on an acknowledgement, the probe result on an uncertainty —
    because "lane 2 acknowledged" must be distinguishable from "lane 2
    was silent" by reading the row, not by asking the lane.
    """

    recipient: str
    status: RecipientAckStatus = "pending"
    note: str = ""


@dataclass(frozen=True)
class BroadcastCommand:
    """One parent intent + a FIXED recipient set + their ack rows (NXT-13).

    The wrapper is the value object the work-wide control plane reads:
    the parent command (the durable intent, exactly one mailbox row),
    the recipients snapshotted at submission (the work's lane set — the
    set is frozen, so a lane created DURING the pause is not silently
    added; it is fenced out by :meth:`BroadcastMailbox.fence_active`
    until the operator re-scopes), and one acknowledgement per
    recipient. Parent semantics:

    - each recipient consumes independently — acknowledging for one lane
      never marks another lane's row;
    - ``status == "completed"`` ONLY when every recipient acknowledged
      (the work-wide barrier; one lane's success cannot hide another
      lane's missing or failed acknowledgement);
    - ``uncertain`` recipients stay INDIVIDUALLY visible
      (:attr:`uncertain_recipients`) — the parent completes with them
      listed, never by rounding them up to acknowledged.
    """

    parent: ControlCommand
    acknowledgements: tuple[RecipientAcknowledgement, ...]

    def __post_init__(self) -> None:
        if not self.acknowledgements:
            raise ValueError(
                "a broadcast with an empty recipient set is no broadcast — "
                "refusing to pretend a fan-out with nothing fanned out"
            )
        seen = [ack.recipient for ack in self.acknowledgements]
        if len(seen) != len(set(seen)):
            raise ValueError(f"a recipient appears twice in one broadcast: {sorted(seen)}")

    # -- identity passthroughs (the parent IS the command) ----------------

    @property
    def command_id(self) -> str:
        return self.parent.command_id

    @property
    def work_id(self) -> str:
        return self.parent.work_id

    @property
    def sequence(self) -> int:
        return self.parent.sequence

    @property
    def kind(self) -> str:
        return self.parent.kind

    @property
    def recipients(self) -> tuple[str, ...]:
        return tuple(ack.recipient for ack in self.acknowledgements)

    # -- the barrier --------------------------------------------------------

    @property
    def status(self) -> BroadcastStatus:
        """ALL acknowledged → ``completed``; all decided with an explicit
        uncertain subset → ``completed_with_uncertain``; else ``pending``."""
        states = {ack.status for ack in self.acknowledgements}
        if states == {"acknowledged"}:
            return "completed"
        if states <= {"acknowledged", "uncertain"} and "uncertain" in states:
            return "completed_with_uncertain"
        if states == {"uncertain"}:
            return "completed_with_uncertain"
        return "pending"

    @property
    def acknowledged_recipients(self) -> tuple[str, ...]:
        return tuple(a.recipient for a in self.acknowledgements if a.status == "acknowledged")

    @property
    def unacknowledged_recipients(self) -> tuple[str, ...]:
        return tuple(a.recipient for a in self.acknowledgements if a.status == "pending")

    @property
    def uncertain_recipients(self) -> tuple[str, ...]:
        """The individually-visible uncertainty list — never collapsed."""
        return tuple(a.recipient for a in self.acknowledgements if a.status == "uncertain")

    def acknowledgement(self, recipient: str) -> RecipientAcknowledgement | None:
        """One recipient's row, or ``None`` for a non-recipient lane."""
        return next((a for a in self.acknowledgements if a.recipient == recipient), None)

    def view_for(self, recipient: str) -> ControlCommand | None:
        """The per-recipient VIEW of the parent, or ``None`` for a stranger.

        The view is the command the lane consumes: the parent's bytes
        with the recipient's ``run_id`` scope and the broadcast id in
        the payload — so the single-lane scope check (a lane ignores
        commands targeted at another run) accepts exactly its own view
        and no foreign lane can consume the wrong delivery.
        """
        if self.acknowledgement(recipient) is None:
            return None
        return broadcast_view(self.parent, recipient)

    def _with(self, recipient: str, status: RecipientAckStatus, note: str) -> BroadcastCommand:
        """One recipient's row replaced; every other row untouched."""
        return BroadcastCommand(
            parent=self.parent,
            acknowledgements=tuple(
                replace(ack, status=status, note=note) if ack.recipient == recipient else ack
                for ack in self.acknowledgements
            ),
        )


def broadcast_view(parent: ControlCommand, recipient: str) -> ControlCommand:
    """Project the parent into the recipient's run-scoped view.

    The view keeps the parent's identity (``command_id`` — the
    acknowledgement key) and sequence; the payload gains the recipient's
    ``run_id`` plus the ``broadcast_id`` correlation, so a lane's own
    scope machinery delivers exactly its view of the work-wide intent.
    """
    payload = dict(parent.payload)
    payload["run_id"] = recipient
    payload["broadcast_id"] = parent.command_id
    return parent.model_copy(update={"payload": payload})


class BroadcastMailbox:
    """The work-wide fan-out over a single-consumer :class:`Mailbox` (NXT-13).

    The parent is one durable row in the wrapped mailbox (sequence
    discipline, idempotency-key dedup and the ladder all apply to it),
    but its DELIVERY is per-recipient: each lane reads
    :meth:`pending_for` — its own view of every broadcast it has not
    acknowledged — and acknowledges independently. The single-lane path
    is untouched: ordinary commands keep flowing through
    :meth:`Mailbox.pending` exactly as before.

    Acknowledgement ladder per recipient: ``pending -> acknowledged``
    (:meth:`acknowledge`), ``pending -> uncertain``
    (:meth:`mark_uncertain` — the probe could not decide; the parent
    completes with the lane listed, it is never rounded up), and
    ``uncertain -> acknowledged`` (:meth:`resolve_uncertain` — later
    evidence settled it). An acknowledged row is terminal: re-acking is
    an idempotent no-op, and un-deciding one raises. A foreign lane
    (not in the frozen recipient set) can neither see a view nor
    acknowledge — ``None`` / :class:`KeyError`.
    """

    def __init__(self, mailbox: Mailbox | None = None) -> None:
        self.mailbox = mailbox if mailbox is not None else Mailbox()
        #: command_id -> the broadcast value object.
        self._broadcasts: dict[str, BroadcastCommand] = {}

    def submit(
        self, command: ControlCommand, recipients: tuple[str, ...]
    ) -> tuple[BroadcastCommand, bool]:
        """Fan one parent intent out to a FIXED recipient set.

        The parent payload is stamped ``scope=work`` with the recipients
        BEFORE it enters the wrapped mailbox, so the single-consumer
        ``pending()`` never delivers it (NXT-13's defect: one status,
        first lane wins). A command already scoped to one ``run_id``
        cannot become a broadcast — that contradiction is refused. The
        recipient set is frozen at submission: redelivery (same
        idempotency key) adopts the winner's broadcast VERBATIM — the
        replay's recipient list is discarded, exactly as its other bytes
        are — and changes nothing.
        """
        if not recipients:
            raise ValueError("a work-wide command needs the work's lane set — empty recipients")
        if len(recipients) != len(set(recipients)):
            raise ValueError(f"recipients must be unique: {list(recipients)}")
        if isinstance(command.payload.get("run_id"), str):
            raise ValueError(
                "a command already scoped to one run cannot become a work-wide broadcast"
            )
        payload = dict(command.payload)
        payload["scope"] = BROADCAST_SCOPE
        payload["recipients"] = list(recipients)
        stored, created = self.mailbox.submit(command.model_copy(update={"payload": payload}))
        existing = self._broadcasts.get(stored.command_id)
        if existing is not None:
            return existing, False
        broadcast = BroadcastCommand(
            parent=stored,
            acknowledgements=tuple(
                RecipientAcknowledgement(recipient=recipient) for recipient in recipients
            ),
        )
        self._broadcasts[stored.command_id] = broadcast
        return broadcast, created

    def acknowledge(self, command_id: str, recipient: str, *, note: str = "") -> BroadcastCommand:
        """One lane's checkpointed/apply acknowledgement (idempotent)."""
        broadcast = self._require_broadcast(command_id)
        ack = self._require_recipient(broadcast, recipient)
        if ack.status == "acknowledged":
            return broadcast  # redelivered ack — nothing re-spent
        if ack.status != "pending":
            raise ValueError(
                f"recipient {recipient!r} of {command_id!r} is {ack.status!r}; an uncertain "
                "lane is settled through resolve_uncertain, never re-acknowledged blind"
            )
        advanced = broadcast._with(recipient, "acknowledged", note)
        self._broadcasts[command_id] = advanced
        return advanced

    def mark_uncertain(self, command_id: str, recipient: str, note: str) -> BroadcastCommand:
        """Record that this lane's outcome could NOT be proven.

        The honest give-up: the row SAYS uncertain — never acknowledged
        (unproven) and never silently dropped. The parent completes only
        with the lane explicitly listed in
        :attr:`BroadcastCommand.uncertain_recipients`.
        """
        broadcast = self._require_broadcast(command_id)
        ack = self._require_recipient(broadcast, recipient)
        if ack.status != "pending":
            raise ValueError(
                f"recipient {recipient!r} of {command_id!r} is {ack.status!r}; only a "
                "pending recipient can be marked uncertain"
            )
        advanced = broadcast._with(recipient, "uncertain", note)
        self._broadcasts[command_id] = advanced
        return advanced

    def resolve_uncertain(
        self, command_id: str, recipient: str, *, note: str = ""
    ) -> BroadcastCommand:
        """Settle an uncertain lane with the evidence that later arrived."""
        broadcast = self._require_broadcast(command_id)
        ack = self._require_recipient(broadcast, recipient)
        if ack.status != "uncertain":
            raise ValueError(
                f"recipient {recipient!r} of {command_id!r} is {ack.status!r}; only an "
                "uncertain recipient can be resolved"
            )
        advanced = broadcast._with(recipient, "acknowledged", note)
        self._broadcasts[command_id] = advanced
        return advanced

    def pending_for(self, work_id: str, recipient: str) -> list[ControlCommand]:
        """The recipient's OWN lane view: every broadcast of the work this
        recipient has not acknowledged, as run-scoped views in sequence
        order. A lane that already acknowledged (or was marked uncertain)
        does not see the command again — and no other lane's consumption
        can remove it from THIS view."""
        views = (
            broadcast.view_for(recipient)
            for broadcast in self._broadcasts.values()
            if broadcast.work_id == work_id
            and broadcast.acknowledgement(recipient) is not None
            and broadcast.acknowledgement(recipient).status == "pending"  # type: ignore[union-attr]
        )
        return sorted((view for view in views if view is not None), key=lambda view: view.sequence)

    def broadcast(self, command_id: str) -> BroadcastCommand | None:
        """The broadcast value object, or ``None`` for an ordinary command."""
        return self._broadcasts.get(command_id)

    def broadcasts(self, work_id: str) -> list[BroadcastCommand]:
        """The work's broadcasts in sequence order (the operator's view)."""
        return sorted(
            (b for b in self._broadcasts.values() if b.work_id == work_id),
            key=lambda b: b.sequence,
        )

    def fence_active(self, work_id: str) -> bool:
        """True while a work-wide pause is not yet decided for every lane.

        The fence for FUTURE child starts (NXT-13): a lane created after
        the snapshot must not begin work while a work-wide pause is still
        awaiting acknowledgement — the recipient set is frozen, so the
        new lane's only honest options are to wait for the fence to lift
        (or for the operator to re-scope a fresh broadcast that includes
        it), never to start straight through an in-flight pause. A
        broadcast that completed with an explicitly uncertain subset has
        finished deciding — the fence lifts and the uncertainty remains
        individually visible at the parent.
        """
        return any(
            broadcast.kind in _FENCE_KINDS and broadcast.status == "pending"
            for broadcast in self._broadcasts.values()
            if broadcast.work_id == work_id
        )

    def _require_broadcast(self, command_id: str) -> BroadcastCommand:
        broadcast = self._broadcasts.get(command_id)
        if broadcast is None:
            raise KeyError(f"unknown broadcast command_id {command_id!r}")
        return broadcast

    @staticmethod
    def _require_recipient(broadcast: BroadcastCommand, recipient: str) -> RecipientAcknowledgement:
        ack = broadcast.acknowledgement(recipient)
        if ack is None:
            raise KeyError(
                f"lane {recipient!r} is not in the frozen recipient set of "
                f"{broadcast.command_id!r} ({list(broadcast.recipients)})"
            )
        return ack


#: The honest pause outcomes (NXT-15). ``running``/``pausing`` are the
#: in-flight legs; the drain decides the terminal one:
#: ``paused`` — a digest-verified checkpoint was COMMITTED (receipt on
#: record); ``paused_partial`` — the turn ended at a cooperative
#: boundary but NO durable WIP was captured (the steering bridge's
#: in-session drain today: the vendor session may hold the conversation,
#: nothing in the store holds the workspace); ``paused_failed`` — a
#: capture was attempted and failed, or the runner was terminated by
#: timeout. Only ``paused`` claims saved work.
PauseStatus = Literal["running", "pausing", "paused", "paused_partial", "paused_failed"]

_TERMINAL_PAUSE_STATUSES: Final = ("paused", "paused_partial", "paused_failed")


@runtime_checkable
class CaptureResult(Protocol):
    """What a real WIP capture returns (implemented by
    :class:`forge.adaptive.checkpointing.CheckpointReceipt`).

    ``artifact_id``/``digest`` are the content address of the committed
    checkpoint manifest in the artifact store — the same value twice
    because the store's address IS its digest. ``sequence`` is the
    applied-command watermark the checkpoint carries.
    ``verified`` is True only when every uploaded blob and the manifest
    itself were READ BACK and re-hashed to their addresses before the
    receipt was minted.
    """

    artifact_id: str
    digest: str
    sequence: int
    verified: bool


@dataclass(frozen=True)
class PauseState:
    """The durable pause book for one work (CTL-05).

    Value semantics: every transition returns a new instance and the
    caller persists it, so the on-disk record is always a state that
    actually held — never a half-applied one.

    ``publication_epoch`` is the fence CTL-05/CTL-08 share: a grant
    minted under an epoch lower than the current one is dead and can
    never authorize a new effect.

    ``pause_status`` (NXT-15) is the honest terminal claim — see
    :data:`PauseStatus`. ``checkpoint_captured`` is the bridge-level
    claim that the drain reached a cooperative boundary with the turn's
    state retained SOMEWHERE (the vendor session, for the in-session
    steering bridge); the DURABLE truth is ``pause_status == "paused"``
    plus a verified ``checkpoint_receipt`` — the strict resume gate
    checks exactly that pair, never the flag alone. ``wip_artifact_id``
    is only ever a REAL store address handed in by a completed capture
    — this module never fabricates one.
    """

    work_id: str
    publication_epoch: int = 0
    pause_requested: bool = False
    interrupt_sent: bool = False
    checkpoint_captured: bool = False
    last_applied_command_sequence: int = 0
    wip_artifact_id: str | None = None
    pause_status: PauseStatus = "running"
    checkpoint_receipt: CaptureResult | None = None
    last_checkpoint_sequence: int | None = None
    failure_reason: str = ""

    @property
    def last_recoverable(self) -> int:
        """The sequence the last RECOVERABLE checkpoint carries.

        ``last_checkpoint_sequence`` is set ONLY by a real committed
        capture, so on a partial or failed pause this names the last
        REAL checkpoint — the thing resume can actually stand on. When
        no durable capture ever happened it falls back to the applied
        watermark (the bridge's book), never to an invented number.
        """
        if self.last_checkpoint_sequence is not None:
            return self.last_checkpoint_sequence
        return self.last_applied_command_sequence


def request_pause(state: PauseState) -> PauseState:
    """Set ``pause_requested`` — and nothing else.

    Persist ``pause_requested`` BEFORE sending any interrupt: the
    ordering IS the guarantee. A process that dies between the two
    leaves a pause on record (an unacknowledged-but-paused work), never
    an interrupted runner with no pause behind it.
    """
    status: PauseStatus = "pausing" if state.pause_status == "running" else state.pause_status
    return replace(state, pause_requested=True, pause_status=status)


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
    state: PauseState,
    *,
    cooperative: bool,
    wip_artifact_id: str | None = None,
    capture: Callable[[], CaptureResult] | None = None,
) -> PauseState:
    """Drain the active turn after the interrupt (NXT-15's transaction end).

    Cooperative means the runner's OWN turn-completed signal was
    observed (quiescence), so the drain may run a REAL capture
    capability: serialize the working tree, upload the blobs, verify
    the digests, commit the manifest — and only a VERIFIED receipt
    lands ``pause_status="paused"`` with the checkpoint reference
    recorded. A capture that raises (disk full, corrupt upload) or
    returns an unverified receipt lands ``paused_failed`` with the
    failure on record and the last REAL checkpoint still named by
    :attr:`PauseState.last_recoverable` — never a fabricated success.

    A cooperative drain with NO capture capability lands
    ``paused_partial``: the turn ended at a boundary, but nothing
    durable was captured, so no artifact id is invented (the old
    ``artifact:wip:<work_id>`` fabrication is gone) and only the vendor
    session's own state survives. ``wip_artifact_id`` supplied directly
    is the caller stating a REAL store address from a transaction it
    ran itself — recorded verbatim, still never fabricated here.

    Timeout (``cooperative=False``): the runner was terminated, so
    nothing from this turn is claimed — ``paused_failed`` unless the
    drain already decided, and :attr:`PauseState.last_recoverable`
    exposes the previous RECOVERABLE checkpoint, not a false clean
    pause. Re-draining a decided pause is an idempotent no-op.
    """
    if state.pause_status in _TERMINAL_PAUSE_STATUSES:
        return state
    if not cooperative:
        return replace(state, pause_status="paused_failed")
    if capture is not None:
        try:
            receipt = capture()
        except Exception as exc:  # noqa: BLE001 — the honest failed-capture booking
            return replace(
                state,
                checkpoint_captured=False,
                wip_artifact_id=None,
                checkpoint_receipt=None,
                pause_status="paused_failed",
                failure_reason=f"capture failed: {exc}",
            )
        if not receipt.verified:
            return replace(
                state,
                checkpoint_captured=False,
                wip_artifact_id=None,
                checkpoint_receipt=None,
                pause_status="paused_failed",
                failure_reason="capture unverified: uploaded bytes were not read back "
                "digest-identical; nothing was committed",
            )
        return replace(
            state,
            checkpoint_captured=True,
            wip_artifact_id=receipt.artifact_id,
            checkpoint_receipt=receipt,
            pause_status="paused",
            last_checkpoint_sequence=receipt.sequence,
            failure_reason="",
        )
    if wip_artifact_id is not None:
        # The caller ran the transaction itself and states the artifact's
        # REAL store address — recorded verbatim, never invented here.
        return replace(
            state,
            checkpoint_captured=True,
            wip_artifact_id=wip_artifact_id,
            pause_status="paused",
            last_checkpoint_sequence=state.last_applied_command_sequence,
        )
    return replace(
        state, checkpoint_captured=True, wip_artifact_id=None, pause_status="paused_partial"
    )


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
    *,
    require_verified_checkpoint: bool = False,
) -> tuple[bool, str]:
    """Gate resume on a confirmed checkpoint, snapshot, and permissions.

    Resumable only when the pause captured a checkpoint AND the snapshot
    set is still available AND the current permissions are valid. The
    plan revision is INFORMATIONAL — recorded in the reason, never
    compared: historical spend and pending remote effects stay attached
    to the same work whatever revision is now active.

    ``require_verified_checkpoint`` is the NXT-18 strict gate: it
    additionally demands ``pause_status == "paused"`` WITH a verified
    :attr:`PauseState.checkpoint_receipt` — a partial pause (nothing
    captured durably) or a failed one refuses, because the strict path
    (:func:`forge.adaptive.checkpointing.resume_from_checkpoint`)
    re-reads the checkpoint BYTES and re-checks authorization NOW
    rather than trusting booleans supplied at construction time. The
    default (False) keeps the in-session steering bridge's legacy gate:
    its cooperative-boundary claim plus live snapshot/permission flags.
    """
    if not checkpoint_state.checkpoint_captured:
        return False, "no confirmed checkpoint: the pause ended without capturing WIP"
    if require_verified_checkpoint:
        receipt = checkpoint_state.checkpoint_receipt
        if checkpoint_state.pause_status != "paused" or receipt is None or not receipt.verified:
            return False, (
                f"no verified durable checkpoint: pause_status="
                f"{checkpoint_state.pause_status!r} with "
                f"{'no' if receipt is None else 'an unverified'} receipt — resume must "
                "stand on a digest-verified capture, not a cooperative-boundary claim"
            )
    if not snapshot_available:
        return False, "snapshot set unavailable: resume must not run from an unbound source"
    if not permissions_valid:
        return False, "permissions invalid: resume must not inherit stale authorizations"
    return True, (f"resumable: active plan revision {active_plan_revision} recorded, not compared")


#: Profiles whose native (vendor) session files are safe to restore across
#: an epoch bump; anything else reconstructs from durable artifacts.
_PINNED_PROFILES: Final = frozenset({"claude-sdk", "codex-app", "opencode-server"})


def new_execution_epoch(
    attempt_id: str,
    prior_epoch: int,
    pinned_profile: str | None = None,
    *,
    native_restore_evidence: str | None = None,
) -> dict:
    """Open a fresh execution epoch for a resume (CTL-06, NXT-18).

    The new epoch starts at ``prior_epoch + 1`` so late artifacts from
    the interrupted attempt cannot masquerade as current ones. The
    native session is restored ONLY when a compatible PINNED profile is
    named AND *native_restore_evidence* carries the content address of
    a native-session artifact whose restore was actually verified — a
    profile NAME alone never yields ``native_session_restored=true``
    (membership is compatibility, not a completed restore). An unpinned
    or unknown profile (or a provider/model change, or missing
    evidence) reconstructs from durable task artifacts, which is
    exactly how a resume on another host with no vendor session files
    must behave.
    """
    native = (
        pinned_profile is not None
        and pinned_profile in _PINNED_PROFILES
        and native_restore_evidence is not None
        and bool(native_restore_evidence.strip())
    )
    return {
        "attempt_id": attempt_id,
        "execution_epoch": prior_epoch + 1,
        "native_session_restored": native,
        "reconstruction": "native_session" if native else "durable_artifacts",
        "native_restore_evidence": native_restore_evidence if native else None,
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
