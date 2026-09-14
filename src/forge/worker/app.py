from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from uuid import uuid4

from forge.agents.registry import AgentRegistry
from forge.config import Settings, get_forge_config
from forge.database import dispose_engine, get_session_factory, init_db
from forge.flows.loader import FlowLoader
from forge.flows.runner import FlowRunner
from forge.flows.state import FlowStateManager
from forge.gitlab.client import GitLabClient
from forge.mcp_client.manager import MCPConnectionManager
from forge.mcp_client.registry import MCPRegistry
from forge.orchestrator.orchestrator import Orchestrator
from forge.runs import RunService, execute_run_command, run_reconciler
from forge.runs.service import forge_token
from forge.utils.logging import setup_logging
from forge.utils.redis_client import RedisManager
from forge.worker.queue import TaskQueue
from forge.worker.retry import RetryPolicy
from forge.worker.steps import (
    claim_command_step,
    command_step_known,
    execute_claimed_step,
    run_step_reaper,
    run_step_worker,
)
from forge.worker.tasks import deserialize_event

logger = logging.getLogger(__name__)


async def _execute_run_command_task(
    settings,
    forge_config,
    session_factory,
    metadata: dict,
    *,
    owner: str,
) -> None:
    """Execute a queued ``run_command`` task through the step runtime (ADR-0017).

    The persisted StepRun is the authority; this Redis task is only the
    wake-up. The step is claimed first (lease + fence), so a step worker can
    never double-run the same command. When no step was persisted (legacy
    producer) or the DB is unreachable, execution falls back to the direct
    :func:`execute_run_command` path.
    """
    source_event_id = str(metadata.get("source_event_id") or "")
    if source_event_id and session_factory is not None:
        try:
            known = await command_step_known(session_factory, source_event_id)
        except Exception:
            logger.warning("Step lookup failed — executing run command directly", exc_info=True)
            known = None
        if known:
            claimed = await claim_command_step(session_factory, owner, source_event_id)
            if claimed is None:
                # Not claimable (running / in backoff / done): the step
                # runtime owns the command — skip, do not double-execute.
                logger.info(
                    "Run command step %s not claimable — left to the step runtime",
                    source_event_id[:12],
                )
                return
            await execute_claimed_step(session_factory, settings, forge_config, claimed)
            return
        if known is False:
            # The identity is known to this system but the step is not
            # visible yet — the ingress transaction may still be committing
            # or the step belongs to another deployment. The step runtime
            # owns this command; executing directly here has already caused
            # double execution (review-class bug, found live).
            logger.warning(
                "No persisted step for %s yet — skipping direct execution", source_event_id[:12]
            )
            return
    await execute_run_command(settings, forge_config, session_factory, metadata)


async def bootstrap():
    """Initialize all dependencies needed by the worker.

    Returns (settings, forge_config, session_factory, registry, task_queue, redis_manager).
    """
    settings = Settings()  # type: ignore[call-arg]
    setup_logging(settings.LOG_LEVEL)

    forge_config = get_forge_config()

    # Database
    await init_db(settings.DATABASE_URL)
    session_factory = get_session_factory(settings.DATABASE_URL)

    # Agent registry
    registry = AgentRegistry(settings.FORGE_AGENTS_DIR)
    registry.load()

    # Redis (required for worker)
    redis_manager = await RedisManager.from_settings(settings)
    if redis_manager is None:
        logger.error("REDIS_URL is required for the worker process")
        sys.exit(1)

    task_queue = TaskQueue(redis_manager, RetryPolicy())

    # Flow system
    flow_loader = FlowLoader(settings.FORGE_AGENTS_DIR.rstrip("/") + "/../agents/flows")
    flow_loader.load()
    flow_state_mgr = FlowStateManager(redis_manager)
    flow_runner = FlowRunner(
        state_mgr=flow_state_mgr,
        loader=flow_loader,
        registry=registry,
        queue=task_queue,
        settings=settings,
        forge_config=forge_config,
        session_factory=session_factory,
    )

    # MCP client
    mcp_registry = MCPRegistry(forge_config.mcp_servers)
    mcp_manager = MCPConnectionManager(mcp_registry)

    return (
        settings,
        forge_config,
        session_factory,
        registry,
        task_queue,
        redis_manager,
        flow_runner,
        mcp_manager,
    )


