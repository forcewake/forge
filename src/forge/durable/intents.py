"""Record publication intent before effects and preserve unresolved outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.durable.controller import as_aware_utc
from forge.durable.models import PublicationIntent

#: Open states — an intent whose effect may exist remotely but whose outcome is not durably
#: recorded yet.
OPEN_STATES: tuple[str, ...] = ("requested", "dispatched", "probing")

#: Terminal states — no further transition is legal (mirrors ActionLog's terminal
#: immutability).
TERMINAL_STATES: tuple[str, ...] = ("committed", "adopted", "duplicated", "unknown", "failed")

#: Legal transitions (requested -> dispatched is the only exit of a fresh intent; probing is
#: the effect-certainty state — a negative probe's bounded settle window; dispatched ->
#: dispatched is the bounded SAME-KEY re-dispatch after the certainty window expired with
#: the probe still negative on a CAS-protected provider).
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"dispatched", "failed"}),
    "dispatched": frozenset(
        {"committed", "adopted", "duplicated", "unknown", "failed", "probing", "dispatched"}
    ),
    "probing": frozenset({"adopted", "duplicated", "unknown", "failed", "dispatched"}),
}
for _terminal in TERMINAL_STATES:
    _LEGAL_TRANSITIONS[_terminal] = frozenset()


class InvalidIntentTransition(Exception):
    """The intent row may not move to the requested state (terminal or no edge)."""


# effect certainty — a negative probe is not proof of absence
#

#: The certainty window opened by the FIRST negative probe (config knob
#: ``FORGE_PUBLISH_SETTLE_SECONDS``; lanes pass their configured value in).
DEFAULT_SETTLE_WINDOW_SECONDS = 30

#: Each still-negative window-end re-probe backs the next window off by this factor (30s →
#: 60s → 120s …) — cheap reads, bounded growth.
SETTLE_BACKOFF_FACTOR = 2

#: The certainty budget: after this many settle windows the negative reads are as good as
#: absence gets on a provider without native preconditions, and the honest outcome is the
#: conservative park (``PARK_UNKNOWN``).
MAX_SETTLE_ROUNDS = 3

CAS_PROTECTED_PROVIDERS: frozenset[str] = frozenset({"github"})


class RedispatchGuarantee(StrEnum):
    """What makes a same-key re-dispatch safe on this adapter."""

    #: A branch-wide CAS write: the redispatch itself refuses a duplicate (the slow first
    #: write would have moved the head).
    CAS_PROTECTED = "cas_protected"
    #: No native precondition: safety must be EARNED by the certainty window, and even then
    #: only the park is honest when it expires negative.
    UNPROTECTED = "unprotected"


def redispatch_guarantee(provider: str) -> RedispatchGuarantee:
    """The guarantee table for *provider* (``PublicationIntent.provider``)."""
    if provider in CAS_PROTECTED_PROVIDERS:
        return RedispatchGuarantee.CAS_PROTECTED
    return RedispatchGuarantee.UNPROTECTED


class SettleDecision(StrEnum):
    """What :func:`settle_negative_probe` decided for a negative probe."""

    #: The certainty window is open (or was just opened/extended) — no dispatch; the
    #: re-probe happens at the window end.
    WAIT = "wait"
    #: The window expired still-negative on a CAS-protected provider — the same-key
    #: redispatch is inherently safe (the CAS refuses a duplicate).
    REDISPATCH = "redispatch"
    #: The window expired still-negative on a provider with no branch-wide CAS — park
    #: blocked(unknown_outcome) with operator instructions; never guess, never re-dispatch.
    PARK_UNKNOWN = "park_unknown"


@dataclass(frozen=True)
class SettleState:
    """The persisted settle bookkeeping (``remote_result['settle']``)."""

    rounds: int = 0
    window_seconds: int = DEFAULT_SETTLE_WINDOW_SECONDS
    opened_at: datetime | None = None
    reprobe_at: datetime | None = None


def settle_state(intent: PublicationIntent) -> SettleState:
    """The intent's settle bookkeeping, defensive about foreign JSON."""
    block = (intent.remote_result or {}).get("settle")
    if not isinstance(block, dict):
        return SettleState()
    rounds = block.get("rounds")
    window = block.get("window_seconds")
    opened = block.get("opened_at")
    reprobe = block.get("reprobe_at")
    return SettleState(
        rounds=int(rounds) if isinstance(rounds, (int, float)) else 0,
        window_seconds=(
            int(window) if isinstance(window, (int, float)) else DEFAULT_SETTLE_WINDOW_SECONDS
        ),
        opened_at=_parse_ts(opened),
        reprobe_at=_parse_ts(reprobe),
    )


