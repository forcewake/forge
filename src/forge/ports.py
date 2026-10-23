"""Engine boundaries: reads are direct, all external writes are effects-owned.

None from a read means confirmed absence; failures raise ReadUnavailable.
Probes are mandatory for every write. A negative eventually-consistent read is
Inconclusive until the adapter can prove absence. Evidence must be credential-free.
WritePort is declared only here and held only by the composition root and Effects.
"""

from datetime import datetime
from typing import Protocol, TypeVar

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


class SubjectPort(Protocol):
    """Read issue identity and recover comments or labels without writing."""

    async def get_task(self, subject: SubjectRef) -> TaskSnapshot: ...
    async def find_comment(self, subject: SubjectRef, marker: str) -> CommentRef | None: ...
    async def probe_comment(
        self, subject: SubjectRef, marker: str
    ) -> ProbeResult[CommentOutcome]: ...
    async def probe_comment_update(
        self, comment: CommentRef, body_digest: Digest
    ) -> ProbeResult[CommentOutcome]: ...
    async def probe_label(
        self, subject: SubjectRef, label: str, present: bool
    ) -> ProbeResult[LabelOutcome]: ...


class RepoPort(Protocol):
    """Read immutable Git snapshots; every read names its repository and SHA."""

    async def head(self, repo: RepoRef, branch: str) -> Sha | None: ...
    async def read_tree(self, repo: RepoRef, sha: Sha, prefix: str = "") -> Tree: ...
    async def read_blob(self, repo: RepoRef, sha: Sha, path: str) -> bytes | None: ...
    async def find_commit_by_marker(
        self, repo: RepoRef, branch: str, marker: str
    ) -> Sha | None: ...
    async def probe_branch(
        self, repo: RepoRef, branch: str, base: Sha
    ) -> ProbeResult[BranchOutcome]: ...
    async def probe_commit(
        self,
        repo: RepoRef,
        branch: str,
        marker: str,
        expected_parent: Sha,
        candidate: ValidatedCandidate,
    ) -> ProbeResult[CommitOutcome]: ...


class ReviewPort(Protocol):
    """Read one draft and its feedback; marker probes preserve full outcomes."""

    async def find_draft(self, repo: RepoRef, head_branch: str) -> ReviewRef | None: ...
    async def review_head(self, review: ReviewRef) -> Sha: ...
    async def list_feedback(
        self, review: ReviewRef, after: Cursor | None
    ) -> tuple[FeedbackNote, ...]: ...
    async def probe_draft(self, repo: RepoRef, head_branch: str) -> ProbeResult[DraftOutcome]: ...
    async def probe_review(self, review: ReviewRef, marker: str) -> ProbeResult[CommentOutcome]: ...
    async def probe_reply(self, note: FeedbackNote, marker: str) -> ProbeResult[CommentOutcome]: ...


class CIPort(Protocol):
    """Native attempts remain distinct; lane success is not verification proof."""

    async def find_lane(self, repo: RepoRef, correlation_id: str) -> LaneHandle | None: ...
    async def lane_status(self, handle: LaneHandle) -> LaneStatus: ...
    async def checks_for(self, repo: RepoRef, sha: Sha) -> tuple[CheckResult, ...]: ...
    async def probe_dispatch(
        self, repo: RepoRef, correlation_id: str
    ) -> ProbeResult[DispatchOutcome]: ...


class ActorPort(Protocol):
    """Connection-scoped authorization and bot identity, without write authority."""

    async def is_approver(self, actor: Actor, subject: SubjectRef) -> bool: ...
    def bot_identity(self) -> Actor: ...


class WritePort(Protocol):
    """External provider writes; no merge or default/protected-branch capability.

    Only Effects invokes these methods. Commit consumes the validated manifest,
    including file modes, and performs native expected-head compare-and-swap.
    Timeouts return Unknown; they never imply success or permit a blind retry.
    """

    async def post_comment(self, subject: SubjectRef, body: str, marker: str) -> CommentOutcome: ...
    async def update_comment(self, comment: CommentRef, body: str) -> CommentOutcome: ...
    async def set_label(self, subject: SubjectRef, label: str, present: bool) -> LabelOutcome: ...
    async def ensure_branch(self, repo: RepoRef, branch: str, base: Sha) -> BranchOutcome: ...
    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: Sha, marker: str
    ) -> CommitOutcome: ...
    async def ensure_draft(
        self, repo: RepoRef, head_branch: str, title: str, body: str
    ) -> DraftOutcome: ...
    async def post_review(
        self, review: ReviewRef, body: ReviewBody, marker: str
    ) -> CommentOutcome: ...
    async def reply(self, note: FeedbackNote, body: str, marker: str) -> CommentOutcome: ...
    async def dispatch_lane(self, repo: RepoRef, dispatch: LaneDispatch) -> DispatchOutcome: ...


class Planner(Protocol):
    """Plan against frozen, explicitly authorized primary and neighbor snapshots."""

    async def plan(self, task: TaskSnapshot, snapshot: PlanningSnapshot) -> Plan: ...


class Reviewer(Protocol):
    """Review the exact candidate; an unknown review cannot authorize readiness."""

    async def review(
        self, candidate_diff: bytes, approved_input: ApprovedInput, *, candidate_sha: Sha
    ) -> ReviewVerdict: ...


class BudgetGateway(Protocol):
    """Effects-owned key writes; read-only spend and probes return no credentials.

    Alias is the nonsecret durable effect identity. The adapter seals the actual
    credential for lane handoff; callers never receive it in outcomes/evidence.
    """

    async def mint(
        self,
        identity: RunIdentity,
        alias: str,
        model: str,
        allowance: Resources,
        expires_at: datetime,
    ) -> GatewayOutcome: ...
    async def update(self, alias: str, allowance: Resources) -> GatewayOutcome: ...
    async def block(self, alias: str) -> GatewayOutcome: ...
    async def read_spend(self, alias: str) -> Observation: ...
    async def probe_mint(self, alias: str) -> ProbeResult[GatewayOutcome]: ...
    async def probe_update(
        self, alias: str, allowance: Resources
    ) -> ProbeResult[GatewayOutcome]: ...
    async def probe_block(self, alias: str) -> ProbeResult[GatewayOutcome]: ...


class ModelClient(Protocol):
    """Server-side model request through an approved credential alias, not a raw key."""

    async def complete(
        self, *, model: str, system: str, prompt: str, credential_alias: str, max_output_tokens: int
    ) -> ModelReply: ...


class Clock(Protocol):
    """An aware UTC clock; deterministic clocks require no sleeping."""

    def now(self) -> datetime: ...


class BlobPort(Protocol):
    """Content-addressed immutable bytes; put returns their SHA-256 digest."""

    async def get(self, digest: Digest) -> bytes | None: ...
    async def put(self, content: bytes) -> Digest: ...


class EffectProbe(Protocol[T]):
    """A read-only recovery callback returning the complete typed write outcome."""

    async def __call__(self) -> ProbeResult[T]: ...
