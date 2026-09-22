"""CandidateBundle: the proposal-only deliverable of a harness job (ADR-0016).

A harness lane never receives write credentials; its result is a **candidate
artifact** — a ``git diff --binary --full-index <attempt_base>`` capture plus a
small meta JSON (attempt base, driver id/model, exit classification, usage
receipt). This module is the trusted parser of that artifact:

- :func:`parse_unified_diff` turns the diff text into a
  :class:`CandidateBundle` of discriminated entries (:class:`CreateFile`,
  :class:`DeleteFile`, :class:`FullReplacement`, :class:`UnifiedPatch`) —
  exactly ONE representation per file (R08). Created files are reconstructed
  to FULL contents directly from the diff (a new-file diff is the whole
  file); modified files keep their raw unified hunks and are completed at
  publish time by applying them to the authoritative attempt-base blobs
  (:meth:`CandidateBundle.materialize`, strict line matching, no fuzz —
  on any mismatch the bundle is rejected with ``patch_does_not_apply``).
- Every patch/replacement entry carries ``base_blob_digest`` (a digest of the
  authoritative base content, verified BEFORE anything is applied — mismatch
  rejects with ``stale_base``) and ``intended_digest`` (a digest of the
  expected result, verified AFTER application — mismatch is an internal
  error, ``result_digest_mismatch``, and never publishes). Digests are
  tagged: ``sha256:<hex>`` of the content, or ``blob:<oid>`` — the full
  pre/post-image blob OIDs the ``--full-index`` capture itself carries.
- Every :class:`DiffHunk` stores ``old_count``/``new_count`` and the parser
  cross-checks them against the hunk body (R09): a deviation is
  ``corrupt_patch``. Placement follows POSIX: an empty old range
  (``@@ -3,0 ...``) inserts AFTER line 3, not before it.
- Binary deltas (``GIT binary patch``), renames/copies and mode-only changes
  are rejected with a typed reason — never silently dropped (R09).

Rejected parses raise :class:`CandidateError` with a machine-readable
``reason`` — the caller (backend/publisher) decides what the rejection means
for the run; nothing here trusts the harness's claims.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from forge.factory.implementer import FORGE_MATERIALIZE_MAX_FILE_CHARS

#: The manifest operations (``update`` in ChangeSet terms is ``modify``).
CandidateOperation = Literal["create", "delete", "modify"]

#: Usage receipt completeness (F22 lite): sums of per-turn receipts are
#: "aggregate"; a missing receipt is "unknown" — never zero, never invented.
UsageCompleteness = Literal["exact", "aggregate", "unknown"]

#: Digest schemes of ``base_blob_digest``/``intended_digest`` claims.
_SHA256_PREFIX = "sha256:"
_BLOB_PREFIX = "blob:"

_HEX = "0123456789abcdef"


class CandidateError(Exception):
    """A candidate diff cannot be parsed/applied; *reason* is machine-readable."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


# ----------------------------------------------------------------------
# Diff primitives
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class HunkLine:
    """One line of a hunk body: ``tag`` is ``" "``, ``"-"`` or ``"+"``."""

    tag: str
    text: str
    no_newline: bool = False


@dataclass(frozen=True)
class DiffHunk:
    """A parsed ``@@ -a,b +c,d @@`` hunk with its body lines.

    ``old_count``/``new_count`` are mandatory (R09): the parser cross-checks
    them against the body before anything is applied, and the applier relies
    on ``old_count == 0`` for the POSIX insertion-point rule.
    """

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[HunkLine, ...] = ()


# ----------------------------------------------------------------------
# Entry representations — exactly ONE per file (R08)
# ----------------------------------------------------------------------


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


def _sha256_digest(text: str) -> str:
    """The tagged sha256 digest of *text*'s utf-8 bytes."""
    return _SHA256_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _blob_digests(text: str) -> tuple[str, ...]:
    """The tagged git blob OIDs *text* would hash to (sha1 and sha256 repos)."""
    data = text.encode("utf-8")
    header = b"blob %d\x00" % len(data)
    return (
        _BLOB_PREFIX + hashlib.sha1(header + data).hexdigest(),
        _BLOB_PREFIX + hashlib.sha256(header + data).hexdigest(),
    )