def settle_state_record(intent: PublicationIntent) -> dict:
    """The settle bookkeeping as a JSON-safe dict (journal/evidence shaped)."""
    state = settle_state(intent)
    return {
        "rounds": state.rounds,
        "window_seconds": state.window_seconds,
        "opened_at": state.opened_at.isoformat() if state.opened_at else None,
        "reprobe_at": state.reprobe_at.isoformat() if state.reprobe_at else None,
    }


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_aware_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def settle_expired(intent: PublicationIntent, *, now: datetime) -> bool:
    """Whether *intent*'s certainty window has run out (re-probe is due)."""
    if intent.status != "probing":
        return False
    return intent.next_probe_at is None or as_aware_utc(intent.next_probe_at) <= as_aware_utc(now)


def _settle_block(rounds: int, window_seconds: int, opened_at: datetime, now: datetime) -> dict:
    return {
        "rounds": rounds,
        "window_seconds": window_seconds,
        "opened_at": opened_at.isoformat(),
        "reprobe_at": (now + timedelta(seconds=window_seconds)).isoformat(),
    }


async def settle_negative_probe(
    session: AsyncSession,
    intent: PublicationIntent,
    *,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_SETTLE_WINDOW_SECONDS,
    max_rounds: int = MAX_SETTLE_ROUNDS,
) -> SettleDecision:
    """The machine for a REDISPATCH verdict — call it on every negative probe."""
    now = as_aware_utc(now) if now is not None else datetime.now(timezone.utc)
    state = settle_state(intent)
    if intent.status == "dispatched":
        return await _open_settle(session, intent, rounds=0, window_seconds=window_seconds, now=now)
    if intent.status != "probing":
        raise InvalidIntentTransition(
            f"publication intent {intent.id} is {intent.status!r}; the settle machine "
            "applies to a dispatched or probing intent"
        )
    if not settle_expired(intent, now=now):
        return SettleDecision.WAIT
    if state.rounds + 1 < max_rounds:
        backed_off = max(window_seconds, state.window_seconds) * SETTLE_BACKOFF_FACTOR
        return await _open_settle(
            session, intent, rounds=state.rounds + 1, window_seconds=backed_off, now=now
        )
    if redispatch_guarantee(intent.provider) is RedispatchGuarantee.CAS_PROTECTED:
        # The guarantee does the work: the redispatch carries expected_parent == the
        # unchanged head, so a slow first write would move the head and the CAS would refuse
        # the duplicate.
        await mark_dispatched(session, intent.id)
        return SettleDecision.REDISPATCH
    return SettleDecision.PARK_UNKNOWN


async def _open_settle(
    session: AsyncSession,
    intent: PublicationIntent,
    *,
    rounds: int,
    window_seconds: int,
    now: datetime,
) -> SettleDecision:
    """Park the intent in ``probing`` for one certainty window (enter or extend)."""
    row = await session.get(PublicationIntent, intent.id)
    if row is None:
        raise InvalidIntentTransition(f"publication intent {intent.id} not found")
    if row.status not in ("dispatched", "probing"):
        raise InvalidIntentTransition(
            f"publication intent {intent.id} is {row.status!r}; only an open dispatch "
            "can enter the settle window"
        )
    row.status = "probing"
    row.next_probe_at = now + timedelta(seconds=window_seconds)
    remote = dict(row.remote_result or {})
    remote["settle"] = _settle_block(rounds, window_seconds, now, now)
    row.remote_result = remote
    row.updated_at = now
    await session.flush()
    return SettleDecision.WAIT


