"""The persistent operator view (R32-23): attempts, decisions, recovery.

An operator watching forge needs ONE answer to three questions — what is
the run doing, what did people decide about it, and how do I recover it
safely — and that answer must be a PROJECTION over the durable rows, never
a second truth store that can drift from them. The console research
(docs/research/2026-09-23-e2e-qualification/05) converged on one shape:
append-only source events, a derived current state, freshness belonging in
the projection, and a closed state vocabulary that is DERIVED from
signals, never asserted by workers. This module is that projection:

- :class:`OperatorProjection` — the versioned, read-only projection over
  one run's durable rows (run, attempts, control commands, checkpoints,
  verifications, publication intents). It COMPOSES the existing pure
  projections instead of forking their parsing: command rows classify
  through :func:`~forge.adaptive.operator_timeline.timeline_from_journal`,
  the blocked/waiting/summary line is
  :func:`~forge.adaptive.ops.status_projection` verbatim, and the
  credential guard is audit_export's ``_redact`` (imported, not copied).
- :func:`derive_state` — the pure derivation of the closed
  :data:`OperatorState` vocabulary. Every state is justified by a specific
  durable row and the derivation carries those row digests as thin
  evidence links. Derived health states say what operators actually need:
  ``wedged`` (executing but no SEMANTIC transition within the threshold —
  the looked-launched-but-stalled failure), ``stale`` (the stored
  projection predates the source rows), ``dead`` (terminal with external
  effects never reconciled).
- :func:`apply_update` — the compare-and-swap guard. A delayed or
  replayed update computed against an OLDER projection version is refused
  (:class:`StaleProjectionRejected`, naming the current state and the safe
  next action); versions are monotonic by construction.
- :class:`RecoveryActions` — the state × actor validity matrix. Controls
  LINK to the existing guarded path (the command router, the classic
  operator commands) and never duplicate it: every offered action carries
  the audit four facts (who / digest / when / linkage) and the
  ``expected_version`` for CAS at execution time, and a command computed
  against an old status comment is refused WITH the current state and the
  safe next action named.
- :func:`render` — the operator-facing document: exact identities (source
  sha, candidate shas, checkpoint id + digest, plan digest, attempt id,
  generation), unresolved external effects, the blocked/waiting line, and
  thin evidence links. No secrets, no raw prompts — identity and evidence
  are digests by construction, and the whole document passes the redaction
  guard anyway.

Row shapes (the documented mapping — hand-built fixtures and durable rows
alike normalize through ``operator_timeline._view_of``, so plain dicts,
dataclasses and pydantic contracts all read):

- ``run`` — a ``flow_runs``-shaped mapping: ``id``, ``status``,
  ``base_sha``, ``candidate_shas``, ``plan_digest``, ``evidence``,
  ``blocked_reason``, ``cancel_requested``, ``updated_at``;
- ``attempts`` — attempt rows: ``attempt_id``/``id``, ``status``
  (``executing`` / ``succeeded`` / ``failed`` / ``cancelled`` /
  ``accepted``; absent → unknown, never guessed), ``started_at``,
  ``updated_at``, ``generation``;
- ``commands`` — control-command rows (the mailbox/router shapes):
  ``command_id``, ``kind`` (``pause``/``resume``/``steer``/``answer``/…),
  ``status`` (the ladder ``received → authorized → dispatching →
  vendor_accepted/outcome_unknown → applied → checkpointed`` | rejected |
  expired), ``sequence``, ``actor_ref``, ``created_at``;
- ``checkpoints`` — checkpoint rows: ``checkpoint_id``/``id``,
  ``digest``, ``committed_at``, ``activated_at`` (the resume proof: the
  bytes were ACTIVATED, not merely requested), ``fence``
  (``held``/``cleared``; empty means the router's pause-booking pairing
  holds it), ``sequence``;
- ``verifications`` — verification rows: ``verification_id``/``id``,
  ``result`` (``passed``/``failed``/``unknown``), ``at``, ``candidate_sha``.
  A ``passed`` row binds to the CURRENT candidate: when it names a
  ``candidate_sha`` that is not among the run's ``candidate_shas`` it is
  an OLD green verdict about a DIFFERENT candidate and decorates nothing
  (R36-15 — the binding is asserted by tests); a row naming no candidate
  binds to whatever candidate the run holds;
- ``publications`` — publication-intent rows: ``operation_key``/``id``,
  ``status``, ``operation``, ``target_ref``, ``at``;
- ``approvals`` — gate-approval rows: ``approved_by``, ``at``,
  ``consumed_at``, ``generation``;
- ``questions`` / ``saga`` — the inputs :func:`ops.status_projection`
  already reads (unresolved questions outrank partial publication).

The view is READ-ONLY BY CHARTER: nothing here launches agents, edits
code, merges PRs or writes rows — recovery verbs link to the guarded
paths that already exist.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal

from forge.adaptive.audit_export import _redact as redact
from forge.adaptive.operator_timeline import _view_of as row_view
from forge.adaptive.operator_timeline import timeline_from_journal
from forge.adaptive.ops import status_projection

__all__ = [
    "ACTION_VIA",
    "ACTOR_ROLES",
    "ACTIONS",
    "ActionDecision",
    "OperatorAction",
    "OperatorProjection",
    "OPERATOR_STATES",
    "OPERATOR_VIEW_SCHEMA",
    "OperatorState",
    "RecoveryActions",
    "StateDerivation",
    "StaleProjectionRejected",
    "UNRESOLVED_PUBLICATION_STATUSES",
    "WEDGED_AFTER",
    "apply_update",
    "derive_state",
    "initial_projection",
    "render",
    "source_digest",
    "status_note_lines",
]

#: The schema discriminator every rendered projection carries (versioned:
#: a breaking change to the view contract bumps the tag).
OPERATOR_VIEW_SCHEMA: Final = "forge.operator.view/1"

#: The closed operator-state vocabulary. Lifecycle states describe the
#: run's position on the request → effect → recovery ladder; health
#: states are DERIVED overlays (``wedged``/``stale``/``dead``); the last
#: three are terminal. A projection carries exactly ONE state.
OperatorState = Literal[
    "requested",
    "authorized",
    "executing",
    "pause_pending",
    "safely_paused",
    "resumed",
    "unverified",
    "verified_ready",
    "wedged",
    "stale",
    "dead",
    "accepted",
    "rejected",
    "cancelled",
]

OPERATOR_STATES: Final[tuple[str, ...]] = (
    "requested",
    "authorized",
    "executing",
    "pause_pending",
    "safely_paused",
    "resumed",
    "unverified",
    "verified_ready",
    "wedged",
    "stale",
    "dead",
    "accepted",
    "rejected",
    "cancelled",
)

#: The default wedged threshold: an attempt marked executing whose last
#: SEMANTIC transition (a timeline row, an attempt update, a checkpoint,
#: a verification) is older than this is "alive but stalled" — the
#: looked-launched-but-stalled failure. A parameter of
#: :func:`derive_state`; 30 minutes is the documented default.
WEDGED_AFTER: Final = timedelta(minutes=30)

#: Publication-intent statuses that are NOT resolved — an effect that may
#: still land (or whose landing is unproven). Terminal-with-residue is the
#: ``dead`` derivation, and each row renders as an unresolved external
#: effect.
UNRESOLVED_PUBLICATION_STATUSES: Final[frozenset[str]] = frozenset(
    {"requested", "dispatched", "probing", "unknown"}
)

#: Command ladder rungs that mean "a pause was COMMANDED but the
#: checkpoint is not yet committed" — from the mailbox's ``received`` to
#: the lane's ``applied`` (the vendor took the interrupt; the booking has
#: not closed the loop).
_PAUSE_COMMANDED: Final[frozenset[str]] = frozenset(
    {
        "received",
        "pending",
        "authorized",
        "dispatching",
        "vendor_accepted",
        "outcome_unknown",
        "applied",
    }
)

#: Command rungs that mean a command never spent (refused on the ladder).
_COMMAND_REFUSED: Final[frozenset[str]] = frozenset({"rejected", "expired"})

#: flow-run statuses that map onto terminal operator states (the rest of
#: the mapping comes from attempt outcomes and acceptance evidence).
_RUN_TERMINAL: Final[Mapping[str, str]] = {"cancelled": "cancelled", "failed": "rejected"}

#: Attempt outcomes that prove a terminal state on their own row.
#: ``succeeded`` deliberately maps to NOTHING here: a succeeded attempt
#: yields a candidate, and acceptance is a decision, not an outcome.
_ATTEMPT_TERMINAL: Final[Mapping[str, str]] = {
    "failed": "rejected",
    "cancelled": "cancelled",
    "accepted": "accepted",
}

#: Verification results that count as "independent verification passed".
_VERIFICATION_PASSED: Final[frozenset[str]] = frozenset({"passed", "pass"})

#: Attempt status words that mean "the process is working right now".
_ATTEMPT_ACTIVE: Final[frozenset[str]] = frozenset({"executing", "running"})


# ---------------------------------------------------------------------------
# Row helpers — normalization, digests, time
# ---------------------------------------------------------------------------


def _norm(row: Any) -> dict[str, Any]:
    """One row as a plain dict (``{}`` when the shape is unrecognizable)."""
    view = row_view(row)
    return dict(view) if isinstance(view, Mapping) else {}


def _digest_json(value: Any) -> str:
    """A stable short digest over one JSON-able value (canonical keys)."""
    canonical = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _row_digest(row: Any) -> str:
    """The digest of one source row — the thin evidence-link pointer."""
    view = row_view(row)
    if isinstance(view, Mapping):
        return _digest_json(dict(view))
    return _digest_json(row)


def source_digest(source_rows: Mapping[str, Any]) -> str:
    """The digest over EVERY source row the projection was derived from.

    Two projections built from the same rows digest identically; a stored
    projection whose ``source_digest`` differs from the current rows'
    digest is STALE — it was computed from older rows than exist now.
    """
    payload: dict[str, Any] = {}
    for key in sorted(source_rows):
        value = source_rows[key]
        if isinstance(value, (list, tuple)):
            payload[key] = [_row_digest(item) for item in value]
        else:
            payload[key] = _row_digest(value)
    return _digest_json(payload)


def _as_datetime(value: Any) -> datetime | None:
    """A timezone-aware datetime from a datetime or ISO string (UTC
    assumed when naive); ``None`` when the value carries no readable
    clock — never a synthesized one."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _iso(value: Any) -> str:
    """The UTC ISO string of *value* (verbatim for unparseable strings,
    ``""`` for nothing) — the shape every rendered timestamp takes."""
    moment = _as_datetime(value)
    if moment is not None:
        return moment.astimezone(timezone.utc).isoformat()
    return str(value) if value else ""


