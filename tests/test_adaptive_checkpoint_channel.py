"""Retained cross-runner lessons on the sole authenticated normalized checkpoint API.

Legacy HMAC/grace windows, filesystem indexes, optional durability modes,
operator list/health endpoints and automatic retention are retired. A current
session grants only exact normalized artifacts; resume must name one retained
prior-generation manifest, never a mutable latest pointer or remote-ref URI.
"""

import asyncio
import json
from hashlib import sha256

from fastapi import FastAPI
import httpx
from pydantic import ValidationError
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.api.lane_settings import LaneAuthSettings
from forge.api.lane_v1 import router
from forge.api.lane_v1_blobs import BlobError, LaneBlobStore
from forge.api.lane_v1_service import LaneV1Service
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.models_lane import EngineBlob, EngineLaneSession
from forge.store.models_runs import EngineRun
from forge_lane.checkpoint import Checkpoints
from forge_lane.client import LaneClient
from forge_lane.errors import LaneFailure
from forge_lane.workspace import Workspace
from forge_protocol import CheckpointManifest
from tests.native_checkpoint_fixture import advance, entry, real_http, saved, seed
from tests.native_lane_fixture import (
    api as api,
    auth as auth,
    settings as settings,
    signing_key as signing_key,
    checkpoint,
    exchange,
    oidc,
    upload,
)
from tests.test_lane_runner_v1 import checkout, git


