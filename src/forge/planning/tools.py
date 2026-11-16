"""Bounded tools and citation resolution over exact authorized Git snapshots."""

from __future__ import annotations

import hashlib
import re
from fnmatch import fnmatchcase
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from forge_protocol import canonical_json

from forge.domain.paths import private_repo_path
from forge.domain.plan import Plan, RepoPath
from forge.domain.value import Value
from forge.port_values import NeighborSnapshot, PlanningSnapshot, ReadUnavailable, TreeEntry
from forge.ports import RepoPort

from .limits import PlanningLimits, PlanningRefused

DECLARATION = re.compile(r"\b(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")
MAX_READ_LINES = 100


class ToolCall(Value):
    tool: Literal["list_paths", "read_file", "grep", "find_symbol", "find_references"]
    repo: Annotated[str, Field(min_length=1, max_length=32)]
    path: RepoPath | None = None
    prefix: RepoPath | Literal[""] = ""

    @field_validator("path", mode="before")
    @classmethod
    def empty_path_is_the_repository_root(cls, value: object) -> object:
        if value == "":
            return None
        return value

    query: Annotated[str, Field(max_length=256)] = ""
    start_line: Annotated[int, Field(gt=0)] = 1
    line_count: Annotated[int, Field(gt=0, le=10_000)] = MAX_READ_LINES


def lf_lines(text: str) -> tuple[str, ...]:
    parts = text.split("\n")
    return tuple(part + "\n" for part in parts[:-1]) + ((parts[-1],) if parts[-1] else ())


