"""Azure DevOps run service tests (AZ-2): the plan + human gate path.

Drives :class:`forge.runs.azure_service.AzureRunService` over an in-memory
fake of the :class:`forge.integrations.azure.AzureDevOpsClient` surface and
the webhook payload fixtures — the GitHub gate semantics (plan comment as a
work-item comment, pending decision, cancel-as-revoke, one active run)
exercised on the Azure DevOps surface, no network, no model.
"""

import hashlib
import json
from dataclasses import replace
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
    RunBudget,
    RunSpec,
    StepRun,
    as_aware_utc,
)
from forge.factory.implementer import IMPLEMENTER_TIER
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
from forge.orchestrator.project_config import clear_cache
from forge.runs.admission import approvers_for, check_admission
from forge.runs.azure_service import (
    AzureAgents,
    AzurePipelinesHandle,
    AzurePRReviewer,
    AzureRunService,
    _strip_html,
    azure_factory_branch,
    evaluate_azure_waiting_ci,
    evaluate_azure_waiting_harness,
    execute_azure_run_command,
    _resolve_repo_name,
)
from forge.runs.service import task_digest_of
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
        FORGE_VERIFICATION_GRACE_SECONDS=0,  # hermetic: grace needs a sleep
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
        # branch name -> [commit dicts newest-first] — the R11 probe surface
        self.commits: dict[str, list[dict]] = {}
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
        # Build objects as the Builds API returns them (R02 verification):
        # seeded with seed_build, listed newest-queue-time first.
        self.builds: list[dict] = []
        self.calls: list[tuple[str, tuple]] = []
        self._next_oid = 1
        self._next_pr = 500
        self._next_build = 300
        # A12 delayed-apply mode: accepted pushes are NOT applied until
        # flush_delayed_pushes() — the provider window in which a probe
        # still sees the old head. The flush applies each pending push's
        # branch-wide CAS: a moved head refuses it (counted in
        # delayed_refused).
        self.delayed_apply: bool = False
        self.delayed_refused = 0
        self.pending_pushes: list[dict] = []

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

    def seed_commit(
        self, branch: str, sha: str, comment: str, parents: list[str] | None = None
    ) -> None:
        """Record a commit on *branch* and move its head there (probe tests)."""
        self.commits.setdefault(branch, []).insert(
            0, {"commit_id": sha, "comment": comment, "parents": list(parents or [])}
        )
        self.heads[branch] = sha

    def seed_build(
        self,
        *,
        source_version: str,
        definition_id: int = 42,
        definition_name: str = "CI",
        status: str = "completed",
        result: str | None = "succeeded",
    ) -> dict:
        """Queue one Build object for the R02 verification surface."""
        self._next_build += 1
        build = {
            "id": self._next_build,
            "definition": {"id": definition_id, "name": definition_name},
            "sourceVersion": source_version,
            "sourceBranch": "refs/heads/main",
            "status": status,
            "result": result,
            "queueTime": f"2026-09-15T10:{self._next_build % 60:02d}:00Z",
        }
        self.builds.append(build)
        return build

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

    async def list_commits(self, project: str, repo: str, branch: str, top: int = 30) -> list[dict]:
        self.calls.append(("list_commits", (project, repo, branch, top)))
        return [dict(commit) for commit in self.commits.get(branch, [])[:top]]

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
        if self.delayed_apply:
            # A12: ACCEPT the push, apply it later — no CAS evaluation yet,
            # the branch keeps its old head (the negative-probe view).
            self.pending_pushes.append(
                {
                    "branch": branch,
                    "expected_old_sha": expected_old_sha,
                    "commits": list(commits),
                }
            )
            return {
                "value": [
                    {
                        "name": f"refs/heads/{branch}",
                        "oldObjectId": expected_old_sha,
                        "newObjectId": self._oid(),
                        "updateStatus": "succeeded",
                        "success": True,
                    }
                ]
            }
        if head != expected_old_sha:
            raise AzureDevOpsDriftError(
                f"refs/heads/{branch}",
                "staleObjectId",
                "the old object id does not match the current tip",
            )
        return self._apply_push(branch, expected_old_sha=expected_old_sha, commits=commits)

    def _apply_push(
        self, branch: str, *, expected_old_sha: str, commits: list[CommitPayload]
    ) -> dict:
        """Execute one accepted push: snapshot + head move + commit records."""
        head = self.heads.get(branch)
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
        for commit in commits:
            self.commits.setdefault(branch, []).insert(
                0,
                {
                    "commit_id": new_sha,
                    "comment": commit.comment,
                    "parents": [expected_old_sha],
                },
            )
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

    def flush_delayed_pushes(self) -> None:
        """Apply the A12 accepted-but-delayed pushes, in order.

        Each push's branch-wide CAS is evaluated NOW: a push whose
        ``expected_old_sha`` no longer matches the current head is REFUSED
        and dropped (``delayed_refused``) — exactly how the real CAS makes a
        slow first push unable to double an applied duplicate.
        """
        for pending in self.pending_pushes:
            head = self.heads.get(pending["branch"])
            if head != pending["expected_old_sha"]:
                self.delayed_refused += 1
                continue
            self._apply_push(
                pending["branch"],
                expected_old_sha=pending["expected_old_sha"],
                commits=list(pending["commits"]),
            )
        self.pending_pushes = []

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

    async def list_builds_by_repository(
        self,
        project: str,
        repo_id: str,
        *,
        definitions: list[int] | None = None,
        min_time: datetime | None = None,
        top: int = 25,
    ) -> list[dict]:
        """Documented-params Builds query; the fake ignores the window."""
        self.calls.append(
            ("list_builds_by_repository", (project, repo_id, tuple(definitions or ())))
        )
        builds = list(reversed(self.builds))  # newest queue time first
        if definitions:
            wanted = {int(d) for d in definitions}
            builds = [b for b in builds if int(b["definition"]["id"]) in wanted]
        return builds[:top]

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


