"""NEXT-14 — ONE supervisor owns the lane drive cycle.

The runtime already had real control channels and multiple vendor models,
but the driven turn, the steering drain and the terminal classification
were three cooperating loops with three LOCAL notions of when the lane is
finished: the turn's own poll deadline, the drain's pause/resume ladder
and the lane module's outcome assembly. Nothing owned the race.

:class:`LaneSupervisor` is that owner. ONE supervisor per drive cycle
(:meth:`forge.lane_driver.drive_lane` and its codex/opencode siblings now
delegate to it — additively: with no steering attached the composed path
is byte-for-byte the old one, only the task bookkeeping moved):

- **It owns the turn task** (:meth:`submit_turn`) — the ONE driven vendor
  turn, submitted to it and awaited by nobody else.
- **It owns the steering drain task** (:meth:`submit_drain`) — the
  control consumer running CONCURRENTLY with the turn. Turn completion
  CANCELS the drain (bounded teardown: the drain's own final pass still
  runs, so a command submitted while the turn was ending is consumed and
  journaled, never dropped); an urgent control (:meth:`request_urgent`,
  called by the drain the moment an interrupt-class command APPLIES)
  SUSPENDS the turn — the vendor interrupt the drain sent ends the turn
  underneath it, and the supervisor does not let the turn's poll loop go
  on waiting on a dead turn.
- **It is the ONLY writer of the terminal outcome.** Exactly one
  :class:`TerminalEvent` is classified (through the ``classify`` callable
  the composing lane supplies), exactly once: a second :meth:`run` raises,
  a late urgent request after the turn ended is a recorded no-op, and a
  turn that raised classifies ``turn_failed`` instead of escaping the
  cycle unclassified. The classifier sees WHICH of the three ends the
  turn had — completion, failure, or suspension — plus the turn's own
  result and the urgent request that suspended it, so an operator pause
  can never be misread as a vendor completion.

The urgent seam is SYNCHRONOUS on purpose (:meth:`request_urgent` never
awaits): the drain applies an interrupt-class command and flags the
supervisor in the same scheduling slice, so nothing the turn does between
the vendor interrupt and the suspension can race the bookkeeping — the
same no-await doctrine the copilot client's cancel ledger uses.

What is deliberately NOT here: the supervisor never touches the vendor
itself (the drain's adapter calls remain the only vendor surface,
``LANE_CONTROL_SURFACE`` unchanged), never restarts a turn (resume is the
drain's own next-epoch mapping), and never re-classifies an outside
cancellation (``asyncio.RunnerError``-shaped teardown propagates — a
supervisor is not a signal handler).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

__all__ = [
    "DRAIN_CANCEL_WAIT_S",
    "TERMINAL_COMPLETED",
    "TERMINAL_FAILED",
    "TERMINAL_KINDS",
    "TERMINAL_SUSPENDED",
    "LaneSupervisor",
    "TerminalEvent",
    "UrgentRequest",
]

#: The turn reached its own terminal verdict (result returned).
TERMINAL_COMPLETED = "turn_completed"
#: The turn raised decisively — classified, never escaped unclassified.
TERMINAL_FAILED = "turn_failed"
#: An urgent control suspended the turn before its own verdict arrived.
TERMINAL_SUSPENDED = "turn_suspended"

#: The closed terminal-event vocabulary (one of these ends every cycle).
TERMINAL_KINDS: tuple[str, ...] = (TERMINAL_COMPLETED, TERMINAL_FAILED, TERMINAL_SUSPENDED)

#: The bounded wait for the drain's teardown (its final pass — the session
#: context's closing drain — rides control-plane I/O that is itself
#: ack-bounded; this bound caps how long a hung plane may hold the cycle
#: AFTER the terminal event already happened). On elapse the drain is left
#: cancelled and the supervisor records the note — the verdict stands.
DRAIN_CANCEL_WAIT_S = 10.0


@dataclass(frozen=True)
class UrgentRequest:
    """One urgent control's request to suspend the running turn.

    ``kind`` is the command kind that applied (the interrupt-class
    vocabulary the drain recognizes — today ``pause``); ``reason`` is the
    human-readable sentence the terminal classification carries, so the
    meta says WHY the turn was suspended, not merely that it was.
    """

    kind: str
    reason: str


@dataclass(frozen=True)
class TerminalEvent:
    """WHICH end the supervised turn came to, and its material.

    ``kind`` is one of :data:`TERMINAL_KINDS`; ``result`` carries the
    turn task's own return value (``turn_completed``); ``reason`` the
    failure/suspension sentence; ``urgent`` the request that suspended
    the turn (``turn_suspended``); ``elapsed_s`` the wall-clock span the
    supervisor measured from submission to terminal — the suspension
    fallback timing when the turn coroutine was cancelled before it could
    record its own.
    """

    kind: str
    result: Any = None
    reason: str = ""
    urgent: UrgentRequest | None = None
    elapsed_s: float = 0.0


T = TypeVar("T")


class LaneSupervisor(Generic[T]):
    """One drive cycle's single owner: turn task, drain task, verdict.

    Usage (the shape every delegating lane follows)::

        supervisor: LaneSupervisor[TurnResult] = LaneSupervisor(
            classify=_classify_terminal, name="claude-sdk-lane"
        )
        supervisor.submit_turn(_the_driven_turn())
        if steering is not None:
            supervisor.submit_drain(_the_steering_drain())
        outcome = await supervisor.run()

    ``classify`` maps the exactly-one :class:`TerminalEvent` to whatever
    the lane's outcome type is; the supervisor calls it ONCE and refuses
    a second verdict (``run`` raises if the terminal was already
    written).
    """

    def __init__(
        self,
        *,
        classify: Callable[[TerminalEvent], T],
        name: str = "lane",
        drain_cancel_wait_s: float = DRAIN_CANCEL_WAIT_S,
    ) -> None:
        if drain_cancel_wait_s <= 0:
            raise ValueError("drain_cancel_wait_s must be positive")
        self._classify = classify
        self._name = name
        self._drain_cancel_wait_s = drain_cancel_wait_s
        self._turn: asyncio.Task[Any] | None = None
        self._drain: asyncio.Task[None] | None = None
        self._urgent: UrgentRequest | None = None
        self._started_at: float | None = None
        self._terminal: T | None = None
        self._classifications = 0
        self.notes: list[str] = []

    # -- submission (ownership is taken exactly once each) -------------------

    def submit_turn(self, turn: Awaitable[Any]) -> asyncio.Task[Any]:
        """Take ownership of the ONE driven turn (never two per cycle)."""
        if self._classifications:
            raise RuntimeError("the terminal outcome was already written — start a new cycle")
        if self._turn is not None:
            raise RuntimeError("one turn per supervisor — the cycle owns a single turn")
        self._started_at = asyncio.get_running_loop().time()
        self._turn = asyncio.ensure_future(turn)
        return self._turn

    def submit_drain(self, drain: Awaitable[None]) -> asyncio.Task[None]:
        """Take ownership of the steering drain running beside the turn."""
        if self._classifications:
            raise RuntimeError("the terminal outcome was already written — start a new cycle")
        if self._drain is not None:
            raise RuntimeError("one drain per supervisor — the cycle owns a single drain")
        self._drain = asyncio.ensure_future(drain)
        return self._drain

    # -- urgency (the drain's synchronous seam) -------------------------------

    def request_urgent(self, kind: str, reason: str = "") -> bool:
        """Suspend the running turn NOW — the drain's urgent control.

        Called by the drain the moment an interrupt-class command APPLIES
        (never awaited: the flag and the turn cancellation happen in the
        drain's own scheduling slice, so the suspension cannot race the
        urgent bookkeeping). Returns whether the request took effect — a
        turn that already ended, or an earlier urgent request, makes this
        a recorded no-op: a late urgent can never REWRITE a verdict, and
        the first urgent wins (one suspension per cycle).
        """
        turn = self._turn
        if turn is None or turn.done() or self._urgent is not None:
            self.notes.append(
                f"urgent {kind!r} arrived after the turn's end — recorded, never re-classified"
            )
            return False
        self._urgent = UrgentRequest(kind=kind, reason=reason or kind)
        return turn.cancel()

    @property
    def urgent(self) -> UrgentRequest | None:
        """The urgent request that suspended the turn, if one did."""
        return self._urgent

    @property
    def terminal(self) -> T | None:
        """The written terminal outcome (None until the cycle classified)."""
        return self._terminal

    @property
    def classifications(self) -> int:
        """How many times the terminal outcome was written — always <= 1."""
        return self._classifications

    # -- the cycle -------------------------------------------------------------

    async def run(self) -> T:
        """Race the turn against the drain's urgency; classify exactly once.

        - the turn's own verdict wins when it arrives first — the drain is
          then cancelled (bounded) and its final pass still consumes late
          commands;
        - an urgent request suspends the turn: the cancellation is caught
          HERE (the turn's CancelledError never escapes the cycle) and the
          classifier receives ``turn_suspended`` with the request;
        - an OUTSIDE cancellation (lane teardown) is not a verdict: it
          propagates after the drain teardown ran;
        - the turn raising decisively classifies ``turn_failed`` — the
          error sentence rides the event instead of escaping unclassified.

        :meth:`run` is once-per-cycle: a second call raises, because a
        terminal outcome is written exactly once.
        """
        if self._turn is None:
            raise RuntimeError("submit the turn before running the cycle")
        if self._classifications:
            raise RuntimeError("the terminal outcome was already written — one verdict per cycle")
        turn = self._turn
        started = self._started_at if self._started_at is not None else 0.0
        event: TerminalEvent
        try:
            result = await turn
        except asyncio.CancelledError:
            urgent = self._urgent
            if urgent is None:
                # An outside cancellation (lane teardown) is not a terminal
                # verdict — tear the drain down and let it propagate.
                await self._teardown_drain()
                raise
            event = TerminalEvent(
                kind=TERMINAL_SUSPENDED,
                reason=urgent.reason,
                urgent=urgent,
                elapsed_s=self._elapsed_since(started),
            )
        except Exception as exc:  # noqa: BLE001 — classified, never unclassified
            event = TerminalEvent(
                kind=TERMINAL_FAILED,
                reason=str(exc),
                elapsed_s=self._elapsed_since(started),
            )
        else:
            event = TerminalEvent(
                kind=TERMINAL_COMPLETED,
                result=result,
                elapsed_s=self._elapsed_since(started),
            )
        await self._teardown_drain()
        return self._write_terminal(event)

    def _write_terminal(self, event: TerminalEvent) -> T:
        """The ONLY write of the terminal outcome — exactly once."""
        if self._classifications:
            raise RuntimeError("the terminal outcome was already written — one verdict per cycle")
        if event.kind not in TERMINAL_KINDS:
            raise ValueError(f"unknown terminal kind {event.kind!r}")
        outcome = self._classify(event)
        self._classifications += 1
        self._terminal = outcome
        return outcome

    def _elapsed_since(self, started: float) -> float:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — run() always has a loop
            return 0.0
        return max(0.0, loop.time() - started) if started else 0.0

    async def _teardown_drain(self) -> None:
        """Cancel the owned drain, bounded — its final pass still runs."""
        drain, self._drain = self._drain, None
        if drain is None:
            return
        drain.cancel()
        try:
            await asyncio.wait_for(drain, timeout=self._drain_cancel_wait_s)
        except asyncio.CancelledError:
            # The drain's own cancellation — the expected teardown path.
            pass
        except TimeoutError:
            self.notes.append(
                f"{self._name}: the steering drain teardown exceeded "
                f"{self._drain_cancel_wait_s}s — the drain was left cancelled; "
                "its final pass is the reconciler's to probe"
            )
        except Exception as exc:  # noqa: BLE001 — teardown never eats the verdict
            self.notes.append(f"{self._name}: the steering drain teardown failed: {exc}")
