"""Retained capture/restore lessons on exact protocol WIP and fail-stop execution.

Fresh checkouts replace sibling-generation pointers, backup renames, savepoint
fallbacks and filesystem GC grants. Every manifest/blob is verified before any
restore write. An apply failure refuses the model; a dirty partial checkout
cannot be reused. Only an authorized exact retained checkpoint can resume.
"""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from forge_protocol import CheckpointManifest

from forge.store.models_lane import EngineCheckpoint
from forge.store.models_runs import EngineRun
from forge.store.pause import PublicationPaused, require_unpaused
from forge_lane.checkpoint import Checkpoints
from forge_lane.client import LaneClient
from forge_lane.errors import ExitCode, LaneFailure
from forge_lane.workspace import Workspace
from tests.native_checkpoint_fixture import capture_setup, entry, restore_setup, run_restore
from tests.test_engine_commands import commands as commands, envelope
from tests.test_engine_lane_handlers import case as case, env as env, settings as settings, started
from tests.test_engine_operator import operator as operator, paused
from tests.test_lane_runner_v1 import FakeDriver, WireServer, checkout, execute, git, inputs


class TestCaptureTransaction:
    async def test_capture_uploads_blobs_and_commits_a_verified_manifest(self, tmp_path):
        def edit(root):
            (root / "base.txt").write_bytes(b"modified\x00exact\r\n")
            (root / "delete.txt").unlink()
            (root / "new.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
            (root / "new.sh").chmod(0o755)

        async with capture_setup(tmp_path, edit) as case:
            manifest = await case.checkpoints.capture()
            assert (
                manifest.base_sha == case.base
                and manifest.approved_digest == case.brief.approved_digest
            )
            assert {item.state for item in manifest.entries} == {"new", "modified", "deleted"}
            assert next(item for item in manifest.entries if item.path == "new.sh").mode == "100755"
            assert case.server.checkpoints == [manifest.model_dump(mode="json")]
            for item in manifest.entries:
                if item.sha256:
                    assert sha256(case.server.blobs[item.sha256]).hexdigest() == item.sha256

    async def test_unchanged_baseline_files_are_not_re_uploaded(self, tmp_path):
        async with capture_setup(
            tmp_path, lambda root: (root / "new.txt").write_bytes(b"new")
        ) as case:
            manifest = await case.checkpoints.capture()
            assert [item.path for item in manifest.entries] == ["new.txt"]
            assert list(case.server.blobs.values()) == [b"new"]

    async def test_the_full_sequence_fence_interrupt_drain_capture_checkpoint(
        self, operator, tmp_path, monkeypatch
    ):
        run, _, _ = await started(operator)
        accepted = await operator["commands"].submit(envelope(operator, "pause"))
        assert accepted.reason == "control_queued"
        async with operator["env"].factory() as session:
            current = await session.get(EngineRun, run.id)
            with pytest.raises(PublicationPaused):
                await require_unpaused(session, current)
        root = tmp_path / "work"
        dispatch, brief = inputs(checkout(root))
        server, driver = WireServer(dispatch, brief), FakeDriver(wait=True)
        driver.edit = lambda path, env: (path / "base.txt").write_bytes(b"exact paused wip")
        original = Checkpoints.capture

        async def capture(checkpoints, *args):
            assert driver.interrupted == 1 and driver.session.stopped.is_set()
            return await original(checkpoints, *args)

        monkeypatch.setattr(Checkpoints, "capture", capture)
        server.command("pause")
        result = await execute(dispatch, server, driver, root)
        manifest = CheckpointManifest.model_validate(server.checkpoints[-1])
        assert (
            result.report.status == "paused"
            and result.report.checkpoint_id == manifest.checkpoint_id
        )
        assert server.acks[-1]["checkpoint_id"] == manifest.checkpoint_id
        calls = [path for method, path in server.calls if method in ("PUT", "POST")]
        assert (
            calls.index("checkpoints/" + manifest.checkpoint_id)
            < calls.index("controls/command-1/ack")
            < calls.index("report")
        )

    async def test_a_failed_upload_lands_paused_failed_not_captured(self, tmp_path, monkeypatch):
        root = tmp_path / "work"
        dispatch, brief = inputs(checkout(root))
        server, driver = WireServer(dispatch, brief), FakeDriver(wait=True)
        server.command("pause")

        async def refused(*args):
            raise LaneFailure(
                ExitCode.TRANSPORT, "server_refused", "owned injected checkpoint failure"
            )

        monkeypatch.setattr(LaneClient, "checkpoint", refused)
        result = await execute(dispatch, server, driver, root)
        assert result.report.status == "failed" and result.report.checkpoint_id is None
        assert not server.checkpoints and not any(ack["status"] == "applied" for ack in server.acks)
        assert driver.interrupted == 1

    async def test_a_symlink_in_the_wip_tree_refuses_capture(self, tmp_path):
        # Symlink entries are supported as bytes; capture never follows them outside.
        outside = tmp_path / "private"
        outside.write_bytes(b"must not be uploaded")
        async with capture_setup(
            tmp_path, lambda root: (root / "link").symlink_to(outside)
        ) as case:
            manifest = await case.checkpoints.capture()
            assert manifest.entries[0].mode == "120000"
            assert list(case.server.blobs.values()) == [str(outside).encode()]
            assert b"must not be uploaded" not in case.server.blobs.values()


class TestRestoreOnASecondRunner:
    async def test_destroyed_runner_restores_on_a_fresh_instance(self, tmp_path):
        import httpx

        async with capture_setup(
            tmp_path, lambda root: (root / "base.txt").write_bytes(b"exact wip")
        ) as case:
            manifest = await case.checkpoints.capture()
            target = tmp_path / "fresh"
            git(tmp_path, "clone", "-q", str(case.source), str(target))
            dispatch = case.dispatch.model_copy(
                update={"generation": 1, "checkpoint_id": manifest.checkpoint_id}
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=case.server.app),
                headers={"Authorization": "Bearer session-only"},
            ) as http:
                await Checkpoints(
                    dispatch, LaneClient(dispatch, http), Workspace(target, case.base)
                ).restore()
            assert (target / "base.txt").read_bytes() == b"exact wip"
            assert (case.source / "base.txt").read_bytes() == b"exact wip"

    async def test_a_deletion_present_again_in_the_target_is_applied(self, tmp_path):
        case = restore_setup(tmp_path, entries=(entry("base.txt", state="deleted"),))
        result = await run_restore(case)
        assert result.report.status == "completed" and not (case.target / "base.txt").exists()
        assert case.server.model_keys == 1 and case.driver.starts

    async def test_a_legacy_filename_manifest_restores_nothing(self, tmp_path):
        case = restore_setup(
            tmp_path,
            change=lambda raw: raw.update(
                schema="forge.wip-manifest/1", files={"base.txt": "old-file"}
            ),
        )
        result = await run_restore(case)
        assert result.report.classification == "invalid_checkpoint"
        assert (case.target / "base.txt").read_bytes() == b"base\n"
        assert case.server.model_keys == 0 and not case.driver.starts

    async def test_an_ungranted_principal_probes_nothing(self, tmp_path):
        case = restore_setup(tmp_path)
        case.server.bad_session = True
        result = await run_restore(case)
        assert result.report.classification == "identity_mismatch"
        assert not any(path.startswith(("checkpoints", "blobs")) for _, path in case.server.calls)
        assert case.server.model_keys == 0 and not case.driver.starts

    @pytest.mark.parametrize(
        "path",
        [
            "../outside",
            "/tmp/outside",
            "a/../b",
            "a//b",
            "a\\b",
            ".git/config",
            ".forge/generation.json",
        ],
    )
    async def test_a_traversal_manifest_is_refused_before_the_first_write(self, tmp_path, path):
        case = restore_setup(tmp_path, change=lambda raw: raw["entries"][0].update(path=path))
        result = await run_restore(case)
        assert result.report.classification == "invalid_checkpoint"
        assert (case.target / "base.txt").read_bytes() == b"base\n"
        assert not case.server.model_keys and not case.driver.starts

    @pytest.mark.parametrize(
        "axis,value",
        [
            ("forge_run_id", "foreign"),
            ("attempt", 9),
            ("generation", 1),
            ("base_sha", "b" * 40),
            ("approved_digest", "f" * 64),
            ("driver", "foreign"),
        ],
    )
    async def test_foreign_baseline_or_execution_identity_cannot_resume(
        self, tmp_path, axis, value
    ):
        case = restore_setup(tmp_path, change=lambda raw: raw.update({axis: value}))
        result = await run_restore(case)
        assert result.report.classification == "checkpoint_mismatch"
        assert not case.server.model_keys and not case.driver.starts
        assert (case.target / "base.txt").read_bytes() == b"base\n"

    @pytest.mark.parametrize(
        "change", ["missing_digest", "missing_blob", "flipped_blob", "invalid_mode"]
    )
    async def test_missing_or_corrupt_required_bytes_are_never_a_fresh_restart(
        self, tmp_path, change
    ):
        case = restore_setup(tmp_path)
        if change == "missing_digest":
            case.document["entries"][0]["sha256"] = None
        elif change == "invalid_mode":
            case.document["entries"][0]["mode"] = "100777"
        elif change == "missing_blob":
            from fastapi.responses import Response

            @case.server.app.middleware("http")
            async def missing(request, call_next):
                if "/blobs/" in request.url.path:
                    return Response(status_code=404)
                return await call_next(request)
        else:
            key = case.manifest.entries[0].sha256
            case.server.blobs[key] = b"not the authorized bytes"
        result = await run_restore(case)
        assert (
            result.report.status == "failed"
            and not case.server.model_keys
            and not case.driver.starts
        )
        assert (case.target / "base.txt").read_bytes() == b"base\n"