async def outbox_targets(db, run_id: str) -> list[str]:
    """The journaled transition targets, in order (the durable walk)."""
    async with db() as session:
        return [
            row.payload["to"]
            for row in (
                await session.execute(
                    select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                )
            )
            .scalars()
            .all()
        ]


async def drive_to_waiting_ci(service: AzureRunService, fake: FakeAzureDevOps) -> tuple[str, str]:
    """start_run → /go on the builtin lane → the run parked in waiting_ci.

    Returns (run_id, candidate_sha)."""
    run_id = await start(service)
    clear_comments(fake)
    await go(service, run_id)
    branch = azure_factory_branch(WORK_ITEM, run_id)
    return run_id, fake.heads[branch]


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

        # The frozen EXECUTABLE RunSpec (v3, A02) exists; its digest is what
        # the decision binds.
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.digest == run.spec_digest
        assert spec.schema_version == 3
        assert spec.document["subject"]["provider"] == "azure_devops"
        assert spec.document["subject"]["project_id"] == PROJECT_ID
        assert spec.document["subject"]["issue_iid"] == WORK_ITEM
        assert spec.document["source_base_oid"] == BASE_HEAD
        # R04/A02: the executable content rides in the document.
        assert spec.document["task"]["title"] == WORK_ITEM_TITLE
        assert spec.document["task"]["description"] == _strip_html(WORK_ITEM_DESC_HTML)
        assert spec.document["task"]["digest"] == task_digest_of(
            WORK_ITEM_TITLE, _strip_html(WORK_ITEM_DESC_HTML)
        )
        assert spec.document["plan"]["digest"] == run.plan_digest
        assert spec.document["plan"]["summary"]
        assert spec.document["model_route"] == {"tier": IMPLEMENTER_TIER}
        assert spec.document["verification"]["required_jobs"] == []
        assert spec.document["budgets"] == {
            "commit_cycles": 3,
            "harness_timeout": make_settings().FORGE_HARNESS_TIMEOUT_SECONDS,
        }
        assert spec.document["backend_config"]["backend"] == "builtin"
        assert spec.document["backend_config"]["harness"] == "claude-code"
        assert "lane_pipeline_id" not in spec.document["backend_config"]

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
        assert backend["harness"] == "claude-code"
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

    async def test_planning_failure_parks_the_run_blocked(self, db, fake):
        class FailingPlanner:
            async def plan(self, *args, **kwargs):
                raise LLMError("proxy down")

        service = make_service(db, fake, stack=make_stack(fake, planner=FailingPlanner()))

        with pytest.raises(LLMError):
            await start(service)

        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
        assert run.status == FlowStatus.BLOCKED.value  # fatal: parked, never silent
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
    async def test_go_publishes_and_parks_in_waiting_ci(self, db, fake):
        """R02: the publish leg STOPS at waiting_ci — the PR's builds are an
        independent verification gate; no review before a verdict."""
        reviewer = StubAzureReviewer()
        service = make_service(db, fake, stack=make_stack(fake, reviewer=reviewer))
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value

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
        evidence_notes = [
            body for body in comments(fake) if "candidate published" in body
        ]  # B13: not ready yet — verification pending
        assert len(evidence_notes) == 1
        assert pr["_links"]["web"]["href"] in evidence_notes[0]
        assert fake.heads[branch] in evidence_notes[0]
        assert "verification pending" in evidence_notes[0]  # B13: state-specific

        # R02: no review ran yet — the verification gate owns the next step.
        assert reviewer.calls == []

        # The publish leg's last hop is waiting_ci, nothing further.
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
        assert targets[-1] == FlowStatus.WAITING_CI.value

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
        assert run.status == FlowStatus.WAITING_CI.value
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
        assert run.status == FlowStatus.WAITING_CI.value

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
        assert {"run_id", "attempt_base", "driver", "model", "work_item_id"} <= set(params)
        # B04: a frozen envelope dispatches the binding trio — the lane
        # renders ENFORCED (no fallback) when all three are present.
        if "plan_note_id" in params:
            assert params["plan_note_id"].isdigit()
            assert params["envelope_digest"]
            assert params["spec_digest"]
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

    async def test_lane_dispatch_failure_parks_the_run_blocked(self, db, fake):
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
        assert run.status == FlowStatus.BLOCKED.value  # 400 config error: fatal, no auto-retry
        assert "harness_start_failed" in (run.status_reason or "")

        # A12 identity-first journal (LIVE-found 2026-09-20): even a failed
        # start must leave the dispatch identity in the evidence — the
        # revival scanner re-dispatches from exactly this handle. A start
        # failure used to leave NO identity and the scanner skipped the
        # run it had just parked ("revival without repo identity").
        handle = AzurePipelinesHandle.from_json(run.evidence["harness"]["handle"])
        assert handle.run_id == 0  # nothing correlated — the dispatch failed
        assert handle.forge_run_id == run_id
        assert handle.project and handle.repo

    async def test_revival_without_identity_re_parks_blocked_and_fails(self, db):
        """The scanner walks a due run blocked → proposing BEFORE it can
        know the redispatch is possible. With no journaled identity the
        redispatch used to return QUIETLY: the attempt recorded succeeded,
        the run stayed proposing with nothing scheduled — a zombie no
        command could touch (/retry refuses non-blocked runs). The
        redispatch must re-park the run blocked and raise."""
        settings = make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        service = make_service(db, FakeAzureDevOps(), settings=settings)
        run_id = await start(service)

        # Simulate the stranded mid-revival state: proposing, no identity.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.PROPOSING.value
            run.status_reason = "harness_start_failed: Azure DevOps API error 500"
            run.evidence = {"backend": "ci_harness"}
            await session.commit()

        async def no_stack(project: str, repo: str):
            raise AssertionError("stack must not be built without an identity")

        from forge.runs.azure_service import _azure_revival_redispatch

        redispatch = _azure_revival_redispatch(settings, ForgeConfig(), db, no_stack)
        with pytest.raises(RuntimeError, match="no repo identity"):
            await redispatch(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "no repo identity" in (run.status_reason or "")


# ----------------------------------------------------------------------
# R02 verification gate: waiting_ci runs are driven by their builds
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# A02: the executable spec v3 on the Azure DevOps path
# ----------------------------------------------------------------------


class TestExecutableSpecA02:
    """The Azure DevOps lane freezes and consumes the SAME executable spec
    v3 as the GitLab/GitHub paths: post-/go settings changes never alter
    execution, the budget ceilings come from the R13 profiles, an
    un-onboarded driver is never selected, and a legacy v2 spec parks
    re-approval-required."""

    async def test_settings_drift_after_freeze_never_moves_the_spec(self, db, fake):
        settings = make_settings(
            FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID,
            FORGE_REQUIRED_JOBS="pytest",
            FORGE_MAX_COMMIT_CYCLES=2,
        )
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            session.expunge(spec)
        frozen_digest = spec.digest
        frozen_document = dict(spec.document)

        settings.FORGE_REQUIRED_JOBS = ""
        settings.FORGE_MAX_COMMIT_CYCLES = 9
        settings.FORGE_AZDO_LANE_PIPELINE_ID = 999
        settings.FORGE_HARNESS_MODEL = "post-gate-model"

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document == frozen_document
        assert spec.digest == frozen_digest
        assert spec.document["verification"]["required_jobs"] == ["pytest"]
        assert spec.document["budgets"]["commit_cycles"] == 2
        assert spec.document["backend_config"]["lane_pipeline_id"] == LANE_PIPELINE_ID

    async def test_unavailable_driver_is_never_selected(self, db, fake):
        """R31 manifest: a driver the project did not onboard is dropped
        from the frozen chain — not selected by the preference."""
        settings = make_settings(
            FORGE_HARNESS_PREFERENCE="grok-build,claude-code",
            FORGE_AVAILABLE_DRIVERS='["claude-code"]',
        )
        service = make_service(db, fake, settings=settings)

        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        backend = spec.document["backend_config"]
        assert backend["harness"] == "claude-code"
        assert backend["harness_fallbacks"] == []  # grok-build is not onboarded

    async def test_budget_profile_freezes_numeric_ceilings(self, db, fake):
        """R13: the class's numeric profile resolves AT FREEZE TIME and
        rides in the spec's budgets block with its honest enforcement
        level (full — the builtin lane intercepts every call)."""
        settings = make_settings(
            FORGE_BUDGET_PROFILES='{"standard": {"max_calls": 40, "max_tokens": 500000,'
            ' "wallclock_s": 3600}}',
        )
        service = make_service(db, fake, settings=settings)

        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            run = await session.get(FlowRun, run_id)
            budgets = (
                (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document["budgets"] == {
            "commit_cycles": 3,
            "harness_timeout": make_settings().FORGE_HARNESS_TIMEOUT_SECONDS,
            "max_calls": 40,
            "max_tokens": 500000,
            "wallclock_s": 3600,
            "enforcement": "full",
        }
        # The budget row was opened BEFORE the first paid call and bound to
        # the frozen spec digest.
        assert budgets.max_calls == 40
        assert budgets.spec_digest == run.spec_digest
        assert (run.evidence or {})["budget"]["enforcement"] == "full"

    async def test_budget_exhausted_blocks_the_lane_dispatch(self, db, fake):
        """R13/A02: an exhausted budget starts no new episode — the dispatch
        is the enforcement point the lane has."""
        settings = make_settings(
            FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID,
            FORGE_BUDGET_PROFILES='{"standard": {"max_calls": 5, "wallclock_s": 3600}}',
        )
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)  # the budget row opens pre-paid
        async with db() as session:
            budget = (
                (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id)))
                .scalars()
                .one()
            )
            budget.status = "exhausted"
            await session.commit()
        fake.pipeline_calls.clear()
        clear_comments(fake)

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("budget_exhausted")
        assert fake.pipeline_calls == []  # no episode was dispatched

    async def test_legacy_v2_spec_parks_reapproval_required(self, db, fake):
        """A02 legacy policy: a run whose stored spec is v2 (created
        pre-upgrade) is never silently executed as v3 — the next dispatch
        leg parks blocked(spec_legacy: re-approval required)."""
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # the gate consumed the v3 spec digest
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value
        # Rewrite the row into exactly what the pre-upgrade lane stored: a
        # v2-shaped, digest-consistent document.
        v2_document = {
            "subject": {
                "provider": "azure_devops",
                "project": PROJECT,
                "repo_full_name": REPO_FULL,
                "project_id": PROJECT_ID,
                "issue_iid": WORK_ITEM,
            },
            "source_base_oid": BASE_HEAD,
            "plan_digest": (await get_run(db, run_id)).plan_digest,
            "task_digest": task_digest_of(WORK_ITEM_TITLE, _strip_html(WORK_ITEM_DESC_HTML)),
            "policy_digest": service._policy_digest(),
            "backend_config": {
                "backend": "builtin",
                "model": make_settings().FORGE_HARNESS_MODEL,
                "target_branch": "main",
                "harness": "claude-code",
                "harness_fallbacks": [],
                "budget_class": "standard",
                "selection_reason": "default",
            },
            "budgets": {"commit_cycles": 3, "harness_timeout": 1800},
        }
        v2_digest = hashlib.sha256(
            json.dumps(v2_document, sort_keys=True).encode("utf-8")
        ).hexdigest()
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            spec.schema_version = 2
            spec.document = v2_document
            spec.digest = v2_digest
            run = await session.get(FlowRun, run_id)
            run.spec_digest = v2_digest
            await session.commit()

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("spec_legacy")
        assert "re-approval required" in (run.status_reason or "")


