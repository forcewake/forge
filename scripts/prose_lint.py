"""Reject review, backlog and issue archaeology in added source lines.

History belongs in git and ADRs, not in code comments. The check reads the
lines added under ``src/`` and ``packages/*/src/`` since ``--base`` and
rejects what ``_codebase.REVIEW_REF`` matches:

- review, backlog and acceptance item ids such as ``R42-16``, ``Q35-09``,
  ``NXT-10``, ``NEXT-05`` or ``AT-08``;
- issue references such as ``#377``;
- "review <sha>" references.

``ADR-0035`` style references are allowed. Moved code counts as added, so a
move is when its references go. A line that must keep a matching literal,
such as the colour ``#009966``, carries the marker ``prose-lint: allow``.

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

PATHS = ("src", ":(glob)packages/*/src/**")
DIFF_OPTIONS = (
    "--unified=0",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--find-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
ALLOW = "prose-lint: allow"


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    path, lineno, in_header = "", 0, False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            path, in_header = "", True
        elif in_header and line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif match := HUNK.match(line):
            lineno, in_header = int(match.group(1)), False
        elif not in_header and line.startswith("+") and path:
            yield path, lineno, line[1:]
            lineno += 1
        elif not in_header and line.startswith(" "):
            lineno += 1


def violations(diff: str) -> list[str]:
    return [
        f"{path}:{lineno}: {text.strip()}"
        for path, lineno, text in added_lines(diff)
        if cb.REVIEW_REF.search(text) and ALLOW not in text
    ]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="git ref to diff against")
    args = parser.parse_args(argv)
    if not args.base.strip("0"):
        print("prose-lint: no base ref, nothing to check")
        return 0
    if _git("rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}").returncode:
        print(f"prose-lint: base {args.base} is not in this checkout (force-push?); skipped")
        return 0
    done = _git("diff", *DIFF_OPTIONS, f"{args.base}...HEAD", "--", *PATHS)
    if done.returncode:
        print(f"prose-lint: git diff failed: {done.stderr.strip()}")
        return 2
    found = violations(done.stdout)
    for item in found:
        print(f"prose-lint: {item}")
    if found:
        print("prose-lint: FAILED — keep review and issue history in git and ADRs")
        return 1
    print("prose-lint: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
