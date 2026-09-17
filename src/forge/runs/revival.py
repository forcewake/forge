"""Terminal-failure classification and revival (Tier 1 auto / Tier 2 operator).

A run that dies terminal must not cost operator attention unless a human is
genuinely needed. Every ``failed`` terminalization is classified first:

- **transient** (dispatch 5xx / network / timeout, rate limits, runner
  startup, an empty harness-start error) — the run parks ``blocked`` with a
  revival stamp in its evidence (``revive_at``-style ``due_at`` +
  ``revive_count``-style ``count``) and the provider reconciler re-dispatches
  the SAME branch after bounded backoff, at most
  ``FORGE_RUN_AUTO_REVIVE_LIMIT`` times. Journaled as ``auto_revive`` actions
  (ADR-0005). No issue comment, no operator.
- **fatal** (driver exit failed — a real quality signal, config errors such
  as 4xx input mismatches or a missing workflow, exhausted cycles) — the run
  parks ``blocked`` with the precise reason. No auto-retry.

Tier 2 is the operator override for everything Tier 1 correctly refuses to
touch: ``/retry [run-id]`` walks ``failed``/``blocked`` back to ``proposing``
through the explicit revival graph edge
(:meth:`forge.durable.controller.Controller.revive_transition`), grants one
extra commit cycle and re-dispatches on the same branch.

Classification lives here (one table) so the GitLab, GitHub and Azure DevOps
services cannot drift apart — the same reason text classifies the same way on
every lane, and the limits come from the same settings.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable.controller import TERMINAL_STATUSES, Controller, FlowStatus, as_aware_utc
from forge.durable.models import FlowRun

logger = logging.getLogger(__name__)

#: Failure classes. ``fatal`` is the default — Tier 1 must be conservative,
#: a mis-classified fatal costs two bounded retries, a mis-classified
#: transient hides a real quality signal from the operator.
FailureClass = Literal["transient", "fatal"]
TRANSIENT: FailureClass = "transient"
FATAL: FailureClass = "fatal"

#: Default revival budget (``FORGE_RUN_AUTO_REVIVE_LIMIT``) and the base of
#: the bounded backoff ladder (``FORGE_RUN_REVIVE_BACKOFF_SECONDS``).
DEFAULT_REVIVE_LIMIT = 2
DEFAULT_BACKOFF_SECONDS = 60
#: Backoff ceiling: an unrecoverable transient never waits longer than this
#: between attempts.
MAX_BACKOFF_SECONDS = 900

#: Markers that make a terminal reason *transient*. Matched case-insensitively
#: against the whole reason; 4xx status codes are deliberately absent (a 4xx
#: is a config error — exactly the db5408f4 undeclared-input 422).
_TRANSIENT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(500|502|503|504|429)\b",
        r"bad gateway",
        r"service (temporarily )?unavailable",
        r"gateway time-?out",
        r"internal server error",
        r"rate limit",
        r"too many requests",
        r"timed? ?out",
        r"timeout",
        r"\bconnection\b",
        r"\bnetwork\b",
        r"unreachable",
        r"try again",
        r"runner (startup|start|unavailable|failed|failure)",
        r"no (matching )?runner",
    )
)

#: Leg prefixes whose *empty* error detail still counts as transient: a
#: dispatch that died with an empty message (``httpx`` raises bare
#: ``ConnectError``/``ReadError`` with ``str() == ""``) is infrastructure
#: silence, not a quality signal.
_DISPATCH_PREFIXES = ("harness_start_failed",)


def classify_terminal_failure(reason: str) -> FailureClass:
    """Classify a terminal ``failed`` reason as ``transient`` or ``fatal``."""
    text = (reason or "").strip()
    lowered = text.lower()
    if lowered.startswith(_DISPATCH_PREFIXES) and not _detail_of(text):
        return TRANSIENT
    for marker in _TRANSIENT_MARKERS:
        if marker.search(lowered):
            return TRANSIENT
    return FATAL


def _detail_of(reason: str) -> str:
    """The part of ``prefix: detail`` after the first colon ("" when absent)."""
    _, _, detail = reason.partition(":")
    return detail.strip()


def revival_limit(settings: object) -> int:
    """The configured auto-revive budget (minimum 0 — revive can be off)."""
    return max(int(getattr(settings, "FORGE_RUN_AUTO_REVIVE_LIMIT", DEFAULT_REVIVE_LIMIT) or 0), 0)


def revival_backoff_seconds(attempt: int, settings: object | None = None) -> int:
    """Bounded exponential backoff for the *attempt*-th revive (0-based).

    60s → 120s → 240s … capped at :data:`MAX_BACKOFF_SECONDS`.
    """
    base = int(
        getattr(settings, "FORGE_RUN_REVIVE_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS)
        or DEFAULT_BACKOFF_SECONDS
    )
    return min(max(base, 1) * (2 ** max(attempt, 0)), MAX_BACKOFF_SECONDS)


def revival_of(run: FlowRun) -> dict:
    """The run's revival stamp from its evidence ({} when none is pending)."""
    stamp = (run.evidence or {}).get("revival")
    return dict(stamp) if isinstance(stamp, dict) else {}


