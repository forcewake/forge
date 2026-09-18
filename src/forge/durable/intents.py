"""PublicationIntent helpers: identity, state machine and the probe decision
table (R11, docs/research/remote-effect-reconciliation.md).

The intent row is the durable ``Idempotency-Key`` forge cannot get from the
git providers: minted once, persisted BEFORE the HTTP effect, and resolved
AFTER an ambiguous outcome by PROBING the remote by identity — the
``(forge-op:<key>)`` message marker (frozen contract, shared with the GitLab
writer) plus the intent-time expected parent OID. This module is the single
place that owns the key format, the marker format and the decision table, so
the GitLab writer and the GitHub/Azure DevOps lanes cannot drift.

Nothing here talks to a provider: probes are performed by the callers (they
own the clients) and classified through :func:`classify_probe` — the pure
decision table, unit-tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.durable.controller import as_aware_utc
from forge.durable.models import PublicationIntent

#: Open states — an intent whose effect may exist remotely but whose outcome
#: is not durably recorded yet. Every retry of the publication REUSES the
#: open intent's ``operation_key`` (same key forever) and probes before it
#: ever re-posts.
OPEN_STATES: tuple[str, ...] = ("requested", "dispatched", "probing")

#: Terminal states — no further transition is legal (mirrors ActionLog's
#: terminal immutability).
TERMINAL_STATES: tuple[str, ...] = ("committed", "adopted", "duplicated", "unknown", "failed")

#: Legal transitions (requested -> dispatched is the only exit of a fresh
#: intent; probing is the explicit "probe in progress" claim; dispatched ->
#: dispatched is the bounded SAME-KEY re-dispatch after a probe proved
#: nothing landed and the head intact). Anything not listed raises
#: :class:`InvalidIntentTransition`.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"dispatched", "failed"}),
    "dispatched": frozenset(
        {"committed", "adopted", "duplicated", "unknown", "failed", "probing", "dispatched"}
    ),
    "probing": frozenset({"adopted", "duplicated", "unknown", "failed"}),
}
for _terminal in TERMINAL_STATES:
    _LEGAL_TRANSITIONS[_terminal] = frozenset()


class InvalidIntentTransition(Exception):
    """The intent row may not move to the requested state (terminal or no edge)."""


def mint_operation_key() -> str:
    """A fresh operation key — minted ONCE per intent, never per dispatch.

    12 hex chars, the exact shape the GitLab writer has always stamped as
    ``(forge-op:<key>)``. Retries MUST reuse the intent's key (this is the
    R11 fix); only a genuinely new intent (new repair cycle, new content)
    mints a new one.
    """
    return uuid4().hex[:12]


def op_marker(operation_key: str) -> str:
    """The frozen commit-message marker contract: ``(forge-op:<key>)``.

    Substring-matched by every provider probe (GitHub REST list-commits has
    no trailer parsing, so a suffix works everywhere — research open
    question §1 resolved as "suffix, frozen").
    """
    return f"(forge-op:{operation_key})"


def message_with_marker(commit_message: str, operation_key: str) -> str:
    """The dispatch message: the human message plus this intent's marker."""
    return f"{commit_message} {op_marker(operation_key)}"


class ProbeVerdict(StrEnum):
    """What the remote probe proves about one intent (the decision table)."""

    #: Exactly one commit with this intent's marker AND the expected parents:
    #: this intent's effect landed — adopt it and advance the run.
    ADOPT = "adopt"
    #: Zero marker matches and the branch head still equals the expected
    #: parent: nothing landed — safe to (re)dispatch with the SAME key.
    REDISPATCH = "redispatch"
    #: Zero marker matches and the head moved (or the branch is gone):
    #: someone else owns the ref now — never force, never adopt.
    DUPLICATED = "duplicated"
    #: Two or more marker/parent matches (a double write or a key bug) —
    #: inconclusive; the run blocks on ``unknown_outcome``, never guesses.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProbeObservation:
    """The probe inputs, provider-shaped into one neutral tuple.

    ``marker_hits`` are the SHAs of branch commits whose message contains
    this intent's exact marker AND whose parent list equals the intent's
    expectation (root commit ⇒ empty parents). ``head_oid`` is the branch's
    current head; ``None`` means the branch has no commits (or is gone).
    """

    marker_hits: tuple[str, ...]
    head_oid: str | None
    expected_parent_oid: str | None


