"""Exact checkpoint properties over real Git trees and wire-verified blob bytes."""

import os
import random
import shutil
import stat
from hashlib import sha256

import httpx
import pytest
from forge_lane.checkpoint import Checkpoints
from forge_lane.client import LaneClient
from forge_lane.errors import LaneFailure
from forge_lane.workspace import Limits, Workspace
from forge_protocol import CheckpointEntry, CheckpointManifest
from tests.test_lane_runner_v1 import checkout, git, inputs


class Blobs:
    def __init__(self):
        self.data, self.puts, self.manifest = {}, [], None

    def handle(self, request):
        if "/blobs/" in request.url.path:
            digest = request.url.path.rsplit("/", 1)[1]
            if request.method == "PUT":
                assert sha256(request.content).hexdigest() == digest
                self.data[digest] = request.content
                self.puts.append(digest)
                return httpx.Response(204)
            return httpx.Response(200, content=self.data[digest])
        if request.method == "PUT":
            self.manifest = CheckpointManifest.model_validate_json(request.content)
            return httpx.Response(200, json=self.manifest.model_dump(mode="json"))
        return httpx.Response(200, json=self.manifest.model_dump(mode="json"))


def tree(root):
    result = {}
    for path in root.rglob("*"):
        if any(part in {".git", ".forge"} for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            result[path.relative_to(root).as_posix()] = ("120000", os.readlink(path).encode())
        elif path.is_file():
            mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
            result[path.relative_to(root).as_posix()] = (mode, path.read_bytes())
    return result


def base_tree(root):
    checkout(root)
    (root / "file_to_dir").write_bytes(b"file")
    for name in ("dir_to_file", "dir_to_link"):
        (root / name).mkdir()
        (root / name / "child").write_bytes(b"child")
    (root / "link_to_dir").symlink_to("base.txt")
    git(root, "add", ".")
    git(root, "commit", "-qm", "file type transition bases")
    return git(root, "rev-parse", "HEAD").decode().strip()


def changed_tree(root, seed):
    rng = random.Random(seed)
    (root / "base.txt").write_bytes(rng.randbytes(48))
    (root / "delete.txt").unlink()
    for name in ("file_to_dir", "link_to_dir"):
        (root / name).unlink()
        (root / name).mkdir()
        (root / name / "new").write_bytes(rng.randbytes(32))
    for name in ("dir_to_file", "dir_to_link"):
        shutil.rmtree(root / name)
    (root / "dir_to_file").write_bytes(rng.randbytes(24))
    (root / "dir_to_link").symlink_to("base.txt")
    shared = rng.randbytes(40)
    for name in ("new-a.bin", "new-b.bin"):
        (root / name).write_bytes(shared)
    (root / "executable").write_text("#!/bin/sh\nexit 0\n")
    (root / "executable").chmod(0o755)
    (root / ".forge").mkdir()
    (root / ".forge" / "private-lane-state").write_bytes(b"must not be checkpointed")


@pytest.mark.parametrize("seed", range(8))
async def test_random_tree_round_trip_preserves_binary_modes_links_and_all_three_states(
    tmp_path, seed
):
    source, target = tmp_path / "source", tmp_path / "restored"
    base = base_tree(source)
    git(tmp_path, "clone", "-q", "--no-hardlinks", str(source), str(target))
    changed_tree(source, seed)
    expected = tree(source)
    dispatch, _ = inputs(base)
    store = Blobs()
    async with httpx.AsyncClient(transport=httpx.MockTransport(store.handle)) as http:
        original = Checkpoints(dispatch, LaneClient(dispatch, http), Workspace(source, base))
        manifest = await original.capture()
        assert {entry.state for entry in manifest.entries} == {"new", "modified", "deleted"}
        resumed, _ = inputs(base, generation=1, checkpoint_id=manifest.checkpoint_id)
        workspace = Workspace(target, base)
        await workspace.validate()
        note, _ = await Checkpoints(resumed, LaneClient(resumed, http), workspace).restore()
    assert tree(target) == expected
    assert "deleted: delete.txt" in note
    assert not (target / ".forge").exists()


async def test_unchanged_blobs_are_uploaded_once_per_generation(tmp_path):
    base = checkout(tmp_path / "source")
    (tmp_path / "source/base.txt").write_bytes(b"same content")
    (tmp_path / "source/new.txt").write_bytes(b"same content")
    dispatch, _ = inputs(base)
    store = Blobs()
    async with httpx.AsyncClient(transport=httpx.MockTransport(store.handle)) as http:
        checkpoints = Checkpoints(
            dispatch, LaneClient(dispatch, http), Workspace(tmp_path / "source", base)
        )
        first = await checkpoints.capture()
        second = await checkpoints.capture()
    assert first.sequence == 1 and second.sequence == 2
    assert len(store.puts) == 1


async def test_manifest_limit_refuses_before_any_blob_upload(tmp_path):
    base = checkout(tmp_path / "source")
    (tmp_path / "source/base.txt").write_bytes(b"modified")
    dispatch, _ = inputs(base)
    store = Blobs()
    async with httpx.AsyncClient(transport=httpx.MockTransport(store.handle)) as http:
        checkpoints = Checkpoints(
            dispatch,
            LaneClient(dispatch, http),
            Workspace(tmp_path / "source", base, Limits(manifest_bytes=32)),
        )
        with pytest.raises(LaneFailure, match="manifest"):
            await checkpoints.capture()
    assert store.puts == [] and store.manifest is None and checkpoints.sequence == 0


async def refuse_before_write(tmp_path, *, symlink_blob=None):
    target = tmp_path / "target"
    base = checkout(target)
    if symlink_blob is None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (target / "escape").symlink_to(outside, target_is_directory=True)
    dispatch, _ = inputs(base)
    data = b"new bytes"
    blob = sha256(data).hexdigest()
    entries = [
        CheckpointEntry(
            path="base.txt", mode="100644", sha256=blob, state="modified", size_bytes=len(data)
        )
    ]
    store = Blobs()
    store.data[blob] = data
    if symlink_blob is None:
        entries.append(
            CheckpointEntry(
                path="escape/out.txt", mode="100644", sha256=blob, state="new", size_bytes=len(data)
            )
        )
    else:
        key = sha256(symlink_blob).hexdigest()
        store.data[key] = symlink_blob
        entries.append(
            CheckpointEntry(
                path="unsafe-link",
                mode="120000",
                sha256=key,
                state="new",
                size_bytes=len(symlink_blob),
            )
        )
    store.manifest = CheckpointManifest(
        **{name: getattr(dispatch, name) for name in ("forge_run_id", "attempt", "generation")},
        base_sha=base,
        approved_digest=dispatch.approved_digest,
        sequence=1,
        entries=tuple(entries),
        driver=dispatch.driver,
    )
    resumed, _ = inputs(base, generation=1, checkpoint_id=store.manifest.checkpoint_id)
    async with httpx.AsyncClient(transport=httpx.MockTransport(store.handle)) as http:
        checkpoints = Checkpoints(resumed, LaneClient(resumed, http), Workspace(target, base))
        with pytest.raises(LaneFailure) as caught:
            await checkpoints.restore()
    assert caught.value.classification == "unsafe_checkpoint"
    assert (target / "base.txt").read_bytes() == b"base\n"
    assert not (tmp_path / "outside/out.txt").exists()


async def test_a_surviving_symlink_cannot_redirect_restore_writes(tmp_path):
    await refuse_before_write(tmp_path)


async def test_invalid_symlink_blob_is_typed_and_refused_before_any_write(tmp_path):
    await refuse_before_write(tmp_path, symlink_blob=b"\xff\xfe")


async def test_a_checkpoint_read_timeout_writes_nothing_and_does_not_fetch_another(tmp_path):
    target = tmp_path / "target"
    base = checkout(target)
    before = tree(target)
    seen = []

    def handle(request):
        seen.append(request.url.path)
        raise httpx.ReadTimeout("synthetic checkpoint timeout", request=request)

    dispatch, _ = inputs(base)
    resumed, _ = inputs(base, generation=1, checkpoint_id=digest_pin(dispatch))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(LaneFailure) as caught:
            await Checkpoints(resumed, LaneClient(resumed, http), Workspace(target, base)).restore()
    assert caught.value.classification == "transport_error"
    assert seen == ["/lane/v1/checkpoints/latest"]
    assert tree(target) == before


async def test_a_newer_checkpoint_is_refused_before_any_workspace_write(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    base = checkout(source)
    git(tmp_path, "clone", "-q", "--no-hardlinks", str(source), str(target))
    (source / "base.txt").write_bytes(b"pinned generation")
    dispatch, _ = inputs(base)
    store = Blobs()
    async with httpx.AsyncClient(transport=httpx.MockTransport(store.handle)) as http:
        pinned = await Checkpoints(
            dispatch, LaneClient(dispatch, http), Workspace(source, base)
        ).capture()
    newer = pinned.model_copy(update={"generation": 1, "sequence": pinned.sequence + 1})
    assert newer.checkpoint_id != pinned.checkpoint_id
    store.manifest = newer
    before = tree(target)
    resumed, _ = inputs(base, generation=1, checkpoint_id=pinned.checkpoint_id)
    async with httpx.AsyncClient(transport=httpx.MockTransport(store.handle)) as http:
        with pytest.raises(LaneFailure) as caught:
            await Checkpoints(resumed, LaneClient(resumed, http), Workspace(target, base)).restore()
    assert caught.value.classification == "checkpoint_mismatch"
    assert tree(target) == before


def digest_pin(dispatch):
    from forge_protocol import digest

    return digest({"pin": dispatch.approved_digest})
