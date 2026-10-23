"""GitHub/GitLab publication and CI parity on the shipped lane-only paths.

Each parameter drives actual provider services, native publication and
verification over transport fakes. No capability claims or attestation
interpreter stands between the assertions and their production owners.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import FlowRun, FlowStatus, RunSpec
from forge.integrations.github_flow import GitHubPublishFlow, github_factory_branch
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.repository.changeset import Change, ChangeSet, Operation
from forge.runs.candidate import bundle_from_changeset

from forge.runs.publisher import publish_candidate, publication_grant_valid
from forge.runs.stubs import factory_branch
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_github_harness import (
    go as github_go,
    make_service as make_github_service,
    make_settings as make_github_settings,
    make_stack as make_github_stack,
    seed_success_with_candidate as seed_github_candidate,
    start as github_start,
)
from tests.test_runs_harness_service import (
    make_service as make_gitlab_service,
    make_settings as make_gitlab_settings,
    seed_success_with_candidate as seed_gitlab_candidate,
)

# ----------------------------------------------------------------------
# Shared subject constants (one per lane, mirrors the provider suites)
# ----------------------------------------------------------------------

BASE = "1" * 40
MOVED = "f" * 40
ISSUE_TITLE = "Add password reset"
ISSUE_DESC = "Users cannot reset their password."

GL_PROJECT_ID = 42
GL_ISSUE = 7
GH_REPO = "acme/acme-widget"
GH_OWNER, GH_REPO_NAME = GH_REPO.split("/", 1)
GH_PROJECT_ID = 70010
GH_ISSUE = 42

RUN_ID = "abcd1234" + "0" * 24

#: The commit-API mutation each lane's fake records when a write is attempted.
MUTATION_CALL = {
    "gitlab": "create_commit",
    "github": "create_commit_on_branch",
}


@dataclass(frozen=True)
class Verdict:
    """The normalized publish-leg verdict the kit reads."""

    ok: bool
    reason: str = ""
    drift: bool = False


@dataclass(frozen=True)
class BadCandidate:
    """One R01 boundary shape: (name, changeset, refusal fragment)."""

    name: str
    changeset: ChangeSet
    fragment: str


def _change(path: str, content: str = "x\n", *, operation: Operation = Operation.CREATE) -> Change:
    return Change(path=path, operation=operation, content=content)


def _cs(*changes: Change) -> ChangeSet:
    return ChangeSet(
        branch="forge/kit/placeholder",
        commit_message="forge: implement (conformance kit)",
        changes=list(changes),
    )


#: The shared negative matrix — the R01 boundary scenarios every lane must
#: refuse with zero commit-API calls (fragments match the policy messages).
def bad_candidates() -> tuple[BadCandidate, ...]:
    return (
        BadCandidate(
            "denylisted_ci_config",
            _cs(_change(".gitlab-ci.yml", "rogue: true\n")),
            "denylisted",
        ),
        BadCandidate(
            "lockfile",
            _cs(_change("package-lock.json", "{}\n")),
            "lockfiles are denylisted",
        ),
        BadCandidate(
            "path_traversal",
            _cs(_change("../escape.py")),
            "path traversal",
        ),
        BadCandidate(
            "oversized_content",
            _cs(_change("big.txt", "x" * (256 * 1024 + 1))),
            "bytes",
        ),
        BadCandidate(
            "duplicate_path",
            _cs(_change("dup/report.md", "one\n"), _change("./dup//report.md", "two\n")),
            "duplicate",
        ),
    )


GOOD_MANIFEST = (
    _change("src/feature.py", "VALUE = 1\n"),
    _change("src/app.py", "print('hello')\n", operation=Operation.UPDATE),
)

OUT_OF_SCOPE = _change("web/components/x.ts", "hi\n")


def good_changeset() -> ChangeSet:
    return _cs(*GOOD_MANIFEST)


# ----------------------------------------------------------------------
# Shared fixtures / row helpers
# ----------------------------------------------------------------------


@asynccontextmanager
async def _fresh_db():
    """One pristine in-memory database (a scenario's whole world)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture()
async def db():
    async with _fresh_db() as session_factory:
        yield session_factory


async def update_run(db, run_id: str, **fields: Any) -> None:
    """Trusted-state tampering the scenarios need (digest / cycle / grant)."""
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        for key, value in fields.items():
            setattr(run, key, value)
        await session.commit()


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


# ----------------------------------------------------------------------
# Lane adapters — one per provider; the kit below is provider-blind.
# Every lane drives its REAL service/transport legs over its fake.
# ----------------------------------------------------------------------


class GitLabLane:
    provider = "gitlab"
    project_id = GL_PROJECT_ID
    issue = GL_ISSUE

    def __init__(self, db, fake: FakeGitLab, service: Any) -> None:
        self.db = db
        self.fake = fake
        self.service = service

    @classmethod
    async def build(cls, db, *, required_jobs="") -> GitLabLane:
        fake = FakeGitLab()
        fake.seed_commit("main", BASE, "initial")
        fake.seed_file("src/app.py", "print('hi')\n")
        fake.seed_issue(GL_ISSUE, ISSUE_TITLE, ISSUE_DESC)
        fake.ci_configuration = {"valid": True, "jobs": [{"name": "forge-agent"}]}
        service = make_gitlab_service(
            db, fake, settings=make_gitlab_settings(FORGE_REQUIRED_JOBS=required_jobs)
        )
        return cls(db, fake, service)

    async def start_run(self) -> str:
        return await self.service.start_run(
            self.project_id, self.issue, ISSUE_TITLE, ISSUE_DESC, "alice"
        )

    async def approve(self, run_id: str) -> None:
        await self.service.handle_command_note(
            self.project_id, f"@forge /go {run_id}", "alice", self.issue, author_user_id=11
        )

    def transport_branch(self, run_id: str) -> str:
        return factory_branch(self.issue, run_id)

    async def publish_transport(
        self, changeset: ChangeSet, *, attempt_base: str, scope: list[str] | None = None
    ) -> Verdict:
        """The ADR-0026 core publisher — the lane's only candidate entry."""
        run = FlowRun(
            id=RUN_ID, project_id=self.project_id, issue_iid=self.issue, base_sha=attempt_base
        )
        async with self.db() as session:
            session.add(run)
            if scope:
                session.add(
                    RunSpec(run_id=run.id, document={"allowed_paths": scope}, digest="kit-digest")
                )
            await session.commit()
        writer = ChangesetWriter(self.fake, self.db, self.project_id)
        result = await publish_candidate(
            gitlab=self.fake,
            session_factory=self.db,
            writer=writer,
            run=run,
            bundle=bundle_from_changeset(changeset, attempt_base_oid=attempt_base),
        )
        return Verdict(ok=result.ok, reason=result.reason, drift="branch_drift" in result.reason)

    # The harness candidate crosses the same trusted publisher boundary.
    publish_candidate = publish_transport

    def write_attempts(self) -> int:
        return len(self.fake.calls_of(MUTATION_CALL[self.provider]))

    def service_writes(self) -> int:
        return self.write_attempts()

    def state(self) -> tuple:
        branches = tuple(
            (name, tuple(c["sha"] for c in commits)) for name, commits in self.fake.branches.items()
        )
        return (branches, tuple(sorted(self.fake.files.items())))

    def head(self, branch: str) -> str:
        commits = self.fake.branches.get(branch) or []
        return commits[0]["sha"] if commits else ""

    def review_surfaces(self) -> int:
        return len(self.fake.merge_requests)

    def review_surfaces_are_draft(self) -> bool:
        return True  # the synthetic Draft MR leg only ever creates drafts

    async def complete_harness(self, run_id: str) -> None:
        run = await get_run(self.db, run_id)
        pipeline_id = int(run.evidence["harness"]["pipeline_id"])
        seed_gitlab_candidate(self.fake, pipeline_id, attempt_base=BASE)
        await self.service.evaluate_waiting_harness()

    async def evaluate_ci(self, run_id: str) -> None:
        await self.service.evaluate_waiting_ci()

    def seed_candidate(self, run_id: str, sha: str) -> None:
        self.fake.seed_commit(self.transport_branch(run_id), sha, "forge commit")


class GitHubLane:
    provider = "github"
    project_id = GH_PROJECT_ID
    issue = GH_ISSUE

    def __init__(self, db, fake: FakeGitHub, service: Any, flow: GitHubPublishFlow) -> None:
        self.db = db
        self.fake = fake
        self.service = service
        self.flow = flow

    @classmethod
    async def build(cls, db, *, required_jobs="") -> GitHubLane:
        fake = FakeGitHub()
        fake.seed_repo(
            GH_REPO,
            {
                "src/app.py": "print('hi')\n",
                ".github/workflows/forge-harness.github.yml": "on: workflow_dispatch\njobs:\n  lane:\n    runs-on: ubuntu-latest\n",
            },
        )
        fake.heads[GH_REPO]["main"] = BASE
        fake.seed_issue(GH_REPO, GH_ISSUE, ISSUE_TITLE, ISSUE_DESC)
        service = make_github_service(
            db,
            fake,
            settings=make_github_settings(FORGE_REQUIRED_JOBS=required_jobs),
            stack=make_github_stack(fake),
        )
        return cls(db, fake, service, service._stack.flow)

    async def start_run(self) -> str:
        return await github_start(self.service)

    async def approve(self, run_id: str) -> None:
        await github_go(self.service, run_id)

    def transport_branch(self, run_id: str) -> str:
        return github_factory_branch(self.issue, run_id)

    async def publish_transport(
        self, changeset: ChangeSet, *, attempt_base: str, scope: list[str] | None = None
    ) -> Verdict:
        """The transport entry — the ADR-0026 boundary validates inside."""
        outcome = await self.flow.publish_changeset(
            GH_OWNER,
            GH_REPO_NAME,
            issue_number=self.issue,
            run_id=RUN_ID,
            changeset=changeset,
            base_branch="main",
            expected_head=attempt_base,
            allowed_paths=scope,
        )
        return Verdict(ok=outcome.ok, reason=outcome.reason, drift=outcome.drift)

    # On GitHub the transport entry re-runs every candidate through the
    # boundary (R01) — one entry serves both kit roles.
    publish_candidate = publish_transport

    def write_attempts(self) -> int:
        return len(self.fake.calls_of(MUTATION_CALL[self.provider]))

    def service_writes(self) -> int:
        return self.write_attempts()

    def state(self) -> tuple:
        return (
            tuple(sorted(self.fake.heads[GH_REPO].items())),
            tuple(sorted(self.fake.files.get(GH_REPO, {}).items())),
        )

    def head(self, branch: str) -> str:
        return self.fake.heads[GH_REPO].get(branch, "")

    def review_surfaces(self) -> int:
        return len(self.fake.pull_requests.get(GH_REPO, []))

    def review_surfaces_are_draft(self) -> bool:
        return all(pr["draft"] is True for pr in self.fake.pull_requests.get(GH_REPO, []))

    async def complete_harness(self, run_id: str) -> None:
        seed_github_candidate(self.fake, run_id)
        await self.service.evaluate_waiting_harness()

    async def evaluate_ci(self, run_id: str) -> None:
        await self.service.evaluate_waiting_ci_one(run_id)

    def seed_candidate(self, run_id: str, sha: str) -> None:
        self.fake.seed_commit(GH_REPO, self.transport_branch(run_id), sha, "forge commit")


LANES: tuple[type, ...] = (GitLabLane, GitHubLane)


def _assert_untouched(lane, before: tuple) -> None:
    assert lane.state() == before, "a refusal scenario must leave zero remote writes"


# ----------------------------------------------------------------------
# The scenarios — one implementation, every provider
# ----------------------------------------------------------------------


async def scenario_positive_control(lane) -> str:
    """The lane publishes EXACTLY the validated manifest."""
    branch = lane.transport_branch(RUN_ID)
    before = lane.state()
    original_files = dict(lane.fake.files[GH_REPO]) if lane.provider == "github" else {}
    verdict = await lane.publish_transport(good_changeset(), attempt_base=BASE)

    assert verdict.ok is True, verdict.reason
    assert lane.state() != before

    # Exactly the validated manifest — no more, no less (per lane surface).
    if lane.provider == "gitlab":
        (commit_call,) = lane.fake.calls_of(MUTATION_CALL[lane.provider])
        actions = commit_call[1][2]
        assert [(a["action"], a["file_path"]) for a in actions] == [
            ("create" if change.operation is Operation.CREATE else "update", change.path)
            for change in GOOD_MANIFEST
        ]
        assert [action["content"] for action in actions] == [
            change.content for change in GOOD_MANIFEST
        ]
    elif lane.provider == "github":
        files = lane.fake.files[GH_REPO]
        assert files["src/feature.py"] == "VALUE = 1\n"
        assert files["src/app.py"] == "print('hello')\n"
        workflow = ".github/workflows/forge-harness.github.yml"
        assert files[workflow] == original_files[workflow]
        assert set(files) == {
            "src/app.py",
            "src/feature.py",
            ".github/workflows/forge-harness.github.yml",
        }
    if lane.provider == "gitlab":
        assert lane.review_surfaces() == 0  # RunService owns its separate Draft MR leg.
    else:
        assert lane.review_surfaces() == 1
        assert lane.review_surfaces_are_draft() is True
    return branch


async def scenario_bad_candidate(lane, candidate: BadCandidate) -> None:
    """A policy-violating candidate is refused with zero commit-API calls."""
    before = lane.state()
    attempts_before = lane.write_attempts()

    verdict = await lane.publish_candidate(candidate.changeset, attempt_base=BASE)

    assert verdict.ok is False, candidate.name
    assert candidate.fragment in verdict.reason, (candidate.name, verdict.reason)
    _assert_untouched(lane, before)
    assert lane.write_attempts() == attempts_before, candidate.name


async def scenario_out_of_scope(lane) -> None:
    """The frozen allowed_paths scope refuses an out-of-scope candidate."""
    verdict = await lane.publish_candidate(
        _cs(OUT_OF_SCOPE), attempt_base=BASE, scope=["services/**"]
    )
    assert verdict.ok is False
    assert "outside the allowed scope" in verdict.reason


async def scenario_stale_head(lane) -> None:
    """A stale head preserves the provider's native CAS semantics."""
    lane.seed_candidate(RUN_ID, MOVED)  # a concurrent writer moved the branch
    before = lane.state()
    attempts_before = lane.write_attempts()

    verdict = await lane.publish_transport(good_changeset(), attempt_base=BASE)

    assert verdict.ok is False
    assert verdict.drift is True
    assert "branch_drift" in verdict.reason
    _assert_untouched(lane, before)

    if lane.provider == "github":
        assert lane.write_attempts() == attempts_before + 1  # native expectedHeadOid refusal
    else:
        assert lane.write_attempts() == attempts_before  # GitLab has no native CAS.


async def scenario_missing_spec(lane) -> None:
    """A run whose approved spec digest is gone/altered never publishes."""
    run_id = await lane.start_run()
    assert (await get_run(lane.db, run_id)).spec_digest
    writes_before = lane.service_writes()
    await update_run(lane.db, run_id, spec_digest="0" * 64)  # the spec is gone/altered

    await lane.approve(run_id)

    run = await get_run(lane.db, run_id)
    assert run.candidate_shas in (None, []), "a spec-less run must never publish"
    assert lane.service_writes() == writes_before
    assert run.status not in (FlowStatus.WAITING_CI.value, FlowStatus.READY_FOR_HUMAN.value)


async def scenario_cancelled_generation(lane) -> None:
    """A revoked publication grant (F13/R10) stands the publish leg down."""
    run_id = await lane.start_run()
    writes_before = lane.service_writes()
    await update_run(lane.db, run_id, cancel_requested=True)

    await lane.approve(run_id)

    run = await get_run(lane.db, run_id)
    assert run.candidate_shas in (None, []), "a cancelled run must never publish"
    assert lane.service_writes() == writes_before
    assert run.status not in (FlowStatus.WAITING_CI.value, FlowStatus.READY_FOR_HUMAN.value)


async def drive_to_waiting_ci(lane) -> tuple[str, str]:
    """Approve, dispatch current CI, ingest its artifact, then wait for verification."""
    run_id = await lane.start_run()
    await lane.approve(run_id)
    run = await get_run(lane.db, run_id)
    assert run.status == FlowStatus.WAITING_HARNESS.value
    await lane.complete_harness(run_id)
    run = await get_run(lane.db, run_id)
    assert run.status == FlowStatus.WAITING_CI.value
    candidate = run.candidate_shas[-1]
    return run_id, candidate


async def scenario_failed_ci_never_ready(lane) -> None:
    """Failed CI is never verified-ready: no ready, no passed verdict (R02)."""
    run_id, candidate = await drive_to_waiting_ci(lane)
    # Exhaust the repair budget first: a red verdict then blocks instead of
    # entering a repair cycle — deterministic on every lane.
    await update_run(lane.db, run_id, commit_cycle=3)
    if lane.provider == "gitlab":
        pipeline_id = (
            await lane.fake.create_pipeline(GL_PROJECT_ID, lane.transport_branch(run_id))
        )["id"]
        lane.fake.set_pipeline_status(pipeline_id, "failed", candidate)
    elif lane.provider == "github":
        lane.fake.seed_workflow_runs(
            [
                {
                    "head_sha": candidate,
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ]
        )
    writes_before = lane.service_writes()

    await lane.evaluate_ci(run_id)

    run = await get_run(lane.db, run_id)
    assert run.status != FlowStatus.READY_FOR_HUMAN.value
    assert "checks passed" not in (run.status_reason or "")
    verification = (run.evidence or {}).get("verification") or {}
    assert verification.get("status") != "passed"
    assert lane.service_writes() == writes_before  # red CI publishes nothing new


async def scenario_unverified_honest(lane) -> None:
    """No verification configured → ready, honestly labeled, never green."""
    run_id, candidate = await drive_to_waiting_ci(lane)
    if lane.provider == "gitlab":
        # A green pipeline under an EMPTY verification profile is pipeline
        # success only — R02 labels it unverified.
        pipeline_id = (
            await lane.fake.create_pipeline(GL_PROJECT_ID, lane.transport_branch(run_id))
        )["id"]
        lane.fake.set_pipeline_status(pipeline_id, "success", candidate)
    # No independent GitHub checks exist; the lane run is not verification.

    await lane.evaluate_ci(run_id)

    run = await get_run(lane.db, run_id)
    assert run.status == FlowStatus.READY_FOR_HUMAN.value
    reason = run.status_reason or ""
    assert reason.startswith("unverified — ")
    assert "checks passed" not in reason
    verification = (run.evidence or {}).get("verification") or {}
    assert verification.get("status") in {"unverified", "not_configured"}
    assert verification.get("producer"), "the verdict must name WHO claimed it"


@pytest.mark.parametrize("lane_cls", LANES, ids=lambda lane_cls: lane_cls.provider)
class TestConformanceKit:
    async def test_positive_control_publishes_exactly_the_validated_manifest(self, db, lane_cls):
        lane = await lane_cls.build(db)
        await scenario_positive_control(lane)

    @pytest.mark.parametrize(
        "candidate",
        bad_candidates(),
        ids=lambda candidate: candidate.name,
    )
    async def test_bad_candidate_refused_with_zero_writes(self, db, lane_cls, candidate):
        lane = await lane_cls.build(db)
        await scenario_bad_candidate(lane, candidate)

    async def test_out_of_scope_candidate_refused(self, db, lane_cls):
        lane = await lane_cls.build(db)
        await scenario_out_of_scope(lane)

    async def test_stale_head_drifts_with_zero_writes(self, db, lane_cls):
        lane = await lane_cls.build(db)
        await scenario_stale_head(lane)

    async def test_missing_spec_refused(self, db, lane_cls):
        lane = await lane_cls.build(db)
        await scenario_missing_spec(lane)

    async def test_cancelled_generation_never_publishes(self, db, lane_cls):
        lane = await lane_cls.build(db)
        await scenario_cancelled_generation(lane)

    async def test_failed_ci_never_verified_ready(self, db, lane_cls):
        lane = await lane_cls.build(db)
        await scenario_failed_ci_never_ready(lane)

    async def test_unverified_run_honestly_labeled(self, db, lane_cls):
        lane = await lane_cls.build(db)
        await scenario_unverified_honest(lane)


# ----------------------------------------------------------------------
# Core pins the kit leans on (the grant fence the services share)
# ----------------------------------------------------------------------


class TestPublicationGrantFence:
    def test_cancel_flag_revokes_the_grant(self):
        run = FlowRun(id=RUN_ID, project_id=1, issue_iid=1, cancel_requested=True)
        assert publication_grant_valid(run, None) is False

    def test_generation_mismatch_revokes_the_grant(self):
        """R10: queue ownership without effect ownership — a claim minted
        before a cancel bumped the generation holds no grant."""
        run = FlowRun(id=RUN_ID, project_id=1, issue_iid=1, cancellation_generation=2)
        assert publication_grant_valid(run, 1) is False
        assert publication_grant_valid(run, 2) is True

    def test_cancelled_status_holds_no_grant_even_without_the_flag(self):
        run = FlowRun(
            id=RUN_ID,
            project_id=1,
            issue_iid=1,
            status=FlowStatus.CANCELLED.value,
        )
        assert publication_grant_valid(run, None) is False


@pytest.mark.parametrize("lane_cls", LANES, ids=lambda lane_cls: lane_cls.provider)
async def test_required_ci_absence_never_becomes_verified(db, lane_cls):
    lane = await lane_cls.build(db, required_jobs="tests")
    run_id, candidate = await drive_to_waiting_ci(lane)
    writes = lane.service_writes()
    if lane.provider == "gitlab":
        pipeline_id = (
            await lane.fake.create_pipeline(GL_PROJECT_ID, lane.transport_branch(run_id))
        )["id"]
        lane.fake.set_pipeline_status(pipeline_id, "success", candidate)
        lane.fake.set_pipeline_jobs(pipeline_id, [{"id": 900, "name": "docs", "status": "success"}])
    else:
        lane.fake.seed_workflow_runs(
            [
                {
                    "head_sha": candidate,
                    "name": "docs",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        )
    await lane.evaluate_ci(run_id)
    waiting = await get_run(db, run_id)
    if lane.provider == "gitlab":
        assert waiting.status == FlowStatus.BLOCKED.value
        assert "quality_contract" in waiting.status_reason
        assert (waiting.evidence.get("verification") or {}).get("status") != "passed"
        assert lane.service_writes() == writes
        return
    assert waiting.status == FlowStatus.WAITING_CI.value
    assert waiting.evidence["verification"]["status"] == "unknown"
    assert lane.service_writes() == writes
    if lane.provider == "gitlab":
        lane.fake.set_pipeline_jobs(
            pipeline_id,
            [
                {"id": 900, "name": "docs", "status": "success"},
                {"id": 901, "name": "tests", "status": "success"},
            ],
        )
    else:
        lane.fake.seed_workflow_runs(
            [
                {
                    "head_sha": candidate,
                    "name": name,
                    "status": "completed",
                    "conclusion": "success",
                }
                for name in ("docs", "tests")
            ]
        )
    await lane.evaluate_ci(run_id)
    ready = await get_run(db, run_id)
    assert ready.status == FlowStatus.READY_FOR_HUMAN.value
    assert ready.evidence["verification"]["status"] == "passed"
    assert ready.evidence["verification"]["tested_oid"] == candidate
    assert lane.service_writes() == writes


async def test_same_plan_comment_id_with_changed_approved_bytes_is_refused(monkeypatch):
    from tests import test_github_runs as owner

    original = owner._composed_db

    async def current_lane_db():
        db, fake = await original()
        fake.files[GH_REPO][".github/workflows/forge-harness.github.yml"] = (
            "on: workflow_dispatch\njobs:\n  lane:\n    runs-on: ubuntu-latest\n"
        )
        return db, fake

    monkeypatch.setattr(owner, "_composed_db", current_lane_db)
    try:
        await owner.composed_a03_comment_same_id_edited_body(monkeypatch)
    finally:
        while owner._composed_db_engines:
            await owner._composed_db_engines.pop().dispose()


@pytest.mark.parametrize("lane_cls", LANES, ids=lambda lane_cls: lane_cls.provider)
async def test_required_ci_success_is_bound_to_the_current_candidate(db, lane_cls):
    lane = await lane_cls.build(db, required_jobs="tests")
    run_id, candidate = await drive_to_waiting_ci(lane)
    writes = lane.service_writes()
    if lane.provider == "gitlab":
        pipeline_id = (
            await lane.fake.create_pipeline(GL_PROJECT_ID, lane.transport_branch(run_id))
        )["id"]
        lane.fake.set_pipeline_status(pipeline_id, "success", candidate)
        lane.fake.set_pipeline_jobs(
            pipeline_id, [{"id": 901, "name": "tests", "status": "success"}]
        )
    else:
        lane.fake.seed_workflow_runs(
            [
                {
                    "head_sha": candidate,
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        )
    await lane.evaluate_ci(run_id)
    ready = await get_run(db, run_id)
    assert ready.status == FlowStatus.READY_FOR_HUMAN.value
    assert ready.evidence["verification"]["status"] == "passed"
    assert ready.evidence["verification"]["tested_oid"] == candidate
    assert lane.service_writes() == writes
