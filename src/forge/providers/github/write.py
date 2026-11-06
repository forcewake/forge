"""Effects-only REST mutations with bot ownership and branch safety preflights."""

from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit

from forge_protocol import LaneDispatch

from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import CommitOutcome, Conflict, Unknown
from forge.port_values import (
    Actor,
    Applied,
    BranchOutcome,
    BranchRef,
    CommentOutcome,
    CommentRef,
    DispatchOutcome,
    DraftOutcome,
    FeedbackNote,
    Found,
    LabelOutcome,
    LabelState,
    Refused,
    RepoRef,
    ReviewBody,
    ReviewRef,
    SubjectRef,
)
from forge.providers.github.ci import GitHubCI
from forge.providers.github.client import API, Reply
from forge.providers.github.repo import (
    GitHubRepo,
    branch_path,
    comment_bound,
    comment_resource,
    evidence,
    list_doc,
    malformed,
    marker_line,
    native_id,
    object_doc,
    object_sha,
    request,
    text,
)
from forge.providers.github.review import GitHubReview, feedback_kind, reply_marker
from forge.providers.github.subject import GitHubSubject
from forge.providers.github.transport import TransportFailure
from forge.port_values import ReadUnavailable


class PolicyRefused(ReadUnavailable):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason, evidence("write_policy", reason=reason))


class Committer(Protocol):
    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: str, marker: str
    ) -> CommitOutcome: ...


def marked(body: str, marker: str) -> str:
    text(body, empty=True)
    marker_line("", marker)
    if marker in body:
        raise malformed("caller_body_already_contains_marker")
    return body + "\n\n" + marker


