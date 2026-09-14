"""The /security durable triage step: dispatch, agent run, comment, write-back.

Covers the provider-neutral executor (GitLab issue/MR + GitHub issue/PR),
the 50-finding batch cap, the remote-dismiss opt-in with the research §4.2
enum quirks, and the gateway → durable step wiring.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.agents.models import SecurityFindingResult, SecurityTriageResult
from forge.config import Settings
from forge.database import reset_engine
from forge.durable import StepRun
from forge.findings.ingest import NormalizedFinding, upsert_findings
from forge.findings.models import SecurityFinding
from forge.findings.triage import (
    TRIAGE_BATCH,
    execute_security_command,
    format_triage_comment,
)
from forge.main import create_app
from forge.models.base import Base
from forge.worker.steps import command_source_event_id
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_gitlab import FakeGitLab, FakeGitLabClientFactory
from tests.test_findings import code_alert

PROJECT_ID = 42
ISSUE_IID = 5
MR_IID = 3

TEST_SECRET = "test-secret-token"  # noqa: S105 — fake value for tests


def run_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_SECRET),
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/forge.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
    )
    values.update(overrides)
    return Settings(**values)


def _settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_SECRET),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
    )
    values.update(overrides)
    return Settings(**values)


def verdict(
    fingerprint: str, *, false_positive: bool = False, severity: str = "high"
) -> SecurityFindingResult:
    return SecurityFindingResult(
        id=fingerprint,
        severity=severity,
        category="testing",
        description="d",
        remediation="rotate the credential",
        is_false_positive=false_positive,
        justification=(
            "test value, not a real credential" if false_positive else "reachable from user input"
        ),
    )


async def seed_findings(db_factory, scope: str = str(PROJECT_ID)) -> dict[str, str]:
    """Seed one finding per severity; returns fingerprints keyed by severity."""
    specs = [
        ("critical", "c" * 64, "Hardcoded secret", "app/config.py", 12),
        ("high", "a" * 64, "SQL injection", "app/db.py", 7),
        ("medium", "e" * 64, "Weak hash", "app/hash.py", 3),
        ("low", "b" * 64, "Verbose logging", "app/log.py", 1),
    ]
    fingerprints: dict[str, str] = {}
    for severity, fingerprint, title, path, line in specs:
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [
                        NormalizedFinding(
                            source="gitlab_sast",
                            fingerprint=fingerprint,
                            severity=severity,
                            title=title,
                            path=path,
                            line=line,
                            identifiers=[{"type": "cwe", "value": "CWE-000"}],
                        )
                    ],
                    provider="gitlab",
                    scope=scope,
                )
        fingerprints[severity] = fingerprint
    return fingerprints


@pytest.fixture()
async def db_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def new_db_factory() -> async_sessionmaker:
    """A fresh in-memory store — for tests that need several isolated ones."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


# ----------------------------------------------------------------------
# GitLab path
# ----------------------------------------------------------------------


