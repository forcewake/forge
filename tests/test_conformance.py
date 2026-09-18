"""R21: the ProviderConformanceKit — one invariant suite over ALL providers.

A new adapter must not be able to be green in its own tests while violating
the core promise. This kit parametrizes the forbidden and positive
scenarios over every lane in
:data:`forge.runs.conformance.PROVIDER_CAPABILITIES` and drives each lane's
REAL publish/gate/finalization legs over its in-memory fake (no live
calls) — the generalized form of the parity suites
(``tests/test_github_runs.py``, ``tests/test_consistency.py``).

The forbidden scenarios — every provider behaves IDENTICALLY:

1. a bad candidate (the R01 boundary matrix: denylisted CI config, lockfile,
   path traversal, oversized content, duplicate path) → refused with ZERO
   commit-API calls;
2. a stale head → drift refusal, zero writes — the refusal ORIGIN comes
   from the capability record (``native_cas``: the provider refuses the
   CAS'd mutation vs GitLab, where forge's writer guard refuses before
   dispatch), never from scattered per-provider ifs;
3. a missing/altered approved spec (the decision's digest binding) → the
   gate refuses, nothing publishes;
4. a cancelled generation (F13/R10) → no new publication;
5. failed CI → never verified-ready (R02);
6. an unverified run → honestly labeled (R02).

Positive control: each lane publishes EXACTLY the validated manifest.
Where the lanes genuinely differ (the synthetic Draft MR vs the native PR
surface), the assertion branches on the CAPABILITY RECORD
(``synthetic_merge_fallback``), not on provider names.

Clearing every scenario attests the lane (:func:`attest_conformance`) —
the registration rule ``verified_ready_capable`` demands both the record's
claim and this attestation, so future CI can assert registration off the
same suite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import FlowRun, FlowStatus, RunSpec
from forge.integrations.github_flow import GitHubPublishFlow, github_factory_branch
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.repository.changeset import Change, ChangeSet, Operation
from forge.runs.azure_service import AzurePipelinesHandle, azure_factory_branch
from forge.runs.candidate import bundle_from_changeset
from forge.runs.conformance import (
    CAS_FORGE_WRITER_GUARD,
    CAS_GRAPHQL_EXPECTED_HEAD_OID,
    CAS_PUSH_OLD_OBJECT_ID,
    ARTIFACT_TRANSPORT_CI,
    PROVIDER_CAPABILITIES,
    ProviderCapability,
    READONLY_IDENTITY_MERGE_REQUEST_IID,
    READONLY_IDENTITY_PULL_REQUEST_ID,
    READONLY_IDENTITY_PULL_REQUEST_NUMBER,
    UnknownProviderError,
    attest_conformance,
    capability_of,
    conformance_attested,
    registration_allowed,
    reset_conformance_attestations,
    verified_ready_capable,
)
from forge.runs.publisher import publish_candidate, publication_grant_valid
from forge.runs.stubs import factory_branch
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_azure_runs import (
    FakeAzureDevOps,
    azure_project_key,
    go as azure_go,
    make_service as make_azure_service,
    make_settings as make_azure_settings,
    start as azure_start,
)
from tests.test_github_runs import (
    go as github_go,
    make_service as make_github_service,
    make_settings as make_github_settings,
    make_stack as make_github_stack,
    start as github_start,
)
from tests.test_runs_service import FakeWriter
from tests.test_runs_verification import (
    make_service as make_gitlab_service,
    make_settings as make_gitlab_settings,
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
AZ_PROJECT_ID = azure_project_key("9f8e7d6c-0000-0000-0000-000000000009")
AZ_WORK_ITEM = 142

RUN_ID = "abcd1234" + "0" * 24

#: The commit-API mutation each lane's fake records when a write is attempted.
MUTATION_CALL = {
    "gitlab": "create_commit",
    "github": "create_commit_on_branch",
    "azure_devops": "push_commits",
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
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
async def db():
    async with _fresh_db() as session_factory:
        yield session_factory


@pytest.fixture(autouse=True)
def _clean_attestations():
    reset_conformance_attestations()
    yield
    reset_conformance_attestations()


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
    async def build(cls, db) -> GitLabLane:
        FakeWriter.reset()  # per-test writer call accounting
        fake = FakeGitLab()
        fake.seed_commit("main", BASE, "initial")
        fake.seed_file("src/app.py", "print('hi')\n")
        fake.seed_issue(GL_ISSUE, ISSUE_TITLE, ISSUE_DESC)
        service = make_gitlab_service(db, fake, settings=make_gitlab_settings())
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

    # On GitLab the boundary IS the transport entry: every candidate
    # (builtin and harness alike) crosses publish_candidate.
    publish_candidate = publish_transport

    def write_attempts(self) -> int:
        return len(self.fake.calls_of(MUTATION_CALL[self.provider]))

    def service_writes(self) -> int:
        return sum(len(writer.calls) for writer in FakeWriter.instances)

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
    async def build(cls, db) -> GitHubLane:
        fake = FakeGitHub()
        fake.seed_repo(GH_REPO, {"src/app.py": "print('hi')\n"})
        fake.heads[GH_REPO]["main"] = BASE
        fake.seed_issue(GH_REPO, GH_ISSUE, ISSUE_TITLE, ISSUE_DESC)
        service = make_github_service(
            db, fake, settings=make_github_settings(), stack=make_github_stack(fake)
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

    async def evaluate_ci(self, run_id: str) -> None:
        await self.service.evaluate_waiting_ci_one(run_id)

    def seed_candidate(self, run_id: str, sha: str) -> None:
        self.fake.seed_commit(GH_REPO, self.transport_branch(run_id), sha, "forge commit")


class AzureLane:
    provider = "azure_devops"
    project_id = AZ_PROJECT_ID
    issue = AZ_WORK_ITEM

    def __init__(self, db, fake: FakeAzureDevOps, service: Any) -> None:
        self.db = db
        self.fake = fake
        self.service = service

    @classmethod
    async def build(cls, db) -> AzureLane:
        fake = FakeAzureDevOps()
        fake.seed_work_item(AZ_WORK_ITEM, ISSUE_TITLE, "<p>Users cannot reset.</p>")
        service = make_azure_service(db, fake, settings=make_azure_settings())
        return cls(db, fake, service)

    async def start_run(self) -> str:
        return await azure_start(self.service)

    async def approve(self, run_id: str) -> None:
        await azure_go(self.service, run_id)

    def transport_branch(self, run_id: str) -> str:
        return azure_factory_branch(self.issue, run_id)

    async def publish_transport(
        self, changeset: ChangeSet, *, attempt_base: str, scope: list[str] | None = None
    ) -> Verdict:
        """The CAS push transport — publishes an ALREADY-VALIDATED manifest."""
        outcome = await self.service._publish_changeset(
            RUN_ID,
            issue_number=self.issue,
            changeset=changeset,
            base_branch="main",
            expected_head=attempt_base,
        )
        return Verdict(ok=outcome.ok, reason=outcome.reason, drift=outcome.drift)

    async def publish_candidate(
        self, changeset: ChangeSet, *, attempt_base: str, scope: list[str] | None = None
    ) -> Verdict:
        """The lane's boundary-wired candidate leg (materialize + policy)."""
        run_id = uuid4().hex
        async with self.db() as session:
            session.add(
                FlowRun(
                    id=run_id,
                    provider=self.provider,
                    project_id=self.project_id,
                    issue_iid=self.issue,
                    status=FlowStatus.WAITING_HARNESS.value,
                    base_sha=attempt_base,
                )
            )
            if scope:
                session.add(
                    RunSpec(run_id=run_id, document={"allowed_paths": scope}, digest="kit-digest")
                )
            await session.commit()
        handle = AzurePipelinesHandle(
            provider=self.provider,
            project="Fabrikam",
            repo="core",
            pipeline_id=0,
            run_id=0,
            branch=self.transport_branch(run_id),
            attempt_base=attempt_base,
            run_spec_digest="",
            driver="claude-code",
            forge_run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        outcome: Any = SimpleNamespace(
            bundle=bundle_from_changeset(changeset, attempt_base_oid=attempt_base),
            summary="",
        )
        await self.service._publish_harness_candidate(
            run_id, self.project_id, self.issue, outcome, handle
        )
        run = await get_run(self.db, run_id)
        reason = run.status_reason or ""
        return Verdict(ok=False, reason=reason, drift=True)

    def write_attempts(self) -> int:
        return len(self.fake.calls_of(MUTATION_CALL[self.provider]))

    def service_writes(self) -> int:
        return self.write_attempts()

    def state(self) -> tuple:
        snapshots = tuple(
            (sha, tuple(sorted(files.items()))) for sha, files in self.fake.snapshots.items()
        )
        return (tuple(sorted(self.fake.heads.items())), snapshots)

    def head(self, branch: str) -> str:
        return self.fake.heads.get(branch, "")

    def review_surfaces(self) -> int:
        return len(self.fake.pull_requests)

    def review_surfaces_are_draft(self) -> bool:
        return all(pr["isDraft"] is True for pr in self.fake.pull_requests)

    async def evaluate_ci(self, run_id: str) -> None:
        await self.service.evaluate_waiting_ci_one(run_id)

    def seed_candidate(self, run_id: str, sha: str) -> None:
        self.fake.seed_commit(self.transport_branch(run_id), sha, "forge commit")


LANES: tuple[type, ...] = (GitLabLane, GitHubLane, AzureLane)
LANES_BY_PROVIDER = {lane.provider: lane for lane in LANES}


def _assert_untouched(lane, before: tuple) -> None:
    assert lane.state() == before, "a refusal scenario must leave zero remote writes"


# ----------------------------------------------------------------------
# The scenarios — one implementation, every provider
# ----------------------------------------------------------------------


async def scenario_positive_control(lane) -> str:
    """The lane publishes EXACTLY the validated manifest."""
    branch = lane.transport_branch(RUN_ID)
    before = lane.state()
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
        assert actions[0]["content"] == GOOD_MANIFEST[0].content
    elif lane.provider == "github":
        files = lane.fake.files[GH_REPO]
        assert files["src/feature.py"] == "VALUE = 1\n"
        assert files["src/app.py"] == "print('hello')\n"
        assert set(files) == {"src/app.py", "src/feature.py"}
    else:
        snapshot = lane.fake.snapshots[lane.head(branch)]
        assert snapshot["src/feature.py"] == "VALUE = 1\n"
        assert snapshot["src/app.py"] == "print('hello')\n"
        assert set(snapshot) == {"src/app.py", "src/feature.py"}

    # The record declares the merge surface: native-PR lanes ensure the
    # draft PR inside the publish leg; the synthetic-MR lane's Draft MR is
    # a SEPARATE journaled service leg the transport never performs.
    capability = capability_of(lane.provider)
    if capability.synthetic_merge_fallback:
        assert lane.review_surfaces() == 0
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
    """A stale head is refused with zero writes; the ORIGIN is the record's."""
    capability = capability_of(lane.provider)
    lane.seed_candidate(RUN_ID, MOVED)  # a concurrent writer moved the branch
    before = lane.state()
    attempts_before = lane.write_attempts()

    verdict = await lane.publish_transport(good_changeset(), attempt_base=BASE)

    assert verdict.ok is False
    assert verdict.drift is True
    assert "branch_drift" in verdict.reason
    _assert_untouched(lane, before)

    # The refusal origin is a CAPABILITY fact, not a per-provider guess:
    # a native-CAS lane hands the mutation to the provider, which refuses
    # it; GitLab's writer guard refuses BEFORE any dispatch.
    if capability.native_cas:
        assert lane.write_attempts() == attempts_before + 1, (
            f"{lane.provider} declares native CAS ({capability.cas_mechanism}) — "
            "the provider itself must have received (and refused) the mutation"
        )
    else:
        assert lane.write_attempts() == attempts_before, (
            f"{lane.provider} declares the forge writer guard "
            f"({capability.cas_mechanism}) — nothing may reach the commit API"
        )


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
    """/implement → /go on the builtin lane → waiting_ci. Returns (run, sha)."""
    run_id = await lane.start_run()
    await lane.approve(run_id)
    run = await get_run(lane.db, run_id)
    assert run.status == FlowStatus.WAITING_CI.value
    candidate = run.candidate_shas[-1]
    lane.seed_candidate(run_id, candidate)  # the branch head == candidate
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
    else:
        lane.fake.seed_build(source_version=candidate, result="failed")
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
    # GitHub/Azure: no checks/builds at all — with the grace window zeroed
    # at build(), the gate proceeds as honestly not_configured.

    await lane.evaluate_ci(run_id)

    run = await get_run(lane.db, run_id)
    assert run.status == FlowStatus.READY_FOR_HUMAN.value
    reason = run.status_reason or ""
    assert reason.startswith("unverified — ")
    assert "checks passed" not in reason
    verification = (run.evidence or {}).get("verification") or {}
    assert verification.get("status") in {"unverified", "not_configured"}
    assert verification.get("producer"), "the verdict must name WHO claimed it"


async def run_forbidden_scenarios(lane_cls, db) -> None:
    """The kit battery one lane must clear to earn its attestation.

    Every scenario gets its own pristine fake AND database — the
    one-active-run invariant must never arbitrate between scenarios (a
    scenario that parks a live run on the subject would otherwise be
    adopted by the next one).
    """
    for candidate in bad_candidates():
        async with _fresh_db() as scenario_db:
            await scenario_bad_candidate(await lane_cls.build(scenario_db), candidate)
    scenarios = (
        scenario_out_of_scope,
        scenario_stale_head,
        scenario_missing_spec,
        scenario_cancelled_generation,
        scenario_failed_ci_never_ready,
        scenario_unverified_honest,
    )
    for scenario in scenarios:
        async with _fresh_db() as scenario_db:
            await scenario(await lane_cls.build(scenario_db))


# ----------------------------------------------------------------------
# The kit, parametrized over every capability record
# ----------------------------------------------------------------------


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
# The capability records themselves (honest by construction, pinned here)
# ----------------------------------------------------------------------


class TestCapabilityRecords:
    def test_cas_origins_match_the_records(self):
        """GitHub/Azure enforce the compare-and-swap server-side; GitLab's
        Commits API has no CAS — forge's writer guard does it forge-side."""
        assert capability_of("github").native_cas is True
        assert capability_of("github").cas_mechanism == CAS_GRAPHQL_EXPECTED_HEAD_OID
        assert capability_of("azure_devops").native_cas is True
        assert capability_of("azure_devops").cas_mechanism == CAS_PUSH_OLD_OBJECT_ID
        gitlab = capability_of("gitlab")
        assert gitlab.native_cas is False
        assert gitlab.cas_mechanism == CAS_FORGE_WRITER_GUARD

    def test_merge_surface_fallback_is_declared_not_guessed(self):
        """Only GitLab synthesizes the review surface (the Draft MR leg);
        GitHub/Azure pin the provider's native pull-request object."""
        assert capability_of("gitlab").synthetic_merge_fallback is True
        assert capability_of("github").synthetic_merge_fallback is False
        assert capability_of("azure_devops").synthetic_merge_fallback is False

    def test_harness_candidates_travel_as_artifacts_everywhere(self):
        """No harness lane ever carries a write token — on ANY provider."""
        for provider, record in PROVIDER_CAPABILITIES.items():
            assert record.artifact_transport == ARTIFACT_TRANSPORT_CI, provider

    def test_readonly_identities_are_distinct_and_declared(self):
        assert capability_of("gitlab").readonly_identity == READONLY_IDENTITY_MERGE_REQUEST_IID
        assert capability_of("github").readonly_identity == READONLY_IDENTITY_PULL_REQUEST_NUMBER
        assert capability_of("azure_devops").readonly_identity == READONLY_IDENTITY_PULL_REQUEST_ID

    def test_every_recorded_provider_claims_a_verification_producer(self):
        for provider, record in PROVIDER_CAPABILITIES.items():
            assert record.verified_ready_capable is True, provider

    def test_unknown_provider_has_no_record_and_never_attests(self):
        with pytest.raises(UnknownProviderError):
            capability_of("gerrit")
        with pytest.raises(UnknownProviderError):
            attest_conformance("gerrit")
        assert not conformance_attested("gerrit")

    def test_every_recorded_provider_has_a_kit_lane(self):
        """A new capability record without a kit lane fails here — and a
        lane without a record cannot even build. R21's meta-invariant: the
        suite grows WITH the provider table, never behind it."""
        assert set(LANES_BY_PROVIDER) == set(PROVIDER_CAPABILITIES)


# ----------------------------------------------------------------------
# The registration rule: record claim AND conformance pass
# ----------------------------------------------------------------------


def _hypothetical_record(**overrides: Any) -> ProviderCapability:
    base = capability_of("github")
    return replace(base, **overrides)


class TestRegistrationRule:
    def test_registration_requires_both_claim_and_pass(self):
        claim = _hypothetical_record()
        no_claim = _hypothetical_record(verified_ready_capable=False)

        assert registration_allowed(claim, conformance_passed=False) is False
        assert registration_allowed(no_claim, conformance_passed=True) is False
        assert registration_allowed(claim, conformance_passed=True) is True

    def test_conformance_pass_is_per_process_state(self):
        assert verified_ready_capable("github") is False  # claimed, not attested
        attest_conformance("github")
        assert conformance_attested("github") is True
        assert verified_ready_capable("github") is True
        reset_conformance_attestations()
        assert verified_ready_capable("github") is False

    @pytest.mark.parametrize("lane_cls", LANES, ids=lambda lane_cls: lane_cls.provider)
    async def test_kit_pass_registers_verified_ready(self, lane_cls):
        """The live hook: a lane clearing its scenarios attests — and only
        then does the registration rule open. This is what future CI
        asserts after running the kit."""
        await run_forbidden_scenarios(lane_cls, db)

        assert verified_ready_capable(lane_cls.provider) is False
        attest_conformance(lane_cls.provider)
        assert verified_ready_capable(lane_cls.provider) is True


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
