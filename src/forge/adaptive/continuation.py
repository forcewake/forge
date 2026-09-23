"""Q35-02: the continuation decision — WHICH recoverable state a retry
continues from, selected from evidence, not from the retry verb.

The defect this module exists for: ``/retry`` (and the revival re-dispatch)
passed ``lane_resume_mode=required`` UNCONDITIONALLY, so a bootstrap or
early-infrastructure death that never produced a WIP checkpoint demanded a
checkpoint that could never exist — the lane's strict required-restore guard
(the thing that refuses a required resume with nothing restored, R28-03) then
made the run unretryable. The guard is right; the MODE SELECTION was wrong.

The fix is a pure decision over an evidence snapshot of the dead attempt:

- ``operator_discard_requested`` — the operator explicitly discarded the WIP
  (``/retry <run-id> restart``) → :attr:`ContinuationMode.EXPLICIT_RESTART`
  (the lane's ``restart`` contract: no download, the report says so).
- ``checkpoint_committed`` is True — an exact committed checkpoint IS the
  authorized continuation → :attr:`ContinuationMode.EXACT_WIP` (the lane's
  ``required`` contract: the pre-turn restore MUST succeed).
- ``vendor_started`` is False — PROVEN, from the recorded bootstrap
  classification / step evidence (never an assumption): there provably was
  no vendor session, so no WIP can exist →
  :attr:`ContinuationMode.COMMITTED_BASELINE` (the lane's ``fresh``
  contract: nothing restores accidentally; the frozen committed base is the
  continuation source).
- ``vendor_started`` is None or True with no committed checkpoint —
  :attr:`ContinuationMode.UNCERTAIN`: the caller dispatches NOTHING and asks
  the operator for an explicit restart or a reconciliation. Absence of a
  checkpoint NEVER proves there was no WIP (issue Q35-02 criterion 4).
- ``candidate_published`` is True (work already delivered by the dead
  attempt) — terminal GUIDANCE, not a resume: the decision stays UNCERTAIN
  but its reason points the caller at the delivered candidate instead of
  proposing a re-execution.

The decision is computed ONCE per native retry event, BEFORE the
acknowledgement note, persisted on the run's evidence under the
``continuation`` key (mode, reason, decided_at, and a digest of the
objective evidence fields), and a later retry event whose evidence digest
matches reuses the persisted decision instead of re-deciding — one decision,
one logical attempt per repeated event.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

__all__ = [
    "CONTINUATION_EVIDENCE_KEY",
    "CheckpointLookup",
    "ContinuationDecision",
    "ContinuationEvidence",
    "ContinuationMode",
    "decide_continuation",
    "durable_checkpoint_lookup",
    "evidence_from_record",
    "matching_decision",
    "operator_discard_requested",
    "retry_ack_line",
    "uncertain_retry_note",
]

#: The evidence key the persisted decision lives under on the run row.
CONTINUATION_EVIDENCE_KEY: str = "continuation"

#: An injectable checkpoint-presence provider (sync or async): run id →
#: ``True``/``False`` when presence is proven, ``None`` when unknown.
CheckpointLookup = Callable[[str], Any]


class ContinuationMode(str, Enum):
    """The recoverable state a retry continues from (Q35-02).

    Each value IS the lane's ``lane_resume_mode`` vocabulary
    (``LANE_RESUME_MODES`` in ``forge.runs.github_service``; the lane-side
    consumer is ``forge.lane_driver.resume_mode``), except UNCERTAIN which
    deliberately maps to NO dispatch at all.
    """

    #: A proven no-WIP retry on the frozen committed base — the lane's
    #: ``fresh`` contract (a 404-shaped restore is normal, never blocks).
    COMMITTED_BASELINE = "fresh"
    #: The exact committed checkpoint is the authorized continuation — the
    #: lane's ``required`` contract (the restore MUST succeed or the lane
    #: halts before any vendor session exists).
    EXACT_WIP = "required"
    #: The operator explicitly discarded the WIP — the lane's ``restart``
    #: contract (no download at all; the report says so).
    EXPLICIT_RESTART = "restart"
    #: The recoverable state is unknown — no dispatch, an operator decision
    #: (explicit restart or reconciliation) is required.
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ContinuationEvidence:
    """What the control plane PROVES about the dead attempt's recoverable state.

    Every tri-state field is evidence, never assumption: ``None`` means
    unknown (absence of a recording is not a recording of absence). The
    objective fields (everything except *prior_mode_selected*) form the
    decision's reuse digest — the prior mode is audit lineage, not input.
    """

    #: Whether a vendor session provably started. ``None`` = unknown (a
    #: plain timeout with no recorded classification proves nothing).
    vendor_started: bool | None = None
    #: Whether the control plane provably holds a committed checkpoint for
    #: the work. ``None`` = unknown (lookup unavailable).
    checkpoint_committed: bool | None = None
    #: Whether the dead attempt's work was already delivered (a candidate
    #: on record whose publication outcome the run died around).
    candidate_published: bool = False
    #: The operator explicitly asked to discard the WIP
    #: (``/retry <run-id> restart``).
    operator_discard_requested: bool = False
    #: The mode a previously persisted decision selected (audit lineage;
    #: excluded from :meth:`digest`).
    prior_mode_selected: str | None = None

    def digest(self) -> str:
        """A stable digest over the OBJECTIVE fields (prior mode excluded)."""
        payload = json.dumps(
            {
                "vendor_started": self.vendor_started,
                "checkpoint_committed": self.checkpoint_committed,
                "candidate_published": bool(self.candidate_published),
                "operator_discard_requested": bool(self.operator_discard_requested),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ContinuationDecision:
    """The selected continuation source, with the reason it was selected.

    Frozen by construction: built only by :func:`decide_continuation` or
    re-materialized from the persisted document by
    :func:`matching_decision` (*reused=True* — one decision per evidence
    snapshot, repeated retry events do not re-decide).
    """

    mode: ContinuationMode
    reason: str
    evidence: ContinuationEvidence
    decided_at: str
    reused: bool = False

    @property
    def mode_selected(self) -> str:
        """``continuation.mode_selected`` — the observability spelling."""
        return self.mode.value

    @property
    def uncertain(self) -> bool:
        """``continuation.uncertain`` — no dispatch may carry this decision."""
        return self.mode is ContinuationMode.UNCERTAIN

    @property
    def dispatchable(self) -> bool:
        """Whether a dispatch may carry this decision (UNCERTAIN never does)."""
        return self.mode is not ContinuationMode.UNCERTAIN

    def resume_mode(self) -> str:
        """The ``lane_resume_mode`` this decision selects for the dispatch.

        Raises ``ValueError`` for UNCERTAIN — an uncertain decision maps to
        NO dispatch, never to a mode the lane would misread as authority.
        """
        if self.mode is ContinuationMode.UNCERTAIN:
            raise ValueError("an UNCERTAIN continuation selects no resume mode — do not dispatch")
        return self.mode.value

    def as_document(self) -> dict[str, Any]:
        """The persisted ``evidence["continuation"]`` document."""
        return {
            "mode": self.mode.value,
            "mode_selected": self.mode.value,
            "reason": self.reason,
            "decided_at": self.decided_at,
            "evidence_digest": self.evidence.digest(),
            "uncertain": self.uncertain,
            "vendor_started": self.evidence.vendor_started,
            "checkpoint_committed": self.evidence.checkpoint_committed,
            "candidate_published": bool(self.evidence.candidate_published),
            "operator_discard_requested": bool(self.evidence.operator_discard_requested),
            "prior_mode_selected": self.evidence.prior_mode_selected,
            # ``retry.no_checkpoint_baseline`` — the observability flag that
            # a baseline retry was selected with no checkpoint involved.
            "no_checkpoint_baseline": (
                self.mode is ContinuationMode.COMMITTED_BASELINE
                and self.evidence.checkpoint_committed is not True
            ),
        }


# ----------------------------------------------------------------------
# The decision table (Q35-02 — every arm is load-bearing and tested)
# ----------------------------------------------------------------------


def decide_continuation(
    evidence: ContinuationEvidence, *, now: datetime | None = None
) -> ContinuationDecision:
    """Select the continuation source for one retry event, from evidence.

    The table, in precedence order (first matching arm wins):

    1. ``operator_discard_requested`` → EXPLICIT_RESTART — the operator
       explicitly discarded the WIP; preservation is never promised.
    2. ``checkpoint_committed is True`` → EXACT_WIP — an exact committed
       checkpoint IS the authorized continuation; the dispatch carries the
       ``required`` contract the lane's strict restore guard enforces.
    3. ``vendor_started is False`` → COMMITTED_BASELINE — PROVEN no vendor
       session existed, so no WIP can exist; the frozen committed base is
       the honest continuation source (``fresh``).
    4. otherwise (``vendor_started`` None/True, no committed checkpoint) →
       UNCERTAIN — absence of a checkpoint NEVER proves no WIP. When
       ``candidate_published`` is True the reason is terminal guidance (the
       work is already delivered — point at the candidate, do not resume);
       otherwise the operator must choose an explicit restart or reconcile.
    """
    decided_at = (now or datetime.now(timezone.utc)).isoformat()
    if evidence.operator_discard_requested:
        return ContinuationDecision(
            mode=ContinuationMode.EXPLICIT_RESTART,
            reason=(
                "operator explicitly discarded the WIP — re-implementing from the "
                "committed baseline; no preservation is promised"
            ),
            evidence=evidence,
            decided_at=decided_at,
        )
    if evidence.checkpoint_committed is True:
        return ContinuationDecision(
            mode=ContinuationMode.EXACT_WIP,
            reason=(
                "a committed checkpoint exists — the exact WIP checkpoint is the "
                "authorized continuation (the lane's required restore)"
            ),
            evidence=evidence,
            decided_at=decided_at,
        )
    if evidence.vendor_started is False:
        return ContinuationDecision(
            mode=ContinuationMode.COMMITTED_BASELINE,
            reason=(
                "no vendor session ever started (recorded bootstrap classification) — "
                "no WIP can exist, so the retry continues from the committed baseline"
            ),
            evidence=evidence,
            decided_at=decided_at,
        )
    if evidence.candidate_published:
        return ContinuationDecision(
            mode=ContinuationMode.UNCERTAIN,
            reason=(
                "the dead attempt's work is already delivered — this is terminal "
                "guidance, not a resume: reconcile the publication or start a new "
                "implement request instead of re-executing"
            ),
            evidence=evidence,
            decided_at=decided_at,
        )
    started = (
        "a vendor session had started"
        if evidence.vendor_started is True
        else "no proof whether a vendor session started"
    )
    return ContinuationDecision(
        mode=ContinuationMode.UNCERTAIN,
        reason=(
            f"{started} and no committed checkpoint is held — the recoverable state "
            "is unknown (absence of a checkpoint never proves there was no WIP)"
        ),
        evidence=evidence,
        decided_at=decided_at,
    )


def matching_decision(
    document: Mapping[str, Any] | None, evidence: ContinuationEvidence
) -> ContinuationDecision | None:
    """The persisted decision a NEW retry event must reuse, or ``None``.

    Reuse is keyed on the objective evidence digest: a repeated retry event
    over an unchanged recoverable state re-materializes the SAME decision
    (*reused=True*, original ``decided_at`` preserved) instead of
    re-deciding. A materially changed snapshot (a checkpoint appeared or
    vanished, the operator asked to discard, …) re-decides. A corrupt or
    unrecognized document is refused — never guessed.
    """
    if not isinstance(document, Mapping):
        return None
    try:
        if str(document.get("evidence_digest") or "") != evidence.digest():
            return None
        mode = ContinuationMode(str(document.get("mode") or ""))
        reason = str(document.get("reason") or "")
        decided_at = str(document.get("decided_at") or "")
        if not reason or not decided_at:
            return None
    except ValueError:
        return None
    return ContinuationDecision(
        mode=mode,
        reason=reason,
        evidence=evidence,
        decided_at=decided_at,
        reused=True,
    )


# ----------------------------------------------------------------------
# Evidence construction from what the services already record
# ----------------------------------------------------------------------

#: The lane's own environment-bootstrap failure classification (A18): the
#: bootstrap stage died BEFORE any vendor client existed. Terminal reasons
#: carry it as ``harness_infrastructure: harness_bootstrap_failed (...)``.
_VENDOR_NEVER_STARTED_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"harness_bootstrap_failed",
        # R17 bounded discovery exhaustion: no workflow_dispatch run was ever
        # observed — no lane, no vendor session, provably.
        r"dispatch never observed",
    )
)

#: Terminal reason markers proving the vendor stage WAS reached: the driver
#: ran and failed / completed (bootstrap had succeeded).
_VENDOR_STARTED_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"harness_driver_failed",
        r"harness_no_changes",
        r"harness_candidate_invalid",
        r"changeset_invalid",
        r"commit_unknown_outcome",
    )
)

#: Terminal reason markers meaning the dead attempt's work was already
#: delivered: the publication happened and the run died around its outcome.
_CANDIDATE_PUBLISHED_PREFIXES: tuple[str, ...] = ("commit_unknown_outcome",)

#: The additive lane bootstrap classification, should a lane journal it
#: directly onto the run evidence (``evidence["bootstrap"]``: ok | failed).
_BOOTSTRAP_OK = "ok"
_BOOTSTRAP_FAILED = "failed"

#: The operator's explicit-discard keyword on the ``/retry`` note.
_RESTART_KEYWORD_RE = re.compile(r"\brestart\b", re.IGNORECASE)


def operator_discard_requested(note_text: str | None) -> bool:
    """Whether the ``/retry`` note explicitly discards the WIP (``restart``)."""
    return _RESTART_KEYWORD_RE.search(note_text or "") is not None


def evidence_from_record(
    *,
    death_reason: str,
    evidence: Mapping[str, Any] | None = None,
    candidate_shas: Any = None,
    operator_discard_requested: bool = False,
    checkpoint_committed: bool | None = None,
    prior_mode_selected: str | None = None,
) -> ContinuationEvidence:
    """Build the evidence snapshot from the durable record the services hold.

    Vendor evidence comes ONLY from recorded classifications — the terminal
    death reason's markers (``harness_bootstrap_failed``,
    ``harness_driver_failed``, …) and the additive ``bootstrap`` evidence
    key — never from assumptions: an unclassifiable death (a plain
    ``harness_timeout`` with no marker) stays ``None``/unknown.
    """
    record = evidence if isinstance(evidence, Mapping) else {}
    reason = (death_reason or "").strip()
    bootstrap = str(record.get("bootstrap") or "").strip().lower()
    vendor_started: bool | None = None
    if bootstrap == _BOOTSTRAP_FAILED or any(
        marker.search(reason) for marker in _VENDOR_NEVER_STARTED_MARKERS
    ):
        vendor_started = False
    elif bootstrap == _BOOTSTRAP_OK or any(
        marker.search(reason) for marker in _VENDOR_STARTED_MARKERS
    ):
        vendor_started = True
    candidates = list(candidate_shas or [])
    candidate_published = reason.lower().startswith(_CANDIDATE_PUBLISHED_PREFIXES) or bool(
        "superseded by issue edit" in reason.lower() and candidates
    )
    return ContinuationEvidence(
        vendor_started=vendor_started,
        checkpoint_committed=checkpoint_committed,
        candidate_published=candidate_published,
        operator_discard_requested=bool(operator_discard_requested),
        prior_mode_selected=prior_mode_selected,
    )


def durable_checkpoint_lookup(run_id: str) -> bool | None:
    """Checkpoint presence via the plumbing the /retry path already uses.

    Wraps :func:`forge.runs.revival._has_durable_checkpoint` (the local
    checkpoint index, then the lane-control checkpoint API — the same
    authority ``retry_rejection`` consults). Any failure maps to ``None``:
    an unreachable lookup is UNKNOWN, never a proven absence.
    """
    try:
        from forge.runs import revival  # lazy: keeps the module import-light

        return bool(revival._has_durable_checkpoint(run_id))
    except Exception:  # noqa: BLE001 — an unreadable lookup is unknown, not False
        return None


# ----------------------------------------------------------------------
# The operator-facing wording (the ack names the continuation source)
# ----------------------------------------------------------------------


def retry_ack_line(decision: ContinuationDecision) -> str:
    """The ack-note line NAMING the selected source of continuation."""
    if decision.mode is ContinuationMode.EXACT_WIP:
        return "exact WIP checkpoint — the lane restores this run's held checkpoint before its turn"
    if decision.mode is ContinuationMode.COMMITTED_BASELINE:
        return "committed baseline (no WIP existed — no vendor session ever started)"
    if decision.mode is ContinuationMode.EXPLICIT_RESTART:
        return (
            "explicit restart — any unrecorded WIP is discarded by operator choice; "
            "re-implementing from the committed baseline"
        )
    return decision.reason


def uncertain_retry_note(run_id: str, decision: ContinuationDecision) -> str:
    """The no-dispatch operator note for an UNCERTAIN (or delivered) decision.

    The acknowledgement never promises preservation: it states that the
    selected source of continuation is unknown, that NOTHING was dispatched
    (zero vendor sessions), and what the operator can do about it.
    """
    short = run_id[:8]
    head = (
        "retry refused — the work is already delivered"
        if decision.evidence.candidate_published
        else "retry needs an operator decision"
    )
    lines = [
        f"## 🔁 Run `{short}` {head}",
        "",
        f"- Continuation source: **unknown** — {decision.reason}",
        "- Nothing was dispatched: no vendor session started, no workflow ran.",
    ]
    if decision.evidence.candidate_published:
        lines.append(
            "- The delivered candidate stays on record; use `/reconcile` for a lost "
            "publication, or start a new implement request for further work."
        )
    else:
        lines.extend(
            [
                f"- `@forge /retry {run_id} restart` — explicitly discard any "
                "unrecorded WIP and re-implement from the committed baseline;",
                "- or reconcile the execution first (inspect the branch and the "
                "Actions run; `/reconcile` drives lost publications).",
            ]
        )
    lines.extend(["", "*This is an automated message.*"])
    return "\n".join(lines)