class TestSecurityCommandGitLab:
    @pytest.fixture()
    def fake_gitlab(self):
        return FakeGitLab()  # no pipelines — the refresh leg skips silently

    async def test_triage_comment_grouped_by_severity_with_writeback(self, db_factory, fake_gitlab):
        fps = await seed_findings(db_factory)

        async def runner(rows):
            assert len(rows) == 4
            return SecurityTriageResult(
                summary="One real secret, three lower-severity issues.",
                risk_level="high",
                findings=[
                    verdict(fps["critical"]),
                    verdict(fps["high"], false_positive=True),
                    verdict(fps["medium"]),
                    verdict(fps["low"], false_positive=True),
                ],
            )

        outcome = await execute_security_command(
            _settings(),
            None,
            db_factory,
            {
                "command": "security_triage",
                "project_id": PROJECT_ID,
                "issue_iid": ISSUE_IID,
                "author_username": "alice",
            },
            gitlab=fake_gitlab,
            triage_runner=runner,
        )

        assert outcome["considered"] == 4
        assert outcome["triaged"] == 2
        assert outcome["false_positives"] == 2
        assert outcome["comment_posted"] is True
        notes = fake_gitlab.notes
        assert len(notes) == 1
        body = notes[0]["body"]
        # Grouped by severity, each finding with its fingerprint.
        assert "Critical" in body and "Medium" in body and "Low" in body
        for fingerprint in fps.values():
            assert fingerprint[:12] in body
        assert "rotate the credential" in body
        assert "test value, not a real credential" in body
        # Write-back: confirmed → triaged, false positives → false_positive.
        async with db_factory() as session:
            rows = {
                row.fingerprint: row
                for row in (await session.execute(select(SecurityFinding))).scalars().all()
            }
        assert rows[fps["critical"]].status == "triaged"
        assert rows[fps["high"]].status == "false_positive"
        assert rows[fps["high"]].triage_note == "test value, not a real credential"

    async def test_pipeline_artifacts_are_ingested_before_triage(self, db_factory, fake_gitlab):
        from tests.test_findings import seed_sast_pipeline

        seed_sast_pipeline(fake_gitlab, pipeline_id=700, job_id=701)
        seen: list[list[str]] = []

        async def runner(rows):
            seen.append([row.title for row in rows])
            return SecurityTriageResult(
                summary="s",
                risk_level="medium",
                findings=[verdict(row.fingerprint) for row in rows],
            )

        outcome = await execute_security_command(
            _settings(),
            None,
            db_factory,
            {"command": "security_triage", "project_id": PROJECT_ID, "issue_iid": ISSUE_IID},
            gitlab=fake_gitlab,
            triage_runner=runner,
        )
        assert seen == [["Hardcoded secret", "Weak hash"]]
        assert outcome["refresh_created"] == 2
        assert outcome["considered"] == 2

    async def test_mr_note_target(self, db_factory, fake_gitlab):
        mr = await fake_gitlab.create_merge_request(PROJECT_ID, "feature", "main", "An MR")
        await seed_findings(db_factory)

        async def runner(rows):
            return SecurityTriageResult(summary="s", findings=[])

        await execute_security_command(
            _settings(),
            None,
            db_factory,
            {
                "command": "security_triage",
                "project_id": PROJECT_ID,
                "issue_iid": None,
                "mr_iid": mr["iid"],
            },
            gitlab=fake_gitlab,
            triage_runner=runner,
        )
        assert len(fake_gitlab.mr_notes) == 1
        assert fake_gitlab.mr_notes[0]["mr_iid"] == mr["iid"]
        assert not fake_gitlab.notes

    async def test_empty_scope_posts_clear_comment(self, db_factory, fake_gitlab):
        async def runner(rows):  # pragma: no cover — must never be called
            raise AssertionError("agent must not run with zero findings")

        outcome = await execute_security_command(
            _settings(),
            None,
            db_factory,
            {"command": "security_triage", "project_id": PROJECT_ID, "issue_iid": ISSUE_IID},
            gitlab=fake_gitlab,
            triage_runner=runner,
        )
        assert outcome["considered"] == 0
        body = fake_gitlab.notes[0]["body"]
        assert "No open security findings" in body

    async def test_batch_cap_and_severity_order(self, db_factory, fake_gitlab):
        findings = [
            NormalizedFinding(
                source="gitlab_sast",
                fingerprint=f"{index:064d}",
                severity="low" if index % 2 else "critical",
                title=f"f{index}",
            )
            for index in range(TRIAGE_BATCH + 10)
        ]
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(session, findings, provider="gitlab", scope="42")

        batches: list[list[SecurityFinding]] = []

        async def runner(rows):
            batches.append(list(rows))
            return SecurityTriageResult(summary="s", findings=[])

        await execute_security_command(
            _settings(),
            None,
            db_factory,
            {"command": "security_triage", "project_id": PROJECT_ID, "issue_iid": ISSUE_IID},
            gitlab=fake_gitlab,
            triage_runner=runner,
        )
        assert len(batches) == 1
        batch = batches[0]
        assert len(batch) == TRIAGE_BATCH == 50
        # Severity-sorted: every critical precedes every low.
        severities = [row.severity for row in batch]
        assert severities == sorted(severities, key={"critical": 0, "low": 1}.get)


# ----------------------------------------------------------------------
# GitHub path
# ----------------------------------------------------------------------


