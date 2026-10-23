"""Actual new-engine plan/approval flows and fenced SQL failure boundaries."""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import os
import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import forge.store.models  # noqa: F401
from forge_protocol import digest

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import BudgetLimits, Resources
from forge.domain.paths import resolve_write_policy
from forge.domain.plan import Plan, PlanStep, RepositoryIdentity
from forge.domain.run_spec import RunPolicy, VerificationContract
from forge.domain.states import Handler, State
from forge.engine.approve import Approve, ApproveHandler
from forge.engine.effects import Effects
from forge.engine.plan import Implement, PlanHandler
from forge.engine.plan_values import PlanIntent
from forge.engine.runner import LeaseHolder, StepContext, StepRunner
from forge.models.base import Base
from forge.port_values import (
    Actor,
    NeighborSnapshot,
    PlanningSnapshot,
    RepoRef,
    SubjectRef,
    TaskSnapshot,
)
from forge.providers.fake import Blob, FakeProvider
from forge.providers.fake.state import commit_value, ref_key
from forge.store.admission import AdmissionStore
from forge.store.approval import ApprovalStore
from forge.store.approval_values import ApprovalClaim
from forge.store.effects import EffectStore
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_runs import EngineApprovedInput, EngineGate, EngineRun
from forge.store.records import RunRequest, StaleLease, StoreConflict
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests.fakes.ports import ScriptedPorts


class Clock:
    def __init__(self):
        self.instant = datetime.now(timezone.utc)

    def now(self):
        return self.instant


@dataclass
class Environment:
    factory: object
    fake: FakeProvider
    clock: Clock
    intent: PlanIntent
    runs: RunStore
    steps: StepStore
    approvals: ApprovalStore
    implement: Implement
    approve: Approve
    plan_handler: PlanHandler
    approve_handler: ApproveHandler
    effects: Effects
    runner: StepRunner
    planner: ScriptedPorts

    async def start(self):
        repo = self.intent.task.subject.repo.identity
        request = RunRequest(
            uuid4().hex, repo.provider, repo.connection_id, repo.repository_id, "issue", "1"
        )
        return await self.implement(request, self.intent)

    async def planned(self):
        run = await self.start()
        assert (await self.runner.tick()).completed == 1
        assert (await self.runner.tick()).completed == 1
        run = await self.runs.load(run.id)
        assert run.state == State.AWAITING_APPROVAL
        return run, await self.approvals.load(run.id)

    async def claim(self, run, frozen, **changes):
        approved = frozen.approved
        return ApprovalClaim(
            gate_id=frozen.gate_id,
            actor=changes.pop("actor", self.intent.actor),
            **{**approved.model_dump(exclude={"brief_text"}), **changes},
        )

    async def count(self, table):
        async with self.factory() as session:
            return await session.scalar(select(func.count()).select_from(table))


