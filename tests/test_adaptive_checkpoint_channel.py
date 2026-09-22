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
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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

    R28-05: the resume command's explicit reference decides WHICH
    checkpoint downloads; the fallback to the active one is recorded in
    the report, never silent."""

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

    def test_the_resume_reference_downloads_that_exact_checkpoint(
        self, httpx_mock, lane_cwd, captures
    ):
        from forge.lane_driver import _maybe_restore_wip

        tree, store, newer, older = captures
        httpx_mock.add_response(
            url=f"{CP}/lane/controls?work_id={WORK_ID}&after_sequence=0",
            json={
                "work_id": WORK_ID,
                "commands": [
                    _resume_command(
                        5,
                        {
                            "checkpoint_ref": format_checkpoint_ref(WORK_ID, older.artifact_id),
                            "run_id": WORK_ID,
                        },
                    )
                ],
            },
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
        httpx_mock.add_response(
            url=f"{CP}/lane/controls?work_id={WORK_ID}&after_sequence=0",
            json={
                "work_id": WORK_ID,
                "commands": [_resume_command(6, {"checkpoint_id": older.artifact_id})],
            },
        )
        self._serve_checkpoint(httpx_mock, store, older.artifact_id, 10)

        report = _maybe_restore_wip(WORK_ID)

        assert report["checkpoint_selection"] == "exact"
        assert report["checkpoint_ref"] == format_checkpoint_ref(WORK_ID, older.artifact_id)

    def test_a_resume_without_a_reference_falls_back_and_records_it(
        self, httpx_mock, lane_cwd, captures
    ):
        from forge.lane_driver import _maybe_restore_wip

        _, store, newer, older = captures
        httpx_mock.add_response(
            url=f"{CP}/lane/controls?work_id={WORK_ID}&after_sequence=0",
            json={"work_id": WORK_ID, "commands": [_resume_command(7, {})]},
        )
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
        httpx_mock.add_response(
            url=f"{CP}/lane/controls?work_id={WORK_ID}&after_sequence=0",
            json={"work_id": WORK_ID, "commands": []},
        )
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

        httpx_mock.add_response(
            url=f"{CP}/lane/controls?work_id={WORK_ID}&after_sequence=0",
            json={
                "work_id": WORK_ID,
                "commands": [
                    _resume_command(
                        8, {"checkpoint_ref": format_checkpoint_ref("wp-someone-else", "a" * 64)}
                    )
                ],
            },
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

        httpx_mock.add_response(
            url=f"{CP}/lane/controls?work_id={WORK_ID}&after_sequence=0",
            json={"work_id": WORK_ID, "commands": [_resume_command(9, {"checkpoint_ref": "junk"})]},
        )

        report = _maybe_restore_wip(WORK_ID)

        assert report["checkpoint_selection"] == "refused"
        assert report["restored"] is False
