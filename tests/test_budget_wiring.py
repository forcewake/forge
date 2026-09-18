"""R13: budgets that actually enforce on the STANDARD /implement path.

Today's regression this file pins: the standard spec builder used to freeze
only lifecycle limits (``commit_cycles``/``harness_timeout``), so a normal
run opened NO enforceable budget. The chain under test:

    budget profile (config) → resolved AT FREEZE TIME into the spec's
    ``budgets`` block → RunBudget row opened BEFORE the first paid call →
    BudgetGuard bound per leg → reserve/reconcile on every LLM dispatch →
    episode/wall-clock gates on the harness lanes.

- a finite profile freezes numeric ceilings + honest ``enforcement`` into
  the spec and opens the run budget before the planner (builtin: "full");
- no profile keeps the byte-compatible unbudgeted shape (no row, no fields);
- an exhausted budget means ZERO further LLM dispatches (builtin propose
  refuses into blocked ``budget_exhausted``) and ZERO new harness episodes;
- a spent wall-clock deadline fires at the poll boundary without any
  provider success (and at reservation time on the builtin lane);
- repeated artifact receipts of the same episode never double-consume;
- harness lanes record ``enforcement="partial"`` evidence — never a token
  cap claimed but not enforced.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings, parse_budget_profiles
from forge.durable import FlowRun, FlowStatus, LLMCall, RunBudget, load_budget_guard
from forge.factory.implementer import LLMImplementer
from forge.factory.llm import LLMError
from forge.factory.planner import LLMPlanner
from forge.factory.reviewer import LLMReviewer
from forge.models.base import Base
from forge.runs import RunService
from forge.runs.candidate import HarnessUsage
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.fixtures.fake_llm import FakeLLM

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."

#: A finite profile bound to the default budget class.
PROFILE_JSON = json.dumps(
    {
        "standard": {"max_calls": 40, "max_tokens": 500000, "wallclock_s": 3600},
        "heavy": {"max_calls": 200},
    }
)

CREATE_DRAFT = json.dumps(
    {
        "branch": "model/chose/this",
        "commit_message": "model's own message",
        "changes": [
            {"path": "forge-demo/feature.md", "operation": "create", "content": "# feature\n"}
        ],
    }
)

REVIEW_OK_JSON = json.dumps({"verdict": "ok", "summary": "Clean change.", "findings": []})


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_HARNESS_TIMEOUT_SECONDS=1800,  # hermetic: dev .env sets 5400
        FORGE_IMPLEMENTER_BACKEND="builtin",  # pinned: the dev .env sets ci_harness
    )
    values.update(overrides)
    return Settings(**values)


class BudgetAwareFakeLLM(FakeLLM):
    """FakeLLM with the real client's budget contract (ADR-0018 §5).

    A refused reservation raises ``LLMError("budget_exhausted")`` BEFORE the
    dispatch is attempted or recorded; a granted hold reserves 1 call + the
    token estimate and is reconciled against the scripted actuals after the
    response — exactly what :class:`forge.factory.llm.LLMClient` does. The
    dispatch counter only moves for calls that actually happened.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._budget = None
        self.dispatches = 0

    def set_budget(self, budget) -> None:
        self._budget = budget

    async def complete(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        role: str,
        flow_run_id: str | None,
        json_mode: bool = False,
        max_tokens: int = 4096,
    ):
        if self._budget is not None:
            reservation = await self._budget.reserve(calls=1, tokens=max_tokens)
            if reservation is None:
                raise LLMError("budget_exhausted")
            try:
                result = await super().complete(
                    tier=tier,
                    system=system,
                    user=user,
                    role=role,
                    flow_run_id=flow_run_id,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                )
            except Exception:
                await self._budget.reconcile(reservation, actual_calls=1)
                raise
            await self._budget.reconcile(reservation, actual_calls=1, actual_tokens=15)
            self.dispatches += 1
            return result
        return await super().complete(
            tier=tier,
            system=system,
            user=user,
            role=role,
            flow_run_id=flow_run_id,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )


@pytest.fixture(autouse=True)
def _clear_project_config_cache():
    """The project-config cache is process-global (5-min TTL) — never leak
    a seeded `.forge.yml` scope between tests."""
    from forge.orchestrator.project_config import clear_cache

    clear_cache()
    yield
    clear_cache()


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
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def get_budget(db, run_id: str) -> RunBudget:
    async with db() as session:
        budget = (
            (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id)))
            .scalars()
            .one()
        )
        session.expunge(budget)
        return budget