def _first(view: Mapping[str, Any], *keys: str) -> Any:
    """The first present, non-empty value among *keys* (row spellings vary
    across the durable and contract shapes; the meaning does not)."""
    for key in keys:
        value = view.get(key)
        if value not in (None, ""):
            return value
    return None


def _require_run(source_rows: Mapping[str, Any]) -> dict[str, Any]:
    run = source_rows.get("run")
    if run is None:
        raise ValueError("no run row — a projection without a run is nothing")
    view = _norm(run)
    if not view:
        raise ValueError("unreadable run row — never guessed into a state")
    return view


def _latest_of(rows: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    """The last row of *kind* in journal order (append-only journals are
    already ordered — nothing here re-sorts or invents timing)."""
    found: dict[str, Any] | None = None
    for view in rows:
        if str(view.get("kind") or "") == kind:
            found = view
    return found


# ---------------------------------------------------------------------------
# The pure state derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateDerivation:
    """What :func:`derive_state` concluded, and why.

    ``state`` is the semantic lifecycle state; ``health`` carries the
    derived overlays that applied (``wedged`` / ``stale`` / ``dead``);
    ``display_state`` is what the console shows (stale > dead > wedged >
    semantic — a possibly-outdated view never displays as a confident
    state). ``evidence`` holds one THIN link (``{"of", "id", "ref"}`` —
    what the row is, which row, its digest) per proving row, and
    ``reasons`` the human sentence for each derivation step.
    """

    state: str
    health: tuple[str, ...]
    display_state: str
    evidence: tuple[dict[str, str], ...]
    reasons: tuple[str, ...]
    last_transition_at: str


def derive_state(
    source_rows: Mapping[str, Any],
    now: datetime | str,
    *,
    wedged_after: timedelta = WEDGED_AFTER,
    stored: OperatorProjection | None = None,
) -> StateDerivation:
    """Derive the operator state from durable rows (pure, evidence-linked).

    The ladder, checked in order — each rung names the row that proves it:

    1. terminal — a cancelled/failed run row, a failed/cancelled/accepted
       attempt outcome, or an acceptance marker in the run's evidence;
    2. ``pause_pending`` — a pause COMMANDED (ladder rung up to ``applied``)
       whose checkpoint is not yet committed;
    3. ``safely_paused`` — a committed checkpoint with the fence held and
       no activated resume;
    4. ``resumed`` — a resume whose checkpoint bytes were ACTIVATED (a
       resume request alone NEVER displays as restored — it stays at 3);
    5. ``executing`` — an attempt actively working (``wedged`` when its
       last semantic transition is older than *wedged_after*);
    6. ``verified_ready`` / ``unverified`` — a candidate exists and
       independent verification passed / has not;
    7. ``authorized`` — an approval or authorized command, nothing running;
    8. ``requested`` — the run row alone.

    ``dead`` overlays a failed/cancelled terminal with unresolved external
    effects (publication intents not in a resolved state). ``stale`` is
    derived when *stored* is given and its ``source_digest`` no longer
    matches these rows — the stored projection is older than the truth.
    """
    run = _require_run(source_rows)
    run_id = str(_first(run, "id", "run_id") or "")
    attempts = [_norm(row) for row in source_rows.get("attempts") or []]
    commands = [_norm(row) for row in source_rows.get("commands") or []]
    checkpoints = [_norm(row) for row in source_rows.get("checkpoints") or []]
    verifications = [_norm(row) for row in source_rows.get("verifications") or []]
    publications = [_norm(row) for row in source_rows.get("publications") or []]
    approvals = [_norm(row) for row in source_rows.get("approvals") or []]

    evidence: list[dict[str, str]] = []
    reasons: list[str] = []

    def link(of: str, ident: Any, row: Any) -> None:
        evidence.append({"of": of, "id": str(ident or ""), "ref": _row_digest(row)})

    def attempt_of(view: dict[str, Any]) -> tuple[str, str]:
        return str(_first(view, "attempt_id", "id") or ""), str(
            _first(view, "status", "state", "outcome") or ""
        )

    latest_attempt = attempts[-1] if attempts else None
    attempt_id, attempt_status = attempt_of(latest_attempt) if latest_attempt else ("", "")

    pause = _latest_of(commands, "pause")
    pause_status = str(_first(pause, "status") or "") if pause else ""
    resume = _latest_of(commands, "resume")
    resume_status = str(_first(resume, "status") or "") if resume else ""
    pause_booked = pause_status == "checkpointed"
    committed = [
        cp
        for cp in checkpoints
        if _first(cp, "committed_at") or str(_first(cp, "status") or "") == "committed"
    ]
    checkpoint = committed[-1] if committed else None
    fence = str(_first(checkpoint, "fence") or "") if checkpoint else ""
    fence_held = bool(checkpoint) and (
        fence == "held" or (not fence and pause_booked)
    )  # the router raises the fence with the booking; an explicit word wins
    activated = any(
        _first(cp, "activated_at") is not None or cp.get("activated") is True for cp in checkpoints
    )
    resume_requested = bool(resume) and resume_status not in _COMMAND_REFUSED

    unresolved = [
        pub
        for pub in publications
        if str(_first(pub, "status") or "") in UNRESOLVED_PUBLICATION_STATUSES
    ]
    candidate_shas = list(run.get("candidate_shas") or [])
    run_evidence = run.get("evidence") if isinstance(run.get("evidence"), Mapping) else {}
    if not candidate_shas and isinstance(_first(run_evidence, "candidate_sha"), str):
        candidate_shas = [run_evidence["candidate_sha"]]
    # R36-15: a passed verification DECORATES only the candidate it tested.
    # A row whose candidate_sha is not among the run's current candidates is
    # an old green verdict about a DIFFERENT artifact — it cannot make the
    # current one verified_ready. A row naming no candidate binds to
    # whatever candidate the run holds (the hand-built fixture shape).
    verifying = [
        v
        for v in verifications
        if str(_first(v, "result", "outcome") or "") in _VERIFICATION_PASSED
        and str(v.get("candidate_sha") or "") in ("", *candidate_shas)
    ]
    verified = bool(verifying)
    accepted_marker = bool(run_evidence.get("accepted") or run_evidence.get("accepted_by"))

    # -- last semantic transition (the wedged clock) ----------------------
    times: list[tuple[datetime, str]] = []
    for entry in timeline_from_journal(list(source_rows.get("commands") or [])):
        moment = _as_datetime(entry.at)
        if moment is not None:
            times.append((moment, entry.at))
    for view in attempts + checkpoints + verifications:
        for key in ("updated_at", "started_at", "committed_at", "activated_at", "at"):
            moment = _as_datetime(view.get(key))
            if moment is not None:
                times.append((moment, _iso(view.get(key))))
    last_transition_at = max(times, key=lambda pair: pair[0])[1] if times else ""

    # -- the ladder --------------------------------------------------------
    state: str
    if run.get("cancel_requested") or str(run.get("status") or "") in _RUN_TERMINAL:
        state = (
            "cancelled"
            if run.get("cancel_requested") or str(run.get("status") or "") == "cancelled"
            else "rejected"
        )
        link("run", run_id, run)
        reasons.append(f"terminal run row: status {run.get('status')!r}")
    elif attempt_status in _ATTEMPT_TERMINAL:
        state = _ATTEMPT_TERMINAL[attempt_status]
        link("attempt", attempt_id, latest_attempt)
        reasons.append(f"attempt {attempt_id} outcome {attempt_status!r}")
    elif accepted_marker:
        state = "accepted"
        link("run", run_id, run)
        reasons.append("acceptance recorded in the run's evidence")
    elif pause and pause_status in _PAUSE_COMMANDED:
        state = "pause_pending"
        link("pause command", _first(pause, "command_id"), pause)
        reasons.append(
            f"pause {_first(pause, 'command_id')} commanded (status {pause_status!r}), "
            "no committed checkpoint yet"
        )
    elif checkpoint and fence_held and not (resume_requested and activated):
        state = "safely_paused"
        if pause:  # the pause row may be absent from these rows — the booking proves it too
            link("pause command", _first(pause, "command_id"), pause)
        link("checkpoint", _first(checkpoint, "checkpoint_id", "id"), checkpoint)
        reasons.append(
            "checkpoint committed with the fence held"
            + ("" if not resume else f"; resume {resume_status!r} requested but not activated")
        )
    elif resume_requested and activated:
        state = "resumed"
        link("resume command", _first(resume, "command_id"), resume)
        activated_cp = checkpoint if checkpoint else (checkpoints[-1] if checkpoints else None)
        link("checkpoint", _first(activated_cp or {}, "checkpoint_id", "id"), activated_cp)
        reasons.append("resume accepted and the checkpoint bytes were activated")
    elif attempt_status in _ATTEMPT_ACTIVE:
        state = "executing"
        link("attempt", attempt_id, latest_attempt)
        reasons.append(f"attempt {attempt_id} is executing")
    elif candidate_shas and verified:
        state = "verified_ready"
        link("verification", _first(verifying[-1], "verification_id", "id"), verifying[-1])
        reasons.append(
            f"candidate {candidate_shas[0][:12]} exists, verification passed for that candidate"
        )
    elif candidate_shas:
        state = "unverified"
        link("run", run_id, run)
        reasons.append(f"candidate {candidate_shas[0][:12]} exists, no passed verification")
    elif approvals or any(str(c.get("status") or "") == "authorized" for c in commands):
        state = "authorized"
        if approvals:
            link("approval", _first(approvals[-1], "approved_by", "approver"), approvals[-1])
        reasons.append("an approval or authorized command exists, nothing executing")
    else:
        state = "requested"
        link("run", run_id, run)
        reasons.append("the run row alone — requested, nothing more proven")

    # -- derived health overlays -------------------------------------------
    health: list[str] = []
    moment_now = _as_datetime(now)
    reference = _as_datetime(last_transition_at)
    if reference is None and latest_attempt is not None:
        reference = _as_datetime(latest_attempt.get("started_at"))
    if (
        state == "executing"
        and moment_now is not None
        and reference is not None
        and moment_now - reference > wedged_after
    ):
        health.append("wedged")
        reasons.append(
            f"no semantic transition for over {int(wedged_after.total_seconds() // 60)} minutes"
        )
    if state in ("rejected", "cancelled") and unresolved:
        health.append("dead")
        reasons.append(
            f"terminal {state} with {len(unresolved)} unresolved external effect(s) — "
            "not reconciled"
        )
    if stored is not None and stored.source_digest != source_digest(source_rows):
        health.append("stale")
        reasons.append(
            f"the stored projection (v{stored.projection_version}) was computed from "
            "older rows than these"
        )

    display = next(
        word for word in ("stale", "dead", "wedged", state) if word in health or word == state
    )
    return StateDerivation(
        state=state,  # type: ignore[arg-type]
        health=tuple(health),
        display_state=display,  # type: ignore[arg-type]
        evidence=tuple(evidence),
        reasons=tuple(reasons),
        last_transition_at=last_transition_at,
    )


# ---------------------------------------------------------------------------
# The versioned projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorProjection:
    """The stored, versioned projection over one run's durable rows.

    ``projection_version`` is monotonic — only :func:`apply_update` mints
    one, always ``current + 1``, and only when the caller's
    ``expected_version`` still matches. ``source_digest`` is the freshness
    anchor: it digests every row the projection was derived from, so a
    stored projection can be checked against the CURRENT rows and marked
    ``stale`` when they have moved. ``identity`` carries the exact
    identities (never "the latest build"), ``unresolved_effects`` the
    external effects that may still land, and ``evidence`` the thin links
    to the rows that prove the state. Everything is redacted at build.
    """

    schema: str
    run_id: str
    projection_version: int
    state: str
    underlying_state: str
    blocked_reason: str
    waiting_on: str | None
    summary: str
    identity: dict[str, Any]
    unresolved_effects: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, str], ...]
    reasons: tuple[str, ...]
    last_transition_at: str
    computed_at: str
    source_digest: str
    rows_observed: dict[str, str]

    @property
    def action_digest(self) -> str:
        """The audit four-facts WHAT — the exact artifact a decision about
        this projection refers to: the candidate sha, else the checkpoint
        digest, else the plan digest, else the whole source digest."""
        candidates = self.identity.get("candidate_shas") or []
        if candidates:
            return str(candidates[0])
        for key in ("checkpoint_digest", "plan_digest"):
            if self.identity.get(key):
                return str(self.identity[key])
        return self.source_digest