@pytest.fixture(params=("github", "gitlab"))
async def env(tmp_path, request):
    engine = create_async_engine(
        os.environ.get("FORGE_ENGINE_TEST_DATABASE_URL")
        or f"sqlite+aiosqlite:///{tmp_path / 'approval.db'}"
    )
    tables = [table for table in Base.metadata.sorted_tables if table.name.startswith("eng_")]
    async with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    fake = FakeProvider(provider=request.param)
    fake.now = clock.now()
    repo = RepoRef(
        identity=RepositoryIdentity(
            provider=request.param, connection_id="test", repository_id="42"
        ),
        full_name="owner/project",
    )
    sha = fake.seed_repository(repo, {"a.py": Blob(b"old\n")})
    subject = SubjectRef(repo=repo, number=1)
    task = TaskSnapshot(
        subject=subject,
        title="Change parser",
        description="Retain exact source.",
        version="issue-1",
    )
    fake.tasks[ref_key(subject)] = task
    actor = Actor(
        provider=request.param, connection_id="test", native_id="owner", login="owner", is_bot=False
    )
    fake.approvers.add((ref_key(subject), actor.native_id))
    policy = RunPolicy(
        driver="test",
        model="test-model",
        target_branch="main",
        verification=VerificationContract(required_checks=("unit",)),
        write_policy=resolve_write_policy("no_dependencies"),
        limits=BudgetLimits(usd=Decimal("10"), calls=10, tokens=10000, wallclock_seconds=600),
        closing_reserve=Resources(usd=Decimal("2"), calls=2, tokens=1000),
        planning_allowance=Resources(usd=Decimal("1"), calls=1, tokens=1000),
    )
    intent = PlanIntent(
        actor=actor,
        task=task,
        snapshot=PlanningSnapshot(primary=NeighborSnapshot(repo=repo, sha=sha)),
        policy=policy,
    )
    plan = Plan(
        summary="Correct the parser",
        steps=(
            PlanStep(
                step_id="change",
                title="Change",
                instructions="Apply the full correction.",
                files=("a.py",),
            ),
            PlanStep(
                step_id="verify",
                title="Verify",
                instructions="Run the required independent checks.",
            ),
        ),
    )
    planner = ScriptedPorts((("plan", plan),))
    runs, steps, approvals = RunStore(factory), StepStore(factory), ApprovalStore(factory)
    effects = Effects(EffectStore(factory), fake, fake, fake, fake, fake, ScriptedPorts(), clock)
    plan_handler = PlanHandler(runs, approvals, planner, fake, clock)
    approve_handler = ApproveHandler(runs, steps, approvals, fake, fake, clock)
    runner = StepRunner(
        steps,
        {Handler.PLAN: plan_handler, Handler.APPROVE: approve_handler},
        effects=effects,
        clock=clock,
        owner="worker",
        lease_seconds=60,
    )
    try:
        yield Environment(
            factory,
            fake,
            clock,
            intent,
            runs,
            steps,
            approvals,
            Implement(AdmissionStore(factory), fake, clock),
            Approve(steps, clock),
            plan_handler,
            approve_handler,
            effects,
            runner,
            planner,
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admission_enforces_actor_subject_and_connection_capacity(env):
    intruder = env.intent.actor.model_copy(update={"native_id": "intruder"})
    with pytest.raises(StoreConflict, match="approver"):
        await env.implement(
            RunRequest(uuid4().hex, env.fake.provider, "test", "42", "issue", "1"),
            env.intent.model_copy(update={"actor": intruder}),
        )
    assert await env.count(EngineRun) == await env.count(EngineStep) == 0
    env.intent = env.intent.model_copy(update={"max_active": 1})
    other_subject = SubjectRef(repo=env.intent.task.subject.repo, number=2)
    env.fake.approvers.add((ref_key(other_subject), env.intent.actor.native_id))
    other_intent = env.intent.model_copy(
        update={"task": env.intent.task.model_copy(update={"subject": other_subject})}
    )
    candidates = [
        (RunRequest(uuid4().hex, env.fake.provider, "test", "42", "issue", str(index)), intent)
        for index, intent in ((1, env.intent), (2, other_intent))
    ]
    results = await asyncio.gather(
        *(env.implement(request, intent) for request, intent in candidates), return_exceptions=True
    )
    assert sum(isinstance(result, StoreConflict) for result in results) == 1
    assert await env.count(EngineRun) == await env.count(EngineStep) == 1
    winner = next(result for result in results if not isinstance(result, Exception))
    same, winning_intent = next(pair for pair in candidates if pair[0].id == winner.id)
    assert (await env.implement(same, winning_intent)).id == winner.id
    with pytest.raises(StoreConflict, match="active run"):
        await env.implement(
            replace(same, id=uuid4().hex), winning_intent.model_copy(update={"max_active": 4})
        )
    second_subject = SubjectRef(repo=env.intent.task.subject.repo, number=2)
    env.fake.approvers.add((ref_key(second_subject), env.intent.actor.native_id))
    second = env.intent.model_copy(
        update={"task": env.intent.task.model_copy(update={"subject": second_subject})}
    )
    with pytest.raises(StoreConflict, match="capacity"):
        await env.implement(replace(same, id=uuid4().hex, subject_id="2"), second)
    assert await env.count(EngineRun) == 1


@pytest.mark.asyncio
async def test_plan_uses_real_recorded_comment_and_a_closed_prepared_gate(env):
    run = await env.start()
    await env.runner.tick()
    (lease,) = await env.steps.claim_due("manual", env.clock.now(), 60)
    step = await env.steps.load(lease.step_id)

    def crash(point, _row):
        if point == "after_call":
            raise RuntimeError("crash before native receipt")

    env.effects.hook = crash
    context = StepContext(LeaseHolder(lease), step, env.effects)
    with pytest.raises(RuntimeError, match="native receipt"):
        await env.plan_handler(context)
    frozen = await env.approvals.load(run.id)
    assert frozen.state == "prepared"
    assert (await env.runs.load(run.id)).state == State.PLANNING
    assert await env.count(EngineApprovedInput) == 0
    comment = next(iter(env.fake.comments.values()))
    assert comment.body == frozen.comment
    assert frozen.spec.spec_json in comment.body and frozen.approved.approved_digest in comment.body
    assert "Run the required independent checks." in comment.body
    initial_expiry = frozen.expires_at
    env.effects.hook = None
    env.clock.instant += timedelta(seconds=1)
    result = await env.plan_handler(context)
    assert len([name for name, _ in env.fake.calls if name == "post_comment"]) == 1
    assert len(env.planner.calls) == 1
    await env.steps.complete_and_enqueue(
        lease,
        result.result_json,
        result.successors,
        now=env.clock.now(),
        transition_to=result.next_state,
        approval=result.approval,
    )
    opened = await env.approvals.load(run.id)
    assert opened.state == "open" and opened.expires_at == initial_expiry
    assert (await env.runs.load(run.id)).state == State.AWAITING_APPROVAL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    (
        "unauthorized",
        "expired",
        "drift",
        "approved_digest",
        "plan_digest",
        "spec_digest",
        "base_sha",
        "revision",
        "gate_id",
        "sibling_gate",
    ),
)
async def test_approval_refuses_unauthorized_expired_drifted_and_changed_inputs(env, mode):
    run, frozen = await env.planned()
    changes = {}
    if mode == "unauthorized":
        changes["actor"] = env.intent.actor.model_copy(update={"native_id": "intruder"})
    elif mode == "expired":
        env.clock.instant = frozen.expires_at + timedelta(seconds=1)
    elif mode == "drift":
        native = env.fake.repo(env.intent.task.subject.repo)
        human = commit_value(
            dict(native.commits[frozen.spec.base_sha].files),
            frozen.spec.base_sha,
            "human moved base",
        )
        native.commits[human.sha] = human
        native.branches["main"] = human.sha
    elif mode == "revision":
        changes[mode] = 2
    elif mode in {"gate_id", "sibling_gate"}:
        claim = await env.claim(run, frozen)
        claim = claim.model_copy(update={"gate_id": "f" * 32})
        if mode == "sibling_gate":
            async with env.factory.begin() as session:
                stored_run = await session.get(EngineRun, run.id)
                session.add(
                    EngineGate(
                        id="f" * 32,
                        run_id=run.id,
                        decision_key="unrelated",
                        state="open",
                        approved_digest=frozen.approved.approved_digest,
                        actor_scope_json=stored_run.subject_key,
                        expires_at=frozen.expires_at,
                    )
                )
    else:
        changes[mode] = "f" * (40 if mode == "base_sha" else 64)
    if mode not in {"gate_id", "sibling_gate"}:
        claim = await env.claim(run, frozen, **changes)
    step = await env.approve(run.id, claim, command_key=uuid4().hex)
    await env.runner.tick()
    completed = await env.steps.load(step.id)
    assert "refused" in completed.result_json
    assert await env.count(EngineApprovedInput) == 0
    async with env.factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(EngineStep).where(EngineStep.handler == "dispatch")
            )
            == 0
        )
    after = await env.approvals.load(run.id)
    assert after.state != "consumed"
    assert (await env.runs.load(run.id)).state == (
        State.BLOCKED if mode in {"expired", "drift"} else State.AWAITING_APPROVAL
    )