def mint_operation_key() -> str:
    """A fresh operation key — minted ONCE per intent, never per dispatch."""
    return uuid4().hex[:12]


def op_marker(operation_key: str) -> str:
    """The frozen commit-message marker contract: ``(forge-op:<key>)``."""
    return f"(forge-op:{operation_key})"


def message_with_marker(commit_message: str, operation_key: str) -> str:
    """The dispatch message: the human message plus this intent's marker."""
    return f"{commit_message} {op_marker(operation_key)}"


class ProbeVerdict(StrEnum):
    """What the remote probe proves about one intent (the decision table)."""

    #: Exactly one commit with this intent's marker AND the expected parents: this intent's
    #: effect landed — adopt it and advance the run.
    ADOPT = "adopt"
    #: Zero marker matches and the branch head still equals the expected parent: nothing
    #: landed — safe to (re)dispatch with the SAME key.
    REDISPATCH = "redispatch"
    #: Zero marker matches and the head moved (or the branch is gone): someone else owns the
    #: ref now — never force, never adopt.
    DUPLICATED = "duplicated"
    #: Two or more marker/parent matches (a double write or a key bug) — inconclusive; the
    #: run blocks on ``unknown_outcome``, never guesses.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProbeObservation:
    """The probe inputs, provider-shaped into one neutral tuple."""

    marker_hits: tuple[str, ...]
    head_oid: str | None
    expected_parent_oid: str | None


def classify_probe(observation: ProbeObservation) -> ProbeVerdict:
    """The decision table (docs/research/2026-09-17-remote-effect-reconciliation.md)."""
    if len(observation.marker_hits) > 1:
        return ProbeVerdict.UNKNOWN
    if len(observation.marker_hits) == 1:
        return ProbeVerdict.ADOPT
    if observation.head_oid == observation.expected_parent_oid:
        # Includes the root-commit case: both None = the branch still has no commits —
        # exactly the state the intent expected.
        return ProbeVerdict.REDISPATCH
    return ProbeVerdict.DUPLICATED


def commit_matches(
    commits: list[dict],
    *,
    operation_key: str,
    expected_parent_oid: str | None,
) -> list[str]:
    """The ``marker_hits`` for:class:`ProbeObservation` from a provider's commit listing."""
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
    """The open intent for this publication identity, newest first."""
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
    """Insert the ``requested`` intent row (the intent-before-I/O write)."""
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
    """``requested → dispatched`` (or the release ``probing → dispatched``): the caller is about to touch the remote."""
    intent = await session.get(PublicationIntent, intent_id)
    if intent is None:
        raise InvalidIntentTransition(f"publication intent {intent_id} not found")
    if "dispatched" not in _LEGAL_TRANSITIONS[intent.status]:
        raise InvalidIntentTransition(
            f"publication intent {intent_id} is {intent.status!r}; only a 'requested' or "
            "'probing' intent can be dispatched"
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
    """Record the outcome of an intent exactly once (terminal is final)."""
    intent = await session.get(PublicationIntent, intent_id)
    if intent is None:
        raise InvalidIntentTransition(f"publication intent {intent_id} not found")
    if status not in _LEGAL_TRANSITIONS[intent.status]:
        # Terminal rows are immutable — but a RE-publication of an effect that is already
        # terminally recorded is not a conflict: e.g. a repair cycle force-pushed the same
        # candidate, the probe had already ``adopted`` the commit, and the direct push then
        # answers ok.
        if (
            intent.status in TERMINAL_STATES
            and intent.provider_object_id
            and (intent.provider_object_id == provider_object_id)
        ):
            return intent
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
    """The open intents the recovery scanner owes a probe, oldest first."""
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