class StaleProjectionRejected(Exception):
    """A delayed or replayed update refused by the CAS guard.

    Raised by :func:`apply_update` when the update's ``expected_version``
    is OLDER than the stored projection — the update was computed against
    rows the stored version has already superseded, and overwriting the
    newer truth with it is exactly the failure the guard exists for. The
    refusal names the CURRENT state and the safe next action so the
    operator re-decides against the world as it now is.
    """

    def __init__(
        self,
        *,
        expected_version: int,
        current_version: int,
        current_state: str,
        safe_next_action: str,
    ) -> None:
        self.expected_version = expected_version
        self.current_version = current_version
        self.current_state = current_state
        self.safe_next_action = safe_next_action
        super().__init__(
            f"stale projection update: expected_version {expected_version} is older than the "
            f"stored projection v{current_version} (state {current_state!r}) — recompute from "
            f"the current rows; safe next action: {safe_next_action or 'none'}"
        )


def _rows_observed(source_rows: Mapping[str, Any]) -> dict[str, str]:
    """Per-source freshness: when each section was last observed (``""``
    for a section this projection never saw — absence stays explicit)."""
    observed: dict[str, str] = {}
    run = _norm(source_rows.get("run") or {})
    observed["run"] = _iso(_first(run, "updated_at", "created_at"))
    for section, keys in (
        ("attempts", ("updated_at", "started_at")),
        ("commands", ("created_at", "applied_at")),
        ("checkpoints", ("committed_at", "activated_at")),
        ("verifications", ("at",)),
        ("publications", ("at",)),
        ("approvals", ("at",)),
        ("questions", ("at",)),
    ):
        stamps = [
            _iso(_first(_norm(row), *keys))
            for row in (source_rows.get(section) or [])
            if _norm(row)
        ]
        stamps = [stamp for stamp in stamps if stamp]
        observed[section] = max(stamps) if stamps else ""
    return observed