def _digest_matches(content: str, claim: str) -> bool:
    """Whether *content*'s digest equals the tagged *claim*."""
    if claim.startswith(_SHA256_PREFIX):
        return claim == _sha256_digest(content)
    if claim.startswith(_BLOB_PREFIX):
        return claim in _blob_digests(content)
    return False


@dataclass(frozen=True)
class CreateFile:
    """A file the candidate creates: the new-file diff IS the whole file."""

    path: str
    new_content: str
    mode: str = "100644"
    #: Digest of the expected result — verified before anything publishes.
    intended_digest: str = ""

    def __post_init__(self) -> None:
        _validated_digest(self.intended_digest)
        if not self.intended_digest:
            object.__setattr__(self, "intended_digest", _sha256_digest(self.new_content))

    @property
    def operation(self) -> CandidateOperation:
        return "create"

    @property
    def hunks(self) -> tuple[DiffHunk, ...]:
        return ()


@dataclass(frozen=True)
class DeleteFile:
    """A file the candidate deletes; the write path removes it wholesale."""

    path: str
    mode: str = "100644"
    #: Digest of the blob being deleted, when the artifact carries it —
    #: verified against the authoritative base before the delete publishes.
    base_blob_digest: str = ""

    def __post_init__(self) -> None:
        _validated_digest(self.base_blob_digest)

    @property
    def operation(self) -> CandidateOperation:
        return "delete"

    @property
    def new_content(self) -> str | None:
        return None

    @property
    def hunks(self) -> tuple[DiffHunk, ...]:
        return ()


@dataclass(frozen=True)
class FullReplacement:
    """A file the candidate rewrites wholesale (the R08 representation).

    ``new_content`` REPLACES the entire base content. An UPDATE is never
    represented as "modify with content and empty hunks" — that shape
    silently materialized to the ORIGINAL file.
    """

    path: str
    new_content: str
    mode: str = "100644"
    #: Digest of the base content the replacement was built against.
    base_blob_digest: str = ""
    #: Digest of the expected result — verified before anything publishes.
    intended_digest: str = ""

    def __post_init__(self) -> None:
        _validated_digest(self.base_blob_digest)
        _validated_digest(self.intended_digest)
        if not self.intended_digest:
            object.__setattr__(self, "intended_digest", _sha256_digest(self.new_content))

    @property
    def operation(self) -> CandidateOperation:
        return "modify"

    @property
    def hunks(self) -> tuple[DiffHunk, ...]:
        return ()


@dataclass(frozen=True)
class UnifiedPatch:
    """A file the candidate modifies by applying unified hunks to the base.

    Carrying hunks excludes carrying full content (R08): the result exists
    only after a verified application to the authoritative base.
    """

    path: str
    hunks: tuple[DiffHunk, ...]
    mode: str = "100644"
    #: Digest of the base content the hunks were diffed against.
    base_blob_digest: str = ""
    #: Digest of the expected post-image (the diff's own new blob OID when
    #: the capture is ``--full-index``) — an applier bug becomes a detected
    #: rejection instead of silent corruption.
    intended_digest: str = ""

    def __post_init__(self) -> None:
        if not self.hunks:
            raise CandidateError(
                "ambiguous_representation",
                f"{self.path}: modify entry without hunks or full content",
            )
        _validated_digest(self.base_blob_digest)
        _validated_digest(self.intended_digest)

    @property
    def operation(self) -> CandidateOperation:
        return "modify"

    @property
    def new_content(self) -> str | None:
        return None


#: One bundle entry — exactly one representation per file (R08).
ChangeEntry = CreateFile | DeleteFile | FullReplacement | UnifiedPatch


