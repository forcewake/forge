"""R42-06 (#379) — budget calibration pinned by tests.

The recorded basis: the #364 live window raised the standard token cap
200k→600k after run ``fbe62ad5`` exhausted the 200k window with the
closing review stood down — ONE observed task shape, never proof every
task needs 600k. This file pins the four sides of the calibration:

- **The assessment** (scope 2): the NORMAL planning composition produces
  a ``budget_class`` — the planner prompt asks for it, the parsed plan
  keeps it, and the frozen harness selection records the request. No
  test-injected field: the scripted model answer is what the prompt asks
  for, nothing more.
- **The bounded profile resolution** (scope 3): the task-class → profile
  mapping is TOTAL with the selection REASON recorded — a valid
  assessment, an absent one and a malformed one are three distinct
  bounded cases, never an exception path; a suggested expensive route
  can never bypass the approved cap (the trivial-task-cannot-self-expand
  arm).
- **The closing reserve on the REAL guard** (scope 4): the measured
  ``fbe62ad5`` receipt shape exhausts an unpartitioned 200k window and
  refuses the reviewer (the recorded fence), while the same shape under
  ``closing-partition/1`` fences the CODER at the share boundary and the
  reviewer still completes within its reserved share — both phases
  enforced by the actual ``reserve`` admission path.
- **The measured artifact** (scope 5): the committed calibration report
  carries the real captures' numbers (usage coverage, the bounded
  unreceipted liability, failed attempts named) and rebuilds
  byte-identically — the measurement exists BEFORE any default changed.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import FlowRun
from forge.durable.budgets import (
    resolve_budget_limits,
    resolve_budget_profile,
)
from forge.factory.planner import LLMPlanner
from forge.models.base import Base
from forge.runs import RunService
from forge.runs.harness_selection import (
    DEFAULT_BUDGET_CLASS_REASON,
    compile_harness_selection,
)
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.fixtures.fake_llm import FakeLLM

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "evaluation" / "economics" / "budget-calibration-v1.json"

#: The recorded live-lab profile (the receipted 200k→600k amendment's
#: result) and the runbook default — both from the calibration report.
RECORDED_LAB_PROFILES = {
    "trivial": {"max_calls": 8, "max_tokens": 40000, "wallclock_s": 900},
    "standard": {"max_calls": 40, "max_tokens": 600000, "wallclock_s": 3600},
    "heavy": {"max_calls": 120, "max_tokens": 600000, "wallclock_s": 10800},
}

#: What a model answering TODAY'S planner prompt returns for a small
#: task — the normal composition's own shape, not an injected selection
#: field (the assessment rides the SAME last_plan the proposal reads).
TRIVIAL_PLAN_JSON = json.dumps(
    {
        "summary": "One-line docs fix.",
        "steps": ["fix the typo"],
        "risks": [],
        "files_hint": ["README.md"],
        "budget_class": "trivial",
        "budget_reason": "single-file docs change",
    }
)

HEAVY_PLAN_JSON = json.dumps(
    {
        "summary": "Rework the parser across modules.",
        "steps": ["a", "b"],
        "risks": ["regressions"],
        "files_hint": ["src/"],
        "budget_class": "heavy",
        "budget_reason": "multi-file risky change",
    }
)

MALFORMED_PLAN_JSON = json.dumps(
    {
        "summary": "Mystery task.",
        "steps": ["do it"],
        "risks": [],
        "files_hint": [],
        "budget_class": "colossal",
        "budget_reason": "the model invented a class",
    }
)


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_IMPLEMENTER_BACKEND="builtin",  # pinned: the dev .env sets ci_harness
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
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


async def start_with_planner(db, fake_gitlab, *, script, settings) -> tuple[RunService, str]:
    """The NORMAL planning composition: the real LLMPlanner over the
    scripted model answer, the real RunService freezing the selection."""
    llm = FakeLLM(session_factory=db, script=script)
    service = RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings,
        planner=LLMPlanner(llm, settings=settings),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    return service, run_id


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


# ----------------------------------------------------------------------
# Scope 2 — the structured assessment the normal composition produces
# ----------------------------------------------------------------------


class TestPlannerAssessment:
    def test_the_prompt_asks_for_the_budget_class(self):
        from forge.factory.planner import _SYSTEM_PROMPT

        assert '"budget_class": "<trivial|standard|heavy>"' in _SYSTEM_PROMPT
        # The request-not-grant rule rides the prompt itself.
        assert "never grant yourself more budget" in _SYSTEM_PROMPT
        assert '"budget_reason"' in _SYSTEM_PROMPT

    async def test_the_normal_composition_produces_the_assessment(self, db, fake_gitlab):
        """No test-injected field: the planner answers its own prompt and
        the assessment flows through last_plan into the frozen selection
        with its ceilings resolved from the OPERATOR's profiles."""
        settings = make_settings(FORGE_BUDGET_PROFILES=json.dumps(RECORDED_LAB_PROFILES))
        service, run_id = await start_with_planner(
            db, fake_gitlab, script=[TRIVIAL_PLAN_JSON], settings=settings
        )

        run = await get_run(db, run_id)
        selection = run.evidence["harness_selection"]
        # The planner's REQUEST was honored — class trivial, reason named.
        assert selection["budget_class"] == "trivial"
        assert selection["budget_class_reason"] == "planner assessment: trivial"
        # The gate sees the assessment's class and its configured profile.
        assert selection["budget_ceilings"] == {
            "max_calls": 8,
            "max_tokens": 40000,
            "wallclock_s": 900,
        }
        # The ENFORCED budget row opened BEFORE the first paid call (R13 —
        # the planner itself reserves) at the DEFAULT class's numbers, and
        # the assessment never moves an opened row: a request, never a
        # grant. The spec's budgets block follows the selection's resolved
        # ceilings (C02) — the gate approves 40k while the durable row
        # keeps the pre-plan 600k it opened at; the guard enforces the
        # ROW, so the divergence can only ever be SAFE (an escalation
        # REQUEST cannot raise the opened ceiling either). The GitHub
        # service additionally pins the selection's displayed ceilings to
        # the opened row (B11); the GitLab path's display pin is a named
        # gap owed in runs/service.py — a file this issue does not touch.
        assert run.evidence["budget"]["budget_class"] == "trivial"
        assert run.evidence["budget"]["max_tokens"] == 600000
        from sqlalchemy import select

        from forge.durable import RunBudget, RunSpec

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            row = (
                (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document["budgets"]["max_tokens"] == 40000  # the gate's approval
        assert row.max_tokens == 600000  # the enforced row — never moved

    async def test_the_stub_path_records_the_absent_case(self, db, fake_gitlab):
        """The stub planner carries no assessment — the honest absent case
        on the frozen evidence, never an error."""
        settings = make_settings(FORGE_BUDGET_PROFILES=json.dumps(RECORDED_LAB_PROFILES))
        service = RunService(
            session_factory=db,
            gitlab=fake_gitlab,
            settings=settings,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubReviewer(),
        )
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        run = await get_run(db, run_id)
        selection = run.evidence["harness_selection"]
        assert selection["budget_class"] == "standard"
        assert selection["budget_class_reason"] == "default: no assessment"

    async def test_the_assessment_may_request_escalation_within_policy(self, db, fake_gitlab):
        """A big task asking heavy resolves the CONFIGURED heavy profile —
        the class names a profile, the numbers stay the operator's."""
        settings = make_settings(FORGE_BUDGET_PROFILES=json.dumps(RECORDED_LAB_PROFILES))
        _, run_id = await start_with_planner(
            db, fake_gitlab, script=[HEAVY_PLAN_JSON], settings=settings
        )
        run = await get_run(db, run_id)
        selection = run.evidence["harness_selection"]
        assert selection["budget_class"] == "heavy"
        assert selection["budget_class_reason"] == "planner assessment: heavy"
        assert selection["budget_ceilings"]["max_tokens"] == 600000

    def test_the_lenient_reader(self):
        llm = FakeLLM(script=[])
        planner = LLMPlanner(llm)
        planner.last_plan = {"budget_class": "trivial"}
        assert planner.budget_assessment() == "trivial"
        planner.last_plan = {"budget_class": "  standard "}
        assert planner.budget_assessment() == "standard"
        for malformed in (None, "", "colossal", 42, {"nested": True}, ["trivial"]):
            planner.last_plan = {"budget_class": malformed}
            assert planner.budget_assessment() == ""
        planner.last_plan = None
        assert planner.budget_assessment() == ""


# ----------------------------------------------------------------------
# Scope 3 — one profile, bounded cases, the reason recorded
# ----------------------------------------------------------------------


class TestBoundedSelectionReason:
    LANES = {"claude-code", "grok-build", "opencode"}

    def test_absent_assessment_is_the_default_bounded_case(self):
        selection = compile_harness_selection(
            ["claude-code"], "ci_harness:claude-code", self.LANES, None
        )
        assert selection.budget_class == "standard"
        assert selection.budget_class_reason == DEFAULT_BUDGET_CLASS_REASON
        assert selection.budget_class_reason == "default: no assessment"

    def test_valid_assessment_is_named(self):
        selection = compile_harness_selection(
            ["claude-code"], "ci_harness:claude-code", self.LANES, {"budget_class": "heavy"}
        )
        assert selection.budget_class == "heavy"
        assert selection.budget_class_reason == "planner assessment: heavy"

    def test_malformed_assessment_is_a_distinct_bounded_case(self):
        """A present-but-garbage assessment NEVER raises: the mapping is
        total, the default class applies and the REASON names the rejected
        value (truncated, sanitized)."""
        for malformed in ("colossal", "", 42, None, ["heavy"], {"a": 1}):
            selection = compile_harness_selection(
                ["claude-code"], "ci_harness:claude-code", self.LANES, {"budget_class": malformed}
            )
            assert selection.budget_class == "standard"
            assert selection.budget_class_reason.startswith("default: malformed assessment")
            assert "budget_class=" in selection.budget_class_reason
            assert "not in trivial|standard|heavy" in selection.budget_class_reason

    def test_the_reason_round_trips_through_the_spec_document(self):
        frozen = compile_harness_selection(
            ["claude-code"], "ci_harness:claude-code", self.LANES, {"budget_class": "trivial"}
        ).as_document()
        assert frozen["budget_class_reason"] == "planner assessment: trivial"
        from forge.runs.harness_selection import selection_from_spec_document

        reborn = selection_from_spec_document({"backend_config": frozen})
        assert reborn is not None and reborn.budget_class_reason == "planner assessment: trivial"
        # Pre-R42 documents (no reason key) read as the default case.
        legacy = dict(frozen)
        legacy.pop("budget_class_reason")
        older = selection_from_spec_document({"backend_config": legacy})
        assert older is not None and older.budget_class_reason == DEFAULT_BUDGET_CLASS_REASON


class TestProfileResolutionReason:
    def test_configured_profile_is_named(self):
        resolution = resolve_budget_profile(RECORDED_LAB_PROFILES, "heavy")
        assert resolution.profile_name == "heavy"
        assert resolution.limits is not None and resolution.limits.max_tokens == 600000
        assert resolution.reason == "configured profile: heavy"
        assert resolution.to_json()["profile_selection_reason"] == resolution.reason

    def test_unknown_class_is_the_bounded_fallback_never_unlimited(self):
        resolution = resolve_budget_profile(RECORDED_LAB_PROFILES, "galactic")
        assert resolution.budget_class == "galactic"
        assert resolution.profile_name == "standard"
        assert resolution.limits is not None and resolution.limits.max_tokens == 600000
        assert "bounded fallback" in resolution.reason

    def test_nothing_configured_is_honestly_unlimited(self):
        resolution = resolve_budget_profile({}, "standard")
        assert resolution.limits is None
        assert resolution.reason == "no budget profiles configured (unlimited)"

    def test_all_unset_profile_is_honestly_unlimited(self):
        resolution = resolve_budget_profile({"standard": {}}, "standard")
        assert resolution.limits is None
        assert resolution.reason == "profile 'standard' carries no limited axis (unlimited)"

    def test_agreement_with_the_legacy_resolver_on_the_shared_cases(self):
        for budget_class in ("trivial", "standard", "heavy", "galactic"):
            legacy = resolve_budget_limits(RECORDED_LAB_PROFILES, budget_class)
            resolved = resolve_budget_profile(RECORDED_LAB_PROFILES, budget_class).limits
            assert legacy == resolved


class TestTrivialTaskCannotSelfExpand:
    """The acceptance arm: a suggested expensive route can never bypass
    the approved cap — the highest CONFIGURED ceiling is the ceiling."""

    ESCALATION_PROFILES = {
        "trivial": {"max_calls": 8, "max_tokens": 40000, "wallclock_s": 900},
        "standard": {"max_calls": 40, "max_tokens": 200000, "wallclock_s": 3600},
        # NOTE: no heavy profile is configured on this deployment.
    }

    def test_a_trivial_task_asking_heavy_lands_on_the_bounded_fallback(self):
        selection = compile_harness_selection(
            ["claude-code"],
            "ci_harness:claude-code",
            {"claude-code"},
            {"budget_class": "heavy"},  # the self-escalation attempt
        )
        assert selection.budget_class == "heavy"  # the class name is valid…
        resolution = resolve_budget_profile(self.ESCALATION_PROFILES, selection.budget_class)
        # …but the NUMBERS are the bounded fallback — never unlimited,
        # never above the highest configured ceiling (200k here).
        assert resolution.limits is not None
        assert resolution.limits.max_tokens == 200000
        assert resolution.profile_name == "standard"
        assert "bounded fallback" in resolution.reason

    def test_no_class_ever_resolves_above_the_configured_maximum(self):
        ceiling = max(
            (profile.get("max_tokens") or 0) for profile in self.ESCALATION_PROFILES.values()
        )
        for budget_class in ("trivial", "standard", "heavy", "colossal", ""):
            limits = resolve_budget_profile(self.ESCALATION_PROFILES, budget_class).limits
            assert limits is None or limits.max_tokens <= ceiling

    async def test_a_changed_default_never_alters_an_approved_run(self, db):
        """The frozen row is idempotent: re-opening the SAME run with the
        amended 600k profile returns the original 200k ceilings — moving a
        live run takes the recorded #340 amendment, not a config change."""
        from forge.durable.budgets import open_budget

        async with db() as session:
            session.add(FlowRun(id="run-freeze", project_id=1))
            await session.commit()
        async with db() as session:
            budget = await open_budget(
                session, run_id="run-freeze", max_tokens=200000, max_calls=40
            )
            await session.commit()
        # The deployment amends its default posture 200k→600k…
        async with db() as session:
            reopened = await open_budget(
                session, run_id="run-freeze", max_tokens=600000, max_calls=40
            )
            await session.commit()
        # …the approved run never moved: the frozen ceilings stand.
        assert reopened.id == budget.id
        assert reopened.max_tokens == 200000


# ----------------------------------------------------------------------
# Scope 4 — the closing reserve verified on the REAL guard
# ----------------------------------------------------------------------


#: The measured fbe62ad5 receipt shape (#364): the SDK reported DISJOINT
#: counters whose one-fold total is 194,789 tokens; 1,523 further gateway
#: tokens landed on the same window (196,312/200,000 consumed) and the
#: reviewer's 4,096-token hold no longer fit — the closing review stood
#: down. THAT is the recorded basis; the tests below replay it against
#: the actual admission path.
MEASURED_SDK_TOKENS = 194_789
MEASURED_GATEWAY_TOKENS = 1_523
MEASURED_CONSUMED = 196_312
REVIEWER_HOLD_TOKENS = 4_096
WINDOW_TOKENS = 200_000
PARTITION_SHARE_TOKENS = 30_000  # closing-partition/1: ceil(200000 x 0.15)


class TestClosingReserveVerification:
    async def _seed_run(self, db) -> None:
        async with db() as session:
            session.add(FlowRun(id="run-reserve", project_id=1))
            await session.commit()

    async def _budget(self, db):
        from forge.durable.budgets import budget_for_run

        async with db() as session:
            budget = await budget_for_run(session, "run-reserve")
            assert budget is not None
            session.expunge(budget)
            return budget

    async def test_the_recorded_fence_unpartitioned_window_stood_the_review_down(self, db):
        """The negative control, exactly as recorded: no closing share on
        the 200k window → the measured coder consumption leaves the
        reviewer's hold no room and the guard refuses it — durably."""
        from forge.durable import RESERVE_CLOSING
        from forge.durable.budgets import open_budget, reconcile_actual, reserve

        await self._seed_run(db)
        async with db() as session:
            await open_budget(session, run_id="run-reserve", max_tokens=WINDOW_TOKENS)
            await session.commit()

        # The lane receipt reconciles (one opaque implementation hold).
        async with db() as session:
            budget = await self._budget(db)
            lane = await reserve(session, budget, calls=1, tokens=MEASURED_SDK_TOKENS)
            assert lane is not None
            await reconcile_actual(session, lane, actual_calls=1, actual_tokens=MEASURED_SDK_TOKENS)
            await session.commit()

        # The gateway planner/reviewer turns land on the same axis.
        async with db() as session:
            budget = await self._budget(db)
            gateway = await reserve(session, budget, calls=1, tokens=MEASURED_GATEWAY_TOKENS)
            assert gateway is not None
            await reconcile_actual(
                session, gateway, actual_calls=1, actual_tokens=MEASURED_GATEWAY_TOKENS
            )
            await session.commit()

        row = await self._budget(db)
        assert row.consumed_tokens == MEASURED_CONSUMED  # the recorded 196,312

        # The closing review's 4,096-token hold no longer fits — the
        # recorded stood-down review, on the real admission path. The
        # refusal is durable (a full-limit refusal exhausts the budget).
        async with db() as session:
            refused = await reserve(
                session,
                row,
                calls=1,
                tokens=REVIEWER_HOLD_TOKENS,
                purpose=RESERVE_CLOSING,
            )
            assert refused is None
            await session.commit()
        assert (await self._budget(db)).status == "exhausted"

    async def test_the_partitioned_window_enforces_both_phases(self, db):
        """closing-partition/1 with the measured window: the CODER cannot
        enter the 30k share (its ceiling is 170k), and when coding hits
        that ceiling the REVIEWER still completes within its reserve."""
        from forge.durable import AXIS_TOKENS, RESERVE_CLOSING, RESERVE_IMPLEMENTATION
        from forge.durable.budgets import (
            limiting_axis,
            open_budget,
            reconcile_actual,
            reserve,
        )

        await self._seed_run(db)
        async with db() as session:
            await open_budget(
                session,
                run_id="run-reserve",
                max_tokens=WINDOW_TOKENS,
                closing_reserved_tokens=PARTITION_SHARE_TOKENS,
                closing_partition_policy="closing-partition/1",
            )
            await session.commit()

        budget = await self._budget(db)
        assert budget.closing_reserved_tokens == PARTITION_SHARE_TOKENS

        # The measured 194,789-token receipt is refused AT ADMISSION — the
        # coder's ceiling is limit - share = 170,000 — and a share-bounded
        # refusal does NOT exhaust the budget the review still needs.
        async with db() as session:
            assert await reserve(session, budget, calls=1, tokens=MEASURED_SDK_TOKENS) is None
            await session.rollback()
        assert (await self._budget(db)).status == "open"

        # Coding hits its OWN ceiling: the coder fills 170,000 exactly…
        async with db() as session:
            top = await reserve(
                session,
                budget,
                calls=1,
                tokens=WINDOW_TOKENS - PARTITION_SHARE_TOKENS,
                purpose=RESERVE_IMPLEMENTATION,
            )
            assert top is not None
            await reconcile_actual(
                session,
                top,
                actual_calls=1,
                actual_tokens=WINDOW_TOKENS - PARTITION_SHARE_TOKENS,
            )
            await session.commit()

        # …one more implementation token would ENTER the share: refused at
        # the TOKEN axis (never the call axis), and still not exhausted.
        async with db() as session:
            assert (
                await limiting_axis(
                    session, "run-reserve", calls=1, tokens=1, purpose=RESERVE_IMPLEMENTATION
                )
                == AXIS_TOKENS
            )
            budget = await self._budget(db)
            assert await reserve(session, budget, calls=1, tokens=1) is None
            await session.rollback()
        assert (await self._budget(db)).status == "open"

        # The reviewer completes within its reserved share: the closing
        # purpose sees the FULL limit, where the intact 30k share is
        # exactly what covers the 4,096-token hold.
        async with db() as session:
            budget = await self._budget(db)
            review = await reserve(
                session,
                budget,
                calls=1,
                tokens=REVIEWER_HOLD_TOKENS,
                purpose=RESERVE_CLOSING,
            )
            assert review is not None
            await reconcile_actual(
                session, review, actual_calls=1, actual_tokens=REVIEWER_HOLD_TOKENS
            )
            await session.commit()

        # closing.reserve_remaining: 30,000 - 4,096 spent, and the run's
        # budget is still open — the review was never the casualty.
        row = await self._budget(db)
        assert row.status == "open"
        assert row.consumed_tokens == (
            WINDOW_TOKENS - PARTITION_SHARE_TOKENS + REVIEWER_HOLD_TOKENS
        )
        # A SECOND review-sized hold also fits (round-2 corrections are
        # the live norm) — the share sized by the observed workloads.
        async with db() as session:
            budget = await self._budget(db)
            assert (
                await reserve(
                    session,
                    budget,
                    calls=1,
                    tokens=REVIEWER_HOLD_TOKENS,
                    purpose=RESERVE_CLOSING,
                )
                is not None
            )
            await session.rollback()

    async def test_the_calls_axis_partition_holds_too(self, db):
        """The live rounds 2/3 froze 6 of 40 calls reserved: the
        implementation purpose tops out at 34, the closing purpose can
        spend the 35th–40th."""
        from forge.durable import RESERVE_CLOSING
        from forge.durable.budgets import open_budget, reserve

        await self._seed_run(db)
        async with db() as session:
            await open_budget(
                session,
                run_id="run-reserve",
                max_calls=40,
                closing_reserved_calls=6,
                closing_partition_policy="closing-partition/1",
            )
            await session.commit()
        budget = await self._budget(db)
        async with db() as session:
            for _ in range(34):
                assert await reserve(session, budget, calls=1, tokens=0) is not None
            assert await reserve(session, budget, calls=1, tokens=0) is None
            for _ in range(6):
                assert (
                    await reserve(session, budget, calls=1, tokens=0, purpose=RESERVE_CLOSING)
                    is not None
                )
            await session.rollback()


# ----------------------------------------------------------------------
# Scope 5 — the measured artifact (the measurement BEFORE any default)
# ----------------------------------------------------------------------


class TestCalibrationArtifact:
    def test_the_artifact_exists_and_carries_the_real_numbers(self):
        document = json.loads(ARTIFACT.read_text())
        assert document["schema"] == "forge.budget.calibration/1"
        measured = document["measured"]
        # The nine SDK receipts across the two captures.
        assert measured["receipt_count"] == 9
        # Usage coverage: 7/8 receipted with the bounded residual named —
        # never zero-filled, never a cost column.
        assert measured["usage_coverage"]["coverage"] == 0.875
        assert measured["usage_coverage"]["unresolved_liability_usd"] == 0.223636
        assert measured["usage_coverage"]["unresolved_liability_attributed_to"] == [
            "6e0fdf3448ee4860b2bc34822db9489f"
        ]
        # The redundant-read fraction: the context, not the task, is what
        # the token axis measures (0.756–0.900 per receipt).
        assert measured["context_expansion"]["redundant_read_fraction_overall"] == 0.846517
        for row in measured["receipts"]:
            assert 0.7 <= row["redundant_read_fraction"] <= 0.95
        # Failed attempts and human corrections are NAMED with spend.
        kinds = {row["kind"] for row in measured["failed_attempts_and_repairs"]}
        assert kinds == {"failed_attempt"}
        fenced = [
            row
            for row in measured["failed_attempts_and_repairs"]
            if row["run_id"].startswith("fbe62ad5")
        ]
        assert len(fenced) == 1 and fenced[0]["cost_usd"] == 0.222881
        corrections = measured["human_corrections"]
        assert {row["round"] for row in corrections} == {2, 3, 4}
        assert any(row["incremental_cost_usd"] == 0.198005 for row in corrections)
        # All-attempt cost rides beside the pending human decision.
        accepted = measured["accepted_task_all_attempt_cost"]
        assert accepted["provider_reported_usd_exact"] == 1.256064
        assert accepted["human_decision"] == "pending (draft MR !4)"
        # The exhaustion event decomposes exactly (cache added ONCE).
        event = document["exhaustion_event"]
        assert (
            event["sdk_receipt_tokens"] + event["gateway_turn_tokens"] == event["consumed_tokens"]
        )
        assert event["resolution_amendment"] == "forge.budget-profiles/1@2026-09-27-align"

    def test_the_600k_stays_a_recorded_profile_not_a_default(self):
        document = json.loads(ARTIFACT.read_text())
        profiles = document["recorded_profiles"]
        assert set(profiles) == {
            "runbook-default@forge-0.42.0",
            "forge.budget-profiles/1@2026-09-27-align",
        }
        # The runbook default keeps standard@200k; the 600k posture names
        # its origin (the live exhaustion event) — one task shape, not a
        # universal requirement.
        assert profiles["runbook-default@forge-0.42.0"]["standard"]["max_tokens"] == 200000
        amended = profiles["forge.budget-profiles/1@2026-09-27-align"]
        assert amended["standard"]["max_tokens"] == 600000
        assert "fbe62ad5" in amended["origin"]

    def test_cache_never_double_added_in_the_inclusive_fold(self):
        document = json.loads(ARTIFACT.read_text())
        for row in document["measured"]["receipts"]:
            # Anthropic-shaped DISJOINT: inclusive = input + cached (+ cache
            # write), the cache counted exactly ONCE.
            assert row["input_tokens_inclusive"] == row["input_tokens"] + row["cached_input_tokens"]
            if row["output_tokens"] is not None:
                assert row["total_known_tokens"] == (
                    row["input_tokens_inclusive"] + row["output_tokens"]
                )

    def test_the_artifact_rebuilds_byte_identically(self):
        """The report is a MEASUREMENT of the recorded captures — the
        committed bytes and a fresh fold over the same inputs agree."""
        from scripts.build_budget_calibration import build_document

        rebuilt = json.dumps(build_document(), indent=2, sort_keys=True) + "\n"
        assert rebuilt == ARTIFACT.read_text()