def _assemble(
    source_rows: Mapping[str, Any], now: datetime | str, *, version: int, wedged_after: timedelta
) -> OperatorProjection:
    run = _require_run(source_rows)
    run_id = str(_first(run, "id", "run_id") or "")
    derivation = derive_state(source_rows, now, wedged_after=wedged_after)
    run_view = {
        "status": derivation.display_state,
        "blocked_reason": str(run.get("blocked_reason") or ""),
    }
    questions = [_norm(row) for row in source_rows.get("questions") or []]
    saga = dict(source_rows["saga"]) if isinstance(source_rows.get("saga"), Mapping) else None
    status = status_projection(run_view, questions, saga)

    checkpoints = [_norm(row) for row in source_rows.get("checkpoints") or []]
    committed = [
        cp
        for cp in checkpoints
        if _first(cp, "committed_at") or str(_first(cp, "status") or "") == "committed"
    ]
    checkpoint = committed[-1] if committed else (checkpoints[-1] if checkpoints else None)
    attempts = [_norm(row) for row in source_rows.get("attempts") or []]
    latest_attempt = attempts[-1] if attempts else None
    publications = [_norm(row) for row in source_rows.get("publications") or []]
    unresolved = tuple(
        redact(
            {
                "operation_key": str(_first(pub, "operation_key", "id") or ""),
                "operation": str(pub.get("operation") or ""),
                "target_ref": str(pub.get("target_ref") or ""),
                "status": str(_first(pub, "status") or ""),
            }
        )
        for pub in publications
        if str(_first(pub, "status") or "") in UNRESOLVED_PUBLICATION_STATUSES
    )
    identity = redact(
        {
            "source_sha": str(run.get("base_sha") or ""),
            "candidate_shas": list(run.get("candidate_shas") or []),
            "checkpoint_id": str(_first(checkpoint, "checkpoint_id", "id") or "")
            if checkpoint
            else "",
            "checkpoint_digest": str(_first(checkpoint, "digest") or "") if checkpoint else "",
            "plan_digest": str(run.get("plan_digest") or ""),
            "attempt_id": str(_first(latest_attempt, "attempt_id", "id") or "")
            if latest_attempt
            else "",
            "generation": _first(latest_attempt, "generation")
            if latest_attempt
            else run.get("commit_cycle"),
        }
    )
    return OperatorProjection(
        schema=OPERATOR_VIEW_SCHEMA,
        run_id=run_id,
        projection_version=version,
        state=derivation.display_state,
        underlying_state=derivation.state,
        blocked_reason=status["blocked_reason"],
        waiting_on=status["waiting_on"],
        summary=status["summary"],
        identity=identity,
        unresolved_effects=unresolved,
        evidence=derivation.evidence,
        reasons=derivation.reasons,
        last_transition_at=derivation.last_transition_at,
        computed_at=_iso(now),
        source_digest=source_digest(source_rows),
        rows_observed=_rows_observed(source_rows),
    )


