"""Actual CI candidate collection, bounded repair and SHA-bound readonly review."""

import difflib
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import FlowRun, FlowStatus, LLMCall, Outbox
from forge.factory.llm import LLMError
from forge.factory.planner import LLMPlanner
from forge.factory.reviewer import LLMReviewer
from forge.models.base import Base
from forge.runs import RunService
from forge.runs import publisher as publisher_module
from tests.fixtures.candidate import create_diff, seed_candidate
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.fixtures.fake_llm import FakeLLM

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."
BASE_SHA = "base-sha-1"
PLAN_JSON = json.dumps(
    {
        "summary": "Tweak the widget constant",
        "steps": ["bump x in src/app.py", "add a demo note"],
        "risks": ["low risk"],
        "files_hint": ["src/app.py"],
    }
)
REVIEW_OK_JSON = json.dumps({"verdict": "ok", "summary": "Clean change.", "findings": []})
REVIEW_CONCERNS_JSON = json.dumps(
    {
        "verdict": "concerns",
        "summary": "Works, but naming is unclear.",
        "findings": [
            {"severity": "minor", "file": "forge-demo/feature.md", "note": "Vague title."}
        ],
    }
)


def make_settings(**overrides):
    values = dict(
        _env_file=None,
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="fixture-token",
        GITLAB_WEBHOOK_SECRET="whsec",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_MAX_COMMIT_CYCLES=3,
        FORGE_REQUIRED_JOBS="",
    )
    values.update(overrides)
    return Settings(**values)


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
def fake_gitlab():
    fake = FakeGitLab()
    fake.ci_configuration = {"valid": True, "jobs": [{"name": "forge-agent-claude-code"}]}
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", BASE_SHA, "initial")
    fake.seed_file("src/app.py", "x = 1\ny = 2\n")
    return fake


def make_service(db, fake_gitlab, llm, settings=None):
    settings = settings or make_settings()
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings,
        planner=LLMPlanner(llm, settings=settings),
        reviewer=LLMReviewer(llm, gitlab=fake_gitlab, settings=settings),
    )


async def get_run(db, run_id):
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def llm_rows(db):
    async with db() as session:
        return (await session.execute(select(LLMCall).order_by(LLMCall.id))).scalars().all()


async def outbox_targets(db, run_id):
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                )
            )
            .scalars()
            .all()
        )
        return [row.payload["to"] for row in rows]


def branch_for(issue_iid, run_id):
    from forge.durable.identity import factory_branch

    return factory_branch(issue_iid, run_id)


async def start_harness(service):
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    run = await get_run(service._session_factory, run_id)
    assert run.status == FlowStatus.WAITING_HARNESS.value, run.status_reason
    assert not run.candidate_shas
    return run_id


async def seed_harness(service, fake, run_id, *, diff=None, meta_override=None):
    run = await get_run(service._session_factory, run_id)
    handle = json.loads(run.evidence["harness"]["handle"])
    pipeline_id = int(run.evidence["harness"]["pipeline_id"])
    job_id = 1000 + pipeline_id
    fake.set_pipeline_jobs(
        pipeline_id, [{"id": job_id, "name": "forge-agent-claude-code", "status": "success"}]
    )
    seed_candidate(
        fake,
        job_id,
        attempt_base=handle["attempt_base"],
        diff=diff if diff is not None else create_diff("forge-demo/feature.md", "# feature\n"),
        usage={"input_tokens": 500, "output_tokens": 200},
        meta_override=meta_override,
    )
    return job_id


async def finish_harness(service, fake, run_id, *, diff=None, meta_override=None):
    await seed_harness(service, fake, run_id, diff=diff, meta_override=meta_override)
    await service.evaluate_waiting_harness()
    return await get_run(service._session_factory, run_id)


async def start_and_go(service, fake):
    run_id = await start_harness(service)
    run = await finish_harness(service, fake, run_id)
    assert run.status == FlowStatus.WAITING_CI.value, run.status_reason
    return run_id


def update_diff(path, before, after):
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n" + "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/" + path,
            tofile="b/" + path,
        )
    )


