"""Atomic in-memory writes with native identity, policy refusal and lost responses."""

from forge_protocol import LaneDispatch, digest

from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import CommitOutcome, Committed, Conflict, Sha, Unknown
from forge.port_values import (
    Applied,
    BranchOutcome,
    BranchRef,
    CommentOutcome,
    CommentRef,
    DispatchOutcome,
    DraftOutcome,
    FeedbackNote,
    LabelOutcome,
    LabelState,
    LaneHandle,
    LaneStatus,
    Refused,
    RepoRef,
    ReviewBody,
    ReviewRef,
    SubjectRef,
)
from forge.providers.fake.state import Blob, Draft, FakeState, NativeComment, commit_value, ref_key


class FakeWrites(FakeState):
    def refused_scope(self, repo: RepoRef) -> Refused | None:
        if (
            repo.identity.provider != self.provider
            or repo.identity.connection_id != self.connection_id
            or repo.identity.digest not in self.repositories
        ):
            return Refused(reason="repository_scope", evidence="fake:scope_refused")
        return None

    def refused_branch(self, repo: RepoRef, branch: str) -> Refused | None:
        native = self.repo(repo)
        if (
            branch in native.protected
            or not branch
            or branch.startswith("/")
            or branch.endswith(("/", ".", ".lock"))
            or ".." in branch
            or "@{" in branch
            or any(ord(c) < 33 or c in "~^:?*[\\" for c in branch)
            or any(p in ("", ".") or p.startswith(".") for p in branch.split("/"))
        ):
            return Refused(
                reason="branch_not_writable", evidence="fake:protected_or_invalid_branch"
            )
        return None

    async def post_comment(self, subject: SubjectRef, body: str, marker: str) -> CommentOutcome:
        fault = self.start("post_comment", subject, body, marker)
        if refused := self.refused_scope(subject.repo):
            return refused
        if ref_key(subject) not in self.tasks:
            return Refused(reason="missing_subject", evidence="fake:missing_subject")
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        ref = CommentRef(subject=subject, native_id=self.next_id())
        self.comments[f"{ref_key(subject)}:{ref.native_id}"] = NativeComment(ref, body, marker)
        return self.finish(
            f"comment:{ref_key(subject)}:{marker}",
            Applied[CommentRef](value=ref, evidence="fake:comment_created"),
            fault,
        )

    async def update_comment(self, comment: CommentRef, body: str) -> CommentOutcome:
        fault = self.start("update_comment", comment, body)
        if refused := self.refused_scope(comment.subject.repo):
            return refused
        native = self.comments.get(f"{ref_key(comment.subject)}:{comment.native_id}")
        if native is None:
            return Refused(reason="missing_comment", evidence="fake:missing_comment")
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        native.body = body
        body_digest = digest({"body": body})
        return self.finish(
            f"update:{ref_key(comment.subject)}:{comment.native_id}:{body_digest}",
            Applied[CommentRef](value=comment, evidence="fake:comment_updated"),
            fault,
        )

    async def set_label(self, subject: SubjectRef, label: str, present: bool) -> LabelOutcome:
        fault = self.start("set_label", subject, label, present)
        if refused := self.refused_scope(subject.repo):
            return refused
        task = self.tasks.get(ref_key(subject))
        if task is None:
            return Refused(reason="missing_subject", evidence="fake:missing_subject")
        outcome = Applied[LabelState](
            value=LabelState(subject=subject, label=label, present=present),
            evidence="fake:label_updated",
        )
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        labels = set(task.labels)
        labels.add(label) if present else labels.discard(label)
        self.tasks[ref_key(subject)] = task.model_copy(update={"labels": tuple(sorted(labels))})
        return self.finish(f"label:{ref_key(subject)}:{label}:{present}", outcome, fault)

    async def ensure_branch(self, repo: RepoRef, branch: str, base: Sha) -> BranchOutcome:
        fault = self.start("ensure_branch", repo, branch, base)
        if refused := self.refused_scope(repo) or self.refused_branch(repo, branch):
            return refused
        native = self.repo(repo)
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        actual = native.branches.get(branch)
        if actual is not None and actual != base:
            return Conflict(actual_head=actual, evidence="fake:branch_drift")
        if base not in native.commits:
            return Refused(reason="missing_base", evidence="fake:missing_base")
        if fault.conflict:
            return Conflict(actual_head=actual, evidence="fake:injected_conflict")
        native.branches[branch] = base
        return self.finish(
            f"branch:{repo.identity.digest}:{branch}:{base}",
            Applied[BranchRef](
                value=BranchRef(repo=repo, branch=branch, sha=base), evidence="fake:branch_ensured"
            ),
            fault,
        )

    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: Sha, marker: str
    ) -> CommitOutcome:
        fault = self.start("commit", repo, candidate, expected_head, marker)
        branch = candidate.changeset.branch
        if refused := self.refused_scope(repo) or self.refused_branch(repo, branch):
            # CommitOutcome intentionally has no refusal variant: no native commit observed.
            return Unknown(evidence=refused.evidence)
        native = self.repo(repo)
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        actual = native.branches.get(branch)
        if fault.conflict and actual is not None:
            competing = commit_value(dict(native.commits[actual].files), actual, "competing commit")
            native.commits[competing.sha] = competing
            native.branches[branch] = competing.sha
            actual = competing.sha
        if actual != expected_head:
            return Conflict(actual_head=actual, evidence="fake:expected_head_mismatch")
        if (
            candidate.bundle.attempt_base_oid != expected_head
            or candidate.changeset.attempt_base_oid != expected_head
        ):
            return Unknown(evidence="fake:attempt_base_mismatch")
        files = dict(native.commits[actual].files)
        for entry in candidate.manifest:
            if entry.operation == "delete":
                if entry.path not in files:
                    return Unknown(evidence="fake:delete_missing_file")
                del files[entry.path]
            else:
                if entry.content_bytes is None or entry.mode not in ("100644", "100755", "120000"):
                    return Unknown(evidence="fake:unsupported_publication")
                if (entry.operation == "create") == (entry.path in files):
                    return Unknown(evidence="fake:file_existence_conflict")
                files[entry.path] = Blob(
                    entry.content_bytes,
                    "100755"
                    if entry.mode == "100755"
                    else "120000"
                    if entry.mode == "120000"
                    else "100644",
                )
        commit = commit_value(files, actual, candidate.changeset.commit_message + "\n" + marker)
        native.commits[commit.sha] = commit
        native.branches[branch] = commit.sha
        return self.finish(
            f"commit:{repo.identity.digest}:{branch}:{marker}:{expected_head}",
            Committed(sha=commit.sha, evidence="fake:native_cas"),
            fault,
        )

    async def ensure_draft(
        self, repo: RepoRef, head_branch: str, title: str, body: str
    ) -> DraftOutcome:
        fault = self.start("ensure_draft", repo, head_branch, title, body)
        if refused := self.refused_scope(repo) or self.refused_branch(repo, head_branch):
            return refused
        if head_branch not in self.repo(repo).branches:
            return Refused(reason="missing_branch", evidence="fake:missing_branch")
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        existing = next(
            (
                draft
                for draft in self.drafts.values()
                if draft.ref.repo.identity == repo.identity and draft.head_branch == head_branch
            ),
            None,
        )
        ref = existing.ref if existing else ReviewRef(repo=repo, number=int(self.next_id()))
        if existing is None:
            self.drafts[ref_key(ref)] = Draft(ref, head_branch, title, body)
        return self.finish(
            f"draft:{repo.identity.digest}:{head_branch}",
            Applied[ReviewRef](value=ref, evidence="fake:draft_ensured"),
            fault,
        )

    async def post_review(self, review: ReviewRef, body: ReviewBody, marker: str) -> CommentOutcome:
        fault = self.start("post_review", review, body, marker)
        if refused := self.refused_scope(review.repo):
            return refused
        draft = self.drafts.get(ref_key(review))
        if draft is None:
            return Refused(reason="missing_review", evidence="fake:missing_review")
        if self.repo(review.repo).branches[draft.head_branch] != body.candidate_sha:
            return Refused(reason="stale_candidate", evidence="fake:review_head_mismatch")
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        ref = CommentRef(subject=review, native_id=self.next_id())
        self.comments[f"{ref_key(review)}:{ref.native_id}"] = NativeComment(ref, body.body, marker)
        note = FeedbackNote(
            ref=ref,
            author=self.bot_identity(),
            body=body.body,
            cursor=ref.native_id,
            created_at=self.now,
        )
        draft.feedback.append(note)
        return self.finish(
            f"review:{ref_key(review)}:{marker}",
            Applied[CommentRef](value=ref, evidence="fake:review_posted"),
            fault,
        )

    async def reply(self, note: FeedbackNote, body: str, marker: str) -> CommentOutcome:
        fault = self.start("reply", note, body, marker)
        if refused := self.refused_scope(note.ref.subject.repo):
            return refused
        if f"{ref_key(note.ref.subject)}:{note.ref.native_id}" not in self.comments:
            return Refused(reason="missing_note", evidence="fake:missing_note")
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        ref = CommentRef(subject=note.ref.subject, native_id=self.next_id())
        self.comments[f"{ref_key(ref.subject)}:{ref.native_id}"] = NativeComment(
            ref, body, marker, reply_to=note.ref.native_id
        )
        if isinstance(ref.subject, ReviewRef):
            self.drafts[ref_key(ref.subject)].feedback.append(
                FeedbackNote(
                    ref=ref,
                    author=self.bot_identity(),
                    body=body,
                    cursor=ref.native_id,
                    created_at=self.now,
                )
            )
        return self.finish(
            f"reply:{ref_key(note.ref.subject)}:{note.ref.native_id}:{marker}",
            Applied[CommentRef](value=ref, evidence="fake:reply_posted"),
            fault,
        )

    async def dispatch_lane(self, repo: RepoRef, dispatch: LaneDispatch) -> DispatchOutcome:
        fault = self.start("dispatch_lane", repo, dispatch)
        if refused := self.refused_scope(repo):
            return refused
        native = self.repo(repo)
        if dispatch.provider != repo.identity.provider or dispatch.repository not in (
            repo.full_name,
            repo.identity.repository_id,
        ):
            return Refused(reason="dispatch_scope", evidence="fake:dispatch_scope_refused")
        if dispatch.ref not in native.branches and dispatch.ref not in native.commits:
            return Refused(reason="missing_dispatch_ref", evidence="fake:missing_dispatch_ref")
        if fault.http_5xx:
            return Unknown(evidence="fake:http_503")
        if dispatch.nonce is None:
            return Refused(reason="missing_correlation_nonce", evidence="fake:missing_nonce")
        correlation = dispatch.nonce
        handle = LaneHandle(
            repo=repo, native_id=self.next_id(), attempt=1, correlation_id=correlation
        )
        lane_key = f"{repo.identity.digest}:{handle.native_id}:{handle.attempt}"
        self.lanes[lane_key] = (
            handle,
            LaneStatus(
                handle=handle, status="queued", observed_at=self.now, evidence="fake:queued"
            ),
        )
        return self.finish(
            f"dispatch:{repo.identity.digest}:{correlation}",
            Applied[LaneHandle](value=handle, evidence="fake:lane_dispatched"),
            fault,
        )
