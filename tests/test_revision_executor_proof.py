"""The R36-13 (#272) unit proofs — the reuse-decision model + the
executor-input digest, and their durable wiring.

The production-entry trace (`tests/production_entry/
test_revision_executor_proof.py`, AT-11) proves the journey at the real
service level; this file pins the RULES themselves:

- :class:`TestWipReuseModel` — the pure partition of work artifacts
  across a material revision (preserve / invalidate / discard by
  artifact KIND and applicability digest): a compatible checkpoint is
  preserved (a resume after the revision stands), a revision that
  touches write-carrying work invalidates or discards it, a
  verification artifact is only valid under the exact plan it verified
  and never auto-returns to passed, and an UNDECIDABLE checkpoint fails
  closed to the explicit fresh attempt — never a silent reuse;
- :class:`TestExecutorInputDigest` — the canonical identity digest the
  dispatch records and the native ledger re-computes;
- :class:`TestActivationPersistsReuseDecision` — the activation
  transaction persists ``checkpoint.reuse_decision`` (evidence + outbox)
  in the SAME commit as the switch, computed from the superseded content
  staged beside the proposal;
- :class:`TestDispatchFenceAgainstFreshAttemptRoute` — the
  ``refused_wip_reuse`` seam the dispatch boundary consults: a
  ``fresh_attempt`` route refuses a ``required`` resume, and nothing
  else;
- :class:`TestUrgentPauseVisibilityStaysDistinct` — the LaneSupervisor
  seam's existing contract (a steer-in-flight urgent pause suspends
  within the cycle; a late urgent is a recorded no-op) and the
  projection-row honesty: the ``control_degraded`` row projects as its
  OWN category (evidence), never as an effect or a resume claim — no
  premature "resumed-success".
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.models import PlanRevision, PlanStep
from forge.adaptive.operator_timeline import timeline_from_journal
from forge.adaptive.revisions import (
    CHECKPOINT_KIND,
    CHECKPOINT_REUSE_DECISION_KEY,
    DISCARD_DECISION,
    EVIDENCE_KIND,
    EXECUTOR_INPUT_FIELDS,
    INVALIDATE_DECISION,
    PRESERVE_DECISION,
    PENDING_PROPOSAL_KEY,
    REVISION_ACTIVATIONS_KEY,
    VERIFICATION_KIND,
    WorkArtifact,
    activate_pending_revision,
    decide_wip_reuse,
    executor_digest_document,
    executor_input_digest,
    plan_digest,
    proposed_revision_identity,
    refused_wip_reuse,
    stage_pending_revision,
    wip_artifacts_of_evidence,
)
from forge.adaptive.revisions import (
    ActivePlanState,
    RevisionDecision,
)
from forge.models.base import Base

D_CONTRACT = "3" * 64  # a lowercase-hex digest the contract validator accepts
D_SNAPSHOT = "5" * 64


def _step(
    step_id: str,
    objective: str,
    *,
    writes: str | None = None,
    impact: tuple[str, ...] = ("internal",),
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        objective=objective,
        write_repository_id=writes,
        impact=list(impact),
        acceptance_refs=["AC-1"],
    )


def _base_steps() -> list[PlanStep]:
    return [
        _step("S1", "Inspect existing behavior."),
        _step("S2", "Implement the authorized change.", writes="acme/forge"),
        _step("S3", "Check the result.", impact=("tests",)),
    ]


def _revision(
    steps: list[PlanStep],
    *,
    revision: int,
    parent: int | None,
    summary: str = "Land the widget.",
    invalidated: tuple[str, ...] = (),
) -> PlanRevision:
    return PlanRevision(
        plan_id="plan-272",
        work_id="wp-272",
        revision=revision,
        parent_revision=parent,
        work_contract_digest=D_CONTRACT,
        snapshot_set_digest=D_SNAPSHOT,
        summary=summary,
        steps=steps,
        invalidated_step_ids=list(invalidated),
    )


def _current() -> ActivePlanState:
    return ActivePlanState(
        work_id="wp-272",
        plan_id="plan-272",
        active_revision=1,
        work_contract_digest=D_CONTRACT,
        authorization_epoch=3,
        publication_epoch=1,
    )


def _decision(proposed: PlanRevision) -> RevisionDecision:
    return RevisionDecision(
        decision_id="rd-272",
        work_id="wp-272",
        parent_revision=1,
        proposed_revision_id=proposed_revision_identity(proposed),
        proposed_digest=plan_digest(proposed),
        work_contract_digest=D_CONTRACT,
        authorization_epoch=3,
    )


# ---------------------------------------------------------------------------
# The pure reuse-decision model
# ---------------------------------------------------------------------------


class TestWipReuseModel:
    def test_a_compatible_revision_preserves_the_checkpoint(self):
        """A revision that changes no write-carrying step keeps the WIP: the
        checkpoint's restored bytes still stand under the new plan."""
        old = _revision(_base_steps(), revision=1, parent=None)
        new = _revision(
            _base_steps(), revision=2, parent=1, summary="Reworded: land the widget fast."
        )
        assert plan_digest(new) != plan_digest(old)  # a genuine revision
        decision = decide_wip_reuse(
            old,
            new,
            [WorkArtifact("ckpt-1", CHECKPOINT_KIND, applicability_digest=plan_digest(old))],
        )
        assert decision.route == "preserve"
        assert decision.preserved == ("ckpt-1",)
        assert decision.invalidated == ()
        assert decision.discarded == ()

    def test_a_revision_touching_write_work_invalidates_the_checkpoint(self):
        old = _revision(_base_steps(), revision=1, parent=None)
        changed = [
            _step("S1", "Inspect existing behavior."),
            _step("S2", "Implement the authorized change differently.", writes="acme/forge"),
            _step("S3", "Check the result.", impact=("tests",)),
        ]
        new = _revision(changed, revision=2, parent=1)
        decision = decide_wip_reuse(
            old,
            new,
            [WorkArtifact("ckpt-1", CHECKPOINT_KIND, applicability_digest=plan_digest(old))],
        )
        assert decision.route == "fresh_attempt"
        assert decision.invalidated == ("ckpt-1",)
        assert "S2" in decision.route_reason

    def test_a_declared_invalidation_routes_the_checkpoint_to_a_fresh_attempt(self):
        old = _revision(_base_steps(), revision=1, parent=None)
        new = _revision(_base_steps(), revision=2, parent=1, invalidated=("S2",))
        decision = decide_wip_reuse(
            old,
            new,
            [WorkArtifact("ckpt-1", CHECKPOINT_KIND, applicability_digest=plan_digest(old))],
        )
        assert decision.route == "fresh_attempt"
        assert decision.invalidated == ("ckpt-1",)

    def test_a_revision_removing_all_write_work_discards_the_checkpoint(self):
        old = _revision(_base_steps(), revision=1, parent=None)
        different = [
            _step("S1", "Inspect existing behavior."),
            _step("S9", "Pursue the documentation instead.", impact=("docs",)),
        ]
        new = _revision(different, revision=2, parent=1)
        decision = decide_wip_reuse(
            old,
            new,
            [WorkArtifact("ckpt-1", CHECKPOINT_KIND, applicability_digest=plan_digest(old))],
        )
        assert decision.route == "fresh_attempt"
        assert decision.discarded == ("ckpt-1",)
        assert decision.invalidated == ()

    def test_without_the_superseded_content_the_checkpoint_fails_closed(self):
        """The UNDECIDABLE case: no staged old content, no declarations that
        could prove compatibility — the checkpoint is invalidated (fail
        closed), never silently preserved."""
        new = _revision(_base_steps(), revision=2, parent=1, summary="Reworded.")
        decision = decide_wip_reuse(
            None,
            new,
            [WorkArtifact("ckpt-1", CHECKPOINT_KIND, applicability_digest="d" * 64)],
        )
        assert decision.route == "fresh_attempt"
        assert decision.invalidated == ("ckpt-1",)
        assert "UNDECIDABLE" in decision.route_reason

    def test_verification_is_only_valid_under_the_exact_plan_it_verified(self):
        old = _revision(_base_steps(), revision=1, parent=None)
        compatible = _revision(_base_steps(), revision=2, parent=1, summary="Reworded only.")
        artifacts = [
            WorkArtifact("verif-1", VERIFICATION_KIND, applicability_digest=plan_digest(old)),
            WorkArtifact(
                "verif-2", VERIFICATION_KIND, applicability_digest=plan_digest(compatible)
            ),
        ]
        decision = decide_wip_reuse(old, compatible, artifacts)
        assert decision.invalidated == ("verif-1",)
        assert decision.preserved == ("verif-2",)
        assert "never" in decision.document()["artifacts"][0]["reason"]  # no auto-return
        # And even a COMPATIBLE revision that only rewords still invalidates
        # the old verification — the applicability digest is the axis.
        assert decision.invalidated == ("verif-1",)

    def test_step_scoped_evidence_discards_invalidate_and_preserves(self):
        old = _revision(_base_steps(), revision=1, parent=None)
        steps = [
            _step("S1", "Inspect existing behavior."),
            _step("S2", "Implement the authorized change.", writes="acme/forge"),
            _step("S3", "Check the result twice.", impact=("tests",)),  # changed
        ]
        new = _revision(steps, revision=2, parent=1)
        decision = decide_wip_reuse(
            old,
            new,
            [
                WorkArtifact("ev-S1", EVIDENCE_KIND, step_id="S1"),
                WorkArtifact("ev-S2", EVIDENCE_KIND, step_id="S2"),
                WorkArtifact("ev-S3", EVIDENCE_KIND, step_id="S3"),
            ],
        )
        by_id = {entry.artifact_id: entry for entry in decision.decisions}
        assert by_id["ev-S1"].decision == PRESERVE_DECISION
        assert by_id["ev-S2"].decision == PRESERVE_DECISION  # carried over byte-for-byte
        assert by_id["ev-S3"].decision == INVALIDATE_DECISION
        # No checkpoint artifacts: the route has nothing to refuse.
        assert decision.route == "preserve"

    def test_a_removed_step_discards_its_artifacts(self):
        old = _revision(_base_steps(), revision=1, parent=None)
        steps = [
            _step("S1", "Inspect existing behavior."),
            _step("S2", "Implement the authorized change.", writes="acme/forge"),
        ]
        new = _revision(steps, revision=2, parent=1)
        decision = decide_wip_reuse(old, new, [WorkArtifact("ev-S3", EVIDENCE_KIND, step_id="S3")])
        assert decision.discarded == ("ev-S3",)
        entry = decision.document()["artifacts"][0]
        assert entry["decision"] == DISCARD_DECISION
        assert "no longer pursued" in entry["reason"]

    def test_no_checkpoint_on_record_routes_preserve(self):
        new = _revision(_base_steps(), revision=2, parent=1)
        decision = decide_wip_reuse(None, new, [])
        assert decision.route == "preserve"
        assert "no workspace checkpoint" in decision.route_reason

    def test_the_document_is_the_durable_audit_shape(self):
        old = _revision(_base_steps(), revision=1, parent=None)
        new = _revision(_base_steps(), revision=2, parent=1, summary="Reworded.")
        decision = decide_wip_reuse(
            old,
            new,
            [
                WorkArtifact("ckpt-1", CHECKPOINT_KIND, applicability_digest=plan_digest(old)),
                WorkArtifact("verif-1", VERIFICATION_KIND, applicability_digest=plan_digest(old)),
            ],
        )
        document = decision.document()
        assert document["schema"] == "forge.checkpoint.reuse-decision/1"
        assert document["activated_revision"] == 2
        assert document["plan_digest"] == plan_digest(new)
        assert document["route"] == "preserve"
        assert {entry["kind"] for entry in document["artifacts"]} == {
            CHECKPOINT_KIND,
            VERIFICATION_KIND,
        }
        assert all(entry["decision"] and entry["reason"] for entry in document["artifacts"])
        assert json.dumps(document)  # JSON-safe (it lands in a JSON column)


