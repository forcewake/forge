"""Refuse tracked database archives outside the reviewed synthetic allowlist.

Detect PGDMP headers, .dump/.backup extensions and nested dump members in
ZIP/tar files. Admission requires an exact path, SHA-256 and review reason.
Operational backups belong in private storage. Stdlib-only CI guard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The reviewed synthetic-fixture allowlist (committed, human-reviewed).
DEFAULT_ALLOWLIST = REPO_ROOT / "scripts" / "public-artifact-fixtures.json"

#: Versioned stamp of the allowlist document.
ALLOWLIST_STAMP = "forge.gate.public-artifacts-allowlist/1"

#: PostgreSQL custom-format dumps start with these five bytes.
PGDMP_MAGIC = b"PGDMP"

#: pg_dump's conventional custom/directory-format file extensions.
DUMP_SUFFIXES = frozenset({".dump", ".backup"})

# ---------------------------------------------------------------------------
# What is tracked
# ---------------------------------------------------------------------------


def tracked_files(root: Path) -> list[str]:
    """The repository's tracked file paths (posix-relative), via git."""
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git ls-files failed in {root}: {completed.stderr.strip()[:200]}")
    return [name for name in completed.stdout.split("\0") if name]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# The allowlist: a synthetic fixture is admitted only when reviewed
# ---------------------------------------------------------------------------


def load_allowlist(path: Path) -> dict[str, Any]:
    """Load and shape-check the allowlist (missing file = empty list)."""
    if not path.is_file():
        return {"stamp": ALLOWLIST_STAMP, "entries": []}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("stamp") != ALLOWLIST_STAMP:
        raise ValueError(f"{path}: allowlist stamp must be {ALLOWLIST_STAMP}")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{path}: allowlist entries must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) for key in ("path", "sha256", "reason")
        ):
            raise ValueError(f"{path}: every allowlist entry needs path, sha256, reason")
    return document


def allowlist_admits(relative: str, digest: str, allowlist: dict[str, Any]) -> bool:
    """True only when path+digest match an entry that carries a review reason."""
    return any(
        entry["path"] == relative and entry["sha256"] == digest and entry["reason"].strip()
        for entry in allowlist.get("entries", [])
    )


# ---------------------------------------------------------------------------
# Database-archive detection
# ---------------------------------------------------------------------------


def archive_dump_members(path: Path) -> list[str]:
    """``*.dump``/``*.backup`` member names inside a zip/tar, if openable."""
    members: list[str] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [
                name for name in archive.namelist() if Path(name).suffix.lower() in DUMP_SUFFIXES
            ]
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:  # noqa: SIM115 - closed immediately
            members = [
                name for name in archive.getnames() if Path(name).suffix.lower() in DUMP_SUFFIXES
            ]
    return members


def database_archive_reasons(path: Path) -> list[str]:
    """Why this file is a database archive (empty = not one)."""
    reasons: list[str] = []
    if path.suffix.lower() in DUMP_SUFFIXES:
        reasons.append(f"pg_dump file extension {path.suffix}")
    try:
        with path.open("rb") as handle:
            if handle.read(len(PGDMP_MAGIC)) == PGDMP_MAGIC:
                reasons.append("PostgreSQL custom-format magic (PGDMP)")
    except OSError:
        reasons.append("unreadable (cannot classify)")
        return reasons
    try:
        members = archive_dump_members(path)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError):
        members = []
    if members:
        reasons.append(f"archive contains database dump members: {', '.join(sorted(members)[:5])}")
    return reasons


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def scan_paths(
    tracked: Sequence[str],
    root: Path,
    *,
    allowlist: dict[str, Any] | None = None,
) -> list[str]:
    """Violations over the given tracked (posix-relative) paths."""
    allowlist = allowlist if allowlist is not None else {"stamp": ALLOWLIST_STAMP, "entries": []}
    violations: list[str] = []
    for relative in tracked:
        path = root / relative
        if not path.is_file():
            continue
        reasons = database_archive_reasons(path)
        if not reasons:
            continue
        digest = sha256_of(path)
        if allowlist_admits(relative, digest, allowlist):
            continue
        violations.append(f"{relative}: " + "; ".join(reasons) + " (not in the reviewed allowlist)")
    return violations


def scan(root: Path, *, allowlist_path: Path = DEFAULT_ALLOWLIST) -> list[str]:
    """Check tracked files against the exact reviewed fixture exceptions."""
    return scan_paths(tracked_files(root), root, allowlist=load_allowlist(allowlist_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/gate_public_artifacts.py",
        description=("Refuse tracked database archives outside the reviewed synthetic allowlist."),
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    args = parser.parse_args(argv)

    try:
        violations = scan(args.root, allowlist_path=args.allowlist)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"gate_public_artifacts: FAILED: {exc}", file=sys.stderr)
        return 2
    for violation in violations:
        print(f"gate_public_artifacts: REFUSED: {violation}")
    if violations:
        print(
            f"gate_public_artifacts: {len(violations)} violation(s) — database archives "
            "belong in private storage with a sanitized receipt, not in the public tree",
            file=sys.stderr,
        )
        return 1
    print("gate_public_artifacts: ok — no tracked database archives")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
