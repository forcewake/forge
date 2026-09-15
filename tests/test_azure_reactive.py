"""Azure DevOps reactive lanes tests (AZ-3, ADR-0024).

- **Reactive review** (:mod:`forge.reactive.azure_review`): drives
  :func:`execute_azure_reactive_review` over a fake
  :class:`~forge.integrations.azure.AzureDevOpsClient` surface and
  :class:`tests.fixtures.fake_llm.FakeLLM` — recursion guards, the sticky
  marker thread (status transitions, not edits), iteration-based
  incremental deltas (research §4.3), severity→thread-status mapping
  (never a vote), cap folding, and the digest footer.
- **CI debug** (:mod:`forge.reactive.azure_ci_debug`): the
  ``build.complete`` failure executor — lane-build skip, fork-safe
  client-side sourceVersion→PR correlation, bounded timeline evidence,
  and the sticky debug thread.
"""

import json
import re
from typing import Any

from forge.agents.models import FailedJobAnalysis, PipelineDebugResult
from forge.config import ForgeConfig
from forge.factory.llm import LLMError
from forge.integrations.azure import AzureDevOpsError, PrIteration
from forge.reactive.azure_ci_debug import (
    DEBUG_MARKER_TEMPLATE,
    execute_azure_debug_ci_command,
    is_forge_lane_build,
)
from forge.reactive.azure_review import (
    REVIEW_MARKER_TEMPLATE,
    REVIEW_MAX_COMMENTS,
    AzureReactiveReviewer,
    execute_azure_reactive_review,
)
from tests.fixtures.fake_llm import FakeLLM

PROJECT = "Fabrikam"
REPO = "core"
PR = 512
BOT = "forge-bot@fabrikam.example"
HUMAN = "dev@fabrikam.example"
BASE = "c" * 40  # merge base (commonRefCommit)
OLD = "a" * 40  # iteration 1 head — what forge reviewed last
NEW = "b" * 40  # iteration 2 head — the push being reviewed
THREAD_ID = 77


def make_settings(**overrides):
    class S:
        FORGE_AZDO_BOT_NAME = BOT
        FORGE_AZDO_LANE_PIPELINE_ID = 207
        FORGE_EVIDENCE_MAX_CHARS = 4000

    for key, value in overrides.items():
        setattr(S, key, value)
    return S()


def verdict_text(
    *,
    verdict: str = "ok",
    summary: str = "clean implementation",
    findings: list[dict] | None = None,
) -> str:
    return json.dumps({"verdict": verdict, "summary": summary, "findings": findings or []})


def make_llm(**kwargs) -> FakeLLM:
    return FakeLLM(script=[verdict_text(**kwargs)])


def iterations_for(*pairs: tuple[int, str]) -> list[PrIteration]:
    """Iterations sharing the merge base: (id, sourceRefCommit) pairs."""
    return [
        PrIteration(
            id=iteration_id,
            source_ref_commit=source,
            target_ref_commit="t" * 40,
            common_ref_commit=BASE,
        )
        for iteration_id, source in pairs
    ]