class TestWipArtifactsOfEvidence:
    def test_the_continuation_checkpoint_and_verification_become_artifacts(self):
        old_digest = "a" * 64
        artifacts = wip_artifacts_of_evidence(
            {
                "continuation": {"checkpoint_digest": "ck" * 32, "mode_selected": "required"},
                "verification": {"status": "passed", "tested_oid": "1" * 40},
            },
            superseded_plan_digest=old_digest,
        )
        by_kind = {artifact.kind: artifact for artifact in artifacts}
        assert by_kind[CHECKPOINT_KIND].artifact_id == "ck" * 32
        assert by_kind[CHECKPOINT_KIND].applicability_digest == old_digest
        assert by_kind[VERIFICATION_KIND].artifact_id == "1" * 40
        assert by_kind[VERIFICATION_KIND].applicability_digest == old_digest

    def test_a_run_without_them_contributes_nothing(self):
        assert wip_artifacts_of_evidence({}, superseded_plan_digest="a" * 64) == []


# ---------------------------------------------------------------------------
# The executor-input digest
# ---------------------------------------------------------------------------


class TestExecutorInputDigest:
    def test_the_digest_is_canonical_and_key_order_independent(self):
        forward = executor_input_digest(
            {
                "run_id": "r" * 32,
                "plan_digest": "p" * 64,
                "envelope_digest": "e" * 64,
                "spec_digest": "s" * 64,
                "lane_resume_mode": "required",
            }
        )
        shuffled = executor_input_digest(
            {
                "lane_resume_mode": "required",
                "spec_digest": "s" * 64,
                "envelope_digest": "e" * 64,
                "plan_digest": "p" * 64,
                "run_id": "r" * 32,
            }
        )
        assert forward == shuffled

    def test_the_digest_covers_exactly_the_identity_fields(self):
        identity = {
            "run_id": "r" * 32,
            "plan_digest": "p" * 64,
            "envelope_digest": "",
            "spec_digest": "s" * 64,
            "lane_resume_mode": "fresh",
        }
        assert set(EXECUTOR_INPUT_FIELDS) == set(identity)
        # An extra field a caller passes is IGNORED (the identity is closed).
        padded = dict(identity, extra="noise")
        assert executor_input_digest(padded) == executor_input_digest(identity)

    def test_the_digest_moves_with_the_plan_and_the_resume_mode(self):
        base = {
            "run_id": "r" * 32,
            "plan_digest": "p" * 64,
            "envelope_digest": "",
            "spec_digest": "s" * 64,
            "lane_resume_mode": "fresh",
        }
        assert executor_input_digest(dict(base, plan_digest="q" * 64)) != (
            executor_input_digest(base)
        )
        assert executor_input_digest(dict(base, lane_resume_mode="required")) != (
            executor_input_digest(base)
        )

    def test_the_document_freezes_the_active_identity(self):
        document = executor_digest_document(
            run_id="r" * 32,
            plan_digest="p" * 64,
            active_revision=2,
            envelope_digest="e" * 64,
            spec_digest="s" * 64,
            lane_resume_mode="required",
            revised_from_digest="o" * 64,
        )
        assert document["schema"] == "forge.revision.executor-digest/1"
        assert document["plan_digest"] == "p" * 64
        assert document["active_revision"] == 2
        assert document["revised_from_digest"] == "o" * 64
        assert document["executor_input_digest"] == executor_input_digest(document)
        # The digest is recomputable from the recorded identity alone.
        recomputed = executor_input_digest({name: document[name] for name in EXECUTOR_INPUT_FIELDS})
        assert recomputed == document["executor_input_digest"]


