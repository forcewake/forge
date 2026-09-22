"""The lane-side control channel — the CI lane's OUTBOUND steering leg (NXT-10).

The lane job (GitHub Actions / GitLab / AzDO) runs where the control plane
can never reach IN (EXE-04: no inbound listener on an ephemeral runner),
while the operator's commands land DURABLY in the forge app's
``control_commands`` rows. :mod:`forge.lane_driver`'s steering attach
consumed an in-process in-memory mailbox — a queue the control plane's
rows can never enter. This module is the missing half: when the dispatch
env carries ``FORGE_LANE_CONTROL_URL`` + ``FORGE_LANE_CONTROL_TOKEN``,
:func:`lane_channel_from_env` swaps the lane-local mailbox for a
:class:`LaneControlChannel` — the SAME sync
:class:`~forge.adaptive.control.MailboxSurface` names the
:class:`~forge.adaptive.lane_control.LaneSteeringSession` drain already
calls (``pending`` / ``authorize`` / ``apply`` / ``checkpoint``), so the
drain path is UNCHANGED; only the backing store moves from a process
dictionary to the control plane's durable rows.

The channel is a poller, not a listener:

- an async fetch loop (``FORGE_LANE_CONTROL_POLL_SECONDS``, default 2 s)
  GETs ``/lane/controls?work_id=<run-id>&after_sequence=<cursor>`` on the
  lane's OWN cadence and buffers what is new — the drain's ``pending()``
  reads the buffer, so a slow or unreachable API never blocks the event
  loop that drives the vendor turn;
- the drain's ladder calls POST one bounded synchronous ack each
  (``authorize`` → ``authorized``, ``apply`` → ``dispatching`` under the
  CTL-04 CAS world, ``checkpoint`` → the ``checkpointed`` climb) — rare,
  per-command round trips whose RESULTS the gate needs before any vendor
  effect runs (a command written against a stale world must EXPIRE at the
  gate, never at the vendor);
- every ack carries a channel journal row that the control plane APPENDS
  to the command's audit journal — the lane's evidence, posted back.

Failure doctrine — steering must never kill the task: an unreachable API
is journaled as a channel error row (``channel_journal``, capped) and the
turn keeps running; a fetch that cannot be proven leaves the buffer as
it was (never a guessed command); an ack that cannot be delivered RAISES
so the mailbox gate REFUSES the command this cycle ("left in the mailbox
for the reconciler") — the honest no, never a silent optimistic booking
the durable row cannot support. The channel journal rides the lane meta
beside the steering journal (:func:`forge.lane_driver._steering_journal`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final
from urllib.parse import quote as _urlquote

import httpx

from forge.adaptive.models import ControlCommand
from forge.adaptive.wiring import OperatorControlService

__all__ = [
    "DEFAULT_ACK_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "LANE_CONTROL_GENERATION_ENV",
    "LANE_CONTROL_POLL_ENV",
    "LANE_CONTROL_TOKEN_ENV",
    "LANE_CONTROL_URL_ENV",
    "LaneControlChannel",
    "lane_channel_from_env",
]

#: The dispatch env pair that swaps the lane-local mailbox for the remote
#: channel. BOTH must be set — a URL without a token (or vice versa) is a
#: misconfigured dispatch and the lane stays on the honest local default
#: rather than dialing out unauthenticated.
LANE_CONTROL_URL_ENV: Final = "FORGE_LANE_CONTROL_URL"
LANE_CONTROL_TOKEN_ENV: Final = "FORGE_LANE_CONTROL_TOKEN"

#: The fetch cadence override (seconds; malformed fails CLOSED to the
#: default rather than guessing a cadence the operator did not set).
LANE_CONTROL_POLL_ENV: Final = "FORGE_LANE_CONTROL_POLL_SECONDS"

#: The runner generation this lane speaks for (R28-07/R28-10): the
#: dispatch injects the run's CURRENT generation and the lane's acks
#: carry it, so a superseded generation's ack is refused by the control
#: plane instead of moving the new attempt's state. Malformed or unset
#: means "no generation declared" — the honest migration default (the
#: pre-generation control plane accepts exactly that).
LANE_CONTROL_GENERATION_ENV: Final = "FORGE_LANE_CONTROL_GENERATION"

#: The fetch loop's cadence — an order slower than the drain's own poll:
#: the buffer decouples them, and the control plane is not hot.
DEFAULT_POLL_INTERVAL_S: Final = 2.0

#: The bounded wait for one synchronous ack round trip. Acks are rare
#: (per command) and their results gate vendor effects, so they wait —
#: but never unbounded (a hung control plane must not hang the drain).
DEFAULT_ACK_TIMEOUT_S: Final = 5.0

#: Cap on retained channel error rows — evidence, not a log sink.
_JOURNAL_CAP: Final = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LaneControlChannel:
    """One lane's outbound control channel: poller + mailbox facade.

    Usable as the ``mailbox`` of an
    :class:`~forge.adaptive.wiring.OperatorControlService`
    (:meth:`service`) — the construction
    :mod:`forge.lane_driver` performs — and as an async context that
    bounds the fetch loop's lifetime to the driven turn::

        async with LaneControlChannel(base_url=..., token=..., work_id=...) as ch:
            steering = LaneSteeringSession(service=ch.service(), ...)
            async with steering.attach():
                await the_turn()

    The sync surface methods are safe without the context too: ``pending``
    falls back to ONE direct fetch when no loop-managed fetcher is running
    (sync callers, tests), and acks always POST directly.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        work_id: str,
        run_id: str = "",
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT_S,
        generation: int | None = None,
    ) -> None:
        if not base_url or not token or not work_id:
            raise ValueError("base_url, token and work_id must be non-empty")
        if poll_interval <= 0 or ack_timeout <= 0:
            raise ValueError("poll_interval and ack_timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.work_id = work_id
        self.run_id = run_id
        self.poll_interval = poll_interval
        self.ack_timeout = ack_timeout
        #: The runner generation this lane's acks declare (R28-10): the
        #: dispatch minted the token FOR this generation, and the control
        #: plane refuses an ack whose declared generation the work has
        #: superseded. ``None`` declares nothing (pre-generation lanes).
        self.generation = generation
        self.vendor_session_id = ""
        #: The lane's delivery cursor — the highest sequence handed to the
        #: drain through :meth:`pending`. The next fetch asks for
        #: ``after_sequence=cursor``, so a command is delivered ONCE per
        #: channel lifetime (a restarted lane re-fetches everything still
        #: pending server-side, which is exactly the redelivery it owes).
        self._cursor = 0
        self._buffer: list[ControlCommand] = []
        self._channel_journal: list[dict[str, Any]] = []
        self._fetch_task: asyncio.Task[None] | None = None
        self._stopped = False

    # -- construction seam ---------------------------------------------------

    def service(self) -> OperatorControlService:
        """The control service over this channel (the lane_driver swap).

        The service's ``mailbox`` IS the channel — the sync surface the
        steering session's drain reads; the service's async seam wraps it
        like any sync mailbox (the lane never calls the async surface).
        """
        return OperatorControlService(mailbox=self)  # type: ignore[arg-type]

    # -- the async context: the fetch loop's lifetime -------------------------

    async def __aenter__(self) -> LaneControlChannel:
        self._stopped = False
        if self._fetch_task is None:
            self._fetch_task = asyncio.create_task(self._fetch_loop(), name="lane-control-channel")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stopped = True
        task, self._fetch_task = self._fetch_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _fetch_loop(self) -> None:
        """Poll the control plane on the lane's own cadence, forever bounded.

        One shared :class:`httpx.AsyncClient` for the loop's lifetime;
        failures journal an error row and the NEXT cycle retries — the
        buffer is only ever replaced by a PROVEN response, so an
        unreachable API degrades to "no new commands", never to a guessed
        or emptied queue.
        """
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._auth_headers(),
            timeout=self.ack_timeout,
        ) as client:
            while not self._stopped:
                await self._fetch_once(client)
                await asyncio.sleep(self.poll_interval)

    async def _fetch_once(self, client: httpx.AsyncClient) -> None:
        try:
            response = await client.get(
                "/lane/controls",
                params={"work_id": self.work_id, "after_sequence": self._cursor},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            self._journal_error(f"control-plane fetch failed: {exc!r}")
            return
        except ValueError as exc:
            self._journal_error(f"control-plane fetch returned unparseable JSON: {exc}")
            return
        self._merge_payload(payload)

    def _sync_fetch(self) -> None:
        """One direct fetch — the no-event-loop fallback (sync callers).

        The drain normally reads the async loop's buffer; without the async
        context (a sync test, an out-of-band reconciliation tool) the same
        GET runs inline, bounded by the same ack timeout.
        """
        try:
            response = httpx.get(
                f"{self.base_url}/lane/controls",
                params={"work_id": self.work_id, "after_sequence": self._cursor},
                headers=self._auth_headers(),
                timeout=self.ack_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            self._journal_error(f"control-plane fetch failed: {exc!r}")
            return
        except ValueError as exc:
            self._journal_error(f"control-plane fetch returned unparseable JSON: {exc}")
            return
        self._merge_payload(payload)

    def _merge_payload(self, payload: Any) -> None:
        """Fold one PROVEN fetch response into the delivery buffer.

        Malformed entries journal an error row and are skipped — one bad
        command never drops its well-formed neighbors.
        """
        fetched = payload.get("commands") if isinstance(payload, dict) else None
        if not isinstance(fetched, list):
            self._journal_error("control-plane fetch returned no commands list")
            return
        known = {command.command_id for command in self._buffer}
        for raw in fetched:
            if not isinstance(raw, dict):
                continue
            try:
                command = ControlCommand.model_validate(raw)
            except ValueError as exc:
                self._journal_error(f"control-plane command failed validation: {exc}")
                continue
            if command.work_id != self.work_id:
                # Defensive work-scoping: the fetch asked for THIS work; a
                # command naming another work is refused at the merge, never
                # delivered to this lane's drain.
                self._journal_error(
                    f"control-plane returned command {command.command_id!r} of work "
                    f"{command.work_id!r} for a {self.work_id!r} fetch — skipped"
                )
                continue
            if command.command_id not in known:
                self._buffer.append(command)
                known.add(command.command_id)
        self._buffer.sort(key=lambda command: command.sequence)

    # -- the sync MailboxSurface (the SAME drain path) ------------------------

    def pending(self, work_id: str) -> list[ControlCommand]:
        """The channel's undelivered commands for *work_id*, sequence order.

        Returns the buffer and advances the cursor past everything handed
        over — exactly-once delivery per channel lifetime. *work_id* is
        honored: a command for another work (a mis-scoped dispatch) is
        never handed to this lane's drain. Without the async context's
        fetch loop running, ONE direct fetch runs inline first (the
        no-event-loop fallback).
        """
        if work_id != self.work_id:
            return []
        if self._fetch_task is None and not self._stopped:
            self._sync_fetch()
        delivered = self._buffer
        self._buffer = []
        if delivered:
            self._cursor = max(command.sequence for command in delivered)
        return list(delivered)

    def authorize(
        self, command_id: str, actor_scopes: dict[str, tuple[str, ...]]
    ) -> ControlCommand:
        """Ack ``authorized`` — book the control plane's acceptance."""
        return self._ack(command_id, "authorized")

    def apply(
        self,
        command_id: str,
        *,
        current_plan_revision: int,
        current_execution_epoch: int,
    ) -> ControlCommand:
        """Ack ``dispatching`` under the lane's CURRENT world (CTL-04 CAS).

        The server's guarded dispatch expires a command whose recorded
        expectations no longer hold — the returned status says which, so
        the gate refuses a stale-world command BEFORE any vendor effect.
        """
        return self._ack(
            command_id,
            "dispatching",
            plan_revision=current_plan_revision,
            execution_epoch=current_execution_epoch,
            vendor_correlation_id=self.vendor_session_id,
        )

    def checkpoint(self, command_id: str) -> ControlCommand:
        """Ack ``checkpointed`` — the application observed, the climb booked."""
        return self._ack(command_id, "checkpointed")

    def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]:
        """The lane never submits — commands enter at the control plane.

        The surface name exists because the service's async seam wraps the
        whole protocol; a lane-side submit would be a capability the
        outbound leg does not have (the token READS and acks, it never
        creates commands).
        """
        raise PermissionError(
            "the lane control channel is read/ack only — commands enter "
            "through the control plane's ingress, never from the lane"
        )

    def bind_vendor_session(self, vendor_session_id: str) -> None:
        """Record the vendor session id (rides dispatch acks as the
        correlation a recovery pass probes)."""
        if vendor_session_id:
            self.vendor_session_id = vendor_session_id

    # -- evidence --------------------------------------------------------------

    @property
    def channel_journal(self) -> list[dict[str, Any]]:
        """The channel's append-only error/evidence rows (a copy)."""
        return list(self._channel_journal)

    def _journal_error(self, message: str) -> None:
        self._channel_journal.append(
            {"type": "lane_control_error", "at": _now_iso(), "error": message[:400]}
        )
        if len(self._channel_journal) > _JOURNAL_CAP:
            del self._channel_journal[:-_JOURNAL_CAP]

    # -- the ack leg -------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _ack_row(self, state: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "source": "lane_channel",
            "state": state,
            "at": _now_iso(),
        }
        if self.generation is not None:
            row["generation"] = self.generation
        if self.vendor_session_id:
            row["vendor_session_id"] = self.vendor_session_id
        if self.run_id:
            row["run_id"] = self.run_id
        return row

    def _ack(self, command_id: str, state: str, **extra: Any) -> ControlCommand:
        """POST one guarded transition; return the command it produced.

        Refusals surface as the SAME exception types the in-memory mailbox
        raises (``ValueError`` for a refused rung, ``PermissionError`` for
        an authorization no) so the drain's gate handles them identically.
        An unreachable API journals the error and raises ``ValueError`` —
        the command is refused THIS cycle and stays pending server-side
        for the reconciler, never optimistically booked.

        R28-10: when the channel knows its runner ``generation`` the ack
        body DECLARES it — the control plane refuses a superseded
        generation's ack with 403 (surfaced here as ``PermissionError``),
        so a retired lane can never move the new attempt's state.
        """
        url = f"{self.base_url}/lane/controls/{_urlquote(command_id, safe='')}/ack"
        body: dict[str, Any] = {
            "state": state,
            "journal_row": self._ack_row(state),
            **extra,
        }
        if self.generation is not None:
            body["generation"] = self.generation
        try:
            response = httpx.post(
                url,
                json=body,
                headers=self._auth_headers(),
                timeout=self.ack_timeout,
            )
        except httpx.HTTPError as exc:
            self._journal_error(f"control-plane ack ({state}) failed: {exc!r}")
            raise ValueError(
                f"lane control API unreachable — the command stays in the "
                f"mailbox for the reconciler ({exc})"
            ) from exc
        if response.status_code in (401, 403):
            self._journal_error(f"control-plane ack ({state}) refused auth: {response.text[:200]}")
            raise PermissionError(
                f"lane control API refused the ack token ({response.status_code})"
            )
        if response.status_code == 404:
            raise KeyError(f"unknown command_id {command_id!r} at the control plane")
        if response.status_code >= 400:
            detail = response.text[:300]
            self._journal_error(f"control-plane ack ({state}) refused: {detail}")
            raise ValueError(f"lane control ack refused ({response.status_code}): {detail}")
        try:
            command = ControlCommand.model_validate(response.json().get("command", {}))
        except ValueError as exc:
            self._journal_error(f"control-plane ack ({state}) returned an invalid command: {exc}")
            raise ValueError(f"lane control ack returned an invalid command: {exc}") from exc
        return command