def revival_due(run: FlowRun, now: datetime) -> bool:
    """Whether *run*'s revival stamp exists and its backoff has elapsed."""
    due_at = revival_of(run).get("due_at")
    if not due_at:
        return False
    try:
        due = as_aware_utc(datetime.fromisoformat(str(due_at)))
    except ValueError:
        return False
    return as_aware_utc(now) >= due


# ----------------------------------------------------------------------
# Tier 1: classification at terminalization + the reconciler revive pass
# ----------------------------------------------------------------------


async def terminalize_failure(
    session_factory: async_sessionmaker,
    settings: object,
    run_id: str,
    *,
    reason: str,
    log: logging.Logger = logger,
) -> None:
    """Park a dead ``failed`` leg as ``blocked`` — reviving it if transient.

    Shared by every provider service's ``_to_terminal(FAILED, …)`` so the
    classification and the budget cannot drift between lanes. The run never
    ends in ``failed`` from here: transient deaths carry a revival stamp the
    reconciler acts on, fatal deaths park ``blocked`` with the precise,
    actionable reason.
    """
    limit = revival_limit(settings)
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            return
        if run.status in {status.value for status in TERMINAL_STATUSES}:
            # A raced double-terminalization: the run is already parked.
            log.warning("Run %s already terminal (%s) — not re-parking", run_id[:8], run.status)
            return
        count = int(revival_of(run).get("count") or 0)
        transient = classify_terminal_failure(reason) is TRANSIENT
        scheduled = transient and count < limit and not run.cancel_requested
        controller = Controller(session)
        if scheduled:
            delay = revival_backoff_seconds(count, settings)
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "revival": {
                        "count": count + 1,
                        "due_at": (
                            datetime.now(timezone.utc) + timedelta(seconds=delay)
                        ).isoformat(),
                        "reason": reason[:200],
                    }
                },
            )
            await controller.transition(
                run_id,
                FlowStatus.BLOCKED,
                reason=f"transient failure — auto-revive {count + 1}/{limit} in ~{delay}s: "
                f"{reason}"[:200],
            )
            await session.commit()
            log.warning(
                "Run %s -> blocked (transient; auto-revive %d/%d in ~%ds): %s",
                run_id[:8],
                count + 1,
                limit,
                delay,
                reason,
            )
            return
        await controller.transition(run_id, FlowStatus.BLOCKED, reason=reason[:200])
        await session.commit()
    log.warning("Run %s -> blocked (fatal, no auto-retry): %s", run_id[:8], reason)