class TestTransactionalRestoreSafety:
    async def test_a_corrupt_later_blob_leaves_the_target_untouched(self, tmp_path):
        first, second = b"first valid", b"second exact"
        case = restore_setup(
            tmp_path,
            entries=(entry("base.txt", first, state="modified"), entry("new.txt", second)),
            blobs={
                sha256(first).hexdigest(): first,
                sha256(second).hexdigest(): b"later corruption",
            },
        )
        result = await run_restore(case)
        assert result.report.classification == "corrupt_blob"
        assert (case.target / "base.txt").read_bytes() == b"base\n" and not (
            case.target / "new.txt"
        ).exists()
        assert not case.server.model_keys and not case.driver.starts

    async def test_a_symlinked_ancestor_writes_and_deletes_nothing_outside(self, tmp_path):
        case = restore_setup(tmp_path, entries=(entry("escape/private", state="deleted"),))
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "private").write_bytes(b"untouched")
        (case.target / "escape").symlink_to(outside)
        result = await run_restore(case)
        assert (
            result.report.status == "failed"
            and not case.server.model_keys
            and not case.driver.starts
        )
        assert (outside / "private").read_bytes() == b"untouched"

    @pytest.mark.parametrize(
        "path", [".mcp.json", ".env", ".claude/settings.json", ".git-credentials", "uv.lock"]
    )
    async def test_reserved_namespaces_and_credentials_are_refused(self, tmp_path, path):
        data = b"not granted"
        case = restore_setup(
            tmp_path, entries=(entry(path, data),), blobs={sha256(data).hexdigest(): data}
        )
        result = await run_restore(case)
        assert (
            result.report.status == "failed"
            and not case.server.model_keys
            and not case.driver.starts
        )
        assert not (case.target / path).exists()

    async def test_path_aliases_and_file_directory_conflicts_are_refused(self, tmp_path):
        data = b"new"
        case = restore_setup(
            tmp_path,
            entries=(entry("a", data), entry("a/b", data)),
            blobs={sha256(data).hexdigest(): data},
        )
        result = await run_restore(case)
        assert result.report.classification == "unsafe_checkpoint"
        assert (
            not (case.target / "a").exists()
            and not case.server.model_keys
            and not case.driver.starts
        )

    async def test_a_mode_only_change_survives_the_restore(self, tmp_path):
        data = b"base\n"
        case = restore_setup(
            tmp_path,
            entries=(entry("base.txt", data, state="modified", mode="100755"),),
            blobs={sha256(data).hexdigest(): data},
        )
        result = await run_restore(case)
        assert (
            result.report.status == "completed"
            and (case.target / "base.txt").stat().st_mode & 0o111
        )
        assert (case.target / "base.txt").read_bytes() == data


