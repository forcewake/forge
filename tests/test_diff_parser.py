from pathlib import Path

from forge.context.diff_parser import parse_diff

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParseDiff:
    def test_empty_diff_returns_empty_list(self):
        assert parse_diff("") == []
        assert parse_diff("   \n  ") == []

    def test_multi_file_diff_count(self):
        raw = _load_fixture("sample.diff")
        files = parse_diff(raw)
        assert len(files) == 5

    def test_modified_file(self):
        raw = _load_fixture("sample.diff")
        files = parse_diff(raw)
        f = files[0]
        assert f.old_path == "src/utils/helper.py"
        assert f.new_path == "src/utils/helper.py"
        assert not f.is_new
        assert not f.is_deleted
        assert not f.is_renamed
        assert not f.is_binary
        assert len(f.hunks) == 2

    def test_modified_file_line_numbers(self):
        raw = _load_fixture("sample.diff")
        files = parse_diff(raw)
        hunk = files[0].hunks[0]
        assert hunk.old_start == 1
        assert hunk.old_count == 7
        assert hunk.new_start == 1
        assert hunk.new_count == 8

        # First line is context "import os"
        first_line = hunk.lines[0]
        assert first_line.line_type == "context"
        assert first_line.content == "import os"
        assert first_line.old_lineno == 1
        assert first_line.new_lineno == 1

        # Second line is added "import sys"
        add_line = hunk.lines[1]
        assert add_line.line_type == "add"
        assert add_line.content == "import sys"
        assert add_line.old_lineno is None
        assert add_line.new_lineno == 2

    def test_new_file(self):
        raw = _load_fixture("sample.diff")
        files = parse_diff(raw)
        f = files[1]
        assert f.new_path == "src/new_module.py"
        assert f.is_new is True
        assert f.is_deleted is False
        assert len(f.hunks) == 1
        assert all(line.line_type == "add" for line in f.hunks[0].lines)

    def test_deleted_file(self):
        raw = _load_fixture("sample.diff")
        files = parse_diff(raw)
        f = files[2]
        assert f.old_path == "src/obsolete.py"
        assert f.is_deleted is True
        assert f.is_new is False
        assert len(f.hunks) == 1
        assert all(line.line_type == "remove" for line in f.hunks[0].lines)

    def test_renamed_file(self):
        raw = _load_fixture("sample.diff")
        files = parse_diff(raw)
        f = files[3]
        assert f.old_path == "src/old_name.py"
        assert f.new_path == "src/renamed.py"
        assert f.is_renamed is True

    def test_binary_file(self):
        raw = _load_fixture("sample.diff")
        files = parse_diff(raw)
        f = files[4]
        assert f.is_binary is True
        assert f.hunks == []
        assert f.old_path == "assets/logo.png"
        assert f.new_path == "assets/logo.png"

    def test_second_hunk_line_numbers(self):
        raw = _load_fixture("sample.diff")
        files = parse_diff(raw)
        hunk2 = files[0].hunks[1]
        assert hunk2.old_start == 15
        assert hunk2.new_start == 16

        # Last line of second hunk is a removal
        last = hunk2.lines[-1]
        assert last.line_type == "remove"
        assert 'return "\\n".join(result)' in last.content

    def test_inline_diff_string(self):
        """Parse a minimal diff provided as a string, not a fixture."""
        raw = (
            "diff --git a/foo.txt b/foo.txt\n"
            "index 1234567..abcdefg 100644\n"
            "--- a/foo.txt\n"
            "+++ b/foo.txt\n"
            "@@ -1 +1 @@\n"
            "-old line\n"
            "+new line\n"
        )
        files = parse_diff(raw)
        assert len(files) == 1
        f = files[0]
        assert f.hunks[0].old_count == 1
        assert f.hunks[0].new_count == 1
        assert len(f.hunks[0].lines) == 2
