"""A13: a config-read failure must never WIDEN a run's scope.

The project config (``.forge.yml``) is the authority a run's path scope
comes from. These tests pin the A13 contract end to end:

- the typed read (:class:`~forge.orchestrator.project_config.ConfigReadResult`)
  distinguishes ``confirmed_absent`` / ``valid`` / ``unreadable`` / ``invalid``
  (the R14 BlobReadResult pattern, reused over HTTP);
- only a provider-confirmed absence earns the documented default profile;
  an unreadable (403/timeout/…) or invalid (malformed YAML) config parks
  the run ``blocked(config_…)`` with ZERO paid calls and ZERO commits;
- the executable RunSpec freezes the config provenance (status, ref,
  content sha256) so restarts validate against the approved snapshot;
- permissions may narrow, never widen: a post-freeze leg scopes from the
  FROZEN ``allowed_paths`` even when the live config becomes unreadable;
- the reconciler's config-recovery pass retries the read and re-enters
  planning (through the fenced plan-restart edge) once it recovers.
"""

import base64
import hashlib
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import FlowRun, FlowStatus, Outbox, RunSpec
from forge.durable.controller import Controller, InvalidTransition
from forge.gitlab.client import GitLabAPIError
from forge.models.base import Base
from forge.orchestrator.project_config import (
    CONFIG_FILE,
    ConfigReadResult,
    ProjectConfig,
    clear_cache,
    load_project_config,
    read_project_config,
)
from forge.runs.spec import (
    EXECUTABLE_SPEC_SCHEMA_VERSION,
    ExecutableRunSpec,
    SpecInvalid,
    canonical_json_digest,
)
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import (
    ISSUE_DESC,
    ISSUE_IID,
    ISSUE_TITLE,
    PROJECT_ID,
    FakeWriter,
    make_service,
)

RESTRICTED_YAML = "implement:\n  paths:\n    - 'services/**'\n"
MALFORMED_YAML = "not: a: valid: [[["


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """The module-level config cache is process-wide — isolate every test."""
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


class FlakyGitLab(FakeGitLab):
    """FakeGitLab that 403s `.forge.yml` reads until disarmed.

    Subclassing (not monkeypatching) keeps the R14 routing honest: the
    fake's ``read_blob`` classifies the raised 403 exactly like the real
    adapter.
    """

    def __init__(self) -> None:
        super().__init__()
        self.armed = True

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD"):
        if self.armed and file_path == CONFIG_FILE:
            raise GitLabAPIError(403, "read forbidden for .forge.yml")
        return await super().get_file(project_id, file_path, ref)


@pytest.fixture()
def fake_gitlab() -> FlakyGitLab:
    fake = FlakyGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


class BoomPlanner:
    """Must never be constructed a prompt — a blocked run costs nothing."""

    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("planner ran for a config-blocked run")


class CountingPlanner:
    """Counts planning calls; a stub plan otherwise."""

    def __init__(self) -> None:
        self.calls = 0
        self.path_scopes: list[list[str] | None] = []

    async def plan(self, issue_title, issue_description, *, flow_run_id=None, path_scope=None):
        self.calls += 1
        self.path_scopes.append(path_scope)
        return "## Implementation plan\n\n- create a widget\n"


