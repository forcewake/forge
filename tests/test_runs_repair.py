"""M2-1 integration tests: real factory agents over FakeLLM + FakeGitLab.

Covers the ADR-0008 quality contract inside RunService: review binding,
failure classification, the bounded repair loop and ledger coverage — all
offline, with scripted model responses.
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import FlowRun, FlowStatus, LLMCall, Outbox
from forge.factory import implementer as implementer_module
from forge.factory.implementer import IMPLEMENTER_MAX_FILE_CHARS, LLMImplementer
from forge.factory.llm import LLMError
from forge.factory.planner import LLMPlanner
from forge.factory.reviewer import LLMReviewer
from forge.models.base import Base
from forge.repository import Change, ChangeSet, Operation, changeset_to_document
from forge.repository.changeset import MaterializationError
from forge.runs import RunService
from forge.runs import service as service_module
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.fixtures.fake_llm import FakeLLM

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."
BASE_SHA = "base-sha-1"


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_MAX_COMMIT_CYCLES=3,
        FORGE_REQUIRED_JOBS="",
    )
    values.update(overrides)
    return Settings(**values)


PLAN_JSON = json.dumps(
    {
        "summary": "Tweak the widget constant",
        "steps": ["bump x in src/app.py", "add a demo note"],
        "risks": ["low risk"],
        "files_hint": ["src/app.py"],
    }
)

CREATE_JSON = json.dumps(
    {
        "branch": "model/chose/this",
        "commit_message": "model's own message",
        "changes": [
            {"path": "forge-demo/feature.md", "operation": "create", "content": "# feature\n"}
        ],
    }
)

REPAIR_JSON = json.dumps(
    {
        "branch": "model/chose/this",
        "commit_message": "model's repair",
        "changes": [
            {
                "path": "src/app.py",
                "operation": "update",
                "old_text": "x = 1\n",
                "new_text": "x = 1\nFIXED = True\n",
            }
        ],
    }
)

REPAIR_CREATED_FILE_JSON = json.dumps(
    {
        "branch": "model/chose/this",
        "commit_message": "model's repair",
        "changes": [
            {
                "path": "forge-demo/feature.md",
                "operation": "update",
                "old_text": "# feature\n",
                "new_text": "# feature\nstatus: fixed\n",
            }
        ],
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
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", BASE_SHA, "initial")
    fake.seed_file("src/app.py", "x = 1\ny = 2\n")
    fake.seed_file("docs/readme.md", "# readme\n")
    return fake


def make_service(db, fake_gitlab, llm, settings=None) -> RunService:
    """RunService with the REAL factory agents over a scripted FakeLLM."""
    settings = settings or make_settings()
    planner = LLMPlanner(llm, settings=settings)
    implementer = LLMImplementer(llm, gitlab=fake_gitlab, settings=settings)
    reviewer = LLMReviewer(llm, gitlab=fake_gitlab, settings=settings)
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings,
        planner=planner,
        implementer=implementer,
        reviewer=reviewer,
    )


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def llm_rows(db) -> list[LLMCall]:
    async with db() as session:
        return (await session.execute(select(LLMCall).order_by(LLMCall.id))).scalars().all()


async def outbox_targets(db, run_id: str) -> list[str]:
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


async def start_and_go(service: RunService, fake_gitlab: FakeGitLab) -> str:
    """@forge /implement + /go — the run lands in waiting_ci."""
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    return run_id


def seed_pipeline(
    fake_gitlab: FakeGitLab,
    branch: str,
    sha: str,
    status: str,
    jobs: list[dict] | None = None,
    logs: dict[int, str] | None = None,
) -> int:
    pipeline_id = fake_gitlab._id()
    fake_gitlab.pipelines.append({"id": pipeline_id, "ref": branch, "status": status, "sha": sha})
    if jobs:
        fake_gitlab.set_pipeline_jobs(pipeline_id, jobs)
    for job_id, log in (logs or {}).items():
        fake_gitlab.set_job_log(job_id, log)
    return pipeline_id


def branch_for(issue_iid: int, run_id: str) -> str:
    from forge.runs.stubs import factory_branch

    return factory_branch(issue_iid, run_id)


class TestHappyPath:
    async def test_issue_to_ready_with_review_bound_to_candidate_sha(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        candidate_sha = run.candidate_shas[-1]
        # Trusted identity overrides whatever branch/message the model chose;
        # the writer appends its unique per-apply operation marker (F07).
        message = fake_gitlab.branches[branch_for(ISSUE_IID, run_id)][0]["message"]
        assert message.startswith(f"forge: implement {ISSUE_IID} (run {run_id[:8]}) ")
        assert "(forge-op:" in message and message.endswith(")")

        pipeline_id = seed_pipeline(
            fake_gitlab, branch_for(ISSUE_IID, run_id), candidate_sha, "success"
        )
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

        # ADR-0008: review evidence is bound to the exact reviewed SHA.
        review = (run.evidence or {}).get("review")
        assert review["verdict"] == "ok"
        assert review["sha"] == candidate_sha
        assert review["summary"] == "Clean change."
        assert (run.evidence or {}).get("pipeline", {}).get("id") == pipeline_id
        assert run.commit_cycle == 1

        # Every model call is in the ledger (ADR-0013).
        roles = [row.role for row in await llm_rows(db)]
        assert roles == ["planner", "implementer", "reviewer"]
        assert all(row.status == "ok" for row in await llm_rows(db))
        assert all(row.input_tokens is not None for row in await llm_rows(db))

        # The evidence comment carries the review summary line.
        (note,) = fake_gitlab.notes_containing("ready for human")
        assert candidate_sha in note["body"]
        assert "Clean change." in note["body"]

    async def test_branch_head_drift_still_blocks(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)
        run_id = await start_and_go(service, fake_gitlab)
        candidate_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), candidate_sha, "success")
        fake_gitlab.seed_commit(branch_for(ISSUE_IID, run_id), "human-sha", "human push")

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "external_change"


class TestRepairLoop:
    async def test_code_failure_repairs_without_a_second_gate(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON, REPAIR_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]

        failed_job_id = 55
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            first_sha,
            "failed",
            jobs=[
                {
                    "id": failed_job_id,
                    "name": "pytest",
                    "status": "failed",
                    "failure_reason": "script_failure",
                },
                {"id": 56, "name": "build", "status": "success"},
            ],
            logs={failed_job_id: "E  AssertionError: widget count 0 != 1"},
        )
        await service.evaluate_waiting_ci()

        # The repair cycle ran end-to-end: second commit, no second /go.
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.commit_cycle == 2
        assert len(run.candidate_shas) == 2
        assert run.candidate_shas[-1] != first_sha

        # The implementer's repair prompt carried the bounded failure logs.
        repair_call = llm.calls_for("implementer")[1]
        assert "AssertionError" in repair_call["user"]
        assert "pytest" in repair_call["user"]

        # Outbox walk: evaluating_ci re-entered proposing (ADR-0004 graph).
        targets = await outbox_targets(db, run_id)
        assert targets.count("evaluating_ci") == 1
        assert targets.count("proposing") == 2
        assert targets.count("waiting_ci") == 2

        # The Draft MR was updated in place, with the repair bot comment.
        assert len(fake_gitlab.mr_updates) == 1
        assert run.candidate_shas[-1] in fake_gitlab.mr_updates[0]["description"]
        (repair_note,) = fake_gitlab.mr_notes_containing("Repair cycle 2")
        assert "pytest" in repair_note["body"]

        # Ledger: planner + two implementer calls so far.
        assert llm.roles() == ["planner", "implementer", "implementer"]

        # Now the repaired candidate passes CI and the review closes the run.
        repaired_sha = run.candidate_shas[-1]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), repaired_sha, "success")
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        review = (run.evidence or {}).get("review")
        assert review["sha"] == repaired_sha
        assert llm.roles() == ["planner", "implementer", "implementer", "reviewer"]

    async def test_repair_updates_a_file_created_in_cycle_one(self, db, fake_gitlab, monkeypatch):
        """R06: create in cycle 1 → CI failure → update the NEW file in cycle 2.

        Every leg of the repair attempt uses the ATTEMPT base (the last
        verified candidate): materialization, the update/delete existence
        check and the write. The frozen source base would report cycle 1's
        file as missing and block the repair; the review still diffs the
        source base so the human sees the cumulative result.
        """
        llm = FakeLLM(
            db,
            script=[PLAN_JSON, CREATE_JSON, REPAIR_CREATED_FILE_JSON, REVIEW_OK_JSON],
        )
        service = make_service(db, fake_gitlab, llm)

        validation_bases: list[str | None] = []
        real_fetch = service_module.fetch_git_base

        async def spy_fetch(gitlab, project_id, paths, base_sha):
            validation_bases.append(base_sha)
            return await real_fetch(gitlab, project_id, paths, base_sha)

        monkeypatch.setattr(service_module, "fetch_git_base", spy_fetch)

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]
        # Cycle 1 committed the new file on the factory branch; the fake's
        # snapshot is ref-independent, so seed the post-commit state every ref
        # read must return.
        fake_gitlab.seed_file("forge-demo/feature.md", "# feature\n")

        failed_job_id = 55
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            first_sha,
            "failed",
            jobs=[
                {
                    "id": failed_job_id,
                    "name": "pytest",
                    "status": "failed",
                    "failure_reason": "script_failure",
                }
            ],
            logs={failed_job_id: "E  AssertionError: feature file incomplete"},
        )
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.commit_cycle == 2
        # Cycle 1 validated at the source base; the repair validated at the
        # attempt base — cycle 1's file is visible to the update.
        assert validation_bases == [BASE_SHA, first_sha]

        repaired_sha = run.candidate_shas[-1]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), repaired_sha, "success")
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        # The cumulative review spans source base .. final candidate — both
        # cycles, not just the repair.
        review_user = llm.calls_for("reviewer")[0]["user"]
        assert f"Candidate diff ({BASE_SHA[:8]}..{repaired_sha[:8]})" in review_user

    async def test_resume_adopts_the_recorded_manifest(self, db, fake_gitlab):
        """R06: a crashed attempt resumes on its persisted record.

        The attempt number, manifest and previous candidate are persisted the
        moment a proposal materializes; the resumed walk re-adopts exactly
        that manifest instead of paying for a second, possibly different,
        proposal.
        """
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]

        # Rewind to the crash the resume path covers: the attempt had
        # materialized (manifest recorded) but nothing after it persisted.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.VALIDATING.value
            run.candidate_shas = []
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        # The walk re-adopted the recorded manifest: the journaled commit is
        # adopted (no second commit) and no new proposal was bought — an
        # exhausted script would have produced a broken draft and blocked.
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.candidate_shas == [first_sha]
        assert llm.roles() == ["planner", "implementer"]

    async def test_resume_publishes_on_the_recorded_base_when_state_drifted(
        self, db, fake_gitlab, monkeypatch
    ):
        """R06/ADR-0017 §3: mid-leg, the persisted attempt record wins.

        The resumed walk continues the attempt it already started — the
        recorded base and manifest — even where a re-derivation from durable
        state would pin a different base. Re-deriving would aim the remaining
        legs at a snapshot the proposal never read and re-propose (a second
        paid call) changes that are already materialized.
        """
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(db, fake_gitlab, llm)

        validation_bases: list[str | None] = []
        real_fetch = service_module.fetch_git_base

        async def spy_fetch(gitlab, project_id, paths, base_sha):
            validation_bases.append(base_sha)
            return await real_fetch(gitlab, project_id, paths, base_sha)

        monkeypatch.setattr(service_module, "fetch_git_base", spy_fetch)

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]
        branch = branch_for(ISSUE_IID, run_id)
        manifest = changeset_to_document(
            ChangeSet(
                branch=branch,
                commit_message=f"forge: implement {ISSUE_IID} (run {run_id[:8]})",
                changes=[
                    Change(
                        path="forge-demo/feature.md",
                        operation=Operation.CREATE,
                        content="# feature\n",
                    )
                ],
                attempt_base_oid=first_sha,
            )
        )

        # Rewind mid-attempt with the durable state DRIFTED from the record:
        # a re-derivation would pin ``divergent-sha``, the record pins the
        # snapshot the proposal was materialized against.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.VALIDATING.value
            run.commit_cycle = 2
            run.candidate_shas = ["divergent-sha"]
            run.evidence = {
                "attempt": {
                    "cycle": 2,
                    "attempt_base": first_sha,
                    "source_base": BASE_SHA,
                    "previous_candidate": "divergent-sha",
                    "manifest": manifest,
                }
            }
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        # The walk continued the recorded attempt: validated at the RECORDED
        # base and re-adopted the journaled candidate — no second proposal
        # and no second commit.
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.candidate_shas == ["divergent-sha", first_sha]
        assert validation_bases == [BASE_SHA, first_sha]
        assert llm.roles() == ["planner", "implementer"]
        branch_shas = [c["sha"] for c in fake_gitlab.branches[branch] if c["sha"] != BASE_SHA]
        assert branch_shas == [first_sha]

    async def test_human_push_on_the_factory_branch_blocks_never_force_fixed(self, db, fake_gitlab):
        """A human push on the factory branch is reported, never force-fixed.

        The journaled candidate is no longer the branch head, so nothing is
        adopted and the guarded apply refuses: the run blocks with the reason
        instead of writing a second candidate over the human's commit.
        """
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]
        branch = branch_for(ISSUE_IID, run_id)

        human_sha = "human-push-sha"
        fake_gitlab.seed_commit(branch, human_sha, "human tweak")

        # Rewind to the crash the resume path covers: the attempt had
        # materialized (manifest recorded) but the candidate sha never
        # persisted, so the walk re-enters at the write.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.VALIDATING.value
            run.candidate_shas = []
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "branch_drift" in (run.status_reason or "")
        # The human's commit stands on top of the candidate — no force-fix.
        branch_shas = [c["sha"] for c in fake_gitlab.branches[branch] if c["sha"] != BASE_SHA]
        assert branch_shas == [human_sha, first_sha]
        assert llm.roles() == ["planner", "implementer"]

    async def test_repair_budget_exhausted_blocks(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(
            db, fake_gitlab, llm, settings=make_settings(FORGE_MAX_COMMIT_CYCLES=1)
        )

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            first_sha,
            "failed",
            jobs=[
                {"id": 1, "name": "pytest", "status": "failed", "failure_reason": "script_failure"}
            ],
        )
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("commit_cycles_exhausted")
        # No repair LLM call was burned: planner + one implementer, that's it.
        assert llm.roles() == ["planner", "implementer"]

    async def test_infrastructure_failure_blocks_without_repair(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            first_sha,
            "failed",
            jobs=[
                {
                    "id": 1,
                    "name": "build",
                    "status": "failed",
                    "failure_reason": "runner_system_failure",
                }
            ],
        )
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("infrastructure_failure")
        # Never burn LLM repairs on infra (ADR-0008): no second implementer.
        assert llm.roles() == ["planner", "implementer"]
        assert len(run.candidate_shas) == 1

    async def test_config_failure_blocks_without_repair(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            first_sha,
            "failed",
            jobs=[
                {"id": 1, "name": "ci-config", "status": "failed", "failure_reason": "config_error"}
            ],
        )
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_failure")
        assert llm.roles() == ["planner", "implementer"]

    async def test_unknown_failure_blocks_without_repair(self, db, fake_gitlab):
        """Empty evidence (a canceled-only pipeline) is not a code failure:
        blocked as unknown_failure — never a repair LLM call (F18)."""
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            first_sha,
            "failed",
            jobs=[{"id": 1, "name": "build", "status": "canceled"}],
        )
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("unknown_failure")
        assert llm.roles() == ["planner", "implementer"]
        assert len(run.candidate_shas) == 1


def _update_draft(path: str, old_text: str, new_text: str) -> str:
    return json.dumps(
        {
            "branch": "model/chose/this",
            "commit_message": "model's own message",
            "changes": [
                {"path": path, "operation": "update", "old_text": old_text, "new_text": new_text}
            ],
        }
    )


class TestImplementerMaterialization:
    """F01/F02 regressions at the implementer boundary: bounded evidence for
    the prompt, COMPLETE blobs for materialization, and the attempt_base
    working ref a repair cycle reads instead of the pinned base."""

    def make_run(self, **overrides) -> FlowRun:
        values = dict(id="runabc123", project_id=PROJECT_ID, issue_iid=ISSUE_IID, base_sha=BASE_SHA)
        values.update(overrides)
        return FlowRun(**values)

    def make_implementer(self, db, fake_gitlab, script):
        llm = FakeLLM(db, script=script)
        return llm, LLMImplementer(llm, gitlab=fake_gitlab, settings=make_settings())

    async def test_update_near_start_of_large_file_preserves_tail(self, db, fake_gitlab):
        # F01: materialization must run against the complete file — an edit
        # inside the first 6000 chars may not drop the tail beyond them.
        tail = "".join(f"line {i:04d}\n" for i in range(1000))
        fake_gitlab.seed_file("src/app.py", "x = 1\n" + tail)
        llm, implementer = self.make_implementer(
            db, fake_gitlab, [_update_draft("src/app.py", "x = 1\n", "x = 42\n")]
        )

        cs = await implementer.propose(self.make_run(), ISSUE_TITLE, files_hint=["src/app.py"])

        assert len(cs.changes[0].content) > 6000
        assert cs.changes[0].content == "x = 42\n" + tail

    async def test_evidence_prompt_stays_truncated(self, db, fake_gitlab):
        # The prompt path is unchanged: file evidence is capped at 6000 chars.
        tail = "".join(f"line {i:04d}\n" for i in range(1000))
        big = "x = 1\n" + tail
        fake_gitlab.seed_file("src/app.py", big)
        llm, implementer = self.make_implementer(
            db, fake_gitlab, [_update_draft("src/app.py", "x = 1\n", "x = 42\n")]
        )

        await implementer.propose(self.make_run(), ISSUE_TITLE, files_hint=["src/app.py"])

        prompt = llm.calls_for("implementer")[0]["user"]
        assert f"--- FILE: src/app.py ---\n{big[:IMPLEMENTER_MAX_FILE_CHARS]}" in prompt
        assert "line 0999\n" not in prompt

    async def test_oversized_base_file_raises_instead_of_truncating(
        self, db, fake_gitlab, monkeypatch
    ):
        # 206 chars: under the evidence cap, over the materialization cap —
        # the raise proves the cap, not evidence truncation, fires.
        monkeypatch.setattr(implementer_module, "FORGE_MATERIALIZE_MAX_FILE_CHARS", 100)
        fake_gitlab.seed_file("src/app.py", "x = 1\n" + "y" * 200)
        llm, implementer = self.make_implementer(
            db, fake_gitlab, [_update_draft("src/app.py", "x = 1\n", "x = 42\n")]
        )

        with pytest.raises(MaterializationError):
            await implementer.propose(self.make_run(), ISSUE_TITLE, files_hint=["src/app.py"])

    async def test_attempt_base_reads_tree_and_contents_from_that_ref(self, db, fake_gitlab):
        candidate_sha = "candidate-sha-9"
        llm, implementer = self.make_implementer(
            db, fake_gitlab, [_update_draft("src/app.py", "x = 1\n", "x = 42\n")]
        )

        cs = await implementer.propose(self.make_run(), ISSUE_TITLE, attempt_base=candidate_sha)

        assert [call[1][2] for call in fake_gitlab.calls_of("get_tree")] == [candidate_sha]
        file_refs = [call[1][2] for call in fake_gitlab.calls_of("get_file")]
        assert file_refs and set(file_refs) == {candidate_sha}
        assert cs.attempt_base_oid == candidate_sha

    async def test_attempt_base_defaults_to_the_pinned_run_base(self, db, fake_gitlab):
        llm, implementer = self.make_implementer(
            db, fake_gitlab, [_update_draft("src/app.py", "x = 1\n", "x = 42\n")]
        )

        cs = await implementer.propose(self.make_run(), ISSUE_TITLE)

        assert [call[1][2] for call in fake_gitlab.calls_of("get_tree")] == [BASE_SHA]
        assert cs.attempt_base_oid == BASE_SHA


class TestQualityContract:
    async def test_skipped_required_job_blocks_a_green_pipeline(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(
            db, fake_gitlab, llm, settings=make_settings(FORGE_REQUIRED_JOBS="pytest")
        )

        run_id = await start_and_go(service, fake_gitlab)
        candidate_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            candidate_sha,
            "success",
            jobs=[{"id": 1, "name": "pytest", "status": "skipped"}],
        )
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("quality_contract")
        assert "skipped" in run.status_reason
        # No review happened — the contract failed before the review leg.
        assert llm.roles() == ["planner", "implementer"]
        assert (run.evidence or {}).get("review") is None

    async def test_missing_required_job_blocks_a_green_pipeline(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON])
        service = make_service(
            db, fake_gitlab, llm, settings=make_settings(FORGE_REQUIRED_JOBS="sast")
        )

        run_id = await start_and_go(service, fake_gitlab)
        candidate_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), candidate_sha, "success")
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "not found" in run.status_reason


class TestReviewLeg:
    async def test_concerns_verdict_still_reaches_ready_with_findings(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON, REVIEW_CONCERNS_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        candidate_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), candidate_sha, "success")
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        review = (run.evidence or {}).get("review")
        assert review["verdict"] == "concerns"
        assert review["sha"] == candidate_sha
        assert review["findings"] == [
            {"severity": "minor", "file": "forge-demo/feature.md", "note": "Vague title."}
        ]

        # Findings were posted to the Draft MR and echoed in the reason.
        (mr_note,) = fake_gitlab.mr_notes_containing("readonly review")
        assert "concerns" in mr_note["body"]
        assert "Vague title." in mr_note["body"]

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
        ready = next(r for r in rows if r.payload["to"] == "ready_for_human")
        assert "concerns" in ready.payload["reason"]

    async def test_review_not_bound_to_candidate_sha_blocks(self, db, fake_gitlab):
        """ADR-0008 self-check: refuse ready when the review SHA mismatches."""
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON, REVIEW_OK_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await start_and_go(service, fake_gitlab)
        candidate_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_pipeline(fake_gitlab, branch_for(ISSUE_IID, run_id), candidate_sha, "success")

        # Sabotage the evidence write so no review sha is ever recorded.
        async def dropping_merge(_run_id, _patch):
            return None

        service._merge_run_evidence = dropping_merge
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("review_sha_mismatch")


class TestAgentFailures:
    async def test_planner_failure_parks_run_blocked_and_journals(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[LLMError("proxy down")])
        service = make_service(db, fake_gitlab, llm)

        with pytest.raises(LLMError):
            await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        run = await get_run(db, (await _only_run_id(db)))
        assert run is not None
        assert run.status == FlowStatus.BLOCKED.value  # fatal: parked, never silent
        assert run.status_reason.startswith("planning_failed")

        (row,) = await llm_rows(db)
        assert row.status == "failed"
        assert row.role == "planner"

    async def test_implementer_invalid_json_parks_run_blocked_and_journals(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, "this is not json"])
        service = make_service(db, fake_gitlab, llm)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        # The service converts the parse failure into a terminal state instead
        # of letting it escape (the ledger still records the failed call).
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value  # fatal: parked, never silent
        assert run.status_reason.startswith("proposal_failed")

        implementer_rows = [row for row in await llm_rows(db) if row.role == "implementer"]
        assert len(implementer_rows) == 1
        assert implementer_rows[0].status == "failed"
        assert "invalid_json" in implementer_rows[0].error

    async def test_unmaterializable_update_blocks_the_run(self, db, fake_gitlab):
        bad_update = json.dumps(
            {
                "branch": "b",
                "commit_message": "m",
                "changes": [
                    {
                        "path": "src/app.py",
                        "operation": "update",
                        "old_text": "text that is not in the file",
                        "new_text": "whatever",
                    }
                ],
            }
        )
        llm = FakeLLM(db, script=[PLAN_JSON, bad_update])
        service = make_service(db, fake_gitlab, llm)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "changeset_invalid" in run.status_reason
        assert "0 time(s)" in run.status_reason


async def _only_run_id(db) -> str:
    async with db() as session:
        runs = (await session.execute(select(FlowRun))).scalars().all()
    assert len(runs) == 1
    return runs[0].id
