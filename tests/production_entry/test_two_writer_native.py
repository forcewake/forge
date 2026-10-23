"""Native publication safety on the surviving single-run owners.

The retained filename records the former multiwriter qualification. ADR-0035
retires that coordinator, its partial-package pivot and synthetic content-
fence protocol. This file preserves current provider contracts: real HTTP,
intent recovery, cancellation fencing, native head drift and Draft-only
review creation. Hermetic HTTP traces run without credentials; the live
GitLab/PostgreSQL qualification keeps its original explicit environment gate.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from typing import NamedTuple

import httpx
import pytest

from forge.durable import Controller, FlowRun
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.repository import ChangesetWriter, WriteOutcome
from forge.repository.writer import BranchDriftError

from .conftest import (
    GL_ISSUE_DESC,
    GL_ISSUE_IID,
    GL_ISSUE_TITLE,
    gl_settings,
    make_gitlab_service,
)
from .test_two_writer_durable import (
    ProcessDied,
    _changeset,
    _commit_posts,
    _intent,
    _new_run,
    _publish,
)

pytestmark = pytest.mark.production_entry
_LIVE_URL = os.environ.get("FORGE_GITLAB_LIVE_URL", "")
_LIVE_TOKEN = os.environ.get("FORGE_GITLAB_LIVE_TOKEN", "")
_LIVE_REASON = (
    "FORGE_PG_TEST_URL and FORGE_GITLAB_LIVE_URL/FORGE_GITLAB_LIVE_TOKEN not both"
    " set — native qualification requires disposable Postgres and GitLab projects"
)
PRODUCER = "repo-producer"
CONSUMER = "repo-consumer"


async def _cancel(factory, run_id):
    async with factory() as session:
        await Controller(session).request_cancel(run_id)
        await session.commit()


class TestNativeHttpPublication:
    async def test_lost_http_response_adopts_the_one_native_commit(
        self, pe_db, gitlab_native, gitlab_client
    ):
        factory = pe_db.worker_factory()
        run = await _new_run(factory)
        branch = f"factory/{run.issue_iid}/{run.id[:8]}"
        gitlab_native.set_latency(
            [
                {
                    "method": "POST",
                    "tail": ["repository", "commits"],
                    "phase": "drop",
                    "ms": 1,
                }
            ]
        )
        result = await _publish(factory, gitlab_client, run, branch)
        assert result.ok and result.commit_sha == gitlab_native.branch_head(branch)
        intent = await _intent(pe_db.worker_factory(), run.id)
        assert intent.status == "adopted"
        assert intent.provider_object_id == result.commit_sha
        assert len(_commit_posts(gitlab_native)) == 1
        commits = await gitlab_client.list_commits(run.project_id, branch)
        assert commits[0]["parent_ids"] == [run.base_sha]
        assert intent.operation_key in commits[0]["message"]

    @pytest.mark.parametrize("probe_error", ["http_503", "protocol_error"])
    async def test_dropped_response_with_unavailable_probe_stays_unknown(
        self,
        pe_db,
        gitlab_native,
        gitlab_client,
        monkeypatch,
        probe_error,
    ):
        factory = pe_db.worker_factory()
        run = await _new_run(factory)
        branch = f"factory/{run.issue_iid}/{run.id[:8]}"
        gitlab_native.set_latency(
            [
                {
                    "method": "POST",
                    "tail": ["repository", "commits"],
                    "phase": "drop",
                    "ms": 1,
                }
            ]
        )

        async def unavailable(*args, **kwargs):
            if probe_error == "protocol_error":
                raise httpx.RemoteProtocolError("probe disconnected")
            raise GitLabAPIError(503, "probe unavailable")

        monkeypatch.setattr(gitlab_client, "list_commits", unavailable)
        result = await _publish(factory, gitlab_client, run, branch)
        assert not result.ok and result.unknown_outcome
        assert result.reason == "commit_unknown_outcome"
        intent = await _intent(pe_db.worker_factory(), run.id)
        assert intent.status == "unknown" and intent.provider_object_id is None
        assert gitlab_native.branch_head(branch) != run.base_sha  # the write DID land
        assert len(_commit_posts(gitlab_native)) == 1
        assert gitlab_native.merge_requests() == {}

    async def test_restarted_review_creators_adopt_one_draft_without_merging(
        self, pe_db, gitlab_native, gitlab_client
    ):
        factory = pe_db.worker_factory()
        run = await _new_run(factory)
        branch = f"factory/{run.issue_iid}/{run.id[:8]}"
        gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
        result = await _publish(factory, gitlab_client, run, branch)
        assert result.ok
        settings = gl_settings(GITLAB_URL=gitlab_native.base_url, DATABASE_URL=pe_db.url)
        first = make_gitlab_service(factory, gitlab_client, settings=settings)
        iid = await first._create_draft_mr(run.project_id, run.id, branch, result.commit_sha)
        restarted = make_gitlab_service(
            pe_db.worker_factory(),
            gitlab_client,
            settings=settings,
        )
        assert (
            await restarted._create_draft_mr(
                run.project_id,
                run.id,
                branch,
                result.commit_sha,
            )
            == iid
        )
        (mr,) = gitlab_native.merge_requests().values()
        assert mr["title"].startswith("Draft:")
        assert mr["state"] == "opened"
        assert mr.get("merged_at") is None
        assert (
            sum(
                r["method"] == "POST" and r["path"] == "merge_requests"
                for r in gitlab_native.state()["request_log"]
            )
            == 1
        )
        assert not any(
            "merge" in r["path"].split("/") and r["method"] == "PUT"
            for r in gitlab_native.state()["request_log"]
        )

    async def test_cancel_during_base_read_prevents_a_new_reservation(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        factory = pe_db.worker_factory()
        run = await _new_run(factory)
        branch = f"factory/{run.issue_iid}/{run.id[:8]}"
        read = asyncio.Event()
        release = asyncio.Event()
        original = gitlab_client.read_blob

        async def blocked_read(*args, **kwargs):
            result = await original(*args, **kwargs)  # actual authoritative HTTP read
            read.set()
            await release.wait()
            return result

        monkeypatch.setattr(gitlab_client, "read_blob", blocked_read)
        publication = asyncio.create_task(_publish(factory, gitlab_client, run, branch))
        try:
            await asyncio.wait_for(read.wait(), timeout=5)
            await _cancel(pe_db.worker_factory(), run.id)
        finally:
            release.set()
        result = await publication
        assert not result.ok and result.reason.startswith("publication_revoked:")
        assert _commit_posts(gitlab_native) == []
        assert branch not in gitlab_native.state()["branches"]

    async def test_cancel_after_native_acceptance_preserves_superseded_effect(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        factory = pe_db.worker_factory()
        run = await _new_run(factory)
        branch = f"factory/{run.issue_iid}/{run.id[:8]}"
        original = gitlab_client.create_commit

        async def cancel_after_acceptance(*args, **kwargs):
            committed = await original(*args, **kwargs)
            await _cancel(pe_db.worker_factory(), run.id)
            return committed

        monkeypatch.setattr(gitlab_client, "create_commit", cancel_after_acceptance)
        result = await _publish(factory, gitlab_client, run, branch)
        assert result.ok and result.superseded
        assert gitlab_native.branch_head(branch) == result.commit_sha
        assert len(_commit_posts(gitlab_native)) == 1
        async with pe_db.worker_factory()() as session:
            fresh = await session.get(FlowRun, run.id)
        assert fresh.cancel_requested and fresh.cancellation_generation == 1
        assert fresh.evidence["superseded"] == {
            "reason": "cancelled_during_publication",
            "commit_sha": result.commit_sha,
            "attempt_base": run.base_sha,
        }
        assert gitlab_native.merge_requests() == {}
        assert not any(r["method"] == "DELETE" for r in gitlab_native.state()["request_log"])


class LiveGitLabLab:
    """The live lab handle: the REAL client plus two disposable projects."""

    def __init__(self, client: GitLabClient, producer_project: int, consumer_project: int):
        self.client = client
        self.producer_project = producer_project
        self.consumer_project = consumer_project

    def projects(self) -> dict[str, int]:
        return {PRODUCER: self.producer_project, CONSUMER: self.consumer_project}

    async def new_publication_branch(self, project: int, label: str) -> tuple[str, str]:
        """A FRESH publication branch off ``main`` — ``(branch, base head)``.

        A dedicated branch per arm keeps the live matrix isolated without
        touching ``main`` of anything: the publication commits and MRs ride
        branches the fixture owns."""
        branch = f"forge/tw-native/{label}/{uuid.uuid4().hex[:8]}"
        await self.client.create_branch(project, branch, ref="main")
        base = await self.client.get_branch_head(project, branch)
        return branch, base


class LiveProjects(NamedTuple):
    """The module's two disposable projects (created sync, deleted sync)."""

    url: str
    token: str
    producer: int
    consumer: int


