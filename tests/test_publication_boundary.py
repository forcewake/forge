"""Negative conformance suite for the publication boundary (ADR-0026).

ONE policy, EVERY provider×backend path: a candidate that violates the
write policy is refused with ZERO commit-API calls, whatever entry it
arrives through, and the only route to a write is a
:class:`~forge.runs.publisher.ValidatedCandidate` issued by the boundary.

The matrix is parametrized over the publish paths the test fakes support —

- the GitHub builtin bridge (``GitHubPublishFlow.publish_proposal`` over
  :class:`tests.fixtures.fake_github.FakeGitHub`; the R01 path that used to
  bypass validation entirely),
- the GitHub transport entry (``publish_changeset`` invoked directly — a
  caller claiming "trust me" must still not be able to publish),
- the GitLab builtin publisher (``publish_candidate`` over
  :class:`tests.fixtures.fake_gitlab.FakeGitLab`),

and over every policy-violating shape: denylisted CI config, the
``.github/`` prefix, lockfiles, absolute paths, ``..`` traversal,
out-of-scope paths, too many files and oversized content. The positive
control publishes EXACTLY the validated manifest.
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import FlowRun, RunSpec
from forge.gitlab.blob_reads import BlobReadResult
from forge.integrations.github_flow import (
    GitHubPublishFlow,
    github_factory_branch,
)
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.repository.changeset import Change, ChangeSet, Operation
from forge.runs.candidate import bundle_from_changeset
from forge.runs.publisher import (
    ValidatedCandidate,
    publish_candidate,
    validate_candidate_bundle,
)
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_gitlab import FakeGitLab

GH_REPO = "acme/acme-widget"
GH_BASE_HEAD = "1" * 40
GL_PROJECT_ID = 42
ISSUE_IID = 7
RUN_ID = "abcd1234" + "0" * 24
BASE_SHA = "base-sha-1"

#: The commit-API mutations a rejection must never reach (GitHub side).
GH_MUTATIONS = ("create_branch", "create_commit_on_branch", "create_draft_pr")


# ----------------------------------------------------------------------
# Scenario matrix: (name, changeset factory, expected refusal fragment, scope)
# ----------------------------------------------------------------------


def _cs(*changes: Change, message: str = "forge: implement 7") -> ChangeSet:
    return ChangeSet(
        branch=github_factory_branch(ISSUE_IID, RUN_ID),
        commit_message=message,
        changes=list(changes),
    )


def create(path: str, content: str = "x\n") -> Change:
    return Change(path=path, operation=Operation.CREATE, content=content)


SCENARIOS = [
    pytest.param(
        "denylisted_ci_config",
        lambda: _cs(create(".gitlab-ci.yml", "rogue: true\n")),
        "denylisted",
        None,
        id="denylisted_ci_config",
    ),
    pytest.param(
        "denylisted_workflow_prefix",
        lambda: _cs(create(".github/workflows/pwn.yml", "on: push\n")),
        "denylisted prefix",
        None,
        id="denylisted_workflow_prefix",
    ),
    pytest.param(
        "lockfile",
        lambda: _cs(create("package-lock.json", "{}\n")),
        "lockfiles are denylisted",
        None,
        id="lockfile",
    ),
    pytest.param(
        "absolute_path",
        lambda: _cs(create("/etc/evil.py")),
        "absolute paths",
        None,
        id="absolute_path",
    ),
    pytest.param(
        "path_traversal",
        lambda: _cs(create("../escape.py")),
        "path traversal",
        None,
        id="path_traversal",
    ),
    pytest.param(
        "out_of_scope",
        lambda: _cs(create("web/components/x.ts", "hi\n")),
        "outside the allowed scope",
        ["services/**"],
        id="out_of_scope",
    ),
    pytest.param(
        "too_many_files",
        lambda: _cs(*(create(f"dir/file{i}.txt", "x") for i in range(21))),
        "max 20",
        None,
        id="too_many_files",
    ),
    pytest.param(
        "oversized_content",
        lambda: _cs(create("big.txt", "x" * (256 * 1024 + 1))),
        "bytes",
        None,
        id="oversized_content",
    ),
]


# ----------------------------------------------------------------------
# Shared fixtures / helpers
# ----------------------------------------------------------------------


def make_run(**overrides) -> FlowRun:
    run = FlowRun(
        id=RUN_ID,
        project_id=GL_PROJECT_ID,
        issue_iid=ISSUE_IID,
        base_sha=BASE_SHA,
    )
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


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


@pytest.fixture()
def fake_github() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(GH_REPO, {"src/app.py": "print('hi')\n", "README.md": "# acme\n"})
    github.heads[GH_REPO]["main"] = GH_BASE_HEAD
    github.seed_issue(GH_REPO, 42, "Add password reset", "Users are locked out.")
    return github


class FixedProposer:
    """A proposer that always returns the given changeset."""

    def __init__(self, changeset: ChangeSet) -> None:
        self.changeset = changeset

    async def propose(self, run, issue_title, *, plan_summary="", attempt_base=None):
        return self.changeset


async def publish_via_github(fake: FakeGitHub, changeset: ChangeSet, scope):
    """The builtin bridge entry (the R01 path): propose → boundary → write."""
    flow = GitHubPublishFlow(
        fake, proposer=FixedProposer(changeset), base_branch="main", reader=fake
    )
    return await flow.publish_proposal(
        owner="acme",
        repo="acme-widget",
        issue_number=42,
        run_id=RUN_ID,
        issue_title="Add password reset",
        allowed_paths=list(scope) if scope else None,
    )


async def publish_via_github_transport(fake: FakeGitHub, changeset: ChangeSet, scope):
    """The transport entry, invoked DIRECTLY with an unvalidated changeset."""
    flow = GitHubPublishFlow(fake, proposer=FixedProposer(changeset), base_branch="main")
    return await flow.publish_changeset(
        "acme",
        "acme-widget",
        issue_number=42,
        run_id=RUN_ID,
        changeset=changeset,
        allowed_paths=list(scope) if scope else None,
    )


async def publish_via_gitlab(db, fake: FakeGitLab, changeset: ChangeSet, scope) -> object:
    """The GitLab builtin publisher entry over the same candidate shape."""
    run = make_run()
    async with db() as session:
        session.add(run)
        if scope:
            session.add(
                RunSpec(
                    run_id=run.id,
                    document={"allowed_paths": list(scope)},
                    digest="digest-with-scope",
                )
            )
        await session.commit()
    writer = ChangesetWriter(fake, db, GL_PROJECT_ID)
    return await publish_candidate(
        gitlab=fake,
        session_factory=db,
        writer=writer,
        run=run,
        bundle=bundle_from_changeset(changeset, attempt_base_oid=BASE_SHA),
    )


# ----------------------------------------------------------------------
# The negative matrix: zero commit-API calls on EVERY path
# ----------------------------------------------------------------------


class TestZeroWritesOnRejection:
    @pytest.mark.parametrize("name,make_cs,fragment,scope", SCENARIOS)
    async def test_github_builtin_bridge_refuses_and_never_mutates(
        self, fake_github, name, make_cs, fragment, scope
    ):
        outcome = await publish_via_github(fake_github, make_cs(), scope)

        assert outcome.ok is False
        assert outcome.invalid is True  # the boundary rejected it
        assert outcome.drift is True  # blocked-class: callers BLOCK, never retry
        assert fragment in outcome.reason
        for mutation in GH_MUTATIONS:
            assert fake_github.calls_of(mutation) == [], name

    @pytest.mark.parametrize("name,make_cs,fragment,scope", SCENARIOS)
    async def test_github_transport_refuses_a_direct_unvalidated_changeset(
        self, fake_github, name, make_cs, fragment, scope
    ):
        """Direct bridge invocation with an unvalidated candidate cannot publish."""
        outcome = await publish_via_github_transport(fake_github, make_cs(), scope)

        assert outcome.ok is False
        assert outcome.invalid is True
        assert fragment in outcome.reason
        for mutation in GH_MUTATIONS:
            assert fake_github.calls_of(mutation) == [], name

    @pytest.mark.parametrize("name,make_cs,fragment,scope", SCENARIOS)
    async def test_gitlab_publisher_refuses_and_never_commits(
        self, db, fake_gitlab, name, make_cs, fragment, scope
    ):
        result = await publish_via_gitlab(db, fake_gitlab, make_cs(), scope)

        assert result.ok is False
        assert fragment in result.reason
        assert fake_gitlab.calls_of("create_commit") == []
        assert fake_gitlab.calls_of("create_branch") == []


# ----------------------------------------------------------------------
# The type/guard: only the boundary's wrapper reaches a write
# ----------------------------------------------------------------------


class TestValidatedWrapperGuard:
    async def test_transport_write_leg_refuses_a_raw_changeset(self, fake_github):
        """The commit API is reachable only through the boundary's wrapper."""
        flow = GitHubPublishFlow(fake_github, base_branch="main")
        raw = _cs(create("src/ok.py"))
        assert not isinstance(raw, ValidatedCandidate)

        with pytest.raises(TypeError, match="ValidatedCandidate"):
            await flow.publish_validated(
                "acme",
                "acme-widget",
                issue_number=42,
                run_id=RUN_ID,
                candidate=raw,  # type: ignore[arg-type]
            )
        for mutation in GH_MUTATIONS:
            assert fake_github.calls_of(mutation) == []

    async def test_wrapper_issued_by_the_boundary_is_accepted(self, fake_github):
        flow = GitHubPublishFlow(fake_github, base_branch="main")
        candidate = validate_candidate_bundle(
            bundle_from_changeset(
                _cs(create("src/feature.py", "VALUE = 1\n")), attempt_base_oid=GH_BASE_HEAD
            ),
            base_contents={},
            branch=github_factory_branch(42, RUN_ID),
            commit_message="forge: implement 42",
        )

        outcome = await flow.publish_validated(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            candidate=candidate,
            expected_head=GH_BASE_HEAD,
        )

        assert outcome.ok is True
        assert fake_github.files[GH_REPO]["src/feature.py"] == "VALUE = 1\n"