class FakeAzdoReview:
    """The review engine's client surface, in memory (no network)."""

    def __init__(self) -> None:
        self.threads: list[dict[str, Any]] = []
        self.created_threads: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.status_updates: list[dict[str, Any]] = []
        self.iterations: list[PrIteration] = []
        self.iteration_changes: dict[int, list[dict[str, Any]]] = {}
        self.items: dict[tuple[str, str], str] = {}  # (path, sha) -> content
        self.thread_seq = THREAD_ID

    # -- threads ---------------------------------------------------------

    def seed_thread(self, comments: list[dict[str, Any]], *, status: str = "active") -> dict:
        self.thread_seq += 1
        thread = {
            "id": self.thread_seq,
            "status": status,
            "comments": [
                {"id": 300 + i, "content": c["content"], "author": {"uniqueName": c["author"]}}
                for i, c in enumerate(comments)
            ],
        }
        self.threads.append(thread)
        return thread

    def seed_forge_review_thread(self, head: str, iteration: int) -> dict:
        """The sticky thread as a PREVIOUS review left it."""
        marker = REVIEW_MARKER_TEMPLATE.format(pr_id=PR)
        return self.seed_thread(
            [
                {"content": f"{marker} ⏳ forge is reviewing this PR…", "author": BOT},
                {
                    "content": f"## 🤖 Forge Code Review\n\ndigest `{'0' * 16}`\n\n"
                    f"{marker} head:{head} iteration:{iteration}",
                    "author": BOT,
                },
            ]
        )

    async def list_pr_threads(self, project: str, repo: str, pr_id: int) -> list[dict[str, Any]]:
        return list(self.threads)

    async def create_pr_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        content: str,
        *,
        status: int = 1,
        file_path: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.thread_seq += 1
        thread = {
            "id": self.thread_seq,
            "status": status,
            "file_path": file_path,
            "line_start": line_start,
            "line_end": line_end,
            "comments": [{"id": 900 + self.thread_seq, "content": content}],
        }
        self.created_threads.append(thread)
        self.threads.append(thread)
        return dict(thread)

    async def reply_pr_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        thread_id: int,
        content: str,
        *,
        parent_comment_id: int = 0,
    ) -> dict[str, Any]:
        self.replies.append({"thread_id": thread_id, "content": content})
        return {"id": 999}

    async def update_thread_status(
        self, project: str, repo: str, pr_id: int, thread_id: int, status: int
    ) -> dict[str, Any]:
        self.status_updates.append({"thread_id": thread_id, "status": status})
        return {"id": thread_id, "status": status}

    # -- diff surface ------------------------------------------------------

    def seed_iteration(self, iteration_id: int, source: str, changes: list[dict[str, Any]]) -> None:
        self.iterations.append(
            PrIteration(
                id=iteration_id,
                source_ref_commit=source,
                target_ref_commit="t" * 40,
                common_ref_commit=BASE,
            )
        )
        self.iteration_changes[iteration_id] = changes

    def seed_item(self, path: str, sha: str, content: str) -> None:
        self.items[(path, sha)] = content

    async def get_pr_iterations(self, project: str, repo: str, pr_id: int) -> list[PrIteration]:
        return list(self.iterations)

    async def get_pr_iteration_changes(
        self,
        project: str,
        repo: str,
        pr_id: int,
        iteration_id: int,
        *,
        compare_to: int | None = None,
        top: int | None = None,
    ) -> list[dict[str, Any]]:
        return list(self.iteration_changes.get(iteration_id, []))

    async def get_item(
        self,
        project: str,
        repo: str,
        path: str,
        *,
        version: str | None = None,
        version_type: str | None = None,
    ) -> dict[str, Any]:
        key = (path, version or "")
        if key not in self.items:
            raise AzureDevOpsError(404, f"{path} at {version} not found")
        return {"path": path, "content": self.items[key]}


def review_metadata(**overrides) -> dict:
    metadata = {
        "command": "review_pr",
        "provider": "azure_devops",
        "project": PROJECT,
        "repo": REPO,
        "pr_id": PR,
        "head_sha": NEW,
        "head_branch": "refs/heads/feature/rate-limiter",
        "sender": HUMAN,
        "pr_author": HUMAN,
    }
    metadata.update(overrides)
    return metadata


async def run_review(
    fake: FakeAzdoReview, llm: FakeLLM, metadata: dict, settings=None
) -> dict | None:
    settings = settings or make_settings()
    stack = (fake, AzureReactiveReviewer(llm, fake, settings=settings))
    return await execute_azure_reactive_review(
        settings,
        ForgeConfig(),
        None,  # the fake LLM journals nothing; no session needed
        metadata,
        stack_factory=lambda project, repo: stack,
    )