@dataclass(frozen=True)
class ChangeManifestEntry:
    """The completed, flat form :meth:`CandidateBundle.materialize` returns.

    Publishers map these onto the write-path ``ChangeSet``: ``new_content``
    is the FULL new text for ``create``/``modify`` entries (``None`` for
    ``delete``).
    """

    path: str
    operation: CandidateOperation
    new_content: str | None = None
    mode: str = "100644"
    hunks: tuple[DiffHunk, ...] = ()


@dataclass(frozen=True)
class HarnessUsage:
    """The F22-lite usage receipt parsed from ``candidate.meta.json``.

    Normalization honesty (R23, docs/research/2026-09-17-actions-artifacts-usage.md
    § Usage normalization table): token fields are ``None`` when unknown —
    unknown stays unknown, never zero — and cache counters are never folded
    into the input count:

    - OpenAI-compatible shapes (grok-build, opencode, copilot) count the
      cache inside the inclusive input; ``cached_input_tokens`` is a
      breakdown, never added on top;
    - Anthropic(-compatible) shapes (the claude-code driver, whether it
      talks to Anthropic or to ``api.z.ai/api/anthropic``) carry DISJOINT
      counters — ``input_tokens`` already excludes the cache — so their
      spend total is ``input + cache_read + cache_write + output``
      (:attr:`total_known_tokens`).
    """

    driver: str = ""
    model: str = ""
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    #: Anthropic ``cache_creation_input_tokens`` — a counter the
    #: OpenAI-compatible shapes never expose. Absent → unknown, never 0.
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    #: Informational only (``reasoning_tokens`` / ``thinking_tokens``): a
    #: breakdown inside the inclusive output on every shape forge sees —
    #: never added on top of anything.
    reasoning_tokens: int | None = None
    completeness: UsageCompleteness = "unknown"
    source: str = ""
    #: The lane's attempt identity (v2 meta ``attempt_id``, GitHub's own
    #: ``<run_id>:<run_attempt>``) — the second component of the receipt
    #: identity: a repair re-dispatch is a legitimately distinct receipt.
    attempt_id: str = ""
    #: The receipt's identity, sha256 over (run id, attempt id, normalized
    #: usage JSON) — :func:`usage_receipt_id`. The lane may embed it as
    #: ``usage.receipt_id``; the control plane recomputes the SAME value
    #: when absent (deterministic), so a re-downloaded artifact replays
    #: byte-identical identity.
    receipt_id: str = ""
    #: The verbatim usage block as received (never normalized in place) —
    #: preserved on the ledger so spend stays reconstructable later.
    raw: dict[str, Any] | None = None

    @classmethod
    def from_meta(
        cls, data: object, *, driver: str = "", model: str = "", attempt_id: str = ""
    ) -> HarnessUsage:
        """Build a receipt from the meta JSON's ``usage`` object (defensive).

        Anything not a non-negative int stays ``None``; ``completeness``
        degrades to ``unknown`` unless at least one token count survived.
        ``attempt_id`` comes from the v2 meta's top-level attempt identity
        (the caller passes it) with an in-block override.
        """
        if not isinstance(data, dict):
            return cls(driver=driver, model=model, attempt_id=attempt_id)

        def _token(key: str) -> int | None:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        input_tokens = _token("input_tokens")
        cached = _token("cached_input_tokens")
        cache_write = _token("cache_write_tokens")
        output_tokens = _token("output_tokens")
        reasoning = _token("reasoning_tokens")
        completeness = data.get("completeness")
        if completeness not in ("exact", "aggregate"):
            # Receipts parsed from the event stream are sums of per-turn
            # counters — the honest default is "aggregate", never fabricated
            # precision.
            completeness = "aggregate"
        if all(
            value is None for value in (input_tokens, cached, cache_write, output_tokens, reasoning)
        ):
            completeness = "unknown"
        return cls(
            driver=str(data.get("driver") or driver or ""),
            model=str(data.get("model") or model or ""),
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning,
            completeness=completeness,  # type: ignore[arg-type]
            source=str(data.get("source") or ""),
            attempt_id=str(data.get("attempt_id") or attempt_id or ""),
            receipt_id=str(data.get("receipt_id") or ""),
            raw=data,
        )

    @property
    def anthropic_shaped(self) -> bool:
        """Whether the counters are DISJOINT (Anthropic-compatible semantics).

        Provider shape is decided by the endpoint, not the hostname, and the
        meta's canonical field names cannot distinguish "input excludes the
        cache" from "input includes it" — so shape is detected by the two
        tells the research table names: the ``cache_write`` counter only an
        Anthropic-compatible endpoint exposes, and the claude-code driver
        that always talks to one.
        """
        return self.cache_write_tokens is not None or self.driver == "claude-code"

    @property
    def total_known_tokens(self) -> int | None:
        """The receipt's KNOWN token total, per the normalization table.

        Anthropic-shaped (disjoint counters): ``input + cache_read +
        cache_write + output`` — the caching guide's total-input formula.
        OpenAI-shaped (inclusive counters): ``input + output`` — the cache
        rides inside the input and is never added on top. Unknown parts
        contribute nothing (never zero-filled); nothing known → ``None``.
        """
        parts: tuple[int | None, ...]
        if self.anthropic_shaped:
            parts = (
                self.input_tokens,
                self.cached_input_tokens,
                self.cache_write_tokens,
                self.output_tokens,
            )
        else:
            parts = (self.input_tokens, self.output_tokens)
        known = [part for part in parts if isinstance(part, int)]
        if not known:
            return None
        return sum(known)


