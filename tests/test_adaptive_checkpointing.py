"""The checkpoint transaction (NXT-15..NXT-18): capture, restore, resume.

The tests pin the difference between a pause that SAYS "saved" and one
that IS saved: a capture that uploads blobs, reads them back
digest-identical and commits the manifest as the checkpoint reference;
a failed capture that books an honest ``paused_failed`` naming the last
REAL checkpoint; a restore on a destroyed first runner that a fresh
second store instance reconstructs file-by-file (modified, new,
deleted, executable bit) under independent digest checks; retention
that cannot delete an active pause's checkpoint on age alone; and a
resume that re-verifies authorization and checkpoint bytes NOW under a
fresh execution epoch.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path

import pytest

from forge.adaptive.artifact_store import (
    ContentAddressedStore,
    wip_manifest,
)
from forge.adaptive.checkpointing import (
    MANIFEST_SCHEMA,
    CaptureFailed,
    book_checkpoint,
    capture_wip,
    cooperative_capture,
    restore_wip,
    resume_from_checkpoint,
    accepts_epoch,
)
from forge.adaptive.control import (
    Mailbox,
    PauseState,
    drain_turn,
    recorded_pause,
    request_pause,
    send_interrupt,
)
from forge.adaptive.models import ControlCommand

TENANT = "work-tenant"
WORK_ID = "wp-check-1"
SCOPES = {"server_authenticated_human": ("human:reviewer-17",)}

_BASE_APP = b'print("v1")\n'
_BASE_OLD = b"old module\n"
_BASE_README = b"# readme\n"
_WIP_APP = b'print("v2")\n'
_WIP_NOTES = b"scratch notes\n"
_WIP_SCRIPT = b"#!/bin/sh\necho wip\n"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _baseline() -> dict[str, str]:
    """The tracked baseline: the snapshot digests the WIP applies on top of."""
    return {
        "src/app.py": _digest(_BASE_APP),
        "src/old.py": _digest(_BASE_OLD),
        "README.md": _digest(_BASE_README),
    }


def _wip_tree(root: Path) -> Path:
    """A working tree one edit-set ahead of the baseline.

    ``src/app.py`` is MODIFIED, ``src/old.py`` is DELETED, ``README.md``
    is unchanged, ``notes.md`` is NEW untracked content and
    ``scripts/run.sh`` is NEW with the executable bit set.
    """
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_bytes(_WIP_APP)
    (root / "src" / "old.py").unlink(missing_ok=True)  # deleted vs baseline
    (root / "README.md").write_bytes(_BASE_README)  # unchanged
    (root / "notes.md").write_bytes(_WIP_NOTES)  # untracked
    (root / "scripts").mkdir(exist_ok=True)
    script = root / "scripts" / "run.sh"
    script.write_bytes(_WIP_SCRIPT)
    script.chmod(0o755)
    return root


def _capture(
    tmp_path: Path, *, work_id: str = WORK_ID
) -> tuple[Path, ContentAddressedStore, object]:
    tree = _wip_tree(tmp_path / "runner-a")
    store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
    receipt = capture_wip(
        work_id=work_id,
        root=tree,
        store=store,
        tracked_baseline=_baseline(),
        source_oids={"repo-main": "0" * 40},
        sequence=7,
    )
    return tree, store, receipt


def _backdate_store(store_root: Path, age_s: int = 7200) -> None:
    stamp = time.time() - age_s
    for shard in store_root.iterdir():
        if shard.is_dir():
            for artifact in shard.iterdir():
                os.utime(artifact, (stamp, stamp))


class TestCaptureTransaction:
    def test_capture_uploads_blobs_and_commits_a_verified_manifest(self, tmp_path: Path):
        tree, store, receipt = _capture(tmp_path)

        assert receipt.verified is True
        assert receipt.sequence == 7
        assert receipt.files == 3  # modified app.py + new notes.md + new run.sh
        assert receipt.deletions == 1  # src/old.py
        assert receipt.artifact_id == receipt.digest  # the address IS the digest
        # The committed reference is a REAL store object...
        assert store.get_verified(receipt.artifact_id, principal=TENANT) is not None
        # ...whose manifest names the checkpoint and the source OIDs.
        manifest = json.loads(store.get(receipt.artifact_id))
        assert manifest["schema"] == MANIFEST_SCHEMA
        assert manifest["work_id"] == WORK_ID
        assert manifest["source_oids"] == {"repo-main": "0" * 40}
        assert manifest["files"]["src/app.py"]["role"] == "modified"
        assert manifest["files"]["notes.md"]["role"] == "new"
        assert manifest["deletions"] == ["src/old.py"]

    def test_unchanged_baseline_files_are_not_re_uploaded(self, tmp_path: Path):
        tree, store, receipt = _capture(tmp_path)

        # README.md still matches the baseline: the manifest does not carry
        # it and its digest was never stored.
        assert "README.md" not in json.loads(store.get(receipt.artifact_id))["files"]
        assert store.resolve(_digest(_BASE_README)) is False

    def test_the_full_sequence_fence_interrupt_drain_capture_checkpoint(self, tmp_path: Path):
        """NXT-15's transaction, end to end over the real Mailbox ladder:
        durable command row + publication fence FIRST, then the interrupt,
        then the cooperative drain running the REAL capture, then the
        mailbox's ``checkpointed`` rung booked on the committed receipt."""
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        mailbox = Mailbox()
        command = ControlCommand(
            schema="forge.proposal.control-command/1",
            command_id="cmd-pause-1",
            work_id=WORK_ID,
            sequence=1,
            kind="pause",
            actor_ref="human:reviewer-17",
            actor_origin="server_authenticated_human",
            idempotency_key="note:pause:1",
            status="received",
        )

        fenced, stored, created = recorded_pause(
            PauseState(work_id=WORK_ID), command, mailbox.submit
        )
        assert created is True and stored.status == "received"
        assert fenced.pause_requested is True and fenced.publication_epoch == 1

        mailbox.authorize(command.command_id, SCOPES)
        mailbox.apply(command.command_id, current_plan_revision=1, current_execution_epoch=1)
        interrupted = send_interrupt(fenced)
        assert interrupted.interrupt_sent is True

        drained = drain_turn(
            interrupted,
            cooperative=True,  # quiescence observed at the cooperative boundary
            capture=cooperative_capture(
                work_id=WORK_ID,
                root=tree,
                store=store,
                tracked_baseline=_baseline(),
                source_oids={"repo-main": "0" * 40},
                sequence=interrupted.last_applied_command_sequence,
            ),
        )

        assert drained.pause_status == "paused"
        assert drained.checkpoint_captured is True
        assert drained.wip_artifact_id == drained.checkpoint_receipt.artifact_id
        assert drained.checkpoint_receipt.verified is True
        assert book_checkpoint(mailbox, command.command_id) == "checkpointed"

    def test_a_failed_upload_lands_paused_failed_not_captured(self, tmp_path: Path):
        """Disk fills mid-upload: the drain books the failure, keeps the last
        REAL checkpoint as recoverable, and claims nothing."""
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        calls = {"n": 0}

        def disk_filling_put(data: bytes, **_kwargs: object) -> str:
            calls["n"] += 1
            if calls["n"] > 1:  # first blob lands, the second hits a full disk
                raise RuntimeError("disk full during artifact upload")
            return ContentAddressedStore.put(store, data)

        store.put = disk_filling_put  # type: ignore[method-assign]
        state = PauseState(
            work_id=WORK_ID, last_applied_command_sequence=9, last_checkpoint_sequence=5
        )

        drained = drain_turn(
            send_interrupt(request_pause(state)),
            cooperative=True,
            capture=cooperative_capture(
                work_id=WORK_ID,
                root=tree,
                store=store,
                tracked_baseline=_baseline(),
                sequence=9,
            ),
        )

        assert drained.pause_status == "paused_failed"
        assert drained.checkpoint_captured is False
        assert drained.wip_artifact_id is None
        assert drained.checkpoint_receipt is None
        assert "disk full" in drained.failure_reason
        assert drained.last_recoverable == 5  # the last REAL checkpoint

    def test_pre_corrupted_store_content_fails_the_capture(self, tmp_path: Path):
        """A blob already rotting under its address: put() is a no-op for the
        existing address, the read-back verification QUARANTINES it and the
        capture refuses — an unverified upload is not a checkpoint."""
        tree, store, _first = _capture(tmp_path)
        blob = store.get(_digest(_WIP_APP))
        assert blob is not None
        target = tmp_path / "store" / _digest(_WIP_APP)[:2] / _digest(_WIP_APP)
        target.write_bytes(b"TAMPERED")  # rot the existing blob in place

        with pytest.raises(CaptureFailed, match="quarantined corrupt content"):
            capture_wip(
                work_id=WORK_ID,
                root=tree,
                store=store,
                tracked_baseline=_baseline(),
                sequence=8,
            )
        # The corrupt content was quarantined aside — the address stops
        # serving it (absent), and the evidence file survives.
        assert store.get_verified(_digest(_WIP_APP), principal=TENANT) is None
        assert (
            tmp_path / "store" / _digest(_WIP_APP)[:2] / f"{_digest(_WIP_APP)}.corrupt"
        ).is_file()

    def test_a_symlink_in_the_wip_tree_refuses_capture(self, tmp_path: Path):
        tree = _wip_tree(tmp_path / "runner-a")
        (tree / "link.py").symlink_to(tree / "src" / "app.py")
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)

        with pytest.raises(CaptureFailed, match="symlink"):
            capture_wip(
                work_id=WORK_ID,
                root=tree,
                store=store,
                tracked_baseline=_baseline(),
            )


