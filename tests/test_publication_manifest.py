"""Recovery must prove all native post-images and preserve every untouched path."""

import hashlib

import pytest

from forge.domain.candidate import (
    CandidateBundle,
    CreateFile,
    DeleteFile,
    FullReplacement,
    validate_candidate_bundle,
)
from forge.domain.publication import matches_manifest


def oid(body: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()


def proposed():
    return validate_candidate_bundle(
        CandidateBundle(
            attempt_base_oid="a" * 40,
            driver_exit="completed",
            entries=(
                CreateFile("tool.sh", "echo ok\n", mode="100755"),
                DeleteFile("old.txt"),
                FullReplacement("same.txt", "unchanged\n"),
            ),
        ),
        base_contents={"old.txt": "old\n", "same.txt": "unchanged\n"},
        branch="forge/run",
        commit_message="Exact publication",
    )


def trees():
    before = {
        "old.txt": ("100644", oid(b"old\n")),
        "same.txt": ("100644", oid(b"unchanged\n")),
        "keep.bin": ("100644", oid(b"\x00\xff")),
        "submodule": ("160000", "b" * 40),
    }
    after = {path: entry for path, entry in before.items() if path != "old.txt"}
    after["tool.sh"] = ("100755", oid(b"echo ok\n"))
    return before, after


def test_exact_publication_allows_unchanged_explicit_postimage_and_preserves_submodules():
    assert matches_manifest(proposed(), *trees())


@pytest.mark.parametrize("fault", ("bytes", "mode", "extra", "delete", "submodule", "create"))
def test_copied_marker_does_not_admit_wrong_native_tree(fault):
    before, after = trees()
    if fault == "bytes":
        after["tool.sh"] = ("100755", oid(b"different\n"))
    elif fault == "mode":
        after["tool.sh"] = ("100644", oid(b"echo ok\n"))
    elif fault == "extra":
        after["extra"] = ("100644", oid(b"unapproved"))
    elif fault == "delete":
        after["old.txt"] = before["old.txt"]
    elif fault == "submodule":
        after["submodule"] = ("160000", "c" * 40)
    else:
        before["tool.sh"] = after["tool.sh"]
    assert not matches_manifest(proposed(), before, after)