class TestSecurityCommandGitHub:
    async def test_ingests_alerts_then_triages_and_comments(self, db_factory):
        fake = FakeGitHub()
        fake.seed_issue("o/r", 7, "Security review")
        fake.code_scanning_alerts["o/r"] = [code_alert(11)]

        async def runner(rows):
            assert [row.fingerprint for row in rows] == ["11"]
            return SecurityTriageResult(
                summary="s",
                risk_level="high",
                findings=[verdict("11")],
            )

        outcome = await execute_security_command(
            _settings(),
            None,
            db_factory,
            {
                "command": "security_triage",
                "provider": "github",
                "repo_full_name": "o/r",
                "project_id": 70010,
                "issue_number": 7,
                "issue_is_pr": False,
                "author_username": "alice",
            },
            github_client=fake,
            triage_runner=runner,
        )
        assert outcome["refresh_created"] == 1
        assert outcome["considered"] == 1
        assert outcome["triaged"] == 1
        assert fake.calls_of("create_issue_comment")
        body = fake.calls_of("create_issue_comment")[0][1][3]
        assert "11"[:12] in body  # fingerprint shown

        async with db_factory() as session:
            row = (await session.execute(select(SecurityFinding))).scalar_one()
        assert row.status == "triaged"
        assert row.provider == "github"

    async def test_remote_dismiss_is_opt_in_with_exact_enums(self, db_factory):
        fake = FakeGitHub()
        fake.seed_issue("o/r", 7, "Security review")
        fake.code_scanning_alerts["o/r"] = [code_alert(11)]
        fake.secret_scanning_alerts["o/r"] = [
            {
                "number": 21,
                "state": "open",
                "secret_type": "aws_access_key",
                "secret_type_display_name": "AWS Access Key",
                "validity": "active",
            }
        ]

        async def runner(rows):
            return SecurityTriageResult(
                summary="s",
                findings=[verdict(row.fingerprint, false_positive=True) for row in rows],
            )

        metadata = {
            "command": "security_triage",
            "provider": "github",
            "repo_full_name": "o/r",
            "project_id": 70010,
            "issue_number": 7,
            "author_username": "alice",
        }

        # Default: verdict recorded locally, provider alert untouched.
        first_store = await new_db_factory()
        await execute_security_command(
            _settings(),
            None,
            first_store,
            dict(metadata),
            github_client=fake,
            triage_runner=runner,
        )
        assert not fake.calls_of("dismiss_code_scanning_alert")
        assert not fake.calls_of("resolve_secret_scanning_alert")
        async with first_store() as session:
            statuses = {
                row.fingerprint: row.status
                for row in (await session.execute(select(SecurityFinding))).scalars().all()
            }
        assert statuses == {"11": "false_positive", "21": "false_positive"}

        # Opt-in (fresh store: the first pass already wrote verdicts back):
        # the research §4.2 enums, justification as the audit comment.
        settings = _settings(FORGE_SECURITY_REMOTE_DISMISS=True)
        outcome = await execute_security_command(
            settings,
            None,
            await new_db_factory(),
            dict(metadata),
            github_client=fake,
            triage_runner=runner,
        )
        assert outcome["remote_dismissed"] == 2
        code_patch = fake.calls_of("dismiss_code_scanning_alert")[0][1]
        assert code_patch[2] == 11
        assert code_patch[3] == "false positive"  # SPACE enum (code scanning)
        assert "forge triage" in code_patch[4]
        secret_patch = fake.calls_of("resolve_secret_scanning_alert")[0][1]
        assert secret_patch[2] == 21
        assert secret_patch[3] == "false_positive"  # UNDERSCORE enum (secret scanning)
        assert statuses == {"11": "false_positive", "21": "false_positive"}

    async def test_dependabot_false_positive_maps_to_inaccurate(self, db_factory):
        fake = FakeGitHub()
        fake.seed_issue("o/r", 7, "Security review")
        fake.dependabot_alerts["o/r"] = [
            {
                "number": 31,
                "state": "open",
                "dependency": {
                    "package": {"ecosystem": "pip", "name": "requests"},
                    "manifest_path": "requirements.txt",
                },
                "security_advisory": {
                    "ghsa_id": "GHSA-x",
                    "cve_id": "CVE-2026-0001",
                    "summary": "SSRF",
                    "severity": "low",
                },
            }
        ]
        settings = _settings(FORGE_SECURITY_REMOTE_DISMISS=True)

        async def runner(rows):
            return SecurityTriageResult(
                summary="s",
                findings=[verdict(rows[0].fingerprint, false_positive=True)],
            )

        outcome = await execute_security_command(
            settings,
            None,
            db_factory,
            {
                "command": "security_triage",
                "provider": "github",
                "repo_full_name": "o/r",
                "project_id": 70010,
                "issue_number": 7,
                "author_username": "alice",
            },
            github_client=fake,
            triage_runner=runner,
        )
        assert outcome["remote_dismissed"] == 1
        patch = fake.calls_of("dismiss_dependabot_alert")[0][1]
        assert patch[3] == "inaccurate"  # no "false positive" in the dependabot enum


