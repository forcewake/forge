"""Retained authoritative-byte lessons against the sole native RepoPort contract.

bytes are found, None is confirmed resource absence, and ReadUnavailable carries
bounded failed-read evidence. Text interpretation is strict at its consumer;
missing encoding never enables a base64/plaintext heuristic.
"""

import hashlib

import httpx
import pytest
from pydantic import ValidationError

from forge.domain.blob import BlobContent
from forge.port_values import ReadUnavailable
from tests import native_clients_fixture as fixture
from tests.native_clients_fixture import blob, missing, timeout

app_key, blob_case = fixture.app_key, fixture.blob_case
github, gitlab = fixture.github, fixture.gitlab


class TestBlobReadResult:
    async def test_found_computes_sha256_of_utf8_bytes(self, blob_case):
        blob(blob_case, b"hello\n")
        result = await blob_case.repositories.read_blob(
            blob_case.repo, blob_case.sha, blob_case.path
        )
        assert result.decode("utf-8") == "hello\n"
        assert hashlib.sha256(result).hexdigest() == hashlib.sha256(b"hello\n").hexdigest()

    async def test_found_accepts_bytes(self, blob_case):
        blob(blob_case, b"\x00\xff")
        assert (
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)
            == b"\x00\xff"
        )

    @pytest.mark.parametrize("status", [401, 403, 429, 500])
    async def test_non_found_statuses_carry_detail_and_no_content(self, blob_case, status):
        missing(blob_case, httpx.Response(status, headers={"x-request-id": "test"}))
        with pytest.raises(ReadUnavailable) as failure:
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)
        assert failure.value.evidence and "synthetic-" not in failure.value.evidence

    async def test_only_not_found_is_confirmed_absent(self, blob_case):
        missing(blob_case, httpx.Response(404))
        assert (
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)
            is None
        )
        missing(blob_case, httpx.Response(403))
        with pytest.raises(ReadUnavailable):
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)

    async def test_text_raises_without_found_content(self, blob_case):
        blob(blob_case, b"\xff")
        result = await blob_case.repositories.read_blob(
            blob_case.repo, blob_case.sha, blob_case.path
        )
        with pytest.raises(UnicodeDecodeError):
            result.decode("utf-8")
        assert result == b"\xff"

    def test_invariants_are_enforced(self):
        value = BlobContent(raw=b"\x00\xff")
        assert BlobContent.model_validate_json(value.model_dump_json()) == value
        for invalid in ({}, {"raw": "text"}, {"raw": b"ok", "status": "found"}):
            with pytest.raises(ValidationError):
                BlobContent.model_validate(invalid)


class TestHttpClassification:
    @pytest.mark.parametrize("status", [404, 401, 403, 429, 500, 400, 302])
    async def test_only_a_confirmed_404_proves_absence(self, blob_case, status):
        missing(blob_case, httpx.Response(status))
        if status == 404:
            assert (
                await blob_case.repositories.read_blob(
                    blob_case.repo, blob_case.sha, blob_case.path
                )
                is None
            )
        else:
            with pytest.raises(ReadUnavailable):
                await blob_case.repositories.read_blob(
                    blob_case.repo, blob_case.sha, blob_case.path
                )


