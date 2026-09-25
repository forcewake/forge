"""The authorized snapshot reader (R36-15, R37-02/R37-03) — projection
inputs from live rows.

The pure projection (:mod:`forge.adaptive.operator_view`) and the export
pack (:mod:`forge.adaptive.support_bundle`) are shapes over durable rows;
this module is the ONE subject-scoped async reader that assembles those
rows from the durable authorities — the run row, the initial execution
synthesized from it, the revival-attempt records, the control commands
and their deliveries, the checkpoint repository, the pause fence, the
verification verdict in the run's evidence, the publication intents, the
gate approvals and the admission leases. Nothing here derives a state,
renders a document or offers an action — it READS rows and maps them
onto the documented shapes, and it is the only place a live surface
should look for them.

Canonical-subject scoping is the module's first rule (R37-02): the
reader takes an authorized scope of :class:`CanonicalSubject` values —
provider family + connection identity + NATIVE repository identity —
and selects runs through per-provider predicates (GitHub rows by their
full name, GitLab/Azure rows by their own ``project_id`` columns, no
artificial GitHub-column population) followed by an EXACT canonical
match that also compares the recorded connection. A display name is
presentation-only: the same full name on two connections, or the same
name across provider families, is two different subjects, and a grant
for one never selects the other's runs on the list, detail or
support-bundle path (pinned by tests). Out-of-scope and unknown are the
same answer (``None`` / an absent page entry). Listing is BOUNDED: the
candidate query runs with a LIMIT window, the page fills from in-scope
members only, and per-run section reads (the expensive checkpoint /
artifact authorities) happen only for page members.

Coverage honesty is the second rule: the snapshot carries a
``source_coverage`` map — ``present`` (queried, rows exist), ``missing``
(queryed, nothing there) and ``unknown`` (never queried, or the authority
was unreachable) — and a source that was not queried is NEVER reported as
an empty success. ``questions`` has no durable authority today, so it is
``unknown`` by construction; a checkpoint repository that raises
:class:`~forge.adaptive.checkpoint_repository.
CheckpointRepositoryUnavailable` makes ``checkpoints`` ``unknown``, never
"no checkpoint". ``projection_age`` says how old the newest observed row
is — the freshness number an operator reads before trusting the state.

Consistency is the third rule (R37-03): every section query of one run's
assembly runs inside ONE session (a single snapshot on sqlite), fenced by
a ``source_version`` — the per-authority max row versions observed before
and after the sections. When the fence moved (a repair committed while
the reader assembled, possible under READ COMMITTED), the snapshot is
marked ``projection_inconsistent`` and the surfaces render that flag
instead of a confident state. The rendered projection is EPHEMERAL — a
fresh version-1 value over the rows just read, never a durable CAS
ticket; guarded actions re-read the authoritative state.

Bounded drill-down is the fourth rule (R37-16): the per-run section
reads carry explicit windows too. A snapshot read takes a ``sections``
selection (unselected sections are NEVER queried — their coverage stays
``unknown``) and a ``section_limit`` window — attempts keep the LAST N
revival records (plus the synthesized initial), settled commands /
deliveries / publications / approvals / released leases keep the LAST K,
while the actionable slices (pending commands, unresolved publication
effects, open leases) are read whole up to :data:`MAX_PENDING_ROWS`.
Every windowed section records its authority ``total_count`` and whether
the window cut anything, and no method ever reads artifact BYTES —
checkpoints surface their content-addressed digest only.
``section_limit = None`` is the explicit full-history opt-in the
support-bundle export uses (evidence completeness, bounded at the EXPORT
by the byte cap instead).

The documented row mappings (durable column → view shape):

- ``run`` — :class:`~forge.durable.models.FlowRun`, with ``status_reason``
  as ``blocked_reason``. The SUBJECT identity is the derived
  :class:`CanonicalSubject` (provider + recorded connection + native id);
  ``github_repo_full_name`` is the GitHub family's native id and every
  family's DISPLAY spelling — never the authorization key. The optional
  ``active_candidate_sha`` pointer (read from the run's evidence when
  recorded) names the CURRENT candidate; without it the LAST
  ``candidate_shas`` member is current (the append order the services
  write);
- ``attempts`` — the INITIAL execution synthesized from the run row
  (R37-03: rendered without requiring a revival ActionLog, its outcome
  mapped only where the run row proves one — ``failed``/``cancelled``
  terminals — and ``generation: "unknown"`` when no creation-time
  generation is recorded, never the current one copied), followed by
  :class:`~forge.durable.models.ActionLog` rows whose ``retryability``
  is set (the documented durable REVIVAL ATTEMPT record, A11), each
  reading ITS OWN generation from its record (``remote_result``), never
  the run's current ``cancellation_generation``: ``requested →
  executing`` (in flight), ``succeeded → succeeded``, ``failed →
  failed``, ``unknown_outcome → unknown``. The coverage mark describes
  the revival-record authority (observed ActionLog rows), not the
  synthesized initial;
- ``commands`` / ``deliveries`` —
  :class:`~forge.adaptive.mailbox_db.ControlCommandRow` /
  :class:`~forge.adaptive.mailbox_db.ControlCommandDeliveryRow` for the
  run's work, in sequence order; a resume command carries its recorded
  ``checkpoint_ref`` (the ``<work>@<checkpoint_id>`` the ResumeSpec
  pinned);
- ``checkpoints`` — the injected
  :class:`~forge.adaptive.checkpoint_repository.CheckpointRepository`'s
  ACTIVE entry (its content address doubles as the digest), the fence
  word from :class:`~forge.adaptive.pause_fence.PauseFenceRow`
  (``held`` while uncleared, ``cleared`` after a resume), and the
  activation proof: the latest ``resume`` command that reached
  ``applied``/``checkpointed`` AND whose recorded ``checkpoint_ref``
  names THIS entry — an applied resume that named a different checkpoint
  is ``activation: "unmatched-command"`` with ``activated_at: None``
  (R37-03: resume A applied + checkpoint B uploaded never renders B
  activated by A's command);
- ``verifications`` — the run's ``evidence["verification"]`` fragment
  (the unified R02 shape: ``status``/``tested_oid``/``observed_at``);
- ``publications`` — :class:`~forge.durable.models.PublicationIntent`
  rows for the run;
- ``approvals`` — :class:`~forge.durable.models.GateApproval` rows;
- ``occupancy`` — :class:`~forge.adaptive.admission.ExecutionLease` rows
  for the run with the DERIVED occupancy word (the admission/lease
  visibility the operator needs beside the state);
- ``saga`` — derived, never stored: ``partially_published`` exactly when
  unresolved publication intents exist (the same condition the
  projection's unresolved-effects line reports).

READ-ONLY BY CHARTER, exactly like the view it feeds: the reader issues
SELECTs and repository reads only — a test pins that rendering through a
recording repository performs no ``put``/``pin``/``unpin``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from forge.adaptive.admission import ExecutionLease, lease_occupancy
from forge.adaptive.checkpoint_repository import (
    CheckpointRepository,
    CheckpointRepositoryUnavailable,
)
from forge.adaptive.operator_view import (
    UNRESOLVED_PUBLICATION_STATUSES,
    OperatorProjection,
    _as_datetime,
    initial_projection,
)

logger = logging.getLogger(__name__)

__all__ = [
    "COVERAGE_SECTIONS",
    "COVERAGE_UNKNOWN",
    "COVERAGE_MISSING",
    "COVERAGE_PRESENT",
    "DEFAULT_SECTION_LIMIT",
    "MAX_PENDING_ROWS",
    "MAX_SECTION_LIMIT",
    "SELECTABLE_SECTIONS",
    "CanonicalSubject",
    "OperatorSnapshot",
    "OperatorSnapshotReader",
    "PROVIDER_FAMILIES",
    "SnapshotPage",
    "UNRECORDED_CONNECTION",
    "operator_scope_of_run",
    "subject_from_ref",
    "subject_of_run",
]


#: The closed coverage vocabulary (mirrors the support bundle's).
COVERAGE_PRESENT: str = "present"
COVERAGE_MISSING: str = "missing"
COVERAGE_UNKNOWN: str = "unknown"

#: The sections a snapshot tracks coverage for. ``questions`` has no
#: durable authority (the control plane holds open questions in the
#: mailbox payloads, not a queryable table), so it is ``unknown`` by
#: construction — an honest "not observed", never an invented empty set.
COVERAGE_SECTIONS: tuple[str, ...] = (
    "run",
    "attempts",
    "commands",
    "deliveries",
    "checkpoints",
    "verifications",
    "publications",
    "approvals",
    "questions",
    "occupancy",
)

#: ActionLog statuses → the operator attempt vocabulary. A revival
#: attempt's ``requested`` row is an attempt IN FLIGHT (the dispatch leg
#: has not answered); ``unknown_outcome`` stays ``unknown`` — an ending
#: forge cannot prove is never guessed into failed or succeeded.
_ATTEMPT_STATUS: Mapping[str, str] = {
    "requested": "executing",
    "succeeded": "succeeded",
    "failed": "failed",
    "unknown_outcome": "unknown",
}

#: The rung at which a resume's bytes were APPLIED by the lane — the
#: activation proof a ``resumed`` state stands on.
_RESUME_APPLIED_STATUSES: frozenset[str] = frozenset({"applied", "checkpointed"})

#: The provider families a canonical subject may name — mirrors
#: ``forge.runs.composition.PROVIDER_FAMILIES`` exactly (the FlowRun
#: ``provider`` values, migration 012; a test asserts the two stay in
#: sync). A run whose provider is outside this tuple (drill fixtures'
#: ``fake``) carries NO canonical subject — it is outside every scope.
PROVIDER_FAMILIES: tuple[str, ...] = ("gitlab", "github", "azure_devops")

#: The connection marker used when a run row records no connection
#: identity (the single-connection deployment default). A real recorded
#: connection is a host (``gitlab.example``, ``github.example``) — never
#: a slash, so the serialized subject stays splittable.
UNRECORDED_CONNECTION: str = "-"

#: The evidence keys a run row may record its connection/host identity
#: under (``evidence["connection"]`` / ``evidence["tenant"]`` — the
#: RepositoryIdentity vocabulary). First match wins; presentation-only
#: elsewhere.
_RECORDED_CONNECTION_KEYS: tuple[str, ...] = ("connection", "tenant")


def _normalize_connection(value: Any) -> str:
    """A recorded connection value as the comparable host spelling —
    scheme and path stripped, lowercased; ``""`` when nothing readable
    (the caller substitutes :data:`UNRECORDED_CONNECTION`)."""
    text = str(value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].strip()
    return text


@dataclass(frozen=True)
class CanonicalSubject:
    """The authorization identity of ONE repository (R37-02).

    Provider family + connection identity + NATIVE repository identity —
    the same three members :class:`~forge.runs.composition.
    RepositoryContext` composes dispatches from, derived here from the
    run row's OWN persisted columns: GitHub rows name ``owner/repo``
    through ``github_repo_full_name``, GitLab and Azure DevOps rows name
    their provider's numeric subject id through ``project_id`` (no
    GitHub-column population for non-GitHub families). ``display`` is
    presentation-only — it never enters :meth:`subject_id`, so a display
    spelling change (or two connections sharing one display name) cannot
    widen or shift a grant.

    ``connection`` is the host/tenant discriminator recorded on the run
    (``evidence["connection"]``); rows that record none share the
    :data:`UNRECORDED_CONNECTION` marker — the single-connection
    deployment default. The serialized form is
    ``<family>/<connection>/<native_id>`` (native ids may contain
    slashes — GitHub's ``owner/repo`` does; connections never do).
    """

    provider_family: str
    connection: str
    native_id: str
    display: str = ""

    def __post_init__(self) -> None:
        family = str(self.provider_family or "").strip()
        if family not in PROVIDER_FAMILIES:
            raise ValueError(
                f"canonical subject provider family must be one of {PROVIDER_FAMILIES}, "
                f"got {self.provider_family!r}"
            )
        native = str(self.native_id or "").strip()
        if not native:
            raise ValueError("canonical subject native_id must be a non-empty string")
        connection = _normalize_connection(self.connection) or UNRECORDED_CONNECTION
        object.__setattr__(self, "provider_family", family)
        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "native_id", native)
        object.__setattr__(self, "display", str(self.display or ""))

    def subject_id(self) -> str:
        """The comparable serialization — the grant and filter key."""
        return f"{self.provider_family}/{self.connection}/{self.native_id}"

    def __str__(self) -> str:  # pragma: no cover — display helper
        return self.subject_id()


def subject_from_ref(ref: str) -> CanonicalSubject:
    """Parse a serialized canonical subject (``family/connection/native``).

    The inverse of :meth:`CanonicalSubject.subject_id` — the shape a v2
    grant declares and signs. A malformed reference (wrong segment
    count, unknown family, empty native id) is refused loudly: an
    authorization subject is never guessed from a partial spelling.
    """
    parts = str(ref or "").strip().split("/", 2)
    if len(parts) != 3:
        raise ValueError(
            f"not a canonical subject reference ({ref!r}): expected "
            "<provider>/<connection>/<native_id>"
        )
    family, connection, native = (part.strip() for part in parts)
    if not connection:
        raise ValueError(f"canonical subject reference ({ref!r}) names no connection")
    return CanonicalSubject(provider_family=family, connection=connection, native_id=native)


def _recorded_connection_of(run: Any) -> str:
    """The connection identity the run row records, if any."""
    evidence = getattr(run, "evidence", None)
    if isinstance(evidence, Mapping):
        for key in _RECORDED_CONNECTION_KEYS:
            recorded = _normalize_connection(evidence.get(key))
            if recorded:
                return recorded
    return UNRECORDED_CONNECTION


def subject_of_run(run: Any) -> CanonicalSubject | None:
    """The run's CANONICAL subject from its own persisted columns.

    ``None`` when the row carries no subject a grant could name — an
    unknown provider family (drill fixtures), or a GitHub family row
    without the full-name column (pre-migration residue): such a run is
    outside every scope, exactly as before. GitLab and Azure DevOps rows
    derive their subject from ``provider`` + ``project_id`` with NO
    GitHub-column population — the R37-02 read adapter.
    """
    provider = str(getattr(run, "provider", "") or "").strip()
    if provider not in PROVIDER_FAMILIES:
        return None
    if provider == "github":
        native = str(getattr(run, "github_repo_full_name", "") or "").strip()
        display = native
    else:
        project = getattr(run, "project_id", None)
        if project is None:
            return None
        native = str(int(project))
        display = ""
    if not native:
        return None
    return CanonicalSubject(
        provider_family=provider,
        connection=_recorded_connection_of(run),
        native_id=native,
        display=display,
    )


def _norm_subjects(
    subjects: Sequence[CanonicalSubject] | Iterable[CanonicalSubject],
) -> tuple[CanonicalSubject, ...]:
    """The canonical subject scope: deduped by subject id, sorted."""
    by_id: dict[str, CanonicalSubject] = {}
    for subject in subjects:
        by_id[subject.subject_id()] = subject
    return tuple(by_id[key] for key in sorted(by_id))


def _norm_sections(sections: Sequence[str] | None) -> frozenset[str]:
    """The section selection as a comparable frozenset — ``None`` reads
    every selectable section; an empty or unknown name is refused loudly
    (a bounded drill-down never guesses what its caller meant). ``run``
    is always included: a projection without a run is nothing."""
    if sections is None:
        return frozenset(SELECTABLE_SECTIONS)
    chosen: set[str] = set()
    for name in sections:
        cleaned = str(name or "").strip()
        if not cleaned:
            continue
        if cleaned not in SELECTABLE_SECTIONS:
            raise ValueError(
                f"unknown operator section {cleaned!r} — select from {SELECTABLE_SECTIONS}"
            )
        chosen.add(cleaned)
    if not chosen:
        raise ValueError(
            "no operator sections selected — name at least one of "
            f"{tuple(name for name in SELECTABLE_SECTIONS if name != 'run')}"
        )
    chosen.add("run")
    return frozenset(chosen)


def _sequence_key(row: Any) -> tuple[Any, Any]:
    """The journal-order key of one merged bounded-read row — the section's
    own sequencing column (a command's sequence, an intent's or lease's
    creation clock) with the RAW row id breaking ties, so the merged
    pending + tail window stays in journal order (ids stay numeric where
    the authority numbers them — never lexicographic ``"10" < "2"``)."""
    for column in ("sequence", "created_at", "acquired_at"):
        value = getattr(row, column, None)
        if value is not None:
            return (value, getattr(row, "id", ""))
    return (0, getattr(row, "id", ""))


def _iso(moment: datetime | None) -> str:
    """UTC ISO string (naive values read as UTC — sqlite stores UTC)."""
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


#: Where a revival attempt's own generation is recorded — the ActionLog
#: journal's ``remote_result`` document (the writer's completion record).
#: A row whose record carries none reads ``"unknown"`` — NEVER the run's
#: CURRENT ``cancellation_generation`` copied onto history (R37-03).
_GENERATION_KEYS: tuple[str, ...] = ("generation", "cancellation_generation", "attempt_generation")


def _recorded_generation(row: Any) -> Any:
    """The attempt's OWN generation from its record, else ``"unknown"``."""
    result = getattr(row, "remote_result", None)
    if isinstance(result, Mapping):
        for key in _GENERATION_KEYS:
            value = result.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
    return "unknown"


#: The list page bound (R37-02): the default and maximum page sizes the
#: reader and the API serve. A page NEVER pre-materializes the whole
#: scope — the candidate query runs with a LIMIT window and per-run
#: section reads happen only for page members.
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200

#: The bounded drill-down (R37-16): the default and maximum number of
#: HISTORY rows a bounded section read returns per section. The detail
#: surface never reads a run's whole journal — attempts keep the LAST N
#: revival records (plus the synthesized initial), settled commands /
#: deliveries / publications / approvals / leases keep the LAST K, and
#: every bounded section reports its ``total_count`` so an operator sees
#: how much history exists beyond the window. ``None`` (the explicit
#: full-evidence opt-in the support-bundle export uses) reads everything.
DEFAULT_SECTION_LIMIT: int = 20
MAX_SECTION_LIMIT: int = 100

#: The hard cap on the rows a bounded read NEVER windows away — pending
#: commands and unresolved effects are the actionable slice, so they are
#: read whole up to this absolute bound (with ``truncated`` and the
#: total saying so beyond it), never silently dropped to a window.
MAX_PENDING_ROWS: int = 200

#: Command ladder rungs that mean a command has SPENT (``applied`` /
#: ``checkpointed`` landed, ``rejected`` / ``expired`` refused) — the
#: settled history a bounded read windows to its tail. Everything else
#: (``received`` … ``outcome_unknown``) is PENDING: shown whole.
_COMMAND_SETTLED: frozenset[str] = frozenset({"applied", "checkpointed", "rejected", "expired"})

#: The sections a snapshot may read (the ``?sections=`` selection
#: vocabulary). ``run`` is always read (a projection without a run is
#: nothing); ``questions`` has no durable authority and is never
#: selectable — it reads ``unknown`` by construction.
SELECTABLE_SECTIONS: tuple[str, ...] = (
    "run",
    "attempts",
    "commands",
    "deliveries",
    "checkpoints",
    "verifications",
    "publications",
    "approvals",
    "occupancy",
)

#: The sections whose reads are windowed by a bounded ``section_limit``
#: (or full under ``None``). ``run`` and ``verifications`` are bounded by
#: construction (one row each); ``checkpoints`` reads exactly the ACTIVE
#: entry the authority exposes — there is no unbounded checkpoint
#: history read to window.


@dataclass(frozen=True)
class SnapshotPage:
    """One bounded page of a scoped listing.

    ``snapshots`` are the in-scope members of this page (newest first);
    ``next_cursor`` is the opaque continuation (``""`` on the last page)
    — it embeds the serving scope and is refused when replayed under a
    different one. ``limit`` is the page size actually served.
    """

    snapshots: tuple[OperatorSnapshot, ...] = ()
    next_cursor: str = ""
    limit: int = DEFAULT_PAGE_SIZE


def _encode_cursor(scope: Sequence[CanonicalSubject], offset: int) -> str:
    """The opaque continuation token: base64url JSON carrying the member
    OFFSET already served and the exact serving scope (replayed under a
    different grant it is refused, never reinterpreted)."""
    payload = json.dumps(
        {"o": int(offset), "s": [subject.subject_id() for subject in scope]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, scope: Sequence[CanonicalSubject]) -> int:
    """The continuation offset for *scope* — the cursor's embedded scope
    must match EXACTLY (a cursor replayed under a different grant is a
    refusal, never a page); a malformed cursor is a refusal too."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        offset = int(document["o"])
        served = [str(entry) for entry in document["s"]]
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise ValueError("malformed pagination cursor") from None
    if offset < 0:
        raise ValueError("malformed pagination cursor")
    if served != [subject.subject_id() for subject in scope]:
        raise ValueError("pagination cursor does not belong to this subject scope")
    return offset


@dataclass(frozen=True)
class OperatorSnapshot:
    """One run's durable rows plus the honesty metadata around them.

    ``rows`` is the :mod:`forge.adaptive.operator_view` source-rows shape
    (feed it to ``initial_projection`` / ``derive_state`` /
    ``SupportBundle.build`` verbatim); ``source_coverage`` marks every
    section ``present | missing | unknown``; ``projection_age_s`` is how
    many seconds old the NEWEST observed row was at ``computed_at``
    (``None`` when no row carried a readable clock). ``occupancy`` is the
    admission/lease visibility slice (not a projection input — a separate
    fact the operator surface renders beside the state). ``subject_id``
    is the CANONICAL subject (R37-02; ``subject`` stays the display
    spelling). ``source_version`` is the consistency fence observed at
    assembly time and ``projection_inconsistent`` says it MOVED while the
    sections were read (R37-03) — the render is not a confident state
    then, whatever it derives.

    The R37-16 bounded-read bookkeeping rides along:
    ``section_totals`` says how many rows each windowed section holds in
    the AUTHORITY, ``section_truncated`` whether the window cut any, and
    ``section_limit`` the window size served (``None`` = the full-history
    read the support-bundle export asks for).
    """

    run_id: str
    subject: str
    rows: dict[str, Any]
    source_coverage: dict[str, str]
    occupancy: tuple[dict[str, Any], ...] = field(default=())
    computed_at: str = ""
    projection_age_s: float | None = None
    subject_id: str = ""
    source_version: str = ""
    projection_inconsistent: bool = False
    #: The R37-16 bounded-read bookkeeping: how many rows each windowed
    #: section holds in the AUTHORITY (``section_totals``), whether the
    #: window cut any (``section_truncated``), and the window size that
    #: was served (``section_limit`` — ``None`` reads full history).
    section_totals: dict[str, int] = field(default_factory=dict)
    section_truncated: dict[str, bool] = field(default_factory=dict)
    section_limit: int | None = DEFAULT_SECTION_LIMIT

    def projection(self, now: datetime | str | None = None) -> OperatorProjection:
        """The projection over this snapshot's rows (an ephemeral v1)."""
        moment = now if now is not None else (self.computed_at or None)
        return initial_projection(self.rows, moment)


def operator_scope_of_run(run: Any) -> str:
    """The run's DISPLAY subject — the repo full name its durable row
    carries (``""`` when the row has none). Presentation only: the
    authorization identity is :func:`subject_of_run`."""
    return str(getattr(run, "github_repo_full_name", "") or "").strip()


class OperatorSnapshotReader:
    """The one subject-scoped reader assembling projection inputs.

    Every public method takes the authorized CANONICAL subject scope and
    every run query filters by it — ``list_snapshots`` selects runs whose
    subject is IN the scope (through per-provider predicates plus the
    exact canonical match), ``snapshot`` selects THE run by id AND
    subject, and the per-run sections key off a run id one of those
    queries already authorized. A run the scope does not name is
    indistinguishable from a run that does not exist (``None``), on every
    path. Listing is bounded (``limit`` + cursor) and the expensive
    per-run section reads run only for page members, after the scope
    decision.

    R37-16 makes the per-run section reads bounded too: ``snapshot``
    takes a ``sections`` selection (a subset of
    :data:`SELECTABLE_SECTIONS`; unselected sections are never queried
    and read ``unknown``) and a ``section_limit`` window (the LAST N
    history rows per windowed section, ``total_count`` and ``truncated``
    recorded beside them; ``None`` reads full history — the support
    bundle's evidence-completeness opt-in). No method ever reads artifact
    BYTES: checkpoints surface their content-addressed digest only.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        checkpoint_repository: CheckpointRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._checkpoints = checkpoint_repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- the scoped queries -------------------------------------------------

    @staticmethod
    def _candidate_filter(subjects: Sequence[CanonicalSubject]) -> Any:
        """The per-provider SQL predicate selecting each subject's
        CANDIDATE rows — cheap and indexable (GitHub rows by full name,
        GitLab/Azure rows by their own ``project_id``). The recorded
        CONNECTION cannot be expressed here; the exact canonical match
        after the fetch closes that discriminator. A non-GitHub subject
        whose native id is not the numeric column spelling (a full-path
        grant) selects NOTHING in SQL — fail closed, never a crash and
        never a widened match."""
        from forge.durable.models import FlowRun

        clauses = []
        for subject in subjects:
            if subject.provider_family == "github":
                clauses.append(
                    (FlowRun.provider == "github")
                    & (FlowRun.github_repo_full_name == subject.native_id)
                )
                continue
            try:
                project_id = int(subject.native_id)
            except ValueError:
                continue  # a path-shaped native id has no project_id predicate
            clauses.append(
                (FlowRun.provider == subject.provider_family) & (FlowRun.project_id == project_id)
            )
        if not clauses:
            return None
        return or_(*clauses)

    async def list_snapshots(
        self,
        subjects: Sequence[CanonicalSubject],
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str = "",
        section_limit: int | None = DEFAULT_SECTION_LIMIT,
    ) -> SnapshotPage:
        """One bounded page of snapshots for runs inside *subjects*,
        newest first — ``next_cursor`` continues the SAME scope. Each
        member's sections read under the *section_limit* window (the
        list's thin summaries never need a run's whole history)."""
        authorized = _norm_subjects(subjects)
        if not authorized:
            return SnapshotPage(snapshots=(), next_cursor="", limit=max(1, limit))
        from forge.durable.models import FlowRun

        bounded = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        offset = _decode_cursor(cursor, authorized) if cursor else 0
        filter_ = self._candidate_filter(authorized)
        if filter_ is None:  # pragma: no cover — non-empty scopes always filter
            return SnapshotPage(snapshots=(), next_cursor="", limit=bounded)

        window = bounded * 2  # candidates per scan chunk (post-filter may drop some)
        authorized_ids = {entry.subject_id() for entry in authorized}
        skipped = 0
        members: list[Any] = []
        async with self._session_factory() as session:
            scan_at = 0
            while len(members) <= bounded:
                chunk = list(
                    (
                        await session.execute(
                            select(FlowRun)
                            .where(filter_)
                            .order_by(FlowRun.updated_at.desc(), FlowRun.id.desc())
                            .limit(window)
                            .offset(scan_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not chunk:
                    break
                for run in chunk:
                    subject = subject_of_run(run)
                    if subject is None or subject.subject_id() not in authorized_ids:
                        continue  # same display name, different canonical subject
                    if skipped < offset:
                        skipped += 1
                        continue
                    members.append(run)
                    if len(members) > bounded:
                        break
                if len(chunk) < window:
                    break
                scan_at += len(chunk)
        has_more = len(members) > bounded
        members = members[:bounded]
        snapshots = [
            snapshot
            for snapshot in (await self._gather(members, section_limit=section_limit))
            if snapshot is not None
        ]
        next_cursor = _encode_cursor(authorized, offset + len(snapshots)) if has_more else ""
        return SnapshotPage(snapshots=tuple(snapshots), next_cursor=next_cursor, limit=bounded)

    async def snapshot(
        self,
        run_id: str,
        subjects: Sequence[CanonicalSubject],
        *,
        sections: Sequence[str] | None = None,
        section_limit: int | None = DEFAULT_SECTION_LIMIT,
    ) -> OperatorSnapshot | None:
        """The snapshot for *run_id* — ``None`` unless the scope names its
        canonical subject (out-of-scope and unknown are the same answer).

        *sections* selects which sections are read (a subset of
        :data:`SELECTABLE_SECTIONS`; ``run`` is always read; ``None``
        reads all). *section_limit* windows each bounded section's
        history to its LAST N rows with the authority totals recorded
        (``None`` reads full history — the bundle export's opt-in)."""
        authorized = _norm_subjects(subjects)
        if not authorized:
            return None
        from forge.durable.models import FlowRun

        filter_ = self._candidate_filter(authorized)
        if filter_ is None:  # pragma: no cover — non-empty scopes always filter
            return None
        async with self._session_factory() as session:
            run = await session.scalar(select(FlowRun).where(FlowRun.id == run_id, filter_))
        if run is None:
            return None
        subject = subject_of_run(run)
        if subject is None or subject.subject_id() not in {
            entry.subject_id() for entry in authorized
        }:
            return None
        return await self._snapshot_of(run, sections=sections, section_limit=section_limit)

    async def _gather(
        self,
        runs: Sequence[Any],
        *,
        section_limit: int | None = DEFAULT_SECTION_LIMIT,
    ) -> list[OperatorSnapshot | None]:
        """Snapshot every page member (None never happens for members —
        the canonical match already authorized each row)."""
        return [
            await self._snapshot_of(run, sections=None, section_limit=section_limit) for run in runs
        ]

    # -- the assembly ---------------------------------------------------

    async def _snapshot_of(
        self,
        run: Any,
        *,
        sections: Sequence[str] | None = None,
        section_limit: int | None = DEFAULT_SECTION_LIMIT,
    ) -> OperatorSnapshot | None:
        """One run row → the full snapshot (sections scoped by run id).

        Every SQL section reads through ONE session (a consistent
        snapshot on sqlite), fenced by :meth:`_source_version` observed
        before and after the sections — a fence that moved marks the
        snapshot ``projection_inconsistent`` (R37-03): the render says so
        instead of presenting a confident state assembled from mixed
        versions. *sections* selects what is read (unselected sections
        stay ``unknown``); *section_limit* windows the history sections.
        """
        run_id = str(run.id)
        now = self._clock()
        selected = _norm_sections(sections)
        coverage: dict[str, str] = {section: COVERAGE_UNKNOWN for section in COVERAGE_SECTIONS}
        rows: dict[str, Any] = {"run": self._run_row(run)}
        coverage["run"] = COVERAGE_PRESENT
        totals: dict[str, int] = {}
        truncated: dict[str, bool] = {}

        async with self._session_factory() as session:
            await self._assemble_sections(
                session,
                run,
                run_id,
                rows,
                coverage,
                selected=selected,
                section_limit=section_limit,
                totals=totals,
                truncated=truncated,
            )

        leases = rows.pop("_leases", None)
        occupancy: tuple[dict[str, Any], ...] = ()
        if leases is not None:
            occupancy = tuple(self._occupancy_row(row) for row in leases)
            coverage["occupancy"] = COVERAGE_PRESENT if leases else COVERAGE_MISSING

        # -- checkpoints: the ONE injected authority (outside the SQL fence;
        #    its coverage mark is the honesty signal for that authority) ----
        if "checkpoints" in selected:
            checkpoints: list[dict[str, Any]] | None = None
            if self._checkpoints is None:
                coverage["checkpoints"] = COVERAGE_UNKNOWN
            else:
                try:
                    entry = await self._checkpoints.entry(run_id)
                except CheckpointRepositoryUnavailable:
                    logger.warning(
                        "checkpoint authority unavailable for run %s — coverage unknown", run_id
                    )
                    coverage["checkpoints"] = COVERAGE_UNKNOWN
                else:
                    checkpoint = self._checkpoint_row(
                        entry, rows.get("fence", ""), commands_view=rows.get("commands")
                    )
                    checkpoints = [checkpoint]
                    coverage["checkpoints"] = COVERAGE_PRESENT if entry else COVERAGE_MISSING
            if checkpoints is not None:
                rows["checkpoints"] = checkpoints
                totals["checkpoints"] = len(checkpoints)
                truncated["checkpoints"] = False

        # -- saga: DERIVED from unresolved effects, never stored -------------
        unresolved = [
            pub
            for pub in rows.get("publications") or []
            if str(pub.get("status") or "") in UNRESOLVED_PUBLICATION_STATUSES
        ]
        if rows.pop("_intents", None) is not None and unresolved:
            rows["saga"] = {"state": "partially_published", "unresolved": len(unresolved)}
        rows.pop("fence", None)  # the bookkeeping key never reaches the view rows

        subject = subject_of_run(run)
        age = self._age_seconds(rows, now)
        snapshot = OperatorSnapshot(
            run_id=run_id,
            subject=operator_scope_of_run(run),
            rows=rows,
            source_coverage=coverage,
            occupancy=occupancy,
            computed_at=_iso(now),
            projection_age_s=age,
            subject_id=subject.subject_id() if subject else "",
            source_version=str(rows.pop("_source_version", "")),
            projection_inconsistent=bool(rows.pop("_projection_inconsistent", False)),
            section_totals=totals,
            section_truncated=truncated,
            section_limit=section_limit,
        )
        return snapshot

    async def _assemble_sections(
        self,
        session: Any,
        run: Any,
        run_id: str,
        rows: dict[str, Any],
        coverage: dict[str, str],
        *,
        selected: frozenset[str],
        section_limit: int | None,
        totals: dict[str, int],
        truncated: dict[str, bool],
    ) -> None:
        """Read the SQL sections of one run through ONE session, bookkeeping
        the intermediate handles (``commands``, ``fence``, occupancy rows,
        the fence values) inside *rows* for the caller's pure mappings.

        R37-16: every history-bearing section reads BOUNDED — the last
        ``section_limit`` rows (or the whole authority when ``None``) —
        with the authority totals and truncation marks recorded in
        *totals* / *truncated*. The actionable slices (pending commands,
        unresolved effects, open leases) are read whole up to
        :data:`MAX_PENDING_ROWS`, never windowed away. Sections not in
        *selected* are never queried: their coverage stays ``unknown``.
        """
        from forge.adaptive.mailbox_db import ControlCommandDeliveryRow, ControlCommandRow
        from forge.adaptive.pause_fence import PauseFenceRow
        from forge.durable.models import ActionLog, GateApproval, PublicationIntent

        fence_start = await self._source_version(session, run_id)

        # -- attempts: the initial execution + the durable revival records --
        if "attempts" in selected:
            try:
                actions, total = await self._tail(
                    session,
                    select(ActionLog).where(
                        ActionLog.flow_run_id == run_id,
                        ActionLog.retryability.is_not(None),
                    ),
                    journal_order=(ActionLog.created_at.asc(), ActionLog.id.asc()),
                    window_order=(ActionLog.created_at.desc(), ActionLog.id.desc()),
                    limit=section_limit,
                )
            except SQLAlchemyError:
                logger.warning("attempt rows for run %s unreadable — coverage unknown", run_id)
                actions = None
            if actions is not None:
                # R37-03: the INITIAL execution renders without requiring a
                # revival ActionLog; each revival reads ITS OWN generation.
                rows["attempts"] = [self._initial_attempt_row(run)] + [
                    self._attempt_row(row) for row in actions
                ]
                # the coverage mark describes the revival-record AUTHORITY (the
                # observed ActionLog rows) — the initial row is derived from the
                # run row, whose own coverage is "run". The total counts the
                # durable revival rows (the initial execution is synthesized).
                coverage["attempts"] = COVERAGE_PRESENT if actions else COVERAGE_MISSING
                totals["attempts"] = total
                truncated["attempts"] = section_limit is not None and total > len(actions)

        # -- commands + deliveries (the control history) --------------------
        if "commands" in selected:
            try:
                commands, total, cut = await self._pending_plus_tail(
                    session,
                    base=select(ControlCommandRow).where(ControlCommandRow.work_id == run_id),
                    pending=(ControlCommandRow.status.notin_(_COMMAND_SETTLED),),
                    settled=(ControlCommandRow.status.in_(_COMMAND_SETTLED),),
                    order=(ControlCommandRow.sequence.asc(), ControlCommandRow.id.asc()),
                    tail_order=(ControlCommandRow.sequence.desc(), ControlCommandRow.id.desc()),
                    limit=section_limit,
                )
            except SQLAlchemyError:
                logger.warning("command rows for run %s unreadable — coverage unknown", run_id)
                commands = None
            if commands is not None:
                rows["commands"] = [self._command_row(row) for row in commands]
                coverage["commands"] = COVERAGE_PRESENT if commands else COVERAGE_MISSING
                totals["commands"] = total
                truncated["commands"] = cut
        if "deliveries" in selected:
            try:
                deliveries, total = await self._tail(
                    session,
                    select(ControlCommandDeliveryRow).where(
                        ControlCommandDeliveryRow.work_id == run_id
                    ),
                    journal_order=(
                        ControlCommandDeliveryRow.command_id.asc(),
                        ControlCommandDeliveryRow.recipient.asc(),
                    ),
                    window_order=(
                        ControlCommandDeliveryRow.command_id.desc(),
                        ControlCommandDeliveryRow.recipient.desc(),
                    ),
                    limit=section_limit,
                )
            except SQLAlchemyError:
                logger.warning("delivery rows for run %s unreadable — coverage unknown", run_id)
                deliveries = None
            if deliveries is not None:
                rows["deliveries"] = [self._delivery_row(row) for row in deliveries]
                coverage["deliveries"] = COVERAGE_PRESENT if deliveries else COVERAGE_MISSING
                totals["deliveries"] = total
                truncated["deliveries"] = section_limit is not None and total > len(deliveries)

        # -- the pause fence (the safely_paused evidence) --------------------
        if "checkpoints" in selected:
            try:
                fence_row = await session.scalar(
                    select(PauseFenceRow).where(PauseFenceRow.work_id == run_id)
                )
            except SQLAlchemyError:
                logger.warning("pause fence for run %s unreadable — fence unknown", run_id)
                fence_row = ...
            if fence_row is ...:
                rows["fence"] = ""
            elif fence_row is None:
                rows["fence"] = ""
            else:
                rows["fence"] = "cleared" if fence_row.cleared_at is not None else "held"

        # -- publications + approvals ---------------------------------------
        if "publications" in selected:
            try:
                intents, total, cut = await self._pending_plus_tail(
                    session,
                    base=select(PublicationIntent).where(PublicationIntent.run_id == run_id),
                    pending=(PublicationIntent.status.in_(UNRESOLVED_PUBLICATION_STATUSES),),
                    settled=(PublicationIntent.status.notin_(UNRESOLVED_PUBLICATION_STATUSES),),
                    order=(PublicationIntent.created_at.asc(), PublicationIntent.id.asc()),
                    tail_order=(PublicationIntent.created_at.desc(), PublicationIntent.id.desc()),
                    limit=section_limit,
                )
            except SQLAlchemyError:
                logger.warning("publication rows for run %s unreadable — coverage unknown", run_id)
                intents = None
            rows["_intents"] = intents
            if intents is not None:
                rows["publications"] = [self._publication_row(row) for row in intents]
                coverage["publications"] = COVERAGE_PRESENT if intents else COVERAGE_MISSING
                totals["publications"] = total
                truncated["publications"] = cut
        if "approvals" in selected:
            try:
                approvals, total = await self._tail(
                    session,
                    select(GateApproval).where(GateApproval.flow_run_id == run_id),
                    journal_order=(GateApproval.created_at.asc(), GateApproval.id.asc()),
                    window_order=(GateApproval.created_at.desc(), GateApproval.id.desc()),
                    limit=section_limit,
                )
            except SQLAlchemyError:
                logger.warning("approval rows for run %s unreadable — coverage unknown", run_id)
                approvals = None
            if approvals is not None:
                rows["approvals"] = [self._approval_row(row) for row in approvals]
                coverage["approvals"] = COVERAGE_PRESENT if approvals else COVERAGE_MISSING
                totals["approvals"] = total
                truncated["approvals"] = section_limit is not None and total > len(approvals)

        # -- occupancy: the admission/lease slice ----------------------------
        if "occupancy" in selected:
            try:
                leases, total, cut = await self._pending_plus_tail(
                    session,
                    base=select(ExecutionLease).where(ExecutionLease.run_id == run_id),
                    pending=(ExecutionLease.released_at.is_(None),),
                    settled=(ExecutionLease.released_at.is_not(None),),
                    order=(ExecutionLease.acquired_at.asc(), ExecutionLease.id.asc()),
                    tail_order=(ExecutionLease.acquired_at.desc(), ExecutionLease.id.desc()),
                    limit=section_limit,
                )
            except SQLAlchemyError:
                logger.warning("lease rows for run %s unreadable — coverage unknown", run_id)
                leases = None
            rows["_leases"] = leases
            if leases is not None:
                totals["occupancy"] = total
                truncated["occupancy"] = cut

        # -- verifications: the run's unified evidence fragment -------------
        if "verifications" in selected:
            evidence = dict(getattr(run, "evidence", None) or {})
            fragment = evidence.get("verification")
            if isinstance(fragment, Mapping) and fragment:
                rows["verifications"] = [self._verification_row(fragment)]
                coverage["verifications"] = COVERAGE_PRESENT
            else:
                rows["verifications"] = []
                coverage["verifications"] = COVERAGE_MISSING

        fence_end = await self._source_version(session, run_id)
        rows["_source_version"] = fence_end
        rows["_projection_inconsistent"] = fence_start != fence_end

    # -- the bounded read helpers (R37-16) ----------------------------------

    @staticmethod
    async def _count(session: Any, statement: Any) -> int:
        """The authority's total for one filtered SELECT (an aggregate —
        never a row materialization)."""
        filtered = statement.with_only_columns(func.count(), maintain_column_froms=True)
        return int(await session.scalar(filtered) or 0)

    async def _tail(
        self,
        session: Any,
        statement: Any,
        *,
        journal_order: tuple[Any, ...],
        window_order: tuple[Any, ...],
        limit: int | None,
    ) -> tuple[list[Any], int]:
        """The LAST *limit* rows of *statement* in journal order, with the
        authority total — the bounded history window (``None`` = all, in
        journal order). *window_order* is the SAME columns newest-first
        (the LIMIT scan); *journal_order* is the append order the merged
        rows keep."""
        total = await self._count(session, statement)
        if limit is None:
            ordered = statement.order_by(*journal_order)
            return list((await session.execute(ordered)).scalars().all()), total
        windowed = statement.order_by(*window_order).limit(limit)
        newest_first = list((await session.execute(windowed)).scalars().all())
        newest_first.reverse()
        return newest_first, total

    async def _pending_plus_tail(
        self,
        session: Any,
        *,
        base: Any,
        pending: tuple[Any, ...],
        settled: tuple[Any, ...],
        order: tuple[Any, ...],
        tail_order: tuple[Any, ...],
        limit: int | None,
    ) -> tuple[list[Any], int, bool]:
        """The actionable slice whole plus the history tail: every PENDING
        row (up to :data:`MAX_PENDING_ROWS` — they are the rows an
        operator acts on, never windowed away silently) followed by the
        LAST *limit* settled rows, merged in journal order.

        Returns the merged rows, the authority total across both slices,
        and whether either bound cut anything (the honest ``truncated``).
        """
        pending_stmt = base.where(*pending)
        settled_stmt = base.where(*settled)
        pending_total = await self._count(session, pending_stmt)
        settled_total = await self._count(session, settled_stmt)
        total = pending_total + settled_total
        pending_rows = list(
            (await session.execute(pending_stmt.order_by(*order).limit(MAX_PENDING_ROWS)))
            .scalars()
            .all()
        )
        cut = pending_total > len(pending_rows)
        if limit is None:
            settled_rows = list(
                (await session.execute(settled_stmt.order_by(*order))).scalars().all()
            )
        elif limit > 0:
            windowed = settled_stmt.order_by(*tail_order).limit(limit)
            settled_rows = list((await session.execute(windowed)).scalars().all())
            settled_rows.reverse()
            cut = cut or settled_total > len(settled_rows)
        else:
            settled_rows = []
            cut = cut or settled_total > 0
        merged = sorted(pending_rows + settled_rows, key=lambda row: _sequence_key(row))
        return merged, total, cut

    async def _source_version(self, session: Any, run_id: str) -> str:
        """The consistency fence (R37-03's ``operator.source_version``):
        the per-authority max row versions for this run. On sqlite the
        single-session assembly is already a snapshot, so the start/end
        comparison is stability itself; under READ COMMITTED it is the
        explicit fence that turns a mid-assembly repair commit into
        ``projection_inconsistent`` instead of a confident mixed state."""
        from forge.adaptive.mailbox_db import ControlCommandDeliveryRow, ControlCommandRow
        from forge.adaptive.pause_fence import PauseFenceRow
        from forge.durable.models import ActionLog, FlowRun, GateApproval, PublicationIntent

        run_updated = await session.scalar(select(FlowRun.updated_at).where(FlowRun.id == run_id))
        parts = [f"run:{_iso(run_updated)}"]
        for statement in (
            select(
                func.max(ActionLog.created_at),
                func.count(ActionLog.id),
            ).where(ActionLog.flow_run_id == run_id),
            select(
                func.max(ControlCommandRow.created_at),
                func.count(ControlCommandRow.id),
            ).where(ControlCommandRow.work_id == run_id),
            select(
                func.max(ControlCommandDeliveryRow.created_at),
                func.count(ControlCommandDeliveryRow.command_id),
            ).where(ControlCommandDeliveryRow.work_id == run_id),
            select(
                func.max(PublicationIntent.updated_at),
                func.count(PublicationIntent.id),
            ).where(PublicationIntent.run_id == run_id),
            select(
                func.max(GateApproval.created_at),
                func.count(GateApproval.id),
            ).where(GateApproval.flow_run_id == run_id),
            select(
                func.max(ExecutionLease.acquired_at),
                func.count(ExecutionLease.id),
            ).where(ExecutionLease.run_id == run_id),
            select(
                func.max(PauseFenceRow.fenced_at),
                func.max(PauseFenceRow.cleared_at),
                func.count(PauseFenceRow.work_id),
            ).where(PauseFenceRow.work_id == run_id),
        ):
            row = (await session.execute(statement)).one_or_none()
            parts.append("|".join("" if value is None else str(value) for value in row or ()))
        return ";".join(parts)

    # -- the row mappings (durable column → view shape) -------------------

    @staticmethod
    def _run_row(run: Any) -> dict[str, Any]:
        evidence = dict(run.evidence or {})
        row: dict[str, Any] = {
            "id": str(run.id),
            "status": str(run.status or ""),
            "base_sha": str(run.base_sha or ""),
            "candidate_shas": [str(sha) for sha in (run.candidate_shas or [])],
            "plan_digest": str(run.plan_digest or ""),
            "evidence": evidence,
            "blocked_reason": str(run.status_reason or ""),
            "cancel_requested": bool(run.cancel_requested),
            "created_at": _iso(run.created_at),
            "updated_at": _iso(run.updated_at),
        }
        # The CURRENT-candidate pointer (R37-03): the run row's explicit
        # active candidate where recorded, else absent — the view then
        # falls back to the LAST candidate_shas member (the append order
        # the services write). Never the first member by accident.
        active = evidence.get("active_candidate_sha")
        if isinstance(active, str) and active.strip():
            row["active_candidate_sha"] = active.strip()
        # R38-15: the #302 finalization markers' slice — where a harness
        # cycle journals the lane outcome on the run's evidence
        # (``FORGE_LANE_OUTCOME:{driver_exit, collector_exit,
        # candidate_state}``, landing in the harness fragment or at the
        # top level), the run row carries it as the documented
        # ``lane_outcome`` shape the recovery surface's delivery outcome
        # derives from. Nothing is invented: no recorded marker, no slice.
        harness = evidence.get("harness") if isinstance(evidence.get("harness"), Mapping) else {}
        lane_outcome: dict[str, Any] = {}
        for key in ("driver_exit", "collector_exit", "candidate_state"):
            for source in (harness, evidence):
                value = source.get(key)
                if value not in (None, ""):
                    lane_outcome[key] = value
                    break
        if lane_outcome:
            row["lane_outcome"] = lane_outcome
        return row

    @staticmethod
    def _initial_attempt_row(run: Any) -> dict[str, Any]:
        """The INITIAL execution, synthesized from the run row (R37-03).

        Rendered without requiring a revival ActionLog. Its outcome maps
        ONLY where the run row proves one — the ``failed``/``cancelled``
        terminals; every other lifecycle leaves the outcome ABSENT (the
        view reads absent as ``unknown`` — an ending forge cannot prove
        is never guessed). Its generation is the run's CREATION-time
        generation where recorded, else ``"unknown"`` — never the
        CURRENT ``cancellation_generation`` copied onto history.
        """
        status_word = str(run.status or "")
        outcome = ""
        if status_word == "failed":
            outcome = "failed"
        elif status_word == "cancelled":
            outcome = "cancelled"
        row: dict[str, Any] = {
            "attempt_id": f"run:{run.id}:initial",
            "kind": "initial_execution",
            "started_at": _iso(run.created_at),
            "updated_at": _iso(run.updated_at),
            "generation": "unknown",
        }
        if outcome:
            row["status"] = outcome
        return row

    @staticmethod
    def _attempt_row(row: Any) -> dict[str, Any]:
        status = _ATTEMPT_STATUS.get(str(row.status or ""), "unknown")
        started = _iso(row.created_at)
        return {
            "attempt_id": f"action:{row.id}",
            "kind": str(row.action_kind or ""),
            "status": status,
            "started_at": started,
            "updated_at": started,
            # R37-03: EACH attempt's own generation, from its own record —
            # the revival journal's ``remote_result`` where the writer
            # recorded one; ``"unknown"`` when the record carries none.
            # Never the run's CURRENT cancellation_generation copied onto
            # history.
            "generation": _recorded_generation(row),
        }

    @staticmethod
    def _command_row(row: Any) -> dict[str, Any]:
        payload = getattr(row, "payload", None)
        command: dict[str, Any] = {
            "command_id": str(row.id),
            "work_id": str(row.work_id),
            "kind": str(row.kind or ""),
            "status": str(row.status or ""),
            "sequence": int(row.sequence),
            "actor_ref": str(row.actor_ref or ""),
            "actor_origin": str(row.actor_origin or ""),
            "created_at": _iso(row.created_at),
            "applied_at": _iso(row.applied_at),
        }
        # R37-03: a resume command's recorded checkpoint reference — the
        # durable ``<work_id>@<checkpoint_id>`` its ResumeSpec pinned. The
        # activation match below reads exactly this; an applied resume
        # without one is an absent receipt, never a restoration proof.
        if isinstance(payload, Mapping):
            checkpoint_ref = str(payload.get("checkpoint_ref") or "").strip()
            if checkpoint_ref:
                command["checkpoint_ref"] = checkpoint_ref
        # R37-16: the CTL-04 world a PENDING command must still match at
        # apply time — the expected-version slice the operator surface
        # shows beside a pending command's kind and age.
        for column in ("expected_plan_revision", "expected_execution_epoch"):
            value = getattr(row, column, None)
            if value is not None:
                command[column] = int(value)
        return command

    @staticmethod
    def _delivery_row(row: Any) -> dict[str, Any]:
        return {
            "command_id": str(row.command_id),
            "recipient": str(row.recipient or ""),
            "status": str(row.status or ""),
            "created_at": _iso(row.created_at),
        }

    @staticmethod
    def _checkpoint_row(
        entry: Mapping[str, Any] | None,
        fence: str,
        commands_view: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        """The ACTIVE checkpoint as the view reads it (R37-03).

        The entry's content address IS the digest (the store is
        content-addressed); ``committed_at`` is the landing the entry
        records. ``activated_at`` is the activation receipt — the LATEST
        resume command that reached ``applied``/``checkpointed`` (the
        rung where the lane applied the restored bytes) AND whose
        recorded ``checkpoint_ref`` names THIS entry's checkpoint id. An
        applied resume that named a DIFFERENT checkpoint leaves
        ``activated_at: None`` with ``activation: "unmatched-command"``
        — resume A applied + checkpoint B uploaded never renders B
        activated by A's command. No applied resume at all is the empty
        claim (``activation: ""``).
        """
        matched_at: str | None = None
        unmatched = False
        if commands_view is not None:
            for command in reversed(commands_view):
                if (
                    str(command.get("kind") or "") != "resume"
                    or str(command.get("status") or "") not in _RESUME_APPLIED_STATUSES
                ):
                    continue
                recorded = str(command.get("checkpoint_ref") or "")
                referenced = recorded.partition("@")[2].strip() if recorded else ""
                if (
                    referenced
                    and entry is not None
                    and referenced == str(entry.get("checkpoint_id") or "")
                ):
                    matched_at = str(command.get("applied_at") or "")
                    unmatched = False
                    break
                unmatched = True  # an applied ACK — but not for this checkpoint
        activation = (
            "matched" if matched_at is not None else ("unmatched-command" if unmatched else "")
        )
        if not entry:
            return {
                "checkpoint_id": "",
                "digest": "",
                "committed_at": "",
                "activated_at": matched_at,
                "activation": activation,
                "fence": fence,
                "sequence": None,
            }
        return {
            "checkpoint_id": str(entry.get("checkpoint_id") or ""),
            "digest": str(entry.get("checkpoint_id") or ""),
            "committed_at": str(entry.get("uploaded_at") or ""),
            "activated_at": matched_at,
            "activation": activation,
            "fence": fence,
            "sequence": entry.get("sequence"),
        }

    @staticmethod
    def _verification_row(fragment: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "verification_id": "run-evidence:verification",
            "result": str(fragment.get("status") or ""),
            "candidate_sha": str(fragment.get("tested_oid") or ""),
            "producer": str(fragment.get("producer") or ""),
            "at": str(fragment.get("observed_at") or ""),
        }

    @staticmethod
    def _publication_row(row: Any) -> dict[str, Any]:
        return {
            "operation_key": str(row.operation_key or ""),
            "status": str(row.status or ""),
            "operation": str(row.operation or ""),
            "target_ref": str(row.target_ref or ""),
            "at": _iso(row.updated_at or row.created_at),
        }

    @staticmethod
    def _approval_row(row: Any) -> dict[str, Any]:
        return {
            "approved_by": f"user:{row.approver_user_id}",
            "generation": int(row.generation),
            "at": _iso(row.created_at),
            "consumed_at": _iso(row.consumed_at),
        }

    @staticmethod
    def _occupancy_row(row: ExecutionLease) -> dict[str, Any]:
        return {
            "lease_id": str(row.id),
            "project_id": int(row.project_id),
            "provider": str(row.provider or ""),
            "slot": int(row.slot),
            "occupancy": lease_occupancy(row).value,
            "acquired_at": _iso(row.acquired_at),
            "released_at": _iso(row.released_at),
            "native_intent_ref": str(row.native_intent_ref or ""),
            "native_handle": str(row.native_handle or ""),
        }

    @staticmethod
    def _age_seconds(rows: Mapping[str, Any], now: datetime) -> float | None:
        """Seconds between *now* and the NEWEST row timestamp — ``None``
        when nothing carried a readable clock (ISO strings parse; an
        unreadable value never becomes a synthesized age)."""
        stamps: list[datetime] = []
        for key, value in rows.items():
            if isinstance(value, Mapping):
                candidates: Iterable[Any] = [value]
            elif isinstance(value, (list, tuple)):
                candidates = [row for row in value if isinstance(row, Mapping)]
            else:
                continue
            for view in candidates:
                for column in ("updated_at", "created_at", "committed_at", "at", "applied_at"):
                    moment = _as_datetime(view.get(column))
                    if moment is not None:
                        stamps.append(moment)
        if not stamps:
            return None
        return max(0.0, (now - max(stamps)).total_seconds())
