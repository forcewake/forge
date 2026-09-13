"""GitHub publish flow tests: branch CAS, drift outcome, Draft PR dedup.

Drives :class:`forge.integrations.github_flow.GitHubPublishFlow` over
:class:`tests.fixtures.fake_github.FakeGitHub` — the in-memory fake mirrors
the parsed client semantics (branch-wide CAS included), so the concurrency
contract is exercised without any network.
"""

import pytest

from forge.integrations.github_flow import (
    GitHubPublishFlow,
    github_factory_branch,
)
from forge.repository.changeset import Change, ChangeSet, Operation
from tests.fixtures.fake_github import FakeGitHub

REPO = "acme/acme-widget"
BASE_HEAD = "1" * 40
RUN_ID = "abcd1234" + "0" * 24  # 32-hex, like an inbox-derived run id


@pytest.fixture()
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO, {"src/app.py": "print('hi')\n", "README.md": "# acme\n"})
    github.heads[REPO]["main"] = BASE_HEAD  # pin the frozen base for assertions
    github.seed_issue(REPO, 42, "Add password reset", "Users are locked out.")
    return github


def changeset(branch: str | None = None) -> ChangeSet:
    return ChangeSet(
        branch=branch or github_factory_branch(42, RUN_ID),
        commit_message=f"forge: implement 42 (run {RUN_ID[:8]})",
        changes=[
            Change(path="src/feature.py", operation=Operation.CREATE, content="VALUE = 1\n"),
            Change(path="src/app.py", operation=Operation.UPDATE, content="print('hello')\n"),
        ],
    )


def make_flow(fake: FakeGitHub, proposer=None) -> GitHubPublishFlow:
    return GitHubPublishFlow(fake, proposer=proposer, base_branch="main")


class TestFactoryBranch:
    def test_branch_name_scheme(self):
        assert github_factory_branch(42, RUN_ID) == "forge/42/abcd1234"
        assert github_factory_branch(None, RUN_ID) == "forge/0/abcd1234"


class TestPublish:
    async def test_publish_commits_on_factory_branch_cut_from_expected_head(self, fake: FakeGitHub):
        flow = make_flow(fake)

        outcome = await flow.publish_changeset(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            changeset=changeset(),
        )

        assert outcome.ok is True
        assert outcome.branch == "forge/42/abcd1234"
        assert outcome.expected_head_oid == BASE_HEAD  # CAS pinned to the base
        assert outcome.commit_oid is not None and outcome.commit_oid != BASE_HEAD
        # The branch head IS the new commit (PR head).
        assert (
            await fake.get_branch_head("acme", "acme-widget", outcome.branch) == outcome.commit_oid
        )
        # Files landed on the shared snapshot.
        assert fake.files[REPO]["src/feature.py"] == "VALUE = 1\n"
        assert fake.files[REPO]["src/app.py"] == "print('hello')\n"

    async def test_publish_opens_a_draft_pr(self, fake: FakeGitHub):
        flow = make_flow(fake)

        outcome = await flow.publish_changeset(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            changeset=changeset(),
        )

        assert outcome.ok is True
        assert outcome.pr_draft is True
        assert outcome.pr_number is not None
        (pr,) = fake.prs_for(REPO, "forge/42/abcd1234")
        assert pr["base"]["ref"] == "main"
        assert pr["head"]["sha"] == outcome.commit_oid

    async def test_stale_data_reports_drift_without_retry(self, fake: FakeGitHub):
        """A moved head is a drift outcome — never a silent retry (ADR-0016 §3)."""
        flow = make_flow(fake)

        # Simulate a concurrent writer: the branch moves AFTER it was cut but
        # BEFORE the commit mutation lands.
        async def frozen_base_head(owner, repo, branch):
            return BASE_HEAD

        original_commit = fake.create_commit_on_branch

        async def intercept_commit(*args, **kwargs):
            fake.heads[REPO]["forge/42/abcd1234"] = "f" * 40  # concurrent push
            return await original_commit(*args, **kwargs)

        fake.get_branch_head = frozen_base_head  # type: ignore[method-assign]
        fake.create_commit_on_branch = intercept_commit  # type: ignore[method-assign]

        outcome = await flow.publish_changeset(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            changeset=changeset(),
        )

        assert outcome.ok is False
        assert outcome.drift is True
        assert "branch_drift" in outcome.reason
        assert outcome.expected_head_oid == BASE_HEAD
        # Exactly ONE mutation attempt — the flow did not retry.
        assert len(fake.calls_of("create_commit_on_branch")) == 1
        # And the concurrent writer's head survived untouched.
        assert fake.heads[REPO]["forge/42/abcd1234"] == "f" * 40

    async def test_drift_outcome_still_reports_existing_pr(self, fake: FakeGitHub):
        """A previous attempt's PR is surfaced as evidence on drift."""
        flow = make_flow(fake)
        # The branch exists with a prior commit and a PR already opened.
        fake.heads[REPO]["forge/42/abcd1234"] = "e" * 40
        fake.pull_requests[REPO] = [
            {
                "number": 7,
                "state": "open",
                "draft": True,
                "head": {"ref": "forge/42/abcd1234", "sha": "e" * 40},
                "base": {"ref": "main"},
                "html_url": f"https://github.test/{REPO}/pull/7",
            }
        ]

        outcome = await flow.publish_changeset(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            changeset=changeset(),
        )

        assert outcome.drift is True and outcome.ok is False
        assert outcome.pr_number == 7

    async def test_branch_create_failure_is_reported(self, fake: FakeGitHub):
        async def failing_create(owner, repo, branch, sha):
            from forge.integrations.github import GitHubAPIError

            raise GitHubAPIError(403, "resource not accessible by integration")

        fake.create_branch = failing_create  # type: ignore[method-assign]
        flow = make_flow(fake)

        outcome = await flow.publish_changeset(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            changeset=changeset(),
        )

        assert outcome.ok is False
        assert "branch_create_failed" in outcome.reason
        assert fake.calls_of("create_commit_on_branch") == []