# ----------------------------------------------------------------------
# The positive control: the validated manifest is exactly what publishes
# ----------------------------------------------------------------------


class TestPositiveControl:
    async def test_github_publishes_exactly_the_validated_manifest(self, fake_github):
        changeset = _cs(
            create("src/feature.py", "VALUE = 1\n"),
            Change(path="src/app.py", operation=Operation.UPDATE, content="print('hello')\n"),
        )

        outcome = await publish_via_github(fake_github, changeset, None)

        assert outcome.ok is True
        # Exactly the validated manifest — no more, no less.
        assert fake_github.files[GH_REPO]["src/feature.py"] == "VALUE = 1\n"
        assert fake_github.files[GH_REPO]["src/app.py"] == "print('hello')\n"
        assert set(fake_github.files[GH_REPO]) == {"src/app.py", "README.md", "src/feature.py"}
        assert outcome.pr_draft is True

    async def test_gitlab_publishes_exactly_the_validated_manifest(self, db, fake_gitlab):
        changeset = _cs(create("forge-demo/x.md", "hello\n"))

        result = await publish_via_gitlab(db, fake_gitlab, changeset, None)

        assert result.ok is True
        (commit_call,) = fake_gitlab.calls_of("create_commit")
        actions = commit_call[1][2]
        assert [(a["action"], a["file_path"]) for a in actions] == [("create", "forge-demo/x.md")]
        assert actions[0]["content"] == "hello\n"


