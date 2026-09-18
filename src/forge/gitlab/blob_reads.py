"""Typed result of one authoritative blob read (review finding R14).

The authoritative base-content readers used to conflate three different
facts — "the file does not exist", "the read failed", and "the read
delivered something we cannot use" — because they caught ANY exception and
treated the path as missing. A 403, a timeout or an undecodable payload
thus "proved" non-existence, and an update could silently flip into a
create (R14).

This module is the provider-neutral read contract that ends that:

- :class:`BlobReadResult` — the outcome of ONE read, with
  ``status ∈ {found, not_found, forbidden, unavailable, incomplete}``:
  only a provider-confirmed 404-style absence is ``not_found``; 401/403
  are ``forbidden``; timeouts / 429 / 5xx / network failures are
  ``unavailable``; a payload that cannot be decoded (invalid UTF-8, no
  inline content, non-regular file) is ``incomplete``.
- :func:`blob_result_for_http_status` — the shared status-code classifier.
- :func:`decode_blob_content` — the STRICT payload decoder: like the legacy
  helpers it probes base64 when the encoding field is absent, but unlike
  them it never ``errors="replace"``-mangles bytes — an undecodable payload
  is ``incomplete``, never silently corrupted text.

Pure stdlib: every provider adapter (GitLab client, GitHub and Azure
repository readers) and every consumer (implementer, publisher) imports
this without cycle or dependency risk.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "AUTHORITATIVE_READ_FAILED",
    "BlobReadResult",
    "BlobReadStatus",
    "blob_result_for_http_status",
    "decode_blob_content",
]

#: The machine-readable failure reason every consumer of this contract
#: blocks a run with (``authoritative_read_failed: <detail>``).
AUTHORITATIVE_READ_FAILED = "authoritative_read_failed"

BlobReadStatus = Literal["found", "not_found", "forbidden", "unavailable", "incomplete"]

#: The ONLY statuses a read may produce (validated in ``__post_init__``).
_STATUSES = ("found", "not_found", "forbidden", "unavailable", "incomplete")


@dataclass(frozen=True)
class BlobReadResult:
    """The outcome of ONE authoritative read of a repository blob.

    R14 honesty rules, enforced by construction:

    - ``content``/``content_sha256``/``encoding`` are set ONLY when
      ``status == "found"`` — content is the COMPLETE decoded text (never
      truncated here; callers own the materialization cap), ``encoding`` is
      the text encoding (``"utf-8"``), and ``content_sha256`` is the plain
      lowercase hex sha256 of the content's utf-8 bytes (the R08/R09
      digest machinery stays tagged and provider-side; this digest is the
      caller's cheap integrity/recomputation hook).
    - ``detail`` is a human-readable fragment for blocked-run evidence —
      empty for ``found``, populated otherwise.
    """

    status: BlobReadStatus
    content: str | bytes | None = None
    content_sha256: str = ""
    encoding: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"unknown blob read status: {self.status!r}")
        if self.status == "found":
            if self.content is None:
                raise ValueError("a found blob read must carry content")
            if not self.content_sha256:
                raise ValueError("a found blob read must carry content_sha256")
        elif self.content is not None:
            raise ValueError(f"status {self.status!r} must not carry content")

    @classmethod
    def found(cls, content: str | bytes, *, encoding: str = "utf-8") -> BlobReadResult:
        """A successful read of *content* (full text), digest computed here."""
        raw = content.encode(encoding) if isinstance(content, str) else content
        return cls(
            status="found",
            content=content,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            encoding=encoding,
        )

    @classmethod
    def not_found(cls, detail: str = "") -> BlobReadResult:
        """Provider-confirmed absence — the ONLY status that proves a path
        is missing and may therefore allow a create."""
        return cls(status="not_found", detail=detail)

    @classmethod
    def forbidden(cls, detail: str = "") -> BlobReadResult:
        """401/403: the credential may not read this path — absence NOT proven."""
        return cls(status="forbidden", detail=detail)

    @classmethod
    def unavailable(cls, detail: str = "") -> BlobReadResult:
        """Timeout / network / 429 / 5xx: transient failure — absence NOT proven."""
        return cls(status="unavailable", detail=detail)

    @classmethod
    def incomplete(cls, detail: str = "") -> BlobReadResult:
        """The blob exists but the payload is unusable (undecodable, no inline
        content, non-regular file) — evidence is incomplete, absence NOT proven."""
        return cls(status="incomplete", detail=detail)

    @property
    def usable(self) -> bool:
        """Whether the read produced complete, usable content."""
        return self.status == "found"

    @property
    def confirmed_absent(self) -> bool:
        """Whether the provider CONFIRMED the path does not exist.

        Every other status leaves existence unknown — a consumer that
        treats them as absent can flip an update into a create (R14).
        """
        return self.status == "not_found"

    def text(self) -> str:
        """The decoded text of a ``found`` read; :class:`ValueError` otherwise."""
        if self.status != "found" or not isinstance(self.content, str):
            raise ValueError(f"blob read has no text content: status={self.status!r}")
        return self.content


def blob_result_for_http_status(status_code: int, detail: str = "") -> BlobReadResult:
    """Classify a provider API failure by HTTP status (R14 taxonomy).

    404 → ``not_found``; 401/403 → ``forbidden``; EVERYTHING else
    (throttles, 5xx, zero-status transport errors, unexpected 4xx) →
    ``unavailable``. Conservative by design: no status other than a
    provider-confirmed 404 may vouch for absence.
    """
    if status_code == 404:
        return BlobReadResult.not_found(detail)
    if status_code in (401, 403):
        return BlobReadResult.forbidden(detail)
    return BlobReadResult.unavailable(detail)


def decode_blob_content(
    raw_content: str,
    encoding: str | None,
    *,
    path: str,
    ref: str,
) -> BlobReadResult:
    """STRICTLY decode a provider repository-file payload to text (R14).

    Mirrors the legacy GitLab-shaped payload handling: ``encoding ==
    "base64"`` decodes as base64; otherwise base64 is probed first (some
    payloads omit the encoding field), falling back to raw text. Embedded
    newlines in base64 payloads are tolerated. Unlike the legacy decoders,
    an undecodable payload yields ``incomplete`` — bytes are never
    ``errors="replace"``-mangled into text that then silently feeds
    materialization or digest verification. An EMPTY payload is a valid
    found read (an existing empty file must not look missing).
    """
    if (encoding or "") == "base64":
        try:
            data = base64.b64decode("".join(raw_content.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            return BlobReadResult.incomplete(
                f"{path!r} at {ref!r}: declared base64 payload is undecodable ({exc})"
            )
    else:
        try:
            data = base64.b64decode("".join(raw_content.split()), validate=True)
        except (binascii.Error, ValueError):
            data = raw_content.encode("utf-8")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return BlobReadResult.incomplete(
            f"{path!r} at {ref!r}: content is not valid UTF-8 ({exc})"
        )
    return BlobReadResult.found(text)