class TestPromotionTransaction:
    async def test_apply_failure_refuses_model_and_dirty_checkout_cannot_be_reused(
        self, tmp_path, monkeypatch
    ):
        first, second = b"first valid", b"second exact"
        case = restore_setup(
            tmp_path,
            entries=(entry("base.txt", first, state="modified"), entry("new.txt", second)),
            blobs={sha256(first).hexdigest(): first, sha256(second).hexdigest(): second},
        )
        original = Path.write_bytes

        def fail(path, data):
            if path == case.target / "new.txt":
                raise OSError("owned injected second-write failure")
            return original(path, data)

        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "write_bytes", fail)
            result = await run_restore(case)
        assert result.report.classification == "restore_failed"
        assert not case.server.model_keys and not case.driver.starts
        assert (case.target / "base.txt").read_bytes() == first
        case.server.calls.clear()
        result = await run_restore(case)
        assert result.report.classification == "dirty_checkout" and not case.driver.starts
        assert not any(path.startswith("checkpoints") for _, path in case.server.calls)

    async def test_a_manifest_cannot_forge_the_generation_pointer(self, tmp_path):
        case = restore_setup(
            tmp_path, change=lambda raw: raw.update(generation_directory="../foreign")
        )
        result = await run_restore(case)
        assert result.report.classification == "invalid_checkpoint" and not case.driver.starts
        assert (case.target / "base.txt").read_bytes() == b"base\n"


