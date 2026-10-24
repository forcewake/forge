"""Deterministic native state and explicit visibility faults, without network I/O."""

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha1
import json
from typing import Literal, TypeVar, cast

from pydantic import TypeAdapter, ValidationError
from forge.domain.plan import RepoPath

from forge.domain.verdicts import Sha, Unknown
from forge.port_values import (
    Actor,
    CommentRef,
    FeedbackNote,
    Found,
    Inconclusive,
    DefinitelyAbsent,
    LaneHandle,
    LaneStatus,
    Outcome,
    ProbeResult,
    ReadUnavailable,
    RepoRef,
    ReviewRef,
    SubjectRef,
    TaskSnapshot,
    CheckResult,
)

T = TypeVar("T")


@dataclass(frozen=True)
class Fault:
    unknown_after_success: bool = False
    conflict: bool = False
    http_5xx: bool = False
    delay_visibility: int = 0

    def __post_init__(self) -> None:
        if (
            self.delay_visibility < 0
            or sum((self.unknown_after_success, self.conflict, self.http_5xx)) > 1
        ):
            raise ValueError("choose one fault outcome and a nonnegative visibility delay")


@dataclass(frozen=True)
class Blob:
    content: bytes
    mode: Literal["100644", "100755", "120000"] = "100644"

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", bytes(self.content))
        if self.mode not in ("100644", "100755", "120000"):
            raise ValueError("unsupported blob mode")

    @property
    def sha(self) -> Sha:
        raw = b"blob " + str(len(self.content)).encode() + b"\x00" + self.content
        return sha1(raw, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Commit:
    sha: Sha
    parent: Sha | None
    files: tuple[tuple[str, Blob], ...]
    message: str


@dataclass
class Repository:
    ref: RepoRef
    default_branch: str
    protected: frozenset[str]
    commits: dict[str, Commit] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)


@dataclass
class NativeComment:
    ref: CommentRef
    body: str
    marker: str
    reply_to: str | None = None
    marker_appended: bool = True

    @property
    def displayed_body(self) -> str:
        return (
            self.body + "\n\n" + self.marker if self.marker_appended and self.marker else self.body
        )


@dataclass
class Draft:
    ref: ReviewRef
    head_branch: str
    title: str
    body: str
    feedback: list[FeedbackNote] = field(default_factory=list)


@dataclass
class Record:
    outcome: Outcome
    hidden_reads: int


def ref_key(ref: SubjectRef | ReviewRef) -> str:
    return f"{ref.repo.identity.digest}:{ref.kind}:{ref.number}"


def commit_value(files: dict[str, Blob], parent: Sha | None, message: str) -> Commit:
    frozen = tuple(sorted(files.items()))
    raw = json.dumps(
        [parent, message, [(path, blob.mode, blob.sha) for path, blob in frozen]],
        separators=(",", ":"),
    ).encode()
    return Commit(sha1(raw, usedforsecurity=False).hexdigest(), parent, frozen, message)


class FakeState:
    def __init__(
        self, *, provider: Literal["github", "gitlab"] = "github", connection_id: str = "test"
    ) -> None:
        self.provider = provider
        self.connection_id = connection_id
        self.now = datetime(2000, 1, 1, tzinfo=UTC)
        self.repositories: dict[str, Repository] = {}
        self.tasks: dict[str, TaskSnapshot] = {}
        self.comments: dict[str, NativeComment] = {}
        self.drafts: dict[str, Draft] = {}
        self.lanes: dict[str, tuple[LaneHandle, LaneStatus]] = {}
        self.checks: dict[str, tuple[CheckResult, ...]] = {}
        self.approvers: set[tuple[str, str]] = set()
        self.records: dict[str, Record] = {}
        self.faults: dict[str, deque[Fault]] = {}
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.sequence = 0

    def repo(self, ref: RepoRef) -> Repository:
        if (
            ref.identity.provider != self.provider
            or ref.identity.connection_id != self.connection_id
        ):
            raise ReadUnavailable("repository outside connection", "fake:scope_refused")
        try:
            return self.repositories[ref.identity.digest]
        except KeyError as exc:
            raise ReadUnavailable("repository unavailable", "fake:missing_repository") from exc

    def seed_repository(
        self,
        ref: RepoRef,
        files: dict[str, Blob],
        *,
        default_branch: str = "main",
        protected: frozenset[str] = frozenset(),
    ) -> Sha:
        if (
            ref.identity.provider != self.provider
            or ref.identity.connection_id != self.connection_id
        ):
            raise ValueError("repository must belong to the fake connection")
        if ref.identity.digest in self.repositories:
            raise ValueError("repository already exists")
        for path in files:
            TypeAdapter(RepoPath).validate_python(path)
        commit = commit_value(dict(files), None, "seed")
        self.repositories[ref.identity.digest] = Repository(
            ref,
            default_branch,
            protected | {default_branch},
            {commit.sha: commit},
            {default_branch: commit.sha},
        )
        return commit.sha

    def fail_next(self, method: str, fault: Fault) -> None:
        writes = {
            "post_comment",
            "update_comment",
            "set_label",
            "ensure_branch",
            "commit",
            "ensure_draft",
            "post_review",
            "reply",
            "dispatch_lane",
        }
        if fault.conflict and method not in {"commit", "ensure_branch"}:
            raise ValueError("conflict injection requires a branch write")
        if method not in writes and (fault.unknown_after_success or fault.delay_visibility):
            raise ValueError("post-write visibility faults require a write operation")
        self.faults.setdefault(method, deque()).append(fault)

    def read_path(self, path: str, *, allow_empty: bool = False) -> None:
        if allow_empty and not path:
            return
        try:
            TypeAdapter(RepoPath).validate_python(path)
        except ValidationError as exc:
            raise ReadUnavailable("path outside repository", "fake:path_refused") from exc

    def start(self, method: str, *args: object) -> Fault:
        self.calls.append((method, args))
        return self.faults[method].popleft() if self.faults.get(method) else Fault()

    def read(self, method: str, *args: object) -> None:
        fault = self.start(method, *args)
        if fault != Fault():
            raise ReadUnavailable("injected read failure", "fake:http_503")

    def finish(self, key: str, outcome: T, fault: Fault) -> T | Unknown:
        self.records[key] = Record(cast(Outcome, outcome), fault.delay_visibility)
        return (
            Unknown(evidence="fake:accepted_response_lost")
            if fault.unknown_after_success
            else outcome
        )

    def probe(self, method: str, key: str, *args: object) -> ProbeResult[T]:
        if self.start(method, *args).http_5xx:
            return Inconclusive(reason="read unavailable", evidence="fake:http_503")
        record = self.records.get(key)
        if record is None:
            return DefinitelyAbsent(evidence="fake:authoritative_atomic_absence")
        if record.hidden_reads:
            record.hidden_reads -= 1
            return Inconclusive(reason="write still settling", evidence="fake:delayed_visibility")
        return Found[T](value=cast(T, record.outcome), evidence="fake:native_record")

    def next_id(self) -> str:
        self.sequence += 1
        return str(self.sequence)

    def bot_identity(self) -> Actor:
        return Actor(
            provider=self.provider,
            connection_id=self.connection_id,
            native_id="bot",
            login="forge-bot",
            is_bot=True,
        )

    async def is_approver(self, actor: Actor, subject: SubjectRef) -> bool:
        self.read("is_approver", actor, subject)
        self.repo(subject.repo)
        return (
            not actor.is_bot
            and actor.provider == self.provider
            and actor.connection_id == self.connection_id
            and (ref_key(subject), actor.native_id) in self.approvers
        )
