"""FND-05 — content-addressed evidence: round-trips, tenant scoping, retention.

The store's three trust properties under test: the digest IS the address
(immutable, idempotent puts), identical bytes across tenants do not leak
read access (grants, not digests, authorize), and expiry is a quiet
deletion whose discovery is the caller's explicit ``resolve()``. Archive
validation refuses traversal/symlink/oversized members BEFORE extraction,
and the WIP manifest freezes the workspace state a checkpoint revives.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

from forge.adaptive.artifact_store import (
    ContentAddressedStore,
    validate_archive,
    wip_manifest,
)


def address(root: Path, digest: str) -> Path:
    """The fan-out layout the store promises: ``root/<first2>/<digest>``."""
    return root / digest[:2] / digest


class TestPutGet:
    def test_round_trip_stores_under_fanout_layout(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")

        digest = store.put(b"evidence-bytes", content_type="text/plain")

        assert digest == hashlib.sha256(b"evidence-bytes").hexdigest()
        assert address(tmp_path / "artifacts", digest).is_file()
        assert store.get(digest) == b"evidence-bytes"
        assert store.resolve(digest) is True

    def test_second_put_of_same_bytes_is_a_no_op(self, tmp_path: Path):
        """Immutability, proven hard: a re-put must not rewrite the bytes."""
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")
        digest = store.put(b"checkpoint-blob")
        # Tamper with whatever is under the address; a rewrite-happy put
        # would restore the original bytes — a no-op put leaves them be.
        address(tmp_path / "artifacts", digest).write_bytes(b"TAMPERED")

        again = store.put(b"checkpoint-blob")

        assert again == digest
        assert store.get(digest) == b"TAMPERED"

    def test_oversized_put_is_refused_and_leaves_nothing_behind(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a", max_bytes=16)

        with pytest.raises(ValueError, match="at most 16"):
            store.put(b"x" * 17)

        assert [p for p in (tmp_path / "artifacts").rglob("*") if p.is_file()] == []

    def test_put_of_exactly_max_bytes_is_accepted(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a", max_bytes=16)
        digest = store.put(b"x" * 16)
        assert store.resolve(digest) is True


class TestTenantScoping:
    def test_cross_tenant_digest_equality_grants_nothing(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")
        digest = store.put(b"tenant-a-secret")

        assert store.get(digest, tenant="tenant-b") is None  # B never put it
        assert store.get(digest, tenant="tenant-a") == b"tenant-a-secret"
        assert store.get(digest) == b"tenant-a-secret"  # tenant=None: internal trust

    def test_tenant_gains_access_only_by_putting_the_bytes(self, tmp_path: Path):
        root = tmp_path / "artifacts"
        store_a = ContentAddressedStore(root, tenant="tenant-a")
        digest = store_a.put(b"shared-bytes")

        store_b = ContentAddressedStore(root, tenant="tenant-b")
        assert store_b.get(digest, tenant="tenant-b") is None  # fresh instance: no grant yet
        assert store_b.put(b"shared-bytes") == digest  # same address, no rewrite

        assert store_b.get(digest, tenant="tenant-b") == b"shared-bytes"  # granted now
        # Grants are per-instance: A's view never learned of B's put.
        assert store_a.get(digest, tenant="tenant-b") is None


class TestDigestValidation:
    @pytest.mark.parametrize(
        "bad",
        ["", "shorthash", "a" * 63, "a" * 65, "A" * 64, "z" * 64, "../evil"],
    )
    def test_malformed_digest_is_rejected(self, tmp_path: Path, bad: str):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")
        with pytest.raises(ValueError, match="64-hex sha256"):
            store.get(bad)

    def test_resolve_fails_closed_on_malformed_digest(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")
        assert store.resolve("not-a-digest") is False

    def test_well_formed_but_absent_digest_is_none_not_an_error(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")
        assert store.get("a" * 64) is None
        assert store.resolve("a" * 64) is False


class TestPrune:
    def test_prune_removes_only_expired_artifacts_and_their_grants(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")
        old = store.put(b"old-evidence")
        fresh = store.put(b"fresh-evidence")
        two_hours_ago = time.time() - 7200
        os.utime(address(tmp_path / "artifacts", old), (two_hours_ago, two_hours_ago))

        removed = store.prune(older_than=timedelta(hours=1))

        assert removed == 1
        assert store.resolve(old) is False  # expired -> explicit absent state
        assert store.resolve(fresh) is True
        assert store.get(old, tenant="tenant-a") is None  # grant went with the bytes
        assert store.get(fresh) == b"fresh-evidence"
        # The emptied shard directory is cleaned up too.
        shard = hashlib.sha256(b"old-evidence").hexdigest()[:2]
        assert not (tmp_path / "artifacts" / shard).exists()

    def test_prune_with_nothing_expired_returns_zero(self, tmp_path: Path):
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")
        digest = store.put(b"keep-me")
        assert store.prune(older_than=timedelta(days=365)) == 0
        assert store.resolve(digest) is True


def nasty_zip() -> bytes:
    """One archive carrying every violation class validate_archive guards."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ok.txt", "perfectly fine")
        zf.writestr("../evil.txt", "traversal")
        zf.writestr("/etc/passwd", "absolute path")
        link = zipfile.ZipInfo("link.txt")
        link.external_attr = 0o120777 << 16  # S_IFLNK | rwxrwxrwx in the unix bits
        zf.writestr(link, "target.txt")
        big = zipfile.ZipInfo("big.bin")
        big.compress_type = zipfile.ZIP_DEFLATED
        with zf.open(big, "w") as dst:
            chunk = b"\0" * (8 * 1024 * 1024)
            for _ in range(38):  # 304 MiB declared; compresses to ~300 KiB
                dst.write(chunk)
    return buf.getvalue()


