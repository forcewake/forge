"""FND-03: authoritative-read failures fail closed in every publisher.

A read failure is not evidence that a path is absent. These tests pin
the guards the publishers must consult: ONLY a confirmed absence grants
create, ONLY a digest-re-verified found grants update, and forbidden /
unavailable / truncated / unsupported outcomes never change a proposed
operation. Mirrors the R14 taxonomy suite (test_blob_reads) for the
adaptive substrate's own vocabulary.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from forge.adaptive.read_guards import (
    MAX_BLOB_BYTES,
    TypedReadOutcome,
    classify_http,
    decode_blob,
    may_create,
    may_update,
)


class TestTypedReadOutcome:
    def test_found_computes_sha256_of_utf8_bytes(self):
        outcome = TypedReadOutcome.found("x = 1\n")
        assert outcome.status == "found"
        assert outcome.content == "x = 1\n"
        assert outcome.sha256 == hashlib.sha256(b"x = 1\n").hexdigest()
        assert outcome.detail == ""

    def test_failure_factories_carry_status_and_detail_without_content(self):
        factories = {
            "absent": TypedReadOutcome.absent,
            "forbidden": TypedReadOutcome.forbidden,
            "unavailable": TypedReadOutcome.unavailable,
            "incomplete": TypedReadOutcome.incomplete,
            "unsupported": TypedReadOutcome.unsupported,
        }
        for status, build in factories.items():
            outcome = build("because")
            assert outcome.status == status
            assert outcome.detail == "because"
            assert outcome.content == ""
            assert outcome.sha256 == ""

    def test_the_status_vocabulary_is_closed(self):
        with pytest.raises(ValueError, match="unknown typed read status"):
            TypedReadOutcome("missing")

    def test_the_record_is_frozen(self):
        outcome = TypedReadOutcome.absent()
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.status = "found"


class TestClassifyHttp:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (200, "found"),  # detail carries the body for a 200
            (404, "absent"),
            (401, "forbidden"),
            (403, "forbidden"),
            (429, "unavailable"),
            (500, "unavailable"),
            (502, "unavailable"),
            (503, "unavailable"),
            (599, "unavailable"),
            (400, "unavailable"),
            (418, "unavailable"),
            (302, "unavailable"),
            (0, "unavailable"),
        ],
    )
    def test_every_status_classifies(self, status, expected):
        assert classify_http(status, "body/detail").status == expected

    def test_a_200_with_an_empty_body_is_incomplete(self):
        outcome = classify_http(200, "")
        assert outcome.status == "incomplete"
        assert "empty body" in outcome.detail

    def test_a_200_with_a_body_is_found_with_digest(self):
        outcome = classify_http(200, "content")
        assert outcome.status == "found"
        assert outcome.sha256 == hashlib.sha256(b"content").hexdigest()

    def test_the_detail_survives_into_the_outcome(self):
        assert classify_http(403, "403 from provider").detail == "403 from provider"


class TestMayCreate:
    def test_only_confirmed_absence_grants_create(self):
        assert may_create(TypedReadOutcome.absent("404")) is True
        assert may_create(classify_http(404)) is True

    @pytest.mark.parametrize(
        "outcome",
        [
            TypedReadOutcome.forbidden("403"),
            TypedReadOutcome.unavailable("429"),
            TypedReadOutcome.unavailable("503"),
            TypedReadOutcome.incomplete("truncated"),
            TypedReadOutcome.unsupported("non-utf8 blob"),
            TypedReadOutcome.unsupported("symlink"),
            TypedReadOutcome.found("content"),
        ],
    )
    def test_no_other_outcome_grants_create(self, outcome):
        assert may_create(outcome) is False

    @pytest.mark.parametrize("status", [403, 429, 500, 503])
    def test_http_failures_never_become_create_permission(self, status):
        assert may_create(classify_http(status)) is False

    def test_truncated_content_never_becomes_create_permission(self):
        # an over-cap blob is incomplete evidence, not absence
        truncated = decode_blob(b"x" * 16, max_bytes=8)
        assert truncated.status == "incomplete"
        assert may_create(truncated) is False

    def test_missing_file_allows_create_only_via_authoritative_absence(self):
        # the same path, read through the classifier: only the 404 result
        # flips create permission — nothing else about the path changed
        assert may_create(classify_http(404, "file not found")) is True
        assert may_create(classify_http(403, "same path, no permission")) is False
        assert may_create(classify_http(429, "same path, throttled")) is False


class TestMayUpdate:
    def test_found_with_reverified_digest_grants_update(self):
        assert may_update(TypedReadOutcome.found("x = 1\n")) is True

    def test_found_with_empty_content_refuses_update(self):
        assert may_update(TypedReadOutcome("found", content="", sha256="")) is False

    def test_found_with_a_bad_digest_blocks_update(self):
        # the digest is recomputed and compared — a mangled payload must
        # not authorize an update against content the provider never sent
        tampered = TypedReadOutcome("found", content="x = 1\n", sha256="0" * 64)
        assert may_update(tampered) is False

    def test_found_without_any_digest_blocks_update(self):
        assert may_update(TypedReadOutcome("found", content="x = 1\n", sha256="")) is False

    @pytest.mark.parametrize(
        "outcome",
        [
            TypedReadOutcome.forbidden("403"),
            TypedReadOutcome.unavailable("429"),
            TypedReadOutcome.incomplete("truncated"),
            TypedReadOutcome.unsupported("symlink"),
        ],
    )
    def test_unavailable_evidence_cannot_change_the_operation(self, outcome):
        # a proposed update stays an update — it never downgrades itself
        # to create or proceeds blind because evidence was unavailable
        assert may_update(outcome) is False
        assert may_create(outcome) is False

    def test_absence_refuses_update_but_that_is_the_one_granted_create(self):
        # the only outcome that may flip an update into a create is the
        # provider-confirmed absence itself — everything else refuses both
        outcome = TypedReadOutcome.absent("404")
        assert may_update(outcome) is False
        assert may_create(outcome) is True

    def test_a_found_outcome_never_grants_create(self):
        # found proves existence: the operation is an update, not a create
        assert may_create(TypedReadOutcome.found("content")) is False


class TestDecodeBlob:
    def test_utf8_text_decodes_found_with_digest(self):
        outcome = decode_blob("x = 1\n".encode("utf-8"))
        assert outcome.status == "found"
        assert outcome.content == "x = 1\n"
        assert outcome.sha256 == hashlib.sha256(b"x = 1\n").hexdigest()

    def test_empty_blob_is_incomplete(self):
        outcome = decode_blob(b"")
        assert outcome.status == "incomplete"
        assert may_create(outcome) is False
        assert may_update(outcome) is False

    def test_invalid_utf8_is_unsupported_never_replaced(self):
        outcome = decode_blob(b"\xff\xfe binary \x80")
        assert outcome.status == "unsupported"
        assert outcome.detail == "non-utf8 blob"
        assert outcome.content == ""
        assert may_create(outcome) is False
        assert may_update(outcome) is False

    def test_symlink_mode_marker_is_unsupported(self):
        # git tree entries carry mode 120000 for symlinks; the blob text
        # of a link is a path reference, not file content
        outcome = decode_blob(b"mode:120000../secret")
        assert outcome.status == "unsupported"
        assert outcome.detail == "symlink"
        assert may_create(outcome) is False
        assert may_update(outcome) is False

    def test_oversized_blob_is_incomplete(self):
        outcome = decode_blob(b"x" * 16, max_bytes=8)
        assert outcome.status == "incomplete"
        assert may_create(outcome) is False
        assert may_update(outcome) is False

    def test_a_blob_at_exactly_the_cap_decodes(self):
        outcome = decode_blob(b"xy", max_bytes=2)
        assert outcome.status == "found"

    def test_the_default_cap_is_ten_mebibytes(self):
        assert MAX_BLOB_BYTES == 10 * 1024 * 1024
        over = b"\x00" * (MAX_BLOB_BYTES + 1)
        assert decode_blob(over).status == "incomplete"

    def test_oversize_is_refused_before_decoding(self):
        # an oversized blob of invalid utf-8 is still just incomplete —
        # the cap check runs first and nothing huge is ever interpreted
        outcome = decode_blob(b"\xff" * 32, max_bytes=4)
        assert outcome.status == "incomplete"

    def test_a_symlink_marker_inside_larger_content_is_content(self):
        # only a blob STARTING with the marker is a symlink; the marker
        # appearing mid-file is ordinary text
        outcome = decode_blob(b"# not a link: mode:120000 in prose\n")
        assert outcome.status == "found"
