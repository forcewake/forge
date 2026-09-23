"""The production-entry invariant suite (issue #246 / Q35-09).

Six mandatory traces (AT-01..AT-06 of the c7ae8db review, lifted to
the PROCESS/DB level) that drive the SAME entry points a customer
invokes — real subprocesses where it matters:

- **PE-1 (AT-01)** — a REAL git checkout, a REAL lane subprocess
  (``python -m forge.lane_driver --driver codex``) whose vendor is the
  controlled fake_vendor executable on the real app-server wire, the
  REAL restore promotion (``promote="generation"``), and the SHIPPED
  collector subprocess. The uploaded diff must contain the
  generation's edits; the original checkout must be unchanged. The
  paired negative arm re-runs the OLD inline emit sequence and proves
  it MISSES the edits (the mutant this trace kills).
- **PE-2 (AT-02)** — a bootstrap death before vendor acceptance, then
  the authenticated ``/retry`` through a REAL service whose dispatch
  travels REAL HTTP to the fake native server: the next attempt is
  recorded on the SERVER as ``lane_resume_mode=fresh`` — the committed
  baseline, no checkpoint prerequisite.
- **PE-3 (AT-03)** — a required continuation whose referenced blob is
  rotted: the lane subprocess halts with ZERO vendor events (the
  fake_vendor executable is never spawned) and ZERO publication.
- **PE-4 (AT-04)** — PG-gated: postgres-mode checkpoint upload over
  real HTTP, the control instance TORN DOWN, a NEW instance with a
  fresh session factory, then ``/resume`` produces the exact
  ResumeSpec from PostgreSQL — with the inverse arms proving a
  filesystem index (stale or absent) never wins.
- **PE-5 (AT-05)** — a native job that outlives local cancellation
  (cancel configured to FAIL on the server): the slot stays HELD at
  the cap until the server marks the job terminal; then exactly one
  more start. The negative arm releases on the local verdict and
  proves the OVERSUBSCRIPTION that follows.
- **PE-6 (AT-06)** — a lost start response (server ACCEPTS, response
  fails) with the worker dying before the handle update: occupancy
  stays uncertain across a RESTARTED worker, capacity is not falsely
  freed, and the reconciler probe resolves the correlation through the
  server's surviving state.

Assertions are on ARTIFACTS — file bytes, diff digests, the server's
dispatch ledger, DB row identities — never on status strings alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from forge.adaptive.admission import ExecutionLease, LeaseOccupancy, lease_occupancy
from forge.adaptive.checkpoint_channel import work_scoped_token
from forge.durable import FlowRun

from .conftest import (
    FAKE_VENDOR,
    PE_LANE_SECRET,
    PE_LEGACY_DEADLINE,
    PE_REPO,
    PE_WORKFLOW,
    make_service,
    start_control_plane,
)

pytestmark = pytest.mark.production_entry

PROJECT_ID = 90210
WORK_ID = "pe-run-at01"


# ----------------------------------------------------------------------
# Shared helpers: real git, real subprocesses
# ----------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result


def make_checkout(parent: Path, name: str) -> tuple[Path, str]:
    """A REAL git checkout at its frozen base (the lane's starting shape)."""
    checkout = parent / name
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "lane@example.com")
    _git(checkout, "config", "user.name", "forge lane")
    (checkout / "README.md").write_text("base readme\n")
    (checkout / "src").mkdir()
    (checkout / "src" / "app.py").write_text("print('base')\n")
    (checkout / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "frozen base")
    base_oid = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    exclude = checkout / ".git" / "info" / "exclude"
    exclude.write_text(
        exclude.read_text() + "\n.forge/\n.codegraph/\n__pycache__/\n*.pyc\n.venv/\nforge-output/\n"
    )
    return checkout, base_oid


def tracked_baseline(checkout: Path) -> dict[str, str]:
    """path -> sha256(raw bytes) of the tracked files at HEAD — capture's
    canonical baseline identity (R28-04)."""
    names = _git(checkout, "ls-files").stdout.split()
    baseline: dict[str, str] = {}
    for name in names:
        blob = subprocess.run(
            ["git", "-C", str(checkout), "show", f"HEAD:{name}"],
            capture_output=True,
            timeout=60,
            check=True,
        ).stdout
        baseline[name] = hashlib.sha256(blob).hexdigest()
    return baseline