async def get_spec_document(db, run_id: str) -> dict:
    from forge.durable import RunSpec

    async with db() as session:
        row = (
            (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
            .scalars()
            .one()
        )
        return dict(row.document)


async def rewind_budget_created_at(db, run_id: str, *, seconds: int) -> None:
    """Rewind the budget's creation — the durable wall-clock anchor."""
    async with db() as session:
        budget = (
            (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id)))
            .scalars()
            .one()
        )
        budget.created_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        await session.commit()


def real_agent_service(db, fake_gitlab, llm, settings) -> RunService:
    """RunService with the REAL factory agents over the budget-aware fake."""
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings,
        planner=LLMPlanner(llm, settings=settings),
        implementer=LLMImplementer(llm, gitlab=fake_gitlab, settings=settings),
        reviewer=LLMReviewer(llm, gitlab=fake_gitlab, settings=settings),
    )


def stub_agent_service(db, fake_gitlab, settings) -> RunService:
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )


class TestProfileFreezeChain:
    """profile → spec budgets block → RunBudget row, on the standard path."""

    async def test_finite_profile_freezes_spec_and_opens_budget_before_planning(
        self, db, fake_gitlab
    ):
        settings = make_settings(FORGE_BUDGET_PROFILES=PROFILE_JSON)
        llm = BudgetAwareFakeLLM(session_factory=db, script=[])
        service = real_agent_service(db, fake_gitlab, llm, settings)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        # The planner was the first paid call and it reserved against the
        # row opened BEFORE it (the profile's 40 calls are far from spent —
        # the run is open with exactly the frozen ceilings).
        assert llm.dispatches == 1
        budget = await get_budget(db, run_id)
        assert budget.status == "open"
        assert budget.max_calls == 40
        assert budget.max_tokens == 500000
        assert budget.wallclock_s == 3600
        assert budget.consumed_calls == 1  # the planner
        assert budget.reserved_calls == 0  # reconciled after the response

        # The ceilings were frozen INTO the spec at freeze time, with the
        # honest enforcement level of the builtin lane.
        document = await get_spec_document(db, run_id)
        assert document["budgets"] == {
            "commit_cycles": 3,
            "harness_timeout": 1800,
            "max_calls": 40,
            "max_tokens": 500000,
            "wallclock_s": 3600,
            "enforcement": "full",
        }
        # Provenance: the pre-plan row was backfilled with the spec digest.
        run = await get_run(db, run_id)
        assert budget.spec_digest == run.spec_digest

        # The honest enforcement record rides the run evidence.
        assert run.evidence["budget"] == {
            "budget_class": "standard",
            "enforcement": "full",
            "max_calls": 40,
            "max_tokens": 500000,
            "wallclock_s": 3600,
        }

    async def test_harness_lane_records_partial_enforcement(self, db, fake_gitlab):
        settings = make_settings(
            FORGE_BUDGET_PROFILES=PROFILE_JSON, FORGE_IMPLEMENTER_BACKEND="ci_harness"
        )
        service = stub_agent_service(db, fake_gitlab, settings)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        document = await get_spec_document(db, run_id)
        assert document["budgets"]["enforcement"] == "partial"
        assert document["budgets"]["max_calls"] == 40
        run = await get_run(db, run_id)
        assert run.evidence["budget"]["enforcement"] == "partial"
        # The row still opened — wall clock and episode gates need it.
        assert (await get_budget(db, run_id)).wallclock_s == 3600

    async def test_no_profile_keeps_the_unbudgeted_shape(self, db, fake_gitlab):
        """Byte-compat: without profiles no ceilings are frozen, no row is
        opened and no enforcement is claimed."""
        service = stub_agent_service(db, fake_gitlab, make_settings())

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        document = await get_spec_document(db, run_id)
        assert document["budgets"] == {"commit_cycles": 3, "harness_timeout": 1800}
        async with db() as session:
            unbudgeted = (
                (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id)))
                .scalars()
                .first()
            )
        assert unbudgeted is None
        assert "budget" not in (await get_run(db, run_id)).evidence

    async def test_post_gate_legs_reserve_against_the_frozen_budget(self, db, fake_gitlab):
        """/go advances on a FRESH service instance (the worker builds one
        per command) — the propose + review legs must still reserve, and the
        budget shows their actuals."""
        settings = make_settings(FORGE_BUDGET_PROFILES=PROFILE_JSON)
        llm = BudgetAwareFakeLLM(
            session_factory=db, script=["{}", CREATE_DRAFT, REVIEW_OK_JSON]
        )
        service = real_agent_service(db, fake_gitlab, llm, settings)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert llm.dispatches == 2  # planner + implementer; review awaits CI
        budget = await get_budget(db, run_id)
        assert budget.consumed_calls == 2
        assert budget.consumed_tokens == 30  # 15 per scripted call
        assert budget.reserved_calls == 0
        assert budget.status == "open"


