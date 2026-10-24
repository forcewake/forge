"""Native repository identity chooses concrete ports for every read and effect."""

from dataclasses import dataclass
from collections.abc import Mapping

from forge_protocol import LaneDispatch

from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import CommitOutcome, Sha
from forge.port_values import (
    BranchOutcome,
    CheckResult,
    CommentOutcome,
    CommentRef,
    Cursor,
    Digest,
    DispatchOutcome,
    DraftOutcome,
    FeedbackNote,
    LabelOutcome,
    LaneHandle,
    LaneStatus,
    ProbeResult,
    RepoRef,
    ReviewBody,
    ReviewRef,
    SubjectRef,
    TaskSnapshot,
    Tree,
)
from forge.ports import ActorPort, CIPort, RepoPort, ReviewPort, SubjectPort
from forge.store.records import StoreConflict
from forge.providers.github.write import GitHubWrites
from forge.providers.gitlab.write import GitLabWrites


@dataclass(frozen=True)
class NativePorts:
    ref: RepoRef
    subject: SubjectPort
    repository: RepoPort
    review: ReviewPort
    ci: CIPort
    actor: ActorPort
    writes: GitHubWrites | GitLabWrites


class Registry:
    def __init__(self, entries: Mapping[str, NativePorts]) -> None:
        self.entries = dict(entries)
        self.native = {value.ref.identity.digest: value for value in entries.values()}
        if len(self.native) != len(self.entries):
            raise ValueError("configured native repository identities must be unique")

    def for_repo(self, repo: RepoRef) -> NativePorts:
        value = self.native.get(repo.identity.digest)
        if value is None or value.ref.identity != repo.identity:
            raise StoreConflict("repository_not_configured")
        return value

    def named(self, key: str) -> NativePorts:
        value = self.entries.get(key)
        if value is None:
            raise StoreConflict("repository_not_configured")
        return value

    def actor_for(self, repo: RepoRef) -> ActorPort:
        return self.for_repo(repo).actor

    async def get_task(self, subject: SubjectRef) -> TaskSnapshot:
        return await self.for_repo(subject.repo).subject.get_task(subject)

    async def find_comment(self, subject: SubjectRef | ReviewRef, marker: str) -> CommentRef | None:
        return await self.for_repo(subject.repo).subject.find_comment(subject, marker)

    async def probe_comment(
        self, subject: SubjectRef | ReviewRef, marker: str
    ) -> ProbeResult[CommentOutcome]:
        return await self.for_repo(subject.repo).subject.probe_comment(subject, marker)

    async def probe_comment_update(
        self, comment: CommentRef, body_digest: Digest
    ) -> ProbeResult[CommentOutcome]:
        return await self.for_repo(comment.subject.repo).subject.probe_comment_update(
            comment, body_digest
        )

    async def probe_label(
        self, subject: SubjectRef, label: str, present: bool
    ) -> ProbeResult[LabelOutcome]:
        return await self.for_repo(subject.repo).subject.probe_label(subject, label, present)

    async def head(self, repo: RepoRef, branch: str) -> Sha | None:
        return await self.for_repo(repo).repository.head(repo, branch)

    async def read_tree(self, repo: RepoRef, sha: Sha, prefix: str = "") -> Tree:
        return await self.for_repo(repo).repository.read_tree(repo, sha, prefix)

    async def read_blob(self, repo: RepoRef, sha: Sha, path: str) -> bytes | None:
        return await self.for_repo(repo).repository.read_blob(repo, sha, path)

    async def find_commit_by_marker(self, repo: RepoRef, branch: str, marker: str) -> Sha | None:
        return await self.for_repo(repo).repository.find_commit_by_marker(repo, branch, marker)

    async def probe_branch(
        self, repo: RepoRef, branch: str, base: Sha
    ) -> ProbeResult[BranchOutcome]:
        return await self.for_repo(repo).repository.probe_branch(repo, branch, base)

    async def probe_commit(
        self,
        repo: RepoRef,
        branch: str,
        marker: str,
        expected_parent: Sha,
        candidate: ValidatedCandidate,
    ) -> ProbeResult[CommitOutcome]:
        return await self.for_repo(repo).repository.probe_commit(
            repo, branch, marker, expected_parent, candidate
        )

    async def find_draft(self, repo: RepoRef, head_branch: str) -> ReviewRef | None:
        return await self.for_repo(repo).review.find_draft(repo, head_branch)

    async def review_head(self, review: ReviewRef) -> Sha:
        return await self.for_repo(review.repo).review.review_head(review)

    async def list_feedback(
        self, review: ReviewRef, after: Cursor | None
    ) -> tuple[FeedbackNote, ...]:
        return await self.for_repo(review.repo).review.list_feedback(review, after)

    async def probe_draft(self, repo: RepoRef, head_branch: str) -> ProbeResult[DraftOutcome]:
        return await self.for_repo(repo).review.probe_draft(repo, head_branch)

    async def probe_review(self, review: ReviewRef, marker: str) -> ProbeResult[CommentOutcome]:
        return await self.for_repo(review.repo).review.probe_review(review, marker)

    async def probe_reply(self, note: FeedbackNote, marker: str) -> ProbeResult[CommentOutcome]:
        return await self.for_repo(note.ref.subject.repo).review.probe_reply(note, marker)

    async def find_lane(self, repo: RepoRef, correlation_id: str) -> LaneHandle | None:
        return await self.for_repo(repo).ci.find_lane(repo, correlation_id)

    async def lane_status(self, handle: LaneHandle) -> LaneStatus:
        return await self.for_repo(handle.repo).ci.lane_status(handle)

    async def checks_for(self, repo: RepoRef, sha: Sha) -> tuple[CheckResult, ...]:
        return await self.for_repo(repo).ci.checks_for(repo, sha)

    async def probe_dispatch(
        self, repo: RepoRef, correlation_id: str
    ) -> ProbeResult[DispatchOutcome]:
        return await self.for_repo(repo).ci.probe_dispatch(repo, correlation_id)

    async def post_comment(
        self, subject: SubjectRef | ReviewRef, body: str, marker: str
    ) -> CommentOutcome:
        return await self.for_repo(subject.repo).writes.post_comment(subject, body, marker)

    async def update_comment(self, comment: CommentRef, body: str) -> CommentOutcome:
        return await self.for_repo(comment.subject.repo).writes.update_comment(comment, body)

    async def set_label(self, subject: SubjectRef, label: str, present: bool) -> LabelOutcome:
        return await self.for_repo(subject.repo).writes.set_label(subject, label, present)

    async def ensure_branch(self, repo: RepoRef, branch: str, base: Sha) -> BranchOutcome:
        return await self.for_repo(repo).writes.ensure_branch(repo, branch, base)

    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: Sha, marker: str
    ) -> CommitOutcome:
        return await self.for_repo(repo).writes.commit(repo, candidate, expected_head, marker)

    async def ensure_draft(
        self, repo: RepoRef, head_branch: str, title: str, body: str
    ) -> DraftOutcome:
        return await self.for_repo(repo).writes.ensure_draft(repo, head_branch, title, body)

    async def post_review(self, review: ReviewRef, body: ReviewBody, marker: str) -> CommentOutcome:
        return await self.for_repo(review.repo).writes.post_review(review, body, marker)

    async def reply(self, note: FeedbackNote, body: str, marker: str) -> CommentOutcome:
        return await self.for_repo(note.ref.subject.repo).writes.reply(note, body, marker)

    async def dispatch_lane(self, repo: RepoRef, dispatch: LaneDispatch) -> DispatchOutcome:
        return await self.for_repo(repo).writes.dispatch_lane(repo, dispatch)
