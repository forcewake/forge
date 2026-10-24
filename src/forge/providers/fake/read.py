"""Read native in-memory snapshots and conservative effect recovery records."""

from forge_protocol import digest

from forge.domain.candidate import ValidatedCandidate
from forge.domain.publication import matches_manifest
from forge.domain.verdicts import CommitOutcome, Committed, Conflict, Sha
from forge.port_values import (
    Applied,
    BranchOutcome,
    BranchRef,
    CheckResult,
    CommentOutcome,
    CommentRef,
    Cursor,
    DispatchOutcome,
    DraftOutcome,
    FeedbackNote,
    Found,
    LabelOutcome,
    LabelState,
    LaneHandle,
    LaneStatus,
    Inconclusive,
    ProbeResult,
    ReadUnavailable,
    Refused,
    RepoRef,
    ReviewRef,
    SubjectRef,
    TaskSnapshot,
    Tree,
    TreeEntry,
)
from forge.providers.fake.state import FakeState, ref_key


class FakeReads(FakeState):
    async def get_task(self, subject: SubjectRef) -> TaskSnapshot:
        self.read("get_task", subject)
        self.repo(subject.repo)
        if ref_key(subject) not in self.tasks:
            raise ReadUnavailable("subject unavailable", "fake:missing_subject")
        return self.tasks[ref_key(subject)]

    async def find_comment(self, subject: SubjectRef | ReviewRef, marker: str) -> CommentRef | None:
        result = await self.probe_comment(subject, marker)
        if result.kind == "definitely_absent":
            return None
        if isinstance(result, Found) and isinstance(result.value, Applied):
            return result.value.value
        raise ReadUnavailable("comment visibility unknown", result.evidence)

    def comment_probe(
        self,
        method: str,
        key: str,
        subject: SubjectRef | ReviewRef,
        marker: str,
        reply_to: str | None = None,
    ) -> ProbeResult[CommentOutcome]:
        self.repo(subject.repo)
        matches = tuple(
            comment
            for comment in self.comments.values()
            if ref_key(comment.ref.subject) == ref_key(subject)
            and comment.marker == marker
            and comment.reply_to == reply_to
        )
        if len(matches) > 1:
            self.start(method, subject, marker, reply_to)
            return Inconclusive(reason="ambiguous native marker", evidence="fake:multiple_comments")
        result: ProbeResult[CommentOutcome] = self.probe(method, key, subject, marker, reply_to)
        if result.kind == "definitely_absent" and matches:
            return Found[CommentOutcome](
                value=Applied[CommentRef](value=matches[0].ref, evidence="fake:marker_found"),
                evidence="fake:native_comment",
            )
        return result

    async def probe_comment(
        self, subject: SubjectRef | ReviewRef, marker: str
    ) -> ProbeResult[CommentOutcome]:
        return self.comment_probe(
            "probe_comment", f"comment:{ref_key(subject)}:{marker}", subject, marker
        )

    async def probe_comment_update(
        self, comment: CommentRef, body_digest: str
    ) -> ProbeResult[CommentOutcome]:
        self.repo(comment.subject.repo)
        result: ProbeResult[CommentOutcome] = self.probe(
            "probe_comment_update",
            f"update:{ref_key(comment.subject)}:{comment.native_id}:{body_digest}",
            comment,
            body_digest,
        )
        native = self.comments.get(f"{ref_key(comment.subject)}:{comment.native_id}")
        if (
            native is None
            or (native.ref.kind, native.ref.native_id, ref_key(native.ref.subject))
            != (comment.kind, comment.native_id, ref_key(comment.subject))
            or digest({"body": native.displayed_body}) != body_digest
        ):
            return Inconclusive(
                reason="comment body or target differs", evidence="fake:native_comment"
            )
        if result.kind in ("definitely_absent", "found"):
            return Found[CommentOutcome](
                value=Applied[CommentRef](value=comment, evidence="fake:body_matches"),
                evidence="fake:native_comment",
            )
        return result

    async def probe_label(
        self, subject: SubjectRef, label: str, present: bool
    ) -> ProbeResult[LabelOutcome]:
        self.repo(subject.repo)
        result: ProbeResult[LabelOutcome] = self.probe(
            "probe_label", f"label:{ref_key(subject)}:{label}:{present}", subject, label, present
        )
        native = self.tasks.get(ref_key(subject))
        if (
            result.kind == "definitely_absent"
            and native is not None
            and (label in native.labels) == present
        ):
            return Found[LabelOutcome](
                value=Applied[LabelState](
                    value=LabelState(subject=subject, label=label, present=present),
                    evidence="fake:labels_match",
                ),
                evidence="fake:native_labels",
            )
        return result

    async def head(self, repo: RepoRef, branch: str) -> Sha | None:
        self.read("head", repo, branch)
        return self.repo(repo).branches.get(branch)

    async def read_tree(self, repo: RepoRef, sha: Sha, prefix: str = "") -> Tree:
        self.read("read_tree", repo, sha, prefix)
        self.read_path(prefix, allow_empty=True)
        native = self.repo(repo).commits.get(sha)
        if native is None:
            raise ReadUnavailable("snapshot unavailable", "fake:missing_commit")
        return Tree(
            repo=repo,
            sha=sha,
            entries=tuple(
                TreeEntry(
                    path=path, sha=blob.sha, mode=blob.mode, kind="blob", size=len(blob.content)
                )
                for path, blob in native.files
                if not prefix or path.startswith(prefix.rstrip("/") + "/") or path == prefix
            ),
        )

    async def read_blob(self, repo: RepoRef, sha: Sha, path: str) -> bytes | None:
        self.read("read_blob", repo, sha, path)
        self.read_path(path)
        native = self.repo(repo).commits.get(sha)
        if native is None:
            raise ReadUnavailable("snapshot unavailable", "fake:missing_commit")
        blob = dict(native.files).get(path)
        return blob.content if blob is not None else None

    async def find_commit_by_marker(self, repo: RepoRef, branch: str, marker: str) -> Sha | None:
        self.read("find_commit_by_marker", repo, branch, marker)
        native = self.repo(repo)
        for key, record in self.records.items():
            if (
                key.startswith(f"commit:{repo.identity.digest}:{branch}:{marker}:")
                and record.hidden_reads
            ):
                record.hidden_reads -= 1
                raise ReadUnavailable("commit still settling", "fake:delayed_visibility")
        matching = []
        cursor = native.branches.get(branch)
        while cursor is not None:
            commit = native.commits[cursor]
            if commit.message.endswith("\n" + marker):
                matching.append(commit.sha)
            cursor = commit.parent
        if len(matching) > 1:
            raise ReadUnavailable("ambiguous commit marker", "fake:multiple_commits")
        return matching[0] if matching else None

    async def probe_branch(
        self, repo: RepoRef, branch: str, base: Sha
    ) -> ProbeResult[BranchOutcome]:
        native = self.repo(repo)
        result: ProbeResult[BranchOutcome] = self.probe(
            "probe_branch", f"branch:{repo.identity.digest}:{branch}:{base}", repo, branch, base
        )
        if result.kind != "definitely_absent":
            return result
        actual = native.branches.get(branch)
        if branch in native.protected:
            return Found[BranchOutcome](
                value=Refused(reason="branch_not_writable", evidence="fake:protected_branch"),
                evidence="fake:native_policy",
            )
        if actual is not None:
            outcome: BranchOutcome = (
                Applied[BranchRef](
                    value=BranchRef(repo=repo, branch=branch, sha=base),
                    evidence="fake:branch_found",
                )
                if actual == base
                else Conflict(actual_head=actual, evidence="fake:branch_drift")
            )
            return Found[BranchOutcome](value=outcome, evidence="fake:native_head")
        return result

    async def probe_commit(
        self,
        repo: RepoRef,
        branch: str,
        marker: str,
        expected_parent: Sha,
        candidate: ValidatedCandidate,
    ) -> ProbeResult[CommitOutcome]:
        native = self.repo(repo)
        result: ProbeResult[CommitOutcome] = self.probe(
            "probe_commit",
            f"commit:{repo.identity.digest}:{branch}:{marker}:{expected_parent}",
            repo,
            branch,
            marker,
            expected_parent,
        )
        if (
            candidate.bundle.attempt_base_oid != expected_parent
            or candidate.changeset.branch != branch
        ):
            return Inconclusive(reason="candidate context mismatch", evidence="fake:manifest")
        if isinstance(result, Found) and isinstance(result.value, Committed):
            return (
                result
                if self._commit_matches(repo, result.value.sha, candidate)
                else Inconclusive(reason="commit manifest mismatch", evidence="fake:manifest")
            )
        if result.kind != "definitely_absent":
            return result
        head = native.branches.get(branch)
        cursor = head
        while cursor is not None:
            commit = native.commits[cursor]
            if commit.parent == expected_parent and commit.message.endswith("\n" + marker):
                if not self._commit_matches(repo, commit.sha, candidate):
                    return Inconclusive(reason="commit manifest mismatch", evidence="fake:manifest")
                return Found[CommitOutcome](
                    value=Committed(sha=commit.sha, evidence="fake:marker_found"),
                    evidence="fake:native_commit",
                )
            cursor = commit.parent
        if head != expected_parent:
            return Found[CommitOutcome](
                value=Conflict(actual_head=head, evidence="fake:branch_drift"),
                evidence="fake:native_head",
            )
        return result

    def _commit_matches(self, repo: RepoRef, sha: Sha, candidate: ValidatedCandidate) -> bool:
        native = self.repo(repo)
        commit = native.commits.get(sha)
        parent = native.commits.get(candidate.bundle.attempt_base_oid)
        return bool(
            commit is not None
            and parent is not None
            and commit.parent == parent.sha
            and matches_manifest(
                candidate,
                {path: (blob.mode, blob.sha) for path, blob in parent.files},
                {path: (blob.mode, blob.sha) for path, blob in commit.files},
            )
        )

    async def find_draft(self, repo: RepoRef, head_branch: str) -> ReviewRef | None:
        result = await self.probe_draft(repo, head_branch)
        if result.kind == "definitely_absent":
            return None
        if isinstance(result, Found) and isinstance(result.value, Applied):
            return result.value.value
        raise ReadUnavailable("draft visibility unknown", result.evidence)

    async def review_head(self, review: ReviewRef) -> Sha:
        self.read("review_head", review)
        native = self.repo(review.repo)
        draft = self.drafts.get(ref_key(review))
        if draft is None:
            raise ReadUnavailable("review unavailable", "fake:missing_review")
        return native.branches[draft.head_branch]

    async def list_feedback(
        self, review: ReviewRef, after: Cursor | None
    ) -> tuple[FeedbackNote, ...]:
        self.read("list_feedback", review, after)
        self.repo(review.repo)
        draft = self.drafts.get(ref_key(review))
        if draft is None:
            raise ReadUnavailable("review unavailable", "fake:missing_review")
        notes = tuple(draft.feedback)
        if after is None:
            return notes
        matching = [index for index, note in enumerate(notes) if note.cursor == after]
        if not matching:
            raise ReadUnavailable("cursor unavailable", "fake:unknown_cursor")
        return notes[matching[0] + 1 :]

    async def probe_draft(self, repo: RepoRef, head_branch: str) -> ProbeResult[DraftOutcome]:
        self.repo(repo)
        result: ProbeResult[DraftOutcome] = self.probe(
            "probe_draft", f"draft:{repo.identity.digest}:{head_branch}", repo, head_branch
        )
        if result.kind == "definitely_absent":
            for draft in self.drafts.values():
                if draft.ref.repo.identity == repo.identity and draft.head_branch == head_branch:
                    return Found[DraftOutcome](
                        value=Applied[ReviewRef](value=draft.ref, evidence="fake:draft_found"),
                        evidence="fake:native_draft",
                    )
        return result

    async def probe_review(self, review: ReviewRef, marker: str) -> ProbeResult[CommentOutcome]:
        return self.comment_probe(
            "probe_review", f"review:{ref_key(review)}:{marker}", review, marker
        )

    async def probe_reply(self, note: FeedbackNote, marker: str) -> ProbeResult[CommentOutcome]:
        return self.comment_probe(
            "probe_reply",
            f"reply:{ref_key(note.ref.subject)}:{note.ref.native_id}:{marker}",
            note.ref.subject,
            marker,
            note.ref.native_id,
        )

    async def find_lane(self, repo: RepoRef, correlation_id: str) -> LaneHandle | None:
        result = await self.probe_dispatch(repo, correlation_id)
        if result.kind == "definitely_absent":
            return None
        if isinstance(result, Found) and isinstance(result.value, Applied):
            return result.value.value
        raise ReadUnavailable("lane visibility unknown", result.evidence)

    async def lane_status(self, handle: LaneHandle) -> LaneStatus:
        self.read("lane_status", handle)
        self.repo(handle.repo)
        pair = self.lanes.get(f"{handle.repo.identity.digest}:{handle.native_id}:{handle.attempt}")
        if pair is None or pair[0] != handle:
            raise ReadUnavailable("lane attempt unavailable", "fake:missing_lane")
        return pair[1]

    async def checks_for(self, repo: RepoRef, sha: Sha) -> tuple[CheckResult, ...]:
        self.read("checks_for", repo, sha)
        self.repo(repo)
        return tuple(
            check for check in self.checks.get(repo.identity.digest, ()) if check.tested_sha == sha
        )

    async def probe_dispatch(
        self, repo: RepoRef, correlation_id: str
    ) -> ProbeResult[DispatchOutcome]:
        self.repo(repo)
        native = tuple(
            pair[0]
            for pair in self.lanes.values()
            if pair[0].repo.identity == repo.identity and pair[0].correlation_id == correlation_id
        )
        if len(native) > 1:
            self.start("probe_dispatch", repo, correlation_id)
            return Inconclusive(reason="ambiguous native dispatch", evidence="fake:multiple_lanes")
        result: ProbeResult[DispatchOutcome] = self.probe(
            "probe_dispatch",
            f"dispatch:{repo.identity.digest}:{correlation_id}",
            repo,
            correlation_id,
        )
        if result.kind == "definitely_absent" and native:
            return Found[DispatchOutcome](
                value=Applied[LaneHandle](value=native[0], evidence="fake:lane_found"),
                evidence="fake:native_lane",
            )
        return result