async def count(factory, model):
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_capture_upload_download_restore_on_a_second_runner(auth, signing_key, tmp_path):
    source, target = tmp_path / "first", tmp_path / "second"
    base = checkout(source)
    git(tmp_path, "clone", "-q", "--no-hardlinks", str(source), str(target))
    (source / "base.txt").write_bytes(b"modified\x00exact\r\n")
    (source / "delete.txt").unlink()
    (source / "new.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (source / "new.sh").chmod(0o755)
    async with real_http(auth, signing_key, tmp_path, base) as api:
        lane, factory, pending, client = api
        manifest = await Checkpoints(
            pending.dispatch, LaneClient(pending.dispatch, client), Workspace(source, base)
        ).capture()
        assert {e.state for e in manifest.entries} == {"modified", "new", "deleted"}
        assert (await saved(factory))[0].id == manifest.checkpoint_id
        new = await advance(api, manifest.checkpoint_id, signing_key)
        restarted = LaneV1Service(
            lane.auth, EngineLaneV1Store(factory), LaneBlobStore(lane.blobs.root)
        )
        client._transport.app.state.lane_v1_service = restarted
        note, _ = await Checkpoints(
            new.dispatch, LaneClient(new.dispatch, client), Workspace(target, base)
        ).restore()
        assert (target / "base.txt").read_bytes() == b"modified\x00exact\r\n"
        assert not (target / "delete.txt").exists()
        assert (target / "new.sh").stat().st_mode & 0o111 and "deleted: delete.txt" in note


async def test_missing_checkpoint_is_honest_404_without_a_filesystem_index(api):
    assert (await api[3].get("/lane/v1/checkpoints/latest")).status_code == 404
    assert await saved(api[1]) == ()


async def test_reupload_is_durable_idempotent_and_normalized_after_restart(api):
    manifest = await seed(api)
    lane, factory, pending, client = api
    response = await client.put(
        "/lane/v1/checkpoints/" + manifest.checkpoint_id, json=manifest.model_dump(mode="json")
    )
    assert response.status_code == 200 and len(await saved(factory)) == 1
    restarted = LaneV1Service(lane.auth, EngineLaneV1Store(factory), LaneBlobStore(lane.blobs.root))
    assert await restarted.latest_checkpoint(pending.identity) == manifest
    async with factory() as session:
        row = await session.scalar(select(EngineLaneSession))
        assert "checkpoints" not in json.loads(row.api_state_json)


@pytest.mark.parametrize("corruption", [b"flipped", b"", b"approved byteS"])
async def test_rotted_content_is_never_adopted_or_returned(api, corruption):
    manifest = await seed(api)
    lane, _, pending, client = api
    key = manifest.entries[0].sha256
    (lane.blobs.root / key).write_bytes(corruption)
    assert (await client.get("/lane/v1/blobs/" + key)).status_code == 409
    with pytest.raises(BlobError):
        await lane.blobs.read(key)
    with pytest.raises(LaneFailure):
        await LaneClient(pending.dispatch, client).blob(key, limit=1024)


@pytest.mark.parametrize(
    "field,value",
    [
        ("forge_run_id", "foreign"),
        ("attempt", 3),
        ("generation", 8),
        ("base_sha", "b" * 40),
        ("approved_digest", "0" * 64),
        ("driver", "copilot_cli"),
    ],
)
async def test_manifest_authority_must_match_the_authenticated_dispatch(api, field, value):
    key = await upload(api[3])
    manifest = checkpoint(api[2], key).model_copy(update={field: value})
    response = await api[3].put(
        "/lane/v1/checkpoints/" + manifest.checkpoint_id, json=manifest.model_dump(mode="json")
    )
    assert response.status_code == 409 and await saved(api[1]) == ()


@pytest.mark.parametrize(
    "path", ["../outside", "/tmp/outside", "a/../b", ".git/config", ".forge/x", "a\\b"]
)
async def test_path_shaped_or_reserved_artifact_keys_refuse_before_write(api, path):
    pending, client = api[2], api[3]
    document = checkpoint(pending, "a" * 64).model_dump(mode="json")
    document["entries"][0]["path"] = path
    response = await client.put("/lane/v1/checkpoints/" + "a" * 64, json=document)
    assert response.status_code == 422 and await saved(api[1]) == ()
    assert await count(api[1], EngineBlob) == 0


@pytest.mark.parametrize("change", ["digest", "size", "missing", "extra", "duplicate", "unsorted"])
async def test_manifest_closure_and_canonical_identity_are_exact(api, change):
    lane, factory, pending, client = api
    key = await upload(client)
    manifest = checkpoint(pending, key)
    document = manifest.model_dump(mode="json")
    address = manifest.checkpoint_id
    if change == "digest":
        address = "0" * 64
    elif change == "size":
        document["entries"][0]["size_bytes"] = 1
    elif change == "missing":
        document["entries"][0]["sha256"] = "f" * 64
    elif change == "extra":
        document["blobs"] = {"../outside": "untrusted"}
    elif change == "duplicate":
        document["entries"] *= 2
    else:
        document["entries"] += [{**document["entries"][0], "path": "a.txt"}]
    response = await client.put("/lane/v1/checkpoints/" + address, json=document)
    assert response.status_code in (409, 422, 404) and await saved(factory) == ()
    assert await lane.blobs.read(key) == b"approved bytes"


@pytest.mark.parametrize(
    "header", [None, "Basic secret", "Bearer legacy-hmac", "Bearer bad.jwt.token"]
)
async def test_old_missing_or_foreign_bearers_are_refused_everywhere(api, header):
    client = api[3]
    headers = {} if header is None else {"Authorization": header}
    for method, target, kwargs in [
        ("GET", "/lane/v1/checkpoints/latest", {}),
        ("GET", "/lane/v1/blobs/" + "a" * 64, {}),
        ("PUT", "/lane/v1/blobs/" + "a" * 64, {"content": b"x"}),
    ]:
        request = client.build_request(method, target, **kwargs)
        request.headers.pop("Authorization", None)
        request.headers.update(headers)
        assert (await client.send(request)).status_code == 401
    assert await saved(api[1]) == () and await count(api[1], EngineBlob) == 0


async def test_session_from_another_run_cannot_read_known_checkpoint_blobs(api, auth, signing_key):
    manifest = await seed(api)
    other = await auth[2](run_id="b" * 32)
    bearer = await exchange(
        api[0].auth, other, oidc(signing_key, other, changes={"jti": "foreign"})
    )
    headers = {"Authorization": "Bearer " + bearer.access_token}
    assert (await api[3].get("/lane/v1/checkpoints/latest", headers=headers)).status_code == 404
    assert (
        await api[3].get("/lane/v1/blobs/" + manifest.entries[0].sha256, headers=headers)
    ).status_code == 404


async def test_superseded_generation_session_is_fenced_without_a_grace_window(api, signing_key):
    manifest = await seed(api)
    old = api[3].headers["Authorization"]
    await advance(api, manifest.checkpoint_id, signing_key)
    for target in ("/lane/v1/checkpoints/latest", "/lane/v1/blobs/" + manifest.entries[0].sha256):
        assert (await api[3].get(target, headers={"Authorization": old})).status_code == 401
    assert (await api[3].get("/lane/v1/checkpoints/latest")).status_code == 200


@pytest.mark.parametrize("authority", ["attempt", "generation", "cancellation", "terminal"])
async def test_current_run_authority_is_rechecked_for_every_artifact_operation(api, authority):
    manifest = await seed(api)
    async with api[1].begin() as session:
        run = await session.get(EngineRun, api[2].identity.forge_run_id)
        if authority == "attempt":
            run.attempt += 1
        elif authority == "generation":
            run.generation += 1
        elif authority == "cancellation":
            run.cancel_requested = True
        else:
            run.state = "cancelled"
    assert (await api[3].get("/lane/v1/checkpoints/latest")).status_code == 401
    assert (await api[3].get("/lane/v1/blobs/" + manifest.entries[0].sha256)).status_code == 401


async def test_unconfigured_router_refuses_instead_of_using_an_old_secret_or_store():
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://forge.example"
    ) as client:
        response = await client.get(
            "/lane/v1/checkpoints/latest", headers={"Authorization": "Bearer old-secret"}
        )
        assert response.status_code == 503


