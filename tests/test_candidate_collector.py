"""The Q35-01 candidate collector — REAL Git, REAL pointer semantics.

Issue #238: a resumed lane restores its WIP into a stable SIBLING
generation (``.forge-workspace-gen-<id>/``) and chdir's the LANE process
into it; the shipped emit step's inline ``git add -A`` +
``git diff --cached`` ran in the ORIGINAL checkout, so the uploaded
candidate could be 0 bytes while the real work sat in the generation.
These tests build the exact layout :mod:`forge.lane_driver` /
:mod:`forge.adaptive.checkpointing` produce (full ``copytree`` of the
checkout — ``.git`` included — plus the checkout's
``.forge/workspace-generation`` pointer) with REAL ``git`` subprocesses,
no mocks, and pin the NEW contract:

- the collector captures the generation's edits the OLD commands miss
  (the fail-before/pass-after regression, with the old sequence kept as
  the documented negative arm);
- the original checkout and its pointer stay untouched;
- a forged or foreign pointer yields a typed refusal and ZERO artifacts;
- zero-change is a valid result DISTINGUISHABLE from a failed Git
  command;
- deletions, executable bits and nested untracked files collect; lane
  infrastructure never rides the diff;
- the ``harness_entry --collect-candidate`` CLI round-trips with honest
  exit codes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forge.candidate_collector import (
    CANDIDATE_DIFF_NAME,
    CollectionError,
    CollectionRefused,
    GenerationPointerMissing,
    collect_candidate,
)

WORK_ID = "run-123"
CHECKPOINT_ID = "ab12cd34ef56" + "0" * 52  # a 64-hex content address


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """One REAL git invocation (the collector's own discipline: explicit
    cwd, byte-honest exit codes)."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result


def make_checkout(parent: Path) -> tuple[Path, str]:
    """A checkout at its frozen base — the lane's starting shape.

    Mirrors the template's "Forge control directory" step (the local
    excludes for ``.forge/`` and the infrastructure paths), because the
    OLD emit sequence relied on exactly those excludes when it staged
    the untouched checkout.
    """
    checkout = parent / "workspace"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.email", "lane@example.com")
    _git(checkout, "config", "user.name", "forge lane")
    (checkout / "README.md").write_text("base readme\n")
    (checkout / "src").mkdir()
    (checkout / "src" / "app.py").write_text("print('base')\n")
    (checkout / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "frozen base")
    base_oid = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    exclude = checkout / ".git" / "info" / "exclude"
    exclude.write_text(
        exclude.read_text() + "\n.forge/\n.codegraph/\n__pycache__/\n*.pyc\n.venv/\nforge-output/\n"
    )
    return checkout, base_oid


def make_generation(
    checkout: Path, *, work_id: str = WORK_ID, checkpoint_id: str = CHECKPOINT_ID
) -> Path:
    """The lane's restore semantics (R32-01), reproduced exactly.

    ``_promote_to_generation`` builds the generation as a full
    ``copytree`` of the checkout (``.git`` included — that is why the
    frozen base resolves there) and writes the pointer BOTH into the
    generation and into the stable checkout.
    """
    generation = checkout.parent / f".forge-workspace-gen-{checkpoint_id[:12]}"
    shutil.copytree(checkout, generation, symlinks=True)
    pointer = {
        "schema": "forge.workspace-generation/1",
        "work_id": work_id,
        "checkpoint_id": checkpoint_id,
        "generation": generation.name,
        "generation_path": str(generation),
    }
    rendered = json.dumps(pointer, indent=2, sort_keys=True) + "\n"
    for root in (generation, checkout):
        target = root / ".forge" / "workspace-generation"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered)
    return generation


def write_pointer(checkout: Path, document: dict) -> None:
    """Drop a (possibly forged) pointer document into the checkout."""
    target = checkout / ".forge" / "workspace-generation"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


@pytest.fixture()
def resumed(tmp_path: Path) -> tuple[Path, Path, str]:
    """A resumed lane's disk shape: checkout at base + generation with
    the restored WIP (a modified tracked file) and the agent's new
    untracked file living IN the generation."""
    checkout, base_oid = make_checkout(tmp_path)
    generation = make_generation(checkout)
    (generation / "src" / "app.py").write_text("print('restored wip')\n")
    notes = generation / "notes"
    notes.mkdir()
    (notes / "new-file.md").write_text("agent edit\n")
    return checkout, generation, base_oid


# ---------------------------------------------------------------------------
# 1 + 10. The regression: the OLD sequence misses the generation, the
# collector captures it.
# ---------------------------------------------------------------------------


