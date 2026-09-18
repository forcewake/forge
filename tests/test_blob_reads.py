"""Typed authoritative blob reads (R14): classification per provider adapter.

Missing, unreadable and incomplete evidence are different facts. These tests
pin the taxonomy at the adapter boundary — ONLY a provider-confirmed 404 is
``not_found``; 401/403 are ``forbidden``; timeouts/rate limits/other API
failures are ``unavailable``; undecodable payloads are ``incomplete`` — and
the strict decoder that never ``errors="replace"``-mangles bytes.
"""

import base64
import hashlib
from urllib.parse import urlencode

import httpx
import pytest
from pytest_httpx import HTTPXMock

from forge.gitlab.blob_reads import (
    BlobReadResult,
    blob_result_for_http_status,
    decode_blob_content,
)
from forge.gitlab.client import GitLabClient
from forge.integrations.azure import AzureDevOpsClient, AzureRepositoryReader
from forge.integrations.github import GitHubClient, GitHubRepositoryReader

GL_BASE = "https://gitlab.test/api/v4"
GH_BASE = "https://api.github.test"
GH_CONTENTS = f"{GH_BASE}/repos/acme/widget/contents/src/app.py"
AZ_BASE = "https://dev.azure.test/fabrikam"
AZ_ITEMS = f"{AZ_BASE}/Fabrikam/_apis/git/repositories/core/items"
AZ_SHA = "9c4e2a7f1b3d8e5a60c2f47b19d3a8e07f5c6b21"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _StaticToken:
    async def token(self) -> str:
        return "ghs_test"

    async def invalidate(self) -> None:
        return None


@pytest.fixture()
async def gitlab_client():
    client = GitLabClient(base_url="https://gitlab.test", token="t", timeout=5.0)
    async with client:
        yield client


@pytest.fixture()
async def gh_reader():
    client = GitHubClient(base_url=GH_BASE, token_provider=_StaticToken(), timeout=5.0)
    async with client:
        yield GitHubRepositoryReader(client, "acme", "widget")


@pytest.fixture()
async def az_reader():
    client = AzureDevOpsClient(base_url=AZ_BASE, token="pat", timeout=5.0)
    async with client:
        yield AzureRepositoryReader(client, "Fabrikam", "core")


@pytest.fixture()
def no_backoff(monkeypatch):
    """Absorb the transports' exponential backoff so failure paths stay fast."""

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("forge.gitlab.client.asyncio.sleep", _instant)
    monkeypatch.setattr("forge.integrations.github.asyncio.sleep", _instant)
    monkeypatch.setattr("forge.integrations.azure.asyncio.sleep", _instant)


# ----------------------------------------------------------------------
# BlobReadResult contract
# ----------------------------------------------------------------------


class TestBlobReadResult:
    def test_found_computes_sha256_of_utf8_bytes(self):
        result = BlobReadResult.found("hello\n")
        assert result.usable
        assert result.text() == "hello\n"
        assert result.content_sha256 == hashlib.sha256(b"hello\n").hexdigest()
        assert result.encoding == "utf-8"

    def test_found_accepts_bytes(self):
        result = BlobReadResult.found(b"\x00\x01", encoding="binary")
        assert result.content_sha256 == hashlib.sha256(b"\x00\x01").hexdigest()
        assert result.encoding == "binary"

    def test_non_found_statuses_carry_detail_and_no_content(self):
        factory = {
            "not_found": BlobReadResult.not_found,
            "forbidden": BlobReadResult.forbidden,
            "unavailable": BlobReadResult.unavailable,
            "incomplete": BlobReadResult.incomplete,
        }
        for status, build in factory.items():
            result = build("because")
            assert result.status == status
            assert result.detail == "because"
            assert result.content is None
            assert not result.usable

    def test_only_not_found_is_confirmed_absent(self):
        assert BlobReadResult.not_found().confirmed_absent
        for status in ("forbidden", "unavailable", "incomplete"):
            result = BlobReadResult(status)
            assert not result.confirmed_absent

    def test_text_raises_without_found_content(self):
        with pytest.raises(ValueError, match="no text content"):
            BlobReadResult.not_found().text()

    def test_invariants_are_enforced(self):
        with pytest.raises(ValueError, match="unknown blob read status"):
            BlobReadResult("missing")
        with pytest.raises(ValueError, match="must carry content"):
            BlobReadResult("found")
        with pytest.raises(ValueError, match="must carry content_sha256"):
            BlobReadResult("found", content="x")
        with pytest.raises(ValueError, match="must not carry content"):
            BlobReadResult("forbidden", content="x")