def lane_channel_from_env(
    env: Mapping[str, str] | None = None,
) -> LaneControlChannel | None:
    """The channel the dispatch env configured, or ``None``.

    ``None`` (the lane-local mailbox stays) unless BOTH
    ``FORGE_LANE_CONTROL_URL`` and ``FORGE_LANE_CONTROL_TOKEN`` are set
    AND a work id can be derived (``FORGE_WORK_ID``, else ``FORGE_RUN_ID``
    — the same derivation the steering attach uses; nothing to scope to,
    nothing to dial). A malformed ``FORGE_LANE_CONTROL_POLL_SECONDS``
    fails CLOSED to the default cadence; a malformed
    ``FORGE_LANE_CONTROL_GENERATION`` fails OPEN to "no generation
    declared" (the migration default — a typo must not brick the lane's
    acks against a pre-generation control plane).
    """
    source = os.environ if env is None else env
    base_url = (source.get(LANE_CONTROL_URL_ENV) or "").strip()
    token = (source.get(LANE_CONTROL_TOKEN_ENV) or "").strip()
    if not base_url or not token:
        return None
    work_id = (source.get("FORGE_WORK_ID") or source.get("FORGE_RUN_ID") or "").strip()
    if not work_id:
        return None
    poll_interval = DEFAULT_POLL_INTERVAL_S
    raw_poll = (source.get(LANE_CONTROL_POLL_ENV) or "").strip()
    if raw_poll:
        try:
            poll_interval = float(raw_poll)
        except ValueError:
            poll_interval = DEFAULT_POLL_INTERVAL_S
        if poll_interval <= 0:
            poll_interval = DEFAULT_POLL_INTERVAL_S
    generation = None
    raw_generation = (source.get(LANE_CONTROL_GENERATION_ENV) or "").strip()
    if raw_generation:
        try:
            generation = int(raw_generation)
        except ValueError:
            generation = None
        if generation is not None and generation < 0:
            generation = None
    return LaneControlChannel(
        base_url=base_url,
        token=token,
        work_id=work_id,
        run_id=(source.get("FORGE_RUN_ID") or "").strip(),
        poll_interval=poll_interval,
        generation=generation,
    )