# ----------------------------------------------------------------------
# Comment formatting + agent prompt cap (pure)
# ----------------------------------------------------------------------


class TestFormatTriageComment:
    def test_unverdicted_rows_stay_open(self):
        row = SecurityFinding(
            provider="gitlab",
            scope="42",
            source="gitlab_sast",
            fingerprint="0" * 64,
            severity="high",
            title="SQL injection",
            path="app/db.py",
            line=7,
        )
        verdicted = SecurityFinding(
            provider="gitlab",
            scope="42",
            source="gitlab_sast",
            fingerprint="1" * 64,
            severity="critical",
            title="Secret",
            path="cfg.py",
            line=1,
        )
        body = format_triage_comment(
            [row, verdicted],
            {"1" * 64: verdict("1" * 64)},
            SecurityTriageResult(summary="s", risk_level="high", findings=[]),
            "42",
        )
        assert "no verdict, stays open" in body
        assert "Confirmed" in body
        assert "High" in body and "Critical" in body

    def test_agent_max_findings_setting_raises_the_prompt_cap(self):
        from forge.agents.registry import AgentRegistry
        from forge.agents.security_triage import SecurityTriageAgent
        from forge.context.engine import AgentContext
        from forge.context.security_report import SecurityFinding as ReportFinding
        from forge.context.security_report import SecurityReport
        from forge.orchestrator.project_config import ProjectConfig

        registry = AgentRegistry("agents")
        registry.load()
        definition = registry.get("security-triage")
        assert definition is not None
        assert int(definition.settings.get("max_findings", 0)) == 50

        findings = [
            ReportFinding(id=str(i), name=f"n{i}", description="d", severity="High")
            for i in range(50)
        ]
        agent = SecurityTriageAgent(
            definition=definition,
            model=None,  # type: ignore[arg-type] — prompt build never touches the model
            context=AgentContext(
                event_type="security_triage",
                project_id=0,
                security_reports=[SecurityReport(findings=findings, scan_type="sast")],
            ),
            project_config=ProjectConfig(),
            gitlab=None,  # type: ignore[arg-type]
        )
        message = agent._build_user_message()
        for i in range(50):
            assert f"n{i}" in message  # all 50 survive the prompt cap


# ----------------------------------------------------------------------
# Gateway → durable step wiring
# ----------------------------------------------------------------------


def issue_note_payload(note: str, *, note_id: int = 900) -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice", "username": "alice"},
        "project": {
            "id": PROJECT_ID,
            "name": "test",
            "path_with_namespace": "group/test",
            "web_url": "https://gitlab.test/group/test",
        },
        "object_attributes": {"id": note_id, "note": note, "noteable_type": "Issue"},
        "issue": {
            "id": ISSUE_IID,
            "iid": ISSUE_IID,
            "title": "Add a widget",
            "state": "opened",
        },
    }


def mr_note_payload(note: str, *, note_id: int = 901) -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice", "username": "alice"},
        "project": {
            "id": PROJECT_ID,
            "name": "test",
            "path_with_namespace": "group/test",
            "web_url": "https://gitlab.test/group/test",
        },
        "object_attributes": {"id": note_id, "note": note, "noteable_type": "MergeRequest"},
        "merge_request": {
            "iid": MR_IID,
            "title": "An MR",
            "source_branch": "f",
            "target_branch": "main",
        },
    }