class TestVerificationGate:
    async def test_green_build_drives_to_verified_ready(self, db, fake):
        """Succeeded builds of the candidate commit → review → ready, with
        the unified verification evidence and NO unverified label."""
        service = make_service(db, fake)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha)

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        # ADR-0027: the ONE shared verified ready tail — identical to the
        # GitLab lane's, no unverified label.
        assert run.status_reason == "checks passed; merge is a human decision"
        verification = (run.evidence or {})["verification"]
        # The unified R02 evidence shape (the same keys on every provider).
        assert set(verification) >= {"status", "tested_oid", "observed_at", "producer"}
        assert verification["status"] == "passed"
        assert verification["tested_oid"] == candidate_sha
        assert verification["producer"] == "azure-build"
        # A01: the surface carries the FULL check identity — name/result plus
        # the native build id and the sourceVersion the provider verified —
        # not just the display name.
        assert verification["surface"] == [
            {
                "name": "CI",
                "result": "succeeded",
                "build_id": 301,
                "source_version": candidate_sha,
            }
        ]
        # The run walked waiting_ci → evaluating_ci → reviewing → ready.
        assert (await outbox_targets(db, run_id))[-3:] == [
            FlowStatus.EVALUATING_CI.value,
            FlowStatus.REVIEWING.value,
            FlowStatus.READY_FOR_HUMAN.value,
        ]

    async def test_builds_from_any_definition_verify(self, db, fake):
        """Branch-policy Build validation, repo CI — any non-lane definition
        that built the exact candidate sha verifies it."""
        service = make_service(db, fake)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha, definition_id=77, definition_name="validate")

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert (run.evidence or {})["verification"]["status"] == "passed"

    async def test_build_for_a_different_sha_is_ignored(self, db, fake):
        """ADR-0008: a verdict approves THIS commit — another branch's green
        build must never verify the candidate."""
        service = make_service(db, fake)
        run_id, _candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version="d" * 40, result="succeeded")

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        # No build for the candidate → grace → honestly unverified.
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert (run.evidence or {})["verification"]["status"] == "not_configured"
        assert "unverified" in (run.status_reason or "")

    async def test_no_builds_after_grace_is_honestly_unverified(self, db, fake):
        settings = make_settings(FORGE_VERIFICATION_GRACE_SECONDS=300)
        service = make_service(db, fake, settings=settings)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)

        # Inside the grace window: keep waiting — builds may still queue.
        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

        # Past the grace: review as honestly unverified (R02).
        await service.evaluate_waiting_ci_one(
            run_id, now=datetime.now(timezone.utc) + timedelta(seconds=301)
        )
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert "unverified" in (run.status_reason or "")
        verification = (run.evidence or {})["verification"]
        assert set(verification) >= {"status", "tested_oid", "observed_at", "producer"}
        assert verification["status"] == "not_configured"
        assert verification["tested_oid"] == candidate_sha
        assert verification["producer"] == "azure-build"

    @pytest.mark.parametrize("result", ["failed", "partiallySucceeded"])
    async def test_red_build_results_enter_repair(self, db, fake, result):
        """ADR-0008: a result that blames the change drives a bounded repair
        cycle that re-dispatches the lane with the failure as its context.

        A02: the lane pipeline id is FROZEN in the spec — the repair
        dispatches exactly that frozen contract (cycle 1's episode was the
        lane dispatch; the red verification build enters cycle 2).

        A01: ``canceled``/``abandoned`` are NOT here — they are
        infrastructure evidence and block instead of repairing (see
        ``test_canceled_build_is_infrastructure_never_repair``)."""
        service = make_service(
            db, fake, settings=make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        )
        run_id = await start(service)
        clear_comments(fake)
        await go(service, run_id)  # episode 1: the frozen-lane dispatch
        branch = azure_factory_branch(WORK_ITEM, run_id)
        candidate_sha = fake.heads[branch]
        # The lane candidate was published — the run waits for verification.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.WAITING_CI.value
            run.candidate_shas = [candidate_sha]
            await session.commit()
        fake.seed_build(source_version=candidate_sha, result=result)
        fake.pipeline_calls.clear()

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        assert run.commit_cycle == 2
        (dispatch,) = fake.pipeline_calls
        assert dispatch["pipeline_id"] == LANE_PIPELINE_ID
        repair_context = dispatch["template_parameters"]["repair_context"]
        assert repair_context.startswith("code: builds failed")
        assert "CI" in repair_context  # the failing build's name
        assert (await outbox_targets(db, run_id))[-3:] == [
            FlowStatus.EVALUATING_CI.value,
            FlowStatus.PROPOSING.value,
            FlowStatus.WAITING_HARNESS.value,
        ]

    async def test_builtin_frozen_run_is_never_upgraded_to_the_lane(self, db, fake):
        """A02: the spec froze backend=builtin — a later lane onboarding
        must not turn the run's repair into a lane dispatch the gate never
        approved. The run parks blocked honestly instead."""
        service = make_service(db, fake)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha, result="failed")

        # The reconciler runs with the lane onboarded AFTER the freeze.
        lane_service = make_service(
            db, fake, settings=make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        )
        await lane_service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("quality_contract")
        assert fake.pipeline_calls == []  # no dispatch against the live lane

    async def test_commit_cycles_exhausted_blocks_quality_contract(self, db, fake):
        service = make_service(db, fake, settings=make_settings(FORGE_MAX_COMMIT_CYCLES=2))
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.commit_cycle = 2  # the last cycle is already spent
            await session.commit()
        fake.seed_build(source_version=candidate_sha, result="failed")

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("quality_contract")
        assert "builds failed" in (run.status_reason or "")
        assert fake.pipeline_calls == []  # no further dispatch

    async def test_pending_build_waits_then_blocks_on_verification_timeout(self, db, fake):
        settings = make_settings(FORGE_VERIFICATION_TIMEOUT_SECONDS=600)
        service = make_service(db, fake, settings=settings)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha, status="inProgress", result=None)

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

        await service.evaluate_waiting_ci_one(
            run_id, now=datetime.now(timezone.utc) + timedelta(seconds=601)
        )
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "verification_timeout: builds did not conclude"

    async def test_lane_build_is_execution_not_verification(self, db, fake):
        """The lane pipeline is excluded from the verification surface (the
        GitHub harness-workflow rule): its build never verifies the candidate.

        A02: the exclusion follows the lane id FROZEN in the run's spec — a
        reconciler whose live settings name a different (or no) lane cannot
        change what verifies the candidate."""
        service = make_service(
            db, fake, settings=make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        )
        run_id = await start(service)
        clear_comments(fake)
        await go(service, run_id)
        branch = azure_factory_branch(WORK_ITEM, run_id)
        candidate_sha = fake.heads[branch]
        # The lane candidate was published — the run waits for verification.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.WAITING_CI.value
            run.candidate_shas = [candidate_sha]
            await session.commit()
        # Only the lane's own build ran for the candidate commit.
        fake.seed_build(
            source_version=candidate_sha,
            definition_id=LANE_PIPELINE_ID,
            definition_name="forge-lane",
        )

        # A reconciler service with NO lane configured still excludes the
        # lane build — the frozen contract, not live settings, decides.
        plain_service = make_service(db, fake)
        await plain_service.evaluate_waiting_ci_one(
            run_id, now=datetime.now(timezone.utc) + timedelta(seconds=301)
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        # Excluding the lane build leaves NO verification surface — honest.
        assert (run.evidence or {})["verification"]["status"] == "not_configured"
        assert "unverified" in (run.status_reason or "")

    async def test_run_advanced_elsewhere_is_left_alone(self, db, fake):
        service = make_service(db, fake)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha, result="failed")
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.CANCELLED.value
            await session.commit()

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value  # superseded, never revived

    # -- A01: the positive-proof contract on the Azure gate -------------------

    @pytest.mark.parametrize("result", ["canceled", "abandoned"])
    async def test_canceled_build_is_infrastructure_never_repair(self, db, fake, result):
        """A01 AC3: a canceled/abandoned validation build is evidence the
        EXECUTION died — the run parks as infrastructure and the repair
        budget is never spent on it (no lane re-dispatch)."""
        service = make_service(db, fake)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha, result=result)
        reviewer = StubAzureReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("verification_infrastructure")
        assert "CI" in (run.status_reason or "")  # the offending build is named
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "unknown"
        assert fake.pipeline_calls == []  # no repair dispatch
        assert reviewer.calls == []  # no review, no ready

    async def test_required_build_absent_with_a_green_docs_pipeline_never_verifies(self, db, fake):
        """A01 AC1 + A02: the required list comes from the FROZEN spec — a
        green optional `docs` pipeline proves nothing while `tests` never
        ran, even when live settings drift to empty after the freeze."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document["verification"]["required_jobs"] == ["tests"]
        settings.FORGE_REQUIRED_JOBS = ""  # the post-gate drift A01 is immune to
        fake.seed_build(source_version=candidate_sha, definition_name="docs", result="succeeded")
        reviewer = StubAzureReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # unknown — keep waiting
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "unknown"
        assert verification["tested_oid"] == candidate_sha
        assert "tests" in verification["summary"]
        assert verification["surface"] == [
            {
                "name": "docs",
                "result": "succeeded",
                "build_id": verification["surface"][0]["build_id"],
                "source_version": candidate_sha,
            }
        ]
        # the A01 identity keys really ride the entry
        assert set(verification["surface"][0]) == {
            "name",
            "result",
            "build_id",
            "source_version",
        }
        assert reviewer.calls == []  # the gate never let it reach review

    async def test_unproven_required_still_blocks_on_the_verification_deadline(self, db, fake):
        """Unknown is a WAITING verdict — the R17 deadline is the bound."""
        settings = make_settings(
            FORGE_REQUIRED_JOBS="tests", FORGE_VERIFICATION_TIMEOUT_SECONDS=600
        )
        service = make_service(db, fake, settings=settings)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha, definition_name="docs", result="succeeded")
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            # B01: the deadline reads the verification EPOCH, not updated_at
            # (evidence merges slide updated_at — the bug this pins).
            started = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
            run.evidence = dict(run.evidence or {}) | {
                "verification_epoch": {"candidate_sha": candidate_sha, "started_at": started}
            }
            await session.commit()

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("verification_timeout")

    async def test_human_push_during_review_supersedes_the_candidate(self, db, fake):
        """A01 AC4: a push that lands while the LLM review is in flight
        invalidates the candidate-specific result — superseded evidence,
        never READY (the F19 parity of the GitLab leg)."""
        service = make_service(db, fake)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha)
        branch = azure_factory_branch(WORK_ITEM, run_id)
        moved_head = "9" * 40

        class BranchMovingReviewer(StubAzureReviewer):
            async def review(self, **kwargs):
                # the human push lands while the review is in flight
                fake.seed_commit(branch, moved_head, "human push during review")
                return await super().review(**kwargs)

        service._stack = make_stack(fake, reviewer=BranchMovingReviewer())

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("candidate_drift_after_review")
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "unknown"
        assert verification["tested_oid"] == moved_head
        assert "superseded" in verification["summary"]
        assert len(service._stack.reviewer.calls) == 1  # the review DID run

    async def test_verified_fragment_binds_the_provider_source_version(self, db, fake):
        """A01: tested_oid is the sha the PROVIDER verified (the matched
        build's sourceVersion), never a self-asserted candidate."""
        service = make_service(db, fake)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha)

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "passed"
        assert verification["tested_oid"] == candidate_sha
        assert verification["surface"][0]["source_version"] == candidate_sha


