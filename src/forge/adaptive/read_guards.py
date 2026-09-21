"""Typed read guards: authoritative-read failures fail closed (FND-03, R07).

A failed authoritative read is not evidence that a path is absent. The
bridges' broad ``APIError → missing file`` mappings let a 403, a 429, a
5xx or a truncated body silently flip a proposed UPDATE into a CREATE —
the publisher "created" a file that existed all along. This module is
the adaptive substrate's provider-neutral contract that ends that:

- :class:`TypedReadOutcome` — the outcome of ONE read with ``status ∈
  {found, absent, forbidden, unavailable, incomplete, unsupported}``:
  only a provider-confirmed absence is ``absent``; 401/403 are
  ``forbidden``; 429/5xx/transports are ``unavailable``; a payload that
  exists but is unusable (empty, oversized, truncated) is
  ``incomplete``; a file we refuse to interpret (non-UTF-8 bytes, a
  symlink's mode marker) is ``unsupported``.
- :func:`may_create` — ONLY ``absent`` (a confirmed 404-class result)
  grants create permission.
- :func:`may_update` — ONLY a ``found`` outcome whose content is
  non-empty AND whose sha256 re-verifies grants update permission, so a
  proposed update can never change its operation because evidence was
  unavailable.
- :func:`classify_http` — the shared HTTP status classifier.
- :func:`decode_blob` — the strict blob decoder with explicit
  non-UTF-8 / symlink / oversized decisions (never ``replace``-mangled).

Pure stdlib: every publisher and materializer imports this without
cycle or dependency risk (the same posture as the GitLab-shaped
``forge.gitlab.blob_reads`` contract this generalizes for the adaptive
publishers).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "MAX_BLOB_BYTES",
    "TypedReadOutcome",
    "TypedReadStatus",
    "classify_http",
    "decode_blob",
    "may_create",
    "may_update",
]

TypedReadStatus = Literal[
    "found", "absent", "forbidden", "unavailable", "incomplete", "unsupported"
]

#: The ONLY statuses a read may produce (validated in ``__post_init__``).
_STATUSES = ("found", "absent", "forbidden", "unavailable", "incomplete", "unsupported")

#: The default materialization cap — a blob larger than this is refused
#: as ``incomplete`` rather than decoded into memory on a runner.
MAX_BLOB_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class TypedReadOutcome:
    """The outcome of ONE authoritative read a publisher gates on.

    FND-03 honesty rules, enforced by the guards below:

    - ``content``/``sha256`` are set ONLY when ``status == "found"`` —
      content is the COMPLETE decoded text and ``sha256`` is the plain
      lowercase hex sha256 of its utf-8 bytes (the caller's cheap
      re-verification hook).
    - ``detail`` is a human-readable fragment for blocked-run evidence —
      empty for ``found``, populated otherwise (never the raw private
      content).
    """

    status: TypedReadStatus
    content: str = ""
    sha256: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"unknown typed read status: {self.status!r}")

    @classmethod
    def found(cls, content: str) -> TypedReadOutcome:
        """A successful read of *content* (full text), digest computed here."""
        return cls(
            status="found",
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def absent(cls, detail: str = "") -> TypedReadOutcome:
        """Provider-CONFIRMED absence — the ONLY status that may allow a create."""
        return cls(status="absent", detail=detail)

    @classmethod
    def forbidden(cls, detail: str = "") -> TypedReadOutcome:
        """401/403: the credential may not read this path — absence NOT proven."""
        return cls(status="forbidden", detail=detail)

    @classmethod
    def unavailable(cls, detail: str = "") -> TypedReadOutcome:
        """429/5xx/transport: transient failure — absence NOT proven."""
        return cls(status="unavailable", detail=detail)

    @classmethod
    def incomplete(cls, detail: str = "") -> TypedReadOutcome:
        """The path exists but the payload is unusable (empty, truncated,
        oversized) — evidence is incomplete, absence NOT proven."""
        return cls(status="incomplete", detail=detail)

    @classmethod
    def unsupported(cls, detail: str = "") -> TypedReadOutcome:
        """The blob is a kind we refuse to interpret (non-UTF-8 bytes, a
        symlink) — an explicit supported/unsupported decision, never a guess."""
        return cls(status="unsupported", detail=detail)


def may_create(outcome: TypedReadOutcome) -> bool:
    """Whether this outcome permits creating the path (fail closed).

    ONLY a provider-confirmed ``absent`` grants create permission: a
    forbidden, unavailable, incomplete or unsupported read leaves
    existence UNKNOWN, and treating any of them as absence is exactly
    the silent update→create flip FND-03 exists to prevent.
    """
    return outcome.status == "absent"


def may_update(outcome: TypedReadOutcome) -> bool:
    """Whether this outcome permits updating the path (fail closed).

    ONLY a ``found`` outcome with non-empty content whose sha256
    re-verifies (recomputed here and compared) grants update permission.
    The digest re-verification is not paranoia: a truncated or mangled
    payload would otherwise authorize an update against content the
    provider never sent. Every other status refuses — a proposed update
    cannot change its operation because evidence was unavailable.
    """
    if outcome.status != "found" or not outcome.content or not outcome.sha256:
        return False
    return hashlib.sha256(outcome.content.encode("utf-8")).hexdigest() == outcome.sha256


def classify_http(status: int, detail: str = "") -> TypedReadOutcome:
    """Classify a provider API response by HTTP status (FND-03 taxonomy).

    404 → ``absent``; 401/403 → ``forbidden``; 429 and 5xx →
    ``unavailable``; a 200 that delivered NO body → ``incomplete`` (for
    a 200, *detail* carries the response body: an empty body proved
    nothing about the content, a non-empty body is a ``found`` read);
    everything else → ``unavailable``. Conservative by design: no status
    other than a provider-confirmed 404 may vouch for absence.
    """
    if status == 404:
        return TypedReadOutcome.absent(detail)
    if status in (401, 403):
        return TypedReadOutcome.forbidden(detail)
    if status == 429 or 500 <= status < 600:
        return TypedReadOutcome.unavailable(detail)
    if status == 200:
        if not detail:
            return TypedReadOutcome.incomplete("200 with empty body")
        return TypedReadOutcome.found(detail)
    return TypedReadOutcome.unavailable(detail)


def decode_blob(raw: bytes, max_bytes: int = MAX_BLOB_BYTES) -> TypedReadOutcome:
    """STRICTLY decode a raw blob payload to a found outcome (FND-03).

    Explicit decisions, in order: over *max_bytes* → ``incomplete``
    (never materialize an oversized blob on a runner); not valid UTF-8 →
    ``unsupported("non-utf8 blob")`` (bytes are never
    ``errors="replace"``-mangled into text that then feeds materialization
    or digest verification); empty → ``incomplete``; a symlink's
    ``mode:120000`` marker → ``unsupported("symlink")`` (a link target is
    a path reference, not file content); otherwise a ``found`` read with
    the digest computed here.
    """
    if len(raw) > max_bytes:
        return TypedReadOutcome.incomplete(f"blob exceeds max_bytes={max_bytes}")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return TypedReadOutcome.unsupported("non-utf8 blob")
    if not text:
        return TypedReadOutcome.incomplete("empty blob")
    if text.startswith("mode:120000"):
        return TypedReadOutcome.unsupported("symlink")
    return TypedReadOutcome.found(text)
