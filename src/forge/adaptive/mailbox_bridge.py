"""The bridge that mounts one control-mailbox implementation behind the service.

The control plane (:class:`forge.adaptive.wiring.OperatorControlService`)
consumes its mailbox through ONE awaitable surface —
:class:`AsyncMailboxSurface` — satisfied by two implementations:

- memory: :class:`AsyncMailboxAdapter`, the thin async wrapper over the
  reference in-memory :class:`forge.adaptive.control.Mailbox` (and its
  :class:`~forge.adaptive.control.BroadcastMailbox` fan-out). The sync
  bodies REMAIN the implementation; the adapter's coroutines call them
  inline, so nothing here owns an event loop the caller does not. Sync
  in-process consumers (the lane steering bridge's
  ``service.mailbox.<op>`` reads, the reference tests) keep calling the
  raw mailbox exactly as before.
- durable: :class:`forge.adaptive.mailbox_db.PostgresMailbox`, used
  DIRECTLY — its methods already carry the surface's names and semantics
  as awaitables (production I/O is async). It is mounted by
  :func:`forge.adaptive.wiring.control_service_from_env` behind
  ``FORGE_CONTROL_MAILBOX=postgres`` — the flag-gated rollout; the
  default stays ``memory``.

The surface carries the names BOTH implementations share verbatim — the
CTL-04 single-consumer quartet ``submit`` / ``authorize`` /
``checkpoint`` / ``pending``, the per-work ``next_sequence`` allocation,
and the NXT-13 broadcast fan-out — as the SAME async signatures, so a
caller typed against the surface moves between implementations without
touching its logic. The coarse in-memory ``apply`` is deliberately
ABSENT from the protocol: the durable twin refines it into NXT-12's
``dispatch -> vendor_accepted | outcome_unknown -> observe`` ladder, and
that split is the review's fix — it does not collapse back into one
call. The adapter still offers :meth:`AsyncMailboxAdapter.apply` as its
memory-only rung (a lane-shaped consumer behind the adapter keeps one
seam); over the durable mailbox that leg walks the ladder instead.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Final, Protocol, cast, runtime_checkable

from forge.adaptive.control import BroadcastCommand, BroadcastMailbox, Mailbox
from forge.adaptive.models import ControlCommand

__all__ = [
    "ASYNC_SURFACE_MEMBERS",
    "AsyncMailboxAdapter",
    "AsyncMailboxSurface",
    "control_surface_for",
]

#: The unified surface, as a set — the drift detector's expected shape.
#: One closed vocabulary over both implementations: the single-consumer
#: quartet, sequence allocation, and the broadcast fan-out (NXT-13).
ASYNC_SURFACE_MEMBERS: Final = frozenset(
    {
        "submit",
        "authorize",
        "checkpoint",
        "pending",
        "next_sequence",
        "submit_broadcast",
        "acknowledge",
        "mark_uncertain",
        "resolve_uncertain",
        "pending_for",
        "broadcast",
        "broadcasts",
        "fence_active",
    }
)


@runtime_checkable
class AsyncMailboxSurface(Protocol):
    """The awaitable mailbox contract the control service codes against.

    Both implementations satisfy the SAME async signatures:
    :class:`AsyncMailboxAdapter` (this module — the in-memory reference
    mailbox behind thin coroutines) and
    :class:`forge.adaptive.mailbox_db.PostgresMailbox` (natively). The
    names, arguments and semantics are
    :class:`forge.adaptive.control.MailboxSurface`'s — the one
    documented difference from the sync protocol is gone: there is no
    "async refinement" asymmetry left to carry, because BOTH sides are
    awaitables now. ``apply`` is not a member (see the module docstring:
    the durable side owns NXT-12's finer ladder and refuses the coarse
    rung by design).
    """

    # -- the CTL-04 single-consumer quartet --------------------------------

    async def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]: ...

    async def authorize(
        self, command_id: str, actor_scopes: dict[str, tuple[str, ...]]
    ) -> ControlCommand: ...

    async def checkpoint(self, command_id: str) -> ControlCommand: ...

    async def pending(self, work_id: str) -> list[ControlCommand]: ...

    # -- sequencing (NXT-09) -------------------------------------------------

    async def next_sequence(self, work_id: str) -> int: ...

    # -- the work-wide broadcast fan-out (NXT-13) ----------------------------

    async def submit_broadcast(
        self, command: ControlCommand, recipients: Sequence[str]
    ) -> tuple[BroadcastCommand, bool]: ...

    async def acknowledge(
        self, command_id: str, recipient: str, *, note: str = ""
    ) -> BroadcastCommand: ...

    async def mark_uncertain(
        self, command_id: str, recipient: str, note: str
    ) -> BroadcastCommand: ...

    async def resolve_uncertain(
        self, command_id: str, recipient: str, *, note: str = ""
    ) -> BroadcastCommand: ...

    async def pending_for(self, work_id: str, recipient: str) -> list[ControlCommand]: ...

    async def broadcast(self, command_id: str) -> BroadcastCommand | None: ...

    async def broadcasts(self, work_id: str) -> list[BroadcastCommand]: ...

    async def fence_active(self, work_id: str) -> bool: ...


class AsyncMailboxAdapter:
    """The in-memory reference mailbox behind the unified async surface.

    Wraps one :class:`~forge.adaptive.control.Mailbox` (constructing a
    fresh one when omitted) plus a
    :class:`~forge.adaptive.control.BroadcastMailbox` over the SAME
    object, and exposes the surface's names as coroutines that call the
    sync bodies inline — no loop, no threads, no scheduling: the memory
    implementation has nothing to await, so the adapter awaits nothing
    the caller's own context does not already provide. The wrapped
    mailbox stays reachable as :attr:`mailbox` (the service keeps its
    raw reference view for sync consumers).
    """

    def __init__(self, mailbox: Mailbox | None = None) -> None:
        self.mailbox = mailbox if mailbox is not None else Mailbox()
        self._broadcasts = BroadcastMailbox(self.mailbox)

    # -- the CTL-04 single-consumer quartet --------------------------------

    async def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]:
        return self.mailbox.submit(command)

    async def authorize(
        self, command_id: str, actor_scopes: dict[str, tuple[str, ...]]
    ) -> ControlCommand:
        return self.mailbox.authorize(command_id, actor_scopes)

    async def checkpoint(self, command_id: str) -> ControlCommand:
        return self.mailbox.checkpoint(command_id)

    async def pending(self, work_id: str) -> list[ControlCommand]:
        return self.mailbox.pending(work_id)

    async def apply(
        self,
        command_id: str,
        *,
        current_plan_revision: int,
        current_execution_epoch: int,
    ) -> ControlCommand:
        """The coarse CAS rung — MEMORY-ONLY, deliberately off the protocol.

        The durable twin refines this single call into NXT-12's
        ``dispatch -> vendor_accepted | outcome_unknown -> observe``
        ladder (intent persisted before the vendor call, application
        booked only after it), so a caller behind the ADAPTER may use
        this rung while a caller behind :class:`PostgresMailbox
        <forge.adaptive.mailbox_db.PostgresMailbox>` must walk the
        ladder — the asymmetry the protocol encodes by omitting the name.
        """
        return self.mailbox.apply(
            command_id,
            current_plan_revision=current_plan_revision,
            current_execution_epoch=current_execution_epoch,
        )

    # -- sequencing (NXT-09) -------------------------------------------------

    async def next_sequence(self, work_id: str) -> int:
        """``max(sequence) + 1`` over the work's accepted commands.

        The same allocation the durable mailbox derives from
        ``max(sequence)`` — per WORK, never per process: the reference
        store's global command count was only ever a proxy for it.
        """
        sequences = [
            command.sequence
            for command in self.mailbox.commands.values()
            if command.work_id == work_id
        ]
        return max(sequences, default=0) + 1

    # -- the work-wide broadcast fan-out (NXT-13) ----------------------------

    async def submit_broadcast(
        self, command: ControlCommand, recipients: Sequence[str]
    ) -> tuple[BroadcastCommand, bool]:
        return self._broadcasts.submit(command, tuple(recipients))

    async def acknowledge(
        self, command_id: str, recipient: str, *, note: str = ""
    ) -> BroadcastCommand:
        return self._broadcasts.acknowledge(command_id, recipient, note=note)

    async def mark_uncertain(self, command_id: str, recipient: str, note: str) -> BroadcastCommand:
        return self._broadcasts.mark_uncertain(command_id, recipient, note)

    async def resolve_uncertain(
        self, command_id: str, recipient: str, *, note: str = ""
    ) -> BroadcastCommand:
        return self._broadcasts.resolve_uncertain(command_id, recipient, note=note)

    async def pending_for(self, work_id: str, recipient: str) -> list[ControlCommand]:
        return self._broadcasts.pending_for(work_id, recipient)

    async def broadcast(self, command_id: str) -> BroadcastCommand | None:
        return self._broadcasts.broadcast(command_id)

    async def broadcasts(self, work_id: str) -> list[BroadcastCommand]:
        return self._broadcasts.broadcasts(work_id)

    async def fence_active(self, work_id: str) -> bool:
        return self._broadcasts.fence_active(work_id)


def control_surface_for(mailbox: object) -> AsyncMailboxSurface:
    """The unified async surface over *mailbox* — either implementation.

    An object whose ``submit`` is a coroutine function
    (:class:`~forge.adaptive.mailbox_db.PostgresMailbox`, or an async
    test fake shaped like it) IS the surface already; anything else (the
    in-memory reference :class:`~forge.adaptive.control.Mailbox`) is
    wrapped in :class:`AsyncMailboxAdapter`. The dispatch happens once,
    at service construction — never per call — so the hot path pays a
    single ``iscoroutinefunction`` check and nothing else.
    """
    submit = getattr(type(mailbox), "submit", None)
    if submit is not None and inspect.iscoroutinefunction(submit):
        return cast("AsyncMailboxSurface", mailbox)
    return AsyncMailboxAdapter(cast("Mailbox", mailbox))
