"""The LIVE cross-runner checkpoint channel (wave C/D): transport + control plane.

The checkpoint transaction's tests proved capture/restore/resume on ONE
runner; these pin the transport that makes the pause survive the
runner: an upload that PUTs the manifest and every blob to the control
plane and returns a durable reference; a download on a SECOND runner's
fresh store that digest-verifies every byte before anything is written
(a flipped blob byte refuses the whole transfer); work-scoped HMAC auth
under the shared ``FORGE_LANE_CONTROL_SECRET`` (503 disabled without
it, 401 for another work's token); honest size-cap refusals on both
sides; atomic temp+rename writes whose crash window never exposes a
partial artifact; retention that never deletes a work's LATEST
checkpoint; and the checkpointing integration — capture with
``upload=`` then restore from the receipt's ``remote_ref`` with
``download=`` on a store that never saw the first runner.

R28-05/R28-06 add the selection discipline: the ACTIVE checkpoint is
the highest SEQUENCE, never the last arrival — a delayed lower-sequence
upload lands as superseded history without demoting the active pointer,
concurrent index writers cannot lose an append (per-work ``flock``),
and the resume consumer downloads the EXACT checkpoint the resume
command names (falling back to the active one only with a recorded
note).

Server-side behavior runs against a FastAPI ``TestClient`` app holding
ONLY this router (main.py registration is the operator's patch); client
tamper/size paths run against ``pytest-httpx`` fakes so the bytes can
be corrupted in flight, which a real server would refuse first.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:  # POSIX flock — the store's deployment target (mirrors the module).
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platform
    fcntl = None  # type: ignore[assignment]

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import forge.api_checkpoint_channel as api_channel
from forge.adaptive.artifact_store import ContentAddressedStore
from forge.adaptive.checkpoint_channel import (
    CheckpointChannel,
    CheckpointChannelError,
    CheckpointRef,
    CheckpointTooLarge,
    LaneControlAPI,
    TamperedCheckpointError,
    download_checkpoint,
    format_checkpoint_ref,
    parse_checkpoint_ref,
    upload_checkpoint,
    work_scoped_token,
)
from forge.adaptive.checkpointing import UploadFailed, capture_wip, restore_wip
from forge.adaptive.models import ControlCommand

SECRET = "test-lane-secret"  # noqa: S105 — fake shared secret for tests
LIST_SCOPE = "checkpoints:list"
TENANT = "work-tenant"
WORK_ID = "wp-chan-1"

_BASE_APP = b'print("v1")\n'
_BASE_OLD = b"old module\n"
_BASE_README = b"# readme\n"
_APP_V2 = b'print("v2")\n'
_APP_V3 = b'print("v3")\n'
_APP_V4 = b'print("v4")\n'
_WIP_NOTES = b"scratch notes\n"
_WIP_SCRIPT = b"#!/bin/sh\necho wip\n"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _baseline() -> dict[str, str]:
    return {
        "src/app.py": _digest(_BASE_APP),
        "src/old.py": _digest(_BASE_OLD),
        "README.md": _digest(_BASE_README),
    }


def _wip_tree(root: Path, app: bytes = _APP_V2, notes: bytes = _WIP_NOTES) -> Path:
    """A working tree one edit-set ahead of the baseline (variants by *app*)."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_bytes(app)
    (root / "src" / "old.py").unlink(missing_ok=True)  # deleted vs baseline
    (root / "README.md").write_bytes(_BASE_README)  # unchanged
    (root / "notes.md").write_bytes(notes)
    (root / "scripts").mkdir(exist_ok=True)
    script = root / "scripts" / "run.sh"
    script.write_bytes(_WIP_SCRIPT)
    script.chmod(0o755)
    return root


def _capture(
    store: ContentAddressedStore, tree: Path, *, sequence: int = 7, work_id: str = WORK_ID
):
    return capture_wip(
        work_id=work_id,
        root=tree,
        store=store,
        tracked_baseline=_baseline(),
        source_oids={"repo-main": "0" * 40},
        sequence=sequence,
    )


def _server_store_dir() -> Path:
    return Path(os.environ[api_channel.CHECKPOINT_STORE_DIR_ENV])


def _cas_file(root: Path, digest: str) -> Path:
    return root / digest[:2] / digest


