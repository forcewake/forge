"""Evidence policy tests (F23): the redaction canary at the durable boundary.

Unit coverage for :class:`forge.policy.evidence.EvidencePolicy` plus one
end-to-end canary: a failing repair run whose CI log carries a glpat-shaped
secret never lets the secret into the repair brief or the MR note.
"""

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import FlowRun, FlowStatus
from forge.factory.implementer import LLMImplementer
from forge.factory.planner import LLMPlanner
from forge.factory.reviewer import LLMReviewer
from forge.models.base import Base
from forge.policy.evidence import REDACTED_PLACEHOLDER, EvidencePolicy
from forge.runs import RunService
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
        "steps": ["bump x in src/app.py"],
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


def branch_for(issue_iid: int, run_id: str) -> str:
    from forge.runs.stubs import factory_branch

    return factory_branch(issue_iid, run_id)


# ----------------------------------------------------------------------
# Unit: redact / apply_policy
# ----------------------------------------------------------------------


class TestRedaction:
    def test_canary_secret_in_ci_log_shape_is_redacted(self):
        log = (
            "$ pytest -q\n"
            "E AssertionError: widget count 0 != 1\n"
            "export DEPLOY_TOKEN=glpat-CANARY-SHORT\n"
            "$ forge publish --token ghs_CANARY-SHORT\n"
        )
        redacted, changed = EvidencePolicy().apply_policy(log)

        assert changed is True
        assert "glpat-" not in redacted
        assert "ghs_" not in redacted
        assert "9xQeWvG816bUx9EPROMj" not in redacted  # suffix-only, scanner-safe
        assert redacted.count(REDACTED_PLACEHOLDER) == 2
        # The surrounding CI-log text survives untouched.
        assert "E AssertionError: widget count 0 != 1" in redacted

    def test_pat_shaped_values_are_redacted(self):
        text = "sk-CANARY and xai-CANARY and glpat-CANARY"
        redacted = EvidencePolicy().redact(text)

        assert (
            redacted
            == f"{REDACTED_PLACEHOLDER} and {REDACTED_PLACEHOLDER} and {REDACTED_PLACEHOLDER}"
        )

    def test_legit_code_text_is_untouched(self):
        code = "def deploy():\n    return x + 1  # bump the constant\n"
        redacted, changed = EvidencePolicy().apply_policy(code)

        assert redacted == code
        assert changed is False

    def test_redaction_is_idempotent(self):
        once = EvidencePolicy().redact("token=glpat-CANARY-SHORT")
        assert EvidencePolicy().redact(once) == once

    def test_oversize_evidence_is_truncated_at_the_cap(self):
        policy = EvidencePolicy(deny_patterns=(), max_chars=100)
        redacted, changed = policy.apply_policy("a" * 500)

        assert len(redacted) == 100
        assert changed is False

    def test_settings_shape_the_policy(self):
        policy = EvidencePolicy.from_settings(
            make_settings(FORGE_EVIDENCE_DENY_PATTERNS="supersecret-", FORGE_EVIDENCE_MAX_CHARS=20)
        )

        redacted, _ = policy.apply_policy("supersecret-value-and-more-junk")
        assert redacted == REDACTED_PLACEHOLDER


# ----------------------------------------------------------------------
# End-to-end: a failing repair run never leaks the CI-log canary
# ----------------------------------------------------------------------


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
    return fake


def make_service(db, fake_gitlab, llm, settings=None) -> RunService:
    settings = settings or make_settings()
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings,
        planner=LLMPlanner(llm, settings=settings),
        implementer=LLMImplementer(llm, gitlab=fake_gitlab, settings=settings),
        reviewer=LLMReviewer(llm, gitlab=fake_gitlab, settings=settings),
    )


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


def seed_failed_pipeline(fake_gitlab: FakeGitLab, branch: str, sha: str, log: str) -> None:
    pipeline_id = fake_gitlab._id()
    fake_gitlab.pipelines.append({"id": pipeline_id, "ref": branch, "status": "failed", "sha": sha})
    fake_gitlab.set_pipeline_jobs(
        pipeline_id,
        [
            {
                "id": 55,
                "name": "pytest",
                "status": "failed",
                "failure_reason": "script_failure",
            }
        ],
    )
    fake_gitlab.set_job_log(55, log)


class TestRepairContextCanary:
    async def test_failing_repair_run_stores_no_canary_secret(self, db, fake_gitlab):
        llm = FakeLLM(db, script=[PLAN_JSON, CREATE_JSON, REPAIR_JSON])
        service = make_service(db, fake_gitlab, llm)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]
        seed_failed_pipeline(
            fake_gitlab,
            branch_for(ISSUE_IID, run_id),
            first_sha,
            "E AssertionError: widget count 0 != 1\n"
            "export DEPLOY_TOKEN=glpat-CANARY-SHORT\n",
        )

        await service.evaluate_waiting_ci()

        # The repair cycle ran with the failure context — redacted.
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.commit_cycle == 2
        repair_prompt = llm.calls_for("implementer")[1]["user"]
        assert "AssertionError" in repair_prompt  # the diagnostics still flow
        assert "glpat-" not in repair_prompt
        assert "9xQeWvG816bUx9EPROMj" not in repair_prompt  # suffix-only
        assert REDACTED_PLACEHOLDER in repair_prompt

        # The MR repair note (the posted surface) is clean too.
        (note,) = fake_gitlab.mr_notes_containing("Repair cycle 2")
        assert "glpat-" not in note["body"]