class TestDraftPrDedup:
    async def test_re_run_adopts_existing_pr_and_never_duplicates(self, fake: FakeGitHub):
        """Re-drive after a crash between commit and PR: the second attempt
        hits the CAS (head moved past the pinned base) and the existing PR is
        adopted — one branch, one PR, one commit."""
        flow = make_flow(fake)

        first = await flow.publish_changeset(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            changeset=changeset(),
        )
        assert first.ok is True
        head_after_first = fake.heads[REPO]["forge/42/abcd1234"]

        # Re-execution with the SAME run identity (deterministic run id from
        # the inbox identity): base head unchanged, branch head moved.
        second = await flow.publish_changeset(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            changeset=changeset(),
        )

        assert second.drift is True  # the CAS refused a duplicate commit
        assert second.pr_number == first.pr_number  # existing PR adopted
        prs = fake.prs_for(REPO, "forge/42/abcd1234")
        assert len(prs) == 1  # found, not duplicated
        assert fake.heads[REPO]["forge/42/abcd1234"] == head_after_first  # one commit only

    async def test_pr_lookup_happens_before_creation(self, fake: FakeGitHub):
        flow = make_flow(fake)
        await flow.publish_changeset(
            "acme",
            "acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            changeset=changeset(),
        )
        assert fake.calls_of("get_pr_by_head")
        assert len(fake.calls_of("create_draft_pr")) == 1


class TestProposalLeg:
    async def test_publish_proposal_uses_proposer_against_reader(self, fake: FakeGitHub):
        """The builtin implementer (reader-bound) proposes; the flow publishes."""
        proposals = []

        class StubProposer:
            async def propose(self, run, issue_title, *, plan_summary="", attempt_base=None):
                proposals.append(
                    {
                        "issue_title": issue_title,
                        "plan_summary": plan_summary,
                        "attempt_base": attempt_base,
                        "run_id": run.id,
                    }
                )
                return changeset()

        flow = make_flow(fake, proposer=StubProposer())
        outcome = await flow.publish_proposal(
            owner="acme",
            repo="acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            issue_title="Add password reset",
            plan_summary="1. add reset endpoint",
        )

        assert outcome.ok is True
        (proposal,) = proposals
        assert proposal["issue_title"] == "Add password reset"
        assert proposal["attempt_base"] == BASE_HEAD  # frozen base fed to proposer
        assert proposal["run_id"] == RUN_ID
        assert outcome.pr_number is not None