class TestRestoreOnASecondRunner:
    def test_destroyed_runner_restores_on_a_fresh_instance(self, tmp_path: Path):
        """The NXT-17 flagship: capture, DESTROY the first runner, open a
        FRESH store instance (a new process — grants came from the persisted
        metadata), restore into a clean second-runner workspace, and check
        the files independently: content, executable bit, deletion."""
        tree, _store, receipt = _capture(tmp_path)
        shutil.rmtree(tree)  # the first runner is gone

        second_store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        target = tmp_path / "runner-b"
        target.mkdir()
        (target / "README.md").write_bytes(_BASE_README)  # the snapshot base

        report = restore_wip(
            artifact_id=receipt.artifact_id, store=second_store, target=target, principal=TENANT
        )

        assert report.ok is True
        assert report.failures == ()
        outcomes = {item.path: item.outcome for item in report.files}
        assert outcomes == {
            "notes.md": "restored",
            "scripts/run.sh": "restored",
            "src/app.py": "restored",
            "src/old.py": "already-absent",
        }
        # Independent file-level checks — not the model's word for it:
        assert (target / "src" / "app.py").read_bytes() == _WIP_APP  # modified survives
        assert (target / "notes.md").read_bytes() == _WIP_NOTES  # new/untracked survives
        assert (target / "scripts" / "run.sh").read_bytes() == _WIP_SCRIPT
        assert os.stat(target / "scripts" / "run.sh").st_mode & 0o111  # the exec bit
        assert not (target / "src" / "old.py").exists()  # the deletion survives
        assert (target / "README.md").read_bytes() == _BASE_README  # base untouched
        # Independent DIGEST check: every restored file hashes to its manifest digest.
        manifest = json.loads(second_store.get(receipt.artifact_id))
        for path, entry in manifest["files"].items():
            assert _digest((target / path).read_bytes()) == entry["digest"]

    def test_a_deletion_present_again_in_the_target_is_applied(self, tmp_path: Path):
        _tree, _store, receipt = _capture(tmp_path)
        target = tmp_path / "runner-b"
        (target / "src").mkdir(parents=True)
        (target / "src" / "old.py").write_bytes(_BASE_OLD)  # stale copy from the base
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)

        report = restore_wip(
            artifact_id=receipt.artifact_id, store=store, target=target, principal=TENANT
        )

        assert report.ok is True
        assert not (target / "src" / "old.py").exists()
        assert {item.outcome for item in report.files if item.path == "src/old.py"} == {"deleted"}

    def test_a_legacy_filename_manifest_restores_nothing(self, tmp_path: Path):
        """The /1 shape — untracked FILENAMES without content blobs — is the
        schema the review called insufficient; restore refuses it outright."""
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        legacy = wip_manifest(
            tracked={"src/app.py": _digest(_WIP_APP)},
            untracked=["notes.md"],
            deletions=["src/old.py"],
            source_oids={"repo-main": "0" * 40},
        )
        artifact_id = store.put(json.dumps(legacy).encode(), content_type="application/json")

        report = restore_wip(
            artifact_id=artifact_id, store=store, target=tmp_path / "runner-b", principal=TENANT
        )

        assert report.ok is False
        assert not (tmp_path / "runner-b" / "notes.md").exists()
        assert any("filename list without content blobs" in failure for failure in report.failures)

    def test_an_entry_without_a_content_digest_is_refused(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "work_id": WORK_ID,
            "sequence": 0,
            "source_oids": {},
            "files": {"notes.md": {"mode": 0o644, "role": "new"}},  # no digest
            "deletions": [],
        }
        artifact_id = store.put(json.dumps(manifest).encode(), content_type="application/json")

        report = restore_wip(
            artifact_id=artifact_id, store=store, target=tmp_path / "runner-b", principal=TENANT
        )

        assert report.ok is False
        assert any("notes.md" in failure for failure in report.failures)
        assert not (tmp_path / "runner-b" / "notes.md").exists()

    def test_a_missing_required_blob_blocks_resume_with_evidence(self, tmp_path: Path):
        _tree, store, receipt = _capture(tmp_path)
        notes_blob = tmp_path / "store" / _digest(_WIP_NOTES)[:2] / _digest(_WIP_NOTES)
        notes_blob.unlink()  # the blob rotted away between capture and restore

        report = restore_wip(
            artifact_id=receipt.artifact_id,
            store=ContentAddressedStore(tmp_path / "store", tenant=TENANT),
            target=tmp_path / "runner-b",
            principal=TENANT,
        )

        assert report.ok is False
        assert any(
            "notes.md" in failure and _digest(_WIP_NOTES) in failure for failure in report.failures
        )
        # R28-02: a missing blob refuses the WHOLE restore — the target
        # stays clean (no half-applied workspace), and the report names
        # exactly which leg failed.
        assert not (tmp_path / "runner-b" / "src" / "app.py").exists()
        assert not (tmp_path / "runner-b" / "notes.md").exists()

    def test_tampered_blob_bytes_are_refused_not_returned(self, tmp_path: Path):
        _tree, _store, receipt = _capture(tmp_path)
        blob = tmp_path / "store" / _digest(_WIP_NOTES)[:2] / _digest(_WIP_NOTES)
        blob.write_bytes(b"EVIL")  # tamper AFTER the capture was committed

        report = restore_wip(
            artifact_id=receipt.artifact_id,
            store=ContentAddressedStore(tmp_path / "store", tenant=TENANT),
            target=tmp_path / "runner-b",
            principal=TENANT,
        )

        assert report.ok is False
        assert any("corrupt" in failure for failure in report.failures)
        assert not (tmp_path / "runner-b" / "notes.md").exists()  # never written

    def test_tampered_manifest_refuses_before_anything_is_written(self, tmp_path: Path):
        _tree, _store, receipt = _capture(tmp_path)
        manifest_path = tmp_path / "store" / receipt.artifact_id[:2] / receipt.artifact_id
        manifest_path.write_bytes(b"NOT JSON")

        report = restore_wip(
            artifact_id=receipt.artifact_id,
            store=ContentAddressedStore(tmp_path / "store", tenant=TENANT),
            target=tmp_path / "runner-b",
            principal=TENANT,
        )

        assert report.ok is False
        assert report.failures and "corrupt" in report.failures[0]

    def test_an_ungranted_principal_probes_nothing(self, tmp_path: Path):
        _tree, _store, receipt = _capture(tmp_path)
        outsider = ContentAddressedStore(tmp_path / "store", tenant="tenant-b")

        report = restore_wip(
            artifact_id=receipt.artifact_id,
            store=outsider,
            target=tmp_path / "runner-b",
            principal="tenant-b",
        )

        assert report.ok is False
        # Absent and ungranted are indistinguishable — knowing the address
        # is not reading the artifact.
        assert any("absent or not granted" in failure for failure in report.failures)

    def test_a_traversal_manifest_is_refused_before_the_first_write(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "work_id": WORK_ID,
            "sequence": 0,
            "source_oids": {},
            "files": {"../evil.txt": {"digest": _digest(b"evil"), "mode": 0o644, "role": "new"}},
            "deletions": [],
        }
        artifact_id = store.put(json.dumps(manifest).encode(), content_type="application/json")

        report = restore_wip(
            artifact_id=artifact_id, store=store, target=tmp_path / "runner-b", principal=TENANT
        )

        assert report.ok is False
        assert any("escapes the restore target" in failure for failure in report.failures)
        assert not (tmp_path / "evil.txt").exists()