def _wire_payload(store: ContentAddressedStore, artifact_id: str) -> dict:
    """The exact JSON-base64 document upload puts on the wire."""
    manifest = json.loads(store.get_verified(artifact_id, principal=store.tenant))
    return {
        "manifest": base64.b64encode(
            store.get_verified(artifact_id, principal=store.tenant)
        ).decode("ascii"),
        "blobs": {
            str(entry["digest"]): base64.b64encode(
                store.get_verified(str(entry["digest"]), principal=store.tenant)
            ).decode("ascii")
            for entry in manifest["files"].values()
        },
        "sequence": manifest["sequence"],
    }


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient app holding ONLY the checkpoint channel router, enabled."""
    monkeypatch.setenv(api_channel.LANE_CONTROL_SECRET_ENV, SECRET)
    monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "server-store"))
    monkeypatch.delenv(api_channel.CHECKPOINT_RETENTION_ENV, raising=False)
    monkeypatch.delenv(api_channel.MAX_BLOB_BYTES_ENV, raising=False)
    application = FastAPI()
    application.include_router(api_channel.checkpoint_channel_router)
    return TestClient(application)


def _channel(client: TestClient, *, max_blob_bytes: int | None = None) -> CheckpointChannel:
    api = LaneControlAPI(base_url="http://testserver", token=SECRET, client=client)
    if max_blob_bytes is not None:
        return CheckpointChannel(api, max_blob_bytes=max_blob_bytes)
    return CheckpointChannel(api)


def _bearer(scope: str, secret: str = SECRET) -> str:
    return f"Bearer {work_scoped_token(secret, scope)}"


class TestUploadDownloadRoundTrip:
    def test_capture_upload_download_restore_on_a_second_runner(self, tmp_path: Path, server):
        """The wave C/D flagship: capture on runner A, PUT to the control
        plane, DESTROY the tree, then a FRESH local store on runner B
        downloads and restores file-by-file."""
        tree = _wip_tree(tmp_path / "runner-a")
        store_a = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store_a, tree, sequence=7)
        channel = _channel(server)

        ref = channel.upload_checkpoint(store_a, WORK_ID)

        assert isinstance(ref, CheckpointRef)
        assert ref.checkpoint_id == receipt.artifact_id  # the durable ref IS the content address
        assert ref.sequence == 7
        assert ref.files == receipt.files == 3
        assert ref.remote_ref == format_checkpoint_ref(WORK_ID, receipt.artifact_id)
        # The operator list sees exactly this checkpoint.
        listed = channel.list_checkpoints()
        assert [item.checkpoint_id for item in listed] == [receipt.artifact_id]
        assert listed[0].work_id == WORK_ID

        shutil.rmtree(tree)  # the first runner is gone
        store_b = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        handle = channel.download_checkpoint(WORK_ID, store_b)

        assert handle.artifact_id == receipt.artifact_id
        assert handle.verified is True
        assert handle.sequence == 7

        target = tmp_path / "runner-b"
        target.mkdir()
        (target / "README.md").write_bytes(_BASE_README)  # the snapshot base
        report = restore_wip(
            artifact_id=handle.artifact_id, store=store_b, target=target, principal=TENANT
        )
        assert report.ok is True
        assert (target / "src" / "app.py").read_bytes() == _APP_V2
        assert (target / "notes.md").read_bytes() == _WIP_NOTES
        assert (target / "scripts" / "run.sh").read_bytes() == _WIP_SCRIPT
        assert os.stat(target / "scripts" / "run.sh").st_mode & 0o111
        assert not (target / "src" / "old.py").exists()

    def test_module_level_upload_and_download_spellings(self, tmp_path: Path, server):
        tree = _wip_tree(tmp_path / "runner-a")
        store_a = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store_a, tree)
        api = LaneControlAPI(base_url="http://testserver", token=SECRET, client=server)

        ref = upload_checkpoint(store_a, WORK_ID, api)
        store_b = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        handle = download_checkpoint(WORK_ID, api, store_b)

        assert ref.checkpoint_id == handle.artifact_id == receipt.artifact_id
        assert store_b.get_verified(handle.artifact_id, principal=TENANT) is not None

    def test_get_before_any_upload_is_404(self, server):
        response = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert response.status_code == 404

    def test_reupload_of_the_same_checkpoint_is_idempotent(self, tmp_path: Path, server):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        _capture(store, tree)
        channel = _channel(server)

        first = channel.upload_checkpoint(store, WORK_ID)
        second = channel.upload_checkpoint(store, WORK_ID)

        assert first.checkpoint_id == second.checkpoint_id
        assert len(channel.list_checkpoints()) == 1  # no duplicate index entry


class TestTamperDetection:
    def test_a_flipped_blob_byte_on_the_server_refuses_the_download(self, tmp_path, server):
        """Rot the blob UNDER the server's CAS address: the digest-verified
        read answers 500 naming the digest, and the client's download
        refuses — no restore handle for bytes that lie."""
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        _capture(store, tree)
        channel = _channel(server)
        channel.upload_checkpoint(store, WORK_ID)

        notes_digest = _digest(_WIP_NOTES)
        blob_file = _cas_file(_server_store_dir(), notes_digest)
        assert blob_file.is_file()
        blob_file.write_bytes(b"EVIL")  # the byte flips AFTER the upload committed

        response = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert response.status_code == 500
        assert notes_digest in response.json()["detail"]

        fresh = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        with pytest.raises(CheckpointChannelError):
            channel.download_checkpoint(WORK_ID, fresh)

    def test_a_flipped_blob_byte_in_flight_refuses_and_writes_nothing(self, tmp_path, httpx_mock):
        """pytest-httpx stands in for a LYING control plane: the response
        carries a blob whose bytes do not reproduce their digest. The
        download refuses before ANY put — the fresh store stays empty."""
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree)
        payload = _wire_payload(store, receipt.artifact_id)
        evil = base64.b64encode(b"TAMPERED-BYTES").decode("ascii")
        payload["blobs"][_digest(_WIP_NOTES)] = evil
        httpx_mock.add_response(
            url=f"http://lane-control.test/lane/checkpoints/{WORK_ID}", json=payload
        )

        fresh = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        api = LaneControlAPI(base_url="http://lane-control.test", token=SECRET)
        with pytest.raises(TamperedCheckpointError, match="failed digest verification"):
            CheckpointChannel(api).download_checkpoint(WORK_ID, fresh)

        # Nothing was written: no shard directories at all in the fresh store.
        assert [item for item in (tmp_path / "store-b").iterdir() if item.is_dir()] == []

    def test_a_manifest_not_hashing_to_its_advertised_id_is_refused(self, tmp_path, httpx_mock):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree)
        payload = _wire_payload(store, receipt.artifact_id)
        payload["checkpoint_id"] = "f" * 64  # the advertised id lies about the bytes
        httpx_mock.add_response(
            url=f"http://lane-control.test/lane/checkpoints/{WORK_ID}", json=payload
        )

        fresh = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        api = LaneControlAPI(base_url="http://lane-control.test", token=SECRET)
        with pytest.raises(TamperedCheckpointError, match="advertised checkpoint"):
            CheckpointChannel(api).download_checkpoint(WORK_ID, fresh)

    def test_a_checkpoint_belonging_to_another_work_is_refused(self, tmp_path, httpx_mock):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree)
        payload = _wire_payload(store, receipt.artifact_id)
        # The LYING control plane serves wp-chan-1's checkpoint AT wp-other's
        # address — the requested-work check must refuse it.
        httpx_mock.add_response(
            url="http://lane-control.test/lane/checkpoints/wp-other", json=payload
        )

        fresh = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        api = LaneControlAPI(base_url="http://lane-control.test", token=SECRET)
        with pytest.raises(TamperedCheckpointError, match="belonging to work"):
            CheckpointChannel(api).download_checkpoint("wp-other", fresh)

    def test_the_server_refuses_an_upload_whose_blob_lies_about_its_digest(
        self, tmp_path: Path, server
    ):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree)
        payload = _wire_payload(store, receipt.artifact_id)
        payload["blobs"][_digest(_WIP_NOTES)] = base64.b64encode(b"EVIL").decode("ascii")

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 400
        assert "does not reproduce its digest" in response.json()["detail"]
        # And nothing was stored: no checkpoint is held for the work.
        empty = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert empty.status_code == 404


class TestWorkScopedAuth:
    def test_disabled_without_the_secret_answers_503(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv(api_channel.LANE_CONTROL_SECRET_ENV, raising=False)
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "store"))
        application = FastAPI()
        application.include_router(api_channel.checkpoint_channel_router)
        client = TestClient(application)

        for method, path, kwargs in (
            ("put", f"/lane/checkpoints/{WORK_ID}", {"json": {}}),
            ("get", f"/lane/checkpoints/{WORK_ID}", {}),
            ("get", "/lane/checkpoints", {}),
        ):
            response = getattr(client, method)(
                path, headers={"Authorization": _bearer(WORK_ID)}, **kwargs
            )
            assert response.status_code == 503
            assert response.json()["detail"] == "lane checkpoint channel disabled"

    def test_a_wrong_work_token_is_refused_everywhere(self, tmp_path: Path, server):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree)
        payload = _wire_payload(store, receipt.artifact_id)
        wrong = {"Authorization": _bearer("wp-someone-elses")}

        put = server.put(f"/lane/checkpoints/{WORK_ID}", json=payload, headers=wrong)
        get = server.get(f"/lane/checkpoints/{WORK_ID}", headers=wrong)

        assert put.status_code == 401
        assert get.status_code == 401
        # And the wrong-token PUT stored nothing.
        ok_list = server.get("/lane/checkpoints", headers={"Authorization": _bearer(LIST_SCOPE)})
        assert ok_list.status_code == 200
        assert ok_list.json()["checkpoints"] == []

    def test_a_missing_or_foreign_scheme_bearer_is_refused(self, server):
        response = server.get(f"/lane/checkpoints/{WORK_ID}")
        assert response.status_code == 401
        response = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": "Basic d3AtLWNsZWFy"}
        )
        assert response.status_code == 401

    def test_the_operator_list_requires_the_list_scope_token(self, tmp_path: Path, server):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        _capture(store, tree)
        _channel(server).upload_checkpoint(store, WORK_ID)

        with_work_token = server.get(
            "/lane/checkpoints", headers={"Authorization": _bearer(WORK_ID)}
        )
        with_list_token = server.get(
            "/lane/checkpoints", headers={"Authorization": _bearer(LIST_SCOPE)}
        )

        assert with_work_token.status_code == 401  # a work token is not an operator token
        assert with_list_token.status_code == 200
        entries = with_list_token.json()["checkpoints"]
        assert len(entries) == 1
        assert entries[0]["work_id"] == WORK_ID
        assert entries[0]["latest"] is True

    def test_an_unsafe_work_id_is_refused_before_anything_runs(self, server):
        response = server.put(
            "/lane/checkpoints/bad%20work", json={}, headers={"Authorization": _bearer("bad work")}
        )
        assert response.status_code == 400

    def test_an_upload_of_another_works_manifest_is_refused(self, tmp_path: Path, server):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree, work_id="wp-owner")
        payload = _wire_payload(store, receipt.artifact_id)

        response = server.put(
            "/lane/checkpoints/wp-impostor",
            json=payload,
            headers={"Authorization": _bearer("wp-impostor")},
        )

        assert response.status_code == 400
        assert "belongs to work" in response.json()["detail"]


class TestAttemptScopedAuthConvergence:
    """NEXT-01: the checkpoint channel authenticates through the SAME
    attempt-credential ladder as the lane-control API. The fixture here
    mounts the router WITH a durable generation authority (a session
    factory + a FlowRun at a known generation) — the production shape
    (create_app); the standalone ``server`` fixture above covers the
    no-authority deployment shape."""

    GENERATION = 3

    @pytest.fixture()
    def authority_server(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        """The router mounted WITH the generation authority, seeded in the
        TestClient's own lifespan loop (aiosqlite owns one loop)."""
        from contextlib import asynccontextmanager

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from forge.durable.models import FlowRun
        from forge.models.base import Base

        monkeypatch.setenv(api_channel.LANE_CONTROL_SECRET_ENV, SECRET)
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "store"))

        @asynccontextmanager
        async def _lifespan(app: FastAPI):
            engine = create_async_engine(
                "sqlite+aiosqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(
                    FlowRun(
                        id=WORK_ID,
                        project_id=1,
                        provider="github",
                        cancellation_generation=self.GENERATION,
                    )
                )
                await session.commit()
            app.state.session_factory = factory
            try:
                yield
            finally:
                await engine.dispose()

        application = FastAPI(lifespan=_lifespan)
        application.include_router(api_channel.checkpoint_channel_router)
        with TestClient(application) as client:
            yield client

    def _gen_bearer(self, generation: int) -> str:
        return f"Bearer {work_scoped_token(SECRET, WORK_ID, generation=generation)}"

    def test_the_current_generations_token_reaches_the_work_surface(
        self, authority_server, tmp_path
    ):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree)

        put = authority_server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, receipt.artifact_id),
            headers={"Authorization": self._gen_bearer(self.GENERATION)},
        )
        get = authority_server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": self._gen_bearer(3)}
        )

        assert put.status_code == 200, put.text
        assert get.status_code == 200

    def test_the_legacy_token_still_validates_inside_the_default_window(self, authority_server):
        response = authority_server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )

        assert response.status_code == 404  # authenticated — nothing held yet

    def test_the_legacy_token_past_the_deadline_is_refused(self, authority_server, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", "2020-01-01T00:00:00+00:00")

        response = authority_server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )

        assert response.status_code == 401
        assert "migration deadline" in response.json()["detail"]

    def test_a_superseded_generations_token_is_refused_with_both_generations(
        self, authority_server
    ):
        response = authority_server.get(
            f"/lane/checkpoints/{WORK_ID}",
            headers={"Authorization": self._gen_bearer(self.GENERATION - 1)},
        )

        assert response.status_code == 401
        detail = response.json()["detail"]
        assert "superseded runner generation" in detail
        assert f"({self.GENERATION - 1};" in detail and f"generation {self.GENERATION}" in detail

    def test_an_authority_outage_is_a_refusal_never_legacy_acceptance(
        self, authority_server, monkeypatch
    ):
        import forge.api_lane_control as api_lane_control

        async def _broken(session_factory, work_id):
            raise api_lane_control.LaneAuthorityUnavailable("storage down")

        monkeypatch.setattr(api_lane_control, "durable_run_generation", _broken)
        response = authority_server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )

        assert response.status_code == 503
        assert "authority is unavailable" in response.json()["detail"]

    def test_the_standalone_mount_keeps_the_pre_generation_posture(self, server, tmp_path):
        """No session factory on the app: no authority to consult — the
        legacy work token inside the window stays the documented default
        (the standalone deployment shape never 503s for want of a DB)."""
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree)

        put = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, receipt.artifact_id),
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert put.status_code == 200


class TestSizeCapRefusal:
    def test_the_client_refuses_over_cap_before_any_network_byte(self, tmp_path, httpx_mock):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        _capture(store, tree)
        api = LaneControlAPI(base_url="http://lane-control.test", token=SECRET)
        channel = CheckpointChannel(api, max_blob_bytes=8)  # every blob here is larger

        with pytest.raises(CheckpointTooLarge, match="refused rather than truncated"):
            channel.upload_checkpoint(store, WORK_ID)

        assert httpx_mock.get_requests() == []  # the refusal happened BEFORE the POST

    def test_the_server_refuses_over_cap_with_a_413(self, tmp_path, server, monkeypatch):
        monkeypatch.setenv(api_channel.MAX_BLOB_BYTES_ENV, "16")
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        _capture(store, tree)

        with pytest.raises(CheckpointChannelError, match="413"):
            _channel(server).upload_checkpoint(store, WORK_ID)

        listed = server.get("/lane/checkpoints", headers={"Authorization": _bearer(LIST_SCOPE)})
        assert listed.json()["checkpoints"] == []  # nothing was stored