class TestCommandDispatch:
    async def test_github_provider_dispatches_to_bridge(self, monkeypatch, tmp_path):
        """execute_run_command routes provider:github payloads to the bridge —
        RunService/GitLabClient are never constructed for GitHub commands."""
        from forge.config import ForgeConfig, Settings
        from pydantic import SecretStr

        import forge.runs.service as service_module

        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("s"),
            DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/dispatch.db",
        )
        dispatched = []

        async def fake_bridge(settings, config, sf, metadata, **kwargs):
            dispatched.append(metadata)

        def boom_gitlab(**kwargs):  # pragma: no cover — must not be hit
            raise AssertionError("GitLabClient constructed for a GitHub command")

        monkeypatch.setattr(service_module, "GitLabClient", boom_gitlab)
        monkeypatch.setattr(
            "forge.integrations.github_flow.execute_github_run_command", fake_bridge
        )

        await service_module.execute_run_command(
            settings, ForgeConfig(), None, {"command": "start_run", "provider": "github"}
        )

        assert dispatched and dispatched[0]["provider"] == "github"

    async def test_go_and_cancel_commands_are_not_wired(self, tmp_path):
        """The human gate is deferred: /go has no pending decision to consume."""
        from forge.config import ForgeConfig, Settings
        from pydantic import SecretStr

        import forge.integrations.github_flow as flow_module

        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("s"),
            DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/gate.db",
            FORGE_GITHUB_PRIVATE_KEY=SecretStr("k"),
        )

        class Boom:
            def __init__(self, *a, **k):
                raise AssertionError("GitHubClient constructed for a deferred command")

        original = flow_module.GitHubClient
        flow_module.GitHubClient = Boom  # type: ignore[misc]
        try:
            for command in ("go", "cancel"):
                outcome = await flow_module.execute_github_run_command(
                    settings,
                    ForgeConfig(),
                    None,
                    {"command": command, "provider": "github", "repo_full_name": REPO},
                )
                assert outcome is None
        finally:
            flow_module.GitHubClient = original  # type: ignore[misc]

    async def test_start_run_via_flow_builder_publishes_and_opens_pr(
        self, fake: FakeGitHub, monkeypatch, tmp_path
    ):
        """The step-execution path end-to-end with an injected flow factory."""
        from forge.config import ForgeConfig, Settings
        from pydantic import SecretStr

        import forge.integrations.github_flow as flow_module

        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("s"),
            DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/start.db",
            FORGE_GITHUB_PRIVATE_KEY=SecretStr("k"),
        )
        source_event_id = "e" * 64  # sha256 hex from the command's inbox row

        class StubClient:
            """Routes the bridge's issue read to the fake; no network."""

            def __init__(self, **kwargs) -> None:
                self._fake = fake

            async def get_issue(self, owner, repo, number):
                return await self._fake.get_issue(owner, repo, number)

            async def aclose(self):
                return None

        monkeypatch.setattr(flow_module, "GitHubClient", StubClient)
        flow = make_flow(fake, proposer=StubProposerForDispatch())
        outcome = await flow_module.execute_github_run_command(
            settings,
            ForgeConfig(),
            None,
            {
                "command": "start_run",
                "provider": "github",
                "repo_full_name": REPO,
                "issue_number": 42,
                "source_event_id": source_event_id,
            },
            flow_builder=lambda: flow,
        )

        assert outcome is not None and outcome.ok is True
        # Deterministic run id from the inbox identity → deterministic branch.
        assert outcome.branch == f"forge/42/{source_event_id[:8]}"
        assert outcome.pr_number is not None


class StubProposerForDispatch:
    """Minimal proposer double for the dispatch test."""

    async def propose(self, run, issue_title, *, plan_summary="", attempt_base=None):
        return ChangeSet(
            branch=github_factory_branch(run.issue_iid, run.id),
            commit_message=f"forge: implement {run.issue_iid}",
            changes=[
                Change(path="src/feature.py", operation=Operation.CREATE, content="VALUE = 1\n")
            ],
        )