def initial_projection(
    source_rows: Mapping[str, Any],
    now: datetime | str | None = None,
    *,
    wedged_after: timedelta = WEDGED_AFTER,
) -> OperatorProjection:
    """Build the first projection over *source_rows* (version 1)."""
    moment = now if now is not None else datetime.now(timezone.utc)
    return _assemble(source_rows, moment, version=1, wedged_after=wedged_after)


def apply_update(
    current: OperatorProjection,
    source_rows: Mapping[str, Any],
    expected_version: int,
    *,
    now: datetime | str | None = None,
    wedged_after: timedelta = WEDGED_AFTER,
) -> OperatorProjection:
    """Apply a recomputed projection under the compare-and-swap guard.

    *expected_version* is the stored version the update was COMPUTED
    against (what the updater read before re-deriving). When it is older
    than ``current.projection_version`` the update is a delayed replay —
    rows the stored version already superseded — and is refused with
    :class:`StaleProjectionRejected` (the newer projection stays). When it
    is AHEAD of the stored version the caller claims a future truth and
    that is a bug, not a race: ``ValueError``. Only an exact match mints
    ``current + 1`` — versions are monotonic by construction, never
    re-used and never rolled back.
    """
    if expected_version < current.projection_version:
        raise StaleProjectionRejected(
            expected_version=expected_version,
            current_version=current.projection_version,
            current_state=current.state,
            safe_next_action=RecoveryActions.safe_next(current.state, "observer"),
        )
    if expected_version > current.projection_version:
        raise ValueError(
            f"expected_version {expected_version} is ahead of the stored projection "
            f"v{current.projection_version} — projection versions are monotonic"
        )
    run = _require_run(source_rows)
    run_id = str(_first(run, "id", "run_id") or "")
    if run_id != current.run_id:
        raise ValueError(
            f"update for run {run_id!r} cannot replace the projection of run {current.run_id!r}"
        )
    moment = now if now is not None else datetime.now(timezone.utc)
    return _assemble(
        source_rows, moment, version=current.projection_version + 1, wedged_after=wedged_after
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(projection: OperatorProjection) -> dict[str, Any]:
    """The operator-facing document: identities, effects, evidence — no
    secrets, no raw prompts.

    Identity and evidence are ids and digests BY CONSTRUCTION (a thin link
    shows that evidence EXISTS and where to drill in — never the payload,
    never the prompt), and the finished document passes the audit-export
    redaction guard regardless: a secret-looking VALUE that sneaks into
    any row-derived string is dropped before an operator ever sees it.
    """
    return redact(
        {
            "schema": projection.schema,
            "run_id": projection.run_id,
            "projection_version": projection.projection_version,
            "state": projection.state,
            "underlying_state": projection.underlying_state,
            "blocked_reason": projection.blocked_reason,
            "waiting_on": projection.waiting_on,
            "summary": projection.summary,
            "identity": dict(projection.identity),
            "unresolved_effects": [dict(effect) for effect in projection.unresolved_effects],
            "evidence": [dict(link) for link in projection.evidence],
            "derivation": list(projection.reasons),
            "last_transition_at": projection.last_transition_at,
            "computed_at": projection.computed_at,
            "rows_observed": dict(projection.rows_observed),
        }
    )


def _short(value: Any, size: int = 12) -> str:
    """A compact identity prefix — the native-comment spelling of a sha."""
    return str(value or "")[:size]


def status_note_lines(projection: OperatorProjection) -> list[str]:
    """Compact native-comment lines for one projection (R36-15 parity).

    The SAME state semantics as :func:`render` — one state, the same
    identities, the same blocked/waiting line — rendered as the short
    lines a ``/status`` native comment carries. Pure: it reads the
    projection and nothing else, writes nothing, and is the parity hook
    the native surface and the management API agree through (a test pins
    that both surfaces agree on state and identity for the same
    snapshot). Long identities are shortened to 12 characters; the full
    values live in the render.
    """
    identity = projection.identity
    lines = [f"Run {projection.run_id} is {projection.state}."]
    detail: list[str] = []
    if identity.get("source_sha"):
        detail.append(f"source {_short(identity['source_sha'])}")
    if identity.get("plan_digest"):
        detail.append(f"plan {_short(identity['plan_digest'])}")
    if identity.get("attempt_id"):
        detail.append(f"attempt {identity['attempt_id']}")
    if identity.get("generation") is not None:
        detail.append(f"generation {identity['generation']}")
    candidates = identity.get("candidate_shas") or []
    if candidates:
        detail.append(f"candidate {_short(candidates[0])}")
    if identity.get("checkpoint_id"):
        digest = _short(identity.get("checkpoint_digest"), 12)
        detail.append(
            f"checkpoint {identity['checkpoint_id']}" + (f" ({digest})" if digest else "")
        )
    if detail:
        lines.append("Identity: " + ", ".join(detail) + ".")
    if projection.blocked_reason:
        lines.append(f"Blocked: {projection.blocked_reason}")
    elif projection.waiting_on:
        lines.append(f"Waiting on: {projection.waiting_on}")
    if projection.unresolved_effects:
        lines.append(
            f"{len(projection.unresolved_effects)} unresolved external effect(s) — "
            "reconcile before retry."
        )
    if projection.last_transition_at:
        lines.append(f"Last transition: {projection.last_transition_at}")
    return lines


# ---------------------------------------------------------------------------
# Recovery actions — the validity matrix and the CAS-checked decision
# ---------------------------------------------------------------------------

#: Every action the view may offer. The control verbs route through the
#: EXISTING guarded paths (the command router's authenticated ingress for
#: pause/resume/steer/answer; the classic operator commands for
#: retry/cancel/reconcile); ``probe`` is the read-only reconciliation
#: query the research mandates before any retry.
ACTIONS: Final[tuple[str, ...]] = (
    "pause",
    "resume",
    "steer",
    "answer",
    "retry",
    "cancel",
    "reconcile",
    "probe",
)

#: Which guarded path an action links to — the view NEVER duplicates the
#: path, it names it.
ACTION_VIA: Final[Mapping[str, str]] = {
    "pause": "command_router:/pause",
    "resume": "command_router:/resume",
    "steer": "command_router:/steer",
    "answer": "command_router:/answer",
    "retry": "operator-commands:/retry",
    "cancel": "operator-commands:/cancel",
    "reconcile": "operator-commands:/reconcile",
    "probe": "read-only:/status",
}

#: The actor roles. ``approver`` is the configured approver set (the SAME
#: authority as /go and the command router); ``observer`` and
#: ``automation`` may only read.
ACTOR_ROLES: Final[tuple[str, ...]] = ("approver", "observer", "automation")

#: The state → action matrix. The pinned constraints: resume ONLY in
#: ``safely_paused`` (CTL-06: a resume stands on a confirmed checkpoint);
#: steer ONLY while ``executing``; retry ONLY in the failed terminals;
#: ``stale`` allows nothing but the probe — refresh before acting.
_STATE_ACTIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "requested": ("cancel", "probe"),
    "authorized": ("pause", "cancel", "probe"),
    "executing": ("pause", "steer", "cancel", "probe"),
    "pause_pending": ("cancel", "probe"),
    "safely_paused": ("resume", "cancel", "probe"),
    "resumed": ("pause", "cancel", "probe"),
    "unverified": ("cancel", "probe"),
    "verified_ready": ("probe",),
    "wedged": ("pause", "cancel", "probe"),
    "stale": ("probe",),
    "dead": ("retry", "reconcile", "probe"),
    "accepted": (),
    "rejected": ("retry", "probe"),
    "cancelled": ("probe",),
}