class TestUploadClosureRefusal:
    """R28-01: every untrusted blob entry is rejected BEFORE any write.

    The reviewed defect: ``put_checkpoint`` persisted every key in the
    ``blobs`` dict while only the manifest-referenced digests were
    validated, and ``_cas_path`` turned the key into a filesystem path —
    an extra traversal-shaped key wrote a NEW FILE outside the CAS with
    the API process's permissions. These tests pin the closed contract:
    exact closure, hex64 keys only, aggregate caps, and the storage
    primitive's own refusal (the mutation guard: restoring an unchecked
    ``_cas_path`` must fail these)."""

    @staticmethod
    def _sandbox_files(root: Path) -> set[Path]:
        return {
            item
            for item in root.parent.rglob("*")
            if ".forge-restore" not in item.name and item.is_file()
        }

    def _valid_payload(self, tmp_path: Path) -> dict:
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = _capture(store, tree)
        return _wire_payload(store, receipt.artifact_id)

    def test_an_extra_path_shaped_blob_key_is_refused_before_any_write(
        self, tmp_path: Path, server
    ):
        """The reviewer's exact repro: a traversal-shaped EXTRA key must
        not become a filesystem path — the PUT is a 400 and the sandbox's
        file set is byte-identical to before the request."""
        payload = self._valid_payload(tmp_path)
        before = self._sandbox_files(tmp_path)
        payload["blobs"]["../../pwned"] = base64.b64encode(b"outside the CAS\n").decode()

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 400
        assert "not a content address" in response.json()["detail"]
        assert "../../pwned" in response.json()["detail"]
        assert self._sandbox_files(tmp_path) == before  # zero writes, anywhere
        listed = server.get("/lane/checkpoints", headers={"Authorization": _bearer(LIST_SCOPE)})
        assert listed.json()["checkpoints"] == []

    def test_an_absolute_path_key_is_refused_the_same_way(self, tmp_path: Path, server):
        payload = self._valid_payload(tmp_path)
        before = self._sandbox_files(tmp_path)
        payload["blobs"]["/tmp/absolute-pwned"] = base64.b64encode(b"x\n").decode()

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 400
        assert self._sandbox_files(tmp_path) == before

    def test_an_extra_wellformed_digest_is_refused_not_stored(self, tmp_path: Path, server):
        """A syntactically valid extra digest with unrelated bytes cannot
        poison a shared content address either — the closure is exact."""
        payload = self._valid_payload(tmp_path)
        before = self._sandbox_files(tmp_path)
        payload["blobs"]["b" * 64] = base64.b64encode(b"unreferenced bytes\n").decode()

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 400
        assert "does not reference" in response.json()["detail"]
        assert self._sandbox_files(tmp_path) == before

    def test_a_missing_referenced_blob_is_refused(self, tmp_path: Path, server):
        payload = self._valid_payload(tmp_path)
        before = self._sandbox_files(tmp_path)
        referenced = sorted(payload["blobs"])
        del payload["blobs"][referenced[0]]

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 400
        assert "is missing from the upload" in response.json()["detail"]
        assert self._sandbox_files(tmp_path) == before

    def test_the_entry_count_cap_answers_413_and_stores_nothing(
        self, tmp_path: Path, server, monkeypatch
    ):
        monkeypatch.setenv(api_channel.MAX_BLOB_ENTRIES_ENV, "1")
        payload = self._valid_payload(tmp_path)
        assert len(payload["blobs"]) > 1
        before = self._sandbox_files(tmp_path)

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 413
        assert "blob entries" in response.json()["detail"]
        assert self._sandbox_files(tmp_path) == before

    def test_the_aggregate_decoded_size_cap_answers_413_and_stores_nothing(
        self, tmp_path: Path, server, monkeypatch
    ):
        monkeypatch.setenv(api_channel.MAX_TOTAL_BLOB_BYTES_ENV, "64")
        payload = self._valid_payload(tmp_path)
        before = self._sandbox_files(tmp_path)

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 413
        assert "bytes per checkpoint" in response.json()["detail"]
        assert self._sandbox_files(tmp_path) == before

    def test_a_valid_upload_round_trips_and_stays_idempotent_after_the_checks(
        self, tmp_path: Path, server
    ):
        """The closed contract does not break the honest path: a valid
        upload is stored, served back byte-identically, and re-putting
        it changes nothing."""
        payload = self._valid_payload(tmp_path)

        first = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )
        second = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert first.status_code == second.status_code == 200
        assert first.json()["checkpoint_id"] == second.json()["checkpoint_id"]
        served = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert served.status_code == 200
        manifest = json.loads(base64.b64decode(served.json()["manifest"]))
        for rel, entry in manifest["files"].items():
            digest = entry["digest"]
            assert (
                base64.b64decode(served.json()["blobs"][digest])
                == (tmp_path / "runner-a" / rel).read_bytes()
            )

    def test_the_store_primitive_refuses_malformed_keys_and_extra_blobs(self, tmp_path: Path):
        """Defense in depth WITHOUT the endpoint: ``CheckpointStore`` itself
        refuses a non-address key and an extra blob BEFORE any write —
        restoring the old unchecked ``_cas_path`` fails here (mutation)."""
        store = api_channel.CheckpointStore(tmp_path / "cas")
        blob = b"content\n"
        digest = _digest(blob)
        manifest = json.dumps(
            {
                "schema": "forge.wip.manifest/2",
                "work_id": WORK_ID,
                "sequence": 0,
                "source_oids": {},
                "files": {"a.txt": {"digest": digest, "mode": 0o644, "role": "new"}},
                "deletions": [],
            }
        ).encode()

        with pytest.raises(ValueError, match="not a content address|non-address blob keys"):
            store.put_checkpoint(
                work_id=WORK_ID,
                manifest_bytes=manifest,
                blobs={digest: blob, "../../escape": b"x"},
                sequence=0,
            )
        assert not (tmp_path / "escape").exists()

        with pytest.raises(ValueError, match="EXACTLY"):
            store.put_checkpoint(
                work_id=WORK_ID,
                manifest_bytes=manifest,
                blobs={digest: blob, "c" * 64: b"unreferenced"},
                sequence=0,
            )
        assert sorted(item.name for item in (tmp_path / "cas").iterdir()) == []

    def test_a_rotted_content_address_is_detected_not_silently_adopted(
        self, tmp_path: Path, server
    ):
        """An existing corrupted address under a digest the upload re-puts
        is DETECTED (500 naming it), never adopted as if stored."""
        payload = self._valid_payload(tmp_path)
        assert (
            server.put(
                f"/lane/checkpoints/{WORK_ID}",
                json=payload,
                headers={"Authorization": _bearer(WORK_ID)},
            ).status_code
            == 200
        )
        # Rot one stored blob under its live address.
        store_dir = Path(os.environ[api_channel.CHECKPOINT_STORE_DIR_ENV])
        digest = sorted(payload["blobs"])[0]
        (store_dir / digest[:2] / digest).write_bytes(b"ROTTED")

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 500
        assert "re-upload" in response.json()["detail"]


class TestAtomicWriteCrashWindow:
    def test_a_crash_at_the_blob_rename_leaves_no_partial_visible(self, tmp_path, monkeypatch):
        """The rename is the crash window: kill it, and the digest path never
        comes into existence — no reader can observe a half-written blob."""
        monkeypatch.setenv(api_channel.LANE_CONTROL_SECRET_ENV, SECRET)
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "server-store"))
        monkeypatch.delenv(api_channel.CHECKPOINT_RETENTION_ENV, raising=False)
        application = FastAPI()
        application.include_router(api_channel.checkpoint_channel_router)
        healthy = TestClient(application)

        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        first = _capture(store, tree, sequence=1)
        _channel(healthy).upload_checkpoint(store, WORK_ID)

        # A second capture with different content, then crash EVERY rename.
        _wip_tree(tmp_path / "runner-a", app=_APP_V3)
        second = _capture(store, tmp_path / "runner-a", sequence=2)

        def crashing_replace(src, dst):
            raise OSError("crash exactly at the rename")

        monkeypatch.setattr(api_channel, "_replace", crashing_replace)
        crashed = TestClient(application, raise_server_exceptions=False)
        response = crashed.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, second.artifact_id),
            headers={"Authorization": _bearer(WORK_ID)},
        )
        assert response.status_code >= 500

        server_dir = _server_store_dir()
        assert not _cas_file(server_dir, second.artifact_id).exists()  # no partial visible
        assert list(server_dir.rglob(".tmp-*")) == []  # the temp is cleaned up, not leaked
        # The FIRST checkpoint is untouched and still the latest on record.
        latest = healthy.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert latest.status_code == 200
        assert latest.json()["checkpoint_id"] == first.artifact_id

        # Recovery: after the crash passes, the retry lands whole.
        monkeypatch.setattr(api_channel, "_replace", os.replace)
        retry = healthy.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, second.artifact_id),
            headers={"Authorization": _bearer(WORK_ID)},
        )
        assert retry.status_code == 200
        assert retry.json()["checkpoint_id"] == second.artifact_id

    def test_a_crash_at_the_index_rename_leaves_orphans_but_no_phantom_checkpoint(
        self, tmp_path, monkeypatch
    ):
        """Blobs and manifest may be on disk when the index rename dies —
        that is an orphan (harmless, retried), never a checkpoint the
        control plane CLAIMS: the index still names only what landed."""
        monkeypatch.setenv(api_channel.LANE_CONTROL_SECRET_ENV, SECRET)
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "server-store"))
        monkeypatch.delenv(api_channel.CHECKPOINT_RETENTION_ENV, raising=False)
        application = FastAPI()
        application.include_router(api_channel.checkpoint_channel_router)
        healthy = TestClient(application)

        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        first = _capture(store, tree, sequence=1)
        _channel(healthy).upload_checkpoint(store, WORK_ID)
        _wip_tree(tmp_path / "runner-a", app=_APP_V3)
        second = _capture(store, tmp_path / "runner-a", sequence=2)

        calls = {"n": 0}
        real_replace = os.replace

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:  # blob(1), manifest(2), INDEX write(3)
                raise OSError("crash at the index rename")
            return real_replace(src, dst)

        monkeypatch.setattr(api_channel, "_replace", flaky_replace)
        crashed = TestClient(application, raise_server_exceptions=False)
        response = crashed.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, second.artifact_id),
            headers={"Authorization": _bearer(WORK_ID)},
        )
        assert response.status_code >= 500

        # Orphaned CAS content, but the CONTROL PLANE claims nothing: the
        # index still names only the first, fully-landed checkpoint.
        listed = healthy.get("/lane/checkpoints", headers={"Authorization": _bearer(LIST_SCOPE)})
        assert [entry["checkpoint_id"] for entry in listed.json()["checkpoints"]] == [
            first.artifact_id
        ]

        # The retry is idempotent over the orphaned CAS content.
        monkeypatch.setattr(api_channel, "_replace", os.replace)
        retry = healthy.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, second.artifact_id),
            headers={"Authorization": _bearer(WORK_ID)},
        )
        assert retry.status_code == 200
        after = healthy.get("/lane/checkpoints", headers={"Authorization": _bearer(LIST_SCOPE)})
        assert len(after.json()["checkpoints"]) == 2


