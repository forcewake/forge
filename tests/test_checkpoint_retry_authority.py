"""R36-03 (review ``16339c2``, issue #262): every retry and revival
checkpoint lookup runs through the CONFIGURED ASYNC authority.

The defect this suite pins out of existence: ``/retry``'s checkpoint
consultation read the RAW filesystem index, then fell back to a
SYNCHRONOUS ``httpx.get`` with the LEGACY work-only token, and collapsed
every failure into ``False`` — a PostgreSQL-only checkpoint read as "no
checkpoint" in ``/retry`` while upload and ``/resume`` saw it fine,
especially after the legacy credential window closed (#243).

The contract here:

- the TYPED outcome (:class:`CheckpointLookupOutcome`) keeps
  exact / absent / unavailable / corrupt / unauthorized DISJOINT — a
  503, a refused credential or rotted bytes are never "no checkpoint";
- the selection matrix (:func:`revival.durable_checkpoint_outcome`):
  a session factory selects the SAME configured repository upload and
  resume share; without one, the control URL + lane credential select
  the authenticated checkpoint-channel proxy; neither answers the TYPED
  ``unavailable`` — never a filesystem index, never "absent", never a
  synchronous HTTP call inside the event loop;
- the EXACT_WIP continuation decision records and PINS its exact
  checkpoint (``continuation.checkpoint_digest``; one pin per authorized
  decision, keyed by the decision's command id) — a checkpoint uploaded
  after the decision changes nothing;
- the retired legacy chain survives ONLY behind
  ``FORGE_RETRY_LEGACY_LOOKUP=1`` (default OFF), clearly labeled.

The PostgreSQL proofs (AT-04's core: upload over the real HTTP channel
→ torn-down instance → a FRESH instance's native ``/retry`` selects the
exact persisted checkpoint, no JSON mirror) are gated on
``FORGE_PG_TEST_URL`` and a disposable database created through the
``forge-postgres`` podman container; the SQLite approximations and the
HTTP-proxy proofs run always.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge import api_checkpoint_channel as api_channel
from forge.adaptive import checkpoint_repository as cr
from forge.adaptive.checkpoint_repository import (
    AUTHORITY_FILESYSTEM,
    AUTHORITY_HTTP,
    AUTHORITY_POSTGRES,
    CheckpointLookupOutcome,
    CheckpointRepositoryUnavailable,
    FilesystemCheckpointRepository,
    HttpCheckpointRepository,
    LOOKUP_ABSENT,
    LOOKUP_CORRUPT,
    LOOKUP_EXACT,
    LOOKUP_UNAVAILABLE,
    LOOKUP_UNAUTHORIZED,
    PostgresCheckpointRepository,
)
from forge.models.base import Base
from forge.runs import revival

WORK_ID = "wp-r36-03"
SECRET = "test-lane-secret"  # noqa: S105 — fake value for tests


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checkpoint(
    work_id: str, files: dict[str, bytes], sequence: int
) -> tuple[bytes, dict[str, bytes], str]:
    """A minimal ``forge.wip.manifest/2`` payload the store verifies."""
    manifest = json.dumps(
        {
            "schema": "forge.wip.manifest/2",
            "work_id": work_id,
            "sequence": sequence,
            "source_oids": {"attempt_base": "e" * 40},
            "files": {
                name: {"digest": _digest(data), "mode": 0o644, "role": "new"}
                for name, data in sorted(files.items())
            },
            "deletions": [],
        }
    ).encode()
    blobs = {entry["digest"]: files[name] for name, entry in json.loads(manifest)["files"].items()}
    return manifest, blobs, _digest(manifest)


async def _sqlite_factory() -> tuple[Any, async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# 1. The typed outcome — the five states stay disjoint on BOTH authorities
# ---------------------------------------------------------------------------


class TestStoreBackedLookupOutcome:
    async def test_exact_carries_the_identity_and_the_authority(self, tmp_path):
        repository = FilesystemCheckpointRepository(tmp_path / "store")
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 1)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        outcome = await repository.lookup_outcome(WORK_ID)

        assert outcome.state == LOOKUP_EXACT
        assert outcome.checkpoint_id == checkpoint_id
        assert outcome.digest == checkpoint_id  # the content address
        assert outcome.authority == AUTHORITY_FILESYSTEM
        assert outcome.latency_s is not None

    async def test_absent_is_a_proven_absence(self, tmp_path):
        repository = FilesystemCheckpointRepository(tmp_path / "store")

        outcome = await repository.lookup_outcome(WORK_ID)

        assert outcome.state == LOOKUP_ABSENT
        assert outcome.checkpoint_id is None
        assert outcome.authority == AUTHORITY_FILESYSTEM

    async def test_an_outage_is_typed_unavailable_never_absent(self, tmp_path):
        class _Outage(FilesystemCheckpointRepository):
            async def entry(self, work_id: str, checkpoint_id: str | None = None) -> dict | None:
                raise CheckpointRepositoryUnavailable("the database is down")

        outcome = await _Outage(tmp_path / "store").lookup_outcome(WORK_ID)

        assert outcome.state == LOOKUP_UNAVAILABLE
        assert "the database is down" in outcome.detail

    async def test_an_unreachable_blob_volume_is_unavailable_not_corrupt(self, tmp_path):
        class _NoBlobs(FilesystemCheckpointRepository):
            async def read_entry(self, entry: dict[str, Any]) -> tuple[bytes, dict[str, bytes]]:
                raise OSError("the blob volume is not mounted")

        repository = _NoBlobs(tmp_path / "store")
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 1)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        outcome = await repository.lookup_outcome(WORK_ID)

        assert outcome.state == LOOKUP_UNAVAILABLE
        assert "blob volume" in outcome.detail or "cannot read" in outcome.detail

    async def test_rotted_bytes_are_corrupt_never_exact_or_absent(self, tmp_path):
        root = tmp_path / "store"
        repository = FilesystemCheckpointRepository(root)
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 1)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)
        # Tamper one stored blob: the index still names it, the bytes rot.
        blob_digest = next(iter(blobs))
        blob_path = root / blob_digest[:2] / blob_digest
        blob_path.write_bytes(b"tampered bytes\n")

        outcome = await repository.lookup_outcome(WORK_ID)

        assert outcome.state == LOOKUP_CORRUPT
        assert outcome.checkpoint_id is None  # rot never carries identity

    async def test_the_postgres_authority_answers_through_the_same_contract(self, tmp_path):
        _engine, factory = await _sqlite_factory()
        repository = PostgresCheckpointRepository(tmp_path / "store", factory)
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"pg\n"}, 2)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        exact = await repository.lookup_outcome(WORK_ID)
        absent = await repository.lookup_outcome(f"{WORK_ID}-other")

        assert exact.state == LOOKUP_EXACT
        assert exact.authority == AUTHORITY_POSTGRES
        assert exact.checkpoint_id == checkpoint_id
        assert absent.state == LOOKUP_ABSENT


# ---------------------------------------------------------------------------
# 2. The HTTP proxy — the lane's own route, statuses mapped to typed states
# ---------------------------------------------------------------------------


class _FakeChannel:
    """A controllable checkpoint channel behind ``httpx.MockTransport``."""

    def __init__(self, status: int = 200, checkpoint_id: str | None = None) -> None:
        self.status = status
        self.checkpoint_id = checkpoint_id or _digest(b"http-checkpoint")
        self.seen_auth: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen_auth.append(request.headers.get("Authorization", ""))
        if self.status == -1:  # the transport-failure arm
            raise httpx.ConnectError("connection refused")
        if self.status == 200:
            body = {
                "work_id": WORK_ID,
                "checkpoint_id": self.checkpoint_id,
                "sequence": 3,
                "manifest": base64.b64encode(b"{}").decode("ascii"),
                "blobs": {},
            }
            return httpx.Response(200, json=body)
        return httpx.Response(self.status, json={"detail": f"status {self.status}"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport)


class TestHttpRepositoryTypedStates:
    async def _lookup(self, channel: _FakeChannel) -> CheckpointLookupOutcome:
        repository = HttpCheckpointRepository(
            base_url="http://control.test",
            token="pre-provisioned-lane-token",
            client=channel.client(),
        )
        return await repository.lookup_outcome(WORK_ID)

    async def test_200_is_exact_with_the_served_identity(self):
        channel = _FakeChannel(status=200)

        outcome = await self._lookup(channel)

        assert outcome.state == LOOKUP_EXACT
        assert outcome.checkpoint_id == channel.checkpoint_id
        assert outcome.authority == AUTHORITY_HTTP

    async def test_404_is_a_proven_absence(self):
        outcome = await self._lookup(_FakeChannel(status=404))

        assert outcome.state == LOOKUP_ABSENT

    async def test_401_is_unauthorized_its_own_answer(self):
        outcome = await self._lookup(_FakeChannel(status=401))

        assert outcome.state == LOOKUP_UNAUTHORIZED
        assert "refused the lane credential" in outcome.detail

    async def test_503_is_unavailable(self):
        outcome = await self._lookup(_FakeChannel(status=503))

        assert outcome.state == LOOKUP_UNAVAILABLE

    async def test_500_is_corrupt(self):
        outcome = await self._lookup(_FakeChannel(status=500))

        assert outcome.state == LOOKUP_CORRUPT

    async def test_a_transport_failure_is_unavailable(self):
        outcome = await self._lookup(_FakeChannel(status=-1))

        assert outcome.state == LOOKUP_UNAVAILABLE
        assert "unreachable" in outcome.detail

    async def test_a_200_without_a_valid_identity_is_corrupt(self):
        channel = _FakeChannel(status=200, checkpoint_id="not-a-digest")

        outcome = await self._lookup(channel)

        assert outcome.state == LOOKUP_CORRUPT

    async def test_the_secret_arm_mints_the_generation_scoped_token_never_legacy(self):
        """The credential is the dispatch's own derivation (NEXT-01):
        ``lane_control_token(secret, work, generation)`` — and WITHOUT a
        generation authority the proxy REFUSES to mint instead of
        falling back to the generation-less legacy token."""
        from forge.api_lane_control import lane_control_token

        channel = _FakeChannel(status=200)
        generation = 4
        repository = HttpCheckpointRepository(
            base_url="http://control.test",
            secret=SECRET,
            generation_lookup=lambda work_id: asyncio.sleep(0, result=generation),
            client=channel.client(),
        )

        outcome = await repository.lookup_outcome(WORK_ID)

        assert outcome.state == LOOKUP_EXACT
        assert channel.seen_auth == [
            f"Bearer {lane_control_token(SECRET, WORK_ID, generation=generation)}"
        ]

        # Without a generation authority: typed unavailable, no request,
        # and the refusal names the legacy token explicitly.
        refused = HttpCheckpointRepository(base_url="http://control.test", secret=SECRET)
        outcome = await refused.lookup_outcome(WORK_ID)
        assert outcome.state == LOOKUP_UNAVAILABLE
        assert "legacy" in outcome.detail


# ---------------------------------------------------------------------------
# 3. The selection matrix — one honest authority per process
# ---------------------------------------------------------------------------


def _env(**extra: str) -> dict[str, str]:
    """An isolated env mapping — never os.environ (except where the
    PG-gated lab pins the real env deliberately)."""
    env: dict[str, str] = {}
    env.update(extra)
    return env


class TestSelectionMatrix:
    async def test_a_session_factory_selects_the_configured_repository(self, tmp_path):
        _engine, factory = await _sqlite_factory()

        authority = cr.resolve_checkpoint_lookup_authority(
            env=_env(**{api_channel.DURABILITY_ENV: "postgres"}),
            session_factory=factory,
            root=tmp_path / "store",
        )

        assert isinstance(authority, PostgresCheckpointRepository)
        assert await authority.authority() == AUTHORITY_POSTGRES

    async def test_a_factory_with_the_default_durability_selects_the_filesystem(self, tmp_path):
        """The default durability's configured authority is the
        filesystem repository — the SAME one upload/resume use (never a
        bare index read beside it)."""
        _engine, factory = await _sqlite_factory()

        authority = cr.resolve_checkpoint_lookup_authority(
            env=_env(**{api_channel.CHECKPOINT_STORE_DIR_ENV: str(tmp_path / "store")}),
            session_factory=factory,
        )

        assert isinstance(authority, FilesystemCheckpointRepository)

    async def test_no_factory_with_url_and_token_selects_the_http_proxy(self):
        authority = cr.resolve_checkpoint_lookup_authority(
            env=_env(FORGE_LANE_CONTROL_URL="http://control.test", FORGE_LANE_CONTROL_TOKEN="tok")
        )

        assert isinstance(authority, HttpCheckpointRepository)

    async def test_url_and_secret_alone_still_answer_the_http_proxy(self):
        """The proxy is selected, but without a generation authority it
        answers the typed ``unavailable`` — it never mints legacy."""
        authority = cr.resolve_checkpoint_lookup_authority(
            env=_env(FORGE_LANE_CONTROL_URL="http://control.test", FORGE_LANE_CONTROL_SECRET=SECRET)
        )

        assert isinstance(authority, HttpCheckpointRepository)
        outcome = await authority.lookup_outcome(WORK_ID)
        assert outcome.state == LOOKUP_UNAVAILABLE

    async def test_neither_answers_the_typed_unavailable_never_filesystem(self):
        outcome = await revival.durable_checkpoint_outcome(WORK_ID, env=_env())

        assert outcome.state == LOOKUP_UNAVAILABLE
        assert outcome.authority != AUTHORITY_FILESYSTEM
        assert "no checkpoint authority is configured" in outcome.detail

    async def test_a_misconfigured_authority_is_typed_unavailable_not_fs(self, tmp_path):
        """A junk durability value with a factory wired: the composition
        point refuses at construction and the refusal is TYPED — never a
        filesystem fallback, never "absent"."""
        _engine, factory = await _sqlite_factory()

        outcome = await revival.durable_checkpoint_outcome(
            WORK_ID,
            session_factory=factory,
            env=_env(
                **{
                    api_channel.DURABILITY_ENV: "junk-mode",
                    api_channel.CHECKPOINT_STORE_DIR_ENV: str(tmp_path / "store"),
                }
            ),
        )

        assert outcome.state == LOOKUP_UNAVAILABLE
        assert "misconfigured" in outcome.detail

    async def test_an_injected_repository_wins(self, tmp_path):
        repository = FilesystemCheckpointRepository(tmp_path / "store")
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 1)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        outcome = await revival.durable_checkpoint_outcome(
            WORK_ID, repository=repository, env=_env()
        )

        assert outcome.state == LOOKUP_EXACT
        assert outcome.checkpoint_id == checkpoint_id

    async def test_the_configured_postgres_authority_wins_over_a_stale_mirror(self, tmp_path):
        """AT-04 arm 2: a stale ``works/<id>.json`` index is never read
        on the modern path — the configured PostgreSQL authority wins."""
        _engine, factory = await _sqlite_factory()
        root = tmp_path / "shared-blobs"
        repository = PostgresCheckpointRepository(root, factory)
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"pg\n"}, 5)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)
        stale = root / "works" / f"{WORK_ID}.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale_bytes = json.dumps(
            {"work_id": WORK_ID, "checkpoints": [{"checkpoint_id": "b" * 64, "sequence": 99}]}
        ).encode()
        stale.write_bytes(stale_bytes)

        outcome = await revival.durable_checkpoint_outcome(
            WORK_ID,
            session_factory=factory,
            env=_env(
                **{
                    api_channel.DURABILITY_ENV: "postgres",
                    api_channel.CHECKPOINT_STORE_DIR_ENV: str(root),
                }
            ),
        )

        assert outcome.state == LOOKUP_EXACT
        assert outcome.checkpoint_id == checkpoint_id  # the DB row, not the mirror
        assert stale.read_bytes() == stale_bytes  # and the mirror was untouched


# ---------------------------------------------------------------------------
# 4. The legacy opt-in — OFF by default, isolated and labeled when ON
# ---------------------------------------------------------------------------


class TestLegacyOptIn:
    async def test_off_by_default_the_modern_path_is_used(self, monkeypatch):
        """The same URL+secret env the legacy chain fed on, WITHOUT the
        opt-in flag: the modern HTTP proxy answers (unavailable — no
        attempt-scoped credential), never the legacy adapter."""
        env = _env(
            FORGE_LANE_CONTROL_URL="http://control.test",
            FORGE_LANE_CONTROL_SECRET=SECRET,
        )
        assert "FORGE_RETRY_LEGACY_LOOKUP" not in env

        outcome = await revival.durable_checkpoint_outcome(WORK_ID, env=env)

        assert outcome.authority == AUTHORITY_HTTP
        assert outcome.state == LOOKUP_UNAVAILABLE

    async def test_on_it_is_used_and_labeled(self, monkeypatch):
        seen: dict[str, Any] = {}

        class _Response:
            status_code = 200
            content = b"{}"

            def json(self) -> dict[str, Any]:
                return {"checkpoint_id": "c" * 64}

        def _fake_get(url: str, **kwargs: Any) -> _Response:
            seen["url"] = url
            seen["auth"] = kwargs.get("headers", {}).get("Authorization", "")
            return _Response()

        monkeypatch.setattr(httpx, "get", _fake_get)
        env = _env(
            FORGE_RETRY_LEGACY_LOOKUP="1",
            FORGE_LANE_CONTROL_URL="http://control.test",
            FORGE_LANE_CONTROL_SECRET=SECRET,
        )

        outcome = await revival.durable_checkpoint_outcome(WORK_ID, env=env)

        assert outcome.authority == revival.LEGACY_LOOKUP_AUTHORITY
        assert outcome.state == LOOKUP_EXACT
        assert outcome.checkpoint_id == "c" * 64
        # ... and it dialed the legacy work-only token, verbatim.
        from forge.api_lane_control import lane_control_token

        assert seen["auth"] == f"Bearer {lane_control_token(SECRET, WORK_ID)}"


# ---------------------------------------------------------------------------
# 5. The legacy credential window closes — the modern path is unaffected
# ---------------------------------------------------------------------------


class TestLegacyWindowClosed:
    async def test_a_closed_window_does_not_break_the_modern_retry_lookup(self, tmp_path):
        """Acceptance 4: with ``FORGE_LEGACY_CREDENTIAL_DEADLINE`` in the
        past (the window #243 made expire), the modern DB arm still reads
        checkpoint metadata — the legacy token is not on its path."""
        from datetime import UTC, datetime, timedelta

        _engine, factory = await _sqlite_factory()
        repository = PostgresCheckpointRepository(tmp_path / "store", factory)
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"pg\n"}, 1)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        past = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        outcome = await revival.durable_checkpoint_outcome(
            WORK_ID,
            session_factory=factory,
            env=_env(
                **{
                    api_channel.DURABILITY_ENV: "postgres",
                    api_channel.CHECKPOINT_STORE_DIR_ENV: str(tmp_path / "store"),
                    "FORGE_LEGACY_CREDENTIAL_DEADLINE": past,
                }
            ),
        )

        assert outcome.state == LOOKUP_EXACT
        assert outcome.checkpoint_id == checkpoint_id


# ---------------------------------------------------------------------------
# 6. Event-loop responsiveness — a bounded slow lookup never blocks a command
# ---------------------------------------------------------------------------


class _SlowRepository:
    """A lookup authority that takes its time — ASYNC, like the real one."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.calls = 0

    async def authority(self) -> str:
        return "slow-test"

    async def lookup_outcome(self, work_id: str) -> CheckpointLookupOutcome:
        self.calls += 1
        await asyncio.sleep(self.seconds)
        return CheckpointLookupOutcome.exact(_digest(b"slow"), authority="slow-test")


class TestEventLoopResponsiveness:
    async def test_a_slow_lookup_does_not_block_a_concurrent_command(self):
        slow = _SlowRepository(seconds=0.5)
        processed_at: list[float] = []

        async def control_command() -> None:
            await asyncio.sleep(0)  # what an event-loop command needs
            processed_at.append(time.monotonic())

        lookup_task = asyncio.create_task(
            revival.durable_checkpoint_outcome(WORK_ID, repository=slow, env=_env())
        )
        await asyncio.sleep(0.05)  # the slow lookup is now in flight
        assert not lookup_task.done()

        for _ in range(5):  # five commands DURING the lookup
            await control_command()
            await asyncio.sleep(0.02)

        assert len(processed_at) == 5
        assert not lookup_task.done()  # still in flight — never blocked it
        outcome = await lookup_task
        assert outcome.state == LOOKUP_EXACT
        assert slow.calls == 1


# ---------------------------------------------------------------------------
# 7. The service path — typed refusals, pinned digests, command dedup
# ---------------------------------------------------------------------------


class TestServiceRetryAuthority:
    """The ``/retry`` handler end-to-end against the sqlite machinery of
    tests.test_adaptive_continuation, with the REAL repository chain."""

    @pytest.fixture()
    async def db(self):
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
    def fake(self):
        from tests.fixtures.fake_github import FakeGitHub
        from tests.test_adaptive_continuation import (
            BASE_HEAD,
            FIXTURES_REPO,
            ISSUE,
            ISSUE_DESC,
            ISSUE_TITLE,
        )

        github = FakeGitHub()
        github.seed_repo(FIXTURES_REPO, {"src/app.py": "print('hi')\n"})
        github.heads[FIXTURES_REPO]["main"] = BASE_HEAD
        github.seed_issue(FIXTURES_REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
        return github

    @staticmethod
    def _outcomes(monkeypatch, outcome: CheckpointLookupOutcome) -> None:
        async def _lookup(run_id: str, **_: Any) -> CheckpointLookupOutcome:
            return outcome

        monkeypatch.setattr(revival, "durable_checkpoint_outcome", _lookup)

    async def _dead_run(self, db, fake, service) -> str:
        from tests.test_adaptive_continuation import DEATH_PLAIN_TIMEOUT, drive_to_dead

        return await drive_to_dead(db, service, reason=DEATH_PLAIN_TIMEOUT, candidates=[])

    async def test_an_unavailable_authority_refuses_with_a_distinct_note_zero_dispatches(
        self, db, fake, monkeypatch
    ):
        from tests.test_adaptive_continuation import (
            dispatch_calls,
            make_service,
            retry as retry_cmd,
        )

        self._outcomes(
            monkeypatch,
            CheckpointLookupOutcome.missing(
                LOOKUP_UNAVAILABLE, authority="postgres", detail="the database is down"
            ),
        )
        service = make_service(db, fake)
        run_id = await self._dead_run(db, fake, service)
        fake.dispatch_inputs.clear()
        before = dispatch_calls(fake)

        await retry_cmd(service, run_id, delivery_id="retry-outage-1")

        assert fake.dispatch_inputs == []  # ZERO native dispatches
        assert dispatch_calls(fake) == before
        from tests.test_adaptive_continuation import comment_bodies

        (note,) = [body for body in comment_bodies(fake) if "checkpoint authority" in body]
        assert "the database is down" in note
        assert "nothing was dispatched" in note
        assert "no stored checkpoint" not in note

    async def test_an_exact_outcome_records_and_pins_the_digest(
        self, db, fake, tmp_path, monkeypatch
    ):
        """Acceptance 5/6: the EXACT_WIP decision records
        ``continuation.checkpoint_digest`` and pins the exact checkpoint
        (one pin per authorized decision, keyed by the command id)."""
        from forge.adaptive.continuation import CONTINUATION_EVIDENCE_KEY
        from tests.test_adaptive_continuation import (
            get_run,
            make_service,
            retry as retry_cmd,
        )

        root = tmp_path / "authority-store"
        repository = FilesystemCheckpointRepository(root)
        # The service resolves its OWN authority for the pin — point the
        # env's store dir at the same root the test wrote to.
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(root))

        service = make_service(db, fake)
        run_id = await self._dead_run(db, fake, service)
        # The pin validates against the work id — the checkpoint must
        # exist under the RUN's id for the real pin to land.
        manifest, blobs, checkpoint_id = _checkpoint(run_id, {"a.txt": b"pinned\n"}, 1)
        await repository.put(run_id, checkpoint_id, manifest, blobs)
        self._outcomes(monkeypatch, CheckpointLookupOutcome.exact(checkpoint_id, authority="test"))
        fake.dispatch_inputs.clear()
        await retry_cmd(service, run_id, delivery_id="retry-pin-1")

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"
        run = await get_run(db, run_id)
        doc = run.evidence[CONTINUATION_EVIDENCE_KEY]
        assert doc["checkpoint_digest"] == checkpoint_id
        pins = await repository.pins(run_id)
        assert len(pins) == 1
        assert pins[0]["checkpoint_id"] == checkpoint_id
        assert pins[0]["reason"] == "retry-continuation:retry-pin-1"

    async def test_a_post_decision_upload_never_changes_the_dispatched_reference(
        self, db, fake, tmp_path, monkeypatch
    ):
        """Acceptance 6 + the dedup reconciliation: a NEWER checkpoint
        landing after the decision changes neither the persisted digest
        nor the pin set — the reused decision binds what the request
        approved."""
        from forge.adaptive.continuation import CONTINUATION_EVIDENCE_KEY
        from tests.test_adaptive_continuation import (
            DEATH_PLAIN_TIMEOUT,
            get_run,
            make_service,
            retry as retry_cmd,
        )

        root = tmp_path / "authority-store"
        repository = FilesystemCheckpointRepository(root)
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(root))
        service = make_service(db, fake)
        run_id = await self._dead_run(db, fake, service)
        m1, b1, first = _checkpoint(run_id, {"a.txt": b"first\n"}, 1)
        await repository.put(run_id, first, m1, b1)
        self._outcomes(monkeypatch, CheckpointLookupOutcome.exact(first, authority="test"))
        fake.dispatch_inputs.clear()
        await retry_cmd(service, run_id, delivery_id="retry-pin-a")

        # The attempt dies again; a NEWER checkpoint lands; a repeated
        # command (a fresh delivery id) arrives.
        async with db() as session:
            from forge.durable import FlowRun

            run = await session.get(FlowRun, run_id)
            run.status = "failed"
            run.status_reason = DEATH_PLAIN_TIMEOUT
            await session.commit()
        m2, b2, newer = _checkpoint(run_id, {"a.txt": b"second\n"}, 2)
        await repository.put(run_id, newer, m2, b2)
        self._outcomes(monkeypatch, CheckpointLookupOutcome.exact(newer, authority="test"))
        fake.dispatch_inputs.clear()
        await retry_cmd(service, run_id, delivery_id="retry-pin-b")

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"
        run = await get_run(db, run_id)
        doc = run.evidence[CONTINUATION_EVIDENCE_KEY]
        assert doc["checkpoint_digest"] == first  # the ORIGINAL, not the newer
        assert doc["decided_at"] == doc["decided_at"]
        pins = await repository.pins(run_id)
        assert len(pins) == 1  # one decision, one pin — the repeat added none
        assert pins[0]["checkpoint_id"] == first
        assert pins[0]["reason"] == "retry-continuation:retry-pin-a"