def seed_pipeline(fake, branch, sha, status, jobs=None, logs=None):
    pipeline_id = fake._id()
    fake.pipelines.append({"id": pipeline_id, "ref": branch, "status": status, "sha": sha})
    if jobs:
        fake.set_pipeline_jobs(pipeline_id, jobs)
    for job_id, log in (logs or {}).items():
        fake.set_job_log(job_id, log)
    return pipeline_id


async def fail_candidate(
    service, fake, run_id, *, reason="script_failure", log="E AssertionError: widget count 0 != 1"
):
    run = await get_run(service._session_factory, run_id)
    seed_pipeline(
        fake,
        branch_for(ISSUE_IID, run_id),
        run.candidate_shas[-1],
        "failed",
        jobs=[{"id": 55, "name": "pytest", "status": "failed", "failure_reason": reason}],
        logs={55: log},
    )
    await service.evaluate_waiting_ci()
    return await get_run(service._session_factory, run_id)


def repair_brief(fake):
    return next(v["value"] for v in fake.pipelines[-1]["variables"] if v["key"] == "FORGE_PLAN")


class TestHappyPath:
    async def test_issue_to_ready_with_review_bound_to_candidate_sha(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_and_go(service, fake_gitlab)
        run = await get_run(db, run_id)
        candidate = run.candidate_shas[-1]
        message = fake_gitlab.branches[branch_for(ISSUE_IID, run_id)][0]["message"]
        assert run_id[:8] in message and "(forge-op:" in message
        pipeline_id = seed_pipeline(
            fake_gitlab, branch_for(ISSUE_IID, run_id), candidate, "success"
        )
        await service.evaluate_waiting_ci()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value, run.status_reason
        assert {
            key: run.evidence["review"][key] for key in ("verdict", "sha", "summary", "findings")
        } == {
            "verdict": "ok",
            "sha": candidate,
            "summary": "Clean change.",
            "findings": [],
        }
        assert run.evidence["pipeline"]["id"] == pipeline_id and run.commit_cycle == 1
        rows = await llm_rows(db)
        assert [r.role for r in rows] == ["planner", "implementer", "reviewer"]
        assert all(r.status == "ok" and r.input_tokens is not None for r in rows)
        assert rows[1].provider == "ci_harness" and llm.roles() == ["planner", "reviewer"]
        (note,) = fake_gitlab.notes_containing("ready for human")
        assert candidate in note["body"] and "Clean change." in note["body"]

    async def test_branch_head_drift_still_blocks(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_and_go(service, fake_gitlab)
        candidate = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), candidate, "success")
        fake_gitlab.seed_commit(branch_for(ISSUE_IID, run_id), "human-sha", "human push")
        await service.evaluate_waiting_ci()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value and run.status_reason == "external_change"