class ReadTools:
    def __init__(self, reader: RepoPort, snapshot: PlanningSnapshot, limits: PlanningLimits):
        self.reader, self.snapshot, self.limits = reader, snapshot, limits
        self.scopes = {"primary": snapshot.primary}
        self.scopes.update(
            {f"neighbor-{index}": scope for index, scope in enumerate(snapshot.neighbors, 1)}
        )
        self.trees: dict[str, dict[str, TreeEntry]] = {}
        self.blobs: dict[tuple[str, str], tuple[str, ...]] = {}
        self.observed: dict[tuple[str, str], set[int]] = {}
        self.read_bytes, self.calls = 0, 0

    def inventory(self) -> list[dict[str, Any]]:
        return [
            {"key": key, "repository": scope.repo.identity.model_dump(), "sha": scope.sha}
            for key, scope in self.scopes.items()
        ]

    def scope(self, key: str) -> NeighborSnapshot:
        if key not in self.scopes:
            raise PlanningRefused("read_not_available")
        return self.scopes[key]

    async def entries(self, key: str) -> dict[str, TreeEntry]:
        scope = self.scope(key)
        if key not in self.trees:
            try:
                tree = await self.reader.read_tree(scope.repo, scope.sha)
            except ReadUnavailable:
                raise PlanningRefused("repository_read_unavailable") from None
            if tree.repo.identity != scope.repo.identity or tree.sha != scope.sha:
                raise PlanningRefused("repository_snapshot_mismatch")
            self.trees[key] = {
                entry.path: entry
                for entry in tree.entries
                if entry.kind == "blob"
                and entry.mode in ("100644", "100755")
                and not private_repo_path(entry.path)
                and any(fnmatchcase(entry.path, glob) for glob in scope.allowed_globs)
            }
        return self.trees[key]

    async def lines(self, key: str, path: str) -> tuple[str, ...]:
        scope = self.scope(key)
        if private_repo_path(path) or not any(
            fnmatchcase(path, glob) for glob in scope.allowed_globs
        ):
            raise PlanningRefused("read_not_available")
        entry = (await self.entries(key)).get(path)
        if entry is None:
            raise PlanningRefused("read_not_available")
        cache_key = key, path
        if cache_key not in self.blobs:
            if (
                self.read_bytes >= self.limits.total_read_bytes
                or entry.size is not None
                and (
                    entry.size > self.limits.blob_bytes
                    or self.read_bytes + entry.size > self.limits.total_read_bytes
                )
            ):
                raise PlanningRefused("blob_read_limit")
            try:
                data = await self.reader.read_blob(scope.repo, scope.sha, path)
            except ReadUnavailable:
                raise PlanningRefused("repository_read_unavailable") from None
            if data is None:
                raise PlanningRefused("read_not_available")
            self.read_bytes += len(data)
            if len(data) > self.limits.blob_bytes or self.read_bytes > self.limits.total_read_bytes:
                raise PlanningRefused("blob_read_limit")
            header = f"blob {len(data)}\0".encode("ascii")
            oid = hashlib.new(
                "sha1" if len(entry.sha) == 40 else "sha256", header + data
            ).hexdigest()
            if oid != entry.sha or entry.size is not None and len(data) != entry.size:
                raise PlanningRefused("blob_identity_mismatch")
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise PlanningRefused("blob_not_text") from None
            if "\0" in text:
                raise PlanningRefused("blob_not_text")
            self.blobs[cache_key] = lf_lines(text)
        return self.blobs[cache_key]

    def fits(self, result: dict[str, Any]) -> bool:
        return len(canonical_json(result)) <= self.limits.tool_output_bytes

    def bounded_search(self, result: dict[str, Any]) -> dict[str, Any]:
        while not self.fits(result) and result["matches"]:
            result["matches"].pop()
            result.update(complete=False, truncated=True)
        if not self.fits(result):
            raise PlanningRefused("tool_output_limit")
        return result

    async def call(self, request: ToolCall) -> dict[str, Any]:
        if self.calls >= self.limits.tool_calls:
            raise PlanningRefused("planning_tool_limit")
        self.calls += 1
        self.scope(request.repo)
        if request.tool == "read_file":
            if request.path is None:
                raise PlanningRefused("invalid_read_request")
            result = await self.read_file(request)
            self.observed.setdefault((request.repo, request.path), set()).update(
                range(request.start_line, request.start_line + len(lf_lines(result["content"])))
            )
            return result
        if request.tool == "list_paths":
            paths = sorted(
                path for path in await self.entries(request.repo) if path.startswith(request.prefix)
            )
            result = {"paths": [], "complete": True, "truncated": False}
            for path in paths:
                result["paths"].append(path)
                if not self.fits(result):
                    result["paths"].pop()
                    result.update(complete=False, truncated=True)
                    break
            return result
        result = await self.search(request)
        for match in result["matches"]:
            self.observed.setdefault((request.repo, match["path"]), set()).add(match["line"])
        return result

    async def read_file(self, request: ToolCall) -> dict[str, Any]:
        assert request.path is not None
        lines = await self.lines(request.repo, request.path)
        start = request.start_line - 1
        result: dict[str, Any] = {
            "path": request.path,
            "start_line": request.start_line,
            "content": "",
            "complete": True,
            "truncated": False,
            "next_line": None,
        }
        kept = 0
        limit = min(request.line_count, MAX_READ_LINES)
        for line in lines[start : start + limit]:
            previous = result["content"]
            result["content"] += line
            result["next_line"] = start + kept + 2 if start + kept + 1 < len(lines) else None
            if not self.fits(result):
                result["content"] = previous
                result["next_line"] = start + kept + 1
                result["truncated"] = True
                break
            kept += 1
        if request.line_count > limit and start + kept < len(lines):
            result["truncated"] = True
        result["complete"] = not result["truncated"] and start + kept >= len(lines)
        if not self.fits(result):
            raise PlanningRefused("tool_output_limit")
        return result

    async def search(self, request: ToolCall) -> dict[str, Any]:
        if not request.query:
            raise PlanningRefused("empty_search")
        result: dict[str, Any] = {"matches": [], "complete": True, "truncated": False}
        paths = sorted(await self.entries(request.repo))
        for path in paths[: self.limits.scan_files]:
            try:
                lines = await self.lines(request.repo, path)
            except PlanningRefused as exc:
                result.update(complete=False, reason=exc.reason)
                break
            for number, text in enumerate(lines, 1):
                declaration = DECLARATION.search(text)
                hit = request.query in text
                if request.tool == "find_symbol":
                    hit = declaration is not None and request.query in declaration[1]
                elif request.tool == "find_references":
                    hit = hit and not (declaration is not None and request.query in declaration[1])
                if hit:
                    result["matches"].append({"path": path, "line": number, "text": text})
                    if not self.fits(result):
                        result["matches"].pop()
                        result.update(complete=False, truncated=True)
                        return self.bounded_search(result)
        if len(paths) > self.limits.scan_files:
            result["complete"] = False
        return self.bounded_search(result)

    async def validate_citations(self, plan: Plan) -> None:
        by_identity = {scope.repo.identity.digest: key for key, scope in self.scopes.items()}
        for step in plan.steps:
            for citation in step.citations:
                key = by_identity.get(citation.repository.digest)
                if key is None or citation.source_sha != self.scopes[key].sha:
                    raise PlanningRefused("unresolved_citation")
                lines = await self.lines(key, citation.path)
                if citation.end_line > len(lines):
                    raise PlanningRefused("unresolved_citation")
                seen = self.observed.get((key, citation.path), set())
                if any(
                    line not in seen for line in range(citation.start_line, citation.end_line + 1)
                ):
                    raise PlanningRefused("unobserved_citation")
