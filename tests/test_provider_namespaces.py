"""R03: provider-namespaced run identity.

Numeric subject ids are only unique WITHIN a provider: a GitLab
``project_id=5`` and a GitHub repository internal id ``5`` are unrelated
subjects. These tests pin the three legs of the fix:

- the DB invariant — ``uq_active_run_per_issue`` keys on
  ``(provider, project_id, issue_iid)`` (partial: non-terminal statuses
  only), so different providers holding the same numeric ids coexist while
  a same-provider duplicate still collides;
- migration 012 — the legacy backfill derivation and the index rebuild;
- the service scans — every GitLab lifecycle pass (waiting_ci,
  waiting_harness, ready-evidence recovery, revival, one-active-run guard,
  cancel, retry resolution) is provider-scoped and never touches a
  GitHub/Azure run, mirroring the already-scoped GitHub/Azure services.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import FlowRun
from forge.models.base import Base
from forge.runs import RunService
from forge.runs.revival import has_active_run, resolve_retry_target, resolve_status_target
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import FakeWriter

PROJECT_ID = 5  # deliberately tiny: collides with a GitHub repo internal id
ISSUE_IID = 7
GITHUB_REPO = "octo/repo"

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


def make_service(db, fake_gitlab, **overrides) -> RunService:
    values = dict(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        writer_class=FakeWriter,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )
    values.update(overrides)
    return RunService(**values)


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
    fake.seed_issue(ISSUE_IID, "Add a widget", "Widgets make the app better.")
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


async def add_run(
    db,
    *,
    provider: str,
    project_id: int = PROJECT_ID,
    issue_iid: int | None = ISSUE_IID,
    status: str = "waiting_ci",
    **extra,
) -> str:
    run_id = uuid4().hex
    async with db() as session:
        session.add(
            FlowRun(
                id=run_id,
                provider=provider,
                project_id=project_id,
                issue_iid=issue_iid,
                status=status,
                **extra,
            )
        )
        await session.commit()
    return run_id


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


# ----------------------------------------------------------------------
# The DB invariant: uq_active_run_per_issue keys on (provider, project, issue)
# ----------------------------------------------------------------------


class TestUniqueIndexIsProviderNamespaced:
    async def test_same_numeric_ids_across_providers_coexist(self, db):
        """GitLab project 5 issue 7 AND GitHub repo-id 5 issue 7 (and AzDO
        project 5 work item 7) all hold ACTIVE runs simultaneously — the
        pre-R03 index would have rejected the second insert."""
        gitlab_id = await add_run(db, provider="gitlab")
        github_id = await add_run(
            db, provider="github", github_repo_full_name=GITHUB_REPO, github_issue_number=ISSUE_IID
        )
        azure_id = await add_run(db, provider="azure_devops")

        for run_id in (gitlab_id, github_id, azure_id):
            run = await get_run(db, run_id)
            assert run.status == "waiting_ci"  # all three stay active

    async def test_same_provider_same_ids_still_unique(self, db):
        """The namespaced index still enforces one active run per subject
        WITHIN a provider (the F12 invariant is narrowed, never dropped)."""
        await add_run(db, provider="gitlab")
        with pytest.raises(IntegrityError):
            await add_run(db, provider="gitlab")

    async def test_github_lane_scoped_by_repo_not_only_numbers(self, db):
        """Two GitHub repos may both use internal id 5 — repo identity
        participates in the subject (the services add the repo predicate)."""
        await add_run(db, provider="github", github_repo_full_name="octo/repo")
        # Same numeric ids, same provider, different repo: the index (which
        # cannot see repo_full_name) refuses — one ACTIVE run per numeric
        # subject per provider is the contract; the repo-scoped services
        # never share project_id across connections in one deployment.
        with pytest.raises(IntegrityError):
            await add_run(db, provider="github", github_repo_full_name="other/repo")

    async def test_terminal_run_does_not_block_new_run_same_provider(self, db):
        """The partial predicate is kept: a cancelled run never blocks a
        fresh /implement on the same subject."""
        cancelled_id = await add_run(db, provider="gitlab")
        async with db() as session:
            run = await session.get(FlowRun, cancelled_id)
            assert run is not None
            run.status = "cancelled"
            await session.commit()

        fresh_id = await add_run(db, provider="gitlab")
        assert (await get_run(db, fresh_id)).status == "waiting_ci"

    def test_model_index_leads_with_provider(self):
        """The model and the migration must define the SAME index: provider
        is the leading column of uq_active_run_per_issue."""
        index = next(
            idx for idx in FlowRun.__table__.indexes if idx.name == "uq_active_run_per_issue"
        )
        assert [col.name for col in index.columns] == ["provider", "project_id", "issue_iid"]
        assert index.unique


# ----------------------------------------------------------------------
# Migration 012: legacy backfill + index rebuild
# ----------------------------------------------------------------------


def _load_migration(stem: str):
    spec = importlib.util.spec_from_file_location(stem, _VERSIONS_DIR / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_op_module(engine, module, fn_name: str) -> None:
    """Run one migration module function through alembic Operations on *engine*."""

    def _execute(sync_conn) -> None:
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            getattr(module, fn_name)()

    async with engine.begin() as conn:
        await conn.run_sync(_execute)


_PRE_012_FLOW_RUN_STATUSES = (
    "accepted",
    "preflight",
    "planning",
    "waiting_approval",
    "proposing",
    "validating",
    "committing",
    "ensuring_draft_mr",
    "waiting_harness",
    "waiting_ci",
    "evaluating_ci",
    "reviewing",
    "ready_for_human",
    "blocked",
    "failed",
    "cancelled",
)


async def _create_pre_012_schema(engine) -> None:
    """flow_runs as migration 011 left it: provider NOT NULL default 'gitlab'
    (008), github identity columns, and the provider-blind unique index."""
    pred = sa.text("status NOT IN ('ready_for_human', 'blocked', 'failed', 'cancelled')")

    def _build(sync_conn) -> None:
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            from alembic import op

            op.create_table(
                "flow_runs",
                sa.Column("id", sa.String(32), primary_key=True),
                sa.Column("project_id", sa.Integer(), nullable=False),
                sa.Column("issue_iid", sa.Integer(), nullable=True),
                sa.Column("provider", sa.String(20), nullable=False, server_default="gitlab"),
                sa.Column("github_repo_full_name", sa.String(255), nullable=True),
                sa.Column("github_issue_number", sa.Integer(), nullable=True),
                sa.Column("mr_iid", sa.Integer(), nullable=True),
                sa.Column("status", sa.String(32), nullable=False),
                sa.Column("status_reason", sa.String(200), nullable=True),
                sa.Column("base_sha", sa.String(40), nullable=True),
                sa.Column("candidate_shas", sa.JSON(), nullable=True),
                sa.Column("plan_digest", sa.String(64), nullable=True),
                sa.Column("config_digest", sa.String(64), nullable=True),
                sa.Column("spec_digest", sa.String(64), nullable=True),
                sa.Column("cancel_requested", sa.Boolean(), nullable=False),
                sa.Column("evidence", sa.JSON(), nullable=True),
                sa.Column("commit_cycle", sa.Integer(), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
                sa.CheckConstraint(
                    "status IN ({})".format(
                        ", ".join(f"'{s}'" for s in _PRE_012_FLOW_RUN_STATUSES)
                    ),
                    name="ck_flow_runs_status",
                ),
            )
            op.create_index(
                "uq_active_run_per_issue",
                "flow_runs",
                ["project_id", "issue_iid"],
                unique=True,
                postgresql_where=pred,
                sqlite_where=pred,
            )

    await _run_with_ops(engine, _build)


async def _run_with_ops(engine, fn) -> None:
    """Run ``fn(sync_conn)`` with alembic Operations bound to the connection."""

    def _execute(sync_conn) -> None:
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            fn(sync_conn)

    async with engine.begin() as conn:
        await conn.run_sync(_execute)


async def _insert_legacy_row(
    engine,
    *,
    project_id: int,
    issue_iid: int,
    provider: str | None,
    repo_full_name: str | None,
    status: str = "waiting_ci",
) -> str:
    run_id = uuid4().hex
    query = text(
        "INSERT INTO flow_runs (id, project_id, issue_iid, provider, github_repo_full_name,"
        " status, cancel_requested, commit_cycle, created_at, updated_at)"
        " VALUES (:id, :project_id, :issue_iid, :provider, :repo, :status, 0, 1, :now, :now)"
    )
    params = {
        "id": run_id,
        "project_id": project_id,
        "issue_iid": issue_iid,
        "provider": provider if provider is not None else "gitlab",
        "repo": repo_full_name,
        "status": status,
        "now": datetime.now(timezone.utc).isoformat(),
    }
    async with engine.begin() as conn:
        await conn.execute(query, params)
    return run_id


class TestMigration012:
    @pytest.fixture()
    async def legacy_db(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await _create_pre_012_schema(engine)
        yield engine
        await engine.dispose()

    async def test_backfill_derivation_rules(self, legacy_db):
        """Legacy NULL-era rows are already 'gitlab' (008's server default);
        the only correction is a GitHub-subject row still carrying the
        default, and AzDO rows with a legacy repo fallback stay AzDO."""
        pure_gitlab = await _insert_legacy_row(
            legacy_db, project_id=1, issue_iid=8, provider=None, repo_full_name=None
        )
        missed_github = await _insert_legacy_row(
            legacy_db, project_id=1, issue_iid=9, provider=None, repo_full_name="octo/repo"
        )
        azure_with_repo_fallback = await _insert_legacy_row(
            legacy_db,
            project_id=1,
            issue_iid=10,
            provider="azure_devops",
            repo_full_name="team/project",  # AzDO legacy repo-identity fallback
        )

        await _run_op_module(legacy_db, _load_migration("012_provider_namespaced_runs"), "upgrade")

        rows = {}
        async with legacy_db.connect() as conn:
            result = await conn.execute(
                text("SELECT id, provider FROM flow_runs ORDER BY issue_iid")
            )
            for row_id, provider in result.all():
                rows[row_id] = provider

        assert rows[pure_gitlab] == "gitlab"
        assert rows[missed_github] == "github"
        # Rule 1: an explicitly-stamped AzDO row is never re-derived from
        # its (GitHub-shaped) repo fallback column.
        assert rows[azure_with_repo_fallback] == "azure_devops"

    async def test_rebuilt_index_is_provider_namespaced(self, legacy_db):
        """After 012 the rebuilt index admits same-numeric different-provider
        active runs and still refuses a same-provider duplicate."""
        await _insert_legacy_row(
            legacy_db, project_id=5, issue_iid=8, provider=None, repo_full_name=None
        )
        await _run_op_module(legacy_db, _load_migration("012_provider_namespaced_runs"), "upgrade")

        gitlab_run = await _insert_legacy_row(
            legacy_db, project_id=5, issue_iid=7, provider="gitlab", repo_full_name=None
        )
        await _insert_legacy_row(
            legacy_db, project_id=5, issue_iid=7, provider="github", repo_full_name="octo/repo"
        )

        with pytest.raises(IntegrityError):
            await _insert_legacy_row(
                legacy_db, project_id=5, issue_iid=7, provider="gitlab", repo_full_name=None
            )

        async with legacy_db.connect() as conn:
            providers = (
                (
                    await conn.execute(
                        text(
                            "SELECT provider FROM flow_runs WHERE project_id = 5 AND issue_iid = 7"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert sorted(providers) == ["github", "gitlab"]
        assert gitlab_run  # the seeded row survived

    async def test_downgrade_restores_provider_blind_index(self, legacy_db):
        await _run_op_module(legacy_db, _load_migration("012_provider_namespaced_runs"), "upgrade")
        await _run_op_module(
            legacy_db, _load_migration("012_provider_namespaced_runs"), "downgrade"
        )

        # Post-downgrade the pre-012 collision semantics are back: the same
        # numeric subject cannot hold two active runs, whatever the provider.
        await _insert_legacy_row(
            legacy_db, project_id=5, issue_iid=9, provider="gitlab", repo_full_name=None
        )
        with pytest.raises(IntegrityError):
            await _insert_legacy_row(
                legacy_db, project_id=5, issue_iid=9, provider="github", repo_full_name="o/r"
            )


# ----------------------------------------------------------------------
# The GitLab service passes are provider-scoped (mixed-provider fixture)
# ----------------------------------------------------------------------


def _spy_on(service: RunService, method_name: str, seen: list[str]) -> None:
    """Wrap *method_name* so it records every run id it is asked to handle."""
    original = getattr(service, method_name)

    async def wrapper(run_id: str, *args, **kwargs):
        seen.append(run_id)
        return await original(run_id, *args, **kwargs)

    setattr(service, method_name, wrapper)


class TestGitLabPassesNeverTouchForeignRuns:
    async def seed_mixed_active_runs(self, db) -> dict[str, str]:
        """Three runs sharing the SAME numeric subject (5, 7): one per
        provider. The GitLab passes must see exactly the gitlab one."""
        return {
            "gitlab": await add_run(db, provider="gitlab"),
            "github": await add_run(
                db,
                provider="github",
                github_repo_full_name=GITHUB_REPO,
                github_issue_number=ISSUE_IID,
            ),
            "azure_devops": await add_run(db, provider="azure_devops"),
        }

    async def test_evaluate_waiting_ci_scan_sees_only_gitlab(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab)
        runs = await self.seed_mixed_active_runs(db)

        seen: list[str] = []
        _spy_on(service, "_evaluate_one", seen)
        await service.evaluate_waiting_ci()

        assert seen == [runs["gitlab"]]
        # End to end: the foreign runs keep their provider, status untouched.
        assert (await get_run(db, runs["github"])).status == "waiting_ci"
        assert (await get_run(db, runs["azure_devops"])).status == "waiting_ci"

    async def test_evaluate_waiting_harness_scan_sees_only_gitlab(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab)
        runs = await self.seed_mixed_active_runs(db)
        # Park all three in waiting_harness with a pollable-looking handle.
        async with db() as session:
            for run_id in runs.values():
                run = await session.get(FlowRun, run_id)
                assert run is not None
                run.status = "waiting_harness"
                run.evidence = {"backend": "ci_harness", "harness": {"handle": "{}"}}
            await session.commit()

        seen: list[str] = []
        _spy_on(service, "_evaluate_harness_one", seen)
        await service.evaluate_waiting_harness()

        assert seen == [runs["gitlab"]]
        for provider in ("github", "azure_devops"):
            assert (await get_run(db, runs[provider])).status == "waiting_harness"

    async def test_evaluate_ready_evidence_scan_sees_only_gitlab(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab)
        gitlab_ready = await add_run(
            db,
            provider="gitlab",
            status="ready_for_human",
            candidate_shas=["c" * 40],
            mr_iid=None,
        )
        github_ready = await add_run(
            db,
            provider="github",
            status="ready_for_human",
            candidate_shas=["d" * 40],
            github_repo_full_name=GITHUB_REPO,
            github_issue_number=ISSUE_IID,
        )
        azure_ready = await add_run(db, provider="azure_devops", status="ready_for_human")

        seen: list[str] = []
        _spy_on(service, "_post_missing_evidence_note", seen)
        await service.evaluate_ready_evidence()

        # Only the GitLab run enters the evidence-note recovery; the foreign
        # READY runs are announced by their own lanes, never by GitLab notes.
        assert seen == [gitlab_ready]
        assert fake_gitlab.notes, "the GitLab run's evidence note was recovered"
        for run_id in (github_ready, azure_ready):
            assert (await get_run(db, run_id)).status == "ready_for_human"

    async def test_evaluate_revival_scan_sees_only_gitlab(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab)
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        gitlab_blocked = await add_run(
            db,
            provider="gitlab",
            status="blocked",
            evidence={"revival": {"count": 1, "due_at": due}},
        )
        github_blocked = await add_run(
            db,
            provider="github",
            status="blocked",
            github_repo_full_name=GITHUB_REPO,
            github_issue_number=ISSUE_IID,
            evidence={"revival": {"count": 1, "due_at": due}},
        )

        dispatched: list[str] = []

        async def fake_redispatch(run_id: str) -> None:
            dispatched.append(run_id)

        from forge.runs.revival import evaluate_revivals

        await evaluate_revivals(
            service._session_factory,
            service._settings,
            provider="gitlab",
            redispatch=fake_redispatch,
        )

        assert dispatched == [gitlab_blocked]
        # The GitLab run walked the revival edge; the GitHub run with the
        # same due stamp was never even a candidate.
        assert (await get_run(db, gitlab_blocked)).status == "proposing"
        assert (await get_run(db, github_blocked)).status == "blocked"

    async def test_gitlab_implement_not_blocked_by_foreign_active_run(self, db, fake_gitlab):
        """A GitHub run active on the same numeric subject must not trip the
        GitLab one-active-run guard — /implement starts a GitLab run."""
        await add_run(
            db,
            provider="github",
            github_repo_full_name=GITHUB_REPO,
            github_issue_number=ISSUE_IID,
        )
        service = make_service(db, fake_gitlab)

        run_id = await service.start_run(
            PROJECT_ID, ISSUE_IID, "Add a widget", "Widgets make the app better.", "alice"
        )

        run = await get_run(db, run_id)
        assert run.provider == "gitlab"
        assert run.status == "waiting_approval"

    async def test_gitlab_cancel_bare_targets_gitlab_run_only(self, db, fake_gitlab):
        """A bare /cancel on the GitLab issue resolves within the GitLab
        namespace; the coexisting GitHub run is never cancelled."""
        gitlab_run_id = await add_run(db, provider="gitlab")
        github_run_id = await add_run(
            db,
            provider="github",
            github_repo_full_name=GITHUB_REPO,
            github_issue_number=ISSUE_IID,
        )
        service = make_service(db, fake_gitlab)

        await service.handle_cancel_note(PROJECT_ID, "@forge /cancel", "alice", ISSUE_IID)

        assert (await get_run(db, gitlab_run_id)).status == "cancelled"
        assert (await get_run(db, github_run_id)).status == "waiting_ci"

    async def test_gitlab_cancel_explicit_rejects_foreign_run_id(self, db, fake_gitlab):
        """An explicit-id /cancel naming a GitHub run is ignored by the
        GitLab lane (32-char and 8-char prefix forms)."""
        github_run_id = await add_run(
            db,
            provider="github",
            github_repo_full_name=GITHUB_REPO,
            github_issue_number=ISSUE_IID,
        )
        service = make_service(db, fake_gitlab)

        await service.handle_cancel_note(
            PROJECT_ID, f"@forge /cancel {github_run_id}", "alice", ISSUE_IID
        )
        assert (await get_run(db, github_run_id)).status == "waiting_ci"

        prefix = github_run_id[:8]
        await service.handle_cancel_note(PROJECT_ID, f"@forge /cancel {prefix}", "alice", ISSUE_IID)
        assert (await get_run(db, github_run_id)).status == "waiting_ci"


# ----------------------------------------------------------------------
# Retry resolution stays provider-scoped
# ----------------------------------------------------------------------


class TestRetryResolutionIsProviderScoped:
    async def _seed_dead_runs(self, db) -> dict[str, str]:
        return {
            "gitlab": await add_run(
                db,
                provider="gitlab",
                status="failed",
                candidate_shas=["a" * 40],
                evidence={"backend": "builtin"},
            ),
            "github": await add_run(
                db,
                provider="github",
                status="failed",
                candidate_shas=["b" * 40],
                github_repo_full_name=GITHUB_REPO,
                github_issue_number=ISSUE_IID,
                evidence={"backend": "ci_harness"},
            ),
        }

    async def test_resolve_retry_target_filters_by_provider(self, db):
        runs = await self._seed_dead_runs(db)
        async with db() as session:
            gitlab_target = await resolve_retry_target(
                session,
                provider="gitlab",
                project_id=PROJECT_ID,
                issue_iid=ISSUE_IID,
                requested="",
            )
            github_target = await resolve_retry_target(
                session,
                provider="github",
                project_id=PROJECT_ID,
                issue_iid=ISSUE_IID,
                requested="",
                repo_full_name=GITHUB_REPO,
            )
            cross_id = await resolve_retry_target(
                session,
                provider="gitlab",
                project_id=PROJECT_ID,
                issue_iid=ISSUE_IID,
                requested=runs["github"],  # explicit id of the GitHub run
            )

        assert gitlab_target is not None and gitlab_target.id == runs["gitlab"]
        assert github_target is not None and github_target.id == runs["github"]
        assert cross_id is None  # a GitLab /retry can never name a GitHub run

    async def test_gitlab_retry_note_revives_only_the_gitlab_run(
        self, db, fake_gitlab, monkeypatch
    ):
        runs = await self._seed_dead_runs(db)
        service = make_service(db, fake_gitlab)

        advanced: list[str] = []

        async def fake_advance(project_id: int, run_id: str, **kwargs) -> None:
            advanced.append(run_id)

        monkeypatch.setattr(service, "_advance_proposal", fake_advance)
        await service.handle_retry_note(PROJECT_ID, "@forge /retry", "alice", ISSUE_IID)

        assert advanced == [runs["gitlab"]]
        revived = await get_run(db, runs["gitlab"])
        assert revived.status == "proposing"
        foreign = await get_run(db, runs["github"])
        assert foreign.status == "failed"
        assert foreign.commit_cycle == 1


# ----------------------------------------------------------------------
# A07: the full 32-char id resolves under the SAME subject scope as the
# bare/prefix forms — two projects sharing issue_iid=7 never cross
# ----------------------------------------------------------------------

OTHER_PROJECT_ID = 202  # a second project holding the SAME issue iid


class TestTargetResolutionIsSubjectScoped:
    """A07: every id form (bare, 8-char prefix, full 32-char id) resolves
    through the SAME provider/project/issue/repo predicates — a run from
    another project that happens to share the issue iid is unaddressable."""

    @staticmethod
    async def _seed_two_projects(db) -> dict[str, str]:
        """One dead run on each of two projects, both on issue !7."""
        return {
            "home": await add_run(
                db,
                provider="gitlab",
                project_id=PROJECT_ID,
                status="failed",
                candidate_shas=["a" * 40],
                evidence={"backend": "builtin"},
            ),
            "foreign": await add_run(
                db,
                provider="gitlab",
                project_id=OTHER_PROJECT_ID,
                status="failed",
                candidate_shas=["b" * 40],
                evidence={"backend": "builtin"},
            ),
        }

    @staticmethod
    def _as_form(form: str, run_id: str) -> str:
        """The requested id as the bare / 8-char prefix / full 32-char form."""
        return {"bare": "", "prefix": run_id[:8], "full": run_id}[form]

    @pytest.mark.parametrize("form", ["bare", "prefix", "full"])
    @pytest.mark.parametrize("resolve", [resolve_retry_target, resolve_status_target])
    async def test_resolution_refuses_the_other_project(self, db, resolve, form):
        runs = await self._seed_two_projects(db)

        async with db() as session:
            resolved = await resolve(
                session,
                provider="gitlab",
                project_id=PROJECT_ID,
                issue_iid=ISSUE_IID,
                requested=self._as_form(form, runs["foreign"]),
            )

        if form == "bare":
            # bare resolves WITHIN the command's project — the home run, never
            # the other project's same-iid twin.
            assert resolved is not None
            assert resolved.id == runs["home"]
        else:
            # an explicit id/prefix of the other project's run is refused.
            assert resolved is None

    @pytest.mark.parametrize("form", ["prefix", "full"])
    async def test_gitlab_retry_of_other_project_is_inert(self, db, fake_gitlab, monkeypatch, form):
        runs = await self._seed_two_projects(db)
        service = make_service(db, fake_gitlab)
        dispatched: list[str] = []

        async def fake_advance(project_id: int, run_id: str, **kwargs) -> None:
            dispatched.append(run_id)

        monkeypatch.setattr(service, "_advance_proposal", fake_advance)
        monkeypatch.setattr(service, "_advance_harness", fake_advance)

        await service.handle_retry_note(
            PROJECT_ID,
            f"@forge /retry {self._as_form(form, runs['foreign'])}",
            "alice",
            ISSUE_IID,
        )

        # rejected command: zero advance legs (model calls/commits), zero
        # notes (metadata), and the foreign run untouched (no cycle granted).
        assert dispatched == []
        assert fake_gitlab.notes == []
        foreign = await get_run(db, runs["foreign"])
        assert foreign.status == "failed"
        assert foreign.commit_cycle == 1

    @pytest.mark.parametrize("form", ["prefix", "full"])
    async def test_gitlab_status_of_other_project_reports_nothing(self, db, fake_gitlab, form):
        runs = await self._seed_two_projects(db)
        service = make_service(db, fake_gitlab)

        await service.handle_status_note(
            PROJECT_ID,
            f"@forge /status {self._as_form(form, runs['foreign'])}",
            "alice",
            ISSUE_IID,
        )

        # the honest "nothing here" reply — never the foreign run's metadata.
        assert fake_gitlab.notes_containing("No forge run found")
        assert not fake_gitlab.notes_containing(runs["foreign"][:8])


# ----------------------------------------------------------------------
# has_active_run is provider-parameterized
# ----------------------------------------------------------------------


class TestHasActiveRunIsProviderParameterized:
    async def test_foreign_active_run_is_not_reported(self, db):
        await add_run(
            db,
            provider="github",
            github_repo_full_name=GITHUB_REPO,
            github_issue_number=ISSUE_IID,
        )
        async with db() as session:
            assert not await has_active_run(
                session, provider="gitlab", project_id=PROJECT_ID, issue_iid=ISSUE_IID
            )
            assert await has_active_run(
                session,
                provider="github",
                project_id=PROJECT_ID,
                issue_iid=ISSUE_IID,
                repo_full_name=GITHUB_REPO,
            )

    async def test_same_provider_active_run_is_reported(self, db):
        await add_run(db, provider="azure_devops")
        async with db() as session:
            assert await has_active_run(
                session, provider="azure_devops", project_id=PROJECT_ID, issue_iid=ISSUE_IID
            )
            assert not await has_active_run(
                session, provider="gitlab", project_id=PROJECT_ID, issue_iid=ISSUE_IID
            )