class TestValidateArchive:
    def test_clean_archive_has_no_violations(self, tmp_path: Path):
        path = tmp_path / "clean.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("notes.txt", "hi")
        assert validate_archive(path) == []

    def test_every_violation_class_is_flagged_before_extraction(self, tmp_path: Path):
        path = tmp_path / "nasty.zip"
        path.write_bytes(nasty_zip())

        violations = validate_archive(path)

        assert not any("ok.txt" in v for v in violations)  # the good member stays good
        assert any("../evil.txt" in v and "escapes the archive" in v for v in violations)
        assert any("/etc/passwd" in v and "escapes the archive" in v for v in violations)
        assert any("link.txt" in v and "symlink" in v for v in violations)
        assert any("big.bin" in v and "over the" in v for v in violations)

    def test_a_directory_entry_with_dotdot_is_also_traversal(self, tmp_path: Path):
        path = tmp_path / "dir.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("../evil/", "")
        assert any("../evil" in v for v in validate_archive(path))

    def test_not_a_zip_is_reported_not_raised(self, tmp_path: Path):
        path = tmp_path / "bogus.zip"
        path.write_bytes(b"this is not a zip")
        assert any("not a zip archive" in v for v in validate_archive(path))


class TestWipManifest:
    def test_manifest_shape_carries_every_workspace_section(self):
        manifest = wip_manifest(
            tracked={"src/app.py": "a" * 40},
            untracked=["scratch/notes.md"],
            deletions=["src/old.py"],
            source_oids={"repo-main": "b" * 40},
        )

        assert manifest == {
            "schema": "forge.wip.manifest/1",
            "tracked": {"src/app.py": "a" * 40},
            "untracked": ["scratch/notes.md"],
            "deletions": ["src/old.py"],
            "source_oids": {"repo-main": "b" * 40},
        }

    def test_manifest_is_isolated_from_later_input_mutation(self):
        tracked = {"src/app.py": "a" * 40}
        untracked = ["scratch/notes.md"]
        deletions = ["src/old.py"]
        source_oids = {"repo-main": "b" * 40}

        manifest = wip_manifest(tracked, untracked, deletions, source_oids)
        tracked["src/other.py"] = "c" * 40
        untracked.append("late.txt")
        deletions.append("late-del.py")
        source_oids["repo-late"] = "d" * 40

        assert manifest["tracked"] == {"src/app.py": "a" * 40}
        assert manifest["untracked"] == ["scratch/notes.md"]
        assert manifest["deletions"] == ["src/old.py"]
        assert manifest["source_oids"] == {"repo-main": "b" * 40}
