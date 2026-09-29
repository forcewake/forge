"""Reject review, backlog and issue archaeology in added source lines.

History belongs in git and ADRs, not in code comments. The check reads the
lines added under ``src/`` and ``packages/`` since ``--base`` and rejects
what ``_codebase.REVIEW_REF`` matches:

- review, backlog and acceptance item ids such as ``R42-16``, ``Q35-09``,
  ``NXT-10``, ``NEXT-05`` or ``AT-08``;
- issue references such as ``#377``;
- "review <sha>" references.

``ADR-0035`` style references are allowed.

Usage::

    uv run python scripts/prose_lint.py --base origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterator

import _codebase as cb

PATHS = ("src", "packages")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    path = ""
    lineno = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif match := HUNK.match(line):
            lineno = int(match.group(1))
        elif line.startswith("+") and path:
            yield path, lineno, line[1:]
            lineno += 1
        elif not line.startswith("-"):
            lineno += 1


def violations(diff: str) -> list[str]:
    return [
        f"{path}:{lineno}: {text.strip()}"
        for path, lineno, text in added_lines(diff)
        if cb.REVIEW_REF.search(text)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="git ref to diff against")
    args = parser.parse_args(argv)
    if not args.base.strip("0"):
        print("prose-lint: no base ref, nothing to check")
        return 0
    diff = subprocess.run(
        ["git", "diff", "--unified=0", "--no-color", f"{args.base}...HEAD", "--", *PATHS],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = violations(diff)
    for item in found:
        print(f"prose-lint: {item}")
    if found:
        print("prose-lint: FAILED — keep review and issue history in git and ADRs")
        return 1
    print("prose-lint: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
