"""Cross-slice contract tests: the Azure DevOps joins (AZ-4, ADR-0024).

The three AZ slices each pinned their own side of a hand-off in isolation:

- AZ-2's webhook normalizers produce command metadata (``docs/specs/
  azure-devops-brief.md`` §3);
- AZ-2's :class:`~forge.runs.azure_service.AzureRunService` consumes the
  work-item commands and journals the :class:`~forge.runs.azure_service.
  AzurePipelinesHandle` on /go;
- AZ-3's reactive lanes (:mod:`forge.reactive.azure_review`,
  :mod:`forge.reactive.azure_ci_debug`) and the
  :class:`~forge.execution.azure_pipelines.AzurePipelinesExecutor`
  consume the other halves.

These tests drive the FULL joins end-to-end over fakes (recorded payload
fixtures → normalizer → dispatch → lane) and pin the exact metadata keys
the consumers read — if a producer drops or renames one, this file fails
at the join, not in a live deployment:

- ``workitem.commented`` → ``start_run`` → plan comment + gate;
- ``/go`` lane leg → journaled handle → executor/template contract and
  the reactive cancel consuming the journaled run id;
- ``git.pullrequest.updated`` → ``review_pr`` metadata (project, repo,
  pr_id, head_sha, head_branch full ref, sender, pr_author) → the
  reactive review engine;
- ``build.complete`` → ``debug_ci`` metadata (project, repo, build_id,
  definition_id, result, source_version) → the Pipelines debugger.

No network, no model.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.agents.models import PipelineDebugResult
from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, FlowStatus
from forge.execution.azure_pipelines import AzurePipelinesExecutor, AzurePipelinesHandle
from forge.gateway.azure_webhook import (
    normalize_build_event,
    normalize_pull_request_event,
    normalize_workitem_comment,
)
from forge.integrations.azure import (
    AzureDevOpsNotFoundError,
    AzureRepositoryReader,
    PipelineRun,
    PrIteration,
)
from forge.models.base import Base
from forge.reactive.azure_ci_debug import execute_azure_debug_ci_command, is_forge_lane_build
from forge.reactive.azure_review import (
    REVIEW_MARKER_TEMPLATE,
    AzureReactiveReviewer,
)
from forge.runs.azure_service import (
    AzureAgents,
    AzurePipelinesHandle as JournaledHandle,
    AzureRunService,
    azure_factory_branch,
    execute_azure_run_command,
)
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.fixtures.fake_llm import FakeLLM

FIXTURES = Path(__file__).parent / "fixtures" / "azure_payloads"
TEMPLATE_PATH = Path(__file__).parent.parent / "ci" / "templates" / "forge-lane.azure-pipelines.yml"

PROJECT = "Fabrikam"
REPO = "core"
REPO_GUID = "1a2b3c4d-0000-0000-0000-000000000001"
PROJECT_GUID = "9f8e7d6c-0000-0000-0000-000000000009"
WORK_ITEM = 142
BASE_HEAD = "1" * 40
LANE_PIPELINE_ID = 207
LANE_RUN_ID = 99001
# The pr_updated_push fixture chain (consistent SHA chain, research §10).
NEW_HEAD = "e6b42d90a1c7583f0d8e4a6b29c17f5308da2b47"
MERGE_2 = "c94f2e6b08d13a57b9e0c4a827f36d1059be4d70"
HUMAN = "dev@fabrikam.example"
BOT = "forge-bot@fabrikam.example"


def load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_bytes())


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="gitlab-person",
        FORGE_AZDO_APPROVERS=HUMAN,
        FORGE_AZDO_ORG_URL="https://dev.azure.com/fabrikam",
        FORGE_AZDO_LANE_PIPELINE_ID=None,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


# ----------------------------------------------------------------------
# The run/lane fake: exactly the client surface the work-item → /go join
# crosses (seeded work item, refs, Runs API dispatch, build cancel)
# ----------------------------------------------------------------------


class FakeLaneClient:
    @property
    def org_url(self) -> str:
        return "https://dev.azure.test/fabrikam"

    def __init__(self) -> None:
        self.heads: dict[str, str] = {"main": BASE_HEAD}
        self.work_items: dict[int, dict] = {}
        self.comments: dict[int, list[str]] = {}
        self.pipeline_calls: list[dict] = []
        self.cancelled_builds: list[int] = []
        self._next_run = LANE_RUN_ID

    def seed_work_item(self, number: int, title: str) -> None:
        self.work_items[number] = {
            "id": number,
            "fields": {"System.Title": title, "System.State": "Approved"},
        }

    async def aclose(self) -> None:
        return None

    async def get_work_item(self, project: str, work_item_id: int) -> dict:
        return self.work_items[work_item_id]

    async def add_work_item_comment(self, project: str, work_item_id: int, text: str) -> dict:
        self.comments.setdefault(work_item_id, []).append(text)
        return {"commentId": len(self.comments[work_item_id])}

    async def get_branch_head(self, project: str, repo: str, branch: str) -> str:
        # Unknown branches read as the base (the real client 404s, which the
        # service treats as "cut the branch"); the join only pins the base.
        return self.heads.get(branch, BASE_HEAD)

    async def get_item(
        self,
        project: str,
        repo: str,
        path: str,
        *,
        version: str | None = None,
        version_type: str | None = None,
    ) -> dict:
        # The lane fake carries no repository files: every items read is a
        # provider-confirmed 404 (A13: the typed config read then honestly
        # reports confirmed_absent and the documented default profile runs).
        raise AzureDevOpsNotFoundError(404, f"{path} not found at {version or 'HEAD'}")

    async def create_branch_from(self, project: str, repo: str, branch: str, base_sha: str) -> dict:
        self.heads[branch] = base_sha
        return {"value": [{"name": f"refs/heads/{branch}", "updateStatus": "succeeded"}]}

    async def run_pipeline(
        self,
        project: str,
        pipeline_id: int,
        *,
        ref_name: str,
        template_parameters: dict | None = None,
        variables: dict | None = None,
    ) -> PipelineRun:
        self.pipeline_calls.append(
            {
                "project": project,
                "pipeline_id": pipeline_id,
                "ref_name": ref_name,
                "template_parameters": dict(template_parameters or {}),
            }
        )
        return PipelineRun(run_id=self._next_run, state="inProgress", result=None, url=None)

    async def get_run(self, project: str, pipeline_id: int, run_id: int) -> PipelineRun:
        return PipelineRun(run_id=run_id, state="inProgress", result=None, url=None)

    async def cancel_build(self, project: str, build_id: int) -> dict:
        self.cancelled_builds.append(build_id)
        return {"status": "cancelling"}


def make_stack(fake: FakeLaneClient) -> AzureAgents:
    return AzureAgents(
        client=fake,
        reader=AzureRepositoryReader(fake, PROJECT, REPO),
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
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
def fake() -> FakeLaneClient:
    azdo = FakeLaneClient()
    azdo.seed_work_item(WORK_ITEM, "Ship the flux capacitor")
    return azdo


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


# ----------------------------------------------------------------------
# Join 1: workitem.commented → normalizer → dispatch → durable run
# ----------------------------------------------------------------------


class TestWorkItemJoin:
    async def test_webhook_payload_reaches_the_gate_over_the_real_dispatch(self, db, fake):
        metadata = normalize_workitem_comment(load_json("workitem_commented_implement.json"))
        assert metadata is not None and metadata["command"] == "start_run"

        await execute_azure_run_command(
            make_settings(),
            ForgeConfig(),
            db,
            metadata,
            stack_factory=lambda project, repo: make_stack(fake),
        )

        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.provider == "azure_devops"
        assert run.issue_iid == WORK_ITEM
        assert run.base_sha == BASE_HEAD
        # The plan was posted on the WORK ITEM the payload named.
        (plan,) = fake.comments[WORK_ITEM]
        assert "Forge plan" in plan and run.plan_digest in plan

    async def test_forges_own_comment_never_reaches_the_dispatch(self):
        """The bot-loop guard runs BEFORE normalization: forge's own plan
        comment re-triggers workitem.commented but must stay inert."""
        from forge.gateway.azure_webhook import is_bot_identity

        assert is_bot_identity(BOT, make_settings().FORGE_AZDO_BOT_NAME)


# ----------------------------------------------------------------------
# Join 2: /go lane leg → journaled handle → executor + template + cancel
# ----------------------------------------------------------------------


class TestLaneHandleJoin:
    async def test_go_journals_a_handle_the_executor_and_cancel_consume(self, db, fake):
        settings = make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        service = AzureRunService(
            db, settings, ForgeConfig(), stack=make_stack(fake), repo_full_name=f"{PROJECT}/{REPO}"
        )
        run_id = await service.start_run(
            project_id=7,
            issue_number=WORK_ITEM,
            issue_title="Ship the flux capacitor",
            issue_description="",
            author_username=HUMAN,
        )
        await service.handle_go(
            project_id=7,
            issue_number=WORK_ITEM,
            note_text=f"/go {run_id}",
            author_username=HUMAN,
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        evidence = run.evidence or {}
        assert evidence["backend"] == "ci_harness"

        # The journaled handle round-trips and carries the dispatch facts.
        handle = JournaledHandle.from_json(evidence["harness"]["handle"])
        assert handle.provider == "azure_devops"
        assert handle.pipeline_id == LANE_PIPELINE_ID
        assert handle.run_id == LANE_RUN_ID  # correlated from the dispatch response
        assert handle.branch == azure_factory_branch(WORK_ITEM, run_id)
        assert handle.attempt_base == run.base_sha == BASE_HEAD
        assert handle.forge_run_id == run_id

        # What AZ-2 dispatched is exactly the lane template's queue-time
        # parameter contract (strings only, research §6.2).
        (dispatch,) = fake.pipeline_calls
        assert dispatch["pipeline_id"] == LANE_PIPELINE_ID
        assert dispatch["ref_name"] == f"refs/heads/{handle.branch}"
        assert dispatch["template_parameters"] == {
            "run_id": run_id,
            "attempt_base": BASE_HEAD,
            "driver": "claude-code",
            "model": str(settings.FORGE_HARNESS_MODEL),
            "work_item_id": str(WORK_ITEM),
            # B04: the envelope binding trio rides the dispatch (the fake's
            # journaled plan-note id + the frozen digests).
            "plan_note_id": dispatch["template_parameters"]["plan_note_id"],
            "envelope_digest": dispatch["template_parameters"]["envelope_digest"],
            "spec_digest": dispatch["template_parameters"]["spec_digest"],
        }
        assert dispatch["template_parameters"]["plan_note_id"].isdigit()
        assert dispatch["template_parameters"]["envelope_digest"]
        assert dispatch["template_parameters"]["spec_digest"]

        # The AZ-3 executor, launched for the SAME lane facts, produces the
        # SAME dispatch — one contract across the slices.
        executor_handle = AzurePipelinesHandle(
            provider="azure_devops",
            project=handle.project,
            repo_id=REPO_GUID,
            pipeline_id=handle.pipeline_id,
            run_id=handle.run_id,
            branch=f"refs/heads/{handle.branch}",
            attempt_base_sha=handle.attempt_base,
            run_spec_digest=handle.run_spec_digest,
            driver=handle.driver,
            model=str(settings.FORGE_HARNESS_MODEL),
            work_item_id=str(WORK_ITEM),
            forge_run_id=handle.forge_run_id,
            started_at=handle.started_at,
            # B04: the re-dispatch carries the envelope binding like the
            # original dispatch.
            plan_note_id=handle.plan_note_id,
            envelope_digest=handle.envelope_digest,
        )
        executor_fake = FakeLaneClient()
        executor = AzurePipelinesExecutor(executor_fake, settings)
        correlated = await executor.launch(executor_handle)
        assert correlated.run_id == LANE_RUN_ID
        assert executor_fake.pipeline_calls[0]["ref_name"] == dispatch["ref_name"]
        assert (
            executor_fake.pipeline_calls[0]["template_parameters"]
            == dispatch["template_parameters"]
        )
        # The reconciler polls the journaled run id (runId == buildId).
        outcome = await executor.poll(correlated)
        assert outcome.status == "running"

        # The reactive cancel consumes the SAME journaled handle: the
        # grant is revoked, then the lane build is cancelled by its id.
        await service.handle_cancel(
            project_id=7,
            issue_number=WORK_ITEM,
            note_text=f"/cancel {run_id}",
            author_username=HUMAN,
        )
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert fake.cancelled_builds == [LANE_RUN_ID]

    def test_the_lane_template_declares_exactly_the_dispatched_parameters(self):
        template = yaml.safe_load(TEMPLATE_PATH.read_text())
        parameters = {p["name"]: p for p in template["parameters"]}

        assert set(parameters) == {
            "run_id",
            "attempt_base",
            "driver",
            "model",
            "work_item_id",
            "repair_context",
            # B04: the envelope binding trio (empty defaults).
            "plan_note_id",
            "envelope_digest",
            "spec_digest",
            # R38-02 (#303): the credential DELIVERY reference + the
            # redemption flag (refs only, never a value — parameters are
            # documented "No support for secret values").
            "credential_ref",
            "credential_redeem",
        }
        assert all(spec["type"] == "string" for spec in parameters.values())

    def test_the_lane_template_publishes_only_the_staged_contract(self):
        """Live (2026-09-20): the lane published all of .forge/ including the
        forensic event/usage logs; the control-plane archive allowlist is
        CLOSED (exactly candidate.diff + candidate.meta.json), so the
        candidate failed as harness_artifact_invalid ("6 entries over the
        4-entry cap"). The publish step must stage the contract pair into a
        clean forge-output/ directory, never the control directory."""
        template = yaml.safe_load(TEMPLATE_PATH.read_text())
        (job,) = template["jobs"]
        steps = job["steps"]
        publish = next(
            s for s in steps if str(s.get("displayName", "")) == "Publish candidate artifact"
        )
        assert publish["publish"] == "forge-output"
        stage = next(
            s for s in steps if str(s.get("displayName", "")) == "Stage candidate contract"
        )
        script = str(stage.get("bash", ""))
        assert "forge-output/" in script
        assert "cp .forge/candidate.diff .forge/candidate.meta.json forge-output/" in script


# ----------------------------------------------------------------------
# Join 3: git.pullrequest.updated → review metadata → reactive engine
# ----------------------------------------------------------------------


def humanized_pr_update() -> dict[str, Any]:
    """The §10.4 fixture as a HUMAN-triggered review event (the ingress
    skips forge-authored PRs and forge/* heads before normalization)."""
    payload = load_json("pr_updated_push.json")
    payload["eventType"] = "git.pullrequest.updated"
    payload["resource"]["createdBy"] = {
        "id": "cc33dd44-0000-0000-0000-0000000000cc",
        "displayName": "Dev User",
        "uniqueName": HUMAN,
    }
    payload["resource"]["sourceRefName"] = "refs/heads/dev/topic"
    return payload


class FakeReviewSurface:
    """The reactive review engine's client surface, in memory."""

    def __init__(self) -> None:
        self.iterations = [
            PrIteration(
                id=2,
                source_ref_commit=NEW_HEAD,
                target_ref_commit="t" * 40,
                common_ref_commit="c" * 40,
            ),
        ]
        self.threads: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.status_updates: list[dict[str, Any]] = []
        self.thread_seq = 70

    async def get_pr_iterations(self, project: str, repo: str, pr_id: int) -> list[PrIteration]:
        return list(self.iterations)

    async def get_pr_iteration_changes(
        self, project: str, repo: str, pr_id: int, iteration_id: int, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return [{"changeType": "add", "item": {"path": "/src/rate_limiter.py"}}]

    async def get_item(
        self,
        project: str,
        repo: str,
        path: str,
        *,
        version: str | None = None,
        version_type: str | None = None,
    ) -> dict:
        if path == "/src/rate_limiter.py" and version == NEW_HEAD:
            return {"content": "def limit():\n    return 42\n"}
        raise KeyError(f"{path} at {version}")

    async def list_pr_threads(self, project: str, repo: str, pr_id: int) -> list[dict]:
        return list(self.threads)

    async def create_pr_thread(
        self, project: str, repo: str, pr_id: int, content: str, *, status: int = 1, **kwargs: Any
    ) -> dict[str, Any]:
        self.thread_seq += 1
        thread = {"id": self.thread_seq, "status": status, "comments": [{"content": content}]}
        self.threads.append(thread)
        return dict(thread)

    async def reply_pr_thread(
        self, project: str, repo: str, pr_id: int, thread_id: int, content: str
    ) -> dict:
        self.replies.append({"thread_id": thread_id, "content": content})
        return {"id": 900}

    async def update_thread_status(
        self, project: str, repo: str, pr_id: int, thread_id: int, status: int
    ) -> dict:
        self.status_updates.append({"thread_id": thread_id, "status": status})
        return {"id": thread_id}


class TestReviewJoin:
    async def test_pr_event_metadata_survives_into_the_review(self, db):
        metadata = normalize_pull_request_event(humanized_pr_update())
        assert metadata is not None

        # The exact keys the reactive lane consumes (AZ-3 contract).
        assert metadata["project"] == PROJECT
        assert metadata["repo"] == REPO
        assert metadata["pr_id"] == 512
        assert metadata["head_sha"] == NEW_HEAD
        # The full ref — the lane strips ``refs/heads/`` itself.
        assert metadata["head_branch"] == "refs/heads/dev/topic"
        assert metadata["sender"] == HUMAN
        assert metadata["pr_author"] == HUMAN

        surface = FakeReviewSurface()
        llm = FakeLLM(script=[json.dumps({"verdict": "ok", "summary": "clean", "findings": []})])

        def stack_factory(project: str, repo: str):
            assert (project, repo) == (PROJECT, REPO)
            return surface, AzureReactiveReviewer(llm, surface, settings=make_settings())

        await execute_azure_run_command(
            make_settings(),
            ForgeConfig(),
            db,
            metadata,
            stack_factory=stack_factory,
        )

        # The engine reviewed THIS head on THIS PR — the metadata survived.
        marker = REVIEW_MARKER_TEMPLATE.format(pr_id=512)
        (thread,) = surface.threads
        assert marker in thread["comments"][0]["content"]
        (summary,) = surface.replies
        assert f"head:{NEW_HEAD}" in summary["content"]
        prompt = llm.calls[0]["user"]
        assert f"Pull request: {PROJECT}/{REPO}#512" in prompt
        assert f"Reviewed head: {NEW_HEAD}" in prompt

    async def test_bot_sender_stops_the_join_before_any_paid_call(self, db):
        """``sender`` is the recursion guard's input — the join must carry it."""
        metadata = normalize_pull_request_event(humanized_pr_update())
        assert metadata is not None
        settings = make_settings(FORGE_AZDO_BOT_NAME=BOT)
        metadata["sender"] = BOT

        surface = FakeReviewSurface()
        llm = FakeLLM()

        def stack_factory(project: str, repo: str):
            return surface, AzureReactiveReviewer(llm, surface, settings=settings)

        outcome = await execute_azure_run_command(
            settings,
            ForgeConfig(),
            db,
            metadata,
            stack_factory=stack_factory,
        )

        # The dispatch discards lane outcomes — the guard is proven by the
        # effects: no thread, no model call (the skip reason itself is
        # pinned by the AZ-3 engine tests).
        assert outcome is None
        assert surface.threads == [] and llm.calls == []


# ----------------------------------------------------------------------
# Join 4: build.complete → debug metadata → Pipelines debugger
# ----------------------------------------------------------------------


class FakeDebugSurface:
    """The CI debug lane's client surface, in memory."""

    def __init__(self) -> None:
        self.prs: list[dict[str, Any]] = []
        self.timelines: dict[int, dict] = {}
        self.task_logs: dict[tuple[int, int], str] = {}
        self.threads: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.status_updates: list[dict[str, Any]] = []
        self.thread_seq = 80

    def seed_pr(self, head: str) -> dict[str, Any]:
        pr = {
            "pullRequestId": 512,
            "createdBy": {"uniqueName": HUMAN},
            "lastMergeSourceCommit": {"commitId": head},
            "lastMergeCommit": {"commitId": MERGE_2},  # policy builds merge
        }
        self.prs.append(pr)
        return pr

    def seed_failure(self, log_text: str) -> None:
        self.timelines[88231] = {
            "records": [
                {"id": "j1", "type": "Job", "name": "ci", "result": "failed"},
                {
                    "id": "t1",
                    "type": "Task",
                    "name": "Test the retry path",
                    "identifier": "test.retry",
                    "result": "failed",
                    "log": {"id": 5},
                },
            ]
        }
        self.task_logs[(88231, 5)] = log_text

    async def list_pull_requests(
        self, project: str, repo: str, *, status: str = "active", top: int = 50
    ) -> list[dict[str, Any]]:
        return list(self.prs)

    async def get_timeline(self, project: str, build_id: int) -> dict:
        return self.timelines.get(build_id, {"records": []})

    async def get_task_log(self, project: str, build_id: int, log_id: int) -> str:
        return self.task_logs.get((build_id, log_id), "")

    async def list_pr_threads(self, project: str, repo: str, pr_id: int) -> list[dict]:
        return list(self.threads)

    async def create_pr_thread(
        self, project: str, repo: str, pr_id: int, content: str, *, status: int = 1, **kwargs: Any
    ) -> dict[str, Any]:
        self.thread_seq += 1
        thread = {"id": self.thread_seq, "status": status, "comments": [{"content": content}]}
        self.threads.append(thread)
        return dict(thread)

    async def reply_pr_thread(
        self, project: str, repo: str, pr_id: int, thread_id: int, content: str
    ) -> dict:
        self.replies.append({"thread_id": thread_id, "content": content})
        return {"id": 901}

    async def update_thread_status(
        self, project: str, repo: str, pr_id: int, thread_id: int, status: int
    ) -> dict:
        self.status_updates.append({"thread_id": thread_id, "status": status})
        return {"id": thread_id}


class TestDebugJoin:
    async def test_build_event_metadata_survives_into_the_debugger(self):
        metadata = normalize_build_event(load_json("build_complete_failed.json"))
        assert metadata is not None

        # The exact keys the CI debug lane consumes (AZ-3 contract).
        assert metadata["command"] == "debug_ci"
        assert metadata["project"] == PROJECT
        assert metadata["repo"] == REPO
        assert metadata["build_id"] == 88231
        assert metadata["definition_id"] == LANE_PIPELINE_ID
        assert metadata["result"] == "failed"
        assert metadata["source_version"] == MERGE_2

    async def test_debugger_correlates_the_source_version_and_posts_once(self):
        metadata = normalize_build_event(load_json("build_complete_failed.json"))
        assert metadata is not None

        surface = FakeDebugSurface()
        surface.seed_pr(head=NEW_HEAD)
        surface.seed_failure("##[error] 1 test failed: retry path races the timeout")

        async def debug_runner(failed_jobs, job_logs):
            assert {job.name for job in failed_jobs} == {"Test the retry path"}
            assert job_logs
            return PipelineDebugResult(
                summary="the retry path races the timeout",
                is_flaky=False,
                suggested_actions=["Serialize the retry timer"],
            )

        outcome = await execute_azure_debug_ci_command(
            make_settings(),
            ForgeConfig(),
            None,
            metadata,
            client=surface,
            debug_runner=debug_runner,
        )

        assert outcome is not None
        assert outcome["status"] == "debugged"
        assert outcome["build_id"] == 88231
        assert outcome["head_sha"] == NEW_HEAD  # the PR head, not the merge SHA
        (thread,) = surface.threads
        assert "the retry path races the timeout" in thread["comments"][0]["content"]

    async def test_forge_lane_builds_are_never_re_debugged(self):
        assert is_forge_lane_build(LANE_PIPELINE_ID, LANE_PIPELINE_ID)
        assert not is_forge_lane_build(LANE_PIPELINE_ID, 999)
        assert not is_forge_lane_build(0, LANE_PIPELINE_ID)


# ----------------------------------------------------------------------
# B04/B05: the shipped lane recipe — envelope binding, dispatch-only,
# parameter/env contract
# ----------------------------------------------------------------------


class TestLaneRecipeContractB04B05:
    def lane_yaml(self) -> dict:
        return yaml.safe_load(TEMPLATE_PATH.read_text())

    def test_the_recipe_is_dispatch_only_with_an_explicit_trigger_none(self):
        # B05: without trigger:none the IMPLIED CI trigger queues the lane
        # on every push outside forge's control.
        assert self.lane_yaml()["trigger"] in (None, "none")

    def test_repair_context_flows_parameter_to_variable_to_env(self):
        # B05: $(repair_context) macro-references an undefined VARIABLE —
        # the parameter must reach the env through an explicit mapping.
        text = TEMPLATE_PATH.read_text()
        assert "FORGE_REPAIR_CONTEXT: ${{ parameters.repair_context }}" in text
        assert "FORGE_REPAIR_CONTEXT: $(FORGE_REPAIR_CONTEXT)" in text
        assert "FORGE_REPAIR_CONTEXT: $(repair_context)" not in text

    def test_the_envelope_binding_parameters_exist(self):
        names = {p["name"] for p in self.lane_yaml()["parameters"]}
        assert {"plan_note_id", "envelope_digest", "spec_digest"} <= names

    def test_credentials_are_scoped_to_the_selected_driver(self):
        # C03: every mapped secret comes through a conditional LANE_*
        # variable; no provider secret is mapped unconditionally. The exact
        # per-driver sets live in TestCredentialContractC03.
        template = self.lane_yaml()
        (job,) = template["jobs"]
        env = {}
        for step in job["steps"]:
            env.update(step.get("env") or {})
        for key in (
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ZAI_API_KEY",
            "FORGE_GROK_AUTH",
            "COPILOT_GITHUB_TOKEN",
        ):
            assert env[key].startswith("$(LANE_"), key

    def test_envelope_inputs_render_enforced_with_no_fallback(self):
        # B04: with the binding trio dispatched, a failed render writes
        # .forge/exit=failed and skips the driver — no .forge/brief.md
        # fallback on the enforced path.
        text = TEMPLATE_PATH.read_text()
        assert "envelope-verified brief render FAILED — lane fails closed" in text
        # the fallback stays ONLY on the legacy (no-envelope) branch
        assert "falling back to .forge/brief.md" in text


class TestCredentialContractC03:
    """C03: the shipped recipes' per-driver credential selection is EXACTLY
    the control plane's DRIVER_CREDENTIAL_VARS registry — one source of
    truth, cross-checked, never three drifting copies of conditions."""

    def _azdo_driver_vars(self) -> dict[str, set[str]]:
        import re

        text = (
            Path(__file__).parent.parent / "ci" / "templates" / "forge-lane.azure-pipelines.yml"
        ).read_text()
        # conditional variables:  ${{ if eq(parameters.driver, '<id>') }}: block of LANE_X: $(SECRET)
        mapping: dict[str, set[str]] = {}
        for match in re.finditer(
            r"\$\{\{ if eq\(parameters\.driver, '([^']+)'\) \}\}:\n((\s+LANE_[A-Z_]+: \$\([A-Z_]+\)\n)+)",
            text,
        ):
            driver = match.group(1)
            names = set(re.findall(r"LANE_([A-Z_]+):", match.group(2)))
            mapping.setdefault(driver, set()).update(names)
        return mapping

    def test_azdo_matrix_matches_the_registry_exactly(self):
        from forge.runs.harness_selection import DRIVER_CREDENTIAL_VARS, SHIPPED_DRIVERS

        matrix = self._azdo_driver_vars()
        for driver in SHIPPED_DRIVERS:
            # R28-21: the dotnet lane is a GitLab-lane recipe in this
            # slice — the AzDO dispatch surface carries no arm for it and
            # the registry deliberately requires no per-driver secrets
            # (it rides the forge gateway), so there is nothing to map.
            if driver == "dotnet-lane":
                assert driver not in matrix, "add a registry entry before mapping secrets"
                continue
            from forge.runs.harness_selection import DRIVER_OPTIONAL_CREDENTIAL_VARS

            allowed = set(DRIVER_CREDENTIAL_VARS[driver]) | set(
                DRIVER_OPTIONAL_CREDENTIAL_VARS.get(driver, ())
            )
            mapped = matrix.get(driver, set())
            # the recipe maps a NON-EMPTY subset of the allowed surfaces —
            # never anything outside the contract.
            assert mapped and mapped <= allowed, (
                f"{driver}: template maps {mapped}, registry allows {allowed}"
            )
            # grok-build's auth blob and opencode/copilot keys are REQUIRED
            required = {
                "grok-build": {"FORGE_GROK_AUTH"},
                "opencode": {"ZAI_API_KEY"},
                "copilot": {"COPILOT_GITHUB_TOKEN"},
            }
            assert mapped >= required.get(driver, set()), driver

    def test_github_workflow_matches_the_registry_exactly(self):
        import re

        from forge.runs.harness_selection import (
            DRIVER_CREDENTIAL_VARS,
            DRIVER_OPTIONAL_CREDENTIAL_VARS,
            SHIPPED_DRIVERS,
        )

        text = (
            Path(__file__).parent.parent / ".github" / "workflows" / "forge-harness.yml"
        ).read_text()
        for driver in SHIPPED_DRIVERS:
            expected = set(DRIVER_CREDENTIAL_VARS[driver])
            granted: set[str] = set()
            for match in re.finditer(
                r"^          ([A-Z_]+): \$\{\{.*?inputs\.driver == '([a-z-]+)'.*?\}\}$",
                text,
                re.M,
            ):
                # One env line may guard SEVERAL drivers (the ANTHROPIC_*
                # lines cover claude-code AND the EXE-02 claude-sdk-lane,
                # which shares the recipe) — credit every arm on the line.
                arms = re.findall(r"inputs\.driver == '([a-z-]+)'", match.group(0))
                if driver in arms:
                    granted.add(match.group(1))
            allowed = expected | set(DRIVER_OPTIONAL_CREDENTIAL_VARS.get(driver, ()))
            assert granted <= allowed and granted >= expected, (
                f"{driver}: github workflow grants {granted}, "
                f"registry requires {expected} (allows {allowed})"
            )

    def test_enforced_dispatch_without_a_read_token_never_reaches_the_driver(self):
        text = (
            Path(__file__).parent.parent / "ci" / "templates" / "forge-lane.azure-pipelines.yml"
        ).read_text()
        # the prerequisite check fires on ANY envelope input present
        assert "enforced dispatch missing prerequisite (read token or envelope inputs)" in text
        # partial envelope inputs are also a missing prerequisite
        assert (
            '[ -n "${FORGE_PLAN_NOTE_ID:-}" ] \\\n             || [ -n "${FORGE_ENVELOPE_DIGEST:-}" ] \\\n             || [ -n "${FORGE_SPEC_DIGEST:-}" ]'
            in text
        )