# ---------------------------------------------------------------------------
# The durable wiring: the activation persists the reuse decision
# ---------------------------------------------------------------------------


class _World:
    """A sqlite-backed FlowRun evidence store (the activation transaction)."""

    def __init__(self, evidence: dict | None = None) -> None:
        self.run_id = "2" * 32
        self._seed = evidence or {}
        self.factory = None

    async def start(self, *, store_dir: str, monkeypatch) -> None:
        # Hermetic env (monkeypatch, never a bare os.environ write that
        # would leak into later tests): the activation's checkpoint-authority
        # read resolves the SAME configured store the traces use.
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", store_dir)
        monkeypatch.delenv("FORGE_CHECKPOINT_DURABILITY", raising=False)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        from forge.durable import FlowRun

        async with self.factory() as session:
            session.add(FlowRun(id=self.run_id, project_id=1, status="planning"))
            await session.commit()
        await self.set_evidence(self._seed)

    async def set_evidence(self, evidence: dict) -> None:
        from forge.adaptive.revisions import ACTIVE_PLAN_KEY
        from forge.durable import FlowRun

        async with self.factory() as session:
            run = await session.get(FlowRun, self.run_id)
            merged = dict(run.evidence or {})
            merged[ACTIVE_PLAN_KEY] = {
                "work_id": "wp-272",
                "plan_id": "plan-272",
                "active_revision": 1,
                "plan_digest": plan_digest(_revision(_base_steps(), revision=1, parent=None)),
                "revised_from_digest": "",
                "work_contract_digest": D_CONTRACT,
                "authorization_epoch": 3,
                "publication_epoch": 1,
            }
            merged.update(evidence)
            run.evidence = merged
            await session.commit()

    async def evidence(self) -> dict:
        from forge.durable import FlowRun

        async with self.factory() as session:
            run = await session.get(FlowRun, self.run_id)
            return dict(run.evidence or {})

    async def outbox_events(self) -> list[tuple[str, dict]]:
        from forge.durable import Outbox

        async with self.factory() as session:
            rows = (await session.execute(select(Outbox))).scalars().all()
        return [(row.event_type, dict(row.payload)) for row in rows]


