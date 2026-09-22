"""Security findings: fingerprints, the dedupe upsert, both ingestions.

Ground truth: docs/research/2026-09-14-ci-security-surface.md §3 (report schema,
alert field sets, tier matrix) and §4.1 (forge-computed GitLab
fingerprints; absence from one scan is never a fix). R25/R26 coverage:
the DB-native concurrent upsert, scan-completeness records, the
connection-scoped dedupe key and the reappearance rule live here.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

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
from forge.findings.models import (
    SCAN_RETENTION_DAYS,
    ScanExecution,
    SecurityFinding,
    SecurityFindingAction,
    normalize_severity,
)
from forge.findings.triage import open_findings_for_scope
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
        # The dedupe key is (connection, source, scope, fingerprint): an
        # identical identity under gitlab_secret never collides with
        # gitlab_sast.
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

    async def test_last_seen_is_monotonic_under_a_late_older_scan(self, db_factory):
        """An out-of-order (older) scan never rolls last_seen backwards."""
        now = datetime.now(timezone.utc)
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [finding("7" * 64)],
                    provider="gitlab",
                    scope="42",
                    seen_at=now,
                )
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [finding("7" * 64)],
                    provider="gitlab",
                    scope="42",
                    seen_at=now - timedelta(hours=1),  # reordered/late delivery
                )
        row = (await self._rows(db_factory))[0]
        assert row.last_seen == now.replace(tzinfo=None)


# ----------------------------------------------------------------------
# R26: the dedupe is DB-native — concurrent ingests collapse to one row
# ----------------------------------------------------------------------


class TestConcurrentUpsert:
    async def test_two_concurrent_ingests_of_one_finding_create_one_row(self, tmp_path):
        """Two simultaneous ingest passes hit the unique index, not a race.

        The upsert is a single INSERT … ON CONFLICT DO UPDATE against the
        unique (connection, source, scope, fingerprint) index, so both
        passes converge on ONE row — no SELECT+INSERT race, no IntegrityError
        leak, and exactly one created row overall.
        """
        database = f"sqlite+aiosqlite:///{tmp_path}/findings.db"
        engines = [create_async_engine(database) for _ in range(2)]
        try:
            for engine in engines:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
            factories = [async_sessionmaker(engine, expire_on_commit=False) for engine in engines]

            fake = FakeGitLab()
            seed_sast_pipeline(fake, pipeline_id=990, job_id=991)
            # Two concurrent full ingestion passes over the SAME pipeline.
            results = await asyncio.gather(
                ingest_gitlab_pipeline(fake, factories[0], 42, 990),
                ingest_gitlab_pipeline(fake, factories[1], 42, 990),
            )
            assert all(result.errors == [] for result in results)
        finally:
            for engine in engines:
                await engine.dispose()

        store = async_sessionmaker(create_async_engine(database), expire_on_commit=False)
        try:
            async with store() as session:
                rows = (await session.execute(select(SecurityFinding))).scalars().all()
            assert len(rows) == 2  # the two distinct pipeline findings — deduped
            assert len({row.fingerprint for row in rows}) == 2
            assert (
                sum(result.created for result in results)
                + sum(result.updated for result in results)
                == 4
            )  # every pass accounted for both findings exactly once
        finally:
            await store.kw["bind"].dispose()  # type: ignore[attr-defined]

    async def test_parallel_upsert_sessions_stay_unique(self, db_factory):
        """Direct upsert races through separate sessions also converge."""
        await asyncio.gather(
            *(self._one_ingest(db_factory, seen_at=datetime.now(timezone.utc)) for _ in range(2))
        )
        async with db_factory() as session:
            rows = (await session.execute(select(SecurityFinding))).scalars().all()
        assert len(rows) == 1

    async def _one_ingest(self, db_factory, *, seen_at: datetime) -> None:
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [finding("8" * 64, title="Raced finding")],
                    provider="gitlab",
                    scope="42",
                    seen_at=seen_at,
                )


# ----------------------------------------------------------------------
# R26: ScanExecution — a partial scan never looks clean
# ----------------------------------------------------------------------


class TestScanExecution:
    async def _scans(self, db_factory) -> list[ScanExecution]:
        async with db_factory() as session:
            return (
                (await session.execute(select(ScanExecution).order_by(ScanExecution.source)))
                .scalars()
                .all()
            )

    async def test_clean_report_records_complete_scan(self, db_factory):
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=710, job_id=711)
        result = await ingest_gitlab_pipeline(fake, db_factory, 42, 710)
        assert result.scan_complete is True
        scans = await self._scans(db_factory)
        assert len(scans) == 1
        assert scans[0].source == "gitlab_sast"
        assert scans[0].completeness == "complete"
        assert scans[0].external_id == "710"
        assert scans[0].parsed == 2
        assert scans[0].created == 2
        # Retention: scan records are observability metadata with a purge date.
        assert scans[0].retention_until is not None
        delta = scans[0].retention_until.replace(tzinfo=None) - (
            scans[0].observed_at.replace(tzinfo=None)
        )
        assert delta == timedelta(days=SCAN_RETENTION_DAYS)

    async def test_expired_artifact_marks_scan_incomplete(self, db_factory):
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=720, job_id=721)
        del fake.job_artifacts[721]["gl-sast-report.json"]  # artifact expired (404)
        result = await ingest_gitlab_pipeline(fake, db_factory, 42, 720)
        assert result.created == 0
        assert result.scan_complete is False
        scans = await self._scans(db_factory)
        assert len(scans) == 1
        assert scans[0].completeness == "incomplete"
        assert "404" in scans[0].errors[0]

    async def test_pipeline_without_report_jobs_is_incomplete(self, db_factory):
        fake = FakeGitLab()
        fake.pipelines.append({"id": 730, "ref": "main", "status": "success", "sha": "d" * 40})
        fake.set_pipeline_jobs(
            730, [{"id": 731, "name": "build", "stage": "build", "status": "success"}]
        )
        result = await ingest_gitlab_pipeline(fake, db_factory, 42, 730)
        assert result.created == 0
        assert result.scan_complete is False  # no evidence — never looks clean
        scans = await self._scans(db_factory)
        assert len(scans) == 1
        assert scans[0].completeness == "incomplete"
        assert "no security report jobs" in scans[0].errors[0]

    async def test_github_403_surface_records_incomplete_scan(self, db_factory):
        fake = FakeGitHub()
        fake.code_scanning_disabled = True
        fake.dependabot_alerts["o/r"] = [dependabot_alert(31)]
        result = await ingest_github_alerts(fake, db_factory, "o", "r")
        assert result.created == 1
        assert result.scan_complete is False  # dependabot ingested, scan still partial
        scans = {(scan.source, scan.completeness) for scan in await self._scans(db_factory)}
        assert ("github_code_scanning", "incomplete") in scans
        assert ("github_dependabot", "complete") in scans

    async def test_partial_gitlab_scan_marks_only_the_failed_source(self, db_factory):
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=740, job_id=741)
        del fake.job_artifacts[741]["gl-sast-report.json"]
        fake.set_pipeline_jobs(
            740,
            [
                {"id": 741, "name": "sast", "stage": "test", "status": "success"},
                {"id": 742, "name": "secret_detection", "stage": "test", "status": "success"},
            ],
        )
        fake.seed_job_artifact(
            742,
            "gl-secret-detection-report.json",
            json.dumps(sast_report(VULNS[:1])),
        )
        result = await ingest_gitlab_pipeline(fake, db_factory, 42, 740)
        assert result.created == 1  # the secret surface still ingested
        assert result.scan_complete is False
        scans = {(scan.source, scan.completeness) for scan in await self._scans(db_factory)}
        assert scans == {
            ("gitlab_sast", "incomplete"),
            ("gitlab_secret", "complete"),
        }


# ----------------------------------------------------------------------
# R26: the connection leads the dedupe key — same project id, no mixing
# ----------------------------------------------------------------------


class TestCrossConnectionIsolation:
    async def test_same_scope_two_connections_stay_separate(self, db_factory):
        for connection_id in ("conn-a", "conn-b"):
            async with db_factory() as session:
                async with session.begin():
                    await upsert_findings(
                        session,
                        [finding("5" * 64, title=f"from {connection_id}")],
                        provider="gitlab",
                        scope="42",
                        connection_id=connection_id,
                    )
        rows = await self._all(db_factory)
        assert len(rows) == 2  # one row per connection, never merged
        assert {row.connection_id for row in rows} == {"conn-a", "conn-b"}

    async def test_default_connection_rows_are_distinct_from_named_ones(self, db_factory):
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [finding("6" * 64)],
                    provider="gitlab",
                    scope="42",  # connection_id defaults to ""
                )
        async with db_factory() as session:
            async with session.begin():
                await upsert_findings(
                    session,
                    [finding("6" * 64, title="other connection")],
                    provider="gitlab",
                    scope="42",
                    connection_id="gitlab:other.example.com",
                )
        assert len(await self._all(db_factory)) == 2

    async def test_triage_queries_are_connection_scoped(self, db_factory):
        """A triage pass on connection A never sees or touches B's rows."""
        for connection_id, status in (("conn-a", "open"), ("conn-b", "open")):
            async with db_factory() as session:
                async with session.begin():
                    await upsert_findings(
                        session,
                        [finding("4" * 64, title=f"on {connection_id}")],
                        provider="gitlab",
                        scope="42",
                        connection_id=connection_id,
                    )
        batch = await open_findings_for_scope(db_factory, "42", connection_id="conn-a")
        assert [row.connection_id for row in batch] == ["conn-a"]
        # Conn-B's identical finding stays untouched by A's verdict writes.
        from forge.agents.models import SecurityFindingResult, SecurityTriageResult
        from forge.findings.triage import apply_triage_verdicts, observe_batch

        observed = observe_batch(batch)
        result = SecurityTriageResult(
            summary="s",
            findings=[
                SecurityFindingResult(
                    id="4" * 64,
                    severity="high",
                    category="testing",
                    description="d",
                    remediation="fix",
                    is_false_positive=True,
                    justification="not real",
                )
            ],
        )
        applied = await apply_triage_verdicts(
            db_factory,
            "42",
            result,
            connection_id="conn-a",
            observed=observed,
            auto_accept=True,
        )
        assert applied.applied == ["4" * 64]
        rows = await self._all(db_factory)
        by_connection = {row.connection_id: row for row in rows}
        assert by_connection["conn-a"].status == "false_positive"
        assert by_connection["conn-b"].status == "open"  # isolated

    async def _all(self, db_factory) -> list[SecurityFinding]:
        async with db_factory() as session:
            return (await session.execute(select(SecurityFinding))).scalars().all()