@pytest.mark.asyncio
async def test_approval_and_dispatch_successor_commit_or_rollback_together(env, monkeypatch):
    run, frozen = await env.planned()
    claim = await env.claim(run, frozen)
    await env.approve(run.id, claim, command_key="atomic")
    (lease,) = await env.steps.claim_due("manual", env.clock.now(), 60)
    step = await env.steps.load(lease.step_id)
    result = await env.approve_handler(StepContext(LeaseHolder(lease), step, env.effects))
    import forge.store.steps as module

    original = module.enqueue_in

    async def crash_after_enqueue(*args):
        await original(*args)
        raise RuntimeError("crash inside atomic completion")

    monkeypatch.setattr(module, "enqueue_in", crash_after_enqueue)
    with pytest.raises(RuntimeError, match="atomic completion"):
        await env.steps.complete_and_enqueue(
            lease,
            result.result_json,
            result.successors,
            now=env.clock.now(),
            transition_to=result.next_state,
            approval=result.approval,
        )
    assert (await env.approvals.load(run.id)).state == "open"
    assert await env.count(EngineApprovedInput) == 0
    assert (await env.steps.load(step.id)).state == "running"
    assert (await env.runs.load(run.id)).state == State.AWAITING_APPROVAL
    monkeypatch.setattr(module, "enqueue_in", original)
    await env.steps.complete_and_enqueue(
        lease,
        result.result_json,
        result.successors,
        now=env.clock.now(),
        transition_to=result.next_state,
        approval=result.approval,
    )
    assert (await env.approvals.load(run.id)).state == "consumed"
    assert (await env.runs.load(run.id)).state == State.RUNNING
    async with env.factory() as session:
        approved = await session.scalar(select(EngineApprovedInput))
        gate = await session.scalar(select(EngineGate))
        assert ApprovedInput.model_validate_json(approved.document_json) == frozen.approved
        assert approved.digest == frozen.approved.approved_digest
        assert gate.consumed_step_id == lease.step_id
        assert (
            await session.scalar(
                select(func.count()).select_from(EngineStep).where(EngineStep.handler == "dispatch")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_double_go_and_lost_epoch_never_duplicate_approval(env):
    run, frozen = await env.planned()
    claim = await env.claim(run, frozen)
    await env.approve(run.id, claim, command_key="first")
    (old,) = await env.steps.claim_due("old", env.clock.now(), 1)
    step = await env.steps.load(old.step_id)
    result = await env.approve_handler(StepContext(LeaseHolder(old), step, env.effects))
    env.clock.instant += timedelta(seconds=2)
    (current,) = await env.steps.claim_due("current", env.clock.now(), 60)
    with pytest.raises(StaleLease):
        await env.steps.complete_and_enqueue(
            old,
            result.result_json,
            result.successors,
            now=env.clock.now(),
            transition_to=result.next_state,
            approval=result.approval,
        )
    assert (await env.approvals.load(run.id)).state == "open" and await env.count(
        EngineApprovedInput
    ) == 0
    fresh_result = await env.approve_handler(
        StepContext(LeaseHolder(current), await env.steps.load(current.step_id), env.effects)
    )
    await env.steps.complete_and_enqueue(
        current,
        fresh_result.result_json,
        fresh_result.successors,
        now=env.clock.now(),
        transition_to=fresh_result.next_state,
        approval=fresh_result.approval,
    )
    second = await env.approve(run.id, claim, command_key="second")
    (second_lease,) = [
        lease
        for lease in await env.steps.claim_due("second", env.clock.now(), 60, 2)
        if lease.step_id == second.id
    ]
    second_result = await env.approve_handler(
        StepContext(LeaseHolder(second_lease), await env.steps.load(second.id), env.effects)
    )
    assert second_result.approval is None and not second_result.successors
    await env.steps.complete_and_enqueue(
        second_lease, second_result.result_json, now=env.clock.now()
    )
    assert await env.count(EngineApprovedInput) == 1
    async with env.factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(EngineStep).where(EngineStep.handler == "dispatch")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_approval_migration_preserves_prior_gates(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    root = Path(__file__).resolve().parents[1]

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            for index, path in enumerate(
                (
                    root / "alembic/versions/033_engine_tables.py",
                    root / "alembic/versions/034_approval_step_binding.py",
                )
            ):
                spec = importlib.util.spec_from_file_location(path.stem, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.upgrade()
                if index == 0:
                    connection.execute(
                        text(
                            "INSERT INTO eng_runs (id,provider,connection_id,repository_id,subject_kind,subject_id,subject_key,root_run_id) VALUES (:id,'github','test','42','issue','1','literal-scope',:id)"
                        ),
                        {"id": "a" * 32},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO eng_gates (id,run_id,decision_key,state,approved_digest,actor_scope_json,expires_at) VALUES (:gate,:run,'initial','open',:digest,'literal-scope','2099-01-01 00:00:00')"
                        ),
                        {"gate": "b" * 32, "run": "a" * 32, "digest": "c" * 64},
                    )
                    original = dict(
                        connection.exec_driver_sql("SELECT * FROM eng_gates").mappings().one()
                    )
            upgraded = dict(connection.exec_driver_sql("SELECT * FROM eng_gates").mappings().one())
            assert all(upgraded[key] == value for key, value in original.items())
            assert upgraded["consumed_step_id"] is None
        columns = connection.exec_driver_sql("PRAGMA table_info(eng_gates)").all()
        assert "consumed_step_id" in [column[1] for column in columns]
        ddl = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name='eng_gates'"
        ).scalar_one()
        assert "'prepared'" in ddl and "FOREIGN KEY(consumed_step_id)" in ddl

    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("missing", "unknown", "body", "scope", "fingerprint"))
async def test_gate_rejects_unproven_or_mismatched_comment_receipts(env, tamper):
    run = await env.start()
    await env.runner.tick()
    (lease,) = await env.steps.claim_due("manual", env.clock.now(), 60)
    step = await env.steps.load(lease.step_id)
    result = await env.plan_handler(StepContext(LeaseHolder(lease), step, env.effects))
    async with env.factory.begin() as session:
        effect = await session.scalar(select(EngineEffect))
        if tamper == "missing":
            effect.key = "unrelated-effect"
        elif tamper == "unknown":
            effect.state = "unknown"
        elif tamper == "fingerprint":
            effect.payload_digest = "0" * 64
        else:
            payload = json.loads(effect.payload_json)
            if tamper == "body":
                payload["body"] = "summary without the full approved policy"
            else:
                payload["subject"]["number"] = 2
                outcome = json.loads(effect.outcome_json)
                outcome["value"]["subject"]["number"] = 2
                effect.outcome_json = json.dumps(outcome, sort_keys=True, separators=(",", ":"))
            effect.payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            effect.payload_digest = digest(payload)
    await env.steps.complete_and_enqueue(
        lease,
        result.result_json,
        now=env.clock.now(),
        transition_to=result.next_state,
        approval=result.approval,
    )
    assert (await env.approvals.load(run.id)).state == "prepared"
    assert (await env.runs.load(run.id)).state == State.BLOCKED
    assert await env.count(EngineApprovedInput) == 0


@pytest.mark.asyncio
async def test_concurrent_go_consumes_one_gate(env):
    run, frozen = await env.planned()
    claim = await env.claim(run, frozen)
    first = await env.approve(run.id, claim, command_key="concurrent-a")
    second = await env.approve(run.id, claim, command_key="concurrent-b")
    assert (await env.runner.tick(limit=2)).completed == 2
    assert await env.count(EngineApprovedInput) == 1
    results = [(await env.steps.load(step.id)).result_json for step in (first, second)]
    assert sum("refused" in result for result in results) == 1
    assert (await env.runs.load(run.id)).state == State.RUNNING
    async with env.factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(EngineStep).where(EngineStep.handler == "dispatch")
            )
            == 1
        )
