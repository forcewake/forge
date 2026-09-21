"""Tests for the ADR-0015/0016 implementer backends over FakeGitLab.

CITharnessBackend: pipeline trigger with run variables (including the frozen
FORGE_ATTEMPT_BASE), job polling, and the candidate-artifact trust boundary
(change_candidate only for a well-formed downloaded bundle; the publisher
owns publication). BuiltinBackend: the propose→publisher path behind the
same protocol. build_backend: settings-driven construction.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import FlowRun
from forge.durable.identity import factory_branch
from forge.models.base import Base
from forge.repository import ChangesetWriter, Change, ChangeSet, Operation
from forge.runs.backends import (
    CITharnessBackend,
    BuiltinBackend,
    build_backend,
    is_harness_backend,
)
from forge.runs.stubs import StubImplementer
from tests.fixtures.candidate import create_diff, seed_candidate
from tests.fixtures.fake_gitlab import FakeGitLab

PROJECT_ID = 42
ISSUE_IID = 7
BASE_SHA = "base-sha-1"
HARNESS_SHA = "harness-sha-1"


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_TARGET_BRANCH="main",
        FORGE_HARNESS_MODEL="glm-5.3-flash[1m]",
    )
    values.update(overrides)
    return Settings(**values)


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
    fake.seed_commit("main", BASE_SHA, "initial")
    return fake


def make_run(run_id: str | None = None) -> FlowRun:
    return FlowRun(
        id=run_id or uuid4().hex,
        project_id=PROJECT_ID,
        issue_iid=ISSUE_IID,
        base_sha=BASE_SHA,
    )


def make_backend(fake_gitlab: FakeGitLab, db, **settings_overrides) -> CITharnessBackend:
    writer = ChangesetWriter(fake_gitlab, db, PROJECT_ID)
    return CITharnessBackend(
        gitlab=fake_gitlab,
        writer=writer,
        settings=make_settings(**settings_overrides),
    )


async def started(fake_gitlab: FakeGitLab, backend: CITharnessBackend, run=None):
    """start() a harness run; return (handle, run, branch)."""
    run = run or make_run()
    handle = await backend.start(run, "Add a widget", "widgets", "PLAN TEXT")
    return handle, run, factory_branch(ISSUE_IID, run.id)


def seed_job(
    fake_gitlab: FakeGitLab,
    pipeline_id: int,
    *,
    status: str,
    job_id: int = 555,
    failure_reason: str | None = None,
    log: str | None = None,
) -> int:
    job: dict = {"id": job_id, "name": "forge-agent", "status": status}
    if failure_reason is not None:
        job["failure_reason"] = failure_reason
    fake_gitlab.set_pipeline_jobs(pipeline_id, [job])
    if log is not None:
        fake_gitlab.set_job_log(job_id, log)
    return job_id


def seed_prefixed_lane_job(
    fake_gitlab: FakeGitLab,
    pipeline_id: int,
    *,
    status: str,
    job_id: int = 556,
) -> int:
    """A per-driver lane job (LIVE-found naming: the SDK lanes name
    their jobs forge-agent-<driver> so GitLab's last-include-wins
    cannot silently replace one lane with another)."""
    job = {"id": job_id, "name": "forge-agent-codex-sdk", "status": status}
    fake_gitlab.set_pipeline_jobs(pipeline_id, [job])
    return job_id


class TestStart:
    async def test_ensures_branch_and_creates_pipeline_with_run_variables(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        run = make_run()
        handle = await backend.start(run, "Add a widget", "widgets", "PLAN TEXT")
        data = json.loads(handle)
        branch = factory_branch(ISSUE_IID, run.id)

        assert data["branch"] == branch
        assert data["base_sha"] == BASE_SHA
        assert data["attempt_base"] == BASE_SHA  # cycle 1: the approved base
        assert data["harness"] == "claude-code"
        assert branch in fake_gitlab.branches  # ensure_branch ran
        (pipeline,) = fake_gitlab.pipelines
        assert pipeline["ref"] == branch
        assert data["pipeline_id"] == pipeline["id"]
        assert data["started_at"]  # durable deadline timestamp

        (entry,) = fake_gitlab.pipeline_variables
        by_key = {v["key"]: v["value"] for v in entry["variables"]}
        assert by_key == {
            "FORGE_RUN_ID": run.id,
            "FORGE_ISSUE_IID": str(ISSUE_IID),
            "FORGE_ISSUE_TITLE": "Add a widget",
            "FORGE_PLAN": "PLAN TEXT",
            "FORGE_HARNESS_MODEL": "glm-5.3-flash[1m]",
            "FORGE_HARNESS_DRIVER": "claude-code",  # ADR-0023 §7 template filter
            "FORGE_ATTEMPT_BASE": BASE_SHA,  # ADR-0016 §4: frozen attempt base
        }

    async def test_discovers_a_prefixed_lane_job(self, fake_gitlab, db):
        """LIVE-found: SDK-lane jobs are named forge-agent-<driver>; the
        discovery matches by PREFIX so all three lanes are found."""
        backend = make_backend(fake_gitlab, db)
        run = make_run()
        handle = await backend.start(run, "Add a widget", "widgets", "PLAN TEXT")
        data = json.loads(handle)
        seed_prefixed_lane_job(fake_gitlab, data["pipeline_id"], status="running")
        rediscovered = await backend._discover_job_id(PROJECT_ID, data["pipeline_id"])
        assert rediscovered is not None


class TestPoll:
    async def test_running_job_keeps_waiting(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        seed_job(fake_gitlab, json.loads(handle)["pipeline_id"], status="running")

        outcome = await backend.poll(run, handle)

        assert outcome.status == "running"
        assert not outcome.ok

    async def test_success_with_candidate_artifacts_is_change_candidate(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="success")
        usage = {"input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 5}
        seed_candidate(
            fake_gitlab,
            555,
            attempt_base=BASE_SHA,
            diff=create_diff("forge-demo/x.md", "hello\n"),
            usage=usage,
        )

        outcome = await backend.poll(run, handle)

        assert outcome.status == "change_candidate"
        assert outcome.ok
        bundle = outcome.bundle
        assert bundle.attempt_base_oid == BASE_SHA
        assert [entry.path for entry in bundle.entries] == ["forge-demo/x.md"]
        assert bundle.entries[0].new_content == "hello\n"
        assert bundle.usage.input_tokens == 11
        assert bundle.usage.completeness == "aggregate"

    async def test_success_without_artifacts_is_rejected(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="success", log="no artifacts at all")

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert outcome.reason == "harness_artifact_missing"

    async def test_empty_diff_is_harness_no_changes(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="success")
        seed_candidate(fake_gitlab, 555, attempt_base=BASE_SHA, diff="")

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert outcome.reason == "harness_no_changes"

    async def test_empty_diff_on_repair_is_repair_no_effect(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        run = make_run()
        run.commit_cycle = 2
        run.candidate_shas = ["candidate-9"]
        handle, run, _branch = await started(fake_gitlab, backend, run=run)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="success")
        seed_candidate(fake_gitlab, 555, attempt_base="candidate-9", diff="")

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert outcome.reason == "repair_no_effect"

    async def test_repair_attempt_base_is_last_candidate(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        run = make_run()
        run.commit_cycle = 2
        run.candidate_shas = ["candidate-9"]
        handle, run, _branch = await started(fake_gitlab, backend, run=run)
        assert json.loads(handle)["attempt_base"] == "candidate-9"

        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="success")
        seed_candidate(
            fake_gitlab,
            555,
            attempt_base="candidate-9",
            diff=create_diff("forge-demo/y.md", "repair\n"),
        )

        outcome = await backend.poll(run, handle)
        assert outcome.status == "change_candidate"

    async def test_artifact_base_mismatch_is_rejected(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="success")
        seed_candidate(fake_gitlab, 555, attempt_base="some-other-base", diff="")

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert "harness_attempt_base_mismatch" in outcome.reason

    async def test_binary_diff_is_rejected_as_invalid_candidate(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="success")
        seed_candidate(
            fake_gitlab,
            555,
            attempt_base=BASE_SHA,
            diff=(
                "diff --git a/img.png b/img.png\n"
                "index 123..456 100644\n"
                "GIT binary patch\n"
                "literal 10\n"
            ),
        )

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert "harness_candidate_invalid" in outcome.reason
        assert "binary_not_supported" in outcome.reason

    async def test_failed_driver_exit_is_never_adopted(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="success")
        seed_candidate(
            fake_gitlab,
            555,
            attempt_base=BASE_SHA,
            diff=create_diff("forge-demo/z.md", "partial\n"),
            exit="failed",
        )

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert "harness_driver_failed" in outcome.reason

    async def test_runner_system_failure_is_infrastructure(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(
            fake_gitlab,
            pipeline_id,
            status="failed",
            failure_reason="runner_system_failure",
        )

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"

    async def test_script_failure_without_patterns_is_code(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(
            fake_gitlab,
            pipeline_id,
            status="failed",
            failure_reason="script_failure",
            log="AssertionError: tests failed",
        )

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"

    async def test_auth_quota_trace_patterns_are_infrastructure(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(
            fake_gitlab,
            pipeline_id,
            status="failed",
            failure_reason="script_failure",
            log="Error: quota exceeded for project — check billing",
        )

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"

    async def test_canceled_job_is_infrastructure(self, fake_gitlab, db):
        backend = make_backend(fake_gitlab, db)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="canceled")

        outcome = await backend.poll(run, handle)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"
        assert "canceled" in outcome.reason

    async def test_timeout_past_durable_deadline(self, fake_gitlab, db):
        import datetime as dt

        backend = make_backend(fake_gitlab, db, FORGE_HARNESS_TIMEOUT_SECONDS=60)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="running")

        data = json.loads(handle)
        late = dt.datetime.fromisoformat(data["started_at"]) + dt.timedelta(seconds=61)
        outcome = await backend.poll(run, handle, now=late)

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"
        assert outcome.reason == "harness_timeout"

    async def test_before_deadline_keeps_waiting(self, fake_gitlab, db):
        import datetime as dt

        backend = make_backend(fake_gitlab, db, FORGE_HARNESS_TIMEOUT_SECONDS=3600)
        handle, run, _branch = await started(fake_gitlab, backend)
        pipeline_id = json.loads(handle)["pipeline_id"]
        seed_job(fake_gitlab, pipeline_id, status="pending")

        data = json.loads(handle)
        soon = dt.datetime.fromisoformat(data["started_at"]) + dt.timedelta(seconds=5)
        outcome = await backend.poll(run, handle, now=soon)

        assert outcome.status == "running"


class TestBuiltinBackend:
    async def _persisted_run(self, db, run: FlowRun) -> FlowRun:
        async with db() as session:
            session.add(run)
            await session.commit()
        return run

    async def test_start_commits_and_poll_reports_change_ready(self, fake_gitlab, db):
        backend = BuiltinBackend(
            implementer=StubImplementer(),
            gitlab=fake_gitlab,
            session_factory=db,
            settings=make_settings(),
        )
        run = await self._persisted_run(db, make_run())
        handle = await backend.start(run, "Add a widget", "", "")

        data = json.loads(handle)
        assert data["commit_sha"]
        assert data["branch"] == factory_branch(ISSUE_IID, run.id)
        # The commit really landed on the run's branch.
        assert fake_gitlab.branches[data["branch"]][0]["sha"] == data["commit_sha"]

        outcome = await backend.poll(run, handle)
        assert outcome.ok
        assert outcome.commit_sha == data["commit_sha"]

    async def test_changeset_violations_become_failed_outcome(self, fake_gitlab, db):
        class DeniedImplementer:
            async def propose(self, run, issue_title, **kwargs):
                return ChangeSet(
                    branch=factory_branch(ISSUE_IID, run.id),
                    commit_message="nope",
                    changes=[
                        Change(path=".gitlab-ci.yml", operation=Operation.CREATE, content="x")
                    ],
                )

        backend = BuiltinBackend(
            implementer=DeniedImplementer(),
            gitlab=fake_gitlab,
            session_factory=db,
            settings=make_settings(),
        )
        run = await self._persisted_run(db, make_run())
        handle = await backend.start(run, "Add a widget", "", "")

        outcome = await backend.poll(run, handle)
        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert "changeset_invalid" in outcome.reason
        assert fake_gitlab.calls_of("create_commit") == []  # never dispatched


class TestBuildBackend:
    def test_ci_harness_builds_cit_harness_backend(self, fake_gitlab, db):
        backend = build_backend(
            make_settings(FORGE_IMPLEMENTER_BACKEND="ci_harness"),
            gitlab=fake_gitlab,
            session_factory=db,
            writer=ChangesetWriter(fake_gitlab, db, PROJECT_ID),
        )
        assert isinstance(backend, CITharnessBackend)

    def test_builtin_without_implementer_is_a_config_error(self, fake_gitlab, db):
        with pytest.raises(ValueError, match="implementer"):
            build_backend(
                make_settings(FORGE_IMPLEMENTER_BACKEND="builtin"),
                gitlab=fake_gitlab,
                session_factory=db,
            )

    def test_builtin_with_implementer_builds_builtin_backend(self, fake_gitlab, db):
        backend = build_backend(
            make_settings(FORGE_IMPLEMENTER_BACKEND="builtin"),
            gitlab=fake_gitlab,
            session_factory=db,
            implementer=StubImplementer(),
        )
        assert isinstance(backend, BuiltinBackend)

    def test_unknown_backend_value_rejected(self, fake_gitlab, db):
        with pytest.raises(ValueError, match="FORGE_IMPLEMENTER_BACKEND"):
            build_backend(
                make_settings(FORGE_IMPLEMENTER_BACKEND="skynet"),
                gitlab=fake_gitlab,
                session_factory=db,
            )

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("builtin", False),
            ("ci_harness", True),
            ("ci_harness:claude-code", True),
            (None, False),
            ("", False),
        ],
    )
    def test_is_harness_backend(self, name, expected):
        assert is_harness_backend(name) is expected


# ----------------------------------------------------------------------
# C05: the backend executes the APPROVED shape, never live defaults
# ----------------------------------------------------------------------


class TestBackendStartSpecC05:
    async def test_the_spec_overrides_live_settings(self, db, fake_gitlab):
        """Model + target come from the BackendStartSpec (frozen RunSpec)
        even when live settings say otherwise — a post-approval settings
        drift cannot move the dispatch."""
        from forge.runs.backends import CITharnessBackend, BackendStartSpec

        writer = ChangesetWriter(fake_gitlab, db, PROJECT_ID)
        backend = CITharnessBackend(gitlab=fake_gitlab, writer=writer, settings=object())
        run_like = SimpleNamespace(
            id="a" * 32, issue_iid=7, project_id=PROJECT_ID, base_sha="b" * 40
        )

        await backend.start(
            run_like,
            "title",
            "",
            "plan",
            spec=BackendStartSpec(
                model="frozen-model",
                target_branch="frozen-target",
                driver="claude-code",
                attempt_base="b" * 40,
            ),
        )

        (pv,) = fake_gitlab.pipeline_variables
        model_var = next(v for v in pv["variables"] if v["key"] == "FORGE_HARNESS_MODEL")
        assert model_var["value"] == "frozen-model"
        # D05: the factory branch is cut from the ATTEMPT OID (the frozen
        # snapshot), not the target name
        (branch_call,) = fake_gitlab.calls_of("create_branch")
        assert branch_call[1][2] == "b" * 40

    async def test_legacy_caller_keeps_the_settings_fallback(self, db, fake_gitlab):
        pass

    async def test_a_moved_target_never_moves_the_branch_base(self, db, fake_gitlab):
        """D05 (probe P07): approval was for SHA-A, main moved to SHA-B —
        the factory branch is cut at A (the attempt OID), the diff base."""
        from forge.runs.backends import CITharnessBackend, BackendStartSpec

        writer = ChangesetWriter(fake_gitlab, db, PROJECT_ID)
        backend = CITharnessBackend(gitlab=fake_gitlab, writer=writer, settings=object())
        run_like = SimpleNamespace(
            id="d" * 32, issue_iid=7, project_id=PROJECT_ID, base_sha="a" * 40
        )

        await backend.start(
            run_like,
            "t",
            "",
            "p",
            spec=BackendStartSpec(
                model="m",
                target_branch="main",
                driver="claude-code",
                attempt_base="a" * 40,
            ),
        )

        branch_calls = fake_gitlab.calls_of("create_branch")
        assert branch_calls[-1][1][2] == "a" * 40  # the attempt OID, not "main"

        class Settings:
            FORGE_TARGET_BRANCH = "live-target"
            FORGE_HARNESS_MODEL = "live-model"

        writer = ChangesetWriter(fake_gitlab, db, PROJECT_ID)
        backend = CITharnessBackend(gitlab=fake_gitlab, writer=writer, settings=Settings())
        run_like = SimpleNamespace(
            id="c" * 32, issue_iid=7, project_id=PROJECT_ID, base_sha="b" * 40
        )

        await backend.start(run_like, "t", "", "p")

        pv = fake_gitlab.pipeline_variables[-1]
        model_var = next(v for v in pv["variables"] if v["key"] == "FORGE_HARNESS_MODEL")
        assert model_var["value"] == "live-model"
