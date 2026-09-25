"""R38-03 (#304) — refuse database archives in the public (tracked) tree.

The review finding: six operational PostgreSQL dumps of the real lab were
committed under ``docs/`` as evaluation evidence. Publishing unclassified
operational state is a confidentiality defect regardless of whether any
secret was inside (classification found none — see the sanitized receipts).
This gate makes the regression class impossible to re-land silently:

- scans the TRACKED files of a repository (``git ls-files``);
- refuses any database-archive artifact:

  - the PostgreSQL custom-format magic ``PGDMP`` in the file header,
  - the pg_dump file extensions ``*.dump`` / ``*.backup``,
  - a tar/zip archive CONTAINING ``*.dump`` / ``*.backup`` members;

- outside an explicit, reviewed allowlist
  (``qualification/fixtures-allowlist.json``): a synthetic fixture is
  admitted only when its path is listed, its sha256 matches and the entry
  carries a review reason — a stale digest refuses, not admits;
- validates the sanitized backup-receipts document (the fenced JSON block in
  the evaluation ``backups/README.md``): every receipt must carry a 64-hex
  ``sha256`` digest, a classification verdict from the closed set and a
  non-empty ``private_reference`` — and no receipt filename may still exist
  as a tracked file.

Exit 0 = the public surface is clean; exit 1 = violations (each message
names the offending file); exit 2 = operational failure (no git, unreadable
allowlist). Stdlib only — safe as a fast CI step next to lint.

Run from the repository root::

    uv run python scripts/gate_public_artifacts.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The reviewed synthetic-fixture allowlist (committed, human-reviewed).
DEFAULT_ALLOWLIST = REPO_ROOT / "qualification" / "fixtures-allowlist.json"

#: The sanitized receipts document whose schema this gate enforces.
DEFAULT_RECEIPTS = (
    REPO_ROOT / "docs" / "evaluation" / "2026-09-24-live-single-writer" / "backups" / "README.md"
)

#: Versioned stamp of the allowlist document.
ALLOWLIST_STAMP = "forge.gate.public-artifacts-allowlist/1"

#: PostgreSQL custom-format dumps start with these five bytes.
PGDMP_MAGIC = b"PGDMP"

#: pg_dump's conventional custom/directory-format file extensions.
DUMP_SUFFIXES = frozenset({".dump", ".backup"})

#: The closed classification-verdict set (R38-03): unknown stays BLOCKING.
VERDICTS = frozenset({"clean_of_credentials", "sensitive_content", "unknown"})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


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
# The receipts document schema
# ---------------------------------------------------------------------------

_REQUIRED_RECEIPT_FIELDS = (
    "filename",
    "created_utc",
    "size_bytes",
    "sha256",
    "format",
    "schema_version",
    "restore_test",
    "classification",
    "classification_basis",
    "private_reference",
    "alignment_receipt",
)


def parse_receipts_block(text: str) -> list[dict[str, Any]]:
    """The first fenced ```json block holding a list, else []."""
    match = re.search(r"```json\s*\n(.*?)```", text, flags=re.DOTALL)
    if match is None:
        return []
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def validate_backup_receipts(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Every receipt must carry digest + verdict + reference (and the rest).

    Returns (receipts, problems): a malformed receipts document is itself a
    violation — the public evidence surface must stay machine-checkable.
    """
    problems: list[str] = []
    receipts = parse_receipts_block(text)
    if not receipts:
        return [], ["receipts block missing or not a JSON list"]
    for index, entry in enumerate(receipts):
        if not isinstance(entry, dict):
            problems.append(f"receipt[{index}] is not an object")
            continue
        missing = [
            key
            for key in _REQUIRED_RECEIPT_FIELDS
            if entry.get(key) is None or not str(entry[key]).strip()
        ]
        if missing:
            problems.append(f"receipt[{index}] ({entry.get('filename', '?')}): missing {missing}")
            continue
        if not _HEX64.match(str(entry["sha256"])):
            problems.append(f"receipt[{index}] ({entry['filename']}): sha256 is not 64 hex chars")
        if entry["classification"] not in VERDICTS:
            problems.append(
                f"receipt[{index}] ({entry['filename']}): classification "
                f"{entry['classification']!r} not in {sorted(VERDICTS)}"
            )
        if not isinstance(entry["size_bytes"], int) or entry["size_bytes"] <= 0:
            problems.append(f"receipt[{index}] ({entry['filename']}): size_bytes must be positive")
    return receipts, problems


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def scan_paths(
    tracked: Sequence[str],
    root: Path,
    *,
    allowlist: dict[str, Any] | None = None,
    receipts_text: str | None = None,
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
    if receipts_text is not None:
        receipts, problems = validate_backup_receipts(receipts_text)
        tracked_names = {Path(name).name for name in tracked}
        for receipt in receipts:
            filename = str(receipt.get("filename", ""))
            if filename and filename in tracked_names:
                violations.append(
                    f"{filename}: receipt references a database backup that is STILL TRACKED"
                )
        violations.extend(f"receipts document: {problem}" for problem in problems)
    return violations


def scan(
    root: Path, *, allowlist_path: Path = DEFAULT_ALLOWLIST, receipts_path: Path | None = None
) -> list[str]:
    """The full gate: tracked files + the receipts document."""
    receipts_path = receipts_path if receipts_path is not None else DEFAULT_RECEIPTS
    receipts_text = (
        receipts_path.read_text(encoding="utf-8")
        if receipts_path and receipts_path.is_file()
        else None
    )
    return scan_paths(
        tracked_files(root),
        root,
        allowlist=load_allowlist(allowlist_path),
        receipts_text=receipts_text,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/gate_public_artifacts.py",
        description=(
            "R38-03 (#304): refuse tracked database-archive artifacts outside the "
            "reviewed allowlist, and validate the sanitized backup receipts."
        ),
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--receipts",
        type=Path,
        default=DEFAULT_RECEIPTS,
        help="the sanitized-receipts README to validate (0 disables the check)",
    )
    args = parser.parse_args(argv)

    try:
        violations = scan(
            args.root,
            allowlist_path=args.allowlist,
            receipts_path=args.receipts if str(args.receipts) != "0" else None,
        )
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
    print("gate_public_artifacts: ok — no tracked database archives; receipts valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