def run_vendor_once(cwd: Path, actions: list[dict], eventlog: Path) -> None:
    """The controlled vendor's pre-checkpoint leg: real edits, immediately."""
    env = {
        **os.environ,
        "FAKE_VENDOR_ACTIONS": json.dumps(actions),
        "FAKE_VENDOR_EVENTLOG": str(eventlog),
    }
    outcome = subprocess.run(
        [sys.executable, str(FAKE_VENDOR), "--once"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert outcome.returncode == 0, outcome.stderr


def run_lane(
    checkout: Path,
    *,
    work_id: str,
    resume: str,
    control_url: str,
    token: str,
    attempt_base: str,
    actions: list[dict] | None,
    eventlog: Path,
    mode: str = "complete",
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """The REAL packaged lane as a subprocess (the CI job's own entry)."""
    brief = checkout / ".forge" / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("# Implement the widget\n\nMake the widget real.\n")
    env = {
        **os.environ,
        "FORGE_LANE_DRIVER": "codex",
        "CODEX_BINARY": str(FAKE_VENDOR),
        "CODEX_CWD": str(checkout),
        "FORGE_BRIEF": str(brief),
        "FORGE_ATTEMPT_BASE": attempt_base,
        "FORGE_ISSUE_IID": "42",
        "FORGE_RUN_ID": work_id,
        "FORGE_WORK_ID": work_id,
        "FORGE_STEERING_ENABLED": "1",
        "FORGE_LANE_RESUME": resume,
        "FORGE_LANE_CONTROL_URL": control_url,
        "FORGE_LANE_CONTROL_TOKEN": token,
        "FAKE_VENDOR_MODE": mode,
        "FAKE_VENDOR_EVENTLOG": str(eventlog),
    }
    if actions is not None:
        env["FAKE_VENDOR_ACTIONS"] = json.dumps(actions)
    return subprocess.run(
        [sys.executable, "-m", "forge.lane_driver", "--driver", "codex"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_collector(
    checkout: Path, *, work_id: str, attempt_base: str
) -> subprocess.CompletedProcess[str]:
    """The SHIPPED collector — the exact template step invocation."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "forge.harness_entry",
            "--collect-candidate",
            "--forge-run-id",
            work_id,
            "--attempt-base-oid",
            attempt_base,
            "--output-root",
            "forge-output",
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=180,
    )


async def upload_wip_checkpoint(
    control_url: str,
    checkout: Path,
    work_id: str,
    token: str,
    attempt_base: str,
) -> str:
    """Capture the checkout's WIP and upload it over REAL HTTP (the pause's
    capture+upload, on the lane-side channel objects)."""
    from forge.adaptive.artifact_store import ContentAddressedStore
    from forge.adaptive.checkpoint_channel import CheckpointChannel, LaneControlAPI
    from forge.adaptive.checkpointing import capture_wip

    store = ContentAddressedStore(root=checkout / ".forge" / "checkpoints", tenant=work_id)
    channel = CheckpointChannel(LaneControlAPI(base_url=control_url, work_token=token))
    receipt = capture_wip(
        work_id=work_id,
        root=checkout,
        store=store,
        tracked_baseline=tracked_baseline(checkout),
        source_oids={"attempt_base": attempt_base},
        sequence=1,
        upload=channel,
    )
    assert receipt.verified
    return str(receipt.artifact_id)


async def create_control_schema(engine) -> None:
    """Every control-plane table on the target engine (imports register them)."""
    from forge import api_checkpoint_channel  # noqa: F401 — checkpoint_metadata
    from forge.adaptive import mailbox_db  # noqa: F401 — control_commands
    from forge.models.base import Base

    assert api_checkpoint_channel and mailbox_db and Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def record_resume_command(session_factory, work_id: str) -> bool:
    """The operator's /resume on a REAL durable control service — the exact
    resume command row the lane's resume-spec lookup reads. The checkpoint
    authority is resolved through the ONE composition point (env-honoring),
    exactly like the app's control service."""
    from forge.adaptive.checkpoint_repository import resolve_repository
    from forge.adaptive.mailbox_db import PostgresMailbox
    from forge.adaptive.wiring import OperatorControlService

    service = OperatorControlService(
        mailbox=PostgresMailbox(session_factory),
        checkpoint_repository=resolve_repository(session_factory=session_factory),
    )
    return await service.resume(work_id, "human:op", f"pe-resume:{work_id}")


async def open_leases(session_factory) -> list[ExecutionLease]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(ExecutionLease.released_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def get_run(session_factory, run_id: str) -> FlowRun:
    async with session_factory() as session:
        return await session.get(FlowRun, run_id)


async def start_run(service, issue: int, *, title: str = "t", description: str = "d") -> str:
    return await service.start_run(
        project_id=PROJECT_ID,
        issue_number=issue,
        issue_title=title,
        issue_description=description,
        author_username="alice",
    )


async def go(service, run_id: str, issue: int) -> None:
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=issue,
        note_text=f"@forge /go {run_id}",
        author_username="alice",
    )


# ----------------------------------------------------------------------
# PE-1 (AT-01) — restored work becomes the shipped candidate
# ----------------------------------------------------------------------


class TestPE1RestoredWorkBecomesTheShippedCandidate:
    @pytest.fixture()
    async def resumed_lane(self, tmp_path: Path, monkeypatch):
        """The full AT-01 shape: WIP captured+uploaded over HTTP, the resume
        command durable, and the REAL lane subprocess having run its resumed
        turn in the restored generation."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        store_dir = tmp_path / "control-store"
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
        engine = create_async_engine(db_url)
        await create_control_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        control = await start_control_plane(db_url)
        try:
            original, base_oid = make_checkout(tmp_path, "workspace")
            eventlog = tmp_path / "vendor-events.jsonl"
            # The vendor's first-turn WIP: changed, new AND deleted files.
            run_vendor_once(
                original,
                [
                    {"op": "write", "path": "src/app.py", "content": "print('restored wip')\n"},
                    {"op": "write", "path": "notes/new-file.md", "content": "agent edit\n"},
                    {"op": "delete", "path": "run.sh"},
                ],
                eventlog,
            )
            token = work_scoped_token(PE_LANE_SECRET, WORK_ID)
            checkpoint_id = await upload_wip_checkpoint(
                control.base_url, original, WORK_ID, token, base_oid
            )
            assert await record_resume_command(factory, WORK_ID) is True

            # The RESUMED runner: a fresh checkout of the same base, the REAL
            # lane subprocess with a required resume, and the controlled
            # vendor doing the resumed turn's own edit.
            resumed, resumed_base = make_checkout(tmp_path, "resumed-workspace")
            lane = run_lane(
                resumed,
                work_id=WORK_ID,
                resume="1",
                control_url=control.base_url,
                token=token,
                attempt_base=resumed_base,
                actions=[
                    {"op": "write", "path": "src/app.py", "content": "print('resumed turn')\n"},
                    {"op": "write", "path": "notes/resumed.md", "content": "resumed-turn edit\n"},
                ],
                eventlog=eventlog,
            )
            assert lane.returncode == 0, lane.stderr
            return {
                "original": original,
                "resumed": resumed,
                "resumed_base": resumed_base,
                "eventlog": eventlog,
                "checkpoint_id": checkpoint_id,
                "token": token,
            }
        finally:
            control.stop()
            await engine.dispose()

    def test_shipped_collector_captures_the_generation_edits(self, resumed_lane):
        resumed = resumed_lane["resumed"]
        resumed_base = resumed_lane["resumed_base"]

        outcome = run_collector(resumed, work_id=WORK_ID, attempt_base=resumed_base)

        assert outcome.returncode == 0, outcome.stderr
        reported = json.loads(outcome.stdout)
        diff_path = Path(reported["diff_path"])
        diff = diff_path.read_bytes()
        assert reported["source"] == "generation"
        assert reported["zero_change"] is False
        assert reported["resolved_work_id"] == WORK_ID
        assert reported["checkpoint_id"] == resumed_lane["checkpoint_id"]
        assert reported["diff_digest"] == hashlib.sha256(diff).hexdigest()
        # The candidate carries the RESTORED WIP and the RESUMED turn's
        # edits — changed, new and deleted files all ride the diff.
        assert b"notes/new-file.md" in diff
        assert b"agent edit" in diff
        assert b"notes/resumed.md" in diff
        assert b"print('resumed turn')" in diff
        assert b"deleted file mode" in diff and b"run.sh" in diff

    def test_both_checkouts_survive_the_turn(self, resumed_lane):
        original = resumed_lane["original"]
        resumed = resumed_lane["resumed"]
        # The FIRST runner's checkout still holds exactly its vendor WIP —
        # the bytes the pause captured and the resume restored. Nothing the
        # resumed attempt did (restore, turn, collection) reached back into
        # it: no generation pointer, no generation edits.
        assert (original / "src" / "app.py").read_text() == "print('restored wip')\n"
        assert not (original / "run.sh").exists()  # the first turn's deletion stands
        assert not (original / ".forge" / "workspace-generation").exists()
        # The RESUMED runner's checkout stays at its base: the lane worked
        # in the generation, and the collector resolved it through the
        # pointer — never by mutating the checkout.
        assert (resumed / "src" / "app.py").read_text() == "print('base')\n"
        assert (resumed / "run.sh").exists()
        assert _git(resumed, "status", "--porcelain").stdout.strip() == ""
        pointer = resumed / ".forge" / "workspace-generation"
        document = json.loads(pointer.read_text())
        assert document["work_id"] == WORK_ID
        assert document["checkpoint_id"] == resumed_lane["checkpoint_id"]
        meta = json.loads((resumed / ".forge" / "candidate.meta.json").read_text())
        assert meta["exit"] == "completed"
        assert meta["workspace_generation"] == str(Path(document["generation_path"]).resolve())

    def test_negative_arm_the_old_inline_emit_misses_the_generation(self, resumed_lane):
        """The MUTATION DOCUMENTATION (the pre-Q35-01 defect, run verbatim):
        the shipped inline emit sequence — executed in the checkout the CI
        shell sits in — produces a 0-byte candidate while the real work sits
        in the generation. If this arm ever FAILS, the fixture stopped
        reproducing the defect the collector exists to fix."""
        resumed = resumed_lane["resumed"]
        resumed_base = resumed_lane["resumed_base"]
        old_diff = resumed.parent / "old-arm.diff"
        script = (
            "rm -rf .codegraph .venv __pycache__ .pytest_cache; "
            "find . -name '*.pyc' -delete 2>/dev/null; "
            "git add -A; "
            f"git diff --cached --binary --full-index {resumed_base} > {old_diff} || true"
        )
        outcome = subprocess.run(
            ["/bin/bash", "-c", script], cwd=resumed, capture_output=True, text=True, timeout=120
        )
        assert outcome.returncode == 0, outcome.stderr
        assert old_diff.stat().st_size == 0, (
            "the OLD emit commands must MISS the generation's edits — the "
            "negative arm no longer reproduces the Q35-01 defect"
        )
        # and the checkout's index is left clean for later arms
        _git(resumed, "reset", "-q")


# ----------------------------------------------------------------------
# PE-2 (AT-02) — retry before any useful work exists
# ----------------------------------------------------------------------


class TestPE2RetryBeforeAnyUsefulWork:
    async def test_retry_after_bootstrap_death_dispatches_fresh_over_real_http(
        self, pe_db, native, native_client, monkeypatch
    ):
        """A bootstrap death before the vendor ever started has NO checkpoint
        and NO candidate: the authenticated /retry must still work, and the
        NEXT attempt's dispatch — recorded by the NATIVE SERVER — demands no
        checkpoint (``lane_resume_mode=fresh`` on the committed baseline)."""
        client, reader = native_client
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", "")  # no checkpoint authority
        factory = pe_db.worker_factory()
        service = make_service(factory, client, reader)
        native.seed_issue(42, "Add a widget", "Make widgets real.")

        run_id = await start_run(service, 42)
        await go(service, run_id, 42)
        assert len(native.dispatches()) == 1  # the first dispatch really landed

        # The lane job died at SDK BOOTSTRAP — before the vendor session
        # existed (the worker's terminal journal, as the reconciler writes
        # it from the lane's bootstrap classification).
        async with factory() as session:
            run = await session.get(FlowRun, run_id)
            run.status = "failed"
            run.status_reason = "harness_infrastructure: harness_bootstrap_failed (node setup)"
            await session.commit()

        await service.handle_retry(
            project_id=PROJECT_ID,
            issue_number=42,
            note_text=f"/retry {run_id}",
            author_username="alice",
            delivery_id="pe2-retry-1",
        )

        # ARTIFACTS on the native server: exactly one NEW dispatch, whose
        # recorded inputs name the committed baseline — never ``required``.
        dispatches = native.dispatches()
        assert len(dispatches) == 2
        branch = f"forge/42/{run_id[:8]}"
        resumed_inputs = [entry["inputs"] for entry in dispatches if entry["ref"] == branch]
        assert resumed_inputs, "the retry must re-dispatch on the run's factory branch"
        assert resumed_inputs[-1]["lane_resume_mode"] == "fresh"
        assert resumed_inputs[-1]["run_id"] == run_id
        # No checkpoint prerequisite rode the dispatch (no checkpoint ref keys).
        assert not [key for key in resumed_inputs[-1] if "checkpoint" in key]

        # The durable decision is evidence-bound and says so.
        run = await get_run(factory, run_id)
        doc = (run.evidence or {}).get("continuation") or {}
        assert doc["mode_selected"] == "fresh"
        assert doc["no_checkpoint_baseline"] is True
        assert doc["vendor_started"] is False
        # The ack note named the continuation source (a real issue comment
        # the server accepted).
        assert any("committed baseline" in body for body in native.comments())
        # The REAL client never fell off the modeled API surface.
        assert native.unknown_paths() == []


# ----------------------------------------------------------------------
# PE-3 (AT-03) — required resume never becomes a silent restart
# ----------------------------------------------------------------------


class TestPE3RequiredResumeNeverBecomesASilentRestart:
    async def test_rotted_blob_halts_the_lane_with_zero_vendor_calls(
        self, tmp_path: Path, monkeypatch
    ):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        store_dir = tmp_path / "control-store"
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
        engine = create_async_engine(db_url)
        await create_control_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        control = await start_control_plane(db_url)
        try:
            checkout, base_oid = make_checkout(tmp_path, "workspace")
            eventlog = tmp_path / "vendor-events.jsonl"
            run_vendor_once(
                checkout,
                [{"op": "write", "path": "src/app.py", "content": "print('paused wip')\n"}],
                eventlog,
            )
            token = work_scoped_token(PE_LANE_SECRET, WORK_ID)
            await upload_wip_checkpoint(control.base_url, checkout, WORK_ID, token, base_oid)
            assert await record_resume_command(factory, WORK_ID) is True

            # ROT one referenced blob at the authority: the bytes no longer
            # hash to the address the ResumeSpec pinned.
            served = httpx.get(
                control.url(f"/lane/checkpoints/{WORK_ID}"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            ).json()
            first_blob = next(iter(served["blobs"]))
            blob_path = store_dir / first_blob[:2] / first_blob
            assert blob_path.is_file()
            blob_path.write_bytes(b"rotted bytes - not the addressed content\n")

            resumed, resumed_base = make_checkout(tmp_path, "resumed-workspace")
            lane = run_lane(
                resumed,
                work_id=WORK_ID,
                resume="1",
                control_url=control.base_url,
                token=token,
                attempt_base=resumed_base,
                actions=[{"op": "write", "path": "evil.txt", "content": "must never happen\n"}],
                eventlog=eventlog,
            )

            # The lane FAILED loudly — a required continuation never turns
            # into a silent fresh restart.
            assert lane.returncode != 0
            assert "wip_restore_failed" in lane.stderr
            meta = json.loads((resumed / ".forge" / "candidate.meta.json").read_text())
            assert meta["exit"] == "failed"
            assert meta["terminal_reason"] == "wip_restore_failed"
            sidecar = json.loads((resumed / ".forge" / "steering.json").read_text())
            assert sidecar["wip_restore"]["restored"] is False
            assert sidecar["wip_restore"]["checkpoint_selection"] == "exact"

            # ZERO vendor calls: the controlled vendor executable was never
            # spawned (its event log holds only the pre-pause leg's events).
            kinds = [
                json.loads(line)["kind"]
                for line in eventlog.read_text().splitlines()
                if line.strip()
            ]
            assert kinds, "the pre-pause vendor leg must be on record"
            post_pause = kinds[kinds.index("vendor_edits") + 1 :]
            assert post_pause == [], f"no vendor event may follow the pause: {post_pause}"
            assert not (resumed / "evil.txt").exists()

            # ZERO publication and no generation: nothing was restored.
            assert not (resumed / "forge-output").exists()
            assert not list(resumed.parent.glob(".forge-workspace-gen-*"))
            assert _git(resumed, "status", "--porcelain").stdout.strip() == ""
        finally:
            control.stop()
            await engine.dispose()


# ----------------------------------------------------------------------
# PE-4 (AT-04) — Postgres upload, restart before resume  [PG-gated]
# ----------------------------------------------------------------------

PG_REQUIRED = pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the AT-04 cross-instance Postgres "
        "authority proof runs only against a disposable real Postgres"
    ),
)


@PG_REQUIRED
class TestPE4PostgresUploadRestartResume:
    async def _reset(self, factory) -> None:
        from forge import api_checkpoint_channel as api_channel
        from forge.adaptive.mailbox_db import ControlCommandRow

        async with factory() as session:
            await session.execute(delete(api_channel.CheckpointMetadataRow))
            await session.execute(delete(ControlCommandRow))
            await session.commit()

    async def test_new_instance_resumes_the_exact_spec_from_postgres(
        self, tmp_path: Path, monkeypatch
    ):
        """Upload over real HTTP in postgres mode, TEAR the control instance
        down, bring a NEW one up on a FRESH session factory, and /resume
        still produces the exact ResumeSpec — read from PostgreSQL, with no
        filesystem index mirror anywhere."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from forge import api_checkpoint_channel as api_channel
        from forge.adaptive.checkpoint_repository import (
            FilesystemCheckpointRepository,
            resolve_repository,
        )
        from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
        from forge.adaptive.wiring import OperatorControlService
        from forge.models.base import Base

        url = os.environ["FORGE_PG_TEST_URL"]
        store_dir = tmp_path / "control-store"
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
        monkeypatch.setenv("FORGE_CHECKPOINT_DURABILITY", "postgres")
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        monkeypatch.setenv("DATABASE_URL", url)

        engine_a = create_async_engine(url)
        async with engine_a.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
        await self._reset(factory_a)

        control_a = await start_control_plane(url)
        try:
            checkout, base_oid = make_checkout(tmp_path, "workspace")
            run_vendor_once(
                checkout,
                [{"op": "write", "path": "src/app.py", "content": "print('pg wip')\n"}],
                tmp_path / "vendor-events.jsonl",
            )
            token = work_scoped_token(PE_LANE_SECRET, WORK_ID)
            checkpoint_id = await upload_wip_checkpoint(
                control_a.base_url, checkout, WORK_ID, token, base_oid
            )
            # The upload really lives in PostgreSQL (row identity), and NO
            # filesystem JSON index mirror exists.
            async with factory_a() as session:
                row = (
                    await session.execute(
                        select(api_channel.CheckpointMetadataRow).where(
                            api_channel.CheckpointMetadataRow.work_id == WORK_ID
                        )
                    )
                ).scalar_one()
                assert row.checkpoint_id == checkpoint_id
            works_dir = store_dir / "works"
            assert not (works_dir.is_dir() and list(works_dir.glob("*.json")))

            service_a = OperatorControlService(
                mailbox=PostgresMailbox(factory_a),
                checkpoint_repository=resolve_repository(session_factory=factory_a),
            )
            assert await service_a.resume(WORK_ID, "human:op", "pe4-resume-1") is True
        finally:
            control_a.stop()
            await engine_a.dispose()

        # The RESTART: a completely fresh instance over the same PostgreSQL.
        engine_b = create_async_engine(url)
        factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
        control_b = await start_control_plane(url)
        try:
            service_b = OperatorControlService(
                mailbox=PostgresMailbox(factory_b),
                checkpoint_repository=resolve_repository(session_factory=factory_b),
            )
            assert await service_b.resume(WORK_ID, "human:op", "pe4-resume-2") is True

            async with factory_b() as session:
                rows = (
                    (
                        await session.execute(
                            select(ControlCommandRow).where(
                                ControlCommandRow.work_id == WORK_ID,
                                ControlCommandRow.kind == "resume",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert len(rows) == 2  # both resumes landed, each with its spec
            for row in rows:
                assert row.payload["checkpoint_ref"] == f"{WORK_ID}@{checkpoint_id}"
                assert row.payload["checkpoint_sequence"] == 1
                assert row.payload["source_oid"] == base_oid

            # The PUBLIC resume-spec surface (real HTTP, the NEW instance)
            # serves the exact same immutable spec.
            served = httpx.get(
                control_b.url("/lane/controls/resume-spec"),
                params={"work_id": WORK_ID},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            assert served.status_code == 200
            payload = served.json()["command"]["payload"]
            assert payload["checkpoint_ref"] == f"{WORK_ID}@{checkpoint_id}"
            assert payload["checkpoint_sequence"] == 1
            assert payload["source_oid"] == base_oid
        finally:
            control_b.stop()
            await engine_b.dispose()

        # NEGATIVE ARM 1 — the OLD behavior (resume reading the filesystem
        # JSON index): with postgres authority the index answers NOTHING,
        # so the old resume producer would have refused the work.
        fs_only = FilesystemCheckpointRepository(store_dir)
        assert await fs_only.entry(WORK_ID) is None

        # NEGATIVE ARM 2 — a STALE filesystem index naming a different
        # checkpoint must never win over the PostgreSQL row.
        stale = store_dir / "works" / f"{WORK_ID}.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(
            json.dumps(
                {
                    "work_id": WORK_ID,
                    "checkpoints": [{"checkpoint_id": "a" * 64, "sequence": 99, "files": 0}],
                }
            )
        )
        engine_c = create_async_engine(url)
        factory_c = async_sessionmaker(engine_c, expire_on_commit=False)
        try:
            service_c = OperatorControlService(
                mailbox=PostgresMailbox(factory_c),
                checkpoint_repository=resolve_repository(session_factory=factory_c),
            )
            assert await service_c.resume(WORK_ID, "human:op", "pe4-resume-3") is True
            async with factory_c() as session:
                latest = (
                    (
                        await session.execute(
                            select(ControlCommandRow)
                            .where(
                                ControlCommandRow.work_id == WORK_ID,
                                ControlCommandRow.kind == "resume",
                            )
                            .order_by(ControlCommandRow.sequence.desc())
                        )
                    )
                    .scalars()
                    .first()
                )
            assert latest.payload["checkpoint_ref"] == f"{WORK_ID}@{checkpoint_id}"
        finally:
            await engine_c.dispose()
            await self._reset(factory_c)


# ----------------------------------------------------------------------
# PE-5 (AT-05) — the native job outlives local cancellation
# ----------------------------------------------------------------------


class TestPE5NativeJobOutlivesLocalCancellation:
    async def _drive(self, pe_db, native, native_client, monkeypatch, *, cap: int = 1):
        client, reader = native_client
        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", str(cap))
        factory = pe_db.worker_factory()
        service = make_service(factory, client, reader)
        return factory, service

    async def test_slot_held_until_native_terminal_then_exactly_one_more_start(
        self, pe_db, native, native_client, monkeypatch
    ):
        factory, service = await self._drive(pe_db, native, native_client, monkeypatch)
        native.seed_issue(42, "Add a widget", "d")
        run_a = await start_run(service, 42)
        await go(service, run_a, 42)
        (dispatch,) = native.dispatches()
        actions_run_id = dispatch["run_id"]
        assert native.state()["runs"][0]["status"] == "in_progress"

        # The provider cancel is configured to FAIL; the local /cancel then
        # lands while the native job keeps running on the server.
        native.configure(cancel_mode="fail")
        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=42,
            note_text=f"@forge /cancel {run_a}",
            author_username="alice",
        )
        run = await get_run(factory, run_a)
        assert run.status == "cancelled"
        (lease,) = await open_leases(factory)
        assert lease_occupancy(lease) is LeaseOccupancy.DRAINING  # the slot is HELD
        assert native.cancels  # the cancel really was attempted over HTTP

        # Another start at the cap: parked, and NO extra native start.
        native.seed_issue(43, "Second widget", "d")
        run_b = await start_run(service, 43)
        await go(service, run_b, 43)
        assert (await get_run(factory, run_b)).status == "blocked"
        assert len(native.dispatches()) == 1  # no start sneaks through

        # The server marks the job terminal; the reconciler's probe (real
        # HTTP) observes it and releases the slot exactly once.
        native.mark_terminal(actions_run_id)
        assert await service._reconcile_draining_leases() == 1
        assert await open_leases(factory) == []
        assert await service._reconcile_draining_leases() == 0  # idempotent

        # Now exactly ONE more start goes through.
        native.seed_issue(44, "Third widget", "d")
        run_c = await start_run(service, 44)
        await go(service, run_c, 44)
        dispatches = native.dispatches()
        assert len(dispatches) == 2
        assert dispatches[-1]["ref"] == f"forge/44/{run_c[:8]}"
        assert (await get_run(factory, run_c)).status == "waiting_harness"
        assert native.unknown_paths() == []

    async def test_negative_arm_release_on_local_status_oversubscribes(
        self, pe_db, native, native_client, monkeypatch
    ):
        """The MUTATION DOCUMENTATION: releasing the slot on the LOCAL
        cancelled verdict (the pre-Q35-04 behavior — spelled here through
        the audited force-override, which exists precisely to emulate it)
        lets the next start through while the NATIVE job still runs on the
        server — two in_progress native jobs at a capacity of one."""
        from forge.adaptive.admission import release_run_leases

        factory, service = await self._drive(pe_db, native, native_client, monkeypatch)
        native.seed_issue(42, "Add a widget", "d")
        run_a = await start_run(service, 42)
        await go(service, run_a, 42)
        native.configure(cancel_mode="fail")
        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=42,
            note_text=f"@forge /cancel {run_a}",
            author_username="alice",
        )
        # The OLD behavior: free on the local verdict regardless of occupancy.
        await release_run_leases(factory, run_a, force=True)
        assert await open_leases(factory) == []

        native.seed_issue(43, "Second widget", "d")
        run_b = await start_run(service, 43)
        await go(service, run_b, 43)

        running = [job for job in native.runs() if job["status"] == "in_progress"]
        assert len(running) == 2, (
            "the mutant oversubscribes: two native jobs running at cap=1 — "
            "the shipped evidence-based release is what prevents exactly this"
        )
        assert len(native.dispatches()) == 2


# ----------------------------------------------------------------------
# PE-6 (AT-06) — lost start response preserves uncertain occupancy
# ----------------------------------------------------------------------


class TestPE6LostStartResponsePreservesUncertainOccupancy:
    async def test_uncertain_occupancy_survives_the_restart_until_the_probe_resolves(
        self, pe_db, native, native_client, monkeypatch
    ):
        client, reader = native_client
        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "1")
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", "")

        # Worker #1 dispatches; the server ACCEPTS the job but the response
        # is made to fail — the worker dies before the handle update.
        native.configure(dispatch_response="server_error")
        factory_a = pe_db.worker_factory()
        service_a = make_service(factory_a, client, reader)
        native.seed_issue(42, "Add a widget", "d")
        run_a = await start_run(service_a, 42)
        await go(service_a, run_a, 42)

        (dispatch,) = native.dispatches()
        assert dispatch["ref"] == f"forge/42/{run_a[:8]}"
        run = await get_run(factory_a, run_a)
        # The leg died on the lost response — the revival classifier may
        # park it ``blocked`` (a transient 500 schedules auto-revive) or
        # ``failed``; either way the run is TERMINAL and its reason names
        # the failed start.
        assert run.status in {"failed", "blocked"}
        assert "harness_start_failed" in (run.status_reason or "")
        (lease,) = await open_leases(factory_a)
        assert lease.native_intent_ref == (
            f"github:workflow:{PE_REPO}/{PE_WORKFLOW}@forge/42/{run_a[:8]}"
        )
        assert lease.native_handle is None  # the correlation never landed
        # Uncertain occupancy: the failed leg's terminal transition parked
        # the intent-carrying lease DRAINING — held, never freed on the
        # local verdict (dispatched_unknown at the moment of death; the
        # evidence columns — intent set, handle NULL — are what carry it).
        assert lease_occupancy(lease) is LeaseOccupancy.DRAINING
        # ...while the native job the server accepted keeps RUNNING.
        assert native.runs()[0]["status"] == "in_progress"

        # The worker DIES; a RESTARTED worker (fresh session factory over
        # the same durable state) requests a new slot.
        await pe_db.dispose()  # worker #1's engine is gone — its process died
        factory_b = pe_db.worker_factory()
        service_b = make_service(factory_b, client, reader)
        native.seed_issue(43, "Second widget", "d")
        run_b = await start_run(service_b, 43)
        await go(service_b, run_b, 43)

        # Capacity is NOT falsely freed: the dead attempt's intent parks
        # draining, the new start is parked, and the server saw NO new job.
        assert (await get_run(factory_b, run_b)).status == "blocked"
        leases = await open_leases(factory_b)
        assert len(leases) == 1
        assert lease_occupancy(leases[0]) is LeaseOccupancy.DRAINING
        assert len(native.dispatches()) == 1

        # Correlation resolution through the reconciler probe: the job is
        # still running on the server -> uncertain occupancy HOLDS...
        assert await service_b._reconcile_draining_leases() == 0
        (still,) = await open_leases(factory_b)
        assert still.draining_at is not None
        # ...the server marks it terminal -> the probe releases the slot.
        native.mark_terminal(dispatch["run_id"])
        assert await service_b._reconcile_draining_leases() == 1
        assert await open_leases(factory_b) == []

        # Then exactly one new start goes through (the transport is healed).
        native.configure(dispatch_response="ok")
        native.seed_issue(44, "Third widget", "d")
        run_c = await start_run(service_b, 44)
        await go(service_b, run_c, 44)
        assert len(native.dispatches()) == 2
        assert (await get_run(factory_b, run_c)).status == "waiting_harness"
        assert native.unknown_paths() == []