@pytest.mark.parametrize(
    "field",
    [
        "lane_control_secret",
        "legacy_deadline",
        "durability_mode",
        "checkpoint_store_dir",
        "retention_keep",
    ],
)
def test_retired_storage_modes_and_secret_grace_configuration_are_not_accepted(settings, field):
    document = settings.model_dump(mode="json")
    document[field] = "synthetic-old-value"
    with pytest.raises(ValidationError):
        LaneAuthSettings.model_validate_json(json.dumps(document))


async def test_exact_older_checkpoint_is_served_even_after_a_newer_capture(api, signing_key):
    old = await seed(api, body=b"older pinned", sequence=1)
    latest = await seed(api, body=b"newer active", sequence=9)
    await advance(api, old.checkpoint_id, signing_key)
    assert (await api[3].get("/lane/v1/checkpoints/latest")).json() == old.model_dump(mode="json")
    assert (await api[3].get("/lane/v1/blobs/" + old.entries[0].sha256)).content == b"older pinned"
    assert (await api[3].get("/lane/v1/blobs/" + latest.entries[0].sha256)).status_code == 404
    assert {row.id for row in await saved(api[1])} == {old.checkpoint_id, latest.checkpoint_id}


@pytest.mark.parametrize("key", [None, "0" * 64])
async def test_missing_required_or_unselected_resume_never_falls_back_to_latest(
    api, signing_key, key
):
    await seed(api)
    await advance(api, key, signing_key)
    assert (await api[3].get("/lane/v1/checkpoints/latest")).status_code == 404
    assert len(await saved(api[1])) == 1


async def test_late_lower_sequence_cannot_demote_committed_checkpoint(api):
    latest = await seed(api, sequence=9)
    key = await upload(api[3], b"late lower")
    late = checkpoint(api[2], key, len(b"late lower"), sequence=2)
    response = await api[3].put(
        "/lane/v1/checkpoints/" + late.checkpoint_id, json=late.model_dump(mode="json")
    )
    assert response.status_code == 409
    assert (await api[3].get("/lane/v1/checkpoints/latest")).json() == latest.model_dump(
        mode="json"
    )
    assert len(await saved(api[1])) == 1


async def test_independent_store_instances_race_one_idempotent_database_receipt(api):
    lane, factory, pending, client = api
    key = await upload(client)
    manifest = checkpoint(pending, key)
    engine = create_async_engine(factory.kw["bind"].url)
    other = async_sessionmaker(engine, expire_on_commit=False)
    instances = [
        LaneV1Service(lane.auth, EngineLaneV1Store(owner), lane.blobs)
        for owner in (factory, other, factory, other)
    ]
    try:
        results = await asyncio.gather(
            *(
                item.checkpoint(pending.identity, manifest.checkpoint_id, manifest)
                for item in instances
            )
        )
        assert sorted(results) == [False, False, False, True]
        assert len(await saved(other)) == 1
    finally:
        await engine.dispose()