# ----------------------------------------------------------------------
# R25 §4: a reappearing finding gets a fresh assessment, never the old
# suppression silently restored
# ----------------------------------------------------------------------


class TestReappearance:
    async def test_suppressed_finding_reappearing_after_absence_is_reopened(self, db_factory):
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=800, job_id=801)  # both vulns
        await ingest_gitlab_pipeline(fake, db_factory, 42, 800)

        # Triage suppresses "Weak hash".
        async with db_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(SecurityFinding).where(SecurityFinding.title == "Weak hash")
                    )
                ).scalar_one()
                row.status = "false_positive"
                row.suggested_verdict = "false_positive"
                row.triage_note = "stale suppression"
                row.version += 1

        # Next scan: the finding is ABSENT (branch-scoped scan / fix).
        absent = [v for v in VULNS if v["name"] == "Hardcoded secret"]
        seed_sast_pipeline(fake, pipeline_id=802, job_id=803, report=sast_report(absent))
        await ingest_gitlab_pipeline(fake, db_factory, 42, 802)
        async with db_factory() as session:
            row = (
                await session.execute(
                    select(SecurityFinding).where(SecurityFinding.title == "Weak hash")
                )
            ).scalar_one()
            assert row.status == "false_positive"  # absence alone never reopens
            assert row.absent_in_last_scan is True

        # It REAPPEARS (reintroduced code): the old suppression must not
        # silently hold — a fresh evidence assessment starts.
        seed_sast_pipeline(fake, pipeline_id=804, job_id=805)  # both vulns again
        await ingest_gitlab_pipeline(fake, db_factory, 42, 804)
        async with db_factory() as session:
            row = (
                await session.execute(
                    select(SecurityFinding).where(SecurityFinding.title == "Weak hash")
                )
            ).scalar_one()
            actions = (
                (
                    await session.execute(
                        select(SecurityFindingAction).order_by(SecurityFindingAction.id)
                    )
                )
                .scalars()
                .all()
            )
        assert row.status == "open"  # reopened for assessment
        assert row.suggested_verdict is None  # stale suggestion cleared
        assert row.absent_in_last_scan is False
        reopens = [a for a in actions if a.action == "reopen"]
        assert len(reopens) == 1
        assert reopens[0].before_status == "false_positive"
        assert reopens[0].after_status == "open"
        assert reopens[0].actor == "ingest:reappearance"
        # The continuously-present finding was never touched.
        async with db_factory() as session:
            other = (
                await session.execute(
                    select(SecurityFinding).where(SecurityFinding.title == "Hardcoded secret")
                )
            ).scalar_one()
        assert other.status == "open" and other.version == 0

    async def test_suppressed_finding_present_in_every_scan_stays_suppressed(self, db_factory):
        """Continuous presence does NOT reopen — only true reappearance does."""
        fake = FakeGitLab()
        seed_sast_pipeline(fake, pipeline_id=810, job_id=811)
        await ingest_gitlab_pipeline(fake, db_factory, 42, 810)
        async with db_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(SecurityFinding).where(SecurityFinding.title == "Weak hash")
                    )
                ).scalar_one()
                row.status = "false_positive"
                row.version += 1

        seed_sast_pipeline(fake, pipeline_id=812, job_id=813)
        await ingest_gitlab_pipeline(fake, db_factory, 42, 812)
        async with db_factory() as session:
            row = (
                await session.execute(
                    select(SecurityFinding).where(SecurityFinding.title == "Weak hash")
                )
            ).scalar_one()
            actions = (await session.execute(select(SecurityFindingAction))).scalars().all()
        assert row.status == "false_positive"  # suppression stands
        assert row.absent_in_last_scan is False
        assert [a.action for a in actions if a.action == "reopen"] == []


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
# Migration 016 (R25/R26) — columns, connection-led key, new tables
# ----------------------------------------------------------------------


