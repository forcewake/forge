"""Terminal-failure revival: bounded auto-retry plus operator ``/retry``.

Two tiers keep a dead run from becoming human work — the operator should
never watch for dead runs; human attention is for the plan gate and the
merge decision:

Tier 1 — a *transient* death (dispatch 5xx/network/timeout, rate limit,
runner startup) schedules its own revival. The run stays parked terminal
with a ``revive`` fragment in its evidence and the reconciler re-dispatches
the SAME branch (attempt base = the last candidate, ADR-0016 §4) once
``revive_at`` is due — a worker-free wait, like ``waiting_ci``. Bounded by
``FORGE_RUN_AUTO_REVIVE_LIMIT`` (default 2) with exponential backoff, and
journaled as ``auto_revive`` actions (ADR-0005).

Tier 2 — everything the classification refuses to touch waits for an
operator ``/retry [run-id]``: the same approver authority as ``/go`` walks
``failed/blocked → proposing`` on the same branch, grants one extra commit
cycle and re-dispatches with the terminal reason as the repair context.
Never a new run id, never a new branch, never re-planning.

A *fatal* death parks ``blocked`` (not ``failed``) with the precise cause
and no auto-retry — that is the genuine "needs a human" signal, and it is
exactly the state ``/retry`` revives in place.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select

from forge.durable import FlowRun, FlowStatus
from forge.durable.controller import Controller, InvalidTransition, as_aware_utc

logger = logging.getLogger(__name__)

FailureClass = Literal["transient", "fatal"]

#: A transient death blames the environment and heals on re-dispatch.
TRANSIENT: FailureClass = "transient"
#: A fatal death needs a human: it is parked blocked with its precise cause.
FATAL: FailureClass = "fatal"

#: Terminal ``status_reason`` prefixes that mean the environment failed, not
#: the change. ``harness_start_failed`` is a dispatch 5xx/network/timeout or
#: a rate limit; ``harness_infrastructure`` is a runner/startup failure; the
#: ADR-0008 ``infrastructure_failure`` is the waiting_ci verdict for one.
TRANSIENT_REASON_PREFIXES: tuple[str, ...] = (
    "harness_start_failed",
    "harness_infrastructure",
    "infrastructure_failure",
)

#: Evidence key holding the revival schedule, and the keys inside it.
REVIVE_EVIDENCE_KEY = "revive"
REVIVE_AT_KEY = "revive_at"
REVIVE_COUNT_KEY = "revive_count"

#: 4xx statuses that are still the environment's fault, not forge's request.
_TRANSIENT_STATUSES = frozenset({408, 429})

#: The HTTP status embedded in a recorded dispatch reason, if any.
_CLIENT_ERROR_RE = re.compile(r"\berror 4\d\d\b")

#: Backoff before the n-th revive: 60s, 120s, … capped so a flapping
#: dependency is never hammered more than once a quarter hour.
REVIVE_BACKOFF_BASE_SECONDS = 60
REVIVE_BACKOFF_MAX_SECONDS = 900

#: ``/retry [run-id]`` — the run id is optional; bare ``/retry`` picks the
#: issue's latest dead run. The id shape matches ``/cancel`` (8-32 hex, the
#: short form posted in every evidence comment).
RETRY_RE = re.compile(r"/retry(?:\s+([0-9a-f]{8,32})\b)?", re.IGNORECASE)

#: Revivable repair-context cap — the dispatch inputs on the Actions and
#: Pipelines lanes clamp to the same 2000 chars.
RETRY_CONTEXT_MAX_CHARS = 2000


def classify_failure_reason(reason: str) -> FailureClass:
    """Classify a terminal ``status_reason`` (the terminalization verdict)."""
    reason = (reason or "").strip().lower()
    if not reason.startswith(TRANSIENT_REASON_PREFIXES):
        return FATAL
    # A recorded dispatch reason carries its HTTP status ("… error 422: …").
    # A 4xx is forge's own request being wrong — fatal, whatever the prefix.
    return FATAL if _CLIENT_ERROR_RE.search(reason) else TRANSIENT


def classify_dispatch_error(exc: BaseException) -> FailureClass:
    """Split a dispatch failure into transient and fatal.

    A 4xx means forge's own request was wrong — an undeclared workflow
    input, a missing workflow, a bad template parameter. Re-dispatching it
    reproduces the same 4xx, so it is fatal (and parks blocked with the
    exact cause). Everything else — a 5xx, a rate limit, a request timeout,
    DNS, a bare ``httpx.HTTPError`` — is the environment and heals on
    re-dispatch.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUSES:
        return TRANSIENT
    if isinstance(status, int) and 400 <= status < 500:
        return FATAL
    return TRANSIENT