@pytest.fixture(scope="module")
def live_projects():
    """Two DISPOSABLE projects under the token's own namespace (never an
    existing lab project such as 68); every review/commit this module
    creates lives inside them and the projects are DELETED at teardown.

    Project create/delete rides a SYNC admin client in the module-scoped
    fixture on purpose: a module-scoped ASYNC teardown would run on a
    closed function-scoped event loop. The async GitLabClient lives in
    the per-test ``live_lab`` fixture, which tears down on its own loop."""
    if not (_LIVE_URL and _LIVE_TOKEN):
        pytest.skip(_LIVE_REASON)
    suffix = uuid.uuid4().hex[:8]
    admin = httpx.Client(
        base_url=f"{_LIVE_URL.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": _LIVE_TOKEN},
        timeout=60.0,
    )
    created: list[int] = []
    try:
        namespace_id = int(admin.get("/user").json()["namespace_id"])
        for role in ("producer", "consumer"):
            response = admin.post(
                "/projects",
                json={
                    "name": f"forge-tw-{suffix}-{role}",
                    "namespace_id": namespace_id,
                    "initialize_with_readme": True,
                    "description": (
                        "R37-14 disposable two-writer-native failpoint matrix project"
                        " — deleted by the fixture that created it"
                    ),
                },
            )
            response.raise_for_status()
            created.append(int(response.json()["id"]))
        yield LiveProjects(_LIVE_URL, _LIVE_TOKEN, created[0], created[1])
    finally:
        for project_id in created:
            admin.delete(f"/projects/{project_id}")  # 202 Accepted
        admin.close()


