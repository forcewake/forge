"""Executable approval bytes survive task, policy and process drift without defaults."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from forge.api.lane_auth_values import LaneAuthError
from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import BudgetLimits, Resources
from forge.domain.paths import resolve_write_policy
from forge.domain.run_spec import RunSpec2, VerificationContract
from forge.domain.states import State
from forge.store.lane_auth import approved_brief
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineApprovedInput, EngineGate, EngineRunSpec
from forge.store.records import RunRequest, StoreConflict
from forge_protocol import Brief, RunIdentity, canonical_json
from tests.test_domain_approved_input import spec
from tests.test_durable_gates import consume
from tests.test_engine_approval import env as env


async def lane_input(env, run):
    current = await env.runs.load(run.id)
    identity = RunIdentity(
        forge_run_id=run.id, attempt=current.attempt, generation=current.generation
    )
    async with env.factory() as session:
        return await approved_brief(session, identity)


async def rewrite_spec(env, run, transform, *, rehash=False):
    async with env.factory.begin() as session:
        row = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run.id))
        document = json.loads(row.document_json)
        transform(document)
        row.document_json = canonical_json(document).decode()
        if rehash:
            row.digest = sha256(row.document_json.encode()).hexdigest()


class TestRunSpec:
    async def test_spec_frozen_at_plan_acceptance(self, env):
        plan = env.planner.script[0][1]
        run, frozen = await env.planned()
        async with env.factory() as session:
            row = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run.id))
            assert row.document_json == frozen.spec.spec_json
            assert row.digest == sha256(row.document_json.encode()).hexdigest()
        assert frozen.spec.task_title == env.intent.task.title
        assert frozen.spec.task_description == env.intent.task.description
        assert frozen.spec.plan == plan
        assert frozen.spec.base_sha == env.intent.snapshot.primary.sha
        assert frozen.spec.spec_json in frozen.comment

    @pytest.mark.parametrize(
        "field,value",
        (
            ("driver", "another_driver"),
            ("model", "another-model"),
            ("target_branch", "release"),
            ("revision", 2),
            ("task_title", "Edited title"),
            ("task_description", "Edited body"),
        ),
    )
    def test_material_policy_and_task_changes_have_another_identity(self, field, value):
        original = spec()
        document = json.loads(original.spec_json)
        document[field] = value
        changed = RunSpec2.model_validate_json(canonical_json(document))
        assert changed.digest != original.digest
        assert (
            ApprovedInput.from_spec(changed).approved_digest
            != ApprovedInput.from_spec(original).approved_digest
        )

    @pytest.mark.parametrize(
        "field",
        (
            "driver",
            "model",
            "target_branch",
            "verification",
            "limits",
            "write_policy",
            "execution_policy",
        ),
    )
    def test_policy_is_complete_and_roundtrips(self, field):
        original = spec()
        loaded = RunSpec2.model_validate_json(original.spec_json)
        assert getattr(loaded, field) == getattr(original, field)
        assert loaded.canonical_bytes == original.canonical_bytes

    async def test_setting_drift_after_start_cannot_change_the_stored_policy(self, env):
        run = await env.start()
        original = env.intent.policy
        env.intent = env.intent.model_copy(
            update={"policy": original.model_copy(update={"model": "new-live-model"})}
        )
        await env.runner.tick()
        await env.runner.tick()
        frozen = await env.approvals.load(run.id)
        assert frozen.spec.model == original.model
        assert frozen.spec.limits == original.limits


class TestPendingDecision:
    async def test_decision_created_at_plan_publication(self, env):
        run, frozen = await env.planned()
        async with env.factory() as session:
            gate = await session.get(EngineGate, frozen.gate_id)
            assert gate.state == "open" and gate.approved_digest == frozen.approved.approved_digest
            assert gate.run_id == run.id and gate.consumed_at is None
        assert frozen.expires_at > env.clock.now()
        assert str(frozen.expires_at.isoformat()) in frozen.comment

    async def test_go_consumes_the_pending_decision(self, env):
        run, frozen = await env.planned()
        await consume(env, run, frozen)
        assert (await env.approvals.load(run.id)).state == "consumed"
        assert (await env.runs.load(run.id)).state == State.RUNNING
        assert await env.count(EngineApprovedInput) == 1

    async def test_go_without_pending_decision_has_no_dispatch_authority(self, env):
        run = await env.start()
        with pytest.raises(LaneAuthError, match="approved input"):
            await lane_input(env, run)
        assert await env.count(EngineApprovedInput) == 0

    async def test_expired_decision_is_invalid(self, env):
        run, frozen = await env.planned()
        env.clock.instant = frozen.expires_at + timedelta(seconds=1)
        saved = await consume(env, run, frozen)
        assert json.loads(saved.result_json)["refused"] == "approval_expired"
        assert await env.count(EngineApprovedInput) == 0

    async def test_spec_digest_drift_invalidates_go(self, env):
        run, frozen = await env.planned()
        await rewrite_spec(env, run, lambda document: document.update(model="tampered"))
        with pytest.raises(StoreConflict, match="inconsistent"):
            await consume(env, run, frozen)
        assert await env.count(EngineApprovedInput) == 0

    async def test_forged_digest_still_blocked_by_the_gate_binding(self, env):
        run, frozen = await env.planned()
        await rewrite_spec(
            env, run, lambda document: document.update(model="tampered"), rehash=True
        )
        with pytest.raises(StoreConflict, match="gate disagrees"):
            await consume(env, run, frozen)
        assert await env.count(EngineApprovedInput) == 0


class TestAdmission:
    @pytest.mark.parametrize("bot", (False, True))
    async def test_non_approver_or_bot_is_denied_before_the_planner(self, env, bot):
        actor = env.intent.actor.model_copy(update={"native_id": "intruder", "is_bot": bot})
        subject = env.intent.task.subject
        request = RunRequest(uuid4().hex, env.fake.provider, "test", "42", "issue", "1")
        if bot:
            from forge.providers.fake.state import ref_key

            env.fake.approvers.add((ref_key(subject), actor.native_id))
        with pytest.raises(StoreConflict, match="approver"):
            await env.implement(request, env.intent.model_copy(update={"actor": actor}))
        assert not env.planner.calls and await env.count(EngineStep) == 0

    async def test_approver_implement_is_admitted(self, env):
        run = await env.start()
        assert run.state == State.QUEUED
        assert await env.count(EngineStep) == 1 and not env.planner.calls


class TestPathScope:
    def test_scoped_project_freezes_allowed_paths_into_the_spec(self):
        original = spec(
            write_policy=replace(
                resolve_write_policy("code_only"), allowed_globs=("src/*", "tests/*")
            )
        )
        loaded = RunSpec2.model_validate_json(original.spec_json)
        assert loaded.write_policy.allowed_globs == ("src/*", "tests/*")
        assert loaded.write_policy.denied_paths == original.write_policy.denied_paths

    def test_unscoped_project_has_explicit_policy_without_added_scope(self):
        original = spec()
        assert original.write_policy.allowed_globs == ()
        assert json.loads(original.spec_json)["write_policy"]["allowed_globs"] == []

    def test_scope_mutation_changes_approval_identity(self):
        original = spec()
        scoped = spec(write_policy=replace(original.write_policy, allowed_globs=("src/*",)))
        assert scoped.digest != original.digest
        assert (
            ApprovedInput.from_spec(scoped).approved_digest
            != ApprovedInput.from_spec(original).approved_digest
        )


class TestExecutableRunSpec:
    def test_schema_version_is_present_and_executable(self):
        original = spec()
        assert json.loads(original.spec_json)["schema"] == "forge.run-spec/2"
        brief = ApprovedInput.from_spec(original).to_brief(
            RunIdentity(forge_run_id="run", attempt=0, generation=0), original
        )
        assert brief.frozen_spec.model == original.model

    def test_round_trip_preserves_every_field(self):
        original = spec()
        assert RunSpec2.model_validate_json(original.spec_json) == original
        assert canonical_json(json.loads(original.spec_json)) == original.canonical_bytes

    @pytest.mark.parametrize(
        "version", (None, 1, 2, "forge.run-spec/1", "forge.run-spec/999", True)
    )
    def test_pre_executable_or_unknown_schema_is_refused(self, version):
        document = json.loads(spec().spec_json)
        document["schema"] = version
        with pytest.raises(ValidationError):
            RunSpec2.model_validate_json(canonical_json(document))

    @pytest.mark.parametrize("document", ("null", "[]", "true", "42", "{", '{"schema":1}'))
    def test_type_garbage_is_invalid_without_a_live_fallback(self, document):
        with pytest.raises(ValidationError):
            RunSpec2.model_validate_json(document)

    @pytest.mark.parametrize("value", (None, "", False))
    def test_missing_frozen_document_is_not_an_executable_brief(self, value):
        original = spec()
        brief = ApprovedInput.from_spec(original).to_brief(
            RunIdentity(forge_run_id="run", attempt=0, generation=0), original
        )
        with pytest.raises(ValidationError):
            Brief.model_validate({**brief.model_dump(), "spec_json": value})


class TestSpecBudgetCeilings:
    def test_ceilings_round_trip_through_the_document(self):
        original = spec()
        loaded = RunSpec2.model_validate_json(original.spec_json)
        assert loaded.limits == original.limits
        assert loaded.planning_allowance == original.planning_allowance
        assert loaded.closing_reserve == original.closing_reserve

    def test_absent_resource_axes_are_zero_instead_of_unlimited(self):
        limits = BudgetLimits(wallclock_seconds=30)
        assert limits.usd == Decimal(0) and limits.calls == limits.tokens == 0
        assert not limits.covers(Resources(calls=1))

    @pytest.mark.parametrize("field", ("usd", "calls", "tokens", "wallclock_seconds"))
    @pytest.mark.parametrize("value", (None, True, -1, float("inf")))
    def test_garbage_or_negative_ceiling_is_refused(self, field, value):
        values = dict(usd=Decimal("10"), calls=20, tokens=10000, wallclock_seconds=600)
        values[field] = value
        with pytest.raises(ValidationError):
            BudgetLimits(**values)

    def test_missing_wallclock_and_zero_deadline_are_refused(self):
        for document in ({}, {"wallclock_seconds": 0}):
            with pytest.raises(ValidationError):
                BudgetLimits(**document)

    @pytest.mark.parametrize("level", ("partial", "full", "none", "unlimited"))
    def test_old_enforcement_levels_cannot_supplement_the_closed_policy(self, level):
        document = json.loads(spec().spec_json)
        document["budget_enforcement"] = level
        with pytest.raises(ValidationError):
            RunSpec2.model_validate_json(canonical_json(document))


class TestFrozenExecution:
    async def test_issue_edited_after_go_lane_receives_frozen_text(self, env):
        run, frozen = await env.planned()
        await consume(env, run, frozen)
        env.fake.tasks.clear()
        env.intent = env.intent.model_copy(
            update={
                "task": env.intent.task.model_copy(
                    update={"title": "Edited issue", "description": "Edited body"}
                )
            }
        )
        brief, executable = await lane_input(env, run)
        assert executable == frozen.spec
        assert executable.task_title == "Change parser"
        assert executable.task_description == "Retain exact source."
        assert "Edited issue" not in brief.brief_text and brief.spec_json == frozen.spec.spec_json
        assert not any(name == "task" for name, _ in env.fake.calls)

    @pytest.mark.parametrize(
        "field,value", (("model", "tampered"), ("task_title", "tampered"), ("base_sha", "b" * 40))
    )
    @pytest.mark.parametrize("rehash", (False, True))
    async def test_tampered_spec_cannot_execute_even_with_a_forged_row_digest(
        self, env, field, value, rehash
    ):
        run, frozen = await env.planned()
        await consume(env, run, frozen)
        await rewrite_spec(
            env, run, lambda document: document.update({field: value}), rehash=rehash
        )
        with pytest.raises(LaneAuthError, match="invalid"):
            await lane_input(env, run)

    async def test_missing_spec_blocks_execution(self, env):
        run, frozen = await env.planned()
        await consume(env, run, frozen)
        async with env.factory.begin() as session:
            await session.execute(delete(EngineRunSpec).where(EngineRunSpec.run_id == run.id))
        with pytest.raises(LaneAuthError, match="approved input"):
            await lane_input(env, run)

    async def test_old_spec_is_not_executable(self, env):
        run, frozen = await env.planned()
        await consume(env, run, frozen)
        await rewrite_spec(env, run, lambda document: document.update(schema=1), rehash=True)
        with pytest.raises(LaneAuthError, match="invalid"):
            await lane_input(env, run)

    async def test_frozen_contract_budget_model_and_driver_survive_setting_drift(self, env):
        run, frozen = await env.planned()
        await consume(env, run, frozen)
        env.intent = env.intent.model_copy(
            update={
                "policy": env.intent.policy.model_copy(
                    update={
                        "driver": "new_driver",
                        "model": "new-model",
                        "limits": BudgetLimits(wallclock_seconds=1),
                        "verification": VerificationContract(required_checks=("new-required-job",)),
                    }
                )
            }
        )
        _, executable = await lane_input(env, run)
        assert executable.verification == frozen.spec.verification
        assert executable.limits == frozen.spec.limits
        assert executable.model == frozen.spec.model and executable.driver == frozen.spec.driver

    def test_an_empty_contract_cannot_be_filled_from_later_settings(self):
        with pytest.raises(ValidationError):
            VerificationContract(required_checks=())
