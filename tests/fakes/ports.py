"""Strict scripted fakes: every result is explicit; unscripted calls fail closed."""

from collections import deque
from datetime import datetime
from hashlib import sha256
from typing import Generic, TypeVar, cast

from forge_protocol import LaneDispatch, RunIdentity

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import Observation, Resources
from forge.domain.candidate import ValidatedCandidate
from forge.domain.plan import Plan
from forge.domain.verdicts import CommitOutcome, Sha
from forge.port_values import (
    Actor,
    BranchOutcome,
    CheckResult,
    CommentOutcome,
    CommentRef,
    Cursor,
    Digest,
    DispatchOutcome,
    DraftOutcome,
    FeedbackNote,
    GatewayOutcome,
    LabelOutcome,
    LaneHandle,
    LaneStatus,
    ModelReply,
    PlanningSnapshot,
    ProbeResult,
    RepoRef,
    ReviewBody,
    ReviewRef,
    ReviewVerdict,
    SubjectRef,
    TaskSnapshot,
    Tree,
)


T = TypeVar("T")


class ScriptedPorts:
    """Ordered deterministic responses for provider and model collaborators.

    Scripts may include exceptions, Unknown writes or Inconclusive probes.
    No method invents a positive response. Arguments are retained for assertions.
    """

    def __init__(self, script: tuple[tuple[str, object], ...] = ()) -> None:
        self.script = deque(script)
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def take(self, method: str, *args: object) -> object:
        self.calls.append((method, args))
        if not self.script:
            raise AssertionError(f"unscripted call: {method}")
        expected, value = self.script.popleft()
        if expected != method:
            raise AssertionError(f"expected {expected}, got {method}")
        if isinstance(value, Exception):
            raise value
        return value

    async def get_task(self, subject: SubjectRef) -> TaskSnapshot:
        return cast(TaskSnapshot, self.take("get_task", subject))

    async def find_comment(self, subject: SubjectRef, marker: str) -> CommentRef | None:
        return cast(CommentRef | None, self.take("find_comment", subject, marker))

    async def probe_comment(self, subject: SubjectRef, marker: str) -> ProbeResult[CommentOutcome]:
        return cast(ProbeResult[CommentOutcome], self.take("probe_comment", subject, marker))

    async def probe_comment_update(
        self, comment: CommentRef, body_digest: Digest
    ) -> ProbeResult[CommentOutcome]:
        return cast(
            ProbeResult[CommentOutcome], self.take("probe_comment_update", comment, body_digest)
        )

    async def probe_label(
        self, subject: SubjectRef, label: str, present: bool
    ) -> ProbeResult[LabelOutcome]:
        return cast(ProbeResult[LabelOutcome], self.take("probe_label", subject, label, present))

    async def head(self, repo: RepoRef, branch: str) -> Sha | None:
        return cast(Sha | None, self.take("head", repo, branch))

    async def read_tree(self, repo: RepoRef, sha: Sha, prefix: str = "") -> Tree:
        return cast(Tree, self.take("read_tree", repo, sha, prefix))

    async def read_blob(self, repo: RepoRef, sha: Sha, path: str) -> bytes | None:
        return cast(bytes | None, self.take("read_blob", repo, sha, path))

    async def find_commit_by_marker(self, repo: RepoRef, branch: str, marker: str) -> Sha | None:
        return cast(Sha | None, self.take("find_commit_by_marker", repo, branch, marker))

    async def probe_branch(
        self, repo: RepoRef, branch: str, base: Sha
    ) -> ProbeResult[BranchOutcome]:
        return cast(ProbeResult[BranchOutcome], self.take("probe_branch", repo, branch, base))

    async def probe_commit(
        self,
        repo: RepoRef,
        branch: str,
        marker: str,
        expected_parent: Sha,
        candidate: ValidatedCandidate,
    ) -> ProbeResult[CommitOutcome]:
        return cast(
            ProbeResult[CommitOutcome],
            self.take("probe_commit", repo, branch, marker, expected_parent, candidate),
        )

    async def find_draft(self, repo: RepoRef, head_branch: str) -> ReviewRef | None:
        return cast(ReviewRef | None, self.take("find_draft", repo, head_branch))

    async def review_head(self, review: ReviewRef) -> Sha:
        return cast(Sha, self.take("review_head", review))

    async def list_feedback(
        self, review: ReviewRef, after: Cursor | None
    ) -> tuple[FeedbackNote, ...]:
        return cast(tuple[FeedbackNote, ...], self.take("list_feedback", review, after))

    async def probe_draft(self, repo: RepoRef, head_branch: str) -> ProbeResult[DraftOutcome]:
        return cast(ProbeResult[DraftOutcome], self.take("probe_draft", repo, head_branch))

    async def probe_review(self, review: ReviewRef, marker: str) -> ProbeResult[CommentOutcome]:
        return cast(ProbeResult[CommentOutcome], self.take("probe_review", review, marker))

    async def probe_reply(self, note: FeedbackNote, marker: str) -> ProbeResult[CommentOutcome]:
        return cast(ProbeResult[CommentOutcome], self.take("probe_reply", note, marker))

    async def find_lane(self, repo: RepoRef, correlation_id: str) -> LaneHandle | None:
        return cast(LaneHandle | None, self.take("find_lane", repo, correlation_id))

    async def lane_status(self, handle: LaneHandle) -> LaneStatus:
        return cast(LaneStatus, self.take("lane_status", handle))

    async def checks_for(self, repo: RepoRef, sha: Sha) -> tuple[CheckResult, ...]:
        return cast(tuple[CheckResult, ...], self.take("checks_for", repo, sha))

    async def probe_dispatch(
        self, repo: RepoRef, correlation_id: str
    ) -> ProbeResult[DispatchOutcome]:
        return cast(ProbeResult[DispatchOutcome], self.take("probe_dispatch", repo, correlation_id))

    async def is_approver(self, actor: Actor, subject: SubjectRef) -> bool:
        return cast(bool, self.take("is_approver", actor, subject))

    def bot_identity(self) -> Actor:
        return cast(Actor, self.take("bot_identity"))

    async def post_comment(self, subject: SubjectRef, body: str, marker: str) -> CommentOutcome:
        return cast(CommentOutcome, self.take("post_comment", subject, body, marker))

    async def update_comment(self, comment: CommentRef, body: str) -> CommentOutcome:
        return cast(CommentOutcome, self.take("update_comment", comment, body))

    async def set_label(self, subject: SubjectRef, label: str, present: bool) -> LabelOutcome:
        return cast(LabelOutcome, self.take("set_label", subject, label, present))

    async def ensure_branch(self, repo: RepoRef, branch: str, base: Sha) -> BranchOutcome:
        return cast(BranchOutcome, self.take("ensure_branch", repo, branch, base))

    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: Sha, marker: str
    ) -> CommitOutcome:
        return cast(CommitOutcome, self.take("commit", repo, candidate, expected_head, marker))

    async def ensure_draft(
        self, repo: RepoRef, head_branch: str, title: str, body: str
    ) -> DraftOutcome:
        return cast(DraftOutcome, self.take("ensure_draft", repo, head_branch, title, body))

    async def post_review(self, review: ReviewRef, body: ReviewBody, marker: str) -> CommentOutcome:
        return cast(CommentOutcome, self.take("post_review", review, body, marker))

    async def reply(self, note: FeedbackNote, body: str, marker: str) -> CommentOutcome:
        return cast(CommentOutcome, self.take("reply", note, body, marker))

    async def dispatch_lane(self, repo: RepoRef, dispatch: LaneDispatch) -> DispatchOutcome:
        return cast(DispatchOutcome, self.take("dispatch_lane", repo, dispatch))

    async def plan(self, task: TaskSnapshot, snapshot: PlanningSnapshot) -> Plan:
        return cast(Plan, self.take("plan", task, snapshot))

    async def review(
        self, candidate_diff: bytes, approved_input: ApprovedInput, *, candidate_sha: Sha
    ) -> ReviewVerdict:
        return cast(
            ReviewVerdict, self.take("review", candidate_diff, approved_input, candidate_sha)
        )

    async def mint(
        self,
        identity: RunIdentity,
        alias: str,
        model: str,
        allowance: Resources,
        expires_at: datetime,
    ) -> GatewayOutcome:
        return cast(
            GatewayOutcome, self.take("mint", identity, alias, model, allowance, expires_at)
        )

    async def update(self, alias: str, allowance: Resources) -> GatewayOutcome:
        return cast(GatewayOutcome, self.take("update", alias, allowance))

    async def block(self, alias: str) -> GatewayOutcome:
        return cast(GatewayOutcome, self.take("block", alias))

    async def read_spend(self, alias: str) -> Observation:
        return cast(Observation, self.take("read_spend", alias))

    async def probe_mint(self, alias: str) -> ProbeResult[GatewayOutcome]:
        return cast(ProbeResult[GatewayOutcome], self.take("probe_mint", alias))

    async def probe_update(self, alias: str, allowance: Resources) -> ProbeResult[GatewayOutcome]:
        return cast(ProbeResult[GatewayOutcome], self.take("probe_update", alias, allowance))

    async def probe_block(self, alias: str) -> ProbeResult[GatewayOutcome]:
        return cast(ProbeResult[GatewayOutcome], self.take("probe_block", alias))

    async def complete(
        self, *, model: str, system: str, prompt: str, credential_alias: str, max_output_tokens: int
    ) -> ModelReply:
        return cast(
            ModelReply,
            self.take("complete", model, system, prompt, credential_alias, max_output_tokens),
        )


class FixedClock:
    def __init__(self, instant: datetime) -> None:
        if instant.utcoffset() is None:
            raise ValueError("clock requires an aware time")
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


class MemoryBlobs:
    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}

    async def get(self, digest: Digest) -> bytes | None:
        return self.content.get(digest)

    async def put(self, content: bytes) -> Digest:
        digest = sha256(content).hexdigest()
        self.content[digest] = bytes(content)
        return digest


class ScriptedProbe(Generic[T]):
    def __init__(self, result: ProbeResult[T]) -> None:
        self.result = result
        self.calls = 0

    async def __call__(self) -> ProbeResult[T]:
        self.calls += 1
        return self.result
