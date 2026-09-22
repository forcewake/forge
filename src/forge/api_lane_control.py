"""The lane control API — the outbound leg the CI lane dials (NXT-10).

EXE-04 doctrine: the lane job runs inside an ephemeral CI runner with NO
inbound listener, so the control plane can never reach INTO it. The lane
dials OUT instead: this module is the control-plane surface it polls —
the ``control_commands`` rows the ingress already lands durably
(GitHub/GitLab/AzDO → :class:`~forge.adaptive.command_router.
ControlCommandRouter` → :class:`~forge.adaptive.mailbox_db.PostgresMailbox`
behind ``FORGE_CONTROL_MAILBOX=postgres``) become reachable from the lane
that must feel them.

Two routes, both mounted unconditionally and FAIL-CLOSED (the
``azure_webhook`` posture — an unauthenticated endpoint is never exposed):

- ``GET /lane/controls?work_id=<run-id>&after_sequence=<n>`` — the work's
  PENDING commands (``received``/``authorized`` ONLY, durable sequence
  order — :meth:`PostgresMailbox.pending`'s contract: once a command is
  ``dispatching`` it has left the queue and its fate is read from the row,
  never re-delivered). ``after_sequence`` is the lane's cursor: only
  commands with ``sequence > n`` return, so a channel that has already
  delivered through *n* fetches only what is new.
- ``POST /lane/controls/<command_id>/ack`` — the drain ACK surface. The
  body names a ladder *state* plus an optional lane ``journal_row``; the
  transition itself is performed by the PostgresMailbox's own guarded
  compare-and-sets (never raw SQL on the status), so a skipped rung, a
  stale-world dispatch CAS, or a concurrent mover is REFUSED exactly as
  the in-process caller would be. ``checkpointed`` is the one composite:
  it climbs ``dispatching -> vendor_accepted -> applied -> checkpointed``
  because the lane's ack IS the application observation (NXT-12 — the
  lane drove the vendor effect and saw it land). The lane's journal row
  is APPENDED to the row's audit journal additively — evidence, never a
  status claim.

Auth is a lane token: ``HMAC-SHA256(secret, work_id)`` under the
server-side ``FORGE_LANE_CONTROL_SECRET``, injected into the lane job
env by the dispatch (``FORGE_LANE_CONTROL_TOKEN``). The token is
WORK-SCOPED by construction — a lane holding run A's token can neither
poll run B's queue nor ack B's commands (403, not 401: the bearer
proved possession of SOME valid token, just not this work's). No
secret configured → BOTH routes answer 503 disabled, never
unauthenticated-open.

R28-07 — attempt-scoped generations: the token gains a generation
component, ``HMAC-SHA256(secret, work_id + ":" + generation)``, minted
by the dispatch at dispatch time for the run's CURRENT generation (the
durable ``FlowRun.cancellation_generation``, bumped whenever the run's
execution authority is superseded). The server validates against the
run's current generation: a token from a SUPERSEDED generation is
refused with 403 and an actionable message, so a retired lane can no
longer poll or ack for the resumed attempt. The legacy work-id-only
HMAC keeps validating while no generation-scoped dispatch exists — the
honest migration default (a token that never carried a generation is
not treated as stale).

R28-10 — replay-safe acks: the ``checkpointed`` composite is
IDEMPOTENT — a replayed ack against an already-checkpointed command
answers 200 with the current state (a redelivered ack spends nothing),
and every ack may declare its ``generation``; an ack from a superseded
generation is refused with 403 (the lane can still READ its queue —
it just cannot move anyone's state).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand

__all__ = [
    "LANE_ACK_STATES",
    "lane_control_router",
    "lane_control_token",
    "verify_lane_token",
]

logger = logging.getLogger(__name__)

lane_control_router = APIRouter()

#: The states a lane may ack, mapped onto the PostgresMailbox's own guarded
#: transitions. Every spelling is a ladder rung (or the checkpointed
#: composite); ``received`` is deliberately absent — the lane never books
#: entry, the ingress does.
LANE_ACK_STATES: frozenset[str] = frozenset(
    {
        "authorized",
        "dispatching",
        "vendor_accepted",
        "outcome_unknown",
        "applied",
        "checkpointed",
    }
)


def lane_control_token(secret: str, work_id: str, *, generation: int | None = None) -> str:
    """The lane token for *work_id*: hex ``HMAC-SHA256`` under *secret*.

    The dispatch-side derivation — the control plane computes this under
    ``FORGE_LANE_CONTROL_SECRET`` and injects the value into the lane job
    env. Work-scoping is by construction: the token for run A does not
    match run B's expected bytes, so the compare in
    :func:`verify_lane_token` fails without ever revealing which half
    differed.

    R28-07: with *generation* the token is ATTEMPT-SCOPED —
    ``HMAC(secret, work_id + ":" + generation)`` — minted at dispatch
    time for the run's current retry/generation number, so a lane whose
    generation was superseded holds bytes nothing accepts anymore.
    ``generation=None`` derives exactly the legacy ``HMAC(secret,
    work_id)`` (the migration default while dispatches still mint
    work-scoped tokens).
    """
    material = work_id if generation is None else f"{work_id}:{generation}"
    return hmac.new(secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_lane_token(
    secret: str, token: str, work_id: str, *, generation: int | None = None
) -> bool:
    """Constant-time check that *token* is THIS work's lane token.

    With *generation* the expected bytes are the attempt-scoped
    derivation; with ``None`` the legacy work-scoped one. Callers that
    want both spellings compared do so explicitly (see
    :func:`_authorize_lane`) — a generation-scoped token never matches
    the legacy expectation and vice versa.
    """
    if not secret or not token or not work_id:
        return False
    return hmac.compare_digest(token, lane_control_token(secret, work_id, generation=generation))


def _superseded_generation(
    secret: str, token: str, work_id: str, current_generation: int | None
) -> int | None:
    """Which PREVIOUS generation a token belongs to, if any.

    The stale-generation oracle behind the actionable 403: when the
    token matches ``HMAC(secret, work_id:g)`` for some ``g`` BELOW the
    run's current generation, the bearer is a retired lane holding a
    once-valid credential — worth naming precisely, not lumping into
    "does not scope this work". Only generations strictly below the
    current one are stale; the scan is bounded by the current
    generation (a small, monotonically managed counter on the run).
    """
    if current_generation is None:
        return None
    for generation in range(int(current_generation)):
        if hmac.compare_digest(token, lane_control_token(secret, work_id, generation=generation)):
            return generation
    return None


async def _current_generation(request: Request, work_id: str) -> int | None:
    """The work's CURRENT runner generation, durably (R28-07).

    The authority is ``FlowRun.cancellation_generation`` — the durable
    per-run generation bumped whenever the run's execution authority is
    superseded (a cancel today; retries as the dispatch grows
    generation-minting). ``None`` means "no durable generation" — an
    unknown work or an unreadable row — and the authorize ladder then
    treats the work as pre-migration: the legacy token scheme is the
    honest default, never a hard failure.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        return None
    from sqlalchemy import select

    try:
        from forge.durable.models import FlowRun

        async with session_factory() as session:
            generation = await session.scalar(
                select(FlowRun.cancellation_generation).where(FlowRun.id == work_id)
            )
    except Exception:  # noqa: BLE001 — an unreadable generation degrades to legacy
        logger.warning("current-generation lookup for work %s failed", work_id, exc_info=True)
        return None
    return int(generation) if generation is not None else None


