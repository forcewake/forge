"""DSC-03: discovery tools are bounded and scope-filtered.

The tests prove the two bounds that make model-driven discovery safe:
output truncation is explicit and pageable (never a silent cut), and
paths outside ``allowed_globs`` do not exist for any tool — their names
never leak into any output.
"""

from __future__ import annotations

import json

import pytest

from forge.adaptive.discovery_tools import SnapshotToolbox

_FILES = {
    "src/app.py": (
        "def greet():\n"
        "    return 'hi'\n"
        "\n"
        "\n"
        "def greet_loud():\n"
        "    return greet().upper()\n"
        "\n"
        "\n"
        "class Greeter:\n"
        "    def render(self):\n"
        "        return greet()\n"
    ),
    "src/util.py": "NEEDLE = 1\nvalue = NEEDLE + 1\n",
    "private/notes.txt": "the NEEDLE is hidden here\n",
}


def _toolbox(**kwargs) -> SnapshotToolbox:
    defaults: dict = {"files": _FILES}
    defaults.update(kwargs)
    return SnapshotToolbox(**defaults)


class TestReadFile:
    def test_reads_whole_content_within_budget(self):
        result = _toolbox().read_file("src/util.py")
        assert result["path"] == "src/util.py"
        assert result["content"] == _FILES["src/util.py"]
        assert result["truncated"] is False
        assert result["complete"] is True

    def test_truncation_is_explicit_and_prefix_only(self):
        toolbox = _toolbox(max_output_bytes=10)
        result = toolbox.read_file("src/app.py")
        assert result["truncated"] is True
        assert result["complete"] is False
        assert result["content"] == _FILES["src/app.py"][:10]

    def test_truncated_reads_page_via_offset_and_length(self):
        toolbox = _toolbox(max_output_bytes=10)
        page1 = toolbox.read_file("src/app.py", offset=0, length=10)
        page2 = toolbox.read_file("src/app.py", offset=10, length=10)
        assert page1["content"] == _FILES["src/app.py"][0:10]
        assert page2["content"] == _FILES["src/app.py"][10:20]
        assert page1["truncated"] is False
        assert page2["complete"] is True
        assert page1["content"] + page2["content"] == _FILES["src/app.py"][:20]

    def test_unknown_path_raises_key_error(self):
        with pytest.raises(KeyError):
            _toolbox().read_file("does/not/exist.py")

    def test_negative_window_is_refused(self):
        with pytest.raises(ValueError, match="non-negative"):
            _toolbox().read_file("src/util.py", offset=-1)


class TestScopeFiltering:
    def test_unauthorized_paths_are_invisible_to_list_paths(self):
        toolbox = _toolbox(allowed_globs=["src/**"])
        result = toolbox.list_paths()
        assert result["paths"] == ["src/app.py", "src/util.py"]
        assert "private/notes.txt" not in json.dumps(result)

    def test_unauthorized_names_never_leak_through_grep(self):
        toolbox = _toolbox(allowed_globs=["src/**"])
        result = toolbox.grep("NEEDLE")
        rendered = json.dumps(result)
        # The private path's NAME and its snippet are both absent.
        assert "private" not in rendered
        assert "notes.txt" not in rendered
        assert "hidden" not in rendered
        assert [match["path"] for match in result["matches"]] == ["src/util.py", "src/util.py"]

    def test_reading_an_unauthorized_path_is_indistinguishable_from_unknown(self):
        toolbox = _toolbox(allowed_globs=["src/**"])
        with pytest.raises(KeyError):
            toolbox.read_file("private/notes.txt")

    def test_default_glob_sees_everything(self):
        toolbox = _toolbox()
        assert toolbox.path_count == 3


class TestListPaths:
    def test_lists_sorted_paths_under_a_prefix(self):
        result = _toolbox().list_paths(prefix="src/")
        assert result["paths"] == ["src/app.py", "src/util.py"]
        assert result["truncated"] is False
        assert result["complete"] is True

    def test_cap_at_five_hundred_marks_truncation(self):
        files = {f"dir/file_{index:04d}.py": "" for index in range(501)}
        result = SnapshotToolbox(files).list_paths()
        assert len(result["paths"]) == 500
        assert result["truncated"] is True
        assert result["complete"] is False

    def test_budget_exhaustion_cuts_the_list_and_marks_incomplete(self):
        files = {f"very_long_directory_name/file_{index:04d}.py": "" for index in range(100)}
        result = SnapshotToolbox(files, max_output_bytes=2000).list_paths()
        assert len(result["paths"]) < 100
        assert result["truncated"] is True
        assert result["complete"] is False


