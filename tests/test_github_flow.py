"""GitHub publish bridge tests: branch CAS, drift outcome, Draft PR dedup.

Since E3a the bridge (forge.integrations.github_flow) keeps ONLY the publish
leg and the agent construction; the run lifecycle lives in
forge.runs.github_service (covered by tests/test_github_runs.py). These
tests drive :class:`GitHubPublishFlow` and :class:`GitHubPRReviewer` over
:class:`tests.fixtures.fake_github.FakeGitHub` — the in-memory fake mirrors
the parsed client semantics (branch-wide CAS included), so the concurrency
contract is exercised without any network.
"""

import pytest

from forge.factory.reviewer import ReviewVerdict
from forge.integrations.github_flow import (
    GitHubPRReviewer,
    GitHubPublishFlow,
    github_factory_branch,
)
from forge.repository.changeset import Change, ChangeSet, Operation
from tests.fixtures.fake_github import FakeGitHub

REPO = "acme/acme-widget"
BASE_HEAD = "1" * 40
RUN_ID = "abcd1234" + "0" * 24  # 32-hex, like a FlowRun id


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
    async def test_re_entry_adopts_existing_pr_and_never_duplicates(self, fake: FakeGitHub):
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

        # Re-execution with the SAME run identity (the run row owns the run
        # id now — a re-driven /go leg re-derives the same branch): base head
        # unchanged, branch head moved.
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

    async def test_publish_proposal_pins_the_frozen_expected_head(self, fake: FakeGitHub):
        """The gate-approved base is the CAS pin — not the live branch head.

        main may have moved between plan time and /go; the publish leg must
        still cut the factory branch from the snapshot the plan was made
        against.
        """
        fake.heads[REPO]["main"] = "9" * 40  # main moved after the plan
        moved_head = await fake.get_branch_head("acme", "acme-widget", "main")
        assert moved_head == "9" * 40

        class StubProposer:
            async def propose(self, run, issue_title, *, plan_summary="", attempt_base=None):
                assert attempt_base == BASE_HEAD  # proposes against the frozen base
                return changeset()

        flow = make_flow(fake, proposer=StubProposer())
        outcome = await flow.publish_proposal(
            owner="acme",
            repo="acme-widget",
            issue_number=42,
            run_id=RUN_ID,
            issue_title="Add password reset",
            expected_head=BASE_HEAD,
        )

        assert outcome.ok is True
        assert outcome.expected_head_oid == BASE_HEAD
        assert (
            fake.heads[REPO]["forge/42/abcd1234"].endswith(outcome.commit_oid or "")
            or fake.heads[REPO]["forge/42/abcd1234"] == outcome.commit_oid
        )


class TestPRReviewer:
    """The bridge's reviewer: readonly review over the PR diff."""

    def _reviewer(self, fake: FakeGitHub, text: str) -> GitHubPRReviewer:
        class StubLLM:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            async def complete(
                self, *, tier, system, user, role, flow_run_id=None, json_mode=False
            ):
                self.prompts.append(user)
                from types import SimpleNamespace

                return SimpleNamespace(text=text)

        llm = StubLLM()
        reviewer = GitHubPRReviewer(llm, fake)
        reviewer._llm = llm  # keep the stub observable
        return reviewer

    async def test_review_renders_the_pr_patches(self, fake: FakeGitHub):
        fake.seed_pr_files(
            7,
            [
                {"filename": "src/app.py", "patch": "@@ -1 +1 @@\n-print('hi')\n+print('hello')"},
                {"filename": "src/feature.py", "patch": "@@ -0,0 +1 @@\n+VALUE = 1"},
            ],
        )
        reviewer = self._reviewer(
            fake,
            '{"verdict": "ok", "summary": "sound", "findings": []}',
        )

        verdict = await reviewer.review(
            owner="acme",
            repo="acme-widget",
            pr_number=7,
            issue_title="Add password reset",
            plan_summary="1. add reset",
            base_sha=BASE_HEAD,
            candidate_sha="c" * 40,
            flow_run_id=RUN_ID,
        )

        assert isinstance(verdict, ReviewVerdict)
        assert verdict.verdict == "ok"
        prompt = reviewer._llm.prompts[0]  # type: ignore[attr-defined]
        assert "src/app.py" in prompt
        assert "VALUE = 1" in prompt
        assert "Add password reset" in prompt

    async def test_review_failure_to_read_pr_still_reviews_without_diff(self, fake: FakeGitHub):
        async def failing(owner, repo, number):
            raise RuntimeError("boom")

        fake.get_pr_files = failing  # type: ignore[method-assign]
        reviewer = self._reviewer(
            fake,
            '{"verdict": "concerns", "summary": "cannot see the diff", "findings": []}',
        )

        verdict = await reviewer.review(
            owner="acme",
            repo="acme-widget",
            pr_number=7,
            issue_title="Add password reset",
            plan_summary="",
            base_sha=BASE_HEAD,
            candidate_sha="c" * 40,
        )
        assert verdict.verdict == "concerns"

    async def test_review_rejects_an_unknown_verdict(self, fake: FakeGitHub):
        fake.seed_pr_files(7, [{"filename": "a.py", "patch": "@@ -1 +1 @@\n+x"}])
        reviewer = self._reviewer(fake, '{"verdict": "shipit", "summary": "!", "findings": []}')

        from forge.factory.llm import LLMResponseError

        with pytest.raises(LLMResponseError):
            await reviewer.review(
                owner="acme",
                repo="acme-widget",
                pr_number=7,
                issue_title="t",
                plan_summary="",
                base_sha=BASE_HEAD,
                candidate_sha="c" * 40,
            )