class TestLatestOfWorkRetention:
    def test_retention_drops_the_oldest_beyond_keep_but_never_the_latest(
        self, tmp_path: Path, server, monkeypatch
    ):
        monkeypatch.setenv(api_channel.CHECKPOINT_RETENTION_ENV, "2")
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        channel = _channel(server)
        receipts = []
        for sequence, app in ((1, _APP_V2), (2, _APP_V3), (3, _APP_V4)):
            _wip_tree(tree, app=app)
            receipts.append(_capture(store, tree, sequence=sequence))
            channel.upload_checkpoint(store, WORK_ID)

        listed = channel.list_checkpoints()
        assert [item.checkpoint_id for item in listed] == [
            receipts[1].artifact_id,
            receipts[2].artifact_id,
        ]
        # The pruned oldest no longer resolves; the LATEST always does.
        pruned = server.get(
            f"/lane/checkpoints/{WORK_ID}",
            params={"checkpoint_id": receipts[0].artifact_id},
            headers={"Authorization": _bearer(WORK_ID)},
        )
        assert pruned.status_code == 404
        latest = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert latest.status_code == 200
        assert latest.json()["checkpoint_id"] == receipts[2].artifact_id

    def test_keep_zero_still_never_deletes_the_latest(self, tmp_path: Path, server):
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        channel = _channel(server)
        receipts = []
        for sequence, app in ((1, _APP_V2), (2, _APP_V3)):
            _wip_tree(tree, app=app)
            receipts.append(_capture(store, tree, sequence=sequence))
            channel.upload_checkpoint(store, WORK_ID)

        removed = api_channel.CheckpointStore(_server_store_dir()).apply_retention(WORK_ID, 0)

        assert removed == 1  # the older one went
        latest = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert latest.status_code == 200  # the LATEST survives keep_last=0
        assert latest.json()["checkpoint_id"] == receipts[1].artifact_id
        assert not _cas_file(_server_store_dir(), receipts[0].artifact_id).exists()

    def test_a_blob_shared_with_the_latest_survives_retention(self, tmp_path: Path, server):
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        channel = _channel(server)
        _wip_tree(tree, app=_APP_V2)  # notes.md content is IDENTICAL in both captures
        first = _capture(store, tree, sequence=1)
        channel.upload_checkpoint(store, WORK_ID)
        _wip_tree(tree, app=_APP_V3)
        second = _capture(store, tree, sequence=2)
        channel.upload_checkpoint(store, WORK_ID)

        removed = api_channel.CheckpointStore(_server_store_dir()).apply_retention(WORK_ID, 1)

        assert removed == 1
        shared = _digest(_WIP_NOTES)
        assert _cas_file(_server_store_dir(), shared).exists()  # the latest still needs it
        assert not _cas_file(
            _server_store_dir(), _digest(_APP_V2)
        ).exists()  # only the pruned one did
        assert not _cas_file(_server_store_dir(), first.artifact_id).exists()
        assert _cas_file(_server_store_dir(), second.artifact_id).exists()


class TestCheckpointingIntegration:
    def test_capture_with_upload_then_remote_restore_on_a_fresh_store(self, tmp_path: Path, server):
        """The additive wiring, end to end: ``capture_wip(upload=channel)``
        returns a receipt CARRYING the durable reference; a destroyed
        first runner's tree and store leave nothing behind; the second
        runner restores straight from ``remote_ref`` via
        ``restore_wip(download=channel)``."""
        tree = _wip_tree(tmp_path / "runner-a")
        store_a = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        channel = _channel(server)

        receipt = capture_wip(
            work_id=WORK_ID,
            root=tree,
            store=store_a,
            tracked_baseline=_baseline(),
            source_oids={"repo-main": "0" * 40},
            sequence=9,
            upload=channel,
        )

        assert receipt.verified is True
        assert receipt.remote_ref == format_checkpoint_ref(WORK_ID, receipt.artifact_id)
        work_id, checkpoint_id = parse_checkpoint_ref(receipt.remote_ref)
        assert (work_id, checkpoint_id) == (WORK_ID, receipt.artifact_id)

        shutil.rmtree(tree)  # the first runner is destroyed
        shutil.rmtree(tmp_path / "store-a")

        store_b = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        target = tmp_path / "runner-b"
        target.mkdir()
        (target / "README.md").write_bytes(_BASE_README)

        report = restore_wip(
            artifact_id=receipt.remote_ref,
            store=store_b,
            target=target,
            principal=TENANT,
            download=channel,
        )

        assert report.ok is True, report.failures
        assert (target / "src" / "app.py").read_bytes() == _APP_V2
        assert (target / "notes.md").read_bytes() == _WIP_NOTES
        assert os.stat(target / "scripts" / "run.sh").st_mode & 0o111
        assert not (target / "src" / "old.py").exists()
        # The fresh store now holds the manifest under its true address.
        assert store_b.get_verified(receipt.artifact_id, principal=TENANT) is not None

    def test_a_refused_upload_books_uploadfailed_and_keeps_the_local_capture(
        self, tmp_path: Path, httpx_mock
    ):
        httpx_mock.add_exception(httpx.ConnectError("control plane down"))
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        local_only = _capture(store, tree, sequence=5)
        api = LaneControlAPI(base_url="http://lane-control.test", token=SECRET)

        with pytest.raises(UploadFailed, match="control plane"):
            capture_wip(
                work_id=WORK_ID,
                root=tree,
                store=store,
                tracked_baseline=_baseline(),
                sequence=5,
                upload=CheckpointChannel(api),
            )

        # The LOCAL capture is still the recoverable state — nothing was lost.
        assert store.get_verified(local_only.artifact_id, principal=TENANT) is not None

    def test_a_malformed_remote_ref_fails_the_restore_with_evidence(self, tmp_path: Path, server):
        store = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        report = restore_wip(
            artifact_id="not-a-remote-reference",
            store=store,
            target=tmp_path / "runner-b",
            principal=TENANT,
            download=_channel(server),
        )

        assert report.ok is False
        assert any("could not be fetched" in failure for failure in report.failures)
        assert not (tmp_path / "runner-b" / "notes.md").exists()


# -- R28-06: sequence selection, never "latest arrival" ---------------------------


def _two_captures(store: ContentAddressedStore, tree: Path):
    """Sequence 20 lands FIRST, then the delayed sequence 10 — the
    reviewer's exact counterexample (out-of-order arrival)."""
    _wip_tree(tree, app=_APP_V2)
    newer = _capture(store, tree, sequence=20)
    _wip_tree(tree, app=_APP_V3)
    older = _capture(store, tree, sequence=10)
    return newer, older


class TestSequenceSelection:
    def test_a_delayed_lower_sequence_never_demotes_the_active_checkpoint(
        self, tmp_path: Path, server
    ):
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        newer, older = _two_captures(store, tree)

        first = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, newer.artifact_id),
            headers={"Authorization": _bearer(WORK_ID)},
        )
        assert first.status_code == 200 and first.json()["latest"] is True

        late = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, older.artifact_id),
            headers={"Authorization": _bearer(WORK_ID)},
        )

        # The delayed upload is STORED — as superseded history only.
        assert late.status_code == 200
        assert late.json()["latest"] is False
        assert late.json()["checkpoint_id"] == older.artifact_id
        # The ACTIVE checkpoint is still the higher sequence: GET-without-id
        # serves sequence 20, never the last arrival's sequence 10.
        latest = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert latest.status_code == 200
        assert latest.json()["checkpoint_id"] == newer.artifact_id
        assert latest.json()["sequence"] == 20
        assert latest.json()["latest"] is True
        # And the superseded artifact remains resolvable BY ID (explicit
        # operator rollback / exact resume addressing).
        exact = server.get(
            f"/lane/checkpoints/{WORK_ID}",
            params={"checkpoint_id": older.artifact_id},
            headers={"Authorization": _bearer(WORK_ID)},
        )
        assert exact.status_code == 200
        assert exact.json()["checkpoint_id"] == older.artifact_id
        assert exact.json()["latest"] is False

    def test_the_operator_list_flags_the_active_checkpoint_by_sequence(
        self, tmp_path: Path, server
    ):
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        newer, older = _two_captures(store, tree)
        for artifact in (newer.artifact_id, older.artifact_id):
            server.put(
                f"/lane/checkpoints/{WORK_ID}",
                json=_wire_payload(store, artifact),
                headers={"Authorization": _bearer(WORK_ID)},
            )

        listed = server.get("/lane/checkpoints", headers={"Authorization": _bearer(LIST_SCOPE)})

        entries = listed.json()["checkpoints"]
        # Sequence-ordered, exactly one latest — the higher sequence.
        assert [entry["sequence"] for entry in entries] == [10, 20]
        assert [entry["latest"] for entry in entries] == [False, True]
        assert entries[-1]["checkpoint_id"] == newer.artifact_id

    def test_retention_judges_by_sequence_not_arrival(self, tmp_path: Path, server):
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        newer, older = _two_captures(store, tree)
        for artifact in (newer.artifact_id, older.artifact_id):
            server.put(
                f"/lane/checkpoints/{WORK_ID}",
                json=_wire_payload(store, artifact),
                headers={"Authorization": _bearer(WORK_ID)},
            )

        removed = api_channel.CheckpointStore(_server_store_dir()).apply_retention(WORK_ID, 1)

        # The late-arriving sequence 10 is the one dropped — the ACTIVE
        # checkpoint (sequence 20) survives whatever landed last.
        assert removed == 1
        latest = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert latest.json()["checkpoint_id"] == newer.artifact_id
        assert not _cas_file(_server_store_dir(), older.artifact_id).exists()


class TestConcurrentIndexWrites:
    def test_simultaneous_uploads_all_land_and_the_active_is_deterministic(
        self, tmp_path: Path, server, monkeypatch
    ):
        """P04's interleaving, made real: N writers race the same work index
        (separate descriptors, one per thread — the same exclusion two
        PROCESSES get from the per-work flock). No append is lost, and the
        active pointer is the highest sequence, deterministically."""
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "server-store"))
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        uploads = []
        for sequence, app in ((1, _APP_V2), (2, _APP_V3), (3, _APP_V4)):
            _wip_tree(tree, app=app)
            uploads.append((sequence, _capture(store, tree, sequence=sequence)))

        server_store = api_channel.CheckpointStore(tmp_path / "server-store")
        errors: list[Exception] = []

        def writer(sequence: int, artifact_id: str) -> None:
            try:
                server_store.put_checkpoint(
                    work_id=WORK_ID,
                    manifest_bytes=store.get_verified(artifact_id, principal=TENANT),
                    blobs={
                        str(entry["digest"]): store.get_verified(
                            str(entry["digest"]), principal=TENANT
                        )
                        for entry in json.loads(store.get_verified(artifact_id, principal=TENANT))[
                            "files"
                        ].values()
                    },
                    sequence=sequence,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced by the assert below
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(sequence, receipt.artifact_id))
            for sequence, receipt in uploads
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        index = server_store._load_index(WORK_ID)  # noqa: SLF001 — the asserted state
        stored_sequences = sorted(entry["sequence"] for entry in index["checkpoints"])
        assert stored_sequences == [1, 2, 3]  # nobody's append was lost
        active = server_store.entry(WORK_ID)
        assert active is not None and active["sequence"] == 3  # deterministic selection
        # Retention concurrent with uploads sees the same discipline: the
        # ACTIVE checkpoint always survives.
        assert server_store.apply_retention(WORK_ID, 0) == 2
        assert server_store.entry(WORK_ID)["sequence"] == 3