def usage_document(usage: HarnessUsage) -> dict[str, Any]:
    """The receipt's canonical normalized counters — the identity material.

    Sorted-key JSON of this document is what :func:`usage_receipt_id`
    hashes; ``raw`` is deliberately excluded (the verbatim provider block is
    preserved on the ledger but is not part of the identity: two lanes that
    aggregate the same counters must produce the same receipt id).
    """
    return {
        "driver": usage.driver,
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "completeness": usage.completeness,
        "source": usage.source,
    }


def usage_receipt_id(run_id: str, attempt_id: str, usage: HarnessUsage) -> str:
    """The receipt's identity: sha256 over (run id, attempt id, usage JSON).

    R23: computed over the NORMALIZED usage document, so it is deterministic
    across re-downloads of the same artifact (the exact double-count
    scenario) while a repair re-dispatch — a new attempt id — is a
    legitimately distinct receipt. The lane may embed the value it computed
    at emit time as ``usage.receipt_id``; the control plane recomputes this
    SAME function when the meta does not carry it.
    """
    document = json.dumps(usage_document(usage), sort_keys=True, separators=(",", ":"))
    material = f"{run_id}|{attempt_id}|{document}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CandidateBundle:
    """A parsed harness candidate: diff manifest + trusted context.

    ``attempt_base_oid`` and ``driver_exit`` are supplied by the TRUSTED
    caller (what forge pinned and what it classified), never read back from
    the artifact alone.
    """

    attempt_base_oid: str
    driver_exit: str
    entries: tuple[ChangeEntry, ...] = ()
    usage: HarnessUsage | None = None

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def paths(self) -> list[str]:
        return [entry.path for entry in self.entries]

    def materialize(self, base_contents: dict[str, str]) -> list[ChangeManifestEntry]:
        """Complete every entry against the authoritative base texts.

        *base_contents* maps path -> FULL file text at the attempt base (the
        trusted full-content reader — no truncation). For each entry the
        ``base_blob_digest`` claim is verified BEFORE anything is applied
        (mismatch → ``stale_base``) and the ``intended_digest`` claim AFTER
        (mismatch → ``result_digest_mismatch``: an internal error, never
        published). Raises :class:`CandidateError` (``patch_does_not_apply``)
        when a hunk does not match the base exactly, or ``file_too_large``
        when a completed content exceeds the materialization cap.
        """
        completed: list[ChangeManifestEntry] = []
        for entry in self.entries:
            completed.append(self._materialize_entry(entry, base_contents))
        return completed

    @staticmethod
    def _materialize_entry(
        entry: ChangeEntry, base_contents: dict[str, str]
    ) -> ChangeManifestEntry:
        if isinstance(entry, DeleteFile):
            base = base_contents.get(entry.path)
            if base is not None and entry.base_blob_digest:
                _verify_base_digest(entry.path, base, entry.base_blob_digest)
            return ChangeManifestEntry(
                path=entry.path, operation="delete", new_content=None, mode=entry.mode
            )
        if isinstance(entry, CreateFile):
            _enforce_cap(entry.path, entry.new_content)
            _verify_result_digest(entry.path, entry.new_content, entry.intended_digest)
            return ChangeManifestEntry(
                path=entry.path,
                operation="create",
                new_content=entry.new_content,
                mode=entry.mode,
            )
        # FullReplacement / UnifiedPatch both need the authoritative base.
        base = base_contents.get(entry.path)
        if base is None:
            raise CandidateError(
                "patch_does_not_apply",
                f"{entry.path}: no authoritative base content at the attempt base",
            )
        if entry.base_blob_digest:
            _verify_base_digest(entry.path, base, entry.base_blob_digest)
        if isinstance(entry, FullReplacement):
            content = entry.new_content
            hunks: tuple[DiffHunk, ...] = ()
        else:
            content = apply_unified_hunks(base, entry.hunks, path=entry.path)
            hunks = entry.hunks
        _verify_result_digest(entry.path, content, entry.intended_digest)
        _enforce_cap(entry.path, content)
        return ChangeManifestEntry(
            path=entry.path,
            operation="modify",
            new_content=content,
            mode=entry.mode,
            hunks=hunks,
        )


