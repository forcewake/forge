"""R38-03 (#304) — the public-artifact confidentiality gate.

``scripts/gate_public_artifacts.py`` refuses tracked database archives
(PostgreSQL custom-format magic, pg_dump extensions, archives CONTAINING
dump members) outside the reviewed synthetic-fixture allowlist, and
validates the sanitized backup receipts (digest + verdict + reference per
artifact). ``scripts/align_lab.py`` must default its backup destination to
private storage and refuse an in-repo destination. Everything here runs
against synthetic bytes and a throwaway git repo — no real dump is ever
created or committed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.align_lab import AlignmentError, build_plan, private_backup_base, resolve_backup_dir
from scripts.gate_public_artifacts import (
    ALLOWLIST_STAMP,
    VERDICTS,
    load_allowlist,
    main,
    scan,
    scan_paths,
    validate_backup_receipts,
)
from tests.test_align_lab import FakeLabRunner  # the podman boundary stub

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A minimal VALID receipt — mutated by the negative tests.
RECEIPT = {
    "filename": "pre-r3708-alignment-20260924T114520Z.dump",
    "created_utc": "2026-09-24T11:45:20Z",
    "size_bytes": 204769,
    "sha256": "b384e96a5b1d7e925ea83740dc1527e23fc95090e51085199871d3662230b0eb",
    "format": "postgresql-custom-dump (PGDMP magic, v1.16)",
    "schema_version": "alembic-026",
    "restore_test": "ok",
    "classification": "sensitive_content",
    "classification_basis": "credential scan negative; internal operational text present",
    "private_reference": "forge-private-2026-09-24-01",
    "alignment_receipt": "r3708-0c76a2e9ab46",
}


def receipts_document(entries: list[dict[str, Any]]) -> str:
    return "# receipts\n\n```json\n" + json.dumps(entries, indent=2) + "\n```\n"


def write_fixture(root: Path, relative: str, payload: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def allowlist_with(root: Path, entries: list[dict[str, str]]) -> Path:
    path = root / "allowlist.json"
    path.write_text(
        json.dumps({"stamp": ALLOWLIST_STAMP, "entries": entries}, indent=2), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# detections: magic, extensions, nested archives
# ---------------------------------------------------------------------------


def test_pgdmp_magic_is_refused_regardless_of_extension(tmp_path: Path) -> None:
    # a PGDMP header hidden behind an innocent .bin name is still a dump
    write_fixture(tmp_path, "docs/evidence/innocent.bin", b"PGDMP\x01\x0f\x00")
    violations = scan_paths(["docs/evidence/innocent.bin"], tmp_path)
    assert len(violations) == 1
    assert "docs/evidence/innocent.bin" in violations[0]
    assert "PGDMP" in violations[0]


def test_pgdump_extensions_are_refused_even_when_empty(tmp_path: Path) -> None:
    for name in ("lab.dump", "older.backup"):
        write_fixture(tmp_path, name, b"not really a dump")
    violations = scan_paths(["lab.dump", "older.backup"], tmp_path)
    assert {v.split(":")[0] for v in violations} == {"lab.dump", "older.backup"}
    assert any(".dump" in v for v in violations)
    assert any(".backup" in v for v in violations)


def test_zip_and_tar_containing_dump_members_are_refused(tmp_path: Path) -> None:
    import io
    import tarfile
    import zipfile

    zip_path = write_fixture(tmp_path, "bundle.zip", b"")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("readme.txt", "evidence bundle")
        archive.writestr("nested/lab.dump", b"PGDMP")
    tar_path = write_fixture(tmp_path, "bundle.tar", b"")
    with tarfile.open(tar_path, "w") as archive:
        info = tarfile.TarInfo("docs/backup.backup")
        payload = b"PGDMP"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    violations = scan_paths(["bundle.zip", "bundle.tar"], tmp_path)
    by_file = {v.split(":")[0]: v for v in violations}
    assert "nested/lab.dump" in by_file["bundle.zip"]
    assert "docs/backup.backup" in by_file["bundle.tar"]


def test_clean_files_pass(tmp_path: Path) -> None:
    write_fixture(tmp_path, "README.md", b"# docs\n")
    write_fixture(tmp_path, "evidence.json", json.dumps({"spend": 0.8}).encode())
    # the STRING "PGDMP" inside text is fine — only the 5-byte header counts
    write_fixture(tmp_path, "trace.txt", b"analysis: the PGDMP magic is five bytes long")
    assert scan_paths(["README.md", "evidence.json", "trace.txt"], tmp_path) == []


# ---------------------------------------------------------------------------
# the allowlist: a synthetic fixture admitted ONLY when reviewed
# ---------------------------------------------------------------------------


def _fixture(root: Path) -> tuple[str, str]:
    import hashlib

    relative = "tests/fixtures/synthetic-lab.dump"
    payload = b"PGDMP\x01 synthetic fixture bytes"
    write_fixture(root, relative, payload)
    return relative, hashlib.sha256(payload).hexdigest()


def test_allowlist_admits_a_reviewed_synthetic_fixture(tmp_path: Path) -> None:
    relative, digest = _fixture(tmp_path)
    allowlist = allowlist_with(
        tmp_path, [{"path": relative, "sha256": digest, "reason": "synthetic, review #304"}]
    )
    loaded = load_allowlist(allowlist)
    assert scan_paths([relative], tmp_path, allowlist=loaded) == []


def test_allowlist_refuses_without_entry_or_with_stale_digest(tmp_path: Path) -> None:
    relative, digest = _fixture(tmp_path)
    # no allowlist at all -> refused
    assert len(scan_paths([relative], tmp_path)) == 1
    # listed, but the digest drifted (fixture edited since review) -> refused
    drifted = allowlist_with(
        tmp_path, [{"path": relative, "sha256": "0" * 64, "reason": "reviewed"}]
    )
    violations = scan_paths([relative], tmp_path, allowlist=load_allowlist(drifted))
    assert len(violations) == 1 and relative in violations[0]
    # listed with a matching digest but NO review reason -> refused
    reasonless = allowlist_with(tmp_path, [{"path": relative, "sha256": digest, "reason": "  "}])
    assert scan_paths([relative], tmp_path, allowlist=load_allowlist(reasonless)) != []


def test_committed_allowlist_is_wellformed_and_empty() -> None:
    document = load_allowlist(REPO_ROOT / "qualification" / "fixtures-allowlist.json")
    assert document["stamp"] == ALLOWLIST_STAMP
    assert document["entries"] == []  # nothing admitted by default


# ---------------------------------------------------------------------------
# the receipts document schema
# ---------------------------------------------------------------------------


def test_validator_accepts_a_complete_receipt() -> None:
    receipts, problems = validate_backup_receipts(receipts_document([RECEIPT]))
    assert problems == []
    assert receipts == [RECEIPT]


@pytest.mark.parametrize(
    "mutation",
    [
        # missing digest
        {"sha256": ""},
        # digest not 64-hex
        {"sha256": "not-hex"},
        # verdict outside the closed set ("safe" is not a verdict)
        {"classification": "safe"},
        # missing private reference
        {"private_reference": ""},
        # missing filename entirely
        {"filename": None},
        # nonsensical size
        {"size_bytes": 0},
    ],
)
def test_validator_refuses_incomplete_receipts(mutation: dict[str, Any]) -> None:
    entry = {**RECEIPT, **mutation}
    _, problems = validate_backup_receipts(receipts_document([entry]))
    assert problems, f"expected a problem for {mutation}"


def test_validator_refuses_missing_block_and_non_list() -> None:
    assert validate_backup_receipts("# no block here")[1] != []
    assert validate_backup_receipts('```json\n{"not": "a list"}\n```')[1] != []


def test_committed_receipts_readme_passes_validation() -> None:
    text = (
        REPO_ROOT
        / "docs"
        / "evaluation"
        / "2026-09-24-live-single-writer"
        / "backups"
        / "README.md"
    ).read_text(encoding="utf-8")
    receipts, problems = validate_backup_receipts(text)
    assert problems == []
    assert len(receipts) == 6  # one per removed dump
    assert {r["classification"] for r in receipts} <= VERDICTS


def test_receipt_referencing_a_tracked_backup_is_a_violation(tmp_path: Path) -> None:
    write_fixture(tmp_path, RECEIPT["filename"], b"PGDMP back again")
    violations = scan_paths(
        [RECEIPT["filename"]],
        tmp_path,
        receipts_text=receipts_document([RECEIPT]),
    )
    assert any("STILL TRACKED" in v for v in violations)


# ---------------------------------------------------------------------------
# end to end over a throwaway git repository
# ---------------------------------------------------------------------------


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "gate@test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "gate"], cwd=root, check=True)
    return root


def test_scan_over_a_real_git_checkout(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    write_fixture(root, "README.md", b"# repo\n")
    write_fixture(root, "docs/evidence/pre-align.dump", b"PGDMP\x01")
    allowlist = allowlist_with(root, [])
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "evidence"], cwd=root, check=True)

    violations = scan(root, allowlist_path=allowlist, receipts_path=root / "no-receipts.md")
    assert len(violations) == 1
    assert "docs/evidence/pre-align.dump" in violations[0]

    # admitting the synthetic dump through the reviewed allowlist clears it
    import hashlib

    digest = hashlib.sha256(b"PGDMP\x01").hexdigest()
    reviewed = allowlist_with(
        root,
        [{"path": "docs/evidence/pre-align.dump", "sha256": digest, "reason": "synthetic"}],
    )
    assert scan(root, allowlist_path=reviewed, receipts_path=root / "no-receipts.md") == []


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _git_repo(tmp_path)
    write_fixture(root, "clean.txt", b"fine")
    allowlist = allowlist_with(root, [])
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "clean"], cwd=root, check=True)
    assert main(["--root", str(root), "--allowlist", str(allowlist), "--receipts", "0"]) == 0

    write_fixture(root, "lab.dump", b"PGDMP")
    subprocess.run(["git", "add", "lab.dump"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "oops"], cwd=root, check=True)
    code = main(["--root", str(root), "--allowlist", str(allowlist), "--receipts", "0"])
    assert code == 1
    assert "lab.dump" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# align_lab: private backup default + in-repo refusal
# ---------------------------------------------------------------------------


def test_private_backup_base_env_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_PRIVATE_BACKUP_DIR", raising=False)
    assert str(private_backup_base({})).endswith("forge-private/backups")
    custom = private_backup_base({"FORGE_PRIVATE_BACKUP_DIR": "/vault/backups"})
    assert str(custom) == "/vault/backups"


def test_resolve_backup_dir_defaults_private_and_datekeyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("FORGE_PRIVATE_BACKUP_DIR", str(tmp_path / "vault"))
    resolved = resolve_backup_dir(repo, None)
    assert resolved.is_relative_to(tmp_path / "vault")
    assert resolved != tmp_path / "vault"  # a date subdirectory was appended
    assert not resolved.is_relative_to(repo)


def test_resolve_backup_dir_refuses_inside_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for candidate in (repo, repo / "docs" / "evaluation" / "x" / "backups"):
        with pytest.raises(AlignmentError, match="INSIDE the repository"):
            resolve_backup_dir(repo, candidate)
        with pytest.raises(AlignmentError, match="R38-03"):
            resolve_backup_dir(repo, candidate, env={})


def test_build_plan_backup_step_lands_in_private_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the fake lab tree tests/test_align_lab builds (version + alembic chain)
    repo = tmp_path / "repo"
    (repo / "src" / "forge").mkdir(parents=True)
    (repo / "src" / "forge" / "__init__.py").write_text(
        '__version__ = "0.36.0"\n', encoding="utf-8"
    )
    versions = repo / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "027_head.py").write_text(
        'revision = "027"\ndown_revision = "026"\n', encoding="utf-8"
    )
    vault = tmp_path / "vault"
    monkeypatch.setenv("FORGE_PRIVATE_BACKUP_DIR", str(vault))

    runner = FakeLabRunner()  # misaligned lab -> full plan including backup
    plan = build_plan(runner, repo)
    backup = next(step for step in plan.steps if step.name == "backup")
    copy_argv = backup.argv[1]  # ["cp", "<container>:/tmp/….dump", "<host target>"]
    target = Path(copy_argv[2])
    assert target.is_relative_to(vault)
    assert not target.is_relative_to(repo)
    # the receipted destination directory is prepared before the copy
    assert backup.prepare_dirs == [target.parent]

    # and the R37-08 defect itself — an explicit docs/ destination — refuses
    with pytest.raises(AlignmentError, match="INSIDE the repository"):
        build_plan(runner, repo, backup_dir=repo / "docs" / "evaluation" / "backups")
