"""Immutable identities, observations and effect recovery values for engine ports.

Opaque evidence is encoded text supplied by an adapter. It must contain no
credentials. A successful lane status is execution evidence, never independent
verification. Provider-native identity and attempt remain part of check data.
"""

from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AwareDatetime, Field, model_validator

from forge.domain.budget import Observation, Resources
from forge.domain.plan import RepoPath, RepositoryIdentity, ShortText
from forge.domain.value import Value
from forge.domain.verdicts import CommitOutcome, Conflict, Evidence, Sha, Unknown

T = TypeVar("T")
Positive = Annotated[int, Field(gt=0)]
Cursor = ShortText
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RepoRef(Value):
    """Provider repository identity plus its immutable-at-observation display path."""

    identity: RepositoryIdentity
    full_name: ShortText


class SubjectRef(Value):
    kind: Literal["issue"] = "issue"
    repo: RepoRef
    number: Positive


class ReviewRef(Value):
    kind: Literal["review"] = "review"
    repo: RepoRef
    number: Positive


class CommentRef(Value):
    subject: SubjectRef | ReviewRef
    native_id: ShortText


class Actor(Value):
    provider: Literal["github", "gitlab"]
    connection_id: ShortText
    native_id: ShortText
    login: ShortText
    is_bot: bool


class TaskSnapshot(Value):
    subject: SubjectRef
    title: ShortText
    description: str
    version: ShortText
    labels: tuple[ShortText, ...] = ()


class TreeEntry(Value):
    path: RepoPath
    sha: Sha
    kind: Literal["blob", "tree", "commit"]
    mode: Literal["100644", "100755", "120000", "040000", "160000"]
    size: Annotated[int, Field(ge=0)] | None = None


class Tree(Value):
    repo: RepoRef
    sha: Sha
    entries: tuple[TreeEntry, ...]

    @model_validator(mode="after")
    def unique_paths(self) -> "Tree":
        if len({entry.path for entry in self.entries}) != len(self.entries):
            raise ValueError("tree paths must be unique")
        return self


class NeighborSnapshot(Value):
    """One frozen read scope; an empty allowlist authorizes no paths."""

    repo: RepoRef
    sha: Sha
    allowed_globs: tuple[ShortText, ...] = ("**",)

    @model_validator(mode="after")
    def confined_globs(self) -> "NeighborSnapshot":
        for pattern in self.allowed_globs:
            if (
                pattern.startswith("/")
                or "\\" in pattern
                or ":" in pattern
                or any(part in ("", ".", "..") for part in pattern.split("/"))
                or any(ord(char) < 32 for char in pattern)
            ):
                raise ValueError("read globs must be confined repository patterns")
        return self


class PlanningSnapshot(Value):
    """The only authorized repository snapshots available to planning tools."""

    primary: NeighborSnapshot
    neighbors: tuple[NeighborSnapshot, ...] = ()

    @model_validator(mode="after")
    def unique_repositories(self) -> "PlanningSnapshot":
        identities = (self.primary.repo.identity,) + tuple(n.repo.identity for n in self.neighbors)
        if len({identity.digest for identity in identities}) != len(identities):
            raise ValueError("planning snapshots must name each repository exactly once")
        return self


class FeedbackNote(Value):
    ref: CommentRef
    author: Actor
    body: str
    cursor: Cursor
    created_at: AwareDatetime


class BranchRef(Value):
    repo: RepoRef
    branch: ShortText
    sha: Sha


class LabelState(Value):
    subject: SubjectRef
    label: ShortText
    present: bool


class LaneHandle(Value):
    repo: RepoRef
    native_id: ShortText
    attempt: Positive
    correlation_id: ShortText


class LaneStatus(Value):
    handle: LaneHandle
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "unknown"]
    observed_at: AwareDatetime
    evidence: Evidence


class CheckResult(Value):
    """One native check attempt bound to the tested SHA and independent role."""

    name: ShortText
    tested_sha: Sha
    native_id: ShortText
    attempt: Positive
    role: Literal["verification", "lane"]
    status: Literal["pending", "passed", "failed", "cancelled", "skipped", "unknown"]
    observed_at: AwareDatetime
    evidence: Evidence
    workflow_path: RepoPath | None = None


class ReviewBody(Value):
    candidate_sha: Sha
    body: str
    decision: Literal["approve", "changes_requested", "inconclusive"]


class ReviewVerdict(ReviewBody):
    evidence: Evidence


class ModelReply(Value):
    text: str
    usage: Observation
    evidence: Evidence


class GatewayKeyRef(Value):
    """Nonsecret identity; the gateway adapter owns and seals the credential."""

    alias: ShortText
    model: ShortText
    allowance: Resources
    expires_at: AwareDatetime
    blocked: bool
    enforced_axes: tuple[Literal["usd"]] = ("usd",)


class Applied(Value, Generic[T]):
    kind: Literal["applied"] = "applied"
    value: T
    evidence: Evidence


class Refused(Value):
    kind: Literal["refused"] = "refused"
    reason: ShortText
    evidence: Evidence


CommentOutcome = Applied[CommentRef] | Refused | Unknown
BranchOutcome = Applied[BranchRef] | Conflict | Refused | Unknown
DraftOutcome = Applied[ReviewRef] | Refused | Unknown
LabelOutcome = Applied[LabelState] | Refused | Unknown
DispatchOutcome = Applied[LaneHandle] | Refused | Unknown
GatewayOutcome = Applied[GatewayKeyRef] | Refused | Unknown
Outcome = (
    CommitOutcome
    | CommentOutcome
    | BranchOutcome
    | DraftOutcome
    | LabelOutcome
    | DispatchOutcome
    | GatewayOutcome
)


class Found(Value, Generic[T]):
    """The complete durable write result, not just a native identifier."""

    kind: Literal["found"] = "found"
    value: T
    evidence: Evidence


class DefinitelyAbsent(Value):
    """Adapter-proven absence; eventual-consistency uncertainty is inconclusive."""

    kind: Literal["definitely_absent"] = "definitely_absent"
    evidence: Evidence


class Inconclusive(Value):
    kind: Literal["inconclusive"] = "inconclusive"
    reason: ShortText
    evidence: Evidence


type ProbeResult[T] = Found[T] | DefinitelyAbsent | Inconclusive


class EffectKind(StrEnum):
    COMMIT = "commit"
    ENSURE_BRANCH = "ensure_branch"
    ENSURE_DRAFT = "ensure_draft"
    POST_COMMENT = "post_comment"
    UPDATE_COMMENT = "update_comment"
    POST_REVIEW = "post_review"
    REPLY = "reply"
    SET_LABEL = "set_label"
    DISPATCH_LANE = "dispatch_lane"
    GW_MINT = "gw_mint"
    GW_UPDATE = "gw_update"
    GW_BLOCK = "gw_block"


class ReadUnavailable(Exception):
    """A failed read is neither confirmed absence nor write permission."""

    def __init__(self, reason: str, evidence: str) -> None:
        super().__init__(reason)
        self.evidence = evidence