async def test_checkpoint_transaction_failure_never_exposes_a_phantom_receipt(api, monkeypatch):
    import forge.store.lane_state as state

    original = state.persist_checkpoints

    async def fail(*args):
        await original(*args)
        raise RuntimeError("injected checkpoint commit failure")

    key = await upload(api[3])
    manifest = checkpoint(api[2], key)
    with monkeypatch.context() as scoped:
        scoped.setattr(state, "persist_checkpoints", fail)
        with pytest.raises(RuntimeError, match="injected"):
            await api[0].checkpoint(api[2].identity, manifest.checkpoint_id, manifest)
    assert await saved(api[1]) == ()
    assert (await api[3].get("/lane/v1/checkpoints/latest")).status_code == 404
    assert (await api[3].get("/lane/v1/blobs/" + key)).content == b"approved bytes"


async def test_metadata_outage_is_unavailable_even_when_blob_bytes_exist(api, monkeypatch):
    manifest = await seed(api)
    import forge.store.lane_v1 as store

    async def unavailable(*args):
        raise OperationalError("owned unavailable", {}, None)

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "locked_current", unavailable)
        with pytest.raises(OperationalError):
            await api[0].latest_checkpoint(api[2].identity)
    assert await api[0].blobs.read(manifest.entries[0].sha256) == b"approved bytes"


async def test_cancel_after_blob_read_refuses_manifest_commit(api, monkeypatch):
    lane, factory, pending, client = api
    key = await upload(client)
    original = lane.read_blob

    async def cancel(*args):
        body = await original(*args)
        async with factory.begin() as session:
            run = await session.get(EngineRun, pending.identity.forge_run_id)
            run.cancel_requested = True
        return body

    monkeypatch.setattr(lane, "read_blob", cancel)
    manifest = checkpoint(pending, key)
    assert (
        await client.put(
            "/lane/v1/checkpoints/" + manifest.checkpoint_id, json=manifest.model_dump(mode="json")
        )
    ).status_code == 401
    assert await saved(factory) == ()


@pytest.mark.parametrize("chunks", [[b"12345"], [b"12", b"345"], [b"", b"123", b"45"]])
async def test_streamed_blob_cap_leaves_no_visible_partial_file_or_authorization(api, chunks):
    lane, factory, _, client = api
    lane.blobs.max_bytes = 4

    async def body():
        for chunk in chunks:
            yield chunk

    key = sha256(b"12345").hexdigest()
    response = await client.put("/lane/v1/blobs/" + key, content=body())
    assert response.status_code == 413 and await count(factory, EngineBlob) == 0
    assert list(lane.blobs.root.iterdir()) == []


async def test_atomic_blob_link_failure_removes_staging_and_authorizes_nothing(api, monkeypatch):
    import forge.api.lane_v1_blobs as blobs

    def fail(*args, **kwargs):
        raise OSError("injected link failure")

    monkeypatch.setattr(blobs.os, "link", fail)
    key = sha256(b"bytes").hexdigest()
    with pytest.raises(OSError, match="injected"):
        await api[3].put("/lane/v1/blobs/" + key, content=b"bytes")
    assert list(api[0].blobs.root.iterdir()) == [] and await count(api[1], EngineBlob) == 0


@pytest.mark.parametrize("header", [None, "1", "2000000"])
async def test_json_stream_cap_bounds_missing_or_misleading_content_length(api, header):
    headers = {"Content-Type": "application/json"}
    if header:
        headers["Content-Length"] = header

    async def body():
        yield b'{"entries":['
        yield b" " * (1024 * 1024)
        yield b"]}"

    response = await api[3].put("/lane/v1/checkpoints/" + "a" * 64, content=body(), headers=headers)
    assert response.status_code == 413 and await saved(api[1]) == ()


async def test_manifest_entry_cap_refuses_before_blob_or_checkpoint_changes(api):
    document = checkpoint(api[2], "a" * 64).model_dump(mode="json")
    document["entries"] = [
        entry(f"file-{i:05}", state="deleted").model_dump(mode="json") for i in range(4097)
    ]
    manifest = CheckpointManifest.model_validate(document)
    response = await api[3].put("/lane/v1/checkpoints/" + manifest.checkpoint_id, json=document)
    assert response.status_code == 409 and await saved(api[1]) == ()
    assert await count(api[1], EngineBlob) == 0