class TestGrep:
    def test_substring_mode_matches_lines(self):
        result = _toolbox().grep("NEEDLE")
        assert result["complete"] is True
        assert {"path": "src/util.py", "line_no": 1, "text": "NEEDLE = 1"} in result["matches"]
        assert {"path": "src/util.py", "line_no": 2, "text": "value = NEEDLE + 1"} in result[
            "matches"
        ]
        assert {"path": "private/notes.txt", "line_no": 1, "text": "the NEEDLE is hidden here"} in (
            result["matches"]
        )

    def test_regex_mode_matches_patterns(self):
        result = _toolbox().grep(r"def\s+\w+", is_regex=True)
        assert {match["line_no"] for match in result["matches"]} == {1, 5, 10}
        assert result["complete"] is True

    def test_budget_exhaustion_returns_what_fits_and_marks_incomplete(self):
        # Iteration is path-sorted, so the private file's hit is gathered
        # first; size the budget to fit it exactly and nothing more.
        first = {"path": "private/notes.txt", "line_no": 1, "text": "the NEEDLE is hidden here"}
        toolbox = _toolbox(max_output_bytes=len(first["path"]) + len(first["text"]))
        result = toolbox.grep("NEEDLE")
        assert result["matches"] == [first]
        assert result["complete"] is False

    def test_budget_smaller_than_one_match_reports_nothing_but_incomplete(self):
        result = _toolbox(max_output_bytes=3).grep("NEEDLE")
        assert result["matches"] == []
        assert result["complete"] is False


class TestFindSymbol:
    def test_locates_a_def_and_labels_it_a_function(self):
        result = _toolbox().find_symbol("greet")
        by_symbol = {(entry["symbol"], entry["kind"]) for entry in result["symbols"]}
        assert ("greet", "function") in by_symbol
        assert ("greet_loud", "function") in by_symbol
        assert result["complete"] is True
        greet = next(entry for entry in result["symbols"] if entry["symbol"] == "greet")
        assert greet["path"] == "src/app.py"
        assert greet["line_no"] == 1

    def test_locates_a_class_and_labels_it_a_class(self):
        result = _toolbox().find_symbol("Greeter")
        assert [(entry["symbol"], entry["kind"]) for entry in result["symbols"]] == [
            ("Greeter", "class")
        ]

    def test_budget_exhaustion_marks_incomplete(self):
        toolbox = _toolbox(max_output_bytes=5)
        result = toolbox.find_symbol("greet")
        assert result["symbols"] == []
        assert result["complete"] is False


class TestFindReferences:
    def test_finds_usages_and_excludes_declarations(self):
        result = _toolbox().find_references("greet")
        texts = [reference["text"] for reference in result["references"]]
        # The def lines are gone; every remaining line actually uses greet.
        assert texts == ["    return greet().upper()", "        return greet()"]
        assert result["complete"] is True

    def test_only_declarations_of_the_name_itself_are_excluded(self):
        files = {"mod.py": "def configure(name):\n    return configure(name.strip())\n"}
        # A line is a declaration only when the DECLARED symbol contains
        # the name — `def configure(name)` declares `configure`, so for
        # "configure" it is excluded, while for the parameter "name" it
        # is an ordinary usage line.
        configure = SnapshotToolbox(files).find_references("configure")
        assert [reference["text"] for reference in configure["references"]] == [
            "    return configure(name.strip())"
        ]
        name = SnapshotToolbox(files).find_references("name")
        assert [reference["text"] for reference in name["references"]] == [
            "def configure(name):",
            "    return configure(name.strip())",
        ]

    def test_budget_exhaustion_marks_incomplete(self):
        result = _toolbox(max_output_bytes=3).find_references("greet")
        assert result["references"] == []
        assert result["complete"] is False
