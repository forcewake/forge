"""Q35-02/R36-02: the continuation decision — retry continues from a
recoverable state, not from the retry verb.

Three layers, tested separately on purpose:

- the DECISION MODEL (:mod:`forge.adaptive.continuation`) — pure over an
  evidence snapshot; every arm of the table, the reuse digest and the
  operator-facing wording. R36-02: the typed recovery-request parse (the
  ``restart`` verb only from its documented argument position) and the
  vendor-start certainty from the persisted native-start intent.
- the REFUSAL CODES (:func:`forge.runs.revival.retry_rejection`) — every
  reason code reachable, messages unchanged, and the override matrix
  (code x decision-mode -> allowed/refused) at the service.
- the SERVICE WIRING (:class:`forge.runs.github_service.GitHubRunService`)
  — ``/retry`` and the revival re-dispatch select ``lane_resume_mode`` from
  the decision, persist it on the run's evidence, reuse it on repeated
  events, dispatch NOTHING when the recoverable state is unknown, and keep
  the strict required-restore contract for genuinely checkpoint-bound
  continuations.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive import continuation
from forge.adaptive.admission import ExecutionLease
from forge.adaptive.continuation import (
    ContinuationDecision,
    ContinuationEvidence,
    ContinuationMode,
    decide_continuation,
    evidence_from_record,
    matching_decision,
    operator_discard_requested,
    parse_recovery_request,
    retry_ack_line,
)
from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, FlowStatus
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.runs.github_service import GitHubRunService
from forge.runs.revival import RetryRejection, RetryRefusalCode, retry_rejection
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
TOKEN = "a1b2c3d4e5f67890a1b2c3d4e5f67890"  # a valid 32-hex run-id token

#: Terminal reasons the four timeout/death stages produce (the shapes the
#: service actually records — see forge.runs.execution/backends).
DEATH_BEFORE_BOOTSTRAP = (
    "harness_infrastructure: harness_bootstrap_failed (driver exit=setup_failed)"
)
DEATH_AFTER_VENDOR = "harness_code: harness_driver_failed (exit=error)"
DEATH_PLAIN_TIMEOUT = "harness_infrastructure: harness_timeout"
DEATH_AFTER_PUBLICATION = "commit_unknown_outcome"
DEATH_DISPATCH_NOT_OBSERVED = (
    "harness_infrastructure: dispatch never observed — discovery found no "
    "workflow_dispatch run after 20 attempts"
)


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

    async def lookup(self, verdict: str | None):
        async def _lookup(run_id: str) -> str | None:
            return verdict

        return _lookup

    async def test_bootstrap_failure_classification_proves_no_vendor(self):
        snap = await evidence_from_record(death_reason=DEATH_BEFORE_BOOTSTRAP)
        assert snap.vendor_started is False

    async def test_dispatch_never_observed_alone_proves_nothing(self):
        """R36-02/AT-03: discovery absence is NOT execution proof — the
        reason alone leaves the vendor start UNKNOWN (the job can have
        started with the response/discovery lost)."""
        snap = await evidence_from_record(death_reason=DEATH_DISPATCH_NOT_OBSERVED)
        assert snap.vendor_started is None
        assert snap.native_start_verdict is None  # no intent consulted

    async def test_dispatch_never_observed_with_a_never_dispatched_intent_proves_it(self):
        """The one upgrade path: the persisted native-start intent query
        says no intent row was ever written on this dead run — the provider
        call was provably never attempted."""
        snap = await evidence_from_record(
            death_reason=DEATH_DISPATCH_NOT_OBSERVED,
            run_id="r1",
            intent_lookup=await self.lookup("never_dispatched"),
        )
        assert snap.vendor_started is False
        assert snap.native_start_verdict == "never_dispatched"

    async def test_a_persisted_intent_stays_unknown_despite_empty_discovery(self):
        """AT-03's core: the intent row IS present (the dispatch call was
        attempted; the response was lost) — even with the reason saying
        "dispatch never observed", the vendor start stays UNKNOWN."""
        snap = await evidence_from_record(
            death_reason=DEATH_DISPATCH_NOT_OBSERVED,
            run_id="r1",
            intent_lookup=await self.lookup("dispatched"),
        )
        assert snap.vendor_started is None
        assert snap.native_start_verdict == "dispatched"

    async def test_an_unreadable_intent_lookup_stays_unknown(self):
        async def _boom(run_id: str) -> str:
            raise RuntimeError("lease table unreadable")

        snap = await evidence_from_record(
            death_reason=DEATH_DISPATCH_NOT_OBSERVED, run_id="r1", intent_lookup=_boom
        )
        assert snap.vendor_started is None

    async def test_a_none_intent_verdict_stays_unknown(self):
        snap = await evidence_from_record(
            death_reason=DEATH_DISPATCH_NOT_OBSERVED,
            run_id="r1",
            intent_lookup=await self.lookup(None),
        )
        assert snap.vendor_started is None

    async def test_the_intent_is_not_consulted_for_other_deaths(self):
        """Only the discovery-exhaustion shape needs the intent — a plain
        timeout never asks, and an intent verdict never leaks into it."""
        snap = await evidence_from_record(
            death_reason=DEATH_PLAIN_TIMEOUT,
            run_id="r1",
            intent_lookup=await self.lookup("never_dispatched"),
        )
        assert snap.vendor_started is None
        assert snap.native_start_verdict is None

    async def test_bootstrap_proof_beats_a_dispatched_intent(self):
        """The bootstrap classification is its own PRE-DISPATCH proof: the
        dispatched job itself reported dying before any vendor client —
        independent of what the intent record says."""
        snap = await evidence_from_record(
            death_reason=DEATH_BEFORE_BOOTSTRAP,
            run_id="r1",
            intent_lookup=await self.lookup("dispatched"),
        )
        assert snap.vendor_started is False

    async def test_driver_failure_proves_the_vendor_ran(self):
        snap = await evidence_from_record(death_reason=DEATH_AFTER_VENDOR)
        assert snap.vendor_started is True

    async def test_empty_driver_completion_proves_the_vendor_ran(self):
        snap = await evidence_from_record(death_reason="harness_code: harness_no_changes")
        assert snap.vendor_started is True

    async def test_plain_timeout_proves_nothing(self):
        snap = await evidence_from_record(death_reason=DEATH_PLAIN_TIMEOUT)
        assert snap.vendor_started is None  # unknown stays unknown

    async def test_journaled_bootstrap_key_classifies_too(self):
        assert (
            await evidence_from_record(
                death_reason=DEATH_PLAIN_TIMEOUT, evidence={"bootstrap": "failed"}
            )
        ).vendor_started is False
        assert (
            await evidence_from_record(
                death_reason=DEATH_PLAIN_TIMEOUT, evidence={"bootstrap": "ok"}
            )
        ).vendor_started is True

    async def test_unknown_publication_outcome_marks_delivered_work(self):
        snap = await evidence_from_record(
            death_reason=DEATH_AFTER_PUBLICATION, candidate_shas=["c1"]
        )
        assert snap.candidate_published is True
        assert snap.vendor_started is True

    async def test_a_candidate_alone_is_not_delivery(self):
        """Candidates accumulate across repair cycles — only a delivery-shaped
        death reason marks the work as already delivered."""
        snap = await evidence_from_record(death_reason=DEATH_PLAIN_TIMEOUT, candidate_shas=["c1"])
        assert snap.candidate_published is False

    async def test_superseded_after_edit_with_candidate_is_delivered(self):
        snap = await evidence_from_record(
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


class TestRecoveryRequestParsing:
    """R36-02: the typed /retry command — `restart` only from its
    documented argument position, never from a mention in the prose."""

    def test_the_documented_position_grants_the_discard(self):
        request = parse_recovery_request(f"@forge /retry {TOKEN} restart")
        assert request.requested == TOKEN
        assert request.restart is True
        assert operator_discard_requested(f"/retry {TOKEN} restart") is True

    def test_the_verb_is_case_insensitive(self):
        assert operator_discard_requested(f"/retry {TOKEN} RESTART") is True

    def test_the_bare_command_and_plain_retry_never_discard(self):
        assert operator_discard_requested("/retry") is False
        assert operator_discard_requested(f"/retry {TOKEN}") is False
        assert operator_discard_requested("") is False
        assert operator_discard_requested(None) is False

    def test_a_verb_without_a_subject_never_grants_the_discard(self):
        """/retry restart (no run id) is not the documented command — a bare
        command must never discard the LATEST dead run's WIP."""
        assert parse_recovery_request("/retry restart").restart is False

    def test_a_negated_mention_never_grants_the_discard(self):
        assert operator_discard_requested(f"/retry {TOKEN}\nPlease do not restart this.") is False
        assert operator_discard_requested(f"/retry {TOKEN} do-not-restart") is False

    def test_a_quoted_mention_never_grants_the_discard(self):
        assert (
            operator_discard_requested(
                f"/retry {TOKEN} — the docs say `restart` discards any unrecorded WIP"
            )
            is False
        )

    def test_an_unrelated_mention_never_grants_the_discard(self):
        assert (
            operator_discard_requested(
                f"/retry {TOKEN}\nThe runner may restart the worldcup coverage later."
            )
            is False
        )

    def test_a_later_command_with_the_verb_is_not_the_first_command(self):
        """Only the FIRST /retry command is acted on; a quoted foreign
        command's verb cannot graft onto it."""
        assert (
            operator_discard_requested(f"/retry\nsee also `/retry {TOKEN} restart` elsewhere")
            is False
        )

    def test_a_verb_stuck_to_a_longer_word_is_not_the_verb(self):
        assert operator_discard_requested(f"/retry {TOKEN} restartable") is False

    def test_an_invalid_id_token_disables_the_verb(self):
        """`abc` is not a run-id token — the mangled command grants nothing
        (the OLD substring behavior accepted it)."""
        assert operator_discard_requested("/retry abc restart") is False

    def test_the_verb_must_address_the_resolved_subject(self):
        other = "f" * 32
        assert parse_recovery_request(f"/retry {TOKEN} restart", run_id=TOKEN).restart is True
        assert parse_recovery_request(f"/retry {TOKEN} restart", run_id=other).restart is False
        # a PREFIX token still addresses the full subject
        assert parse_recovery_request(f"/retry {TOKEN[:8]} restart", run_id=TOKEN).restart is True