class TestTheRegression:
    def test_old_template_sequence_misses_the_generation_edits(self, resumed):
        """The NEGATIVE ARM (documents the defect): the shipped pre-Q35-01
        command sequence — run verbatim in the original checkout, with its
        own ``|| true`` — produces a diff that MISSES the generation's
        edits entirely. The new collector is what fixes this."""
        checkout, _generation, base_oid = resumed
        old_diff = checkout.parent / "old-arm.diff"
        # The exact old emit-step shape: cleanup + add + diff + || true.
        script = (
            "rm -rf .codegraph .venv __pycache__ .pytest_cache; "
            "find . -name '*.pyc' -delete 2>/dev/null; "
            "git add -A; "
            f"git diff --cached --binary --full-index {base_oid} > {old_diff} || true"
        )
        outcome = subprocess.run(
            ["/bin/bash", "-c", script], cwd=checkout, capture_output=True, text=True
        )
        assert outcome.returncode == 0, outcome.stderr
        assert old_diff.stat().st_size == 0, (
            "the OLD commands must not see the generation's edits — if they "
            "do, this fixture no longer reproduces the defect"
        )

    def test_collector_captures_modified_and_untracked_from_generation(self, resumed):
        """The fail-before/pass-after regression: the collector resolves the
        ACTIVE generation through the pointer and its diff contains BOTH
        the restored WIP (a modified tracked file) and the agent's new
        untracked file — the exact bytes the old sequence dropped."""
        checkout, generation, base_oid = resumed

        result = collect_candidate(checkout, WORK_ID, base_oid)

        assert result.source == "generation"
        assert result.zero_change is False
        assert Path(result.generation_path) == generation.resolve()
        assert result.resolved_work_id == WORK_ID
        assert result.checkpoint_id == CHECKPOINT_ID
        diff = result.diff_path.read_bytes()
        assert b"src/app.py" in diff
        assert b"print('restored wip')" in diff
        assert b"notes/new-file.md" in diff
        assert result.diff_digest == hashlib.sha256(diff).hexdigest()
        # The staged path contract emit-meta consumes is preserved.
        assert result.diff_path == checkout.resolve() / "forge-output" / CANDIDATE_DIFF_NAME


# ---------------------------------------------------------------------------
# 2. The original checkout stays unchanged.
# ---------------------------------------------------------------------------


class TestCheckoutIntegrity:
    def test_original_checkout_and_pointer_survive_collection(self, resumed):
        checkout, _generation, base_oid = resumed
        pointer_bytes = (checkout / ".forge" / "workspace-generation").read_bytes()
        app_before = (checkout / "src" / "app.py").read_bytes()

        collect_candidate(checkout, WORK_ID, base_oid)

        # The pointer is intact, byte for byte.
        assert (checkout / ".forge" / "workspace-generation").read_bytes() == pointer_bytes
        # Nothing was staged in the checkout (its index still matches HEAD).
        assert _git(checkout, "diff", "--cached", "--quiet").returncode == 0
        assert _git(checkout, "status", "--porcelain").stdout.strip() == ""
        # The checkout's tracked content never moved.
        assert (checkout / "src" / "app.py").read_bytes() == app_before


# ---------------------------------------------------------------------------
# 3 + 4. Forged / foreign pointers: refused, zero artifacts.
# ---------------------------------------------------------------------------


class TestPointerOwnership:
    @pytest.mark.parametrize(
        "document",
        [
            # An absolute path outside the sibling pattern as the NAME.
            {
                "schema": "forge.workspace-generation/1",
                "work_id": WORK_ID,
                "checkpoint_id": CHECKPOINT_ID,
                "generation": "/etc",
                "generation_path": "/etc",
            },
            # A traversal-shaped name.
            {
                "schema": "forge.workspace-generation/1",
                "work_id": WORK_ID,
                "checkpoint_id": CHECKPOINT_ID,
                "generation": "../../evil",
                "generation_path": "../../evil",
            },
            # An owned NAME but a FOREIGN absolute path riding beside it.
            {
                "schema": "forge.workspace-generation/1",
                "work_id": WORK_ID,
                "checkpoint_id": CHECKPOINT_ID,
                "generation": f".forge-workspace-gen-{CHECKPOINT_ID[:12]}",
                "generation_path": "/definitely/not/the/sibling",
            },
        ],
        ids=["absolute-name", "traversal-name", "foreign-absolute-path"],
    )
    def test_forged_pointer_is_refused_with_zero_artifacts(self, tmp_path, document):
        checkout, base_oid = make_checkout(tmp_path)
        make_generation(checkout)  # a real generation exists beside it
        write_pointer(checkout, document)

        with pytest.raises(CollectionRefused):
            collect_candidate(checkout, WORK_ID, base_oid)

        # ZERO publishable artifacts: no staging dir, no diff anywhere.
        assert not (checkout / "forge-output").exists()
        assert not list(tmp_path.glob("**/candidate.diff"))

    def test_foreign_work_id_is_refused(self, tmp_path):
        checkout, base_oid = make_checkout(tmp_path)
        make_generation(checkout, work_id="another-run-999")
        with pytest.raises(CollectionRefused, match="another-run-999"):
            collect_candidate(checkout, WORK_ID, base_oid)
        assert not (checkout / "forge-output").exists()

    def test_unknown_schema_and_junk_pointer_are_refused(self, tmp_path):
        checkout, base_oid = make_checkout(tmp_path)
        make_generation(checkout)
        target = checkout / ".forge" / "workspace-generation"
        target.write_text("not json at all")
        with pytest.raises(CollectionRefused):
            collect_candidate(checkout, WORK_ID, base_oid)
        target.write_text(json.dumps({"schema": "evil/1", "work_id": WORK_ID}))
        with pytest.raises(CollectionRefused):
            collect_candidate(checkout, WORK_ID, base_oid)


