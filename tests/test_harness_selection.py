"""ADR-0023: the pure harness-selection compiler + dispatch fallback step.

Rules 1–6 of the v0.9 brief §2, each pinned by its own test, plus the
shared ``advance_harness_fallback`` helper (§6): OFF by default,
infrastructure-only, pre-candidate-only, chain-exhaustion fails visibly.
No network, no DB — everything here is a pure function of its inputs.
"""

import pytest

from forge.runs.harness_selection import (
    BUDGET_CLASSES,
    DEFAULT_DRIVER,
    SHIPPED_DRIVERS,
    advance_harness_fallback,
    compile_harness_selection,
    current_driver,
    implementation_block,
    parse_preference,
    selection_from_spec_document,
    validate_preference,
)

LANES = {"claude-code", "grok-build", "opencode", "copilot"}


class TestImplementationBlock:
    """Brief §4: five fixed lines for the plan comment, gate-visible."""

    def test_five_fixed_lines_with_model_and_chain(self):
        selection = compile_harness_selection(
            ["claude-code", "grok-build", "opencode"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "grok-build", "budget_class": "trivial", "reason": "docs one-liner"},
        )
        block = implementation_block(selection, model="glm-5.3-flash[1m]", commit_cycles=3)
        assert block == (
            "## Implementation\n"
            "- Harness: **grok-build** · model glm-5.3-flash[1m]\n"
            "- Fallbacks: opencode\n"
            "- Budget class: trivial\n"
            "- Commit cycles: 3\n"
            "- Selection reason: docs one-liner\n"
        )

    def test_empty_fallbacks_read_none(self):
        selection = compile_harness_selection([], "ci_harness", LANES, None)
        block = implementation_block(selection, model="m", commit_cycles=3)
        assert "- Fallbacks: none\n" in block
        assert "- Selection reason: default\n" in block

    def test_block_starts_with_the_implementation_heading(self):
        selection = compile_harness_selection([], "ci_harness", LANES, None)
        assert implementation_block(selection, model="", commit_cycles=1).startswith(
            "## Implementation\n"
        )