class TestPublicationUncertainty:
    """Issue scope 4: `commit_unknown_outcome` is uncertainty — never
    delivery proof, never a resume."""

    async def test_an_unknown_publication_outcome_never_authorizes_a_resume(self):
        decision = decide_continuation(
            await evidence_from_record(death_reason=DEATH_AFTER_PUBLICATION, candidate_shas=["c1"])
        )
        assert decision.mode is ContinuationMode.UNCERTAIN
        assert decision.dispatchable is False
        with pytest.raises(ValueError, match="selects no resume mode"):
            decision.resume_mode()

    async def test_the_plain_unknown_publication_note_offers_both_exits(self):
        note = continuation.uncertain_retry_note(
            "run" * 11,
            decide_continuation(
                await evidence_from_record(
                    death_reason=DEATH_AFTER_PUBLICATION, candidate_shas=["c1"]
                )
            ),
        )
        assert "/reconcile" in note  # the lost-publication path
        assert "Nothing was dispatched" in note


class TestDecisionLineage:
    """R36-02: the persisted decision records its originating attempt,
    evidence version, command identity and discard authority."""

    def test_the_document_carries_the_lineage_keys(self):
        decision = decide_continuation(
            ContinuationEvidence(
                operator_discard_requested=True,
                discard_authorized_by="operator:@alice",
                native_command_id="delivery-7",
                source_attempt=2,
            )
        )
        doc = decision.as_document()
        assert doc["evidence_version"] == 2
        assert doc["source_attempt"] == 2
        assert doc["native_command_id"] == "delivery-7"
        assert doc["discard_authorized_by"] == "operator:@alice"

    def test_no_discard_authority_is_recorded_without_a_discard(self):
        doc = decide_continuation(ContinuationEvidence(vendor_started=False)).as_document()
        assert doc["discard_authorized_by"] is None

    def test_a_discard_without_a_named_author_defaults_to_operator(self):
        doc = decide_continuation(
            ContinuationEvidence(operator_discard_requested=True)
        ).as_document()
        assert doc["discard_authorized_by"] == "operator"

    def test_reuse_keeps_the_originating_command_identity(self):
        decision = decide_continuation(
            ContinuationEvidence(
                vendor_started=False, native_command_id="delivery-1", source_attempt=1
            )
        )
        doc = decision.as_document()
        # a NEW event over the unchanged snapshot reuses the SAME decision —
        # the ORIGINAL command identity survives, not the repeating event's.
        again = matching_decision(doc, ContinuationEvidence(vendor_started=False))
        assert again is not None and again.reused is True
        assert again.evidence.native_command_id == "delivery-1"
        assert again.evidence.source_attempt == 1

    def test_the_lineage_fields_do_not_move_the_reuse_digest(self):
        base = ContinuationEvidence(vendor_started=None, checkpoint_committed=False)
        assert (
            base.digest()
            == ContinuationEvidence(
                vendor_started=None,
                checkpoint_committed=False,
                native_command_id="d-2",
                source_attempt=5,
                discard_authorized_by="operator:@bob",
                native_start_verdict="dispatched",
            ).digest()
        )


