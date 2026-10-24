"""Immutable GitLab repository snapshots and conservative publication probes."""

import base64
import binascii
import hashlib
from typing import Literal, cast
from urllib.parse import quote

from pydantic import ValidationError

from forge.domain.candidate import ValidatedCandidate
from forge.domain.publication import matches_manifest
from forge.domain.verdicts import CommitOutcome, Committed, Conflict
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
from forge.providers.gitlab.client import API
from forge.providers.gitlab.transport import Failure
from forge.providers.gitlab.values import (
    MAX_BYTES,
    array,
    branch as branch_path,
    evidence,
    marker,
    obj,
    path as repo_path,
    sha as object_sha,
    text,
    unavailable,
)


class GitLabRepo:
    def __init__(self, api: API, *, max_blob_bytes: int = MAX_BYTES) -> None:
        if not 0 < max_blob_bytes <= MAX_BYTES:
            raise ValueError("blob limit must be bounded")
        self.api, self.max_blob_bytes = api, max_blob_bytes

    async def head(self, repo: RepoRef, branch: str) -> str | None:
        try:
            reply = await self.api.request(
                repo, "GET", "repository/branches/" + branch_path(branch)
            )
        except Failure as error:
            if error.reason == "resource_not_found":
                return None
            raise ReadUnavailable("branch_unavailable", error.evidence) from None
        except ValueError:
            raise unavailable("invalid_branch_resource") from None
        row = obj(reply.document)
        if row.get("name") != branch:
            raise unavailable("branch_identity_mismatch")
        return object_sha(obj(row.get("commit")).get("id"))

    async def read_tree(self, repo: RepoRef, sha: str, prefix: str = "") -> Tree:
        commit = object_sha(sha)
        confined = repo_path(prefix) if prefix else ""
        row = obj((await self.api.read(repo, "repository/commits/" + commit)).document)
        if row.get("id") != commit:
            raise unavailable("commit_identity_mismatch")
        rows = await self.api.pages(
            repo, "repository/tree", params={"ref": commit, "recursive": "true"}, max_pages=1000
        )
        if len(rows) > 100_000:
            raise unavailable("tree_too_large")
        try:
            entries = tuple(self._entry(row) for row in rows)
            complete = Tree(repo=repo, sha=commit, entries=entries)
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
            raise unavailable("invalid_tree") from None

    @staticmethod
    def _entry(row: dict[str, object]) -> TreeEntry:
        mode, kind = row.get("mode"), row.get("type")
        expected = {
            "100644": "blob",
            "100755": "blob",
            "120000": "blob",
            "040000": "tree",
            "160000": "commit",
        }
        if not isinstance(mode, str) or expected.get(mode) != kind:
            raise unavailable("tree_mode_type_mismatch")
        return TreeEntry(
            path=repo_path(row.get("path")),
            sha=object_sha(row.get("id")),
            mode=cast(Literal["100644", "100755", "120000", "040000", "160000"], mode),
            kind=cast(Literal["blob", "tree", "commit"], kind),
        )

    async def read_blob(self, repo: RepoRef, sha: str, path: str) -> bytes | None:
        confined = repo_path(path)
        tree = await self.read_tree(repo, sha)
        entry = next((value for value in tree.entries if value.path == confined), None)
        if entry is None:
            try:
                await self.api.request(
                    repo,
                    "GET",
                    "repository/files/" + quote(confined, safe=""),
                    params={"ref": sha},
                )
            except Failure as error:
                if error.reason == "resource_not_found":
                    return None
                raise ReadUnavailable("blob_unavailable", error.evidence) from None
            except ValueError:
                raise unavailable("unsupported_blob_resource") from None
            raise unavailable("tree_file_disagreement")
        if entry.kind != "blob":
            raise unavailable("nonblob_read_refused")
        row = obj((await self.api.read(repo, "repository/blobs/" + entry.sha)).document)
        content, size = row.get("content"), row.get("size")
        if (
            row.get("sha") != entry.sha
            or row.get("encoding") != "base64"
            or type(size) is not int
            or not 0 <= size <= self.max_blob_bytes
            or not isinstance(content, str)
        ):
            raise unavailable("invalid_blob_document")
        try:
            body = base64.b64decode(content.replace("\n", "").replace("\r", ""), validate=True)
        except (binascii.Error, ValueError):
            raise unavailable("invalid_blob_encoding") from None
        hashing = hashlib.sha1 if len(entry.sha) == 40 else hashlib.sha256
        actual = hashing(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
        if len(body) != size or actual != entry.sha:
            raise unavailable("blob_integrity_mismatch")
        return body

    async def _markers(
        self, repo: RepoRef, branch_name: str, value: str
    ) -> tuple[str | None, list[dict[str, object]]]:
        head = await self.head(repo, branch_name)
        if head is None:
            return None, []
        rows = await self.api.pages(repo, "repository/commits", params={"ref_name": head})
        seen: set[str] = set()
        matches = []
        for row in rows:
            ident = object_sha(row.get("id"))
            if ident in seen:
                raise unavailable("duplicate_commit_page")
            seen.add(ident)
            if marker(text(row.get("message"), empty=True), value):
                matches.append(row)
        if not rows or rows[0].get("id") != head:
            raise unavailable("history_head_mismatch")
        return head, matches

    async def find_commit_by_marker(self, repo: RepoRef, branch: str, marker: str) -> str | None:
        _, matches = await self._markers(repo, branch, marker)
        if len(matches) != 1:
            raise unavailable("commit_marker_visibility_unknown")
        return object_sha(matches[0].get("id"))

    async def probe_branch(
        self, repo: RepoRef, branch: str, base: str
    ) -> ProbeResult[BranchOutcome]:
        try:
            head = await self.head(repo, branch)
            proof = evidence("probe_branch", head=head or "")
            if head is None:
                return Inconclusive(
                    reason="branch visibility is not proof of absence", evidence=proof
                )
            outcome: BranchOutcome = (
                Applied[BranchRef](
                    value=BranchRef(repo=repo, branch=branch, sha=head), evidence=proof
                )
                if head == base
                else Conflict(actual_head=head, evidence=proof)
            )
            return Found[BranchOutcome](value=outcome, evidence=proof)
        except ReadUnavailable as error:
            return Inconclusive(reason="branch read unavailable", evidence=error.evidence)

    async def probe_commit(
        self,
        repo: RepoRef,
        branch: str,
        marker: str,
        expected_parent: str,
        candidate: ValidatedCandidate,
    ) -> ProbeResult[CommitOutcome]:
        try:
            if (
                candidate.bundle.attempt_base_oid != expected_parent
                or candidate.changeset.branch != branch
            ):
                return Inconclusive(
                    reason="commit probe context differs from validated candidate",
                    evidence=evidence("probe_commit"),
                )
            head, matches = await self._markers(repo, branch, marker)
            if len(matches) != 1:
                return Inconclusive(
                    reason="commit marker missing or ambiguous", evidence=evidence("probe_commit")
                )
            row = matches[0]
            parents = array(row.get("parent_ids"))
            proof = evidence("probe_commit", commit=object_sha(row.get("id")))
            if parents == [expected_parent]:
                before = await self.read_tree(repo, expected_parent)
                after = await self.read_tree(repo, object_sha(row.get("id")))
                leaves_before = {
                    entry.path: (entry.mode, entry.sha)
                    for entry in before.entries
                    if entry.kind != "tree"
                }
                leaves_after = {
                    entry.path: (entry.mode, entry.sha)
                    for entry in after.entries
                    if entry.kind != "tree"
                }
                if not matches_manifest(candidate, leaves_before, leaves_after):
                    return Inconclusive(
                        reason="commit tree differs from validated manifest", evidence=proof
                    )
            outcome: CommitOutcome = (
                Committed(sha=object_sha(row.get("id")), evidence=proof)
                if parents == [expected_parent]
                else Conflict(actual_head=head, evidence=proof)
            )
            return Found[CommitOutcome](value=outcome, evidence=proof)
        except ReadUnavailable as error:
            return Inconclusive(reason="commit read unavailable", evidence=error.evidence)