def _secret(request: Request) -> str:
    setting = getattr(request.app.state.settings, "FORGE_LANE_CONTROL_SECRET", None)
    return setting.get_secret_value() if setting is not None else ""


def _bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


async def _authorize_lane(request: Request, authorization: str | None, work_id: str) -> str:
    """The shared gate: secret configured, bearer present, token scoped.

    Accepts the CURRENT generation's attempt-scoped token (R28-07) or
    the legacy work-scoped one (the migration default — a token that
    never carried a generation cannot be treated as stale). A token
    from a SUPERSEDED generation is refused with an actionable 403 that
    names the stale generation and the current one; anything else is
    plain work-scoping refusal.

    Returns the verified secret (the caller never needs it beyond the
    check — returning it keeps the 503/401/403 ladder in ONE place).
    Raises :class:`HTTPException` on every refusal.
    """
    secret = _secret(request)
    if not secret:
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing lane bearer token")
    current_generation = await _current_generation(request, work_id)
    if verify_lane_token(secret, token, work_id, generation=current_generation):
        return secret
    if verify_lane_token(secret, token, work_id):
        return secret  # the legacy migration default, never "stale"
    stale = _superseded_generation(secret, token, work_id, current_generation)
    if stale is not None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"this credential belongs to a superseded runner generation ({stale}; "
                f"the work is at generation {current_generation}) — the current "
                "attempt must dial in with its own dispatch-issued token"
            ),
        )
    # 403, not 401: the bearer holds A token, just not this work's —
    # work-scoping refused, not authentication retried.
    raise HTTPException(status_code=403, detail="lane token does not scope this work")