class TestOperatorWording:
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
# R36-02: the typed /retry refusals — every code reachable, messages kept
# ----------------------------------------------------------------------


def make_run(**overrides) -> FlowRun:
    """A dead-run row for the pure retry_rejection table (no DB needed)."""
    values = dict(
        id=TOKEN,
        project_id=PROJECT_ID,
        provider="github",
        issue_iid=ISSUE,
        status=FlowStatus.FAILED.value,
        status_reason=DEATH_PLAIN_TIMEOUT,
        candidate_shas=["c1"],
        cancel_requested=False,
    )
    values.update(overrides)
    return FlowRun(**values)


def absent_outcome() -> Any:
    """A typed PROVEN-absent lookup answer (R36-03)."""
    from forge.adaptive.checkpoint_repository import (
        LOOKUP_ABSENT,
        CheckpointLookupOutcome,
    )

    return CheckpointLookupOutcome.missing(LOOKUP_ABSENT, authority="test")


def exact_outcome(checkpoint_id: str = "e" * 64) -> Any:
    """A typed exact lookup answer (R36-03)."""
    from forge.adaptive.checkpoint_repository import CheckpointLookupOutcome

    return CheckpointLookupOutcome.exact(checkpoint_id, authority="test")


def patch_checkpoint(monkeypatch, *, exact: bool, checkpoint_id: str = "e" * 64) -> None:
    """Point the retry lookup chain at a typed fake (R36-03: the async
    configured authority — sync boolean monkeypatches are gone)."""
    from forge.runs import revival

    async def _lookup(run_id: str, **_: Any):
        return exact_outcome(checkpoint_id) if exact else absent_outcome()

    monkeypatch.setattr(revival, "durable_checkpoint_outcome", _lookup)