class TestStrictDecode:
    async def test_declared_base64_decodes(self, blob_case):
        blob(blob_case, b"x = 1\n")
        assert (
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)
            == b"x = 1\n"
        )

    async def test_probed_base64_without_encoding_field(self, blob_case):
        # Retired heuristic: an omitted encoding is failed evidence, even if decodable.
        document = blob(blob_case, b"x = 1\n")
        document.pop("encoding")
        with pytest.raises(ReadUnavailable):
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)

    async def test_embedded_newlines_in_base64_are_tolerated(self, blob_case):
        document = blob(blob_case, b"keep\nold\ntail\n")
        document["content"] = document["content"][:8] + "\r\n" + document["content"][8:]
        assert (
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)
            == b"keep\nold\ntail\n"
        )

    async def test_plain_text_falls_back_when_not_base64(self, blob_case):
        # Retired plaintext fallback: native bytes require explicit encoding and hash.
        document = blob(blob_case, b"# plain\n")
        document["content"], document["encoding"] = "# plain\n", None
        with pytest.raises(ReadUnavailable):
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)

    async def test_invalid_utf8_is_incomplete_never_replaced(self, blob_case):
        blob(blob_case, b"\xff\xfe binary \x80")
        result = await blob_case.repositories.read_blob(
            blob_case.repo, blob_case.sha, blob_case.path
        )
        with pytest.raises(UnicodeDecodeError):
            result.decode("utf-8")
        assert result == b"\xff\xfe binary \x80"

    async def test_undeclarable_base64_is_incomplete(self, blob_case):
        blob(blob_case, b"ok")["content"] = "!!!not base64!!!"
        with pytest.raises(ReadUnavailable):
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)

    async def test_empty_payload_is_a_found_empty_file(self, blob_case):
        blob(blob_case, b"")
        assert (
            await blob_case.repositories.read_blob(blob_case.repo, blob_case.sha, blob_case.path)
            == b""
        )


class TestGitLabReadBlob:
    async def test_found_read_carries_content_and_digest(self, gitlab):
        blob(gitlab, b"x = 1\n")
        result = await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, gitlab.path)
        assert result == b"x = 1\n"
        assert hashlib.sha256(result).hexdigest() == hashlib.sha256(b"x = 1\n").hexdigest()

    async def test_404_is_the_only_confirmed_absence(self, gitlab):
        missing(gitlab, httpx.Response(404))
        assert await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, gitlab.path) is None

    @pytest.mark.parametrize("status", [401, 403, 400])
    async def test_failures_never_prove_absence(self, gitlab, status):
        missing(gitlab, httpx.Response(status))
        with pytest.raises(ReadUnavailable):
            await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, gitlab.path)

    async def test_transport_timeout_is_unavailable(self, gitlab):
        missing(gitlab, timeout)
        with pytest.raises(ReadUnavailable):
            await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, gitlab.path)

    async def test_invalid_utf8_payload_is_incomplete(self, gitlab):
        blob(gitlab, b"\xff\xfe\x00\x80")
        raw = await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, gitlab.path)
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        assert raw == b"\xff\xfe\x00\x80"


class TestGitHubReadBlob:
    async def test_found_read_carries_content_and_digest(self, github):
        blob(github, b"x = 1\n")
        result = await github.repositories.read_blob(github.repo, github.sha, github.path)
        assert result == b"x = 1\n"
        assert hashlib.sha256(result).hexdigest() == hashlib.sha256(b"x = 1\n").hexdigest()

    async def test_over_1mb_file_resolved_through_the_blob_api(self, github):
        body = b"x" * (1024 * 1024 + 1)
        blob(github, body)
        assert await github.repositories.read_blob(github.repo, github.sha, github.path) == body
        assert not any("/contents/" in request.url.path for request in github.wire.calls)

    async def test_404_is_the_only_confirmed_absence(self, github):
        missing(github, httpx.Response(404))
        assert await github.repositories.read_blob(github.repo, github.sha, github.path) is None

    @pytest.mark.parametrize("status", [401, 403, 422])
    async def test_failures_never_prove_absence(self, github, status):
        missing(github, httpx.Response(status))
        with pytest.raises(ReadUnavailable):
            await github.repositories.read_blob(github.repo, github.sha, github.path)

    async def test_rate_limit_is_unavailable_not_forbidden(self, github):
        missing(github, httpx.Response(403, headers={"Retry-After": "60"}))
        with pytest.raises(ReadUnavailable) as failure:
            await github.repositories.read_blob(github.repo, github.sha, github.path)
        assert "rate_limited" in failure.value.evidence

    async def test_transport_timeout_is_unavailable(self, github):
        missing(github, timeout)
        with pytest.raises(ReadUnavailable):
            await github.repositories.read_blob(github.repo, github.sha, github.path)

    async def test_invalid_utf8_blob_is_incomplete(self, github):
        blob(github, b"\xff\xfe\x00\x80")
        raw = await github.repositories.read_blob(github.repo, github.sha, github.path)
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        assert raw == b"\xff\xfe\x00\x80"
