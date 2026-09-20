"""Stage B2 (ADR-0018) + R04: the executable RunSpec, pending decision, admission.

- F14: the immutable RunSpec is frozen at plan acceptance — canonical JSON
  document + digest, mirrored into ``run.spec_digest`` — before the plan note
  is posted. The extended policy digest binds the effective execution policy.
- R04: the spec is an EXECUTABLE immutable input, not just a digest — it
  carries the frozen task text, the plan artifact, the model route, the path
  policy, the verification contract and the budgets. Post-approval legs read
  it digest-verified (a tampered/missing spec blocks ``spec_invalid``, never
  a silent fallback to live settings); the implementer executes the frozen
  task text, whatever the issue shows now (drift is recorded as
  ``spec_drift`` evidence).
- F15: the pending decision is created at plan publication carrying the
  plan/task/spec digests and an absolute deadline; ``/go`` consumes THAT row;
  expiry or spec/policy drift invalidates it; issue-text drift after approval
  is flagged in the evidence comment.
- F16: admission refuses non-approvers (and a bot-in-approvers config)
  BEFORE the planner — a denial never burns a model call.
"""

import dataclasses
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings
from forge.durable import Controller, FlowRun, FlowStatus, GateApproval, RunSpec
from forge.factory.implementer import IMPLEMENTER_TIER
from forge.models.base import Base
from forge.runs import RunService
from forge.runs.service import task_digest_of
from forge.runs.spec import (
    EXECUTABLE_SPEC_SCHEMA_VERSION,
    ExecutableRunSpec,
    SpecInvalid,
    load_verified_spec,
)
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer, factory_branch
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import FakeWriter

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."


@pytest.fixture(autouse=True)
def _clear_project_config_cache():
    """The project-config cache is process-global (5-min TTL) — never leak
    a seeded `.forge.yml` scope between tests."""
    from forge.orchestrator.project_config import clear_cache

    clear_cache()
    yield
    clear_cache()


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_HARNESS_TIMEOUT_SECONDS=1800,  # hermetic: dev .env sets 5400
    )
    values.update(overrides)
    return Settings(**values)


class RecordingPlanner(StubPlanner):
    """StubPlanner that counts calls — admission denials must never call it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def plan(self, *args, **kwargs) -> str:
        self.calls += 1
        return await super().plan(*args, **kwargs)


class RecordingImplementer(StubImplementer):
    """Records the frozen-spec inputs the service passes to propose (R04)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[dict] = []

    async def propose(self, run, issue_title, **kwargs):
        self.calls.append({"issue_title": issue_title, **kwargs})
        return await super().propose(run, issue_title, **kwargs)


def make_service(db, fake_gitlab, *, settings=None, planner=None) -> RunService:
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings or make_settings(),
        writer_class=FakeWriter,
        planner=planner or StubPlanner(),
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
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


