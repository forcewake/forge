from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict


class DiffLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_type: Literal["add", "remove", "context"]
    content: str
    old_lineno: int | None = None
    new_lineno: int | None = None


class DiffHunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[DiffLine] = []


class FileDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    old_path: str
    new_path: str
    is_new: bool = False
    is_deleted: bool = False
    is_renamed: bool = False
    is_binary: bool = False
    hunks: list[DiffHunk] = []


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_HEADER_RE = re.compile(r"^diff --git a/(.*) b/(.*)$", re.MULTILINE)


def parse_diff(raw: str) -> list[FileDiff]:
    """Parse raw unified diff text into structured file diffs."""
    if not raw or not raw.strip():
        return []

    # Split into per-file blocks by the "diff --git" header.
    # We re-add the prefix so each block starts with "diff --git".
    parts = re.split(r"(?=^diff --git )", raw, flags=re.MULTILINE)
    results: list[FileDiff] = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        header_match = _FILE_HEADER_RE.match(part)
        if not header_match:
            continue

        git_old_path = header_match.group(1)
        git_new_path = header_match.group(2)
        lines = part.split("\n")

        old_path = git_old_path
        new_path = git_new_path
        is_new = False
        is_deleted = False
        is_renamed = False
        is_binary = False

        # Scan for metadata lines before the first hunk
        hunk_start_idx = len(lines)  # default: no hunks
        for i, line in enumerate(lines[1:], start=1):
            if line.startswith("--- "):
                path = line[4:]
                if path == "/dev/null":
                    is_new = True
                elif path.startswith("a/"):
                    old_path = path[2:]
            elif line.startswith("+++ "):
                path = line[4:]
                if path == "/dev/null":
                    is_deleted = True
                elif path.startswith("b/"):
                    new_path = path[2:]
            elif line.startswith("rename from "):
                is_renamed = True
                old_path = line[len("rename from ") :]
            elif line.startswith("rename to "):
                is_renamed = True
                new_path = line[len("rename to ") :]
            elif line.startswith("Binary files "):
                is_binary = True
                break
            elif _HUNK_RE.match(line):
                hunk_start_idx = i
                break

        if is_binary:
            results.append(
                FileDiff(
                    old_path=old_path,
                    new_path=new_path,
                    is_new=is_new,
                    is_deleted=is_deleted,
                    is_renamed=is_renamed,
                    is_binary=True,
                )
            )
            continue

        # Parse hunks
        hunks = _parse_hunks(lines[hunk_start_idx:])

        results.append(
            FileDiff(
                old_path=old_path,
                new_path=new_path,
                is_new=is_new,
                is_deleted=is_deleted,
                is_renamed=is_renamed,
                is_binary=False,
                hunks=hunks,
            )
        )

    return results


def _parse_hunks(lines: list[str]) -> list[DiffHunk]:
    """Parse hunk sections from a list of lines starting at the first @@ header."""
    hunks: list[DiffHunk] = []
    current_header: str | None = None
    old_start = old_count = new_start = new_count = 0
    old_lineno = new_lineno = 0
    current_lines: list[DiffLine] = []

    for line in lines:
        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            # Flush previous hunk
            if current_header is not None:
                hunks.append(
                    DiffHunk(
                        old_start=old_start,
                        old_count=old_count,
                        new_start=new_start,
                        new_count=new_count,
                        header=current_header,
                        lines=current_lines,
                    )
                )
            current_header = line
            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
            old_lineno = old_start
            new_lineno = new_start
            current_lines = []
            continue

        if current_header is None:
            continue

        if line.startswith("\\ No newline at end of file"):
            continue

        if line.startswith("+"):
            current_lines.append(
                DiffLine(
                    line_type="add",
                    content=line[1:],
                    old_lineno=None,
                    new_lineno=new_lineno,
                )
            )
            new_lineno += 1
        elif line.startswith("-"):
            current_lines.append(
                DiffLine(
                    line_type="remove",
                    content=line[1:],
                    old_lineno=old_lineno,
                    new_lineno=None,
                )
            )
            old_lineno += 1
        else:
            # Context line (starts with space or is empty)
            content = line[1:] if line.startswith(" ") else line
            current_lines.append(
                DiffLine(
                    line_type="context",
                    content=content,
                    old_lineno=old_lineno,
                    new_lineno=new_lineno,
                )
            )
            old_lineno += 1
            new_lineno += 1

    # Flush last hunk
    if current_header is not None:
        hunks.append(
            DiffHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                header=current_header,
                lines=current_lines,
            )
        )

    return hunks