# -- R28-05/R28-07: exact downloads and generation-scoped tokens -------------------


class TestExactCheckpointDownload:
    def test_the_client_downloads_the_named_checkpoint_not_the_active_one(
        self, tmp_path: Path, server
    ):
        """The resume leg's passthrough: ``checkpoint_id`` rides the GET as
        ``?checkpoint_id=``, so the OLDER named checkpoint is served even
        though a higher-sequence one is active."""
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        newer, older = _two_captures(store, tree)
        channel = _channel(server)
        for artifact in (newer.artifact_id, older.artifact_id):
            server.put(
                f"/lane/checkpoints/{WORK_ID}",
                json=_wire_payload(store, artifact),
                headers={"Authorization": _bearer(WORK_ID)},
            )

        fresh = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        api = LaneControlAPI(base_url="http://testserver", token=SECRET, client=server)
        handle = download_checkpoint(WORK_ID, api, fresh, checkpoint_id=older.artifact_id)

        assert handle.artifact_id == older.artifact_id  # the NAMED one, not sequence 20
        assert handle.sequence == 10
        # Without a name the ACTIVE one answers — the two spellings differ.
        latest_handle = channel.download_checkpoint(WORK_ID, fresh)
        assert latest_handle.artifact_id == newer.artifact_id


class TestWorkScopedTokenGenerations:
    def test_a_generation_scoped_token_differs_from_the_legacy_bytes(self):
        legacy = work_scoped_token(SECRET, WORK_ID)

        assert work_scoped_token(SECRET, WORK_ID, generation=2) != legacy
        assert work_scoped_token(SECRET, WORK_ID, generation=2) != work_scoped_token(
            SECRET, WORK_ID, generation=3
        )
        assert work_scoped_token(SECRET, WORK_ID, generation=None) == legacy
        # Work-scoping survives the generation component: another work's
        # generation-scoped token still differs on every generation.
        assert work_scoped_token(SECRET, "wp-other", generation=2) != work_scoped_token(
            SECRET, WORK_ID, generation=2
        )


# -- R28-05: the resume consumer binds the exact approved checkpoint ----------------


def _resume_command(seq: int, payload: dict) -> dict:
    command = ControlCommand.model_validate(
        {
            "schema": "forge.proposal.control-command/1",
            "command_id": f"cmd-resume-{seq}",
            "work_id": WORK_ID,
            "sequence": seq,
            "kind": "resume",
            "actor_ref": "human:op",
            "actor_origin": "server_authenticated_human",
            "idempotency_key": f"resume-key-{seq}",
            "status": "received",
            "payload": payload,
        }
    )
    return command.model_dump(mode="json")


CP = "http://cp.test"
LANE_ENV = {
    "FORGE_LANE_CONTROL_URL": CP,
    "FORGE_LANE_CONTROL_TOKEN": "lane-token-1",
}


class TestExactResumeSelection:
    """``forge.lane_driver._maybe_restore_wip`` over a faked control plane.

    R28-05/NEXT-03: the durable RESUME COMMAND ROW's explicit ResumeSpec
    decides WHICH checkpoint downloads (read from
    ``GET /lane/controls/resume-spec`` — the row, never the pending
    queue); the fallback to the active one is recorded in the report,
    never silent, and a REQUIRED resume never turns a timeout into a
    "latest" guess."""

    @pytest.fixture()
    def captures(self, tmp_path: Path):
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        return (tree, store, *_two_captures(store, tree))

    @pytest.fixture()
    def lane_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        cwd = tmp_path / "lane-checkout"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        for key, value in LANE_ENV.items():
            monkeypatch.setenv(key, value)
        return cwd

    def _serve_checkpoint(self, httpx_mock, store, artifact_id: str, sequence: int) -> None:
        document = _wire_payload(store, artifact_id)
        document["checkpoint_id"] = artifact_id
        document["sequence"] = sequence
        httpx_mock.add_response(
            url=f"{CP}/lane/checkpoints/{WORK_ID}?checkpoint_id={artifact_id}", json=document
        )

    @staticmethod
    def _serve_resume_spec(httpx_mock, command: dict | None) -> None:
        """Fake the DURABLE resume-command read (NEXT-03): the latest
        resume ROW, whatever rung it sits on — never the pending queue."""
        httpx_mock.add_response(
            url=f"{CP}/lane/controls/resume-spec?work_id={WORK_ID}",
            json={"work_id": WORK_ID, "command": command},
        )

    def test_the_resume_reference_downloads_that_exact_checkpoint(
        self, httpx_mock, lane_cwd, captures
    ):
        from forge.lane_driver import _maybe_restore_wip

        tree, store, newer, older = captures
        self._serve_resume_spec(
            httpx_mock,
            _resume_command(
                5,
                {
                    "checkpoint_ref": format_checkpoint_ref(WORK_ID, older.artifact_id),
                    "run_id": WORK_ID,
                },
            ),
        )
        self._serve_checkpoint(httpx_mock, store, older.artifact_id, 10)

        report = _maybe_restore_wip(WORK_ID)

        assert report is not None
        assert report["restored"] is True, report
        assert report["checkpoint_selection"] == "exact"
        assert report["checkpoint_ref"] == format_checkpoint_ref(WORK_ID, older.artifact_id)
        assert report["checkpoint_sequence"] == 10
        assert report["resume_command"] == "cmd-resume-5"
        # The request NAMED the checkpoint — the older one, not sequence 20.
        checkpoint_gets = [
            request
            for request in httpx_mock.get_requests()
            if request.url.path == f"/lane/checkpoints/{WORK_ID}"
        ]
        assert [request.url.params.get("checkpoint_id") for request in checkpoint_gets] == [
            older.artifact_id
        ]

    def test_a_bare_checkpoint_id_payload_binds_too(self, httpx_mock, lane_cwd, captures):
        from forge.lane_driver import _maybe_restore_wip

        _, store, newer, older = captures
        self._serve_resume_spec(
            httpx_mock, _resume_command(6, {"checkpoint_id": older.artifact_id})
        )
        self._serve_checkpoint(httpx_mock, store, older.artifact_id, 10)

        report = _maybe_restore_wip(WORK_ID)

        assert report["checkpoint_selection"] == "exact"
        assert report["checkpoint_ref"] == format_checkpoint_ref(WORK_ID, older.artifact_id)

    def test_the_spec_binds_even_after_the_command_left_the_pending_set(
        self, httpx_mock, lane_cwd, captures
    ):
        """NEXT-03: the resume decision is the durable ROW, not the pending
        queue. A resume command already ACKED all the way to
        ``checkpointed`` still names the exact checkpoint — the pending
        view would return nothing here and the old consumer silently
        fell back to latest."""
        from forge.lane_driver import _maybe_restore_wip

        _, store, newer, older = captures
        command = _resume_command(
            5, {"checkpoint_ref": format_checkpoint_ref(WORK_ID, older.artifact_id)}
        )
        command["status"] = "checkpointed"  # long gone from pending
        self._serve_resume_spec(httpx_mock, command)
        self._serve_checkpoint(httpx_mock, store, older.artifact_id, 10)

        report = _maybe_restore_wip(WORK_ID)

        assert report["restored"] is True, report
        assert report["checkpoint_selection"] == "exact"
        assert report["checkpoint_ref"] == format_checkpoint_ref(WORK_ID, older.artifact_id)

    def test_a_required_resume_with_an_unreachable_spec_never_selects_latest(
        self, httpx_mock, lane_cwd, monkeypatch
    ):
        """NEXT-03: a control API timeout must not select another
        checkpoint. With FORGE_LANE_RESUME=1 the failed spec lookup is a
        refusal the required-restore gate halts on — and no checkpoint
        GET ever leaves the lane."""
        from forge.lane_driver import _maybe_restore_wip

        monkeypatch.setenv("FORGE_LANE_RESUME", "1")
        httpx_mock.add_exception(
            httpx.ConnectError("control plane unreachable"),
            url=f"{CP}/lane/controls/resume-spec?work_id={WORK_ID}",
        )

        report = _maybe_restore_wip(WORK_ID)

        assert report is not None
        assert report["restored"] is False
        assert report["checkpoint_selection"] == "unavailable"
        assert any("unavailable" in failure for failure in report["failures"])
        assert [
            request
            for request in httpx_mock.get_requests()
            if request.url.path.startswith("/lane/checkpoints")
        ] == []

    def test_a_fresh_run_with_an_unreachable_spec_still_falls_back_to_active(
        self, httpx_mock, lane_cwd, captures
    ):
        """The labelled legacy fallback stays available to a FRESH run (no
        resume marker): the unreachable spec degrades to the active
        checkpoint with the note, never a halt."""
        from forge.lane_driver import _maybe_restore_wip

        _, store, newer, older = captures
        httpx_mock.add_exception(
            httpx.ConnectError("control plane unreachable"),
            url=f"{CP}/lane/controls/resume-spec?work_id={WORK_ID}",
        )
        document = _wire_payload(store, newer.artifact_id)
        document["checkpoint_id"] = newer.artifact_id
        document["sequence"] = 20
        httpx_mock.add_response(url=f"{CP}/lane/checkpoints/{WORK_ID}", json=document)

        report = _maybe_restore_wip(WORK_ID)

        assert report["restored"] is True, report
        assert report["checkpoint_selection"] == "latest"
        assert "unavailable" in report["selection_note"]

    def test_an_explicit_restart_discards_the_wip_without_any_download(
        self, httpx_mock, lane_cwd, monkeypatch
    ):
        """NEXT-03's third mode: FORGE_LANE_RESUME=restart intentionally
        drops the WIP — zero requests to the control plane, a documented
        discard in the report."""
        import forge.lane_driver as lane_driver
        from forge.lane_driver import _maybe_restore_wip

        assert lane_driver.resume_mode({"FORGE_LANE_RESUME": "restart"}) == "restart"
        assert lane_driver.resume_requested({"FORGE_LANE_RESUME": "restart"}) is False
        monkeypatch.setenv("FORGE_LANE_RESUME", "restart")  # the discard branch reads env

        report = _maybe_restore_wip(WORK_ID)

        assert report is not None
        assert report["restored"] is False
        assert report["checkpoint_selection"] == "discarded"
        assert "intentionally discarded" in report["note"]
        # No request ever left — not even the resume-spec read.
        assert httpx_mock.get_requests() == []

    def test_a_resume_without_a_reference_falls_back_and_records_it(
        self, httpx_mock, lane_cwd, captures
    ):
        from forge.lane_driver import _maybe_restore_wip

        _, store, newer, older = captures
        self._serve_resume_spec(httpx_mock, _resume_command(7, {}))
        document = _wire_payload(store, newer.artifact_id)
        document["checkpoint_id"] = newer.artifact_id
        document["sequence"] = 20
        httpx_mock.add_response(url=f"{CP}/lane/checkpoints/{WORK_ID}", json=document)

        report = _maybe_restore_wip(WORK_ID)

        assert report is not None
        assert report["restored"] is True, report
        assert report["checkpoint_selection"] == "latest"  # recorded, not silent
        assert "fallback" in report["selection_note"]
        assert report["checkpoint_ref"] == format_checkpoint_ref(WORK_ID, newer.artifact_id)
        # The GET named no checkpoint — the ACTIVE one answered.
        checkpoint_gets = [
            request
            for request in httpx_mock.get_requests()
            if request.url.path == f"/lane/checkpoints/{WORK_ID}"
        ]
        assert checkpoint_gets[-1].url.params.get("checkpoint_id") is None

    def test_no_resume_command_at_all_falls_back_to_the_active(
        self, httpx_mock, lane_cwd, captures
    ):
        from forge.lane_driver import _maybe_restore_wip

        _, store, newer, older = captures
        self._serve_resume_spec(httpx_mock, None)
        document = _wire_payload(store, newer.artifact_id)
        document["checkpoint_id"] = newer.artifact_id
        document["sequence"] = 20
        httpx_mock.add_response(url=f"{CP}/lane/checkpoints/{WORK_ID}", json=document)

        report = _maybe_restore_wip(WORK_ID)

        assert report["checkpoint_selection"] == "latest"
        assert report["resume_command"] is None

    def test_a_cross_work_reference_is_a_refusal_never_a_latest_substitute(
        self, httpx_mock, lane_cwd
    ):
        from forge.lane_driver import _maybe_restore_wip

        self._serve_resume_spec(
            httpx_mock,
            _resume_command(
                8, {"checkpoint_ref": format_checkpoint_ref("wp-someone-else", "a" * 64)}
            ),
        )

        report = _maybe_restore_wip(WORK_ID)

        assert report is not None
        assert report["restored"] is False
        assert report["checkpoint_selection"] == "refused"
        assert any("wp-someone-else" in failure for failure in report["failures"])
        # Nothing was downloaded — no checkpoint GET ever left.
        assert [
            request
            for request in httpx_mock.get_requests()
            if request.url.path.startswith("/lane/checkpoints")
        ] == []

    def test_a_malformed_reference_is_a_refusal(self, httpx_mock, lane_cwd):
        from forge.lane_driver import _maybe_restore_wip

        self._serve_resume_spec(httpx_mock, _resume_command(9, {"checkpoint_ref": "junk"}))

        report = _maybe_restore_wip(WORK_ID)

        assert report["checkpoint_selection"] == "refused"
        assert report["restored"] is False

    def test_the_lane_restore_lands_a_generation_and_points_the_checkout_at_it(
        self, httpx_mock, lane_cwd, captures
    ):
        """R32-01 at the lane seam: the restore never replaces the checkout
        the lane process sits in — it lands a SIBLING generation, the
        report names it (``workspace_generation``), and the checkout's
        ``.forge/workspace-generation`` pointer records it for the
        collector step. The parent shell's directory stays exactly where
        it was."""
        from forge.lane_driver import _maybe_restore_wip

        _tree, store, _newer, older = captures
        self._serve_resume_spec(
            httpx_mock, _resume_command(5, {"checkpoint_id": older.artifact_id})
        )
        self._serve_checkpoint(httpx_mock, store, older.artifact_id, 10)

        report = _maybe_restore_wip(WORK_ID)

        assert report["restored"] is True, report
        generation = Path(report["workspace_generation"])
        assert generation.parent == lane_cwd.parent
        assert generation.name == f".forge-workspace-gen-{older.artifact_id[:12]}"
        assert generation.is_dir()
        # The checkpointed bytes live in the GENERATION, not the checkout.
        assert (generation / "src" / "app.py").read_bytes() == _APP_V3
        assert not (lane_cwd / "src").exists()
        # The collector contract: the pointer inside the checkout names the
        # active generation and the checkpoint that produced it.
        pointer = json.loads((lane_cwd / ".forge" / "workspace-generation").read_text())
        assert pointer["schema"] == "forge.workspace-generation/1"
        assert pointer["work_id"] == WORK_ID
        assert pointer["checkpoint_id"] == older.artifact_id
        assert pointer["generation"] == generation.name
        assert pointer["generation_path"] == str(generation)
        # No promotion leftovers beside the checkout.
        assert list(lane_cwd.parent.glob(".forge-restore-*")) == []


