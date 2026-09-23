"""Q35-02: the continuation decision — retry continues from a recoverable
state, not from the retry verb.

Two layers, tested separately on purpose:

- the DECISION MODEL (:mod:`forge.adaptive.continuation`) — pure over an
  evidence snapshot; every arm of the table, the reuse digest and the
  operator-facing wording;
- the SERVICE WIRING (:class:`forge.runs.github_service.GitHubRunService`)
  — ``/retry`` and the revival re-dispatch select ``lane_resume_mode`` from
  the decision, persist it on the run's evidence, reuse it on repeated
  events, dispatch NOTHING when the recoverable state is unknown, and keep
  the strict required-restore contract for genuinely checkpoint-bound
  continuations.
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive import continuation
from forge.adaptive.continuation import (
    ContinuationDecision,
    ContinuationEvidence,
    ContinuationMode,
    decide_continuation,
    evidence_from_record,
    matching_decision,
    operator_discard_requested,
    retry_ack_line,
)
from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, FlowStatus
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.runs.github_service import GitHubRunService
from forge.runs.stubs import StubImplementer, StubPlanner
from tests.fixtures.fake_github import FakeGitHub

FIXTURES_REPO = "acme/acme-widget"
PROJECT_ID = 70010
ISSUE = 42
ISSUE_TITLE = "Add password reset"
ISSUE_DESC = "Users cannot reset their password."
BASE_HEAD = "1" * 40
HARNESS_WORKFLOW = "forge-harness.github.yml"
HARNESS_MODEL = "glm-5.3-flash[1m]"

#: Terminal reasons the four timeout/death stages produce (the shapes the
#: service actually records — see forge.runs.execution/backends).
DEATH_BEFORE_BOOTSTRAP = (
    "harness_infrastructure: harness_bootstrap_failed (driver exit=setup_failed)"
)
DEATH_AFTER_VENDOR = "harness_code: harness_driver_failed (exit=error)"
DEATH_PLAIN_TIMEOUT = "harness_infrastructure: harness_timeout"
DEATH_AFTER_PUBLICATION = "commit_unknown_outcome"


# ----------------------------------------------------------------------
# The decision table (pure — every arm)
# ----------------------------------------------------------------------


class TestDecisionTable:
    def evidence(self, **overrides) -> ContinuationEvidence:
        return ContinuationEvidence(**overrides)

    def test_operator_discard_wins_over_everything(self):
        """Arm 1: the explicit discard beats even a committed checkpoint —
        the operator said the WIP is gone; preservation is never promised."""
        decision = decide_continuation(
            self.evidence(
                operator_discard_requested=True,
                checkpoint_committed=True,
                vendor_started=True,
            )
        )
        assert decision.mode is ContinuationMode.EXPLICIT_RESTART
        assert decision.resume_mode() == "restart"
        assert decision.mode_selected == "restart"
        assert "explicitly discarded" in decision.reason

    def test_committed_checkpoint_is_the_exact_wip_resume(self):
        """Arm 2: a committed checkpoint IS the authorized continuation —
        the dispatch carries the strict ``required`` contract."""
        decision = decide_continuation(self.evidence(checkpoint_committed=True))
        assert decision.mode is ContinuationMode.EXACT_WIP
        assert decision.resume_mode() == "required"
        assert "authorized continuation" in decision.reason

    def test_proven_no_vendor_session_uses_the_committed_baseline(self):
        """Arm 3: a PROVEN no-vendor death has no WIP to lose — the frozen
        committed base is the continuation source (``fresh``)."""
        decision = decide_continuation(
            self.evidence(vendor_started=False, checkpoint_committed=False)
        )
        assert decision.mode is ContinuationMode.COMMITTED_BASELINE
        assert decision.resume_mode() == "fresh"
        assert "no vendor session ever started" in decision.reason

    def test_proven_no_vendor_with_unknown_checkpoint_still_uses_the_baseline(self):
        """Arm 3 with an UNKNOWN (not False) checkpoint lookup: the vendor
        proof alone settles it — no checkpoint was ever uploadable."""
        decision = decide_continuation(self.evidence(vendor_started=False))
        assert decision.mode is ContinuationMode.COMMITTED_BASELINE
        assert decision.resume_mode() == "fresh"

    def test_unknown_vendor_state_is_uncertain(self):
        """Arm 4a: no proof either way → UNCERTAIN. An uncertain decision
        selects NO resume mode — the caller must not dispatch."""
        decision = decide_continuation(self.evidence(vendor_started=None))
        assert decision.mode is ContinuationMode.UNCERTAIN
        assert decision.uncertain is True
        assert decision.dispatchable is False
        with pytest.raises(ValueError, match="selects no resume mode"):
            decision.resume_mode()
        assert "never proves there was no WIP" in decision.reason

    def test_vendor_started_without_checkpoint_is_uncertain(self):
        """Arm 4b: the vendor provably ran and no checkpoint is held → the
        absence of the checkpoint is NOT proof there was no WIP."""
        decision = decide_continuation(
            self.evidence(vendor_started=True, checkpoint_committed=False)
        )
        assert decision.mode is ContinuationMode.UNCERTAIN
        assert "a vendor session had started" in decision.reason

    def test_delivered_work_is_terminal_guidance_not_a_resume(self):
        """Arm 5: work already delivered → the decision stays UNCERTAIN (no
        dispatch) but the reason is terminal guidance pointing at the
        candidate, distinct from the choose-restart-or-reconcile wording."""
        decision = decide_continuation(self.evidence(vendor_started=True, candidate_published=True))
        guidance = decide_continuation(self.evidence(vendor_started=True))
        assert decision.mode is ContinuationMode.UNCERTAIN
        assert "already delivered" in decision.reason
        assert decision.reason != guidance.reason  # four distinct outcomes

    def test_the_four_outcomes_are_distinct(self):
        """The four timeout-stage shapes land in four DISTINCT modes."""
        stages = {
            "before bootstrap": self.evidence(vendor_started=False),
            "after vendor acceptance": self.evidence(vendor_started=True),
            "after checkpoint upload": self.evidence(checkpoint_committed=True),
            "after candidate publication": self.evidence(
                vendor_started=True, candidate_published=True
            ),
        }
        decisions = {stage: decide_continuation(ev) for stage, ev in stages.items()}
        outcomes = {
            (d.mode, d.reason) for d in decisions.values()
        }  # mode+reason — the guidance variant differs in reason
        assert len(outcomes) == 4
        assert decisions["before bootstrap"].resume_mode() == "fresh"
        assert decisions["after checkpoint upload"].resume_mode() == "required"
        assert decisions["after vendor acceptance"].uncertain
        assert decisions["after candidate publication"].uncertain


class TestEvidenceFromRecord:
    """Vendor/delivery evidence comes ONLY from recorded classifications."""

    def test_bootstrap_failure_classification_proves_no_vendor(self):
        snap = evidence_from_record(death_reason=DEATH_BEFORE_BOOTSTRAP)
        assert snap.vendor_started is False

    def test_dispatch_never_observed_proves_no_vendor(self):
        snap = evidence_from_record(
            death_reason=(
                "harness_infrastructure: dispatch never observed — discovery found no "
                "workflow_dispatch run after 20 attempts"
            )
        )
        assert snap.vendor_started is False

    def test_driver_failure_proves_the_vendor_ran(self):
        snap = evidence_from_record(death_reason=DEATH_AFTER_VENDOR)
        assert snap.vendor_started is True

    def test_empty_driver_completion_proves_the_vendor_ran(self):
        snap = evidence_from_record(death_reason="harness_code: harness_no_changes")
        assert snap.vendor_started is True

    def test_plain_timeout_proves_nothing(self):
        snap = evidence_from_record(death_reason=DEATH_PLAIN_TIMEOUT)
        assert snap.vendor_started is None  # unknown stays unknown

    def test_journaled_bootstrap_key_classifies_too(self):
        assert (
            evidence_from_record(
                death_reason=DEATH_PLAIN_TIMEOUT, evidence={"bootstrap": "failed"}
            ).vendor_started
            is False
        )
        assert (
            evidence_from_record(
                death_reason=DEATH_PLAIN_TIMEOUT, evidence={"bootstrap": "ok"}
            ).vendor_started
            is True
        )

    def test_unknown_publication_outcome_marks_delivered_work(self):
        snap = evidence_from_record(death_reason=DEATH_AFTER_PUBLICATION, candidate_shas=["c1"])
        assert snap.candidate_published is True
        assert snap.vendor_started is True

    def test_a_candidate_alone_is_not_delivery(self):
        """Candidates accumulate across repair cycles — only a delivery-shaped
        death reason marks the work as already delivered."""
        snap = evidence_from_record(death_reason=DEATH_PLAIN_TIMEOUT, candidate_shas=["c1"])
        assert snap.candidate_published is False

    def test_superseded_after_edit_with_candidate_is_delivered(self):
        snap = evidence_from_record(
            death_reason="superseded by issue edit (digest drift)", candidate_shas=["c1"]
        )
        assert snap.candidate_published is True


class TestDecisionReuse:
    """One decision per evidence snapshot — repeated events reuse it."""

    def test_digest_covers_the_objective_fields_only(self):
        base = ContinuationEvidence(vendor_started=None, checkpoint_committed=False)
        assert (
            base.digest()
            == ContinuationEvidence(
                vendor_started=None, checkpoint_committed=False, prior_mode_selected="fresh"
            ).digest()
        )  # prior mode is lineage, not input

    def test_digest_moves_with_the_material_fields(self):
        base = ContinuationEvidence()
        assert base.digest() != base.__class__(checkpoint_committed=True).digest()
        assert base.digest() != base.__class__(vendor_started=False).digest()
        assert base.digest() != base.__class__(candidate_published=True).digest()
        assert base.digest() != base.__class__(operator_discard_requested=True).digest()

    def test_matching_decision_reuses_the_persisted_one(self):
        decision = decide_continuation(ContinuationEvidence(checkpoint_committed=True))
        doc = decision.as_document()
        again = matching_decision(doc, ContinuationEvidence(checkpoint_committed=True))
        assert again is not None
        assert again.reused is True
        assert again.decided_at == decision.decided_at  # the ORIGINAL stamp
        assert again.mode is ContinuationMode.EXACT_WIP

    def test_matching_decision_refuses_a_changed_snapshot(self):
        doc = decide_continuation(ContinuationEvidence(checkpoint_committed=True))
        changed = matching_decision(
            doc.as_document(), ContinuationEvidence(checkpoint_committed=False)
        )
        assert changed is None  # the checkpoint vanished — re-decide

    def test_matching_decision_refuses_corrupt_documents(self):
        evidence = ContinuationEvidence()
        assert matching_decision(None, evidence) is None
        assert matching_decision({"mode": "nonsense", "evidence_digest": "x"}, evidence) is None
        assert matching_decision({"mode": "fresh"}, evidence) is None  # no digest
        assert (
            matching_decision({"mode": "fresh", "evidence_digest": evidence.digest()}, evidence)
            is None
        )  # no reason/decided_at — never guessed

    def test_the_document_carries_the_observability_keys(self):
        doc = decide_continuation(
            ContinuationEvidence(vendor_started=False, checkpoint_committed=False)
        ).as_document()
        assert doc["mode_selected"] == "fresh"
        assert doc["uncertain"] is False
        assert doc["no_checkpoint_baseline"] is True  # retry.no_checkpoint_baseline
        uncertain_doc = decide_continuation(ContinuationEvidence()).as_document()
        assert uncertain_doc["uncertain"] is True
        assert uncertain_doc["no_checkpoint_baseline"] is False


class TestOperatorWording:
    def test_the_discard_keyword_parses_off_the_note(self):
        assert operator_discard_requested("/retry abc restart") is True
        assert operator_discard_requested("/retry abc RESTART") is True
        assert operator_discard_requested("/retry abcdef1234567890") is False
        assert operator_discard_requested("") is False

    def test_the_ack_line_names_the_continuation_source(self):
        assert "committed baseline" in retry_ack_line(
            decide_continuation(ContinuationEvidence(vendor_started=False))
        )
        assert "exact WIP checkpoint" in retry_ack_line(
            decide_continuation(ContinuationEvidence(checkpoint_committed=True))
        )
        assert "explicit restart" in retry_ack_line(
            decide_continuation(ContinuationEvidence(operator_discard_requested=True))
        )

    def test_the_uncertain_note_never_promises_preservation(self):
        note = continuation.uncertain_retry_note(
            "run" * 11, decide_continuation(ContinuationEvidence(vendor_started=True))
        )
        assert "needs an operator decision" in note
        assert "restart" in note  # the explicit-discard way out is offered
        assert "Nothing was dispatched" in note

    def test_the_delivered_note_points_at_the_candidate(self):
        note = continuation.uncertain_retry_note(
            "run" * 11,
            decide_continuation(
                ContinuationEvidence(vendor_started=True, candidate_published=True)
            ),
        )
        assert "already delivered" in note
        assert "/reconcile" in note

    def test_decisions_are_frozen_value_objects(self):
        decision = decide_continuation(ContinuationEvidence())
        assert isinstance(decision, ContinuationDecision)
        with pytest.raises(AttributeError):
            decision.mode = ContinuationMode.EXACT_WIP  # type: ignore[misc]


# ----------------------------------------------------------------------
# Service wiring: /retry and the revival re-dispatch
# ----------------------------------------------------------------------


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_GITHUB_HARNESS_WORKFLOW=HARNESS_WORKFLOW,
        FORGE_HARNESS_MODEL=HARNESS_MODEL,
        FORGE_VERIFICATION_GRACE_SECONDS=0,
    )
    values.update(overrides)
    return Settings(**values)


class StubPRReviewer:
    async def review(self, **kwargs):  # pragma: no cover - unused on these paths
        raise AssertionError("no review happens on the retry paths")


def make_stack(fake: FakeGitHub) -> GitHubAgents:
    implementer = StubImplementer()
    return GitHubAgents(
        client=fake,
        reader=fake,
        planner=StubPlanner(),
        implementer=implementer,
        reviewer=StubPRReviewer(),
        flow=GitHubPublishFlow(fake, proposer=implementer, base_branch="main"),
    )


def make_service(db, fake: FakeGitHub) -> GitHubRunService:
    return GitHubRunService(
        db,
        make_settings(),
        ForgeConfig(),
        stack=make_stack(fake),
        repo_full_name=FIXTURES_REPO,
    )


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(FIXTURES_REPO, {"src/app.py": "print('hi')\n"})
    github.heads[FIXTURES_REPO]["main"] = BASE_HEAD
    github.seed_issue(FIXTURES_REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
    return github


@pytest.fixture()
def with_checkpoint(monkeypatch):
    """The control plane holds a committed checkpoint for every run."""
    from forge.runs import revival

    monkeypatch.setattr(revival, "_has_durable_checkpoint", lambda run_id: True)


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def start(service: GitHubRunService) -> str:
    return await service.start_run(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        issue_title=ISSUE_TITLE,
        issue_description=ISSUE_DESC,
        author_username="alice",
    )


async def go(service: GitHubRunService, run_id: str) -> None:
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=f"/go {run_id}",
        author_username="alice",
    )


async def drive_to_dead(
    db,
    service: GitHubRunService,
    *,
    reason: str,
    candidates: list[str] | None = None,
) -> str:
    """A dispatched run that died terminally with the recorded *reason*."""
    run_id = await start(service)
    await go(service, run_id)
    assert service is not None
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        run.status = FlowStatus.FAILED.value
        run.status_reason = reason
        run.candidate_shas = list(candidates or [])
        await session.commit()
    return run_id


async def retry(
    service: GitHubRunService,
    run_id: str,
    *,
    note: str | None = None,
    delivery_id: str,
) -> None:
    await service.handle_retry(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=note or f"/retry {run_id}",
        author_username="alice",
        delivery_id=delivery_id,
    )


def comment_bodies(fake: FakeGitHub) -> list[str]:
    return [call[1][3] for call in fake.calls_of("create_issue_comment")]


def retry_ack(fake: FakeGitHub) -> str:
    """The ``/retry`` acknowledgement note (the dispatch's taken-in-work ack
    may follow it, so 'last comment' is not the ack)."""
    return next(body for body in comment_bodies(fake) if "retried by @alice" in body)


def dispatch_calls(fake: FakeGitHub) -> int:
    """The TOTAL number of native workflow_dispatch calls so far."""
    return len(fake.calls_of("dispatch_workflow"))


class TestRetryDispatchModes:
    async def test_death_before_the_vendor_retries_from_the_committed_baseline(self, db, fake):
        """The issue's headline defect, fixed: a bootstrap death before the
        vendor started has NO checkpoint and NO candidate — the old
        no-candidate/no-checkpoint refusal made it unretryable, and the old
        unconditional ``required`` mode demanded a checkpoint that could
        never exist. The proven no-WIP retry now dispatches ``fresh`` on the
        frozen committed base."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_BEFORE_BOOTSTRAP)
        fake.dispatch_inputs.clear()

        await retry(service, run_id, delivery_id="retry-bootstrap-1")

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "fresh"  # no checkpoint demanded
        assert "committed baseline" in retry_ack(fake)  # the source is named
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["mode_selected"] == "fresh"
        assert doc["no_checkpoint_baseline"] is True

    async def test_a_repair_cycle_death_before_the_vendor_also_retries_fresh(self, db, fake):
        """The same proven no-WIP death on a run WITH an earlier candidate:
        the checkpoint that ``required`` would demand still cannot exist for
        this attempt — the committed baseline is the honest source."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_BEFORE_BOOTSTRAP, candidates=["c1"])
        fake.dispatch_inputs.clear()

        await retry(service, run_id, delivery_id="retry-bootstrap-2")

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "fresh"

    async def test_a_committed_checkpoint_dispatches_required_with_the_ref(
        self, db, fake, with_checkpoint
    ):
        """A paused execution with a committed checkpoint: the exact WIP is
        the authorized continuation — ``required`` plus the checkpoint
        reference (the run's held, latest committed checkpoint)."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=[])
        fake.dispatch_inputs.clear()

        await retry(service, run_id, delivery_id="retry-checkpoint-1")

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["mode_selected"] == "required"
        assert doc["checkpoint_committed"] is True
        ack = retry_ack(fake)
        assert "exact WIP checkpoint" in ack  # the source is named

    async def test_uncertain_evidence_dispatches_nothing_and_asks_the_operator(self, db, fake):
        """Ambiguous death with a candidate on record but no checkpoint and
        no vendor proof: ZERO native dispatches, ZERO vendor sessions — the
        operator-facing note states the source is unknown and offers the
        explicit restart or a reconciliation."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        fake.dispatch_inputs.clear()
        before = dispatch_calls(fake)

        await retry(service, run_id, delivery_id="retry-uncertain-1")

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before  # ZERO native dispatches
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.FAILED.value  # untouched: no attempt, no walk
        doc = run.evidence["continuation"]
        assert doc["mode_selected"] == "uncertain"
        assert doc["uncertain"] is True
        note = next(b for b in comment_bodies(fake) if "operator decision" in b)
        assert "needs an operator decision" in note
        assert f"/retry {run_id} restart" in note
        assert "Nothing was dispatched" in note

    async def test_the_explicit_restart_verb_discards_and_dispatches_restart(self, db, fake):
        """``/retry <run-id> restart``: the operator's explicit discard —
        the ack never promises preservation."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        fake.dispatch_inputs.clear()

        await retry(service, run_id, note=f"/retry {run_id} restart", delivery_id="retry-restart-1")

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "restart"
        ack = retry_ack(fake)
        assert "explicit restart" in ack
        assert "discarded by operator choice" in ack

    async def test_delivered_work_gets_terminal_guidance_not_a_resume(self, db, fake):
        """Death after candidate publication: the note points at the
        delivered candidate and `/reconcile`; nothing re-executes."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_AFTER_PUBLICATION, candidates=["c1"])
        fake.dispatch_inputs.clear()

        await retry(service, run_id, delivery_id="retry-delivered-1")

        assert fake.dispatch_inputs == []
        note = comment_bodies(fake)[-1]
        assert "already delivered" in note
        assert "/reconcile" in note


class TestRepeatedRetryEvents:
    async def test_a_redelivered_event_is_a_no_op(self, db, fake, with_checkpoint):
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        await retry(service, run_id, delivery_id="retry-once-1")
        fake.dispatch_inputs.clear()

        await retry(service, run_id, delivery_id="retry-once-1")  # same delivery id

        assert fake.dispatch_inputs == []  # A11: one logical attempt

    async def test_an_unchanged_snapshot_reuses_the_persisted_decision(
        self, db, fake, with_checkpoint
    ):
        """A repeated retry (a NEW delivery id, the run died again in the
        same shape) re-materializes the SAME decision — decided_at and all
        — instead of re-deciding."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        await retry(service, run_id, delivery_id="retry-reuse-1")
        first = dict((await get_run(db, run_id)).evidence["continuation"])
        assert first["mode_selected"] == "required"

        # The retried attempt dies the SAME way (same recorded reason, same
        # recoverable state) and is retried again.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = DEATH_PLAIN_TIMEOUT
            await session.commit()
        fake.dispatch_inputs.clear()

        await retry(service, run_id, delivery_id="retry-reuse-2")

        second = dict((await get_run(db, run_id)).evidence["continuation"])
        assert second["decided_at"] == first["decided_at"]  # ONE decision, reused
        assert second["mode_selected"] == "required"
        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"

    async def test_a_materially_changed_snapshot_re_decides(self, db, fake):
        """The vendor proof appearing (a recorded classification) re-decides
        the mode — the digest moved, the old decision is not reused."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        await retry(service, run_id, delivery_id="retry-redecide-1")
        uncertain = dict((await get_run(db, run_id)).evidence["continuation"])
        assert uncertain["mode_selected"] == "uncertain"

        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = DEATH_BEFORE_BOOTSTRAP  # the classification arrived
            await session.commit()
        fake.dispatch_inputs.clear()

        await retry(service, run_id, delivery_id="retry-redecide-2")

        fresh = dict((await get_run(db, run_id)).evidence["continuation"])
        assert fresh["mode_selected"] == "fresh"
        assert fresh["decided_at"] != uncertain["decided_at"]


class TestRevivalRedispatch:
    async def _revived_dead_run(self, db, service, *, reason: str) -> str:
        from forge.durable.controller import Controller

        run_id = await drive_to_dead(db, service, reason=reason, candidates=["c1"])
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            # What terminalize_failure + the auto-revive stamp leave behind:
            # the ORIGINAL death reason survives the walk in the stamp (the
            # walk itself overwrites status_reason).
            evidence = dict(run.evidence or {})
            evidence["revival"] = {"count": 1, "reason": reason}
            run.evidence = evidence
            controller = Controller(session)
            await controller.revive_transition(
                run_id, reason="test revival", authorized_by="operator:test"
            )
            await session.commit()
        return run_id

    async def test_the_revival_redispatch_carries_the_selected_mode(self, db, fake):
        """A proven no-WIP auto-revive re-drives the lane ``fresh`` on the
        committed baseline — the recovery scan no longer demands a
        checkpoint nobody uploaded."""
        service = make_service(db, fake)
        run_id = await self._revived_dead_run(db, service, reason=DEATH_BEFORE_BOOTSTRAP)
        fake.dispatch_inputs.clear()

        await service._redispatch_revival(run_id)

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "fresh"

    async def test_an_uncertain_revival_dispatches_nothing(self, db, fake):
        service = make_service(db, fake)
        run_id = await self._revived_dead_run(db, service, reason=DEATH_PLAIN_TIMEOUT)
        fake.dispatch_inputs.clear()
        before = dispatch_calls(fake)

        await service._redispatch_revival(run_id)

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before

    async def test_a_restart_after_decision_commit_before_dispatch_reuses_it(
        self, db, fake, with_checkpoint
    ):
        """Issue negative test 2: the worker died between the decision commit
        and the dispatch. The recovery scan's re-drive (``_redispatch_revival``)
        reuses the PERSISTED decision — the actual workflow inputs carry the
        mode that was selected, not a re-derived one."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        # The decision commits (what handle_retry does before its dispatch).
        committed = await service._select_continuation(
            run_id,
            death_reason=DEATH_PLAIN_TIMEOUT,
            evidence=dict((await get_run(db, run_id)).evidence or {}),
            candidate_shas=["c1"],
        )
        assert committed.mode_selected == "required"
        # The stranded dispatch is recovered — the walk already happened.
        from forge.durable.controller import Controller

        async with db() as session:
            controller = Controller(session)
            await controller.revive_transition(
                run_id, reason="retry requested by @alice", authorized_by="operator:alice"
            )
            await session.commit()
        fake.dispatch_inputs.clear()

        await service._redispatch_revival(run_id)

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["decided_at"] == committed.decided_at  # the SAME decision


class TestCheckpointBoundInversion:
    """The strict side of the fix: losing the checkpoint must NEVER produce
    a checkpoint-BOUND (required) dispatch with nothing to restore."""

    async def test_a_checkpoint_bound_retry_that_lost_its_checkpoint_stops(
        self, db, fake, monkeypatch
    ):
        from forge.runs import revival

        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        fake.dispatch_inputs.clear()

        # First retry: the checkpoint is held — exact WIP, required mode.
        monkeypatch.setattr(revival, "_has_durable_checkpoint", lambda run_id: True)
        await retry(service, run_id, delivery_id="retry-inversion-1")
        (first,) = fake.dispatch_inputs
        assert first["inputs"]["lane_resume_mode"] == "required"

        # The attempt dies again — and this time the checkpoint is GONE
        # (cleanup, quota, rot). The retry must not re-dispatch required
        # over a vanished checkpoint, and must not guess fresh either: the
        # state is uncertain, so nothing dispatches. The lane's own
        # required-restore refusal (R28-03) stays the last line of defense
        # for whatever still slips through.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = DEATH_PLAIN_TIMEOUT
            await session.commit()
        monkeypatch.setattr(revival, "_has_durable_checkpoint", lambda run_id: False)
        fake.dispatch_inputs.clear()
        before = dispatch_calls(fake)

        await retry(service, run_id, delivery_id="retry-inversion-2")

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["mode_selected"] == "uncertain"


class TestTimeoutStages:
    """The four timeout/death stages as separate service cases (issue
    Q35-02 negative tests): four distinct outcomes."""

    async def _stage(
        self, db, fake, monkeypatch, *, reason: str, candidates: list[str], checkpoint: bool
    ) -> tuple[FakeGitHub, FlowRun]:
        from forge.runs import revival

        monkeypatch.setattr(revival, "_has_durable_checkpoint", lambda run_id: bool(checkpoint))
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=reason, candidates=candidates)
        fake.dispatch_inputs.clear()
        await retry(service, run_id, delivery_id=f"retry-stage-{reason[:12]}")
        return fake, await get_run(db, run_id)

    async def test_timeout_before_bootstrap_retries_the_committed_baseline(
        self, db, fake, monkeypatch
    ):
        fake_, run = await self._stage(
            db, fake, monkeypatch, reason=DEATH_BEFORE_BOOTSTRAP, candidates=[], checkpoint=False
        )
        (dispatch,) = fake_.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "fresh"
        assert run.evidence["continuation"]["mode_selected"] == "fresh"

    async def test_timeout_after_vendor_acceptance_is_uncertain(self, db, fake, monkeypatch):
        fake_, run = await self._stage(
            db, fake, monkeypatch, reason=DEATH_AFTER_VENDOR, candidates=["c1"], checkpoint=False
        )
        assert fake_.dispatch_inputs == []
        assert run.evidence["continuation"]["mode_selected"] == "uncertain"

    async def test_timeout_after_checkpoint_upload_resumes_the_exact_wip(
        self, db, fake, monkeypatch
    ):
        fake_, run = await self._stage(
            db, fake, monkeypatch, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"], checkpoint=True
        )
        (dispatch,) = fake_.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"
        assert run.evidence["continuation"]["checkpoint_committed"] is True

    async def test_timeout_after_candidate_publication_is_terminal_guidance(
        self, db, fake, monkeypatch
    ):
        fake_, run = await self._stage(
            db,
            fake,
            monkeypatch,
            reason=DEATH_AFTER_PUBLICATION,
            candidates=["c1"],
            checkpoint=False,
        )
        assert fake_.dispatch_inputs == []
        assert run.evidence["continuation"]["candidate_published"] is True
        assert "already delivered" in comment_bodies(fake_)[-1]