class TestBuiltinExhaustion:
    async def test_exhausted_budget_stops_all_dispatches_and_blocks_budget_exhausted(
        self, db, fake_gitlab
    ):
        """max_calls=1: the planner spends the budget, /go's proposer is
        refused BEFORE any dispatch and the run classifies as blocked
        (budget_exhausted) — not a proposal failure."""
        settings = make_settings(
            FORGE_BUDGET_PROFILES=json.dumps({"standard": {"max_calls": 1}})
        )
        llm = BudgetAwareFakeLLM(session_factory=db, script=["{}"])
        service = real_agent_service(db, fake_gitlab, llm, settings)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        assert llm.dispatches == 1  # the planner spent the single call

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("budget_exhausted")
        # ZERO further dispatches: the proposer never ran, nothing committed.
        assert llm.dispatches == 1
        assert fake_gitlab.merge_requests == {}
        assert run.candidate_shas in (None, [])
        budget = await get_budget(db, run_id)
        assert budget.status == "exhausted"
        assert budget.consumed_calls == 1  # the refusal spent nothing


class TestHarnessGates:
    async def test_exhausted_budget_starts_no_harness_episode(self, db, fake_gitlab):
        """The harness lane's dispatch is its enforcement point: an exhausted
        budget blocks /go with no pipeline at all."""
        settings = make_settings(
            FORGE_BUDGET_PROFILES=json.dumps({"standard": {"max_calls": 1}}),
            FORGE_IMPLEMENTER_BACKEND="ci_harness",
        )
        service = stub_agent_service(db, fake_gitlab, settings)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        # Burn the single call outside any lane (the stubs make no calls).
        guard = await load_budget_guard(db, run_id)
        assert guard is not None
        hold = await guard.reserve(calls=1, tokens=0)
        assert hold is not None
        await guard.reconcile(hold, actual_calls=1)
        assert (await guard.refresh()) is not None
        assert (await guard.refresh()).status == "exhausted"

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("budget_exhausted")
        assert fake_gitlab.pipelines == []  # no episode was dispatched

    async def test_wallclock_deadline_fires_at_the_poll_without_provider_success(
        self, db, fake_gitlab
    ):
        """A green candidate artifact is seeded, but the budget's wall clock
        has run out: the poll boundary blocks the run WITHOUT reading the
        provider — the candidate is never adopted."""
        settings = make_settings(
            FORGE_BUDGET_PROFILES=json.dumps({"standard": {"wallclock_s": 3600}}),
            FORGE_IMPLEMENTER_BACKEND="ci_harness",
        )
        service = stub_agent_service(db, fake_gitlab, settings)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value
        pipeline_id = int((await get_run(db, run_id)).evidence["harness"]["pipeline_id"])

        # A finished episode waits behind the deadline — and loses.
        fake_gitlab.set_pipeline_jobs(
            pipeline_id, [{"id": 555, "name": "forge-agent", "status": "success"}]
        )
        provider_calls = len(fake_gitlab.calls)
        await rewind_budget_created_at(db, run_id, seconds=7200)

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("budget_exhausted")
        # No provider call was made (and nothing was adopted).
        assert len(fake_gitlab.calls) == provider_calls
        assert fake_gitlab.merge_requests == {}
        assert run.candidate_shas in (None, [])
        # The gate durably exhausted the budget — the stop is visible.
        assert (await get_budget(db, run_id)).status == "exhausted"

    async def test_within_the_wallclock_the_episode_still_runs(self, db, fake_gitlab):
        """Control: inside the budget the harness flow works unchanged."""
        from tests.fixtures.candidate import create_diff, seed_candidate

        settings = make_settings(
            FORGE_BUDGET_PROFILES=json.dumps({"standard": {"wallclock_s": 3600}}),
            FORGE_IMPLEMENTER_BACKEND="ci_harness",
        )
        service = stub_agent_service(db, fake_gitlab, settings)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        pipeline_id = int((await get_run(db, run_id)).evidence["harness"]["pipeline_id"])
        fake_gitlab.set_pipeline_jobs(
            pipeline_id, [{"id": 555, "name": "forge-agent", "status": "success"}]
        )
        seed_candidate(
            fake_gitlab,
            555,
            attempt_base="base-sha-1",
            diff=create_diff("forge-demo/x.md", "hello\n"),
            usage={"input_tokens": 21, "output_tokens": 7},
        )

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        budget = await get_budget(db, run_id)
        # The episode's receipt reconciled as one opaque call.
        assert budget.consumed_calls == 1
        assert budget.consumed_tokens == 28
        assert budget.status == "open"


