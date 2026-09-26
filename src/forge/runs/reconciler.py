"""Periodic run reconciler (ADR-0005, ADR-0015).

Webhooks accelerate transitions; this loop closes the ones whose
notifications were lost or that simply need polling (CI completion, harness
jobs running minutes in the target project's CI). It is a plain asyncio task
suitable for ``asyncio.gather`` in the worker: one bounded pass per tick,
never holding a worker for the whole run.
"""

from __future__ import annotations

import asyncio
import logging

from forge.runs.service import RunService

logger = logging.getLogger(__name__)


async def run_reconciler(
    service: RunService,
    interval_seconds: float = 15,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Reconcile waiting_ci, waiting_harness, config-blocked and crashed
    evidence notes until shutdown."""
    shutdown_event = shutdown_event or asyncio.Event()
    logger.info("Run reconciler started (interval=%ss)", interval_seconds)
    while not shutdown_event.is_set():
        for evaluate in (
            service.evaluate_waiting_ci,
            service.evaluate_waiting_harness,
            # Tier-1 auto-revive: re-dispatch runs whose transient-failure
            # backoff has elapsed (forge.runs.revival).
            service.evaluate_revival,
            # A11 revival-attempt recovery: re-drive the dispatch of revival
            # attempts whose worker died between the revive commit and the
            # backend call — exactly once per stranded attempt.
            service.evaluate_revival_recovery,
            # A13 config-gate recovery: retry the `.forge.yml` read of runs
            # parked blocked(config_…) and re-enter planning on recovery.
            service.evaluate_config_recovery,
            # ADR-0017 §5: re-post the evidence note for runs that reached
            # ready_for_human before their worker died mid-announcement.
            service.evaluate_ready_evidence,
            # R24 acceptance: record a READY run's merge outcome (the human's
            # merge is observable provider-side; forge never merges itself).
            service.evaluate_accepted,
            # R11: probe-and-resolve publication intents whose effect may
            # have landed while the worker was stalled (adopt / duplicate /
            # unknown — never a blind re-publish).
            service.evaluate_publication_intents,
            # R40-01 (#337): the bounded review-correction pass — re-drive
            # runs whose staged reviewer correction (/fix on the Draft MR)
            # a human has since APPROVED via /approve-revision. The pass is
            # the reconciler's own shape (a bounded scan over
            # waiting_ci/evaluating_ci runs, one exception-isolated loop per
            # run) and doubles as the RECOVERY path when the approving
            # delivery was lost: discovery of requests stays in the
            # ingress/step path and authorization to start an attempt
            # stays with the human approval — this pass re-drives only what
            # BOTH already decided, never more.
            service.evaluate_review_corrections,
            # R40-02 (#338): the bounded review-ROUND pass — close rounds
            # whose child work unit reached a terminal status (freeing the
            # lineage's ONE outstanding-correction slot) and re-drive the
            # rounds a crash left between admission and the advance leg
            # (head-fenced, at most one child + one effect intent).
            service.evaluate_review_rounds,
        ):
            try:
                await evaluate()
            except Exception:
                # A failed pass must never kill the reconciler task.
                logger.exception("Run reconciler pass failed")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break  # Event set — clean shutdown.
        except asyncio.TimeoutError:
            pass  # Interval elapsed — next tick.
    logger.info("Run reconciler stopped")
