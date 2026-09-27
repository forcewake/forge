"""R41-02 (#357) — the REAL ASGI gateway subprocess for the kill matrix.

``tests/production_entry/test_durable_ingress.py`` launches THIS script as a
separate OS process (uvicorn serving ``forge.main.create_app`` over the same
durable database and fake native GitLab the trace owns) and SIGKILLs it at
the acknowledgement/commit boundaries. The boundaries cannot be reached from
the test process — they live inside the child's request handling — so the
child applies its OWN instrumentation at startup, before serving:

- ``--kill before-commit``  — SIGKILL after ``ingest_event`` ran but before
  the ingress transaction commits (the inbox row and the scheduled step are
  rolled back with the dead connection);
- ``--kill after-commit``   — SIGKILL at the first strictly-post-commit
  call (``TaskQueue.mark_delivered`` / ``BackgroundTasks.add_task``): the
  durable rows are committed, the HTTP response never left;
- ``--kill after-response`` — SIGKILL when the after-response work starts
  (``run_pending_command_step`` — the no-Redis execution nudge — or, under
  the background-mailbox mutation, ``route_adaptive_command_note`` itself):
  the client already holds the acknowledgement;
- ``--db-failures N``       — the persistence-outage injector: the first N
  ``ingest_event`` calls raise inside the open transaction;
- ``--mutation pre-commit-cache`` / ``--mutation background-mailbox`` — the
  regression patches: the pre-#357 ``_ingest_run_command`` (SET-NX marker
  consulted BEFORE the transaction, answering ``deduplicated`` with no DB
  check) and the pre-#357 ``_ingest_adaptive_control`` (immediate 202, the
  mailbox written only by an after-response BackgroundTask), applied to the
  SHIPPED module attributes exactly the way a regression would;
- ``--stub-model`` (R41-07 / #362) — patch the DEFAULT agent builder to the
  deterministic stubs: the gateway's no-Redis execution nudge runs run
  commands INSIDE this process, and the matrix's Redis-absent arms drive
  full lifecycle legs (plan/publish/review) through it — the sanctioned
  model double, never a policy replacement.

The child serves on an ephemeral port and writes ``{"port": ...}`` to the
ready file once listening (the fake-native server's convention).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path


def _hard_kill() -> None:  # pragma: no cover — the child dies here
    """SIGKILL this process — the boundary the matrix kills at."""
    os.kill(os.getpid(), signal.SIGKILL)


def _install_kill_point(name: str) -> None:
    """Patch the shipped seams so the named boundary hard-kills the child."""
    import forge.durable as durable_package
    import forge.worker.steps as steps_module

    real_ingest_event = durable_package.ingest_event

    if name == "before-commit":

        async def kill_before_commit(session, **kwargs):
            result = await real_ingest_event(session, **kwargs)
            _hard_kill()  # pragma: no cover — never returns
            return result  # pragma: no cover — satisfies the type checker

        durable_package.ingest_event = kill_before_commit
        return

    if name == "after-commit":
        import fastapi

        import forge.worker.queue as worker_queue

        async def kill_at_mark(self, fingerprint, ttl=300):  # noqa: ANN001
            _hard_kill()  # pragma: no cover — never returns

        worker_queue.TaskQueue.mark_delivered = kill_at_mark

        def kill_at_add_task(self, func, *args, **kwargs):  # noqa: ANN001
            _hard_kill()  # pragma: no cover — never returns

        fastapi.BackgroundTasks.add_task = kill_at_add_task
        return

    if name == "after-response":
        # The no-Redis nudge runs strictly after the response was sent; the
        # adaptive background-task mutation (below) calls the router the
        # same way — patch BOTH so the boundary is deterministic on either
        # code shape.
        def kill_when_nudged(*args, **kwargs):
            _hard_kill()  # pragma: no cover — never returns

        steps_module.run_pending_command_step = kill_when_nudged

        import forge.adaptive.command_router as adaptive_module

        async def kill_when_routed(settings, session_factory, note):  # noqa: ANN001
            _hard_kill()  # pragma: no cover — never returns

        adaptive_module.route_adaptive_command_note = kill_when_routed
        return

    raise SystemExit(f"unknown kill point: {name!r}")


def _install_db_failures(count: int) -> None:
    """The persistence outage: the first *count* ingest transactions raise."""
    import forge.durable as durable_package

    real_ingest_event = durable_package.ingest_event
    state = {"failed": 0}

    async def flaky_ingest_event(session, **kwargs):
        if state["failed"] < count:
            state["failed"] += 1
            raise RuntimeError(f"injected persistence outage #{state['failed']}")
        return await real_ingest_event(session, **kwargs)

    durable_package.ingest_event = flaky_ingest_event


def _install_pre_commit_cache_mutation() -> None:
    """Mutation (a): the pre-#357 ``_ingest_run_command``, verbatim in shape.

    The SET-NX dedup marker is consulted BEFORE the inbox transaction and a
    hit answers ``{"deduplicated": True}`` with NO database check — the
    exact regression the P02 schedule (SET-NX ok → SQL fails → retry within
    the TTL) turns into a successful empty duplicate.
    """
    import forge.gateway.router as gateway
    from forge.durable import ingest_event
    from forge.gitlab.events import NoteEvent, PipelineEvent
    from forge.worker.steps import (
        STEP_WAKE_KEY,
        command_source_event_id,
        run_pending_command_step,
        schedule_command_step,
    )
    from forge.worker.tasks import create_run_command_task

    async def _old_ingest_run_command(request, background_tasks, event, run_command):
        settings = request.app.state.settings
        session_factory = getattr(request.app.state, "session_factory", None)
        queue = getattr(request.app.state, "task_queue", None)
        if isinstance(event, (NoteEvent, PipelineEvent)):
            note_id: int | str = event.object_attributes.id
        else:  # pragma: no cover
            note_id = 0
        note_id = str(run_command.get("delivery_key") or note_id)
        source_event_id = command_source_event_id(
            run_command["command"], run_command["project_id"], note_id
        )

        if queue is not None:
            delivery_uuid = str(run_command.get("delivery_uuid") or "")
            is_feedback = run_command.get("command") == "review_feedback"
            if delivery_uuid and await queue.is_duplicate(f"delivery:{delivery_uuid}"):
                if is_feedback:
                    gateway.logger.info(
                        "feedback.duplicate_delivery layer=transport uuid=%s", delivery_uuid
                    )
                return {"status": "accepted", "event": event.object_kind, "deduplicated": True}
            if await queue.is_duplicate(f"run:{run_command['project_id']}:{note_id}"):
                if is_feedback:
                    gateway.logger.info(
                        "feedback.duplicate_delivery layer=logical note=%s", note_id
                    )
                return {"status": "accepted", "event": event.object_kind, "deduplicated": True}

        if session_factory is not None:
            deduplicated = False
            async with session_factory() as session:
                async with session.begin():
                    _, created = await ingest_event(
                        session,
                        source_event_id=source_event_id,
                        project_id=run_command["project_id"],
                        event_type="run_command",
                        payload={**run_command, "note_id": note_id},
                    )
                    if created:
                        await schedule_command_step(
                            session, run_command, source_event_id=source_event_id
                        )
                    else:
                        deduplicated = True
            if deduplicated:
                return {"status": "accepted", "event": event.object_kind, "deduplicated": True}

        if queue is not None:
            try:
                await queue.submit(create_run_command_task(run_command, note_id=note_id))
            except Exception:
                gateway.logger.warning("Run command queue wake-up failed", exc_info=True)
            redis_manager = getattr(request.app.state, "redis_manager", None)
            if redis_manager is not None:
                try:
                    await redis_manager.lpush(STEP_WAKE_KEY, source_event_id)
                except Exception:
                    gateway.logger.debug("Run command step wake-up failed", exc_info=True)
            return {
                "status": "accepted",
                "event": event.object_kind,
                "queued": True,
                "run_command": True,
            }

        if session_factory is not None:
            from uuid import uuid4

            background_tasks.add_task(
                run_pending_command_step,
                session_factory,
                settings,
                request.app.state.forge_config,
                source_event_id,
                owner=f"gateway-{uuid4().hex[:6]}",
            )
            return {"status": "accepted", "event": event.object_kind, "run_command": True}

        return {"status": "accepted", "event": event.object_kind}

    gateway._ingest_run_command = _old_ingest_run_command


def _install_stub_model() -> None:
    """R41-07 (#362): the deterministic agents behind the REAL seams.

    The gateway's no-Redis nudge executes run-command steps in THIS process
    through ``execute_run_command``, whose RunService takes the DEFAULT
    agents. The default builder is the one seam where the model enters this
    path; the double replaces it there and nowhere else.
    """
    import forge.runs.service as service_module
    from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

    def _stub_agents(settings, gitlab, session_factory, budget=None):  # noqa: ANN001
        return StubPlanner(), StubImplementer(), StubReviewer()

    assert service_module and StubImplementer and StubPlanner and StubReviewer
    service_module.build_default_agents = _stub_agents


def _install_background_mailbox_mutation() -> None:
    """Mutation (b): the pre-#357 ``_ingest_adaptive_control``, verbatim.

    The 202 answers immediately and the mailbox write is ONLY the
    after-response Starlette BackgroundTask — no inbox row, no scheduled
    step — so a process death in that window loses an acknowledged
    /pause. The signature matches the CURRENT call site (a regression
    keeps the dispatcher it has).
    """
    import forge.adaptive.command_router as adaptive_module
    import forge.gateway.router as gateway

    async def _old_ingest_adaptive_control(request, background_tasks, run_command, event):
        session_factory = getattr(request.app.state, "session_factory", None)
        if session_factory is None:
            gateway.logger.warning("Adaptive command without a database — not routed")
            return {"status": "accepted", "event": event.object_kind, "adaptive_command": False}
        background_tasks.add_task(
            adaptive_module.route_adaptive_command_note,
            request.app.state.settings,
            session_factory,
            run_command,
        )
        return {"status": "accepted", "event": event.object_kind, "adaptive_command": True}

    gateway._ingest_adaptive_control = _old_ingest_adaptive_control


async def _serve(app, ready_file: Path) -> None:
    from uvicorn import Config, Server

    server = Server(Config(app, host="127.0.0.1", port=0, log_level="error"))

    async def _announce() -> None:
        while not server.started:
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        ready_file.write_text(json.dumps({"port": int(port)}))

    announcer = asyncio.create_task(_announce())
    try:
        await server.serve()
    finally:
        announcer.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--db-url", required=True, help="the shared durable database URL")
    parser.add_argument("--kill", choices=("before-commit", "after-commit", "after-response"))
    parser.add_argument("--db-failures", type=int, default=0)
    parser.add_argument("--mutation", choices=("pre-commit-cache", "background-mailbox"))
    parser.add_argument(
        "--stub-model",
        action="store_true",
        help="patch the default agent builder to the deterministic stubs "
        "(R41-07: the no-Redis nudge executes lifecycle legs in-process)",
    )
    args = parser.parse_args()

    if args.stub_model:
        _install_stub_model()
    if args.kill:
        _install_kill_point(args.kill)
    if args.db_failures:
        _install_db_failures(args.db_failures)
    if args.mutation == "pre-commit-cache":
        _install_pre_commit_cache_mutation()
    elif args.mutation == "background-mailbox":
        _install_background_mailbox_mutation()

    # The app's lifespan bootstraps its OWN scratch database (a fresh
    # sqlite file it may create/stamp freely); the SESSION FACTORY is
    # redirected to the shared durable database the trace owns — the same
    # swap the in-process traces perform by injecting
    # ``application.state.session_factory`` after create_app. Here it must
    # happen before uvicorn runs the lifespan, and per-process (the
    # subprocess's engines are never the test process's).
    import forge.main as main_module
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    scratch_url = os.environ["DATABASE_URL"] + ".appscratch"
    real_get_session_factory = main_module.get_session_factory

    def _shared_factory(url: str):
        if url == scratch_url:
            engine = create_async_engine(
                args.db_url,
                connect_args={"timeout": 30} if args.db_url.startswith("sqlite") else {},
            )
            return async_sessionmaker(engine, expire_on_commit=False)
        return real_get_session_factory(url)

    async def _noop_init_db(database_url, *, alembic_cfg=None):  # noqa: ANN001
        return None

    os.environ["DATABASE_URL"] = scratch_url
    main_module.get_session_factory = _shared_factory
    main_module.init_db = _noop_init_db

    app = main_module.create_app()
    asyncio.run(_serve(app, Path(args.ready_file)))


if __name__ == "__main__":
    sys.exit(main())