class GitHubWrites:
    def __init__(
        self,
        api: API,
        *,
        bot: Actor,
        repositories: GitHubRepo,
        reviews: GitHubReview,
        ci: GitHubCI,
        committer: Committer,
        requirements_url: str,
        requirements_sha256: str,
    ) -> None:
        parsed = urlsplit(requirements_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
            or len(requirements_sha256) != 64
            or any(c not in "0123456789abcdef" for c in requirements_sha256)
        ):
            raise ValueError("trusted immutable lane requirement URL and SHA256 are required")
        self.api, self.subjects = api, GitHubSubject(api, bot=bot)
        self.repositories, self.reviews, self.ci, self.committer = (
            repositories,
            reviews,
            ci,
            committer,
        )
        self.requirements_url, self.requirements_sha256 = requirements_url, requirements_sha256

    async def writable(self, repo: RepoRef, branch: str) -> str:
        encoded = branch_path(branch)
        metadata = object_doc((await self.api.metadata(repo)).document)
        default = text(metadata.get("default_branch"))
        if metadata.get("archived") is True or metadata.get("disabled") is True:
            raise PolicyRefused("repository_not_writable")
        if metadata.get("archived") is not False or metadata.get("disabled") is not False:
            raise malformed("repository_write_policy_unavailable")
        if branch == default or not branch.startswith("forge/"):
            raise PolicyRefused("default_or_nonfactory_branch_refused")
        # Effective rules apply to prospective branches too; a missing/forbidden rules read is not permission.
        rules = list_doc((await request(self.api, repo, "rules/branches/" + encoded)).document)
        if rules:
            raise PolicyRefused("protected_branch_refused")
        try:
            row = object_doc((await self.api.request(repo, "GET", "branches/" + encoded)).document)
        except TransportFailure as error:
            if error.reason != "resource_not_found":
                raise ReadUnavailable("branch_policy_unavailable", error.evidence) from None
        else:
            if row.get("name") != branch or row.get("protected") is not False:
                raise PolicyRefused("protected_or_wrong_branch_refused")
        return encoded

    async def _mutate(
        self, repo: RepoRef, method: str, resource: str, body: dict[str, Any] | None = None
    ) -> Reply:
        return await self.api.request(repo, method, resource, body=body, write=True)

    def _comment(
        self,
        reply: Reply,
        subject: SubjectRef | ReviewRef,
        body: str,
        kind: Literal["issue_comment", "review_comment", "review"] = "issue_comment",
    ) -> Applied[CommentRef]:
        row = object_doc(reply.document)
        if not self.subjects._owned(row) or row.get("body") != body:
            raise malformed("write_comment_response_mismatch")
        ref = CommentRef(subject=subject, native_id=native_id(row.get("id")), kind=kind)
        if not comment_bound(ref, row):
            raise malformed("write_comment_target_mismatch")
        proof = evidence("comment_write", reply.request_id)
        return Applied[CommentRef](
            value=CommentRef(subject=subject, native_id=native_id(row.get("id")), kind=kind),
            evidence=proof,
        )

    @staticmethod
    def _unknown(error: TransportFailure | ReadUnavailable | ValueError) -> Unknown:
        return Unknown(
            evidence=error.evidence
            if isinstance(error, (TransportFailure, ReadUnavailable))
            else evidence("write", reason="invalid_native_response")
        )

    @classmethod
    def _failure(cls, error: TransportFailure | ReadUnavailable | ValueError) -> Unknown | Refused:
        if isinstance(error, PolicyRefused):
            return Refused(reason=error.reason, evidence=error.evidence)
        return cls._unknown(error)

    async def post_comment(
        self, subject: SubjectRef | ReviewRef, body: str, marker: str
    ) -> CommentOutcome:
        try:
            content = marked(body, marker)
            if isinstance(subject, SubjectRef):
                await self.subjects.get_task(subject)
            else:
                await self.reviews.document(subject)
            reply = await self._mutate(
                subject.repo, "POST", f"issues/{subject.number}/comments", {"body": content}
            )
            return self._comment(reply, subject, content)
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._unknown(error)

    async def update_comment(self, comment: CommentRef, body: str) -> CommentOutcome:
        try:
            text(body, empty=True)
            resource = comment_resource(comment)
            row = object_doc((await request(self.api, comment.subject.repo, resource)).document)
            if not comment_bound(comment, row) or not self.subjects._owned(row):
                return Refused(
                    reason="comment_not_bot_owned_or_wrong_subject",
                    evidence=evidence("update_comment"),
                )
            reply = await self._mutate(
                comment.subject.repo,
                "PUT" if comment.kind == "review" else "PATCH",
                resource,
                {"body": body},
            )
            return self._comment(reply, comment.subject, body, comment.kind)
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._unknown(error)

    async def set_label(self, subject: SubjectRef, label: str, present: bool) -> LabelOutcome:
        try:
            text(label)
            current = await self.subjects.get_task(subject)
            if (label in current.labels) != present:
                resource = f"issues/{subject.number}/labels"
                reply = await self._mutate(
                    subject.repo,
                    "POST" if present else "DELETE",
                    resource if present else resource + "/" + quote(label, safe=""),
                    {"labels": [label]} if present else None,
                )
                labels = [text(object_doc(item).get("name")) for item in list_doc(reply.document)]
                if (label in labels) != present:
                    raise malformed("label_write_response_mismatch")
            proof = evidence("set_label")
            return Applied[LabelState](
                value=LabelState(subject=subject, label=label, present=present), evidence=proof
            )
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._unknown(error)

    async def ensure_branch(self, repo: RepoRef, branch: str, base: str) -> BranchOutcome:
        try:
            await self.writable(repo, branch)
            base = object_sha(base)
            current = await self.repositories.head(repo, branch)
            if current is not None:
                return (
                    Applied[BranchRef](
                        value=BranchRef(repo=repo, branch=branch, sha=base),
                        evidence=evidence("branch_exists"),
                    )
                    if current == base
                    else Conflict(actual_head=current, evidence=evidence("branch_drift"))
                )
            reply = await self._mutate(
                repo, "POST", "git/refs", {"ref": "refs/heads/" + branch, "sha": base}
            )
            row = object_doc(reply.document)
            if (
                row.get("ref") != "refs/heads/" + branch
                or object_doc(row.get("object")).get("sha") != base
                or object_doc(row.get("object")).get("type") != "commit"
            ):
                raise malformed("branch_write_response_mismatch")
            return Applied[BranchRef](
                value=BranchRef(repo=repo, branch=branch, sha=base),
                evidence=evidence("branch_created", reply.request_id),
            )
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: str, marker: str
    ) -> CommitOutcome:
        try:
            await self.writable(repo, candidate.changeset.branch)
            return await self.committer.commit(repo, candidate, expected_head, marker)
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._unknown(error)

    async def ensure_draft(
        self, repo: RepoRef, head_branch: str, title: str, body: str
    ) -> DraftOutcome:
        try:
            await self.writable(repo, head_branch)
            text(title)
            text(body, empty=True)
            existing = await self.reviews.find_draft(repo, head_branch)
            if existing is not None:
                return Applied[ReviewRef](value=existing, evidence=evidence("draft_exists"))
            metadata = object_doc((await self.api.metadata(repo)).document)
            base = text(metadata.get("default_branch"))
            reply = await self._mutate(
                repo,
                "POST",
                "pulls",
                {
                    "head": head_branch,
                    "base": base,
                    "title": title,
                    "body": body,
                    "draft": True,
                    "maintainer_can_modify": False,
                },
            )
            row = object_doc(reply.document)
            ref = self.reviews.validate(repo, row)
            if (
                row.get("draft") is not True
                or row.get("state") != "open"
                or not self.subjects._owned(row)
                or object_doc(row.get("head")).get("ref") != head_branch
                or object_doc(row.get("base")).get("ref") != base
            ):
                raise malformed("draft_write_response_mismatch")
            return Applied[ReviewRef](
                value=ref, evidence=evidence("draft_created", reply.request_id)
            )
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def post_review(self, review: ReviewRef, body: ReviewBody, marker: str) -> CommentOutcome:
        try:
            row = await self.reviews.document(review)
            if (
                not self.subjects._owned(row)
                or row.get("draft") is not True
                or row.get("state") != "open"
            ):
                return Refused(reason="review_not_open_bot_draft", evidence=evidence("post_review"))
            await self.writable(review.repo, text(object_doc(row.get("head")).get("ref")))
            if object_doc(row.get("head")).get("sha") != body.candidate_sha:
                return Refused(reason="stale_candidate", evidence=evidence("post_review"))
            content = marked(f"Decision: {body.decision}\n\n{body.body}", marker)
            # Model review is evidence, never the native human approval authority.
            reply = await self._mutate(
                review.repo,
                "POST",
                f"pulls/{review.number}/reviews",
                {"commit_id": body.candidate_sha, "event": "COMMENT", "body": content},
            )
            outcome = self._comment(reply, review, content, "review")
            if object_doc(reply.document).get("commit_id") != body.candidate_sha:
                raise malformed("review_candidate_response_mismatch")
            return outcome
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def reply(self, note: FeedbackNote, body: str, marker: str) -> CommentOutcome:
        try:
            review = note.ref.subject
            if not isinstance(review, ReviewRef):
                return Refused(reason="reply_requires_review_feedback", evidence=evidence("reply"))
            row = await self.reviews.document(review)
            if (
                not self.subjects._owned(row)
                or row.get("state") != "open"
                or row.get("draft") is not True
            ):
                return Refused(reason="reply_requires_open_bot_draft", evidence=evidence("reply"))
            await self.writable(review.repo, text(object_doc(row.get("head")).get("ref")))
            # Re-read the native target: a caller cannot forge a cursor/author and redirect a reply.
            visible = await self.reviews.list_feedback(review, None)
            if note not in visible:
                return Refused(reason="reply_feedback_changed", evidence=evidence("reply"))
            kind = feedback_kind(note)
            content = marked(body, marker)
            if kind == "inline":
                resource = f"pulls/{review.number}/comments/{note.ref.native_id}/replies"
            else:
                content += "\n" + reply_marker(note)
                resource = f"issues/{review.number}/comments"
            reply = await self._mutate(review.repo, "POST", resource, {"body": content})
            outcome = self._comment(
                reply, review, content, "review_comment" if kind == "inline" else "issue_comment"
            )
            if kind == "inline" and object_doc(reply.document).get("in_reply_to_id") != int(
                note.ref.native_id
            ):
                raise malformed("reply_target_response_mismatch")
            return outcome
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def dispatch_lane(self, repo: RepoRef, dispatch: LaneDispatch) -> DispatchOutcome:
        try:
            if (
                dispatch.provider != "github"
                or dispatch.nonce is None
                or await self.ci.binding(repo, dispatch.nonce) != dispatch
            ):
                return Refused(
                    reason="dispatch_not_durably_bound", evidence=evidence("dispatch_lane")
                )
            await self.writable(repo, dispatch.ref)
            if await self.repositories.head(repo, dispatch.ref) != dispatch.execution_base_sha:
                return Refused(reason="dispatch_head_changed", evidence=evidence("dispatch_lane"))
            workflow_id = native_id((await self.ci.workflow(repo)).get("id"))
            await self._mutate(
                repo,
                "POST",
                "actions/workflows/" + workflow_id + "/dispatches",
                {
                    "ref": dispatch.ref,
                    "inputs": {
                        "dispatch": dispatch.model_dump_json(),
                        "requirements_url": self.requirements_url,
                        "requirements_sha256": self.requirements_sha256,
                    },
                },
            )
            result = await self.ci.probe_dispatch(repo, dispatch.nonce)
            if isinstance(result, Found):
                return result.value
            return Unknown(evidence=result.evidence)
        except (TransportFailure, ReadUnavailable, ValueError) as error:
            return self._failure(error)
