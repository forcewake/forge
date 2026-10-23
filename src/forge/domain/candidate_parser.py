"""Strict unified-diff parser with explicit unsupported representations."""

from __future__ import annotations

import re
from dataclasses import replace

from .candidate import (
    CandidateBundle,
    ChangeEntry,
    CreateFile,
    DeleteFile,
    UnifiedPatch,
    BinaryPatch,
    ModeChange,
)
from .git_binary import parse_binary_blocks
from .git_paths import decode_path, header_path
from .candidate_usage import HarnessUsage
from .patch import (
    CandidateError,
    DiffHunk,
    HunkLine,
    _BLOB_PREFIX,
    _enforce_cap,
    apply_unified_hunks,
)

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


_FULL_OID_LENGTHS = (40, 64)


_NO_NEWLINE_PREFIX = "\\ "


_INDEX_LINE_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)(?: (\d+))?$")


def parse_unified_diff(
    diff_text: str,
    attempt_base_oid: str,
    driver_exit: str = "completed",
    usage: HarnessUsage | None = None,
) -> CandidateBundle:
    """Parse ``git diff --binary --full-index <base>`` output into a bundle."""
    parser = _DiffParser(diff_text)
    entries = parser.parse()
    return CandidateBundle(
        attempt_base_oid=attempt_base_oid,
        driver_exit=driver_exit,
        entries=entries,
        usage=usage,
    )


def parse_diff_bytes(diff: bytes, attempt_base_oid: str) -> CandidateBundle:
    return CandidateBundle(
        attempt_base_oid, "completed", _DiffParser(diff.decode("latin1"), wire_bytes=True).parse()
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
        self.old_mode: str = ""
        self.binary_lines: list[str] = []
        self.old_oid: str = ""
        self.new_oid: str = ""
        self.hunks: list[DiffHunk] = []


class _DiffParser:
    def __init__(self, diff_text: str, *, wire_bytes: bool = False) -> None:
        self.wire_bytes = wire_bytes
        # Split on "\n" ONLY; a trailing newline must not produce a phantom empty context
        # line at EOF. CR stays content (CRLF diffs), and U+2028/\\x85 inside a line never
        # splits it.
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

    def _path_from_git_header(self, line: str) -> str:
        return header_path(line, wire_bytes=self.wire_bytes)

    def _file_metadata(self, file: _FileParse, line: str) -> None:
        if file.is_binary:
            file.binary_lines.append(line)
            return
        if line.startswith("new file mode "):
            file.is_create = True
            file.mode = line.rsplit(" ", 1)[-1].strip() or file.mode
        elif line.startswith("deleted file mode "):
            file.is_delete = True
            file.mode = line.rsplit(" ", 1)[-1]
        elif line.startswith(("old mode ", "new mode ")):
            # A pure mode change later requires the authoritative old mode and bytes.
            file.is_mode_change = True
            if line.startswith("old mode "):
                file.old_mode = line.rsplit(" ", 1)[-1]
            else:
                file.mode = line.rsplit(" ", 1)[-1]
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
                if match.group(3) and not file.is_mode_change:
                    file.mode = match.group(3)
        elif line.startswith("--- "):
            file.old_path = decode_path(line[4:].removesuffix("\r"), wire_bytes=self.wire_bytes)
        elif line.startswith("+++ "):
            file.new_path = decode_path(line[4:].removesuffix("\r"), wire_bytes=self.wire_bytes)
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
            if file.is_rename:
                raise CandidateError(
                    "rename_not_supported", "binary rename requires explicit file actions"
                )
            forward, _ = parse_binary_blocks(tuple(file.binary_lines))
            if file.is_delete:
                return DeleteFile(
                    path=path, mode=file.mode, base_blob_digest=_blob_claim(file.old_oid)
                )
            return BinaryPatch(
                path,
                forward,
                "create" if file.is_create else "modify",
                file.mode,
                _blob_claim(file.old_oid),
                _blob_claim(file.new_oid),
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
            return ModeChange(path, file.old_mode, file.mode)
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