class TestHttpClassification:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (404, "not_found"),
            (401, "forbidden"),
            (403, "forbidden"),
            (429, "unavailable"),
            (500, "unavailable"),
            (400, "unavailable"),
            (0, "unavailable"),
        ],
    )
    def test_only_a_confirmed_404_proves_absence(self, status, expected):
        result = blob_result_for_http_status(status, "detail")
        assert result.status == expected
        assert (result.status == "not_found") == result.confirmed_absent


class TestStrictDecode:
    def test_declared_base64_decodes(self):
        result = decode_blob_content(b64("x = 1\n".encode()), "base64", path="a.py", ref="s")
        assert result.text() == "x = 1\n"

    def test_probed_base64_without_encoding_field(self):
        result = decode_blob_content(b64("x = 1\n".encode()), None, path="a.py", ref="s")
        assert result.text() == "x = 1\n"

    def test_embedded_newlines_in_base64_are_tolerated(self):
        padded = b64(b"keep\nold\ntail\n")
        wrapped = "\n".join(padded[i : i + 30] for i in range(0, len(padded), 30))
        result = decode_blob_content(wrapped, "base64", path="a.py", ref="s")
        assert result.text() == "keep\nold\ntail\n"

    def test_plain_text_falls_back_when_not_base64(self):
        result = decode_blob_content("# not base64!\n", None, path="a.md", ref="s")
        assert result.text() == "# not base64!\n"

    def test_invalid_utf8_is_incomplete_never_replaced(self):
        result = decode_blob_content(b64(b"\xff\xfe binary \x80"), "base64", path="b.bin", ref="s")
        assert result.status == "incomplete"
        assert "not valid UTF-8" in result.detail
        assert result.content is None

    def test_undeclarable_base64_is_incomplete(self):
        result = decode_blob_content("!!!not base64!!!", "base64", path="b.txt", ref="s")
        assert result.status == "incomplete"
        assert "undecodable" in result.detail

    def test_empty_payload_is_a_found_empty_file(self):
        result = decode_blob_content("", "base64", path="empty.txt", ref="s")
        assert result.usable
        assert result.text() == ""
        assert result.content_sha256 == hashlib.sha256(b"").hexdigest()


# ----------------------------------------------------------------------
# GitLab adapter
# ----------------------------------------------------------------------


class TestGitLabReadBlob:
    async def test_found_read_carries_content_and_digest(
        self, httpx_mock: HTTPXMock, gitlab_client: GitLabClient
    ):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/repository/files/src%2Fapp.py?ref=base-1",
            json={
                "file_name": "app.py",
                "file_path": "src/app.py",
                "size": 7,
                "encoding": "base64",
                "content": b64(b"x = 1\n"),
                "ref": "base-1",
            },
        )

        result = await gitlab_client.read_blob(42, "src/app.py", ref="base-1")

        assert result.usable
        assert result.text() == "x = 1\n"
        assert result.content_sha256 == hashlib.sha256(b"x = 1\n").hexdigest()

    async def test_404_is_the_only_confirmed_absence(
        self, httpx_mock: HTTPXMock, gitlab_client: GitLabClient
    ):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/repository/files/gone%2Fpy?ref=base-1",
            status_code=404,
            json={"message": "404 file not found"},
        )

        result = await gitlab_client.read_blob(42, "gone/py", ref="base-1")

        assert result.confirmed_absent
        assert "404" in result.detail

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(401, "forbidden"), (403, "forbidden"), (400, "unavailable")],
    )
    async def test_failures_never_prove_absence(
        self, httpx_mock: HTTPXMock, gitlab_client: GitLabClient, status, expected
    ):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/repository/files/src%2Fapp.py?ref=base-1",
            status_code=status,
            json={"message": "nope"},
        )

        result = await gitlab_client.read_blob(42, "src/app.py", ref="base-1")

        assert result.status == expected
        assert not result.confirmed_absent

    async def test_transport_timeout_is_unavailable(
        self,
        httpx_mock: HTTPXMock,
        gitlab_client: GitLabClient,
        no_backoff,  # noqa: ARG001 — fixture patches the backoff sleeps
    ):
        for _ in range(3):  # the transport retries the connect error twice first
            httpx_mock.add_exception(httpx.ConnectTimeout("timed out"))

        result = await gitlab_client.read_blob(42, "src/app.py", ref="base-1")

        assert result.status == "unavailable"
        assert "transport" in result.detail

    async def test_invalid_utf8_payload_is_incomplete(
        self, httpx_mock: HTTPXMock, gitlab_client: GitLabClient
    ):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/repository/files/blob%2Fbin?ref=base-1",
            json={
                "file_name": "bin",
                "file_path": "blob/bin",
                "size": 4,
                "encoding": "base64",
                "content": b64(b"\xff\xfe\x00\x80"),
                "ref": "base-1",
            },
        )

        result = await gitlab_client.read_blob(42, "blob/bin", ref="base-1")

        assert result.status == "incomplete"
        assert "not valid UTF-8" in result.detail


