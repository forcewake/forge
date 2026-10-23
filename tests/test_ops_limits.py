"""Native capacity, restart, control CAS and retention remain actual runtime contracts."""

from __future__ import annotations
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from forge.adaptive.admission import (
    AdmissionPolicy,
    ExecutionLease,
    NativeStatus,
    clear_native_start_intent,
    definite_start_refusal,
    lease_occupancy,
    reconcile_draining,
    record_native_handle,
    record_native_start_intent,
    release_lease_with_evidence,
    saturation_report,
    admission_report,
    try_acquire_lease,
)
from forge.adaptive.checkpoint_repository import FilesystemCheckpointRepository
from forge.adaptive.credential_audit import (
    RedemptionReceipt,
    prune_expired_redemptions,
    record_redemption,
    retention_holds,
)
from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun
from forge.main import create_app
from forge.models.base import Base

SECRET = "operator-secret"  # noqa: S105 — fake value for tests


NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


PROJECT_ID = 42


CHECKPOINT_A = "e" * 64


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/operator.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_LANE_CONTROL_SECRET=SecretStr(SECRET),
    )
    values.update(overrides)
    return Settings(**values)


class RecordingRepository:
    """The injected checkpoint authority — reads only (the entry the
    resume command's checkpoint_ref names)."""

    def __init__(self, entry: dict | None = None) -> None:
        self.entry_document = entry

    async def entry(self, work_id: str) -> dict | None:
        return dict(self.entry_document) if self.entry_document else None

    async def put(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003 — recorder
        return None

    async def read(self, work_id: str):
        return None

    async def lookup_outcome(self, work_id: str):
        return None

    async def authority(self) -> str:
        return "recording"

    async def pin(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003 — recorder
        return False

    async def unpin(self, *args, **kwargs) -> int:  # noqa: ANN002, ANN003 — recorder
        return 0

    async def pins(self, work_id: str | None = None) -> list[dict]:
        return []


@pytest.fixture()
async def app(tmp_path):
    reset_engine()
    application = create_app(settings=_settings(tmp_path))
    async with application.router.lifespan_context(application):
        yield application
    reset_engine()


@pytest.fixture()
def repository(app) -> RecordingRepository:
    recording = RecordingRepository(
        entry={
            "checkpoint_id": CHECKPOINT_A,
            "sequence": 2,
            "files": 2,
            "uploaded_at": (NOW - timedelta(minutes=60)).isoformat(),
        }
    )
    app.state.operator_checkpoint_repository = recording
    return recording


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


POLICY = AdmissionPolicy(max_active_per_project=3, max_queued_runs=10)


class SlowNativeLane:
    """A fake native lane with INJECTABLE faults — realistic runner
    latency and vendor throttling (the ops_drills pattern, minimal for
    these drills). Occupancy resolves ONLY through probes."""

    def __init__(self, *, start_latency: float = 0.01) -> None:
        self.start_latency = start_latency
        self.starts = 0
        self.statuses: dict[str, NativeStatus] = {}

    async def start(self, key: str) -> str:
        await asyncio.sleep(self.start_latency)  # the slow runner's real start cost
        self.starts += 1
        self.statuses[key] = NativeStatus.RUNNING
        return f"{key}#job-{self.starts}"

    async def finish(self, key: str) -> None:
        self.statuses[key] = NativeStatus.TERMINAL

    async def probe(self, key: str) -> NativeStatus:
        return self.statuses.get(key, NativeStatus.UNKNOWN)


async def _seed_run(db, run_id: str, status: str = "validating", **over) -> str:
    values: dict[str, Any] = {
        "id": run_id,
        "project_id": PROJECT_ID,
        "provider": "gitlab",
        "status": status,
        "status_reason": "",
        "evidence": {},
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=30),
    }
    values.update(over)
    async with db() as session:
        session.add(FlowRun(**values))
        await session.commit()
    return run_id


class TestCappedAdmissionUnderSlowRunnerAndThrottling:
    async def test_the_cap_holds_and_the_verdict_follows_occupancy_not_intake(self, db) -> None:
        """Slow runners hold all three slots; the fourth dispatch parks;
        releasing ONE slot from evidence flips the verdict while the
        QUEUED INTAKE GREW — the drill's one pinned sentence."""
        lane = SlowNativeLane(start_latency=0.01)
        run_ids = [await _seed_run(db, f"run-{slot}") for slot in range(4)]
        for run_id in run_ids[:3]:
            lease = await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run_id, provider="lab")
            assert lease is not None
            await record_native_start_intent(db, run_id, f"gitlab:pipeline:{run_id}@lane")
            handle = await lane.start(f"gitlab:pipeline:{run_id}@lane")
            await record_native_handle(lease.lease_id, handle, db)

        # the cap: the fourth dispatch parks (no lease — honest None)
        assert (
            await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run_ids[3], provider="lab")
            is None
        )
        report = await saturation_report(POLICY, PROJECT_ID, db, provider="lab")
        assert report["execution.occupied_vs_limit"] == {
            "occupied": 3,
            "limit": 3,
            "available": 0,
            "at_limit": True,
        }

        # one slot frees from EVIDENCE (its native job observed terminal)
        await lane.finish(f"gitlab:pipeline:{run_ids[0]}@lane")
        outcome = await release_lease_with_evidence(
            db, run_ids[0], reason="terminal", native_terminal=True
        )
        assert outcome.released == 1
        # the intake count GREW (9 queued now) — the verdict follows
        # occupancy and flips; the intake count never claimed a slot
        report = await saturation_report(POLICY, PROJECT_ID, db, provider="lab")
        assert report["execution.occupied_vs_limit"]["at_limit"] is False
        for number in range(9):
            await _seed_run(db, f"queued-{number}", status="accepted", provider="lab")
        accounting = await admission_report(POLICY, PROJECT_ID, db, provider="lab")
        assert accounting["admitted_work"] == 9
        assert accounting["execution_attempts"]["held"] == 2
        assert accounting["execution_attempts"]["completed"] == 1
        # and the parked dispatch now takes the freed slot
        assert await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run_ids[3], provider="lab")

    async def test_a_throttled_start_keeps_the_slot_until_evidence(self, db) -> None:
        run_id = await _seed_run(db, "run-429")
        lease = await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run_id, provider="lab")
        assert lease is not None
        await record_native_start_intent(db, run_id, "gitlab:pipeline:run-429@lane")

        # the vendor answers 429: NOT a definite refusal — the start may
        # have been accepted before the throttle, so occupancy stays
        # dispatched_unknown (the slot is held), never freed on the 429
        assert definite_start_refusal(429) is False
        assert definite_start_refusal(404) is True  # the definite contrast

        async with db() as session:
            row = await session.get(ExecutionLease, lease.lease_id)
        assert row is not None and lease_occupancy(row).value == "dispatched_unknown"

        # only a terminal probe observation frees it
        released = await release_lease_with_evidence(db, run_id, reason="terminal")
        assert released.released == 0 and released.drained == 1  # parked, not freed
        assert await reconcile_draining(db, native_status=lambda key: NativeStatus.TERMINAL) == 1

        # the pre-call abort spelling: a DEFINITE refusal clears the
        # intent so a never-started job never parks a slot (AT-06)
        run2 = await _seed_run(db, "run-404")
        assert await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run2, provider="lab")
        await record_native_start_intent(db, run2, "gitlab:pipeline:run-404@lane")
        assert await clear_native_start_intent(db, run2, "gitlab:pipeline:run-404@lane") == 1
        outcome = await release_lease_with_evidence(db, run2, reason="definite 404 refusal")
        assert outcome.released == 1  # proven never-dispatched — freed now


