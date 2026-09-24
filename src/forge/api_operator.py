"""The operator read API (R36-15, R37-02/R37-03, R37-16) — authorized
projection reads, zero writes.

The operator view was a pure model; this router is its live, authenticated
read surface. Three GET routes, all subject-scoped by the caller's token,
all read-only by construction:

- ``GET /operator/runs?subject=<canonical>…&limit=&cursor=`` — a BOUNDED
  page of the runs inside the authorized scope, each with its derived
  state and the blocked/waiting line (the list an operator scans
  first);
- ``GET /operator/runs/{run_id}?subject=<canonical>&limit=&sections=`` —
  the full projection render under R37-16's BOUNDED drill-down: state,
  exact identities, unresolved external effects, the blocked/waiting
  line, thin evidence links, per-section source coverage, projection
  age, admission/lease occupancy, the source-version fence, the ADVISORY
  action hints — plus the diagnostics sections: the per-section read
  bounds (``total_count`` / ``truncated`` / the window served), the
  PENDING commands (kind, age, the CTL-04 world they must still match),
  the native occupancy summary (``occupied_vs_limit`` and the ages of
  the uncertain leases) and the TYPED blocked reasons
  (:func:`~forge.adaptive.operator_view.explain_blocked` — each with its
  evidence link and the safe, non-generic next action);
- ``GET /operator/runs/{run_id}/support-bundle?subject=<canonical>
  &max_bytes=&sections=`` — the exportable
  :class:`~forge.adaptive.support_bundle.SupportBundle` document
  (coverage + digest + every attempt, failed ones included), redaction
  on, its EXPORT SCOPE AND SIZE stated in the ``export`` block, refused
  with a typed ``operator.bundle_too_large`` (413) when the serialized
  document exceeds the requested ``max_bytes`` (default 1 MiB, hard cap
  8 MiB) — narrow with ``?sections=`` instead of truncating evidence.

Every response carries the R37-16 time-to-diagnose observability as
headers: ``operator.query_duration`` (seconds) and
``operator.page_payload_bytes`` (the serialized response body) — the two
numbers the pilot reads to trust that the surface stays bounded as
history accumulates.

**Authentication is the lane-control credential family, reused** (the
same scheme the other adaptive surfaces mount): ``HMAC-SHA256`` under the
server-side ``FORGE_LANE_CONTROL_SECRET``, presented as a bearer token.
R37-02 versions the grant: a v2 token signs the CANONICAL SERIALIZED
SUBJECT SET — ``operator-scope-v2:`` + the canonical JSON of the sorted
:class:`~forge.adaptive.operator_snapshot.CanonicalSubject` ids
(:func:`operator_subject_scope_token`). A canonical subject is provider
family + connection identity + native repository identity (``github/
github.example/owner/repo``, ``gitlab/gitlab.test/123``) — a grant names
THAT, never a display name: the same display name on two connections,
or across provider families, is two different subjects, and the token
for one cannot declare the other's scope. The caller DECLARES the scope
it is asking about (exactly as the lane declares its ``work_id``) and
the server verifies the token signs exactly that scope.

**Legacy grants (v1, name-only) work ONLY through resolution**: a v1
token (``operator:`` + repo full names, :func:`operator_scope_token`)
is accepted only when EVERY declared name resolves to EXACTLY ONE
canonical subject among the CONFIGURED repositories
(``app.state.operator_subjects``) — an ambiguous or unknown name fails
closed with ``operator.legacy_grant_refused`` (403) and the reissue
instruction; never a silent fan-out across connections.

The reader filters by canonical subject BEFORE the expensive per-run
checkpoint/artifact reads, and listing is bounded (``limit`` capped,
``cursor`` continuation bound to the serving scope — replayed under a
different grant it is refused, never reinterpreted). R37-16 bounds the
drill-down the same way: the detail's section reads run under an
explicit ``?limit=`` window (default 20, max 100) with the authority
totals reported beside them, ``?sections=`` selects which sections are
read at all (unselected sections are never queried — their coverage
reads ``unknown``), and NO per-run artifact bytes are ever read (blob
digests only).

Fail-closed like every adaptive surface: mounted unconditionally, but no
secret or no session factory → **503**, never an unauthenticated open;
no bearer → 401; empty declared scope → 400 (there is no wildcard —
least authority); token/scope mismatch → 403; a run outside the
verified scope is a 404 (indistinguishable from unknown). The read-only
actor carries NO control privileges: the action hints render with the
``observer`` role (probe only) and remain hints — execution goes through
the EXISTING guarded command routes, which revalidate authority and the
expected world (a stale action is refused there with the current state
and a safe alternative). The rendered projection is EPHEMERAL (a fresh
version-1 value over rows just read), never a durable CAS ticket.
Rendering performs zero provider writes, zero model calls and zero state
transitions (pinned by a test with a recording checkpoint repository and
row-count checks).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Final

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from forge.adaptive.operator_snapshot import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SECTION_LIMIT,
    MAX_PAGE_SIZE,
    MAX_SECTION_LIMIT,
    SELECTABLE_SECTIONS,
    CanonicalSubject,
    OperatorSnapshot,
    OperatorSnapshotReader,
    subject_from_ref,
)
from forge.adaptive.operator_view import RecoveryActions, explain_blocked, render
from forge.adaptive.support_bundle import COVERAGE_SECTIONS as _SUPPORT_COVERAGE_SECTIONS
from forge.adaptive.support_bundle import SupportBundle
from forge.api_lane_control import _bearer, _secret, lane_control_token, verify_lane_token

logger = logging.getLogger(__name__)

__all__ = [
    "BUNDLE_TOO_LARGE",
    "DEFAULT_MAX_BUNDLE_BYTES",
    "LEGACY_GRANT_REFUSED",
    "MAX_BUNDLE_BYTES",
    "OPERATOR_SCOPE_PREFIX",
    "OPERATOR_SCOPE_V2_PREFIX",
    "OPERATOR_SUBJECTS_STATE",
    "PAGE_PAYLOAD_BYTES_HEADER",
    "QUERY_DURATION_HEADER",
    "operator_router",
    "operator_scope_material",
    "operator_scope_token",
    "operator_subject_material",
    "operator_subject_scope_token",
    "resolve_legacy_grant",
]

#: The LEGACY (v1) operator token's material prefix — name-only grants
#: (R36-15). Kept verbatim so already-minted tokens keep working through
#: :func:`resolve_legacy_grant` — and ONLY through it.
OPERATOR_SCOPE_PREFIX: Final = "operator:"

#: The v2 operator token's material prefix — the canonical serialized
#: subject set (R37-02). A lane work id can never collide with either
#: prefix (work ids are run/workpackage ids, never these strings).
OPERATOR_SCOPE_V2_PREFIX: Final = "operator-scope-v2:"

#: Where the composition (or a test) mounts the checkpoint authority the
#: reader consults. Absent → the checkpoints section reads ``unknown``
#: (never an invented empty history).
CHECKPOINT_REPOSITORY_STATE: Final = "operator_checkpoint_repository"

#: Where the composition (or a test) mounts the CONFIGURED repositories
#: — the canonical subjects a deployment knows (the legacy-name-grant
#: migration resolves names against exactly these, never against a
#: live-database guess). Absent → no configured subjects → a legacy
#: name-only grant fails closed (``operator.legacy_grant_refused``).
OPERATOR_SUBJECTS_STATE: Final = "operator_subjects"

#: Where the composition (or a test) mounts the deployment's
#: :class:`~forge.adaptive.admission.AdmissionPolicy` — the occupancy
#: summary's ``limit`` (``max_active_per_project``). Absent → the limit
#: reads ``null`` (an honest unknown, never an invented bound).
OPERATOR_ADMISSION_POLICY_STATE: Final = "operator_admission_policy"

#: The refusal code a legacy name-only grant that cannot resolve to
#: EXACTLY ONE configured canonical subject answers with (R37-02
#: observability).
LEGACY_GRANT_REFUSED: Final = "operator.legacy_grant_refused"

#: The typed refusal code when a support-bundle export exceeds the
#: requested byte cap (R37-16): the export is REFUSED, never silently
#: truncated — the operator narrows the scope (``?sections=``) or raises
#: the cap within the hard maximum.
BUNDLE_TOO_LARGE: Final = "operator.bundle_too_large"

#: The default and hard-maximum support-bundle export sizes (bytes). The
#: default keeps an unattended export bounded; ``?max_bytes=`` may raise
#: it up to the hard cap and never beyond.
DEFAULT_MAX_BUNDLE_BYTES: Final = 1_048_576
MAX_BUNDLE_BYTES: Final = 8_388_608

#: The bundle sections an export may select (the bundle's coverage
#: vocabulary minus ``questions`` — no durable authority, never
#: selectable). The default export takes them all.
BUNDLE_SECTIONS: Final[tuple[str, ...]] = tuple(
    name for name in _SUPPORT_COVERAGE_SECTIONS if name != "questions"
)

#: The R37-16 time-to-diagnose observability, on every response: the
#: wall-clock seconds the query took and the serialized payload size.
QUERY_DURATION_HEADER: Final = "operator.query_duration"
PAGE_PAYLOAD_BYTES_HEADER: Final = "operator.page_payload_bytes"

operator_router = APIRouter()


def operator_scope_material(repos: Sequence[str]) -> str:
    """The LEGACY (v1) canonical HMAC material — the declared repo full
    names, sorted and comma-joined (kept for already-minted tokens)."""
    cleaned = sorted({str(repo).strip() for repo in repos if str(repo).strip()})
    return OPERATOR_SCOPE_PREFIX + ",".join(cleaned)


def operator_scope_token(secret: str, repos: Sequence[str]) -> str:
    """The LEGACY (v1) operator token for *repos* — the name-only grant
    (kept for already-minted tokens; new grants are v2)."""
    return lane_control_token(secret, operator_scope_material(repos))


def operator_subject_material(subjects: Sequence[CanonicalSubject]) -> str:
    """The v2 canonical HMAC material: the versioned prefix + the
    canonical JSON of the sorted, deduped serialized subject SET — the
    unambiguous authorization representation (no comma-splitting of
    arbitrary identifiers; the set is JSON, order-independent)."""
    refs = sorted({subject.subject_id() for subject in subjects})
    return OPERATOR_SCOPE_V2_PREFIX + json.dumps(refs, separators=(",", ":"))


def operator_subject_scope_token(secret: str, subjects: Sequence[CanonicalSubject]) -> str:
    """The v2 operator token for *subjects* — the lane-control HMAC
    derivation over the canonical subject material (the minting side of
    the R37-02 grant)."""
    return lane_control_token(secret, operator_subject_material(subjects))


def _configured_subjects(request: Request) -> tuple[CanonicalSubject, ...]:
    """The CONFIGURED repositories the legacy name resolution may use —
    mounted by the composition (or a test) on ``app.state``. Absent or
    malformed entries are skipped, never guessed."""
    raw = getattr(request.app.state, OPERATOR_SUBJECTS_STATE, None) or ()
    subjects: list[CanonicalSubject] = []
    for entry in raw:
        if isinstance(entry, CanonicalSubject):
            subjects.append(entry)
        elif isinstance(entry, str):
            try:
                subjects.append(subject_from_ref(entry))
            except ValueError:
                continue  # a malformed CONFIGURED entry is skipped, never a grant
    return tuple(subjects)


def resolve_legacy_grant(
    names: Sequence[str], configured: Sequence[CanonicalSubject]
) -> tuple[CanonicalSubject, ...]:
    """Resolve a name-only (v1) grant to canonical subjects — or refuse.

    Every declared name must match EXACTLY ONE configured canonical
    subject's DISPLAY name: zero matches (an unknown name, or nothing
    configured at all) or more than one (the same display name on two
    connections/providers — precisely the collision a name cannot
    express) raises :class:`ValueError` with the reissue instruction.
    Never a silent fan-out across connections."""
    resolved: list[CanonicalSubject] = []
    collisions: list[str] = []
    unknown: list[str] = []
    for name in dict.fromkeys(str(entry).strip() for entry in names if str(entry).strip()):
        matches = [subject for subject in configured if subject.display == name]
        if len(matches) == 1:
            resolved.append(matches[0])
        elif len(matches) > 1:
            collisions.append(name)
        else:
            unknown.append(name)
    if collisions or unknown:
        detail = []
        if collisions:
            shown = ", ".join(sorted(collisions))
            detail.append(
                f"name(s) {shown!r} match more than one configured repository "
                "(same display name on different connections/providers)"
            )
        if unknown:
            shown = ", ".join(sorted(unknown))
            detail.append(
                f"name(s) {shown!r} match no configured repository (or none are configured)"
            )
        raise ValueError(
            f"legacy name-only grant refused ({LEGACY_GRANT_REFUSED}): "
            + "; ".join(detail)
            + " — reissue the grant as canonical subjects "
            "(operator_subject_scope_token) naming family/connection/native id"
        )
    return tuple(resolved)


def _declared_names(values: list[str]) -> tuple[str, ...]:
    """The legacy declared scope as a canonical tuple (repeated params and
    comma-joined values both read; empty after cleaning is a 400)."""
    joined: list[str] = []
    for value in values:
        joined.extend(part for part in str(value or "").split(","))
    return tuple(sorted({part.strip() for part in joined if part.strip()}))


async def _authorized_subjects(
    request: Request,
    authorization: str | None,
    subject_values: list[str] | None,
    repo_values: list[str] | None,
) -> tuple[tuple[CanonicalSubject, ...], int]:
    """The fail-closed ladder: 503 → 401 → 400 → 403 → the verified
    canonical subject scope, with the grant's scope version (2, or 1
    when a legacy grant resolved)."""
    if not _secret(request):
        raise HTTPException(status_code=503, detail="operator endpoint disabled")
    if getattr(request.app.state, "session_factory", None) is None:
        raise HTTPException(status_code=503, detail="operator endpoint disabled")
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing operator bearer token")
    secret = _secret(request)
    declared_subjects = [value for value in (subject_values or []) if str(value or "").strip()]
    declared_names_list = [value for value in (repo_values or []) if str(value or "").strip()]

    if declared_subjects and declared_names_list:
        raise HTTPException(
            status_code=400,
            detail="declare the scope once: ?subject=<canonical> (v2) or the legacy "
            "?repo=<name>, never both",
        )

    if declared_subjects:
        try:
            subjects = tuple(dict.fromkeys(subject_from_ref(value) for value in declared_subjects))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not verify_lane_token(secret, token, operator_subject_material(subjects)):
            raise HTTPException(
                status_code=403,
                detail="operator token does not scope these canonical subjects",
            )
        return subjects, 2

    names = _declared_names(declared_names_list)
    if not names:
        raise HTTPException(
            status_code=400,
            detail="declare the subject scope with ?subject=<provider>/<connection>/"
            "<native-id> (there is no wildcard)",
        )
    if not verify_lane_token(secret, token, operator_scope_material(names)):
        raise HTTPException(
            status_code=403,
            detail="operator token does not scope these repositories",
        )
    try:
        resolved = resolve_legacy_grant(names, _configured_subjects(request))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    if not resolved:
        raise HTTPException(status_code=400, detail="the declared scope is empty")
    return resolved, 1


def _reader(request: Request) -> OperatorSnapshotReader:
    repository = getattr(request.app.state, CHECKPOINT_REPOSITORY_STATE, None)
    return OperatorSnapshotReader(
        request.app.state.session_factory, checkpoint_repository=repository
    )


def _bounded_limit(value: int | None) -> int:
    return max(1, min(int(value or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))


def _parse_sections(raw: str | None, vocabulary: Sequence[str]) -> tuple[str, ...] | None:
    """The ``?sections=`` selection — a deduped tuple of known names, or
    ``None`` (the default: every section). An unknown or empty selection
    is refused with 400: a bounded drill-down never guesses what its
    caller meant to read."""
    if raw is None:
        return None
    names = [part.strip() for part in str(raw).split(",") if part.strip()]
    if not names:
        raise HTTPException(
            status_code=400,
            detail=f"no sections selected — name at least one of {list(vocabulary)}",
        )
    unknown = sorted({name for name in names if name not in vocabulary})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown section(s) {unknown} — select from {list(vocabulary)}",
        )
    return tuple(dict.fromkeys(names))


def _finish(document: dict[str, Any], started: float, *, route: str) -> JSONResponse:
    """The response with the R37-16 observability attached: the wall-clock
    query duration and the SERIALIZED payload size (the response body
    itself), as headers on every operator response — the numbers the
    pilot reads to trust the surface stays bounded as history grows."""
    response = JSONResponse(content=document)
    duration = max(0.0, time.perf_counter() - started)
    response.headers[QUERY_DURATION_HEADER] = f"{duration:.6f}"
    response.headers[PAGE_PAYLOAD_BYTES_HEADER] = str(len(response.body))
    logger.info(
        "operator.query route=%s %s=%.6f %s=%d",
        route,
        QUERY_DURATION_HEADER,
        duration,
        PAGE_PAYLOAD_BYTES_HEADER,
        len(response.body),
    )
    return response


def _export_json(document: dict[str, Any]) -> bytes:
    """The export measurement — serialized EXACTLY the way the response
    body renders (``JSONResponse.render``'s separators and
    ``ensure_ascii=False``), so the stated ``export.bytes`` is the size
    of the body the operator receives."""
    return json.dumps(
        document, default=str, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _age_seconds(at: str, now: str) -> float | None:
    """Seconds between *now* and the ISO *at* — ``None`` when either
    carries no readable clock (never a synthesized age)."""
    try:
        moment = datetime.fromisoformat(str(at or ""))
        reference = datetime.fromisoformat(str(now or ""))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0.0, (reference - moment).total_seconds())


def _sections_block(snapshot: OperatorSnapshot) -> dict[str, Any]:
    """The R37-16 per-section read bounds: how many rows the AUTHORITY
    holds (``total_count``), how many this render carries, whether the
    window cut any, and the window size served (``null`` = the full
    evidence read)."""
    rendered: dict[str, Any] = {}
    for name, total in sorted(snapshot.section_totals.items()):
        if name == "occupancy":
            returned = len(snapshot.occupancy)
        else:
            returned = len(snapshot.rows.get(name) or [])
        rendered[name] = {
            "total_count": total,
            "returned": returned,
            "truncated": bool(snapshot.section_truncated.get(name, False)),
            "limit": snapshot.section_limit,
        }
    return rendered


def _pending_commands(snapshot: OperatorSnapshot) -> list[dict[str, Any]]:
    """The PENDING command surface — the actionable slice of the control
    history: kind, age and the CTL-04 world (expected plan revision /
    execution epoch) the command must STILL match to apply. A command
    whose world moved expires on the guarded route, never here."""
    pending: list[dict[str, Any]] = []
    for command in snapshot.rows.get("commands") or []:
        status = str(command.get("status") or "")
        if status in {"applied", "checkpointed", "rejected", "expired"}:
            continue
        entry: dict[str, Any] = {
            "command_id": str(command.get("command_id") or ""),
            "kind": str(command.get("kind") or ""),
            "status": status,
            "sequence": command.get("sequence"),
            "created_at": str(command.get("created_at") or ""),
            "age_seconds": _age_seconds(str(command.get("created_at") or ""), snapshot.computed_at),
        }
        if command.get("expected_plan_revision") is not None:
            entry["expected_plan_revision"] = command["expected_plan_revision"]
        if command.get("expected_execution_epoch") is not None:
            entry["expected_execution_epoch"] = command["expected_execution_epoch"]
        pending.append(entry)
    return pending


def _occupancy_summary(request: Request, snapshot: OperatorSnapshot) -> dict[str, Any]:
    """The native occupancy surface (R37-16): the OPEN lease count beside
    the deployment's active-per-project bound (``occupied_vs_limit`` —
    the limit from the mounted admission policy, ``null`` when none is
    mounted: an honest unknown), the per-occupancy counts, and the ages
    of the leases whose occupancy is UNCERTAIN (``dispatched_unknown`` /
    ``draining``) — the capacity the reconciler must prove free."""
    counts: dict[str, int] = {}
    open_leases = 0
    unknown_ages: list[dict[str, Any]] = []
    for row in snapshot.occupancy:
        word = str(row.get("occupancy") or "")
        counts[word] = counts.get(word, 0) + 1
        if not row.get("released_at"):
            open_leases += 1
        if word in ("dispatched_unknown", "draining"):
            unknown_ages.append(
                {
                    "lease_id": str(row.get("lease_id") or ""),
                    "occupancy": word,
                    "age_seconds": _age_seconds(
                        str(row.get("acquired_at") or ""), snapshot.computed_at
                    ),
                }
            )
    policy = getattr(request.app.state, OPERATOR_ADMISSION_POLICY_STATE, None)
    limit: int | None = None
    if policy is not None:
        bound = int(getattr(policy, "max_active_per_project", 0) or 0)
        limit = bound if bound > 0 else None
    return {
        "occupied_vs_limit": {"occupied": open_leases, "limit": limit},
        "counts": counts,
        "unknown_ages": unknown_ages,
    }


@operator_router.get("/operator/runs")
async def list_operator_runs(
    request: Request,
    subject: list[str] | None = Query(None),
    repo: list[str] | None = Query(None),
    limit: int | None = Query(None, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(None),
    authorization: str | None = Header(None),
) -> Any:
    """A bounded page of the runs inside the authorized scope — state,
    blocked/waiting, age. The cursor continues the SAME scope only."""
    started = time.perf_counter()
    scope, scope_version = await _authorized_subjects(request, authorization, subject, repo)
    page_size = _bounded_limit(limit)
    try:
        page = await _reader(request).list_snapshots(
            scope, limit=page_size, cursor=str(cursor or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    runs = []
    for snapshot in page.snapshots:
        projection = snapshot.projection()
        runs.append(
            {
                "run_id": snapshot.run_id,
                "subject": snapshot.subject,
                "subject_id": snapshot.subject_id,
                "state": projection.state,
                "underlying_state": projection.underlying_state,
                "blocked_reason": projection.blocked_reason,
                "waiting_on": projection.waiting_on,
                "summary": projection.summary,
                "updated_at": snapshot.rows["run"].get("updated_at", ""),
                "projection_age_seconds": snapshot.projection_age_s,
                "projection_inconsistent": snapshot.projection_inconsistent,
                "unresolved_effects": len(projection.unresolved_effects),
            }
        )
    return _finish(
        {
            "scope": [entry.subject_id() for entry in scope],
            "scope_version": scope_version,
            "page_size": page.limit,
            "runs": runs,
            "next_cursor": page.next_cursor,
        },
        started,
        route="list",
    )


@operator_router.get("/operator/runs/{run_id}")
async def get_operator_run(
    request: Request,
    run_id: str,
    subject: list[str] | None = Query(None),
    repo: list[str] | None = Query(None),
    limit: int = Query(DEFAULT_SECTION_LIMIT, ge=1, le=MAX_SECTION_LIMIT),
    sections: str | None = Query(None),
    authorization: str | None = Header(None),
) -> Any:
    """One run's full projection render — plus coverage, age, occupancy,
    the source-version fence and the ADVISORY action hints (the observer
    role: probe only).

    R37-16 bounded drill-down: ``?limit=`` windows every history section
    to its LAST N rows (default 20, max 100) with the authority totals
    reported in ``sections``; ``?sections=`` selects which sections are
    read at all (unselected sections read ``unknown`` — never queried,
    never invented). The diagnostics sections render beside the state:
    ``pending_commands``, ``occupancy_summary`` and the typed
    ``blocked_reasons`` with their evidence links and safe next actions.
    """
    started = time.perf_counter()
    scope, _ = await _authorized_subjects(request, authorization, subject, repo)
    selected = _parse_sections(sections, SELECTABLE_SECTIONS)
    try:
        snapshot = await _reader(request).snapshot(
            run_id, scope, sections=selected, section_limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r} within the authorized scope",
        )
    projection = snapshot.projection()
    document = render(projection)
    document["subject"] = snapshot.subject
    document["subject_id"] = snapshot.subject_id
    document["source_coverage"] = dict(snapshot.source_coverage)
    document["projection_age_seconds"] = snapshot.projection_age_s
    document["occupancy"] = [dict(row) for row in snapshot.occupancy]
    document["occupancy_summary"] = _occupancy_summary(request, snapshot)
    document["source_version"] = snapshot.source_version
    document["projection_inconsistent"] = snapshot.projection_inconsistent
    document["sections"] = _sections_block(snapshot)
    document["pending_commands"] = _pending_commands(snapshot)
    document["blocked_reasons"] = [
        reason.as_document()
        for reason in explain_blocked(
            projection,
            coverage=snapshot.source_coverage,
            occupancy=snapshot.occupancy,
            checkpoints=snapshot.rows.get("checkpoints"),
        )
    ]
    document["actions"] = [
        {
            "action": action.action,
            "via": action.via,
            "digest": action.digest,
            "expected_version": action.expected_version,
            "at": action.at,
            "linkage": action.linkage,
        }
        for action in RecoveryActions.plan(projection, actor="operator:read", actor_role="observer")
    ]
    document["actions_advisory"] = (
        "action hints only — execution goes through the guarded command routes, "
        "which revalidate authority and the current world; this render is an "
        "ephemeral projection, not a durable CAS ticket"
    )
    return _finish(document, started, route="detail")


@operator_router.get("/operator/runs/{run_id}/support-bundle")
async def get_operator_support_bundle(
    request: Request,
    run_id: str,
    subject: list[str] | None = Query(None),
    repo: list[str] | None = Query(None),
    max_bytes: int | None = Query(None, ge=1, le=MAX_BUNDLE_BYTES),
    sections: str | None = Query(None),
    authorization: str | None = Header(None),
) -> Any:
    """The exportable support bundle — every attempt (failed included),
    explicit coverage, content digest, redaction on.

    R37-16 export bounds: the document STATES its export scope and size
    in the ``export`` block; a serialized bundle exceeding ``?max_bytes=``
    (default 1 MiB, hard max 8 MiB) is refused with the typed
    ``operator.bundle_too_large`` (413) — narrow with ``?sections=``
    instead of truncating evidence. The bundle reads FULL history
    (evidence completeness — the drill-down windows live on the detail
    route), digests only, never artifact bytes.
    """
    started = time.perf_counter()
    scope, _ = await _authorized_subjects(request, authorization, subject, repo)
    selected = _parse_sections(sections, BUNDLE_SECTIONS)
    cap = int(max_bytes) if max_bytes is not None else DEFAULT_MAX_BUNDLE_BYTES
    try:
        snapshot = await _reader(request).snapshot(
            run_id, scope, sections=selected, section_limit=None
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r} within the authorized scope",
        )
    bundle = SupportBundle.build(snapshot.run_id, snapshot.rows)
    document = bundle.as_document()
    document["subject"] = snapshot.subject
    document["subject_id"] = snapshot.subject_id
    document["source_version"] = snapshot.source_version
    document["projection_inconsistent"] = snapshot.projection_inconsistent
    export: dict[str, Any] = {
        "scope": list(selected) if selected is not None else list(BUNDLE_SECTIONS),
        "sections_selected": selected is not None,
        "bytes": 0,
        "max_bytes": cap,
        "hard_max_bytes": MAX_BUNDLE_BYTES,
        "truncated": False,
    }
    document["export"] = export
    size = 0
    for _ in range(3):  # converge on the byte count the block itself states
        export["bytes"] = size
        size = len(_export_json(document))
    export["bytes"] = size
    if size > cap:
        raise HTTPException(
            status_code=413,
            detail={
                "code": BUNDLE_TOO_LARGE,
                "bytes": size,
                "max_bytes": cap,
                "hard_max_bytes": MAX_BUNDLE_BYTES,
                "hint": (
                    "refused, not truncated — narrow the export with ?sections="
                    f" (from {list(BUNDLE_SECTIONS)}) or raise ?max_bytes= within the hard cap"
                ),
            },
        )
    return _finish(document, started, route="support-bundle")