# ----------------------------------------------------------------------
# GitHub adapter
# ----------------------------------------------------------------------


class TestGitHubReadBlob:
    async def test_found_read_carries_content_and_digest(
        self, httpx_mock: HTTPXMock, gh_reader: GitHubRepositoryReader
    ):
        httpx_mock.add_response(
            url=GH_CONTENTS,
            json={
                "type": "file",
                "name": "app.py",
                "path": "src/app.py",
                "size": 7,
                "sha": "b" * 40,
                "encoding": "base64",
                "content": b64(b"x = 1\n"),
            },
        )

        result = await gh_reader.read_blob(0, "src/app.py")

        assert result.usable
        assert result.text() == "x = 1\n"
        assert result.content_sha256 == hashlib.sha256(b"x = 1\n").hexdigest()

    async def test_over_1mb_file_resolved_through_the_blob_api(
        self, httpx_mock: HTTPXMock, gh_reader: GitHubRepositoryReader
    ):
        # Contents API delivers NO inline content above 1 MB — the reader
        # resolves the sha through the git blobs API (embedded newlines and all).
        encoded = b64(b"big = True\n")
        httpx_mock.add_response(
            url=GH_CONTENTS,
            json={
                "type": "file",
                "path": "src/app.py",
                "size": 2_000_000,
                "sha": "b" * 40,
                "content": "",
            },
        )
        httpx_mock.add_response(
            url=f"{GH_BASE}/repos/acme/widget/git/blobs/{'b' * 40}",
            json={
                "sha": "b" * 40,
                "content": encoded[:8] + "\n" + encoded[8:],
                "encoding": "base64",
            },
        )

        result = await gh_reader.read_blob(0, "src/app.py")

        assert result.usable
        assert result.text() == "big = True\n"

    async def test_404_is_the_only_confirmed_absence(
        self, httpx_mock: HTTPXMock, gh_reader: GitHubRepositoryReader
    ):
        httpx_mock.add_response(url=GH_CONTENTS, status_code=404, json={"message": "Not Found"})

        result = await gh_reader.read_blob(0, "src/app.py")

        assert result.confirmed_absent

    @pytest.mark.parametrize(
        ("status", "expected", "responses"),
        [(401, "forbidden", 2), (403, "forbidden", 1), (422, "unavailable", 1)],
    )
    async def test_failures_never_prove_absence(
        self, httpx_mock: HTTPXMock, gh_reader: GitHubRepositoryReader, status, expected, responses
    ):
        # 401 triggers exactly one token re-mint + retry (never processed),
        # so it needs a second registered answer before surfacing.
        for _ in range(responses):
            httpx_mock.add_response(url=GH_CONTENTS, status_code=status, json={"message": "nope"})

        result = await gh_reader.read_blob(0, "src/app.py")

        assert result.status == expected
        assert not result.confirmed_absent

    async def test_rate_limit_is_unavailable_not_forbidden(
        self, httpx_mock: HTTPXMock, gh_reader: GitHubRepositoryReader
    ):
        # A 403 WITH rate-limit headers is transient saturation — parked as
        # unavailable, never conflated with a permission wall (and never
        # with absence).
        httpx_mock.add_response(
            url=GH_CONTENTS,
            status_code=403,
            headers={"Retry-After": "60"},
            json={"message": "API rate limit exceeded"},
        )

        result = await gh_reader.read_blob(0, "src/app.py")

        assert result.status == "unavailable"
        assert "rate limit" in result.detail

    async def test_transport_timeout_is_unavailable(
        self, httpx_mock: HTTPXMock, gh_reader: GitHubRepositoryReader, no_backoff
    ):
        # The GitHub transport never retries a transport-level exception —
        # one failure surfaces immediately as unavailable.
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))

        result = await gh_reader.read_blob(0, "src/app.py")

        assert result.status == "unavailable"

    async def test_invalid_utf8_blob_is_incomplete(
        self, httpx_mock: HTTPXMock, gh_reader: GitHubRepositoryReader
    ):
        httpx_mock.add_response(
            url=GH_CONTENTS,
            json={
                "type": "file",
                "path": "src/app.py",
                "size": 4,
                "sha": "c" * 40,
                "content": "",
            },
        )
        httpx_mock.add_response(
            url=f"{GH_BASE}/repos/acme/widget/git/blobs/{'c' * 40}",
            json={"sha": "c" * 40, "content": b64(b"\xff\xfe\x00\x80"), "encoding": "base64"},
        )

        result = await gh_reader.read_blob(0, "src/app.py")

        assert result.status == "incomplete"
        assert "not valid UTF-8" in result.detail