class TestTransactionalRestoreSafety:
    """R28-02: the restore is path-safe, reserved-path-safe and transactional.

    The reviewed defects: a lexical in-root path under a pre-existing
    symlink ancestor wrote/deleted OUTSIDE the workspace; ``.git/config``
    passed the lexical validator; a failing later blob left the earlier
    files applied. The contract pinned here: lstat every ancestor
    (symlink → refuse, never follow), refuse reserved namespaces and
    credential-shaped names, refuse aliases/conflicts/unsupported kinds
    with precise reasons, and apply NOTHING until the whole plan is
    verified — staged, then moved, staging discarded on any failure."""

    @staticmethod
    def _manifest_for(files: dict[str, tuple[bytes, int]], deletions: list[str] | None = None):
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "work_id": WORK_ID,
            "sequence": 0,
            "source_oids": {},
            "files": {
                rel: {"digest": _digest(data), "mode": mode, "role": "new"}
                for rel, (data, mode) in files.items()
            },
            "deletions": deletions or [],
        }
        blobs = {_digest(data): data for _rel, (data, _mode) in files.items()}
        return manifest, blobs

    def _restore_manifest(
        self,
        tmp_path: Path,
        manifest: dict,
        target: Path,
        blobs: dict[str, bytes] | None = None,
    ) -> object:
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        for digest, data in (blobs or {}).items():
            store.put(data)
        artifact_id = store.put(json.dumps(manifest).encode(), content_type="application/json")
        return restore_wip(artifact_id=artifact_id, store=store, target=target, principal=TENANT)

    def test_a_symlinked_ancestor_writes_and_deletes_nothing_outside(self, tmp_path: Path):
        """The reviewer's P05 repro: a sibling directory reached through a
        symlink INSIDE the workspace — zero writes and zero deletes may
        land through it."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"precious\n")
        (outside / "doomed.txt").write_bytes(b"stale\n")
        target = tmp_path / "runner-b"
        target.mkdir()
        (target / "link").symlink_to(outside)

        manifest, blobs = self._manifest_for(
            {"link/escape.txt": (b"escaped\n", 0o644)}, deletions=["link/doomed.txt"]
        )
        report = self._restore_manifest(tmp_path, manifest, target, blobs)

        assert report.ok is False
        assert any("symlink" in failure for failure in report.failures)
        # Zero outside writes AND zero outside deletes:
        assert (outside / "keep.txt").read_bytes() == b"precious\n"
        assert (outside / "doomed.txt").read_bytes() == b"stale\n"
        assert not (outside / "escape.txt").exists()
        assert not (target / "link" / "escape.txt").exists()  # not even via the link

    def test_reserved_namespaces_and_credentials_are_refused(self, tmp_path: Path):
        target = tmp_path / "runner-b"
        target.mkdir()
        (target / ".git").mkdir()
        reserved = {
            ".git/config": b"[core]\n",
            ".forge/checkpoints/ab/sentinel": b"store poison\n",
            "deploy.pem": b"PRIVATE KEY MATERIAL\n",
            "secrets/tenant.key": b"PRIVATE KEY MATERIAL\n",
            ".env": b"TOKEN=1\n",
            "credentials.json": b"{}\n",
        }
        manifest, blobs = self._manifest_for({rel: (data, 0o644) for rel, data in reserved.items()})

        report = self._restore_manifest(tmp_path, manifest, target, blobs)

        assert report.ok is False
        joined = "\n".join(report.failures)
        for rel in reserved:
            assert rel in joined, f"{rel} must be named in the refusal"
        assert ".git namespace" in joined
        assert "checkpoint store itself" in joined
        assert "credential-shaped" in joined
        assert not (target / ".git" / "config").exists()
        assert not (target / "deploy.pem").exists()

    def test_a_corrupt_later_blob_leaves_the_target_untouched(self, tmp_path: Path):
        """The reviewer's rollback case: a valid FIRST file and a corrupt
        SECOND blob — nothing is applied, no staging remains, the
        workspace stays clean."""
        target = tmp_path / "runner-b"
        target.mkdir()
        manifest, blobs = self._manifest_for(
            {"a_first.txt": (b"first\n", 0o644), "b_second.txt": (b"second\n", 0o644)}
        )
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        for data in blobs.values():
            store.put(data)
        artifact_id = store.put(json.dumps(manifest).encode(), content_type="application/json")
        # Rot the SECOND blob (sorted file order) after the put.
        second_digest = manifest["files"]["b_second.txt"]["digest"]
        (tmp_path / "store" / second_digest[:2] / second_digest).write_bytes(b"EVIL")

        report = restore_wip(artifact_id=artifact_id, store=store, target=target, principal=TENANT)

        assert report.ok is False
        assert not (target / "a_first.txt").exists()  # the valid first file was NOT applied
        assert not (target / "b_second.txt").exists()
        assert list(target.parent.glob(".forge-restore-*")) == []  # staging discarded

    def test_path_aliases_and_file_directory_conflicts_are_refused(self, tmp_path: Path):
        target = tmp_path / "runner-b"
        target.mkdir()
        alias, blobs = self._manifest_for(
            {"src/app.py": (b"one\n", 0o644), "src//app.py": (b"two\n", 0o644)}
        )
        report = self._restore_manifest(tmp_path, alias, target, blobs)
        assert report.ok is False
        assert any("normalizes to the same path" in failure for failure in report.failures)
        assert not (target / "src").exists()

        conflict, blobs = self._manifest_for(
            {"a": (b"file\n", 0o644), "a/b.txt": (b"under\n", 0o644)}
        )
        report = self._restore_manifest(tmp_path, conflict, tmp_path / "runner-c", blobs)
        assert report.ok is False
        assert any("as a directory" in failure for failure in report.failures)
        runner_c = tmp_path / "runner-c"
        assert not runner_c.exists() or not any(runner_c.iterdir())

    def test_a_manifest_file_cannot_replace_a_target_directory(self, tmp_path: Path):
        target = tmp_path / "runner-b"
        target.mkdir()
        (target / "notes.md").mkdir()
        (target / "notes.md" / "inner.txt").write_bytes(b"inner\n")
        manifest, blobs = self._manifest_for({"notes.md": (b"file content\n", 0o644)})

        report = self._restore_manifest(tmp_path, manifest, target, blobs)

        assert report.ok is False
        assert any("exists as a directory" in failure for failure in report.failures)
        assert (target / "notes.md" / "inner.txt").read_bytes() == b"inner\n"

    def test_a_deletion_of_a_directory_is_refused_with_the_reason(self, tmp_path: Path):
        target = tmp_path / "runner-b"
        target.mkdir()
        (target / "olddir").mkdir()
        (target / "olddir" / "x.txt").write_bytes(b"x\n")
        manifest, blobs = self._manifest_for({}, deletions=["olddir"])

        report = self._restore_manifest(tmp_path, manifest, target, blobs)

        assert report.ok is False
        assert any("olddir" in failure and "directory" in failure for failure in report.failures)
        assert (target / "olddir" / "x.txt").exists()

    def test_an_unsupported_mode_is_refused_with_a_precise_reason(self, tmp_path: Path):
        data = b"#!/bin/sh\n"
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "work_id": WORK_ID,
            "sequence": 0,
            "source_oids": {},
            "files": {"unsafe.sh": {"digest": _digest(data), "mode": 0o777, "role": "new"}},
            "deletions": [],
        }
        report = self._restore_manifest(
            tmp_path, manifest, tmp_path / "runner-b", {_digest(data): data}
        )
        assert report.ok is False
        assert any("unsupported file kind/mode" in failure for failure in report.failures)
        assert not (tmp_path / "runner-b" / "unsafe.sh").exists()

    def test_a_mode_only_change_survives_the_restore(self, tmp_path: Path):
        """A mode-only delta (content identical to the baseline, executable
        bit changed) restores the mode — the transactional apply keeps
        chmod off the symlink-checked path."""
        target = tmp_path / "runner-b"
        target.mkdir()
        script = target / "run.sh"
        script.write_bytes(b"#!/bin/sh\necho hi\n")
        manifest, blobs = self._manifest_for({"run.sh": (b"#!/bin/sh\necho hi\n", 0o755)})

        report = self._restore_manifest(tmp_path, manifest, target, blobs)

        assert report.ok is True
        assert os.stat(script).st_mode & 0o111


class TestPromotionTransaction:
    """NEXT-04: the promotion is a TRANSACTION, not a series of per-file
    renames. The reviewed defect: preflight verified everything, but the
    apply loop could die on the second ``os.replace`` with the first
    file already changed — a half-applied workspace resume would mistake
    for a restored one. The contract pinned here: the COMPLETE next
    generation is built in staging and activated by ONE whole-tree
    switch; a failure at ANY boundary (staging, the switch's second
    rename, a deletion, the process itself) leaves the target at its
    ORIGINAL state or explicitly invalid — never mixed; the report says
    ``promotion_failed`` (rolled back) apart from ``preflight_failed``
    (nothing was written); a restart identifies and resolves an
    abandoned promotion's leftovers."""

    ORIG_A = b"original a\n"
    ORIG_B = b"original b\n"
    ORIG_DOOMED = b"stale baseline copy\n"
    NEW_A = b"checkpointed a\n"
    NEW_B = b"checkpointed b\n"

    @staticmethod
    def _two_files_one_deletion() -> tuple[dict, dict[str, bytes]]:
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "work_id": WORK_ID,
            "sequence": 0,
            "source_oids": {},
            "files": {
                "a.txt": {
                    "digest": _digest(b"checkpointed a\n"),
                    "mode": 0o644,
                    "role": "modified",
                },
                "b.txt": {
                    "digest": _digest(b"checkpointed b\n"),
                    "mode": 0o644,
                    "role": "modified",
                },
            },
            "deletions": ["doomed.txt"],
        }
        blobs = {
            _digest(b"checkpointed a\n"): b"checkpointed a\n",
            _digest(b"checkpointed b\n"): b"checkpointed b\n",
        }
        return manifest, blobs

    def _prepared_target(self, tmp_path: Path) -> Path:
        target = tmp_path / "runner-b"
        target.mkdir()
        (target / "a.txt").write_bytes(self.ORIG_A)
        (target / "b.txt").write_bytes(self.ORIG_B)
        (target / "doomed.txt").write_bytes(self.ORIG_DOOMED)
        (target / "untracked.txt").write_bytes(b"rides along\n")
        return target

    def _restore(self, tmp_path: Path, target: Path, manifest: dict, blobs: dict) -> object:
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        for data in blobs.values():
            store.put(data)
        artifact_id = store.put(json.dumps(manifest).encode(), content_type="application/json")
        return restore_wip(artifact_id=artifact_id, store=store, target=target, principal=TENANT)

    def test_the_whole_tree_switch_promotes_everything_or_nothing(self, tmp_path: Path):
        """The happy path: modified files, the deletion AND the untouched
        rider all land together through one generation switch."""
        target = self._prepared_target(tmp_path)
        manifest, blobs = self._two_files_one_deletion()

        report = self._restore(tmp_path, target, manifest, blobs)

        assert report.ok is True
        assert report.phase == "completed"
        assert report.target_invalid is False
        assert (target / "a.txt").read_bytes() == self.NEW_A
        assert (target / "b.txt").read_bytes() == self.NEW_B
        assert not (target / "doomed.txt").exists()
        assert (target / "untracked.txt").read_bytes() == b"rides along\n"
        outcomes = {item.path: item.outcome for item in report.files}
        assert outcomes == {"a.txt": "restored", "b.txt": "restored", "doomed.txt": "deleted"}

    def test_a_failed_second_rename_rolls_the_first_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The reviewer's exact repro: the SECOND promotion boundary fails
        (an injected OSError on the generation switch's landing rename)
        — BOTH files are at their ORIGINAL content after the failure,
        not mixed."""
        target = self._prepared_target(tmp_path)
        manifest, blobs = self._two_files_one_deletion()
        landed = {"n": 0}
        real_replace = os.replace

        def failing_second_rename(src, dst):
            # The switch's renames are the ones whose DESTINATION is the
            # workspace itself (the store's atomic writes use os.replace
            # too — key on dst, not call order).
            if dst == target:
                landed["n"] += 1
                if landed["n"] == 1:  # tree->target, the LANDING (move-aside went to backup)
                    raise OSError("injected: the landing rename fails")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", failing_second_rename)

        report = self._restore(tmp_path, target, manifest, blobs)

        assert report.ok is False
        assert report.phase == "promotion_failed"
        assert report.target_invalid is False
        assert any("rolled back" in failure for failure in report.failures)
        # BOTH files at their original content — never mixed.
        assert (target / "a.txt").read_bytes() == self.ORIG_A
        assert (target / "b.txt").read_bytes() == self.ORIG_B
        assert (target / "doomed.txt").read_bytes() == self.ORIG_DOOMED
        assert (target / "untracked.txt").read_bytes() == b"rides along\n"
        # Per-file evidence: NOTHING landed.
        assert {item.outcome for item in report.files} == {"failed"}
        assert list(target.parent.glob(".forge-restore-*")) == []  # no leftovers

    def test_a_failed_move_aside_never_touches_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The switch's FIRST rename fails: the target was never mutated
        and the report still distinguishes the promotion phase."""
        target = self._prepared_target(tmp_path)
        manifest, blobs = self._two_files_one_deletion()
        real_replace = os.replace

        def failing_first_rename(src, dst):
            # The MOVE-ASIDE: destination is the parked backup name.
            if ".forge-restore-backup-" in str(dst):
                raise OSError("injected: the move-aside fails")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", failing_first_rename)

        report = self._restore(tmp_path, target, manifest, blobs)

        assert report.ok is False
        assert report.phase == "promotion_failed"
        assert report.target_invalid is False
        assert (target / "a.txt").read_bytes() == self.ORIG_A
        assert (target / "doomed.txt").read_bytes() == self.ORIG_DOOMED

    def test_a_failed_deletion_aborts_before_the_target_is_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A deletion that fails inside the staged generation aborts the
        promotion with the LIVE target untouched — deletions are applied
        to the copy, never per-file onto the live workspace."""
        target = self._prepared_target(tmp_path)
        manifest, blobs = self._two_files_one_deletion()
        real_unlink = os.unlink

        def failing_doomed_unlink(path, *args, **kwargs):
            if str(path).endswith("doomed.txt"):
                raise OSError("injected: the deletion fails")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", failing_doomed_unlink)

        report = self._restore(tmp_path, target, manifest, blobs)

        assert report.ok is False
        assert report.phase == "promotion_failed"
        assert report.target_invalid is False
        assert (target / "a.txt").read_bytes() == self.ORIG_A
        assert (target / "b.txt").read_bytes() == self.ORIG_B
        assert (target / "doomed.txt").read_bytes() == self.ORIG_DOOMED

    def test_a_killed_restore_between_the_renames_is_rolled_back_by_the_next_run(
        self, tmp_path: Path
    ):
        """The kill-midway trace, simulated at the exact crash point: the
        original generation is parked beside a MISSING target. The next
        restore identifies it, rolls the original back FIRST, and only
        then proceeds — the report's ``recovery`` carries the evidence."""
        target = self._prepared_target(tmp_path)
        manifest, blobs = self._two_files_one_deletion()

        # The crash state: the switch died between its two renames.
        parked = target.parent / ".forge-restore-backup-9999-deadbeef"
        os.replace(target, parked)
        (target.parent / ".forge-restore-stale-staging").mkdir()
        (target.parent / ".forge-restore-stale-staging" / "tree").mkdir()
        assert not target.exists()  # explicitly unusable, never mixed

        report = self._restore(tmp_path, target, manifest, blobs)

        assert report.ok is True
        assert report.phase == "completed"
        assert any("rolled back abandoned backup" in note for note in report.recovery)
        assert any("collected abandoned staging" in note for note in report.recovery)
        # The retry then restored on top of the RECOVERED original.
        assert (target / "a.txt").read_bytes() == self.NEW_A
        assert (target / "b.txt").read_bytes() == self.NEW_B
        assert not (target / "doomed.txt").exists()
        assert (target / "untracked.txt").read_bytes() == b"rides along\n"
        assert list(target.parent.glob(".forge-restore-*")) == []

    def test_a_parked_backup_beside_a_landed_target_is_discarded(self, tmp_path: Path):
        """The other crash point: the landing rename SUCCEEDED and only
        the cleanup died. The next restore keeps the promoted generation
        and discards the parked copy — no rollback of good work."""
        target = self._prepared_target(tmp_path)
        (target / "a.txt").write_bytes(self.NEW_A)  # an earlier promotion landed
        parked = target.parent / ".forge-restore-backup-7777-cafe"
        parked.mkdir()
        (parked / "a.txt").write_bytes(self.ORIG_A)

        report = self._restore(
            tmp_path, target, *self._two_files_one_deletion()
        )  # a fresh, unrelated restore

        assert report.ok is True
        assert any("discarded abandoned backup" in note for note in report.recovery)
        assert not parked.exists()

    def test_preflight_failures_say_so_and_write_nothing(self, tmp_path: Path):
        """The phase distinction: a reserved-namespace entry refuses at
        PREFLIGHT — nothing was written — and the report says
        ``preflight_failed``, never ``promotion_failed``."""
        target = self._prepared_target(tmp_path)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "work_id": WORK_ID,
            "sequence": 0,
            "source_oids": {},
            "files": {
                "a.txt": {"digest": _digest(self.NEW_A), "mode": 0o644, "role": "modified"},
                ".git/config": {"digest": _digest(b"[core]\n"), "mode": 0o644, "role": "new"},
            },
            "deletions": [],
        }

        report = self._restore(tmp_path, target, manifest, {_digest(self.NEW_A): self.NEW_A})

        assert report.ok is False
        assert report.phase == "preflight_failed"
        assert report.target_invalid is False
        assert (target / "a.txt").read_bytes() == self.ORIG_A  # nothing was written

    def test_the_savepoint_fallback_rolls_a_half_applied_plan_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The degraded path (a read-only parent parks the staging inside
        ``target/.forge``): per-file promotion under a FULL savepoint.
        The second per-file replace fails — the first file's original is
        restored, the plan's new state never surfaces, and the report
        carries ``promotion_failed`` with a usable (original) target."""
        import forge.adaptive.checkpointing as cp

        target = self._prepared_target(tmp_path)
        manifest, blobs = self._two_files_one_deletion()
        inside = target / ".forge"

        def inside_staging(t: Path) -> tuple[Path, bool]:
            inside.mkdir(parents=True, exist_ok=True)
            import tempfile as _tempfile

            return Path(_tempfile.mkdtemp(dir=inside, prefix="restore-")), False

        monkeypatch.setattr(cp, "_staging_dir", inside_staging)
        calls = {"n": 0}
        real_replace = os.replace

        def failing_second_file(src, dst):
            if str(dst).endswith("b.txt"):
                calls["n"] += 1
                if calls["n"] == 1:  # the first promotion of b.txt (a.txt landed)
                    raise OSError("injected: the second file replace fails")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", failing_second_file)

        report = self._restore(tmp_path, target, manifest, blobs)

        assert report.ok is False
        assert report.phase == "promotion_failed"
        assert report.target_invalid is False
        assert any("savepoint" in failure for failure in report.failures)
        # BOTH files at their original content; the deletion never applied.
        assert (target / "a.txt").read_bytes() == self.ORIG_A
        assert (target / "b.txt").read_bytes() == self.ORIG_B
        assert (target / "doomed.txt").read_bytes() == self.ORIG_DOOMED

    def test_the_savepoint_fallback_rolls_a_failed_deletion_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Same degraded path, failure at the DELETION boundary: a.txt and
        b.txt already promoted, the deletion of doomed.txt fails — the
        savepoint recreates it and un-promotes both files."""
        import forge.adaptive.checkpointing as cp

        target = self._prepared_target(tmp_path)
        manifest, blobs = self._two_files_one_deletion()
        inside = target / ".forge"

        def inside_staging(t: Path) -> tuple[Path, bool]:
            inside.mkdir(parents=True, exist_ok=True)
            import tempfile as _tempfile

            return Path(_tempfile.mkdtemp(dir=inside, prefix="restore-")), False

        monkeypatch.setattr(cp, "_staging_dir", inside_staging)
        real_unlink = os.unlink

        def failing_doomed_unlink(path, *args, **kwargs):
            if str(path).endswith("doomed.txt") and inside.as_posix() not in str(path):
                raise OSError("injected: the deletion fails on the live target")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", failing_doomed_unlink)

        report = self._restore(tmp_path, target, manifest, blobs)

        assert report.ok is False
        assert report.phase == "promotion_failed"
        assert report.target_invalid is False
        assert (target / "a.txt").read_bytes() == self.ORIG_A
        assert (target / "b.txt").read_bytes() == self.ORIG_B
        assert (target / "doomed.txt").read_bytes() == self.ORIG_DOOMED

    def test_the_savepoint_fallback_restores_cleanly_when_it_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The degraded path's happy day: same end state as the whole-tree
        switch — modified, deleted and rider all land."""
        import forge.adaptive.checkpointing as cp

        target = self._prepared_target(tmp_path)
        manifest, blobs = self._two_files_one_deletion()
        inside = target / ".forge"

        def inside_staging(t: Path) -> tuple[Path, bool]:
            inside.mkdir(parents=True, exist_ok=True)
            import tempfile as _tempfile

            return Path(_tempfile.mkdtemp(dir=inside, prefix="restore-")), False

        monkeypatch.setattr(cp, "_staging_dir", inside_staging)

        report = self._restore(tmp_path, target, manifest, blobs)

        assert report.ok is True
        assert report.phase == "completed"
        assert (target / "a.txt").read_bytes() == self.NEW_A
        assert (target / "b.txt").read_bytes() == self.NEW_B
        assert not (target / "doomed.txt").exists()
        assert (target / "untracked.txt").read_bytes() == b"rides along\n"


class TestBoundedCaptureScope:
    """R28-04: the capture scope is the agent's work tree only, the delta
    is bounded by ONE canonical digest scheme, and repeated captures
    cannot grow from self-capture."""

    def test_runtime_dirs_caches_and_the_store_are_never_captured(self, tmp_path: Path):
        tree = _wip_tree(tmp_path / "runner-a")
        # The store itself, caches, egg-info and node_modules inside the tree:
        (tree / ".forge" / "checkpoints" / "ab").mkdir(parents=True)
        (tree / ".forge" / "checkpoints" / "ab" / ("c" * 64)).write_bytes(b"self capture\n")
        (tree / "src" / "__pycache__").mkdir()
        (tree / "src" / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00pyc")
        (tree / ".pytest_cache").mkdir()
        (tree / ".pytest_cache" / "v").write_bytes(b"cache")
        (tree / "node_modules").mkdir()
        (tree / "node_modules" / "dep.js").write_bytes(b"dep")
        (tree / "lib.egg-info").mkdir()
        (tree / "lib.egg-info" / "meta").write_bytes(b"meta")
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)

        receipt = capture_wip(
            work_id=WORK_ID,
            root=tree,
            store=store,
            tracked_baseline=_baseline(),
            sequence=1,
        )

        manifest = json.loads(store.get(receipt.artifact_id))
        assert sorted(manifest["files"]) == ["notes.md", "scripts/run.sh", "src/app.py"]
        assert not any(
            rel.startswith((".forge/", "node_modules/", ".pytest_cache/"))
            or "__pycache__" in rel
            or ".egg-info" in rel
            for rel in manifest["files"]
        )

    def test_repeated_capture_is_stable_and_never_grows_from_self_capture(self, tmp_path: Path):
        """Capture twice with the store INSIDE the walked tree (the lane's
        real layout): the second manifest is byte-identical — the store
        never captures itself."""
        tree = _wip_tree(tmp_path / "runner-a")
        inside = ContentAddressedStore(tree / ".forge" / "checkpoints", tenant=TENANT)

        first = capture_wip(
            work_id=WORK_ID, root=tree, store=inside, tracked_baseline=_baseline(), sequence=1
        )
        second = capture_wip(
            work_id=WORK_ID, root=tree, store=inside, tracked_baseline=_baseline(), sequence=1
        )

        assert first.artifact_id == second.artifact_id
        manifest = json.loads(inside.get(first.artifact_id))
        assert not any(rel.startswith(".forge/") for rel in manifest["files"])

    def test_a_git_oid_shaped_baseline_never_matches_so_nothing_is_skipped(self, tmp_path: Path):
        """The reviewed defect from the other side: a baseline carrying git
        blob OIDs (40-hex, different hash scheme) must not silently match
        anything — the capture is honest about what it carries — while
        the SAME digest scheme (raw sha256) skips unchanged files."""
        tree = _wip_tree(tmp_path / "runner-a")
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)

        git_shaped = {"src/app.py": "0" * 40, "README.md": "1" * 40, "src/old.py": "2" * 40}
        oid_receipt = capture_wip(
            work_id=WORK_ID, root=tree, store=store, tracked_baseline=git_shaped, sequence=1
        )
        oid_manifest = json.loads(store.get(oid_receipt.artifact_id))
        assert (
            "README.md" in oid_manifest["files"]
        )  # unchanged content NOT skipped under a foreign scheme

        canonical_receipt = capture_wip(
            work_id=WORK_ID, root=tree, store=store, tracked_baseline=_baseline(), sequence=2
        )
        canonical_manifest = json.loads(store.get(canonical_receipt.artifact_id))
        assert "README.md" not in canonical_manifest["files"]  # the canonical scheme skips it
        assert sorted(canonical_manifest["files"]) == ["notes.md", "scripts/run.sh", "src/app.py"]

    def test_a_mode_only_change_is_captured_exactly_once(self, tmp_path: Path):
        tree = _wip_tree(tmp_path / "runner-a")
        script = tree / "scripts" / "run.sh"
        script.chmod(0o644)  # content identical to the baseline, mode differs
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        baseline = {**_baseline(), "scripts/run.sh": _digest(_WIP_SCRIPT)}

        receipt = capture_wip(
            work_id=WORK_ID,
            root=tree,
            store=store,
            tracked_baseline=baseline,
            baseline_modes={
                "src/app.py": 0o644,
                "src/old.py": 0o644,
                "README.md": 0o644,
                "scripts/run.sh": 0o755,  # the baseline says executable; the tree says not
            },
            sequence=1,
        )

        manifest = json.loads(store.get(receipt.artifact_id))
        assert sorted(manifest["files"]) == ["notes.md", "scripts/run.sh", "src/app.py"]
        assert manifest["files"]["scripts/run.sh"]["mode"] == 0o644
        assert manifest["files"]["scripts/run.sh"]["role"] == "modified"

    def test_content_and_mode_unchanged_is_fully_skipped(self, tmp_path: Path):
        tree = _wip_tree(tmp_path / "runner-a")
        tree.joinpath("scripts/run.sh").chmod(0o755)  # matches the baseline mode too
        store = ContentAddressedStore(tmp_path / "store", tenant=TENANT)
        baseline = {**_baseline(), "scripts/run.sh": _digest(_WIP_SCRIPT)}

        receipt = capture_wip(
            work_id=WORK_ID,
            root=tree,
            store=store,
            tracked_baseline=baseline,
            baseline_modes={
                "src/app.py": 0o644,
                "src/old.py": 0o644,
                "README.md": 0o644,
                "scripts/run.sh": 0o755,
            },
            sequence=1,
        )

        manifest = json.loads(store.get(receipt.artifact_id))
        assert sorted(manifest["files"]) == ["notes.md", "src/app.py"]  # run.sh fully unchanged


class TestReferenceAwareRetention:
    def test_prune_cannot_delete_an_active_pause_checkpoint(self, tmp_path: Path):
        _tree, _store, receipt = _capture(tmp_path)
        _backdate_store(tmp_path / "store")  # everything is now "old"

        # A FRESH process: the retention references came from the persisted
        # metadata, not from the dead capture process's memory.
        fresh = ContentAddressedStore(tmp_path / "store", tenant=TENANT)

        removed = fresh.prune(older_than=timedelta(hours=1))

        assert removed == 0
        assert fresh.get_verified(receipt.artifact_id, principal=TENANT) is not None
        assert fresh.resolve(_digest(_WIP_APP)) is True

    def test_releasing_the_reference_reenables_collection(self, tmp_path: Path):
        _tree, _store, receipt = _capture(tmp_path)
        _backdate_store(tmp_path / "store")
        fresh = ContentAddressedStore(tmp_path / "store", tenant=TENANT)

        released = fresh.release_references(f"checkpoint:{WORK_ID}")
        removed = fresh.prune(older_than=timedelta(hours=1))

        assert released >= 4  # the manifest + three blobs were pinned
        assert removed >= 4
        assert fresh.resolve(receipt.artifact_id) is False

    def test_grants_persist_across_processes_and_merge_per_tenant(self, tmp_path: Path):
        root = tmp_path / "store"
        first = ContentAddressedStore(root, tenant="tenant-a")
        digest = first.put(b"shared-evidence")

        second = ContentAddressedStore(root, tenant="tenant-b")
        assert second.put(b"shared-evidence") == digest  # same address, no rewrite

        # Both tenants' grants survive a restart — identical bytes, segregated reads.
        third = ContentAddressedStore(root, tenant="tenant-c")
        assert third.get(digest, tenant="tenant-a") == b"shared-evidence"
        assert third.get(digest, tenant="tenant-b") == b"shared-evidence"
        assert third.get_verified(digest, principal="tenant-a") == b"shared-evidence"
        assert third.get(digest, tenant="tenant-c") is None


class TestResumeUnderAFreshEpoch:
    def _paused(self, tmp_path: Path) -> tuple[PauseState, ContentAddressedStore, Path]:
        _tree, store, receipt = _capture(tmp_path)
        state = PauseState(
            work_id=WORK_ID,
            last_applied_command_sequence=7,
            checkpoint_receipt=receipt,
            pause_status="paused",
        )
        return state, store, tmp_path

    def test_current_authorization_and_verified_bytes_resume_under_a_fresh_epoch(
        self, tmp_path: Path
    ):
        state, store, tmp = self._paused(tmp_path)

        outcome = resume_from_checkpoint(
            state,
            store=store,
            principal=TENANT,
            authorization=lambda: (True, "operator scope still granted"),
            attempt_id="attempt-2",
            prior_epoch=4,
        )

        assert outcome.ok is True
        assert outcome.checkpoint_verified is True
        assert outcome.epoch is not None
        assert outcome.epoch["execution_epoch"] == 5  # ALWAYS prior + 1
        assert outcome.epoch["reconstruction"] == "durable_artifacts"
        # The stale epoch's artifacts cannot act in the new one (NXT-18).
        assert accepts_epoch(4, outcome.epoch["execution_epoch"]) is False
        assert accepts_epoch(5, outcome.epoch["execution_epoch"]) is True
        assert accepts_epoch(None, outcome.epoch["execution_epoch"]) is False

    def test_revoked_authorization_blocks_resume_even_if_valid_before_pause(self, tmp_path: Path):
        state, store, tmp = self._paused(tmp_path)

        outcome = resume_from_checkpoint(
            state,
            store=store,
            principal=TENANT,
            authorization=lambda: (False, "operator scope revoked during the pause"),
            attempt_id="attempt-2",
            prior_epoch=4,
        )

        assert outcome.ok is False
        assert outcome.epoch is None  # no epoch was spent
        assert "revoked" in outcome.reason

    def test_expired_checkpoint_bytes_block_resume(self, tmp_path: Path):
        state, store, tmp = self._paused(tmp_path)
        artifact = tmp / "store" / state.checkpoint_receipt.artifact_id[:2]
        (artifact / state.checkpoint_receipt.artifact_id).unlink()  # GC'd away

        outcome = resume_from_checkpoint(
            state,
            store=ContentAddressedStore(tmp / "store", tenant=TENANT),
            principal=TENANT,
            authorization=lambda: (True, "ok"),
            attempt_id="attempt-2",
            prior_epoch=4,
        )

        assert outcome.ok is False
        assert "no longer resolves" in outcome.reason

    def test_a_partial_pause_has_no_durable_checkpoint_to_resume_from(self, tmp_path: Path):
        state = PauseState(work_id=WORK_ID, pause_status="paused_partial")

        outcome = resume_from_checkpoint(
            state,
            store=ContentAddressedStore(tmp_path / "store", tenant=TENANT),
            principal=TENANT,
            authorization=lambda: (True, "ok"),
            attempt_id="attempt-2",
            prior_epoch=1,
        )

        assert outcome.ok is False
        assert "no verified durable checkpoint" in outcome.reason