def _mailbox(request: Request) -> PostgresMailbox:
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    return PostgresMailbox(session_factory)


class LaneAckBody(BaseModel):
    """One drain acknowledgement: the target ladder state + lane evidence."""

    state: str = Field(min_length=1)
    journal_row: dict[str, Any] | None = None
    #: The lane's CURRENT world — required for ``dispatching`` (the CTL-04
    #: CAS: a command written against another plan revision / execution
    #: epoch expires instead of dispatching).
    plan_revision: int | None = Field(default=None, ge=0)
    execution_epoch: int | None = Field(default=None, ge=0)
    vendor_correlation_id: str = ""
    #: The runner generation this ack speaks for (R28-10). Optional and
    #: omitted by pre-generation lanes (the migration default); when the
    #: work's durable generation is known and this declares a SUPERSEDED
    #: one, the ack is refused — the retired lane can still READ its
    #: queue, but it cannot transition anyone's state.
    generation: int | None = Field(default=None, ge=0)

    model_config = {"extra": "forbid"}


async def _append_lane_journal(
    request: Request, command_id: str, state: str, journal_row: dict[str, Any] | None
) -> bool:
    """Append the lane's evidence row to ``control_commands.journal``.

    Purely additive — the status and the mailbox's own rung journal are
    never touched; this is the lane-side audit trail (what the channel
    observed when it acked), distinct from the transition journal the
    guarded CAS writes. Failures are logged, never fatal: the transition
    already committed, and a lost evidence append must not turn a
    successful ack into a 5xx the lane would retry.
    """
    if journal_row is None:
        return False
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        return False
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "lane_ack": state,
        "row": journal_row,
    }
    from sqlalchemy import select, update

    try:
        async with session_factory() as session:
            journal = await session.scalar(
                select(ControlCommandRow.journal).where(ControlCommandRow.id == command_id)
            )
            if journal is None:
                return False
            appended = list(journal or [])
            appended.append(entry)
            await session.execute(
                update(ControlCommandRow)
                .where(ControlCommandRow.id == command_id)
                .values(journal=appended)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — evidence append is best-effort by contract
        logger.warning("lane journal append for %s failed", command_id, exc_info=True)
        return False
    return True


@lane_control_router.get("/lane/controls")
async def get_lane_controls(
    request: Request,
    work_id: str = Query(min_length=1),
    after_sequence: int = Query(default=0, ge=0),
    authorization: str | None = Header(None),
) -> Any:
    """The work's pending control commands, after the lane's cursor.

    Pending means ``received``/``authorized`` only, in durable sequence
    order — the same view an in-process drain consumes, so the remote lane
    applies commands through the SAME discipline (a command that left the
    queue is never re-delivered; its fate is read from the row).
    """
    await _authorize_lane(request, authorization, work_id)
    mailbox = _mailbox(request)
    commands = [
        command for command in await mailbox.pending(work_id) if command.sequence > after_sequence
    ]
    return {
        "work_id": work_id,
        "after_sequence": after_sequence,
        "commands": [command.model_dump(mode="json") for command in commands],
    }


@lane_control_router.post("/lane/controls/{command_id}/ack")
async def ack_lane_control(
    request: Request,
    command_id: str,
    body: LaneAckBody,
    authorization: str | None = Header(None),
) -> Any:
    """Transition one command through the mailbox's guarded ladder.

    The lane names the rung it observed; the PostgresMailbox performs the
    compare-and-set (a skipped rung or a concurrent mover is refused with
    409 — the caller reloads and retries, never writes on top of a state
    it did not see). ``checkpointed`` climbs the observation rungs because
    the ack itself is the application evidence; an already-checkpointed
    command is an idempotent no-op — the replayed ack answers 200 with
    the current state and spends nothing (R28-10: a lost response must
    never make the lane re-drive the vendor effect). An ack that declares
    a ``generation`` superseded by the work's durable current generation
    is refused with 403 BEFORE any transition — the retired lane can
    still read, it cannot move state.
    """
    if body.state not in LANE_ACK_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state must be one of {sorted(LANE_ACK_STATES)}, got {body.state!r}",
        )
    if body.state == "dispatching" and (body.plan_revision is None or body.execution_epoch is None):
        # A request-shape refusal, not a ladder refusal: the CAS world is
        # REQUIRED input for a dispatch — guessing one would apply commands
        # against a world the lane never held.
        raise HTTPException(
            status_code=422,
            detail="dispatching requires the lane's current plan_revision and "
            "execution_epoch (the CTL-04 CAS world)",
        )
    # The fail-closed ladder, in the order that leaks nothing an
    # unconfigured/unauthenticated caller could use: 503 before anything
    # (no secret → the surface does not exist), 401 before any database
    # read (no bearer → no information), then the guarded transition.
    if not _secret(request):
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    if not _bearer(authorization):
        raise HTTPException(status_code=401, detail="missing lane bearer token")
    mailbox = _mailbox(request)
    current = await mailbox.get(command_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"unknown command_id {command_id!r}")
    # The token must scope THIS command's work (the path cannot cross
    # works even with a valid token for another run).
    await _authorize_lane(request, authorization, current.work_id)
    if body.generation is not None:
        # R28-10: an ack from a superseded generation never transitions
        # state. Only a KNOWN durable generation can judge staleness —
        # without one (unknown work, pre-migration row) the ack's
        # generation is recorded trust-free and the ladder proceeds.
        current_generation = await _current_generation(request, current.work_id)
        if current_generation is not None and body.generation != current_generation:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"ack from a superseded runner generation ({body.generation}; the "
                    f"work is at generation {current_generation}) — the command is "
                    "unchanged; a retired lane may read, it cannot transition state"
                ),
            )

    try:
        updated = await _transition(mailbox, current, body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"guarded transition refused: {exc}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=f"authorization refused: {exc}") from exc

    journal_appended = await _append_lane_journal(request, command_id, body.state, body.journal_row)
    return {
        "command_id": command_id,
        "state": updated.status,
        "command": updated.model_dump(mode="json"),
        "journal_appended": journal_appended,
    }


async def _transition(mailbox: PostgresMailbox, current: ControlCommand, body: LaneAckBody):
    """One ack state → the mailbox's own guarded transition(s)."""
    state = body.state
    if state == "authorized":
        # The lane books the control plane's acceptance, exactly as the
        # in-process drain does: "this actor, as recorded".
        return await mailbox.authorize(
            current.command_id, {current.actor_origin: (current.actor_ref,)}
        )
    if state == "dispatching":
        if body.plan_revision is None or body.execution_epoch is None:
            # Unreachable via HTTP (the 422 shape gate precedes); the guard
            # narrows the type for the CAS call — never a guessed world.
            raise ValueError("dispatching requires the lane's CAS world")
        return await mailbox.dispatch(
            current.command_id,
            current_plan_revision=body.plan_revision,
            current_execution_epoch=body.execution_epoch,
            vendor_correlation_id=body.vendor_correlation_id or "",
        )
    if state == "vendor_accepted":
        return await mailbox.vendor_accepted(current.command_id)
    if state == "outcome_unknown":
        return await mailbox.outcome_unknown(current.command_id)
    if state == "applied":
        return await mailbox.observe(current.command_id)
    # checkpointed — the composite climb: the lane's ack IS the application
    # observation (it drove the vendor effect and saw it land). The climb
    # is IDEMPOTENT by construction (R28-10): a command already sitting at
    # ``checkpointed`` matches no climb step and returns as-is — the
    # replayed ack answers 200 with the current state, appends only its
    # evidence row, and drives no vendor effect twice.
    command = current
    if command.status == "dispatching":
        command = await mailbox.vendor_accepted(command.command_id)
    if command.status in ("vendor_accepted", "outcome_unknown"):
        command = await mailbox.observe(command.command_id)
    if command.status == "applied":
        command = await mailbox.checkpoint(command.command_id)
    if command.status != "checkpointed":
        raise ValueError(
            f"command {command.command_id!r} is {command.status!r}; the "
            "checkpointed climb starts only from dispatching, "
            "vendor_accepted, outcome_unknown, applied or checkpointed"
        )
    return command