class TestWorkerVerificationPass:
    async def test_pass_drives_every_waiting_ci_run(self, db, fake):
        """The module-level pass (the reconciler tick) drives each parked run
        through its own repo's service — the evaluate_github_waiting_ci twin."""
        service = make_service(db, fake)
        run_id, candidate_sha = await drive_to_waiting_ci(service, fake)
        fake.seed_build(source_version=candidate_sha)

        await evaluate_azure_waiting_ci(
            make_settings(),
            ForgeConfig(),
            db,
            stack_factory=lambda p, r: make_stack(fake),
            now=datetime.now(timezone.utc),
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert (run.evidence or {})["verification"]["status"] == "passed"

    async def test_pass_is_a_noop_without_waiting_ci_runs(self, db, fake):
        await evaluate_azure_waiting_ci(
            make_settings(),
            ForgeConfig(),
            db,
            stack_factory=lambda p, r: make_stack(fake),
        )

        assert fake.calls == []


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


class CountingStackPlanner:
    """Plan-call recorder for the A13 gate: counts, records, answers stub."""

    def __init__(self) -> None:
        self.calls = 0
        self.path_scopes: list[list[str] | None] = []

    async def plan(self, issue_title, issue_description, *, flow_run_id=None, path_scope=None):
        self.calls += 1
        self.path_scopes.append(path_scope)
        return "## Implementation plan\n\n- create the thing\n"


class TestConfigGateA13:
    """A13 on the AzDO lane: an unreadable/invalid `.forge.yml` parks the
    run — scope never widens, nothing is paid while the read is retried."""

    RESTRICTED = "implement:\n  paths:\n    - 'services/**'\n"

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        clear_cache()
        yield
        clear_cache()

    def seed_config(self, fake: FakeAzureDevOps, content: str) -> None:
        # The start path reads `.forge.yml` at the target branch; the fake's
        # get_item keys snapshots by that raw version string ("main").
        fake.seed_snapshot("main", {".forge.yml": content})

    def arm_403(self, fake: FakeAzureDevOps):
        """403 every `.forge.yml` read until disarmed — classified by the
        real reader's typed ``read_blob`` like a real DevOps 403. Returns
        the original bound method for disarming."""
        original = fake.get_item

        async def flaky(project, repo, path, *, version=None, version_type=None):
            if path.lstrip("/") == ".forge.yml":
                raise AzureDevOpsError(403, "read forbidden")
            return await original(project, repo, path, version=version, version_type=version_type)

        fake.get_item = flaky
        return original

    async def test_403_parks_config_unreadable_with_zero_paid_calls(self, db, fake):
        self.seed_config(fake, self.RESTRICTED)
        self.arm_403(fake)
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_unreadable:")
        assert planner.calls == 0
        assert fake.calls_of("push_commits") == []
        assert fake.calls_of("create_branch_from") == []
        assert run.evidence["config_block"]["issue_title"] == WORK_ITEM_TITLE
        assert any("config_unreadable" in body for body in comments(fake))

    async def test_malformed_yaml_parks_config_invalid(self, db, fake):
        self.seed_config(fake, "not: a: valid: [[[")
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_invalid:")
        assert planner.calls == 0

    async def test_valid_config_freezes_scope_and_provenance(self, db, fake):
        self.seed_config(fake, self.RESTRICTED)
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))

        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document["allowed_paths"] == ["services/**"]
        provenance = spec.document["project_config"]
        assert provenance["status"] == "valid"
        assert provenance["sha256"] == hashlib.sha256(self.RESTRICTED.encode()).hexdigest()
        assert planner.path_scopes == [["services/**"]]

    async def test_recovery_re_enters_planning_after_the_read_recovers(self, db, fake):
        self.seed_config(fake, self.RESTRICTED)
        original = self.arm_403(fake)
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))
        run_id = await start(service)
        assert (await get_run(db, run_id)).status == FlowStatus.BLOCKED.value
        assert planner.calls == 0

        fake.get_item = original  # the config is readable again
        await service.evaluate_config_recovery()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert planner.calls == 1
        assert planner.path_scopes == [["services/**"]]

    async def test_reconciler_scan_finds_the_run_via_the_stashed_repo(self, db, fake):
        """The module-level pass (what the reconciler loop drives) rebuilds
        the repo's service from the stashed AzDO subject identity — the
        durable columns carry no repo string on this lane."""
        from forge.runs.azure_service import evaluate_azure_config_recovery

        self.seed_config(fake, self.RESTRICTED)
        original = self.arm_403(fake)
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))
        run_id = await start(service)
        assert (await get_run(db, run_id)).status == FlowStatus.BLOCKED.value
        assert planner.calls == 0

        fake.get_item = original  # readable again
        await evaluate_azure_config_recovery(
            make_settings(),
            ForgeConfig(),
            db,
            stack_factory=lambda project, repo: make_stack(fake, planner=planner),
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert planner.calls == 1


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


# ----------------------------------------------------------------------
# #29 lifecycle commands on the AzDO surface: work-item-edit replan +
# trigger-tag-off cancel — the mirror of the GitHub handlers
# ----------------------------------------------------------------------


async def edit_item(
    service: AzureRunService,
    *,
    body: str,
    title: str = WORK_ITEM_TITLE,
    author: str = "dev@fabrikam.example",
) -> str | None:
    return await service.handle_issue_edited(
        project_id=PROJECT_ID,
        issue_number=WORK_ITEM,
        issue_title=title,
        issue_body=body,
        author_username=author,
    )


class TestIssueEdited:
    async def test_edit_while_waiting_approval_replans(self, db, fake):
        service = make_service(db, fake)
        stale_id = await start(service)
        clear_comments(fake)
        new_body = "<p>Users <b>cannot</b> reset. The reset mail bounces with SMTP 550.</p>"

        new_id = await edit_item(service, body=new_body)

        assert new_id is not None and new_id != stale_id
        stale = await get_run(db, stale_id)
        fresh = await get_run(db, new_id)
        # The stale run is cancelled DURABLY: grant revoked, not just parked.
        assert stale.status == FlowStatus.CANCELLED.value
        assert stale.cancel_requested is True
        assert fresh.status == FlowStatus.WAITING_APPROVAL.value

        # The fresh run's frozen snapshot digests the STRIPPED new text.
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == new_id)))
                .scalars()
                .one()
            )
        assert spec.document["task_digest"] == task_digest_of(
            WORK_ITEM_TITLE, "Users cannot reset. The reset mail bounces with SMTP 550."
        )

        # The plan comment went out again, plus the regeneration note.
        bodies = comments(fake)
        assert len([b for b in bodies if "Forge plan" in b]) == 1
        (note,) = [b for b in bodies if "stale" in b]
        assert stale_id[:8] in note and new_id[:8] in note

        # The stale run's gate was never consumed by the replan.
        async with db() as session:
            gates = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == stale_id)
                    )
                )
                .scalars()
                .all()
            )
        assert gates and all(gate.consumed_at is None for gate in gates)

    async def test_redelivered_edit_is_a_no_op(self, db, fake):
        """A redelivered edit (fresh run's snapshot already IS that text)
        must not spawn a third run or re-post anything."""
        service = make_service(db, fake)
        await start(service)
        new_body = "<p>The work item, edited once.</p>"
        first = await edit_item(service, body=new_body)
        comments_after_first = len(comments(fake))

        second = await edit_item(service, body=new_body)

        assert second == first
        assert len(comments(fake)) == comments_after_first
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 2  # the stale run + the replan, nothing more

    async def test_sparse_delivery_repairs_text_with_one_api_read(self, db, fake):
        """A 'changed fields only' subscription may omit untouched fields —
        the handler restores the authoritative text with ONE get_work_item
        read instead of digesting empty strings as if they were blanked.
        The unchanged text then matches the snapshot: no replan."""
        service = make_service(db, fake)
        await start(service)
        clear_comments(fake)

        result = await edit_item(service, title="", body="")

        assert result is not None  # the waiting run still owns the work item
        assert fake.calls_of("get_work_item")  # the ONE API read happened
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1  # no replan: the text matches the snapshot
        assert comments(fake) == []

    async def test_gate_consumed_but_still_waiting_posts_note_only(self, db, fake):
        """The "gate already consumed" guard: an edit in the consume→commit
        window must never cancel an approved run."""
        service = make_service(db, fake)
        run_id = await start(service)
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
            gate.consumed_at = datetime.now(timezone.utc)
            await session.commit()
        clear_comments(fake)

        result = await edit_item(service, body="<p>an edited body</p>")

        run = await get_run(db, run_id)
        assert result == run_id
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.cancel_requested is False
        (note,) = comments(fake)
        assert "not** in the approved plan" in note

    async def test_mid_flight_edit_notes_once_and_does_not_yank(self, db, fake):
        service = make_service(db, fake)
        run_id, _candidate = await drive_to_waiting_ci(service, fake)
        notes_before = len(comments(fake))

        result = await edit_item(service, body="<p>edited while the run executes</p>")

        run = await get_run(db, run_id)
        assert result == run_id
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.cancel_requested is False
        # ONE informational note; the ready-for-review comment was already out.
        bodies = comments(fake)
        assert len(bodies) == notes_before + 1
        assert "in flight" in bodies[-1]

    async def test_non_admitted_edit_is_ignored(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        result = await edit_item(service, body="<p>vandalism</p>", author="mallory@x.example")

        assert result is None
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert comments(fake) == []

    async def test_edited_command_dispatch_replans(self, db, fake):
        """The gateway-normalized command drives the service end to end."""
        service = make_service(db, fake)
        stale_id = await start(service)
        metadata = {
            "command": "issue_edited",
            "provider": "azure_devops",
            "project": PROJECT,
            "repo_full_name": REPO_FULL,
            "project_id": PROJECT_ID,
            "issue_number": WORK_ITEM,
            "issue_title": WORK_ITEM_TITLE,
            "issue_body": "<p>dispatched edit body</p>",
            "author_username": "dev@fabrikam.example",
            "note_text": "",
            "note_id": "edit:142:abc:12",
        }

        await execute_azure_run_command(
            make_settings(),
            ForgeConfig(),
            db,
            metadata,
            stack_factory=lambda project, repo: make_stack(fake),
        )

        stale = await get_run(db, stale_id)
        assert stale.status == FlowStatus.CANCELLED.value
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 2
        assert any(run.status == FlowStatus.WAITING_APPROVAL.value for run in runs)


class TestLabelOff:
    async def test_tag_removal_cancels_the_gate_waiting_run(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_number=WORK_ITEM, author_username="dev@fabrikam.example"
        )

        assert cancelled == 1
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True
        (note,) = comments(fake)
        assert "cancelled" in note and "tag" in note

    async def test_tag_removal_leaves_a_past_gate_run_alone(self, db, fake):
        service = make_service(db, fake)
        run_id, _candidate = await drive_to_waiting_ci(service, fake)

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_number=WORK_ITEM, author_username="dev@fabrikam.example"
        )

        assert cancelled == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value

    async def test_non_approver_tag_removal_is_ignored(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_number=WORK_ITEM, author_username="mallory@x.example"
        )

        assert cancelled == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert comments(fake) == []

    async def test_unlabeled_command_dispatch_cancels(self, db, fake):
        """The gateway-normalized command drives the service end to end."""
        service = make_service(db, fake)
        run_id = await start(service)
        metadata = {
            "command": "unlabeled",
            "provider": "azure_devops",
            "project": PROJECT,
            "repo_full_name": REPO_FULL,
            "project_id": PROJECT_ID,
            "issue_number": WORK_ITEM,
            "author_username": "dev@fabrikam.example",
            "note_text": "",
            "note_id": "unlabel:142:13",
        }

        await execute_azure_run_command(
            make_settings(),
            ForgeConfig(),
            db,
            metadata,
            stack_factory=lambda project, repo: make_stack(fake),
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True


# ----------------------------------------------------------------------
# R17 on the AzDO surface: deadlines/cancel checked BEFORE provider I/O,
# and discovery bounded
# ----------------------------------------------------------------------


class TestAzureVerificationDeadlineBeforeIO:
    async def test_verification_timeout_fires_without_builds_call(self, db, fake):
        """The Builds API has been dead the whole wait: the deadline is
        evaluated locally and blocks without asking the provider again."""
        settings = make_settings(FORGE_VERIFICATION_TIMEOUT_SECONDS=600)
        service = make_service(db, fake, settings=settings)
        run_id, _candidate = await drive_to_waiting_ci(service, fake)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            # B01: the deadline reads the verification EPOCH, not updated_at
            # (evidence merges slide updated_at — the bug this pins).
            started = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
            run.evidence = dict(run.evidence or {}) | {
                "verification_epoch": {"candidate_sha": _candidate, "started_at": started}
            }
            await session.commit()

        builds_reads = len(fake.calls_of("list_builds_by_repository"))

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "verification_timeout: builds did not conclude"
        assert len(fake.calls_of("list_builds_by_repository")) == builds_reads

    async def test_cancel_requested_ignores_the_late_pass(self, db, fake):
        """F13: the grant was revoked mid-wait — no provider call, no publish."""
        service = make_service(db, fake)
        run_id, _candidate = await drive_to_waiting_ci(service, fake)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.cancel_requested = True
            await session.commit()
        builds_reads = len(fake.calls_of("list_builds_by_repository"))

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # terminal stays /cancel's
        assert len(fake.calls_of("list_builds_by_repository")) == builds_reads


class TestAzureHarnessDeadlineBeforeIO:
    async def _waiting_harness_run(self, db, fake):
        settings = make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        clear_comments(fake)
        await go(service, run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        return service, run_id

    async def _age_journaled_handle(self, db, run_id: str, *, seconds: int) -> None:
        """Rewind the journaled handle's started_at — the deadline rides on it."""
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            harness = dict(run.evidence["harness"])
            handle = AzurePipelinesHandle.from_json(harness["handle"])
            aged = replace(
                handle,
                started_at=(datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(),
            )
            harness["handle"] = aged.to_json()
            run.evidence = {**run.evidence, "harness": harness}
            await session.commit()

    async def test_expired_deadline_blocks_without_any_provider_call(self, db, fake):
        """The journaled deadline passed: the run parks blocked on
        harness_timeout even though the Pipelines API errors on every call —
        and the failing API is never asked (discovery/poll not reached)."""
        service, run_id = await self._waiting_harness_run(db, fake)
        # Age relative to the EFFECTIVE budget (the environment may override
        # the config default) so the deadline is provably behind.
        await self._age_journaled_handle(
            db,
            run_id,
            seconds=make_settings().FORGE_HARNESS_TIMEOUT_SECONDS + 1,
        )

        async def provider_forbidden(*args, **kwargs):
            raise AssertionError("provider called after the harness deadline expired")

        fake.get_run = provider_forbidden  # type: ignore[method-assign]
        builds_reads = len(fake.calls_of("list_builds_by_repository"))

        await service.evaluate_waiting_harness_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "harness_timeout" in (run.status_reason or "")
        assert len(fake.calls_of("list_builds_by_repository")) == builds_reads

    async def test_cancel_requested_stands_down_pre_poll(self, db, fake):
        """F13: a revoked grant stops the harness evaluation before the
        provider is touched — the late candidate would be superseded."""
        service, run_id = await self._waiting_harness_run(db, fake)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.cancel_requested = True
            await session.commit()
        builds_reads = len(fake.calls_of("list_builds_by_repository"))

        await service.evaluate_waiting_harness_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value  # NOT advanced, NOT terminalized
        assert run.evidence["superseded"] == {
            "reason": "cancelled",
            "attempt_base": BASE_HEAD,
        }
        assert len(fake.calls_of("list_builds_by_repository")) == builds_reads


class TestAzureBoundedDiscovery:
    async def _uncorrelated_waiting_harness_run(self, db, fake):
        """A lane run whose journaled handle never correlated (the legacy
        empty-202 shape): run_id 0 — discovery owns the correlation."""
        settings = make_settings(
            FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID,
            FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS=3,
        )
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        clear_comments(fake)
        await go(service, run_id)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            harness = dict(run.evidence["harness"])
            handle = AzurePipelinesHandle.from_json(harness["handle"])
            harness["handle"] = replace(handle, run_id=0).to_json()
            harness["run_id"] = None
            run.evidence = {**run.evidence, "harness": harness}
            await session.commit()
        discovery_calls = len(fake.calls_of("list_builds_by_repository"))
        return service, run_id, discovery_calls

    async def test_unknown_dispatch_blocks_after_the_attempt_cap(self, db, fake):
        """Discovery retries exactly up to FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS,
        then the run parks blocked with the precise 'dispatch never observed'
        reason instead of polling until heat death."""
        service, run_id, discovery_calls = await self._uncorrelated_waiting_harness_run(db, fake)

        for _ in range(3):
            await service.evaluate_waiting_harness_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "dispatch never observed" in (run.status_reason or "")
        assert "harness_infrastructure" in (run.status_reason or "")
        assert len(fake.calls_of("list_builds_by_repository")) == discovery_calls + 3

        # The blocked run is out of the reconciler's set — no further calls.
        await evaluate_azure_waiting_harness(
            make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID),
            ForgeConfig(),
            db,
            stack_factory=lambda project, repo: make_stack(fake),
        )
        assert len(fake.calls_of("list_builds_by_repository")) == discovery_calls + 3

    async def test_attempts_reset_when_the_run_surfaces(self, db, fake):
        """Discovery is bounded but not eager: within the cap a run that
        surfaces later is adopted, the counter resets, and the lane completes."""
        service, run_id, discovery_calls = await self._uncorrelated_waiting_harness_run(db, fake)

        await service.evaluate_waiting_harness_one(run_id, now=datetime.now(timezone.utc))
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value
        assert (await get_run(db, run_id)).evidence["harness"]["discovery_attempts"] == 1

        # The pipeline run shows up in the Builds API afterwards — on the
        # dispatched branch, carrying forge's run id (the discovery match).
        branch = azure_factory_branch(WORK_ITEM, run_id)
        build = fake.seed_build(source_version=fake.heads[branch], definition_id=LANE_PIPELINE_ID)
        build["sourceBranch"] = f"refs/heads/{branch}"
        build["templateParameters"] = {"run_id": run_id}

        await service.evaluate_waiting_harness_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.evidence["harness"]["run_id"] == build["id"]
        assert run.evidence["harness"]["discovery_attempts"] == 0