class TestNoDoubleConsumption:
    async def test_repeated_artifact_receipts_count_once(self, db, fake_gitlab):
        """A repeated poll / crash-retry re-records the same episode's
        receipt: the ledger keeps every row, the budget consumes ONE call."""
        settings = make_settings(
            FORGE_BUDGET_PROFILES=json.dumps({"standard": {"max_calls": 5}}),
            FORGE_IMPLEMENTER_BACKEND="ci_harness",
        )
        service = stub_agent_service(db, fake_gitlab, settings)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        bundle = SimpleNamespace(
            usage=HarnessUsage(
                driver="claude-code", input_tokens=21, output_tokens=7, completeness="aggregate"
            )
        )
        await service._record_harness_usage(run_id, bundle)
        await service._record_harness_usage(run_id, bundle)  # the re-polled artifact
        await service._record_harness_usage(run_id, bundle)  # …and a crash retry

        budget = await get_budget(db, run_id)
        assert budget.consumed_calls == 1  # the episode, once
        assert budget.consumed_tokens == 28  # 21 + 7, once
        assert budget.status == "open"

        async with db() as session:
            rows = (
                (await session.execute(select(LLMCall).where(LLMCall.role == "implementer")))
                .scalars()
                .all()
            )
        assert len(rows) == 3  # the LEDGER records every receipt honestly


class TestProfileConfig:
    """The two config surfaces (env JSON, forge.yml) and the resolver."""

    def test_parse_budget_profiles_accepts_valid_json(self):
        profiles = parse_budget_profiles('{"heavy": {"max_calls": 10}, "tiny": {}}')
        assert profiles["heavy"] == {"max_calls": 10, "max_tokens": None, "wallclock_s": None}
        assert profiles["tiny"] == {"max_calls": None, "max_tokens": None, "wallclock_s": None}
        assert parse_budget_profiles("") == {}
        assert parse_budget_profiles(None) == {}

    def test_parse_budget_profiles_fails_closed_on_garbage(self):
        for raw in ("not json", "[1, 2]", '{"heavy": {"max_calls": "lots"}}',
                    '{"heavy": {"max_calls": -1}}', '{"": {}}'):
            with pytest.raises(ValueError):
                parse_budget_profiles(raw)

    def test_forge_config_yaml_profiles_win_over_env(self, tmp_path):
        config_file = tmp_path / "forge.yml"
        config_file.write_text(
            "forge:\n"
            "  budget_profiles:\n"
            "    heavy:\n"
            "      max_calls: 7\n",
            encoding="utf-8",
        )
        config = ForgeConfig(config_file)
        assert config.budget_profiles["heavy"]["max_calls"] == 7

        default = ForgeConfig(tmp_path / "nonexistent.yml")
        assert default.budget_profiles == {}

    def test_unknown_budget_class_degrades_to_standard_profile(self):
        from forge.durable.budgets import resolve_budget_limits

        profiles = parse_budget_profiles(PROFILE_JSON)
        heavy = resolve_budget_limits(profiles, "heavy")
        assert heavy is not None and heavy.max_calls == 200  # exact match
        fallback = resolve_budget_limits(profiles, "galactic")
        assert fallback is not None and fallback.max_calls == 40  # standard
        assert resolve_budget_limits({}, "standard") is None  # nothing configured
        assert resolve_budget_limits({"standard": {}}, "standard") is None  # all-unset
