"""Fail when a JUnit XML report contains skipped or no tests.

The Invariant Suite must execute, not skip: a skipped invariant is not
evidence. Used by the ``invariants`` CI job.

Usage::

    uv run python scripts/assert_no_skips.py invariants.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def skipped_cases(path: str) -> tuple[int, list[str]]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    skipped = [
        f"{case.get('classname')}::{case.get('name')}"
        for case in cases
        if case.find("skipped") is not None
    ]
    return len(cases), skipped


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: assert_no_skips.py REPORT.xml")
        return 2
    total, skipped = skipped_cases(args[0])
    for name in skipped:
        print(f"assert-no-skips: skipped {name}")
    if total == 0 or skipped:
        print(f"assert-no-skips: FAILED — {total} cases, {len(skipped)} skipped")
        return 1
    print(f"assert-no-skips: ok — {total} cases executed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
