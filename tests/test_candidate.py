"""Stage D: CandidateBundle parsing (ADR-0016 §1) — pure parser tests.

parse_unified_diff turns a `git diff --binary --full-index` capture into a
manifest of discriminated entries (R08): created files carry FULL
reconstructed contents, modified files carry raw hunks (strictly applied
against the authoritative base at materialize time), deletes carry nothing.
Hunk counts are mandatory and cross-checked (R09); binary deltas, renames,
mode-only changes, oversized files, malformed hunks and lying counts are
rejected with a typed reason — never guessed around, never silently dropped.
"""

import hashlib

import pytest

from forge.factory.implementer import FORGE_MATERIALIZE_MAX_FILE_CHARS
from forge.repository import Change, ChangeSet, Operation
from forge.runs.candidate import (
    AttemptContext,
    CandidateBundle,
    CandidateError,
    DiffHunk,
    FullReplacement,
    HarnessUsage,
    HunkLine,
    UnifiedPatch,
    apply_unified_hunks,
    attempt_base_for,
    bundle_from_changeset,
    parse_unified_diff,
)

BASE = "base-oid-1"


def blob_oid(text: str) -> str:
    """The git blob OID *text* would hash to (sha1 repo)."""
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


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

    def test_hunk_counts_are_parsed_from_the_header(self):
        diff = "diff --git a/m.txt b/m.txt\n--- a/m.txt\n+++ b/m.txt\n@@ -2,3 +2,0 @@\n-x\n-y\n-z\n"
        bundle = parse_unified_diff(diff, BASE, "completed")
        (entry,) = bundle.entries
        (hunk,) = entry.hunks
        assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (2, 3, 2, 0)

    def test_missing_hunk_count_defaults_to_one(self):
        # POSIX: "%1d,1" may be abbreviated to "%1d".
        diff = "diff --git a/m.txt b/m.txt\n--- a/m.txt\n+++ b/m.txt\n@@ -1 +1 @@\n-old\n+new\n"
        bundle = parse_unified_diff(diff, BASE, "completed")
        (entry,) = bundle.entries
        (hunk,) = entry.hunks
        assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (1, 1, 1, 1)
        assert bundle.materialize({"m.txt": "old\n"})[0].new_content == "new\n"


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

    def test_mode_only_change_is_rejected_not_dropped(self):
        diff = "diff --git a/script.sh b/script.sh\nold mode 100644\nnew mode 100755\n"
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "mode_change_not_supported"

    def test_mode_change_with_content_change_still_applies_the_content(self):
        # A mode flip riding along a content delta is applied for its bytes;
        # only the PURE mode-only change is unsupported.
        diff = (
            "diff --git a/script.sh b/script.sh\n"
            "old mode 100644\n"
            "new mode 100755\n"
            "index 1111111..2222222\n"
            "--- a/script.sh\n"
            "+++ b/script.sh\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        completed = bundle.materialize({"script.sh": "old\n"})
        assert completed[0].new_content == "new\n"

    def test_empty_modify_is_an_ambiguous_representation(self):
        diff = "diff --git a/e.txt b/e.txt\nindex 1111111..2222222\n--- a/e.txt\n+++ b/e.txt\n"
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "ambiguous_representation"

    def test_unparseable_hunk_header_is_malformed(self):
        diff = "diff --git a/m.txt b/m.txt\n--- a/m.txt\n+++ b/m.txt\n@@ nonsense @@\n+a\n"
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "malformed_diff"

    def test_counts_disagreeing_with_the_body_are_corrupt(self):
        # Header claims 2 old lines; the body consumes 3 (git: corrupt patch).
        diff = (
            "diff --git a/m.txt b/m.txt\n"
            "--- a/m.txt\n"
            "+++ b/m.txt\n"
            "@@ -1,2 +1,2 @@\n"
            " a\n"
            " b\n"
            "-c\n"
            "+d\n"
        )
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "corrupt_patch"
        assert "line 4" in str(excinfo.value)  # the hunk header's 1-based line

    def test_no_newline_marker_without_a_body_line_is_corrupt(self):
        diff = "diff --git a/m.txt b/m.txt\n--- a/m.txt\n+++ b/m.txt\n@@ -1,1 +1,1 @@\n\\ No newline at end of file\n+a\n"
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(diff, BASE, "completed")
        assert excinfo.value.reason == "corrupt_patch"

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


class TestRepresentation:
    """The R08 discriminated union: exactly ONE representation per entry."""

    def test_unified_patch_without_hunks_is_a_construction_error(self):
        with pytest.raises(CandidateError) as excinfo:
            UnifiedPatch(path="m.txt", hunks=())
        assert excinfo.value.reason == "ambiguous_representation"

    def test_full_replacement_carries_content_not_hunks(self):
        entry = FullReplacement(path="m.txt", new_content="new\n")
        assert entry.operation == "modify"
        assert entry.new_content == "new\n"
        assert entry.hunks == ()
        assert entry.intended_digest.startswith("sha256:")

    def test_digest_claims_are_format_validated(self):
        with pytest.raises(ValueError):
            FullReplacement(path="m.txt", new_content="new\n", base_blob_digest="deadbeef")
        with pytest.raises(ValueError):
            UnifiedPatch(
                path="m.txt",
                hunks=(DiffHunk(1, 1, 1, 1, (HunkLine("-", "a"), HunkLine("+", "b"))),),
                intended_digest="sha256:not-hex",
            )

    def test_wrong_intended_digest_refuses_to_publish(self):
        entry = FullReplacement(path="m.txt", new_content="new\n")
        tampered = FullReplacement(
            path="m.txt", new_content="new\n", intended_digest="sha256:" + "0" * 64
        )
        bundle = CandidateBundle(BASE, "completed", (tampered,))
        with pytest.raises(CandidateError) as excinfo:
            bundle.materialize({"m.txt": "old\n"})
        assert excinfo.value.reason == "result_digest_mismatch"
        assert entry.new_content == "new\n"  # untouched — immutability


class TestFullReplacementMaterialize:
    """R08: an UPDATE with full content materializes THE NEW CONTENT."""

    def test_review_example_answer_is_materialized(self):
        cs = ChangeSet(
            branch="b",
            commit_message="m",
            changes=[Change(path="u.txt", operation=Operation.UPDATE, content="answer = 43\n")],
        )
        bundle = bundle_from_changeset(cs, attempt_base_oid=BASE)
        completed = bundle.materialize({"u.txt": "answer = 42\n"})
        assert completed[0].new_content == "answer = 43\n"

    def test_bundle_from_changeset_represents_update_as_full_replacement(self):
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
        assert isinstance(bundle.entries[1], FullReplacement)
        assert bundle.entries[1].hunks == ()

    def test_update_without_content_is_a_construction_rejection(self):
        cs = ChangeSet(
            branch="b",
            commit_message="m",
            changes=[Change(path="u.txt", operation=Operation.UPDATE, content=None)],
        )
        with pytest.raises(CandidateError) as excinfo:
            bundle_from_changeset(cs, attempt_base_oid=BASE)
        assert excinfo.value.reason == "ambiguous_representation"


class TestDigestVerification:
    """--full-index captures carry the base/result blob OIDs — forge verifies."""

    def _modify_diff(self, base: str, result: str) -> str:
        return (
            f"diff --git a/m.txt b/m.txt\n"
            f"index {blob_oid(base)}..{blob_oid(result)} 100644\n"
            "--- a/m.txt\n"
            "+++ b/m.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " keep\n"
            "-old\n"
            "+new\n"
            " tail\n"
        )

    def test_full_index_oids_become_base_and_result_claims(self):
        base = "keep\nold\ntail\n"
        result = "keep\nnew\ntail\n"
        bundle = parse_unified_diff(self._modify_diff(base, result), BASE, "completed")
        (entry,) = bundle.entries
        assert entry.base_blob_digest == f"blob:{blob_oid(base)}"
        assert entry.intended_digest == f"blob:{blob_oid(result)}"
        completed = bundle.materialize({"m.txt": base})
        assert completed[0].new_content == result

    def test_stale_base_is_rejected_before_application(self):
        bundle = parse_unified_diff(
            self._modify_diff("keep\nold\ntail\n", "keep\nnew\ntail\n"), BASE, "completed"
        )
        with pytest.raises(CandidateError) as excinfo:
            # The hunks would NOT match this base anyway — but the digest
            # fires FIRST, naming the real problem: the base moved.
            bundle.materialize({"m.txt": "keep\nmoved\ntail\n"})
        assert excinfo.value.reason == "stale_base"

    def test_abbreviated_index_makes_no_claim(self):
        diff = (
            "diff --git a/m.txt b/m.txt\n"
            "index 1111111..2222222 100644\n"
            "--- a/m.txt\n"
            "+++ b/m.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        (entry,) = bundle.entries
        assert entry.base_blob_digest == ""
        assert entry.intended_digest == ""

    def test_delete_carries_a_base_claim(self):
        base = "one\ntwo\n"
        diff = (
            f"diff --git a/gone.txt b/gone.txt\n"
            f"deleted file mode 100644\n"
            f"index {blob_oid(base)}..0000000000000000000000000000000000000000\n"
            "--- a/gone.txt\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-one\n"
            "-two\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        (entry,) = bundle.entries
        assert entry.base_blob_digest == f"blob:{blob_oid(base)}"
        assert bundle.materialize({"gone.txt": base})[0].operation == "delete"

        stale = parse_unified_diff(diff, BASE, "completed")
        with pytest.raises(CandidateError) as excinfo:
            stale.materialize({"gone.txt": "other\n"})
        assert excinfo.value.reason == "stale_base"

    def test_create_result_is_verified_against_the_full_index_oid(self):
        content = "alpha\nbeta\n"
        good = (
            "diff --git a/n.txt b/n.txt\n"
            "new file mode 100644\n"
            f"index 0000000000000000000000000000000000000000..{blob_oid(content)}\n"
            "--- /dev/null\n"
            "+++ b/n.txt\n"
            "@@ -0,0 +1,2 @@\n"
            "+alpha\n"
            "+beta\n"
        )
        bundle = parse_unified_diff(good, BASE, "completed")
        assert bundle.materialize({})[0].new_content == content

        bad = good.replace(blob_oid(content), "a" * 40)
        with pytest.raises(CandidateError) as excinfo:
            parse_unified_diff(bad, BASE, "completed").materialize({})
        assert excinfo.value.reason == "result_digest_mismatch"


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

    def test_full_replacement_without_base_content_is_rejected(self):
        bundle = bundle_from_changeset(
            ChangeSet(
                branch="b",
                commit_message="m",
                changes=[Change(path="u.txt", operation=Operation.UPDATE, content="y\n")],
            ),
            attempt_base_oid=BASE,
        )
        with pytest.raises(CandidateError) as excinfo:
            bundle.materialize({})
        assert excinfo.value.reason == "patch_does_not_apply"

    def test_overlapping_hunks_are_rejected(self):
        hunks = (
            DiffHunk(1, 2, 1, 2, (HunkLine(" ", "a"), HunkLine("-", "b"), HunkLine("+", "B"))),
            DiffHunk(1, 1, 1, 1, (HunkLine(" ", "a"),)),
        )
        with pytest.raises(CandidateError) as excinfo:
            apply_unified_hunks("a\nb\nc\n", hunks)
        assert excinfo.value.reason == "patch_does_not_apply"

    def test_apply_rejects_counts_that_disagree_with_the_body(self):
        lying = DiffHunk(1, 2, 1, 1, (HunkLine("-", "a"),))
        with pytest.raises(CandidateError) as excinfo:
            apply_unified_hunks("a\nb\n", (lying,))
        assert excinfo.value.reason == "corrupt_patch"

    def test_zero_context_insertion_goes_after_old_start(self):
        # POSIX: "@@ -3,0 ..." is EMPTY — insertion AFTER line 3 (R09 G1).
        diff = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -3,0 +4,1 @@\n+INSERTED\n"
        bundle = parse_unified_diff(diff, BASE, "completed")
        completed = bundle.materialize({"f.txt": "line1\nline2\nline3\nline4\n"})
        assert completed[0].new_content == "line1\nline2\nline3\nINSERTED\nline4\n"

    def test_zero_context_insertion_at_bof(self):
        diff = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -0,0 +1,1 @@\n+HEAD\n"
        bundle = parse_unified_diff(diff, BASE, "completed")
        completed = bundle.materialize({"f.txt": "line1\nline2\n"})
        assert completed[0].new_content == "HEAD\nline1\nline2\n"

    def test_crlf_diff_applies_to_crlf_base(self):
        # Split on "\n" only: CR stays content, never a line break (R09 G6).
        diff = (
            "diff --git a/w.txt b/w.txt\n"
            "--- a/w.txt\r\n"
            "+++ b/w.txt\r\n"
            "@@ -1,2 +1,2 @@\r\n"
            " alpha\r\n"
            "-beta\r\n"
            "+BETA\r\n"
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        completed = bundle.materialize({"w.txt": "alpha\r\nbeta\r\n"})
        assert completed[0].new_content == "alpha\r\nBETA\r\n"

    def test_u2028_inside_a_line_is_not_a_line_break(self):
        line = 'const s = "a\u2028b";\n'
        diff = (
            "diff --git a/s.js b/s.js\n"
            "--- a/s.js\n"
            "+++ b/s.js\n"
            "@@ -1,1 +1,1 @@\n"
            '-const s = "a\u2028b";\n'
            '+const s = "a\u2028b"; // touched\n'
        )
        bundle = parse_unified_diff(diff, BASE, "completed")
        completed = bundle.materialize({"s.js": line})
        assert completed[0].new_content == 'const s = "a\u2028b"; // touched\n'

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

    def test_attempt_context_cycle_one_is_the_source_base(self):
        run = type("R", (), {"commit_cycle": 1, "candidate_shas": None, "base_sha": "src-1"})
        ctx = AttemptContext.of(run)
        assert ctx.cycle == 1
        assert ctx.attempt_base == "src-1"
        assert ctx.source_base == "src-1"
        assert ctx.previous_candidate is None

    def test_attempt_context_repair_extends_the_last_candidate(self):
        run = type(
            "R", (), {"commit_cycle": 2, "candidate_shas": ["c1", "c2"], "base_sha": "src-1"}
        )
        ctx = AttemptContext.of(run)
        assert ctx.attempt_base == "c2"
        assert ctx.source_base == "src-1"  # frozen for cumulative review only
        assert ctx.previous_candidate == "c2"
        assert ctx.document() == {
            "cycle": 2,
            "attempt_base": "c2",
            "source_base": "src-1",
            "previous_candidate": "c2",
        }

    def test_attempt_context_repair_without_a_candidate_falls_back_to_source(self):
        run = type("R", (), {"commit_cycle": 3, "candidate_shas": None, "base_sha": None})
        ctx = AttemptContext.of(run)
        assert ctx.attempt_base == ""
        assert ctx.previous_candidate is None

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