def seeded_repo(fake: FakeAzdoReview) -> None:
    """Two iterations; only src/fresh.py changed since the last review."""
    fake.seed_iteration(
        1,
        OLD,
        [
            {"changeType": "edit", "item": {"path": "/src/keeper.py"}},
            {"changeType": "add", "item": {"path": "/src/fresh.py"}},
        ],
    )
    fake.seed_iteration(
        2,
        NEW,
        [
            {"changeType": "edit", "item": {"path": "/src/keeper.py"}},
            {"changeType": "add", "item": {"path": "/src/fresh.py"}},
        ],
    )
    fake.seed_item("/src/keeper.py", BASE, "keep v1\n")
    fake.seed_item("/src/keeper.py", OLD, "keep v2\n")
    fake.seed_item("/src/keeper.py", NEW, "keep v2\n")
    fake.seed_item("/src/fresh.py", NEW, "fresh code\n")


def summary_reply(fake: FakeAzdoReview) -> str:
    marker_replies = [
        r for r in fake.replies if REVIEW_MARKER_TEMPLATE.format(pr_id=PR) in r["content"]
    ]
    return marker_replies[-1]["content"] if marker_replies else ""


def finding_threads(fake: FakeAzdoReview) -> list[dict]:
    return [t for t in fake.created_threads if t["file_path"] is not None]


# ----------------------------------------------------------------------
# Review: recursion guards
# ----------------------------------------------------------------------


class TestReviewGuards:
    async def test_bot_sender_is_skipped_before_any_paid_call(self):
        fake = FakeAzdoReview()
        llm = make_llm()

        outcome = await run_review(fake, llm, review_metadata(sender=BOT))

        assert outcome == {"status": "skipped", "reason": "bot_sender"}
        assert llm.calls == []
        assert fake.created_threads == []

    async def test_bot_authored_pr_is_skipped(self):
        fake = FakeAzdoReview()
        llm = make_llm()

        outcome = await run_review(fake, llm, review_metadata(pr_author=BOT))

        assert outcome == {"status": "skipped", "reason": "bot_authored_pr"}
        assert llm.calls == []

    async def test_forge_branch_head_is_skipped(self):
        fake = FakeAzdoReview()
        llm = make_llm()

        outcome = await run_review(fake, llm, review_metadata(head_branch="refs/heads/forge/wi-42"))

        assert outcome == {"status": "skipped", "reason": "forge_branch"}
        assert llm.calls == []

    async def test_incomplete_metadata_is_skipped(self):
        fake = FakeAzdoReview()
        llm = make_llm()

        outcome = await run_review(fake, llm, review_metadata(pr_id=0))

        assert outcome == {"status": "skipped", "reason": "incomplete_metadata"}

    async def test_missing_project_repo_is_rejected(self):
        outcome = await execute_azure_reactive_review(
            make_settings(),
            ForgeConfig(),
            None,
            {"project": "", "repo": ""},
            stack_factory=lambda project, repo: (FakeAzdoReview(), make_llm()),
        )

        assert outcome is None


# ----------------------------------------------------------------------
# Review: the sticky marker thread
# ----------------------------------------------------------------------