# ---------------------------------------------------------------------------
# 8. PG-gated: AT-04's core over real PostgreSQL and the real HTTP channel
# ---------------------------------------------------------------------------

#: The disposable database the PG-gated proofs create and drop.
PG_DISPOSABLE_DB = "forge_retry_test"


def _podman_psql(statement: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "podman",
            "exec",
            "forge-postgres",
            "psql",
            "-U",
            "forge",
            "-d",
            "forge",
            "-c",
            statement,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the AT-04 real-isolation proof runs only "
        "against a disposable real Postgres (created/dropped via the "
        "forge-postgres podman container)"
    ),
)
class TestAT04PostgresRetryAuthority:
    """Upload in PostgreSQL mode over the REAL HTTP channel → the control
    instance's wiring torn down → a FRESH instance (its own engine and
    session factory) performs the native ``/retry`` and selects the exact
    persisted checkpoint — no JSON mirror anywhere."""

    @pytest.fixture()
    async def lab(self, tmp_path: Path, monkeypatch):
        from sqlalchemy.engine import make_url

        created = _podman_psql(f"CREATE DATABASE {PG_DISPOSABLE_DB}")
        if created.returncode != 0 and "already exists" not in created.stderr:
            pytest.skip(f"the forge-postgres podman container is unavailable: {created.stderr}")
        _podman_psql(f"DROP DATABASE IF EXISTS {PG_DISPOSABLE_DB} WITH (FORCE)")
        created = _podman_psql(f"CREATE DATABASE {PG_DISPOSABLE_DB}")
        if created.returncode != 0:
            pytest.skip(f"the disposable database cannot be created: {created.stderr}")

        # render_as_string: plain str() masks the password as "***"
        # (SQLAlchemy 2.x), and the engine would dial the LITERAL stars.
        url = (
            make_url(os.environ["FORGE_PG_TEST_URL"])
            .set(database=PG_DISPOSABLE_DB)
            .render_as_string(hide_password=False)
        )
        root = tmp_path / "shared-blobs"
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(root))
        monkeypatch.setenv(api_channel.DURABILITY_ENV, "postgres")
        monkeypatch.setenv(api_channel.LANE_CONTROL_SECRET_ENV, SECRET)
        try:
            yield str(url), root
        finally:
            _podman_psql(f"DROP DATABASE IF EXISTS {PG_DISPOSABLE_DB} WITH (FORCE)")

    async def test_upload_then_fresh_instance_retry_selects_the_exact_checkpoint(
        self, lab, tmp_path
    ):
        from forge.api_lane_control import lane_control_token
        from tests.fixtures.fake_github import FakeGitHub
        from tests.test_adaptive_continuation import (
            DEATH_PLAIN_TIMEOUT,
            dispatch_calls,
            drive_to_dead,
            get_run,
            make_service,
            retry as retry_cmd,
        )

        url, root = lab
        engine_a = create_async_engine(url)
        async with engine_a.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory_a = async_sessionmaker(engine_a, expire_on_commit=False)

        # The control instance A drives the run to a plain-timeout death
        # WITH a candidate on record (the checkpoint-continuation shape).
        fake = FakeGitHub()
        service_a = make_service(factory_a, fake)
        run_id = await drive_to_dead(
            factory_a, service_a, reason=DEATH_PLAIN_TIMEOUT, candidates=["c1"]
        )
        async with factory_a() as session:
            from sqlalchemy import update

            from forge.durable import FlowRun

            await session.execute(
                update(FlowRun).where(FlowRun.id == run_id).values(cancellation_generation=3)
            )
            await session.commit()

        # The checkpoint UPLOAD goes over the real HTTP channel, with the
        # run's CURRENT generation-scoped token (exactly what the dispatch
        # provisions the lane with).
        manifest, blobs, checkpoint_id = _checkpoint(run_id, {"a.txt": b"at04\n"}, 7)
        application = FastAPI()
        application.include_router(api_channel.checkpoint_channel_router)
        application.state.session_factory = factory_a
        token = lane_control_token(SECRET, run_id, generation=3)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://control.test") as client:
            payload = {
                "manifest": base64.b64encode(manifest).decode("ascii"),
                "blobs": {
                    digest: base64.b64encode(data).decode("ascii") for digest, data in blobs.items()
                },
                "sequence": 7,
            }
            response = await client.put(
                f"/lane/checkpoints/{run_id}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200, response.text

        # The control instance A is torn down.
        await engine_a.dispose()

        # The FRESH instance: its OWN engine, session factory and service
        # over the same PostgreSQL — the native /retry.
        engine_b = create_async_engine(url)
        factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
        fresh_fake = FakeGitHub()
        service_b = make_service(factory_b, fresh_fake)
        fresh_fake.dispatch_inputs.clear()
        before = dispatch_calls(fresh_fake)
        try:
            await retry_cmd(service_b, run_id, delivery_id="retry-at04-1")

            (dispatch,) = fresh_fake.dispatch_inputs
            assert dispatch["inputs"]["lane_resume_mode"] == "required"
            assert dispatch_calls(fresh_fake) == before + 1
            run = await get_run(factory_b, run_id)
            doc = run.evidence["continuation"]
            assert doc["mode_selected"] == "required"
            assert doc["checkpoint_digest"] == checkpoint_id  # the EXACT persisted one
            # No JSON mirror anywhere: the postgres authority kept the
            # index in checkpoint_metadata; the works/ dir holds nothing.
            works = root / "works"
            assert not (works.is_dir() and list(works.glob("*.json")))
            # And the exact bytes still read through the FRESH authority.
            repository = PostgresCheckpointRepository(root, factory_b)
            served = await repository.read(run_id)
            assert served is not None
            assert served[0] == manifest
            pins = await repository.pins(run_id)
            assert len(pins) == 1
            assert pins[0]["checkpoint_id"] == checkpoint_id
            assert pins[0]["reason"] == "retry-continuation:retry-at04-1"
        finally:
            await engine_b.dispose()

    async def test_the_stale_mirror_never_wins_over_the_pg_authority(self, lab, tmp_path):
        url, root = lab
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            repository = PostgresCheckpointRepository(root, factory)
            manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"pg\n"}, 9)
            await repository.put(WORK_ID, checkpoint_id, manifest, blobs)
            stale = root / "works" / f"{WORK_ID}.json"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_bytes(b'{"checkpoints": [{"checkpoint_id": "' + b"f" * 64 + b'"}]}')

            outcome = await revival.durable_checkpoint_outcome(WORK_ID, session_factory=factory)

            assert outcome.state == LOOKUP_EXACT
            assert outcome.checkpoint_id == checkpoint_id
            assert stale.is_file()  # untouched — never consulted, never fixed
        finally:
            await engine.dispose()

    async def test_a_database_outage_is_unavailable_not_absent(self, lab, tmp_path):
        from sqlalchemy.engine import make_url

        url, root = lab
        outage_url = make_url(url).set(database="forge_no_such_db_r3603")
        engine = create_async_engine(str(outage_url))
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)

            outcome = await revival.durable_checkpoint_outcome(WORK_ID, session_factory=factory)

            assert outcome.state == LOOKUP_UNAVAILABLE
            assert outcome.checkpoint_id is None  # never "no checkpoint"
        finally:
            await engine.dispose()