#: Role → the subset of ACTIONS the role may ever take. ``approver`` is
#: bounded only by the state matrix; read-only roles get the read-only
#: probe.
_ROLE_ACTIONS: Final[Mapping[str, frozenset[str] | None]] = {
    "approver": None,
    "observer": frozenset({"probe"}),
    "automation": frozenset({"probe"}),
}


@dataclass(frozen=True)
class OperatorAction:
    """One offered recovery action with its audit four facts and its CAS
    ticket.

    who (``actor``) / what exactly (``digest`` — the projection's
    :attr:`OperatorProjection.action_digest`, never "the latest build") /
    when (``at``, a server-side timestamp) / why-linkage (``linkage`` —
    the run and projection version the decision traces to). The action
    LINKS to the guarded path (``via``); it never executes anything, and
    ``expected_version`` is the version it must still match at execution
    time or be refused.
    """

    action: str
    state: str
    actor_role: str
    actor: str
    digest: str
    at: str
    linkage: str
    expected_version: int
    via: str

    def audit_fact(self) -> dict[str, Any]:
        """The four facts as one record (what an executed action journals)."""
        return {
            "who": self.actor,
            "digest": self.digest,
            "when": self.at,
            "linkage": self.linkage,
            "action": self.action,
            "state": self.state,
            "expected_version": self.expected_version,
        }


