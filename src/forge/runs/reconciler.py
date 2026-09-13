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
    """Reconcile ``waiting_ci`` and ``waiting_harness`` runs until shutdown."""
    shutdown_event = shutdown_event or asyncio.Event()
    logger.info("Run reconciler started (interval=%ss)", interval_seconds)
    while not shutdown_event.is_set():
        for evaluate in (service.evaluate_waiting_ci, service.evaluate_waiting_harness):
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
