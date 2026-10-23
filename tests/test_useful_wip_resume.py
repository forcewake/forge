"""Useful WIP preserves exact files and explicit deletions across repository reopen.

The live qualification driver is retired. These assertions exercise the
checkpoint bytes the resume path consumes, without grading a scripted agent.
"""

import hashlib
import json
import pytest

from forge.adaptive.checkpoint_repository import FilesystemCheckpointRepository
from forge.api_checkpoint_channel import CheckpointCorruptError
from tests.test_checkpoint_repository import _checkpoint


async def test_new_modified_executable_and_deleted_paths_round_trip_exactly(tmp_path):
    manifest, blobs, _ = _checkpoint(
        "work", {"new.txt": b"new", "bin/run": b"#!/bin/sh\necho ok\n"}, 2
    )
    document = json.loads(manifest)
    document["files"]["bin/run"].update(mode=0o755, role="modified")
    document["deletions"] = ["obsolete.txt"]
    manifest = json.dumps(document).encode()
    identity = hashlib.sha256(manifest).hexdigest()
    await FilesystemCheckpointRepository(tmp_path).put("work", identity, manifest, blobs)
    reopened = FilesystemCheckpointRepository(tmp_path)
    entry = await reopened.entry("work", identity)
    recovered, recovered_blobs = await reopened.read_entry(entry)
    assert recovered == manifest
    assert recovered_blobs == blobs
    assert json.loads(recovered)["files"]["bin/run"]["mode"] == 0o755
    assert json.loads(recovered)["deletions"] == ["obsolete.txt"]


async def test_forged_blob_digest_cannot_be_admitted_as_useful_wip(tmp_path):
    manifest, blobs, identity = _checkpoint("work", {"new.txt": b"approved"}, 1)
    blobs[next(iter(blobs))] = b"different"
    with pytest.raises((CheckpointCorruptError, ValueError)):
        await FilesystemCheckpointRepository(tmp_path).put("work", identity, manifest, blobs)
    assert await FilesystemCheckpointRepository(tmp_path).entry("work") is None


async def test_exact_resume_pin_is_older_than_active_and_keeps_its_base_identity(tmp_path):
    repository = FilesystemCheckpointRepository(tmp_path)
    old_manifest, old_blobs, old = _checkpoint(
        "work", {"file": b"approved"}, 1, source_oid="a" * 40
    )
    await repository.put("work", old, old_manifest, old_blobs)
    await repository.pin("work", old, "resume:approved-generation")
    newer, blobs, new = _checkpoint("work", {"file": b"unapproved"}, 2, source_oid="b" * 40)
    await repository.put("work", new, newer, blobs)
    await repository.apply_retention("work", keep_last=1)
    entry = await repository.entry("work", old)
    recovered, _ = await repository.read_entry(entry)
    assert json.loads(recovered)["source_oids"]["attempt_base"] == "a" * 40
    assert (await repository.entry("work"))["checkpoint_id"] == new
    assert entry["checkpoint_id"] != new