def make_flaky_service(db, fake_gitlab, *, planner=None):
    FakeWriter.reset()
    return make_service(db, fake_gitlab, planner=planner or BoomPlanner())


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def get_spec(db, run_id: str) -> RunSpec | None:
    async with db() as session:
        return (
            (
                await session.execute(
                    select(RunSpec)
                    .where(RunSpec.run_id == run_id)
                    .order_by(RunSpec.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )


def forge_yml_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Unit: the typed config read
# ----------------------------------------------------------------------


class TestReadProjectConfig:
    """The four statuses of the typed `.forge.yml` read (A13 §1)."""

    async def test_confirmed_404_is_the_only_absence(self):
        client = FakeGitLab()  # no .forge.yml seeded
        result = await read_project_config(client, PROJECT_ID)
        assert result.status == "confirmed_absent"
        assert result.needs_block is False
        assert result.blocked_reason is None

    async def test_valid_read_carries_config_and_provenance(self):
        client = FakeGitLab()
        client.seed_file(CONFIG_FILE, RESTRICTED_YAML)
        result = await read_project_config(client, PROJECT_ID, ref="main")
        assert result.status == "valid"
        assert result.needs_block is False
        assert result.config is not None
        assert result.config.implement_paths == ["services/**"]
        assert result.content_sha256 == forge_yml_digest(RESTRICTED_YAML)
        assert result.ref == "main"

    async def test_403_is_unreadable_and_blocks(self):
        fake = FlakyGitLab()
        fake.seed_file(CONFIG_FILE, RESTRICTED_YAML)
        result = await read_project_config(fake, PROJECT_ID)
        assert result.status == "unreadable"
        assert result.needs_block is True
        assert result.blocked_reason is not None
        assert result.blocked_reason.startswith("config_unreadable:")
        assert result.config is None

    async def test_timeout_is_unreadable(self):
        class TimeoutGitLab(FakeGitLab):
            async def get_file(self, project_id, file_path, ref="HEAD"):
                raise httpx.ReadTimeout("read timed out")

        result = await read_project_config(TimeoutGitLab(), PROJECT_ID)
        assert result.status == "unreadable"
        assert "timed out" in (result.blocked_reason or "")

    async def test_malformed_yaml_is_invalid_not_defaults(self):
        client = FakeGitLab()
        client.seed_file(CONFIG_FILE, MALFORMED_YAML)
        result = await read_project_config(client, PROJECT_ID)
        assert result.status == "invalid"
        assert result.needs_block is True
        assert result.blocked_reason.startswith("config_invalid:")

    async def test_non_mapping_yaml_is_invalid(self):
        client = FakeGitLab()
        client.seed_file(CONFIG_FILE, "- just\n- a\n- list\n")
        result = await read_project_config(client, PROJECT_ID)
        assert result.status == "invalid"

    async def test_empty_file_is_confirmed_absent(self):
        client = FakeGitLab()
        client.seed_file(CONFIG_FILE, "   \n")
        result = await read_project_config(client, PROJECT_ID)
        assert result.status == "confirmed_absent"

    async def test_get_file_only_client_failing_is_unreadable(self):
        """A client without the typed read never gets 404-vouched absence:
        an unclassifiable raise is `unavailable` — conservative (A13)."""

        class LegacyClient:
            async def get_file(self, project_id, file_path, ref="HEAD"):
                raise RuntimeError("404 Not Found")  # not a typed error

        result = await read_project_config(LegacyClient(), PROJECT_ID)
        assert result.status == "unreadable"

    async def test_get_file_only_client_success_is_valid(self):
        class LegacyClient:
            async def get_file(self, project_id, file_path, ref="HEAD"):
                from forge.gitlab.schemas import RepositoryFile

                return RepositoryFile.model_validate(
                    {
                        "file_name": file_path,
                        "file_path": file_path,
                        "size": len(RESTRICTED_YAML),
                        "encoding": "base64",
                        "content": base64.b64encode(RESTRICTED_YAML.encode()).decode("ascii"),
                        "ref": ref,
                    }
                )

        result = await read_project_config(LegacyClient(), PROJECT_ID)
        assert result.status == "valid"
        assert result.content_sha256 == forge_yml_digest(RESTRICTED_YAML)

    async def test_invalid_reads_are_never_cached(self):
        client = FakeGitLab()
        client.seed_file(CONFIG_FILE, MALFORMED_YAML)
        first = await read_project_config(client, PROJECT_ID)
        assert first.status == "invalid"
        client.seed_file(CONFIG_FILE, RESTRICTED_YAML)  # fixed between reads
        second = await read_project_config(client, PROJECT_ID)
        assert second.status == "valid"

    async def test_valid_reads_are_cached_with_provenance(self):
        client = FakeGitLab()
        client.seed_file(CONFIG_FILE, RESTRICTED_YAML)
        first = await read_project_config(client, PROJECT_ID, ref="main")
        client.files.pop(CONFIG_FILE)  # a later read failure must not matter
        second = await read_project_config(client, PROJECT_ID, ref="main")
        assert second == first

    async def test_legacy_wrapper_still_degrades_to_defaults(self):
        """`load_project_config` keeps the legacy review-lane semantics."""
        fake = FlakyGitLab()
        config = await load_project_config(fake, PROJECT_ID)
        assert isinstance(config, ProjectConfig)
        assert config.implement_paths == []


# ----------------------------------------------------------------------
# GitLab start path: the config gate
# ----------------------------------------------------------------------


class TestConfigGateStartPath:
    """A failed config read parks the run — zero paid calls, zero commits."""

    async def test_403_parks_config_unreadable_before_any_paid_call(self, db, fake_gitlab):
        fake_gitlab.seed_file(CONFIG_FILE, RESTRICTED_YAML)  # restricted config…
        planner = BoomPlanner()
        service = make_flaky_service(db, fake_gitlab, planner=planner)  # …but unreadable

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_unreadable:")
        # Zero paid calls, zero commits, no spec, no gate.
        assert planner.calls == 0
        assert FakeWriter.instances == []
        assert await get_spec(db, run_id) is None
        # The stash lets the recovery pass re-plan with the same input.
        assert run.evidence["config_block"]["issue_title"] == ISSUE_TITLE
        assert run.evidence["config_block"]["issue_description"] == ISSUE_DESC
        # Operator-facing note, journaled.
        notes = fake_gitlab.notes_containing("config_unreadable")
        assert len(notes) == 1
        assert "never widens" in notes[0]["body"]

    async def test_timeout_parks_config_unreadable(self, db, fake_gitlab):
        class TimeoutGitLab(FlakyGitLab):
            async def get_file(self, project_id, file_path, ref="HEAD"):
                if file_path == CONFIG_FILE:
                    raise httpx.ReadTimeout("read timed out")
                return await super().get_file(project_id, file_path, ref)

        fake = TimeoutGitLab()
        fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
        fake.seed_commit("main", "base-sha-1", "initial")
        service = make_flaky_service(db, fake)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_unreadable:")

    async def test_malformed_yaml_parks_config_invalid(self, db, fake_gitlab):
        fake_gitlab.armed = False
        fake_gitlab.seed_file(CONFIG_FILE, MALFORMED_YAML)
        service = make_flaky_service(db, fake_gitlab)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_invalid:")
        assert FakeWriter.instances == []

    async def test_confirmed_404_runs_the_documented_default_profile(self, db, fake_gitlab):
        """No config at all: the empty scope is HONEST — the project has no
        config, and the absence is proven (and frozen as provenance)."""
        fake_gitlab.armed = False
        planner = CountingPlanner()
        service = make_flaky_service(db, fake_gitlab, planner=planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert planner.calls == 1
        assert planner.path_scopes == [None]  # unscoped plan prompt
        spec = await get_spec(db, run_id)
        assert spec is not None
        assert "allowed_paths" not in spec.document
        assert spec.document["project_config"]["status"] == "confirmed_absent"
        assert spec.document["project_config"]["ref"] == "main"

    async def test_valid_config_freezes_scope_and_provenance(self, db, fake_gitlab):
        fake_gitlab.armed = False
        fake_gitlab.seed_file(CONFIG_FILE, RESTRICTED_YAML)
        planner = CountingPlanner()
        service = make_flaky_service(db, fake_gitlab, planner=planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        spec = await get_spec(db, run_id)
        assert spec is not None
        assert spec.document["allowed_paths"] == ["services/**"]
        provenance = spec.document["project_config"]
        assert provenance["status"] == "valid"
        assert provenance["ref"] == "main"
        assert provenance["sha256"] == forge_yml_digest(RESTRICTED_YAML)
        # The frozen document is self-consistent (the gate binds THIS digest).
        assert spec.digest == canonical_json_digest(spec.document)
        assert planner.path_scopes == [["services/**"]]

    async def test_restart_scopes_from_the_frozen_spec_not_the_live_config(self, db, fake_gitlab):
        """A restarted worker reads the approved snapshot: a fresh service
        instance (same DB) loads the same frozen allowed_paths, and the
        provenance digest validates against the stored document."""
        fake_gitlab.armed = False
        fake_gitlab.seed_file(CONFIG_FILE, RESTRICTED_YAML)
        service = make_flaky_service(db, fake_gitlab, planner=CountingPlanner())
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        # "Restart": a brand-new service instance over the same DB.
        restarted = make_flaky_service(db, fake_gitlab, planner=CountingPlanner())
        spec = await restarted._load_executable_spec(run_id)
        assert spec.allowed_paths == ("services/**",)
        assert spec.config_status == "valid"
        assert spec.config_sha256 == forge_yml_digest(RESTRICTED_YAML)


class TestPermissionsNeverWiden:
    """A13 §3: post-freeze legs scope from the spec, never the live config."""

    async def test_frozen_scope_survives_an_unreadable_config(self, db, fake_gitlab):
        """Scoped at freeze, unreadable at `/go`: the advance leg neither
        re-reads the config nor widens — the FROZEN allowed_paths still
        reject an out-of-scope candidate."""
        from forge.repository import Change, ChangeSet, Operation
        from forge.runs.stubs import StubImplementer, factory_branch

        fake_gitlab.armed = False
        fake_gitlab.seed_file(CONFIG_FILE, RESTRICTED_YAML)
        service = make_flaky_service(db, fake_gitlab, planner=CountingPlanner())
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value

        def config_reads() -> int:
            return len([c for c in fake_gitlab.calls_of("get_file") if c[1][1] == CONFIG_FILE])

        config_reads_before = config_reads()
        fake_gitlab.armed = True  # the live config is now unreadable

        class OutOfScopeImplementer(StubImplementer):
            async def propose(self, run, issue_title, **kwargs):
                return ChangeSet(
                    branch=factory_branch(run.issue_iid, run.id),
                    commit_message=f"forge: implement {run.issue_iid or 0}",
                    changes=[
                        Change(
                            path="webapp/out-of-scope.ts",
                            operation=Operation.CREATE,
                            content="// outside the frozen scope\n",
                        )
                    ],
                )

        service._implementer = OutOfScopeImplementer()
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "outside the allowed scope" in (run.status_reason or "")
        # The advance leg never touched the live config.
        assert config_reads() == config_reads_before


class TestConfigRecovery:
    """The reconciler retries the read; recovered → the run proceeds."""

    async def test_recovered_read_re_enters_planning(self, db, fake_gitlab):
        service = make_flaky_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        assert (await get_run(db, run_id)).status == FlowStatus.BLOCKED.value

        # The config becomes readable — restricted, as the project meant it.
        fake_gitlab.armed = False
        fake_gitlab.seed_file(CONFIG_FILE, RESTRICTED_YAML)
        planner = CountingPlanner()
        service._planner = planner
        await service.evaluate_config_recovery()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert planner.calls == 1  # exactly one paid call, after recovery
        assert planner.path_scopes == [["services/**"]]
        spec = await get_spec(db, run_id)
        assert spec is not None
        assert spec.document["project_config"]["status"] == "valid"
        # The parked-state evidence was consumed into the normal flow.
        assert run.evidence["config_block"]["issue_title"] == ISSUE_TITLE

    async def test_still_failing_read_leaves_the_run_parked(self, db, fake_gitlab):
        service = make_flaky_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        assert (await get_run(db, run_id)).status == FlowStatus.BLOCKED.value

        await service.evaluate_config_recovery()  # still 403ing

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_unreadable:")

    async def test_recovered_absence_proceeds_on_the_documented_default(self, db, fake_gitlab):
        """The config was deleted while blocked: absence is provider-
        confirmed, so the documented default profile is honest."""
        service = make_flaky_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        assert (await get_run(db, run_id)).status == FlowStatus.BLOCKED.value

        fake_gitlab.armed = False  # no .forge.yml seeded → confirmed 404
        planner = CountingPlanner()
        service._planner = planner
        await service.evaluate_config_recovery()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        spec = await get_spec(db, run_id)
        assert spec is not None
        assert "allowed_paths" not in spec.document
        assert spec.document["project_config"]["status"] == "confirmed_absent"


# ----------------------------------------------------------------------
# Unit: the frozen provenance in the executable spec
# ----------------------------------------------------------------------


class TestSpecConfigProvenance:
    """A13 §4: the spec pins the approved config snapshot."""

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

    def test_valid_provenance_round_trips(self):
        spec = self.make_spec(
            allowed_paths=("services/**",),
            config_status="valid",
            config_ref="main",
            config_sha256=forge_yml_digest(RESTRICTED_YAML),
        )
        parsed = ExecutableRunSpec.from_document(spec.to_document())
        assert parsed == spec
        document = spec.to_document()
        assert document["project_config"] == {
            "status": "valid",
            "ref": "main",
            "sha256": forge_yml_digest(RESTRICTED_YAML),
        }

    def test_confirmed_absent_provenance_round_trips_without_a_digest(self):
        spec = self.make_spec(config_status="confirmed_absent", config_ref="main")
        parsed = ExecutableRunSpec.from_document(spec.to_document())
        assert parsed == spec
        assert spec.to_document()["project_config"] == {
            "status": "confirmed_absent",
            "ref": "main",
        }

    def test_pre_a13_documents_stay_parseable(self):
        """A stored document without the section parses with empty
        provenance — the additive convention is byte-compatible."""
        spec = self.make_spec()
        assert "project_config" not in spec.to_document()
        assert ExecutableRunSpec.from_document(spec.to_document()).config_status == ""

    def test_valid_provenance_requires_a_real_digest(self):
        with pytest.raises(SpecInvalid, match="config_sha256"):
            self.make_spec(config_status="valid", config_ref="main", config_sha256="nope")

    def test_valid_provenance_requires_a_ref(self):
        with pytest.raises(SpecInvalid, match="read ref"):
            self.make_spec(
                config_status="valid",
                config_ref="",
                config_sha256=forge_yml_digest(RESTRICTED_YAML),
            )

    def test_absent_config_has_no_digest(self):
        with pytest.raises(SpecInvalid, match="no content digest"):
            self.make_spec(
                config_status="confirmed_absent",
                config_ref="main",
                config_sha256=forge_yml_digest(RESTRICTED_YAML),
            )

    def test_unknown_status_is_rejected(self):
        with pytest.raises(SpecInvalid, match="config_status"):
            self.make_spec(config_status="guessed", config_ref="main")

    def test_tampered_provenance_fails_the_verified_read(self):
        """The provenance is part of the digest-bound document: tampering
        with the approved config snapshot cannot survive a verified read."""
        from forge.runs.spec import load_verified_spec

        spec = self.make_spec(
            allowed_paths=("services/**",),
            config_status="valid",
            config_ref="main",
            config_sha256=forge_yml_digest(RESTRICTED_YAML),
        )
        document = spec.to_document()
        digest = canonical_json_digest(document)
        document["project_config"]["sha256"] = "c" * 64  # a different snapshot
        with pytest.raises(SpecInvalid, match="digest mismatch"):
            load_verified_spec(document=document, digest=digest, run_spec_digest=digest)


# ----------------------------------------------------------------------
# Unit: the fenced plan-restart edge
# ----------------------------------------------------------------------


class TestRestartPlanTransition:
    """The only walk from `blocked` back to planning is fenced."""

    async def test_blocked_pre_gate_run_walks_to_preflight(self, db):
        run_id = uuid4().hex
        async with db() as session:
            controller = Controller(session)
            session.add(FlowRun(id=run_id, project_id=PROJECT_ID, issue_iid=ISSUE_IID))
            await controller.transition(run_id, FlowStatus.PREFLIGHT)
            await controller.transition(run_id, FlowStatus.BLOCKED, reason="config_unreadable: x")
            await session.commit()

        async with db() as session:
            await session.get(FlowRun, run_id)
            await Controller(session).restart_plan_transition(
                run_id, reason="recovered", authorized_by="config_recovery"
            )
            await session.commit()
        refreshed = await get_run(db, run_id)
        assert refreshed.status == FlowStatus.PREFLIGHT.value
        assert refreshed.spec_digest in ("", None)
        # The walk is journaled like the revival edge.
        async with db() as session:
            hops = (
                (
                    await session.execute(
                        select(Outbox)
                        .where(Outbox.flow_run_id == run_id)
                        .order_by(Outbox.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .all()
            )
        assert hops[0].payload["to"] == FlowStatus.PREFLIGHT.value
        assert hops[0].payload["revival"] == "config_recovery"

    async def test_post_freeze_run_refuses_the_plan_restart(self, db):
        """A run with a frozen spec may only revive FORWARD — an approved
        input is never re-planned past its gate."""
        run_id = uuid4().hex
        async with db() as session:
            controller = Controller(session)
            session.add(
                FlowRun(
                    id=run_id,
                    project_id=PROJECT_ID,
                    issue_iid=ISSUE_IID,
                    spec_digest="d" * 64,
                )
            )
            await controller.transition(run_id, FlowStatus.PREFLIGHT)
            await controller.transition(run_id, FlowStatus.BLOCKED, reason="config_unreadable: x")
            await session.commit()

        async with db() as session:
            with pytest.raises(InvalidTransition, match="frozen spec"):
                await Controller(session).restart_plan_transition(
                    run_id, reason="recovered", authorized_by="config_recovery"
                )

    async def test_non_blocked_run_refuses_the_plan_restart(self, db):
        run_id = uuid4().hex
        async with db() as session:
            controller = Controller(session)
            session.add(FlowRun(id=run_id, project_id=PROJECT_ID, issue_iid=ISSUE_IID))
            await controller.transition(run_id, FlowStatus.PREFLIGHT)
            await session.commit()

        async with db() as session:
            with pytest.raises(InvalidTransition, match="requires 'blocked'"):
                await Controller(session).restart_plan_transition(
                    run_id, reason="recovered", authorized_by="config_recovery"
                )


# ----------------------------------------------------------------------
# Unit: the typed result contract
# ----------------------------------------------------------------------


class TestConfigReadResultContract:
    """The result type is honest by construction (R14 mirror)."""

    def test_valid_requires_a_config_object(self):
        with pytest.raises(ValueError, match="must carry a ProjectConfig"):
            ConfigReadResult.valid(None, ref="main", content_sha256="a" * 64)  # type: ignore[arg-type]

    def test_valid_requires_a_digest(self):
        with pytest.raises(ValueError, match="sha256"):
            ConfigReadResult.valid(ProjectConfig(), ref="main", content_sha256="short")

    def test_non_valid_status_must_not_carry_content(self):
        with pytest.raises(ValueError, match="must not carry"):
            ConfigReadResult(status="unreadable", config=ProjectConfig())

    def test_unknown_status_rejected(self):
        with pytest.raises(ValueError, match="unknown config read status"):
            ConfigReadResult(status="guessed")  # type: ignore[arg-type]

    def test_provenance_status_is_empty_only_for_failures(self):
        assert (
            ConfigReadResult.valid(
                ProjectConfig(), ref="main", content_sha256="a" * 64
            ).provenance_status
            == "valid"
        )
        assert ConfigReadResult.confirmed_absent(ref="main").provenance_status == (
            "confirmed_absent"
        )
        assert ConfigReadResult.unreadable(detail="x").provenance_status == ""
        assert ConfigReadResult.invalid(detail="x").provenance_status == ""


def test_schema_version_still_v3():
    """A13 is additive: the executable schema stays v3."""
    assert EXECUTABLE_SPEC_SCHEMA_VERSION == 3