def bundle_from_changeset(cs: object, *, attempt_base_oid: str) -> CandidateBundle:
    """Wrap an already-materialized ChangeSet (builtin path) as a bundle.

    The builtin backend produces ChangeSets with FULL contents — every update
    becomes a :class:`FullReplacement` (R08: never "modify with content and
    empty hunks"); routing it through the same publisher boundary (ADR-0016
    §2) gives every backend identical validation. ``cs`` is typed loosely to
    avoid an import cycle with :mod:`forge.repository`.
    """
    # ChangeSet operation (create/update/delete) → manifest operation; typed
    # so the manifest Literal is checked at this boundary, not erased to str.
    operation_map: dict[str, CandidateOperation] = {
        "create": "create",
        "update": "modify",
        "delete": "delete",
    }
    entries: list[ChangeEntry] = []
    for change in cs.changes:  # type: ignore[attr-defined]
        path: str = change.path  # type: ignore[attr-defined]
        operation = operation_map[change.operation.value]  # type: ignore[attr-defined]
        content: str | None = change.content  # type: ignore[attr-defined]
        if operation == "delete":
            entries.append(DeleteFile(path=path))
        elif content is None:
            raise CandidateError(
                "ambiguous_representation",
                f"{path}: {operation} entry without full content",
            )
        elif operation == "create":
            entries.append(CreateFile(path=path, new_content=content))
        else:
            entries.append(FullReplacement(path=path, new_content=content))
    return CandidateBundle(
        attempt_base_oid=attempt_base_oid, driver_exit="completed", entries=tuple(entries)
    )


