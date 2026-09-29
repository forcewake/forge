"""scripts/prose_lint.py: no review, backlog or issue archaeology in added source lines."""

from __future__ import annotations

from tests._scripts import load_script

prose_lint = load_script("prose_lint")

DIFF = """diff --git a/src/forge/x.py b/src/forge/x.py
--- a/src/forge/x.py
+++ b/src/forge/x.py
@@ -10,0 +11,5 @@
+# R42-16: the parity rule
+x = 1  # see ADR-0035
+# fixed in #377
+# as agreed in review `9e8d6c6`
+# carried over from NXT-10
"""


def test_review_backlog_and_issue_references_are_rejected():
    found = prose_lint.violations(DIFF)
    assert [item.split(":")[1] for item in found] == ["11", "13", "14", "15"]


def test_adr_references_are_allowed():
    assert not any("ADR-0035" in item for item in prose_lint.violations(DIFF))


def test_removed_lines_are_ignored():
    diff = "+++ b/src/forge/x.py\n@@ -3,1 +2,0 @@\n-# R42-16 old comment\n"
    assert prose_lint.violations(diff) == []


def test_a_missing_or_zero_base_is_a_no_op():
    assert prose_lint.main(["--base", ""]) == 0
    assert prose_lint.main(["--base", "0" * 40]) == 0
