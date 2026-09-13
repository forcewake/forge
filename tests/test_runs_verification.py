"""Stage B2 (F19, ADR-0018 §5): the verification profile.

- The profile is built from FORGE_REQUIRED_JOBS (sorted tuple) with a
  freshness window; ``evaluate`` keeps the quality contract's (ok, reason)
  shape.
- An EMPTY profile lets the run proceed, but the evidence comment carries the
  "no verification profile configured" warning.
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
from forge.runs.verification import DEFAULT_FRESHNESS_WINDOW_SECONDS, VerificationProfile, evaluate
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
    async def test_empty_profile_labels_the_evidence_comment(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab)  # FORGE_REQUIRED_JOBS="" by default
        run_id, _branch = await drive_to_green_pipeline(service, fake_gitlab, db)

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        notes = [note["body"] for note in fake_gitlab.notes if "Forge run ready" in note["body"]]
        assert notes, "evidence comment posted"
        assert "⚠️ No verification profile configured — pipeline success only." in notes[0]

    async def test_nonempty_profile_succeeds_without_the_warning(self, db, fake_gitlab):
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
        notes = [note["body"] for note in fake_gitlab.notes if "Forge run ready" in note["body"]]
        assert notes and "No verification profile" not in notes[0]


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