# ----------------------------------------------------------------------
# Azure DevOps adapter
# ----------------------------------------------------------------------


def _az_item_url(path: str, **params: str) -> str:
    return f"{AZ_ITEMS}?{urlencode({'api-version': '7.1', 'path': path, 'includeContent': 'true', **params})}"


class TestAzureReadBlob:
    async def test_found_read_carries_content_and_digest(
        self, httpx_mock: HTTPXMock, az_reader: AzureRepositoryReader
    ):
        httpx_mock.add_response(
            url=_az_item_url(
                "/src/app.py",
                **{
                    "versionDescriptor.version": AZ_SHA,
                    "versionDescriptor.versionType": "commit",
                },
            ),
            json={"path": "/src/app.py", "content": "x = 1\n", "isSymLink": False},
        )

        result = await az_reader.read_blob(0, "/src/app.py", ref=AZ_SHA)

        assert result.usable
        assert result.text() == "x = 1\n"
        assert result.content_sha256 == hashlib.sha256(b"x = 1\n").hexdigest()

    async def test_404_is_confirmed_absence(
        self, httpx_mock: HTTPXMock, az_reader: AzureRepositoryReader
    ):
        httpx_mock.add_response(
            url=_az_item_url(
                "/gone.py",
                **{
                    "versionDescriptor.version": AZ_SHA,
                    "versionDescriptor.versionType": "commit",
                },
            ),
            status_code=404,
            json={"message": "TF401174: not found"},
        )

        result = await az_reader.read_blob(0, "/gone.py", ref=AZ_SHA)

        assert result.confirmed_absent

    async def test_401_is_forbidden(self, httpx_mock: HTTPXMock, az_reader: AzureRepositoryReader):
        httpx_mock.add_response(
            url=_az_item_url(
                "/src/app.py",
                **{
                    "versionDescriptor.version": AZ_SHA,
                    "versionDescriptor.versionType": "commit",
                },
            ),
            status_code=401,
            json={"message": "TF400813: resource not available for anonymous access"},
        )

        result = await az_reader.read_blob(0, "/src/app.py", ref=AZ_SHA)

        assert result.status == "forbidden"
        assert not result.confirmed_absent

    async def test_missing_inline_content_is_incomplete(
        self, httpx_mock: HTTPXMock, az_reader: AzureRepositoryReader
    ):
        # The reader's own honesty refusal (no inline content) must stay
        # incomplete — the path EXISTS, it just delivered nothing usable.
        httpx_mock.add_response(
            url=_az_item_url(
                "/big.bin",
                **{
                    "versionDescriptor.version": AZ_SHA,
                    "versionDescriptor.versionType": "commit",
                },
            ),
            json={"path": "/big.bin", "contentMetadata": {"size": 10_000_000}},
        )

        result = await az_reader.read_blob(0, "/big.bin", ref=AZ_SHA)

        assert result.status == "incomplete"
        assert "no inline content" in result.detail

    async def test_symlink_is_incomplete(
        self, httpx_mock: HTTPXMock, az_reader: AzureRepositoryReader
    ):
        httpx_mock.add_response(
            url=_az_item_url(
                "/link",
                **{
                    "versionDescriptor.version": AZ_SHA,
                    "versionDescriptor.versionType": "commit",
                },
            ),
            json={"path": "/link", "content": "../secret", "isSymLink": True},
        )

        result = await az_reader.read_blob(0, "/link", ref=AZ_SHA)

        assert result.status == "incomplete"
        assert "symlink" in result.detail

    async def test_throttle_is_unavailable(
        self, httpx_mock: HTTPXMock, az_reader: AzureRepositoryReader, no_backoff
    ):
        url = _az_item_url(
            "/src/app.py",
            **{"versionDescriptor.version": AZ_SHA, "versionDescriptor.versionType": "commit"},
        )
        httpx_mock.add_response(
            url=url, status_code=429, headers={"Retry-After": "0"}, json={"message": "TSTU"}
        )
        httpx_mock.add_response(
            url=url, status_code=429, headers={"Retry-After": "0"}, json={"message": "TSTU"}
        )

        result = await az_reader.read_blob(0, "/src/app.py", ref=AZ_SHA)

        assert result.status == "unavailable"

    async def test_transport_error_is_unavailable(
        self, httpx_mock: HTTPXMock, az_reader: AzureRepositoryReader, no_backoff
    ):
        for _ in range(3):
            httpx_mock.add_exception(httpx.ConnectError("boom"))

        result = await az_reader.read_blob(0, "/src/app.py", ref=AZ_SHA)

        assert result.status == "unavailable"
