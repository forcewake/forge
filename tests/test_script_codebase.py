"""scripts/_codebase.py: the static analysis shared by the program's scripts."""

from __future__ import annotations

import pytest

from tests._scripts import load_script, write_tree

cb = load_script("_codebase")

PACKAGE = {
    "src/forge/__init__.py": "",
    "src/forge/top.py": "X = 1\n",
    "src/forge/pkg/__init__.py": "from . import sibling\nfrom .. import top\n",
    "src/forge/pkg/sibling.py": "Y = 1\n",
    "src/forge/pkg/deep.py": "",
    "src/forge/pkg/other.py": "",
    "src/forge/pkg/mod.py": (
        "import importlib\n"
        "from .sibling import Y\n"
        "from ..top import X\n"
        "def lazy():\n"
        "    from forge.pkg import deep\n"
        "    return importlib.import_module('forge.pkg.other')\n"
    ),
}


@pytest.fixture()
def modules(tmp_path):
    return cb.discover_modules(write_tree(tmp_path, PACKAGE))


def test_package_level_relative_imports_resolve_with_parents(modules):
    imported = cb.imports_of(modules["forge.pkg"].path, modules, "forge.pkg")
    assert imported == {"forge", "forge.pkg", "forge.pkg.sibling", "forge.top"}


def test_module_level_relative_lazy_and_literal_dynamic_imports(modules):
    imported = cb.imports_of(modules["forge.pkg.mod"].path, modules, "forge.pkg.mod")
    assert {"forge.pkg.sibling", "forge.top", "forge.pkg.deep", "forge.pkg.other"} <= imported


def test_function_sizes_cover_nested_conditional_and_duplicate_definitions(tmp_path):
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner\n"
        "class A:\n"
        "    class B:\n"
        "        def m(self):\n"
        "            return 2\n"
        "    @property\n"
        "    def p(self):\n"
        "        return 3\n"
        "    @p.setter\n"
        "    def p(self, value):\n"
        "        pass\n"
        "if True:\n"
        "    def guarded():\n"
        "        pass\n"
        "try:\n"
        "    def in_try():\n"
        "        pass\n"
        "except ImportError:\n"
        "    def in_try():\n"
        "        pass\n"
    )
    path = write_tree(tmp_path, {"m.py": source}) / "m.py"
    sizes = {size.qualname: size.lines for size in cb.function_sizes(path)}
    assert sizes == {
        "outer": 4,
        "outer.inner": 2,
        "A.B.m": 2,
        "A.p": 2,
        "A.p#2": 2,
        "guarded": 2,
        "in_try": 2,
        "in_try#2": 2,
    }


def test_syntax_errors_name_the_file(tmp_path):
    path = write_tree(tmp_path, {"broken.py": "def f(:\n"}) / "broken.py"
    with pytest.raises(SyntaxError) as caught:
        cb.function_sizes(path)
    assert caught.value.filename == str(path)


def test_prose_lines_count_comments_and_docstrings(tmp_path):
    source = '"""Module doc\nsecond line."""\n# a comment\nx = 1  # trailing\n'
    source += 'def f():\n    """One line."""\n'
    path = write_tree(tmp_path, {"m.py": source}) / "m.py"
    assert cb.prose_lines(path) == 5


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("# R42-16: the parity rule", 1),
        ("# Q35-09, NXT-10, NEXT-05 and AT-08", 4),
        ("# fixed in #377", 1),
        ("# as agreed in review `9e8d6c6`", 1),
        ("# ADR-0035 decides this", 0),
        ('url = "https://example.test/page#123"', 0),
        ('html = "&#123;"', 0),
        ('color = "#abc"', 0),
    ],
)
def test_review_reference_pattern(text: str, hits: int):
    assert len(cb.REVIEW_REF.findall(text)) == hits


def test_console_scripts_are_tooling_roots(tmp_path):
    root = write_tree(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n[project.scripts]\nforge = "forge.cli:main"\n',
            "src/forge/__init__.py": "",
            "src/forge/cli.py": "import importlib\n",
            "tests/test_cli.py": "from forge import cli\n",
        },
    )
    reach = cb.classify(root, cb.discover_modules(root))
    assert "forge.cli" in reach.tooling_only
    assert "forge.cli" not in reach.tests_only


def test_declared_roots_that_do_not_exist_are_reported(tmp_path):
    root = write_tree(tmp_path, {"src/forge/__init__.py": "", "src/forge/main.py": ""})
    reach = cb.classify(root, cb.discover_modules(root))
    assert "forge.main" not in reach.missing_roots
    assert "forge.migrate" in reach.missing_roots
    assert "forge_lane" not in reach.missing_roots
