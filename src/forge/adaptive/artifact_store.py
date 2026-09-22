"""Immutable evidence and workspace-checkpoint persistence (FND-05).

Why content addressing: a checkpoint must keep resolving after the runner
that produced it is destroyed, and two tenants may independently upload
byte-identical evidence. Addressing an artifact by its sha256 makes the
digest both the integrity proof and the address — an artifact can never
silently change under a digest that was already approved, cited in a
contract, or bound into a :class:`forge.adaptive.models.Checkpoint`. A
second ``put`` of the same bytes is a no-op by construction.

Why a grant table: content addressing deduplicates *across* tenants, so
the file's existence alone cannot answer "may this tenant read it".
Cross-tenant digest equality must not grant access; the grant map
(digest -> tenants that put it) restores that boundary. Grants and the
immutable content types are PERSISTED beside the blobs (NXT-16, a JSON
metadata document under the store root, merged on every write) — a new
process over the same root reads its own checkpoint back, instead of a
fresh instance granting nothing it did not see written. A production
deployment moves the same document into Postgres (FND-05 scope item 2);
the shape here is fixed so that swap is a storage detail.

Why verified reads: the address promises integrity, so a TRUSTED read
(:meth:`ContentAddressedStore.get_verified`) recomputes the digest of
the bytes it is about to return and quarantines content whose bytes no
longer match their own address — tampered bytes are refused with
evidence, never returned. The plain :meth:`ContentAddressedStore.get`
is the isolated administrative/forensic view (it returns what is on
disk, byte for byte); the checkpoint paths never use it.

Why reference-aware retention: age alone must not delete the checkpoint
of an actively paused work. Captures register REFERENCES (labels such as
``checkpoint:<work_id>``) for every blob and the manifest itself; the
references are persisted with the grants, and :meth:`ContentAddressedStore.prune`
refuses to remove a digest that still carries one. Releasing the label
(a resume, a final cancel) is what makes the blobs collectable again.

Why prune is silent: an expired artifact leaves the "does it still
resolve" question to the caller (:meth:`ContentAddressedStore.resolve`)
— the explicit, recoverable state the review asks for. A stale
checkpoint reports missing evidence instead of resurrecting it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath

#: The address space of the store: lowercase 64-hex sha256.
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: The persisted metadata document: grants, content types and retention
#: references, written atomically (temp file + rename) beside the blobs.
#: The name is a FILE at the store root, so the fan-out shard walk in
#: :meth:`ContentAddressedStore.prune` (which only descends directories)
#: never mistakes it for an artifact shard.
_METADATA_NAME = "_metadata.json"

#: Largest size one archive member may declare (checked against the zip's
#: central directory BEFORE any extraction — the declared size is attacker
#: input, the point is to refuse, not to discover mid-stream).
MAX_ARCHIVE_ENTRY_BYTES = 256 * 1024 * 1024


def _address(root: Path, digest: str) -> Path:
    """The fan-out layout ``root/<first2>/<digest>`` — the digest IS the address."""
    return root / digest[:2] / digest


class CorruptArtifactError(Exception):
    """Bytes under a content address no longer hash to that address (NXT-16).

    Raised by :meth:`ContentAddressedStore.get_verified` after the
    content has been QUARANTINED (moved aside under a ``.corrupt``
    sibling name — the bytes are kept as evidence, but the address stops
    serving them). The message names the address and both digests, so a
    failed restore can say exactly which blob rotted.
    """

    def __init__(self, digest: str, actual: str) -> None:
        super().__init__(
            f"artifact {digest} is corrupt: bytes hash to {actual or '<empty>'}; "
            "the content was quarantined and must be re-uploaded, never served"
        )
        self.digest = digest
        self.actual = actual


class ContentAddressedStore:
    """A content-addressed, per-tenant-granted, prunable artifact store."""

    def __init__(self, root: Path, *, tenant: str, max_bytes: int = 512 * 1024 * 1024) -> None:
        self._root = Path(root)
        self._tenant = tenant
        self._max_bytes = max_bytes
        #: digest -> tenants that put it. Seeded from the persisted
        #: metadata document when one exists, so a NEW process over the
        #: same root reads back what earlier processes wrote (NXT-16).
        self._grants: dict[str, set[str]] = {}
        #: digest -> content type of the first write (immutable like the bytes).
        self._content_types: dict[str, str] = {}
        #: digest -> retention reference labels (e.g. ``checkpoint:wp-1``).
        #: A digest with a live reference is exempt from :meth:`prune`.
        self._references: dict[str, set[str]] = {}
        self._root.mkdir(parents=True, exist_ok=True)
        self._apply_metadata(self._read_metadata())

    @property
    def tenant(self) -> str:
        return self._tenant

    def put(self, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        """Store *data* under its sha256 address and return the digest.

        Idempotent and immutable: when the address already exists the bytes
        are left untouched (the digest already vouches for them), so a
        concurrent or replayed upload cannot rewrite approved evidence.
        The grant and the content type are persisted to the metadata
        document after the blob lands — a crash between the two leaves an
        orphan blob (harmless, collectable) rather than a grant for bytes
        that were never written.
        """
        if len(data) > self._max_bytes:
            raise ValueError(
                f"artifact is {len(data)} bytes; this store accepts at most {self._max_bytes}"
            )
        digest = hashlib.sha256(data).hexdigest()
        target = _address(self._root, digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write to a sibling temp file, then rename into place: a crash
            # must never leave a half-written artifact under an address whose
            # digest promises integrity.
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp_name, target)
            except BaseException:
                os.unlink(tmp_name)
                raise
        self._grants.setdefault(digest, set()).add(self._tenant)
        self._content_types.setdefault(digest, content_type)
        self._save_metadata()
        return digest

    def get(self, digest: str, *, tenant: str | None = None) -> bytes | None:
        """Read an artifact by address, or ``None`` when it is absent/unsigned.

        This is the ADMINISTRATIVE read: it returns whatever bytes sit
        under the address WITHOUT recomputing the digest — the forensic
        view an operator needs to see tampering, which is exactly why
        the trusted paths (:meth:`get_verified`,
        :mod:`forge.adaptive.checkpointing`) must not use it.

        ``tenant`` scoping: a get with an explicit tenant returns ``None``
        unless that tenant put this digest — identical bytes shared across
        tenants stay segregated. ``tenant=None`` (the default) is the
        isolated internal-trust path: the caller takes responsibility for
        the check (e.g. the store's own pruning, or a same-tenant worker
        replay).
        """
        if not _HEX64.fullmatch(digest):
            raise ValueError("digest must be a lowercase 64-hex sha256")
        if tenant is not None and tenant not in self._grants.get(digest, frozenset()):
            return None
        target = _address(self._root, digest)
        if not target.is_file():
            return None
        return target.read_bytes()

    def get_verified(self, digest: str, *, principal: str) -> bytes | None:
        """The TRUSTED read: grant-checked, digest-verified (NXT-16).

        Refuses twice before any bytes are trusted: a ``principal`` that
        was never granted this digest gets ``None`` — indistinguishable
        from absence, so knowing the address probes nothing — and bytes
        whose sha256 does not reproduce the address raise
        :class:`CorruptArtifactError` after being quarantined aside (the
        tampered content is never returned, and the address stops
        resolving instead of silently serving a lie). Absent content is
        the caller's explicit ``None``.
        """
        if not _HEX64.fullmatch(digest):
            raise ValueError("digest must be a lowercase 64-hex sha256")
        if principal not in self._grants.get(digest, frozenset()):
            return None
        target = _address(self._root, digest)
        if not target.is_file():
            return None
        data = target.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            quarantine = target.with_name(f"{digest}.corrupt")
            quarantine.unlink(missing_ok=True)
            os.replace(target, quarantine)
            raise CorruptArtifactError(digest, actual)
        return data

    def resolve(self, digest: str, *, tenant: str | None = None) -> bool:
        """Does the artifact exist? The post-prune probe callers rely on.

        With an explicit ``tenant`` the probe is grant-scoped: an
        ungranted tenant learns nothing, not even existence. Without one
        it is the administrative existence check the store's own
        bookkeeping uses. A malformed address is never a resolvable
        artifact; failing closed keeps callers' resolve() checks total.
        """
        if not _HEX64.fullmatch(digest):
            return False
        if tenant is not None and tenant not in self._grants.get(digest, frozenset()):
            return False
        return _address(self._root, digest).is_file()

    def add_reference(self, digest: str, label: str, *more_labels: str) -> None:
        """Pin *digest* against age-based retention under the given labels.

        References are persisted with the grants, so the pin survives a
        process restart — the checkpoint of an actively paused work stays
        collectable only once its label is RELEASED
        (:meth:`release_references`), never because it grew old.
        """
        if not _HEX64.fullmatch(digest):
            raise ValueError("digest must be a lowercase 64-hex sha256")
        labels = self._references.setdefault(digest, set())
        labels.add(label)
        labels.update(more_labels)
        self._save_metadata()

    def release_references(self, label: str) -> int:
        """Drop *label* from every digest that carries it; return how many.

        The resume/cancel side of reference-aware retention: releasing
        the ``checkpoint:<work_id>`` label is what re-enables collection
        of that work's blobs — prune still applies its age rule to them.
        A cleared digest KEEPS its (now empty) entry in the live map, so
        the next metadata save treats this process's release as
        authoritative over a stale on-disk document.
        """
        released = 0
        for labels in self._references.values():
            if label in labels:
                labels.discard(label)
                released += 1
        if released:
            self._save_metadata()
        return released

    def references(self, digest: str) -> tuple[str, ...]:
        """The live retention labels on *digest* (evidence for prune decisions)."""
        return tuple(sorted(self._references.get(digest, ())))

    def prune(self, older_than: timedelta) -> int:
        """Delete artifacts whose mtime precedes ``now - older_than``.

        Reference-aware (NXT-16): a digest that still carries a live
        retention label is KEPT however old it is — the age rule only
        ever releases unreferenced content. Returns how many were
        removed. Removal is quiet by design: callers discover expired
        evidence through :meth:`resolve` / :meth:`get` returning absent,
        which is the explicit recoverable state. Grants for a removed
        digest go with it — the content is gone, so no tenant retains a
        read that would now return ``None`` anyway.
        """
        cutoff = time.time() - older_than.total_seconds()
        removed = 0
        removed_digests: set[str] = set()
        for shard in sorted(self._root.iterdir()):
            if not shard.is_dir():
                continue
            for artifact in sorted(shard.iterdir()):
                if artifact.name.endswith(".corrupt"):
                    # Quarantined content is evidence, not a live artifact;
                    # it ages out with the same clock, references or not.
                    if artifact.stat().st_mtime >= cutoff:
                        continue
                    artifact.unlink(missing_ok=True)
                    removed += 1
                    continue
                if artifact.stat().st_mtime >= cutoff:
                    continue
                if self._references.get(artifact.name):
                    continue  # a live reference outlives the age rule
                artifact.unlink(missing_ok=True)
                self._grants.pop(artifact.name, None)
                self._content_types.pop(artifact.name, None)
                self._references.pop(artifact.name, None)
                removed_digests.add(artifact.name)
                removed += 1
            if not any(shard.iterdir()):
                shard.rmdir()
        if removed:
            self._save_metadata(drop=frozenset(removed_digests))
        return removed

    # -- persisted metadata (NXT-16) ------------------------------------------

    def _metadata_path(self) -> Path:
        return self._root / _METADATA_NAME

    def _read_metadata(self) -> dict:
        """Read the persisted grants/content-types/references document.

        PURE: parses and returns the document without touching live
        state (the save path re-reads it to merge, so reading must not
        mutate). A missing or unreadable document is an EMPTY one — the
        blobs on disk stay addressable to the admin paths, and the store
        simply grants nothing it did not see written.
        """
        path = self._metadata_path()
        if not path.is_file():
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}

    def _apply_metadata(self, document: dict) -> None:
        """Seed the LIVE grant/content-type/reference maps from *document*.

        Used at construction, where the live maps are empty: a NEW
        process over the same root reads back what earlier processes
        wrote (NXT-16).
        """
        artifacts = document.get("artifacts")
        if isinstance(artifacts, dict):
            for digest, meta in artifacts.items():
                if not isinstance(meta, dict):
                    continue
                tenants = meta.get("tenants")
                if isinstance(tenants, list):
                    grants = self._grants.setdefault(digest, set())
                    grants.update(tenant for tenant in tenants if isinstance(tenant, str))
                content_type = meta.get("content_type")
                if isinstance(content_type, str):
                    self._content_types.setdefault(digest, content_type)
        references = document.get("references")
        if isinstance(references, dict):
            for digest, labels in references.items():
                if isinstance(labels, list):
                    refs = self._references.setdefault(digest, set())
                    refs.update(label for label in labels if isinstance(label, str))

    def _save_metadata(self, *, drop: frozenset[str] = frozenset()) -> None:
        """Persist grants/content types/references atomically, merged.

        Merge rules, per section: GRANTS union this process's view with
        the on-disk document (sequential writers never erase each
        other's tenants; concurrent identical uploads preserve metadata
        and per-tenant grants). REFERENCES are memory-authoritative for
        any digest this process knows — a RELEASED label must win over
        the stale document still carrying it — and adopt only digests
        this process never touched. Digests in *drop* (just pruned) are
        excluded from both: a deletion must win over a stale document
        that still lists them. Two processes writing the SAME instant
        can still interleave last-writer-wins on the union — the
        production answer is the Postgres metadata table (NXT-16 scope
        item 1); this document is the same shape, durable for restarts,
        honest about the residual race.
        """
        disk = self._read_metadata()
        disk_artifacts = disk.get("artifacts") if isinstance(disk.get("artifacts"), dict) else {}
        disk_references = disk.get("references") if isinstance(disk.get("references"), dict) else {}

        artifacts: dict[str, dict[str, object]] = {}
        for digest in sorted(set(self._grants) | set(disk_artifacts)):
            if digest in drop:
                continue
            live = self._grants.get(digest, frozenset())
            stored = disk_artifacts.get(digest, {})
            stored_tenants = stored.get("tenants", []) if isinstance(stored, dict) else []
            tenants = sorted(set(live) | {t for t in stored_tenants if isinstance(t, str)})
            if not tenants:
                continue  # fully pruned: the grant went with the bytes
            content_type = (
                self._content_types.get(digest)
                or (stored.get("content_type") if isinstance(stored, dict) else None)
                or "application/octet-stream"
            )
            artifacts[digest] = {"content_type": content_type, "tenants": tenants}

        references: dict[str, list[str]] = {}
        for digest in sorted(set(self._references) | set(disk_references)):
            if digest in drop:
                continue
            if digest in self._references:
                labels = sorted(self._references[digest])
            else:
                stored = disk_references.get(digest, [])
                labels = sorted({label for label in stored if isinstance(label, str)})
            if labels:
                references[digest] = labels

        document = {"version": 1, "artifacts": artifacts, "references": references}
        path = self._metadata_path()
        fd, tmp_name = tempfile.mkstemp(dir=self._root, prefix=".tmp-metadata-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise


def validate_archive(path: Path) -> list[str]:
    """Return every violation found in the zip at *path* — BEFORE extraction.

    A checkpoint bundle is untrusted input until proven otherwise: path
    traversal (absolute names or ``..`` components), symlink entries (the
    external_attr unix mode says ``S_IFLNK``), and members declaring more
    than :data:`MAX_ARCHIVE_ENTRY_BYTES` are all decided from the central
    directory alone — nothing is ever extracted to find out. Backslashes
    are normalized before the traversal check because extraction tools on
    Windows treat them as separators too.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        return [f"not a zip archive: {exc}"]

    violations: list[str] = []
    for info in infos:
        name = info.filename
        normalized = name.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if normalized.startswith("/") or ".." in parts:
            violations.append(f"{name}: escapes the archive (absolute path or '..' component)")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            violations.append(f"{name}: symlink entry")
        if info.file_size > MAX_ARCHIVE_ENTRY_BYTES:
            violations.append(
                f"{name}: declares {info.file_size} bytes "
                f"(over the {MAX_ARCHIVE_ENTRY_BYTES}-byte member limit)"
            )
    return violations


def wip_manifest(
    tracked: dict[str, str],
    untracked: list[str],
    deletions: list[str],
    source_oids: dict[str, str],
) -> dict:
    """Build the portable work-in-progress manifest (schema ``forge.wip.manifest/1``).

    Everything a revived runner needs to reconstruct the workspace state:
    tracked changes (path -> blob digest), untracked files, deletions, and
    the source OIDs the WIP applies on top of. Inputs are COPIED — once a
    manifest is frozen into a checkpoint it must not shift under the caller
    that keeps mutating its working views.
    """
    return {
        "schema": "forge.wip.manifest/1",
        "tracked": dict(tracked),
        "untracked": list(untracked),
        "deletions": list(deletions),
        "source_oids": dict(source_oids),
    }