class TestRetryRejectionCodes:
    @pytest.fixture(autouse=True)
    def _no_checkpoint(self):
        """The typed-refusal table tests pass their outcome explicitly."""

    def code_of(self, run: FlowRun | None, **kwargs) -> str:
        rejection = retry_rejection(run, **kwargs)
        assert isinstance(rejection, RetryRejection) or rejection == ""
        return rejection.code if isinstance(rejection, RetryRejection) else ""

    def test_every_reason_code_is_reachable(self):
        assert self.code_of(None) == RetryRefusalCode.NO_RETRYABLE_RUN.value
        assert self.code_of(make_run(), other_active=True) == RetryRefusalCode.OTHER_ACTIVE.value
        assert (
            self.code_of(make_run(status="completed"))
            == RetryRefusalCode.NOT_RETRYABLE_STATUS.value
        )
        assert (
            self.code_of(make_run(cancel_requested=True)) == RetryRefusalCode.CANCEL_REQUESTED.value
        )
        assert (
            self.code_of(make_run(candidate_shas=[]), checkpoint=absent_outcome())
            == RetryRefusalCode.NOTHING_TO_RETRY.value
        )

    def test_the_operator_messages_are_unchanged(self):
        """R36-02: the typing must not reword the operator-facing text."""
        assert retry_rejection(None) == (
            "`/retry` found no retryable run on this issue. "
            "Start a fresh run by posting a new implement request."
        )
        assert retry_rejection(make_run(candidate_shas=[]), checkpoint=absent_outcome()) == (
            f"Run `{TOKEN[:8]}` died before it committed a candidate and has no "
            "stored checkpoint — there is no work to retry in place. Start fresh "
            "with a new implement request."
        )
        assert retry_rejection(make_run(), other_active=True) == (
            f"Run `{TOKEN[:8]}` cannot be retried: another run is already in flight on this "
            "subject — forge keeps one active run per subject. Let it finish or cancel it "
            "first."
        )
        assert retry_rejection(make_run(cancel_requested=True)) == (
            f"Run `{TOKEN[:8]}` was cancelled by an operator — retrying a revoked publication "
            "grant is not allowed. Start fresh with a new implement request."
        )
        assert (
            retry_rejection(make_run(status="completed"))
            == f"Run `{TOKEN[:8]}` is `completed`, not `failed`/`blocked` — there is nothing to "
            "retry. Cancelled runs and fresh work need a new implement request."
        )

    def test_a_rejection_is_a_plain_string_to_its_callers(self):
        """The azure/gitlab callers keep posting it verbatim: truthiness and
        f-string interpolation behave exactly like the old plain strings."""
        rejection = retry_rejection(make_run(candidate_shas=[]), checkpoint=absent_outcome())
        assert isinstance(rejection, str)
        assert bool(rejection) is True
        assert f"🔁 {rejection}".startswith("🔁 Run")
        assert retry_rejection(make_run()) == ""  # fine — no refusal

    def test_the_checkpoint_resume_point_still_passes(self):
        assert retry_rejection(make_run(candidate_shas=[]), checkpoint=exact_outcome()) == ""

    def test_an_unanswerable_authority_is_never_worded_as_absence(self):
        """R36-03: an outage/refused-credential outcome is its OWN typed
        refusal — never the "no stored checkpoint" wording (which would
        send the operator to a fresh run over recoverable work)."""
        from forge.adaptive.checkpoint_repository import (
            LOOKUP_UNAVAILABLE,
            CheckpointLookupOutcome,
        )

        unavailable = CheckpointLookupOutcome.missing(
            LOOKUP_UNAVAILABLE, authority="postgres", detail="connection refused"
        )
        rejection = retry_rejection(make_run(candidate_shas=[]), checkpoint=unavailable)
        assert isinstance(rejection, RetryRejection)
        assert rejection.code == RetryRefusalCode.CHECKPOINT_AUTHORITY_UNAVAILABLE.value
        assert "no stored checkpoint" not in rejection
        assert "connection refused" in rejection
        assert "nothing was dispatched" in rejection
        # An unconsulted lookup (None) is the same honest refusal.
        none_rejection = retry_rejection(make_run(candidate_shas=[]))
        assert isinstance(none_rejection, RetryRejection)
        assert none_rejection.code == RetryRefusalCode.CHECKPOINT_AUTHORITY_UNAVAILABLE.value


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
    patch_checkpoint(monkeypatch, exact=True)


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


