"""The NATIVE-SHAPED remote — the in-process GitLab/GitHub-shaped
reference remote with provider-realistic duplicate behavior (R36-18
#277 → R37-19 #300).

**This module is REFERENCE material** (label:
:data:`forge.adaptive.reference.REFERENCE_PACKAGE_LABEL`): a strict
in-process object chosen to behave like a provider, extracted from
``saga_durable.py`` so the durable publication contracts stay
reference-free. It is never selected by a runtime default — callers
hand it to :class:`~forge.adaptive.saga_durable.DurablePublicationEntry`
explicitly in tests and qualification runs, or hand it the REAL native
adapters (``forge.adaptive.saga_native``) instead. The
``PublicationProvider`` adapter methods on top keep the native
semantics: :meth:`NativeShapedRemote.commit` refuses (4xx) before any
effect when the branch is protected or the pinned head moved; a lost
response (:attr:`NativeShapedRemote.lose_commit_response`) lands the
effect and kills the answer (:class:`TimeoutError`);
:meth:`NativeShapedRemote.head_carries_marker` is NATIVE correlation —
it lists the branch's commits and scans messages.

The native surface (what a provider actually offers):

- :meth:`NativeShapedRemote.create_commit` appends on the branch's
  CURRENT head — there is NO dedup by content or marker; a repeated
  call creates a SECOND commit. Safety comes from the caller probing
  FIRST, never from the provider remembering.
- :meth:`NativeShapedRemote.update_ref` carries an EXPECTED-HEAD
  precondition — a moved head is a 422-shaped
  :class:`~forge.adaptive.publication_saga.ProviderRejectedError`
  (GitHub's update-refs contract; :meth:`NativeShapedRemote.pin_expected_head`
  gives the publication the same CAS on its commits).
- :meth:`NativeShapedRemote.create_merge_request` is idempotent ONLY by
  the provider-native key ``(repository, source branch)`` — a second
  creation for the same source branch returns the SAME MR (GitLab
  refuses duplicates; so does GitHub).
- protected branches refuse direct commits; publication rides the MR
  flow.
- every effect lands in :attr:`NativeShapedRemote.journal` with its
  native identity; every commit records its author (``forge-bot`` vs
  ``human``). No merge/force-push/delete exists on the writer surface
  at all — the only human merge is
  :meth:`NativeShapedRemote.human_merge`, a WORLD knob tests use,
  never a publication call.
"""

from __future__ import annotations

import hashlib
from typing import Any

from forge.adaptive.publication_saga import (
    ProviderRejectedError,
    ProviderUnavailableError,
)
from forge.adaptive.saga_durable import NativeCommit

__all__ = ["NativeCommit", "NativeShapedRemote"]