@pytest.fixture()
async def world(tmp_path, monkeypatch):
    instance = _World()
    await instance.start(store_dir=str(tmp_path / "checkpoints"), monkeypatch=monkeypatch)
    return instance


class TestActivationPersistsReuseDecision:
    async def _activate(self, world: _World, new: PlanRevision, *, old: PlanRevision | None):
        await stage_pending_revision(
            world.factory, world.run_id, _decision(new), new, _current(), old=old
        )
        return await activate_pending_revision(
            world.factory, world.run_id, _decision(new).decision_id, decided_by="alice"
        )

    async def test_a_compatible_activation_preserves_the_pinned_checkpoint(self, world):
        old = _revision(_base_steps(), revision=1, parent=None)
        new = _revision(_base_steps(), revision=2, parent=1, summary="Reworded.")
        await world.set_evidence(
            {"continuation": {"checkpoint_digest": "ck" * 32, "mode_selected": "required"}}
        )
        outcome = await self._activate(world, new, old=old)
        assert outcome.status == "activated"
        evidence = await world.evidence()
        document = evidence[CHECKPOINT_REUSE_DECISION_KEY]
        assert document["route"] == "preserve"
        assert document["activated_revision"] == 2
        preserved = [entry for entry in document["artifacts"] if entry["kind"] == CHECKPOINT_KIND]
        assert [entry["artifact_id"] for entry in preserved] == ["ck" * 32]
        assert preserved[0]["decision"] == PRESERVE_DECISION
        # The outbox row lands in the SAME transaction (one commit).
        assert any(
            kind == "checkpoint.reuse_decision" and payload["route"] == "preserve"
            for kind, payload in await world.outbox_events()
        )
        # The switch and the decision committed together.
        assert evidence[REVISION_ACTIVATIONS_KEY]
        assert PENDING_PROPOSAL_KEY not in evidence

    async def test_an_incompatible_activation_records_the_fresh_attempt(self, world):
        old = _revision(_base_steps(), revision=1, parent=None)
        changed = [
            _step("S1", "Inspect existing behavior."),
            _step("S2", "Implement it the other way.", writes="acme/forge"),
            _step("S3", "Check the result.", impact=("tests",)),
        ]
        new = _revision(changed, revision=2, parent=1)
        await world.set_evidence(
            {"continuation": {"checkpoint_digest": "ck" * 32, "mode_selected": "required"}}
        )
        outcome = await self._activate(world, new, old=old)
        assert outcome.status == "activated"
        document = (await world.evidence())[CHECKPOINT_REUSE_DECISION_KEY]
        assert document["route"] == "fresh_attempt"
        assert "S2" in document["route_reason"]
        checkpoint_entry = next(
            entry for entry in document["artifacts"] if entry["kind"] == CHECKPOINT_KIND
        )
        assert checkpoint_entry["decision"] == INVALIDATE_DECISION

    async def test_a_redelivery_does_not_rewrite_the_decision(self, world):
        old = _revision(_base_steps(), revision=1, parent=None)
        new = _revision(_base_steps(), revision=2, parent=1, summary="Reworded.")
        assert (await self._activate(world, new, old=old)).status == "activated"
        decision_id = _decision(new).decision_id
        second = await activate_pending_revision(
            world.factory, world.run_id, decision_id, decided_by="bob"
        )
        assert second.status == "already_active"
        reuse_events = [
            payload
            for kind, payload in await world.outbox_events()
            if kind == "checkpoint.reuse_decision"
        ]
        assert len(reuse_events) == 1  # one decision, however many deliveries


