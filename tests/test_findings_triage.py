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
from forge.findings.models import SecurityFinding, SecurityFindingAction
from forge.findings.triage import (
    TRIAGE_BATCH,
    confirm_finding_verdict,
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

    async def test_triage_comment_grouped_by_severity_with_suggestions(
        self, db_factory, fake_gitlab
    ):
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
        assert outcome["suggested"] == 4
        assert outcome["confirmed"] == 0  # default: no auto-accept
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
        # R25: the verdict is a SUGGESTION — the authoritative status stays
        # open until an authorized actor (or the auto-accept opt-in) confirms.
        assert "Suggested: confirmed" in body and "Suggested: false positive" in body
        async with db_factory() as session:
            rows = {
                row.fingerprint: row
                for row in (await session.execute(select(SecurityFinding))).scalars().all()
            }
            actions = (await session.execute(select(SecurityFindingAction))).scalars().all()
        assert all(row.status == "open" for row in rows.values())
        assert rows[fps["high"]].suggested_verdict == "false_positive"
        assert rows[fps["high"]].triage_note == "test value, not a real credential"
        assert rows[fps["critical"]].suggested_verdict == "triaged"
        assert rows[fps["critical"]].suggested_by == "ai:security-triage"
        assert rows[fps["critical"]].version == 1  # suggestion bumps the CAS version
        # The governance journal recorded every suggestion.
        assert {action.action for action in actions} == {"suggest_verdict"}
        assert all(action.outcome == "applied" for action in actions)

    async def test_auto_accept_opt_in_moves_authoritative_status(self, db_factory, fake_gitlab):
        """FORGE_SECURITY_AUTO_ACCEPT (default OFF) lets the pass confirm."""
        fps = await seed_findings(db_factory)

        async def runner(rows):
            return SecurityTriageResult(
                summary="s",
                risk_level="high",
                findings=[
                    verdict(fps["critical"]),
                    verdict(fps["high"], false_positive=True),
                ],
            )

        outcome = await execute_security_command(
            _settings(FORGE_SECURITY_AUTO_ACCEPT=True),
            None,
            db_factory,
            {"command": "security_triage", "project_id": PROJECT_ID, "issue_iid": ISSUE_IID},
            gitlab=fake_gitlab,
            triage_runner=runner,
        )
        assert outcome["confirmed"] == 2
        async with db_factory() as session:
            rows = {
                row.fingerprint: row
                for row in (await session.execute(select(SecurityFinding))).scalars().all()
            }
            actions = (await session.execute(select(SecurityFindingAction))).scalars().all()
        assert rows[fps["critical"]].status == "triaged"
        assert rows[fps["high"]].status == "false_positive"
        # Both legs journaled: the suggestion AND the machine confirmation.
        by_action = {action.action for action in actions}
        assert by_action == {"suggest_verdict", "confirm_verdict"}
        confirm = next(a for a in actions if a.action == "confirm_verdict")
        assert confirm.actor == "auto_accept"
        rows_by_id = {row.id: row for row in rows.values()}
        assert confirm.after_status == rows_by_id[confirm.finding_id].status

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
        # R25: the suggestion is recorded; the status stays open.
        assert row.suggested_verdict == "triaged"
        assert row.status == "open"
        assert row.provider == "github"
        assert row.connection_id == "github:o/r"  # metadata fallback scope key

    async def test_suggestion_and_remote_dismiss_are_separate_privileges(self, db_factory):
        """R25: suggestion != status != remote dismissal.

        - default settings: the AI verdict lands as a suggestion only — no
          remote call, no authoritative write;
        - grant + auto-accept: the confirmed false positives may be
          dismissed remotely, each with an intent journal row written
          before the provider call;
        - grant WITHOUT confirmation: still no remote dismissal — a model
          suggestion alone can never close a remote alert.
        """
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

        # Default: verdict recorded as a suggestion, provider untouched.
        suggestion_store = await new_db_factory()
        outcome = await execute_security_command(
            _settings(),
            None,
            suggestion_store,
            dict(metadata),
            github_client=fake,
            triage_runner=runner,
        )
        assert not fake.calls_of("dismiss_code_scanning_alert")
        assert not fake.calls_of("resolve_secret_scanning_alert")
        assert outcome["confirmed"] == 0 and outcome["remote_dismissed"] == 0
        async with suggestion_store() as session:
            statuses = {
                row.fingerprint: (row.status, row.suggested_verdict)
                for row in (await session.execute(select(SecurityFinding))).scalars().all()
            }
            actions = (await session.execute(select(SecurityFindingAction))).scalars().all()
        assert statuses == {"11": ("open", "false_positive"), "21": ("open", "false_positive")}
        assert {a.action for a in actions} == {"suggest_verdict"}

        # Grant WITHOUT a confirmation: the suggestions exist but no
        # authoritative status — the remote alerts stay untouched.
        grant_store = await new_db_factory()
        outcome = await execute_security_command(
            _settings(FORGE_SECURITY_REMOTE_DISMISS=True),
            None,
            grant_store,
            dict(metadata),
            github_client=fake,
            triage_runner=runner,
        )
        assert outcome["confirmed"] == 0
        assert outcome["remote_dismissed"] == 0
        assert not fake.calls_of("dismiss_code_scanning_alert")

        # Grant + auto-accept (fresh store): the suggestions are confirmed,
        # and ONLY THEN the research §4.2 enum-dismissals run, journal
        # intent-first, flipping the mirror to `dismissed`.
        settings = _settings(FORGE_SECURITY_REMOTE_DISMISS=True, FORGE_SECURITY_AUTO_ACCEPT=True)
        outcome = await execute_security_command(
            settings,
            None,
            await new_db_factory(),
            dict(metadata),
            github_client=fake,
            triage_runner=runner,
        )
        assert outcome["confirmed"] == 2
        assert outcome["remote_dismissed"] == 2
        code_patch = fake.calls_of("dismiss_code_scanning_alert")[0][1]
        assert code_patch[2] == 11
        assert code_patch[3] == "false positive"  # SPACE enum (code scanning)
        assert "forge triage" in code_patch[4]
        secret_patch = fake.calls_of("resolve_secret_scanning_alert")[0][1]
        assert secret_patch[2] == 21
        assert secret_patch[3] == "false_positive"  # UNDERSCORE enum (secret scanning)

    async def test_remote_dismiss_journals_intent_before_the_call(self, db_factory):
        """The privileged action leaves an audit trail: intent → outcome."""
        fake = FakeGitHub()
        fake.seed_issue("o/r", 7, "Security review")
        fake.code_scanning_alerts["o/r"] = [code_alert(11)]
        settings = _settings(FORGE_SECURITY_REMOTE_DISMISS=True, FORGE_SECURITY_AUTO_ACCEPT=True)

        async def runner(rows):
            return SecurityTriageResult(
                summary="s",
                findings=[verdict(rows[0].fingerprint, false_positive=True)],
            )

        await execute_security_command(
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
        async with db_factory() as session:
            actions = (
                (
                    await session.execute(
                        select(SecurityFindingAction).order_by(SecurityFindingAction.id)
                    )
                )
                .scalars()
                .all()
            )
            row = (await session.execute(select(SecurityFinding))).scalar_one()
        dismissals = [a for a in actions if a.action == "remote_dismiss"]
        assert [a.outcome for a in dismissals] == ["requested", "succeeded"]
        assert dismissals[0].payload["alert"] == "11"
        assert dismissals[0].before_status == "false_positive"
        assert dismissals[1].after_status == "dismissed"
        assert dismissals[0].justification == "test value, not a real credential"
        assert row.status == "dismissed"  # the mirror follows the remote state
        suggestions = [a for a in actions if a.action == "suggest_verdict"]
        assert len(suggestions) == 1

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
        settings = _settings(FORGE_SECURITY_REMOTE_DISMISS=True, FORGE_SECURITY_AUTO_ACCEPT=True)

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
# Optimistic binding (R25): the LLM call never overwrites a manual change
# ----------------------------------------------------------------------


class TestOptimisticBinding:
    @pytest.fixture()
    def fake_gitlab(self):
        return FakeGitLab()  # no pipelines — the refresh leg skips silently

    async def test_manual_change_during_llm_call_is_not_overwritten(self, db_factory, fake_gitlab):
        """A status change racing the model call wins; the stale verdict skips."""
        fps = await seed_findings(db_factory)
        victim = fps["critical"]

        async def runner(rows):
            # Simulate a human flipping the status WHILE the LLM call is in
            # flight: an authoritative change that bumps the version the
            # triage pass pinned at snapshot time.
            async with db_factory() as session:
                async with session.begin():
                    row = (
                        await session.execute(
                            select(SecurityFinding).where(SecurityFinding.fingerprint == victim)
                        )
                    ).scalar_one()
                    row.status = "fixed"
                    row.version += 1
            return SecurityTriageResult(
                summary="s",
                risk_level="high",
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
        assert outcome["superseded"] == 1
        assert outcome["suggested"] == 3
        async with db_factory() as session:
            rows = {
                row.fingerprint: row
                for row in (await session.execute(select(SecurityFinding))).scalars().all()
            }
            actions = (await session.execute(select(SecurityFindingAction))).scalars().all()
        # The manual decision stands untouched — no suggestion landed on it.
        assert rows[victim].status == "fixed"
        assert rows[victim].suggested_verdict is None
        assert rows[victim].version == 1  # only the manual bump (seed = 0)
        skipped = [a for a in actions if a.outcome == "skipped"]
        assert len(skipped) == 1
        assert skipped[0].finding_id == rows[victim].id
        assert skipped[0].payload["reason"] == "version_changed_since_snapshot"
        # The other three findings still got their suggestions.
        assert rows[fps["high"]].suggested_verdict == "triaged"

    async def test_out_of_batch_verdict_is_never_applied(self, db_factory, fake_gitlab):
        """Verdicts for suppressed / hallucinated fingerprints do nothing."""
        await seed_findings(db_factory)
        # A suppressed row that is NOT part of the open batch, plus a
        # fingerprint the model simply hallucinated.
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [
                        NormalizedFinding(
                            source="gitlab_sast",
                            fingerprint="9" * 64,
                            severity="high",
                            title="Suppressed earlier",
                        )
                    ],
                    provider="gitlab",
                    scope=str(PROJECT_ID),
                )
                suppressed = (
                    await session.execute(
                        select(SecurityFinding).where(SecurityFinding.fingerprint == "9" * 64)
                    )
                ).scalar_one()
                suppressed.status = "false_positive"
                suppressed.version += 1
        suppressed_version = suppressed.version

        async def runner(rows):
            assert len(rows) == 4  # the suppressed row is not in the batch
            return SecurityTriageResult(
                summary="s",
                risk_level="high",
                findings=[verdict(row.fingerprint) for row in rows]
                + [
                    verdict("9" * 64, false_positive=False),  # suppressed row
                    verdict("deadbeef" * 8),  # hallucinated
                ],
            )

        outcome = await execute_security_command(
            _settings(FORGE_SECURITY_AUTO_ACCEPT=True),
            None,
            db_factory,
            {"command": "security_triage", "project_id": PROJECT_ID, "issue_iid": ISSUE_IID},
            gitlab=fake_gitlab,
            triage_runner=runner,
        )
        assert outcome["out_of_batch"] == 2
        assert outcome["suggested"] == 4
        async with db_factory() as session:
            row = (
                await session.execute(
                    select(SecurityFinding).where(SecurityFinding.fingerprint == "9" * 64)
                )
            ).scalar_one()
        # The out-of-batch row kept its authoritative suppression, version
        # untouched — the model cannot reach rows outside the pinned batch.
        assert row.status == "false_positive"
        assert row.suggested_verdict is None
        assert row.version == suppressed_version


# ----------------------------------------------------------------------
# Authorized confirmation (R25): the human gate on suggestions
# ----------------------------------------------------------------------


class TestConfirmFindingVerdict:
    @pytest.fixture()
    def fake_gitlab(self):
        return FakeGitLab()

    async def seed_one(self, db_factory) -> str:
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [
                        NormalizedFinding(
                            source="gitlab_sast",
                            fingerprint="c" * 64,
                            severity="critical",
                            title="Hardcoded secret",
                        )
                    ],
                    provider="gitlab",
                    scope=str(PROJECT_ID),
                )
                row = (
                    await session.execute(
                        select(SecurityFinding).where(SecurityFinding.fingerprint == "c" * 64)
                    )
                ).scalar_one()
                row.suggested_verdict = "false_positive"
                row.suggested_by = "ai:security-triage"
                row.triage_note = "test value, not a real credential"
                row.version += 1
                return row.id

    async def test_unauthorized_actor_is_refused(self, db_factory):
        finding_id = await self.seed_one(db_factory)
        with pytest.raises(PermissionError):
            await confirm_finding_verdict(
                db_factory,
                finding_id,
                "mallory",
                triagers="alice",
            )
        with pytest.raises(PermissionError):
            await confirm_finding_verdict(
                db_factory,
                finding_id,
                "alice",
                triagers="",  # empty allowlist: nobody may confirm
            )
        async with db_factory() as session:
            row = (await session.execute(select(SecurityFinding))).scalar_one()
        assert row.status == "open"  # nothing moved

    async def test_confirm_promotes_suggestion_to_status(self, db_factory):
        finding_id = await self.seed_one(db_factory)
        result = await confirm_finding_verdict(
            db_factory,
            finding_id,
            "alice",
            justification="checked with the team",
            triagers="alice, bob",
        )
        assert result["status"] == "false_positive"
        async with db_factory() as session:
            row = (await session.execute(select(SecurityFinding))).scalar_one()
            actions = (await session.execute(select(SecurityFindingAction))).scalars().all()
        assert row.version == 2
        assert len(actions) == 1
        assert actions[0].action == "confirm_verdict"
        assert actions[0].actor == "alice"
        assert actions[0].justification == "checked with the team"
        assert actions[0].after_status == "false_positive"

    async def test_reject_clears_suggestion_and_journals(self, db_factory):
        finding_id = await self.seed_one(db_factory)
        result = await confirm_finding_verdict(
            db_factory,
            finding_id,
            "bob",
            accept=False,
            triagers="bob",
        )
        assert result["status"] == "open"
        assert result["suggested_verdict"] is None
        async with db_factory() as session:
            row = (await session.execute(select(SecurityFinding))).scalar_one()
            actions = (await session.execute(select(SecurityFindingAction))).scalars().all()
        assert row.suggested_verdict is None and row.suggested_by is None
        assert row.version == 2
        assert [a.action for a in actions] == ["reject_verdict"]

    async def test_confirm_requires_a_suggestion(self, db_factory):
        finding_id = await self.seed_one(db_factory)
        async with db_factory() as session:
            async with session.begin():
                row = (await session.execute(select(SecurityFinding))).scalar_one()
                row.suggested_verdict = None
        with pytest.raises(RuntimeError, match="no suggested verdict"):
            await confirm_finding_verdict(db_factory, finding_id, "alice", triagers="alice")


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
        assert "Suggested: confirmed" in body  # R25: suggestions, not writes
        assert "awaiting confirmation" in body
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
