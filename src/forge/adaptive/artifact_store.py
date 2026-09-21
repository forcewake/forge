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
(digest -> tenants that put it) restores that boundary. A production
deployment moves grants and metadata into Postgres (FND-05 scope item 2);
the in-memory dict here keeps the substrate runnable and the shape fixed.

Why prune is silent: an expired artifact leaves the "does it still
resolve" question to the caller (:meth:`ContentAddressedStore.resolve`)
— the explicit, recoverable state the review asks for. A stale
checkpoint reports missing evidence instead of resurrecting it.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath

#: The address space of the store: lowercase 64-hex sha256.
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: Largest size one archive member may declare (checked against the zip's
#: central directory BEFORE any extraction — the declared size is attacker
#: input, the point is to refuse, not to discover mid-stream).
MAX_ARCHIVE_ENTRY_BYTES = 256 * 1024 * 1024


def _address(root: Path, digest: str) -> Path:
    """The fan-out layout ``root/<first2>/<digest>`` — the digest IS the address."""
    return root / digest[:2] / digest


class ContentAddressedStore:
    """A content-addressed, per-tenant-granted, prunable artifact store."""

    def __init__(self, root: Path, *, tenant: str, max_bytes: int = 512 * 1024 * 1024) -> None:
        self._root = Path(root)
        self._tenant = tenant
        self._max_bytes = max_bytes
        #: digest -> tenants that put it. Grants live only as long as the
        #: process unless a caller persists them; a fresh instance over the
        #: same root therefore grants nothing it did not see written.
        self._grants: dict[str, set[str]] = {}
        #: digest -> content type of the first write (immutable like the bytes).
        self._content_types: dict[str, str] = {}
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def tenant(self) -> str:
        return self._tenant

    def put(self, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        """Store *data* under its sha256 address and return the digest.

        Idempotent and immutable: when the address already exists the bytes
        are left untouched (the digest already vouches for them), so a
        concurrent or replayed upload cannot rewrite approved evidence.
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
        return digest

    def get(self, digest: str, *, tenant: str | None = None) -> bytes | None:
        """Read an artifact by address, or ``None`` when it is absent/unsigned.

        ``tenant`` scoping: a get with an explicit tenant returns ``None``
        unless that tenant put this digest — identical bytes shared across
        tenants stay segregated. ``tenant=None`` (the default) is the
        internal-trust path: the caller takes responsibility for the check
        (e.g. the store's own pruning, or a same-tenant worker replay).
        """
        if not _HEX64.fullmatch(digest):
            raise ValueError("digest must be a lowercase 64-hex sha256")
        if tenant is not None and tenant not in self._grants.get(digest, frozenset()):
            return None
        target = _address(self._root, digest)
        if not target.is_file():
            return None
        return target.read_bytes()

    def resolve(self, digest: str) -> bool:
        """Does the artifact exist? The post-prune probe callers rely on."""
        if not _HEX64.fullmatch(digest):
            # A malformed address is never a resolvable artifact; failing
            # closed keeps callers' resolve() checks total.
            return False
        return _address(self._root, digest).is_file()

    def prune(self, older_than: timedelta) -> int:
        """Delete artifacts whose mtime precedes ``now - older_than``.

        Returns how many were removed. Removal is quiet by design: callers
        discover expired evidence through :meth:`resolve` / :meth:`get`
        returning absent, which is the explicit recoverable state. Grants
        for a removed digest go with it — the content is gone, so no tenant
        retains a read that would now return ``None`` anyway.
        """
        cutoff = time.time() - older_than.total_seconds()
        removed = 0
        for shard in sorted(self._root.iterdir()):
            if not shard.is_dir():
                continue
            for artifact in sorted(shard.iterdir()):
                if artifact.stat().st_mtime >= cutoff:
                    continue
                artifact.unlink(missing_ok=True)
                self._grants.pop(artifact.name, None)
                self._content_types.pop(artifact.name, None)
                removed += 1
            if not any(shard.iterdir()):
                shard.rmdir()
        return removed


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