# ----------------------------------------------------------------------
# R14: authoritative evidence is never conflated at the boundary
# ----------------------------------------------------------------------


class TestAuthoritativeEvidence:
    async def test_gitlab_forbidden_base_read_refuses_with_zero_commits(
        self, db, fake_gitlab, monkeypatch
    ):
        """A 403 on an EXISTING base file is never evidence of absence."""
        fake_gitlab.seed_file("src/app.py", "print('hi')\n")

        async def forbidden_read(project_id, file_path, ref="HEAD"):
            return BlobReadResult.forbidden(f"gitlab api error 403: denied for {file_path!r}")

        monkeypatch.setattr(fake_gitlab, "read_blob", forbidden_read)
        changeset = _cs(
            Change(path="src/app.py", operation=Operation.UPDATE, content="print('hello')\n")
        )

        result = await publish_via_gitlab(db, fake_gitlab, changeset, None)

        assert result.ok is False
        assert "authoritative_read_failed" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []
        assert fake_gitlab.calls_of("create_branch") == []

    async def test_confirmed_absence_is_the_only_honest_missing(self, db, fake_gitlab):
        """A real 404 at the base makes an update honestly unapplicable —
        the candidate is refused for having no base content, and nothing
        is created, updated or deleted."""
        changeset = _cs(Change(path="never/there.py", operation=Operation.UPDATE, content="x\n"))

        result = await publish_via_gitlab(db, fake_gitlab, changeset, None)

        assert result.ok is False
        assert "no authoritative base content" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_github_unreadable_base_stays_blocked_with_zero_mutations(
        self, fake_github, monkeypatch
    ):
        """The GitHub transport entry keeps refusing a candidate whose base
        cannot be read — blocked-class, zero commit-API calls."""
        from forge.integrations.github import GitHubAPIError

        async def forbidden_text(file_path, ref="HEAD"):
            raise GitHubAPIError(403, "read denied")

        monkeypatch.setattr(fake_github, "read_text", forbidden_text)
        changeset = _cs(
            Change(path="src/app.py", operation=Operation.UPDATE, content="print('hello')\n")
        )

        outcome = await publish_via_github(fake_github, changeset, None)

        assert outcome.ok is False
        assert outcome.invalid is True
        assert outcome.drift is True
        for mutation in GH_MUTATIONS:
            assert fake_github.calls_of(mutation) == []

    async def test_github_create_over_confirmed_existing_is_refused(self, fake_github):
        """The strict base read FOUND src/app.py — a create over it is a
        violation, never a silent overwrite (R14 create rule)."""
        changeset = _cs(create("src/app.py", "clobber = True\n"))

        outcome = await publish_via_github(fake_github, changeset, None)

        assert outcome.ok is False
        assert outcome.invalid is True
        assert "already exists in the base snapshot" in outcome.reason
        for mutation in GH_MUTATIONS:
            assert fake_github.calls_of(mutation) == []
