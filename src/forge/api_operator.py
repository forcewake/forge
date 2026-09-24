"""The operator read API (R36-15) — authorized projection reads, zero writes.

The operator view was a pure model; this router is its live, authenticated
read surface. Three GET routes, all subject-scoped by the caller's token,
all read-only by construction:

- ``GET /operator/runs?repo=owner/a&repo=owner/b`` — the runs inside the
  authorized scope, each with its derived state and the blocked/waiting
  line (the list an operator scans first);
- ``GET /operator/runs/{run_id}?repo=owner/a`` — the full projection
  render: state, exact identities, unresolved external effects, the
  blocked/waiting line, thin evidence links, per-section source coverage,
  projection age, admission/lease occupancy, and the ADVISORY action
  hints;
- ``GET /operator/runs/{run_id}/support-bundle?repo=owner/a`` — the
  exportable :class:`~forge.adaptive.support_bundle.SupportBundle`
  document (coverage + digest + every attempt, failed ones included),
  redaction on.

**Authentication is the lane-control credential family, reused** (the
same scheme the other adaptive surfaces mount): ``HMAC-SHA256`` under the
server-side ``FORGE_LANE_CONTROL_SECRET``, presented as a bearer token.
Where the lane token signs ONE work id, the operator token signs the
SUBJECT SCOPE — the canonical material is ``operator:`` + the declared
repo full names, comma-joined in sorted order
(:func:`operator_scope_token`). The caller DECLARES the scope it is
asking about (exactly as the lane declares its ``work_id``) and the
server verifies the token signs exactly that scope: a token minted for
``owner/a`` cannot sign ``owner/b``, ``owner/a,owner/b`` or anything
wider, so a restricted operator sees only permitted repositories across
list, detail and support-bundle — cross-scope is a 403, a run outside
the verified scope is a 404 (indistinguishable from unknown). The
read-only actor carries NO control privileges: the action hints render
with the ``observer`` role (probe only) and remain hints — execution
goes through the EXISTING guarded command routes, which revalidate
authority and the expected world (a stale action is refused there with
the current state and a safe alternative).

Fail-closed like every adaptive surface: mounted unconditionally, but no
secret or no session factory → **503**, never an unauthenticated open;
no bearer → 401; empty declared scope → 400 (there is no wildcard —
least authority); token/scope mismatch → 403. Rendering performs zero
provider writes, zero model calls and zero state transitions (pinned by
a test with a recording checkpoint repository and row-count checks).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from fastapi import APIRouter, Header, HTTPException, Query, Request

from forge.adaptive.operator_snapshot import OperatorSnapshotReader
from forge.adaptive.operator_view import RecoveryActions, render
from forge.adaptive.support_bundle import SupportBundle
from forge.api_lane_control import _bearer, _secret, lane_control_token, verify_lane_token

__all__ = [
    "OPERATOR_SCOPE_PREFIX",
    "operator_router",
    "operator_scope_material",
    "operator_scope_token",
]

#: The operator token's material prefix — the scope family inside the
#: lane-control HMAC namespace (a lane work id can never collide with it:
#: work ids are run/workpackage ids, never ``operator:`` strings).
OPERATOR_SCOPE_PREFIX: Final = "operator:"

#: Where the composition (or a test) mounts the checkpoint authority the
#: reader consults. Absent → the checkpoints section reads ``unknown``
#: (never an invented empty history).
CHECKPOINT_REPOSITORY_STATE: Final = "operator_checkpoint_repository"

operator_router = APIRouter()


def operator_scope_material(repos: Sequence[str]) -> str:
    """The canonical HMAC material for a subject scope (sorted, deduped)."""
    cleaned = sorted({str(repo).strip() for repo in repos if str(repo).strip()})
    return OPERATOR_SCOPE_PREFIX + ",".join(cleaned)


def operator_scope_token(secret: str, repos: Sequence[str]) -> str:
    """The operator token for *repos* — the lane-control HMAC derivation
    over the scope material (the dispatch-side/minting counterpart)."""
    return lane_control_token(secret, operator_scope_material(repos))


def _declared_scope(values: list[str]) -> tuple[str, ...]:
    """The declared scope as a canonical tuple (repeated params and
    comma-joined values both read; empty after cleaning is a 400)."""
    joined: list[str] = []
    for value in values:
        joined.extend(part for part in str(value or "").split(","))
    cleaned = tuple(sorted({part.strip() for part in joined if part.strip()}))
    return cleaned


async def _authorized_scope(
    request: Request, authorization: str | None, values: list[str]
) -> tuple[str, ...]:
    """The fail-closed ladder: 503 → 401 → 400 → 403 → the verified scope."""
    if not _secret(request):
        raise HTTPException(status_code=503, detail="operator endpoint disabled")
    if getattr(request.app.state, "session_factory", None) is None:
        raise HTTPException(status_code=503, detail="operator endpoint disabled")
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing operator bearer token")
    scope = _declared_scope(values)
    if not scope:
        raise HTTPException(
            status_code=400,
            detail="declare the subject scope with ?repo=<owner/name> (there is no wildcard)",
        )
    if not verify_lane_token(_secret(request), token, operator_scope_material(scope)):
        raise HTTPException(
            status_code=403,
            detail="operator token does not scope these repositories",
        )
    return scope


def _reader(request: Request) -> OperatorSnapshotReader:
    repository = getattr(request.app.state, CHECKPOINT_REPOSITORY_STATE, None)
    return OperatorSnapshotReader(
        request.app.state.session_factory, checkpoint_repository=repository
    )


@operator_router.get("/operator/runs")
async def list_operator_runs(
    request: Request,
    repo: list[str] = Query(..., min_length=1),
    authorization: str | None = Header(None),
) -> Any:
    """The runs inside the authorized scope — state, blocked/waiting, age."""
    scope = await _authorized_scope(request, authorization, repo)
    reader = _reader(request)
    snapshots = await reader.list_snapshots(scope)
    runs = []
    for snapshot in snapshots:
        projection = snapshot.projection()
        runs.append(
            {
                "run_id": snapshot.run_id,
                "subject": snapshot.subject,
                "state": projection.state,
                "underlying_state": projection.underlying_state,
                "blocked_reason": projection.blocked_reason,
                "waiting_on": projection.waiting_on,
                "summary": projection.summary,
                "updated_at": snapshot.rows["run"].get("updated_at", ""),
                "projection_age_seconds": snapshot.projection_age_s,
                "unresolved_effects": len(projection.unresolved_effects),
            }
        )
    return {"scope": list(scope), "runs": runs}


@operator_router.get("/operator/runs/{run_id}")
async def get_operator_run(
    request: Request,
    run_id: str,
    repo: list[str] = Query(..., min_length=1),
    authorization: str | None = Header(None),
) -> Any:
    """One run's full projection render — plus coverage, age, occupancy
    and the ADVISORY action hints (the observer role: probe only)."""
    scope = await _authorized_scope(request, authorization, repo)
    snapshot = await _reader(request).snapshot(run_id, scope)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r} within the authorized scope",
        )
    projection = snapshot.projection()
    document = render(projection)
    document["subject"] = snapshot.subject
    document["source_coverage"] = dict(snapshot.source_coverage)
    document["projection_age_seconds"] = snapshot.projection_age_s
    document["occupancy"] = [dict(row) for row in snapshot.occupancy]
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
        "which revalidate authority and the current world"
    )
    return document


@operator_router.get("/operator/runs/{run_id}/support-bundle")
async def get_operator_support_bundle(
    request: Request,
    run_id: str,
    repo: list[str] = Query(..., min_length=1),
    authorization: str | None = Header(None),
) -> Any:
    """The exportable support bundle — every attempt (failed included),
    explicit coverage, content digest, redaction on."""
    scope = await _authorized_scope(request, authorization, repo)
    snapshot = await _reader(request).snapshot(run_id, scope)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r} within the authorized scope",
        )
    bundle = SupportBundle.build(snapshot.run_id, snapshot.rows)
    return bundle.as_document()