class TestDispatchFenceAgainstFreshAttemptRoute:
    async def test_a_fresh_attempt_route_refuses_only_a_required_resume(self, world):
        old = _revision(_base_steps(), revision=1, parent=None)
        changed = [
            _step("S1", "Inspect existing behavior."),
            _step("S2", "Implement it the other way.", writes="acme/forge"),
            _step("S3", "Check the result.", impact=("tests",)),
        ]
        new = _revision(changed, revision=2, parent=1)
        await world.set_evidence(
            {"continuation": {"checkpoint_digest": "ck" * 32, "mode_selected": "required"}}
        )
        await stage_pending_revision(
            world.factory, world.run_id, _decision(new), new, _current(), old=old
        )
        assert (
            await activate_pending_revision(
                world.factory, world.run_id, _decision(new).decision_id, decided_by="alice"
            )
        ).status == "activated"

        refused = await refused_wip_reuse(world.factory, world.run_id, resume_mode="required")
        assert refused is not None
        assert refused["route"] == "fresh_attempt"
        # A dispatch that restores nothing, and the operator's explicit
        # discard, both pass — the fence only guards the silent reuse.
        assert await refused_wip_reuse(world.factory, world.run_id, resume_mode="fresh") is None
        assert await refused_wip_reuse(world.factory, world.run_id, resume_mode="restart") is None

    async def test_no_recorded_decision_never_refuses(self, world):
        assert await refused_wip_reuse(world.factory, world.run_id, resume_mode="required") is None

    async def test_a_preserve_route_never_refuses(self, world):
        old = _revision(_base_steps(), revision=1, parent=None)
        new = _revision(_base_steps(), revision=2, parent=1, summary="Reworded.")
        await world.set_evidence(
            {"continuation": {"checkpoint_digest": "ck" * 32, "mode_selected": "required"}}
        )
        await stage_pending_revision(
            world.factory, world.run_id, _decision(new), new, _current(), old=old
        )
        await activate_pending_revision(
            world.factory, world.run_id, _decision(new).decision_id, decided_by="alice"
        )
        assert await refused_wip_reuse(world.factory, world.run_id, resume_mode="required") is None


