"""Verify that a prose-only source change preserves code and tool directives.

Only leading string docstrings are excluded from the AST comparison. By
default compare --base with the working tree; --head compares two commits.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

SOURCE_PATHS = ("src", ":(glob)packages/*/src/**")
DIRECTIVE = re.compile(
    r"^#!|coding\s*[:=]|\b(?:noqa|nosec|prose-lint|fmt|isort|ruff|pylint|"
    r"pyright|mypy|pyre|coverage|pragma|doctest)\s*(?::|\b)|\btype\s*:",
    re.IGNORECASE,
)
IGNORED_TOKENS = {
    tokenize.ENCODING,
    tokenize.ENDMARKER,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.NEWLINE,
    tokenize.NL,
    tokenize.COMMENT,
}


def _docstring(node: ast.AST) -> ast.Expr | None:
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.body and isinstance(node.body[0], ast.Expr):
            value = node.body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return node.body[0]
    return None


class _Normalize(ast.NodeTransformer):
    def generic_visit(self, node: ast.AST) -> ast.AST:
        doc = _docstring(node)
        if doc is not None:
            node.body = node.body[1:]
        return super().generic_visit(node)

    def visit_TypeIgnore(self, node: ast.TypeIgnore) -> ast.TypeIgnore:
        # Its target is checked by directive token position, independent of prose lines.
        node.lineno = 0
        return node


def normalized_ast(source: str) -> str:
    tree = ast.parse(source, type_comments=True)
    return ast.dump(_Normalize().visit(tree), include_attributes=False)


def directive_signature(source: str) -> list[tuple[int, bool, int, str]]:
    """Bind each exact directive to its surrounding executable token position."""
    tree = ast.parse(source, type_comments=True)
    spans = []
    for node in ast.walk(tree):
        doc = _docstring(node)
        if doc is not None:
            spans.append(((doc.lineno, doc.col_offset), (doc.end_lineno, doc.end_col_offset)))
    position, code_row = 0, 0
    result = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and DIRECTIVE.search(token.string):
            fixed_line = token.start[0] if re.search(r"^#!|coding\s*[:=]", token.string) else 0
            result.append((position, code_row == token.start[0], fixed_line, token.string))
        elif token.type not in IGNORED_TOKENS:
            if token.type != tokenize.STRING or not any(
                start <= token.start and token.end <= end for start, end in spans
            ):
                position += 1
                code_row = token.end[0]
    return result


def differences(before: str, after: str) -> list[str]:
    found = []
    if normalized_ast(before) != normalized_ast(after):
        found.append("non-docstring AST changed")
    if directive_signature(before) != directive_signature(after):
        found.append("tool directive changed or moved to different code")
    return found


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise ValueError(done.stderr.strip() or f"git {args[0]} failed")
    return done.stdout


def _commit(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()


def changed_sources(root: Path, base: str, head: str | None) -> list[str]:
    refs = [base, head] if head else [base]
    changed = _git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
        *refs,
        "--",
        *SOURCE_PATHS,
    )
    paths = set(changed.split("\0"))
    if head is None:
        paths.update(
            _git(
                root, "ls-files", "--others", "--exclude-standard", "-z", "--", *SOURCE_PATHS
            ).split("\0")
        )
    return sorted(path for path in paths if path)


def check(root: Path, base: str, head: str | None = None) -> tuple[list[str], int]:
    base_sha = _commit(root, base)
    head_sha = _commit(root, head) if head is not None else None
    paths = changed_sources(root, base_sha, head_sha)
    failures = []
    for path in paths:
        if not path.endswith((".py", ".pyi")):
            failures.append(f"{path}: non-Python source changed; AST equivalence is unavailable")
            continue
        try:
            before = _git(root, "show", f"{base_sha}:{path}")
            after = (
                _git(root, "show", f"{head_sha}:{path}")
                if head_sha
                else (root / path).read_text(encoding="utf-8")
            )
            failures.extend(f"{path}: {message}" for message in differences(before, after))
        except (OSError, SyntaxError, ValueError, tokenize.TokenError) as exc:
            failures.append(f"{path}: cannot compare source: {exc}")
    return failures, len(paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="existing commit before the prose change")
    parser.add_argument(
        "--head", help="existing commit after the prose change (default: working tree)"
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        found, count = check(args.root, args.base, args.head)
    except (OSError, ValueError) as exc:
        print(f"ast-equivalence: cannot resolve comparison: {exc}", file=sys.stderr)
        return 2
    for failure in found:
        print(f"ast-equivalence: {failure}")
    if found:
        print("ast-equivalence: FAILED — prose-only changes must preserve code and directives")
        return 1
    print(f"ast-equivalence: ok — {count} source files, code and directives unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