class TestCompilerRules:
    def test_rule_1_lanes_cap_the_result(self):
        """A proposal can reorder, never extend: a lane the project did not
        onboard can never be selected, whatever the planner asks for."""
        selection = compile_harness_selection(
            ["claude-code", "grok-build"],
            "ci_harness:claude-code",
            available_lanes={"grok-build"},  # claude-code not onboarded
            planner_proposal={"harness": "claude-code", "reason": "wants claude"},
        )
        assert selection.harness == "grok-build"
        assert selection.fallbacks == ()

    def test_rule_2_empty_preference_is_the_backend_one_element_list(self):
        """Byte-compatible default: no preference ⇒ [current_backend], no
        fallbacks — the pre-ADR-0023 behavior, verbatim."""
        selection = compile_harness_selection([], "ci_harness:opencode", LANES, None)
        assert selection.harness == "opencode"
        assert selection.fallbacks == ()
        assert selection.budget_class == "standard"
        assert selection.reason == "default"

    def test_rule_2_bare_ci_harness_defaults_to_claude_code(self):
        selection = compile_harness_selection([], "ci_harness", LANES, None)
        assert selection.harness == DEFAULT_DRIVER

    def test_rule_3_non_onboarded_preference_entries_are_dropped(self):
        selection = compile_harness_selection(
            ["copilot", "claude-code", "grok-build"],
            "ci_harness:claude-code",
            available_lanes={"claude-code", "grok-build"},  # copilot not onboarded
            planner_proposal=None,
        )
        assert selection.harness == "claude-code"
        assert selection.fallbacks == ("grok-build",)  # the tail is ∩ available

    def test_rule_4_planner_proposal_honored_within_preference_and_lanes(self):
        selection = compile_harness_selection(
            ["claude-code", "grok-build"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "grok-build", "budget_class": "trivial", "reason": "docs one-liner"},
        )
        assert selection.harness == "grok-build"
        assert selection.fallbacks == ()
        assert selection.budget_class == "trivial"
        assert selection.reason == "docs one-liner"

    def test_rule_4_proposal_outside_preference_is_ignored(self):
        """The planner is never the authority: a harness outside the
        preference (∩ lanes) degrades to the chain head."""
        selection = compile_harness_selection(
            ["claude-code"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "copilot", "reason": "overreach"},
        )
        assert selection.harness == "claude-code"
        assert selection.reason == "default"  # not honored → planner reason too

    def test_rule_4_invalid_budget_class_falls_back_to_default(self):
        selection = compile_harness_selection(
            ["claude-code"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "claude-code", "budget_class": "galactic"},
        )
        assert selection.budget_class == "standard"

    def test_rule_4_missing_reason_defaults_to_planner_selection(self):
        selection = compile_harness_selection(
            ["claude-code", "opencode"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "opencode"},
        )
        assert selection.harness == "opencode"
        assert selection.reason == "planner selection"

    def test_rule_4_budget_class_applies_even_without_harness(self):
        """The budget-class clause is independent of the harness clause."""
        selection = compile_harness_selection(
            ["claude-code"], "ci_harness:claude-code", LANES, {"budget_class": "heavy"}
        )
        assert selection.harness == "claude-code"
        assert selection.budget_class == "heavy"
        assert selection.reason == "default"

    def test_rule_5_fallback_tail_frozen_even_when_switch_is_off(self):
        """The spec describes the chain; the runtime switch is a separate
        policy — the tail is recorded either way."""
        selection = compile_harness_selection(
            ["claude-code", "grok-build", "opencode"],
            "ci_harness:claude-code",
            LANES,
            None,
        )
        assert selection.fallbacks == ("grok-build", "opencode")

    def test_rule_5_proposal_selection_splits_the_tail(self):
        selection = compile_harness_selection(
            ["claude-code", "grok-build", "opencode"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "grok-build"},
        )
        assert selection.harness == "grok-build"
        assert selection.fallbacks == ("opencode",)

    def test_rule_6_deterministic_same_inputs_identical_output(self):
        kwds = {
            "preference": ["claude-code", "grok-build", "opencode"],
            "current_backend": "ci_harness:claude-code",
            "available_lanes": LANES,
            "planner_proposal": {"harness": "grok-build", "budget_class": "heavy"},
        }
        first = compile_harness_selection(**kwds)
        for _ in range(5):
            assert compile_harness_selection(**kwds) == first

    def test_malformed_planner_proposal_is_tolerated(self):
        """Parsing stays lenient: a non-dict proposal is no proposal at all."""
        selection = compile_harness_selection(
            ["claude-code"],
            "ci_harness:claude-code",
            LANES,
            "garbage",  # type: ignore[arg-type]
        )
        assert selection.harness == "claude-code"
        assert selection.reason == "default"


class TestBudgetClasses:
    def test_budget_class_set_is_the_briefs_triple(self):
        assert BUDGET_CLASSES == {"trivial", "standard", "heavy"}


class TestCurrentDriver:
    @pytest.mark.parametrize(
        ("backend", "driver"),
        [
            ("ci_harness:grok-build", "grok-build"),
            ("ci_harness", DEFAULT_DRIVER),
            ("builtin", DEFAULT_DRIVER),
            ("", DEFAULT_DRIVER),
            (None, DEFAULT_DRIVER),
        ],
    )
    def test_driver_resolution(self, backend, driver):
        assert current_driver(backend) == driver


class TestParsePreference:
    def test_splits_strips_and_deduplicates(self):
        assert parse_preference(" claude-code, grok-build ,claude-code,") == [
            "claude-code",
            "grok-build",
        ]

    def test_empty_is_empty(self):
        assert parse_preference("") == []
        assert parse_preference(None) == []