async def evaluate_revivals(
    session_factory: async_sessionmaker,
    settings: object,
    *,
    provider: str,
    redispatch: Callable[[str], Awaitable[None]],
    now: datetime | None = None,
    log: logging.Logger = logger,
) -> None:
    """One reconciler pass over every run waiting for its auto-revive.

    Scans ``blocked`` runs of *provider* whose revival stamp is due, walks
    each back to ``proposing`` through the revival graph edge (journaled as
    an ``auto_revive`` action) and hands it to *redispatch* — the same
    ``_advance_harness``/``_advance_proposal`` leg that ``_begin_repair``
    uses, on the same branch. Not-due runs are skipped: the wait is
    worker-free, like ``waiting_ci``.
    """
    now = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        runs = (
            (
                await session.execute(
                    select(FlowRun).where(
                        FlowRun.provider == provider,
                        FlowRun.status == FlowStatus.BLOCKED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        due_ids = [run.id for run in runs if revival_due(run, now)]

    for run_id in due_ids:
        try:
            action_id = await _begin_auto_revive(session_factory, settings, run_id, now)
        except Exception:
            # Another pass/worker may have taken it; never stall the loop.
            log.exception("Auto-revive walk failed for run %s", run_id[:8])
            continue
        try:
            await redispatch(run_id)
        except Exception as exc:
            log.exception("Auto-revive dispatch failed for run %s", run_id[:8])
            await _complete_auto_revive(session_factory, action_id, "failed", {"error": str(exc)})
        else:
            await _complete_auto_revive(session_factory, action_id, "succeeded", {})


async def _begin_auto_revive(
    session_factory: async_sessionmaker, settings: object, run_id: str, now: datetime
) -> int:
    """Journal the revive intent, mark the stamp dispatched and re-open the run.

    Returns the ``auto_revive`` action id for :func:`_complete_auto_revive`.
    """
    async with session_factory() as session:
        controller = Controller(session)
        run = await session.get(FlowRun, run_id)
        if run is None or run.status != FlowStatus.BLOCKED.value:
            raise LookupError(f"run {run_id} is no longer a parked revival")
        if await has_active_run(
            session,
            provider=run.provider,
            project_id=run.project_id,
            issue_iid=run.issue_iid,
            repo_full_name=run.github_repo_full_name,
            exclude_run_id=run.id,
        ):
            # ADR-0017: one active run per subject — a fresh /implement wins
            # over a stale revival stamp (the run stays parked and due).
            raise LookupError(f"run {run_id} has a live sibling run — revival superseded")
        revival = revival_of(run)
        count = int(revival.get("count") or 0)
        # Consume the due stamp before dispatching: a second pass must never
        # re-dispatch while this leg is in flight.
        run.evidence = _merge_evidence(
            run.evidence,
            {"revival": {"count": count, "dispatched_at": now.isoformat()}},
        )
        action_id = await controller.record_action(run_id, "auto_revive")
        await controller.revive_transition(
            run_id,
            reason=f"auto-revive {count + 1}/{revival_limit(settings)}: "
            f"{revival.get('reason') or 'transient failure'}"[:200],
            authorized_by="auto_revive",
        )
        await session.commit()
        return action_id


async def _complete_auto_revive(
    session_factory: async_sessionmaker, action_id: int, status: str, result: dict
) -> None:
    async with session_factory() as session:
        controller = Controller(session)
        await controller.complete_action(action_id, status, result or None)  # type: ignore[arg-type]
        await session.commit()


# ----------------------------------------------------------------------
# Tier 2: the operator /retry target resolution and guards
# ----------------------------------------------------------------------

#: Statuses a run must be parked in for ``/retry`` to touch it.
_RETRYABLE_STATUSES = frozenset({FlowStatus.FAILED.value, FlowStatus.BLOCKED.value})


async def has_active_run(
    session: AsyncSession,
    *,
    provider: str,
    project_id: int,
    issue_iid: int | None,
    repo_full_name: str | None = None,
    exclude_run_id: str | None = None,
) -> bool:
    """Whether another NON-terminal run is live for the subject.

    The durable one-active-run-per-subject invariant (ADR-0017) is enforced
    by a partial unique index — a revival walk that ignored it would blow up
    on commit; both revival tiers check before walking instead.
    """
    query = select(FlowRun.id).where(
        FlowRun.provider == provider,
        FlowRun.project_id == project_id,
        FlowRun.issue_iid == issue_iid,
        FlowRun.status.not_in([status.value for status in TERMINAL_STATUSES]),
    )
    if repo_full_name:
        query = query.where(FlowRun.github_repo_full_name == repo_full_name)
    if exclude_run_id:
        query = query.where(FlowRun.id != exclude_run_id)
    return (await session.execute(query.limit(1))).scalar() is not None


async def resolve_retry_target(
    session: AsyncSession,
    *,
    provider: str,
    project_id: int,
    issue_iid: int | None,
    requested: str,
    repo_full_name: str | None = None,
) -> FlowRun | None:
    """The run ``/retry [run-id]`` refers to — latest dead run when bare.

    Mirrors ``/cancel``'s resolution: an explicit 32-char id, a unique 8-char
    prefix among the issue's runs, or — bare — the most recent
    ``failed``/``blocked`` run for the issue. ``None`` when nothing matches.
    """
    query = select(FlowRun).where(
        FlowRun.provider == provider,
        FlowRun.project_id == project_id,
        FlowRun.issue_iid == issue_iid,
    )
    if repo_full_name is not None:
        query = query.where(FlowRun.github_repo_full_name == repo_full_name)

    if not requested:
        dead = query.where(FlowRun.status.in_(sorted(_RETRYABLE_STATUSES))).order_by(
            FlowRun.updated_at.desc()
        )
        return (await session.execute(dead)).scalars().first()

    if len(requested) == 32:
        run = await session.get(FlowRun, requested)
        if run is None or run.provider != provider or run.issue_iid != issue_iid:
            return None
        if repo_full_name is not None and run.github_repo_full_name != repo_full_name:
            return None
        return run

    matches = (
        (
            await session.execute(
                query.where(FlowRun.id.like(f"{requested}%")).order_by(FlowRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return matches[0] if len(matches) == 1 else None


def retry_rejection(run: FlowRun | None, *, other_active: bool = False) -> str:
    """Why ``/retry`` refuses *run* — an actionable message, or "" when fine."""
    if run is None:
        return (
            "`/retry` found no retryable run on this issue. "
            "Start a fresh run with `@forge /implement`."
        )
    if other_active:
        return (
            f"Run `{run.id[:8]}` cannot be retried: another run is already in flight on this "
            "subject — forge keeps one active run per subject. Let it finish or `/cancel` it "
            "first."
        )
    status = run.status
    if status not in _RETRYABLE_STATUSES:
        return (
            f"Run `{run.id[:8]}` is `{status}`, not `failed`/`blocked` — there is nothing to "
            "retry. Cancelled runs and fresh work need `@forge /implement`."
        )
    if run.cancel_requested:
        return (
            f"Run `{run.id[:8]}` was cancelled by an operator — retrying a revoked publication "
            "grant is not allowed. Start fresh with `@forge /implement`."
        )
    if not list(run.candidate_shas or []):
        return (
            f"Run `{run.id[:8]}` died before it committed a candidate, so there is no work to "
            "retry in place. Start fresh with `@forge /implement`."
        )
    return ""


def _merge_evidence(evidence: dict | None, patch: dict) -> dict:
    """Shallow-merge *patch* into the run's evidence blob (as the services do)."""
    merged = dict(evidence or {})
    merged.update(patch)
    return merged


def build_retry_context(settings: object, status_reason: str, evidence: dict) -> str:
    """Bounded ``/retry`` brief: why the run stopped + its last verification evidence.

    Shared by every provider so a retried lane gets the same shape of context.
    Redacted like all CI-derived context (F23) and capped like all repair
    briefs (ADR-0013).
    """
    from forge.policy.evidence import EvidencePolicy
    from forge.runs.service import REPAIR_CONTEXT_MAX_CHARS  # lazy: avoids the import cycle

    sections: list[str] = []
    if status_reason:
        sections.append(f"Why the previous attempt stopped: {status_reason}")
    pipeline = evidence.get("pipeline")
    if isinstance(pipeline, dict) and pipeline:
        sections.append(
            "Last pipeline: {status} (sha {sha}) — {url}".format(
                status=pipeline.get("status"),
                sha=str(pipeline.get("sha") or "")[:8],
                url=pipeline.get("url"),
            )
        )
    review = evidence.get("review")
    if isinstance(review, dict) and review:
        sections.append(f"Last review verdict: {review.get('verdict')} — {review.get('summary')}")
        for finding in (review.get("findings") or [])[:5]:
            sections.append(
                "- {severity}: {file}: {note}".format(
                    severity=finding.get("severity"),
                    file=finding.get("file"),
                    note=finding.get("note"),
                )
            )
    redacted, _ = EvidencePolicy.from_settings(settings).apply_policy("\n\n".join(sections))
    return redacted[-REPAIR_CONTEXT_MAX_CHARS:]
