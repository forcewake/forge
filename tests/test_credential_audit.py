"""Q39-03 (#322) — the append-only credential-redemption audit ledger.

The recorded defect (review 6df4020, probe P04): the redemption endpoint
read the whole ``FlowRun.evidence`` document, appended the receipt and
wrote the whole JSON back — a concurrent evidence write between the read
and the write was LOST, and the last-50 cap was the ONLY audit history.
These tests pin the replacement contract:

- THE LEDGER: INSERT-only, complete history (the cap is a projection
  parameter, never a retention rule), refs and metadata ONLY — a
  value-bearing audit document refuses the redemption outright;
- IDEMPOTENT RETRIES: a re-delivered receipt id bumps the retry counter
  on the SAME logical row; a duplicate with CONFLICTING identity facts
  is a typed refusal; logical totals never inflate;
- THE PROJECTION: ``evidence["credential_redemptions"]`` (the bounded
  summary, byte-compatible with the legacy shape) is rewritten through
  an optimistic CAS that re-reads on conflict — a concurrent evidence
  writer (the P04 schedule) survives, and the summary version tracks the
  ledger total;
- CONCURRENCY: two concurrent redemptions through two real sessions with
  a barrier preserve BOTH audit facts;
- RETENTION: a receipt is deletable only when no active attempt or
  investigation references its work — prunes report what they held;
- BACKFILL: embedded legacy evidence lands with
  ``provenance='legacy-embedded'``, no invented grant ids, idempotent
  re-runs, and a doctor check that reports the backlog.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.adaptive.credential_audit import (
    LEGACY_EMBEDDED_PROVENANCE,
    PROJECTION_LIMIT,
    AuditProjectionConflict,
    RedemptionConflict,
    RedemptionReceipt,
    backfill_embedded_redemptions,
    credential_audit_health,
    prune_expired_redemptions,
    recent_redemptions,
    record_redemption,
    retention_holds,
)
from forge.durable.models import CredentialRedemption, FlowRun
from forge.models.base import Base

WORK = "r" * 32
OTHER = "o" * 32


def receipt(
    receipt_id: str = "redemption-1",
    work_id: str = WORK,
    *,
    grant_id: str = "",
    **overrides,
) -> RedemptionReceipt:
    """One value-free receipt (the endpoint's R38-02/Q39-01 entry shape).

    The default expiry is NOW-RELATIVE (always an hour ahead): a
    hardcoded "future" timestamp is a time bomb — the 2026-09-25T13:00Z
    original expired mid-suite that day and turned the live receipt into
    a prunable one (found by the #334 full-suite gate; not a code
    defect). Tests that need an EXPIRED receipt pass their own
    ``entry_extra`` (also now-relative)."""
    entry = {
        "at": "2026-09-25T12:00:00+00:00",
        "redemption_id": receipt_id,
        "grant_id": grant_id,
        "subject": "gitlab/-/90210",
        "provider": "anthropic-gateway",
        "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
        "binding_revision": 1,
        "resolver": "env-broker",
        "attempt_generation": 2,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "broker_receipt_id": "br-1",
        "resolved_version_kind": "presence",
        "credential_policy": "default",
    }
    entry.update(overrides.pop("entry_extra", {}))
    built = RedemptionReceipt.from_audit_entry(work_id, entry)
    return RedemptionReceipt(**{**built.__dict__, **overrides}) if overrides else built


@pytest.fixture()
async def factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'credential-audit.db'}",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(FlowRun(id=WORK, project_id=1, provider="gitlab"))
        session.add(FlowRun(id=OTHER, project_id=1, provider="gitlab"))
        await session.commit()
    yield maker
    await engine.dispose()


async def _rows(factory, work_id: str = WORK) -> list[CredentialRedemption]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(CredentialRedemption).where(CredentialRedemption.work_id == work_id)
                )
            )
            .scalars()
            .all()
        )


async def _evidence(factory, work_id: str = WORK) -> dict:
    async with factory() as session:
        run = await session.get(FlowRun, work_id)
        return dict(run.evidence or {})


# ----------------------------------------------------------------------
# The ledger — INSERT-only, complete, value-free
# ----------------------------------------------------------------------


class TestTheLedger:
    async def test_a_redemption_lands_before_return_and_projects_the_summary(self, factory):
        receipt_id = await record_redemption(factory, receipt())

        assert receipt_id == "redemption-1"
        rows = await _rows(factory)
        assert len(rows) == 1
        assert rows[0].grant_id == ""  # empty is labelled, never invented
        assert rows[0].attempt_generation == 2
        assert rows[0].credential_policy == "default"
        # the projection: the bounded evidence summary + its observables
        evidence = await _evidence(factory)
        summary = evidence["credential_redemptions"]
        assert summary[0]["redemption_id"] == "redemption-1"
        assert summary[0]["attempt_generation"] == 2
        observables = evidence["credential_redemptions_summary"]
        assert observables["total"] == observables["version"] == 1
        assert observables["capped_to"] == PROJECTION_LIMIT
        assert observables["authority"] == "credential_redemptions"

    async def test_value_material_refuses_the_redemption(self, factory):
        with pytest.raises(ValueError, match="value material"):
            await record_redemption(
                factory, receipt(entry_extra={"staged_env": {"ANTHROPIC_API_KEY": "sk-x"}})
            )
        assert await _rows(factory) == []
        # every forbidden spelling, not just one
        with pytest.raises(ValueError):
            await record_redemption(factory, receipt(entry_extra={"VALUE": "sk-x"}))
        assert await _rows(factory) == []

    async def test_an_overlength_identity_is_rejected_never_truncated(self, factory):
        with pytest.raises(ValueError, match="durable width"):
            await record_redemption(factory, receipt(receipt_id="x" * 65))
        assert await _rows(factory) == []

    async def test_a_duplicate_receipt_id_counts_the_retry_never_re_counts(self, factory):
        await record_redemption(factory, receipt())
        receipt_id = await record_redemption(factory, receipt())

        assert receipt_id == "redemption-1"
        rows = await _rows(factory)
        assert len(rows) == 1  # ONE logical redemption
        assert rows[0].retry_count == 1
        assert rows[0].last_retry_at is not None
        await record_redemption(factory, receipt())
        rows = await _rows(factory)
        assert len(rows) == 1 and rows[0].retry_count == 2
        # and the projection never inflates the logical total
        evidence = await _evidence(factory)
        assert evidence["credential_redemptions_summary"]["total"] == 1

    async def test_a_duplicate_with_conflicting_facts_refuses(self, factory):
        await record_redemption(factory, receipt())
        with pytest.raises(RedemptionConflict, match="conflicting"):
            await record_redemption(factory, receipt(credential_ref="env:OTHER"))
        rows = await _rows(factory)
        assert len(rows) == 1 and rows[0].retry_count == 0  # untouched

    async def test_more_than_fifty_receipts_retain_full_history(self, factory):
        for index in range(PROJECTION_LIMIT + 5):
            await record_redemption(factory, receipt(receipt_id=f"redemption-{index:03d}"))

        rows = await _rows(factory)
        assert len(rows) == PROJECTION_LIMIT + 5  # the cap NEVER trims the ledger
        evidence = await _evidence(factory)
        assert len(evidence["credential_redemptions"]) == PROJECTION_LIMIT  # bounded summary
        assert evidence["credential_redemptions_summary"]["total"] == PROJECTION_LIMIT + 5
        # the summary holds the NEWEST 50; the full history is a read away
        recent = await recent_redemptions(factory, WORK, limit=PROJECTION_LIMIT + 5)
        assert len(recent) == PROJECTION_LIMIT + 5
        assert recent[0]["redemption_id"] == "redemption-054"  # newest first

    async def test_recent_redemptions_is_a_read_scoped_to_the_work(self, factory):
        await record_redemption(factory, receipt())
        await record_redemption(factory, receipt("other-1", work_id=OTHER))
        only_ours = await recent_redemptions(factory, WORK)
        assert [row["redemption_id"] for row in only_ours] == ["redemption-1"]


# ----------------------------------------------------------------------
# The P04 counterexample — concurrent evidence writers survive
# ----------------------------------------------------------------------


class TestConcurrentWritersSurvive:
    async def test_two_concurrent_redemptions_preserve_both_facts(self, factory):
        barrier = asyncio.Barrier(2)

        async def redeem(receipt_id: str) -> None:
            # each task holds its OWN session; the barrier makes both
            # read-modify-write windows overlap for real
            async def run() -> None:
                await barrier.wait()
                await record_redemption(factory, receipt(receipt_id))

            await run()

        await asyncio.gather(redeem("redemption-a"), redeem("redemption-b"))

        rows = await _rows(factory)
        assert sorted(row.receipt_id for row in rows) == ["redemption-a", "redemption-b"]
        evidence = await _evidence(factory)
        assert sorted(entry["redemption_id"] for entry in evidence["credential_redemptions"]) == [
            "redemption-a",
            "redemption-b",
        ]

    async def test_a_simultaneous_evidence_write_survives_the_audit(self, factory):
        """The P04 schedule, repeated: a competing evidence writer updates
        an UNRELATED key while the redemption audit runs. The acceptance
        direction: the COMPETITOR'S write survives every interleaving
        (the old whole-document audit overwrite lost it whenever its read
        went first), and the LEDGER never loses a receipt. The bounded
        summary can be clobbered by a *naive* competitor's own blind
        write — it is a projection: the next redemption rebuilds it from
        the ledger with every surviving key intact (asserted at the end).
        """
        for round_index in range(12):
            key = f"harness-{round_index}"

            async def competing_writer() -> None:
                async with factory() as session:
                    run = await session.get(FlowRun, WORK)
                    evidence = dict(run.evidence or {})
                    evidence[key] = {"attempt": round_index}
                    run.evidence = evidence
                    await session.commit()

            barrier = asyncio.Barrier(2)

            async def audited_redemption() -> None:
                await barrier.wait()
                await record_redemption(factory, receipt(f"redemption-{round_index:03d}"))

            async def writer_after_barrier() -> None:
                await barrier.wait()
                await competing_writer()

            await asyncio.gather(audited_redemption(), writer_after_barrier())

            # the competing evidence write ALWAYS survives (P04's defect)
            evidence = await _evidence(factory)
            assert key in evidence, f"round {round_index}: the competing write was lost"
            # the LEDGER never loses the audit fact (the table, not the summary)
            rows = await _rows(factory)
            assert f"redemption-{round_index:03d}" in {row.receipt_id for row in rows}, (
                f"round {round_index}: the redemption audit was lost"
            )

        # convergence: one more redemption rebuilds the projection FROM
        # THE LEDGER — the summary's total is the full history and every
        # surviving evidence key (all twelve) is preserved by the CAS.
        await record_redemption(factory, receipt("redemption-final"))
        evidence = await _evidence(factory)
        assert evidence["credential_redemptions_summary"]["total"] == 13
        for round_index in range(12):
            assert f"harness-{round_index}" in evidence

    async def test_a_projection_failure_refuses_the_response_and_keeps_the_ledger(
        self, factory, monkeypatch
    ):
        """An audit failure PREVENTS the credential response: a projection
        that cannot land raises the typed conflict — the LEDGER row stands
        (committed first), and the exception is what the endpoint turns
        into its 503."""
        from forge.adaptive import credential_audit

        async def refusing_projection(session_factory, work_id, *, limit=50):
            raise AuditProjectionConflict("the CAS could not win — busy evidence document")

        monkeypatch.setattr(credential_audit, "_project_summary", refusing_projection)
        with pytest.raises(AuditProjectionConflict):
            await record_redemption(factory, receipt())
        rows = await _rows(factory)
        assert len(rows) == 1 and rows[0].receipt_id == "redemption-1"


# ----------------------------------------------------------------------
# Retention and the doctor check
# ----------------------------------------------------------------------


class TestRetentionAndDoctor:
    async def test_an_active_attempt_holds_its_receipts(self, factory):
        await record_redemption(
            factory,
            receipt(
                entry_extra={
                    "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                }
            ),
        )
        decision = await retention_holds(factory, WORK)
        assert decision.held and "active_attempt" in decision.reasons
        report = await prune_expired_redemptions(factory)
        assert report["deleted"] == 0  # an ACTIVE run's evidence never prunes
        assert "active_attempt" in report["held_works"][WORK]

    async def test_an_investigation_hold_blocks_pruning_even_terminal(self, factory):
        async with factory() as session:
            run = await session.get(FlowRun, WORK)
            run.status = "failed"
            run.evidence = {"credential_investigation_open": True}
            await session.commit()
        await record_redemption(
            factory,
            receipt(
                entry_extra={
                    "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                }
            ),
        )
        decision = await retention_holds(factory, WORK)
        assert decision.reasons == ("investigation",)
        report = await prune_expired_redemptions(factory)
        assert report["deleted"] == 0

    async def test_a_terminal_unheld_work_prunes_only_expired_receipts(self, factory):
        async with factory() as session:
            await session.execute(update(FlowRun).where(FlowRun.id == WORK).values(status="failed"))
            await session.commit()
        expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await record_redemption(
            factory, receipt("expired-1", entry_extra={"expires_at": expired_at})
        )
        await record_redemption(factory, receipt("live-1"))
        report = await prune_expired_redemptions(factory)
        assert report["deleted"] == 1
        remaining = {row.receipt_id for row in await _rows(factory)}
        assert remaining == {"live-1"}  # the unexpired receipt stays


# ----------------------------------------------------------------------
# The legacy backfill
# ----------------------------------------------------------------------


class TestLegacyBackfill:
    async def _put_legacy_run(self, factory, entries: list[dict]) -> None:
        async with factory() as session:
            run = await session.get(FlowRun, OTHER)
            run.evidence = {
                "credential_redemptions": entries,
                "harness": {"keep": True},
            }
            await session.commit()

    async def test_embedded_evidence_backfills_with_legacy_provenance(self, factory):
        entry = {
            "at": "2026-09-20T10:00:00+00:00",
            "redemption_id": "legacy-abc",
            "subject": "gitlab/-/90210",
            "provider": "anthropic-gateway",
            "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
            "binding_revision": 1,
            "resolver": "env-broker",
            "attempt_generation": 1,
        }
        await self._put_legacy_run(factory, [entry])

        report = await backfill_embedded_redemptions(factory)

        assert report.works_scanned == 1 and report.created == 1
        assert report.grant_ids_invented == 0
        rows = await _rows(factory, OTHER)
        assert len(rows) == 1
        assert rows[0].provenance == LEGACY_EMBEDDED_PROVENANCE
        assert rows[0].grant_id == ""  # never invented
        assert "none is invented" in rows[0].details["grant"]
        # the embedded evidence STAYS (readable during migration)
        evidence = await _evidence(factory, OTHER)
        assert evidence["credential_redemptions"] == [entry]
        assert evidence["harness"] == {"keep": True}

    async def test_backfill_is_idempotent_and_digests_idless_entries(self, factory):
        await self._put_legacy_run(
            factory,
            [
                {"redemption_id": "legacy-abc", "provider": "anthropic-gateway"},
                {"provider": "anthropic-gateway", "at": "2026-09-20T10:00:00+00:00"},
            ],
        )
        first = await backfill_embedded_redemptions(factory)
        second = await backfill_embedded_redemptions(factory)

        assert first.created == 2
        assert second.created == 0 and second.already_present == 2
        rows = await _rows(factory, OTHER)
        assert len(rows) == 2
        digested = [row for row in rows if row.receipt_id.startswith("legacy:")]
        assert len(digested) == 1  # a STABLE digest id — identical on re-runs

    async def test_value_bearing_legacy_entries_are_skipped_reported(self, factory):
        await self._put_legacy_run(factory, [{"redemption_id": "bad-1", "value": "sk-x"}])
        report = await backfill_embedded_redemptions(factory)
        assert report.created == 0
        assert any("value-material" in skip for skip in report.skipped_entries)
        assert await _rows(factory, OTHER) == []

    async def test_the_doctor_reports_the_backlog_and_the_ledger(self, factory):
        await self._put_legacy_run(
            factory, [{"redemption_id": "legacy-abc", "provider": "anthropic-gateway"}]
        )
        await record_redemption(factory, receipt())

        health = await credential_audit_health(factory)

        assert health["embedded_backlog"] == {OTHER: 1}
        assert health["ledger_receipts"] == 1

        await backfill_embedded_redemptions(factory)
        health = await credential_audit_health(factory)
        assert health["backfilled"] == {OTHER: 1}


# ----------------------------------------------------------------------
# The PG-gated variant — real independent sessions, a real barrier
# ----------------------------------------------------------------------


class TestRealPostgres:
    @pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason=(
            "FORGE_PG_TEST_URL not set — the real-PostgreSQL P04 barrier proof "
            "runs only against a disposable real Postgres"
        ),
    )
    async def test_concurrent_redemptions_and_evidence_writers_survive(self):
        engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        try:
            from forge.durable.models import UsageIngestionConflict, UsageReceipt

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    CredentialRedemption.__table__.delete().where(
                        CredentialRedemption.work_id == WORK
                    )
                )
                # the shared disposable database may hold usage rows for
                # this run id from a sibling PG-gated proof — clear them
                # before the run row itself (the FK would refuse).
                await conn.execute(delete(UsageReceipt).where(UsageReceipt.run_id == WORK))
                await conn.execute(
                    delete(UsageIngestionConflict).where(UsageIngestionConflict.run_id == WORK)
                )
                await conn.execute(FlowRun.__table__.delete().where(FlowRun.id == WORK))
                await conn.execute(
                    FlowRun.__table__.insert().values(
                        [
                            {
                                "id": WORK,
                                "project_id": 1,
                                "provider": "gitlab",
                                "evidence": {"harness": {"attempt": 0}},
                            }
                        ]
                    )
                )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            barrier = asyncio.Barrier(2)

            async def native_handle_writer() -> None:
                async with factory() as session:
                    await barrier.wait()
                    run = await session.get(FlowRun, WORK)
                    evidence = dict(run.evidence or {})
                    evidence["harness"] = {"attempt": 1, "native_handle": "gh:run:42"}
                    run.evidence = evidence
                    await session.commit()

            async def redemption() -> None:
                await barrier.wait()
                await record_redemption(factory, receipt("pg-redemption-1"))

            await asyncio.gather(native_handle_writer(), redemption())

            async with factory() as session:
                run = await session.get(FlowRun, WORK)
                count = await session.scalar(select(func.count()).select_from(CredentialRedemption))
            # BOTH facts survived: the native-handle evidence AND the audit
            # LEDGER row (the bounded summary is a projection — a naive
            # whole-document writer may clobber it; the ledger never lies).
            assert run.evidence["harness"] == {"attempt": 1, "native_handle": "gh:run:42"}
            assert count == 1
            ledger = await recent_redemptions(factory, WORK)
            assert [row["receipt_id"] for row in ledger] == ["pg-redemption-1"]
        finally:
            await engine.dispose()