def classify_probe(observation: ProbeObservation) -> ProbeVerdict:
    """The R11 decision table (docs/research/remote-effect-reconciliation.md).

    | Observation                                   | Proof                      | Verdict     |
    |-----------------------------------------------|----------------------------|-------------|
    | exactly 1 marker+parent match                 | this attempt landed        | ADOPT       |
    | ≥2 matches                                    | double write / key bug     | UNKNOWN     |
    | 0 matches, head == expected parent            | nothing landed             | REDISPATCH  |
    | 0 matches, head moved / branch gone           | someone else owns the ref  | DUPLICATED  |
    """
    if len(observation.marker_hits) > 1:
        return ProbeVerdict.UNKNOWN
    if len(observation.marker_hits) == 1:
        return ProbeVerdict.ADOPT
    if observation.head_oid == observation.expected_parent_oid:
        # Includes the root-commit case: both None = the branch still has no
        # commits — exactly the state the intent expected.
        return ProbeVerdict.REDISPATCH
    return ProbeVerdict.DUPLICATED


def commit_matches(
    commits: list[dict],
    *,
    operation_key: str,
    expected_parent_oid: str | None,
) -> list[str]:
    """The ``marker_hits`` for :class:`ProbeObservation` from a provider's
    commit listing.

    *commits* is the provider-shaped list each client returns:
    ``{"sha"/"id"/"commit_id", "message"/"comment", "parent_ids"/"parents"}``
    — both GitLab's ``list_commits`` and the new GitHub/Azure listings, so
    the three lanes share one matcher. A commit counts ONLY when the exact
    marker is present AND its parents equal the intent-time expectation; the
    human message repeats across repair cycles and proves nothing (F07).
    """
    marker = op_marker(operation_key)
    expected_parents: list[str] = [] if expected_parent_oid is None else [expected_parent_oid]
    hits: list[str] = []
    for commit in commits:
        message = str(commit.get("message") or commit.get("comment") or "")
        if marker not in message:
            continue
        parents = commit.get("parent_ids")
        if parents is None:
            parents = commit.get("parents")
        if list(parents or []) != expected_parents:
            continue
        sha = str(commit.get("sha") or commit.get("id") or commit.get("commit_id") or "")
        if sha:
            hits.append(sha)
    return hits


async def find_open_intent(
    session: AsyncSession,
    *,
    run_id: str,
    provider: str,
    repo: str,
    target_ref: str,
    operation: str = "commit",
    idempotency_scope: str | None = None,
) -> PublicationIntent | None:
    """The open intent for this publication identity, newest first.

    ``idempotency_scope=None`` matches any scope: a crashed attempt's intent
    must be found regardless of the cycle counter a resumed walk derives
    (the durable row wins over re-derivation). None when every intent for
    the identity is terminal — the caller may mint a fresh one.
    """
    query = (
        select(PublicationIntent)
        .where(
            PublicationIntent.run_id == run_id,
            PublicationIntent.provider == provider,
            PublicationIntent.repo == repo,
            PublicationIntent.target_ref == target_ref,
            PublicationIntent.operation == operation,
            PublicationIntent.status.in_(OPEN_STATES),
        )
        .order_by(PublicationIntent.created_at.desc(), PublicationIntent.id.desc())
    )
    if idempotency_scope is not None:
        query = query.where(PublicationIntent.idempotency_scope == idempotency_scope)
    return (await session.execute(query.limit(1))).scalars().first()


