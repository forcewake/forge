"""Bounded-step checkpoints (R07): persistable step results with replay.

A bounded step is ONE non-deterministic, paid or remote-effecting unit of a
run's advance leg — ``plan`` (LLM), ``propose`` (LLM), ``review`` (LLM),
``publish`` (remote), ``ensure_change_request`` (remote), ``notify``
(remote). The crash-safety contract: a handler's output is persisted under
its replay key ``(run_id, cycle, step, input_digest)`` BEFORE the next stage
is scheduled, and a re-entering handler that finds its output persisted
REPLAYS it instead of calling the model / the provider again. A crash can
therefore never re-derive (never double-pay for) work whose result is
already durable; a changed ``input_digest`` (new task text, new base, new
cycle) legitimately re-runs.

The store is the existing ``step_runs`` table (ADR-0017 machinery, no new
schema): a checkpoint row is inserted directly as ``succeeded`` — the
worker's claim loop only picks ``scheduled`` rows, so a checkpoint is never
scheduled, claimed or retried; it is the durable RESULT of a handler, keyed
for replay. ``payload`` carries the key, ``output`` carries the result
document the replaying handler consumes (the same shape
``step_runs.output`` already holds for command steps).

The publish leg keeps its own dedicated machinery (R11
``publication_intents``): these checkpoints record RESULTS; the intents own
the remote-effect window. A replaying handler that reaches its publish leg
hands off to the intents (probe/adopt/redispatch) — it never posts around
them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable.models import StepRun
from forge.runs.spec import canonical_json_digest

#: The ``step_name`` marker distinguishing checkpoint rows from schedulable
#: command steps. Checkpoint rows are born ``succeeded`` and are invisible
#: to the claim loop (which selects ``scheduled`` only).
CHECKPOINT_STEP_PREFIX = "checkpoint:"


def step_input_digest(parts: dict[str, Any]) -> str:
    """The replay key's input half: sha256 over the canonical JSON of *parts*.

    Every input that legitimately changes the handler's result belongs in
    here (task text, plan summary, attempt base, candidate sha, ...); the
    run id / step / cycle are key columns, not digest material. Same digest
    ⇒ replay the persisted output; different digest ⇒ the input moved and
    the handler re-runs.
    """
    return canonical_json_digest(parts)


def checkpoint_step_name(step: str) -> str:
    return f"{CHECKPOINT_STEP_PREFIX}{step}"


def _key_matches(payload: dict[str, Any], cycle: int, input_digest: str) -> bool:
    """Whether a row's payload carries exactly this replay key."""
    return (
        int(payload.get("cycle") or 1) == int(cycle)
        and str(payload.get("input_digest") or "") == input_digest
    )


async def record_step_output(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    step: str,
    input_digest: str,
    output: dict[str, Any],
    cycle: int = 1,
) -> bool:
    """Persist one handler result under its replay key; ``True`` when written.

    Idempotent: a row for the same ``(run_id, cycle, step, input_digest)``
    key is left untouched — the FIRST persisted result wins, which is what
    makes the checkpoint itself crash-safe (a re-driven recorder cannot
    overwrite the result a replay is entitled to).
    """
    name = checkpoint_step_name(step)
    async with session_factory() as session:
        existing = (
            (
                await session.execute(
                    select(StepRun)
                    .where(StepRun.flow_run_id == run_id, StepRun.step_name == name)
                    .order_by(StepRun.id.desc())
                )
            )
            .scalars()
            .all()
        )
        for row in existing:
            payload = dict(row.payload or {})
            if _key_matches(payload, cycle, input_digest):
                return False  # already persisted — the first result stands
        session.add(
            StepRun(
                flow_run_id=run_id,
                step_name=name,
                status="succeeded",
                payload={"step": step, "cycle": int(cycle), "input_digest": input_digest},
                output=dict(output),
            )
        )
        await session.commit()
    return True


async def load_step_output(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    step: str,
    input_digest: str,
    cycle: int = 1,
) -> dict[str, Any] | None:
    """The persisted output for this replay key, or ``None``.

    ``None`` means "no persisted result for THIS input" — either the step
    never ran, or its input digest moved and a re-run is the honest answer.
    The newest matching row wins (rows are written newest-last; the key is
    unique in practice, the ordering only guards a raced double insert).
    """
    name = checkpoint_step_name(step)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(StepRun)
                    .where(StepRun.flow_run_id == run_id, StepRun.step_name == name)
                    .order_by(StepRun.id.desc())
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if _key_matches(dict(row.payload or {}), cycle, input_digest):
                return dict(row.output or {})
    return None
