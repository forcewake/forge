"""Bounded read, search and symbol tools over one snapshot (DSC-03).

A plan step discovers a repository through tools, and tools are where
context exhaustion and scope leaks actually happen — so the bounds live
here, not in prompts:

- **Bounded output** — every tool answers within ``max_output_bytes``.
  Truncation is explicit (``truncated`` / ``complete`` flags), never a
  silent cut, and a budget exhaustion never erases the failure marker:
  the caller gets what was gathered plus ``complete=False``.
- **Scope-filtered before ranking** — paths outside ``allowed_globs``
  are removed before any matching runs. An unauthorized path never
  appears in any output: not its name, not a snippet, not a count.
- **Read-only** — these tools answer questions; they never mutate the
  snapshot they run against.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from fnmatch import fnmatchcase

#: ``list_paths`` hard cap: a repository can hold unbounded files, but a
#: discovery answer must stay readable by a model in one turn.
_MAX_PATHS = 500

#: Declaration shape for the lexical symbol search. Captures the declared
#: name so ``find_symbol`` can report the full symbol and
#: ``find_references`` can recognize the declaration lines it must drop.
_DECL_RE = re.compile(r"\b(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")


class SnapshotToolbox:
    """Read/search/symbol tools over the files of ONE repository snapshot.

    ``files`` maps path to content; ``allowed_globs`` restricts which
    paths exist at all (fnmatch semantics; ``**`` matches everything,
    matching is case-sensitive so authorization never depends on the host
    OS). ``max_output_bytes`` is the output budget each call answers
    within — string lengths are the cost model, applied cumulatively
    across the results of a single call.
    """

    def __init__(
        self,
        files: dict[str, str],
        *,
        max_output_bytes: int = 65536,
        allowed_globs: list[str] | None = None,
    ) -> None:
        self._files = dict(files)
        self._max_output_bytes = max_output_bytes
        globs = ["**"] if allowed_globs is None else list(allowed_globs)
        # Filtering happens HERE, before any tool ranks or matches: what
        # is not authorized does not exist for this toolbox.
        self._paths = sorted(
            path for path in self._files if any(fnmatchcase(path, g) for g in globs)
        )

    @property
    def path_count(self) -> int:
        """How many paths this toolbox can see (unauthorized ones excluded)."""
        return len(self._paths)

    def read_file(self, path: str, offset: int = 0, length: int | None = None) -> dict[str, object]:
        """Read one file, optionally a window of it.

        Returns ``{"path", "content", "truncated", "complete"}``. When the
        requested content exceeds the output budget the prefix is returned
        with ``truncated=True`` and the remainder is paged via
        ``offset``/``length``. An unauthorized path and an unknown path
        raise the same :class:`KeyError` — on purpose, so the failure says
        nothing about whether the path exists.
        """
        if path not in self._paths:
            # Unauthorized and unknown are indistinguishable by design.
            raise KeyError(path)
        if offset < 0 or (length is not None and length < 0):
            raise ValueError("offset and length must be non-negative")
        content = self._files[path]
        end = len(content) if length is None else min(len(content), offset + length)
        window = content[offset:end]
        if len(window) > self._max_output_bytes:
            return {
                "path": path,
                "content": window[: self._max_output_bytes],
                "truncated": True,
                "complete": False,
            }
        return {"path": path, "content": window, "truncated": False, "complete": True}

    def list_paths(self, prefix: str = "") -> dict[str, object]:
        """List visible paths under ``prefix``, sorted, capped at 500.

        Returns ``{"paths", "truncated", "complete"}``. ``truncated`` marks
        the cap or the budget cutting the list short; ``complete`` is the
        budget-honesty flag — ``True`` only when every matching path is in
        the answer.
        """
        all_paths = [path for path in self._paths if path.startswith(prefix)]
        kept: list[str] = []
        remaining = self._max_output_bytes
        for path in all_paths[:_MAX_PATHS]:
            if len(path) > remaining:
                break
            remaining -= len(path)
            kept.append(path)
        truncated = len(kept) < len(all_paths)
        return {"paths": kept, "truncated": truncated, "complete": not truncated}

    def grep(self, pattern: str, *, is_regex: bool = False) -> dict[str, object]:
        """Search every visible file for ``pattern`` line by line.

        Returns ``{"matches", "complete"}`` where each match is
        ``{"path", "line_no", "text"}``. Without ``is_regex`` the pattern
        is a literal substring. Match text is never cut mid-line — when
        the budget runs out, gathering stops and ``complete`` is ``False``
        so the caller knows the match list is partial.
        """
        regex = re.compile(pattern) if is_regex else None
        return self._scan_lines(
            "matches",
            lambda text: bool(regex.search(text)) if regex else pattern in text,
        )

    def find_symbol(self, name: str) -> dict[str, object]:
        """Find ``def``/``class`` declarations whose symbol contains *name*.

        Returns ``{"symbols", "complete"}`` where each entry is
        ``{"path", "line_no", "symbol", "kind"}`` and ``kind`` comes from
        the declaration itself (``def``/``async def`` -> ``function``,
        ``class`` -> ``class``).
        """
        symbols: list[dict[str, object]] = []
        remaining = self._max_output_bytes
        complete = True
        for path in self._paths:
            for line_no, text in enumerate(self._files[path].splitlines(), start=1):
                match = _DECL_RE.search(text)
                if match is None or name not in match.group(1):
                    continue
                cost = len(path) + len(match.group(1))
                if cost > remaining:
                    complete = False
                    break
                remaining -= cost
                keyword = match.group(0).split()[0]
                symbols.append(
                    {
                        "path": path,
                        "line_no": line_no,
                        "symbol": match.group(1),
                        "kind": "class" if keyword == "class" else "function",
                    }
                )
            if not complete:
                break
        return {"symbols": symbols, "complete": complete}

    def find_references(self, name: str) -> dict[str, object]:
        """Find every line containing *name* except its declarations.

        Returns ``{"references", "complete"}`` where each entry is
        ``{"path", "line_no", "text"}``. A line is a declaration when it
        declares a symbol containing *name* — exactly the lines
        :meth:`find_symbol` would return; a parameter that merely shares
        the name (``def run(name):``) is a usage, not a declaration.
        """
        return self._scan_lines(
            "references",
            lambda text: name in text and not self._declares(text, name),
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _declares(text: str, name: str) -> bool:
        """True when *text* declares a symbol containing *name*."""
        match = _DECL_RE.search(text)
        return match is not None and name in match.group(1)

    def _scan_lines(self, result_key: str, hit: Callable[[str], bool]) -> dict[str, object]:
        """Shared line-scanner: gather matching lines under the budget.

        ``hit`` decides whether a line matches; ``result_key`` names the
        list in the answer (``matches`` for grep, ``references`` for
        find_references). Gathering stops at the first line that would
        exceed the remaining budget — partial text would be worse than
        partial coverage, which ``complete=False`` already announces.
        """
        matches: list[dict[str, object]] = []
        remaining = self._max_output_bytes
        complete = True
        for path in self._paths:
            for line_no, text in enumerate(self._files[path].splitlines(), start=1):
                if not hit(text):
                    continue
                cost = len(path) + len(text)
                if cost > remaining:
                    complete = False
                    break
                remaining -= cost
                matches.append({"path": path, "line_no": line_no, "text": text})
            if not complete:
                break
        return {result_key: matches, "complete": complete}
