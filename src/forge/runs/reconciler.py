"""Periodic run reconciler (ADR-0005).

Webhooks accelerate transitions; this loop closes the ones whose
notifications were lost or that simply need polling (CI completion). It is a
plain asyncio task suitable for ``asyncio.gather`` in the worker: one bounded
pass per tick, never holding a worker for the whole run.
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
    """Reconcile ``waiting_ci`` runs every *interval_seconds* until shutdown."""
    shutdown_event = shutdown_event or asyncio.Event()
    logger.info("Run reconciler started (interval=%ss)", interval_seconds)
    while not shutdown_event.is_set():
        try:
            await service.evaluate_waiting_ci()
        except Exception:
            # A failed pass must never kill the reconciler task.
            logger.exception("Run reconciler pass failed")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break  # Event set — clean shutdown.
        except asyncio.TimeoutError:
            pass  # Interval elapsed — next tick.
    logger.info("Run reconciler stopped")