class TestMigration016:
    def _run(self, conn, filename: str, direction: str):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            spec = importlib.util.spec_from_file_location(
                filename,
                Path(__file__).resolve().parent.parent / "alembic" / "versions" / filename,
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            getattr(module, direction)()

    def test_upgrade_adds_governance_columns_and_tables(self):
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            # The findings table starts at its 010 shape.
            self._run(conn, "010_security_findings.py", "upgrade")
            self._run(conn, "016_findings_triage_governance.py", "upgrade")

            inspector = inspect(conn)
            columns = {column["name"] for column in inspector.get_columns("security_findings")}
            index_names = {index["name"] for index in inspector.get_indexes("security_findings")}
            tables = inspector.get_table_names()
        assert {"suggested_verdict", "suggested_at", "suggested_by", "version"} <= columns
        assert {"connection_id", "absent_in_last_scan"} <= columns
        # The dedupe key is connection-led now (R26).
        assert "uq_finding_per_source_scope_fingerprint" not in index_names
        assert "uq_finding_per_connection_source_scope_fingerprint" in index_names
        assert "security_scan_executions" in tables
        assert "security_finding_actions" in tables
        engine.dispose()

    def test_upgrade_downgrade_round_trip(self):
        from sqlalchemy import create_engine, inspect

        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            self._run(conn, "010_security_findings.py", "upgrade")
            self._run(conn, "016_findings_triage_governance.py", "upgrade")
            assert "security_finding_actions" in inspect(conn).get_table_names()
            self._run(conn, "016_findings_triage_governance.py", "downgrade")
            inspector = inspect(conn)
            assert "security_finding_actions" not in inspector.get_table_names()
            assert "security_scan_executions" not in inspector.get_table_names()
            index_names = {index["name"] for index in inspector.get_indexes("security_findings")}
            assert "uq_finding_per_source_scope_fingerprint" in index_names
            columns = {column["name"] for column in inspector.get_columns("security_findings")}
            assert "suggested_verdict" not in columns and "connection_id" not in columns
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