# -- R28-14: the unified storage policy, quotas and health report -------------------


def _tiny_checkpoint(work_id: str, files: dict[str, bytes], sequence: int):
    """A minimal valid manifest + its exact blob set, handcrafted in-test."""
    manifest = json.dumps(
        {
            "schema": "forge.wip.manifest/2",
            "work_id": work_id,
            "sequence": sequence,
            "source_oids": {},
            "files": {
                name: {"digest": _digest(data), "mode": 0o644, "role": "new"}
                for name, data in sorted(files.items())
            },
            "deletions": [],
        }
    ).encode()
    document = json.loads(manifest)
    blobs = {entry["digest"]: files[name] for name, entry in document["files"].items()}
    return manifest, blobs


class TestStoragePolicy:
    def test_from_env_folds_the_existing_caps_and_retention(self, monkeypatch):
        monkeypatch.setenv(api_channel.MAX_BLOB_BYTES_ENV, "1000")
        monkeypatch.setenv(api_channel.MAX_BLOB_ENTRIES_ENV, "7")
        monkeypatch.setenv(api_channel.CHECKPOINT_RETENTION_ENV, "3")
        monkeypatch.setenv(api_channel.MAX_WORK_TOTAL_BYTES_ENV, "5000")
        monkeypatch.setenv(api_channel.HISTORY_KEEP_ENV, "2")

        policy = api_channel.StoragePolicy.from_env()

        assert policy == api_channel.StoragePolicy(
            max_blob_bytes=1000,
            max_manifest_entries=7,
            max_total_bytes_per_work=5000,
            max_checkpoints_per_work=3,
            history_keep=2,
        )

    def test_defaults_keep_the_documented_behaviour(self, monkeypatch):
        for name in (
            api_channel.MAX_BLOB_BYTES_ENV,
            api_channel.MAX_BLOB_ENTRIES_ENV,
            api_channel.CHECKPOINT_RETENTION_ENV,
            api_channel.MAX_WORK_TOTAL_BYTES_ENV,
            api_channel.HISTORY_KEEP_ENV,
        ):
            monkeypatch.delenv(name, raising=False)

        policy = api_channel.StoragePolicy.from_env()

        assert policy.max_blob_bytes == api_channel.DEFAULT_MAX_BLOB_BYTES
        assert policy.max_manifest_entries == api_channel.DEFAULT_MAX_BLOB_ENTRIES
        assert policy.max_total_bytes_per_work == 0  # no quota unless asked
        assert policy.max_checkpoints_per_work == 0  # keep everything
        assert policy.history_keep == 0
        assert policy.retention_keep() == 0  # therefore: no cleanup at all

    def test_retention_keep_bounds_below_the_dr_history_floor(self):
        no_floor = api_channel.StoragePolicy(max_checkpoints_per_work=2)
        floor_only = api_channel.StoragePolicy(history_keep=1)
        floor_wins = api_channel.StoragePolicy(max_checkpoints_per_work=1, history_keep=2)

        assert no_floor.retention_keep() == 2
        # The floor means: the active checkpoint PLUS history_keep superseded
        # ones survive even when the retention cap alone would drop them.
        assert floor_only.retention_keep() == 2
        assert floor_wins.retention_keep() == 3

    def test_the_dr_floor_bounds_what_upload_cleanup_may_drop(self, tmp_path: Path):
        """history_keep=1: after three puts, the active plus ONE superseded
        checkpoint survive — a bad resume can still roll back one step."""
        policy = api_channel.StoragePolicy(history_keep=1)
        store = api_channel.CheckpointStore(tmp_path / "cas", policy=policy)
        for sequence in (1, 2, 3):
            manifest, blobs = _tiny_checkpoint(
                WORK_ID, {f"v{sequence}.txt": f"content {sequence}\n".encode()}, sequence
            )
            store.put_checkpoint(
                work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=sequence
            )

        entries = store.list_entries()

        assert [entry["sequence"] for entry in entries] == [2, 3]  # active + 1 history
        assert entries[-1]["latest"] is True