@pytest.fixture()
def service(db, fake_gitlab):
    FakeWriter.reset()
    return make_service(db, fake_gitlab)


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def get_gate(db, run_id: str) -> GateApproval | None:
    async with db() as session:
        gate = (
            (
                await session.execute(
                    select(GateApproval)
                    .where(GateApproval.flow_run_id == run_id)
                    .order_by(GateApproval.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if gate is not None:
            session.expunge(gate)
        return gate


async def get_spec(db, run_id: str) -> RunSpec | None:
    async with db() as session:
        spec = (
            (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
            .scalars()
            .first()
        )
        if spec is not None:
            session.expunge(spec)
        return spec


async def replace_spec(db, run_id: str, document: dict, *, digest: str | None = None) -> None:
    """Rewrite the stored spec row (tamper tests); JSON needs reassignment."""
    async with db() as session:
        spec = (
            (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id))).scalars().one()
        )
        spec.document = document
        if digest is not None:
            spec.digest = digest
        await session.commit()


async def spec_document_of(db, run_id: str) -> dict:
    spec = await get_spec(db, run_id)
    assert spec is not None
    return dict(spec.document)


async def drive_to_waiting_ci(service, fake_gitlab: FakeGitLab, db) -> str:
    """start_run → /go → committed candidate waiting for CI."""
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    branch = factory_branch(ISSUE_IID, run_id)
    sha = (await get_run(db, run_id)).candidate_shas[-1]
    fake_gitlab.seed_commit(branch, sha, "forge commit")
    pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
    fake_gitlab.set_pipeline_status(pipeline_id, "success", sha)
    return run_id


def sha256_of_document(document: dict) -> str:
    """An independent canonical-JSON sha256 (pins the digest contract)."""
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


class TestRunSpec:
    async def test_spec_frozen_at_plan_acceptance(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        run = await get_run(db, run_id)
        spec = await get_spec(db, run_id)
        assert spec is not None
        assert spec.run_id == run_id
        assert spec.schema_version == EXECUTABLE_SPEC_SCHEMA_VERSION
        assert run.spec_digest == spec.digest

        document = spec.document
        assert document["subject"] == {
            "provider": "gitlab",
            "project_id": PROJECT_ID,
            "issue_iid": ISSUE_IID,
        }
        assert document["source_base_oid"] == "base-sha-1"  # run.base_sha
        assert document["plan_digest"] == run.plan_digest
        assert document["task_digest"] == task_digest_of(ISSUE_TITLE, ISSUE_DESC)
        assert document["policy_digest"] == service._policy_digest()
        # R04: the executable content — the frozen task text, the plan
        # artifact, the model route and the verification contract — rides in
        # the document the digest covers.
        assert document["task"] == {
            "title": ISSUE_TITLE,
            "description": ISSUE_DESC,
            "digest": task_digest_of(ISSUE_TITLE, ISSUE_DESC),
        }
        assert document["plan"]["digest"] == run.plan_digest
        assert document["plan"]["summary"]
        assert document["plan"]["files_hint"] == []
        assert document["model_route"] == {"tier": IMPLEMENTER_TIER}
        assert document["verification"] == {"required_jobs": [], "waived_conclusions": []}
        # ADR-0023: the frozen harness decision rides in backend_config —
        # default preference ⇒ the configured backend's driver alone.
        assert document["backend_config"] == {
            "backend": "builtin",
            "model": make_settings().FORGE_HARNESS_MODEL,
            "target_branch": "main",
            "harness": "claude-code",
            "harness_fallbacks": [],
            "budget_class": "standard",
            "selection_reason": "default",
        }
        assert document["budgets"] == {"commit_cycles": 3, "harness_timeout": 1800}

        # The digest is the sha256 over the canonical (sorted-key) JSON.
        assert spec.digest == sha256_of_document(document)

        # The frozen document parses into the typed spec, round-trip.
        typed = ExecutableRunSpec.from_document(document)
        assert typed.to_document() == document
        assert typed.task_text == f"{ISSUE_TITLE}\n{ISSUE_DESC}"

        # ADR-0023: the selection is also visible in the run's evidence at
        # run start ("backend" stays the reconciler's backend-name string).
        evidence = run.evidence or {}
        assert evidence["backend"] == "builtin"
        assert evidence["harness_selection"]["harness"] == "claude-code"
        assert evidence["harness_selection"]["harness_fallbacks"] == []

    async def test_policy_digest_binds_effective_policy(self, service):
        settings = make_settings(FORGE_REQUIRED_JOBS="pytest", FORGE_TARGET_BRANCH="release")
        scoped = make_service(None, None, settings=settings)  # type: ignore[arg-type]
        document = {
            "approvers": ["alice"],
            "target_branch": "release",
            "required_jobs": ["pytest"],
            "implementer_backend": "builtin",
            "harness_model": settings.FORGE_HARNESS_MODEL,
            "harness_preference": [],
            "harness_fallback": False,
        }
        assert scoped._policy_digest() == sha256_of_document(document)
        # The default policy digest differs once any policy input moves.
        assert scoped._policy_digest() != service._policy_digest()

    async def test_policy_digest_binds_the_harness_chain(self):
        """ADR-0023 §3: changing the preference (or the fallback switch)
        invalidates pending gates — the digest moves with the chain."""
        plain = make_service(None, None)  # type: ignore[arg-type]
        preferred = make_service(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            settings=make_settings(FORGE_HARNESS_PREFERENCE="claude-code,grok-build"),
        )
        fallback_on = make_service(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            settings=make_settings(FORGE_HARNESS_FALLBACK=True),
        )
        assert preferred._policy_digest() != plain._policy_digest()
        assert fallback_on._policy_digest() != plain._policy_digest()

    async def test_harness_preference_freezes_the_chain_into_the_spec(
        self, fake_gitlab, db, tmp_path
    ):
        """A multi-entry preference (forge.yml `implement.harnesses`) freezes
        the full chain (harness + fallbacks) into the RunSpec the gate
        approves."""
        from forge.runs.harness_selection import resolve_preference

        config_path = tmp_path / "forge.yml"
        config_path.write_text(
            "forge:\n  implement:\n    harnesses:\n      - claude-code\n      - grok-build\n"
        )
        config = ForgeConfig(str(config_path))
        assert resolve_preference(config, make_settings()) == ["claude-code", "grok-build"]

        service = RunService(
            session_factory=db,
            gitlab=fake_gitlab,
            settings=make_settings(FORGE_IMPLEMENTER_BACKEND="ci_harness"),
            config=config,
            writer_class=FakeWriter,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubReviewer(),
        )
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        spec = await get_spec(db, run_id)
        assert spec is not None
        backend_config = spec.document["backend_config"]
        assert backend_config["harness"] == "claude-code"
        assert backend_config["harness_fallbacks"] == ["grok-build"]

    async def test_preference_drift_after_start_changes_the_policy_digest(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        gate = await get_gate(db, run_id)
        before = service._policy_digest()

        service._settings.FORGE_HARNESS_PREFERENCE = "claude-code,grok-build"

        assert service._policy_digest() != before
        assert gate.policy_digest == before  # the decision froze the old policy

    async def test_setting_drift_after_start_changes_the_policy_digest(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        gate = await get_gate(db, run_id)
        spec = await get_spec(db, run_id)
        before = service._policy_digest()

        service._settings.FORGE_REQUIRED_JOBS = "pytest"

        assert service._policy_digest() != before
        assert gate.policy_digest == before  # the decision froze the old policy
        assert spec.document["policy_digest"] == before


class TestPendingDecision:
    async def test_decision_created_at_plan_publication(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        run = await get_run(db, run_id)
        gate = await get_gate(db, run_id)

        assert gate is not None
        assert gate.generation == 0
        assert gate.consumed_at is None
        assert gate.approver_user_id == 0  # no approver yet — recorded at /go
        assert gate.plan_digest == run.plan_digest
        assert gate.base_sha == run.base_sha
        assert gate.policy_digest == service._policy_digest()
        assert gate.spec_digest == run.spec_digest
        assert gate.task_digest == task_digest_of(ISSUE_TITLE, ISSUE_DESC)
        # Absolute deadline: FORGE_DECISION_TTL_SECONDS after publication.
        ttl = make_settings().FORGE_DECISION_TTL_SECONDS
        span = (gate.expires_at - gate.created_at).total_seconds()
        assert abs(span - ttl) < 5

    async def test_go_consumes_the_pending_decision(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        gate = await get_gate(db, run_id)
        assert gate.consumed_at is not None
        assert gate.approver_user_id == 11
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

    async def test_go_without_pending_decision_is_ignored(self, db, fake_gitlab):
        """A waiting_approval run whose decision row is missing: /go is a no-op."""
        service = make_service(db, fake_gitlab)
        run_id = uuid4().hex
        async with db() as session:
            controller = Controller(session)
            session.add(FlowRun(id=run_id, project_id=PROJECT_ID, issue_iid=ISSUE_IID))
            await controller.transition(run_id, FlowStatus.PREFLIGHT)
            await controller.transition(run_id, FlowStatus.PLANNING)
            await controller.transition(run_id, FlowStatus.WAITING_APPROVAL)
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value

    async def test_expired_decision_is_invalid(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
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
            gate.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
        gate = await get_gate(db, run_id)
        assert gate.consumed_at is None

    async def test_spec_digest_drift_invalidates_go(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.spec_digest = "f" * 64  # the spec the gate froze moved
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
        assert (await get_gate(db, run_id)).consumed_at is None

    async def test_issue_text_drift_is_flagged_in_evidence(self, service, fake_gitlab, db):
        run_id = await drive_to_waiting_ci(service, fake_gitlab, db)

        # The issue body changed AFTER the approval — the run still executes
        # the approved task snapshot and says so in the evidence comment.
        fake_gitlab.seed_issue(ISSUE_IID, "A different task", "The body moved on.")
        await service.evaluate_waiting_ci()

        assert fake_gitlab.notes_containing(
            "⚠️ issue text changed since approval; the run executed the approved task snapshot"
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_no_issue_drift_no_warning(self, service, fake_gitlab, db):
        await drive_to_waiting_ci(service, fake_gitlab, db)
        await service.evaluate_waiting_ci()

        assert fake_gitlab.notes, "evidence comment posted"
        assert not fake_gitlab.notes_containing("issue text changed")


class TestAdmission:
    async def test_non_approver_implement_is_denied_before_the_planner(self, db, fake_gitlab):
        planner = RecordingPlanner()
        service = make_service(db, fake_gitlab, planner=planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "mallory")

        # F16: the refusal is comment-only — no planner (LLM) call happened.
        assert planner.calls == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "admission_denied: actor @mallory not in approvers"
        assert await get_spec(db, run_id) is None  # no spec frozen either
        assert fake_gitlab.notes_containing("admission denied")
        assert fake_gitlab.notes_containing("@mallory")
        assert not fake_gitlab.notes_containing("Forge plan")

    async def test_bot_username_in_approvers_is_a_config_contradiction(self, db, fake_gitlab):
        settings = make_settings(FORGE_APPROVERS="alice,forge-bot", FORGE_BOT_USERNAME="forge-bot")
        planner = RecordingPlanner()
        service = make_service(db, fake_gitlab, settings=settings, planner=planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        assert planner.calls == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "must not appear in FORGE_APPROVERS" in run.status_reason

    async def test_approver_implement_is_admitted(self, db, fake_gitlab):
        planner = RecordingPlanner()
        service = make_service(db, fake_gitlab, planner=planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        assert planner.calls == 1
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value


class TestPathScope:
    """Monorepo path scoping (v0.7): `implement.paths` → RunSpec + prompt."""

    class ScopeRecordingPlanner(StubPlanner):
        """Records the path_scope kwarg the service passes to the planner."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.path_scopes: list[list[str] | None] = []

        async def plan(self, *args, **kwargs) -> str:
            self.path_scopes.append(kwargs.get("path_scope"))
            return await super().plan(*args, **kwargs)

    class OutOfScopeImplementer(StubImplementer):
        """Proposes one CREATE outside any configured scope."""

        async def propose(self, run, issue_title, **kwargs):
            from forge.repository import Change, ChangeSet, Operation

            return ChangeSet(
                branch=factory_branch(run.issue_iid, run.id),
                commit_message=f"forge: implement {run.issue_iid or 0}",
                changes=[
                    Change(
                        path="webapp/out-of-scope.ts",
                        operation=Operation.CREATE,
                        content="// outside implement.paths\n",
                    )
                ],
            )

    async def _scoped_service(self, db, fake_gitlab, planner) -> RunService:
        fake_gitlab.seed_file(".forge.yml", "implement:\n  paths:\n    - 'services/**'\n")
        return make_service(db, fake_gitlab, planner=planner)

    async def test_scoped_project_freezes_allowed_paths_into_the_spec(self, db, fake_gitlab):
        planner = self.ScopeRecordingPlanner()
        service = await self._scoped_service(db, fake_gitlab, planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        spec = await get_spec(db, run_id)
        assert spec is not None
        assert spec.document["allowed_paths"] == ["services/**"]
        assert spec.digest == sha256_of_document(spec.document)
        # The plan prompt aimed inside the scope from the start.
        assert planner.path_scopes == [["services/**"]]

    async def test_unscoped_project_spec_has_no_allowed_paths(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        spec = await get_spec(db, run_id)
        assert spec is not None
        assert "allowed_paths" not in spec.document

    async def test_out_of_scope_change_blocks_at_builtin_validation(self, db, fake_gitlab):
        from forge.durable import FlowStatus

        planner = self.ScopeRecordingPlanner()
        service = await self._scoped_service(db, fake_gitlab, planner)
        service._implementer = self.OutOfScopeImplementer()

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "outside the allowed scope" in (run.status_reason or "")


class TestExecutableRunSpec:
    """Unit contract of the typed executable spec (R04, forge.runs.spec)."""

    @staticmethod
    def make_spec(**overrides) -> ExecutableRunSpec:
        values = dict(
            provider="gitlab",
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            source_base_oid="base-sha-1",
            task_title=ISSUE_TITLE,
            task_description=ISSUE_DESC,
            plan_summary="Plan summary.",
            plan_files_hint=("src/app.py",),
            plan_digest="a" * 64,
            model_route="code",
            policy_digest="b" * 64,
            required_jobs=("pytest",),
            backend="builtin",
            harness_model="glm-5.3-flash[1m]",
            target_branch="main",
            harness_driver="claude-code",
            commit_cycles=3,
            harness_timeout=1800,
        )
        values.update(overrides)
        return ExecutableRunSpec.freeze(**values)

    def test_schema_version_is_present_and_executable(self):
        spec = self.make_spec()
        assert spec.schema_version == EXECUTABLE_SPEC_SCHEMA_VERSION == 3
        assert ExecutableRunSpec.from_document(spec.to_document()).schema_version == 3

    def test_round_trip_preserves_every_field(self):
        spec = self.make_spec(allowed_paths=("services/**",), harness_fallbacks=("grok-build",))
        parsed = ExecutableRunSpec.from_document(spec.to_document())
        assert parsed == spec

    def test_task_digest_binds_the_text(self):
        """A tampered task text cannot survive a verified parse."""
        spec = self.make_spec()
        document = spec.to_document()
        document["task"]["title"] = "Tampered title"
        with pytest.raises(SpecInvalid, match="task artifact"):
            ExecutableRunSpec.from_document(document)

    def test_pre_executable_schema_is_refused(self):
        spec = self.make_spec()
        with pytest.raises(SpecInvalid, match="not executable"):
            dataclasses.replace(spec, schema_version=2)

    def test_verified_read_detects_digest_mismatch(self):
        document = self.make_spec().to_document()
        with pytest.raises(SpecInvalid, match="digest mismatch"):
            load_verified_spec(document=document, digest="f" * 64)

    def test_verified_read_enforces_the_gate_binding(self):
        document = self.make_spec().to_document()
        with pytest.raises(SpecInvalid, match="gate-approved"):
            load_verified_spec(
                document=document,
                digest=sha256_of_document(document),
                run_spec_digest="f" * 64,
            )

    def test_verified_read_refuses_missing_document(self):
        with pytest.raises(SpecInvalid, match="no spec document"):
            load_verified_spec(document=None, digest=None)

    def test_legacy_row_raises_spec_legacy(self):
        """A02 legacy policy: a pre-executable stored row raises SpecLegacy —
        the re-approval-required signal, still caught as a SpecInvalid by
        every fail-closed caller."""
        from forge.runs.spec import SpecLegacy

        document = self.make_spec().to_document()
        with pytest.raises(SpecLegacy, match="re-approval required") as excinfo:
            load_verified_spec(
                document=document,
                digest=sha256_of_document(document),
                schema_version=2,
            )
        assert isinstance(excinfo.value, SpecInvalid)
        assert "not executable" in str(excinfo.value)

    def test_type_garbage_is_spec_invalid_not_a_crash(self):
        """Corrupt numeric fields surface as SpecInvalid (blocked spec_invalid),
        never as a raw ValueError past the verified read."""
        document = self.make_spec().to_document()
        document["budgets"]["commit_cycles"] = "garbage"
        with pytest.raises(SpecInvalid, match="unreadable"):
            ExecutableRunSpec.from_document(document)

    def test_lane_dispatch_contract_round_trips(self):
        """A02: the additive frozen dispatch-contract fields (the GitHub
        Actions workflow filename / the Azure lane pipeline id) round-trip
        through the document — and stay ABSENT for lane-less runs."""
        gh = self.make_spec(backend="ci_harness", harness_workflow="forge-harness.github.yml")
        parsed_gh = ExecutableRunSpec.from_document(gh.to_document())
        assert parsed_gh.harness_workflow == "forge-harness.github.yml"
        assert "harness_workflow" in gh.to_document()["backend_config"]
        assert "lane_pipeline_id" not in gh.to_document()["backend_config"]

        az = self.make_spec(backend="ci_harness", lane_pipeline_id=207)
        parsed_az = ExecutableRunSpec.from_document(az.to_document())
        assert parsed_az.lane_pipeline_id == 207
        assert az.to_document()["backend_config"]["lane_pipeline_id"] == 207
        assert "harness_workflow" not in az.to_document()["backend_config"]

        bare = self.make_spec()
        parsed_bare = ExecutableRunSpec.from_document(bare.to_document())
        assert parsed_bare.harness_workflow == ""
        assert parsed_bare.lane_pipeline_id is None
        assert "harness_workflow" not in bare.to_document()["backend_config"]
        assert "lane_pipeline_id" not in bare.to_document()["backend_config"]

    def test_non_positive_lane_pipeline_id_is_spec_invalid(self):
        with pytest.raises(SpecInvalid, match="lane_pipeline_id"):
            self.make_spec(lane_pipeline_id=0)


class TestSpecBudgetCeilings:
    """R13: the numeric budget ceilings frozen into the budgets block —
    additive fields, tolerant parse, honest enforcement level."""

    @staticmethod
    def make_spec(**overrides) -> ExecutableRunSpec:
        values = dict(
            provider="gitlab",
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            source_base_oid="base-sha-1",
            task_title=ISSUE_TITLE,
            task_description=ISSUE_DESC,
            plan_summary="Plan summary.",
            plan_files_hint=(),
            plan_digest="a" * 64,
            model_route="code",
            policy_digest="b" * 64,
            required_jobs=(),
            backend="builtin",
            harness_model="glm-5.3-flash[1m]",
            target_branch="main",
            harness_driver="claude-code",
            commit_cycles=3,
            harness_timeout=1800,
            budget_max_calls=40,
            budget_max_tokens=500000,
            budget_wallclock_s=3600,
            budget_enforcement="full",
        )
        values.update(overrides)
        return ExecutableRunSpec.freeze(**values)

    def test_ceilings_round_trip_through_the_document(self):
        spec = self.make_spec()
        document = spec.to_document()
        assert document["budgets"] == {
            "commit_cycles": 3,
            "harness_timeout": 1800,
            "max_calls": 40,
            "max_tokens": 500000,
            "wallclock_s": 3600,
            "enforcement": "full",
        }
        assert ExecutableRunSpec.from_document(document) == spec

    def test_absent_ceilings_parse_as_unlimited(self):
        """Pre-R13 v3 documents carry none of the new keys — from_document is
        tolerant of their absence and reads them as unlimited."""
        legacy = self.make_spec(
            budget_max_calls=None,
            budget_max_tokens=None,
            budget_wallclock_s=None,
            budget_enforcement="",
        )
        document = legacy.to_document()
        assert set(document["budgets"]) == {"commit_cycles", "harness_timeout"}
        assert ExecutableRunSpec.from_document(document) == legacy

    def test_partial_enforcement_is_a_valid_frozen_level(self):
        spec = self.make_spec(budget_enforcement="partial")
        assert ExecutableRunSpec.from_document(spec.to_document()).budget_enforcement == "partial"

    def test_unknown_enforcement_level_is_refused(self):
        with pytest.raises(SpecInvalid, match="budget_enforcement"):
            self.make_spec(budget_enforcement="total")

    def test_ceilings_require_an_enforcement_level(self):
        """A ceiling without an honest enforcement claim is a spec defect —
        never claim a cap you do not enforce, and never freeze one unnamed."""
        with pytest.raises(SpecInvalid, match="enforcement"):
            self.make_spec(budget_enforcement="")

    def test_garbage_ceiling_is_spec_invalid(self):
        document = self.make_spec().to_document()
        document["budgets"]["max_calls"] = "lots"
        with pytest.raises(SpecInvalid, match="unreadable"):
            ExecutableRunSpec.from_document(document)

    def test_non_positive_ceiling_is_spec_invalid(self):
        document = self.make_spec().to_document()
        document["budgets"]["wallclock_s"] = 0
        with pytest.raises(SpecInvalid, match="unreadable"):
            ExecutableRunSpec.from_document(document)


class TestFrozenExecution:
    """R04: post-approval legs execute the FROZEN spec, never live config."""

    async def test_issue_edited_after_go_implementer_receives_frozen_text(
        self, service, fake_gitlab, db
    ):
        """The exact R04 regression: the issue drifts after the freeze, the
        implementer still executes the text the approver saw."""
        implementer = RecordingImplementer()
        service._implementer = implementer
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        fake_gitlab.seed_issue(ISSUE_IID, "A different task", "The body moved on.")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # proceeds, not blocked
        (call,) = implementer.calls
        assert call["task_text"] == f"{ISSUE_TITLE}\n{ISSUE_DESC}"
        assert call["issue_title"] == ISSUE_TITLE
        assert call["plan_summary"]  # plan artifact from the spec
        assert call["model_route"] == IMPLEMENTER_TIER
        # The drift after approval is recorded as evidence — not executed,
        # not a blocking path (#29 owns issue freshness).
        drift = run.evidence["spec_drift"]
        assert drift["frozen_task_digest"] == task_digest_of(ISSUE_TITLE, ISSUE_DESC)
        assert drift["live_task_digest"] == task_digest_of("A different task", "The body moved on.")

    async def test_no_drift_records_no_spec_drift_evidence(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert "spec_drift" not in (run.evidence or {})

    async def test_tampered_spec_blocks_execution(self, service, fake_gitlab, db):
        """A stored spec whose bytes no longer match its digest blocks the
        run as spec_invalid — never a silent fallback to live settings."""
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        document = await spec_document_of(db, run_id)
        document["task"] = {**document["task"], "title": "Tampered task"}
        await replace_spec(db, run_id, document)

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("spec_invalid")
        assert (await get_gate(db, run_id)).consumed_at is not None  # gate was spent

    async def test_forged_digest_still_blocked_by_the_gate_binding(self, service, db):
        """A consistent (document, digest) pair that is NOT the digest the
        pending decision froze is equally spec_invalid."""
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        document = await spec_document_of(db, run_id)
        document["task"] = {
            "title": "Tampered task",
            "description": "Rewritten after the fact.",
            "digest": task_digest_of("Tampered task", "Rewritten after the fact."),
        }
        document["task_digest"] = document["task"]["digest"]
        await replace_spec(db, run_id, document, digest=sha256_of_document(document))

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "gate-approved" in (run.status_reason or "")

    async def test_missing_spec_blocks_execution(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            await session.delete(spec)
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("spec_invalid")

    async def test_legacy_spec_is_not_executable(self, service, db):
        """A pre-executable (v2) spec row has no verified executable shape —
        fail closed, never re-derive the task from the live issue."""
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            spec.schema_version = 2
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "not executable" in (run.status_reason or "")

    async def test_corrupt_spec_blocks_at_the_verification_read(self, service, fake_gitlab, db):
        """The verified read runs at EVERY consumption point — tamper after
        the commit and the CI evaluation blocks instead of judging green."""
        run_id = await drive_to_waiting_ci(service, fake_gitlab, db)
        document = await spec_document_of(db, run_id)
        document["verification"] = {"required_jobs": ["never-seeded-job"], "waived_conclusions": []}
        await replace_spec(db, run_id, document)

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("spec_invalid")

    async def test_frozen_verification_contract_survives_setting_drift(self, db, fake_gitlab):
        """The required jobs come from the spec: relaxing the live setting
        after the gate consumed the frozen contract cannot soften it."""
        settings = make_settings(FORGE_REQUIRED_JOBS="pytest")
        service = make_service(db, fake_gitlab, settings=settings)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        spec = await get_spec(db, run_id)
        assert spec is not None
        assert spec.document["verification"] == {
            "required_jobs": ["pytest"],
            "waived_conclusions": [],
        }

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        service._settings.FORGE_REQUIRED_JOBS = ""  # drift AFTER approval
        branch = factory_branch(ISSUE_IID, run_id)
        sha = (await get_run(db, run_id)).candidate_shas[-1]
        fake_gitlab.seed_commit(branch, sha, "forge commit")
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "success", sha)  # no pytest job

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("quality_contract")

    async def test_empty_frozen_contract_never_adopts_a_later_requirement(
        self, service, fake_gitlab, db
    ):
        """The converse: the spec froze no required jobs, so a requirement
        added to live settings after approval cannot retro-block — the run
        is honestly labeled unverified, not re-judged by live config."""
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        service._settings.FORGE_REQUIRED_JOBS = "pytest"  # drift AFTER approval
        branch = factory_branch(ISSUE_IID, run_id)
        sha = (await get_run(db, run_id)).candidate_shas[-1]
        fake_gitlab.seed_commit(branch, sha, "forge commit")
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "success", sha)  # no pytest job

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert run.evidence["verification"]["status"] == "unverified"

    async def test_frozen_commit_cycle_budget_survives_setting_drift(self, db, fake_gitlab):
        settings = make_settings(FORGE_MAX_COMMIT_CYCLES=1)
        service = make_service(db, fake_gitlab, settings=settings)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        service._settings.FORGE_MAX_COMMIT_CYCLES = 5  # drift AFTER approval
        branch = factory_branch(ISSUE_IID, run_id)
        sha = (await get_run(db, run_id)).candidate_shas[-1]
        fake_gitlab.seed_commit(branch, sha, "forge commit")
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "failed", sha)
        fake_gitlab.set_pipeline_jobs(
            pipeline_id,
            [{"id": 1, "name": "pytest", "status": "failed", "failure_reason": "script_error"}],
        )

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("commit_cycles_exhausted")
        assert "1 of 1" in (run.status_reason or "")


class TestImplementerFrozenInputs:
    """R04 at the agent seam: task_text skips the live issue read; the
    model route overrides the tier."""

    CREATE_DRAFT = json.dumps(
        {
            "branch": "model/chose/this",
            "commit_message": "model's own message",
            "changes": [
                {"path": "forge-demo/feature.md", "operation": "create", "content": "# feature\n"}
            ],
        }
    )

    @staticmethod
    def make_run() -> FlowRun:
        return FlowRun(
            id=uuid4().hex, project_id=PROJECT_ID, issue_iid=ISSUE_IID, base_sha="base-sha-1"
        )

    async def make_implementer(self, fake_gitlab: FakeGitLab):
        from tests.fixtures.fake_llm import FakeLLM

        from forge.factory.implementer import LLMImplementer

        llm = FakeLLM(script=[self.CREATE_DRAFT])
        return llm, LLMImplementer(llm, gitlab=fake_gitlab)

    async def test_task_text_is_executed_verbatim_without_live_issue_read(self, fake_gitlab):
        llm, implementer = await self.make_implementer(fake_gitlab)

        await implementer.propose(
            self.make_run(),
            ISSUE_TITLE,
            task_text=f"{ISSUE_TITLE}\n{ISSUE_DESC}",
            model_route=IMPLEMENTER_TIER,
        )

        call = llm.calls_for("implementer")[0]
        assert call["tier"] == IMPLEMENTER_TIER
        assert f"{ISSUE_TITLE}\n{ISSUE_DESC}" in call["user"]
        assert fake_gitlab.calls_of("get_issue") == []  # no live issue re-read

    async def test_model_route_overrides_the_default_tier(self, fake_gitlab):
        llm, implementer = await self.make_implementer(fake_gitlab)

        await implementer.propose(
            self.make_run(), ISSUE_TITLE, task_text="T\nB", model_route="code-strong"
        )

        assert llm.calls_for("implementer")[0]["tier"] == "code-strong"

    async def test_without_task_text_the_live_issue_is_read(self, fake_gitlab):
        llm, implementer = await self.make_implementer(fake_gitlab)

        await implementer.propose(self.make_run(), ISSUE_TITLE)

        assert llm.calls_for("implementer")[0]["user"].startswith(f"Issue title: {ISSUE_TITLE}")
        assert fake_gitlab.calls_of("get_issue")
