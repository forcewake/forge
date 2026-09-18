"""Differential tests: forge's patch applier against `git apply` as the oracle.

For every corpus case the base file is committed as BYTES in a hermetic git
repo (no user/system config, no autocrlf — a developer's config must never
change the oracle's verdict), the patch is produced by git itself, and
``git apply --check`` / a real ``git apply`` decide the reference verdict.
When git accepts, forge's materialization must be BYTE-IDENTICAL to git's
post-image (files are read with ``read_bytes``, never with text semantics);
where forge rejects, either git's ``--check`` fails too (parity) or the
rejection reason is one of the documented policy divergences
(``binary_not_supported``, ``rename_not_supported``, ``mode_change_not_supported``).

Corpus coverage mirrors git's own apply suite areas (t4101/t4113/t4118/
t4126/t4129/t4135): LF clean patches, zero-context insertion (G1/G2), CRLF
files and patches, the four no-final-newline combinations (G5), empty-file
edges (t4126), multiple hunks, Unicode incl. U+2028 (G6), lying hunk counts,
mode-only changes (t4129), renames, binary deltas, and a stale base.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from forge.repository import Change, ChangeSet, Operation
from forge.runs.candidate import CandidateError, bundle_from_changeset, parse_unified_diff

ATTEMPT_BASE = "attempt-base"


def _git(repo, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run git inside *repo*; I/O is binary-safe (never text pipes)."""
    return subprocess.run(
        ("git", "-C", str(repo), *args),
        input=b"",
        capture_output=True,
        check=check,
        timeout=30,
    )


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    """Hermetic git workspace: no user/system config, no autocrlf."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "git.config"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "git.config").write_text("")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "core.filemode", "true")
    _git(repo, "config", "user.email", "forge@example.invalid")
    _git(repo, "config", "user.name", "forge")
    return repo


# ----------------------------------------------------------------------
# Corpus plumbing
# ----------------------------------------------------------------------


def _commit_base(repo, path: str, data: bytes) -> None:
    """Seed the base file as a blob, exactly like production."""
    (repo / path).write_bytes(data)
    _git(repo, "add", "--", path)
    _git(repo, "commit", "-q", "-m", "base")


def _produce_patch(repo, path: str, new_data: bytes, *flags: str) -> bytes:
    """The git-generated base→*new_data* patch for *path*; restores the base."""
    (repo / path).write_bytes(new_data)
    patch = _git(repo, "diff", "--full-index", *flags, "--", path).stdout
    _git(repo, "checkout", "--", path)
    assert patch, f"git produced no patch for {path}"
    return patch


def _git_apply(repo, patch: bytes, *flags: str, check: bool = True):
    # The patch file is ALWAYS written binary: text mode would translate \n
    # and destroy CRLF / no-final-newline fixtures.
    (repo / "candidate.diff").write_bytes(patch)
    return _git(repo, "apply", "--whitespace=nowarn", *flags, "candidate.diff", check=check)


def _forge_materialize(patch: bytes, base: bytes | None, path: str) -> bytes:
    """forge's accepted verdict: the materialized bytes of the entry."""
    bundle = parse_unified_diff(patch.decode("utf-8"), ATTEMPT_BASE)
    contents = {} if base is None else {path: base.decode("utf-8")}
    (completed,) = bundle.materialize(contents)
    assert completed.new_content is not None
    return completed.new_content.encode("utf-8")


def _forge_reason(patch: bytes, base: bytes | None, path: str) -> str:
    """forge's rejected verdict: the machine-readable reason."""
    try:
        _forge_materialize(patch, base, path)
    except CandidateError as exc:
        return exc.reason
    raise AssertionError("forge accepted the patch; expected a rejection")


def _assert_byte_identical(repo, path: str, base: bytes, patch: bytes, *flags: str) -> None:
    """Oracle: git --check + real apply; forge must be byte-identical after."""
    _git_apply(repo, patch, "--check", *flags)  # raises when git rejects
    _git_apply(repo, patch, *flags)  # real application — the reference post-image
    expected = (repo / path).read_bytes()
    assert _forge_materialize(patch, base, path) == expected