async def test_checkpoint_count_cap_is_database_owned_and_existing_receipts_stay_idempotent(api):
    lane, factory, pending, client = api
    manifests = [
        checkpoint(pending, "a" * 64).model_copy(update={"entries": (), "sequence": i})
        for i in range(1, 257)
    ]

    def fill(p, state, namespace):
        namespace["checkpoints"] = {m.checkpoint_id: m.model_dump(mode="json") for m in manifests}
        state["latest_checkpoint"] = manifests[-1].checkpoint_id

    await lane.store.access(pending.identity, fill, write=True)
    assert (
        await lane.checkpoint(pending.identity, manifests[-1].checkpoint_id, manifests[-1]) is False
    )
    extra = manifests[-1].model_copy(update={"sequence": 257})
    response = await client.put(
        "/lane/v1/checkpoints/" + extra.checkpoint_id, json=extra.model_dump(mode="json")
    )
    assert response.status_code == 409 and len(await saved(factory)) == 256


@pytest.mark.parametrize(
    "axis,cap", [("entries", 1), ("blob_bytes", 4), ("total_bytes", 7), ("manifest_bytes", 32)]
)
async def test_lane_capture_caps_refuse_before_any_upload(auth, signing_key, tmp_path, axis, cap):
    from forge_lane.workspace import Limits

    source = tmp_path / "source"
    base = checkout(source)
    (source / "base.txt").write_bytes(b"changed")
    (source / "new.txt").write_bytes(b"new data")
    async with real_http(auth, signing_key, tmp_path, base) as api:
        workspace = Workspace(source, base, Limits(**{axis: cap}))
        capture = Checkpoints(api[2].dispatch, LaneClient(api[2].dispatch, api[3]), workspace)
        with pytest.raises(LaneFailure) as failure:
            await capture.capture()
        assert failure.value.classification == "checkpoint_too_large"
        assert capture.sequence == 0 and await saved(api[1]) == ()
        assert await count(api[1], EngineBlob) == 0 and list(api[0].blobs.root.iterdir()) == []


@pytest.mark.parametrize("tamper", ["blob", "manifest"])
async def test_lane_independently_refuses_inflight_tampering_before_restore_writes(
    auth, signing_key, tmp_path, tamper
):
    source, target = tmp_path / "source", tmp_path / "target"
    base = checkout(source)
    git(tmp_path, "clone", "-q", str(source), str(target))
    (source / "base.txt").write_bytes(b"approved wip")
    async with real_http(auth, signing_key, tmp_path, base) as api:
        manifest = await Checkpoints(
            api[2].dispatch, LaneClient(api[2].dispatch, api[3]), Workspace(source, base)
        ).capture()
        new = await advance(api, manifest.checkpoint_id, signing_key)

        class CorruptWire(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                response = await api[3]._transport.handle_async_request(request)
                raw = await response.aread()
                if tamper == "blob" and "/blobs/" in request.url.path:
                    raw = b"flipped in flight"
                elif tamper == "manifest" and request.url.path.endswith("/latest"):
                    document = json.loads(raw)
                    document["base_sha"] = "b" * 40
                    raw = json.dumps(document).encode()
                return httpx.Response(response.status_code, content=raw, request=request)

        async with httpx.AsyncClient(transport=CorruptWire(), headers=api[3].headers) as client:
            restore = Checkpoints(
                new.dispatch, LaneClient(new.dispatch, client), Workspace(target, base)
            )
            with pytest.raises(LaneFailure) as failure:
                await restore.restore()
        assert failure.value.classification in ("corrupt_blob", "checkpoint_mismatch")
        assert (target / "base.txt").read_bytes() == b"base\n"
        assert await api[0].read_blob(new.identity, manifest.entries[0].sha256) == b"approved wip"


async def test_valid_small_stream_and_json_payloads_preserve_exact_bytes(api):
    api[0].blobs.max_bytes = 14
    manifest = await seed(api)
    assert (await api[3].get("/lane/v1/checkpoints/latest")).json() == manifest.model_dump(
        mode="json"
    )
    assert (
        await api[3].get("/lane/v1/blobs/" + manifest.entries[0].sha256)
    ).content == b"approved bytes"