def revive_delay_seconds(attempt: int) -> int:
    """Bounded exponential backoff before the *attempt*-th revive."""
    return min(
        REVIVE_BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0),
        REVIVE_BACKOFF_MAX_SECONDS,
    )


def revivals_spent(evidence: dict | None) -> int:
    """How many auto-revives the run has already used."""
    fragment = (evidence or {}).get(REVIVE_EVIDENCE_KEY)
    if not isinstance(fragment, dict):
        return 0
    try:
        return int(fragment.get(REVIVE_COUNT_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def revival_fragment(revive_count: int, reason: str, *, now: datetime, limit: int) -> dict | None:
    """The evidence fragment scheduling auto-revive number ``revive_count + 1``.

    ``None`` once the limit is spent — the run then stays dead for good and
    only an operator ``/retry`` can move it.
    """
    if revive_count >= limit:
        return None
    attempt = revive_count + 1
    due = as_aware_utc(now) + timedelta(seconds=revive_delay_seconds(attempt))
    return {
        REVIVE_AT_KEY: due.isoformat(),
        REVIVE_COUNT_KEY: attempt,
        "reason": (reason or "")[:200],
    }


def revive_due(evidence: dict | None, now: datetime) -> bool:
    """True when the run carries a revival the reconciler should act on now.

    The reconciler polls every terminal run each tick; the backoff stamp is
    what keeps it skipping until due.
    """
    fragment = (evidence or {}).get(REVIVE_EVIDENCE_KEY)
    if not isinstance(fragment, dict) or fragment.get("exhausted"):
        return False
    try:
        due = datetime.fromisoformat(str(fragment.get(REVIVE_AT_KEY) or ""))
    except ValueError:
        return False
    return as_aware_utc(now) >= as_aware_utc(due)


def retry_repair_context(status: str, reason: str, evidence: dict | None) -> str:
    """Bounded repair context for an operator retry: why it died + the last
    verification verdict on the candidate."""
    sections = [f"The run was parked {status} with: {reason or 'unrecorded reason'}"]
    verification = (evidence or {}).get("verification") or (evidence or {}).get("pipeline")
    if isinstance(verification, dict) and verification:
        summary = {
            key: verification[key]
            for key in sorted(verification)
            if isinstance(verification.get(key), (str, int, float, bool))
        }
        sections.append(f"Last verification evidence: {json.dumps(summary, sort_keys=True)}")
    return "\n".join(sections)[:RETRY_CONTEXT_MAX_CHARS]


async def repos_with_due_revival(session_factory, provider: str, now: datetime) -> list[str]:
    """Distinct repo identities holding a due revival for *provider*.

    The GitHub/Azure reconcilers group their passes by repository; this is
    the revival slot's subject list (``github_repo_full_name`` carries the
    Azure ``project/repo`` identity too).
    """
    async with session_factory() as session:
        runs = (
            (await session.execute(select(FlowRun).where(FlowRun.provider == provider)))
            .scalars()
            .all()
        )
    repos = {
        str(run.github_repo_full_name or "").strip()
        for run in runs
        if str(run.github_repo_full_name or "").strip()
        and run.status in (FlowStatus.FAILED.value, FlowStatus.BLOCKED.value)
        and revive_due(run.evidence, now)
    }
    return sorted(repos)


#: Statuses a revival may start from; ``cancelled`` is never revivable.
REVIVABLE_STATUSES = frozenset({FlowStatus.FAILED.value, FlowStatus.BLOCKED.value})


class RevivalMixin:
    """Revival behaviour shared by the three lane services.

    Host requirements the services already provide: ``_session_factory``,
    ``_settings``, ``_get_run``, ``_approvers``, an issue-note poster named
    ``_post_operator_note`` and the lane's own re-dispatch leg
    :meth:`_revival_redispatch`. The GitLab/GitHub/Azure split is otherwise
    identical — same classification, same limits, same evidence shape.
    """

    def _revival_limit(self) -> int:
        """``FORGE_RUN_AUTO_REVIVE_LIMIT`` — auto-revives per run (default 2)."""
        return int(getattr(self._settings, "FORGE_RUN_AUTO_REVIVE_LIMIT", 2) or 0)

    def _owns_revival(self, run: FlowRun) -> bool:
        """Whether *run* belongs to this service's lane (provider + subject)."""
        raise NotImplementedError

    async def _revival_redispatch(
        self, run: FlowRun, *, repair_context: str, repair_reason: str
    ) -> None:
        """Re-dispatch the lane on the SAME branch (attempt base = last candidate)."""
        raise NotImplementedError

    async def _post_operator_note(
        self, project_id: int, issue_iid: int | None, body: str, run_id: str | None, kind: str
    ) -> None:
        """Post an issue note, journaled against *run_id* (None when no run
        matched — the rejection still has to reach the operator)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Tier 1: bounded auto-revive
    # ------------------------------------------------------------------

    async def _schedule_revival(self, run_id: str, reason: str) -> None:
        """Stamp the evidence with the next auto-revive, unless the limit is spent.

        The run itself stays parked terminal: the reconciler acts only when
        ``revive_at`` is due, so a flapping dependency is retried on a
        backoff, not hammered every tick.
        """
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            fragment = revival_fragment(
                revivals_spent(run.evidence), reason, now=now, limit=self._revival_limit()
            )
            evidence = dict(run.evidence or {})
            if fragment is None:
                # The limit is spent: defuse any stale schedule so the
                # reconciler never fires again — only an operator /retry
                # moves the run from here.
                evidence[REVIVE_EVIDENCE_KEY] = {
                    REVIVE_AT_KEY: "",
                    REVIVE_COUNT_KEY: revivals_spent(run.evidence),
                    "exhausted": True,
                    "reason": reason[:200],
                }
                run.evidence = evidence
                await session.commit()
                logger.warning(
                    "Run %s stays terminal — auto-revive limit %d spent: %s",
                    run_id[:8],
                    self._revival_limit(),
                    reason,
                )
                return
            evidence[REVIVE_EVIDENCE_KEY] = fragment
            run.evidence = evidence
            await session.commit()
        logger.warning(
            "Run %s scheduled for auto-revive #%d/%d (backoff %ss): %s",
            run_id[:8],
            fragment[REVIVE_COUNT_KEY],
            self._revival_limit(),
            revive_delay_seconds(fragment[REVIVE_COUNT_KEY]),
            reason,
        )

    async def evaluate_revival(self, now: datetime | None = None) -> None:
        """One reconciler pass over terminal runs due for auto-revival (Tier 1)."""
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(FlowRun).where(
                            FlowRun.status.in_(sorted(REVIVABLE_STATUSES)),
                        )
                    )
                )
                .scalars()
                .all()
            )
        due = [
            run
            for run in runs
            if self._owns_revival(run)
            and not run.cancel_requested
            and revive_due(run.evidence, now)
        ]
        for run in due:
            try:
                await self._evaluate_revival_one(run, now)
            except Exception:
                # One broken run must not stall the reconciler loop.
                logger.exception("Revival pass failed for run %s", run.id[:8])

    async def _evaluate_revival_one(self, run: FlowRun, now: datetime) -> None:
        """Re-dispatch one due run on its own branch (never a new run id)."""
        run_id = run.id
        fragment = dict((run.evidence or {}).get(REVIVE_EVIDENCE_KEY) or {})
        attempt = int(fragment.get(REVIVE_COUNT_KEY) or 0)
        reason = str(fragment.get("reason") or "transient failure")

        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(run_id, "auto_revive")
            await session.commit()
        try:
            async with self._session_factory() as session:
                controller = Controller(session)
                await controller.revive(
                    run_id,
                    reason=f"auto-revive #{attempt}: {reason}"[:200],
                    authorized_by="auto_revive",
                )
                await session.commit()
        except InvalidTransition:
            # Something else moved the run on — one revive per intent.
            await self._complete_action_safe(action_id, {"error": "run no longer revivable"})
            return
        await self._complete_action_safe(action_id, {"attempt": attempt, "reason": reason[:200]})
        logger.warning(
            "Run %s auto-revived (attempt %d/%d) after: %s",
            run_id[:8],
            attempt,
            self._revival_limit(),
            reason,
        )
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
        await self._revival_redispatch(
            run,
            repair_context=f"The previous dispatch attempt died before finishing: {reason}",
            repair_reason=f"transient failure: {reason}",
        )

    # ------------------------------------------------------------------
    # Tier 2: operator /retry
    # ------------------------------------------------------------------

    async def handle_retry_note(
        self,
        *,
        project_id: int,
        issue_iid: int | None,
        note_text: str,
        author_username: str,
        now: datetime | None = None,
    ) -> None:
        """``/retry [run-id]``: re-dispatch a dead run in place, on its branch.

        Same approver authority as ``/go`` (ADR-0009). Guards — terminal
        ``failed``/``blocked``, not cancelled, and at least one recorded
        candidate sha — reject with an actionable note: without a candidate
        there is nothing to continue, so ``/implement`` is the only path.
        """
        match = RETRY_RE.search(note_text or "")
        if match is None:
            return
        now = now or datetime.now(timezone.utc)
        if author_username not in self._approvers():
            logger.info("/retry from @%s who is not an approver — ignoring", author_username)
            return

        async with self._session_factory() as session:
            run = await self._pick_retry_run(session, project_id, issue_iid, match.group(1))
            if run is None:
                await self._post_operator_note(
                    project_id,
                    issue_iid,
                    "No revivable run found for `/retry` — a dead run needs a recorded"
                    " candidate to continue. Start fresh with `/implement`.",
                    None,
                    "retry_rejected",
                )
                return
            if run.status not in REVIVABLE_STATUSES or run.cancel_requested:
                await self._post_operator_note(
                    project_id,
                    issue_iid,
                    f"Run `{run.id[:8]}` is `{run.status}` — `/retry` only revives a"
                    " **failed** or **blocked** run. Use `/implement` instead.",
                    run.id,
                    "retry_rejected",
                )
                return
            if not list(run.candidate_shas or []):
                await self._post_operator_note(
                    project_id,
                    issue_iid,
                    f"Run `{run.id[:8]}` never recorded a candidate, so there is no work"
                    " to continue — re-derive it with `/implement`.",
                    run.id,
                    "retry_rejected",
                )
                return
            # One operator-granted commit cycle per /retry: it may legitimately
            # exceed FORGE_MAX_COMMIT_CYCLES, because a human just asked for it.
            next_cycle = (run.commit_cycle or 1) + 1
            reason = run.status_reason or "terminal failure"
            repair_context = retry_repair_context(run.status, reason, run.evidence)
            run_id = run.id

        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(run_id, "retry_requested")
            await session.commit()
        try:
            async with self._session_factory() as session:
                controller = Controller(session)
                retried = await self._get_run(session, run_id)
                retried.commit_cycle = next_cycle
                await controller.revive(
                    run_id,
                    reason=f"/retry by @{author_username}: {reason}"[:200],
                    authorized_by=f"operator:@{author_username}",
                )
                await session.commit()
        except InvalidTransition as exc:
            await self._complete_action_safe(action_id, {"error": str(exc)})
            await self._post_operator_note(
                project_id,
                issue_iid,
                f"Run `{run_id[:8]}` could not be retried: {exc}",
                run_id,
                "retry_rejected",
            )
            return
        await self._complete_action_safe(
            action_id,
            {"cycle": next_cycle, "reason": reason[:200], "by": author_username},
        )
        logger.info("Run %s retried by @%s — cycle %d", run_id[:8], author_username, next_cycle)
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
        # The taken-in-work ack is the advance leg's own journaled note.
        await self._revival_redispatch(
            run,
            repair_context=repair_context,
            repair_reason=f"operator retry: {reason}",
        )

    async def _pick_retry_run(
        self,
        session,
        project_id: int,
        issue_iid: int | None,
        run_ref: str | None,
    ) -> FlowRun | None:
        """The run *run_ref* names, or the issue's latest dead run for bare ``/retry``.

        An explicit reference resolves against ALL of the issue's runs so a
        non-terminal one gets a precise rejection; the bare form only ever
        picks a revivable one.
        """
        stmt = select(FlowRun).where(FlowRun.project_id == project_id)
        if issue_iid is not None:
            stmt = stmt.where(FlowRun.issue_iid == issue_iid)
        if run_ref:
            ref = run_ref.lower()
            named = (await session.execute(stmt.where(FlowRun.id.like(f"{ref}%")))).scalars().all()
            if len(named) == 1:
                return named[0]
            logger.info(
                "/retry %s matched %d runs — refusing",
                ref,
                len(named),
            )
            return None
        return (
            (
                await session.execute(
                    stmt.where(FlowRun.status.in_(sorted(REVIVABLE_STATUSES)))
                    .order_by(FlowRun.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    async def _complete_action_safe(self, action_id: int, remote_result: dict) -> None:
        try:
            async with self._session_factory() as session:
                controller = Controller(session)
                await controller.complete_action(action_id, "succeeded", remote_result)
                await session.commit()
        except Exception:  # pragma: no cover — the journal never blocks a revive
            logger.warning("Revival action journal %d not completed", action_id, exc_info=True)
