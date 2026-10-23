"""Native-repository reads bound to immutable commits and verified Git objects."""

import base64
import binascii
import hashlib
import json
import re
from typing import Literal, cast
from urllib.parse import quote

from pydantic import TypeAdapter, ValidationError

from forge.domain.plan import RepoPath
from forge.domain.verdicts import CommitOutcome, Committed, Conflict, Sha
from forge.port_values import (
    Applied,
    BranchOutcome,
    BranchRef,
    Found,
    Inconclusive,
    ProbeResult,
    ReadUnavailable,
    RepoRef,
    Tree,
    TreeEntry,
)
from forge.providers.github.client import API, Reply
from forge.providers.github.transport import MAX_JSON, TransportFailure

MAX_TREE_ENTRIES = 100_000
MAX_HISTORY_PAGES = 10
_PATH = TypeAdapter(RepoPath)
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def evidence(operation: str, request_id: str = "", **details: str) -> str:
    safe_id = request_id if re.fullmatch(r"[A-Fa-f0-9:]{1,128}", request_id) else ""
    return json.dumps(
        {"provider": "github", "operation": operation, "request_id": safe_id, **details},
        sort_keys=True,
        separators=(",", ":"),
    )


def malformed(reason: str) -> ReadUnavailable:
    return ReadUnavailable(reason, evidence("read", reason=reason))


