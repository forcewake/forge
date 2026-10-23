"""Actual SQL persistence and races for the additive engine repositories."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import forge.durable.models  # noqa: F401
import forge.store.models  # noqa: F401
from forge.domain.states import Handler, State
from forge.models.base import Base
from forge.store.effects import EffectStore
from forge.store.records import RunRequest, StepRequest, StaleLease, StoreConflict
from forge.store.runs import RunStore
from forge.store.steps import StepStore

URLS = ["sqlite"] + (
    [os.environ["FORGE_ENGINE_TEST_DATABASE_URL"]]
    if os.environ.get("FORGE_ENGINE_TEST_DATABASE_URL")
    else []
)


@pytest.fixture(params=URLS, ids=lambda value: "sqlite" if value == "sqlite" else "postgres")
async def store(tmp_path, request):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}"
        if request.param == "sqlite"
        else request.param
    )
    engine = create_async_engine(url)
    tables = [table for table in Base.metadata.sorted_tables if table.name.startswith("eng_")]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def request(subject="7", connection="main"):
    return RunRequest(uuid4().hex, "github", connection, "123", "issue", subject)


async def claimed(factory):
    run = await RunStore(factory).admit(request())
    now = datetime.now(timezone.utc)
    await StepStore(factory).enqueue(
        StepRequest(run.id, Handler.PLAN, f"{run.id}:plan", "{}", now - timedelta(seconds=1))
    )
    (lease,) = await StepStore(factory).claim_due("worker-a", now, 60)
    return run, lease


async def intent(factory, lease, key="effect", kind="commit", payload='{"base":"abc"}', **kwargs):
    return await EffectStore(factory).request_once(
        lease,
        key=key,
        kind=kind,
        payload_json=payload,
        payload_digest=hashlib.sha256(payload.encode()).hexdigest(),
        **kwargs,
    )


async def test_canonical_subject_admission_is_atomic_and_connections_are_distinct(store):
    candidates = [request() for _ in range(2)]
    results = await asyncio.gather(
        *(RunStore(store).admit(item) for item in candidates), return_exceptions=True
    )
    assert sum(isinstance(item, StoreConflict) for item in results) == 1
    winner = next(item for item in results if not isinstance(item, Exception))
    assert (
        await RunStore(store).admit(next(item for item in candidates if item.id == winner.id))
    ).id == winner.id
    assert (await RunStore(store).admit(request(connection="other"))).connection_id == "other"
    with pytest.raises(StoreConflict, match="identity changed"):
        await RunStore(store).admit(
            replace(next(item for item in candidates if item.id == winner.id), subject_id="8")
        )


async def test_two_workers_never_claim_one_step_epoch_twice(store):
    run = await RunStore(store).admit(request())
    now = datetime.now(timezone.utc)
    await StepStore(store).enqueue(StepRequest(run.id, Handler.PLAN, "one", "{}", now))
    results = await asyncio.gather(
        *(StepStore(store).claim_due(f"worker-{n}", now, 20) for n in range(2))
    )
    leases = [lease for group in results for lease in group]
    assert len(leases) == 1
    assert leases[0].epoch == 1
    (reclaimed,) = await StepStore(store).claim_due("replacement", now + timedelta(seconds=21), 20)
    assert reclaimed.epoch == 2 and reclaimed.owner == "replacement"
    with pytest.raises(StaleLease):
        await StepStore(store).complete_and_enqueue(
            leases[0], "{}", now=now + timedelta(seconds=22)
        )
    await StepStore(store).complete_and_enqueue(reclaimed, "{}", now=now + timedelta(seconds=22))


async def test_result_and_every_successor_rollback_together(store):
    run, lease = await claimed(store)
    now = datetime.now(timezone.utc)
    duplicate = StepRequest(run.id, Handler.APPROVE, "duplicate", "{}", now)
    await StepStore(store).enqueue(duplicate)
    fresh = StepRequest(run.id, Handler.APPROVE, "new", "{}", now)
    with pytest.raises(StoreConflict, match="frozen inputs"):
        await StepStore(store).complete_and_enqueue(
            lease, '{"done":true}', (fresh, replace(duplicate, payload_json='{"changed":true}'))
        )
    assert await StepStore(store).get("new") is None
    original = await StepStore(store).get(f"{run.id}:plan")
    assert original.state == "running" and original.result_json is None
    completed = await StepStore(store).complete_and_enqueue(
        lease, '{"done":true}', (fresh, duplicate)
    )
    assert completed.state == "succeeded"
    assert (await StepStore(store).get("new")).state == "scheduled"


async def test_cancel_counter_fences_dispatch_without_changing_resume_generation(store):
    run, lease = await claimed(store)
    effect = await intent(store, lease)
    cancelled = await RunStore(store).request_cancel(run.id, expected_version=run.version)
    assert cancelled.generation == run.generation and cancelled.cancellation_generation == 1
    with pytest.raises(StaleLease):
        await EffectStore(store).mark_dispatched(effect.id, lease, expected_version=effect.version)
    with pytest.raises(StaleLease):
        await StepStore(store).complete_and_enqueue(lease, "{}")
    assert (await EffectStore(store).get(effect.key)).state == "requested"


async def test_effect_keys_and_canonical_payloads_refuse_changed_inputs(store):
    _, lease = await claimed(store)
    effect = await intent(store, lease)
    assert (await intent(store, lease)).id == effect.id
    with pytest.raises(StoreConflict):
        await intent(store, lease, payload='{"base":"changed"}')
    with pytest.raises(StoreConflict):
        await intent(store, lease, kind="comment")
    with pytest.raises(ValueError, match="canonical"):
        await intent(store, lease, key="bad", payload='{"z": 1, "a": 2}')
    with pytest.raises(ValueError, match="duplicate"):
        await intent(store, lease, key="bad", payload='{"x":1,"x":2}')
    with pytest.raises(StoreConflict, match="digest"):
        await EffectStore(store).request_once(
            lease, key="bad", kind="commit", payload_json="{}", payload_digest="0" * 64
        )


async def test_dispatched_window_is_persisted_and_t3_is_unfenced_monotonic(store):
    run, lease = await claimed(store)
    effect = await intent(store, lease)
    dispatched = await EffectStore(store).mark_dispatched(
        effect.id, lease, expected_version=effect.version
    )
    assert (
        dispatched.dispatched_at is not None and dispatched.dispatch_deadline_at == lease.expires_at
    )
    await RunStore(store).request_cancel(run.id, expected_version=run.version)
    adopted = await EffectStore(store).resolve_probe(
        effect.id,
        expected_version=dispatched.version,
        state="adopted",
        evidence_json='{"found":true}',
        outcome_json='{"sha":"yes"}',
    )
    delayed = await EffectStore(store).record_observation(
        effect.id, state="unknown", outcome_json='{"timeout":true}'
    )
    contradictory = await EffectStore(store).record_observation(
        effect.id, state="committed", outcome_json='{"sha":"other"}'
    )
    assert delayed.state == contradictory.state == "adopted"
    assert contradictory.outcome_json == adopted.outcome_json == '{"sha":"yes"}'
    assert len(contradictory.observations) == 3


async def test_probe_resolution_uses_version_cas_and_keeps_old_dispatch_window(store):
    _, lease = await claimed(store)
    effect = await intent(store, lease)
    dispatched = await EffectStore(store).mark_dispatched(
        effect.id, lease, expected_version=effect.version
    )
    results = await asyncio.gather(
        *(
            EffectStore(store).resolve_probe(
                effect.id,
                expected_version=dispatched.version,
                state="requested",
                evidence_json='{"definitely_absent":true}',
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, StoreConflict) for item in results) == 1
    requested = await EffectStore(store).get(effect.key)
    assert (
        requested.state == "requested"
        and requested.dispatch_deadline_at == dispatched.dispatch_deadline_at
    )
    # A late observation survives even after an adapter's incorrect absence conclusion.
    late = await EffectStore(store).record_observation(
        effect.id, state="committed", outcome_json='{"sha":"late"}'
    )
    assert late.state == "committed" and len(late.observations) == 2


async def test_draft_uniqueness_is_repository_and_head_not_display_key(store):
    _, lease = await claimed(store)
    effect = await intent(
        store,
        lease,
        key="root:draft",
        kind="ensure_draft",
        repo_key="github:main:123",
        head_branch="factory/root",
    )
    with pytest.raises(StoreConflict, match="draft identity"):
        await intent(
            store,
            lease,
            key="other:draft",
            kind="ensure_draft",
            repo_key="github:main:123",
            head_branch="factory/root",
        )
    assert (
        await intent(
            store,
            lease,
            key="root:draft",
            kind="ensure_draft",
            repo_key="github:main:123",
            head_branch="factory/root",
        )
    ).id == effect.id
    with pytest.raises(StoreConflict):
        await intent(store, lease, key="no-identity", kind="ensure_draft")


async def test_frozen_spec_checks_digest_and_rejects_changed_reinsert(store):
    run = await RunStore(store).admit(request())
    digest = hashlib.sha256(b"{}").hexdigest()
    first = await RunStore(store).put_spec_once(run.id, document_json="{}", digest=digest)
    assert await RunStore(store).put_spec_once(run.id, document_json="{}", digest=digest) == first
    changed = '{"schema":"changed"}'
    with pytest.raises(StoreConflict):
        await RunStore(store).put_spec_once(
            run.id, document_json=changed, digest=hashlib.sha256(changed.encode()).hexdigest()
        )


async def test_schema_has_only_seventeen_groups_and_enforces_closed_states(store):
    async with store() as session:
        names = await session.run_sync(lambda sync: inspect(sync.bind).get_table_names())
        assert len([name for name in names if name.startswith("eng_")]) == 17
    run = await RunStore(store).admit(request())
    async with store() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE eng_runs SET state='invented' WHERE id=:id"), {"id": run.id}
            )
            await session.commit()
        await session.rollback()


async def test_additive_revision_preserves_legacy_bytes_and_refuses_history_downgrade(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    path = Path(__file__).parents[1] / "alembic/versions/033_engine_tables.py"
    module_spec = importlib.util.spec_from_file_location("engine_revision", path)
    migration = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE flow_runs (id TEXT PRIMARY KEY, evidence TEXT NOT NULL)")
            )
            await conn.execute(
                text("INSERT INTO flow_runs VALUES (:id, :evidence)"),
                {"id": "legacy", "evidence": '{"literal":true}'},
            )

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()

            await conn.run_sync(upgrade)
            assert (
                await conn.execute(text("SELECT evidence FROM flow_runs"))
            ).scalar_one() == '{"literal":true}'
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await RunStore(factory).admit(request())
        async with engine.begin() as conn:

            def downgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.downgrade()

            with pytest.raises(RuntimeError, match="history exists"):
                await conn.run_sync(downgrade)
    finally:
        await engine.dispose()


async def test_money_and_count_storage_preserves_exact_values_without_float(store):
    from decimal import Decimal, localcontext
    from sqlalchemy.exc import StatementError
    from forge.store.models_budget import EngineBudgetPool

    run = await RunStore(store).admit(request())
    values = (Decimal("99999999999999999999.123456789123456789"), Decimal("0.000000000000000001"))
    with localcontext() as context:
        context.prec = 6
        async with store.begin() as session:
            for index, value in enumerate(values):
                session.add(
                    EngineBudgetPool(
                        id=uuid4().hex,
                        run_id=run.id,
                        purpose="implementation" if index == 0 else "closing",
                        usd=value,
                        calls=2**63 - 1,
                        tokens=9007199254740993,
                        state_json="{}",
                    )
                )
        async with store() as session:
            rows = (
                await session.scalars(
                    select(EngineBudgetPool).order_by(EngineBudgetPool.purpose.desc())
                )
            ).all()
            assert tuple(row.usd for row in rows) == values
            assert all(row.calls == 2**63 - 1 and row.tokens == 9007199254740993 for row in rows)
    for kwargs in (
        {"usd": 0.1, "calls": 1},
        {"usd": Decimal("0"), "calls": 2**63},
        {"usd": Decimal("0"), "calls": True},
    ):
        async with store() as session:
            session.add(
                EngineBudgetPool(
                    id=uuid4().hex,
                    run_id=run.id,
                    purpose="implementation",
                    tokens=1,
                    state_json="{}",
                    **kwargs,
                )
            )
            with pytest.raises(StatementError):
                await session.flush()
            await session.rollback()


async def test_root_draft_key_survives_child_round_without_cross_lineage_reuse(store):
    root, lease = await claimed(store)
    options = dict(
        key="root:draft",
        kind="ensure_draft",
        repo_key="github:main:123",
        head_branch="factory/root",
    )
    effect = await intent(store, lease, **options)
    await RunStore(store).transition(
        root.id, expected_version=root.version, target=State.BLOCKED, failed_handler=Handler.PLAN
    )
    child = await RunStore(store).admit(
        replace(request(subject="8"), root_run_id=root.id, parent_run_id=root.id)
    )
    now = datetime.now(timezone.utc)
    await StepStore(store).enqueue(
        StepRequest(child.id, Handler.PUBLISH, "child-publish", "{}", now)
    )
    (child_lease,) = await StepStore(store).claim_due("child-worker", now, 60)
    assert (await intent(store, child_lease, **options)).id == effect.id
    dispatched = await EffectStore(store).mark_dispatched(
        effect.id, child_lease, expected_version=effect.version
    )
    assert dispatched.run_id == child.id and dispatched.state == "dispatched"
    foreign = await RunStore(store).admit(request(subject="9"))
    await StepStore(store).enqueue(
        StepRequest(foreign.id, Handler.PUBLISH, "foreign-publish", "{}", now)
    )
    (foreign_lease,) = await StepStore(store).claim_due("foreign-worker", now, 60)
    with pytest.raises(StoreConflict):
        await intent(store, foreign_lease, **options)


async def test_identical_concurrent_admission_returns_the_winning_frozen_run(store, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from forge.store.models_runs import EngineRun

    item = request()
    barrier = asyncio.Barrier(2)
    original = AsyncSession.get

    async def synchronized_get(session, entity, ident, *args, **kwargs):
        result = await original(session, entity, ident, *args, **kwargs)
        if (
            entity is EngineRun
            and ident == item.id
            and result is None
            and session.get_bind().dialect.name == "postgresql"
        ):
            await barrier.wait()
        return result

    monkeypatch.setattr(AsyncSession, "get", synchronized_get)
    results = await asyncio.gather(
        *(RunStore(store).admit(item) for _ in range(2)), return_exceptions=True
    )
    assert all(not isinstance(result, Exception) and result.id == item.id for result in results)
    assert results[0] == results[1]