class TestQuotaEnforcement:
    def test_an_over_quota_upload_is_refused_before_any_write(self, tmp_path: Path):
        """The store's own refusal (no HTTP): quota exhaustion is explicit,
        recoverable, and leaves the previous checkpoint byte-identical."""
        first_files = {"a.txt": b"first attempt content\n"}
        manifest, blobs = _tiny_checkpoint(WORK_ID, first_files, 1)
        usage = len(manifest) + sum(len(data) for data in blobs.values())
        policy = api_channel.StoragePolicy(max_total_bytes_per_work=usage)
        store = api_channel.CheckpointStore(tmp_path / "cas", policy=policy)
        store.put_checkpoint(work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=1)
        before = {
            item: item.stat().st_mtime for item in (tmp_path / "cas").rglob("*") if item.is_file()
        }

        second_files = {"a.txt": b"first attempt content\n", "b.txt": b"brand new blob\n"}
        manifest2, blobs2 = _tiny_checkpoint(WORK_ID, second_files, 2)
        with pytest.raises(api_channel.StorageQuotaExceededError, match="per-work quota"):
            store.put_checkpoint(
                work_id=WORK_ID, manifest_bytes=manifest2, blobs=blobs2, sequence=2
            )

        assert isinstance(
            api_channel.StorageQuotaExceededError("x"), ValueError
        )  # every old except-ValueError path still refuses
        after = {
            item: item.stat().st_mtime for item in (tmp_path / "cas").rglob("*") if item.is_file()
        }
        assert after == before  # byte-identical: nothing was added or removed
        entry = store.entry(WORK_ID)
        assert entry is not None and entry["sequence"] == 1  # the previous checkpoint stands

    def test_an_idempotent_re_put_under_quota_is_not_new_usage(self, tmp_path: Path):
        """Content addressing: the same checkpoint again adds ZERO fresh
        bytes, so a tight quota never refuses a retry of what landed."""
        manifest, blobs = _tiny_checkpoint(WORK_ID, {"a.txt": b"content\n"}, 1)
        usage = len(manifest) + sum(len(data) for data in blobs.values())
        store = api_channel.CheckpointStore(
            tmp_path / "cas", policy=api_channel.StoragePolicy(max_total_bytes_per_work=usage)
        )
        first = store.put_checkpoint(
            work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=1
        )

        again = store.put_checkpoint(
            work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=1
        )

        assert first["checkpoint_id"] == again["checkpoint_id"]
        assert again["latest"] is True

    def test_the_http_boundary_answers_413_and_preserves_the_work(
        self, tmp_path: Path, server, monkeypatch
    ):
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        _wip_tree(tree, app=_APP_V2)
        first = _capture(store, tree, sequence=1)
        payload = _wire_payload(store, first.artifact_id)
        usage = len(base64.b64decode(payload["manifest"])) + sum(
            len(base64.b64decode(item)) for item in payload["blobs"].values()
        )
        monkeypatch.setenv(api_channel.MAX_WORK_TOTAL_BYTES_ENV, str(usage))

        landed = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )
        assert landed.status_code == 200

        _wip_tree(tree, app=_APP_V3)
        second = _capture(store, tree, sequence=2)
        refused = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, second.artifact_id),
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert refused.status_code == 413
        assert "per-work quota" in refused.json()["detail"]
        # The work still resolves to its FIRST checkpoint — untouched.
        latest = server.get(
            f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
        )
        assert latest.status_code == 200
        assert latest.json()["checkpoint_id"] == first.artifact_id

    def test_the_store_enforces_the_manifest_entry_cap_and_blob_cap(self, tmp_path: Path):
        """The policy is a property of the STORAGE, not of one HTTP path."""
        store = api_channel.CheckpointStore(
            tmp_path / "cas", policy=api_channel.StoragePolicy(max_blob_bytes=8)
        )
        big = {"a.txt": b"this blob is far over eight bytes\n"}
        manifest, blobs = _tiny_checkpoint(WORK_ID, big, 1)
        with pytest.raises(api_channel.StorageQuotaExceededError, match="per blob"):
            store.put_checkpoint(work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=1)

        narrow = api_channel.CheckpointStore(
            tmp_path / "cas2", policy=api_channel.StoragePolicy(max_manifest_entries=1)
        )
        two = {"a.txt": b"x\n", "b.txt": b"y\n"}
        manifest2, blobs2 = _tiny_checkpoint(WORK_ID, two, 1)
        with pytest.raises(api_channel.StorageQuotaExceededError, match="file entries"):
            narrow.put_checkpoint(
                work_id=WORK_ID, manifest_bytes=manifest2, blobs=blobs2, sequence=1
            )

        assert sorted(item.name for item in (tmp_path / "cas").iterdir()) == []  # nothing landed
        assert sorted(item.name for item in (tmp_path / "cas2").iterdir()) == []


class TestStorageHealthReport:
    def _seeded_store(self, tmp_path: Path) -> api_channel.CheckpointStore:
        store = api_channel.CheckpointStore(tmp_path / "cas")
        manifest_a1, blobs_a1 = _tiny_checkpoint("wp-a", {"shared.txt": b"shared\n"}, 1)
        store.put_checkpoint(work_id="wp-a", manifest_bytes=manifest_a1, blobs=blobs_a1, sequence=1)
        manifest_a2, blobs_a2 = _tiny_checkpoint(
            "wp-a", {"shared.txt": b"shared\n", "v2.txt": b"second\n"}, 2
        )
        store.put_checkpoint(work_id="wp-a", manifest_bytes=manifest_a2, blobs=blobs_a2, sequence=2)
        manifest_b, blobs_b = _tiny_checkpoint("wp-b", {"other.txt": b"other work\n"}, 1)
        store.put_checkpoint(work_id="wp-b", manifest_bytes=manifest_b, blobs=blobs_b, sequence=1)
        return store

    def test_the_report_accounts_usage_per_work_and_finds_orphans(self, tmp_path: Path):
        store = self._seeded_store(tmp_path)
        root = tmp_path / "cas"
        orphan_data = b"present on disk, referenced by nobody\n"
        orphan_digest = _digest(orphan_data)
        (root / orphan_digest[:2]).mkdir(parents=True, exist_ok=True)
        (root / orphan_digest[:2] / orphan_digest).write_bytes(orphan_data)
        (root / orphan_digest[:2] / ".tmp-crash-leftover").write_bytes(b"partial")

        report = store.storage_health_report()

        assert report["root"] == str(root)
        assert set(report["policy"]) == {
            "max_blob_bytes",
            "max_manifest_entries",
            "max_total_bytes_per_work",
            "max_checkpoints_per_work",
            "history_keep",
            "cleanup_trigger",
        }
        assert report["works"]["wp-a"]["checkpoints"] == 2
        assert report["works"]["wp-a"]["missing_digests"] == []
        assert report["works"]["wp-b"]["checkpoints"] == 1
        # wp-a references: 2 manifests + shared + v2 blobs = 4 digests.
        assert report["works"]["wp-a"]["referenced_digests"] == 4
        # Orphan detection: the planted digest is on disk, referenced by NO work.
        assert report["orphan_cas_entries"] == [orphan_digest]
        assert report["orphan_bytes"] == len(orphan_data)
        # Temp files are listed relative to the report's own root.
        assert report["temp_files"] == [f"{orphan_digest[:2]}/.tmp-crash-leftover"]
        # Disk usage counts every file under the CAS shards (orphans and
        # crash temps included) — the operator's actual footprint.
        shard_bytes = sum(
            item.stat().st_size
            for shard in root.iterdir()
            if shard.is_dir() and shard.name != "works"
            for item in shard.iterdir()
            if item.is_file()
        )
        assert report["disk_usage_bytes"] == shard_bytes
        assert report["cas_entry_count"] == 7  # 6 referenced digests + the orphan
        assert report["over_quota_works"] == []  # no quota configured

    def test_a_missing_referenced_blob_is_named_not_hidden(self, tmp_path: Path):
        store = self._seeded_store(tmp_path)
        manifest_b, _ = _tiny_checkpoint("wp-b", {"other.txt": b"other work\n"}, 1)
        blob_digest = json.loads(manifest_b)["files"]["other.txt"]["digest"]
        (tmp_path / "cas" / blob_digest[:2] / blob_digest).unlink()

        report = store.storage_health_report()

        assert report["works"]["wp-b"]["missing_digests"] == [blob_digest]
        # The missing blob is ALSO an unreferenced-on-paper orphan? No — it
        # is referenced but absent; orphans are the opposite direction.
        assert blob_digest not in report["orphan_cas_entries"]

    def test_a_work_over_quota_is_flagged_with_its_reasons(self, tmp_path: Path):
        store = self._seeded_store(tmp_path)
        tiny = api_channel.StoragePolicy(max_total_bytes_per_work=1, max_checkpoints_per_work=1)

        report = store.storage_health_report(policy=tiny)

        assert report["over_quota_works"] == ["wp-a", "wp-b"]
        assert report["works"]["wp-a"]["over_quota_reasons"] == ["bytes", "checkpoints"]
        assert report["works"]["wp-b"]["over_quota_reasons"] == ["bytes"]

    def test_the_http_health_endpoint_serves_the_report_to_the_operator(
        self, tmp_path: Path, server
    ):
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        _capture(store, tree, sequence=1)
        _channel(server).upload_checkpoint(store, WORK_ID)

        health = server.get(
            "/lane/checkpoints/health", headers={"Authorization": _bearer(LIST_SCOPE)}
        )
        wrong_scope = server.get(
            "/lane/checkpoints/health", headers={"Authorization": _bearer(WORK_ID)}
        )

        assert wrong_scope.status_code == 401  # a work token is not an operator token
        assert health.status_code == 200  # the literal route beat {work_id}: health answered
        body = health.json()
        assert body["works"][WORK_ID]["checkpoints"] == 1
        assert body["orphan_cas_entries"] == []
        assert body["disk_usage_bytes"] > 0


# ----------------------------------------------------------------------
# NEXT-05: the request ALLOCATION bound — before JSON, before base64
# ----------------------------------------------------------------------

#: A genuine OTHER PROCESS holding the per-work index flock — the
#: declared multi-process topology proof (NEXT-06).
HOLD_LOCK_SNIPPET = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "time.sleep(30)\n"
)


class TestRequestAllocationBound:
    """A declared cap on the decoded aggregate is not an allocation bound:
    ``request.json()`` materializes the whole encoded body first. These
    pin the network-boundary refusal — the Content-Length is judged
    before one body byte is read, and a chunked or lying body is refused
    while it streams, never after it materialized."""

    def test_an_oversized_content_length_is_refused_before_the_body_is_read(
        self, tmp_path: Path, server, monkeypatch
    ):
        monkeypatch.setenv(api_channel.MAX_REQUEST_BYTES_ENV, "1024")
        manifest, blobs = _tiny_checkpoint(WORK_ID, {"a.txt": b"x" * 8192}, 1)
        payload = json.dumps(
            {
                "manifest": base64.b64encode(manifest).decode("ascii"),
                "blobs": {
                    digest: base64.b64encode(data).decode("ascii") for digest, data in blobs.items()
                },
                "sequence": 1,
            }
        ).encode()

        response = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            content=payload,
            headers={"Authorization": _bearer(WORK_ID)},
        )

        assert response.status_code == 413
        assert api_channel.MAX_REQUEST_BYTES_ENV in response.json()["detail"]
        assert "1024" in response.json()["detail"]
        # A refused upload leaves no metadata and no artifact references.
        assert (
            server.get(
                f"/lane/checkpoints/{WORK_ID}", headers={"Authorization": _bearer(WORK_ID)}
            ).status_code
            == 404
        )

    async def test_a_chunked_body_over_the_cap_is_refused_mid_stream(self):
        """No Content-Length at all (chunked encoding): the accumulator
        itself refuses the moment the cap is passed."""
        request = _raw_stream_request([b"x" * 4096, b"y" * 4096])
        with pytest.raises(HTTPException) as excinfo:
            await api_channel._read_bounded_body(request, 4096)
        assert excinfo.value.status_code == 413
        assert api_channel.MAX_REQUEST_BYTES_ENV in excinfo.value.detail

    async def test_a_misleading_content_length_is_refused_by_the_stream_cap(self):
        """The header says 16 bytes; the stream delivers more. The bound
        that answers is the accumulator's, not the client's word."""
        request = _raw_stream_request([b"x" * 4096, b"y" * 4096], content_length="16")
        with pytest.raises(HTTPException) as excinfo:
            await api_channel._read_bounded_body(request, 1024)
        assert excinfo.value.status_code == 413
        assert "did not bound it" in excinfo.value.detail

    async def test_a_body_under_the_cap_passes_through_byte_identical(self):
        request = _raw_stream_request([b"hello ", b"world"], content_length="11")
        assert await api_channel._read_bounded_body(request, 1024) == b"hello world"

    def test_a_valid_upload_under_a_small_cap_round_trips(self, tmp_path, server, monkeypatch):
        monkeypatch.setenv(api_channel.MAX_REQUEST_BYTES_ENV, str(256 * 1024))
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        _capture(store, tree, sequence=7)

        ref = _channel(server).upload_checkpoint(store, WORK_ID)

        handle = _channel(server).download_checkpoint(
            WORK_ID, ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        )
        assert handle.artifact_id == ref.checkpoint_id
        assert handle.verified is True

    def test_the_health_report_carries_the_request_cap(self, server, monkeypatch):
        monkeypatch.setenv(api_channel.MAX_REQUEST_BYTES_ENV, "4096")

        health = server.get(
            "/lane/checkpoints/health", headers={"Authorization": _bearer(LIST_SCOPE)}
        )
        report = api_channel.storage_health_report(
            Path(os.environ[api_channel.CHECKPOINT_STORE_DIR_ENV])
        )

        assert health.json()["request_max_bytes"] == 4096
        assert report["request_max_bytes"] == 4096