class TestOwnershipScopedRecovery:
    @pytest.mark.parametrize(
        "name", [".forge-workspace-gen-old", ".forge-backup-foreign", ".forge-staging-unowned"]
    )
    async def test_legacy_unowned_leftovers_are_never_promoted_or_collected(self, tmp_path, name):
        foreign = tmp_path / name
        foreign.mkdir()
        (foreign / "private").write_bytes(b"foreign owner")
        case = restore_setup(tmp_path)
        result = await run_restore(case)
        assert result.report.status == "completed"
        assert (foreign / "private").read_bytes() == b"foreign owner"
        assert (case.target / "base.txt").read_bytes().startswith(b"restored exact")


class TestBoundedCaptureScope:
    async def test_runtime_dirs_caches_and_the_store_are_never_captured(self, tmp_path):
        def prepare(root):
            (root / ".gitignore").write_text(".venv/\n__pycache__/\n")
            git(root, "add", ".gitignore")
            git(root, "commit", "-qm", "target ignore policy")

        def edit(root):
            for path in (".forge/store", ".venv/cache", "__pycache__/file"):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"private")
            (root / "new.py").write_bytes(b"approved")

        async with capture_setup(tmp_path, edit, prepare=prepare) as case:
            # The target owns its Git ignore policy; the lane always excludes .forge.
            manifest = await case.checkpoints.capture()
            assert [item.path for item in manifest.entries] == ["new.py"]
            assert list(case.server.blobs.values()) == [b"approved"]

    async def test_repeated_capture_is_stable_and_never_grows_from_self_capture(self, tmp_path):
        async with capture_setup(
            tmp_path, lambda root: (root / "base.txt").write_bytes(b"exact")
        ) as case:
            first, second = await case.checkpoints.capture(), await case.checkpoints.capture()
            assert first.entries == second.entries and second.sequence == first.sequence + 1
            assert len(case.server.blobs) == 1
            assert (
                sum(
                    method == "PUT" and path.startswith("blobs/")
                    for method, path in case.server.calls
                )
                == 1
            )

    async def test_a_git_oid_shaped_baseline_never_matches_so_nothing_is_skipped(self, tmp_path):
        async with capture_setup(tmp_path) as case:
            case.checkpoints.workspace.base_sha = "f" * 40
            with pytest.raises(LaneFailure):
                await case.checkpoints.capture()
            assert not case.server.checkpoints and not case.server.blobs

    async def test_a_mode_only_change_is_captured_exactly_once(self, tmp_path):
        async with capture_setup(tmp_path, lambda root: (root / "base.txt").chmod(0o755)) as case:
            manifest = await case.checkpoints.capture()
            assert [(item.path, item.mode) for item in manifest.entries] == [("base.txt", "100755")]
            assert list(case.server.blobs.values()) == [b"base\n"]

    async def test_content_and_mode_unchanged_is_fully_skipped(self, tmp_path):
        async with capture_setup(tmp_path) as case:
            manifest = await case.checkpoints.capture()
            assert manifest.entries == () and not case.server.blobs


class TestResumeUnderAFreshEpoch:
    async def test_current_authorization_and_verified_bytes_resume_under_a_fresh_epoch(
        self, operator
    ):
        run, frozen, pending, checkpoint, key, raw = await paused(operator)
        result = await operator["commands"].submit(envelope(operator, "resume"))
        current = await operator["env"].runs.load(run.id)
        assert result.reason == "resume_scheduled" and current.attempt == pending.identity.attempt
        assert current.generation == pending.identity.generation + 1
        assert (await operator["env"].approvals.load(run.id)).approved == frozen.approved
        assert json.loads(result.data_json)["checkpoint_id"] == checkpoint.checkpoint_id

    async def test_revoked_authorization_blocks_resume_even_if_valid_before_pause(self, operator):
        run, _, pending, *_ = await paused(operator)
        operator["env"].fake.approvers.clear()
        result = await operator["commands"].submit(envelope(operator, "resume"))
        assert result.status == "refused"
        assert (await operator["env"].runs.load(run.id)).generation == pending.identity.generation

    async def test_expired_checkpoint_bytes_block_resume(self, operator):
        run, _, pending, manifest, *_ = await paused(operator)
        async with operator["env"].factory.begin() as session:
            checkpoint = await session.get(EngineCheckpoint, manifest.checkpoint_id)
            checkpoint.retained = False
        result = await operator["commands"].submit(envelope(operator, "resume"))
        assert result.reason == "checkpoint_unavailable"
        assert (await operator["env"].runs.load(run.id)).generation == pending.identity.generation

    async def test_a_partial_pause_has_no_durable_checkpoint_to_resume_from(self, operator):
        run, _, pending = await started(operator)
        async with operator["env"].factory.begin() as session:
            current = await session.get(EngineRun, run.id)
            current.state, current.reason = "blocked", "lane_paused"
        result = await operator["commands"].submit(envelope(operator, "resume"))
        assert result.reason == "checkpoint_unavailable"
        assert (await operator["env"].runs.load(run.id)).generation == pending.identity.generation
