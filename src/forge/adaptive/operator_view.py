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
- :func:`explain_blocked` — the typed, evidence-linked diagnostics
  (R37-16): why a run waits as a closed vocabulary of outcome codes
  (:data:`BLOCKED_CODES`), each with a one-line explanation, a thin link
  to the exact proving row and the SAFE next action — never a generic
  retry suggestion for the non-retryable conditions.

Row shapes (the documented mapping — hand-built fixtures and durable rows
alike normalize through ``operator_timeline._view_of``, so plain dicts,
dataclasses and pydantic contracts all read):

- ``run`` — a ``flow_runs``-shaped mapping: ``id``, ``status``,
  ``base_sha``, ``candidate_shas`` (a HISTORY list — the CURRENT
  candidate is the row's ``active_candidate_sha`` pointer when it
  records one, else the LAST member), ``plan_digest``, ``evidence``,
  ``blocked_reason``, ``cancel_requested``, ``updated_at``;
- ``attempts`` — attempt rows: ``attempt_id``/``id``, ``status``
  (``executing`` / ``succeeded`` / ``failed`` / ``cancelled`` /
  ``accepted``; absent → unknown, never guessed), ``started_at``,
  ``updated_at``, ``generation`` (the attempt's OWN record's
  generation — ``"unknown"`` when not recorded, R37-03);
- ``commands`` — control-command rows (the mailbox/router shapes):
  ``command_id``, ``kind`` (``pause``/``resume``/``steer``/``answer``/…),
  ``status`` (the ladder ``received → authorized → dispatching →
  vendor_accepted/outcome_unknown → applied → checkpointed`` | rejected |
  expired), ``sequence``, ``actor_ref``, ``created_at``;
- ``checkpoints`` — checkpoint rows: ``checkpoint_id``/``id``,
  ``digest``, ``committed_at``, ``activated_at`` (the resume proof: the
  bytes were ACTIVATED by the resume command that named THIS
  checkpoint — an applied resume naming a different one leaves it
  ``None`` with ``activation: "unmatched-command"``, R37-03), ``fence``
  (``held``/``cleared``; empty means the router's pause-booking pairing
  holds it), ``sequence``;
- ``verifications`` — verification rows: ``verification_id``/``id``,
  ``result`` (``passed``/``failed``/``unknown``), ``at``, ``candidate_sha``.
  A ``passed`` row binds to the CURRENT candidate: when it names a
  ``candidate_sha`` that is not among the run's ``candidate_shas`` it is
  an OLD green verdict about a DIFFERENT candidate and decorates nothing
  (R36-15 — the binding is asserted by tests); a row naming the CURRENT
  candidate (or naming none) is current readiness; a row naming a
  NON-current member renders ``historical_pass`` in the history section,
  never current readiness (R37-03);
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
import re
from collections.abc import Mapping, Sequence
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
    "BLOCKED_CODES",
    "BlockedReason",
    "DELIVERY_FAILED_OUTCOMES",
    "DELIVERY_OUTCOMES",
    "DELIVERY_VIEW_SCHEMA",
    "DIAGNOSTIC_MAX_ENTRIES",
    "DIAGNOSTIC_SECTION_FIELDS",
    "DIAGNOSTICS_SCHEMA",
    "DeliveryOutcome",
    "FEEDBACK_REFUSAL_NEXT_ACTIONS",
    "NON_RETRYABLE_CODES",
    "OperatorAction",
    "OperatorProjection",
    "OPERATOR_STATES",
    "OPERATOR_VIEW_SCHEMA",
    "OperatorState",
    "RECOVERY_MILESTONES",
    "RECOVERY_SCHEMA",
    "REVIEW_ROUND_OPEN_STATUSES",
    "REVIEW_ROUND_SUPERSEDING_STATUSES",
    "RecoveryActions",
    "RecoveryHint",
    "SAFE_ACTION_DESCRIPTIONS",
    "STALE_ACTION_REFUSAL",
    "STATUS_COMMENT_SCHEMA",
    "StateDerivation",
    "StaleProjectionRejected",
    "UNRESOLVED_PUBLICATION_STATUSES",
    "WEDGED_AFTER",
    "action_hint_block",
    "apply_update",
    "budget_blocked_review",
    "current_candidate",
    "delivery_outcome_of",
    "delivery_view",
    "derive_state",
    "export_diagnostics",
    "explain_blocked",
    "initial_projection",
    "recovery_document",
    "recovery_hint",
    "recovery_ladder",
    "render",
    "render_status_comment",
    "review_round_fact",
    "source_digest",
    "status_comment_identity",
    "status_note_lines",
    "verification_binding",
    "with_status_comment_identity",
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


def current_candidate(run: Mapping[str, Any], candidate_shas: Sequence[Any]) -> str:
    """The run's CURRENT candidate (R37-03) — the row's explicit
    ``active_candidate_sha`` pointer when it records one, else the LAST
    ``candidate_shas`` member (the append order the services write: each
    repair cycle appends). NEVER the first member by accident — the list
    is a HISTORY, and an old member's green verdict is history, not
    current readiness."""
    active = run.get("active_candidate_sha")
    if isinstance(active, str) and active.strip():
        return active.strip()
    entries = [str(sha) for sha in candidate_shas if str(sha or "").strip()]
    return entries[-1] if entries else ""