@dataclass(frozen=True)
class AttemptContext:
    """The base snapshot set of ONE proposal attempt (R06).

    One definition of the bases every leg of an attempt shares: the
    implementer reads and materializes at ``attempt_base``, update/delete
    existence is validated against the same snapshot, and the writer pins the
    factory branch to it (``start_ref == expected_head``). Cycle 1 reads the
    approved source base; a repair extends the last verified candidate so it
    can see cycle 1's files. ``source_base`` stays frozen at the approved
    snapshot for the final cumulative review/acceptance diff only — it is
    never consulted for repair existence checks.
    """

    cycle: int
    #: read/materialize/validate/publish base for the WHOLE attempt.
    attempt_base: str
    #: frozen approved base — final cumulative review/acceptance only.
    source_base: str
    #: the verified candidate this attempt extends (None on cycle 1).
    previous_candidate: str | None

    @classmethod
    def of(cls, run: object) -> AttemptContext:
        """The context of *run*'s current attempt, from its durable state."""
        cycle = getattr(run, "commit_cycle", None) or 1
        source_base = str(getattr(run, "base_sha", None) or "")
        candidates = list(getattr(run, "candidate_shas", None) or [])
        previous = candidates[-1] if cycle > 1 and candidates else None
        return cls(
            cycle=cycle,
            attempt_base=previous or source_base,
            source_base=source_base,
            previous_candidate=previous,
        )

    def document(self) -> dict:
        """The evidence document: this attempt's number and bases."""
        return {
            "cycle": self.cycle,
            "attempt_base": self.attempt_base,
            "source_base": self.source_base,
            "previous_candidate": self.previous_candidate,
        }


def attempt_base_for(run: object) -> str:
    """The frozen attempt base for *run* (ADR-0016 §4): cycle 1 → approved
    source base; repair → the last verified candidate OID. The
    :class:`AttemptContext` shared by the harness lane (``FORGE_ATTEMPT_BASE``,
    checked against every candidate artifact) and the builtin proposal path.
    """
    return AttemptContext.of(run).attempt_base


# ----------------------------------------------------------------------
# Unified-diff parsing
# ----------------------------------------------------------------------

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: A ``--full-index`` capture carries full pre/post blob OIDs — the artifact's
#: own claim about base and result, used for stale-base/result verification.
_FULL_OID_LENGTHS = (40, 64)

_NO_NEWLINE_PREFIX = "\\ "

_INDEX_LINE_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)(?: \d+)?$")


def parse_unified_diff(
    diff_text: str,
    attempt_base_oid: str,
    driver_exit: str = "completed",
    usage: HarnessUsage | None = None,
) -> CandidateBundle:
    """Parse ``git diff --binary --full-index <base>`` output into a bundle.

    The diff is split on ``"\\n"`` ONLY — never :meth:`str.splitlines`, which
    also splits on ``\\r``, U+2028 and friends and destroys both CRLF diffs
    and hunk lines containing those characters (R09).

    Raises :class:`CandidateError` on binary deltas, renames, mode-only
    changes, oversized created files, malformed hunks and hunk counts that
    disagree with their body. An empty diff yields an empty bundle (the
    caller classifies "no changes").
    """
    parser = _DiffParser(diff_text)
    entries = parser.parse()
    return CandidateBundle(
        attempt_base_oid=attempt_base_oid,
        driver_exit=driver_exit,
        entries=entries,
        usage=usage,
    )


def _blob_claim(oid: str) -> str:
    """A tagged digest claim for a FULL blob OID; empty when unclaimed."""
    return f"{_BLOB_PREFIX}{oid}" if oid else ""


class _FileParse:
    """Mutable per-file accumulator used only while parsing."""

    def __init__(self) -> None:
        self.path: str = ""
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.mode: str = "100644"
        self.is_create: bool = False
        self.is_delete: bool = False
        self.is_binary: bool = False
        self.is_rename: bool = False
        self.is_mode_change: bool = False
        self.old_oid: str = ""
        self.new_oid: str = ""
        self.hunks: list[DiffHunk] = []