class TestRepairLoop:
    async def test_code_failure_repairs_without_a_second_gate(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_and_go(service, fake_gitlab)
        first = (await get_run(db, run_id)).candidate_shas[-1]
        run = await fail_candidate(service, fake_gitlab, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value and run.commit_cycle == 2
        assert run.candidate_shas == [first]
        assert "AssertionError" in repair_brief(fake_gitlab) and "pytest" in repair_brief(
            fake_gitlab
        )
        run = await finish_harness(
            service,
            fake_gitlab,
            run_id,
            diff=update_diff("src/app.py", "x = 1\ny = 2\n", "x = 1\nFIXED = True\ny = 2\n"),
        )
        assert run.status == FlowStatus.WAITING_CI.value and len(run.candidate_shas) == 2
        assert run.candidate_shas[-1] != first
        targets = await outbox_targets(db, run_id)
        assert targets.count("evaluating_ci") == 1 and targets.count("waiting_ci") == 2
        assert targets.count("waiting_approval") == 1
        assert len(fake_gitlab.mr_updates) == 1
        assert run.candidate_shas[-1] in fake_gitlab.mr_updates[0]["description"]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), run.candidate_shas[-1], "success")
        await service.evaluate_waiting_ci()
        final = await get_run(db, run_id)
        assert final.status == FlowStatus.READY_FOR_HUMAN.value
        assert final.evidence["review"]["sha"] == run.candidate_shas[-1]
        assert llm.roles() == ["planner", "reviewer"]

    async def test_repair_updates_a_file_created_in_cycle_one(self, db, fake_gitlab, monkeypatch):
        llm = FakeLLM(db, script=[PLAN_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)
        bases = []
        fetch = publisher_module._fetch_base_contents

        async def observed(gitlab, project, ref, paths):
            bases.append(ref)
            return await fetch(gitlab, project, ref, paths)

        monkeypatch.setattr(publisher_module, "_fetch_base_contents", observed)
        run_id = await start_and_go(service, fake_gitlab)
        first = (await get_run(db, run_id)).candidate_shas[-1]
        fake_gitlab.seed_file("forge-demo/feature.md", "# feature\n")
        await fail_candidate(service, fake_gitlab, run_id)
        run = await finish_harness(
            service,
            fake_gitlab,
            run_id,
            diff=update_diff("forge-demo/feature.md", "# feature\n", "# feature\nstatus: fixed\n"),
        )
        assert run.status == FlowStatus.WAITING_CI.value and bases == [BASE_SHA, first]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), run.candidate_shas[-1], "success")
        await service.evaluate_waiting_ci()
        assert (await get_run(db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value
        prompt = llm.calls_for("reviewer")[0]["user"]
        assert f"Candidate diff ({BASE_SHA[:8]}..{run.candidate_shas[-1][:8]})" in prompt

    async def test_resume_adopts_the_recorded_harness_handle(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_harness(service)
        await seed_harness(service, fake_gitlab, run_id)
        restarted = make_service(db, fake_gitlab, llm)
        await restarted.evaluate_waiting_harness()
        await restarted.evaluate_waiting_harness()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value and len(run.candidate_shas) == 1
        assert len(fake_gitlab.calls_of("create_commit")) == 1 and len(fake_gitlab.pipelines) == 1
        assert llm.roles() == ["planner"]

    async def test_resume_publishes_on_the_recorded_base_when_default_branch_moved(
        self, db, fake_gitlab, monkeypatch
    ):
        llm = FakeLLM(db, script=[PLAN_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_harness(service)
        fake_gitlab.seed_commit("main", "new-main-head", "main moved after dispatch")
        run = await finish_harness(make_service(db, fake_gitlab, llm), fake_gitlab, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.evidence["published_candidate"]["attempt_base"] == BASE_SHA
        assert fake_gitlab.calls_of("get_file")[-1][1][-1] == BASE_SHA
        assert len(fake_gitlab.calls_of("create_commit")) == 1 and llm.roles() == ["planner"]

    async def test_human_push_on_the_factory_branch_blocks_never_force_fixed(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_harness(service)
        branch = branch_for(ISSUE_IID, run_id)
        fake_gitlab.seed_commit(branch, "human-push-sha", "human tweak")
        run = await finish_harness(service, fake_gitlab, run_id)
        assert run.status == FlowStatus.BLOCKED.value and "branch_drift" in run.status_reason
        assert fake_gitlab.branches[branch][0]["sha"] == "human-push-sha"
        assert not fake_gitlab.calls_of("create_commit")

    async def test_repair_budget_exhausted_blocks(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON])
        service = make_service(db, fake_gitlab, llm, make_settings(FORGE_MAX_COMMIT_CYCLES=1))
        run_id = await start_and_go(service, fake_gitlab)
        before = len(fake_gitlab.pipelines)
        run = await fail_candidate(service, fake_gitlab, run_id)
        assert run.status == FlowStatus.BLOCKED.value and run.status_reason.startswith(
            "commit_cycles_exhausted"
        )
        assert len(fake_gitlab.pipelines) == before + 1 and llm.roles() == ["planner"]

    @pytest.mark.parametrize(
        "kind,reason",
        [("runner_system_failure", "infrastructure_failure"), ("config_error", "config_failure")],
    )
    async def test_infrastructure_failure_blocks_without_repair(
        self, db, fake_gitlab, kind, reason
    ):
        llm = FakeLLM(db, script=[PLAN_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_and_go(service, fake_gitlab)
        run = await fail_candidate(service, fake_gitlab, run_id, reason=kind)
        assert run.status == FlowStatus.BLOCKED.value and run.status_reason.startswith(reason)
        assert len(fake_gitlab.pipelines) == 2 and len(run.candidate_shas) == 1
        assert llm.roles() == ["planner"]

    async def test_unknown_failure_blocks_without_repair(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_and_go(service, fake_gitlab)
        run = await get_run(db, run_id)
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            run.candidate_shas[-1],
            "failed",
            jobs=[{"id": 1, "name": "build", "status": "canceled"}],
        )
        await service.evaluate_waiting_ci()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value and run.status_reason.startswith(
            "unknown_failure"
        )
        assert len(fake_gitlab.pipelines) == 2 and llm.roles() == ["planner"]


class TestCandidateMaterialization:
    async def test_update_near_start_of_large_file_preserves_tail(self, db, fake_gitlab):
        tail = "".join(f"line {i:04d}\n" for i in range(1000))
        fake_gitlab.seed_file("src/app.py", "x = 1\n" + tail)
        service = make_service(db, fake_gitlab, FakeLLM(db, script=[PLAN_JSON]))
        run_id = await start_harness(service)
        run = await finish_harness(
            service,
            fake_gitlab,
            run_id,
            diff=update_diff("src/app.py", "x = 1\n" + tail, "x = 42\n" + tail),
        )
        assert run.status == FlowStatus.WAITING_CI.value
        action = fake_gitlab.calls_of("create_commit")[0][1][2][0]
        assert action["content"] == "x = 42\n" + tail and len(action["content"]) > 6000

    async def test_evidence_prompt_stays_truncated(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab, FakeLLM(db, script=[PLAN_JSON]))
        run_id = await start_and_go(service, fake_gitlab)
        run = await fail_candidate(
            service, fake_gitlab, run_id, log="BEGIN_SENTINEL" + "x" * 30000 + "TAIL_SENTINEL"
        )
        assert run.status == FlowStatus.WAITING_HARNESS.value
        brief = repair_brief(fake_gitlab)
        assert "BEGIN_SENTINEL" not in brief and "TAIL_SENTINEL" in brief
        assert len(brief) < 12000

    async def test_oversized_base_file_raises_instead_of_truncating(
        self, db, fake_gitlab, monkeypatch
    ):
        monkeypatch.setattr(publisher_module, "FORGE_MATERIALIZE_MAX_FILE_CHARS", 100)
        fake_gitlab.seed_file("src/app.py", "x = 1\n" + "y" * 200)
        service = make_service(db, fake_gitlab, FakeLLM(db, script=[PLAN_JSON]))
        run_id = await start_harness(service)
        run = await finish_harness(
            service,
            fake_gitlab,
            run_id,
            diff=update_diff("src/app.py", "x = 1\n" + "y" * 200, "x = 42\n" + "y" * 200),
        )
        assert run.status == FlowStatus.BLOCKED.value and "file_too_large" in run.status_reason
        assert not fake_gitlab.calls_of("create_commit")

    async def test_attempt_base_reads_authoritative_contents_from_that_ref(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab, FakeLLM(db, script=[PLAN_JSON]))
        run_id = await start_and_go(service, fake_gitlab)
        first = (await get_run(db, run_id)).candidate_shas[-1]
        await fail_candidate(service, fake_gitlab, run_id)
        fake_gitlab.calls.clear()
        run = await finish_harness(
            service,
            fake_gitlab,
            run_id,
            diff=update_diff("src/app.py", "x = 1\ny = 2\n", "x = 42\ny = 2\n"),
        )
        assert run.status == FlowStatus.WAITING_CI.value
        refs = [call[1][-1] for call in fake_gitlab.calls_of("get_file")]
        assert refs and set(refs) == {first}
        assert run.evidence["published_candidate"]["attempt_base"] == first

    async def test_attempt_base_defaults_to_the_pinned_run_base(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab, FakeLLM(db, script=[PLAN_JSON]))
        run_id = await start_harness(service)
        run = await finish_harness(service, fake_gitlab, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.evidence["published_candidate"]["attempt_base"] == BASE_SHA
        assert fake_gitlab.calls_of("get_file")[-1][1][-1] == BASE_SHA


class TestQualityContract:
    @pytest.mark.parametrize("status", ["skipped", "missing"])
    async def test_skipped_required_job_blocks_a_green_pipeline(self, db, fake_gitlab, status):
        llm = FakeLLM(db, script=[PLAN_JSON])
        service = make_service(db, fake_gitlab, llm, make_settings(FORGE_REQUIRED_JOBS="pytest"))
        run_id = await start_and_go(service, fake_gitlab)
        run = await get_run(db, run_id)
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            run.candidate_shas[-1],
            "success",
            jobs=[{"id": 1, "name": "pytest", "status": status}] if status != "missing" else [],
        )
        await service.evaluate_waiting_ci()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value and run.status_reason.startswith(
            "quality_contract"
        )
        assert run.evidence.get("review") is None and llm.roles() == ["planner"]


class TestReviewLeg:
    async def test_concerns_verdict_still_reaches_ready_with_findings(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, REVIEW_CONCERNS_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_and_go(service, fake_gitlab)
        run = await get_run(db, run_id)
        candidate = run.candidate_shas[-1]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), candidate, "success")
        await service.evaluate_waiting_ci()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        review = run.evidence["review"]
        assert review["sha"] == candidate and review["verdict"] == "concerns"
        assert review["findings"] == [
            {"severity": "minor", "file": "forge-demo/feature.md", "note": "Vague title."}
        ]
        (note,) = fake_gitlab.mr_notes_containing("readonly review")
        assert "concerns" in note["body"] and "Vague title." in note["body"]

    async def test_review_not_bound_to_candidate_sha_blocks(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_and_go(service, fake_gitlab)
        run = await get_run(db, run_id)
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), run.candidate_shas[-1], "success")

        async def dropping_merge(_run_id, _patch):
            return None

        service._merge_run_evidence = dropping_merge
        await service.evaluate_waiting_ci()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value and run.status_reason.startswith(
            "review_sha_mismatch"
        )


class TestAgentFailures:
    async def test_planner_failure_parks_run_blocked_and_journals(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[LLMError("proxy down")])
        service = make_service(db, fake_gitlab, llm)
        with pytest.raises(LLMError):
            await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        async with db() as session:
            (run,) = (await session.execute(select(FlowRun))).scalars().all()
        assert run.status == FlowStatus.BLOCKED.value and run.status_reason.startswith(
            "planning_failed"
        )
        (row,) = await llm_rows(db)
        assert row.status == "failed" and row.role == "planner"

    async def test_candidate_invalid_json_parks_run_blocked_without_model_call(
        self, db, fake_gitlab
    ):
        llm = FakeLLM(db, script=[PLAN_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_harness(service)
        job = await seed_harness(service, fake_gitlab, run_id)
        fake_gitlab.seed_job_artifact(job, ".forge/candidate.meta.json", "not json")
        await service.evaluate_waiting_harness()
        run = await get_run(db, run_id)
        assert (
            run.status == FlowStatus.BLOCKED.value
            and run.status_reason == "harness_code: harness_artifact_missing"
        )
        assert not run.candidate_shas and not fake_gitlab.calls_of("create_commit")
        assert llm.roles() == ["planner"]

    async def test_unmaterializable_update_blocks_the_run(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab, FakeLLM(db, script=[PLAN_JSON]))
        run_id = await start_harness(service)
        run = await finish_harness(
            service,
            fake_gitlab,
            run_id,
            diff=update_diff("src/app.py", "missing text\n", "whatever\n"),
        )
        assert run.status == FlowStatus.BLOCKED.value and "candidate_rejected" in run.status_reason
        assert not fake_gitlab.calls_of("create_commit")