class TestControlPlaneRestart:
    async def test_occupancy_survives_the_restart_and_resolves_by_correlation(
        self, tmp_path
    ) -> None:
        """The restart drill: the engine is disposed and recreated over
        the SAME durable store (the process died; the database did not).
        Occupancy identities survive, an unknown native start parks
        draining, and the reconciler resolves it by its durable
        native_intent_ref through the fresh engine."""
        url = f"sqlite+aiosqlite:///{tmp_path}/restart.db"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(factory, "run-restart")
        assert await try_acquire_lease(POLICY, PROJECT_ID, factory, run_id=run_id, provider="lab")
        # the start response was LOST: intent persisted, no handle
        await record_native_start_intent(factory, run_id, "gitlab:pipeline:run-restart@lane")
        await engine.dispose()  # the control plane dies here

        # the fresh process: same store, fresh engine, nothing in memory
        revived = create_async_engine(url)
        fresh = async_sessionmaker(revived, expire_on_commit=False)
        report = await saturation_report(POLICY, PROJECT_ID, fresh, provider="lab")
        assert report["native_start.unknown_count"] == 1
        assert report["execution.occupied_vs_limit"]["occupied"] == 1
        # the run's local terminal transition WITHOUT native evidence
        # parks the lease draining — never frees on the local verdict
        parked = await release_lease_with_evidence(fresh, run_id, reason="run terminal")
        assert parked.drained == 1 and parked.released == 0

        # the reconciler resolves by the durable correlation prefix
        async def probe(key: str) -> NativeStatus:
            assert key.startswith("gitlab:")
            return NativeStatus.TERMINAL

        assert await reconcile_draining(fresh, native_status=probe) == 1
        report = await saturation_report(POLICY, PROJECT_ID, fresh, provider="lab")
        assert report["execution.occupied_vs_limit"]["occupied"] == 0
        await revived.dispose()


