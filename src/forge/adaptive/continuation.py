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

R36-02 (issue #261) tightens the three evidence edges this module trusted:

- the ``restart`` verb is parsed off the COMMAND into a typed
  :class:`RecoveryRequest` — granted only from its documented argument
  position (``/retry <run-id> restart``), never from a mention anywhere in
  the note text (negated, quoted or unrelated);
- a ``dispatch never observed`` death reason is NOT proof no vendor started
  (the response/discovery can be lost while the job ran): the vendor-start
  certainty comes from the PERSISTED native-start intent
  (:func:`forge.adaptive.admission.record_native_start_intent` — no intent
  row on a dead run is the only never-dispatched proof), and discovery
  absence stays UNKNOWN;
- the persisted decision additionally records its lineage — the originating
  attempt, the native command/event identity, the evidence schema version
  and who authorized a discard — so reuse is auditable, not just
  boolean-equal.

R37-01 (issue #282) separates the decision LOOKUP KEY from the evidence
CONTENT digest. The old reuse rule — "equal objective digest ⇒ the same
decision" — let a NEW recovery event for a LATER dead attempt re-use (and
graft) the FIRST attempt's decision whenever the four objective booleans
happened to line up: attempt 1 → checkpoint A → retry E1 approves
continuing from A; attempt 2 → checkpoint B → dies → retry E2 (a NEW
delivery, a NEW source attempt) matched the unchanged booleans and was
handed attempt 1's decision and checkpoint A. The same-command replay
protection was right; treating a new command as that replay was the
defect. Reuse is now keyed on the decision IDENTITY
(:func:`decision_identity` — canonical subject + source attempt + native
command/event id):

- SAME identity + SAME objective digest → an EXACT REPLAY: the frozen
  payload/checkpoint loads (a newer checkpoint landing later changes
  nothing — the graft semantics, correct for a replay);
- SAME identity + a DIFFERENT objective digest → the typed
  :class:`ContinuationConflictError` (surfaced as
  ``continuation.conflicting_recovery_event``), never a silent reuse;
- a NEW identity (a new ``native_command_id`` or ``source_attempt``) NEVER
  matches the old document — the caller decides fresh, and the persisted
  document carries every decision in an identity-keyed ``decisions`` map
  (the latest stays at the top level for pre-R37-01 readers; documents
  without an ``event`` block are read through the legacy adapter).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:  # pragma: no cover — typing only, keeps the module import-light
    from forge.adaptive.checkpoint_repository import CheckpointLookupOutcome

__all__ = [
    "CONTINUATION_EVIDENCE_KEY",
    "DECISION_HISTORY_KEY",
    "DECISION_HISTORY_LIMIT",
    "CheckpointLookup",
    "ContinuationConflictError",
    "ContinuationDecision",
    "ContinuationEvidence",
    "ContinuationMode",
    "EVIDENCE_VERSION",
    "IntentLookup",
    "NATIVE_START_DISPATCHED",
    "NATIVE_START_NEVER_DISPATCHED",
    "NATIVE_START_UNKNOWN",
    "RecoveryRequest",
    "decide_continuation",
    "decision_history",
    "decision_identity",
    "durable_checkpoint_lookup",
    "evidence_from_record",
    "find_decision_by_event",
    "legacy_decision",
    "matching_decision",
    "normalize_checkpoint_result",
    "operator_discard_requested",
    "parse_recovery_request",
    "persisted_document",
    "retry_ack_line",
    "uncertain_retry_note",
]

#: The evidence key the persisted decision lives under on the run row.
CONTINUATION_EVIDENCE_KEY: str = "continuation"

#: The key of the identity-keyed decision HISTORY map inside the persisted
#: ``evidence["continuation"]`` document (R37-01): every decision the run
#: minted, keyed by :func:`decision_identity` — the latest decision ALSO
#: stays at the document's top level so pre-R37-01 readers (the compat
#: fixtures, ``_advance_harness``'s pinned-digest read) are unaffected.
DECISION_HISTORY_KEY: str = "decisions"

#: How many decisions the history map retains (the oldest are dropped
#: first; the active pin set keeps the pinned checkpoints themselves
#: auditable regardless).
DECISION_HISTORY_LIMIT: int = 16

#: The evidence SCHEMA version this build writes (R37-01: 3 — reuse is
#: keyed on the decision IDENTITY, the document carries its own ``event``
#: block and the identity-keyed ``decisions`` map). Purely informational
#: for readers: reuse is governed by the event identity (with the objective
#: digest as a consistency guard), which moves by itself whenever the
#: classification semantics change.
EVIDENCE_VERSION: int = 3

#: An injectable checkpoint-presence provider (ASYNC — R36-03): work id
#: → a :class:`~forge.adaptive.checkpoint_repository.
#: CheckpointLookupOutcome` (the typed modern answer), or a legacy
#: ``True``/``False``/``None`` bool-ish (the tests' shorthand — True
#: reads as exact, False as a proven absence, None as unknown).
CheckpointLookup = Callable[[str], Any]

#: The three verdicts a native-start INTENT lookup may return (R36-02).
#: The persisted intent is the ONLY evidence that may prove a dispatch was
#: never attempted; an intent that IS present proves the provider call was
#: attempted — which still leaves the vendor start UNKNOWN (the response
#: may have been lost after acceptance).
NATIVE_START_NEVER_DISPATCHED = "never_dispatched"
NATIVE_START_DISPATCHED = "dispatched"
NATIVE_START_UNKNOWN = "unknown"

#: An injectable native-start-intent provider (async): run id → one of
#: :data:`NATIVE_START_NEVER_DISPATCHED` / :data:`NATIVE_START_DISPATCHED`
#: / :data:`NATIVE_START_UNKNOWN` (``None`` tolerated as unknown). A
#: raising lookup reads as UNKNOWN — never as proof.
IntentLookup = Callable[[str], Awaitable["str | None"]]


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
class RecoveryRequest:
    """The TYPED ``/retry`` command (R36-02): subject + recovery verb.

    Built only by :func:`parse_recovery_request` — a regex over the COMMAND
    (the first ``/retry`` token group), never a substring scan of the note:
    the ``restart`` verb is granted ONLY from its documented argument
    position (``/retry <run-id> restart``), so a negated ("do not
    restart"), quoted or unrelated mention of the word can never request a
    WIP discard.
    """

    #: The run-id token the command names ("" — the bare ``/retry``).
    requested: str = ""
    #: ``/retry <run-id> restart`` — the operator's explicit discard of the
    #: held WIP, granted only from the documented argument position AND only
    #: when the token addresses the resolved subject.
    restart: bool = False
    #: The resolved subject run the command addresses (audit lineage; the
    #: *requested* token may be a prefix of it).
    run_id: str = ""


@dataclass(frozen=True)
class ContinuationEvidence:
    """What the control plane PROVES about the dead attempt's recoverable state.

    Every tri-state field is evidence, never assumption: ``None`` means
    unknown (absence of a recording is not a recording of absence). The
    objective fields (everything except the lineage block below) form the
    decision's reuse digest; the lineage fields are audit context — who
    asked, which command, which attempt, what the intent lookup said — and
    are deliberately excluded from :meth:`digest` (their decision-relevant
    effect already flows through the objective fields).
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
    #: R36-02 lineage: what the persisted native-start intent lookup said
    #: (one of the ``NATIVE_START_*`` verdicts, ``None`` when never
    #: consulted). Observability, not a table input — its effect on the
    #: decision flows through *vendor_started*.
    native_start_verdict: str | None = None
    #: R36-02 lineage: who authorized the discard (e.g. ``operator:@alice``).
    discard_authorized_by: str | None = None
    #: R36-02 lineage: the native command/event identity the decision was
    #: made for (the ``/retry`` webhook delivery id; ``None`` when the
    #: decision is not event-driven, e.g. the recovery scan's re-drive).
    native_command_id: str | None = None
    #: R36-02 lineage: the originating (dead) attempt's durable generation
    #: the decision continues FROM.
    source_attempt: int | None = None
    #: R36-03 lineage: the EXACT checkpoint the lookup resolved when the
    #: decision was authorized — its content address, the
    #: ``continuation.checkpoint_digest`` observability spelling. A
    #: LATER upload changes nothing about a decision that already holds
    #: one: reuse grafts the ORIGINAL digest back (the dispatched
    #: reference is pinned to what the request approved), which is why
    #: this field is deliberately excluded from :meth:`digest` — a newer
    #: checkpoint is not a changed recoverable STATE, it is newer bytes
    #: for the same "a checkpoint exists" fact.
    checkpoint_digest: str | None = None
    #: R37-01 identity: the canonical subject (the run id) the decision
    #: was minted for — the first input of :func:`decision_identity`.
    #: Lineage, deliberately excluded from :meth:`digest`: the document
    #: lives ON the run row, so the subject is context, not evidence.
    subject: str | None = None

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


#: A sha256 content-address shape — the only digest spelling a persisted
#: decision document may carry for its ``evidence_digest`` to be treated
#: as a REAL recorded digest (anything else marks the document corrupt,
#: never a conflict: a conflict is genuine recorded evidence that moved,
#: not an unreadable one).
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def decision_identity(
    subject: str | None, source_attempt: int | None, native_command_id: str | None
) -> str:
    """The deterministic DECISION IDENTITY (R37-01): sha256 over the
    canonical subject (the run id), the originating source attempt and the
    native recovery-event identity (the webhook delivery id; ``None`` for
    a decision that is not event-driven, e.g. the recovery scan's own
    re-drive).

    Deterministic by construction: the same ``(subject, source_attempt,
    native_command_id)`` triple yields the same id in every process, so a
    restart between decision persistence and dispatch RECONSTRUCTS the
    identical decision BY ID — and a new event (a new delivery id) or a
    new source attempt never collides with the old decision's identity.
    """
    payload = json.dumps(
        {
            "subject": str(subject or ""),
            "source_attempt": (int(source_attempt) if source_attempt is not None else None),
            "native_command_id": (str(native_command_id) if native_command_id else None),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class ContinuationConflictError(Exception):
    """The SAME recovery-event identity arrived with a DIFFERENT objective
    evidence snapshot (R37-01) — the recorded decision names this exact
    event/attempt, but the booleans it was decided over have moved.

    Raised by :func:`matching_decision`. This is
    ``continuation.conflicting_recovery_event``: the caller surfaces the
    conflict (observability) and re-decides; it NEVER silently reuses the
    frozen payload over evidence that no longer supports it.
    """

    #: The decision identity whose recorded evidence disagrees with the
    #: delivered snapshot.
    decision_id: str
    #: The digest the persisted decision recorded.
    recorded_digest: str
    #: The digest the delivered evidence snapshot computed.
    delivered_digest: str

    def __init__(self, *, decision_id: str, recorded_digest: str, delivered_digest: str) -> None:
        self.decision_id = str(decision_id)
        self.recorded_digest = str(recorded_digest)
        self.delivered_digest = str(delivered_digest)
        super().__init__(
            "continuation.conflicting_recovery_event: the recovery event behind "
            f"decision {self.decision_id[:12]} returned with a changed objective "
            f"evidence snapshot (recorded {self.recorded_digest[:12]}, delivered "
            f"{self.delivered_digest[:12]}) — the decision is re-made, never "
            "silently reused"
        )


@dataclass(frozen=True)
class ContinuationDecision:
    """The selected continuation source, with the reason it was selected.

    Frozen by construction: built only by :func:`decide_continuation` or
    re-materialized from the persisted document by
    :func:`matching_decision` (*reused=True* — one decision per recovery
    EVENT, a repeated event does not re-decide).

    R37-01: every decision carries its deterministic :attr:`decision_id`
    (:func:`decision_identity` over the subject, the source attempt and
    the native command/event id) — the identity the persisted document is
    looked up BY, and the identity a restart between decision commit and
    dispatch reconstructs.
    """

    mode: ContinuationMode
    reason: str
    evidence: ContinuationEvidence
    decided_at: str
    reused: bool = False
    #: ``continuation.decision_id`` — the deterministic decision identity.
    decision_id: str = ""
    #: ``reestablished`` when the decision was minted because the caller's
    #: lineage to a prior decision could not be established (the versioned
    #: legacy adapter path) — reconciliation, never guessed equality.
    lineage: str | None = None
    #: The decision id of the LIVE in-flight revival this command's
    #: decision stands behind (same-attempt competing commands collapse
    #: into the existing revival uniqueness; this records the collapse).
    superseded_by: str | None = None

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
        document: dict[str, Any] = {
            "mode": self.mode.value,
            "mode_selected": self.mode.value,
            "reason": self.reason,
            "decided_at": self.decided_at,
            "evidence_digest": self.evidence.digest(),
            # R36-02: the evidence SCHEMA generation this document was
            # written under (informational — the identity governs reuse).
            "evidence_version": EVIDENCE_VERSION,
            "uncertain": self.uncertain,
            "vendor_started": self.evidence.vendor_started,
            "checkpoint_committed": self.evidence.checkpoint_committed,
            "candidate_published": bool(self.evidence.candidate_published),
            "operator_discard_requested": bool(self.evidence.operator_discard_requested),
            "prior_mode_selected": self.evidence.prior_mode_selected,
            # R36-02 lineage — the originating attempt, the native
            # command/event identity, the intent verdict and the discard
            # authority, recorded for reuse and audit. R36-03 adds the
            # pinned checkpoint identity (``continuation.checkpoint_
            # digest``) — the exact bytes the decision approved.
            "source_attempt": self.evidence.source_attempt,
            "native_command_id": self.evidence.native_command_id,
            "native_start_verdict": self.evidence.native_start_verdict,
            "checkpoint_digest": self.evidence.checkpoint_digest,
            "discard_authorized_by": (
                (self.evidence.discard_authorized_by or "operator")
                if self.evidence.operator_discard_requested
                else None
            ),
            # ``retry.no_checkpoint_baseline`` — the observability flag that
            # a baseline retry was selected with no checkpoint involved.
            "no_checkpoint_baseline": (
                self.mode is ContinuationMode.COMMITTED_BASELINE
                and self.evidence.checkpoint_committed is not True
            ),
            # R37-01: the decision IDENTITY and the event block as recorded
            # AT DECISION TIME — reuse looks decisions up BY this identity,
            # never by the content digest alone.
            "decision_id": self.decision_id,
            "event": {
                "source_attempt": self.evidence.source_attempt,
                "native_command_id": self.evidence.native_command_id,
            },
        }
        if self.lineage:
            document["lineage"] = self.lineage
        if self.superseded_by and self.superseded_by != self.decision_id:
            document["superseded_by"] = self.superseded_by
        return document


# ----------------------------------------------------------------------
# The decision table (Q35-02 — every arm is load-bearing and tested)
# ----------------------------------------------------------------------


def decide_continuation(
    evidence: ContinuationEvidence,
    *,
    now: datetime | None = None,
    lineage: str | None = None,
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

    R37-01: the minted decision carries its deterministic identity
    (:func:`decision_identity` over ``evidence.subject``, the source
    attempt and the native command id) and the *lineage* label the caller
    passes (``reestablished`` when the decision had to be re-made because
    no prior decision's lineage was establishable).
    """
    decided_at = (now or datetime.now(timezone.utc)).isoformat()
    identity = decision_identity(
        evidence.subject, evidence.source_attempt, evidence.native_command_id
    )

    def _decision(mode: ContinuationMode, reason: str) -> ContinuationDecision:
        return ContinuationDecision(
            mode=mode,
            reason=reason,
            evidence=evidence,
            decided_at=decided_at,
            decision_id=identity,
            lineage=lineage,
        )

    if evidence.operator_discard_requested:
        return _decision(
            ContinuationMode.EXPLICIT_RESTART,
            "operator explicitly discarded the WIP — re-implementing from the "
            "committed baseline; no preservation is promised",
        )
    if evidence.checkpoint_committed is True:
        return _decision(
            ContinuationMode.EXACT_WIP,
            "a committed checkpoint exists — the exact WIP checkpoint is the "
            "authorized continuation (the lane's required restore)",
        )
    if evidence.vendor_started is False:
        return _decision(
            ContinuationMode.COMMITTED_BASELINE,
            "no vendor session ever started (recorded bootstrap classification) — "
            "no WIP can exist, so the retry continues from the committed baseline",
        )
    if evidence.candidate_published:
        return _decision(
            ContinuationMode.UNCERTAIN,
            "the dead attempt's work is already delivered — this is terminal "
            "guidance, not a resume: reconcile the publication or start a new "
            "implement request instead of re-executing",
        )
    started = (
        "a vendor session had started"
        if evidence.vendor_started is True
        else "no proof whether a vendor session started"
    )
    return _decision(
        ContinuationMode.UNCERTAIN,
        f"{started} and no committed checkpoint is held — the recoverable state "
        "is unknown (absence of a checkpoint never proves there was no WIP)",
    )


def _entry_identity(entry: Mapping[str, Any]) -> tuple[int | None, str | None] | None:
    """One decision document's EVENT IDENTITY ``(source_attempt,
    native_command_id)``, or ``None`` when it cannot be established.

    R37-01 documents carry an explicit ``event`` block; pre-R37-01
    (legacy) documents are read through the versioned adapter — their
    top-level ``source_attempt``/``native_command_id`` fields form the
    synthetic event identity.
    """
    event = entry.get("event")
    if isinstance(event, Mapping):
        attempt = event.get("source_attempt")
        command = event.get("native_command_id")
    else:
        attempt = entry.get("source_attempt")
        command = entry.get("native_command_id")
    if attempt is None:
        normalized_attempt: int | None = None
    elif isinstance(attempt, bool) or not isinstance(attempt, int):
        try:
            normalized_attempt = int(attempt)
        except (TypeError, ValueError):
            return None
    else:
        normalized_attempt = attempt
    normalized_command = str(command) if command else None
    return (normalized_attempt, normalized_command)


def _valid_core(entry: Mapping[str, Any]) -> tuple[ContinuationMode, str, str] | None:
    """The decision core ``(mode, reason, decided_at)`` — ``None`` when the
    entry is corrupt (an invalid mode, a missing reason or stamp): a
    corrupt document is refused, never guessed."""
    try:
        mode = ContinuationMode(str(entry.get("mode") or ""))
    except ValueError:
        return None
    reason = str(entry.get("reason") or "")
    decided_at = str(entry.get("decided_at") or "")
    if not reason or not decided_at:
        return None
    return (mode, reason, decided_at)


def _candidate_entries(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The persisted decision documents to match against: the LATEST
    (top-level) document first, then the identity-keyed history entries."""
    entries: list[Mapping[str, Any]] = [document]
    carried = document.get(DECISION_HISTORY_KEY)
    if isinstance(carried, Mapping):
        entries.extend(entry for entry in carried.values() if isinstance(entry, Mapping))
    return entries


def matching_decision(
    document: Mapping[str, Any] | None, evidence: ContinuationEvidence
) -> ContinuationDecision | None:
    """The persisted decision this recovery EVENT must reuse, or ``None``.

    R37-01: reuse is keyed on the EVENT IDENTITY — a decision is reused
    ONLY when the caller's ``source_attempt`` AND ``native_command_id``
    BOTH equal the persisted decision's recorded event identity. The
    objective evidence digest remains as a CONSISTENCY GUARD:

    - equal identity + equal digest → an EXACT REPLAY: the frozen decision
      re-materializes (*reused=True*, original ``decided_at`` and
      ``decision_id`` preserved, the ORIGINAL pinned ``checkpoint_digest``
      grafted back — a newer checkpoint landing after the decision changes
      nothing about a decision that already holds one);
    - equal identity + a DIFFERENT digest → the typed
      :class:`ContinuationConflictError`
      (``continuation.conflicting_recovery_event``) — never a silent
      reuse, never a silent re-decide;
    - a DIFFERENT identity (a new delivery id, a new source attempt) →
      ``None``: the caller decides fresh, however equal the booleans are.

    Documents without an ``event`` block are LEGACY and read through the
    versioned adapter (their top-level lineage fields form the synthetic
    identity); a corrupt or unrecognized document is refused — never
    guessed.
    """
    if not isinstance(document, Mapping):
        return None
    caller_identity = (evidence.source_attempt, evidence.native_command_id)
    for entry in _candidate_entries(document):
        core = _valid_core(entry)
        if core is None:
            continue  # a corrupt entry is skipped, never guessed
        entry_identity = _entry_identity(entry)
        if entry_identity is None or entry_identity != caller_identity:
            continue
        recorded_digest = str(entry.get("evidence_digest") or "")
        if not recorded_digest:
            continue  # no recorded digest — corrupt, never authority
        if recorded_digest != evidence.digest():
            if _is_hex64(recorded_digest):
                raise ContinuationConflictError(
                    decision_id=str(entry.get("decision_id") or ""),
                    recorded_digest=recorded_digest,
                    delivered_digest=evidence.digest(),
                )
            continue  # not a digest shape — corrupt, not a conflict
        mode, reason, decided_at = core
        attempt, command = entry_identity
        decision_id = str(entry.get("decision_id") or "")
        if not _is_hex64(decision_id):
            # A legacy (pre-R37-01) document carries no recorded identity —
            # derive it from the caller's subject and the entry's own event.
            decision_id = decision_identity(evidence.subject, attempt, command)

        def _graft(key: str, current: Any) -> Any:
            value = entry.get(key)
            return current if value is None else value

        return ContinuationDecision(
            mode=mode,
            reason=reason,
            evidence=replace(
                evidence,
                native_command_id=_graft("native_command_id", evidence.native_command_id),
                source_attempt=_graft("source_attempt", evidence.source_attempt),
                native_start_verdict=_graft("native_start_verdict", evidence.native_start_verdict),
                discard_authorized_by=_graft(
                    "discard_authorized_by", evidence.discard_authorized_by
                ),
                checkpoint_digest=_graft("checkpoint_digest", evidence.checkpoint_digest),
            ),
            decided_at=decided_at,
            reused=True,
            decision_id=decision_id,
        )
    return None


def find_decision_by_event(
    document: Mapping[str, Any] | None, native_command_id: str | None
) -> dict[str, Any] | None:
    """The persisted decision document whose event names *native_command_id*.

    R37-01: the identity the revival recovery re-drive resolves its
    stranded delivery through — the caller establishes the event identity
    from the ``retry_delivery_key`` history (the open revival attempt's
    idempotency key) and looks the committed decision up BY that identity.
    """
    if not native_command_id or not isinstance(document, Mapping):
        return None
    for entry in _candidate_entries(document):
        if _valid_core(entry) is None:
            continue
        identity = _entry_identity(entry)
        if identity is not None and identity[1] == native_command_id:
            return dict(entry)
    return None


def legacy_decision(document: Mapping[str, Any] | None) -> bool:
    """Whether *document* is a pre-R37-01 single-decision document: a valid
    decision core with NO ``event`` block and NO identity-keyed history —
    the shape the versioned adapter reads (its top-level lineage fields
    form the synthetic identity, and a caller that cannot tie to it
    re-decides with ``lineage: reestablished``)."""
    if not isinstance(document, Mapping):
        return False
    return (
        "event" not in document
        and DECISION_HISTORY_KEY not in document
        and _valid_core(document) is not None
    )


def decision_history(document: Mapping[str, Any] | None, *, subject: str = "") -> dict[str, dict]:
    """The identity-keyed decision history carried forward from *document*.

    Pre-R37-01 single documents (no ``decisions`` map) are read through the
    versioned adapter: the legacy decision — when its core is intact — is
    adapted into the map under its DERIVED identity, so the old decision
    and its pin survive audit after the run starts minting identity-keyed
    decisions.
    """
    history: dict[str, dict] = {}
    if not isinstance(document, Mapping):
        return history
    carried = document.get(DECISION_HISTORY_KEY)
    if isinstance(carried, Mapping):
        for key, entry in carried.items():
            if isinstance(entry, Mapping) and _valid_core(entry) is not None:
                record = dict(entry)
                record.setdefault("decision_id", str(key))
                history[str(key)] = record
    if history or _valid_core(document) is None:
        return history
    # The legacy single-document shape: adapt it under its derived identity.
    identity = _entry_identity(document)
    if identity is None:
        return history
    attempt, command = identity
    decision_id = str(document.get("decision_id") or "")
    if not _is_hex64(decision_id):
        decision_id = decision_identity(subject, attempt, command)
    record: dict[str, Any] = {
        "decision_id": decision_id,
        "event": {"source_attempt": attempt, "native_command_id": command},
    }
    for field in (
        "mode",
        "mode_selected",
        "reason",
        "decided_at",
        "evidence_digest",
        "evidence_version",
        "uncertain",
        "checkpoint_digest",
        "source_attempt",
        "native_command_id",
        "native_start_verdict",
        "discard_authorized_by",
        "no_checkpoint_baseline",
        "refusal_code",
    ):
        if field in document:
            record[field] = document[field]
    history[decision_id] = record
    return history


def _history_entry(decision: ContinuationDecision) -> dict[str, Any]:
    """The frozen record one decision contributes to the history map."""
    entry = decision.as_document()
    entry.pop(DECISION_HISTORY_KEY, None)
    return entry


def persisted_document(
    decision: ContinuationDecision,
    prior: Mapping[str, Any] | None = None,
    *,
    superseded_by: str | None = None,
    history_limit: int = DECISION_HISTORY_LIMIT,
) -> dict[str, Any]:
    """The full ``evidence["continuation"]`` document for *decision*.

    The decision's own document stays at the TOP level (the latest — the
    spelling pre-R37-01 readers consume), and the identity-keyed
    ``decisions`` map carries EVERY decision the run minted (the prior
    entries adapted forward, the newest added under its identity, the
    oldest dropped beyond *history_limit*). The old decisions and their
    pins survive audit; reuse looks entries up BY identity.
    """
    document = decision.as_document()
    history = decision_history(prior, subject=decision.evidence.subject or "")
    history[decision.decision_id] = _history_entry(decision)
    while len(history) > max(1, history_limit):
        history.pop(next(iter(history)))
    document[DECISION_HISTORY_KEY] = history
    if superseded_by and superseded_by != decision.decision_id:
        document["superseded_by"] = superseded_by
    return document


# ----------------------------------------------------------------------
# Evidence construction from what the services already record
# ----------------------------------------------------------------------

#: The lane's own environment-bootstrap failure classification (A18): the
#: bootstrap stage died BEFORE any vendor client existed. A PRE-DISPATCH
#: proof that no vendor session started, independent of the intent record
#: (the dispatched job itself reported the bootstrap death). Terminal
#: reasons carry it as ``harness_infrastructure: harness_bootstrap_failed (...)``.
_BOOTSTRAP_FAILED_MARKER = re.compile(r"harness_bootstrap_failed", re.IGNORECASE)

#: R17 bounded discovery exhaustion: no workflow_dispatch run was ever
#: OBSERVED — which is NOT proof that none ran: the dispatch response or
#: the discovery listing can be lost while the job started. R36-02: this
#: marker alone leaves the vendor start UNKNOWN; only the persisted
#: native-start intent (see :func:`evidence_from_record`) may upgrade it to
#: a proven never-dispatched.
_DISPATCH_NOT_OBSERVED_MARKER = re.compile(r"dispatch never observed", re.IGNORECASE)

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

#: The typed ``/retry`` COMMAND grammar (R36-02) — the SAME token shape the
#: services' ``_RETRY_RE`` matches (``/retry [run-id]``), extended with the
#: one documented recovery verb in its argument position:
#: ``/retry <run-id> restart``. The verb is matched ONLY here — a mention
#: anywhere else in the note text is just prose.
_RECOVERY_COMMAND_RE = re.compile(
    r"/retry(?:\s+([0-9a-f]{8,32})\b)?(?:\s+(restart)\b)?", re.IGNORECASE
)

#: The operator's explicit-discard verb on the ``/retry`` command.
_RESTART_VERB = "restart"


def parse_recovery_request(note_text: str | None, run_id: str = "") -> RecoveryRequest:
    """Parse the ``/retry`` note into the typed :class:`RecoveryRequest`.

    ``restart`` is accepted ONLY as the token immediately following the
    command's run-id argument — ``/retry <run-id> restart`` — never from a
    mention elsewhere in the text (negated, quoted or unrelated prose can
    never grant a WIP discard). When *run_id* (the resolved subject) is
    given, the verb additionally must ADDRESS that run (exact or prefix
    match); the bare ``/retry [<id>]`` shape is parsed unchanged.
    """
    match = _RECOVERY_COMMAND_RE.search(note_text or "")
    requested = (match.group(1) or "").lower() if match else ""
    verb = (match.group(2) or "").lower() if match else ""
    subject = (run_id or "").lower()
    addresses_subject = (
        not subject or not requested or subject == requested or subject.startswith(requested)
    )
    restart = verb == _RESTART_VERB and bool(requested) and addresses_subject
    return RecoveryRequest(requested=requested, restart=restart, run_id=subject)


def operator_discard_requested(note_text: str | None) -> bool:
    """Whether the ``/retry`` note explicitly discards the WIP (``restart``).

    R36-02: the verb is honored only from its documented argument position
    (see :func:`parse_recovery_request`) — a substring match anywhere in
    the note ("do not restart", quoted documentation, an unrelated
    mention) is NOT a discard request.
    """
    return parse_recovery_request(note_text).restart


async def _consult_native_start_intent(
    intent_lookup: IntentLookup | None, run_id: str | None
) -> str | None:
    """One native-start intent consultation — UNKNOWN on any failure.

    A missing lookup, a missing run id, a ``None`` verdict or a raising
    provider all read as UNKNOWN: absence of an answer is never proof of
    never-dispatched (R36-02).
    """
    if intent_lookup is None or not run_id:
        return None
    try:
        verdict = await intent_lookup(run_id)
    except Exception:  # noqa: BLE001 — an unreadable lookup is unknown, not proof
        return None
    return str(verdict) if verdict else None


async def evidence_from_record(
    *,
    death_reason: str,
    run_id: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    candidate_shas: Any = None,
    operator_discard_requested: bool = False,
    checkpoint_committed: bool | None = None,
    prior_mode_selected: str | None = None,
    intent_lookup: IntentLookup | None = None,
    discard_authorized_by: str | None = None,
    native_command_id: str | None = None,
    source_attempt: int | None = None,
    checkpoint_digest: str | None = None,
) -> ContinuationEvidence:
    """Build the evidence snapshot from the durable record the services hold.

    Vendor evidence comes ONLY from recorded classifications — the terminal
    death reason's markers (``harness_bootstrap_failed``,
    ``harness_driver_failed``, …) and the additive ``bootstrap`` evidence
    key — never from assumptions: an unclassifiable death (a plain
    ``harness_timeout`` with no marker) stays ``None``/unknown.

    R36-02: a ``dispatch never observed`` reason is treated as what it is —
    DISCOVERY absence, not execution proof. The vendor-start certainty for
    that shape comes from the PERSISTED native-start intent
    (*intent_lookup* over the ``execution_leases`` rows
    :func:`forge.adaptive.admission.record_native_start_intent` wrote):
    no intent row on a dead run PROVES the provider call was never
    attempted; an intent row (or an unreadable lookup) leaves the start
    UNKNOWN even though discovery found nothing. The bootstrap-failure
    classification stays its own pre-dispatch proof either way.

    R36-03: *checkpoint_committed* comes from the TYPED lookup outcome
    (exact → True, absent → False, anything else → None — the caller
    normalizes), and *checkpoint_digest* carries the exact checkpoint's
    content address when the lookup answered ``exact`` — the pinned
    ``continuation.checkpoint_digest`` lineage.

    R37-01: the snapshot's *subject* is the *run_id* — the first input of
    the decision identity (:func:`decision_identity`).
    """
    record = evidence if isinstance(evidence, Mapping) else {}
    reason = (death_reason or "").strip()
    bootstrap = str(record.get("bootstrap") or "").strip().lower()
    vendor_started: bool | None = None
    native_start_verdict: str | None = None
    if bootstrap == _BOOTSTRAP_FAILED or _BOOTSTRAP_FAILED_MARKER.search(reason):
        vendor_started = False
    elif _DISPATCH_NOT_OBSERVED_MARKER.search(reason):
        native_start_verdict = await _consult_native_start_intent(intent_lookup, run_id)
        # Only a PROVEN never-dispatched intent upgrades discovery absence
        # to a proven no-vendor start; "dispatched" stays unknown too (the
        # intent proves the call was attempted, never that it was accepted).
        vendor_started = False if native_start_verdict == NATIVE_START_NEVER_DISPATCHED else None
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
        native_start_verdict=native_start_verdict,
        discard_authorized_by=discard_authorized_by,
        native_command_id=native_command_id,
        source_attempt=source_attempt,
        checkpoint_digest=checkpoint_digest,
        subject=run_id or None,
    )


async def durable_checkpoint_lookup(work_id: str) -> CheckpointLookupOutcome:
    """The TYPED checkpoint-presence lookup through the configured async
    authority (R36-03, issue #262).

    Wraps :func:`forge.runs.revival.durable_checkpoint_outcome` — the
    async selection over the configured repository (session factory →
    the SAME authority upload/resume use; else the authenticated
    checkpoint-channel proxy; else a typed ``unavailable``). The retired
    synchronous chain (raw filesystem index + the legacy work-only
    token, every failure collapsed to ``False``) is gone from this
    path; its explicitly opt-in remnant lives in
    :func:`forge.runs.revival._legacy_http_lookup`.
    """
    from forge.runs import revival  # lazy: keeps the module import-light

    return await revival.durable_checkpoint_outcome(work_id)


def normalize_checkpoint_result(result: Any) -> tuple[bool | None, str | None]:
    """One lookup answer → ``(checkpoint_committed, checkpoint_digest)``.

    The bridge between the TYPED outcome (R36-03) and the evidence
    snapshot's tri-state field: ``exact`` → ``(True, <digest>)``,
    ``absent`` → ``(False, None)``, and EVERYTHING else —
    ``unavailable`` / ``corrupt`` / ``unauthorized`` — → ``(None,
    None)``: an unprovable checkpoint state is never a proven absence.
    A legacy bool-ish answer (``True``/``False``/``None`` from the
    tests' shorthand providers) maps through unchanged.
    """
    state = getattr(result, "state", None)
    if state is not None:
        state = str(state)
        if state == "exact":
            digest = getattr(result, "checkpoint_id", None) or getattr(result, "digest", None)
            return True, (str(digest) if digest else None)
        if state == "absent":
            return False, None
        return None, None
    if isinstance(result, bool):
        return result, None
    return None, None


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