def verification_binding(
    verifications: Sequence[Mapping[str, Any]],
    candidate_shas: Sequence[Any],
    current: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split passed verifications into (current, historical) bindings.

    A ``passed`` row binds to the CURRENT candidate only when it names
    it (or names no candidate at all — the hand-built fixture shape that
    binds to whatever the run holds). A pass naming a NON-current
    ``candidate_shas`` member is an OLD green verdict about an earlier
    artifact: it renders as ``historical_pass`` (a history entry), never
    as current readiness. A pass naming something outside the run's
    candidates entirely decorates nothing at all (the R36-15 pin).
    """
    members = {str(sha) for sha in candidate_shas}
    current_rows: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    for row in verifications:
        if str(_first(row, "result", "outcome") or "") not in _VERIFICATION_PASSED:
            continue
        named = str(row.get("candidate_sha") or "")
        if named in ("", current):
            current_rows.append(dict(row))
        elif named in members:
            historical.append(
                {
                    "verification_id": str(_first(row, "verification_id", "id") or ""),
                    "candidate_sha": named,
                    "at": _iso(row.get("at")),
                    "verdict": "historical_pass",
                }
            )
    return current_rows, historical


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
    raw_evidence = run.get("evidence")
    run_evidence: Mapping[str, Any] = raw_evidence if isinstance(raw_evidence, Mapping) else {}
    if not candidate_shas and isinstance(_first(run_evidence, "candidate_sha"), str):
        candidate_shas = [run_evidence["candidate_sha"]]
    # R36-15/#274: a passed verification DECORATES only the candidate it
    # tested. R37-03 tightens the binding to the CURRENT candidate (the
    # row's active pointer, else the LAST member — the list is history):
    # a pass naming a NON-current member is an old green verdict about an
    # earlier artifact — it renders as a historical pass, never current
    # readiness. A row naming no candidate binds to whatever candidate
    # the run holds (the hand-built fixture shape).
    candidate_now = current_candidate(run, candidate_shas)
    verifying, historical_passes = verification_binding(
        verifications, candidate_shas, candidate_now
    )
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
        link("resume command", _first(resume or {}, "command_id"), resume)
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
            f"candidate {candidate_now[:12]} exists, verification passed for that candidate"
        )
    elif candidate_shas:
        state = "unverified"
        link("run", run_id, run)
        reasons.append(f"candidate {candidate_now[:12]} exists, no passed verification")
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
    verification_history: tuple[dict[str, Any], ...] = ()
    #: R40-13 (#349): the CURRENT delivery/round subject the projection's
    #: actions name — ``delivery:1`` (the original delivery) or
    #: ``round:<n>:<decision_id>`` (a linked follow-up round, whether this
    #: run is its parent or its child). The action-versioning ticket an
    #: offered action carries as ``expected_round``; ``""`` when no round
    #: row and no evidence names one (hand-built rows without rounds).
    round_ref: str = "delivery:1"
    #: The round that SUPERSEDED this run's delivery (``""`` when none):
    #: a round row naming this run as parent in a superseding status. The
    #: render labels the run's ready delivery and its green evidence
    #: HISTORICAL through this field — never as current readiness.
    superseded_by_round: str = ""

    @property
    def action_digest(self) -> str:
        """The audit four-facts WHAT — the exact artifact a decision about
        this projection refers to: the CURRENT candidate (the active
        pointer, else the newest list member — never an old member by
        accident), else the checkpoint digest, else the plan digest, else
        the whole source digest."""
        current = self.identity.get("active_candidate")
        if current:
            return str(current)
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
    verifications = [_norm(row) for row in source_rows.get("verifications") or []]
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
    candidate_history = list(run.get("candidate_shas") or [])
    candidate_now = current_candidate(run, candidate_history)
    identity = redact(
        {
            "source_sha": str(run.get("base_sha") or ""),
            "candidate_shas": candidate_history,
            "active_candidate": candidate_now,
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
    _, historical_passes = verification_binding(verifications, candidate_history, candidate_now)
    round_view = review_round_fact(source_rows)
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
        verification_history=tuple(redact(entry) for entry in historical_passes),
        round_ref=round_view["ref"] if round_view else "delivery:1",
        superseded_by_round=str(round_view.get("supersedes", "") or "") if round_view else "",
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
            "verification_history": [dict(entry) for entry in projection.verification_history],
            "unresolved_effects": [dict(effect) for effect in projection.unresolved_effects],
            "evidence": [dict(link) for link in projection.evidence],
            "derivation": list(projection.reasons),
            "last_transition_at": projection.last_transition_at,
            "computed_at": projection.computed_at,
            "rows_observed": dict(projection.rows_observed),
            # R40-13 (#349): the delivery/round subject + the supersession —
            # the fields the round-aware surfaces (the API's delivery
            # section, the status comment) agree through.
            "round_ref": projection.round_ref,
            "superseded_by_round": projection.superseded_by_round,
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
    current = identity.get("active_candidate") or (candidates[-1] if candidates else "")
    if current:
        detail.append(f"candidate {_short(current)}")
    if candidates and len(candidates) > 1:
        detail.append(f"{len(candidates) - 1} earlier candidate(s) in history")
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
    # R40-13 (#349): the round line — which delivery/round is CURRENT.
    # A superseded delivery says so OUT LOUD (the old ready evidence is
    # history, never current readiness).
    if projection.superseded_by_round:
        lines.append(
            f"Round: delivery superseded by {projection.superseded_by_round} — "
            "the green evidence above is HISTORICAL."
        )
    elif projection.round_ref and projection.round_ref != "delivery:1":
        lines.append(f"Round: {projection.round_ref} is the current delivery.")
    if projection.last_transition_at:
        lines.append(f"Last transition: {projection.last_transition_at}")
    return lines


# ---------------------------------------------------------------------------
# Blocked-reason explanations (R37-16) — typed, evidence-linked, non-generic
# ---------------------------------------------------------------------------

#: The typed blocked-reason outcome codes. Every code names an OBSERVED
#: condition (a durable row or an honesty mark), never a guess, and each
#: carries its own safe next action — a non-retryable condition NEVER
#: suggests retry (a revoked credential cannot be retried into validity,
#: an uncertain effect cannot be retried around, a lost checkpoint
#: cannot be retried into existence).
BLOCKED_CODES: Final[tuple[str, ...]] = (
    "revoked_authority",
    "uncertain_native_effect",
    "required_checkpoint_loss",
    "capacity_wait",
    "verification_stale",
)

#: The codes whose safe next action is NEVER retry — retrying these is
#: exactly the drive-by write the operator console exists to prevent.
NON_RETRYABLE_CODES: Final[frozenset[str]] = frozenset(
    {"revoked_authority", "uncertain_native_effect", "required_checkpoint_loss"}
)

#: The authority-refusal spellings a run's ``blocked_reason`` (the run
#: row's ``status_reason``) may carry for a revoked/refused authority —
#: the typed outcome the provider surfaces (401/403 wordings, revoked or
#: expired credentials, suspended permissions).
_REVOKED_AUTHORITY_RE: Final[re.Pattern[str]] = re.compile(
    r"revoked|suspended|unauthori[sz]ed|forbidden|permission(?:s)? denied"
    r"|(?:credential|token|key|secret)\s+[a-z]*\s*(?:rejected|invalid|expired|revoked)"
    r"|\b(?:401|403)\b",
    re.IGNORECASE,
)

#: The capacity/admission spellings a ``blocked_reason`` may carry while
#: work waits for a slot (the bounded-admission refusals and provider
#: throttling wordings).
_CAPACITY_WAIT_RE: Final[re.Pattern[str]] = re.compile(
    r"queue|capacity|slot|admission|concurren|throttl|saturation|rate.?limit",
    re.IGNORECASE,
)

#: The occupancy words that mean "capacity is held and UNCERTAIN" — the
#: leases whose release needs evidence (the reconciler's probe), never a
#: timer. ``native_running`` is healthy occupancy, not a blockage.
_UNCERTAIN_OCCUPANCY: Final[frozenset[str]] = frozenset({"dispatched_unknown", "draining"})

#: The safe next actions the explanations name — runbook verbs or the
#: read-only probe, each linking to where it is executed (a guarded
#: command route or a runbook document, never a new mutation path here).
_SUGGESTED_VIA: Final[Mapping[str, str]] = {
    "rotate_or_rebind_credential": "runbook:token-rotation",
    "reconcile": "operator-commands:/reconcile",
    "restore_checkpoint_or_retire": "runbook:backup-restore",
    "wait_for_reconciler": "read-only:/status",
    "verify_current_candidate": "read-only:/status",
    "probe": "read-only:/status",
}


@dataclass(frozen=True)
class BlockedReason:
    """One typed explanation of why a run waits (R37-16).

    ``code`` is the closed :data:`BLOCKED_CODES` outcome; ``explanation``
    is the one-line human sentence; ``evidence`` is the THIN link to the
    exact row that proves it (``{"of", "id", "ref"}`` — which section,
    which row, its digest — never the payload); ``suggested_action`` is
    the safe next action with ``via`` naming the guarded route or
    runbook that executes it; ``retryable`` says whether a retry could
    ever help (``False`` for the non-retryable conditions — their
    suggested action is never retry).
    """

    code: str
    explanation: str
    evidence: dict[str, str]
    suggested_action: str
    via: str
    retryable: bool

    def as_document(self) -> dict[str, Any]:
        """The redacted render of one reason (the diagnostics surface)."""
        return redact(
            {
                "code": self.code,
                "explanation": self.explanation,
                "evidence": dict(self.evidence),
                "suggested_action": self.suggested_action,
                "via": self.via,
                "retryable": self.retryable,
            }
        )


def _age_seconds(at: str, now: str) -> float | None:
    """Seconds between *now* and the ISO *at* — ``None`` when either
    carries no readable clock (never a synthesized age)."""
    moment = _as_datetime(at)
    reference = _as_datetime(now)
    if moment is None or reference is None:
        return None
    return max(0.0, (reference - moment).total_seconds())


def explain_blocked(
    projection: OperatorProjection,
    *,
    coverage: Mapping[str, str] | None = None,
    occupancy: Sequence[Mapping[str, Any]] | None = None,
    checkpoints: Sequence[Mapping[str, Any]] | None = None,
) -> list[BlockedReason]:
    """The typed, evidence-linked explanations of why *projection* waits.

    Each reason is derived from an OBSERVED fact on the projection — the
    run row's blocked reason (an authority refusal), an unresolved
    external effect, a paused run whose checkpoint authority reads
    ``missing``/``unknown`` (the honesty marks a live snapshot carries;
    pass them through *coverage*, with the *checkpoints* section rows so
    a HELD fence is visible), an execution lease holding capacity with
    uncertain occupancy (*occupancy*, the snapshot's lease slice), or a
    green verdict that names an earlier candidate — and links to the
    EXACT evidence row (its id/digest). Non-retryable conditions name
    their real remediation (rotate the credential, reconcile the effect,
    restore the checkpoint), never a generic "try retrying".

    Pure: reads the projection (and the optional honesty slices), writes
    nothing, executes nothing.
    """
    reasons: list[BlockedReason] = []
    run_link = next(
        (dict(link) for link in projection.evidence if link.get("of") == "run"),
        {
            "of": "run",
            "id": projection.run_id,
            "ref": _digest_json(
                {"run_id": projection.run_id, "blocked_reason": projection.blocked_reason}
            ),
        },
    )
    blocked_reason = str(projection.blocked_reason or "")

    # 1. a revoked / refused authority — the credential or permission the
    #    run depends on was withdrawn; retrying cannot restore it.
    if blocked_reason and _REVOKED_AUTHORITY_RE.search(blocked_reason):
        reasons.append(
            BlockedReason(
                code="revoked_authority",
                explanation=(
                    "the run is blocked by a revoked or refused authority "
                    f"({blocked_reason}) — a retry cannot restore withdrawn authority"
                ),
                evidence=run_link,
                suggested_action="rotate_or_rebind_credential",
                via=_SUGGESTED_VIA["rotate_or_rebind_credential"],
                retryable=False,
            )
        )

    # 2. uncertain external effects — an effect whose landing is unproven
    #    blocks any safe retry until it is determined.
    for effect in projection.unresolved_effects:
        effect_view = dict(effect)
        reasons.append(
            BlockedReason(
                code="uncertain_native_effect",
                explanation=(
                    f"external effect {effect_view.get('operation_key', '')} "
                    f"({effect_view.get('operation', '')} on "
                    f"{effect_view.get('target_ref', '')}) is "
                    f"{effect_view.get('status', '')} — its landing is unproven; "
                    "determine it before any retry"
                ),
                evidence={
                    "of": "publication",
                    "id": str(effect_view.get("operation_key", "")),
                    "ref": _digest_json(effect_view),
                },
                suggested_action="reconcile",
                via=_SUGGESTED_VIA["reconcile"],
                retryable=False,
            )
        )

    # 3. required-checkpoint loss — the run stands paused (a held fence or
    #    a safely_paused derivation) on a checkpoint the authority no
    #    longer holds (the coverage honesty marks from the snapshot).
    fence_held = any(str(row.get("fence") or "") == "held" for row in checkpoints or ())
    if (
        coverage is not None
        and str(coverage.get("checkpoints", "")) in ("missing", "unknown")
        and (projection.underlying_state == "safely_paused" or fence_held)
    ):
        checkpoint_id = str(projection.identity.get("checkpoint_id") or "")
        reasons.append(
            BlockedReason(
                code="required_checkpoint_loss",
                explanation=(
                    f"the run is paused on checkpoint {checkpoint_id or '(none named)'} "
                    f"but the checkpoint authority reads "
                    f"{coverage.get('checkpoints')!r} — a resume has no bytes to restore"
                ),
                evidence={
                    "of": "checkpoint",
                    "id": checkpoint_id,
                    "ref": str(projection.identity.get("checkpoint_digest") or "")
                    or _digest_json({"coverage": coverage.get("checkpoints")}),
                },
                suggested_action="restore_checkpoint_or_retire",
                via=_SUGGESTED_VIA["restore_checkpoint_or_retire"],
                retryable=False,
            )
        )

    # 4. capacity wait — a lease holds capacity with UNCERTAIN occupancy
    #    (the reconciler must prove it free), or the blocked reason names
    #    the bounded-admission wait.
    for row in occupancy or ():
        word = str(row.get("occupancy") or "")
        if word not in _UNCERTAIN_OCCUPANCY:
            continue
        age = _age_seconds(str(row.get("acquired_at") or ""), projection.computed_at)
        lease_link = {
            "of": "lease",
            "id": str(row.get("lease_id", "")),
            "ref": _digest_json(dict(row)),
        }
        reasons.append(
            BlockedReason(
                code="capacity_wait",
                explanation=(
                    f"execution capacity is held by lease {row.get('lease_id', '')} in "
                    f"state {word}"
                    + (f" for {int(age)}s" if age is not None else "")
                    + " — the slot frees only from evidence (the reconciler's probe)"
                ),
                evidence=lease_link,
                suggested_action="wait_for_reconciler",
                via=_SUGGESTED_VIA["wait_for_reconciler"],
                retryable=True,
            )
        )
    if (
        blocked_reason
        and _CAPACITY_WAIT_RE.search(blocked_reason)
        and not any(reason.code == "capacity_wait" for reason in reasons)
    ):
        reasons.append(
            BlockedReason(
                code="capacity_wait",
                explanation=(
                    f"the run waits for execution capacity ({blocked_reason}) — "
                    "admission is bounded by policy, not stuck"
                ),
                evidence=run_link,
                suggested_action="wait_for_reconciler",
                via=_SUGGESTED_VIA["wait_for_reconciler"],
                retryable=True,
            )
        )

    # 5. verification stale — the only green verdict names an earlier
    #    candidate; the CURRENT candidate is unverified.
    if projection.state == "unverified" and projection.verification_history:
        current = str(projection.identity.get("active_candidate") or "")
        for entry in projection.verification_history:
            named = str(entry.get("candidate_sha") or "")
            reasons.append(
                BlockedReason(
                    code="verification_stale",
                    explanation=(
                        f"the passing verification covers candidate {named[:12]}, not the "
                        f"current {current[:12]} — the current candidate is unverified"
                    ),
                    evidence={
                        "of": "verification",
                        "id": str(entry.get("verification_id", "")),
                        "ref": _digest_json(dict(entry)),
                    },
                    suggested_action="verify_current_candidate",
                    via=_SUGGESTED_VIA["verify_current_candidate"],
                    retryable=True,
                )
            )

    return reasons


# ---------------------------------------------------------------------------
# The recovery surface (R38-15) — delivery outcome, the five-milestone
# ladder, advisory recovery hints
# ---------------------------------------------------------------------------

#: The closed delivery-outcome vocabulary (R38-15): what the CURRENT
#: attempt actually delivered, derived from the recorded candidate meta /
#: collector exit + driver exit (the #302 finalization markers) — never
#: from a successful SDK turn. ``empty_diff_no_effect`` is the issue's
#: headline: a resumed/executing attempt whose collected candidate was
#: ZERO-CHANGE is a FAILED/no-effect delivery, never a successful resume
#: of useful work.
DELIVERY_OUTCOMES: Final[tuple[str, ...]] = (
    "delivered",
    "empty_diff_no_effect",
    "collection_failed",
    "not_collected_yet",
    "driver_failed",
)

#: The outcomes that are FAILED deliveries — a render carrying one of
#: these NEVER presents the run as a successful resume (the R38-15
#: acceptance arm: an empty resumed diff showed as a "successful resume"
#: shape in the live run).
DELIVERY_FAILED_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"empty_diff_no_effect", "collection_failed", "driver_failed"}
)

#: The five recovery milestones (R38-15), each displayed INDEPENDENTLY —
#: present / absent / unknown per milestone, never collapsed into one
#: boolean. The live failure was exactly the collapse: operators could not
#: tell a requested pause from a verified checkpoint, a stopped runner, an
#: authorized resume or an APPLIED exact resume.
RECOVERY_MILESTONES: Final[tuple[str, ...]] = (
    "pause_requested",
    "checkpoint_committed",
    "runner_stopped",
    "resume_authorized",
    "exact_resume_applied",
)

#: The recovery document's schema discriminator (versioned like the view's).
RECOVERY_SCHEMA: Final = "forge.operator.recovery/1"

#: The #302 finalization marker's ``candidate_state`` spellings
#: (``FORGE_LANE_OUTCOME:{"driver_exit": …, "collector_exit": …,
#: "candidate_state": …}``) mapped onto the delivery-outcome vocabulary —
#: the recorded classification wins over every derived fallback because the
#: lane already reconciled driver and collector into one honest word.
_CANDIDATE_STATE_OUTCOMES: Final[Mapping[str, str]] = {
    "candidate": "delivered",
    "zero_change": "empty_diff_no_effect",
    "collection_failed": "collection_failed",
    "driver_failed": "driver_failed",
}

#: The run-row blocked-reason wordings that prove a failed DELIVERY when no
#: lane-outcome marker was journaled (the harness backend's typed codes at
#: ``status_reason``): an empty repair, a missing/invalid artifact, a
#: failed driver.
_NO_EFFECT_RE: Final[re.Pattern[str]] = re.compile(
    r"no[_-]?effect|no[_-]?changes|zero[_-]?change", re.IGNORECASE
)
_COLLECTION_FAILED_RE: Final[re.Pattern[str]] = re.compile(
    r"artifact[_-]?missing|candidate[_-]?invalid|collection[_-]?failed", re.IGNORECASE
)
_DRIVER_FAILED_RE: Final[re.Pattern[str]] = re.compile(r"driver[_-]?failed", re.IGNORECASE)


@dataclass(frozen=True)
class DeliveryOutcome:
    """What the current attempt actually delivered, and from what evidence.

    ``outcome`` is the closed :data:`DELIVERY_OUTCOMES` vocabulary;
    ``reason`` names WHERE the derivation read it (the recorded
    ``candidate_state``, the driver exit, the run row's typed blocked
    reason, or the collected candidates — the honest "nothing recorded");
    ``evidence`` is the thin link to the proving row. ``failed`` says
    whether this is a FAILED delivery — the render side of the R38-15
    acceptance: such a run never displays as a successful resume.
    """

    outcome: str
    reason: str
    evidence: dict[str, str]

    @property
    def failed(self) -> bool:
        return self.outcome in DELIVERY_FAILED_OUTCOMES


def delivery_outcome_of(source_rows: Mapping[str, Any]) -> DeliveryOutcome:
    """Derive the delivery outcome from the rows already recorded (pure).

    Priority, each rung naming its evidence in ``reason``:

    1. the recorded ``candidate_state`` — the #302 marker's classification
       where it lands in evidence (the reader-mapped ``lane_outcome``
       slice, the harness fragment, or the top-level spelling);
    2. a recorded ``driver_exit`` that is not ``completed``;
    3. the run row's typed blocked reason (``repair_no_effect`` /
       ``harness_no_changes`` → no effect; ``harness_artifact_missing`` /
       ``harness_candidate_invalid`` → collection failed;
       ``harness_driver_failed`` → driver failed);
    4. collected candidates on the run row → ``delivered``;
    5. otherwise ``not_collected_yet`` — an honest "no delivery recorded",
       never a guessed success.
    """
    run = _require_run(source_rows)
    run_id = str(_first(run, "id", "run_id") or "")
    link = {"of": "run", "id": run_id, "ref": _row_digest(run)}
    blocked = str(run.get("blocked_reason") or "")

    lane_view = run.get("lane_outcome")
    lane: Mapping[str, Any] = lane_view if isinstance(lane_view, Mapping) else {}
    raw_run_evidence = run.get("evidence")
    run_evidence: Mapping[str, Any] = (
        raw_run_evidence if isinstance(raw_run_evidence, Mapping) else {}
    )
    raw_harness = run_evidence.get("harness")
    evidence_lane: Mapping[str, Any] = raw_harness if isinstance(raw_harness, Mapping) else {}

    def _recorded(key: str) -> Any:
        for source in (lane, evidence_lane, run_evidence):
            value = source.get(key)
            if value not in (None, ""):
                return value
        return None

    candidate_state = str(_recorded("candidate_state") or "").strip()
    if candidate_state in _CANDIDATE_STATE_OUTCOMES:
        return DeliveryOutcome(
            _CANDIDATE_STATE_OUTCOMES[candidate_state],
            f"harness candidate_state={candidate_state}",
            link,
        )
    driver_exit = str(_recorded("driver_exit") or "").strip()
    if driver_exit and driver_exit != "completed":
        return DeliveryOutcome("driver_failed", f"driver_exit={driver_exit}", link)
    if blocked:
        if _NO_EFFECT_RE.search(blocked):
            return DeliveryOutcome("empty_diff_no_effect", f"run blocked: {blocked}", link)
        if _COLLECTION_FAILED_RE.search(blocked):
            return DeliveryOutcome("collection_failed", f"run blocked: {blocked}", link)
        if _DRIVER_FAILED_RE.search(blocked):
            return DeliveryOutcome("driver_failed", f"run blocked: {blocked}", link)
    candidates = [str(sha) for sha in run.get("candidate_shas") or [] if str(sha or "").strip()]
    if candidates:
        return DeliveryOutcome("delivered", f"collected candidates: {len(candidates)}", link)
    return DeliveryOutcome("not_collected_yet", "no recorded lane outcome", link)


def _section_observed(
    coverage: Mapping[str, str] | None, section: str, source_rows: Mapping[str, Any]
) -> bool:
    """Whether *section*'s authority was OBSERVED for these rows — the
    coverage map's word when given (``unknown`` or absent → not observed),
    else whether the rows carry the section at all (a hand-built rows
    document that omits the section never read it)."""
    if coverage is not None:
        return str(coverage.get(section, "") or "") in ("present", "missing")
    return section in source_rows


def _milestone(
    name: str,
    status: str,
    *,
    of: str = "",
    ident: Any = None,
    at: Any = None,
    row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One milestone's display row: status + (when present) the thin
    evidence link and the moment it was observed."""
    entry: dict[str, Any] = {"milestone": name, "status": status, "evidence": None, "at": ""}
    if status == "present" and row is not None:
        entry["evidence"] = {"of": of, "id": str(ident or ""), "ref": _row_digest(row)}
        entry["at"] = _iso(at) if at else ""
    return entry


def recovery_ladder(
    source_rows: Mapping[str, Any],
    *,
    coverage: Mapping[str, str] | None = None,
    occupancy: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """The five-milestone pause/resume ladder, each INDEPENDENT (R38-15).

    - ``pause_requested`` — the pause COMMAND row (any rung: the request
      itself is the milestone);
    - ``checkpoint_committed`` — the verified checkpoint (its id + digest);
    - ``runner_stopped`` — the native job's terminal observation (a
      released execution lease — the reconciler's probe, never a timer);
    - ``resume_authorized`` — the resume DECISION row (a resume command
      that was not refused on the ladder);
    - ``exact_resume_applied`` — the activation receipt from the R37-03
      matching: an applied resume whose recorded ``checkpoint_ref`` named
      THIS checkpoint (``activation: "matched"``).

    Each is ``present`` (with its thin evidence link), ``absent`` (the
    authority was observed and holds no such row) or ``unknown`` (the
    section was never queried or its authority was unreachable — a
    checkpoint outage is UNKNOWN, never "no checkpoint").
    """
    commands = [_norm(row) for row in source_rows.get("commands") or []]
    checkpoints = [_norm(row) for row in source_rows.get("checkpoints") or []]
    commands_observed = _section_observed(coverage, "commands", source_rows)
    checkpoints_observed = _section_observed(coverage, "checkpoints", source_rows)

    ladder: dict[str, dict[str, Any]] = {}

    # pause_requested — the command row
    pause = _latest_of(commands, "pause")
    if not commands_observed:
        ladder["pause_requested"] = _milestone("pause_requested", "unknown")
    elif pause is not None:
        ladder["pause_requested"] = _milestone(
            "pause_requested",
            "present",
            of="pause command",
            ident=_first(pause, "command_id", "id"),
            at=pause.get("created_at"),
            row=pause,
        )
    else:
        ladder["pause_requested"] = _milestone("pause_requested", "absent")

    # checkpoint_committed — the verified checkpoint id + digest
    committed = [
        cp
        for cp in checkpoints
        if _first(cp, "committed_at") or str(_first(cp, "status") or "") == "committed"
    ]
    checkpoint = committed[-1] if committed else None
    if not checkpoints_observed:
        ladder["checkpoint_committed"] = _milestone("checkpoint_committed", "unknown")
    elif checkpoint is not None:
        ladder["checkpoint_committed"] = _milestone(
            "checkpoint_committed",
            "present",
            of="checkpoint",
            ident=_first(checkpoint, "checkpoint_id", "id"),
            at=checkpoint.get("committed_at"),
            row=checkpoint,
        )
    else:
        ladder["checkpoint_committed"] = _milestone("checkpoint_committed", "absent")

    # runner_stopped — the native job's terminal observation (a released
    # lease). The occupancy slice is observed when the caller PASSED it and
    # the coverage map (when any) says its authority was queried — an
    # unselected occupancy section reads unknown, never "no terminal
    # observation".
    occupancy_observed = occupancy is not None and (
        coverage is None or str(coverage.get("occupancy", "") or "") in ("present", "missing")
    )
    if not occupancy_observed:
        ladder["runner_stopped"] = _milestone("runner_stopped", "unknown")
    else:
        observed_occupancy: Sequence[Mapping[str, Any]] = occupancy or ()
        stopped = [row for row in observed_occupancy if str(row.get("released_at") or "")]
        if stopped:
            latest = stopped[-1]
            ladder["runner_stopped"] = _milestone(
                "runner_stopped",
                "present",
                of="lease",
                ident=latest.get("lease_id"),
                at=latest.get("released_at"),
                row=latest,
            )
        else:
            ladder["runner_stopped"] = _milestone("runner_stopped", "absent")

    # resume_authorized — the decision row (a resume not refused on the ladder)
    resume = _latest_of(commands, "resume")
    resume_status = str(_first(resume, "status") or "") if resume else ""
    if not commands_observed:
        ladder["resume_authorized"] = _milestone("resume_authorized", "unknown")
    elif resume is not None and resume_status not in _COMMAND_REFUSED:
        ladder["resume_authorized"] = _milestone(
            "resume_authorized",
            "present",
            of="resume command",
            ident=_first(resume, "command_id", "id"),
            at=resume.get("created_at"),
            row=resume,
        )
    else:  # no resume row, or one refused (rejected/expired) — never authorized
        ladder["resume_authorized"] = _milestone("resume_authorized", "absent")

    # exact_resume_applied — the R37-03 activation receipt (matched command)
    applied = [
        cp
        for cp in checkpoints
        if _first(cp, "activated_at") is not None
        and str(cp.get("activation") or "") != "unmatched-command"
    ]
    if not checkpoints_observed:
        ladder["exact_resume_applied"] = _milestone("exact_resume_applied", "unknown")
    elif applied:
        receipt = applied[-1]
        ladder["exact_resume_applied"] = _milestone(
            "exact_resume_applied",
            "present",
            of="checkpoint",
            ident=_first(receipt, "checkpoint_id", "id"),
            at=receipt.get("activated_at"),
            row=receipt,
        )
    else:
        ladder["exact_resume_applied"] = _milestone("exact_resume_applied", "absent")

    return ladder


@dataclass(frozen=True)
class RecoveryHint:
    """One ADVISORY recovery guidance (R38-15): what is safe next, in human
    words, with the EXACT command shapes and the guarded route each one
    executes through. Hints never authorize anything — the guarded routes
    revalidate authority and the current world at execution time.

    ``retryable`` is ``False`` exactly for the non-retryable conditions
    (the R37-16/#297 codes): a revoked authority is rotated or reconciled,
    never retried.
    """

    advisory: str
    commands: tuple[dict[str, str], ...]
    retryable: bool

    def as_document(self) -> dict[str, Any]:
        """The redacted render of one hint (the recovery surface)."""
        return redact(
            {
                "advisory": self.advisory,
                "commands": [dict(command) for command in self.commands],
                "retryable": self.retryable,
            }
        )


def recovery_hint(
    state: str,
    delivery_outcome: str,
    *,
    blocked_reason: str = "",
) -> RecoveryHint | None:
    """The advisory guidance for one state × delivery outcome (pure).

    Priority: a revoked/stale authority (the #297 non-retryable condition
    — rotate or reconcile, NEVER retry) outranks the delivery outcome.
    Then the outcome speaks: an ``empty_diff_no_effect`` resume names BOTH
    escapes with their exact shapes (re-issue the guidance, or restart
    from the verified checkpoint); a failed collection names the typed
    error and the rerun path; a not-yet-collected attempt says probe.
    ``None`` only where nothing needs recovering (a delivered candidate
    that is verified or accepted).

    Unknown states and outcomes fail visibly — least-authority guessing is
    how consoles drift into drive-by writes.
    """
    if state not in OPERATOR_STATES:
        raise ValueError(f"unknown operator state: {state!r}")
    if delivery_outcome not in DELIVERY_OUTCOMES:
        raise ValueError(f"unknown delivery outcome: {delivery_outcome!r}")
    blocked = str(blocked_reason or "")
    commands = _RECOVERY_COMMANDS

    if blocked and _REVOKED_AUTHORITY_RE.search(blocked):
        return RecoveryHint(
            advisory=(
                f"the run's authority is revoked or stale ({blocked}) — rotate the credential "
                "or reconcile the access before any rerun; a retry cannot restore withdrawn "
                "authority"
            ),
            commands=(commands["rotate"], commands["reconcile"], commands["probe"]),
            retryable=False,
        )

    if delivery_outcome == "empty_diff_no_effect":
        if state == "resumed":
            return RecoveryHint(
                advisory=(
                    "the resume delivered NO changes — a failed/no-effect delivery, not a "
                    "successful resume: re-issue the task guidance or restart from the "
                    "verified checkpoint"
                ),
                commands=(commands["steer"], commands["resume"], commands["probe"]),
                retryable=True,
            )
        if state == "safely_paused":
            return RecoveryHint(
                advisory=(
                    "the last attempt delivered a zero-change candidate — restart from the "
                    "verified checkpoint, or re-issue the guidance once resumed"
                ),
                commands=(commands["resume"], commands["probe"]),
                retryable=True,
            )
        if state in ("executing", "wedged"):
            return RecoveryHint(
                advisory=(
                    "the attempt collected a zero-change candidate — no effect was delivered; "
                    "re-issue the guidance so the turn has something to do"
                ),
                commands=(commands["steer"], commands["probe"]),
                retryable=True,
            )
        return RecoveryHint(
            advisory=(
                "a zero-change candidate was collected — no effect was delivered; probe the "
                "current state before deciding"
            ),
            commands=(commands["probe"],),
            retryable=True,
        )

    if delivery_outcome == "collection_failed":
        typed = f" ({blocked})" if blocked else ""
        return RecoveryHint(
            advisory=(
                f"the candidate collection failed{typed} — address the collector's typed "
                "error, then rerun the attempt through the guarded route"
            ),
            commands=(commands["retry"], commands["probe"]),
            retryable=True,
        )

    if delivery_outcome == "driver_failed":
        return RecoveryHint(
            advisory=(
                "the lane driver failed before a candidate existed — rerun the attempt "
                "through the guarded route once the driver's cause is addressed"
            ),
            commands=(commands["retry"], commands["probe"]),
            retryable=True,
        )

    if delivery_outcome == "not_collected_yet":
        return RecoveryHint(
            advisory=(
                "no candidate collection is recorded for the current attempt — probe the "
                "current state before deciding anything"
            ),
            commands=(commands["probe"],),
            retryable=True,
        )

    # delivered — only the healthy ends need nothing
    if state in ("verified_ready", "accepted"):
        return None
    if state == "unverified":
        return RecoveryHint(
            advisory=(
                "the candidate was delivered but is not independently verified — verify the "
                "CURRENT candidate before accepting it"
            ),
            commands=(commands["probe"],),
            retryable=True,
        )
    return RecoveryHint(
        advisory="the candidate was delivered; probe the current state for what remains",
        commands=(commands["probe"],),
        retryable=True,
    )


def _recovery_headline(state: str, delivery: DeliveryOutcome) -> str:
    """The one-line delivery display — a FAILED delivery never reads as a
    successful resume (R38-15's headline acceptance)."""
    if delivery.outcome == "empty_diff_no_effect":
        if state == "resumed":
            return (
                "the resume delivered no changes — a FAILED/no-effect delivery, never a "
                "successful resume of useful work"
            )
        return "the attempt delivered a zero-change candidate — no effect"
    if delivery.outcome == "collection_failed":
        return "the delivery FAILED — the candidate was never collected"
    if delivery.outcome == "driver_failed":
        return "the delivery FAILED — the lane driver failed before a candidate existed"
    if delivery.outcome == "not_collected_yet":
        return "no delivery recorded yet"
    return "the candidate was delivered"


def recovery_document(
    source_rows: Mapping[str, Any],
    *,
    state: str | None = None,
    coverage: Mapping[str, str] | None = None,
    occupancy: Sequence[Mapping[str, Any]] | None = None,
    projection_inconsistent: bool = False,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """The composed recovery surface over one snapshot's rows (pure).

    The delivery outcome (failed deliveries display AS failures), the
    five-milestone ladder (each independent), the advisory hint (the
    guarded routes named) and the consistency word. A snapshot whose
    source fence MOVED while it was read (the R37-03/#284 fence) renders
    EXPLICIT UNCERTAINTY — ``consistency: "inconsistent"`` plus the
    uncertainty note — instead of a confident ladder assembled from mixed
    versions.
    """
    run = _require_run(source_rows)
    moment = now if now is not None else datetime.now(timezone.utc)
    state_word = str(state) if state else derive_state(source_rows, moment).display_state
    delivery = delivery_outcome_of(source_rows)
    ladder = recovery_ladder(source_rows, coverage=coverage, occupancy=occupancy)
    hint = recovery_hint(
        state_word, delivery.outcome, blocked_reason=str(run.get("blocked_reason") or "")
    )
    document: dict[str, Any] = {
        "schema": RECOVERY_SCHEMA,
        "state": state_word,
        "delivery": {
            "outcome": delivery.outcome,
            "failed": delivery.failed,
            "headline": _recovery_headline(state_word, delivery),
            "reason": delivery.reason,
            "evidence": dict(delivery.evidence),
        },
        "ladder": {name: dict(entry) for name, entry in ladder.items()},
        "hint": hint.as_document() if hint is not None else None,
        "consistency": "inconsistent" if projection_inconsistent else "observed",
    }
    if projection_inconsistent:
        document["uncertainty"] = (
            "the source fence moved while this snapshot was read — every milestone and the "
            "delivery outcome may describe a moved world; re-read before acting"
        )
    return redact(document)


# ---------------------------------------------------------------------------
# Recovery actions — the validity matrix and the CAS-checked decision
# ---------------------------------------------------------------------------

#: Every action the view may offer. The control verbs route through the
#: EXISTING guarded paths (the command router's authenticated ingress for
#: pause/resume/steer/answer; the classic operator commands for
#: retry/cancel/reconcile); ``probe`` is the read-only reconciliation
#: query the research mandates before any retry. R40-13 (#349) adds the
#: two post-readiness recovery verbs — ``continue_review_only`` (the
#: #325/#340 review-only continuation) and ``follow_up_correction`` (the
#: #337/#338 linked follow-up round) — as DIFFERENT actions from the
#: implementation restart (``retry``) and the WIP discard (``cancel``):
#: each carries its own one-line safe-action description in
#: :data:`SAFE_ACTION_DESCRIPTIONS`.
ACTIONS: Final[tuple[str, ...]] = (
    "pause",
    "resume",
    "steer",
    "answer",
    "retry",
    "cancel",
    "reconcile",
    "probe",
    "continue_review_only",
    "follow_up_correction",
)

#: The typed refusal code when an action is replayed against a candidate
#: or round it was not planned for (R40-13's ``operator.stale_action_
#: refusal`` observability): the action named an expected candidate /
#: round / version and the current world moved — the refusal names the
#: CURRENT subject and the safe next action, never a silent apply.
STALE_ACTION_REFUSAL: Final = "operator.stale_action_refusal"

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
    "continue_review_only": "runs-service:continue_review_only",
    "follow_up_correction": "native-note:/fix",
}

#: R40-13 (#349): the one-line SAFE-ACTION description for every offered
#: action — what it WILL and what it WILL NOT do. Review-only recovery and
#: follow-up correction are DIFFERENT verbs from the implementation
#: restart and the WIP discard, and the operator reads the difference
#: here, before clicking.
SAFE_ACTION_DESCRIPTIONS: Final[Mapping[str, str]] = {
    "pause": (
        "will pause safely at the next boundary and commit a checkpoint; "
        "it will not cancel the run or discard work"
    ),
    "resume": (
        "will restore the exact committed checkpoint bytes; it will not "
        "re-plan, re-run finished legs, or touch the base"
    ),
    "steer": (
        "will add corrected guidance to the RUNNING attempt; it will not "
        "restart it or discard its WIP"
    ),
    "answer": ("will answer one open question; it will never write code or dispatch"),
    "retry": (
        "IMPLEMENTATION RESTART: re-runs the failed implementation in place "
        "(one commit cycle, same branch); it will not preserve the failed "
        "attempt's WIP and will not re-plan the task"
    ),
    "cancel": (
        "WIP DISCARD: cancels the run and abandons uncommitted work; it will "
        "not checkpoint, and a new task needs a new implement request"
    ),
    "reconcile": (
        "will probe lost publication intents by identity; it will not "
        "re-dispatch anything the probe cannot attribute"
    ),
    "probe": ("read-only: re-reads the current state; it changes nothing"),
    "continue_review_only": (
        "REVIEW-ONLY RECOVERY: repeats ONLY the review of the SAME verified "
        "candidate — zero coder dispatches, zero new commits, no replacement "
        "MR; it will not fix code, and it refuses if the candidate or its "
        "verification moved"
    ),
    "follow_up_correction": (
        "FOLLOW-UP CORRECTION: admits ONE linked round from the approved MR "
        "head (a NEW child work unit with its own budget); it preserves "
        "human edits and the prior delivery as immutable history — it will "
        "not discard WIP, restart implementation in place, or reuse the "
        "parent's ready evidence as current readiness"
    ),
}

#: The exact command shapes the recovery hints name (R38-15) — every one
#: routes through an EXISTING guarded path (the command router's comment
#: verbs, the classic operator commands, the read-only probe, the
#: token-rotation runbook); the hint never executes anything and never
#: becomes a second authorization surface. Defined beside :data:`ACTION_VIA`
#: because it names those same routes.
_RECOVERY_COMMANDS: Final[Mapping[str, dict[str, str]]] = {
    "steer": {"command": "/steer <run-id> <corrected guidance>", "via": ACTION_VIA["steer"]},
    "resume": {"command": "/resume <run-id>", "via": ACTION_VIA["resume"]},
    "retry": {"command": "/retry <run-id>", "via": ACTION_VIA["retry"]},
    "reconcile": {"command": "/reconcile <run-id>", "via": ACTION_VIA["reconcile"]},
    "probe": {"command": "GET /operator/runs/<run-id>", "via": ACTION_VIA["probe"]},
    "rotate": {
        "command": "rotate/rebind the credential per the token-rotation runbook",
        "via": _SUGGESTED_VIA["rotate_or_rebind_credential"],
    },
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

    R40-13 (#349) widens the CAS ticket to the SUBJECT the action was
    planned against: ``expected_candidate`` (the exact candidate sha)
    and ``expected_round`` (the delivery/round reference, e.g.
    ``round:2:<decision>``). :meth:`RecoveryActions.decide` refuses with
    :data:`STALE_ACTION_REFUSAL` when either moved — an action planned
    for the parent's superseded delivery never applies to the round's
    new candidate. ``safe_action`` carries the one-line will/will-not
    description from :data:`SAFE_ACTION_DESCRIPTIONS`.
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
    expected_candidate: str = ""
    expected_round: str = "delivery:1"

    @property
    def safe_action(self) -> str:
        """The one-line safe-action description (what it will and will
        not do) — the operator reads the verb's meaning BEFORE
        executing it."""
        return SAFE_ACTION_DESCRIPTIONS.get(self.action, "")

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
            "expected_candidate": self.expected_candidate,
            "expected_round": self.expected_round,
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
        the four facts and the CAS ticket (the projection's version, the
        exact candidate and the delivery/round reference)."""
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
                expected_candidate=str(projection.identity.get("active_candidate") or ""),
                expected_round=projection.round_ref,
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

        R40-13 (#349) adds the SUBJECT fence: an action whose
        ``expected_candidate`` or ``expected_round`` names a different
        candidate/delivery than the current projection holds is refused
        with :data:`STALE_ACTION_REFUSAL` — the parent's superseded
        delivery and its actions never apply to the round's new world,
        whatever the projection version says.
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
        # the SUBJECT fence (R40-13): same version, different world — the
        # candidate or the round moved under the version number.
        current_candidate = str(current.identity.get("active_candidate") or "")
        if (action.expected_candidate and action.expected_candidate != current_candidate) or (
            action.expected_round and action.expected_round != current.round_ref
        ):
            return ActionDecision(
                allowed=False,
                reason=(
                    f"{STALE_ACTION_REFUSAL}: the action was planned for candidate "
                    f"{action.expected_candidate[:12] or '(none)'} on {action.expected_round} "
                    f"but the current subject is candidate {current_candidate[:12] or '(none)'} "
                    f"on {current.round_ref} — the delivery or round moved; re-decide "
                    "against the current world"
                ),
                current_state=current.state,
                safe_next_action=safe_next or "probe",
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


def action_hint_block(
    projection: OperatorProjection,
    *,
    actor: str = "operator:read",
    actor_role: str = "observer",
    snapshot_inconsistent: bool = False,
) -> dict[str, Any]:
    """The ADVISORY action-hint block the render surfaces (R38-15).

    The hints themselves are :meth:`RecoveryActions.plan` verbatim — the
    read-only observer shape the API serves. R38-15 adds the
    old-snapshot revalidation DISPLAY: a hint rendered from a snapshot
    whose consistency fence MOVED (``snapshot_inconsistent`` — the
    R37-03/#284 fence; an assembly that mixed versions, the "action hint
    from an old snapshot" of the acceptance) carries ``stale: true`` on
    every entry plus the current state's safe alternative, and the block
    itself says WHY it is stale. The guarded route at execution time
    still enforces the refusal (CTL-04 CAS); this is the honest display
    of the same fact, before the operator clicks.
    """
    actions: list[dict[str, Any]] = []
    for action in RecoveryActions.plan(projection, actor, actor_role):
        entry: dict[str, Any] = {
            "action": action.action,
            "via": action.via,
            "digest": action.digest,
            "expected_version": action.expected_version,
            "expected_candidate": action.expected_candidate,
            "expected_round": action.expected_round,
            "safe_action": action.safe_action,
            "at": action.at,
            "linkage": action.linkage,
        }
        if snapshot_inconsistent:
            entry["stale"] = True
            entry["safe_alternative"] = RecoveryActions.safe_next("stale", "observer")
        actions.append(entry)
    document: dict[str, Any] = {
        "actions": actions,
        "actions_advisory": (
            "action hints only — execution goes through the guarded command routes, "
            "which revalidate authority and the current world; this render is an "
            "ephemeral projection, not a durable CAS ticket"
        ),
        "actions_stale": bool(snapshot_inconsistent),
    }
    if snapshot_inconsistent:
        document["actions_stale_reason"] = (
            "the consistency fence moved while this snapshot was read — these hints may "
            "describe a moved world; refresh (probe) and re-decide before executing any "
            "of them, the guarded route will refuse the stale ones anyway"
        )
    return document


# ---------------------------------------------------------------------------
# The export diagnostics slice (R38-15) — bounded, allowlisted, backup-free
# ---------------------------------------------------------------------------

#: The diagnostics document's schema discriminator.
DIAGNOSTICS_SCHEMA: Final = "forge.operator.diagnostics/1"

#: The per-section FIELD ALLOWLISTS (the audit_export
#: ``CREDENTIAL_DOCUMENT_FIELDS`` pattern): each diagnostics section
#: serializes ONLY its declared fields — anything else a source row ever
#: carried is dropped, never rescued into the export.
DIAGNOSTIC_SECTION_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "delivery": frozenset({"outcome", "failed", "headline", "reason", "evidence"}),
    "recovery_ladder": frozenset({"milestone", "status", "evidence", "at"}),
    "recovery_hint": frozenset({"advisory", "commands", "retryable"}),
    "blocked_reasons": frozenset(
        {"code", "explanation", "evidence", "suggested_action", "via", "retryable"}
    ),
}

#: The per-section ENTRY bound: the diagnostics slice of a support-bundle
#: export stays small whatever the history (the evidence sections carry
#: the full story under the export's byte cap; diagnostics summarize it).
DIAGNOSTIC_MAX_ENTRIES: Final[int] = 10

#: Raw operational backup NAME shapes (the R38-03/#304 world: PostgreSQL
#: custom-format dumps, SQL/SQLite exports, backup directories) — a
#: filename-shaped token carrying one of these extensions, or a backups/
#: path. A diagnostics render never carries a raw backup name or path;
#: the sanitized RECEIPTS are referenced at most (a value naming a
#: receipt passes; the bytes never exist here to begin with).
_RAW_BACKUP_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"[\w./\\-]+\.(?:dump|pgdump|backup|bak|sql|sqlite3?|db)(?:\.(?:gz|zst|xz|bz2))?(?![\w.-])"
    r"|(?:^|[/\\])backups?[/\\][\w./\\-]*",
    re.IGNORECASE,
)


def _allowlisted(document: Mapping[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    """One document reduced to its declared fields (unknown keys dropped)."""
    return {str(key): document[key] for key in document if key in fields}


def _bounded(rows: Sequence[Mapping[str, Any]], cap: int) -> tuple[list[dict[str, Any]], bool]:
    """The first *cap* entries + whether the bound cut anything."""
    entries = [dict(row) for row in rows]
    if len(entries) <= cap:
        return entries, False
    return entries[:cap], True


def _scrub_raw_backups(value: Any) -> tuple[Any, int]:
    """Recursively replace raw operational backup NAMES with the exclusion
    marker, counting what was excluded (a #304 receipt reference — a value
    naming a receipt — survives whole; the raw backup name never does)."""
    if isinstance(value, str):
        if "receipt" in value.lower():
            return value, 0
        return _RAW_BACKUP_NAME_RE.subn("[backup-excluded]", value)
    if isinstance(value, Mapping):
        cleaned: dict[Any, Any] = {}
        excluded = 0
        for key, item in value.items():
            cleaned[key], dropped = _scrub_raw_backups(item)
            excluded += dropped
        return cleaned, excluded
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        excluded = 0
        for item in value:
            scrubbed, dropped = _scrub_raw_backups(item)
            items.append(scrubbed)
            excluded += dropped
        return items, excluded
    return value, 0


def export_diagnostics(
    source_rows: Mapping[str, Any],
    *,
    state: str | None = None,
    coverage: Mapping[str, str] | None = None,
    occupancy: Sequence[Mapping[str, Any]] | None = None,
    projection: OperatorProjection | None = None,
    projection_inconsistent: bool = False,
    max_entries: int = DIAGNOSTIC_MAX_ENTRIES,
) -> dict[str, Any]:
    """The support-bundle export's DIAGNOSTICS slice (R38-15).

    Three bounds, all structural:

    - SIZE — every list section carries at most *max_entries* entries
      (``truncated`` names what was cut; the byte cap at the export route
      bounds the whole document on top);
    - ALLOWLIST — every section serializes ONLY its declared
      :data:`DIAGNOSTIC_SECTION_FIELDS` (the audit_export pattern);
    - BACKUP EXCLUSION — raw operational backup names (R38-03/#304) are
      scrubbed from every string the slice carries; the sanitized
      receipts are referenced at most (``raw_backups_excluded`` counts).

    Pure: derives from the same rows the bundle reads, writes nothing.
    """
    recovery = recovery_document(
        source_rows,
        state=state,
        coverage=coverage,
        occupancy=occupancy,
        projection_inconsistent=projection_inconsistent,
    )
    if projection is None:
        projection = initial_projection(source_rows)
    blocked_reasons = [
        reason.as_document()
        for reason in explain_blocked(
            projection,
            coverage=coverage,
            occupancy=occupancy,
            checkpoints=source_rows.get("checkpoints"),
        )
    ]
    ladder_rows, ladder_cut = _bounded(
        [
            _allowlisted(dict(entry), DIAGNOSTIC_SECTION_FIELDS["recovery_ladder"])
            for entry in recovery["ladder"].values()
        ],
        max_entries,
    )
    hint = recovery["hint"]
    blocked_rows, blocked_cut = _bounded(
        [
            _allowlisted(reason, DIAGNOSTIC_SECTION_FIELDS["blocked_reasons"])
            for reason in blocked_reasons
        ],
        max_entries,
    )
    sections: dict[str, Any] = {
        "delivery": _allowlisted(dict(recovery["delivery"]), DIAGNOSTIC_SECTION_FIELDS["delivery"]),
        "recovery_ladder": ladder_rows,
        "recovery_hint": (
            [_allowlisted(dict(hint), DIAGNOSTIC_SECTION_FIELDS["recovery_hint"])]
            if isinstance(hint, Mapping)
            else []
        ),
        "blocked_reasons": blocked_rows,
    }
    sections, excluded = _scrub_raw_backups(sections)
    return redact(
        {
            "schema": DIAGNOSTICS_SCHEMA,
            "sections": sections,
            "export": {
                "max_entries_per_section": max_entries,
                "truncated": {"recovery_ladder": ladder_cut, "blocked_reasons": blocked_cut},
                "fields": "allowlisted",
                "raw_backups_excluded": excluded,
            },
        }
    )


# ---------------------------------------------------------------------------
# R40-13 (#349) — the five linked facts: execution, review round,
# candidate, verification, acceptance — and the stable status comment
# ---------------------------------------------------------------------------

#: The delivery view's schema discriminator (versioned like the view's).
DELIVERY_VIEW_SCHEMA: Final = "forge.operator.delivery/1"

#: The status comment's schema discriminator + the machine-readable marker
#: prefix every rendered status comment carries.
STATUS_COMMENT_SCHEMA: Final = "forge.operator.status-comment/1"
STATUS_COMMENT_MARKER: Final = "forge-status"

#: R40-02 (#338): the round statuses that still hold the lineage's ONE
#: outstanding correction slot (the partial unique index's own set).
REVIEW_ROUND_OPEN_STATUSES: Final[tuple[str, ...]] = ("admitted", "dispatched")

#: R40-13 (#349): the round statuses that SUPERSEDE the parent's ready
#: delivery — a child work unit exists (or existed), so the parent's
#: candidate and its green evidence are HISTORY. ``stale`` is deliberately
#: ABSENT: a stale round dispatched nothing (human edits preserved) — the
#: parent's delivery is still the current one and the operator re-raises
#: the correction against the moved head.
REVIEW_ROUND_SUPERSEDING_STATUSES: Final[tuple[str, ...]] = (
    "admitted",
    "dispatched",
    "completed",
    "ended",
)

#: R40-13 (#349): the #337 feedback-request refusal statuses that surface
#: as EXPLICIT next-actions (the unavailable prerequisites and the #338
#: head-fence refusals an operator must see named, never discover): each
#: maps to its one-line remediation and the guarded route it runs through.
FEEDBACK_REFUSAL_NEXT_ACTIONS: Final[Mapping[str, dict[str, str]]] = {
    "stale_head": {
        "code": "stale_head",
        "description": (
            "the MR head moved before the correction dispatched — human edits are "
            "preserved and nothing was dispatched; re-raise the correction against "
            "the CURRENT head"
        ),
        "via": "native-note:/fix",
    },
    "round_limit": {
        "code": "round_limit",
        "description": (
            "the bounded review-round count for this lineage is exhausted — further "
            "corrections need an operator policy change (FORGE_MAX_REVIEW_ROUNDS), "
            "not another note"
        ),
        "via": "operator-policy:FORGE_MAX_REVIEW_ROUNDS",
    },
    "mr_closed": {
        "code": "mr_closed",
        "description": (
            "the MR is merged or closed — the human decision already ended the "
            "collaboration surface; there is nothing to correct"
        ),
        "via": "none",
    },
    "conflicting_correction": {
        "code": "conflicting_correction",
        "description": (
            "ONE outstanding correction per lineage — the open round holds the "
            "slot; wait for it to complete before raising the next"
        ),
        "via": "read-only:/status",
    },
    "correction_window_closed": {
        "code": "correction_window_closed",
        "description": (
            "the correction window for this run is closed — a follow-up needs the "
            "post-readiness round route or a fresh task"
        ),
        "via": "native-note:/fix",
    },
    "refused_unauthorized": {
        "code": "refused_unauthorized",
        "description": (
            "the note's author may not request corrections on this run — the "
            "authorized reviewer set is configured approver-side"
        ),
        "via": "operator-policy:FORGE_APPROVERS",
    },
}


def _round_ref_of(number: Any, decision_id: Any) -> str:
    """``round:<n>:<decision>`` — the stable round reference actions name."""
    try:
        numbered = int(number)
    except (TypeError, ValueError):
        return ""
    if numbered <= 0:
        return ""
    return f"round:{numbered}:{str(decision_id or '')}"


def _evidence_mapping(run: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """One run-evidence fragment as a mapping (``{}`` when absent)."""
    evidence = run.get("evidence")
    if not isinstance(evidence, Mapping):
        return {}
    fragment = evidence.get(key)
    return fragment if isinstance(fragment, Mapping) else {}


def _feedback_requests_of(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The #337 feedback requests recorded on the run's evidence — the
    durable request documents keyed by note id. Value-free by
    construction: only the identity + lifecycle status ride out (the
    reviewer's TEXT never renders on the operator surface)."""
    evidence = run.get("evidence")
    if not isinstance(evidence, Mapping):
        return []
    requests = evidence.get("review_feedback_requests")
    if not isinstance(requests, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for note_id, document in sorted(requests.items(), key=lambda pair: str(pair[0])):
        if not isinstance(document, Mapping):
            continue
        rows.append(
            {
                "note_id": str(document.get("note_id") or note_id),
                "status": str(document.get("status") or ""),
                "classification": str(document.get("classification") or ""),
                "decision_id": str(document.get("decision_id") or ""),
                "head_sha": str(document.get("head_sha") or ""),
                "created_at": _iso(document.get("created_at")),
            }
        )
    return rows


def review_round_fact(source_rows: Mapping[str, Any]) -> dict[str, Any] | None:
    """The CURRENT review-round fact over one snapshot's rows (pure).

    Sources, composed — never re-derived:

    - the ``rounds`` section (the #338 ``review_rounds`` table rows the
      snapshot reader read: the rounds naming THIS run as parent or
      child, round order);
    - the run evidence's ``review_round`` fragment (the CHILD's own copy
      of its admission — present exactly when this run IS a round's work
      unit);
    - the run evidence's ``review_feedback_requests`` (#337 — the
      requests' lifecycle statuses, incl. the R40-02 refusal words).

    The returned fact carries: ``ref`` (the lineage's CURRENT
    delivery/round reference — ``delivery:1`` when no round exists),
    ``role`` (how THIS run stands in the newest round: parent / child /
    ``""``), the newest round's identity + status with its thin evidence
    link, the supersession reference (``supersedes`` — set when a
    SUPERSEDING round names this run as parent), the lineage root and
    round count, and the request rows. ``None`` when neither source
    names a round.
    """
    run = _norm(source_rows.get("run") or {})
    if not run:
        return None
    run_id = str(_first(run, "id", "run_id") or "")
    rounds = [_norm(row) for row in source_rows.get("rounds") or [] if _norm(row)]
    own = _evidence_mapping(run, "review_round")
    if not rounds and not own:
        return None

    def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        def key(row: dict[str, Any]) -> tuple[int, str]:
            try:
                number = int(row.get("round_number") or 0)
            except (TypeError, ValueError):
                number = 0
            return (number, str(row.get("decision_id") or ""))

        return max(rows, key=key) if rows else None

    newest_table = _latest(rounds)
    newest: dict[str, Any] = dict(newest_table) if newest_table is not None else {}
    role = ""
    if newest_table is not None:
        if str(newest_table.get("parent_run_id") or "") == run_id:
            role = "parent"
        elif str(newest_table.get("child_run_id") or "") == run_id:
            role = "child"
    if own:
        # the child's own copy: authoritative for THIS run's role even
        # when the rounds table section was not selected (coverage
        # unknown, never guessed).
        role = "child"
        newest = {**newest, **{str(k): v for k, v in own.items()}}
        newest["round_number"] = own.get("round_number") or newest.get("round_number")
        newest["decision_id"] = own.get("decision_id") or newest.get("decision_id")
        newest["root_run_id"] = own.get("root_run_id") or newest.get("root_run_id")
        newest["parent_run_id"] = own.get("parent_run_id") or newest.get("parent_run_id")

    ref = _round_ref_of(newest.get("round_number"), newest.get("decision_id"))
    supersedes = ""
    if role == "parent" and str(newest.get("status") or "") in REVIEW_ROUND_SUPERSEDING_STATUSES:
        supersedes = ref
    round_row: dict[str, Any] = {
        "round_number": newest.get("round_number"),
        "status": str(newest.get("status") or ""),
        "status_reason": str(newest.get("status_reason") or ""),
        "decision_id": str(newest.get("decision_id") or ""),
        "note_id": str(newest.get("note_id") or ""),
        "base_head_sha": str(newest.get("base_head_sha") or ""),
        "parent_run_id": str(newest.get("parent_run_id") or ""),
        "child_run_id": str(newest.get("child_run_id") or ""),
        "root_run_id": str(newest.get("root_run_id") or ""),
        "open": str(newest.get("status") or "") in REVIEW_ROUND_OPEN_STATUSES,
        "evidence": (
            {
                "of": "review round",
                "id": str(newest.get("decision_id") or ""),
                "ref": _row_digest(newest),
            }
            if newest
            else None
        ),
    }
    return {
        "ref": ref or "delivery:1",
        "run_id": run_id,
        "role": role,
        "round": round_row,
        "rounds_recorded": len(rounds) or (1 if own else 0),
        "root_run_id": str(newest.get("root_run_id") or "") or run_id,
        "supersedes": supersedes,
        "requests": _feedback_requests_of(run),
    }


def acceptance_fact(
    run: Mapping[str, Any],
    *,
    verified_current: bool,
    candidate_sha: str,
) -> dict[str, Any]:
    """The ACCEPTANCE fact — attributed to a real HUMAN decision or
    honestly absent (never to green CI).

    The only durable acceptance record is the run evidence's
    ``acceptance`` fragment (the R24 reconciliation): the human's OWN
    merge/close decision on the MR, observed provider-side (forge never
    merges — ADR-0003). Green CI, a passed verification, a ready status
    are NONE of them acceptance: they render ``pending_human_decision``
    at best, and the document says so — the operator never confuses a
    verified candidate with an accepted one.
    """
    recorded = _evidence_mapping(run, "acceptance")
    state = str(recorded.get("state") or "").strip().lower()
    link = (
        {
            "of": "acceptance",
            "id": f"mr:{str(run.get('mr_iid') or '')}",
            "ref": _row_digest(dict(recorded)),
        }
        if recorded
        else None
    )
    if state == "merged":
        return {
            "state": "accepted_by_human",
            "decided_by": "human",
            "basis": (
                "the MR was MERGED by a human — the merge IS the acceptance "
                "decision, observed provider-side (forge never merges)"
            ),
            "at": _iso(_first(recorded, "merged_at", "observed_at")),
            "evidence": link,
        }
    if state == "closed":
        return {
            "state": "rejected_by_human",
            "decided_by": "human",
            "basis": "the MR was CLOSED by a human — an honest rejection, recorded once",
            "at": _iso(_first(recorded, "observed_at")),
            "evidence": link,
        }
    if verified_current and candidate_sha:
        return {
            "state": "pending_human_decision",
            "decided_by": None,
            "basis": (
                "the candidate is verified and awaits the HUMAN merge decision — "
                "green CI and a passed verification are NOT acceptance"
            ),
            "at": "",
            "evidence": None,
        }
    return {
        "state": "none",
        "decided_by": None,
        "basis": (
            "no human decision is recorded — acceptance is attributed only to a "
            "real human merge/close decision, never to green CI"
        ),
        "at": "",
        "evidence": None,
    }


#: The amendment route's command shape (the #340 contract): an amendment
#: names EXACTLY ONE axis and rides the ORIGINATING native command's
#: identity — two identical amount/reason commands are two decisions; a
#: redelivery of one applies exactly once.
AMENDMENT_COMMAND_SHAPE: Final[Mapping[str, str]] = {
    "identity": "run:continue_review:<project>:<note-id> (the originating native command)",
    "fields": "axis=<usd|calls|tokens|wallclock> amount=<n> reason=<text>",
    "rule": (
        "ONE axis per amendment — cross-axis conversion is refused (it would "
        "need a stated versioned policy); count axes move the run_budgets "
        "limits atomically, usd raises the closing gate's effective cap"
    ),
}


def budget_blocked_review(source_rows: Mapping[str, Any]) -> dict[str, Any] | None:
    """The review-budget block as the operator reads it (R40-13 over
    #325/#340): the ACTUAL limiting axis and the SUPPORTED amendment
    route, or ``None`` when no review-budget decision is recorded.

    A pure fold over the recorded rows (the run evidence's
    ``review_budget_block`` — the durable decision the reviewer leg
    wrote; the run row's own blocked reason). The limiting axis, in
    priority order: the axis a REFUSED amendment's typed refusal_reason
    names (the guard's own recording), else ``usd`` when the recorded
    closing report says the reserve cannot cover the review (the closing
    gate's cap is the refusing axis), else honestly ``None`` with the
    basis saying the durable rows name no single axis — the LIVE
    :func:`forge.durable.budgets.limiting_axis` read decides at
    continuation time. The amendment route is the #340 command shape
    (one axis, one amount, a reason, the originating command identity) —
    never a silent re-plan.
    """
    run = _norm(source_rows.get("run") or {})
    if not run:
        return None
    block = _evidence_mapping(run, "review_budget_block")
    blocked_reason = str(run.get("blocked_reason") or "")
    if not block and "budget_exhausted" not in blocked_reason:
        return None
    if not block:
        return {
            "blocked": True,
            "limiting_axis": None,
            "limiting_axis_basis": (
                "the run's blocked reason names a budget refusal but no recorded "
                "review-budget decision carries the axis — consult the live "
                "limiting-axis read before amending"
            ),
            "amendment_route": None,
            "amendments_recorded": 0,
            "short_reason": blocked_reason,
        }
    amendments = block.get("amendments")
    amendment_rows = [dict(row) for row in amendments] if isinstance(amendments, list) else []
    refused_axes = [
        str(row.get("refusal_reason") or "").split(":", 1)[0].strip()
        for row in amendment_rows
        if row.get("status") == "refused" and str(row.get("refusal_reason") or "").strip()
    ]
    raw_report = block.get("budget")
    budget_report: Mapping[str, Any] = raw_report if isinstance(raw_report, Mapping) else {}
    fits = budget_report.get("closing_review_fits")
    axis: str | None = None
    basis = ""
    if refused_axes:
        axis = refused_axes[-1]
        basis = (
            f"the refused amendment's typed refusal names the {axis} axis — amend "
            "the LIMITING axis; an amendment on another axis does not permit the review"
        )
    elif fits is False:
        axis = "usd"
        basis = (
            "the recorded closing report says the reserve cannot cover the review — "
            "the closing gate's usd cap is the refusing axis"
        )
    else:
        basis = (
            "the recorded decision names no single limiting axis — the live "
            "limiting_axis read at continuation time decides; probe before amending"
        )
    released = bool(block.get("released"))
    return {
        "blocked": not released,
        "released": released,
        "limiting_axis": axis,
        "limiting_axis_basis": basis,
        "amendment_route": (
            {
                "command": "continue_review_only: amend the limiting axis",
                "shape": dict(AMENDMENT_COMMAND_SHAPE),
                "via": ACTION_VIA["continue_review_only"],
                "safe_action": SAFE_ACTION_DESCRIPTIONS["continue_review_only"],
            }
            if not released
            else None
        ),
        "amendments_recorded": len(amendment_rows),
        "refused_amendments": sum(1 for row in amendment_rows if row.get("status") == "refused"),
        "short_reason": str(block.get("short_reason") or ""),
        "evidence": {
            "of": "review budget block",
            "id": str(_first(run, "id", "run_id") or ""),
            "ref": _row_digest(dict(block)),
        },
    }


def delivery_next_actions(
    requests: Sequence[Mapping[str, Any]],
    round_row: Mapping[str, Any] | None,
    budget_block: Mapping[str, Any] | None,
    *,
    acceptance: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The EXPLICIT next-actions over the request/round/budget facts
    (pure): unavailable prerequisites and the #338 head-fence refusals
    render as named rows — each carrying its one-line remediation and
    the guarded route — never as silence the operator must debug.

    *requests* are the #337 feedback-request rows read from the run's
    own evidence (they exist even when the rounds-table section was not
    selected); *round_row* is the newest round fact (``None`` when no
    round identity is observed)."""
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for request in requests:
        status = str(request.get("status") or "")
        entry = FEEDBACK_REFUSAL_NEXT_ACTIONS.get(status)
        if entry is None or status in seen:
            continue
        seen.add(status)
        actions.append(
            {
                "code": entry["code"],
                "description": entry["description"],
                "via": entry["via"],
                "available": False,
                "evidence": {
                    "of": "feedback request",
                    "id": str(request.get("note_id") or ""),
                    "ref": _row_digest(dict(request)),
                },
            }
        )
    if round_row is not None and str(round_row.get("status") or "") == "stale":
        entry = FEEDBACK_REFUSAL_NEXT_ACTIONS["stale_head"]
        actions.append(
            {
                "code": entry["code"],
                "description": (
                    f"round {round_row.get('round_number')} went stale — " + entry["description"]
                ),
                "via": entry["via"],
                "available": False,
                "evidence": round_row.get("evidence"),
            }
        )
    if budget_block is not None and budget_block.get("blocked"):
        axis = budget_block.get("limiting_axis")
        actions.append(
            {
                "code": "budget_blocked_review",
                "description": (
                    "the closing review is budget-blocked on the "
                    f"{axis or 'undetermined'} axis — "
                    + str(budget_block.get("limiting_axis_basis") or "")
                ),
                "via": str((budget_block.get("amendment_route") or {}).get("via") or ""),
                "available": True,
                "amendment_route": budget_block.get("amendment_route"),
                "evidence": budget_block.get("evidence"),
            }
        )
    if acceptance is not None and str(acceptance.get("state") or "") == "pending_human_decision":
        actions.append(
            {
                "code": "pending_human_merge_decision",
                "description": (
                    "the candidate is verified — the MERGE decision is a human's; "
                    "forge never merges (a /fix note admits a follow-up round instead)"
                ),
                "via": "human:merge-decision",
                "available": False,
            }
        )
    return actions


def delivery_view(
    source_rows: Mapping[str, Any],
    *,
    projection: OperatorProjection | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """The FIVE linked facts of one delivery lineage (R40-13, pure).

    CURRENT execution, review round, candidate, verification and
    acceptance are SEPARATE facts — each derived from its own canonical
    rows and carrying its own evidence links — and LINKED: the candidate
    names the round that supersedes it, the verification names the
    candidate it tested, the acceptance names the human who decided, and
    the execution names the run that currently executes. After a
    follow-up round starts, the old ready delivery renders HISTORICAL
    (its candidate role and its green verdict both labelled) and is
    never presented as current readiness; acceptance is attributed only
    to a real human decision.

    Supersession rule (#338): the round row IS the supersession — the
    parent's terminal record, verdict and evidence stay immutable
    history; a ``stale`` round (nothing dispatched, human edits
    preserved) supersedes nothing.
    """
    run = _require_run(source_rows)
    run_id = str(_first(run, "id", "run_id") or "")
    moment = now if now is not None else datetime.now(timezone.utc)
    if projection is None:
        projection = initial_projection(source_rows, moment)
    round_fact = review_round_fact(source_rows)
    round_row = dict(round_fact.get("round") or {}) if round_fact else {}
    open_round = bool(round_row.get("open"))
    superseded_by = str((round_fact or {}).get("supersedes") or "")

    candidate_history = [str(sha) for sha in run.get("candidate_shas") or [] if str(sha or "")]
    candidate_now = current_candidate(run, candidate_history)
    verifications = [_norm(row) for row in source_rows.get("verifications") or []]
    current_passes, _ = verification_binding(verifications, candidate_history, candidate_now)

    # the candidate fact: HISTORICAL when a superseding round names this
    # run's delivery — the current candidate of the LINEAGE lives on the
    # round's child run (named by id), never re-presented as this run's.
    if superseded_by:
        candidate_fact: dict[str, Any] = {
            "sha": candidate_now,
            "role": "historical",
            "superseded_by": superseded_by,
            "note": (
                "superseded by a follow-up round — this delivery and its green "
                "evidence are immutable HISTORY, never current readiness"
            ),
            "lineage_current_run": str(round_row.get("child_run_id") or ""),
            "evidence": {
                "of": "run",
                "id": run_id,
                "ref": _row_digest(run),
            },
        }
    else:
        candidate_fact = {
            "sha": candidate_now,
            "role": "current" if candidate_now else "none",
            "superseded_by": "",
            "note": (
                "the run's current candidate (the active pointer, else the newest member)"
                if candidate_now
                else "no candidate recorded"
            ),
            "lineage_current_run": run_id,
            "evidence": {"of": "run", "id": run_id, "ref": _row_digest(run)},
        }

    # the verification fact: bound to the candidate it tested, HISTORICAL
    # when that candidate's delivery was superseded — the current
    # candidate is never presented as verified through old green evidence.
    if superseded_by and candidate_now:
        verification_fact: dict[str, Any] = {
            "binding": "historical",
            "verdict": "pass" if current_passes else "none",
            "candidate_sha": candidate_now,
            "note": (
                "the green evidence covers a SUPERSEDED delivery — it is history; "
                "the lineage's current candidate is not verified by it"
            ),
            "evidence": (
                {
                    "of": "verification",
                    "id": str(_first(current_passes[-1], "verification_id", "id") or ""),
                    "ref": _row_digest(current_passes[-1]),
                }
                if current_passes
                else None
            ),
        }
    elif current_passes:
        verification_fact = {
            "binding": "current",
            "verdict": "pass",
            "candidate_sha": candidate_now,
            "note": "a passed verification bound to the CURRENT candidate",
            "evidence": {
                "of": "verification",
                "id": str(_first(current_passes[-1], "verification_id", "id") or ""),
                "ref": _row_digest(current_passes[-1]),
            },
        }
    else:
        verification_fact = {
            "binding": "none",
            "verdict": "none",
            "candidate_sha": candidate_now,
            "note": (
                "no passed verification binds the current candidate"
                + (" (earlier passes render as history)" if projection.verification_history else "")
            ),
            "evidence": None,
        }

    acceptance = acceptance_fact(
        run,
        verified_current=bool(current_passes) and not superseded_by,
        candidate_sha=candidate_now,
    )
    budget_block = budget_blocked_review(source_rows)
    execution_subject = str(round_row.get("child_run_id") or "") if open_round else run_id
    execution_is_this_run = execution_subject == run_id
    execution_fact: dict[str, Any] = {
        "run_id": execution_subject,
        "role": (
            "round-child"
            if (round_fact or {}).get("role") == "child"
            else ("round-parent-superseded" if superseded_by else "delivery")
        ),
        "flow_status": str(run.get("status") or "") if execution_is_this_run else "",
        "status_scope": (
            "this-run"
            if execution_is_this_run
            else "separate-run — the round's child is its own snapshot; its status lives on its own run"
        ),
        "operator_state": projection.state,
        "blocked_reason": projection.blocked_reason,
        "attempt_id": str(projection.identity.get("attempt_id") or ""),
        "generation": projection.identity.get("generation"),
        "updated_at": _iso(run.get("updated_at")),
        "evidence": {"of": "run", "id": run_id, "ref": _row_digest(run)},
    }
    document: dict[str, Any] = {
        "schema": DELIVERY_VIEW_SCHEMA,
        "run_id": run_id,
        "computed_at": _iso(moment),
        "round_ref": projection.round_ref,
        "facts": {
            "execution": execution_fact,
            "review_round": (
                {
                    "ref": round_fact.get("ref"),
                    "role": round_fact.get("role"),
                    **round_row,
                    "root_run_id": round_fact.get("root_run_id"),
                    "rounds_recorded": round_fact.get("rounds_recorded"),
                    "requests": round_fact.get("requests"),
                }
                if round_fact
                else None
            ),
            "candidate": candidate_fact,
            "verification": verification_fact,
            "acceptance": acceptance,
        },
        "next_actions": delivery_next_actions(
            _feedback_requests_of(run), round_row or None, budget_block, acceptance=acceptance
        ),
        "budget_blocked_review": budget_block,
        "linkage": (
            "five separate facts, linked by identity: the candidate names the round "
            "that supersedes it, the verification names the candidate it tested, the "
            "acceptance names the human who decided, the execution names the run that "
            "currently executes — a superseded delivery's green evidence is history, "
            "and green CI is never acceptance"
        ),
    }
    return redact(document)


def status_comment_identity(
    *,
    run_id: str = "",
    state: str = "",
    round_ref: str = "",
    candidate_sha: str = "",
    verification_binding_word: str = "",
    acceptance_state: str = "",
    extra: Mapping[str, Any] | None = None,
) -> str:
    """The STABLE identity of one status comment (R40-13).

    A short digest over the FACTS the comment states — not over its
    bytes: two renders of the same world produce the SAME identity
    (replayed comment delivery collapses to ONE current status), and a
    moved world produces a DIFFERENT one (an old comment is identifiable
    as superseded by comparing its identity against the current rows).
    """
    payload = {
        "schema": STATUS_COMMENT_SCHEMA,
        "run_id": str(run_id or ""),
        "state": str(state or ""),
        "round_ref": str(round_ref or ""),
        "candidate": str(candidate_sha or ""),
        "verification": str(verification_binding_word or ""),
        "acceptance": str(acceptance_state or ""),
        "extra": dict(extra or {}),
    }
    return _digest_json(payload)[:20]


def render_status_comment(source_rows: Mapping[str, Any]) -> str:
    """The compact MR/issue status representation (R40-13): one block a
    native comment carries — the five facts in five short lines, links
    to the immutable evidence as digests, and the machine-readable
    identity marker that makes replayed delivery collapse to ONE
    current status. Pure: reads the rows, writes nothing."""
    view = delivery_view(source_rows)
    facts = view["facts"]
    execution = facts["execution"]
    round_fact = facts["review_round"]
    candidate = facts["candidate"]
    verification = facts["verification"]
    acceptance = facts["acceptance"]
    lines = [f"**Forge delivery — run `{view['run_id'][:8]}`**"]
    if round_fact is not None:
        lines.append(
            f"- **Round:** {round_fact.get('ref')} — `{round_fact.get('status')}`"
            + (f" ({round_fact.get('status_reason')})" if round_fact.get("status_reason") else "")
        )
    else:
        lines.append("- **Round:** delivery 1 (no follow-up round recorded)")
    if execution["flow_status"]:
        lines.append(
            f"- **Execution:** `{execution['flow_status']}` on `{execution['run_id'][:8]}`"
        )
    else:
        # the current execution is the round's CHILD — its status lives on
        # its own run, never guessed onto this render
        lines.append(
            f"- **Execution:** the round's child `{execution['run_id'][:8]}` "
            "(its own run carries its status)"
        )
    candidate_word = "none recorded" if not candidate["sha"] else str(candidate["sha"])[:12]
    lines.append(
        f"- **Candidate:** `{candidate_word}` ({candidate['role']}"
        + (f" — superseded by {candidate['superseded_by']}" if candidate["superseded_by"] else "")
        + ")"
    )
    if verification["binding"] == "historical":
        if verification["verdict"] == "pass":
            lines.append(
                f"- **Verification:** pass on `{str(verification['candidate_sha'])[:12]}` — "
                "HISTORICAL (the delivery it verified was superseded; it does not "
                "verify the lineage's current candidate)"
            )
        else:
            lines.append("- **Verification:** none bound to the lineage's current candidate")
    elif verification["binding"] == "none":
        lines.append("- **Verification:** none bound to the current candidate")
    else:
        lines.append(
            f"- **Verification:** pass on `{str(verification['candidate_sha'])[:12]}` "
            "(current — bound to the current candidate)"
        )
    decided = acceptance.get("decided_by")
    lines.append(
        f"- **Acceptance:** {acceptance['state']}"
        + (" — decided by a HUMAN merge decision" if decided else "")
    )
    links: list[str] = []
    if candidate.get("evidence"):
        links.append(f"candidate@{str(candidate['evidence'].get('ref', ''))[:12]}")
    if verification.get("evidence"):
        links.append(
            f"verification:{verification['evidence'].get('id')}@{str(verification['evidence'].get('ref', ''))[:12]}"
        )
    if round_fact is not None and round_fact.get("evidence"):
        links.append(
            f"round:{round_fact.get('decision_id')}@{str(round_fact['evidence'].get('ref', ''))[:12]}"
        )
    if links:
        lines.append(f"- **Evidence (immutable, digests):** {' · '.join(links)}")
    for action in view["next_actions"]:
        lines.append(f"- **Next:** {action['code']} — {action['description']}")
    identity = status_comment_identity(
        run_id=view["run_id"],
        state=str(facts["execution"].get("operator_state") or ""),
        round_ref=str(view.get("round_ref") or ""),
        candidate_sha=str(candidate["sha"] or ""),
        verification_binding_word=str(verification["binding"] or ""),
        acceptance_state=str(acceptance["state"] or ""),
    )
    lines.append(
        f"<!-- {STATUS_COMMENT_MARKER}:1 run={view['run_id']} identity={identity} "
        f"round={view.get('round_ref') or 'delivery:1'} -->"
    )
    return "\n".join(lines)


def with_status_comment_identity(body: str, snapshot: Mapping[str, Any]) -> str:
    """Append the stable status identity marker to an already-rendered
    ``/status`` body (the ONE call-site wiring the runs service makes).

    *snapshot* is the ``collect_status_snapshot`` document the body was
    rendered from — the identity digests the FACTS that body states
    (run, status, reason, cycle, candidates, verification, publication
    intents), so a replayed delivery of the same world carries the SAME
    identity and a changed world a different one: no contradictory
    ready/pending comment identities, ever.
    """
    facts = {
        "run_id": str(snapshot.get("run_id") or ""),
        "status": str(snapshot.get("status") or ""),
        "status_reason": str(snapshot.get("status_reason") or ""),
        "commit_cycle": snapshot.get("commit_cycle"),
        "candidate_shas": [str(sha) for sha in snapshot.get("candidate_shas") or []],
        "verification": dict(snapshot.get("verification") or {}),
        "intents": [str(intent.get("status") or "") for intent in snapshot.get("intents") or []],
        "updated_at": str(snapshot.get("updated_at") or ""),
    }
    identity = _digest_json({"schema": STATUS_COMMENT_SCHEMA, "facts": facts})[:20]
    marker = (
        f"<!-- {STATUS_COMMENT_MARKER}:1 run={facts['run_id']} identity={identity} "
        f"state={facts['status']} -->"
    )
    text = str(body or "").rstrip()
    return f"{text}\n\n{marker}" if text else marker