# ---------------------------------------------------------------------------
# 5. Missing pointer: typed error vs the fresh-run fallback.
# ---------------------------------------------------------------------------


class TestMissingPointer:
    def test_required_pointer_missing_is_a_typed_error(self, tmp_path):
        checkout, base_oid = make_checkout(tmp_path)
        with pytest.raises(GenerationPointerMissing):
            collect_candidate(checkout, WORK_ID, base_oid)
        assert not (checkout / "forge-output").exists()

    def test_allow_missing_collects_the_checkout_fresh_run(self, tmp_path):
        checkout, base_oid = make_checkout(tmp_path)
        # A fresh run's agent worked IN the checkout.
        (checkout / "src" / "app.py").write_text("print('fresh edit')\n")
        (checkout / "brand-new.txt").write_text("fresh file\n")

        result = collect_candidate(checkout, WORK_ID, base_oid, allow_missing_pointer=True)

        assert result.source == "checkout"
        assert result.zero_change is False
        assert Path(result.generation_path) == checkout.resolve()
        diff = result.diff_path.read_bytes()
        assert b"src/app.py" in diff
        assert b"brand-new.txt" in diff


# ---------------------------------------------------------------------------
# 6. Zero-change vs Git failure: distinguishable by TYPE, never by bytes.
# ---------------------------------------------------------------------------


class TestZeroChangeVersusGitFailure:
    def test_zero_change_generation_is_a_valid_empty_candidate(self, resumed):
        """A restored-but-untouched generation: exit 0, empty diff, the
        ``zero_change`` flag — a no-op turn, honestly reported."""
        checkout, generation, base_oid = resumed
        # Undo the fixture's edits: the generation returns to the base.
        _git(generation, "checkout", "--", ".")
        shutil.rmtree(generation / "notes")

        result = collect_candidate(checkout, WORK_ID, base_oid)

        assert result.zero_change is True
        assert result.diff_path.stat().st_size == 0
        assert result.diff_digest == hashlib.sha256(b"").hexdigest()

    def test_git_failure_is_a_typed_error_never_an_empty_candidate(self, resumed):
        checkout, _generation, _base_oid = resumed
        bogus_base = "deadbeef" + "0" * 32  # not in any object database

        with pytest.raises(CollectionError) as raised:
            collect_candidate(checkout, WORK_ID, bogus_base)

        # git's own stderr rides the typed error — the step log names the
        # real cause (the OLD step turned exactly this into 0 bytes via
        # ``|| true``).
        assert not isinstance(raised.value, GenerationPointerMissing)
        assert "deadbeef" in str(raised.value)
        assert raised.value.args[0].startswith("git diff")

    def test_non_repository_generation_is_refused_before_git_runs(self, tmp_path):
        checkout, base_oid = make_checkout(tmp_path)
        generation = make_generation(checkout)
        shutil.rmtree(generation / ".git")
        with pytest.raises(CollectionError, match="not a Git repository"):
            collect_candidate(checkout, WORK_ID, base_oid)
        assert not (checkout / "forge-output").exists()


# ---------------------------------------------------------------------------
# 7. Deletions, executable bits, nested untracked dirs — a REAL patch.
# ---------------------------------------------------------------------------