def object_doc(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise malformed("invalid_object")
    return cast(dict[str, object], value)


def list_doc(value: object) -> list[object]:
    if not isinstance(value, list):
        raise malformed("invalid_array")
    return cast(list[object], value)


def text(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or len(value) > MAX_JSON:
        raise malformed("invalid_text")
    return value


def native_id(value: object) -> str:
    if type(value) is not int or value <= 0:
        raise malformed("invalid_native_id")
    return str(value)


def object_sha(value: object) -> Sha:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise malformed("invalid_sha")
    return value


def repo_path(value: str) -> str:
    try:
        return _PATH.validate_python(value)
    except ValidationError:
        raise malformed("invalid_read_path") from None


def branch_path(value: str) -> str:
    if (
        not value
        or len(value) > 1024
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
        or any(char in value for char in "~^:?*[\\")
        or "@{" in value
        or ".." in value
        or value.endswith(("/", ".", ".lock"))
        or any(not part or part.startswith(".") for part in value.split("/"))
    ):
        raise malformed("invalid_branch")
    return quote(value, safe="/")


async def request(
    api: API, repo: RepoRef, resource: str, *, params: dict[str, str | int] | None = None
) -> Reply:
    try:
        return await api.request(repo, "GET", resource, params=params)
    except TransportFailure as error:
        raise ReadUnavailable("github_read_unavailable", error.evidence) from None
    except ValueError:
        raise malformed("unsupported_read_resource") from None


def marker_line(body: str, marker: str) -> bool:
    if (
        not marker
        or len(marker) > 1024
        or marker.strip() != marker
        or "\n" in marker
        or "\r" in marker
    ):
        raise malformed("invalid_marker")
    count = sum(line.strip() == marker for line in body.splitlines())
    if count > 1:
        raise malformed("duplicate_marker_lines")
    return count == 1


class GitHubRepo:
    def __init__(self, api: API, *, max_blob_bytes: int = MAX_JSON) -> None:
        if not 0 < max_blob_bytes <= MAX_JSON:
            raise ValueError("blob limit must be bounded")
        self.api, self.max_blob_bytes = api, max_blob_bytes

    async def head(self, repo: RepoRef, branch: str) -> Sha | None:
        try:
            reply = await self.api.request(repo, "GET", "git/ref/heads/" + branch_path(branch))
        except TransportFailure as error:
            if error.reason == "resource_not_found":
                return None
            raise ReadUnavailable("github_branch_unavailable", error.evidence) from None
        except ValueError:
            raise malformed("unsupported_branch_resource") from None
        document = object_doc(reply.document)
        target = object_doc(document.get("object"))
        if document.get("ref") != "refs/heads/" + branch or target.get("type") != "commit":
            raise malformed("branch_identity_mismatch")
        return object_sha(target.get("sha"))

    async def read_tree(self, repo: RepoRef, sha: Sha, prefix: str = "") -> Tree:
        commit_sha = object_sha(sha)
        confined = repo_path(prefix) if prefix else ""
        commit = object_doc((await request(self.api, repo, "git/commits/" + commit_sha)).document)
        if commit.get("sha") != commit_sha:
            raise malformed("commit_identity_mismatch")
        tree_sha = object_sha(object_doc(commit.get("tree")).get("sha"))
        reply = await request(self.api, repo, "git/trees/" + tree_sha, params={"recursive": "1"})
        document = object_doc(reply.document)
        values = list_doc(document.get("tree"))
        if (
            document.get("sha") != tree_sha
            or document.get("truncated") is not False
            or len(values) > MAX_TREE_ENTRIES
        ):
            raise malformed("incomplete_or_wrong_tree")
        try:
            entries = tuple(self._entry(value) for value in values)
            # Validate every entry before applying a caller's prefix: malformed hidden paths are not a complete tree.
            complete = Tree(repo=repo, sha=commit_sha, entries=entries)
            return complete.model_copy(
                update={
                    "entries": tuple(
                        entry
                        for entry in entries
                        if not confined
                        or entry.path == confined
                        or entry.path.startswith(confined + "/")
                    )
                }
            )
        except ValidationError:
            raise malformed("invalid_tree_entry") from None

    @staticmethod
    def _entry(value: object) -> TreeEntry:
        entry = object_doc(value)
        mode, kind = entry.get("mode"), entry.get("type")
        expected = {
            "100644": "blob",
            "100755": "blob",
            "120000": "blob",
            "040000": "tree",
            "160000": "commit",
        }
        if not isinstance(mode, str) or expected.get(mode) != kind:
            raise malformed("tree_mode_type_mismatch")
        size = entry.get("size")
        if size is not None and (type(size) is not int or size < 0):
            raise malformed("invalid_blob_size")
        return TreeEntry(
            path=repo_path(text(entry.get("path"))),
            sha=object_sha(entry.get("sha")),
            mode=cast(Literal["100644", "100755", "120000", "040000", "160000"], mode),
            kind=cast(Literal["blob", "tree", "commit"], kind),
            size=size,
        )

    async def read_blob(self, repo: RepoRef, sha: Sha, path: str) -> bytes | None:
        confined = repo_path(path)
        tree = await self.read_tree(repo, sha)
        entry = next((entry for entry in tree.entries if entry.path == confined), None)
        if entry is None:
            try:
                await self.api.request(
                    repo, "GET", "contents/" + quote(confined, safe="/"), params={"ref": sha}
                )
            except TransportFailure as error:
                if error.reason == "resource_not_found":
                    return None
                raise ReadUnavailable("github_blob_unavailable", error.evidence) from None
            except ValueError:
                raise malformed("unsupported_blob_resource") from None
            raise malformed("tree_contents_disagree")
        if entry.kind != "blob" or (entry.size is not None and entry.size > self.max_blob_bytes):
            raise malformed("blob_kind_or_size_refused")
        document = object_doc((await request(self.api, repo, "git/blobs/" + entry.sha)).document)
        size, content = document.get("size"), document.get("content")
        if (
            document.get("sha") != entry.sha
            or document.get("encoding") != "base64"
            or type(size) is not int
            or not 0 <= size <= self.max_blob_bytes
            or not isinstance(content, str)
            or len(content) > MAX_JSON
        ):
            raise malformed("invalid_blob_document")
        try:
            body = base64.b64decode(content.replace("\n", "").replace("\r", ""), validate=True)
        except (binascii.Error, ValueError):
            raise malformed("invalid_blob_encoding") from None
        digest = hashlib.sha1 if len(entry.sha) == 40 else hashlib.sha256
        actual = digest(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
        if (
            len(body) != size
            or (entry.size is not None and len(body) != entry.size)
            or actual != entry.sha
        ):
            raise malformed("blob_integrity_mismatch")
        return body

    async def _marker(
        self, repo: RepoRef, branch: str, marker: str
    ) -> tuple[str | None, list[dict[str, object]]]:
        head = await self.head(repo, branch)
        if head is None:
            return None, []
        matches: list[dict[str, object]] = []
        seen: set[str] = set()
        for page in range(1, MAX_HISTORY_PAGES + 1):
            reply = await request(
                self.api, repo, "commits", params={"sha": head, "per_page": 100, "page": page}
            )
            rows = list_doc(reply.document)
            if len(rows) > 100:
                raise malformed("commit_page_too_large")
            for value in rows:
                row = object_doc(value)
                oid = object_sha(row.get("sha"))
                if oid in seen:
                    raise malformed("duplicate_commit_page")
                seen.add(oid)
                if marker_line(
                    text(object_doc(row.get("commit")).get("message"), empty=True), marker
                ):
                    matches.append(row)
            if page == 1 and (not rows or object_doc(rows[0]).get("sha") != head):
                raise malformed("commit_history_head_mismatch")
            if len(rows) < 100:
                return head, matches
        raise malformed("commit_history_truncated")

    async def find_commit_by_marker(self, repo: RepoRef, branch: str, marker: str) -> Sha | None:
        _, matches = await self._marker(repo, branch, marker)
        if len(matches) == 1:
            return object_sha(matches[0].get("sha"))
        raise malformed(
            "commit_marker_ambiguous" if matches else "commit_marker_visibility_unknown"
        )

    async def probe_branch(
        self, repo: RepoRef, branch: str, base: Sha
    ) -> ProbeResult[BranchOutcome]:
        try:
            head = await self.head(repo, branch)
            proof = evidence("probe_branch", head=head or "")
            if head is None:
                return Inconclusive(
                    reason="branch visibility is not proof of absence", evidence=proof
                )
            result: BranchOutcome = (
                Applied[BranchRef](
                    value=BranchRef(repo=repo, branch=branch, sha=head), evidence=proof
                )
                if head == base
                else Conflict(actual_head=head, evidence=proof)
            )
            return Found[BranchOutcome](value=result, evidence=proof)
        except ReadUnavailable as error:
            return Inconclusive(reason="branch read unavailable", evidence=error.evidence)

    async def probe_commit(
        self, repo: RepoRef, branch: str, marker: str, expected_parent: Sha
    ) -> ProbeResult[CommitOutcome]:
        try:
            head, matches = await self._marker(repo, branch, marker)
            if len(matches) != 1:
                return Inconclusive(
                    reason="commit marker missing or ambiguous", evidence=evidence("probe_commit")
                )
            row = matches[0]
            parents = list_doc(row.get("parents"))
            proof = evidence("probe_commit", commit=object_sha(row.get("sha")))
            result: CommitOutcome = (
                Committed(sha=object_sha(row.get("sha")), evidence=proof)
                if len(parents) == 1 and object_doc(parents[0]).get("sha") == expected_parent
                else Conflict(actual_head=head, evidence=proof)
            )
            return Found[CommitOutcome](value=result, evidence=proof)
        except ReadUnavailable as error:
            return Inconclusive(reason="commit read unavailable", evidence=error.evidence)