class TestGatewayWiring:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=run_settings(tmp_path))
        async with application.router.lifespan_context(application):
            application.state.task_queue = AsyncMock()
            application.state.task_queue.is_duplicate = AsyncMock(return_value=False)
            yield application
        reset_engine()

    async def post(self, app, payload: dict) -> dict:
        headers = {"X-Gitlab-Token": TEST_SECRET, "X-Gitlab-Event": "Note Hook"}
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/webhook", json=payload, headers=headers)
        return response

    async def _latest_step(self, app) -> StepRun:
        async with app.state.session_factory() as session:
            return (
                (await session.execute(select(StepRun).order_by(StepRun.id.desc())))
                .scalars()
                .first()
            )

    async def test_issue_note_schedules_security_step(self, app):
        response = await self.post(app, issue_note_payload("@forge /security"))

        assert response.status_code == 202
        assert response.json().get("run_command") is True
        step = await self._latest_step(app)
        assert step.step_name == "security_triage"
        assert step.status == "scheduled"
        assert step.payload["command"] == "security_triage"
        assert step.payload["issue_iid"] == ISSUE_IID
        assert step.payload["mr_iid"] is None

    async def test_bare_security_note_routes(self, app):
        response = await self.post(app, issue_note_payload("/security", note_id=910))

        assert response.status_code == 202
        step = await self._latest_step(app)
        assert step.step_name == "security_triage"

    async def test_mr_note_schedules_security_step_with_mr_target(self, app):
        response = await self.post(app, mr_note_payload("@forge /security"))

        assert response.status_code == 202
        step = await self._latest_step(app)
        assert step.step_name == "security_triage"
        assert step.payload["mr_iid"] == MR_IID
        assert step.payload["issue_iid"] is None

    async def test_mr_implement_still_takes_the_legacy_path(self, app):
        response = await self.post(app, mr_note_payload("@forge /implement"))

        assert response.status_code == 202
        assert response.json().get("run_command") is None  # not a durable run command
        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert steps == []

    async def test_executed_step_reaches_the_triage_executor(self, app, monkeypatch):
        """End-to-end: note → durable step → executor → triage comment.

        The step runs through the SAME claim/execute protocol as the worker
        (execute_run_command → execute_security_command), with the GitLab
        transport and the LLM agent replaced by fakes.
        """
        from forge.findings import triage as triage_module
        from forge.worker.steps import run_pending_command_step
        from tests.test_findings import VULNS, sast_report, seed_sast_pipeline

        fake_gitlab = FakeGitLab()
        seed_sast_pipeline(fake_gitlab, pipeline_id=700, job_id=701, report=sast_report(VULNS))
        monkeypatch.setattr(
            "forge.runs.service.GitLabClient", FakeGitLabClientFactory(shared=fake_gitlab)
        )

        async def stub_default_runner(settings, forge_config, rows, *, project_path):
            return SecurityTriageResult(
                summary="s",
                risk_level="high",
                findings=[verdict(row.fingerprint) for row in rows],
            )

        monkeypatch.setattr(triage_module, "_default_triage_runner", stub_default_runner)

        note_id = 930
        response = await self.post(app, issue_note_payload("/security", note_id=note_id))
        assert response.status_code == 202
        source_event_id = command_source_event_id("security_triage", PROJECT_ID, note_id)

        await run_pending_command_step(
            app.state.session_factory,
            app.state.settings,
            app.state.forge_config,
            source_event_id,
            owner="test",
        )
        step = await self._latest_step(app)
        assert step.step_name == "security_triage"
        assert step.status == "succeeded"
        # The executor ingested the pipeline reports and posted the comment.
        assert fake_gitlab.notes, "triage comment must be posted on the issue"
        assert any("Security Triage" in note["body"] for note in fake_gitlab.notes)


# The GitHub ingress: /security on an issue or PR comment normalizes to the
# same durable command.
class TestGitHubIngress:
    def test_security_comment_normalizes_to_triage_command(self):
        from forge.gateway.github_webhook import normalize_issue_comment

        payload = {
            "issue": {"number": 42, "state": "open"},
            "comment": {"id": 88100, "body": "/security", "user": {"login": "alice"}},
            "repository": {"id": 70010, "full_name": "acme/acme-widget"},
            "installation": {"id": 9},
        }
        metadata = normalize_issue_comment(payload)
        assert metadata is not None
        assert metadata["command"] == "security_triage"
        assert metadata["provider"] == "github"
        assert metadata["repo_full_name"] == "acme/acme-widget"
        assert metadata["issue_number"] == 42

    def test_pr_security_comment_flags_issue_is_pr(self):
        from forge.gateway.github_webhook import normalize_issue_comment

        payload = {
            "issue": {"number": 42, "pull_request": {"url": "x"}},
            "comment": {"id": 88101, "body": "@forge /security please", "user": {"login": "bob"}},
            "repository": {"id": 70010, "full_name": "acme/acme-widget"},
            "installation": {"id": 9},
        }
        metadata = normalize_issue_comment(payload)
        assert metadata is not None
        assert metadata["command"] == "security_triage"
        assert metadata["issue_is_pr"] is True
