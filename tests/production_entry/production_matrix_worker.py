"""R41-07 (#362) — the REAL worker subprocess for the production matrix.

``tests/production_entry/test_production_matrix.py`` launches THIS script as
a separate OS process and SIGKILLs it at the worker-side fault windows. The
installed composition runs inside the child — ``run_step_worker`` (the exact
step loop ``worker/app.main`` gathers: claim → lease → fence → execute) and
``run_reconciler`` (the exact periodic pass tuple) — over the SAME durable
database and fake native GitLab the trace owns. The boundaries live inside
the child's execution, so the child applies its OWN instrumentation at
startup, before any loop runs:

- ``--kill child-admission``  — SIGKILL inside the round ADMISSION
  transaction, after the child FlowRun flush, at the child-budget open (the
  ``open_budget_from_spec`` call the admission makes): the whole admission —
  round row, child run, child budget, MR reservation, outbox — dies with the
  uncommitted connection. NOTHING partial survives (the #356 atomicity bar
  at a real process boundary);
- ``--kill native-commit --nth N`` — SIGKILL after the Nth
  ``GitLabClient.create_commit`` returned: the native commit LANDED on the
  recording provider, the journal's action stays open, the run state is
  mid-leg (the #358 own-effect recovery window);
- ``--kill journal-completion --nth N`` — SIGKILL after the Nth
  ``ChangesetWriter.apply`` returned: intent, commit and journal all
  completed durably, the run state still sits mid-leg (the mid-leg
  idempotency window — a fresh worker adopts the journaled candidate).

The regression patches (the defects the matrix's mutation arms seed back,
applied to the SHIPPED symbols exactly the way a revert would):

- ``--mutation budget-before-child`` — the pre-#356 ordering: the child
  budget opened BEFORE the child run existed, so ``open_budget``'s
  application guard (``RunNotFound``) fired for EVERY finite spec. Spelled
  at the seam the reorder moved: when the budgeted run is not yet committed
  anywhere (the admission's flushed-but-uncommitted child), the guard raises
  — the committed root's own open (the /go leg) is untouched, exactly the
  old code's blast radius;
- ``--mutation base-guard-first`` — the pre-#358 recovery order: the
  base-head fence decided BEFORE any own-effect classification, so a round
  whose OWN commit moved the head read as a foreign head and staled.
  Spelled as the removed branch order: the classifier answers
  ``not_dispatched`` at the base and ``foreign`` on ANY moved head;
- ``--mutation any-terminal-occupancy`` — the pre-#360 occupancy predicate:
  ANY terminal pipeline in the branch listing released the slot, so a
  historical success beside a current running job read TERMINAL.

The model is ALWAYS the deterministic stub (``build_default_agents`` patched
to the stub agents — the sanctioned double: "stubs may replace the model and
remote effects, never the policy under test"). Settings come from the
launching environment.

The child writes ``{"pid": ...}`` to the ready file once the loops started,
exits 0 when the step queue is quiet (no scheduled/running steps for
``--quiet-polls`` consecutive checks — the reconciler's in-flight pass
always completes before the loops drain). ``--max-seconds`` is a runaway
ceiling that may end the child only while the queue is EMPTY — it never
cancels a step mid-flight (a cancelled publication re-executes after the
lease expires and duplicates the native effect); the launching test's
subprocess timeout is the loud outer backstop. ``--mode reconciler
--ticks N`` runs only the reconciler for N full pass cycles (the recovery
driver). The step reaper (``run_step_reaper``, also part of the installed
composition) reschedules the steps a killed worker left leased, once
their leases expire.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path


def _hard_kill() -> None:  # pragma: no cover — the child dies here
    """SIGKILL this process — the fault window the trace kills at."""
    os.kill(os.getpid(), signal.SIGKILL)


# ----------------------------------------------------------------------
# The model double — the sanctioned stub seam (never the policy)
# ----------------------------------------------------------------------


def _install_stub_model() -> None:
    """The deterministic agents behind the REAL service seams.

    ``execute_run_command`` constructs its RunService with the DEFAULT
    agents; the default builder is the one seam where the model enters the
    worker path, so the double replaces it there. Every policy — admission,
    budgets, fences, targets, publication — is the shipped code.
    """
    import forge.runs.service as service_module
    from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

    def _stub_agents(settings, gitlab, session_factory, budget=None):  # noqa: ANN001
        return StubPlanner(), StubImplementer(), StubReviewer()

    assert service_module and StubImplementer and StubPlanner and StubReviewer
    service_module.build_default_agents = _stub_agents


# ----------------------------------------------------------------------
# Shared discrimination: is the budgeted run committed anywhere?
# ----------------------------------------------------------------------


def _install_budget_seam(action: str) -> None:
    """Instrument ``forge.durable.open_budget_from_spec`` — the ONE symbol
    both the /go leg and the round admission call (function-local import:
    the package attribute is the resolved seam).

    *action* selects the instrumentation:

    - ``kill-child-admission`` — SIGKILL after the real call when the
      budgeted run is not yet COMMITTED anywhere (the admission's child,
      flushed only inside the caller's open transaction): the admission
      transaction dies with the process, nothing partial survives;
    - ``budget-before-child`` — the #356 regression: raise the
      ``open_budget`` application guard (``RunNotFound``) for exactly that
      uncommitted-child call — the observable the pre-fix ordering produced
      for every finite spec.
    """
    import forge.durable as durable_package
    from forge.durable.controller import RunNotFound
    from forge.durable.models import FlowRun
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    real = durable_package.open_budget_from_spec
    engine_holder: dict[str, object] = {}

    def _factory(db_url: str):
        if "engine" not in engine_holder:
            engine_holder["engine"] = create_async_engine(
                db_url, connect_args={"timeout": 30} if db_url.startswith("sqlite") else {}
            )
        return async_sessionmaker(engine_holder["engine"], expire_on_commit=False)

    async def _committed_anywhere(db_url: str, run_id: str) -> bool:
        """A SEPARATE connection cannot see the caller's open transaction —
        this is the honest read of 'does this run exist yet' from outside
        the admission.

        The probe is RETRIED under a small deadline before falling back:
        a single transient read failure (first-connection latency, a
        lock wait, a loaded runner's scheduling hiccup) must not suppress
        the kill boundary — the answer "committed" is reserved for a
        probe that genuinely, repeatedly fails (the honest fallback: a
        probe failure must not fake the boundary)."""
        factory = _factory(db_url)
        deadline = time.monotonic() + 10.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                async with factory() as session:
                    return (
                        await session.execute(select(FlowRun.id).where(FlowRun.id == run_id))
                    ).first() is not None
            except Exception as exc:  # noqa: BLE001 — a transient read retries
                last_error = exc
                await asyncio.sleep(0.1)
        if last_error is not None:  # pragma: no cover — the genuinely failing probe
            print(f"mx-worker: budget probe failed: {last_error!r}", file=sys.stderr)
        return True

    async def instrumented(session, **kwargs):  # noqa: ANN001
        run_id = str(kwargs.get("run_id") or "")
        fresh = not await _committed_anywhere(os.environ["MX_DB_URL"], run_id)
        if action == "budget-before-child" and fresh:
            raise RunNotFound(f"flow run {run_id!r} not found")
        result = await real(session, **kwargs)
        if action == "kill-child-admission" and fresh:
            _hard_kill()  # pragma: no cover — never returns
        return result

    durable_package.open_budget_from_spec = instrumented


def _install_kill_point(name: str, nth: int) -> None:
    """The writer-boundary fault windows (see the module docstring)."""
    if name == "native-commit":
        import forge.gitlab.client as gitlab_client_module

        real = gitlab_client_module.GitLabClient.create_commit
        state = {"calls": 0}

        async def kill_after(self, *args, **kwargs):  # noqa: ANN001
            result = await real(self, *args, **kwargs)
            state["calls"] += 1
            if state["calls"] >= nth:
                _hard_kill()  # pragma: no cover — never returns
            return result

        gitlab_client_module.GitLabClient.create_commit = kill_after
        return
    if name == "journal-completion":
        import forge.repository.writer as writer_module

        real = writer_module.ChangesetWriter.apply
        state = {"calls": 0}

        async def kill_after_apply(self, *args, **kwargs):  # noqa: ANN001
            result = await real(self, *args, **kwargs)
            state["calls"] += 1
            if state["calls"] >= nth:
                _hard_kill()  # pragma: no cover — never returns
            return result

        writer_module.ChangesetWriter.apply = kill_after_apply
        return
    raise SystemExit(f"unknown kill point: {name!r}")


def _install_mutation(name: str) -> None:
    """The regression patches — see the module docstring."""
    if name == "budget-before-child":
        _install_budget_seam("budget-before-child")
        return
    if name == "base-guard-first":
        from forge.runs.service import RunService

        async def fence_first(self, row, child, head):  # noqa: ANN001
            # The pre-#358 branch order: the base-head fence decided BEFORE
            # any own-effect classification — a moved head is FOREIGN, the
            # intact base is NOT_DISPATCHED, and nothing else exists.
            if str(head) == str(row.base_head_sha):
                return "not_dispatched", ""
            return "foreign", ""

        RunService._classify_round_head = fence_first  # type: ignore[method-assign]
        return
    if name == "any-terminal-occupancy":
        from forge.adaptive.admission import NativeStatus
        from forge.gitlab.schemas import Pipeline
        from forge.runs.service import RunService, _CI_ACTIVE_STATUSES

        def any_terminal(  # noqa: ANN001
            cls,
            pipelines: list[Pipeline],
            corr,  # noqa: ARG001
        ):
            # The pre-#360 predicate, verbatim in shape: an empty listing is
            # "never started" (TERMINAL), and ANY terminal row in the
            # listing releases the slot — a historical success beside a
            # current running job read TERMINAL.
            if not pipelines:
                return NativeStatus.TERMINAL
            statuses = {(p.status or "").lower() for p in pipelines}
            if statuses - _CI_ACTIVE_STATUSES:
                return NativeStatus.TERMINAL
            return NativeStatus.RUNNING

        RunService._branch_search_occupancy = classmethod(any_terminal)  # type: ignore[assignment]
        return
    raise SystemExit(f"unknown mutation: {name!r}")


# ----------------------------------------------------------------------
# The loops
# ----------------------------------------------------------------------


async def _quiet_supervisor(session_factory, shutdown: asyncio.Event, args) -> None:  # noqa: ANN001
    """Set *shutdown* once the step queue is quiet and the reconciler ran.

    Termination is keyed on the OBSERVABLE (no ``scheduled``/``running``
    step for ``--quiet-polls`` consecutive reads), never on wall-clock:
    ``--max-seconds`` is a runaway ceiling that may end the child only
    while the queue is EMPTY — it must NEVER cut a step mid-flight. A
    publication cancelled between its native commit/MR and the journaled
    completion is re-executed once the lease expires and duplicates the
    NATIVE effect (the duplicate-MR flake a starved runner produced when
    it outran the old deadline). With work in flight the child keeps
    going; the launching test's subprocess timeout is the loud backstop."""
    from sqlalchemy import select

    from forge.durable.models import StepRun

    quiet = 0
    runaway = time.monotonic() + args.max_seconds
    while not shutdown.is_set():
        await asyncio.sleep(0.15)
        try:
            async with session_factory() as session:
                pending = (
                    await session.execute(
                        select(StepRun.id).where(StepRun.status.in_(["scheduled", "running"]))
                    )
                ).first()
        except Exception:  # noqa: BLE001 — a transient read retries
            continue
        if pending is not None:
            quiet = 0  # work in flight — the ceiling never cuts here
            continue
        quiet += 1
        if quiet >= args.quiet_polls:
            shutdown.set()
            return
        if time.monotonic() > runaway:
            print("mx-worker: runaway ceiling reached on a quiet queue", file=sys.stderr)
            shutdown.set()
            return


async def _tick_supervisor(service, shutdown: asyncio.Event, ticks: int) -> None:  # noqa: ANN001
    """Count full reconciler cycles (keyed on the FIRST pass) → shutdown."""
    counted = 0
    real = service.evaluate_waiting_ci

    async def _counting() -> None:
        nonlocal counted
        counted += 1
        await real()

    service.evaluate_waiting_ci = _counting  # type: ignore[method-assign]
    while counted < ticks and not shutdown.is_set():
        await asyncio.sleep(0.05)
    shutdown.set()


async def _run(args, settings, ready_file: Path) -> None:  # noqa: ANN001
    from forge.config import ForgeConfig
    from forge.database import reset_engine
    from forge.gitlab.client import GitLabClient
    from forge.runs.reconciler import run_reconciler
    from forge.runs.service import RunService, forge_token
    from forge.worker.steps import run_step_reaper, run_step_worker
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(
        args.db_url, connect_args={"timeout": 30} if args.db_url.startswith("sqlite") else {}
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    shutdown = asyncio.Event()
    gitlab = GitLabClient(
        base_url=settings.GITLAB_URL,
        token=forge_token(settings),
    )
    service = RunService(
        session_factory=session_factory,
        gitlab=gitlab,
        settings=settings,
        config=ForgeConfig(),
    )
    tasks = [asyncio.create_task(run_reconciler(service, 0.3, shutdown))]
    if args.mode == "full":
        tasks.append(
            asyncio.create_task(
                run_step_worker(
                    session_factory,
                    settings,
                    ForgeConfig(),
                    "mx-worker",
                    shutdown,
                    None,
                    poll_interval=0.1,
                )
            )
        )
        # The step reaper belongs to the same installed composition
        # (``worker/app.main`` gathers it): a worker that died mid-step has
        # its expired lease rescheduled here — the recovery the fault-window
        # traces rely on.
        tasks.append(asyncio.create_task(run_step_reaper(session_factory, shutdown, interval=0.5)))
        tasks.append(asyncio.create_task(_quiet_supervisor(session_factory, shutdown, args)))
    else:
        tasks.append(asyncio.create_task(_tick_supervisor(service, shutdown, args.ticks)))
    ready_file.write_text(json.dumps({"pid": os.getpid()}))
    try:
        # No timeout here: the supervisors own termination and only ever
        # complete with NOTHING in flight (see _quiet_supervisor) — an
        # outer wall-clock timeout is exactly the mid-flight cancellation
        # that duplicated native effects on a starved runner. The
        # launching test's subprocess timeout is the loud backstop.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        shutdown.set()
        # By now every loop is idle (the supervisor fired on an empty
        # queue); 30s is a CI-tolerant grace for a pass to land its
        # terminal write under load.
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=30)
        for task in done:
            task.result()  # a crashed loop fails the child loudly
    finally:
        await gitlab.close()
        await engine.dispose()
        reset_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--db-url", required=True, help="the shared durable database URL")
    parser.add_argument("--mode", choices=("full", "reconciler"), default="full")
    parser.add_argument("--ticks", type=int, default=2, help="reconciler cycles (reconciler mode)")
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=90.0,
        help="runaway ceiling, enforced only while the step queue is empty "
        "(never cuts a step mid-flight; the launcher's subprocess timeout "
        "is the loud outer backstop)",
    )
    parser.add_argument("--quiet-polls", type=int, default=3)
    parser.add_argument(
        "--kill",
        choices=("child-admission", "native-commit", "journal-completion"),
    )
    parser.add_argument("--nth", type=int, default=1, help="the Nth call for writer kill points")
    parser.add_argument(
        "--mutation",
        choices=("budget-before-child", "base-guard-first", "any-terminal-occupancy"),
    )
    args = parser.parse_args()

    _install_stub_model()
    if args.kill == "child-admission":
        os.environ["MX_DB_URL"] = args.db_url
        _install_budget_seam("kill-child-admission")
    elif args.kill:
        _install_kill_point(args.kill, args.nth)
    if args.mutation:
        if args.mutation == "budget-before-child":
            os.environ["MX_DB_URL"] = args.db_url
        _install_mutation(args.mutation)

    import forge.adaptive.mailbox_db  # noqa: F401 — control_commands joins the schema
    import forge.adaptive.pause_fence  # noqa: F401 — pause_fences joins the schema
    from forge.config import Settings

    assert forge.adaptive.mailbox_db and forge.adaptive.pause_fence
    asyncio.run(_run(args, Settings(), Path(args.ready_file)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
