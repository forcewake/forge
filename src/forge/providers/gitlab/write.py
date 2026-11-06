"""Effects-owned native writes: project bots, factory branches and no merge action."""

from fnmatch import fnmatchcase
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from forge_protocol import LaneDispatch

from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import CommitOutcome, Conflict, Unknown
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
    Refused,
    RepoRef,
    ReviewBody,
    ReviewRef,
    SubjectRef,
    ReadUnavailable,
)
from forge.providers.gitlab.ci import GitLabCI
from forge.providers.gitlab.client import API
from forge.providers.gitlab.notes import resolve_inline, review_document
from forge.providers.gitlab.repo import GitLabRepo
from forge.providers.gitlab.review import GitLabReview, feedback_discussion, reply_marker
from forge.providers.gitlab.subject import GitLabSubject, note_resource
from forge.providers.gitlab.transport import Failure, Reply
from forge.providers.gitlab.values import (
    array,
    branch,
    evidence,
    marked,
    native_id,
    obj,
    safe_body,
    sha,
    text,
    unavailable,
)


class PolicyRefused(ReadUnavailable):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason, evidence("write_policy", reason=reason))


class Committer(Protocol):
    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: str, marker: str
    ) -> CommitOutcome: ...


class GitLabWrites:
    def __init__(
        self,
        api: API,
        *,
        repositories: GitLabRepo,
        reviews: GitLabReview,
        ci: GitLabCI,
        committer: Committer,
        requirements_url: str,
        requirements_sha256: str,
    ) -> None:
        parsed = urlsplit(requirements_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or len(requirements_sha256) != 64
            or any(c not in "0123456789abcdef" for c in requirements_sha256)
        ):
            raise ValueError("explicit immutable lane requirements URL and digest required")
        self.api, self.repositories, self.reviews, self.ci, self.committer = (
            api,
            repositories,
            reviews,
            ci,
            committer,
        )
        self.subjects = GitLabSubject(api)
        self.requirements_url, self.requirements_sha256 = requirements_url, requirements_sha256

    async def writable(self, repo: RepoRef, name: str) -> str:
        encoded = branch(name)
        metadata = obj((await self.api.metadata(repo)).document)
        default = text(metadata.get("default_branch"))
        if metadata.get("archived") is True:
            raise PolicyRefused("project_archived")
        if metadata.get("archived") is not False:
            raise unavailable("project_write_policy_unavailable")
        if name == default or not name.startswith("forge/"):
            raise PolicyRefused("default_or_nonfactory_branch_refused")
        patterns = await self.api.pages(repo, "protected_branches")
        if any(fnmatchcase(name, text(row.get("name"))) for row in patterns):
            raise PolicyRefused("protected_branch_refused")
        try:
            row = obj(
                (await self.api.request(repo, "GET", "repository/branches/" + encoded)).document
            )
        except Failure as error:
            if error.reason != "resource_not_found":
                raise ReadUnavailable("branch_policy_unavailable", error.evidence) from None
        else:
            if row.get("name") != name or row.get("protected") is not False:
                raise PolicyRefused("protected_or_wrong_branch_refused")
        return encoded

    async def _mutate(
        self, repo: RepoRef, method: str, resource: str, body: dict[str, Any]
    ) -> Reply:
        return await self.api.request(repo, method, resource, body=body, write=True)

    def _comment(
        self,
        reply: Reply,
        subject: SubjectRef | ReviewRef,
        body: str,
        kind: Literal["issue_comment", "review_comment", "review"] = "issue_comment",
    ) -> Applied[CommentRef]:
        row = obj(reply.document)
        ref = CommentRef(subject=subject, native_id=native_id(row.get("id")), kind=kind)
        if (
            not self.subjects.bound(ref, row)
            or not self.subjects.owned(subject, row)
            or row.get("body") != body
        ):
            raise unavailable("note_write_response_mismatch")
        return Applied[CommentRef](value=ref, evidence=evidence("note_write", reply.request_id))

    @staticmethod
    def _failure(error: Failure | ReadUnavailable | ValueError) -> Unknown | Refused:
        if isinstance(error, PolicyRefused):
            return Refused(reason=error.reason, evidence=error.evidence)
        return Unknown(
            evidence=error.evidence
            if isinstance(error, (Failure, ReadUnavailable))
            else evidence("write", reason="invalid_native_response")
        )

    @staticmethod
    def _body(body: str) -> str:
        try:
            return safe_body(body)
        except ReadUnavailable:
            raise PolicyRefused("gitlab_quick_action_refused") from None

    async def post_comment(
        self, subject: SubjectRef | ReviewRef, body: str, marker: str
    ) -> CommentOutcome:
        try:
            self._body(body)
            content = marked(body, marker)
            if isinstance(subject, SubjectRef):
                await self.subjects.get_task(subject)
                collection = "issues"
            else:
                await review_document(self.api, subject)
                collection = "merge_requests"
            reply = await self._mutate(
                subject.repo, "POST", f"{collection}/{subject.number}/notes", {"body": content}
            )
            return self._comment(reply, subject, content)
        except (Failure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def update_comment(self, comment: CommentRef, body: str) -> CommentOutcome:
        try:
            self._body(body)
            if comment.kind == "review_comment":
                resource, row = await resolve_inline(self.api, comment)
            else:
                resource = note_resource(comment)
                row = obj((await self.api.read(comment.subject.repo, resource)).document)
                if not self.subjects.bound(comment, row):
                    raise PolicyRefused("comment_target_mismatch")
            if not self.subjects.owned(comment.subject, row):
                raise PolicyRefused("comment_not_bot_owned")
            reply = await self._mutate(comment.subject.repo, "PUT", resource, {"body": body})
            return self._comment(reply, comment.subject, body, comment.kind)
        except (Failure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def set_label(self, subject: SubjectRef, label: str, present: bool) -> LabelOutcome:
        try:
            text(label)
            if "," in label or any(ord(c) < 32 or ord(c) == 127 for c in label):
                raise PolicyRefused("ambiguous_label_refused")
            current = await self.subjects.get_task(subject)
            if (label in current.labels) != present:
                reply = await self._mutate(
                    subject.repo,
                    "PUT",
                    f"issues/{subject.number}",
                    {"add_labels" if present else "remove_labels": label},
                )
                row = obj(reply.document)
                if (
                    native_id(row.get("project_id")) != subject.repo.identity.repository_id
                    or native_id(row.get("iid")) != str(subject.number)
                    or (label in array(row.get("labels"))) != present
                ):
                    raise unavailable("label_write_response_mismatch")
            return Applied[LabelState](
                value=LabelState(subject=subject, label=label, present=present),
                evidence=evidence("label_write"),
            )
        except (Failure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def ensure_branch(self, repo: RepoRef, branch: str, base: str) -> BranchOutcome:
        try:
            await self.writable(repo, branch)
            base = sha(base)
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
                repo, "POST", "repository/branches", {"branch": branch, "ref": base}
            )
            row = obj(reply.document)
            if (
                row.get("name") != branch
                or obj(row.get("commit")).get("id") != base
                or row.get("protected") is not False
            ):
                raise unavailable("branch_write_response_mismatch")
            return Applied[BranchRef](
                value=BranchRef(repo=repo, branch=branch, sha=base),
                evidence=evidence("branch_created", reply.request_id),
            )
        except (Failure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: str, marker: str
    ) -> CommitOutcome:
        try:
            await self.writable(repo, candidate.changeset.branch)
            return await self.committer.commit(repo, candidate, expected_head, marker)
        except (Failure, ReadUnavailable, ValueError) as error:
            outcome = self._failure(error)
            return Unknown(evidence=outcome.evidence)

    async def ensure_draft(
        self, repo: RepoRef, head_branch: str, title: str, body: str
    ) -> DraftOutcome:
        try:
            self._body(body)
            text(title)
            await self.writable(repo, head_branch)
            existing = await self.reviews.find_draft(repo, head_branch)
            if existing is not None:
                return Applied[ReviewRef](value=existing, evidence=evidence("draft_exists"))
            metadata = obj((await self.api.metadata(repo)).document)
            target = text(metadata.get("default_branch"))
            reply = await self._mutate(
                repo,
                "POST",
                "merge_requests",
                {
                    "source_branch": head_branch,
                    "target_branch": target,
                    "title": "Draft: " + title,
                    "description": body,
                    "remove_source_branch": False,
                    "allow_collaboration": False,
                },
            )
            row = obj(reply.document)
            ref = self.reviews.validate(repo, row)
            if (
                row.get("draft") is not True
                or row.get("state") != "opened"
                or row.get("source_branch") != head_branch
                or row.get("target_branch") != target
                or not self.reviews.bot_owned(repo, row)
            ):
                raise unavailable("draft_write_response_mismatch")
            return Applied[ReviewRef](
                value=ref, evidence=evidence("draft_created", reply.request_id)
            )
        except (Failure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def post_review(self, review: ReviewRef, body: ReviewBody, marker: str) -> CommentOutcome:
        try:
            self._body(body.body)
            row = await self._review_writable(review)
            if row.get("sha") != body.candidate_sha:
                raise PolicyRefused("stale_candidate")
            content = marked(f"Decision: {body.decision}\n\n{body.body}", marker)
            reply = await self._mutate(
                review.repo, "POST", f"merge_requests/{review.number}/notes", {"body": content}
            )
            return self._comment(reply, review, content, "review")
        except (Failure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def _review_writable(self, review: ReviewRef) -> dict[str, object]:
        row = await self.reviews.document(review)
        if (
            row.get("draft") is not True
            or row.get("state") != "opened"
            or not self.reviews.bot_owned(review.repo, row)
        ):
            raise PolicyRefused("review_requires_open_bot_draft")
        await self.writable(review.repo, text(row.get("source_branch")))
        return row

    async def reply(self, note: FeedbackNote, body: str, marker: str) -> CommentOutcome:
        try:
            self._body(body)
            if not isinstance(note.ref.subject, ReviewRef):
                raise PolicyRefused("reply_requires_review_feedback")
            review = note.ref.subject
            await self._review_writable(review)
            if note not in await self.reviews.list_feedback(review, None):
                raise PolicyRefused("reply_feedback_changed")
            discussion = feedback_discussion(note)
            content = marked(body, marker) + "\n" + reply_marker(note)
            reply = await self._mutate(
                review.repo,
                "POST",
                f"merge_requests/{review.number}/discussions/{discussion}/notes",
                {"body": content},
            )
            return self._comment(reply, review, content, "review_comment")
        except (Failure, ReadUnavailable, ValueError) as error:
            return self._failure(error)

    async def dispatch_lane(self, repo: RepoRef, dispatch: LaneDispatch) -> DispatchOutcome:
        try:
            if (
                dispatch.provider != "gitlab"
                or dispatch.nonce is None
                or await self.ci.binding(repo, dispatch.nonce) != dispatch
            ):
                raise PolicyRefused("dispatch_not_durably_bound")
            await self.writable(repo, dispatch.ref)
            if await self.repositories.head(repo, dispatch.ref) != dispatch.execution_base_sha:
                raise PolicyRefused("dispatch_head_changed")
            variables = {
                "FORGE_LANE_NONCE": dispatch.nonce,
                "FORGE_LANE_DISPATCH": dispatch.model_dump_json(),
                "FORGE_LANE_REQUIREMENTS_URL": self.requirements_url,
                "FORGE_LANE_REQUIREMENTS_SHA256": self.requirements_sha256,
                "FORGE_PUBLIC_URL": str(dispatch.server_url).rstrip("/"),
            }
            reply = await self._mutate(
                repo,
                "POST",
                "pipeline",
                {
                    "ref": dispatch.ref,
                    "variables": [
                        {"key": key, "value": value, "variable_type": "env_var"}
                        for key, value in variables.items()
                    ],
                },
            )
            row = obj(reply.document)
            handle = self.ci._handle(
                repo,
                row,
                {
                    "FORGE_LANE_NONCE": dispatch.nonce,
                    "FORGE_LANE_DISPATCH": dispatch.model_dump_json(),
                },
                dispatch,
            )
            return Applied(
                value=handle,
                evidence=evidence(
                    "pipeline_created", reply.request_id, pipeline_id=handle.native_id
                ),
            )
        except (Failure, ReadUnavailable, ValueError) as error:
            return self._failure(error)