@pytest.fixture()
async def live_lab(live_projects: LiveProjects):
    """The per-test handle: the REAL GitLabClient over the disposable
    projects (a fresh client per test — closed on the test's own loop)."""
    client = GitLabClient(base_url=live_projects.url, token=live_projects.token, timeout=60.0)
    try:
        yield LiveGitLabLab(client, live_projects.producer, live_projects.consumer)
    finally:
        await client.close()


@pytest.mark.skipif(
    not (os.environ.get("FORGE_PG_TEST_URL") and _LIVE_URL and _LIVE_TOKEN),
    reason=_LIVE_REASON,
)
class TestLiveNativePublication:
    @pytest.mark.parametrize("boundary", ["before_dispatch", "after_provider", "after_journal"])
    async def test_death_recovery_preserves_native_history(
        self,
        pe_db,
        live_lab,
        monkeypatch,
        boundary,
    ):
        project = live_lab.producer_project
        branch, base = await live_lab.new_publication_branch(project, boundary)
        factory = pe_db.worker_factory()
        run = await _new_run(factory, project_id=project, base_sha=base)
        writer = ChangesetWriter(live_lab.client, factory, project)
        original = writer._succeed

        async def die_before_dispatch(*args, **kwargs):
            assert (await _intent(pe_db.worker_factory(), run.id)).status == "dispatched"
            raise ProcessDied

        async def die_after_provider(*args, **kwargs):
            if boundary == "after_journal":
                await original(*args, **kwargs)
            raise ProcessDied

        monkeypatch.setattr(
            writer,
            "_dispatch" if boundary == "before_dispatch" else "_succeed",
            die_before_dispatch if boundary == "before_dispatch" else die_after_provider,
        )
        with pytest.raises(ProcessDied):
            await _publish(factory, live_lab.client, run, branch, writer=writer)
        history = await live_lab.client.list_commits(project, branch)
        restarted = ChangesetWriter(live_lab.client, pe_db.worker_factory(), project)
        if boundary == "before_dispatch":
            result = await restarted.apply(
                run.id,
                _changeset(run, branch),
                start_ref=base,
                expected_head=base,
            )
            assert result.outcome is WriteOutcome.SETTLING
            assert await live_lab.client.get_branch_head(project, branch) == base
        elif boundary == "after_provider":
            result = await restarted.apply(
                run.id,
                _changeset(run, branch),
                start_ref=base,
                expected_head=base,
            )
            assert result.outcome is WriteOutcome.COMMITTED
            assert result.commit_sha == history[0]["sha"]
            assert (await _intent(pe_db.worker_factory(), run.id)).status == "adopted"
            assert (
                await live_lab.client.read_blob(
                    project,
                    "forge-demo/candidate.md",
                    ref=branch,
                )
            ).content == "candidate\n"
        else:
            with pytest.raises(BranchDriftError):
                await restarted.apply(
                    run.id,
                    _changeset(run, branch),
                    start_ref=base,
                    expected_head=base,
                )
            assert (await _intent(pe_db.worker_factory(), run.id)).status == "committed"
        assert await live_lab.client.list_commits(project, branch) == history

    async def test_human_edit_after_committed_intent_is_never_overwritten(
        self,
        pe_db,
        live_lab,
        monkeypatch,
    ):
        project = live_lab.consumer_project
        branch, base = await live_lab.new_publication_branch(project, "human")
        factory = pe_db.worker_factory()
        run = await _new_run(factory, project_id=project, base_sha=base)
        writer = ChangesetWriter(live_lab.client, factory, project)

        async def die(*args, **kwargs):
            raise ProcessDied

        monkeypatch.setattr(writer, "_dispatch", die)
        with pytest.raises(ProcessDied):
            await _publish(factory, live_lab.client, run, branch, writer=writer)
        human = await live_lab.client.create_commit(
            project,
            branch,
            [
                {
                    "action": "create",
                    "file_path": "human.md",
                    "content": "human's content\n",
                }
            ],
            "human edit",
        )
        restarted = ChangesetWriter(live_lab.client, pe_db.worker_factory(), project)
        with pytest.raises(BranchDriftError):
            await restarted.apply(
                run.id,
                _changeset(run, branch),
                start_ref=base,
                expected_head=base,
            )
        assert await live_lab.client.get_branch_head(project, branch) == human["id"]
        assert (await live_lab.client.read_blob(project, "human.md", ref=branch)).content == (
            "human's content\n"
        )
        assert (await _intent(pe_db.worker_factory(), run.id)).status == "duplicated"

    async def test_revoked_scoped_credential_does_not_fall_back_to_admin(
        self,
        pe_db,
        live_lab,
        live_projects,
        monkeypatch,
    ):
        project = live_lab.consumer_project
        branch, base = await live_lab.new_publication_branch(project, "revoked")
        factory = pe_db.worker_factory()
        run = await _new_run(factory, project_id=project, base_sha=base)
        writer = ChangesetWriter(live_lab.client, factory, project)

        async def die(*args, **kwargs):
            raise ProcessDied

        monkeypatch.setattr(writer, "_dispatch", die)
        with pytest.raises(ProcessDied):
            await _publish(factory, live_lab.client, run, branch, writer=writer)
        with httpx.Client(
            base_url=f"{live_projects.url.rstrip('/')}/api/v4",
            headers={"PRIVATE-TOKEN": live_projects.token},
            timeout=60.0,
        ) as admin:
            response = admin.post(
                f"/projects/{project}/access_tokens",
                json={
                    "name": f"forge-publish-scoped-{uuid.uuid4().hex[:6]}",
                    "scopes": ["read_api"],
                    "expires_at": (datetime.date.today() + datetime.timedelta(days=2)).isoformat(),
                },
            )
            response.raise_for_status()
            token = response.json()
            scoped = GitLabClient(base_url=live_projects.url, token=token["token"], timeout=60.0)
            try:
                assert await scoped.get_branch_head(project, branch) == base
                response = admin.delete(f"/projects/{project}/access_tokens/{token['id']}")
                response.raise_for_status()
                result = await _publish(
                    pe_db.worker_factory(),
                    scoped,
                    run,
                    branch,
                )
                assert not result.ok and "authoritative_read_failed" in result.reason
                with pytest.raises(GitLabAPIError) as refused:
                    await scoped.get_branch_head(project, branch)
                assert refused.value.status_code == 401
                assert await live_lab.client.get_branch_head(project, branch) == base
                intent = await _intent(pe_db.worker_factory(), run.id)
                assert intent.status == "dispatched"
                assert (
                    await live_lab.client.read_blob(
                        project,
                        "forge-demo/candidate.md",
                        ref=branch,
                    )
                ).confirmed_absent
            finally:
                await scoped.close()

    async def test_review_stays_open_draft_and_recovery_creates_no_second_review(
        self, pe_db, live_lab, live_projects
    ):
        project = live_lab.producer_project
        branch, base = await live_lab.new_publication_branch(project, "draft")
        factory = pe_db.worker_factory()
        run = await _new_run(factory, project_id=project, base_sha=base)
        result = await _publish(factory, live_lab.client, run, branch)
        assert result.ok
        settings = gl_settings(GITLAB_URL=live_projects.url, DATABASE_URL=pe_db.url)
        service = make_gitlab_service(factory, live_lab.client, settings=settings)
        iid = await service._create_draft_mr(project, run.id, branch, result.commit_sha)
        restarted = make_gitlab_service(pe_db.worker_factory(), live_lab.client, settings=settings)
        assert await restarted._create_draft_mr(project, run.id, branch, result.commit_sha) == iid
        matching = [
            mr
            for mr in await live_lab.client.list_merge_requests(project, state="all")
            if mr.source_branch == branch
        ]
        assert len(matching) == 1
        assert matching[0].state == "opened"
        assert matching[0].draft and matching[0].merged_at is None