class TestGitOracleAccepted:
    """git applies → forge must produce byte-identical results."""

    def test_lf_clean_patch(self, git_repo):
        base = b"line1\nline2\nline3\nline4\nline5\n"
        new = b"line1\nline2 CHANGED\nline3\nline4\nline5\n"
        _commit_base(git_repo, "f.txt", base)
        patch = _produce_patch(git_repo, "f.txt", new)
        _assert_byte_identical(git_repo, "f.txt", base, patch)

    def test_zero_context_insertion_after_line_3(self, git_repo):
        # G1: "@@ -3,0 +4,1 @@" inserts AFTER line 3 (t4118 territory).
        # git itself needs --unidiff-zero to not shove it to EOF (G2); forge
        # applies the POSIX placement rule directly.
        base = b"line1\nline2\nline3\nline4\n"
        new = b"line1\nline2\nline3\nINSERTED\nline4\n"
        _commit_base(git_repo, "f.txt", base)
        patch = _produce_patch(git_repo, "f.txt", new, "--unified=0")
        # POSIX: a range of exactly one line may drop the ",1" — the applier
        # must default it (G1).
        assert b"@@ -3,0 +4 @@" in patch
        _assert_byte_identical(git_repo, "f.txt", base, patch, "--unidiff-zero")

    def test_crlf_file_with_crlf_patch(self, git_repo):
        # G6: CR is content, not whitespace — round-trips byte-exactly.
        base = b"line1\r\nline2\r\nline3\r\n"
        new = b"line1\r\nLINE2 EDITED\r\nline3\r\n"
        _commit_base(git_repo, "f.txt", base)
        patch = _produce_patch(git_repo, "f.txt", new)
        assert b" line1\r\n" in patch and b"-line2\r\n" in patch  # CR kept as content
        _assert_byte_identical(git_repo, "f.txt", base, patch)

    @pytest.mark.parametrize(
        ("base", "new"),
        [
            (b"x\ny", b"x\ny\nz"),  # both sides lack the final newline (t4101)
            (b"x\ny\n", b"x\ny"),  # new side loses the final newline
            (b"x\ny", b"x\ny\nz\n"),  # old side lacked it, new side gains it
            (b"x\ny", b"x\ny\n"),  # only the NL-ness flips, content identical
        ],
        ids=["both-no-nl", "new-loses-nl", "old-no-nl", "nl-flip-only"],
    )
    def test_no_final_newline_combinations(self, git_repo, base, new):
        _commit_base(git_repo, "f.txt", base)
        patch = _produce_patch(git_repo, "f.txt", new)
        _assert_byte_identical(git_repo, "f.txt", base, patch)

    def test_empty_file_gains_content(self, git_repo):
        # t4126: "@@ -0,0 +1,N @@" against an empty base blob.
        base = b""
        new = b"first\nsecond\n"
        _commit_base(git_repo, "f.txt", base)
        patch = _produce_patch(git_repo, "f.txt", new)
        _assert_byte_identical(git_repo, "f.txt", base, patch)

    def test_content_replaced_by_empty_file(self, git_repo):
        base = b"first\nsecond\n"
        new = b""
        _commit_base(git_repo, "f.txt", base)
        patch = _produce_patch(git_repo, "f.txt", new)
        _assert_byte_identical(git_repo, "f.txt", base, patch)

    def test_new_empty_file_has_no_hunks(self, git_repo):
        # t4126: a new EMPTY file is headers only ("@@ -0,0 +0,0 @@" absent).
        _commit_base(git_repo, "seed.txt", b"seed\n")
        (git_repo / "empty.txt").write_bytes(b"")
        _git(git_repo, "add", "-N", "empty.txt")
        patch = _git(git_repo, "diff", "--full-index").stdout
        assert b"new file mode 100644" in patch
        _git(git_repo, "reset", "-q", "--", "empty.txt")
        (git_repo / "empty.txt").unlink()
        _git_apply(git_repo, patch)  # oracle creates the empty file
        assert (git_repo / "empty.txt").read_bytes() == b""
        assert _forge_materialize(patch, None, "empty.txt") == b""

    def test_multiple_hunks_in_one_file(self, git_repo):
        base = b"".join(b"line%d\n" % n for n in range(1, 22))
        new = base.replace(b"line1\n", b"LINE1\n", 1)
        new = new.replace(b"line11\n", b"LINE11\n", 1)
        new = new.replace(b"line21\n", b"LINE21\n", 1)
        _commit_base(git_repo, "f.txt", base)
        patch = _produce_patch(git_repo, "f.txt", new)
        assert patch.count(b"@@ -") == 3
        _assert_byte_identical(git_repo, "f.txt", base, patch)

    def test_unicode_content_incl_u2028_stays_one_line(self, git_repo):
        # G6: U+2028 inside a JS string literal must never split the line.
        base = 'const a = "héllo\u2028world";\nconst b = 2;\n'.encode("utf-8")
        new = 'const a = "héllo\u2028world";\nconst b = 3; // changed\n'.encode("utf-8")
        _commit_base(git_repo, "app.js", base)
        patch = _produce_patch(git_repo, "app.js", new)
        assert "\u2028".encode("utf-8") in patch  # the context line survived whole
        _assert_byte_identical(git_repo, "app.js", base, patch)


