"""Stage B2 (F19, ADR-0018 §5) + R02 honest labeling: the verification profile.

- The profile is built from FORGE_REQUIRED_JOBS (sorted tuple) with a
  freshness window; ``evaluate`` keeps the quality contract's (ok, reason)
  shape.
- R02: an EMPTY profile never presents pipeline success as verified — the
  run still reaches ready_for_human, but the evidence records
  ``verification.status="unverified"`` (the unified
  :class:`~forge.runs.verification.VerificationResult` shape), the ready
  reason says unverified and the evidence comment carries the warning.
  A non-empty profile records ``passed`` only when every required job
  succeeded for the EXACT candidate sha.
- Post-review freshness: if the branch head moved past the reviewed candidate
  before the run went ready, the run is blocked ``candidate_drift_after_review``.
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import pytest

from forge.config import Settings
from forge.durable import FlowRun, FlowStatus
from forge.gitlab.schemas import Job, Pipeline
from forge.models.base import Base
from forge.runs import RunService
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer, factory_branch
from forge.runs.verification import (
    AZURE_CODE_FAILURE_RESULTS,
    AZURE_INFRA_RESULTS,
    AZURE_SUCCESS_RESULTS,
    DEFAULT_FRESHNESS_WINDOW_SECONDS,
    GITHUB_CODE_FAILURE_CONCLUSIONS,
    GITHUB_INFRA_CONCLUSIONS,
    GITHUB_SUCCESS_CONCLUSIONS,
    PRODUCER_GITLAB_PIPELINE,
    VERIFICATION_INFRA_REASON,
    PositiveProof,
    VerificationProfile,
    VerificationResult,
    evaluate,
    evaluate_positive_proof,
    waived_conclusions_from_settings,
)
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import FakeWriter

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


def make_service(db, fake_gitlab, *, reviewer=None, settings=None) -> RunService:
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings or make_settings(),
        writer_class=FakeWriter,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=reviewer or StubReviewer(),
    )


def pipeline(status: str) -> Pipeline:
    return Pipeline.model_validate({"id": 9, "status": status})


def job(name: str, status: str) -> Job:
    return Job.model_validate({"id": 1, "name": name, "status": status})


class DriftingReviewer(StubReviewer):
    """A reviewer during whose (in-flight) run a human push lands."""

    def __init__(self, fake: FakeGitLab, branch: str) -> None:
        super().__init__()
        self._fake = fake
        self._branch = branch

    async def review(self, **kwargs):
        self._fake.seed_commit(self._branch, "human-sha", "human push during review")
        return await super().review(**kwargs)


class TestVerificationResult:
    """R02: the ONE evidence shape every provider records."""

    def test_as_evidence_carries_the_unified_keys(self):
        result = VerificationResult.passed(
            "a" * 40,
            PRODUCER_GITLAB_PIPELINE,
            summary="quality contract satisfied",
            surface=({"name": "test", "status": "success"},),
        )
        evidence = result.as_evidence()
        assert set(evidence) >= {"status", "tested_oid", "observed_at", "producer"}
        assert evidence["status"] == "passed"
        assert evidence["tested_oid"] == "a" * 40
        assert evidence["producer"] == PRODUCER_GITLAB_PIPELINE
        assert evidence["surface"] == [{"name": "test", "status": "success"}]

    def test_only_a_passed_verdict_is_verified(self):
        assert VerificationResult.passed("a" * 40, "x").verified is True
        for status in (
            VerificationResult.pending,
            VerificationResult.failed,
            VerificationResult.unknown,
            VerificationResult.not_configured,
            VerificationResult.unverified,
        ):
            assert status("a" * 40, "x").verified is False


class TestVerificationProfile:
    def test_from_settings_sorts_required_jobs(self):
        settings = make_settings(FORGE_REQUIRED_JOBS="pytest, lint ,sast")
        profile = VerificationProfile.from_settings(settings)
        assert profile.required_jobs == ("lint", "pytest", "sast")
        assert profile.freshness_window == DEFAULT_FRESHNESS_WINDOW_SECONDS == 60

    def test_from_settings_empty_profile(self):
        profile = VerificationProfile.from_settings(make_settings())
        assert profile.required_jobs == ()

    def test_evaluate_empty_profile_is_ok_with_warning_reason(self):
        ok, reason = evaluate(pipeline("success"), [], VerificationProfile(required_jobs=()))
        assert ok is True
        assert reason == "no verification profile configured (warning)"

    def test_evaluate_nonempty_profile_enforces_jobs(self):
        profile = VerificationProfile(required_jobs=("test",))
        ok, reason = evaluate(pipeline("success"), [job("build", "success")], profile)
        assert ok is False
        assert "test" in reason

        ok, _ = evaluate(pipeline("success"), [job("test", "success")], profile)
        assert ok is True


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
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def drive_to_green_pipeline(service, fake_gitlab: FakeGitLab, db) -> tuple[str, str]:
    """start_run → /go → committed candidate with a green pipeline (no CI tick)."""
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    branch = factory_branch(ISSUE_IID, run_id)
    sha = (await get_run(db, run_id)).candidate_shas[-1]
    fake_gitlab.seed_commit(branch, sha, "forge commit")
    pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
    fake_gitlab.set_pipeline_status(pipeline_id, "success", sha)
    return run_id, branch


class TestEmptyProfileEvidence:
    async def test_empty_profile_is_honestly_unverified(self, db, fake_gitlab):
        """R02: an empty profile never presents pipeline success as verified —
        the run still reaches the human, labeled unverified everywhere."""
        service = make_service(db, fake_gitlab)  # FORGE_REQUIRED_JOBS="" by default
        run_id, _branch = await drive_to_green_pipeline(service, fake_gitlab, db)

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        # The ready reason says unverified (the honest form, R02).
        assert "unverified" in (run.status_reason or "")
        # The evidence records the unified VerificationResult shape.
        verification = (run.evidence or {})["verification"]
        assert set(verification) >= {"status", "tested_oid", "observed_at", "producer"}
        assert verification["status"] == "unverified"
        assert verification["tested_oid"] == run.candidate_shas[-1]
        assert verification["producer"] == PRODUCER_GITLAB_PIPELINE
        notes = [note["body"] for note in fake_gitlab.notes if "Forge run ready" in note["body"]]
        assert notes, "evidence comment posted"
        assert "⚠️ No verification profile configured — pipeline success only." in notes[0]
        assert "**unverified**" in notes[0]

    async def test_nonempty_profile_verifies_and_records_passed(self, db, fake_gitlab):
        settings = make_settings(FORGE_REQUIRED_JOBS="test")
        service = make_service(db, fake_gitlab, settings=settings)
        run_id, _branch = await drive_to_green_pipeline(service, fake_gitlab, db)
        # The pipeline must contain the required job, green.
        async with db() as session:
            sha = (await session.get(FlowRun, run_id)).candidate_shas[-1]
        pipeline_id = (await fake_gitlab.list_pipelines(PROJECT_ID, sha=sha))[0].id
        fake_gitlab.set_pipeline_jobs(pipeline_id, [job("test", "success")])

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert (run.status_reason or "").startswith("checks passed")
        verification = (run.evidence or {})["verification"]
        assert set(verification) >= {"status", "tested_oid", "observed_at", "producer"}
        assert verification["status"] == "passed"
        assert verification["tested_oid"] == run.candidate_shas[-1]
        assert verification["producer"] == PRODUCER_GITLAB_PIPELINE
        notes = [note["body"] for note in fake_gitlab.notes if "Forge run ready" in note["body"]]
        assert notes and "No verification profile" not in notes[0]
        assert "All checks passed" in notes[0]


class TestPostReviewFreshness:
    async def test_head_drift_during_review_blocks(self, db, fake_gitlab):
        # The reviewer is where the race happens: the human push lands while
        # the review is in flight — past the pre-review external_change check.
        service = make_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        branch = factory_branch(ISSUE_IID, run_id)
        sha = (await get_run(db, run_id)).candidate_shas[-1]
        service._reviewer = DriftingReviewer(fake_gitlab, branch)
        fake_gitlab.seed_commit(branch, sha, "forge commit")
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "success", sha)

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("candidate_drift_after_review")

    async def test_stable_head_reaches_ready(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab)
        run_id, _branch = await drive_to_green_pipeline(service, fake_gitlab, db)

        await service.evaluate_waiting_ci()

        assert (await get_run(db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value


# ----------------------------------------------------------------------
# A01: the positive-proof contract shared by the GitHub/Azure gates
# ----------------------------------------------------------------------


class TestPositiveProof:
    """Verification is POSITIVE PROOF that the required checks RAN and
    succeeded for the candidate commit — never "nothing failed" (A01).

    The evaluator is provider-neutral: GitHub passes its conclusion
    vocabulary, Azure its build results; the semantics are identical."""

    def proof(
        self,
        required: tuple[str, ...],
        observations: dict[str, str | None],
        *,
        waived: frozenset[str] = frozenset(),
        azure: bool = False,
    ) -> PositiveProof:
        return evaluate_positive_proof(
            required,
            observations,
            success_conclusions=AZURE_SUCCESS_RESULTS if azure else GITHUB_SUCCESS_CONCLUSIONS,
            code_failure_conclusions=(
                AZURE_CODE_FAILURE_RESULTS if azure else GITHUB_CODE_FAILURE_CONCLUSIONS
            ),
            infra_conclusions=AZURE_INFRA_RESULTS if azure else GITHUB_INFRA_CONCLUSIONS,
            waived_conclusions=waived,
        )

    def test_required_absent_with_a_green_optional_is_never_verified(self):
        """A01 AC1: the required `tests` check never ran — a green
        `documentation` workflow proves nothing and never substitutes."""
        proof = self.proof(("tests",), {"documentation": "success"})

        assert proof.verified is False
        assert proof.missing == ("tests",)
        assert proof.unproven == ("tests",)
        assert proof.code_failures == () and proof.infra_failures == ()
        assert "tests" in proof.summary() and "not run" in proof.summary()

    @pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
    def test_inconclusive_required_conclusion_never_verifies(self, conclusion):
        """A01 AC2: a skipped/neutral required check is not proof — the
        verdict is unknown, not green."""
        proof = self.proof(("tests",), {"tests": conclusion})

        assert proof.verified is False
        assert proof.inconclusive == ("tests",)

    def test_missing_conclusion_is_inconclusive_not_missing(self):
        """A check the provider observed but that concluded NOTHING carries
        no proof either."""
        proof = self.proof(("tests",), {"tests": None})

        assert proof.verified is False
        assert proof.inconclusive == ("tests",)
        assert proof.missing == ()

    def test_waiver_config_flips_an_inconclusive_required_to_verified(self):
        """FORGE_VERIFICATION_WAIVE_CONCLUSIONS is the ONLY way a skipped/
        neutral required check counts — an explicit deployment policy."""
        proof = self.proof(
            ("tests", "lint"),
            {"tests": "skipped", "lint": "success"},
            waived=frozenset({"skipped", "neutral"}),
        )

        assert proof.verified is True
        assert proof.waived == ("tests",)
        assert proof.succeeded == ("lint",)

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
    def test_the_waiver_can_never_flip_failure_or_infra_conclusions(self, conclusion):
        """The waiver is for genuinely inconclusive conclusions only: a
        failure-class or cancelled/timed_out conclusion never becomes proof."""
        proof = self.proof(("tests",), {"tests": conclusion}, waived=frozenset({conclusion}))

        assert proof.verified is False

    def test_github_cancelled_and_timed_out_are_infrastructure(self):
        """A01 AC3: cancel/timeout evidence blames the EXECUTION — the infra
        class blocks, the code-repair budget is never spent on it."""
        for conclusion, expected in (
            ("cancelled", "infra_failure"),
            ("timed_out", "infra_failure"),
            ("failure", "code_failure"),
            ("action_required", "code_failure"),
            ("success", "succeeded"),
        ):
            proof = self.proof(("tests",), {"tests": conclusion})
            state = (
                "infra_failure"
                if proof.infra_failures
                else (
                    "code_failure"
                    if proof.code_failures
                    else ("succeeded" if proof.verified else "inconclusive")
                )
            )
            assert state == expected, conclusion

    def test_azure_results_classify_the_same_way(self):
        """Azure parity: succeeded proves; failed/partiallySucceeded blame
        the change; canceled/abandoned are infrastructure. Matching is
        case-insensitive (the Azure vocabulary is camelCase)."""
        assert self.proof(("ci",), {"ci": "succeeded"}, azure=True).verified is True
        assert self.proof(("ci",), {"ci": "SUCCEEDED"}, azure=True).verified is True
        proof = self.proof(("ci",), {"ci": "partiallySucceeded"}, azure=True)
        assert proof.code_failures == ("ci",) and proof.verified is False
        for result in ("canceled", "abandoned"):
            proof = self.proof(("ci",), {"ci": result}, azure=True)
            assert proof.infra_failures == ("ci",) and proof.verified is False

    def test_empty_required_contract_makes_every_observed_check_required(self):
        """With NO frozen contract, every OBSERVED check is the proof set —
        so a lone skipped workflow still never verifies without the waiver
        (A01 AC2), while a green surface does."""
        proof = self.proof((), {"ci": "skipped"})
        assert proof.verified is False and proof.inconclusive == ("ci",)

        proof = self.proof((), {"ci": "success", "docs": "success"})
        assert proof.verified is True

    def test_unobserved_names_only_count_when_required(self):
        """Names nobody observed only matter when the contract REQUIRES
        them — an empty required list never invents missing checks."""
        proof = self.proof((), {})
        assert proof.verified is True
        proof = self.proof(("tests",), {})
        assert proof.verified is False and proof.missing == ("tests",)

    def test_waived_conclusions_parse_from_settings(self):
        settings = make_settings(FORGE_VERIFICATION_WAIVE_CONCLUSIONS="Skipped, neutral ,")
        assert waived_conclusions_from_settings(settings) == frozenset({"skipped", "neutral"})
        assert waived_conclusions_from_settings(make_settings()) == frozenset()

    def test_infra_reason_token_is_the_parked_vocabulary(self):
        """The infra blocked reason is the verification_timeout-family token
        both provider gates park runs with."""
        assert VERIFICATION_INFRA_REASON == "verification_infrastructure"
