"""Byte-preserving strict patch application and content digest checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

FORGE_MATERIALIZE_MAX_FILE_CHARS = 400_000


class CandidateError(Exception):
    """A candidate diff cannot be parsed/applied; *reason* is machine-readable."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class HunkLine:
    """One line of a hunk body: ``tag`` is ``" "``, ``"-"`` or ``"+"``."""

    tag: str
    text: str
    no_newline: bool = False


@dataclass(frozen=True)
class DiffHunk:
    """A parsed ``@@ -a,b +c,d @@`` hunk with its body lines."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[HunkLine, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(self.lines))


_SHA256_PREFIX = "sha256:"


_BLOB_PREFIX = "blob:"


_HEX = "0123456789abcdef"


def _validated_digest(digest: str) -> str:
    """Validate a tagged digest claim; ``ValueError`` on a malformed format."""
    if digest == "":
        return ""
    for prefix, lengths in ((_SHA256_PREFIX, (64,)), (_BLOB_PREFIX, (40, 64))):
        if digest.startswith(prefix):
            hex_part = digest[len(prefix) :]
            if len(hex_part) in lengths and all(c in _HEX for c in hex_part):
                return digest
            break
    raise ValueError(f"malformed digest claim: {digest!r}")


def _sha256_digest(text: str | bytes) -> str:
    """The tagged sha256 digest of *text*'s utf-8 bytes."""
    return (
        _SHA256_PREFIX
        + hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()
    )


def _blob_digests(text: str | bytes) -> tuple[str, ...]:
    """The tagged git blob OIDs *text* would hash to (sha1 and sha256 repos)."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    header = b"blob %d\x00" % len(data)
    return (
        _BLOB_PREFIX + hashlib.sha1(header + data).hexdigest(),
        _BLOB_PREFIX + hashlib.sha256(header + data).hexdigest(),
    )


def _digest_matches(content: str | bytes, claim: str) -> bool:
    """Whether *content*'s digest equals the tagged *claim*."""
    if claim.startswith(_SHA256_PREFIX):
        return claim == _sha256_digest(content)
    if claim.startswith(_BLOB_PREFIX):
        return claim in _blob_digests(content)
    return False


def apply_unified_hunks(
    base_text: str, hunks: tuple[DiffHunk, ...], *, path: str = "<data>"
) -> str:
    """Apply *hunks* to *base_text* with strict line matching."""
    where = f"{path}: "
    base_lines, base_ends_nl = _split_text(base_text)
    out: list[str] = []
    pos = 0
    last_emit_no_nl: bool | None = None
    last_old_no_nl: bool | None = None

    for hunk in hunks:
        consumed_old = sum(1 for item in hunk.lines if item.tag in (" ", "-"))
        emitted_new = sum(1 for item in hunk.lines if item.tag in (" ", "+"))
        if consumed_old != hunk.old_count or emitted_new != hunk.new_count:
            raise CandidateError("corrupt_patch", f"{where}hunk counts disagree with the hunk body")
        target = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        target = max(target, 0)
        if target < pos:
            raise CandidateError("patch_does_not_apply", f"{where}hunk overlaps the previous hunk")
        out.extend(base_lines[pos:target])
        pos = target
        for item in hunk.lines:
            if item.tag == "+":
                out.append(item.text)
                last_emit_no_nl = item.no_newline
                continue
            if pos >= len(base_lines) or base_lines[pos] != item.text:
                raise CandidateError(
                    "patch_does_not_apply",
                    f"{where}line {pos + 1} does not match the base (expected {item.text!r})",
                )
            if item.tag == " ":
                out.append(item.text)
                last_emit_no_nl = item.no_newline
            pos += 1
            last_old_no_nl = item.no_newline

    tail = base_lines[pos:]
    out.extend(tail)

    if pos >= len(base_lines) and last_old_no_nl is not None:
        # The diff consumed the base to EOF: its trailing-newline claim about the old side
        # must match the base exactly.
        if base_ends_nl == last_old_no_nl:
            raise CandidateError(
                "patch_does_not_apply", f"{where}trailing-newline mismatch on the base"
            )

    if tail:
        new_ends_nl = base_ends_nl
    elif last_emit_no_nl is None:
        new_ends_nl = True  # nothing emitted — empty new content
    else:
        new_ends_nl = not last_emit_no_nl
    return _join_lines(out, new_ends_nl)


def _split_text(text: str) -> tuple[list[str], bool]:
    """Split on ``"\n"`` ONLY into (lines without terminators, ends_with_newline)."""
    if text == "":
        return [], True
    lines = text.split("\n")
    if lines[-1] == "":
        return lines[:-1], True
    return lines, False


def _join_lines(lines: list[str], ends_with_newline: bool) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if ends_with_newline else "")


def _verify_base_digest(path: str, base: str | bytes, claim: str) -> None:
    """Reject a stale base BEFORE anything is applied (ADR-0016 honesty)."""
    if not _digest_matches(base, claim):
        raise CandidateError(
            "stale_base",
            f"{path}: base content does not match the digest the candidate was "
            f"diffed against ({claim[:20]}…) — the attempt base moved",
        )


def _verify_result_digest(path: str, content: str | bytes, claim: str) -> None:
    """The applied result must equal the entry's claim — never publish otherwise."""
    if claim and not _digest_matches(content, claim):
        raise CandidateError(
            "result_digest_mismatch",
            f"{path}: materialized content does not match the intended digest "
            f"({claim[:20]}…) — internal applier error, refusing to publish",
        )


def _enforce_cap(path: str, content: str) -> None:
    if len(content) > FORGE_MATERIALIZE_MAX_FILE_CHARS:
        raise CandidateError(
            "file_too_large",
            f"{path}: {len(content)} chars exceeds the materialization cap of "
            f"{FORGE_MATERIALIZE_MAX_FILE_CHARS}",
        )