def _raw_stream_request(chunks: list[bytes], *, content_length: str | None = None) -> Request:
    """A starlette Request over a hand-rolled ASGI receive() — the precise
    chunked/misleading-header shapes a cooperating client cannot produce."""
    headers: list[tuple[bytes, bytes]] = []
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    scope = {
        "type": "http",
        "method": "PUT",
        "path": f"/lane/checkpoints/{WORK_ID}",
        "headers": headers,
        "query_string": b"",
    }
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    return Request(scope, receive)


# ----------------------------------------------------------------------
# NEXT-06: concurrent workers — bounded lock, holder identity, decisions
# ----------------------------------------------------------------------


class TestConcurrentWriters:
    """Two stores over ONE directory (the multi-process deployment shape):
    the lock serializes the index, the loser of a bounded retry is
    superseded history, and the winner's state stands byte-identical."""

    def _two_stores(self, tmp_path: Path):
        root = tmp_path / "cas"
        return (
            api_channel.CheckpointStore(root),
            api_channel.CheckpointStore(root),
            root,
        )

    def test_a_late_lower_sequence_upload_lands_as_superseded_history(self, tmp_path):
        store_a, store_b, _root = self._two_stores(tmp_path)

        manifest, blobs = _tiny_checkpoint(WORK_ID, {"v3.txt": b"three\n"}, 3)
        winner = store_a.put_checkpoint(
            work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=3
        )
        manifest, blobs = _tiny_checkpoint(WORK_ID, {"v2.txt": b"two\n"}, 2)
        late = store_b.put_checkpoint(
            work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=2
        )

        assert winner["latest"] is True
        assert late["latest"] is False  # superseded history, never a demotion
        assert store_a.entry(WORK_ID)["sequence"] == 3
        assert len(store_a.list_entries()) == 2  # both records preserved

    def test_a_writer_that_cannot_take_the_lock_is_superseded_and_writes_nothing(self, tmp_path):
        store_a, store_b, root = self._two_stores(tmp_path)
        manifest, blobs = _tiny_checkpoint(WORK_ID, {"first.txt": b"one\n"}, 1)
        winner = store_a.put_checkpoint(
            work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=1
        )
        before = {item: item.stat().st_mtime for item in root.rglob("*") if item.is_file()}
        assert winner["latest"] is True

        # A second WRITER (its own flock descriptor, as another process
        # would hold) keeps the critical section; the loser's wait budget
        # is one retry tick.
        lock_fd = os.open(root / "works" / f"{WORK_ID}.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            manifest, blobs = _tiny_checkpoint(WORK_ID, {"second.txt": b"two\n"}, 2)
            verdict = store_b.put_checkpoint(
                work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=2
            )
        finally:
            os.close(lock_fd)

        assert verdict["superseded"] is True
        assert verdict["latest"] is False
        assert "superseded_reason" in verdict and "index lock" in verdict["superseded_reason"]
        after = {item: item.stat().st_mtime for item in root.rglob("*") if item.is_file()}
        assert after == before  # nothing written: no blobs, no index change
        assert store_b.entry(WORK_ID)["sequence"] == 1  # the winner's index stands

    def test_the_lock_exhaustion_names_the_recorded_holder(self, tmp_path):
        _store_a, store_b, root = self._two_stores(tmp_path)
        lock_path = root / "works" / f"{WORK_ID}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("host-a|4242|2026-09-23T00:00:00+00:00", encoding="utf-8")

        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with pytest.raises(api_channel.IndexLockHeldError, match=r"host-a\|4242"):
                with store_b._index_lock(WORK_ID, wait_seconds=0.01):
                    pass  # pragma: no cover — never reached
        finally:
            os.close(lock_fd)

    def test_the_bounded_wait_lets_a_short_critical_section_finish(self, tmp_path):
        """A live holder keeps the lock only for its short
        read-modify-write: the retry-with-jitter budget rides it out and
        the second write LANDS (as history or active, by sequence)."""
        store_a, store_b, _root = self._two_stores(tmp_path)
        manifest, blobs = _tiny_checkpoint(WORK_ID, {"v1.txt": b"one\n"}, 1)
        store_a.put_checkpoint(work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=1)

        def hold_briefly() -> None:
            with store_a._index_lock(WORK_ID):
                time.sleep(0.2)

        thread = threading.Thread(target=hold_briefly)
        thread.start()
        try:
            manifest, blobs = _tiny_checkpoint(WORK_ID, {"v2.txt": b"two\n"}, 2)
            verdict = store_b.put_checkpoint(
                work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=2
            )
        finally:
            thread.join()

        assert verdict.get("superseded") is not True
        assert verdict["latest"] is True
        assert store_b.entry(WORK_ID)["sequence"] == 2

    @pytest.mark.skipif(fcntl is None, reason="POSIX flock required")
    def test_a_real_other_process_holding_the_lock_wins(self, tmp_path, monkeypatch):
        """The declared topology proof: a genuine OS PROCESS holds the
        exclusive flock; the store's bounded retry loses honestly."""
        monkeypatch.setenv(api_channel.LOCK_WAIT_SECONDS_ENV, "0.05")
        store_a, store_b, root = self._two_stores(tmp_path)
        manifest, blobs = _tiny_checkpoint(WORK_ID, {"first.txt": b"one\n"}, 1)
        store_a.put_checkpoint(work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=1)

        lock_path = root / "works" / f"{WORK_ID}.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                HOLD_LOCK_SNIPPET,
                str(lock_path),
            ],
            stdout=subprocess.PIPE,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == b"held"
            manifest, blobs = _tiny_checkpoint(WORK_ID, {"second.txt": b"two\n"}, 2)
            verdict = store_b.put_checkpoint(
                work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=2
            )
        finally:
            holder.kill()
            holder.wait()

        assert verdict["superseded"] is True
        assert store_b.entry(WORK_ID)["sequence"] == 1


class TestRetentionDecisions:
    """NEXT-06: the retention DECISION is durable — a re-run over the same
    state re-deletes nothing, and a GC interrupted between the index
    update and the unlink is completed by the next pass."""

    def _store_with_history(self, tmp_path: Path, count: int = 3):
        store = api_channel.CheckpointStore(tmp_path / "cas")
        for sequence in range(1, count + 1):
            manifest, blobs = _tiny_checkpoint(
                WORK_ID, {f"v{sequence}.txt": f"content {sequence}\n".encode()}, sequence
            )
            store.put_checkpoint(
                work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=sequence
            )
        return store

    def _index_document(self, store, work_id: str = WORK_ID) -> dict:
        return json.loads((store._root / "works" / f"{work_id}.json").read_text())

    def test_the_decision_is_recorded_in_the_index(self, tmp_path):
        store = self._store_with_history(tmp_path)

        removed = store.apply_retention(WORK_ID, keep_last=1)

        assert removed == 2
        decision = self._index_document(store)["retention"]
        assert decision["keep"] == 1
        assert decision["removed"] == 2
        assert decision["pending_gc"] == []
        assert (
            decision["kept_tail"] == self._index_document(store)["checkpoints"][-1]["checkpoint_id"]
        )
        assert "|" in decision["holder"]  # hostname|pid

    def test_a_rerun_over_the_same_state_re_deletes_nothing(self, tmp_path):
        store = self._store_with_history(tmp_path)
        store.apply_retention(WORK_ID, keep_last=1)
        retained_id = store.entry(WORK_ID)["checkpoint_id"]
        blob_files = [item for item in (store._root / "works").parent.rglob("*") if item.is_file()]
        index_file = store._root / "works" / f"{WORK_ID}.json"
        snapshot = {item: item.stat().st_mtime for item in blob_files + [index_file]}

        removed_again = store.apply_retention(WORK_ID, keep_last=1)

        assert removed_again == 0
        after = {item: item.stat().st_mtime for item in blob_files + [index_file]}
        assert after == snapshot  # the recorded decision short-circuited the pass
        assert store.entry(WORK_ID)["checkpoint_id"] == retained_id

    def test_a_crashed_gc_is_completed_by_the_next_pass(self, tmp_path, monkeypatch):
        store = self._store_with_history(tmp_path)
        original_unlink = Path.unlink

        def crashing_unlink(self: Path, missing_ok: bool = False) -> None:
            raise OSError("crash between index update and blob cleanup")

        monkeypatch.setattr(Path, "unlink", crashing_unlink)
        with pytest.raises(OSError, match="crash between index update"):
            store.apply_retention(WORK_ID, keep_last=1)
        monkeypatch.setattr(Path, "unlink", original_unlink)

        # The crash left the decision recorded with a PENDING GC.
        decision = self._index_document(store)["retention"]
        assert decision["pending_gc"], "the interrupted pass recorded its pending GC"

        removed = store.apply_retention(WORK_ID, keep_last=1)

        assert removed == 0  # the entries were already dropped; only GC ran
        assert self._index_document(store)["retention"]["pending_gc"] == []
        listed = store.list_entries()
        assert [entry["sequence"] for entry in listed] == [3]  # active survives
        for digest in decision["pending_gc"]:
            assert not (store._root / digest[:2] / digest).exists()  # GC completed