class TestValidatePreference:
    def test_unknown_driver_is_refused(self):
        with pytest.raises(ValueError, match="unknown harness driver"):
            validate_preference(["claude-code", "warp"], "claude-code")

    def test_list_must_include_the_configured_backend(self):
        with pytest.raises(ValueError, match="tightens, never deselects"):
            validate_preference(["claude-code"], "grok-build")

    def test_valid_list_passes(self):
        validate_preference(["claude-code", "grok-build"], "claude-code")

    def test_empty_list_is_always_valid(self):
        validate_preference([], "copilot")

    def test_driver_check_is_skipped_for_the_builtin_backend(self):
        """The builtin lane dispatches no harness — only the id set binds."""
        validate_preference(["grok-build"], None)

    def test_shipped_driver_set_is_the_four_templates(self):
        assert SHIPPED_DRIVERS == {"claude-code", "grok-build", "opencode", "copilot"}


class TestSelectionFromSpecDocument:
    def test_round_trips_the_backend_config_fragment(self):
        selection = compile_harness_selection(
            ["claude-code", "grok-build"], "ci_harness:claude-code", LANES, None
        )
        document = {"backend_config": {"backend": "ci_harness", **selection.as_document()}}
        assert selection_from_spec_document(document) == selection

    def test_pre_v2_document_has_no_selection(self):
        assert selection_from_spec_document({"backend_config": {"backend": "builtin"}}) is None
        assert selection_from_spec_document(None) is None
        assert selection_from_spec_document({}) is None