async def run_worker(
    worker_id: str,
    settings,
    forge_config,
    session_factory,
    registry,
    task_queue: TaskQueue,
    shutdown_event: asyncio.Event,
    flow_runner: FlowRunner | None = None,
    mcp_manager: MCPConnectionManager | None = None,
) -> None:
    """Main worker loop: claim tasks, execute, report results."""
    logger.info("Worker %s started", worker_id)

    while not shutdown_event.is_set():
        task = await task_queue.claim(worker_id, timeout=5.0)
        if task is None:
            continue  # Timeout, loop back

        try:
            if task.task_type == "flow_step" and flow_runner is not None:
                await flow_runner.execute_step(
                    flow_id=task.metadata["flow_id"],
                    step_index=task.metadata["step_index"],
                )
            elif task.task_type == "run_command":
                # M1 durable run loop — ADR-0017: executed through the step
                # runtime (claim → lease + fence → run), never concurrently
                # with the step worker.
                await _execute_run_command_task(
                    settings,
                    forge_config,
                    session_factory,
                    task.metadata,
                    owner=worker_id,
                )
            else:
                event = deserialize_event(task)
                orchestrator = Orchestrator(
                    settings=settings,
                    forge_config=forge_config,
                    session_factory=session_factory,
                    registry=registry,
                    mcp_manager=mcp_manager,
                )
                await orchestrator.handle_event(event)
            await task_queue.complete(task)
        except Exception as e:
            logger.error("Task %s failed: %s", task.task_id, e, exc_info=True)
            await task_queue.fail(task, str(e))

    logger.info("Worker %s shutting down", worker_id)


async def run_reaper(
    task_queue: TaskQueue,
    shutdown_event: asyncio.Event,
    interval: float = 60.0,
    max_age: float = 300.0,
) -> None:
    """Periodically requeue stale tasks from crashed workers."""
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            break  # Event was set
        except asyncio.TimeoutError:
            pass  # Interval elapsed, do work

        try:
            count = await task_queue.requeue_stale(max_age_seconds=max_age)
            if count:
                logger.info("Reaper requeued %d stale task(s)", count)
        except Exception:
            logger.error("Reaper error", exc_info=True)


async def run_heartbeat(
    worker_id: str,
    redis_manager: RedisManager,
    shutdown_event: asyncio.Event,
    interval: float = 10.0,
    ttl: int = 30,
) -> None:
    """Periodically set a heartbeat key so /metrics can count active workers."""
    key = f"forge:worker:{worker_id}"
    while not shutdown_event.is_set():
        try:
            await redis_manager.set_ex(key, "1", ex=ttl)
        except Exception:
            logger.warning("Heartbeat failed", exc_info=True)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    """Entry point for the worker process."""
    (
        settings,
        forge_config,
        session_factory,
        registry,
        task_queue,
        redis_manager,
        flow_runner,
        mcp_manager,
    ) = await bootstrap()

    worker_id = f"worker-{os.getpid()}-{uuid4().hex[:6]}"
    shutdown_event = asyncio.Event()

    # Run reconciler (M1): polls waiting_ci runs for pipeline completion.
    gitlab = GitLabClient(
        base_url=settings.GITLAB_URL,
        token=forge_token(settings),
    )
    run_service = RunService(
        session_factory=session_factory,
        gitlab=gitlab,
        settings=settings,
        config=forge_config,
    )

    # Signal handling
    loop = asyncio.get_running_loop()

    def _signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            signal.signal(sig, lambda *_: _signal_handler())

    # Run worker, step runtime, reaper, heartbeat, and the run reconciler
    # concurrently (ADR-0017: the step runtime owns run commands; the Redis
    # queue loop keeps serving the legacy event/flow_step path).
    try:
        await asyncio.gather(
            run_worker(
                worker_id,
                settings,
                forge_config,
                session_factory,
                registry,
                task_queue,
                shutdown_event,
                flow_runner=flow_runner,
                mcp_manager=mcp_manager,
            ),
            run_step_worker(
                session_factory,
                settings,
                forge_config,
                worker_id,
                shutdown_event,
                redis_manager,
            ),
            run_reconciler(run_service, interval_seconds=15, shutdown_event=shutdown_event),
            run_reaper(task_queue, shutdown_event),
            run_step_reaper(session_factory, shutdown_event),
            run_heartbeat(worker_id, redis_manager, shutdown_event),
        )
    finally:
        await gitlab.close()
        await mcp_manager.close_all()
        await redis_manager.close()
        await dispose_engine()  # F26: await engine disposal on shutdown
        logger.info("Worker %s exited cleanly", worker_id)


if __name__ == "__main__":
    asyncio.run(main())