async def record_intent(
    session: AsyncSession,
    *,
    run_id: str,
    provider: str,
    repo: str,
    target_ref: str,
    idempotency_scope: str,
    operation: str = "commit",
    operation_key: str | None = None,
    commit_cycle: int = 1,
    content_digest: str | None = None,
    expected_parent_oid: str | None = None,
    expected_head: str | None = None,
) -> PublicationIntent:
    """Insert the ``requested`` intent row (the intent-before-I/O write).

    The row is flushed into the CALLER's transaction — the same transaction
    that journals the ``action_log`` intent — so the effect can never start
    without a durable key (R11 invariant 1). ``operation_key`` is minted
    here exactly once when not supplied.
    """
    intent = PublicationIntent(
        run_id=run_id,
        provider=provider,
        repo=repo,
        operation=operation,
        target_ref=target_ref,
        idempotency_scope=idempotency_scope,
        operation_key=operation_key or mint_operation_key(),
        commit_cycle=commit_cycle,
        content_digest=content_digest,
        expected_parent_oid=expected_parent_oid,
        expected_head=expected_head,
        status="requested",
    )
    session.add(intent)
    await session.flush()
    return intent


async def mark_dispatched(
    session: AsyncSession,
    intent_id: str,
    *,
    expected_parent_oid: str | None = None,
    expected_head: str | None = None,
) -> PublicationIntent:
    """``requested → dispatched``: the caller is about to touch the remote.

    Stamps the intent-time branch head (the probe's parent expectation) when
    given and counts the attempt. Flushes into the caller's transaction; the
    caller commits BEFORE the HTTP effect so a crash leaves the intent
    findable by the recovery scanner.
    """
    intent = await session.get(PublicationIntent, intent_id)
    if intent is None:
        raise InvalidIntentTransition(f"publication intent {intent_id} not found")
    if "dispatched" not in _LEGAL_TRANSITIONS[intent.status]:
        raise InvalidIntentTransition(
            f"publication intent {intent_id} is {intent.status!r}; only a 'requested' "
            "intent can be dispatched"
        )
    if expected_parent_oid is not None or expected_head is not None:
        intent.expected_parent_oid = (
            expected_parent_oid if expected_parent_oid is not None else intent.expected_parent_oid
        )
        intent.expected_head = expected_head if expected_head is not None else intent.expected_head
    intent.status = "dispatched"
    intent.attempt_count = int(intent.attempt_count) + 1
    intent.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return intent


async def complete_intent(
    session: AsyncSession,
    intent_id: str,
    status: str,
    *,
    provider_object_id: str | None = None,
    remote_result: dict | None = None,
) -> PublicationIntent:
    """Record the outcome of an intent exactly once (terminal is final).

    ``committed`` = this dispatch's own response arrived; ``adopted`` = a
    probe found a PREVIOUS attempt's effect (the durable result is filled in
    from the probe); ``duplicated`` = the ref moved away from this intent;
    ``unknown`` = inconclusive — the caller must block the run (ADR-0005);
    ``failed`` = a deterministic provider rejection. Flushes into the
    caller's transaction.
    """
    intent = await session.get(PublicationIntent, intent_id)
    if intent is None:
        raise InvalidIntentTransition(f"publication intent {intent_id} not found")
    if status not in _LEGAL_TRANSITIONS[intent.status]:
        raise InvalidIntentTransition(
            f"publication intent transition {intent.status!r} -> {status!r} is not legal "
            "(terminal rows are immutable)"
        )
    intent.status = status
    if provider_object_id is not None:
        intent.provider_object_id = provider_object_id
    if remote_result is not None:
        intent.remote_result = remote_result
    intent.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return intent


async def due_intents(
    session: AsyncSession,
    *,
    provider: str,
    repo: str | None = None,
    now: datetime | None = None,
    limit: int = 25,
) -> list[PublicationIntent]:
    """The open intents the recovery scanner owes a probe, oldest first.

    ``next_probe_at`` NULL means due immediately (a just-crashed attempt);
    a set value is the jittered backoff after an inconclusive probe pass.
    """
    query = (
        select(PublicationIntent)
        .where(
            PublicationIntent.provider == provider,
            PublicationIntent.status.in_(OPEN_STATES),
        )
        .order_by(PublicationIntent.created_at.asc())
        .limit(limit)
    )
    if repo is not None:
        query = query.where(PublicationIntent.repo == repo)
    rows = (await session.execute(query)).scalars().all()
    if now is None:
        return list(rows)
    now = as_aware_utc(now)
    return [
        row for row in rows if row.next_probe_at is None or as_aware_utc(row.next_probe_at) <= now
    ]