class _DiffParser:
    def __init__(self, diff_text: str) -> None:
        # Split on "\n" ONLY; a trailing newline must not produce a phantom
        # empty context line at EOF. CR stays content (CRLF diffs), and
        # U+2028/\\x85 inside a line never splits it (R09).
        self._lines = diff_text.split("\n")
        if self._lines and self._lines[-1] == "":
            self._lines.pop()

    def parse(self) -> tuple[ChangeEntry, ...]:
        current: _FileParse | None = None
        pending: list[HunkLine] = []
        hunk_header: tuple[int, int, int, int] | None = None
        hunk_line_no = 0
        entries: list[ChangeEntry] = []

        def flush_hunk() -> None:
            nonlocal pending, hunk_header
            if hunk_header is not None and current is not None:
                old_start, old_count, new_start, new_count = hunk_header
                consumed_old = sum(1 for item in pending if item.tag in (" ", "-"))
                emitted_new = sum(1 for item in pending if item.tag in (" ", "+"))
                if consumed_old != old_count or emitted_new != new_count:
                    raise CandidateError(
                        "corrupt_patch",
                        f"line {hunk_line_no}: hunk counts disagree with the body "
                        f"(@@ -{old_start},{old_count} +{new_start},{new_count} @@ but the "
                        f"body consumes {consumed_old} old / emits {emitted_new} new lines)",
                    )
                current.hunks.append(
                    DiffHunk(
                        old_start=old_start,
                        old_count=old_count,
                        new_start=new_start,
                        new_count=new_count,
                        lines=tuple(pending),
                    )
                )
            pending = []
            hunk_header = None

        def flush_file() -> None:
            nonlocal current
            flush_hunk()
            if current is not None:
                entries.append(self._entry(current))
            current = None

        for line_no, line in enumerate(self._lines, start=1):
            if line.startswith("diff --git "):
                flush_file()
                current = _FileParse()
                current.path = self._path_from_git_header(line)
                continue
            if line.startswith("@@ "):
                match = _HUNK_HEADER_RE.match(line)
                if match is None:
                    raise CandidateError(
                        "malformed_diff", f"line {line_no}: unparseable hunk header: {line!r}"
                    )
                flush_hunk()
                # POSIX: a missing ",count" means exactly one line.
                hunk_header = (
                    int(match.group(1)),
                    int(match.group(2) or 1),
                    int(match.group(3)),
                    int(match.group(4) or 1),
                )
                hunk_line_no = line_no
                continue
            if hunk_header is not None and (line.startswith(("+", "-", " ", "\\")) or line == ""):
                if line.startswith(_NO_NEWLINE_PREFIX):
                    if not pending:
                        raise CandidateError(
                            "corrupt_patch",
                            f"line {line_no}: no-newline marker without a preceding body line",
                        )
                    pending[-1] = replace(pending[-1], no_newline=True)
                    continue
                text = line[1:] if line else ""
                pending.append(HunkLine(tag=line[:1] or " ", text=text))
                continue
            if hunk_header is not None:
                flush_hunk()  # a file-level header line ends the hunk body
            if current is None:
                continue  # noise before the first file header
            self._file_metadata(current, line)
        flush_file()
        return tuple(entries)

    @staticmethod
    def _path_from_git_header(line: str) -> str:
        # "diff --git a/<p> b/<p>" — best-effort b-side; the +++/--- headers
        # refine it when present.
        _, _, rest = line.partition("diff --git ")
        parts = rest.split(" b/")
        if len(parts) > 1:
            return parts[-1].strip()
        return rest.strip()

    @staticmethod
    def _file_metadata(file: _FileParse, line: str) -> None:
        if line.startswith("new file mode "):
            file.is_create = True
            file.mode = line.rsplit(" ", 1)[-1].strip() or file.mode
        elif line.startswith("deleted file mode "):
            file.is_delete = True
        elif line.startswith(("old mode ", "new mode ")):
            # A pure mode flip has no content delta and the write path has no
            # mode action — rejected, never silently dropped (R09).
            file.is_mode_change = True
        elif line.startswith(("rename ", "copy ")):
            file.is_rename = True
        elif line.startswith("GIT binary patch"):
            file.is_binary = True
        elif line.startswith("Binary files ") and "differ" in line:
            file.is_binary = True
        elif line.startswith("index "):
            match = _INDEX_LINE_RE.match(line)
            if match is not None:
                file.old_oid = _full_oid(match.group(1))
                file.new_oid = _full_oid(match.group(2))
        elif line.startswith("--- "):
            file.old_path = _strip_diff_prefix(line[4:].strip())
        elif line.startswith("+++ "):
            file.new_path = _strip_diff_prefix(line[4:].strip())
        # "similarity ", "dissimilarity ", anything unknown: ignore.

    def _entry(self, file: _FileParse) -> ChangeEntry:
        path = file.path
        if file.is_delete and file.old_path:
            path = file.old_path
        elif file.new_path:
            path = file.new_path
        if not path or path == "/dev/null":
            raise CandidateError("malformed_diff", "file header without a usable path")

        if file.is_binary:
            raise CandidateError(
                "binary_not_supported",
                f"{path}: binary deltas are not supported in candidate bundles",
            )
        if file.is_rename:
            # TODO(v0.4): represent renames as delete+create pairs.
            raise CandidateError(
                "rename_not_supported",
                f"{path}: renames/copies are not supported in candidate bundles",
            )

        if file.is_create:
            content = apply_unified_hunks("", tuple(file.hunks), path=path)
            _enforce_cap(path, content)
            return CreateFile(
                path=path,
                new_content=content,
                mode=file.mode,
                intended_digest=_blob_claim(file.new_oid),
            )
        if file.is_delete:
            return DeleteFile(path=path, mode=file.mode, base_blob_digest=_blob_claim(file.old_oid))
        if file.is_mode_change and not file.hunks:
            raise CandidateError(
                "mode_change_not_supported",
                f"{path}: mode-only changes are not supported in candidate bundles",
            )
        if not file.hunks:
            raise CandidateError(
                "ambiguous_representation",
                f"{path}: modify entry without hunks or full content",
            )
        return UnifiedPatch(
            path=path,
            hunks=tuple(file.hunks),
            mode=file.mode,
            base_blob_digest=_blob_claim(file.old_oid),
            intended_digest=_blob_claim(file.new_oid),
        )