async def wipe_native_intent(db, run_id: str) -> None:
    """AT-03 shaping: the run's lease rows carry NO native-start intent —
    the dispatch leg provably never attempted the provider call."""
    async with db() as session:
        rows = (
            (await session.execute(select(ExecutionLease).where(ExecutionLease.run_id == run_id)))
            .scalars()
            .all()
        )
        for row in rows:
            row.native_intent_at = None
            row.native_intent_ref = None
        await session.commit()


async def lease_intents(db, run_id: str) -> list:
    """The persisted native-start intent timestamps for the run."""
    async with db() as session:
        return list(
            (
                await session.execute(
                    select(ExecutionLease.native_intent_at).where(ExecutionLease.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )


class TestExplicitRestartRecovery:
    """R36-02/AT-02: an authorized ``/retry <run> restart`` with no candidate
    and no checkpoint reaches exactly ONE restart dispatch; every other
    refusal code still refuses; prose cannot grant the discard."""

    async def _dead_with_no_work_in_place(self, db, fake, *, reason=DEATH_PLAIN_TIMEOUT) -> str:
        run_id = await drive_to_dead(db, make_service(db, fake), reason=reason, candidates=[])
        fake.dispatch_inputs.clear()
        return run_id

    async def test_the_explicit_restart_dispatches_exactly_once_in_restart_mode(self, db, fake):
        """The issue's headline defect, fixed: the documented recovery exit
        works exactly where it is needed — no candidate, no checkpoint, and
        the operator-authorized discard satisfies the continuity arm."""
        run_id = await self._dead_with_no_work_in_place(db, fake)
        before = dispatch_calls(fake)

        await retry(
            make_service(db, fake),
            run_id,
            note=f"/retry {run_id} restart",
            delivery_id="retry-restart-nocand-1",
        )

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "restart"
        assert dispatch_calls(fake) == before + 1  # exactly ONE native dispatch
        ack = retry_ack(fake)
        assert "explicit restart" in ack
        assert "discarded by operator choice" in ack
        run = await get_run(db, run_id)
        doc = run.evidence["continuation"]
        assert doc["mode_selected"] == "restart"
        assert doc["discard_authorized_by"] == "operator:@alice"
        assert doc["native_command_id"] == "retry-restart-nocand-1"
        assert doc["source_attempt"] == 0  # the dead attempt's generation
        assert "refusal_code" not in doc  # the override lifted the refusal

    async def test_a_plain_uncertain_retry_with_no_work_in_place_parks(self, db, fake):
        """The same shape WITHOUT the verb: nothing dispatches and the note
        is the UNCERTAIN park (the honest explanation + the two ways out),
        never an inference that the WIP was empty."""
        run_id = await self._dead_with_no_work_in_place(db, fake)
        before = dispatch_calls(fake)

        await retry(make_service(db, fake), run_id, delivery_id="retry-plain-nocand-1")

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before
        run = await get_run(db, run_id)
        doc = run.evidence["continuation"]
        assert doc["mode_selected"] == "uncertain"
        assert doc["refusal_code"] == "nothing_to_retry"  # the typed refusal
        note = next(b for b in comment_bodies(fake) if "operator decision" in b)
        assert "needs an operator decision" in note
        assert f"/retry {run_id} restart" in note
        assert "no work to retry in place" not in note  # not the presuming wording

    @pytest.mark.parametrize(
        "note",
        [
            "/retry {run_id}\nPlease do NOT restart this — the branch has work.",
            "/retry {run_id} — the docs say `restart` discards unrecorded WIP",
            "/retry {run_id}\nthe runner may restart mid-race, unrelated",
        ],
    )
    async def test_prose_never_grants_the_discard(self, db, fake, note):
        run_id = await self._dead_with_no_work_in_place(db, fake)
        before = dispatch_calls(fake)

        await retry(
            make_service(db, fake),
            run_id,
            note=note.format(run_id=run_id),
            delivery_id=f"retry-prose-{abs(hash(note)) % 1000}",
        )

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before  # zero dispatches, no discard
        assert (await get_run(db, run_id)).evidence["continuation"]["mode_selected"] == (
            "uncertain"
        )

    async def test_a_cancelled_run_still_refuses_the_explicit_restart(self, db, fake):
        run_id = await self._dead_with_no_work_in_place(db, fake)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.cancel_requested = True
            await session.commit()
        before = dispatch_calls(fake)

        await retry(
            make_service(db, fake),
            run_id,
            note=f"/retry {run_id} restart",
            delivery_id="retry-restart-cancelled-1",
        )

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before
        note = comment_bodies(fake)[-1]
        assert "cancelled by an operator" in note
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["refusal_code"] == "cancel_requested"

    async def test_a_non_retryable_status_still_refuses_the_explicit_restart(self, db, fake):
        run_id = await self._dead_with_no_work_in_place(db, fake)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.READY_FOR_HUMAN.value
            await session.commit()
        before = dispatch_calls(fake)

        await retry(
            make_service(db, fake),
            run_id,
            note=f"/retry {run_id} restart",
            delivery_id="retry-restart-status-1",
        )

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before
        note = comment_bodies(fake)[-1]
        assert "not `failed`/`blocked`" in note
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["refusal_code"] == "not_retryable_status"

    async def test_another_active_run_still_refuses_the_explicit_restart(self, db, fake):
        service = make_service(db, fake)
        run_id = await self._dead_with_no_work_in_place(db, fake)
        await start(service)  # a second, LIVE run on the same subject
        fake.dispatch_inputs.clear()
        before = dispatch_calls(fake)

        await retry(
            make_service(db, fake),
            run_id,
            note=f"/retry {run_id} restart",
            delivery_id="retry-restart-active-1",
        )

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before
        note = comment_bodies(fake)[-1]
        assert "another run is already in flight" in note
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["refusal_code"] == "other_active"

    async def test_a_foreign_subject_retry_is_ignored(self, db, fake):
        """A run id that does not resolve on THIS subject is not a command
        here: no dispatch, no note, no decision."""
        run_id = await self._dead_with_no_work_in_place(db, fake)
        foreign = "f" * 32
        before = dispatch_calls(fake)

        await retry(
            make_service(db, fake),
            foreign,
            note=f"/retry {foreign} restart",
            delivery_id="retry-restart-foreign-1",
        )

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before
        assert not any("retried by" in b for b in comment_bodies(fake))
        assert "continuation" not in ((await get_run(db, run_id)).evidence or {})


class TestNativeStartIntentEvidence:
    """R36-02/AT-03: vendor-start certainty from the persisted native-start
    intent — an undiscovered remote execution stays UNKNOWN; only a missing
    intent on a dead run proves the never-dispatched baseline."""

    async def _dead_after_a_lost_dispatch_response(self, db, fake) -> str:
        """The AT-03 shape: the dispatch call was ATTEMPTED (the intent is
        durable), the response/discovery was lost, the run died with the
        bounded-discovery reason — no candidate, no checkpoint."""
        run_id = await drive_to_dead(
            db, make_service(db, fake), reason=DEATH_DISPATCH_NOT_OBSERVED, candidates=[]
        )
        fake.dispatch_inputs.clear()
        intents = await lease_intents(db, run_id)
        assert intents and all(value is not None for value in intents)
        return run_id

    async def test_a_persisted_intent_keeps_the_outcome_uncertain(self, db, fake):
        run_id = await self._dead_after_a_lost_dispatch_response(db, fake)
        before = dispatch_calls(fake)

        await retry(make_service(db, fake), run_id, delivery_id="retry-intent-1")

        assert fake.dispatch_inputs == []
        assert dispatch_calls(fake) == before  # NO never_started proof, NO fresh restart
        run = await get_run(db, run_id)
        doc = run.evidence["continuation"]
        assert doc["mode_selected"] == "uncertain"
        assert doc["vendor_started"] is None
        assert doc["native_start_verdict"] == "dispatched"
        assert doc["refusal_code"] == "nothing_to_retry"

    async def test_a_missing_intent_proves_the_never_dispatched_baseline(self, db, fake):
        """The inverse: no intent row was ever persisted — the provider call
        provably never happened, so the committed baseline IS the retry."""
        run_id = await self._dead_after_a_lost_dispatch_response(db, fake)
        await wipe_native_intent(db, run_id)

        await retry(make_service(db, fake), run_id, delivery_id="retry-nointent-1")

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "fresh"
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["vendor_started"] is False
        assert doc["native_start_verdict"] == "never_dispatched"

    async def test_the_true_before_dispatch_failure_is_the_positive_control(self, db, fake):
        """The bootstrap-classification proof (the job itself reported dying
        before any vendor client) allows the fresh retry without any
        intent consultation."""
        run_id = await drive_to_dead(
            db, make_service(db, fake), reason=DEATH_BEFORE_BOOTSTRAP, candidates=[]
        )
        await wipe_native_intent(db, run_id)
        fake.dispatch_inputs.clear()

        await retry(make_service(db, fake), run_id, delivery_id="retry-bootstrap-nointent-1")

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "fresh"
        doc = (await get_run(db, run_id)).evidence["continuation"]
        assert doc["vendor_started"] is False
        assert doc["native_start_verdict"] is None  # never consulted


class TestRepeatedRetryEvents:
    async def test_a_redelivered_event_is_a_no_op(self, db, fake, with_checkpoint):
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        await retry(service, run_id, delivery_id="retry-once-1")
        fake.dispatch_inputs.clear()

        await retry(service, run_id, delivery_id="retry-once-1")  # same delivery id

        assert fake.dispatch_inputs == []  # A11: one logical attempt

    async def test_a_repeated_restart_event_is_one_decision_and_one_attempt_per_event(
        self, db, fake
    ):
        """R36-02: repeated delivery of the same command reuses the decision
        (one ``decided_at``) and never double-increments attempts — each NEW
        event dispatches once, a redelivered event not at all."""
        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=[])
        before = dispatch_calls(fake)

        await retry(
            service, run_id, note=f"/retry {run_id} restart", delivery_id="retry-rerestart-1"
        )
        first = dict((await get_run(db, run_id)).evidence["continuation"])
        assert first["mode_selected"] == "restart"
        cycle_after_first = int((await get_run(db, run_id)).commit_cycle or 0)
        assert dispatch_calls(fake) == before + 1

        # The same delivery redelivered: a NO-OP (A11 idempotency).
        await retry(
            service, run_id, note=f"/retry {run_id} restart", delivery_id="retry-rerestart-1"
        )
        assert dispatch_calls(fake) == before + 1
        assert int((await get_run(db, run_id)).commit_cycle or 0) == cycle_after_first

        # The restarted attempt dies the SAME way and is restarted again: a
        # NEW event over an unchanged snapshot REUSES the decision.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = DEATH_PLAIN_TIMEOUT
            run.candidate_shas = []
            await session.commit()
        await retry(
            service, run_id, note=f"/retry {run_id} restart", delivery_id="retry-rerestart-2"
        )
        second = dict((await get_run(db, run_id)).evidence["continuation"])
        assert second["decided_at"] == first["decided_at"]  # ONE decision, reused
        assert second["mode_selected"] == "restart"
        assert second["native_command_id"] == "retry-rerestart-1"  # the ORIGINATING event
        assert dispatch_calls(fake) == before + 2  # one dispatch per NEW event

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

        service = make_service(db, fake)
        run_id = await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"])
        fake.dispatch_inputs.clear()

        # First retry: the checkpoint is held — exact WIP, required mode.
        patch_checkpoint(monkeypatch, exact=True)
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
        patch_checkpoint(monkeypatch, exact=False)
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

        patch_checkpoint(monkeypatch, exact=bool(checkpoint))
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