# ---------------------------------------------------------------------------
# The urgent-pause seam + its projection-row visibility
# ---------------------------------------------------------------------------


class TestUrgentPauseVisibilityStaysDistinct:
    async def test_an_urgent_pause_suspends_a_running_turn_within_the_cycle(self):
        """The LaneSupervisor's existing contract (the seam the design rides):
        an urgent control — the pause that arrives while a steer is in
        flight — suspends the turn DURING the cycle, and the bounded
        teardown never lets a hung plane hold it."""
        from forge.adaptive.lane_supervisor import LaneSupervisor, TerminalEvent

        async def slow_turn() -> str:
            await asyncio.sleep(30.0)  # a slow vendor call
            return "done"

        supervisor: LaneSupervisor[TerminalEvent] = LaneSupervisor(
            classify=lambda event: event, name="proof-lane", drain_cancel_wait_s=0.05
        )
        supervisor.submit_turn(slow_turn())
        await asyncio.sleep(0)  # let the turn task start

        async def steering_drain() -> None:
            # A steer is in flight; the urgent pause arrives mid-turn.
            await asyncio.sleep(0.01)
            assert supervisor.request_urgent("pause", "operator pause") is True

        supervisor.submit_drain(steering_drain())
        event = await asyncio.wait_for(supervisor.run(), timeout=5.0)
        assert event.kind == "turn_suspended"
        assert event.urgent is not None and event.urgent.kind == "pause"

    async def test_a_late_urgent_is_a_visible_no_op_never_a_rewrite(self):
        from forge.adaptive.lane_supervisor import LaneSupervisor, TerminalEvent

        supervisor: LaneSupervisor[TerminalEvent] = LaneSupervisor(
            classify=lambda event: event, name="proof-lane"
        )
        supervisor.submit_turn(_quick_turn())
        event = await asyncio.wait_for(supervisor.run(), timeout=5.0)
        assert event.kind == "turn_completed"
        # The urgent arrives AFTER the verdict: recorded, never re-classified.
        assert supervisor.request_urgent("pause", "late") is False
        assert any("never re-classified" in note for note in supervisor.notes)

    async def test_control_degraded_projects_as_its_own_distinct_category(self):
        """R32-09/R36-13: a drain death while the turn ran degrades control —
        and the PROJECTION keeps that degradation an EVIDENCE row, never an
        effect or a resume claim: no premature "resumed-success"."""
        from forge.adaptive.lane_supervisor import LaneSupervisor, TerminalEvent

        supervisor: LaneSupervisor[TerminalEvent] = LaneSupervisor(
            classify=lambda event: event, name="proof-lane", strict_control=False
        )
        release = asyncio.Event()

        async def turn() -> str:
            release.set()
            await asyncio.sleep(0.05)
            return "completed anyway"

        async def dying_drain() -> None:
            await release.wait()
            raise RuntimeError("control plane exploded mid-steer")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(dying_drain())
        event = await asyncio.wait_for(supervisor.run(), timeout=5.0)
        assert event.kind == "turn_completed"
        assert supervisor.control_degraded  # observed live, rode the event

        # The lane composes the degradation into its journal as a
        # control-error row (the lane-control sidecar's shape); the
        # honest projection keeps it DISTINCT from every effect claim.
        rows = [
            {
                "type": "lane_control_error",
                "error": event.control_degraded,
                "at": "2026-09-23T00:00:01Z",
            },
            {"kind": "pause", "command_id": "cmd-1", "outcome": "applied", "at": "t2"},
            {"kind": "resume", "command_id": "cmd-2", "status": "received", "at": "t3"},
        ]
        entries = timeline_from_journal(rows)
        categories = [entry.category for entry in entries]
        assert categories == ["evidence_recorded", "effect_observed", "request_received"]
        # The degradation row is NEVER dressed as an effect — and the resume
        # that no runner drained stays a REQUEST, not a success.
        assert categories[0] != "effect_observed"
        assert categories[2] == "request_received"


async def _quick_turn() -> str:
    return "quick"
