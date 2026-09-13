"""CandidateBundle: the proposal-only deliverable of a harness job (ADR-0016).

A harness lane never receives write credentials; its result is a **candidate
artifact** — a ``git diff --binary --full-index <attempt_base>`` capture plus a
small meta JSON (attempt base, driver id/model, exit classification, usage
receipt). This module is the trusted parser of that artifact:

- :func:`parse_unified_diff` turns the diff text into a
  :class:`CandidateBundle` of :class:`ChangeManifestEntry` records. Created
  files are reconstructed to FULL contents directly from the diff (a new-file
  diff is the whole file); modified files keep their raw unified hunks and are
  completed at publish time by applying them to the authoritative attempt-base
  blobs (:meth:`CandidateBundle.materialize`, strict line matching, no fuzz —
  on any mismatch the bundle is rejected with ``patch_does_not_apply``).
- Binary deltas (``GIT binary patch``) and renames are rejected as
  unsupported (v0.3 scope; TODO(v0.4): rename support via delete+create).
- Mode-only changes carry no content delta and are dropped from the manifest
  (the Commits API write path has no mode action).

Rejected parses raise :class:`CandidateError` with a machine-readable
``reason`` — the caller (backend/publisher) decides what the rejection means
for the run; nothing here trusts the harness's claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from forge.factory.implementer import FORGE_MATERIALIZE_MAX_FILE_CHARS

#: The three manifest operations (``update`` in ChangeSet terms is ``modify``).
CandidateOperation = Literal["create", "delete", "modify"]

#: Usage receipt completeness (F22 lite): sums of per-turn receipts are
#: "aggregate"; a missing receipt is "unknown" — never zero, never invented.
UsageCompleteness = Literal["exact", "aggregate", "unknown"]


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
    """A parsed ``@@ -a,b +c,d @@`` hunk with its body lines."""

    old_start: int
    new_start: int
    lines: tuple[HunkLine, ...] = ()


@dataclass(frozen=True)
class ChangeManifestEntry:
    """One file action of the candidate.

    ``new_content`` is the FULL new text for ``create`` entries (reconstructed
    at parse time), ``None`` for ``delete``. For ``modify`` entries it is
    ``None`` until :meth:`CandidateBundle.materialize` applies ``hunks`` to
    the authoritative attempt-base content.
    """

    path: str
    operation: CandidateOperation
    new_content: str | None = None
    mode: str = "100644"
    hunks: tuple[DiffHunk, ...] = ()


@dataclass(frozen=True)
class HarnessUsage:
    """The F22-lite usage receipt parsed from ``candidate.meta.json``.

    Token fields are ``None`` when unknown — unknown stays unknown, never
    zero, and cached tokens are never folded into the input count.
    """

    driver: str = ""
    model: str = ""
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    completeness: UsageCompleteness = "unknown"
    source: str = ""

    @classmethod
    def from_meta(
        cls, data: object, *, driver: str = "", model: str = ""
    ) -> HarnessUsage:
        """Build a receipt from the meta JSON's ``usage`` object (defensive).

        Anything not a non-negative int stays ``None``; ``completeness``
        degrades to ``unknown`` unless at least one token count survived.
        """
        if not isinstance(data, dict):
            return cls(driver=driver, model=model)

        def _token(key: str) -> int | None:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        input_tokens = _token("input_tokens")
        cached = _token("cached_input_tokens")
        output_tokens = _token("output_tokens")
        completeness = data.get("completeness")
        if completeness not in ("exact", "aggregate"):
            # Receipts parsed from the event stream are sums of per-turn
            # counters — the honest default is "aggregate", never fabricated
            # precision.
            completeness = "aggregate"
        if input_tokens is None and cached is None and output_tokens is None:
            completeness = "unknown"
        return cls(
            driver=str(data.get("driver") or driver or ""),
            model=str(data.get("model") or model or ""),
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
            completeness=completeness,  # type: ignore[arg-type]
            source=str(data.get("source") or ""),
        )


@dataclass(frozen=True)
class CandidateBundle:
    """A parsed harness candidate: diff manifest + trusted context.

    ``attempt_base_oid`` and ``driver_exit`` are supplied by the TRUSTED
    caller (what forge pinned and what it classified), never read back from
    the artifact alone.
    """

    attempt_base_oid: str
    driver_exit: str
    entries: tuple[ChangeManifestEntry, ...] = ()
    usage: HarnessUsage | None = None

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def paths(self) -> list[str]:
        return [entry.path for entry in self.entries]

    def materialize(self, base_contents: dict[str, str]) -> list[ChangeManifestEntry]:
        """Complete ``modify`` entries against the authoritative base texts.

        *base_contents* maps path -> FULL file text at the attempt base (the
        trusted full-content reader — no truncation). Raises
        :class:`CandidateError` (``patch_does_not_apply``) when a hunk does
        not match the base exactly, or ``file_too_large`` when a completed
        content exceeds the materialization cap.
        """
        completed: list[ChangeManifestEntry] = []
        for entry in self.entries:
            if entry.operation != "modify":
                completed.append(entry)
                continue
            base = base_contents.get(entry.path)
            if base is None:
                raise CandidateError(
                    "patch_does_not_apply",
                    f"{entry.path}: no authoritative base content at the attempt base",
                )
            content = apply_unified_hunks(base, entry.hunks, path=entry.path)
            _enforce_cap(entry.path, content)
            completed.append(replace(entry, new_content=content))
        return completed


def bundle_from_changeset(
    cs: object, *, attempt_base_oid: str
) -> CandidateBundle:
    """Wrap an already-materialized ChangeSet (builtin path) as a bundle.

    The builtin backend produces ChangeSets with FULL contents — no hunks are
    needed; routing it through the same publisher boundary (ADR-0016 §2)
    gives every backend identical validation. ``cs`` is typed loosely to
    avoid an import cycle with :mod:`forge.repository`.
    """
    operation_map = {"create": "create", "update": "modify", "delete": "delete"}
    entries = tuple(
        ChangeManifestEntry(
            path=change.path,  # type: ignore[attr-defined]
            operation=operation_map[change.operation.value],  # type: ignore[attr-defined]
            new_content=change.content,  # type: ignore[attr-defined]
        )
        for change in cs.changes  # type: ignore[attr-defined]
    )
    return CandidateBundle(
        attempt_base_oid=attempt_base_oid, driver_exit="completed", entries=entries
    )


def attempt_base_for(run: object) -> str:
    """The frozen attempt base for *run* (ADR-0016 §4): cycle 1 → approved
    source base; repair → the last verified candidate OID. Mirrors
    ``RunService._advance_proposal`` — passed to the harness lane as
    ``FORGE_ATTEMPT_BASE`` and checked against every candidate artifact.
    """
    cycle = getattr(run, "commit_cycle", None) or 1
    candidates = list(getattr(run, "candidate_shas", None) or [])
    if cycle > 1 and candidates:
        return candidates[-1]
    return str(getattr(run, "base_sha", None) or "")


# ----------------------------------------------------------------------
# Unified-diff parsing
# ----------------------------------------------------------------------

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_NO_NEWLINE_MARKER = "\\ No newline at end of file"


def parse_unified_diff(
    diff_text: str,
    attempt_base_oid: str,
    driver_exit: str = "completed",
    usage: HarnessUsage | None = None,
) -> CandidateBundle:
    """Parse ``git diff --binary --full-index <base>`` output into a bundle.

    Raises :class:`CandidateError` on binary deltas, renames, oversized
    created files and malformed hunks. An empty diff yields an empty bundle
    (the caller classifies "no changes").
    """
    parser = _DiffParser(diff_text)
    entries = parser.parse()
    return CandidateBundle(
        attempt_base_oid=attempt_base_oid,
        driver_exit=driver_exit,
        entries=entries,
        usage=usage,
    )


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
        self.hunks: list[DiffHunk] = []


class _DiffParser:
    def __init__(self, diff_text: str) -> None:
        # splitlines() (not split("\n")): a trailing newline must not produce
        # a phantom empty context line at EOF.
        self._lines = diff_text.splitlines()

    def parse(self) -> tuple[ChangeManifestEntry, ...]:
        current: _FileParse | None = None
        pending: list[HunkLine] = []
        hunk_header: tuple[int, int] | None = None
        entries: list[ChangeManifestEntry] = []

        def flush_hunk() -> None:
            nonlocal pending, hunk_header
            if hunk_header is not None and current is not None:
                current.hunks.append(
                    DiffHunk(
                        old_start=hunk_header[0],
                        new_start=hunk_header[1],
                        lines=tuple(pending),
                    )
                )
            pending = []
            hunk_header = None

        def flush_file() -> None:
            nonlocal current
            flush_hunk()
            if current is not None:
                entry = self._entry(current)
                if entry is not None:
                    entries.append(entry)
            current = None

        for line in self._lines:
            if line.startswith("diff --git "):
                flush_file()
                current = _FileParse()
                current.path = self._path_from_git_header(line)
                continue
            if line.startswith("@@ "):
                match = _HUNK_HEADER_RE.match(line)
                if match is None:
                    raise CandidateError("malformed_diff", f"unparseable hunk header: {line!r}")
                flush_hunk()
                hunk_header = (int(match.group(1)), int(match.group(3)))
                continue
            if hunk_header is not None and (
                line.startswith(("+", "-", " ", "\\")) or line == ""
            ):
                if line == _NO_NEWLINE_MARKER:
                    if pending:
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
        elif line.startswith("old mode "):
            pass  # source mode of a modify — the new mode below wins
        elif line.startswith("new mode "):
            file.mode = line.rsplit(" ", 1)[-1].strip() or file.mode
        elif line.startswith(("rename ", "copy ")):
            file.is_rename = True
        elif line.startswith("GIT binary patch"):
            file.is_binary = True
        elif line.startswith("Binary files ") and "differ" in line:
            file.is_binary = True
        elif line.startswith("--- "):
            file.old_path = _strip_diff_prefix(line[4:].strip())
        elif line.startswith("+++ "):
            file.new_path = _strip_diff_prefix(line[4:].strip())
        # "index ", "similarity ", "dissimilarity ", anything unknown: ignore.

    def _entry(self, file: _FileParse) -> ChangeManifestEntry | None:
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
            return ChangeManifestEntry(
                path=path, operation="create", new_content=content, mode=file.mode
            )
        if file.is_delete:
            return ChangeManifestEntry(
                path=path, operation="delete", new_content=None, mode=file.mode
            )
        if not file.hunks:
            # Mode-only change: no content delta; the Commits API write path
            # has no mode action, so the entry is dropped (documented).
            return None
        return ChangeManifestEntry(
            path=path, operation="modify", new_content=None, mode=file.mode,
            hunks=tuple(file.hunks),
        )


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
    position; hunks must not overlap and must appear in order. Any deviation
    raises :class:`CandidateError` (``patch_does_not_apply``) — no fuzzy
    matching, ever.
    """
    where = f"{path}: "
    base_lines, base_ends_nl = _split_text(base_text)
    out: list[str] = []
    pos = 0
    last_emit_no_nl: bool | None = None
    last_old_no_nl: bool | None = None

    for hunk in hunks:
        target = max(hunk.old_start - 1, 0)
        if target < pos:
            raise CandidateError(
                "patch_does_not_apply", f"{where}hunk overlaps the previous hunk"
            )
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
                    f"{where}line {pos + 1} does not match the base "
                    f"(expected {item.text!r})",
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
    """Split into (lines without terminators, ends_with_newline)."""
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


def _enforce_cap(path: str, content: str) -> None:
    if len(content) > FORGE_MATERIALIZE_MAX_FILE_CHARS:
        raise CandidateError(
            "file_too_large",
            f"{path}: {len(content)} chars exceeds the materialization cap of "
            f"{FORGE_MATERIALIZE_MAX_FILE_CHARS}",
        )