class TestConcurrentOperatorCommands:
    async def test_one_command_applies_per_world_and_the_stale_twin_expires(self, db) -> None:
        """Two operators race: both commands enter the durable mailbox;
        the CTL-04 CAS means exactly the command matching the CURRENT
        world dispatches — the stale twin EXPIRES with the reason, and a
        redelivered duplicate is refused atomically (created=False)."""
        run_id = await _seed_run(db, "run-race")
        mailbox = PostgresMailbox(db)

        def command(command_id: str, sequence: int, revision: int | None) -> ControlCommand:
            return ControlCommand.model_validate(
                {
                    "schema": "forge.proposal.control-command/1",
                    "command_id": command_id,
                    "work_id": run_id,
                    "sequence": sequence,
                    "kind": "steer",
                    "actor_ref": f"human:{command_id}",
                    "actor_origin": "server_authenticated_human",
                    "idempotency_key": f"key-{command_id}",
                    "payload": {"run_id": run_id, "text": "approach"},
                    "status": "received",
                    "expected_plan_revision": revision,
                    "expected_execution_epoch": 0,
                }
            )

        first, created = await mailbox.submit(command("cmd-a", 1, 2))
        assert created is True
        # the redelivery: a fresh submission of the SAME logical command
        # (new sequence slot, SAME dedup key) loses the insert race and
        # READS the winner — created=False, nothing changed
        twin, created_again = await mailbox.submit(command("cmd-a", 2, 2))
        assert created_again is False and twin.command_id == first.command_id

        second, _ = await mailbox.submit(command("cmd-b", 3, 3))
        scopes = {
            "server_authenticated_human": ("human:cmd-a", "human:cmd-b"),
            "operator_token": (),
            "automation_reconciler": (),
        }
        await mailbox.authorize(first.command_id, scopes)
        await mailbox.authorize(second.command_id, scopes)

        # the world moved to plan revision 3 (operator B's plan landed):
        # B's command matches, A's was written against revision 2
        taken = await mailbox.dispatch(
            second.command_id, current_plan_revision=3, current_execution_epoch=0
        )
        assert taken.status == "dispatching"
        expired = await mailbox.dispatch(
            first.command_id, current_plan_revision=3, current_execution_epoch=0
        )
        assert expired.status == "expired"

        async with db() as session:
            row = await session.scalar(
                select(ControlCommandRow).where(ControlCommandRow.id == first.command_id)
            )
        assert row is not None and row.status == "expired"
        assert any("stale expectations" in str(entry) for entry in (row.journal or []))

    async def test_a_sequence_race_keeps_one_logical_command(self, tmp_path) -> None:
        """Two operators submit CONCURRENTLY on separate connections: the
        per-work sequence uniqueness keeps exactly ONE logical command;
        the loser is refused loudly (pre-read or insert race — both are
        the honest refusal), never silently merged."""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(factory, "run-seq")
        mailbox = PostgresMailbox(factory)

        def command(command_id: str, sequence: int) -> ControlCommand:
            return ControlCommand.model_validate(
                {
                    "schema": "forge.proposal.control-command/1",
                    "command_id": command_id,
                    "work_id": run_id,
                    "sequence": sequence,
                    "kind": "pause",
                    "actor_ref": f"human:{command_id}",
                    "actor_origin": "server_authenticated_human",
                    "idempotency_key": f"key-{command_id}",
                    "payload": {"run_id": run_id},
                    "status": "received",
                }
            )

        # both racers read max(sequence)=0 and submit sequence 1
        results = await asyncio.gather(
            mailbox.submit(command("cmd-r1", 1)),
            mailbox.submit(command("cmd-r2", 1)),
            return_exceptions=True,
        )
        landed = [entry for entry in results if not isinstance(entry, BaseException)]
        refused = [entry for entry in results if isinstance(entry, BaseException)]
        assert len(landed) == 1 and len(refused) == 1  # one winner, one loud refusal
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ControlCommandRow).where(ControlCommandRow.work_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1  # exactly one logical command exists
        await engine.dispose()


class TestRetentionUnderPausedCheckpointAndInvestigationHold:
    async def test_the_pinned_paused_checkpoint_survives_the_retention_pass(self, tmp_path) -> None:
        """A PAUSED run's active checkpoint is PINNED (the persisted
        continuation decision): the retention pass removes the superseded
        row but the pinned checkpoint AND its blobs survive."""
        repository = FilesystemCheckpointRepository(tmp_path / "cas")
        work_id = "pausedwork0000000000000000000000f"

        def payload(sequence: int) -> tuple[bytes, dict[str, bytes], str]:
            data = f"v{sequence}\n".encode()
            manifest = json.dumps(
                {
                    "schema": "forge.wip.manifest/2",
                    "work_id": work_id,
                    "sequence": sequence,
                    "source_oids": {"attempt_base": "e" * 40},
                    "files": {
                        "src/app.py": {
                            "digest": hashlib.sha256(data).hexdigest(),
                            "mode": 0o644,
                            "role": "new",
                        }
                    },
                    "deletions": [],
                }
            ).encode()
            return (
                manifest,
                {hashlib.sha256(data).hexdigest(): data},
                hashlib.sha256(manifest).hexdigest(),
            )

        pinned_id = ""
        for sequence in (1, 2, 3):
            manifest, blobs, checkpoint_id = payload(sequence)
            await repository.put(work_id, checkpoint_id, manifest, blobs)
            if sequence == 2:  # the PAUSED run's active checkpoint
                pinned_id = checkpoint_id
                assert await repository.pin(work_id, checkpoint_id, "pause fence: safely paused")

        removed = await repository.apply_retention(work_id, keep_last=1)
        assert removed == 1  # the superseded row went; the pinned one stayed
        pins = await repository.pins(work_id)
        assert any(row.get("checkpoint_id") == pinned_id for row in pins)
        served = await repository.read(work_id)
        assert served is not None  # the active checkpoint's bytes still resolve
        active_manifest, _blobs = served
        assert json.loads(active_manifest)["sequence"] == 3  # the ACTIVE survives too

    async def test_held_receipts_are_refused_never_pruned(self, db) -> None:
        """The #322 retention holds: a non-terminal (paused) run holds
        its receipts; an investigation hold holds them REGARDLESS of
        status; only an unheld terminal work's expired receipts prune."""
        paused = await _seed_run(db, "workpaused")  # non-terminal: the paused run
        investigated = await _seed_run(
            db,
            "workprobe",
            status="failed",
            evidence={"credential_investigation_open": {"by": "op", "at": "now"}},
        )
        free = await _seed_run(db, "workfree", status="cancelled")
        expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
        for work_id in (paused, investigated, free):
            await record_redemption(
                db,
                RedemptionReceipt(
                    receipt_id=f"rcpt-{work_id}",
                    work_id=work_id,
                    route="gitlab-protected-variable",
                    credential_ref="lane-token",
                    expires_at=expiry,
                ),
            )

        assert (await retention_holds(db, paused)).reasons == ("active_attempt",)
        assert "investigation" in (await retention_holds(db, investigated)).reasons
        assert (await retention_holds(db, free)).reasons == ()

        outcome = await prune_expired_redemptions(db)
        assert outcome["deleted"] == 1  # only the unheld terminal work
        assert set(outcome["held_works"]) == {paused, investigated}