class TestStickyThread:
    async def test_first_review_creates_one_thread_and_finalizes_it(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        llm = make_llm(verdict="ok", summary="delta clean")

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome["status"] == "reviewed"
        plain = [t for t in fake.created_threads if t["file_path"] is None]
        assert len(plain) == 1  # the summary thread — never a duplicate
        marker = REVIEW_MARKER_TEMPLATE.format(pr_id=PR)
        assert marker in plain[0]["comments"][0]["content"]
        # Progress transitions ride on thread STATUS plus one final reply.
        body = summary_reply(fake)
        assert "Forge Code Review" in body
        assert fake.status_updates == [{"thread_id": plain[0]["id"], "status": 5}]  # closed

    async def test_re_review_reuses_the_existing_thread(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        fake.seed_forge_review_thread(OLD, 1)
        llm = make_llm(verdict="ok", summary="delta clean")

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome["status"] == "reviewed"
        assert [t for t in fake.created_threads if t["file_path"] is None] == []
        replies = [r for r in fake.replies if r["thread_id"] == THREAD_ID + 1]
        assert replies  # the review body landed in the EXISTING thread

    async def test_already_reviewed_head_is_skipped(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        fake.seed_forge_review_thread(NEW, 2)
        llm = make_llm()

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome == {"status": "skipped", "reason": "already_reviewed"}
        assert llm.calls == []
        assert "already reviewed" in fake.replies[-1]["content"]


# ----------------------------------------------------------------------
# Review: iteration-based incremental delta (research §4.3)
# ----------------------------------------------------------------------


class TestIncrementalDelta:
    async def test_push_reviews_only_the_new_iteration(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        fake.seed_forge_review_thread(OLD, 1)
        llm = make_llm(verdict="ok", summary="delta clean")

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome["status"] == "reviewed"
        prompt = llm.calls[0]["user"]
        # The delta is OLD..NEW: the fresh file only — never the full PR.
        assert "src/fresh.py" in prompt
        assert "src/keeper.py" not in prompt
        assert f"{OLD[:8]}..{NEW[:8]}" in summary_reply(fake)
        assert outcome["iteration"] == 2

    async def test_first_review_covers_the_full_pr_from_the_merge_base(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        llm = make_llm(verdict="ok", summary="full clean")

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome["status"] == "reviewed"
        prompt = llm.calls[0]["user"]
        assert "src/keeper.py" in prompt  # changed between BASE and NEW
        assert "src/fresh.py" in prompt

    async def test_empty_delta_skips_the_llm_call(self):
        fake = FakeAzdoReview()
        fake.seed_iteration(1, OLD, [{"changeType": "edit", "item": {"path": "/src/keeper.py"}}])
        fake.seed_iteration(2, NEW, [{"changeType": "edit", "item": {"path": "/src/keeper.py"}}])
        fake.seed_item("/src/keeper.py", OLD, "same\n")
        fake.seed_item("/src/keeper.py", NEW, "same\n")  # unchanged content
        fake.seed_forge_review_thread(OLD, 1)
        llm = make_llm()

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome == {"status": "skipped", "reason": "empty_delta"}
        assert llm.calls == []

    async def test_no_iterations_reviews_without_a_diff(self):
        fake = FakeAzdoReview()
        llm = make_llm(verdict="ok", summary="blind but calm")

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome["status"] == "reviewed"
        assert "(diff unavailable)" in llm.calls[0]["user"]


# ----------------------------------------------------------------------
# Review: severity → thread status, never a vote
# ----------------------------------------------------------------------


class TestSeverityMapping:
    async def test_critical_finding_stays_active_and_inline(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        llm = make_llm(
            verdict="concerns",
            summary="secret committed",
            findings=[
                {"severity": "critical", "file": "src/fresh.py", "line": 2, "note": "hardcoded key"}
            ],
        )

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome["status"] == "reviewed"
        (thread,) = finding_threads(fake)
        assert thread["status"] == 1  # Active — the REQUEST_CHANGES analog
        assert thread["file_path"] == "/src/fresh.py"
        assert thread["line_start"] == 2
        assert "hardcoded key" in thread["comments"][0]["content"]
        # The vote-note: forge NEVER votes (display-only), the summary
        # thread stays Active for the humans.
        assert "never votes" in summary_reply(fake)
        assert fake.status_updates[-1]["status"] == 1

    async def test_suggestion_is_filed_closed(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        llm = make_llm(
            findings=[
                {"severity": "suggestion", "file": "src/fresh.py", "line": 1, "note": "rename me"}
            ]
        )

        await run_review(fake, llm, review_metadata())

        (thread,) = finding_threads(fake)
        assert thread["status"] == 5  # Closed — informational

    async def test_unanchored_finding_folds_into_the_summary(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        llm = make_llm(
            findings=[
                {"severity": "warning", "file": "src/elsewhere.py", "line": 9, "note": "ghost file"}
            ]
        )

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome["finding_threads"] == 0
        body = summary_reply(fake)
        assert "folded into this summary" in body
        assert "ghost file" in body


class TestCapsAndFooter:
    async def test_findings_beyond_the_cap_fold_into_the_summary(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        findings = [
            {"severity": "warning", "file": "src/fresh.py", "line": i + 1, "note": f"note {i}"}
            for i in range(REVIEW_MAX_COMMENTS + 5)
        ]
        llm = make_llm(findings=findings)

        outcome = await run_review(fake, llm, review_metadata())

        assert len(finding_threads(fake)) == REVIEW_MAX_COMMENTS
        assert outcome["finding_threads"] == REVIEW_MAX_COMMENTS
        assert "5 finding(s) folded" in summary_reply(fake)

    async def test_oversized_body_is_truncated_defensively(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        llm = make_llm(verdict="concerns", summary="x" * 70_000)

        outcome = await run_review(fake, llm, review_metadata())

        assert outcome["status"] == "reviewed"
        body = summary_reply(fake)
        assert "[truncated" in body
        assert len(body) < 61_000

    async def test_summary_carries_the_machine_readable_anchor_and_digest(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        llm = make_llm()

        await run_review(fake, llm, review_metadata())

        body = summary_reply(fake)
        assert f"head:{NEW} iteration:2" in body  # the next event's anchor
        assert re.search(r"digest `[0-9a-f]{16}`", body)

    async def test_llm_failure_finalizes_the_thread_and_reraises(self):
        fake = FakeAzdoReview()
        seeded_repo(fake)
        llm = FakeLLM(script=[LLMError("litellm down")])

        try:
            await run_review(fake, llm, review_metadata())
        except LLMError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected the LLM error to propagate")

        assert "review failed" in fake.replies[-1]["content"]
        assert fake.status_updates[-1]["status"] == 1  # stays Active for the retry


# ----------------------------------------------------------------------
# CI debug: build.complete failure → sticky PR thread
# ----------------------------------------------------------------------


BUILD_ID = 88231
MERGE = "f" * 40  # the policy build's sourceVersion (lastMergeCommit)
HEAD = "e" * 40  # the PR head (lastMergeSourceCommit)


class FakeAzdoDebug:
    def __init__(self) -> None:
        self.builds: dict[int, dict[str, Any]] = {}
        self.prs: list[dict[str, Any]] = []
        self.timelines: dict[int, dict[str, Any]] = {}
        self.task_logs: dict[tuple[int, int], str] = {}
        self.threads: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.status_updates: list[dict[str, Any]] = []
        self.log_reads: list[int] = []
        self.thread_seq = THREAD_ID

    def seed_pr(self, **overrides: Any) -> dict[str, Any]:
        pr = {
            "pullRequestId": PR,
            "createdBy": {"uniqueName": HUMAN},
            "lastMergeSourceCommit": {"commitId": HEAD},
            "lastMergeCommit": {"commitId": MERGE},
        }
        pr.update(overrides)
        self.prs.append(pr)
        return pr

    def seed_timeline(self, records: list[dict[str, Any]]) -> None:
        self.timelines[BUILD_ID] = {"id": "t" * 8, "records": records}

    def seed_log(self, log_id: int, text: str) -> None:
        self.task_logs[(BUILD_ID, log_id)] = text

    async def get_build(self, project: str, build_id: int) -> dict[str, Any]:
        build = self.builds.get(build_id)
        if build is None:
            raise AzureDevOpsError(404, f"build {build_id} not found")
        return build

    async def list_pull_requests(
        self, project: str, repo: str, *, status: str = "active", top: int = 50
    ) -> list[dict[str, Any]]:
        return list(self.prs)

    async def get_timeline(self, project: str, build_id: int) -> dict[str, Any]:
        return self.timelines.get(build_id, {"records": []})

    async def get_task_log(self, project: str, build_id: int, log_id: int) -> str:
        self.log_reads.append(log_id)
        return self.task_logs.get((build_id, log_id), "")

    async def list_pr_threads(self, project: str, repo: str, pr_id: int) -> list[dict[str, Any]]:
        return list(self.threads)

    async def create_pr_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        content: str,
        *,
        status: int = 1,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.thread_seq += 1
        thread = {"id": self.thread_seq, "status": status, "comments": [{"content": content}]}
        self.created.append(thread)
        self.threads.append(thread)
        return dict(thread)

    async def reply_pr_thread(
        self, project: str, repo: str, pr_id: int, thread_id: int, content: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.replies.append({"thread_id": thread_id, "content": content})
        return {"id": 999}

    async def update_thread_status(
        self, project: str, repo: str, pr_id: int, thread_id: int, status: int
    ) -> dict[str, Any]:
        self.status_updates.append({"thread_id": thread_id, "status": status})
        return {"id": thread_id}


def debug_result() -> PipelineDebugResult:
    return PipelineDebugResult(
        summary="the retry path races the timeout",
        is_flaky=True,
        jobs=[
            FailedJobAnalysis(
                job_name="build-and-test",
                job_stage="test",
                root_cause="unbounded retry",
                fix_suggestion="cap the retries",
                confidence="high",
            )
        ],
        suggested_actions=["Cap the retry loop"],
    )


def debug_metadata(**overrides) -> dict:
    metadata = {
        "command": "debug_ci",
        "provider": "azure_devops",
        "project": PROJECT,
        "repo": REPO,
        "build_id": BUILD_ID,
        "definition_id": 7,
        "result": "failed",
        "source_version": MERGE,
    }
    metadata.update(overrides)
    return metadata


async def run_debug(fake: FakeAzdoDebug, metadata: dict, settings=None, runner=None) -> dict | None:
    settings = settings or make_settings()

    async def default_runner(failed_jobs, job_logs):
        return debug_result()

    return await execute_azure_debug_ci_command(
        settings,
        ForgeConfig(),
        None,
        metadata,
        client=fake,
        debug_runner=runner or default_runner,
    )


class TestDebugGuards:
    def test_lane_pipeline_id_match(self):
        assert is_forge_lane_build(207, 207)
        assert not is_forge_lane_build(7, 207)
        assert not is_forge_lane_build(7, 0)  # unconfigured lane id: no guard

    async def test_forge_lane_builds_are_skipped(self):
        fake = FakeAzdoDebug()

        outcome = await run_debug(fake, debug_metadata(definition_id=207))

        assert outcome == {"status": "skipped", "reason": "forge_lane_build"}

    async def test_non_failed_results_are_skipped(self):
        fake = FakeAzdoDebug()

        outcome = await run_debug(fake, debug_metadata(result="succeeded"))

        assert outcome == {"status": "skipped", "reason": "not_failed"}

    async def test_incomplete_metadata_is_skipped(self):
        fake = FakeAzdoDebug()

        outcome = await run_debug(fake, debug_metadata(build_id=0))

        assert outcome == {"status": "skipped", "reason": "incomplete_metadata"}

    async def test_no_open_pr_for_the_sha_is_skipped(self):
        fake = FakeAzdoDebug()  # no PRs seeded

        outcome = await run_debug(fake, debug_metadata())

        assert outcome == {"status": "skipped", "reason": "no_associated_pr"}

    async def test_bot_authored_prs_are_skipped(self):
        fake = FakeAzdoDebug()
        fake.seed_pr(createdBy={"uniqueName": BOT})

        outcome = await run_debug(fake, debug_metadata())

        assert outcome == {"status": "skipped", "reason": "bot_authored_pr"}


class TestDebugCorrelationAndEvidence:
    async def test_correlates_via_the_payload_source_version_and_posts_once(self):
        fake = FakeAzdoDebug()
        fake.seed_pr()
        fake.seed_timeline(
            [
                {
                    "type": "Task",
                    "name": "build-and-test",
                    "identifier": "test",
                    "result": "failed",
                    "log": {"id": 5},
                }
            ]
        )
        fake.seed_log(5, "E: unbounded retry loop\n" * 10)

        outcome = await run_debug(fake, debug_metadata())

        assert outcome == {
            "status": "debugged",
            "pr_id": PR,
            "build_id": BUILD_ID,
            "head_sha": HEAD,  # the marker keys the PR HEAD, not the merge SHA
            "is_flaky": True,
        }
        (thread,) = fake.created
        assert DEBUG_MARKER_TEMPLATE.format(head_sha=HEAD) in thread["comments"][0]["content"]
        assert "Forge CI Failure Analysis" in thread["comments"][0]["content"]
        assert fake.status_updates == []

    async def test_source_version_falls_back_to_the_build_read(self):
        fake = FakeAzdoDebug()
        fake.seed_pr()
        fake.builds[BUILD_ID] = {"sourceVersion": MERGE}
        fake.seed_timeline([])

        outcome = await run_debug(fake, debug_metadata(source_version=""))

        assert outcome["status"] == "debugged"
        assert outcome["head_sha"] == HEAD

    async def test_failed_task_evidence_is_capped_and_tail_bounded(self):
        fake = FakeAzdoDebug()
        fake.seed_pr()
        fake.seed_timeline(
            [
                {
                    "type": "Task",
                    "name": f"task-{i}",
                    "result": "failed",
                    "log": {"id": i},
                }
                for i in range(1, 6)  # five failed tasks — evidence caps at 3
            ]
        )
        for log_id in range(1, 6):
            fake.seed_log(log_id, "x" * 20_000)

        captured: dict[str, Any] = {}

        async def runner(failed_jobs, job_logs):
            captured["failed"] = failed_jobs
            captured["logs"] = job_logs
            return debug_result()

        await run_debug(fake, debug_metadata(), runner=runner)

        from forge.reactive.ci_debug import DEBUG_LOG_PER_JOB_CHARS, DEBUG_MAX_FAILED_JOBS

        assert len(captured["failed"]) == DEBUG_MAX_FAILED_JOBS
        assert len(captured["logs"]) == DEBUG_MAX_FAILED_JOBS
        assert all(len(log) <= DEBUG_LOG_PER_JOB_CHARS for log in captured["logs"].values())

    async def test_timeline_without_failed_tasks_debugs_without_evidence(self):
        fake = FakeAzdoDebug()
        fake.seed_pr()
        fake.seed_timeline([{"type": "Task", "name": "ok", "result": "succeeded"}])

        captured: dict[str, Any] = {}

        async def runner(failed_jobs, job_logs):
            captured["failed"] = failed_jobs
            return debug_result()

        outcome = await run_debug(fake, debug_metadata(), runner=runner)

        assert outcome["status"] == "debugged"
        assert captured["failed"] == []


class TestDebugStickyThread:
    async def test_re_debug_replies_into_the_marker_thread(self):
        fake = FakeAzdoDebug()
        fake.seed_pr()
        fake.thread_seq += 1
        marker_thread = {
            "id": fake.thread_seq,
            "status": "fixed",
            "comments": [
                {
                    "content": f"{DEBUG_MARKER_TEMPLATE.format(head_sha=HEAD)} old analysis",
                    "author": {"uniqueName": BOT},
                }
            ],
        }
        fake.threads.append(marker_thread)
        fake.seed_timeline([])

        outcome = await run_debug(fake, debug_metadata())

        assert outcome["status"] == "debugged"
        assert fake.created == []  # no second thread
        (reply,) = fake.replies
        assert reply["thread_id"] == marker_thread["id"]
        assert "Forge CI Failure Analysis" in reply["content"]
        assert fake.status_updates == [{"thread_id": marker_thread["id"], "status": 1}]

    async def test_a_quoted_marker_in_a_human_thread_is_not_hijacked(self):
        fake = FakeAzdoDebug()
        fake.seed_pr()
        fake.thread_seq += 1
        fake.threads.append(
            {
                "id": fake.thread_seq,
                "status": "active",
                "comments": [
                    {
                        "content": f"quoting {DEBUG_MARKER_TEMPLATE.format(head_sha=HEAD)} here",
                        "author": {"uniqueName": HUMAN},
                    }
                ],
            }
        )
        fake.seed_timeline([])

        outcome = await run_debug(fake, debug_metadata())

        assert outcome["status"] == "debugged"
        assert fake.replies == []  # the human's thread was left alone
        assert len(fake.created) == 1  # forge posted its own thread