class NativeShapedRemote:
    """A GitLab/GitHub-shaped remote whose DUPLICATE BEHAVIOR IS REALISTIC.

    See the module docstring for the native surface this object models;
    the class body is unchanged from its pre-extraction home (the compat
    re-export on ``saga_durable`` keeps the old import path working).
    """

    def __init__(self) -> None:
        self._history: dict[tuple[str, str], list[NativeCommit]] = {}
        self._merge_requests: dict[tuple[str, str], dict[str, Any]] = {}
        self._protected: set[tuple[str, str]] = set()
        self._pins: dict[tuple[str, str], str] = {}
        self._counter = 0
        self._mr_counter = 0
        #: Every effect with its native identity, oldest first.
        self.journal: list[dict[str, Any]] = []
        self.commit_calls: dict[str, int] = {}
        self.review_calls: dict[str, int] = {}
        # -- the injected windows (test knobs) --------------------------------
        self.unavailable: set[str] = set()
        self.lose_commit_response: set[str] = set()
        self.refuse_commits: set[str] = set()

    # -- world knobs ----------------------------------------------------------

    def seed(self, repository_id: str, branch: str, base_oid: str) -> None:
        self._history[(repository_id, branch)] = [
            NativeCommit(sha=base_oid, parent="", message="base", author="provider")
        ]

    def protect_branch(self, repository_id: str, branch: str) -> None:
        self._protected.add((repository_id, branch))

    def pin_expected_head(self, repository_id: str, branch: str, expected_head: str) -> None:
        """The publication's CAS pin (the writer's ``expected_head`` drift guard)."""
        self._pins[(repository_id, branch)] = expected_head

    def human_commit(self, repository_id: str, branch: str, message: str = "human edit") -> str:
        """A person pushed: appended, never overwritten, journaled as human."""
        return self.create_commit(repository_id, branch, message, author="human")

    def human_merge(self, repository_id: str, source_branch: str) -> None:
        """A person merged the MR — a WORLD event, never a publication call."""
        request = self._merge_requests.get((repository_id, source_branch))
        if request is None:
            raise KeyError(f"no merge request for {repository_id}/{source_branch}")
        request["merged"] = True
        self.journal.append(
            {
                "op": "merge_merge_request",
                "repository_id": repository_id,
                "source_branch": source_branch,
                "iid": request["iid"],
                "author": "human",
            }
        )

    # -- reads ------------------------------------------------------------------

    def list_commits(self, repository_id: str, branch: str) -> tuple[NativeCommit, ...]:
        """The branch's commits, oldest first — the listing providers offer."""
        return tuple(self._history.setdefault((repository_id, branch), []))

    def branch_history(self, repository_id: str, branch: str) -> tuple[str, ...]:
        """The branch's commit shas, oldest first (the prefix-check view)."""
        return tuple(commit.sha for commit in self.list_commits(repository_id, branch))

    def commits_carrying(
        self, repository_id: str, branch: str, marker: str
    ) -> tuple[NativeCommit, ...]:
        """Every DISTINCT commit whose message carries the marker — the
        duplicate-effect view (two entries = a duplicated logical effect)."""
        return tuple(
            commit
            for commit in self.list_commits(repository_id, branch)
            if marker in commit.message
        )

    def merge_request_creates(self, repository_id: str) -> int:
        """How many DISTINCT merge requests were created (native idempotence)."""
        return sum(
            1
            for entry in self.journal
            if entry["op"] == "create_merge_request" and entry["repository_id"] == repository_id
        )

    def effects_for(self, repository_id: str) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.journal if entry["repository_id"] == repository_id]

    def destructive_operations(self) -> list[str]:
        """Always empty — the surface cannot express a force-push or delete."""
        return []

    def _head(self, repository_id: str, branch: str) -> str:
        history = self._history.setdefault((repository_id, branch), [])
        return history[-1].sha if history else ""

    # -- the native write surface ------------------------------------------------

    def create_commit(
        self,
        repository_id: str,
        branch: str,
        message: str,
        *,
        author: str,
        expected_head: str | None = None,
    ) -> str:
        """Append a commit on the CURRENT head — no dedup, no CAS (the
        provider's commits-API shape). Returns the new commit's sha.

        ``expected_head`` (the effect-interface precondition, #295) is the
        one-shot spelling of :meth:`pin_expected_head`: when given, it
        pins the drift guard the ADAPTER methods (:meth:`commit` /
        :meth:`update_ref`) enforce — the native append itself stays
        precondition-free, exactly like the providers' commits APIs."""
        if expected_head is not None:
            self.pin_expected_head(repository_id, branch, expected_head)
        self._counter += 1
        parent = self._head(repository_id, branch)
        sha = hashlib.sha256(
            f"{repository_id}|{branch}|{parent}|{message}|{author}|{self._counter}".encode()
        ).hexdigest()[:40]
        self._history[(repository_id, branch)].append(
            NativeCommit(sha=sha, parent=parent, message=message, author=author)
        )
        self.journal.append(
            {
                "op": "create_commit",
                "repository_id": repository_id,
                "branch": branch,
                "sha": sha,
                "parent": parent,
                "author": author,
            }
        )
        return sha

    def update_ref(self, repository_id: str, branch: str, sha: str, *, expected_head: str) -> None:
        """The refs API with its CAS: a moved head is a 422 refusal."""
        if self._head(repository_id, branch) != expected_head:
            raise ProviderRejectedError(
                f"422 update_ref {repository_id}/{branch}: expected head {expected_head},"
                f" live head {self._head(repository_id, branch)}"
            )
        history = self._history[(repository_id, branch)]
        if not history or history[-1].sha != sha:
            raise ProviderRejectedError(
                f"422 update_ref {repository_id}/{branch}: {sha} is not the live head"
            )

    def create_merge_request(
        self, repository_id: str, source_branch: str, *, target_branch: str, title: str
    ) -> str:
        """Idempotent ONLY by the provider-native key (repository, source
        branch): a repeat returns the SAME MR — no second review is
        created, exactly the native duplicate behavior."""
        key = (repository_id, source_branch)
        existing = self._merge_requests.get(key)
        if existing is not None:
            return str(existing["url"])
        self._mr_counter += 1
        iid = self._mr_counter
        url = f"https://native.test/{repository_id}/merge_requests/{iid}"
        self._merge_requests[key] = {
            "iid": iid,
            "url": url,
            "title": title,
            "target_branch": target_branch,
            "merged": False,
        }
        self.journal.append(
            {
                "op": "create_merge_request",
                "repository_id": repository_id,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "iid": iid,
                "url": url,
            }
        )
        return url

    # -- the PublicationProvider adapter --------------------------------------

    async def remote_head(self, repository_id: str, branch: str) -> str:
        if repository_id in self.unavailable:
            raise ProviderUnavailableError(f"{repository_id}: surface unreadable")
        return self._head(repository_id, branch)

    async def head_carries_marker(self, repository_id: str, branch: str, marker: str) -> bool:
        """NATIVE correlation: list the branch's commits, scan the messages."""
        if repository_id in self.unavailable:
            raise ProviderUnavailableError(f"{repository_id}: surface unreadable")
        return bool(self.commits_carrying(repository_id, branch, marker))

    async def commit(self, repository_id: str, branch: str, marker: str) -> str:
        self.commit_calls[repository_id] = self.commit_calls.get(repository_id, 0) + 1
        if repository_id in self.unavailable:
            raise ProviderUnavailableError(f"{repository_id}: surface unreadable")
        if repository_id in self.refuse_commits:
            raise ProviderRejectedError(f"{repository_id}: the provider refused the commit")
        if (repository_id, branch) in self._protected:
            raise ProviderRejectedError(
                f"{repository_id}/{branch} is protected — direct commits are refused,"
                " publication rides the merge-request flow"
            )
        pin = self._pins.get((repository_id, branch))
        live = self._head(repository_id, branch)
        if pin is not None and live != pin:
            raise ProviderRejectedError(
                f"422 commit {repository_id}/{branch}: expected head {pin}, live head {live}"
                " — the branch moved under the publication"
            )
        sha = self.create_commit(
            repository_id,
            branch,
            f"forge: publish candidate\n\n({marker})",
            author="forge-bot",
        )
        if repository_id in self.lose_commit_response:
            raise TimeoutError(f"{repository_id}: response lost after the effect landed")
        return sha

    async def open_review(self, repository_id: str, branch: str, marker: str) -> str:
        self.review_calls[repository_id] = self.review_calls.get(repository_id, 0) + 1
        if repository_id in self.unavailable:
            raise ProviderUnavailableError(f"{repository_id}: surface unreadable")
        return self.create_merge_request(
            repository_id,
            branch,
            target_branch="main",
            title=f"forge publication ({marker})",
        )