def _full_oid(hex_oid: str) -> str:
    """*hex_oid* when it is a full blob OID (never abbreviated/all-zero)."""
    if len(hex_oid) in _FULL_OID_LENGTHS and set(hex_oid) != {"0"}:
        return hex_oid
    return ""


def _strip_diff_prefix(path: str) -> str:
    if path == "/dev/null":
        return "/dev/null"
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


# ----------------------------------------------------------------------
# Strict hunk application (no fuzz, ever — ADR-0001 semantics for patches)
# ----------------------------------------------------------------------


def apply_unified_hunks(
    base_text: str, hunks: tuple[DiffHunk, ...], *, path: str = "<data>"
) -> str:
    """Apply *hunks* to *base_text* with strict line matching.

    Every context/removal line must equal the base line at the hunk's exact
    position; hunks must not overlap and must appear in order. An empty old
    range (``old_count == 0``) positions AFTER base line ``old_start`` —
    POSIX: "if a range is empty, its beginning line number shall be the
    number of the line just before the range" (R09: ``@@ -3,0 +4,1 @@``
    inserts between lines 3 and 4). Any deviation raises
    :class:`CandidateError` (``patch_does_not_apply``, ``corrupt_patch``) —
    no fuzzy matching, ever.
    """
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
        # The diff consumed the base to EOF: its trailing-newline claim about
        # the old side must match the base exactly.
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
    """Split on ``"\\n"`` ONLY into (lines without terminators, ends_with_newline).

    ``splitlines()`` would also split on ``\\r``, U+2028, U+0085 and friends,
    destroying CRLF content and mid-line Unicode separators — CR is content,
    never a line break and never resynthesized.
    """
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


def _verify_base_digest(path: str, base: str, claim: str) -> None:
    """Reject a stale base BEFORE anything is applied (ADR-0016 honesty)."""
    if not _digest_matches(base, claim):
        raise CandidateError(
            "stale_base",
            f"{path}: base content does not match the digest the candidate was "
            f"diffed against ({claim[:20]}…) — the attempt base moved",
        )


def _verify_result_digest(path: str, content: str, claim: str) -> None:
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