class TestChangeKinds:
    def test_deletions_execbit_and_nested_untracked_collect(self, tmp_path):
        checkout, base_oid = make_checkout(tmp_path)
        generation = make_generation(checkout)
        (generation / "README.md").unlink()  # a deletion
        (generation / "run.sh").chmod(0o755)  # an executable-bit change
        nested = generation / "docs" / "deep" / "nested"
        nested.mkdir(parents=True)
        (nested / "new.txt").write_text("nested content\n")

        result = collect_candidate(checkout, WORK_ID, base_oid)
        diff = result.diff_path.read_bytes()

        assert b"deleted file mode 100644" in diff or b"deleted file mode" in diff
        assert b"README.md" in diff
        assert b"old mode 100644" in diff and b"new mode 100755" in diff
        assert b"docs/deep/nested/new.txt" in diff

        # The collected bytes are a REAL applicable candidate: a pristine
        # checkout of the same base accepts the patch.
        pristine, pristine_base = make_checkout(tmp_path / "pristine")
        assert pristine_base  # same content base (content, not oid, binds apply)
        applied = subprocess.run(
            ["git", "-C", str(pristine), "apply", "--check", str(result.diff_path)],
            capture_output=True,
            text=True,
        )
        assert applied.returncode == 0, applied.stderr


# ---------------------------------------------------------------------------
# 8. Lane infrastructure never rides the diff.
# ---------------------------------------------------------------------------


class TestInfrastructureExclusion:
    def test_infra_paths_are_excluded_from_the_staged_diff(self, resumed):
        checkout, generation, base_oid = resumed
        (generation / ".codegraph").mkdir()
        (generation / ".codegraph" / "graph.db").write_bytes(b"\x00sqlite")
        (generation / ".venv").mkdir()
        (generation / ".venv" / "pyvenv.cfg").write_text("home = /x\n")
        (generation / "__pycache__").mkdir()
        (generation / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00pyc")
        (generation / ".pytest_cache").mkdir()
        (generation / ".pytest_cache" / "v").mkdir()
        (generation / ".pytest_cache" / "v" / "cache").write_text("lastfailed")
        (generation / "src" / "app.cpython-313.pyc").write_bytes(b"\x00nested")

        result = collect_candidate(checkout, WORK_ID, base_oid)
        diff = result.diff_path.read_bytes()

        for forbidden in (
            b".codegraph",
            b".venv",
            b"pyvenv.cfg",
            b"graph.db",
            b"__pycache__",
            b".pytest_cache",
            b".pyc",
            b"cpython-313",
        ):
            assert forbidden not in diff, forbidden
        # The real work still rides along.
        assert b"src/app.py" in diff


# ---------------------------------------------------------------------------
# 9. The CLI round-trip: the exact invocation the template ships.
# ---------------------------------------------------------------------------


class TestCollectCandidateCLI:
    def _run(self, cwd: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "forge.harness_entry",
                "--collect-candidate",
                "--forge-run-id",
                WORK_ID,
                *extra,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_success_prints_result_json_and_exits_zero(self, resumed):
        checkout, generation, base_oid = resumed
        outcome = self._run(
            checkout, "--attempt-base-oid", base_oid, "--output-root", "forge-output"
        )
        assert outcome.returncode == 0, outcome.stderr

        reported = json.loads(outcome.stdout)
        diff = (checkout / "forge-output" / CANDIDATE_DIFF_NAME).read_bytes()
        assert reported["source"] == "generation"
        assert reported["generation_path"] == str(generation.resolve())
        assert reported["resolved_work_id"] == WORK_ID
        assert reported["base_oid"] == base_oid
        assert reported["zero_change"] is False
        assert reported["diff_path"].endswith("forge-output/candidate.diff")
        assert diff and b"notes/new-file.md" in diff

    def test_refused_pointer_exits_nonzero_with_the_reason(self, tmp_path):
        checkout, base_oid = make_checkout(tmp_path)
        make_generation(checkout, work_id="someone-else")
        outcome = self._run(checkout, "--attempt-base-oid", base_oid)
        assert outcome.returncode != 0
        assert "someone-else" in outcome.stderr
        assert not (checkout / "forge-output").exists()

    def test_require_generation_refuses_a_missing_pointer(self, tmp_path):
        checkout, base_oid = make_checkout(tmp_path)
        outcome = self._run(checkout, "--attempt-base-oid", base_oid, "--require-generation")
        assert outcome.returncode != 0
        assert "pointer" in outcome.stderr.lower()
        # The default (no flag) keeps the fresh-run fallback.
        fresh = self._run(checkout, "--attempt-base-oid", base_oid)
        assert fresh.returncode == 0, fresh.stderr
        assert json.loads(fresh.stdout)["source"] == "checkout"

    def test_git_failure_exits_nonzero_and_writes_no_diff(self, resumed):
        checkout, _generation, _base = resumed
        outcome = self._run(
            checkout, "--attempt-base-oid", "f" * 40, "--output-root", "forge-output"
        )
        assert outcome.returncode != 0
        assert "git diff" in outcome.stderr
        assert not (checkout / "forge-output" / CANDIDATE_DIFF_NAME).exists()