class TestAdvanceHarnessFallback:
    """Brief §6: OFF by default, infrastructure-only, pre-candidate-only."""

    def make_selection(self) -> object:
        return compile_harness_selection(
            ["claude-code", "grok-build", "opencode"], "ci_harness:claude-code", LANES, None
        )

    def test_off_by_default_never_advances(self):
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="claude-code",
                failure_kind="infrastructure",
                fallback_enabled=False,
                candidate_exists=False,
            )
            is None
        )

    def test_infrastructure_failure_advances_to_the_next_entry(self):
        selection = self.make_selection()
        nxt = advance_harness_fallback(
            selection,  # type: ignore[arg-type]
            failed_driver="claude-code",
            failure_kind="infrastructure",
            fallback_enabled=True,
            candidate_exists=False,
        )
        assert nxt is not None
        assert nxt.harness == "grok-build"
        assert nxt.fallbacks == ("opencode",)
        assert nxt.budget_class == selection.budget_class  # type: ignore[attr-defined]
        assert "claude-code" in nxt.reason and "infrastructure" in nxt.reason

    def test_code_failure_never_switches(self):
        """A CI-code failure is a signal about the CHANGE (ADR-0008)."""
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="claude-code",
                failure_kind="code",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )

    def test_config_failure_never_switches(self):
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="claude-code",
                failure_kind="config",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )

    def test_never_switches_once_a_candidate_exists(self):
        """One frozen attempt base → one candidate → one producer (ADR-0016)."""
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="claude-code",
                failure_kind="infrastructure",
                fallback_enabled=True,
                candidate_exists=True,
            )
            is None
        )

    def test_stale_event_for_a_different_driver_is_ignored(self):
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="opencode",
                failure_kind="infrastructure",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )

    def test_chain_exhaustion_returns_none(self):
        """The last entry failing means fail visibly — wait for a human."""
        selection = compile_harness_selection(
            ["claude-code"], "ci_harness:claude-code", LANES, None
        )
        assert selection.fallbacks == ()
        assert (
            advance_harness_fallback(
                selection,
                failed_driver="claude-code",
                failure_kind="infrastructure",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )

    def test_chain_walks_to_exhaustion(self):
        selection = self.make_selection()
        second = advance_harness_fallback(
            selection,  # type: ignore[arg-type]
            failed_driver="claude-code",
            failure_kind="infrastructure",
            fallback_enabled=True,
            candidate_exists=False,
        )
        assert second is not None and second.harness == "grok-build"
        third = advance_harness_fallback(
            second,
            failed_driver="grok-build",
            failure_kind="infrastructure",
            fallback_enabled=True,
            candidate_exists=False,
        )
        assert third is not None and third.harness == "opencode"
        assert third.fallbacks == ()
        assert (
            advance_harness_fallback(
                third,
                failed_driver="opencode",
                failure_kind="infrastructure",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )


# ----------------------------------------------------------------------
# Dispatch-time fallback flows (brief §6) over the fake lanes — no network.
# OFF by default; infrastructure-only; pre-candidate-only; journaled;
# chain exhaustion fails visibly.
# ----------------------------------------------------------------------

ISSUE_IID = 7
PROJECT_ID = 42
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."
BASE_SHA = "base-sha-1"


def fallback_settings(**overrides):
    from forge.config import Settings

    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_IMPLEMENTER_BACKEND="ci_harness",
        FORGE_HARNESS_PREFERENCE="claude-code,grok-build",
        FORGE_HARNESS_FALLBACK=True,
    )
    values.update(overrides)
    return Settings(**values)


class _DbBase:
    @pytest.fixture()
    async def db(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from forge.models.base import Base

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
    def fake_gitlab(self):
        from tests.fixtures.fake_gitlab import FakeGitLab

        fake = FakeGitLab()
        fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
        fake.seed_commit("main", BASE_SHA, "initial")
        return fake

    async def get_run(self, db, run_id):
        from forge.durable import FlowRun

        async with db() as session:
            return await session.get(FlowRun, run_id)

    async def fallback_actions(self, db, run_id):
        from sqlalchemy import select

        from forge.durable import ActionLog

        async with db() as session:
            rows = (
                (
                    await session.execute(
                        select(ActionLog).where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "harness_fallback",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                session.expunge(row)
        return rows

    @staticmethod
    def seed_forge_agent_job(fake_gitlab, pipeline_id, *, status, failure_reason=None, log=None):
        job: dict = {"id": 555, "name": "forge-agent", "status": status}
        if failure_reason is not None:
            job["failure_reason"] = failure_reason
        fake_gitlab.set_pipeline_jobs(pipeline_id, [job])
        if log is not None:
            fake_gitlab.set_job_log(555, log)

    @staticmethod
    def driver_of(pipeline: dict) -> str:
        by_key = {v["key"]: v["value"] for v in pipeline["variables"]}
        return by_key["FORGE_HARNESS_DRIVER"]


class TestGitLabFallbackFlow(_DbBase):
    async def _start_and_go(self, db, fake_gitlab, *, settings) -> tuple[object, str]:
        from forge.durable.identity import factory_branch
        from forge.repository import ChangesetWriter
        from forge.runs import RunService
        from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

        service = RunService(
            session_factory=db,
            gitlab=fake_gitlab,
            settings=settings,
            writer_class=ChangesetWriter,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubReviewer(),
        )
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        run = await self.get_run(db, run_id)
        assert run.status == "waiting_harness"
        assert factory_branch(ISSUE_IID, run_id)
        return service, run_id

    async def test_off_by_default_blocks_on_the_first_infrastructure_failure(self, db, fake_gitlab):
        """The invariant: without FORGE_HARNESS_FALLBACK the run blocks —
        exactly the pre-ADR-0023 semantics, one pipeline, no advance."""
        service, run_id = await self._start_and_go(
            db, fake_gitlab, settings=fallback_settings(FORGE_HARNESS_FALLBACK=False)
        )
        run = await self.get_run(db, run_id)
        pipeline_id = int(run.evidence["harness"]["pipeline_id"])
        self.seed_forge_agent_job(
            fake_gitlab, pipeline_id, status="failed", failure_reason="runner_system_failure"
        )

        await service.evaluate_waiting_harness()

        run = await self.get_run(db, run_id)
        # Infra is transient (revival): parked failed with a revive due.
        assert run.status == "failed"
        assert run.status_reason.startswith("harness_infrastructure")
        assert run.evidence["revive"]["revive_count"] == 1
        assert len(fake_gitlab.pipelines) == 1
        assert await self.fallback_actions(db, run_id) == []

    async def test_infrastructure_failure_advances_down_the_frozen_chain(self, db, fake_gitlab):
        service, run_id = await self._start_and_go(db, fake_gitlab, settings=fallback_settings())

        run = await self.get_run(db, run_id)
        (pipeline_1,) = fake_gitlab.pipelines
        assert self.driver_of(pipeline_1) == "claude-code"  # the frozen head
        self.seed_forge_agent_job(
            fake_gitlab, pipeline_1["id"], status="failed", failure_reason="runner_system_failure"
        )

        await service.evaluate_waiting_harness()

        # Still waiting — parked on the NEXT leg of the frozen chain, and
        # the second pipeline carries the advanced driver.
        run = await self.get_run(db, run_id)
        assert run.status == "waiting_harness"
        assert len(fake_gitlab.pipelines) == 2
        assert self.driver_of(fake_gitlab.pipelines[-1]) == "grok-build"
        assert run.evidence["harness_selection"]["harness"] == "grok-build"
        assert run.evidence["harness_selection"]["harness_fallbacks"] == []
        assert run.evidence["harness"]["driver"] == "grok-build"

        # Every advance is journaled with the event/from/to/reason contract.
        (action,) = await self.fallback_actions(db, run_id)
        assert action.status == "succeeded"
        assert action.remote_result == {
            "event": "harness_fallback",
            "from": "claude-code",
            "to": "grok-build",
            "reason": "harness job forge-agent failed (runner_system_failure)",
        }

    async def test_chain_exhaustion_fails_visibly(self, db, fake_gitlab):
        service, run_id = await self._start_and_go(db, fake_gitlab, settings=fallback_settings())

        run = await self.get_run(db, run_id)
        (pipeline_1,) = fake_gitlab.pipelines
        self.seed_forge_agent_job(
            fake_gitlab, pipeline_1["id"], status="failed", failure_reason="runner_system_failure"
        )
        await service.evaluate_waiting_harness()

        pipeline_2 = fake_gitlab.pipelines[-1]
        self.seed_forge_agent_job(
            fake_gitlab, pipeline_2["id"], status="failed", failure_reason="runner_system_failure"
        )
        await service.evaluate_waiting_harness()

        run = await self.get_run(db, run_id)
        assert run.status == "failed"
        assert run.status_reason.startswith("harness_infrastructure")
        assert run.evidence["revive"]["revive_count"] == 1
        assert len(fake_gitlab.pipelines) == 2  # no third leg — chain done

    async def test_code_failure_never_switches_even_when_enabled(self, db, fake_gitlab):
        service, run_id = await self._start_and_go(db, fake_gitlab, settings=fallback_settings())
        run = await self.get_run(db, run_id)
        pipeline_id = int(run.evidence["harness"]["pipeline_id"])
        self.seed_forge_agent_job(
            fake_gitlab,
            pipeline_id,
            status="failed",
            failure_reason="script_failure",
            log="AssertionError",
        )

        await service.evaluate_waiting_harness()

        run = await self.get_run(db, run_id)
        assert run.status == "blocked"
        assert run.status_reason.startswith("harness_code")
        assert len(fake_gitlab.pipelines) == 1

    async def test_never_switches_once_a_candidate_exists(self, db, fake_gitlab):
        """ADR-0016: one frozen attempt → one candidate → one producer."""
        service, run_id = await self._start_and_go(db, fake_gitlab, settings=fallback_settings())
        run = await self.get_run(db, run_id)
        pipeline_id = int(run.evidence["harness"]["pipeline_id"])
        from forge.durable import FlowRun

        async with db() as session:
            persisted = await session.get(FlowRun, run_id)
            persisted.candidate_shas = ["candidate-sha"]  # a candidate exists
            await session.commit()

        self.seed_forge_agent_job(
            fake_gitlab, pipeline_id, status="failed", failure_reason="runner_system_failure"
        )
        await service.evaluate_waiting_harness()

        run = await self.get_run(db, run_id)
        assert run.status == "failed"
        assert run.evidence["revive"]["revive_count"] == 1
        assert len(fake_gitlab.pipelines) == 1


class TestGitHubFallbackFlow(_DbBase):
    REPO = "acme/acme-widget"
    WORKFLOW = "forge-harness.github.yml"
    ISSUE = 42

    @pytest.fixture()
    def fake(self):
        from tests.fixtures.fake_github import FakeGitHub

        github = FakeGitHub()
        github.seed_repo(self.REPO, {"src/app.py": "print('hi')\n"})
        github.heads[self.REPO]["main"] = BASE_SHA
        github.seed_issue(self.REPO, self.ISSUE, ISSUE_TITLE, ISSUE_DESC)
        return github

    async def _start_and_go(self, db, fake, *, settings) -> tuple[object, str]:
        from forge.config import ForgeConfig
        from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
        from forge.runs.github_service import GitHubRunService
        from forge.runs.stubs import StubImplementer, StubPlanner

        flow = GitHubPublishFlow(fake, proposer=StubImplementer(), base_branch="main")
        stack = GitHubAgents(
            client=fake,
            reader=fake,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=None,  # never reached on the harness lane
            flow=flow,
        )
        service = GitHubRunService(
            db, settings, ForgeConfig(), stack=stack, repo_full_name=self.REPO
        )
        await service.start_run(
            project_id=PROJECT_ID,
            issue_number=self.ISSUE,
            issue_title=ISSUE_TITLE,
            issue_description=ISSUE_DESC,
            author_username="alice",
        )
        await service.handle_go(
            project_id=PROJECT_ID,
            issue_number=self.ISSUE,
            note_text=f"/go {(await self._only_run_id(db))}",
            author_username="alice",
        )
        run_id = await self._only_run_id(db)
        run = await self.get_run(db, run_id)
        assert run.status == "waiting_harness"
        return service, run_id

    async def _only_run_id(self, db) -> str:
        from sqlalchemy import select

        from forge.durable import FlowRun

        async with db() as session:
            return (await session.execute(select(FlowRun.id))).scalars().one()

    def _seed_actions_failure(self, fake, actions_run_id: int, *, log: str) -> None:
        for run in fake.actions_runs:
            if run["id"] == actions_run_id:
                run.update(status="completed", conclusion="failure")
        fake.seed_actions_jobs(
            actions_run_id, [{"id": 9, "name": "harness", "conclusion": "failure"}]
        )
        fake.seed_job_log(9, log)

    async def test_off_by_default_blocks_on_the_first_infrastructure_failure(self, db, fake):
        service, run_id = await self._start_and_go(
            db,
            fake,
            settings=fallback_settings(
                FORGE_GITHUB_HARNESS_WORKFLOW=self.WORKFLOW, FORGE_HARNESS_FALLBACK=False
            ),
        )
        self._seed_actions_failure(fake, 501, log="Error: quota exceeded for this API key\n")

        await service.evaluate_waiting_harness()

        run = await self.get_run(db, run_id)
        # Infra is transient (revival): parked failed with a revive due.
        assert run.status == "failed"
        assert (run.status_reason or "").startswith("harness_infrastructure")
        assert run.evidence["revive"]["revive_count"] == 1
        assert len(fake.dispatch_inputs) == 1
        assert await self.fallback_actions(db, run_id) == []

    async def test_infrastructure_failure_advances_down_the_frozen_chain(self, db, fake):
        service, run_id = await self._start_and_go(
            db, fake, settings=fallback_settings(FORGE_GITHUB_HARNESS_WORKFLOW=self.WORKFLOW)
        )
        assert fake.dispatch_inputs[0]["inputs"]["driver"] == "claude-code"

        self._seed_actions_failure(fake, 501, log="Error: quota exceeded for this API key\n")
        await service.evaluate_waiting_harness()

        run = await self.get_run(db, run_id)
        assert run.status == "waiting_harness"
        assert len(fake.dispatch_inputs) == 2
        assert fake.dispatch_inputs[1]["inputs"]["driver"] == "grok-build"
        assert run.evidence["harness_selection"]["harness"] == "grok-build"
        assert run.evidence["harness"]["driver"] == "grok-build"

        (action,) = await self.fallback_actions(db, run_id)
        assert action.status == "succeeded"
        assert action.remote_result["event"] == "harness_fallback"
        assert action.remote_result["from"] == "claude-code"
        assert action.remote_result["to"] == "grok-build"

    async def test_chain_exhaustion_fails_visibly(self, db, fake):
        service, run_id = await self._start_and_go(
            db, fake, settings=fallback_settings(FORGE_GITHUB_HARNESS_WORKFLOW=self.WORKFLOW)
        )
        self._seed_actions_failure(fake, 501, log="Error: quota exceeded\n")
        await service.evaluate_waiting_harness()

        self._seed_actions_failure(fake, 502, log="Error: quota exceeded\n")
        await service.evaluate_waiting_harness()

        run = await self.get_run(db, run_id)
        assert run.status == "failed"
        assert (run.status_reason or "").startswith("harness_infrastructure")
        assert run.evidence["revive"]["revive_count"] == 1
        assert len(fake.dispatch_inputs) == 2

    async def test_code_failure_never_switches_even_when_enabled(self, db, fake):
        service, run_id = await self._start_and_go(
            db, fake, settings=fallback_settings(FORGE_GITHUB_HARNESS_WORKFLOW=self.WORKFLOW)
        )
        self._seed_actions_failure(fake, 501, log="claude: the agent exited with code 1\n")

        await service.evaluate_waiting_harness()

        run = await self.get_run(db, run_id)
        assert run.status == "blocked"
        assert (run.status_reason or "").startswith("harness_code")
        assert len(fake.dispatch_inputs) == 1


# ----------------------------------------------------------------------
# Doctor per-driver lane checks (brief §8): variable NAMES only.
# ----------------------------------------------------------------------


class TestDoctorHarnessLanes:
    @staticmethod
    def _check(**setting_overrides):
        from forge.doctor import check_harness_lanes

        return check_harness_lanes(
            fallback_settings(**setting_overrides),
            {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"},
        )

    @staticmethod
    def _by_name(results):
        return {r.name: r for r in results}

    def test_full_chain_reports_per_driver_passes(self):
        results = self._by_name(self._check())

        assert results["project.harness.claude-code"].status == "pass"
        assert "ANTHROPIC_AUTH_TOKEN" in results["project.harness.claude-code"].detail
        # grok-build has no credentials in the project → warn, chain shrinks.
        assert results["project.harness.grok-build"].status == "warn"
        assert "FORGE_GROK_AUTH" in results["project.harness.grok-build"].detail
        assert results["project.harness_chain"].detail == "[claude-code]"

    def test_empty_chain_with_harness_backend_fails(self):
        results = self._by_name(
            self._check(
                FORGE_HARNESS_PREFERENCE="grok-build",
                FORGE_IMPLEMENTER_BACKEND="ci_harness:grok-build",
            )
        )

        assert results["project.harness.grok-build"].status == "warn"
        assert results["project.harness_chain"].status == "fail"

    def test_empty_chain_with_builtin_backend_is_only_a_warning(self):
        """Doctor must stay green for builtin projects that merely declare
        a chain wider than today's lanes."""
        results = self._by_name(
            self._check(
                FORGE_HARNESS_PREFERENCE="grok-build",
                FORGE_IMPLEMENTER_BACKEND="builtin",
            )
        )

        assert results["project.harness_chain"].status == "warn"

    def test_contradictory_preference_is_reported_as_a_failure(self):
        results = self._by_name(
            self._check(
                FORGE_HARNESS_PREFERENCE="grok-build",  # backend driver missing
                FORGE_IMPLEMENTER_BACKEND="ci_harness:claude-code",
            )
        )

        assert results["project.harness_preference"].status == "fail"
        assert "tightens, never deselects" in results["project.harness_preference"].detail

    def test_default_preference_is_the_backend_driver(self):
        results = self._by_name(
            self._check(FORGE_HARNESS_PREFERENCE="", FORGE_IMPLEMENTER_BACKEND="ci_harness")
        )

        assert "project.harness.claude-code" in results
        assert results["project.harness_chain"].detail == "[claude-code]"