@dataclass(frozen=True)
class ActionDecision:
    """The CAS-checked verdict for one action against the CURRENT
    projection: allowed, or refused with the current state and the safe
    next action named."""

    allowed: bool
    reason: str
    current_state: str
    safe_next_action: str
    action: OperatorAction


class RecoveryActions:
    """The state × actor validity matrix (pure; controls link out)."""

    @staticmethod
    def valid_for(state: str, actor_role: str) -> tuple[str, ...]:
        """The actions valid in *state* for *actor_role*, in safe-first
        order. Unknown states and roles fail visibly (``ValueError``) —
        least-authority guessing is how consoles drift into drive-by
        writes."""
        if state not in OPERATOR_STATES:
            raise ValueError(f"unknown operator state: {state!r}")
        if actor_role not in ACTOR_ROLES:
            raise ValueError(f"unknown actor role: {actor_role!r}")
        allowed = _STATE_ACTIONS[state]
        bounded = _ROLE_ACTIONS[actor_role]
        return tuple(a for a in allowed if bounded is None or a in bounded)

    @staticmethod
    def safe_next(state: str, actor_role: str) -> str:
        """The first safe action for *state* / *actor_role* (``""`` when
        none — an accepted run needs nothing)."""
        actions = RecoveryActions.valid_for(state, actor_role)
        return actions[0] if actions else ""

    @staticmethod
    def plan(
        projection: OperatorProjection,
        actor: str,
        actor_role: str,
        *,
        at: datetime | str | None = None,
        linkage: str = "",
    ) -> tuple[OperatorAction, ...]:
        """Plan every action valid right now for *actor* — each carrying
        the four facts and the CAS ticket (the projection's version)."""
        moment = at if at is not None else datetime.now(timezone.utc)
        when = _iso(moment)
        trace = linkage or f"run:{projection.run_id}"
        return tuple(
            OperatorAction(
                action=action,
                state=projection.state,
                actor_role=actor_role,
                actor=actor,
                digest=projection.action_digest,
                at=when,
                linkage=f"{trace} projection:v{projection.projection_version}",
                expected_version=projection.projection_version,
                via=ACTION_VIA[action],
            )
            for action in RecoveryActions.valid_for(projection.state, actor_role)
        )

    @staticmethod
    def decide(action: OperatorAction, current: OperatorProjection) -> ActionDecision:
        """Check one action against the CURRENT projection.

        The stale-status-comment case: an action whose ``expected_version``
        is older than the current projection was decided against state
        that has since moved — refused, with the current state and the
        safe next action named so the operator re-decides against the
        world as it now is. A version AHEAD of the stored truth is a bug
        and refused as such. A fresh version whose action is no longer
        valid in the current state (or for the actor's role) is refused
        the same honest way.
        """
        actions_now = RecoveryActions.valid_for(current.state, action.actor_role)
        safe_next = actions_now[0] if actions_now else ""
        if action.expected_version < current.projection_version:
            return ActionDecision(
                allowed=False,
                reason=(
                    f"stale command: computed against projection v{action.expected_version} "
                    f"({action.state!r}) but v{current.projection_version} is current — "
                    f"current state is {current.state!r}"
                ),
                current_state=current.state,
                safe_next_action=safe_next,
                action=action,
            )
        if action.expected_version > current.projection_version:
            return ActionDecision(
                allowed=False,
                reason=(
                    f"expected_version {action.expected_version} is ahead of the stored "
                    f"projection v{current.projection_version} — commands may not claim a "
                    "future state"
                ),
                current_state=current.state,
                safe_next_action=safe_next,
                action=action,
            )
        if action.action not in actions_now:
            return ActionDecision(
                allowed=False,
                reason=(
                    f"{action.action!r} is not valid in state {current.state!r} for role "
                    f"{action.actor_role!r} (it was planned for {action.state!r})"
                ),
                current_state=current.state,
                safe_next_action=safe_next,
                action=action,
            )
        return ActionDecision(
            allowed=True,
            reason=f"{action.action!r} is valid in {current.state!r} at v{current.projection_version}",
            current_state=current.state,
            safe_next_action=safe_next,
            action=action,
        )
