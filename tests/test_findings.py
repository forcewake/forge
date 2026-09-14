"""Security findings: fingerprints, the dedupe upsert, both ingestions.

Ground truth: docs/research/ci-security-surface.md §3 (report schema,
alert field sets, tier matrix) and §4.1 (forge-computed GitLab
fingerprints; absence from one scan is never a fix).
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.findings.fingerprints import gitlab_fingerprint, github_fingerprint
from forge.findings.ingest import (
    NormalizedFinding,
    ingest_gitlab_pipeline,
    ingest_github_alerts,
    upsert_findings,
)
from forge.findings.models import SecurityFinding, normalize_severity
from forge.models.base import Base
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_gitlab import FakeGitLab


def _sast_vulnerability(
    *,
    vid: str,
    name: str,
    severity: str,
    file: str,
    line: int,
    cwe: str = "CWE-798",
    category: str = "sast",
) -> dict:
    return {
        "id": vid,
        "category": category,
        "name": name,
        "message": name,
        "description": f"{name} detected",
        "severity": severity,
        "location": {"file": file, "start_line": line, "end_line": line},
        "identifiers": [{"type": "cwe", "name": cwe, "value": cwe}],
        "solution": "Fix it",
    }


def sast_report(vulnerabilities: list[dict]) -> dict:
    return {
        "version": "15.0.0",
        "scan": {
            "analyzer": {"id": "semgrep", "name": "Semgrep", "version": "5.0"},
            "scanner": {"id": "semgrep", "name": "Semgrep", "version": "5.0"},
            "type": "sast",
            "start_time": "2026-09-14T00:00:00",
            "end_time": "2026-09-14T00:01:00",
            "status": "success",
        },
        "vulnerabilities": vulnerabilities,
    }


VULNS = [
    _sast_vulnerability(
        vid="v1", name="Hardcoded secret", severity="Critical", file="app/config.py", line=12
    ),
    _sast_vulnerability(
        vid="v2",
        name="Weak hash",
        severity="Unknown",
        file="app/hash.py",
        line=3,
        cwe="CWE-327",
    ),
]


def seed_sast_pipeline(
    fake: FakeGitLab,
    *,
    pipeline_id: int,
    job_id: int,
    report: dict | None = None,
    job_name: str = "sast",
    artifact: str = "gl-sast-report.json",
) -> int:
    """Seed one pipeline whose security job carries a report artifact."""
    fake.pipelines.append({"id": pipeline_id, "ref": "main", "status": "success", "sha": "b" * 40})
    fake.set_pipeline_jobs(
        pipeline_id, [{"id": job_id, "name": job_name, "stage": "test", "status": "success"}]
    )
    fake.seed_job_artifact(job_id, artifact, json.dumps(report or sast_report(VULNS)))
    return pipeline_id


def finding(
    fingerprint: str,
    *,
    source: str = "gitlab_sast",
    severity: str = "high",
    title: str = "Finding",
    path: str | None = "app.py",
    line: int | None = 1,
) -> NormalizedFinding:
    return NormalizedFinding(
        source=source,
        fingerprint=fingerprint,
        severity=severity,
        title=title,
        path=path,
        line=line,
    )


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


# ----------------------------------------------------------------------
# Fingerprints
# ----------------------------------------------------------------------


class TestFingerprints:
    def test_gitlab_fingerprint_is_stable_sha256(self):
        kwargs = dict(
            category="sast",
            identifiers=[{"type": "cwe", "value": "CWE-798"}],
            location={"file": "app/config.py", "start_line": 12},
            fallback_key="Hardcoded secret",
        )
        first = gitlab_fingerprint(**kwargs)
        assert first == gitlab_fingerprint(**kwargs)
        assert len(first) == 64
        int(first, 16)  # sha256 hex — must not raise

    def test_gitlab_fingerprint_changes_on_identity_parts(self):
        base = dict(identifiers=[{"type": "cwe", "value": "CWE-798"}], fallback_key="n")
        same_loc = dict(category="sast", location={"file": "a.py", "start_line": 1})
        moved = dict(category="sast", location={"file": "a.py", "start_line": 99})
        other_rule = dict(
            base,
            category="sast",
            location={"file": "a.py", "start_line": 1},
            identifiers=[{"type": "cwe", "value": "CWE-89"}],
        )
        assert gitlab_fingerprint(**{**base, **same_loc}) != gitlab_fingerprint(**{**base, **moved})
        assert gitlab_fingerprint(**{**base, **same_loc}) != gitlab_fingerprint(**other_rule)

    def test_github_fingerprint_is_the_alert_number(self):
        assert github_fingerprint(412) == "412"
        assert github_fingerprint("412") == "412"

    def test_normalize_severity_maps_provider_vocab(self):
        assert normalize_severity("Critical") == "critical"
        assert normalize_severity("Unknown") == "info"  # GitLab's floor
        assert normalize_severity("High") == "high"
        assert normalize_severity("error") == "high"  # GitHub rule severity
        assert normalize_severity("warning") == "low"
        assert normalize_severity(None) == "info"
        assert normalize_severity("bogus") == "info"


# ----------------------------------------------------------------------
# Table semantics
# ----------------------------------------------------------------------


class TestFindingModel:
    @pytest.fixture()
    async def session(self, db_factory):
        async with db_factory() as session:
            yield session

    async def test_defaults(self, session: AsyncSession):
        finding_row = SecurityFinding(
            provider="gitlab",
            scope="42",
            source="gitlab_sast",
            fingerprint="a" * 64,
        )
        session.add(finding_row)
        await session.flush()
        assert len(finding_row.id) == 32
        int(finding_row.id, 16)  # uuid4 hex — must not raise
        assert finding_row.status == "open"
        assert finding_row.severity == "info"
        assert finding_row.first_seen is not None
        assert finding_row.last_seen is not None

    async def test_unknown_status_rejected(self, session: AsyncSession):
        session.add(
            SecurityFinding(
                provider="gitlab",
                scope="42",
                source="gitlab_sast",
                fingerprint="b" * 64,
                status="teleported",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_duplicate_fingerprint_rejected(self, session: AsyncSession):
        for _ in range(2):
            session.add(
                SecurityFinding(
                    provider="gitlab",
                    scope="42",
                    source="gitlab_sast",
                    fingerprint="c" * 64,
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()


# ----------------------------------------------------------------------
# Core upsert
# ----------------------------------------------------------------------


class TestUpsert:
    async def _rows(self, db_factory) -> list[SecurityFinding]:
        async with db_factory() as session:
            return (
                (await session.execute(select(SecurityFinding).order_by(SecurityFinding.source)))
                .scalars()
                .all()
            )

    async def test_creates_open_rows(self, db_factory):
        now = datetime.now(timezone.utc)
        async with db_factory() as session:
            async with session.begin():
                result = await upsert_findings(
                    session,
                    [finding("d" * 64, severity="critical", title="Hardcoded secret")],
                    provider="gitlab",
                    scope="42",
                    seen_at=now,
                )
        assert result.created == 1
        rows = await self._rows(db_factory)
        assert len(rows) == 1
        assert rows[0].status == "open"
        # SQLite drops the tzinfo; compare wall-clock values.
        assert rows[0].first_seen == now.replace(tzinfo=None)

    async def test_rescan_updates_observability_never_status(self, db_factory):
        first_seen = datetime.now(timezone.utc)
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [finding("e" * 64, title="SQL injection")],
                    provider="gitlab",
                    scope="42",
                    seen_at=first_seen,
                )
        # A triage verdict exists before the second scan.
        async with db_factory() as session:
            async with session.begin():
                row = (await session.execute(select(SecurityFinding))).scalar_one()
                row.status = "false_positive"
                row.triage_note = "test fixture value"

        later = datetime.now(timezone.utc)
        async with db_factory() as session:
            async with session.begin():
                result = await upsert_findings(
                    session,
                    [finding("e" * 64, title="SQL injection", line=2)],
                    provider="gitlab",
                    scope="42",
                    seen_at=later,
                )
        assert (result.created, result.updated) == (0, 1)
        row = (await self._rows(db_factory))[0]
        assert row.status == "false_positive"  # the verdict survives the rescan
        assert row.triage_note == "test fixture value"
        assert row.first_seen == first_seen.replace(tzinfo=None)
        assert row.last_seen == later.replace(tzinfo=None)
        assert row.line == 2  # observability refreshed

    async def test_same_fingerprint_different_source_coexists(self, db_factory):
        # The dedupe key is (source, scope, fingerprint): an identical
        # identity under gitlab_secret never collides with gitlab_sast.
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [
                        finding("f" * 64, source="gitlab_sast"),
                        finding("f" * 64, source="gitlab_secret"),
                    ],
                    provider="gitlab",
                    scope="42",
                )
        rows = await self._rows(db_factory)
        assert {row.source for row in rows} == {"gitlab_sast", "gitlab_secret"}


# ----------------------------------------------------------------------
# GitLab CE: pipeline artifacts
# ----------------------------------------------------------------------


class TestGitLabIngestion:
    async def test_parse_pipeline_artifacts_into_findings(self, db_factory):
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=700, job_id=701)
        fake.set_pipeline_jobs(
            700,
            [
                {"id": 701, "name": "sast", "stage": "test", "status": "success"},
                {"id": 702, "name": "secret_detection", "stage": "test", "status": "success"},
            ],
        )
        fake.seed_job_artifact(
            702,
            "gl-secret-detection-report.json",
            json.dumps(
                sast_report(
                    [
                        _sast_vulnerability(
                            vid="s1",
                            name="AWS key",
                            severity="Critical",
                            file="secrets.txt",
                            line=1,
                            category="secret_detection",
                        )
                    ]
                )
            ),
        )

        result = await ingest_gitlab_pipeline(fake, db_factory, 42, 700, sha="b" * 40)
        assert result.created == 3  # 2 SAST + 1 secret
        async with db_factory() as session:
            rows = (
                (await session.execute(select(SecurityFinding).order_by(SecurityFinding.source)))
                .scalars()
                .all()
            )
        by_source = {row.source: row for row in rows}
        assert set(by_source) == {"gitlab_sast", "gitlab_secret"}
        assert all(row.provider == "gitlab" and row.scope == "42" for row in rows)
        # Severity normalization: Critical → critical, Unknown → info.
        sast_rows = [row for row in rows if row.source == "gitlab_sast"]
        assert sorted(row.severity for row in sast_rows) == ["critical", "info"]
        assert all(row.status == "open" for row in rows)
        assert all(row.sha == "b" * 40 for row in rows)
        assert by_source["gitlab_secret"].path == "secrets.txt"

    async def test_job_without_report_is_skipped_not_fatal(self, db_factory):
        fake = FakeGitLab()
        fake.pipelines.append({"id": 800, "ref": "main", "status": "success", "sha": "c" * 40})
        fake.set_pipeline_jobs(
            800, [{"id": 801, "name": "sast", "stage": "test", "status": "success"}]
        )
        # No artifact seeded — real GitLab answers 404.

        result = await ingest_gitlab_pipeline(fake, db_factory, 42, 800)
        assert result.created == 0
        assert len(result.errors) == 1

    async def test_job_name_patterns_match_prefixed_names(self, db_factory):
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=810, job_id=811, job_name="semgrep-sast")
        result = await ingest_gitlab_pipeline(fake, db_factory, 42, 810)
        assert result.created == 2

    async def test_rescan_absent_finding_is_not_fixed(self, db_factory):
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=900, job_id=901)
        await ingest_gitlab_pipeline(fake, db_factory, 42, 900)

        # Next scan: only "Hardcoded secret" is still present.
        present = [v for v in VULNS if v["name"] == "Hardcoded secret"]
        assert len(present) == 1
        seed_sast_pipeline(fake, pipeline_id=902, job_id=903, report=sast_report(present))
        await ingest_gitlab_pipeline(fake, db_factory, 42, 902)

        async with db_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(SecurityFinding).order_by(SecurityFinding.fingerprint)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2  # nothing deleted, nothing auto-fixed
        states = {row.title: (row.status, row.last_seen) for row in rows}
        # The absent finding stays open — scan silence is never a fix.
        assert states["Weak hash"][0] == "open"
        # The present finding got a fresh last_seen.
        assert states["Hardcoded secret"][1] > states["Weak hash"][1]

    async def test_fingerprints_dedupe_across_scans(self, db_factory):
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=950, job_id=951)
        first = await ingest_gitlab_pipeline(fake, db_factory, 42, 950)
        assert first.created == 2
        seed_sast_pipeline(fake, pipeline_id=952, job_id=953)
        second = await ingest_gitlab_pipeline(fake, db_factory, 42, 952)
        assert (second.created, second.updated) == (0, 2)
        async with db_factory() as session:
            count = len((await session.execute(select(SecurityFinding))).scalars().all())
        assert count == 2


# ----------------------------------------------------------------------
# GitHub: alert APIs
# ----------------------------------------------------------------------


def code_alert(number: int, *, severity: str = "high", path: str = "app.py", line: int = 10):
    return {
        "number": number,
        "state": "open",
        "rule": {
            "id": "py/sql-injection",
            "name": "SQL injection",
            "severity": "error",
            "security_severity_level": severity,
            "description": "Untrusted input in a query",
        },
        "tool": {"name": "CodeQL", "version": "2.0"},
        "most_recent_instance": {
            "ref": "refs/heads/main",
            "commit_sha": "a" * 40,
            "location": {"path": path, "start_line": line},
        },
        "created_at": "2026-09-01T00:00:00Z",
    }


def secret_alert(number: int, *, validity: str = "active"):
    return {
        "number": number,
        "state": "open",
        "secret_type": "aws_access_key",
        "secret_type_display_name": "AWS Access Key",
        "validity": validity,
    }


def dependabot_alert(number: int):
    return {
        "number": number,
        "state": "open",
        "dependency": {
            "package": {"ecosystem": "pip", "name": "requests"},
            "manifest_path": "requirements.txt",
        },
        "security_advisory": {
            "ghsa_id": "GHSA-xxxx",
            "cve_id": "CVE-2026-0001",
            "summary": "SSRF in requests",
            "severity": "high",
        },
        "security_vulnerability": {"vulnerable_version_range": "< 2.32.0"},
    }


class TestGitHubIngestion:
    async def test_all_three_surfaces_ingest(self, db_factory):
        fake = FakeGitHub()
        fake.code_scanning_alerts["o/r"] = [code_alert(11), code_alert(12, severity="medium")]
        fake.secret_scanning_alerts["o/r"] = [secret_alert(21)]
        fake.dependabot_alerts["o/r"] = [dependabot_alert(31)]

        result = await ingest_github_alerts(fake, db_factory, "o", "r")
        assert result.created == 4
        assert result.errors == []
        async with db_factory() as session:
            rows = (
                (await session.execute(select(SecurityFinding).order_by(SecurityFinding.source)))
                .scalars()
                .all()
            )
        by_source = {row.source: row for row in rows}
        assert set(by_source) == {
            "github_code_scanning",
            "github_secret",
            "github_dependabot",
        }
        assert all(row.provider == "github" and row.scope == "o/r" for row in rows)
        # GitHub fingerprints are the alert numbers per repo (research §3.2).
        code_rows = [row for row in rows if row.source == "github_code_scanning"]
        assert {row.fingerprint for row in code_rows} == {"11", "12"}
        assert by_source["github_secret"].fingerprint == "21"
        assert by_source["github_dependabot"].fingerprint == "31"
        assert by_source["github_secret"].severity == "critical"  # validity=active
        assert by_source["github_dependabot"].path == "requirements.txt"
        assert by_source["github_dependabot"].severity == "high"
        assert code_rows[0].path == "app.py"

    async def test_disabled_surface_degrades_rest_ingests(self, db_factory):
        fake = FakeGitHub()
        fake.code_scanning_disabled = True  # 403: Code Security off on this repo
        fake.secret_scanning_disabled = True
        fake.dependabot_alerts["o/r"] = [dependabot_alert(31)]

        result = await ingest_github_alerts(fake, db_factory, "o", "r")
        assert result.created == 1  # dependabot is free for all repos
        assert len(result.errors) == 2
        assert all("403" in err for err in result.errors)

    async def test_reingest_dedupes_by_alert_number(self, db_factory):
        fake = FakeGitHub()
        fake.code_scanning_alerts["o/r"] = [code_alert(11)]
        await ingest_github_alerts(fake, db_factory, "o", "r")
        async with db_factory() as session:
            first = (await session.execute(select(SecurityFinding))).scalar_one()

        fake.code_scanning_alerts["o/r"] = [code_alert(11, line=99)]
        result = await ingest_github_alerts(fake, db_factory, "o", "r")
        assert (result.created, result.updated) == (0, 1)
        async with db_factory() as session:
            second = (await session.execute(select(SecurityFinding))).scalar_one()
        assert second.fingerprint == first.fingerprint == "11"
        assert second.line == 99
        assert second.first_seen == first.first_seen


# ----------------------------------------------------------------------
# Migration 010 (in isolation — the 004+ chain needs Postgres for ALTER of
# constraints; production migrates on Postgres, dev bootstraps create_all)
# ----------------------------------------------------------------------


class TestMigration010:
    def test_security_findings_table_matches_model(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                spec = importlib.util.spec_from_file_location(
                    "migration_010",
                    Path(__file__).resolve().parent.parent
                    / "alembic"
                    / "versions"
                    / "010_security_findings.py",
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                module.upgrade()

            inspector = inspect(conn)
            names = {column["name"] for column in inspector.get_columns("security_findings")}
            index_names = {index["name"] for index in inspector.get_indexes("security_findings")}
        assert names == {
            "id",
            "run_id",
            "provider",
            "scope",
            "source",
            "fingerprint",
            "severity",
            "title",
            "path",
            "line",
            "identifiers",
            "status",
            "triage_note",
            "ref",
            "sha",
            "first_seen",
            "last_seen",
        }
        # The dedupe key IS the unique index.
        assert "uq_finding_per_source_scope_fingerprint" in index_names
        engine.dispose()

    def test_upgrade_downgrade_round_trip(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                spec = importlib.util.spec_from_file_location(
                    "migration_010_rt",
                    Path(__file__).resolve().parent.parent
                    / "alembic"
                    / "versions"
                    / "010_security_findings.py",
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                module.upgrade()
                assert "security_findings" in inspect(conn).get_table_names()
                module.downgrade()
                assert "security_findings" not in inspect(conn).get_table_names()
        engine.dispose()


# ----------------------------------------------------------------------
# GitHub client: dismiss enums (research §4.2, against pytest-httpx)
# ----------------------------------------------------------------------


class TestGitHubDismissEnums:
    async def test_invalid_reason_rejected_before_http(self):
        from forge.integrations.github import GitHubClient

        client = GitHubClient(
            base_url="https://api.github.test",
            token_provider=FakeGitHub().token_stub(),
        )
        with pytest.raises(ValueError, match="dismissed_reason"):
            await client.dismiss_code_scanning_alert("o", "r", 1, dismissed_reason="won't fix it")
        with pytest.raises(ValueError, match="resolution"):
            await client.resolve_secret_scanning_alert("o", "r", 1, resolution="false positive")
        with pytest.raises(ValueError, match="dismissed_reason"):
            await client.dismiss_dependabot_alert("o", "r", 1, dismissed_reason="false positive")
        await client.aclose()

    async def test_valid_reasons_use_the_documented_enums(self, httpx_mock):
        from forge.integrations.github import GitHubClient
        from tests.fixtures.fake_github import sample_installation_token
        from tests.test_github_client import BASE, MINT_URL, make_credentials

        httpx_mock.add_response(
            url=MINT_URL, method="POST", json=sample_installation_token("ghs_test")
        )
        httpx_mock.add_response(
            url=f"{BASE}/repos/o/r/code-scanning/alerts/7",
            method="PATCH",
            json={"number": 7, "state": "dismissed"},
        )
        client = GitHubClient(base_url=BASE, token_provider=make_credentials(BASE))
        await client.dismiss_code_scanning_alert(
            "o", "r", 7, dismissed_reason="false positive", dismissed_comment="because"
        )
        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.read())
        assert body == {
            "state": "dismissed",
            "dismissed_reason": "false positive",  # spaces, not underscores
            "dismissed_comment": "because",
        }
        await client.aclose()