class TestGitOracleRejections:
    """forge rejects ⇔ git --check rejects, or the reason is documented."""

    def test_crlf_base_with_lf_patch_is_rejected_by_both(self, git_repo):
        # The patch's preimage has no CR; the committed CRLF file does.
        # CR is content (G6): neither git nor forge may silently match.
        base = b"line1\r\nline2\r\n"
        patch = (
            "diff --git a/f.txt b/f.txt\n"
            "--- a/f.txt\n"
            "+++ b/f.txt\n"
            "@@ -1,2 +1,2 @@\n"
            "-line1\n"
            "+line1 edited\n"
            " line2\n"
        ).encode("utf-8")
        _commit_base(git_repo, "f.txt", base)
        assert _git_apply(git_repo, patch, check=False).returncode != 0
        assert _forge_reason(patch, base, "f.txt") == "patch_does_not_apply"

    def test_lying_hunk_counts_are_corrupt_for_both(self, git_repo):
        # Header claims 2 old lines; the body consumes 3 (git: "corrupt
        # patch at line N").
        base = b"line1\nline2\nline3\n"
        patch = (
            "diff --git a/f.txt b/f.txt\n"
            "--- a/f.txt\n"
            "+++ b/f.txt\n"
            "@@ -1,2 +1,2 @@\n"
            " line1\n"
            " line2\n"
            "-line3\n"
            "+LINE3\n"
        ).encode("utf-8")
        _commit_base(git_repo, "f.txt", base)
        assert _git_apply(git_repo, patch, check=False).returncode != 0
        assert _forge_reason(patch, base, "f.txt") == "corrupt_patch"

    def test_stale_base_is_rejected_by_both(self, git_repo):
        _commit_base(git_repo, "f.txt", b"one\ntwo\nthree\n")
        patch = _produce_patch(git_repo, "f.txt", b"one\nTWO\nthree\n")
        # The base moves AFTER the patch was captured: the attempt base's
        # blob no longer matches the diff's --full-index pre-image claim.
        moved = b"one\nMOVED AWAY\nthree\n"
        (git_repo / "f.txt").write_bytes(moved)
        _git(git_repo, "commit", "-qam", "base moved")
        assert _git_apply(git_repo, patch, check=False).returncode != 0
        assert _forge_reason(patch, moved, "f.txt") == "stale_base"

    def test_binary_placeholder_rejected_by_both(self, git_repo):
        _commit_base(git_repo, "blob.bin", b"\x00\x01binary-ish\n")
        (git_repo / "blob.bin").write_bytes(b"\x00\x02other bytes\n")
        patch = _git(git_repo, "diff", "--full-index", "--", "blob.bin").stdout
        assert b"Binary files" in patch  # placeholder — git cannot apply it
        _git(git_repo, "checkout", "--", "blob.bin")
        assert _git_apply(git_repo, patch, check=False).returncode != 0
        assert _forge_reason(patch, b"\x00\x01binary-ish\n", "blob.bin") == ("binary_not_supported")

    def test_git_binary_patch_is_a_documented_divergence(self, git_repo):
        _commit_base(git_repo, "blob.bin", b"\x00\x01binary-ish\n")
        (git_repo / "blob.bin").write_bytes(b"\x00\x02other bytes\n")
        patch = _git(git_repo, "diff", "--binary", "--full-index", "--", "blob.bin").stdout
        assert b"GIT binary patch" in patch
        _git(git_repo, "checkout", "--", "blob.bin")
        assert _git_apply(git_repo, patch, check=False).returncode == 0  # git accepts
        assert _forge_reason(patch, b"\x00\x01binary-ish\n", "blob.bin") == ("binary_not_supported")

    def test_mode_only_change_is_a_documented_divergence(self, git_repo):
        # R09: a pure mode flip is REJECTED, never silently dropped. git
        # applies it — that is the declared v0.3 policy divergence (G7).
        _commit_base(git_repo, "run.sh", b"#!/bin/sh\necho hi\n")
        (git_repo / "run.sh").chmod(0o755)
        patch = _git(git_repo, "diff", "--full-index", "--", "run.sh").stdout
        assert b"old mode 100644" in patch and b"new mode 100755" in patch
        (git_repo / "run.sh").chmod(0o644)
        assert _git_apply(git_repo, patch, check=False).returncode == 0  # git accepts
        assert _forge_reason(patch, b"#!/bin/sh\necho hi\n", "run.sh") == (
            "mode_change_not_supported"
        )

    def test_rename_is_a_documented_divergence(self, git_repo):
        _commit_base(git_repo, "old.txt", b"content\n")
        _git(git_repo, "mv", "old.txt", "new.txt")
        patch = _git(git_repo, "diff", "--cached", "--full-index").stdout
        assert b"rename from old.txt" in patch
        # git mv already staged the rename, so reset the index back to HEAD
        # and let the oracle re-apply the patch to it — git accepts.
        _git(git_repo, "reset", "-q")
        assert _git_apply(git_repo, patch, "--cached", check=False).returncode == 0
        assert _forge_reason(patch, b"content\n", "old.txt") == "rename_not_supported"

    def test_empty_modify_is_rejected_forge_side(self, git_repo):
        # Headers without hunks and without a mode flip: nothing to apply.
        base = b"content\n"
        patch = (
            "diff --git a/f.txt b/f.txt\n"
            "index 1111111111111111111111111111111111111111.."
            "2222222222222222222222222222222222222222 100644\n"
            "--- a/f.txt\n"
            "+++ b/f.txt\n"
        ).encode("utf-8")
        _commit_base(git_repo, "f.txt", base)
        assert _forge_reason(patch, base, "f.txt") == "ambiguous_representation"

    def test_delete_of_empty_file_is_a_bare_delete_entry(self, git_repo):
        _commit_base(git_repo, "gone.txt", b"")
        (git_repo / "gone.txt").unlink()
        patch = _git(git_repo, "diff", "--full-index", "--", "gone.txt").stdout
        assert b"deleted file mode 100644" in patch
        (git_repo / "gone.txt").write_bytes(b"")
        _git_apply(git_repo, patch)  # oracle removes the file
        assert not (git_repo / "gone.txt").exists()
        bundle = parse_unified_diff(patch.decode("utf-8"), ATTEMPT_BASE)
        (entry,) = bundle.entries
        assert entry.operation == "delete"
        assert bundle.materialize({"gone.txt": ""})[0].operation == "delete"


class TestReviewExampleR08:
    """The review's example, ported: a full-content UPDATE materializes."""

    def test_update_new_content_wins_over_the_original(self):
        cs = ChangeSet(
            branch="b",
            commit_message="m",
            changes=[Change(path="answer.py", operation=Operation.UPDATE, content="answer = 43\n")],
        )
        bundle = bundle_from_changeset(cs, attempt_base_oid=ATTEMPT_BASE)
        (completed,) = bundle.materialize({"answer.py": "answer = 42\n"})
        assert completed.new_content == "answer = 43\n"
