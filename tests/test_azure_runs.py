"""Azure DevOps run service tests (AZ-2): the plan + human gate path.

Drives :class:`forge.runs.azure_service.AzureRunService` over an in-memory
fake of the :class:`forge.integrations.azure.AzureDevOpsClient` surface and
the webhook payload fixtures — the GitHub gate semantics (plan comment as a
work-item comment, pending decision, cancel-as-revoke, one active run)
exercised on the Azure DevOps surface, no network, no model.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings
from forge.durable import (
    ActionLog,
    FlowRun,
    FlowStatus,
    GateApproval,
    Outbox,
    RunSpec,
    StepRun,
    as_aware_utc,
)
from forge.factory.llm import LLMError, LLMResult
from forge.factory.reviewer import ReviewVerdict
from forge.gateway.azure_webhook import normalize_workitem_comment
from forge.integrations.azure import (
    AzureDevOpsDriftError,
    AzureDevOpsError,
    AzureDevOpsNotFoundError,
    AzureRepositoryReader,
    CommitPayload,
    PipelineRun,
)
from forge.models.base import Base
from forge.runs.admission import approvers_for, check_admission
from forge.runs.azure_service import (
    AzureAgents,
    AzurePipelinesHandle,
    AzurePRReviewer,
    AzureRunService,
    azure_factory_branch,
    execute_azure_run_command,
    _resolve_repo_name,
)
from forge.runs.stubs import StubImplementer, StubPlanner

FIXTURES = Path(__file__).parent / "fixtures" / "azure_payloads"
PROJECT = "Fabrikam"
REPO = "core"
REPO_FULL = f"{PROJECT}/{REPO}"
PROJECT_GUID = "9f8e7d6c-0000-0000-0000-000000000009"
REPO_GUID = "1a2b3c4d-0000-0000-0000-000000000001"
WORK_ITEM = 142
WORK_ITEM_TITLE = "Ship the flux capacitor"
WORK_ITEM_DESC_HTML = "<p>Users <b>cannot</b> reset</p>"
BASE_HEAD = "1" * 40
LANE_PIPELINE_ID = 207


def azure_project_key(project_id: str) -> int:
    from forge.gateway.azure_webhook import azure_project_key as key

    return key(project_id)


PROJECT_ID = azure_project_key(PROJECT_GUID)


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="gitlab-person",
        FORGE_AZDO_APPROVERS="dev@fabrikam.example",
        FORGE_AZDO_ORG_URL="https://dev.azure.com/fabrikam",
        FORGE_AZDO_LANE_PIPELINE_ID=None,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


class FakeAzureDevOps:
    """In-memory Azure DevOps covering the run-service + reader surface.

    Mirrors the PARSED semantics of the real client: branch-wide CAS on
    ``push_commits`` (a moved head surfaces as
    :class:`AzureDevOpsDriftError`, never a silent overwrite) and the
    per-ref ``GitRefUpdateResult`` response shape (research §3.3/§10.11).
    """

    def __init__(self) -> None:
        # branch name -> head sha
        self.heads: dict[str, str] = {"main": BASE_HEAD}
        # sha -> path -> text content (snapshots created by pushes/seeds)
        self.snapshots: dict[str, dict[str, str]] = {BASE_HEAD: dict(self._seed_files)}
        self.work_items: dict[int, dict] = {}
        # work item id -> list of comment bodies, chronological
        self.work_item_comments: dict[int, list[str]] = {}
        self.work_item_links: list[dict] = []
        self.pull_requests: list[dict] = []
        # The project's repositories as list_repositories returns them
        # (None = the listing fails — the fallback path).
        self.repositories: list[dict] | None = []
        self.pipeline_calls: list[dict] = []
        self.cancelled_builds: list[int] = []
        self.calls: list[tuple[str, tuple]] = []
        self._next_oid = 1
        self._next_pr = 500

    # -- seeding --------------------------------------------------------------

    @property
    def _seed_files(self) -> dict[str, str]:
        return {"src/app.py": "print('hi')\n"}

    def seed_work_item(self, number: int, title: str, description: str) -> None:
        self.work_items[number] = {
            "id": number,
            "fields": {
                "System.Title": title,
                "System.Description": description,
                "System.State": "Approved",
            },
        }

    def seed_snapshot(self, sha: str, files: dict[str, str]) -> None:
        self.snapshots[sha] = dict(files)

    def fail_next_push(self) -> None:
        self._push_failure = True  # type: ignore[attr-defined]

    # -- ids ----------------------------------------------------------------

    def _oid(self) -> str:
        self._next_oid += 1
        return f"{self._next_oid:040x}"

    def _number(self) -> int:
        self._next_pr += 1
        return self._next_pr

    def calls_of(self, name: str) -> list[tuple[str, tuple]]:
        return [call for call in self.calls if call[0] == name]

    # -- work items ---------------------------------------------------------

    async def link_work_item_to_pr(
        self, project: str, work_item_id: int, project_id: str, repo_id: str, pr_id: int
    ) -> dict:
        """The AZ-4 client method (the WIT ArtifactLink PATCH)."""
        self.calls.append(
            ("link_work_item_to_pr", (project, work_item_id, project_id, repo_id, pr_id))
        )
        self.work_item_links.append(
            {
                "method": "PATCH",
                "path": f"/{project}/_apis/wit/workItems/{work_item_id}",
                "body": [
                    {
                        "op": "add",
                        "path": "/relations/-",
                        "value": {
                            "rel": "ArtifactLink",
                            "url": f"vstfs:///Git/PullRequestId/{project_id}%2F{repo_id}%2F{pr_id}",
                            "attributes": {"name": "Pull Request"},
                        },
                    }
                ],
            }
        )
        return {"id": work_item_id, "rev": 99}

    async def get_work_item(self, project: str, work_item_id: int) -> dict:
        self.calls.append(("get_work_item", (project, work_item_id)))
        item = self.work_items.get(work_item_id)
        if item is None:
            raise AzureDevOpsNotFoundError(404, f"work item {work_item_id} not found")
        return item

    async def add_work_item_comment(self, project: str, work_item_id: int, text: str) -> dict:
        self.calls.append(("add_work_item_comment", (project, work_item_id)))
        comments = self.work_item_comments.setdefault(work_item_id, [])
        comments.append(text)
        return {"workItemId": work_item_id, "commentId": len(comments), "text": text}

    # -- refs / branches --------------------------------------------------------

    async def get_branch_head(self, project: str, repo: str, branch: str) -> str:
        self.calls.append(("get_branch_head", (project, repo, branch)))
        head = self.heads.get(branch)
        if head is None:
            raise AzureDevOpsNotFoundError(404, f"branch head not found for {branch!r}")
        return head

    async def create_branch_from(self, project: str, repo: str, branch: str, base_sha: str) -> dict:
        self.calls.append(("create_branch_from", (project, repo, branch, base_sha)))
        if branch in self.heads:
            raise AzureDevOpsDriftError(f"refs/heads/{branch}", "alreadyExists", "ref exists")
        self.heads[branch] = base_sha
        return {
            "value": [
                {
                    "name": f"refs/heads/{branch}",
                    "oldObjectId": "0" * 40,
                    "newObjectId": base_sha,
                    "updateStatus": "succeeded",
                    "success": True,
                }
            ]
        }

    async def push_commits(
        self,
        project: str,
        repo: str,
        branch: str,
        *,
        expected_old_sha: str,
        commits: list[CommitPayload],
    ) -> dict:
        self.calls.append(("push_commits", (project, repo, branch, expected_old_sha, len(commits))))
        head = self.heads.get(branch)
        if head != expected_old_sha:
            raise AzureDevOpsDriftError(
                f"refs/heads/{branch}",
                "staleObjectId",
                "the old object id does not match the current tip",
            )
        files = dict(self.snapshots.get(head, {}))
        for commit in commits:
            for change in commit.changes:
                path = change.path.lstrip("/")
                if change.change_type == "delete":
                    files.pop(path, None)
                else:
                    files[path] = change.content or ""
        new_sha = self._oid()
        self.heads[branch] = new_sha
        self.snapshots[new_sha] = files
        return {
            "value": [
                {
                    "name": f"refs/heads/{branch}",
                    "oldObjectId": expected_old_sha,
                    "newObjectId": new_sha,
                    "updateStatus": "succeeded",
                    "success": True,
                }
            ]
        }

    # -- pull requests ---------------------------------------------------------

    async def create_draft_pr(
        self,
        project: str,
        repo: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> dict:
        self.calls.append(("create_draft_pr", (project, repo, source_branch, target_branch, title)))
        pr_id = self._number()
        pr = {
            "pullRequestId": pr_id,
            "title": title,
            "description": description,
            "creationDate": f"2026-09-15T10:0{self._next_pr % 10}:00.0000000Z",
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "isDraft": True,
            "status": "active",
            "repository": {
                "id": REPO_GUID,
                "name": repo,
                "project": {"id": PROJECT_GUID, "name": project},
            },
            "_links": {
                "web": {
                    "href": f"https://dev.azure.com/fabrikam/{project}/_git/{repo}/pullrequest/{pr_id}"
                }
            },
        }
        self.pull_requests.append(pr)
        return pr

    async def find_draft_pr_by_head(
        self, project: str, repo: str, source_branch: str
    ) -> dict | None:
        self.calls.append(("find_draft_pr_by_head", (project, repo, source_branch)))
        wanted = source_branch.removeprefix("refs/heads/")
        matches = [
            pr
            for pr in self.pull_requests
            if pr.get("isDraft") is True
            and str(pr.get("sourceRefName") or "").removeprefix("refs/heads/") == wanted
        ]
        matches.sort(
            key=lambda pr: (str(pr.get("creationDate") or ""), int(pr["pullRequestId"])),
            reverse=True,
        )
        return matches[0] if matches else None

    # -- repositories (the AZ-4 repo-resolution surface) -------------------------

    async def list_repositories(self, project: str) -> list[dict]:
        self.calls.append(("list_repositories", (project,)))
        if self.repositories is None:
            raise AzureDevOpsError(400, "TF400813: resource not available")
        return self.repositories

    async def get_pr(self, project: str, repo: str, pr_id: int) -> dict:
        for pr in self.pull_requests:
            if pr["pullRequestId"] == pr_id:
                return pr
        raise AzureDevOpsNotFoundError(404, f"PR {pr_id} not found")

    # -- pipelines -------------------------------------------------------------

    async def run_pipeline(
        self,
        project: str,
        pipeline_id: int,
        *,
        ref_name: str,
        template_parameters: dict | None = None,
        variables: dict | None = None,
    ) -> PipelineRun:
        self.calls.append(("run_pipeline", (project, pipeline_id, ref_name)))
        self.pipeline_calls.append(
            {
                "project": project,
                "pipeline_id": pipeline_id,
                "ref_name": ref_name,
                "template_parameters": dict(template_parameters or {}),
            }
        )
        return PipelineRun(run_id=99001, state="inProgress", result=None, url=None)

    async def cancel_build(self, project: str, build_id: int) -> dict:
        self.calls.append(("cancel_build", (project, build_id)))
        self.cancelled_builds.append(build_id)
        return {"status": "cancelling"}

    # -- items / trees (the reader + reviewer surface) --------------------------

    async def get_item(
        self,
        project: str,
        repo: str,
        path: str,
        *,
        version: str | None = None,
        version_type: str | None = None,
    ) -> dict:
        self.calls.append(("get_item", (project, repo, path, version)))
        sha = version or BASE_HEAD
        files = self.snapshots.get(sha, {})
        key = path.lstrip("/")
        if key not in files:
            raise AzureDevOpsNotFoundError(404, f"{path} not found at {sha}")
        return {
            "path": path,
            "content": files[key],
            "contentMetadata": {"size": len(files[key].encode())},
        }

    async def get_tree(
        self, project: str, repo: str, tree_id: str, *, recursive: bool = False
    ) -> dict:
        self.calls.append(("get_tree", (project, repo, tree_id, recursive)))
        files = self.snapshots.get(tree_id, {})
        return {
            "treeEntries": [
                {"relativePath": path, "objectId": self._oid(), "gitObjectType": "blob"}
                for path in files
            ],
            "truncated": False,
        }

    async def get_refs(self, project: str, repo: str, filter: str | None = None) -> list[dict]:
        self.calls.append(("get_refs", (project, repo, filter)))
        refs = []
        for branch, head in self.heads.items():
            name = f"heads/{branch}"
            if filter is None or filter == name or filter.rstrip("/") == name:
                refs.append({"name": f"refs/heads/{branch}", "objectId": head})
        return refs

    async def get_repository(self, project: str, repo: str) -> dict:
        self.calls.append(("get_repository", (project, repo)))
        return {
            "id": REPO_GUID,
            "name": repo,
            "defaultBranch": "refs/heads/main",
            "project": {"id": PROJECT_GUID, "name": project},
        }


class StubAzureReviewer:
    """Deterministic PR verdict — the Azure-shaped review surface."""

    def __init__(self, verdict: str = "ok", summary: str = "clean implementation") -> None:
        self.verdict = verdict
        self.summary = summary
        self.calls: list[dict] = []

    async def review(
        self,
        *,
        project: str,
        repo: str,
        pr_id: int,
        issue_title: str,
        plan_summary: str,
        base_sha: str,
        candidate_sha: str,
        flow_run_id: str | None = None,
    ) -> ReviewVerdict:
        self.calls.append(
            {
                "project": project,
                "repo": repo,
                "pr_id": pr_id,
                "candidate_sha": candidate_sha,
                "base_sha": base_sha,
            }
        )
        return ReviewVerdict(verdict=self.verdict, summary=self.summary, findings=())


class BoomPlanner:
    """Must never be constructed a prompt (admission denies first)."""

    async def plan(self, *args, **kwargs):
        raise AssertionError("planner ran for an admission-denied /implement")


def make_stack(
    fake: FakeAzureDevOps,
    *,
    planner=None,
    implementer=None,
    reviewer=None,
) -> AzureAgents:
    reader = AzureRepositoryReader(fake, PROJECT, REPO)
    return AzureAgents(
        client=fake,
        reader=reader,
        planner=planner or StubPlanner(),
        implementer=implementer or StubImplementer(),
        reviewer=reviewer or StubAzureReviewer(),
    )


def make_service(
    db,
    fake: FakeAzureDevOps,
    *,
    settings: Settings | None = None,
    stack: AzureAgents | None = None,
) -> AzureRunService:
    return AzureRunService(
        db,
        settings or make_settings(),
        ForgeConfig(),
        stack=stack or make_stack(fake),
        repo_full_name=REPO_FULL,
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
def fake() -> FakeAzureDevOps:
    azdo = FakeAzureDevOps()
    azdo.seed_work_item(WORK_ITEM, WORK_ITEM_TITLE, WORK_ITEM_DESC_HTML)
    return azdo


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def start(service: AzureRunService, author: str = "dev@fabrikam.example") -> str:
    return await service.start_run(
        project_id=PROJECT_ID,
        issue_number=WORK_ITEM,
        issue_title=WORK_ITEM_TITLE,
        issue_description=WORK_ITEM_DESC_HTML,
        author_username=author,
    )


async def go(
    service: AzureRunService,
    run_id: str,
    author: str = "dev@fabrikam.example",
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=WORK_ITEM,
        note_text=note or f"/go {run_id}",
        author_username=author,
        now=now,
    )


def comments(fake: FakeAzureDevOps) -> list[str]:
    return fake.work_item_comments.get(WORK_ITEM, [])


def clear_comments(fake: FakeAzureDevOps) -> None:
    fake.work_item_comments[WORK_ITEM] = []


# ----------------------------------------------------------------------
# /implement: plan comment + waiting_approval (no PR yet!)
# ----------------------------------------------------------------------


class TestImplement:
    async def test_implement_parks_the_run_at_the_gate(self, db, fake):
        service = make_service(db, fake)

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        # The FlowRun row carries the Azure subject identity (AZ-2 schema).
        assert run.provider == "azure_devops"
        assert run.issue_iid == WORK_ITEM
        assert run.project_id == PROJECT_ID
        assert run.base_sha == BASE_HEAD  # the frozen base, pinned at plan time
        assert run.plan_digest

        # The frozen RunSpec v2 exists; its digest is what the decision binds.
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.digest == run.spec_digest
        assert spec.schema_version == 2
        assert spec.document["subject"]["provider"] == "azure_devops"
        assert spec.document["subject"]["repo_full_name"] == REPO_FULL
        assert spec.document["source_base_oid"] == BASE_HEAD

        # The pending decision exists, carrying the plan/base/spec/task digests.
        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is None
        assert gate.plan_digest == run.plan_digest
        assert gate.base_sha == BASE_HEAD
        assert gate.spec_digest == run.spec_digest
        assert gate.task_digest
        assert as_aware_utc(gate.expires_at) > datetime.now(timezone.utc)

        # The plan was posted as ONE work-item comment with the digest and
        # the full-id /go instruction.
        (body,) = comments(fake)
        assert "Forge plan" in body
        assert run.plan_digest in body
        assert f"/go {run_id}" in body

        # NO branch, push or PR yet — publishing happens only after /go.
        assert fake.calls_of("create_branch_from") == []
        assert fake.calls_of("push_commits") == []
        assert fake.calls_of("create_draft_pr") == []

    async def test_plan_comment_carries_the_implementation_block(self, db, fake):
        """ADR-0023 §4: the gate sees the execution shape."""
        service = make_service(db, fake)

        run_id = await start(service)

        (body,) = comments(fake)
        assert "## Implementation" in body
        assert f"- Harness: **claude-code** · model {make_settings().FORGE_HARNESS_MODEL}" in body
        assert "- Fallbacks: none" in body
        assert "- Budget class: standard" in body
        assert "- Commit cycles: 3" in body
        assert "- Selection reason: default" in body
        assert body.index("## Implementation") < body.index("Plan digest")
        assert body.index("## Implementation") < body.index(f"/go {run_id}")

        run = await get_run(db, run_id)
        assert (run.evidence or {})["harness_selection"]["harness"] == "claude-code"
        assert (run.evidence or {})["subject"]["repo_full_name"] == REPO_FULL

    async def test_lane_settings_freeze_the_ci_harness_selection(self, db, fake):
        """FORGE_AZDO_LANE_PIPELINE_ID: the frozen backend_config names the
        lane pipeline + driver — the dispatch inputs come FROM the spec."""
        settings = make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        service = make_service(db, fake, settings=settings)

        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        backend = spec.document["backend_config"]
        assert backend["backend"] == "ci_harness"
        assert backend["lane_pipeline_id"] == LANE_PIPELINE_ID
        assert backend["driver"] == "claude-code"
        (body,) = comments(fake)
        assert "## Implementation" in body and "**claude-code**" in body

    async def test_non_approver_implement_is_denied_before_any_model_call(self, db, fake):
        service = make_service(db, fake, stack=make_stack(fake, planner=BoomPlanner()))

        run_id = await start(service, author="mallory@fabrikam.example")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "admission_denied" in (run.status_reason or "")
        (body,) = comments(fake)
        assert "admission denied" in body and "mallory" in body

    async def test_planning_failure_blocks_the_run(self, db, fake):
        class FailingPlanner:
            async def plan(self, *args, **kwargs):
                raise LLMError("proxy down")

        service = make_service(db, fake, stack=make_stack(fake, planner=FailingPlanner()))

        with pytest.raises(LLMError):
            await start(service)

        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
        # Tier 1: an unclassified failure is fatal — it parks blocked for a
        # human instead of failed.
        assert run.status == FlowStatus.BLOCKED.value
        assert "planning_failed" in (run.status_reason or "")


async def _only_run_id(db) -> str:
    async with db() as session:
        return (await session.execute(select(FlowRun.id))).scalars().one()


# ----------------------------------------------------------------------
# Connection-scoped approvers: FORGE_AZDO_APPROVERS
# ----------------------------------------------------------------------


class TestApproverScoping:
    """The Azure DevOps connection resolves its own approver list — the
    shared FORGE_APPROVERS (GitLab usernames) must never authorize a run."""

    def test_azure_approvers_resolve_independently(self):
        settings = make_settings(
            FORGE_APPROVERS="gitlab-person,dev@fabrikam.example",
            FORGE_AZDO_APPROVERS="dev@fabrikam.example",
        )

        assert approvers_for("azure_devops", settings) == frozenset({"dev@fabrikam.example"})
        assert approvers_for("gitlab", settings) == frozenset(
            {"gitlab-person", "dev@fabrikam.example"}
        )

    def test_empty_azure_list_falls_back_to_the_shared_list(self):
        settings = make_settings(FORGE_AZDO_APPROVERS="", FORGE_APPROVERS="dev@fabrikam.example")

        assert approvers_for("azure_devops", settings) == frozenset({"dev@fabrikam.example"})

    def test_bot_in_the_azure_list_denies_runs(self):
        settings = make_settings(
            FORGE_AZDO_APPROVERS="forge-bot,dev@fabrikam.example",
            FORGE_BOT_USERNAME="forge-bot",
        )

        decision = check_admission(
            settings, ForgeConfig(), PROJECT_ID, "dev@fabrikam.example", provider="azure_devops"
        )

        assert decision.allowed is False
        assert "must not appear in FORGE_AZDO_APPROVERS" in decision.reason

    async def test_gitlab_only_login_cannot_start_an_azure_run(self, db, fake):
        settings = make_settings(FORGE_APPROVERS="gitlab-person")
        service = make_service(
            db, fake, settings=settings, stack=make_stack(fake, planner=BoomPlanner())
        )

        run_id = await start(service, author="gitlab-person")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "admission_denied" in (run.status_reason or "")

    async def test_go_from_a_gitlab_only_login_is_ignored(self, db, fake):
        settings = make_settings(FORGE_APPROVERS="gitlab-person")
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id, author="gitlab-person")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert fake.calls_of("push_commits") == []
        assert fake.calls_of("create_draft_pr") == []


# ----------------------------------------------------------------------
# /go: the gate and the builtin publish leg
# ----------------------------------------------------------------------


class TestGoBuiltin:
    async def test_go_publishes_and_reaches_ready_for_human(self, db, fake):
        reviewer = StubAzureReviewer()
        service = make_service(db, fake, stack=make_stack(fake, reviewer=reviewer))
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

        # The decision was consumed exactly once.
        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is not None

        # The factory branch was cut at the FROZEN base, then one CAS push.
        (branch_call,) = fake.calls_of("create_branch_from")
        branch = azure_factory_branch(WORK_ITEM, run_id)
        assert branch_call[1][2] == branch
        assert branch_call[1][3] == BASE_HEAD
        (push_call,) = fake.calls_of("push_commits")
        assert push_call[1][2] == branch
        assert push_call[1][3] == BASE_HEAD  # the CAS expected head

        # The Draft PR (isDraft) exists and the run carries it.
        (pr,) = fake.pull_requests
        assert pr["isDraft"] is True
        assert pr["sourceRefName"] == f"refs/heads/{branch}"
        assert run.mr_iid == pr["pullRequestId"]
        assert run.candidate_shas == [fake.heads[branch]]
        evidence = run.evidence or {}
        assert evidence["published_candidate"]["base"] == BASE_HEAD
        assert evidence["published_candidate"]["pr_id"] == pr["pullRequestId"]

        # The evidence comment carries the PR link, the candidate sha and the
        # branch-policy verification note (YAML pr: triggers are ignored on
        # Azure Repos — Build validation is the contract's enforcement point).
        evidence_notes = [body for body in comments(fake) if "ready for human review" in body]
        assert len(evidence_notes) == 1
        assert pr["_links"]["web"]["href"] in evidence_notes[0]
        assert fake.heads[branch] in evidence_notes[0]
        assert "Build validation" in evidence_notes[0]

        # The readonly review ran over the PR diff and is bound to the sha.
        (review_call,) = reviewer.calls
        assert review_call["pr_id"] == pr["pullRequestId"]
        assert review_call["candidate_sha"] == fake.heads[branch]
        assert evidence["review"]["sha"] == fake.heads[branch]
        assert evidence["review"]["verdict"] == "ok"

        # The run walked waiting_ci → evaluating_ci → reviewing → ready.
        async with db() as session:
            targets = [
                row.payload["to"]
                for row in (
                    await session.execute(
                        select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            ]
        assert targets[-4:] == [
            FlowStatus.WAITING_CI.value,
            FlowStatus.EVALUATING_CI.value,
            FlowStatus.REVIEWING.value,
            FlowStatus.READY_FOR_HUMAN.value,
        ]

    async def test_work_item_is_linked_to_the_draft_pr(self, db, fake):
        """research §4.6: the reliable link is the WIT ArtifactLink PATCH."""
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id)

        (link,) = fake.work_item_links
        assert link["method"] == "PATCH"
        assert f"/_apis/wit/workItems/{WORK_ITEM}" in link["path"]
        value = link["body"][0]["value"]
        assert value["rel"] == "ArtifactLink"
        assert value["attributes"]["name"] == "Pull Request"
        assert value["url"].startswith("vstfs:///Git/PullRequestId/")
        run = await get_run(db, run_id)
        assert (run.evidence or {})["published_candidate"]["work_item_link"] is True

    async def test_an_open_draft_on_the_branch_is_adopted_not_duplicated(self, db, fake):
        """AZ-4 dedupe: create_draft_pr is non-idempotent — an open draft on
        the run's own source branch (a previous leg's PR) is adopted, never
        forked a second time."""
        service = make_service(db, fake)
        run_id = await start(service)
        branch = azure_factory_branch(WORK_ITEM, run_id)
        # A PR a previous publish leg created and the run row lost track of.
        existing_pr = {
            "pullRequestId": 4242,
            "title": "forge: implement #142 (run earlier-leg)",
            "creationDate": "2026-09-15T09:00:00.0000000Z",
            "sourceRefName": f"refs/heads/{branch}",
            "targetRefName": "refs/heads/main",
            "isDraft": True,
            "status": "active",
            "repository": {
                "id": REPO_GUID,
                "name": REPO,
                "project": {"id": PROJECT_GUID, "name": PROJECT},
            },
        }
        fake.pull_requests.append(existing_pr)
        clear_comments(fake)

        await go(service, run_id)

        assert fake.calls_of("create_draft_pr") == []
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert run.mr_iid == 4242
        # The link still lands on the ADOPTED PR (its ids come off the payload).
        (link,) = fake.work_item_links
        assert (
            link["body"][0]["value"]["url"]
            == f"vstfs:///Git/PullRequestId/{PROJECT_GUID}%2F{REPO_GUID}%2F4242"
        )
        assert [pr["pullRequestId"] for pr in fake.pull_requests] == [4242]

    async def test_go_is_idempotent_on_redelivery(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        pushes_after_first = len(fake.calls_of("push_commits"))

        await go(service, run_id)  # re-delivered /go

        assert len(fake.calls_of("push_commits")) == pushes_after_first
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_go_after_ttl_blocks_decision_expired(self, db, fake):
        settings = make_settings(FORGE_DECISION_TTL_SECONDS=3600)
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        clear_comments(fake)

        late = datetime.now(timezone.utc) + timedelta(seconds=3600 + 60)
        await go(service, run_id, now=late)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "decision_expired" in (run.status_reason or "")
        (body,) = comments(fake)
        assert "blocked" in body
        # Nothing published; the decision stays unconsumed.
        assert fake.calls_of("push_commits") == []
        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is None

    async def test_branch_drift_blocks_the_run_without_retry(self, db, fake):
        """ADR-0024 §4: staleObjectId under HTTP 200 is the drift case —
        surfaced as a blocked run, never retried or force-pushed."""
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        # Simulate a concurrent writer: the branch head moves after the cut,
        # before the push CAS — the push must refuse.
        original_push = fake.push_commits
        branch = azure_factory_branch(WORK_ITEM, run_id)

        async def intercept_push(*args, **kwargs):
            fake.heads[branch] = "f" * 40
            return await original_push(*args, **kwargs)

        fake.push_commits = intercept_push  # type: ignore[method-assign]

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "branch_drift" in (run.status_reason or "")
        assert len(fake.calls_of("push_commits")) == 1  # never retried
        (body,) = comments(fake)
        assert "could not publish" in body


# ----------------------------------------------------------------------
# /go: the Pipelines lane (FORGE_AZDO_LANE_PIPELINE_ID)
# ----------------------------------------------------------------------


class TestGoLane:
    async def test_go_dispatches_the_lane_and_parks_in_waiting_harness(self, db, fake):
        settings = make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value

        # The factory branch was cut at the frozen attempt base for the lane
        # to check out detached.
        branch = azure_factory_branch(WORK_ITEM, run_id)
        (branch_call,) = fake.calls_of("create_branch_from")
        assert branch_call[1][2] == branch
        assert branch_call[1][3] == BASE_HEAD

        # One Runs-API dispatch with the lane's string templateParameters.
        (pipeline_call,) = fake.pipeline_calls
        assert pipeline_call["pipeline_id"] == LANE_PIPELINE_ID
        assert pipeline_call["ref_name"] == f"refs/heads/{branch}"
        params = pipeline_call["template_parameters"]
        assert set(params) == {"run_id", "attempt_base", "driver", "model", "work_item_id"}
        assert params["run_id"] == run_id
        assert params["attempt_base"] == BASE_HEAD
        assert params["driver"] == "claude-code"  # the driver frozen in the spec
        assert params["work_item_id"] == str(WORK_ITEM)
        assert all(isinstance(value, str) for value in params.values())

        # The durable journaled handle: the AZ-3 reconciler restarts from it.
        handle = AzurePipelinesHandle.from_json(run.evidence["harness"]["handle"])
        assert handle.provider == "azure_devops"
        assert handle.pipeline_id == LANE_PIPELINE_ID
        assert handle.run_id == 99001
        assert handle.branch == branch
        assert handle.attempt_base == BASE_HEAD
        assert handle.driver == "claude-code"
        assert handle.forge_run_id == run_id
        assert run.evidence["backend"] == "ci_harness"

        # The dispatch was journaled intent-first, and NO PR exists yet — the
        # candidate only reaches the publisher through the AZ-3 reconciler.
        async with db() as session:
            actions = (
                (await session.execute(select(ActionLog).where(ActionLog.flow_run_id == run_id)))
                .scalars()
                .all()
            )
        harness = [action for action in actions if action.action_kind == "harness_start"]
        assert len(harness) == 1 and harness[0].status == "succeeded"
        assert fake.pull_requests == []

    async def test_lane_dispatch_failure_blocks_the_run(self, db, fake):
        settings = make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)

        class ExplodingClient(FakeAzureDevOps):
            async def run_pipeline(self, *args, **kwargs):
                raise AzureDevOpsError(400, "TF400813: pipeline not found")

        exploding = ExplodingClient()
        exploding.seed_work_item(WORK_ITEM, WORK_ITEM_TITLE, WORK_ITEM_DESC_HTML)
        service = make_service(db, exploding, settings=settings, stack=make_stack(exploding))
        run_id = await start(service)

        await go(service, run_id)

        run = await get_run(db, run_id)
        # "pipeline not found" is a config error — Tier 1 parks it blocked
        # for a human instead of scheduling a revive.
        assert run.status == FlowStatus.BLOCKED.value
        assert "harness_start_failed" in (run.status_reason or "")


# ----------------------------------------------------------------------
# One active run per (project, work item)
# ----------------------------------------------------------------------


class TestOneActiveRun:
    async def test_second_implement_while_active_is_refused(self, db, fake):
        service = make_service(db, fake)
        first_id = await start(service)
        clear_comments(fake)

        second_id = await start(service)

        assert second_id == first_id  # the existing run is adopted, not forked
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1
        (refusal,) = comments(fake)
        assert "already active" in refusal
        assert f"/go {first_id}" in refusal

    async def test_partial_index_rejects_a_second_active_run(self, db):
        """The DB invariant of last resort: two active (project, work item)
        rows cannot coexist; a terminal run does not block a fresh one."""
        async with db() as session:
            session.add(
                FlowRun(
                    id="a" * 32,
                    project_id=PROJECT_ID,
                    issue_iid=WORK_ITEM,
                    provider="azure_devops",
                    status=FlowStatus.WAITING_APPROVAL.value,
                )
            )
            await session.commit()
            session.add(
                FlowRun(
                    id="b" * 32,
                    project_id=PROJECT_ID,
                    issue_iid=WORK_ITEM,
                    provider="azure_devops",
                    status=FlowStatus.WAITING_APPROVAL.value,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

        # A terminal run does NOT hold the slot (partial index predicate).
        async with db() as session:
            session.add(
                FlowRun(
                    id="c" * 32,
                    project_id=PROJECT_ID,
                    issue_iid=WORK_ITEM,
                    provider="azure_devops",
                    status=FlowStatus.READY_FOR_HUMAN.value,
                )
            )
            await session.commit()  # must not raise


# ----------------------------------------------------------------------
# /cancel: cancel-as-revoke (F13)
# ----------------------------------------------------------------------


class TestCancel:
    async def test_cancel_revokes_the_grant_and_cancels_scheduled_steps(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)
        async with db() as session:
            session.add(
                StepRun(
                    flow_run_id=run_id,
                    step_name="go",
                    status="scheduled",
                    source_event_id="e" * 64,
                )
            )
            await session.commit()

        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=WORK_ITEM,
            note_text=f"@forge /cancel {run_id}",
            author_username="dev@fabrikam.example",
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True  # the publication grant is revoked
        async with db() as session:
            step = (
                (await session.execute(select(StepRun).where(StepRun.flow_run_id == run_id)))
                .scalars()
                .one()
            )
        assert step.status == "cancelled"
        (body,) = comments(fake)
        assert "cancelled" in body

        # A late /go is ignored: the run is terminal, nothing publishes.
        await go(service, run_id)
        assert fake.calls_of("push_commits") == []
        assert fake.calls_of("create_draft_pr") == []

    async def test_cancel_from_non_approver_is_ignored(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)

        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=WORK_ITEM,
            note_text=f"@forge /cancel {run_id}",
            author_username="mallory@fabrikam.example",
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.cancel_requested is False

    async def test_cancel_without_run_id_targets_the_active_run(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)

        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=WORK_ITEM,
            note_text="@forge /cancel",
            author_username="dev@fabrikam.example",
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value

    async def test_lane_cancel_stops_the_pipeline_run(self, db, fake):
        """/cancel on a waiting_harness run also stops the lane build (the
        journaled run id IS the build id, research §6.5)."""
        settings = make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        await go(service, run_id)

        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=WORK_ITEM,
            note_text=f"/cancel {run_id}",
            author_username="dev@fabrikam.example",
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert fake.cancelled_builds == [99001]

    async def test_mid_leg_cancel_stands_the_publish_down(self, db, fake):
        """F13: a cancel that lands while the publish leg is in flight revokes
        the grant — the leg stands down instead of racing the cancel."""
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)
        # Simulate the cancel having committed its revoke flag between the
        # gate consumption and the publish (the run is not terminal yet).
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.cancel_requested = True
            await session.commit()

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.PROPOSING.value  # the leg stood down
        assert run.candidate_shas == []  # nothing published
        assert fake.calls_of("create_branch_from") == []
        assert fake.calls_of("push_commits") == []
        assert fake.calls_of("create_draft_pr") == []


# ----------------------------------------------------------------------
# Dispatch: webhook payload fixtures → durable step → service
# ----------------------------------------------------------------------


class TestDispatch:
    def payload_metadata(self) -> dict:
        from forge.gateway.azure_webhook import normalize_workitem_comment

        payload = json.loads((FIXTURES / "workitem_commented_implement.json").read_bytes())
        metadata = normalize_workitem_comment(payload)
        assert metadata is not None and metadata["command"] == "start_run"
        return metadata

    async def test_execute_azure_run_command_start_run(self, db, fake):
        metadata = self.payload_metadata()

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
        assert run.project_id == metadata["project_id"]
        # The work item's HTML description was stripped before the planner.
        (body,) = comments(fake)
        assert WORK_ITEM_TITLE in body
        assert "<p>" not in body
        assert "Users cannot reset" in body

    async def test_repo_resolution_uses_the_configured_mapping(self, db, fake, tmp_path):
        """``forge.yml`` azure_devops.default_repos resolves the repo for
        repo-less work-item commands."""
        config_path = tmp_path / "forge.yml"
        config_path.write_text(
            f"forge:\n  azure_devops:\n    default_repos:\n      {PROJECT}: {REPO}\n"
        )
        metadata = self.payload_metadata()

        await execute_azure_run_command(
            make_settings(),
            ForgeConfig(config_path),
            db,
            metadata,
            stack_factory=lambda project, repo: make_stack(fake),
        )

        # The run planned against the configured repo's target branch.
        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
        assert run.base_sha == BASE_HEAD
        (head_call,) = fake.calls_of("get_branch_head")
        assert head_call[1] == (PROJECT, REPO, "main")

    async def test_durable_step_executes_the_azure_command(self, db, fake, monkeypatch):
        """The scheduled command step (gateway ingress) drives the Azure
        service through the SAME claim/execute protocol as the worker."""
        from forge.gateway.azure_webhook import (
            azure_source_event_id,
            normalize_workitem_comment,
        )
        from forge.worker.steps import (
            claim_command_step,
            execute_claimed_step,
            schedule_command_step,
        )

        import forge.worker.steps as steps_module

        payload = json.loads((FIXTURES / "workitem_commented_implement.json").read_bytes())
        metadata = normalize_workitem_comment(payload)
        source_event_id = azure_source_event_id(
            metadata["connection_id"], "workitem.commented", metadata["note_id"]
        )

        async with db() as session:
            async with session.begin():
                await schedule_command_step(session, metadata, source_event_id=source_event_id)

        # The worker calls forge.runs.execute_run_command; route it to the
        # Azure service with the fake stack injected.
        async def routed(settings, forge_config, session_factory, cmd_metadata):
            await execute_azure_run_command(
                settings,
                forge_config,
                session_factory,
                cmd_metadata,
                stack_factory=lambda project, repo: make_stack(fake),
            )

        monkeypatch.setattr(steps_module, "execute_run_command", routed)

        claimed = await claim_command_step(db, "worker-test", source_event_id)
        assert claimed is not None
        await execute_claimed_step(db, make_settings(), ForgeConfig(), claimed)

        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
            step = (await session.execute(select(StepRun))).scalars().one()
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert step.status == "succeeded"
        assert comments(fake)  # the plan comment went out


# ----------------------------------------------------------------------
# Repo resolution for repo-less work-item commands (AZ-4: list_repositories)
# ----------------------------------------------------------------------


class TestRepoResolution:
    async def test_forge_yml_mapping_wins_before_any_api_call(self, fake, tmp_path):
        config_path = tmp_path / "forge.yml"
        config_path.write_text(
            f"forge:\n  azure_devops:\n    default_repos:\n      {PROJECT}: {REPO}\n"
        )

        repo = await _resolve_repo_name(make_settings(), ForgeConfig(config_path), PROJECT)

        assert repo == REPO
        assert fake.calls_of("list_repositories") == []

    async def test_single_repository_resolves_over_the_api(self, fake):
        fake.repositories = [{"id": REPO_GUID, "name": REPO}]

        repo = await _resolve_repo_name(
            make_settings(FORGE_AZDO_PAT="pat-test"), ForgeConfig(), PROJECT, client=fake
        )

        assert repo == REPO
        (call,) = fake.calls_of("list_repositories")
        assert call[1] == (PROJECT,)

    async def test_multi_repository_projects_prefer_the_default_name(self, fake):
        """AzDO creates a repository named like the project — the API-verified
        form of the old name-guessing fallback."""
        fake.repositories = [
            {"id": REPO_GUID, "name": REPO},
            {"id": "b" * 8 + "-0000-0000-0000-000000000002", "name": PROJECT},
        ]

        repo = await _resolve_repo_name(
            make_settings(FORGE_AZDO_PAT="pat-test"), ForgeConfig(), PROJECT, client=fake
        )

        assert repo == PROJECT

    async def test_ambiguous_multi_repository_project_falls_back_with_the_default(self, fake):
        fake.repositories = [{"name": "one"}, {"name": "two"}, {"name": "three"}]

        repo = await _resolve_repo_name(
            make_settings(FORGE_AZDO_PAT="pat-test"), ForgeConfig(), PROJECT, client=fake
        )

        assert repo == PROJECT

    async def test_a_failed_listing_degrades_to_the_default_name(self, fake):
        fake.repositories = None  # the fake raises on the listing

        repo = await _resolve_repo_name(
            make_settings(FORGE_AZDO_PAT="pat-test"), ForgeConfig(), PROJECT, client=fake
        )

        assert repo == PROJECT

    async def test_no_pat_on_the_connection_skips_the_api_leg(self):
        """make_settings() carries no FORGE_AZDO_PAT — resolution must degrade
        to the default name instead of attempting a network call."""

        repo = await _resolve_repo_name(make_settings(), ForgeConfig(), PROJECT)

        assert repo == PROJECT

    async def test_dispatch_resolves_through_the_repositories_list(self, db, fake, monkeypatch):
        """The full dispatch: a repo-less work-item command resolves its repo
        via the AZ-4 client surface, then plans against it."""
        fake.repositories = [{"id": REPO_GUID, "name": REPO}]
        monkeypatch.setattr("forge.runs.azure_service.AzureDevOpsClient", lambda **kwargs: fake)
        payload = json.loads((FIXTURES / "workitem_commented_implement.json").read_bytes())
        metadata = normalize_workitem_comment(payload)
        assert metadata is not None and metadata["command"] == "start_run"

        await execute_azure_run_command(
            make_settings(FORGE_AZDO_PAT="pat-test"),
            ForgeConfig(),
            db,
            metadata,
            stack_factory=lambda project, repo: make_stack(fake),
        )

        (head_call,) = fake.calls_of("get_branch_head")
        assert head_call[1] == (PROJECT, REPO, "main")


# ----------------------------------------------------------------------
# The thin PR reviewer (trees + items diff)
# ----------------------------------------------------------------------


class TestAzurePRReviewer:
    async def test_review_renders_the_candidate_diff(self, db, fake):
        llm_calls: list[dict] = []

        class FakeLLM:
            async def complete(self, **kwargs):
                llm_calls.append(kwargs)
                return LLMResult(
                    text=json.dumps(
                        {
                            "verdict": "ok",
                            "summary": "sound",
                            "findings": [],
                        }
                    ),
                    input_tokens=0,
                    output_tokens=0,
                )

        branch = "forge/wi-review"
        base_files = {"src/app.py": "a = 1\n", "docs/readme.md": "hi\n"}
        candidate = {**base_files, "src/app.py": "a = 2\n", "src/new.py": "new\n"}
        fake.seed_snapshot(BASE_HEAD, base_files)
        candidate_sha = fake._oid()
        fake.heads[branch] = candidate_sha
        fake.seed_snapshot(candidate_sha, candidate)
        reviewer = AzurePRReviewer(FakeLLM(), fake)  # type: ignore[arg-type]

        verdict = await reviewer.review(
            project=PROJECT,
            repo=REPO,
            pr_id=7,
            issue_title=WORK_ITEM_TITLE,
            plan_summary="plan",
            base_sha=BASE_HEAD,
            candidate_sha=candidate_sha,
        )

        assert verdict.verdict == "ok"
        (call,) = llm_calls
        diff = call["user"]
        assert "docs/readme.md" not in diff  # unchanged files are not in the diff
        assert "src/new.py" in diff  # created
        assert "-a = 1" in diff and "+a = 2" in diff  # edited

    async def test_review_without_a_readable_diff_still_verdicts(self, fake):
        llm_prompts: list[str] = []

        class FakeLLM:
            async def complete(self, **kwargs):
                llm_prompts.append(kwargs["user"])
                return LLMResult(
                    text=json.dumps({"verdict": "concerns", "summary": "no diff", "findings": []}),
                    input_tokens=0,
                    output_tokens=0,
                )

        reviewer = AzurePRReviewer(FakeLLM(), fake)  # type: ignore[arg-type]

        # An unknown SHA yields an empty (not crashing) diff…
        verdict = await reviewer.review(
            project=PROJECT,
            repo=REPO,
            pr_id=7,
            issue_title=WORK_ITEM_TITLE,
            plan_summary="plan",
            base_sha="nonexistent" * 5,
            candidate_sha="alsomissing" * 5,
        )

        assert verdict.verdict == "concerns"
        assert "Candidate diff" in llm_prompts[0]

        # …and a hard tree-read failure degrades to the placeholder diff.
        async def broken_tree(*args, **kwargs):
            raise AzureDevOpsError(200, "timeline of trees unavailable")

        fake.get_tree = broken_tree  # type: ignore[method-assign]
        verdict = await reviewer.review(
            project=PROJECT,
            repo=REPO,
            pr_id=7,
            issue_title=WORK_ITEM_TITLE,
            plan_summary="plan",
            base_sha=BASE_HEAD,
            candidate_sha=BASE_HEAD,
        )

        assert verdict.verdict == "concerns"
        assert "(diff unavailable)" in llm_prompts[1]


# ----------------------------------------------------------------------
# Unit helpers
# ----------------------------------------------------------------------


class TestUnitHelpers:
    def test_strip_html(self):
        from forge.runs.azure_service import _strip_html

        assert _strip_html("<p>Users <b>cannot</b> reset</p>") == "Users cannot reset"
        assert _strip_html("") == ""

    def test_changeset_to_commits_maps_operations(self):
        from forge.repository import Change, ChangeSet, Operation
        from forge.runs.azure_service import _changeset_to_commits

        changeset = ChangeSet(
            branch="forge/1/x",
            commit_message="forge: implement 1",
            changes=[
                Change(path="new.py", operation=Operation.CREATE, content="x"),
                Change(path="old.py", operation=Operation.UPDATE, content="y"),
                Change(path="gone.py", operation=Operation.DELETE),
            ],
            attempt_base_oid=BASE_HEAD,
        )
        (commit,) = _changeset_to_commits(changeset)
        assert commit.comment == "forge: implement 1"
        by_type = {change.path: change.change_type for change in commit.changes}
        assert by_type == {"/new.py": "add", "/old.py": "edit", "/gone.py": "delete"}

    def test_push_new_object_id_reads_the_ref_result(self):
        from forge.runs.azure_service import _push_new_object_id

        assert (
            _push_new_object_id({"value": [{"newObjectId": "b" * 40, "updateStatus": "succeeded"}]})
            == "b" * 40
        )
        assert _push_new_object_id({}) == ""
