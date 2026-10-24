"""Exact frozen WIP may resume; material revisions and urgent controls cannot fake reuse."""

import asyncio
from hashlib import sha256
import json

import pytest
from sqlalchemy import select

from forge.domain.approved_input import ApprovedInput
from forge.domain.states import State
from forge.store.models_lane import EngineCheckpoint, EngineLaneSession
from forge.store.resume import paused_checkpoint
from forge_protocol import RunIdentity, CheckpointManifest
from forge_lane.errors import ExitCode
from tests.test_engine_commands import commands as commands, envelope
from tests.test_engine_lane_handlers import case as case, env as env, settings as settings
from tests.test_engine_operator import operator as operator, paused
from tests.test_lane_runner_v1 import FakeDriver, WireServer, checkout, execute, inputs


class TestExecutorInputDigest:
    @pytest.mark.parametrize(
        "field,value",
        (
            ("task_title", "Material revision"),
            ("model", "different"),
            ("driver", "different_driver"),
            ("base_sha", "b" * 40),
            ("revision", 2),
        ),
    )
    def test_material_revision_cannot_reuse_an_approval(self, field, value):
        from tests.test_domain_approved_input import spec

        original = spec()
        changed = json.loads(original.spec_json)
        changed[field] = value
        new = type(original).model_validate_json(json.dumps(changed))
        approved = ApprovedInput.from_spec(original)
        assert new.digest != original.digest
        assert ApprovedInput.from_spec(new).approved_digest != approved.approved_digest
        with pytest.raises(ValueError, match="does not match"):
            approved.to_brief(RunIdentity(forge_run_id="run", attempt=1, generation=1), new)

    def test_exact_input_uses_canonical_full_spec_and_six_approval_fields(self):
        from tests.test_domain_approved_input import spec

        frozen = spec()
        approved = ApprovedInput.from_spec(frozen)
        brief = approved.to_brief(RunIdentity(forge_run_id="run", attempt=1, generation=1), frozen)
        assert brief.spec_digest == sha256(brief.spec_json.encode()).hexdigest()
        assert brief.frozen_spec.execution_policy == frozen.execution_policy
        assert brief.approved_digest == approved.approved_digest
        assert brief.revision == frozen.revision


class TestWipReuseModel:
    async def test_exact_paused_checkpoint_is_the_only_resume_input(self, operator):
        env = operator["env"]
        run, frozen, pending, checkpoint, key, raw = await paused(operator)
        async with env.factory() as session:
            lane = await session.scalar(
                select(EngineLaneSession).where(EngineLaneSession.run_id == run.id)
            )
            from forge.store.models_runs import EngineRun

            current = await session.get(EngineRun, run.id)
            named, dispatch = await paused_checkpoint(session, current, lane)
        assert named == checkpoint and dispatch == pending.dispatch
        assert named.approved_digest == frozen.approved.approved_digest
        assert named.entries[0].sha256 == sha256(raw).hexdigest() == key
        assert named.entries[0].size_bytes == len(raw)

    @pytest.mark.parametrize(
        "field,value",
        (
            ("forge_run_id", "another-run"),
            ("attempt", 99),
            ("generation", 99),
            ("base_sha", "b" * 40),
            ("approved_digest", "c" * 64),
            ("driver", "another_driver"),
        ),
    )
    async def test_material_or_foreign_checkpoint_is_refused_without_fresh_restart(
        self, operator, field, value
    ):
        env = operator["env"]
        run, _, _, checkpoint, _, _ = await paused(operator)
        async with env.factory.begin() as session:
            row = await session.get(EngineCheckpoint, checkpoint.checkpoint_id)
            document = json.loads(row.manifest_json)
            document[field] = value
            row.manifest_json = json.dumps(document)
        before = await env.runs.load(run.id)
        launches = len(env.fake.lanes)
        result = await operator["commands"].submit(envelope(operator, "resume"))
        assert result.status == "refused" and result.reason == "checkpoint_unavailable"
        after = await env.runs.load(run.id)
        assert (after.attempt, after.generation, after.state) == (
            before.attempt,
            before.generation,
            State.BLOCKED,
        )
        assert len(env.fake.lanes) == launches

    async def test_resume_replay_keeps_exact_pin_and_never_grants_new_budget(self, operator):
        env = operator["env"]
        run, frozen, _, checkpoint, _, _ = await paused(operator)
        pool = await operator["budgets"].load(run.id, "implementation")
        command = envelope(operator, "resume")
        first = await operator["commands"].submit(command)
        assert first.reason == "resume_scheduled"
        assert await operator["commands"].submit(command) == first
        assert (await operator["budgets"].load(run.id, "implementation")) == pool
        assert (await env.approvals.load(run.id)).approved == frozen.approved
        assert (await env.runs.load(run.id)).generation == 2
        assert checkpoint.generation == 1

    async def test_unretained_checkpoint_cannot_be_inferred_from_latest(self, operator):
        env = operator["env"]
        run, _, pending, checkpoint, _, _ = await paused(operator)
        async with env.factory.begin() as session:
            row = await session.get(EngineCheckpoint, checkpoint.checkpoint_id)
            row.retained = False
        result = await operator["commands"].submit(envelope(operator, "resume"))
        assert result.reason == "checkpoint_unavailable"
        assert (await env.runs.load(run.id)).generation == pending.identity.generation


