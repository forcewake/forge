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
- ``GET /lane/controls/resume-spec?work_id=<run-id>`` — the work's LATEST
  ``resume`` command ROW, whatever rung it sits on (NEXT-03). The pending
  view above deliberately stops at the dispatch boundary; the resume
  decision must survive its own acknowledgement, so this surface reads
  the durable row (payload and all) instead of the pending queue — the
  immutable ResumeSpec a resumed runner restores by, still reachable
  after the command has left ``received``/``authorized``.
- ``GET /lane/credentials/redeem`` — the runner-time MODEL-credential
  redemption (R38-02/#303, delivery profile b): the lane exchanges its
  EXISTING attempt-scoped token for the bound model credential at
  startup, TTL-bound to the attempt, audited durably before the value
  leaves. The registry's fail-closed checks re-run against the WORK's
  own canonical subject; a revoked binding, a rotated-away ref, a
  foreign ref or a superseded generation each refuse with 403 and zero
  successful retrievals. Since Q39-01 (#320) the request is authorized
  against the attempt's persisted **operation grant** — the exact ref +
  route THIS attempt's dispatch authorized, an absolute redemption
  deadline fixed at dispatch (never ``now + TTL`` per request), and a
  terminal attempt or a sibling binding of the same project refuse
  typed with ZERO broker calls; authority is re-validated across the
  awaited broker resolution (a cancel/rotation during the await never
  emits under retired authority).

Auth is a lane token: ``HMAC-SHA256(secret, work_id)`` under the
server-side ``FORGE_LANE_CONTROL_SECRET``, injected into the lane job
env by the dispatch (``FORGE_LANE_CONTROL_TOKEN``). The token is
WORK-SCOPED by construction — a lane holding run A's token can neither
poll run B's queue nor ack B's commands (403, not 401: the bearer
proved possession of SOME valid token, just not this work's). No
secret configured → BOTH routes answer 503 disabled, never
unauthenticated-open.

R28-07/NEXT-01 — attempt-scoped generations, ONE credential: the token
gains a generation component, ``HMAC-SHA256(secret, work_id + ":" +
generation)``, minted by the DISPATCH at dispatch time for the run's
CURRENT generation (the durable ``FlowRun.cancellation_generation``).
The server validates against the run's current generation: a token from
a SUPERSEDED generation is refused with 403 and an actionable message,
so a retired lane can no longer poll or ack for the resumed attempt.
The legacy work-id-only HMAC is accepted ONLY inside an explicit
migration window (``FORGE_LEGACY_CREDENTIAL_DEADLINE`` — spelling
``FORGE_LANE_LEGACY_TOKEN_DEADLINE`` —, or the recorded migration
START plus 30 days — ``FORGE_LANE_LEGACY_TOKEN_START``; with neither
recorded, the FIRST resolution persists a WRITE-ONCE anchor file
``lane-legacy-credential-anchor`` under the checkpoint store dir —
``FORGE_LEGACY_CREDENTIAL_ANCHOR_FILE`` overrides the location — and
the deadline is that persisted anchor + 30 days, so a restart without
recorded env still derives the SAME window instead of opening a fresh
one; when no anchor can be read or persisted, legacy acceptance is
REFUSED, fail-closed): old attempts finish, new dispatches must be
generation-scoped — and an UNAVAILABLE generation authority (a failed
lookup) is a 503 refusal, never a silent legacy acceptance.

NEXT-01's generation policy — which transitions open a NEW attempt
generation (and therefore retire the previous dispatch's token), and
which keep the current identity:

- retry / re-dispatch after a terminal or stalled attempt (GitHub
  ``/retry``, the revival recovery scan): ``+1`` — a new lane boots with
  its own token and any delayed callback of the previous attempt is
  fenced;
- resume after pause when the resume DISPATCHES a new attempt: ``+1``
  (aligned with the pause fence's resumed publication epoch — see
  :mod:`forge.adaptive.pause_fence`);
- steering, checkpoint capture/upload, ack ladder transitions: SAME
  identity — they address the live attempt, never mint a new one.

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
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand

__all__ = [
    "LANE_ACK_STATES",
    "LANE_CREDENTIAL_REDEEM_ROUTE",
    "LANE_LEGACY_TOKEN_DEADLINE_ENV",
    "LANE_LEGACY_TOKEN_START_ENV",
    "LEGACY_CREDENTIAL_ANCHOR_FILE_ENV",
    "LEGACY_CREDENTIAL_DEADLINE_ENV",
    "REDEMPTION_TTL_ENV",
    "DEFAULT_REDEMPTION_TTL_SECONDS",
    "LaneAuthorityUnavailable",
    "LegacyWindow",
    "LegacyWindowInvalid",
    "authorize_work_credential",
    "durable_run_generation",
    "lane_control_router",
    "lane_control_token",
    "legacy_token_deadline",
    "legacy_token_start",
    "persist_operation_grant",
    "redemption_ttl_seconds",
    "resolve_legacy_window",
    "verify_lane_token",
]

logger = logging.getLogger(__name__)

lane_control_router = APIRouter()

#: NEXT-01/Q35-06: the migration deadline for LEGACY (work-scoped,
#: generation-less) lane tokens. Until this instant a token that never
#: carried a generation still authenticates — old attempts finish inside
#: the window — and after it every credential must be the
#: dispatch-issued, attempt-scoped one. An explicit ISO date/datetime
#: (naive values read as UTC) closes the window on the operator's exact
#: schedule; the value is operator STATE, so it survives restarts
#: unchanged (R32-03).
LANE_LEGACY_TOKEN_DEADLINE_ENV: Final = "FORGE_LANE_LEGACY_TOKEN_DEADLINE"
#: Q35-06: the CANONICAL explicit-deadline spelling. Functionally the
#: same variable as ``FORGE_LANE_LEGACY_TOKEN_DEADLINE`` (either
#: satisfies the explicit branch; the canonical name wins when both are
#: set) — kept as a separate name so the credential-window family reads
#: uniformly in new deployments.
LEGACY_CREDENTIAL_DEADLINE_ENV: Final = "FORGE_LEGACY_CREDENTIAL_DEADLINE"
#: R32-03: the RECORDED migration START — the instant the legacy-token
#: compatibility window OPENED (the deployment that began minting
#: generation-scoped tokens). The deadline is ``start + 30 days``, a
#: FIXED instant derived from the recorded value, never from the request
#: clock: record it once (deployment env) and process restarts cannot
#: extend the window. Naive values read as UTC.
LANE_LEGACY_TOKEN_START_ENV: Final = "FORGE_LANE_LEGACY_TOKEN_START"
#: Q35-06: where the WRITE-ONCE anchor file lives when neither an
#: explicit deadline nor a recorded start exists. The default sits under
#: the checkpoint store dir (``data/checkpoints``) — durable,
#: already-backed-up deployment state — so the default window survives
#: restarts exactly like the recorded spellings do.
LEGACY_CREDENTIAL_ANCHOR_FILE_ENV: Final = "FORGE_LEGACY_CREDENTIAL_ANCHOR_FILE"
#: The anchor file's name under the checkpoint store dir. Mirrored
#: locally (not imported) because :mod:`forge.api_checkpoint_channel`
#: imports THIS module — a cycle either way is refused.
CHECKPOINT_STORE_DIR_ENV: Final = "FORGE_CHECKPOINT_STORE_DIR"
DEFAULT_CHECKPOINT_STORE_DIR: Final = "data/checkpoints"
LEGACY_CREDENTIAL_ANCHOR_FILENAME: Final = "lane-legacy-credential-anchor"
DEFAULT_LEGACY_TOKEN_DEADLINE_DAYS: Final = 30
#: Sanity bounds for EXPLICIT operator configuration (Q35-06): a value a
#: deployment could never have meant is refused as invalid, never
#: silently re-anchored. Anything inside the bounds — including a
#: deadline already in the past — is a VALID, honored configuration.
LEGACY_WINDOW_EARLIEST: Final[datetime] = datetime(2019, 1, 1, tzinfo=timezone.utc)
LEGACY_WINDOW_LATEST: Final[datetime] = datetime(2101, 1, 1, tzinfo=timezone.utc)
#: The deadline a REFUSED window reports through the legacy
#: ``legacy_token_deadline`` spelling: the beginning of time, i.e. the
#: window is closed forever (fail-closed), never re-anchored.
LEGACY_WINDOW_REFUSED_DEADLINE: Final[datetime] = datetime.min.replace(tzinfo=timezone.utc)

#: R32-03/Q35-06: the migration start captured ONCE, at module import
#: (process start). Since Q35-06 this instant is ONLY the SEED written
#: into the write-once anchor file on a first resolution — the window
#: itself is anchored at the FILE's value forever after, so a restart
#: without operator state cannot open a fresh 30-day window.
_PROCESS_MIGRATION_START: Final[datetime] = datetime.now(timezone.utc)


class LegacyWindowInvalid(ValueError):
    """EXPLICIT legacy-window configuration is malformed (Q35-06).

    Raised by :func:`resolve_legacy_window` when a deadline/start
    variable is SET but is not a readable ISO instant (including the
    empty string) or lies outside the sanity bounds: the value is
    operator state, so a typo must fail loudly — at composition, at
    ``forge doctor`` (``credential.configuration_invalid``), and as a
    specific refusal on the wire — never a silent re-anchor onto a
    fresh process window.
    """


@dataclass(frozen=True)
class LegacyWindow:
    """The resolved legacy-credential migration window (Q35-06).

    ``source`` names the anchor the deadline was derived from —
    ``"explicit"`` (operator deadline), ``"recorded-start"`` (operator
    start + window), ``"persisted-file"`` (the write-once anchor file)
    or ``"refused"`` (nothing restart-stable could be configured or
    persisted: legacy acceptance is refused, fail-closed, and
    ``diagnostic`` says why). ``anchor`` is the instant the window
    opened (for the explicit deadline it is reported as
    ``deadline - window``), ``anchor_file`` the persisted anchor's path
    when one is in play. No field ever carries a credential value.
    """

    source: str
    deadline: datetime | None
    anchor: datetime | None = None
    anchor_file: Path | None = None
    diagnostic: str = ""

    @property
    def refused(self) -> bool:
        """True when legacy acceptance is refused outright (fail-closed)."""
        return self.deadline is None

    def days_remaining(self, now: datetime) -> int:
        """Whole days from *now* to the deadline; negative once closed."""
        if self.deadline is None:
            return 0
        return (self.deadline - now).days

    def open_at(self, now: datetime) -> bool:
        """Whether *now* is still inside the window (the boundary itself
        refuses — the comparison is strict, as ever)."""
        return self.deadline is not None and now < self.deadline


def _configured_instant(raw: str, *, env: str) -> datetime:
    """Parse one EXPLICIT operator value or raise :class:`LegacyWindowInvalid`.

    The old silent-degrade (log a warning, fall back to the default
    window) re-anchored the migration onto process state whenever an
    operator typo'd the env — Q35-06 removes exactly that: set-but-empty
    and unparseable values are typed failures carrying the variable
    name and the offending value, and so is anything outside the sanity
    bounds. Naive values read as UTC, as ever.
    """
    value = raw.strip()
    if not value:
        raise LegacyWindowInvalid(
            f"{env} is set but empty — unset it or record an ISO date/datetime "
            f"(a restart-stable deadline; do not leave a blank value in place)"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LegacyWindowInvalid(
            f"{env}={value!r} is not an ISO date/datetime — fix the value; the "
            "legacy window is never silently re-anchored on malformed configuration"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if not LEGACY_WINDOW_EARLIEST <= parsed <= LEGACY_WINDOW_LATEST:
        raise LegacyWindowInvalid(
            f"{env}={value!r} is outside the sanity bounds "
            f"[{LEGACY_WINDOW_EARLIEST.date().isoformat()}, "
            f"{LEGACY_WINDOW_LATEST.date().isoformat()}] — a deadline no "
            "deployment could have meant; fix the value"
        )
    return parsed


def _parse_stored_instant(raw: str) -> datetime | None:
    """Parse the persisted anchor payload; None when unreadable.

    The anchor file is machine-written state, so a malformed payload is
    reported (the caller refuses the window) instead of raised — but it
    is NEVER rewritten: overwriting corrupt state with a fresh anchor
    is precisely the restart hole Q35-06 closes.
    """
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _anchor_file_path(source: Mapping[str, str]) -> Path:
    """Where the write-once anchor lives for THIS environment."""
    configured = str(source.get(LEGACY_CREDENTIAL_ANCHOR_FILE_ENV, "")).strip()
    if configured:
        return Path(configured)
    root = str(source.get(CHECKPOINT_STORE_DIR_ENV, "")).strip() or DEFAULT_CHECKPOINT_STORE_DIR
    return Path(root) / LEGACY_CREDENTIAL_ANCHOR_FILENAME


def _read_anchor_or_none(path: Path) -> datetime | None:
    """The persisted anchor, or None when the payload cannot be read."""
    anchor = _parse_stored_instant(path.read_text(encoding="utf-8"))
    if anchor is None:
        logger.error(
            "the legacy credential anchor %s holds malformed state — legacy "
            "credentials are refused rather than re-anchored",
            path,
        )
    return anchor


def _malformed_anchor_diagnostic(path: Path) -> str:
    return (
        f"the persisted legacy credential anchor {path} holds malformed "
        "state — legacy credentials are refused rather than re-anchored; "
        f"restore the file from backup or set {LEGACY_CREDENTIAL_DEADLINE_ENV}"
    )


def _write_once_anchor(path: Path) -> tuple[datetime | None, str]:
    """Create the anchor file exactly once; the winner's value is THE anchor.

    ``O_CREAT | O_EXCL`` makes the write atomic against concurrent first
    resolutions (both losing races read the winner's instant). The seed
    is ``_PROCESS_MIGRATION_START`` — the module-import instant of the
    process that happened to resolve first — written ONCE and never
    extended afterwards. An unwritable/uncreatable location is a
    fail-closed REFUSAL with a specific diagnostic, never a fall-back
    to a fresh in-memory window (that fall-back is the restart hole
    Q35-06 closes).
    """
    seed = _PROCESS_MIGRATION_START
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        # A concurrent first resolution won the race: ITS value is THE anchor.
        try:
            anchor = _read_anchor_or_none(path)
        except OSError as exc:
            return None, _unusable_anchor_diagnostic(path, exc)
        return anchor, ("" if anchor is not None else _malformed_anchor_diagnostic(path))
    except OSError as exc:
        return None, _unusable_anchor_diagnostic(path, exc)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(seed.isoformat())
        handle.flush()
        os.fsync(handle.fileno())
    return seed, ""


def _unusable_anchor_diagnostic(path: Path, exc: OSError) -> str:
    return (
        f"no restart-stable legacy credential anchor is available (the anchor "
        f"file {path} could not be read or created: {exc.__class__.__name__}) — "
        f"legacy credentials are refused rather than re-anchored at a new "
        f"process start; set {LEGACY_CREDENTIAL_DEADLINE_ENV} (or "
        f"{LANE_LEGACY_TOKEN_START_ENV}) to an explicit ISO instant, or make "
        "the anchor path writable"
    )


def resolve_legacy_window(env: Mapping[str, str] | None = None) -> LegacyWindow:
    """THE legacy-window resolution (Q35-06) — one ladder, fail-closed.

    Precedence, every surviving branch a FIXED, restart-stable instant:

    1. an EXPLICIT deadline — ``FORGE_LEGACY_CREDENTIAL_DEADLINE`` or
       its ``FORGE_LANE_LEGACY_TOKEN_DEADLINE`` spelling. Operator
       state; a set-but-malformed or out-of-bounds value raises
       :class:`LegacyWindowInvalid` (never a silent re-anchor);
    2. the RECORDED migration start ``FORGE_LANE_LEGACY_TOKEN_START``
       plus the window — restart-stable exactly as in R32-03;
    3. the PERSISTED anchor file (``FORGE_LEGACY_CREDENTIAL_ANCHOR_FILE``,
       by default ``lane-legacy-credential-anchor`` under the checkpoint
       store dir): WRITE-ONCE — a file that exists is THE anchor
       forever, an absent one is created with this process's import
       instant exactly once, so two fresh resolutions (including two
       processes) agree as long as the file persists;
    4. neither writable nor configured → a REFUSED window (fail-closed,
       ``source="refused"`` with a specific diagnostic): legacy
       acceptance stops, generation-scoped tokens are unaffected.

    The result never carries credential material — anchors and
    deadlines only.
    """
    source = os.environ if env is None else env
    for name in (LEGACY_CREDENTIAL_DEADLINE_ENV, LANE_LEGACY_TOKEN_DEADLINE_ENV):
        if name in source:
            deadline = _configured_instant(str(source[name]), env=name)
            return LegacyWindow(
                source="explicit",
                deadline=deadline,
                anchor=deadline - timedelta(days=DEFAULT_LEGACY_TOKEN_DEADLINE_DAYS),
            )
    if LANE_LEGACY_TOKEN_START_ENV in source:
        start = _configured_instant(
            str(source[LANE_LEGACY_TOKEN_START_ENV]), env=LANE_LEGACY_TOKEN_START_ENV
        )
        return LegacyWindow(
            source="recorded-start",
            anchor=start,
            deadline=start + timedelta(days=DEFAULT_LEGACY_TOKEN_DEADLINE_DAYS),
        )
    path = _anchor_file_path(source)
    if path.is_file():
        try:
            anchor = _read_anchor_or_none(path)
        except OSError as exc:
            return LegacyWindow(
                source="refused",
                deadline=None,
                anchor_file=path,
                diagnostic=_unusable_anchor_diagnostic(path, exc),
            )
        if anchor is not None:
            return LegacyWindow(
                source="persisted-file",
                anchor=anchor,
                deadline=anchor + timedelta(days=DEFAULT_LEGACY_TOKEN_DEADLINE_DAYS),
                anchor_file=path,
            )
        return LegacyWindow(
            source="refused",
            deadline=None,
            anchor_file=path,
            diagnostic=_malformed_anchor_diagnostic(path),
        )
    anchor, diagnostic = _write_once_anchor(path)
    if anchor is None:
        return LegacyWindow(
            source="refused", deadline=None, anchor_file=path, diagnostic=diagnostic
        )
    return LegacyWindow(
        source="persisted-file",
        anchor=anchor,
        deadline=anchor + timedelta(days=DEFAULT_LEGACY_TOKEN_DEADLINE_DAYS),
        anchor_file=path,
    )


def legacy_token_start(env: Mapping[str, str] | None = None) -> datetime:
    """The instant the legacy migration window OPENED (R32-03, Q35-06).

    :func:`resolve_legacy_window`'s anchor; a REFUSED window reports the
    beginning of time (there is no anchor to name). Malformed explicit
    configuration raises :class:`LegacyWindowInvalid`.
    """
    window = resolve_legacy_window(env)
    if window.anchor is not None:
        return window.anchor
    return LEGACY_WINDOW_REFUSED_DEADLINE


def legacy_token_deadline(env: Mapping[str, str] | None = None) -> datetime:
    """The instant legacy work-scoped tokens stop authenticating (NEXT-01).

    :func:`resolve_legacy_window`'s deadline; a REFUSED window reports
    the beginning of time — the window is closed forever, fail-closed.
    Malformed explicit configuration raises :class:`LegacyWindowInvalid`
    (Q35-06: never a silent re-anchor).
    """
    window = resolve_legacy_window(env)
    return window.deadline if window.deadline is not None else LEGACY_WINDOW_REFUSED_DEADLINE


def _legacy_window_open(env: Mapping[str, str] | None = None) -> bool:
    """Whether the resolved legacy window (:func:`resolve_legacy_window`)
    is still open. The deadline never moves per check (R32-03) — only
    the comparison clock does."""
    window = resolve_legacy_window(env)
    return window.open_at(datetime.now(timezone.utc))


class LaneAuthorityUnavailable(RuntimeError):
    """The durable attempt-generation authority could not be read (NEXT-01).

    Raised by :func:`durable_run_generation` when the lookup itself fails.
    The authorize ladder maps this to 503: an authority outage is a
    refusal, never a silent downgrade to the legacy token scheme (the
    reviewer's "database outage is not a migration state").
    """


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


async def durable_run_generation(session_factory: Any, work_id: str) -> int | None:
    """The work's CURRENT runner generation from durable authority (R28-07).

    The authority is ``FlowRun.cancellation_generation`` — the durable
    per-run attempt generation, bumped by every transition that opens a
    NEW attempt (see the module docstring's generation policy). Returns
    ``None`` when the work has no durable generation (an unknown work —
    the pre-migration posture), and RAISES
    :class:`LaneAuthorityUnavailable` when the lookup itself fails: a
    storage outage is a refusal, never "legacy must be fine then".
    """
    from sqlalchemy import select

    from forge.durable.models import FlowRun

    try:
        async with session_factory() as session:
            generation = await session.scalar(
                select(FlowRun.cancellation_generation).where(FlowRun.id == work_id)
            )
    except Exception as exc:  # noqa: BLE001 — the outage is a refusal, not legacy
        raise LaneAuthorityUnavailable(
            f"the attempt-generation authority for work {work_id!r} is unavailable"
        ) from exc
    return int(generation) if generation is not None else None


async def authorize_work_credential(
    request: Request,
    *,
    secret: str,
    authorization: str | None,
    work_id: str,
    refusal_status: int = 403,
    require_authority: bool = True,
) -> int | None:
    """THE one attempt-credential check both lane surfaces share (NEXT-01).

    The lane-control polling/ack surface and the checkpoint channel's
    work-scoped upload/download authenticate through this single ladder
    — one derivation (:func:`lane_control_token`), one authority
    (:func:`durable_run_generation`), one migration window
    (:func:`legacy_token_deadline`):

    - the CURRENT generation's attempt-scoped token authenticates and
      the VERIFIED generation is returned (the ack surface reuses it);
    - the legacy work-scoped token authenticates only inside the
      migration window (:func:`resolve_legacy_window`, Q35-06:
      explicit deadline, recorded start, or the persisted write-once
      anchor) — past the deadline it is refused with the deadline
      named, and with NO restart-stable anchor at all it is refused
      fail-closed with the specific diagnostic (401/403 by
      *refusal_status*);
    - a token minted for a generation BELOW the current one is refused
      naming both generations (the retired-lane oracle);
    - anything else is a work-scoping refusal.

    ``require_authority``: with the durable authority configured but
    UNREADABLE, or (on the control surface) absent entirely, the answer
    is 503 — never a legacy acceptance. A surface mounted WITHOUT any
    session factory (the checkpoint channel's standalone deployment
    shape) passes ``False`` and keeps the documented pre-generation
    posture: no authority to consult, legacy inside the window, no
    staleness oracle.
    """
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing lane bearer token")
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None and require_authority:
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    current_generation: int | None = None
    if session_factory is not None:
        try:
            current_generation = await durable_run_generation(session_factory, work_id)
        except LaneAuthorityUnavailable as exc:
            logger.warning("generation lookup for work %s failed", work_id, exc_info=True)
            raise HTTPException(
                status_code=503,
                detail=(
                    "the attempt-generation authority is unavailable — the credential "
                    "is refused rather than degraded to the legacy scheme"
                ),
            ) from exc
    if verify_lane_token(secret, token, work_id, generation=current_generation):
        return current_generation
    if verify_lane_token(secret, token, work_id):
        # Q35-06: one resolution per attempt — the window is anchored at
        # operator state or the write-once anchor file, never at this
        # process's import. Malformed explicit configuration is a typed
        # refusal carrying the diagnostic (fail-closed, no silent
        # re-anchor); a refused window names why.
        try:
            window = resolve_legacy_window()
        except LegacyWindowInvalid as exc:
            raise HTTPException(status_code=refusal_status, detail=str(exc)) from None
        if window.open_at(datetime.now(timezone.utc)):
            return current_generation
        if window.refused:
            raise HTTPException(status_code=refusal_status, detail=window.diagnostic)
        raise HTTPException(
            status_code=refusal_status,
            detail=(
                "the legacy work-scoped lane token is past its migration deadline "
                f"({window.deadline.isoformat() if window.deadline else ''}) — "
                "the current attempt must dial in with its dispatch-issued "
                "generation token"
            ),
        )
    stale = _superseded_generation(secret, token, work_id, current_generation)
    if stale is not None:
        raise HTTPException(
            status_code=refusal_status,
            detail=(
                f"this credential belongs to a superseded runner generation ({stale}; "
                f"the work is at generation {current_generation}) — the current "
                "attempt must dial in with its own dispatch-issued token"
            ),
        )
    # Work-scoping refused, not authentication retried: the bearer holds
    # A token, just not this work's.
    raise HTTPException(status_code=refusal_status, detail="lane token does not scope this work")


async def _current_generation(request: Request, work_id: str) -> int | None:
    """The work's current generation behind the ack surface (R28-10).

    Authority-unavailable here is the caller's 503: an ack must not be
    judged against a generation nobody could read.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    try:
        return await durable_run_generation(session_factory, work_id)
    except LaneAuthorityUnavailable:
        logger.warning("current-generation lookup for work %s failed", work_id, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="the attempt-generation authority is unavailable — the ack is refused",
        ) from None


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

    Delegates to :func:`authorize_work_credential` — the ONE ladder the
    lane-control surface and the checkpoint channel share (NEXT-01):
    the CURRENT generation's attempt-scoped token, the legacy
    work-scoped one inside the migration deadline only, and a
    superseded generation's token refused with the actionable 403.

    Returns the verified secret (the caller never needs it beyond the
    check — returning it keeps the 503/401/403 ladder in ONE place).
    Raises :class:`HTTPException` on every refusal.
    """
    secret = _secret(request)
    if not secret:
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    await authorize_work_credential(
        request, secret=secret, authorization=authorization, work_id=work_id
    )
    return secret


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


@lane_control_router.get("/lane/controls/resume-spec")
async def get_lane_resume_spec(
    request: Request,
    work_id: str = Query(min_length=1),
    authorization: str | None = Header(None),
) -> Any:
    """The work's LATEST ``resume`` command ROW, whatever rung it sits on.

    NEXT-03: the pending view above stops at the dispatch boundary by
    design — but the resume decision must outlive its own
    acknowledgement. A resumed runner reads its ResumeSpec from THIS
    surface: the durable ``control_commands`` row (payload immutable
    once submitted), never a pending-queue lookup that has already
    progressed past ``received``. ``command`` is ``None`` when the work
    never received a resume.
    """
    await _authorize_lane(request, authorization, work_id)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    from sqlalchemy import select

    from forge.adaptive.mailbox_db import _to_command

    async with session_factory() as session:
        row = await session.scalar(
            select(ControlCommandRow)
            .where(ControlCommandRow.work_id == work_id, ControlCommandRow.kind == "resume")
            .order_by(ControlCommandRow.sequence.desc(), ControlCommandRow.id.desc())
            .limit(1)
        )
    command = _to_command(row) if row is not None else None
    return {
        "work_id": work_id,
        "command": command.model_dump(mode="json") if command is not None else None,
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


# ---------------------------------------------------------------------------
# R38-02 (#303) — runner-time credential redemption (delivery profile b).
# ---------------------------------------------------------------------------

#: The redemption route on this router (the transport the delivery plan
#: names under ``runner-redemption``; the lane dials it at startup).
LANE_CREDENTIAL_REDEEM_ROUTE: Final = "/lane/credentials/redeem"

#: The redemption TTL (seconds) — the response's ``expires_at`` bound.
#: The credential is already attempt-scoped by the token's generation
#: component; the TTL additionally bounds how long a redeemed value is
#: presented as current.
REDEMPTION_TTL_ENV: Final = "FORGE_CREDENTIAL_REDEEM_TTL_SECONDS"
DEFAULT_REDEMPTION_TTL_SECONDS: Final = 3600


def redemption_ttl_seconds(env: Mapping[str, str] | None = None) -> float:
    """The redemption TTL from env (default 1h). Operator state: a
    malformed or non-positive value is a typed failure, never a silent
    re-default."""
    source = os.environ if env is None else env
    raw = str(source.get(REDEMPTION_TTL_ENV, "") or "").strip()
    if not raw:
        return float(DEFAULT_REDEMPTION_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        raise LegacyWindowInvalid(
            f"{REDEMPTION_TTL_ENV}={raw!r} is not a number — fix the value; the "
            "redemption TTL is never silently re-defaulted"
        ) from None
    if value <= 0:
        raise LegacyWindowInvalid(
            f"{REDEMPTION_TTL_ENV}={raw!r} must be positive — fix the value; the "
            "redemption TTL is never silently re-defaulted"
        )
    return value


async def _record_redemption_audit(
    session_factory: Any,
    work_id: str,
    entry: dict[str, Any],
) -> None:
    """Persist the redemption audit row BEFORE the value is returned.

    Q39-03 (#322): the audit lands in the APPEND-ONLY
    ``credential_redemptions`` ledger — refs and metadata ONLY
    (redemption id, grant id, binding revision, resolver identity,
    attempt generation, expiry); there is no value slot and no value
    digest. The bounded ``credential_redemptions`` evidence list becomes
    a VERSIONED PROJECTION of that ledger, rewritten through an
    optimistic compare-and-swap that re-reads on conflict — a concurrent
    native-handle / checkpoint / continuation / grant write between the
    projection's read and write is PRESERVED, never lost to the audit
    (the old whole-document overwrite was exactly that loss). A failed
    audit write REFUSES the redemption (the broker-audit doctrine: every
    redemption is centrally auditable or it does not happen); a
    re-delivered receipt id is a counted retry observation on the SAME
    logical row, never a second redemption.
    """
    from forge.adaptive.credential_audit import RedemptionReceipt, record_redemption

    try:
        await record_redemption(session_factory, RedemptionReceipt.from_audit_entry(work_id, entry))
    except LaneAuthorityUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — ANY audit failure refuses the
        # redemption (typed conflict, malformed receipt, projection loss or
        # an unreachable ledger): centrally auditable or it does not happen.
        logger.warning("redemption audit for work %s failed", work_id[:8], exc_info=True)
        raise LaneAuthorityUnavailable(
            f"the redemption audit could not be persisted ({exc.__class__.__name__})"
        ) from exc


async def persist_operation_grant(session_factory: Any, *, grant: Any) -> Any:
    """Persist the dispatch's operation grant into the run evidence
    (Q39-01/#320) — idempotently, BEFORE the provider call.

    The grant lives under ``credential_operation_grants`` keyed by
    attempt+route; :func:`forge.adaptive.credential_broker.
    merge_operation_grant` keeps the EXISTING document when this
    attempt+route already authorized the SAME ref (a re-dispatch neither
    widens nor re-anchors the redemption window), so the returned
    EFFECTIVE grant is the one a redemption under this attempt will
    load. Raises :class:`LaneAuthorityUnavailable` when the run row
    cannot be read or written — the dispatch caller parks the run: a
    lane must never boot able to dial a redemption endpoint that would
    refuse it for a grant nobody persisted.
    """
    from forge.adaptive.credential_broker import merge_operation_grant
    from forge.durable.models import FlowRun

    async with session_factory() as session:
        run = await session.get(FlowRun, grant.work_id)
        if run is None:
            raise LaneAuthorityUnavailable(
                f"work {grant.work_id!r} is unknown — the operation grant cannot be persisted"
            )
        merged, effective = merge_operation_grant(dict(run.evidence or {}), grant)
        run.evidence = merged
        await session.commit()
        return effective


def _redemption_refusal(reason: str, detail: str, *, work_id: str = "") -> HTTPException:
    """One typed redemption refusal (Q39-01) — the observability seam
    (``credential.redemption_refused{reason}``) and the 403 the lane
    sees, carrying refs ONLY, never a value."""
    logger.warning(
        "credential.redemption_refused reason=%s work=%s %s", reason, work_id[:8], detail
    )
    return HTTPException(status_code=403, detail=f"{reason}: {detail}")


def _run_is_terminal(status: Any) -> bool:
    """Whether the run's status is terminal (ADR-0004: no outgoing
    transitions) — a finished attempt redeems nothing (Q39-01)."""
    from forge.durable.controller import TERMINAL_STATUSES

    return str(status or "") in {member.value for member in TERMINAL_STATUSES}


async def _authorize_operation_grant(
    run: Any,
    *,
    work_id: str,
    generation: int | None,
    provider: str,
    credential_ref: str,
) -> Any:
    """Load and judge the attempt's persisted OPERATION GRANT (Q39-01).

    THE authorization decision, in fail-closed order, every arm ZERO
    broker calls:

    - ``attempt_terminal`` — the run reached a terminal status; a
      finished attempt redeems nothing, no matter what it holds;
    - ``grant_route_mismatch`` — the attempt holds grants, but none for
      the REQUESTED provider route: the sibling binding of the same
      project is exactly the confused-deputy shape this closes (project
      membership ≠ operation authorization);
    - ``grant_ref_mismatch`` — the route matches but the requested ref
      is not the grant's EXACT ref;
    - ``grant_absent_native_only`` — this attempt's dispatch selected a
      NATIVE delivery mode: redemption was never authorized for it, and
      a registry entry alone grants nothing;
    - ``grant_absent_legacy`` — no grant and no delivery plan for the
      attempt: an unbound/legacy run (the credential policy in force is
      named in the refusal — under ``compat`` this is the labeled
      ambient-legacy boundary, under ``strict-broker`` a configuration
      defect; either way THIS endpoint authorizes nothing without a
      grant);
    - ``grant_expired`` — the request is at/past the grant's ABSOLUTE
      deadline (a fixed instant persisted at dispatch authorization —
      frozen across requests and restarts alike, never re-derived).

    Returns the surviving grant; raises the typed 403 otherwise.
    """
    from forge.adaptive.credential_broker import (
        DELIVERY_MODE_RUNNER_REDEMPTION,
        attempt_delivery_mode,
        operation_grants_for_attempt,
    )

    evidence = dict(run.evidence or {})
    grants = operation_grants_for_attempt(evidence, generation)
    if not grants:
        mode = attempt_delivery_mode(evidence, generation)
        if mode and mode != DELIVERY_MODE_RUNNER_REDEMPTION:
            raise _redemption_refusal(
                "grant_absent_native_only",
                (
                    f"this attempt's dispatch selected the {mode} delivery mode — "
                    "no runner-time redemption was authorized for it; a project "
                    "registry entry alone is not an operation grant"
                ),
                work_id=work_id,
            )
        raise _redemption_refusal(
            "grant_absent_legacy",
            (
                "this attempt carries no operation grant (an unbound or pre-grant "
                "legacy dispatch) — no credential is redeemed without the grant "
                "its dispatch persisted"
            ),
            work_id=work_id,
        )
    matching = [grant for grant in grants if grant.provider == provider]
    if not matching:
        granted = ", ".join(sorted(grant.provider for grant in grants))
        raise _redemption_refusal(
            "grant_route_mismatch",
            (
                f"the requested provider route {provider!r} is not this attempt's "
                f"authorized route (granted: {granted}) — a sibling binding of "
                "the same project is not an authorization for THIS operation"
            ),
            work_id=work_id,
        )
    grant = matching[0]
    if grant.credential_ref != credential_ref:
        raise _redemption_refusal(
            "grant_ref_mismatch",
            (
                "the requested credential ref is not the exact ref this attempt's "
                f"grant authorized (granted route {grant.provider!r})"
            ),
            work_id=work_id,
        )
    if grant.expired_at(datetime.now(timezone.utc)):
        raise _redemption_refusal(
            "grant_expired",
            (
                "the attempt's operation grant expired at its absolute deadline "
                f"{grant.redemption_deadline.isoformat()} — the deadline was fixed "
                "at dispatch authorization and never re-derives"
            ),
            work_id=work_id,
        )
    return grant


@lane_control_router.get(LANE_CREDENTIAL_REDEEM_ROUTE)
async def redeem_lane_credential(
    request: Request,
    work_id: str = Query(min_length=1),
    credential_ref: str = Query(min_length=1),
    provider: str = Query(min_length=1),
    authorization: str | None = Header(None),
) -> Any:
    """Redeem THIS attempt's model credential (R38-02 profile b, Q39-01).

    Auth is the EXISTING attempt-scoped lane token — the same
    ``HMAC(secret, work_id:generation)`` the dispatch minted for this
    attempt (the ladder :func:`authorize_work_credential` owns: 503 no
    secret, 401 no bearer, 403 wrong work / superseded generation, 503
    authority outage). The redemption is then authorized against the
    attempt's persisted **operation grant**
    (:func:`_authorize_operation_grant`): the requested route and ref
    must EQUAL the grant's, the run must not be terminal, and the
    grant's ABSOLUTE deadline (fixed at dispatch authorization, frozen
    across requests and restarts) must not have passed — a sibling
    binding of the same project, a native-only dispatch, an unbound
    legacy run and an expired window each refuse typed with ZERO broker
    calls. The registry's fail-closed checks re-run against the WORK's
    OWN canonical subject (a revoked binding or a rotated-away ref
    refuses as ever); the broker resolves; and AUTHORITY IS RE-VALIDATED
    after the awaited resolution and before the response publishes — a
    cancellation, supersede, rotation or expiry that lands during the
    await refuses typed, never emitting a value under retired authority.

    The lost-response retry window is the grant's own lifetime: a
    repeated request under the SAME grant inside the deadline is
    idempotent (the audit trail records separate observations, the
    receipts all join on the same ``grant_id``); past the deadline the
    grant is expired, absolutely.

    NOTE (Q39-01): the HMAC lane token stays THE authentication for this
    fix; a workload-OIDC ``id_token`` (Free-tier GitLab CE documented)
    is a LATER authentication ADAPTER that must still present this same
    operation grant — authentication may change bearer, authorization
    never widens.
    """
    import uuid

    from forge.adaptive.credential_broker import (
        BrokerCredentialRefusal,
        EnvBroker,
        credential_policy,
        reveal_secret,
    )
    from forge.adaptive.project_credentials import (
        CredentialRefusal,
        binding_subject_of_run,
        registry_from_env,
        resolve_dispatch_credential,
    )

    secret = _secret(request)
    if not secret:
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="lane control endpoint disabled")
    try:
        ttl = redemption_ttl_seconds()
    except LegacyWindowInvalid as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    generation = await authorize_work_credential(
        request, secret=secret, authorization=authorization, work_id=work_id
    )
    from forge.durable.models import FlowRun

    async with session_factory() as session:
        run = await session.get(FlowRun, work_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown work {work_id!r}")
    if _run_is_terminal(run.status):
        raise _redemption_refusal(
            "attempt_terminal",
            (
                "the run reached a terminal status — a finished attempt redeems "
                "nothing and refreshes no window"
            ),
            work_id=work_id,
        )
    subject = binding_subject_of_run(run)
    if subject is None:
        raise HTTPException(
            status_code=403,
            detail="the work names no credential binding subject — no credential is redeemed",
        )
    grant = await _authorize_operation_grant(
        run,
        work_id=work_id,
        generation=generation,
        provider=provider,
        credential_ref=credential_ref,
    )
    registry = getattr(request.app.state, "credential_registry", None) or registry_from_env()
    broker = getattr(request.app.state, "credential_broker", None) or EnvBroker()
    try:
        dispatch_credential = resolve_dispatch_credential(
            registry, subject=subject, provider=provider, presented_ref=credential_ref
        )
        resolved = await broker.resolve(
            grant.credential_ref,
            grant={
                "work_id": work_id,
                "attempt_generation": generation,
                "operation": "credential-redemption",
                "grant_id": grant.grant_id,
            },
        )
        staged_keys = set(resolved.staged_env)
        if staged_keys != {dispatch_credential.env_var}:
            raise CredentialRefusal(
                "staged_slot_mismatch",
                {
                    "subject": dispatch_credential.subject,
                    "bound_env_var": dispatch_credential.env_var,
                    "staged_env_vars": sorted(staged_keys),
                },
            )
    except CredentialRefusal as exc:
        logger.warning("credential redemption for work %s refused (%s)", work_id[:8], exc.reason)
        raise HTTPException(status_code=403, detail=f"{exc.reason}: {exc.detail}") from exc
    except BrokerCredentialRefusal as exc:
        logger.warning("credential redemption for work %s refused (%s)", work_id[:8], exc.reason)
        raise HTTPException(status_code=403, detail=f"{exc.reason}: {exc.detail}") from exc

    # Q39-01 — THE AUTHORITY FENCE across the awaited broker resolution:
    # the grant, the run's state and the binding are re-loaded AFTER the
    # broker answers and BEFORE anything publishes. A cancellation, a
    # superseding generation, a rotation or an expiry that landed during
    # the await refuses typed here — no value is emitted (and no audit
    # row pretends one was) under retired authority.
    async with session_factory() as session:
        fresh = await session.get(FlowRun, work_id)
    if fresh is None:
        raise LaneAuthorityUnavailable(f"work {work_id!r} disappeared during resolution")
    fence_refusals: HTTPException | None = None
    try:
        if _run_is_terminal(fresh.status):
            raise CredentialRefusal(
                "attempt_terminal",
                {
                    "fence": "authority retired during the broker resolution",
                    "status": str(fresh.status or ""),
                },
            )
        if int(fresh.cancellation_generation or 0) != int(grant.attempt_generation):
            raise CredentialRefusal(
                "attempt_superseded",
                {
                    "fence": "the attempt generation moved during the broker resolution",
                    "grant_generation": grant.attempt_generation,
                    "current_generation": int(fresh.cancellation_generation or 0),
                },
            )
        if grant.expired_at(datetime.now(timezone.utc)):
            raise CredentialRefusal(
                "grant_expired",
                {
                    "fence": "the grant's absolute deadline passed during the broker resolution",
                    "redemption_deadline": grant.redemption_deadline.isoformat(),
                },
            )
        # The binding, too: a rotation/revocation that landed while the
        # broker was resolving refuses exactly as it would have before.
        resolve_dispatch_credential(
            registry, subject=subject, provider=provider, presented_ref=grant.credential_ref
        )
    except CredentialRefusal as exc:
        logger.warning(
            "credential.redemption_refused reason=%s work=%s the authority fence refused "
            "after the broker resolved (no value was emitted)",
            exc.reason,
            work_id[:8],
        )
        fence_refusals = HTTPException(status_code=403, detail=f"{exc.reason}: {exc.detail}")
    if fence_refusals is not None:
        raise fence_refusals

    now = datetime.now(timezone.utc)
    # The response's expiry is the redemption TTL CAPPED at the grant's
    # absolute deadline: the value is never presented as current beyond
    # the authorization window that produced it (Q39-01 — the TTL alone
    # was response metadata, not a bound).
    expires_at = min(now + timedelta(seconds=ttl), grant.redemption_deadline)
    redemption_id = uuid.uuid4().hex
    # R38-04 (#305) — the consumer-receipt correlation join, durable in
    # the audit row BEFORE the value leaves: the broker's own receipt id,
    # the resolved version's KIND (a presence stamp displays as one) and
    # the credential policy in force. Q39-01 (#320) adds the GRANT id —
    # the foreign key into the operation grant (and #Q39-03's receipt
    # store when it lands) — plus the grant's age for observability.
    # Refs/metadata only, as ever.
    broker_receipt_id = str(resolved.receipt.get("receipt_id") or "")
    resolved_version_kind = str(resolved.version_kind or "")
    policy = credential_policy()
    prior_under_grant = sum(
        1
        for row in (run.evidence or {}).get("credential_redemptions") or []
        if isinstance(row, dict) and row.get("grant_id") == grant.grant_id
    )
    audit = {
        "at": now.isoformat(),
        "redemption_id": redemption_id,
        "grant_id": grant.grant_id,
        "subject": dispatch_credential.subject,
        "provider": dispatch_credential.provider,
        "credential_ref": dispatch_credential.credential_ref,
        "binding_revision": int(dispatch_credential.binding_revision),
        "resolver": resolved.resolver_identity,
        "attempt_generation": generation,
        "expires_at": expires_at.isoformat(),
        "broker_receipt_id": broker_receipt_id,
        "resolved_version_kind": resolved_version_kind,
        "credential_policy": policy,
        # The lost-response retry observation: a repeated request under
        # the SAME grant is idempotent — this counts the earlier
        # observations under it (0 on the first).
        "grant_retry_observation": prior_under_grant,
        "grant_age_seconds": max(0, int((now - grant.created_at).total_seconds())),
    }
    try:
        await _record_redemption_audit(session_factory, work_id, audit)
    except LaneAuthorityUnavailable as exc:
        logger.warning("redemption audit for work %s failed", work_id[:8], exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="the redemption audit could not be persisted — the redemption is refused",
        ) from exc
    return {
        "redemption_id": redemption_id,
        "grant_id": grant.grant_id,
        "work_id": work_id,
        "provider": dispatch_credential.provider,
        "credential_ref": dispatch_credential.credential_ref,
        "env_var": dispatch_credential.env_var,
        "value": reveal_secret(resolved.staged_env[dispatch_credential.env_var]),
        "expires_at": expires_at.isoformat(),
        "binding_revision": int(dispatch_credential.binding_revision),
        "resolver_identity": resolved.resolver_identity,
        "resolved_version": resolved.version,
        # The consumer-receipt correlation fields (R38-04): the lane's
        # bootstrap echoes these into its value-free consumer receipt,
        # joining broker id ↔ redemption id ↔ attempt ↔ consumer — and,
        # since Q39-01, the grant id every join keys on.
        "broker_receipt_id": broker_receipt_id,
        "resolved_version_kind": resolved_version_kind,
        "attempt_generation": generation,
        "credential_policy": policy,
    }
