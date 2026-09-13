"""Stage D: CandidateBundle parsing (ADR-0016 §1) — pure parser tests.

parse_unified_diff turns a `git diff --binary --full-index` capture into a
manifest: created files carry FULL reconstructed contents, modified files
carry raw hunks (strictly applied against the authoritative base at
materialize time), deletes carry nothing. Binary deltas, renames, oversized
files and malformed hunks are rejected — never guessed around.
"""

import pytest

from forge.factory.implementer import FORGE_MATERIALIZE_MAX_FILE_CHARS
from forge.repository import Change, ChangeSet, Operation
from forge.runs.candidate import (
    CandidateBundle,
    CandidateError,
    HarnessUsage,
    attempt_base_for,
    bundle_from_changeset,
    parse_unified_diff,
)

BASE = "base-oid-1"


class TestParseManifest:
    def test_create_reconstructs_full_content_and_mode(self):
        diff = (
            "diff --git a/src/new.py b/src/new.py\n"
            "new file mode 100755\n"
            "index 0000000..abcdef0\n"
            "--- /dev/null\n"
            "+++ b/src/new.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+alpha\n"
            "+beta\n"
            "+gamma\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        (entry,) = bundle.entries
        assert entry.path == "src/new.py"
        assert entry.operation == "create"
        assert entry.new_content == "alpha\nbeta\ngamma\n"
        assert entry.mode == "100755"

    def test_modify_keeps_hunks_and_materializes_against_base(self):
        diff = (
            "diff --git a/src/mod.py b/src/mod.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/mod.py\n"
            "+++ b/src/mod.py\n"
            "@@ -1,3 +1,3 @@\n"
            " keep\n"
            "-old\n"
            "+new\n"
            " tail\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        (entry,) = bundle.entries
        assert entry.operation == "modify"
        assert entry.new_content is None  # completed at materialize time
        assert entry.hunks

        completed = bundle.materialize({"src/mod.py": "keep\nold\ntail\n"})
        assert completed[0].new_content == "keep\nnew\ntail\n"

    def test_delete_carries_no_content(self):
        diff = (
            "diff --git a/src/gone.py b/src/gone.py\n"
            "deleted file mode 100644\n"
            "index 1111111..0000000\n"
            "--- a/src/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-one\n"
            "-two\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        (entry,) = bundle.entries
        assert entry.operation == "delete"
        assert entry.new_content is None

    def test_mixed_manifest_preserves_order(self):
        diff = "\n".join(
            [
                "diff --git a/a.txt b/a.txt",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/a.txt",
                "@@ -0,0 +1,1 @@",
                "+a",
                "diff --git a/b.txt b/b.txt",
                "--- a/b.txt",
                "+++ b/b.txt",
                "@@ -1,1 +1,1 @@",
                "-b0",
                "+b1",
                "diff --git a/c.txt b/c.txt",
                "deleted file mode 100644",
                "--- a/c.txt",
                "+++ /dev/null",
                "@@ -1,1 +0,0 @@",
                "-c",
            ]
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        assert [(e.path, e.operation) for e in bundle.entries] == [
            ("a.txt", "create"),
            ("b.txt", "modify"),
            ("c.txt", "delete"),
        ]

    def test_empty_diff_is_an_empty_bundle(self):
        bundle = parse_unified_diff("", BASE, "completed")
        assert bundle.is_empty
        assert bundle.attempt_base_oid == BASE
        assert bundle.driver_exit == "completed"


class TestParseRejections:
    def test_binary_delta_rejected(self):
        diff = (
            "diff --git a/img.png b/img.png\n"
            "index 1111111..2222222 100644\n"
            "GIT binary patch\n"
            "literal 10\n"
            "FmcmZhfxiH\n"
        )
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "binary_not_supported"

    def test_binary_placeholder_rejected(self):
        diff = "diff --git a/blob.bin b/blob.bin\nindex 111..222 100644\nBinary files a/blob.bin and b/blob.bin differ\n"
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "binary_not_supported"

    def test_rename_rejected(self):
        diff = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 100%\n"
            "rename from old.py\n"
            "rename to new.py\n"
        )
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "rename_not_supported"

    def test_oversized_created_file_rejected(self, monkeypatch):
        monkeypatch.setattr("forge.runs.candidate.FORGE_MATERIALIZE_MAX_FILE_CHARS", 10)
        diff = (
            "diff --git a/big.txt b/big.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/big.txt\n"
            "@@ -0,0 +1,1 @@\n"
            "+" + "x" * 64 + "\n"
        )
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "file_too_large"

    def test_mode_only_change_is_dropped(self):
        diff = "diff --git a/script.sh b/script.sh\nold mode 100644\nnew mode 100755\n"
        bundle = parse_unified_diff(diff, BASE, "completed")
        assert bundle.is_empty  # no content delta — documented drop


class TestStrictApply:
    def test_mismatch_against_authoritative_base_is_rejected(self):
        diff = (
            "diff --git a/mod.py b/mod.py\n"
            "--- a/mod.py\n"
            "+++ b/mod.py\n"
            "@@ -1,2 +1,2 @@\n"
            " keep\n"
            "-actual\n"
            "+changed\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        with pytest.raises(CandidateError) as excinfo:
            bundle.materialize({"mod.py": "keep\nsomething-else\n"})
        assert excinfo.value.reason == "patch_does_not_apply"

    def test_modify_without_base_content_is_rejected(self):
        diff = (
            "diff --git a/mod.py b/mod.py\n"
            "--- a/mod.py\n"
            "+++ b/mod.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        with pytest.raises(CandidateError) as excinfo:
            bundle.materialize({})  # file absent from the snapshot
        assert excinfo.value.reason == "patch_does_not_apply"

    def test_overlapping_hunks_are_rejected(self):
        from forge.runs.candidate import DiffHunk, HunkLine, apply_unified_hunks

        hunks = (
            DiffHunk(1, 1, (HunkLine(" ", "a"), HunkLine("-", "b"), HunkLine("+", "B"))),
            DiffHunk(1, 2, (HunkLine(" ", "a"),)),
        )
        with pytest.raises(CandidateError):
            apply_unified_hunks("a\nb\nc\n", hunks)

    def test_no_newline_markers_round_trip(self):
        diff = (
            "diff --git a/nl.txt b/nl.txt\n"
            "--- a/nl.txt\n"
            "+++ b/nl.txt\n"
            "@@ -1,2 +1,2 @@\n"
            " a\n"
            "-b\n"
            "\\ No newline at end of file\n"
            "+B\n"
            "\\ No newline at end of file\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        (entry,) = bundle.entries
        completed = bundle.materialize({"nl.txt": "a\nb"})
        assert completed[0].new_content == "a\nB"

    def test_base_newline_claim_mismatch_is_rejected(self):
        diff = (
            "diff --git a/nl.txt b/nl.txt\n"
            "--- a/nl.txt\n"
            "+++ b/nl.txt\n"
            "@@ -1,2 +1,2 @@\n"
            " a\n"
            "-b\n"
            "\\ No newline at end of file\n"
            "+B\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        with pytest.raises(CandidateError) as excinfo:
            # The base DOES end with a newline; the diff claims it does not.
            bundle.materialize({"nl.txt": "a\nb\n"})
        assert excinfo.value.reason == "patch_does_not_apply"


class TestUsageReceipt:
    def test_from_meta_sums_are_aggregate(self):
        usage = HarnessUsage.from_meta(
            {
                "input_tokens": 11,
                "cached_input_tokens": 3,
                "output_tokens": 5,
                "completeness": "aggregate",
                "source": "grok:usage",
            },
            driver="grok-build",
            model="grok-4.6",
        )
        assert usage.input_tokens == 11
        assert usage.cached_input_tokens == 3  # never folded into input
        assert usage.output_tokens == 5
        assert usage.completeness == "aggregate"
        assert usage.driver == "grok-build"

    def test_from_meta_without_receipt_is_unknown(self):
        usage = HarnessUsage.from_meta(None, driver="claude-code")
        assert usage.completeness == "unknown"
        assert usage.input_tokens is None  # unknown stays unknown, never zero

    def test_from_meta_garbage_numbers_are_ignored(self):
        usage = HarnessUsage.from_meta(
            {"input_tokens": "many", "output_tokens": -3, "cached_input_tokens": True}
        )
        assert usage.completeness == "unknown"
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.cached_input_tokens is None


class TestHelpers:
    def test_attempt_base_cycle_one_is_source_base(self):
        run = type("R", (), {"commit_cycle": 1, "candidate_shas": None, "base_sha": "src-1"})
        assert attempt_base_for(run) == "src-1"

    def test_attempt_base_repair_is_last_candidate(self):
        run = type(
            "R", (), {"commit_cycle": 2, "candidate_shas": ["c1", "c2"], "base_sha": "src-1"}
        )
        assert attempt_base_for(run) == "c2"

    def test_bundle_from_changeset_maps_operations(self):
        cs = ChangeSet(
            branch="b",
            commit_message="m",
            changes=[
                Change(path="n.txt", operation=Operation.CREATE, content="x\n"),
                Change(path="u.txt", operation=Operation.UPDATE, content="y\n"),
                Change(path="d.txt", operation=Operation.DELETE, content=None),
            ],
        )
        bundle = bundle_from_changeset(cs, attempt_base_oid=BASE)
        assert isinstance(bundle, CandidateBundle)
        assert [(e.path, e.operation) for e in bundle.entries] == [
            ("n.txt", "create"),
            ("u.txt", "modify"),
            ("d.txt", "delete"),
        ]
        assert bundle.entries[0].new_content == "x\n"

    def test_materialize_enforces_cap(self, monkeypatch):
        diff = (
            "diff --git a/mod.py b/mod.py\n"
            "--- a/mod.py\n"
            "+++ b/mod.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        monkeypatch.setattr(
            "forge.runs.candidate.FORGE_MATERIALIZE_MAX_FILE_CHARS",
            FORGE_MATERIALIZE_MAX_FILE_CHARS,
        )
        monkeypatch.setattr(
            "forge.runs.candidate._enforce_cap",
            lambda path, content: (_ for _ in ()).throw(CandidateError("file_too_large", path)),
        )
        with pytest.raises(CandidateError) as excinfo:
            bundle.materialize({"mod.py": "old\n"})
        assert excinfo.value.reason == "file_too_large"