class TestUrgentPauseVisibilityStaysDistinct:
    async def test_an_urgent_pause_suspends_a_running_turn_within_the_cycle(self, tmp_path):
        workspace = tmp_path / "work"
        dispatch, brief = inputs(checkout(workspace))
        server, driver = WireServer(dispatch, brief), FakeDriver(wait=True)
        steering = asyncio.Event()

        async def blocked_steer(text):
            steering.set()
            await asyncio.Future()

        driver.session.steer = blocked_steer
        server.command("steer", 1, "Keep the public API")
        execution = asyncio.create_task(execute(dispatch, server, driver, workspace))
        try:
            async with asyncio.timeout(1):
                await steering.wait()
                server.command("pause", 2)
                result = await execution
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
        assert result.exit_code == ExitCode.PAUSED
        assert driver.interrupted == 1 and not server.candidates
        assert [(ack["sequence"], ack["status"]) for ack in server.acks] == [
            (1, "refused"),
            (2, "applied"),
        ]
        assert server.acks[0]["detail"] == "Steering interrupted by operator control"
        assert (
            result.report.checkpoint_id
            == CheckpointManifest.model_validate(server.checkpoints[-1]).checkpoint_id
        )

    async def test_a_late_urgent_is_a_visible_no_op_never_a_rewrite(self, tmp_path):
        workspace = tmp_path / "work"
        dispatch, brief = inputs(checkout(workspace))
        server = WireServer(dispatch, brief)
        driver = FakeDriver(lambda path, env: (path / "base.txt").write_text("completed\n"))
        result = await execute(dispatch, server, driver, workspace)
        saved = result.report.model_dump()
        server.command("pause")
        await asyncio.sleep(0.01)
        assert result.exit_code == ExitCode.COMPLETED and result.report.model_dump() == saved
        assert not server.acks and not server.checkpoints

    async def test_failed_steering_is_visible_failure_without_publication(self, tmp_path):
        workspace = tmp_path / "work"
        dispatch, brief = inputs(checkout(workspace))
        server, driver = WireServer(dispatch, brief), FakeDriver(wait=True)

        async def failed(text):
            raise RuntimeError("synthetic control failure")

        driver.session.steer = failed
        server.command("steer", 1, "Keep the public API")
        result = await execute(dispatch, server, driver, workspace)
        assert result.report.status == "failed" and result.classification == "control_failed"
        assert driver.interrupted == 1 and not server.candidates
