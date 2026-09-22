"""Coordinated multi-repo publication as a recoverable saga (NXT-24).

The review's MRP-04/MRP-07 gap: "multi-repository coordination" phased
CALLS, not finished work — and a lost publish response in one child
could not be reconciled without either duplicating the effect or
pretending it away. This module is the saga layer the review asks for:

- **persisted intents before effects** — every per-repo publication is a
  :class:`RepoPublication` value whose commit intent
  (``intent_recorded``, with the branch and the ``expected_base_oid``
  the saga builds on) is PERSISTED through the :class:`SagaStore`
  BEFORE the provider call. The durable pattern is the one
  ``mr_reservations`` / migration 019 established in ``runs/``: the
  logical intent is committed first, a concurrent or recovering worker
  ADOPTS the recorded effect before ever creating a second one, and a
  read that proves nothing (provider outage) fails CLOSED — no create
  on an unknown surface.
- **crash recovery adopts or supersedes, never double-creates** — a
  saga interrupted between intent and commit resumes from its persisted
  intents: remote head still at the expected base means nothing landed
  (re-issue the SAME intent); a head carrying the saga's
  :attr:`~PublicationSaga.commit_marker` is OUR late-landed effect
  (adopt it); anything else is a HUMAN edit.
- **human-edit protection** — the saga never deletes or force-pushes a
  branch a human touched. Before superseding or re-issuing, the remote
  head is compared against the saga's expected base: a moved head the
  saga's marker cannot account for PARKS that repository for a human
  decision (:func:`park_for_human`) — the branch is left exactly as
  the human left it, never force-overwritten.
- **superseded-not-pretend-undone** — effects a provider already
  accepted STAND and stay reported
  (:attr:`~PublicationSaga.effects_standing`); a fenced or superseded
  saga never claims rollback it cannot perform. The bot still never
  merges: ``human_merged`` is an OBSERVED human outcome
  (:func:`observe_human_merge`), the only way that status is reached.

Per-repo step order: ``prepare -> fence-check -> commit-intent ->
provider commit -> verify -> record`` — each hop appending to the
saga's ``steps`` journal, whose whole-digest
(:attr:`~PublicationSaga.steps_digest`) is pinned by tests so a step
cannot silently disappear from the record.

The value objects are frozen (transitions return new instances; the
caller's store is the durability), and :class:`SagaCoordinator` is the
async orchestrator that keeps the write-ahead ordering honest: state is
saved BEFORE every provider effect.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

__all__ = [
    "InMemorySagaStore",
    "PublicationProvider",
    "PublicationSaga",
    "ProviderRejectedError",
    "ProviderUnavailableError",
    "RepoPublication",
    "RepoPublicationStatus",
    "SagaCoordinator",
    "SagaStatus",
    "SagaStore",
    "adopt_remote_effect",
    "begin_saga",
    "fence_check",
    "observe_human_merge",
    "outcome_unknown",
    "park_for_human",
    "provider_committed",
    "provider_failed",
    "record_commit_intent",
    "review_opened",
    "verified",
]

#: One repository's journey through a saga. ``preparing``,
#: ``intent_recorded``, ``committed``, ``verified``,
#: ``ready_for_review``, ``human_merged`` are the climbing rungs;
#: ``failed`` (the provider definitively refused — retryable by an
#: explicit new attempt, prior WIP retained), ``outcome_unknown`` (the
#: lost-response window: the request may or may not have landed —
#: recovery adopts or re-issues, never blindly retries) and
#: ``parked_human`` (a human edit stands in the way — a decision, not a
#: workaround) are per-repo outcomes; ``superseded`` is the fence's
#: verdict on an intent that never became an effect.
RepoPublicationStatus = Literal[
    "preparing",
    "intent_recorded",
    "committed",
    "verified",
    "ready_for_review",
    "human_merged",
    "failed",
    "outcome_unknown",
    "parked_human",
    "superseded",
]

#: The saga-level status, DERIVED from the per-repo states — the parent
#: never claims more than its children prove. ``complete`` — every repo
#: published (PR ready for review or observed human-merged; the bot
#: never merges). ``parked`` / ``failed`` — at least one repo needs a
#: human decision / has unresolved effects (failed or unknown) while
#: the successful repos' PRs stay standing and reported.
#: ``fenced`` — a pause/cancel bumped the publication epoch past the
#: saga's grant; not-yet-committed intents died, committed effects
#: stand. ``running`` — anything still climbing.
SagaStatus = Literal["running", "complete", "parked", "failed", "fenced"]

#: Statuses whose effect a provider already accepted — they STAND
#: through fences, failures and supersession (never pretend-undone).
_STANDING: Final = frozenset({"committed", "verified", "ready_for_review", "human_merged"})

#: The statuses recovery must reconcile (the intent exists; the effect
#: window is open — crash, lost response, or unverified commit).
_RECONCILE: Final = frozenset({"intent_recorded", "outcome_unknown"})


class ProviderUnavailableError(Exception):
    """The provider surface could not be OBSERVED (outage, auth, 5xx).

    The fail-closed signal (the mr_reservations doctrine): a read that
    proves nothing permits no create. The coordinator raises it out of
    the run — the saga stays persisted at its last state and is
    retried later; nothing is created on an unknown surface.
    """


class ProviderRejectedError(Exception):
    """The provider DEFINITIVELY refused an effect (a 4xx-style answer).

    Unlike an outage this proves the effect did not land: the repo
    books ``failed`` (retryable only through an explicit new attempt)
    and the saga CONTINUES — one repository's refusal must not
    resubmit or discard the others (NXT-24: never resubmit all children
    after one failure).
    """


@dataclass(frozen=True)
class RepoPublication:
    """One repository's persisted publication intent and outcome.

    ``expected_base_oid`` is the head the saga builds on — the value
    EVERY supersede/re-issue decision compares the remote head against
    (the human-edit protection). ``head_oid`` is the head AFTER the
    saga's own commit (filled by the provider or by adoption).
    ``review_url`` is the opened PR — the bot opens reviews; it never
    merges. ``adopted`` marks an effect recovery found already landed
    (the crash window closed by evidence, not by a duplicate create).
    """

    repository_id: str
    branch: str
    expected_base_oid: str
    status: RepoPublicationStatus = "preparing"
    head_oid: str | None = None
    review_url: str | None = None
    adopted: bool = False
    note: str = ""


@dataclass(frozen=True)
class PublicationSaga:
    """The parent: a fixed per-repo plan, one journal, a derived status.

    ``publication_epoch`` is the fence the whole saga runs under (the
    grant's epoch — :func:`forge.adaptive.control.new_publication_epoch`
    bumps it on pause/cancel); ``candidate_digest`` identifies the
    verified candidate set being published, so a PR opened by this saga
    is traceable to exactly the tested world.
    """

    saga_id: str
    work_id: str
    publication_epoch: int
    candidate_digest: str
    repos: tuple[RepoPublication, ...]
    steps: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.repos:
            raise ValueError("a saga with no repositories is no saga — refuse the empty fan-out")
        ids = [repo.repository_id for repo in self.repos]
        if len(ids) != len(set(ids)):
            raise ValueError(f"a repository appears twice in one saga: {sorted(ids)}")
        for repo in self.repos:
            if not repo.branch or not repo.expected_base_oid:
                raise ValueError(
                    f"repo {repo.repository_id!r} needs a branch and an expected base oid — "
                    "an intent without its comparison anchor cannot protect human edits"
                )

    # -- identity --------------------------------------------------------

    @property
    def commit_marker(self) -> str:
        """The idempotency marker every effect of THIS saga carries.

        The adopt-or-supersede decision turns on it: a remote head
        carrying the marker is OUR late-landed effect (adopt); a moved
        head without it is a HUMAN edit (park).
        """
        return f"forge-saga:{self.saga_id}:{self.candidate_digest[:16]}"

    def repo(self, repository_id: str) -> RepoPublication | None:
        return next((r for r in self.repos if r.repository_id == repository_id), None)

    # -- derived state -----------------------------------------------------

    @property
    def status(self) -> SagaStatus:
        statuses = {repo.status for repo in self.repos}
        if statuses <= {"ready_for_review", "human_merged"}:
            return "complete"
        if "parked_human" in statuses:
            return "parked"
        if "failed" in statuses or "outcome_unknown" in statuses:
            return "failed"
        if "superseded" in statuses:
            return "fenced"
        return "running"

    def effects_standing(self) -> tuple[RepoPublication, ...]:
        """The repos whose provider-accepted effects STAND — the honest
        report a fenced/superseded/failed saga gives instead of claiming
        rollback (superseded-not-pretend-undone)."""
        return tuple(repo for repo in self.repos if repo.status in _STANDING)

    def unresolved(self) -> tuple[RepoPublication, ...]:
        """The repos the parent cannot call finished: undecided intents,
        definitive failures, unknown outcomes, and human-parked branches."""
        return tuple(
            repo
            for repo in self.repos
            if repo.status in _RECONCILE or repo.status in {"preparing", "failed", "parked_human"}
        )

    @property
    def steps_digest(self) -> str:
        """The whole-digest of the journaled steps.

        A stable hash over the COMPLETE step journal (canonical JSON,
        sorted keys) — the value a test or an audit pins so a step
        cannot silently vanish from the record between two runs of the
        same history.
        """
        canonical = json.dumps(
            [dict(step) for step in self.steps], sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pure per-repo transitions — each returns a new saga, journals its step
# ---------------------------------------------------------------------------


def _transition(
    saga: PublicationSaga,
    repository_id: str,
    step: str,
    to_status: str,
    *,
    from_statuses: frozenset[str],
    **detail: object,
) -> PublicationSaga:
    """One guarded per-repo transition with its journaled step."""
    repo = saga.repo(repository_id)
    if repo is None:
        raise KeyError(f"unknown repository {repository_id!r} in saga {saga.saga_id!r}")
    if repo.status not in from_statuses:
        raise ValueError(
            f"repo {repository_id!r} is {repo.status!r}; the publication ladder refuses "
            f"to move to {to_status!r} from anything but "
            f"{' or '.join(sorted(repr(s) for s in from_statuses))}"
        )
    moved = replace(
        repo,
        status=to_status,  # type: ignore[arg-type]
        # The detail kwargs come from this module's typed call sites
        # (head_oid/review_url/note as str, adopted as bool); the closed
        # construction is the type proof the unpack cannot give mypy.
        **cast(
            "Any",
            {
                key: value
                for key, value in detail.items()
                if key in RepoPublication.__dataclass_fields__ and value is not None
            },
        ),
    )
    entry: dict[str, str] = {
        "repo": repository_id,
        "step": step,
        "from": repo.status,
        "to": to_status,
    }
    entry.update({key: str(value) for key, value in detail.items() if value is not None})
    repos = tuple(moved if r.repository_id == repository_id else r for r in saga.repos)
    return replace(saga, repos=repos, steps=saga.steps + (entry,))


def begin_saga(
    work_id: str,
    publication_epoch: int,
    candidate_digest: str,
    repo_plans: Mapping[str, tuple[str, str]],
) -> PublicationSaga:
    """Prepare the per-repo intents: one :class:`RepoPublication` per
    repository (branch + expected base), each journaled at ``prepare``."""
    repos = tuple(
        RepoPublication(repository_id=repository_id, branch=branch, expected_base_oid=expected_base)
        for repository_id, (branch, expected_base) in repo_plans.items()
    )
    saga = PublicationSaga(
        saga_id=f"saga-{uuid.uuid4().hex[:12]}",
        work_id=work_id,
        publication_epoch=publication_epoch,
        candidate_digest=candidate_digest,
        repos=repos,
    )
    # The prepare step is journaled per repo — the record of WHAT the
    # saga set out to do on each branch before any fence or intent.
    entries = tuple(
        {"repo": repo.repository_id, "step": "prepare", "to": "preparing"} for repo in repos
    )
    return replace(saga, steps=saga.steps + entries)


def record_commit_intent(saga: PublicationSaga, repository_id: str) -> PublicationSaga:
    """``preparing -> intent_recorded`` — the write-ahead leg.

    The caller persists the returned saga BEFORE the provider call:
    this row IS the reservation whose marker-keyed comparison makes
    recovery adopt-or-reissue instead of double-create.
    """
    return _transition(
        saga,
        repository_id,
        "commit_intent",
        "intent_recorded",
        from_statuses=frozenset({"preparing"}),
    )


def provider_committed(
    saga: PublicationSaga, repository_id: str, head_oid: str, *, adopted: bool = False
) -> PublicationSaga:
    """The provider accepted the commit (``intent_recorded ->
    committed``), or recovery adopted the late-landed effect."""
    step = "provider_commit" if not adopted else "adopt"
    return _transition(
        saga,
        repository_id,
        step,
        "committed",
        from_statuses=frozenset({"intent_recorded", "outcome_unknown"}),
        head_oid=head_oid,
        adopted=adopted or None,
    )


def verified(saga: PublicationSaga, repository_id: str) -> PublicationSaga:
    """``committed -> verified`` — the remote head read back as the
    committed head, carrying this saga's marker."""
    return _transition(
        saga, repository_id, "verify", "verified", from_statuses=frozenset({"committed"})
    )


def review_opened(saga: PublicationSaga, repository_id: str, review_url: str) -> PublicationSaga:
    """``verified -> ready_for_review`` — the PR is open; a human reviews
    and merges. The bot never merges."""
    return _transition(
        saga,
        repository_id,
        "record",
        "ready_for_review",
        from_statuses=frozenset({"verified"}),
        review_url=review_url,
    )


def observe_human_merge(saga: PublicationSaga, repository_id: str) -> PublicationSaga:
    """``ready_for_review -> human_merged`` — an OBSERVED human outcome.

    The only route to ``human_merged``: a human merged the PR. The saga
    records the observation; it never performs or triggers the merge.
    """
    return _transition(
        saga,
        repository_id,
        "human_merged",
        "human_merged",
        from_statuses=frozenset({"ready_for_review"}),
    )


def provider_failed(saga: PublicationSaga, repository_id: str, reason: str) -> PublicationSaga:
    """A definitive provider refusal (``failed``) — retryable only by an
    explicit new attempt; the prior WIP (branch, PR, unpushed commits)
    is retained, never deleted as compensation."""
    return _transition(
        saga,
        repository_id,
        "provider_failed",
        "failed",
        from_statuses=frozenset({"intent_recorded", "committed", "verified"}),
        note=reason,
    )


def outcome_unknown(saga: PublicationSaga, repository_id: str, reason: str) -> PublicationSaga:
    """The lost-response window (``outcome_unknown``): the request may
    or may not have landed. Recovery reconciles by evidence — never a
    blind retry."""
    return _transition(
        saga,
        repository_id,
        "outcome_unknown",
        "outcome_unknown",
        from_statuses=frozenset({"intent_recorded", "verified"}),
        note=reason,
    )


def park_for_human(saga: PublicationSaga, repository_id: str, reason: str) -> PublicationSaga:
    """Park this repository for a HUMAN decision.

    The human-edit protection's verdict: the remote head moved past the
    saga's expected base without this saga's marker — a person's work
    is on that branch. The saga leaves the branch EXACTLY as the human
    left it (no delete, no force-push, no overwrite) and routes the
    conflict to a decision instead of a workaround.
    """
    repo = saga.repo(repository_id)
    if repo is None:
        raise KeyError(f"unknown repository {repository_id!r} in saga {saga.saga_id!r}")
    moved = replace(repo, status="parked_human", note=reason)
    entry: dict[str, str] = {
        "repo": repository_id,
        "step": "park",
        "from": repo.status,
        "to": "parked_human",
        "reason": reason,
    }
    return replace(
        saga,
        repos=tuple(moved if r.repository_id == repository_id else r for r in saga.repos),
        steps=saga.steps + (entry,),
    )


def adopt_remote_effect(
    saga: PublicationSaga, repository_id: str, head_oid: str
) -> PublicationSaga:
    """Adopt an effect recovery found already landed.

    The remote head carries THIS saga's commit marker, so the effect is
    ours — the crash window closes by EVIDENCE (adopt), never by a
    duplicate create and never by discarding the landing. Reachable
    from ``intent_recorded`` / ``outcome_unknown``.
    """
    return provider_committed(saga, repository_id, head_oid, adopted=True)


def fence_check(saga: PublicationSaga, current_publication_epoch: int) -> PublicationSaga:
    """The CTL-05/CTL-08 fence, applied per repository.

    A pause or cancel bumped the publication epoch past the saga's
    grant (``current > saga.publication_epoch``): every intent that
    never became an effect dies as ``superseded`` — but effects a
    provider already accepted STAND and stay reported
    (:func:`PublicationSaga.effects_standing`): superseded means "the
    new generation moves on without chasing it", never "we took it
    back". An equal-or-older epoch changes nothing (the honest no-op —
    a redelivered fence must not spend a second supersession).
    """
    if current_publication_epoch <= saga.publication_epoch:
        return saga
    entries: list[dict[str, str]] = []
    repos: list[RepoPublication] = []
    for repo in saga.repos:
        if repo.status in _RECONCILE or repo.status == "preparing":
            repos.append(
                replace(
                    repo,
                    status="superseded",
                    note=(
                        f"publication epoch moved {saga.publication_epoch} -> "
                        f"{current_publication_epoch} before the effect landed; the "
                        "intent is dead, no effect was created"
                    ),
                )
            )
            entries.append(
                {
                    "repo": repo.repository_id,
                    "step": "superseded",
                    "from": repo.status,
                    "to": "superseded",
                    "epoch": str(current_publication_epoch),
                }
            )
        else:
            repos.append(repo)
    return replace(saga, repos=tuple(repos), steps=saga.steps + tuple(entries))


# ---------------------------------------------------------------------------
# The seams: provider I/O and saga persistence
# ---------------------------------------------------------------------------


@runtime_checkable
class PublicationProvider(Protocol):
    """The remote surface the saga publishes through.

    Deliberately minimal and marker-keyed: every method can be answered
    from branch state plus the commit-trailer marker, which is exactly
    what the adopt-or-park decision needs. There is NO merge, NO
    force-push and NO branch-delete method — the protocol cannot
    express the effects NXT-24 forbids.
    """

    async def remote_head(self, repository_id: str, branch: str) -> str:
        """The branch's current head oid (fail with
        :class:`ProviderUnavailableError` when the surface is unreadable)."""
        ...

    async def head_carries_marker(self, repository_id: str, branch: str, marker: str) -> bool:
        """Whether the branch's history contains a commit carrying ``marker``."""
        ...

    async def commit(self, repository_id: str, branch: str, marker: str) -> str:
        """Commit the candidate on the branch; return the new head oid.

        Implementations MUST be idempotent per ``(branch, marker)`` — a
        retried saga re-issues the same intent and must not create a
        second commit for it.
        """
        ...

    async def open_review(self, repository_id: str, branch: str, marker: str) -> str:
        """Open (or return the already-open) review for the marker; the URL."""
        ...


@runtime_checkable
class SagaStore(Protocol):
    """The persistence seam: the write-ahead log of saga state.

    ``save`` is called BEFORE every provider effect (the intent) and
    AFTER every outcome — a crash always leaves the last honest state.
    """

    async def save(self, saga: PublicationSaga) -> None: ...

    async def load(self, saga_id: str) -> PublicationSaga | None: ...


class InMemorySagaStore:
    """The reference store (tests, reasoning): last-write-wins per id."""

    def __init__(self) -> None:
        self._sagas: dict[str, PublicationSaga] = {}

    async def save(self, saga: PublicationSaga) -> None:
        self._sagas[saga.saga_id] = saga

    async def load(self, saga_id: str) -> PublicationSaga | None:
        return self._sagas.get(saga_id)


# ---------------------------------------------------------------------------
# The coordinator — keeps the write-ahead ordering honest
# ---------------------------------------------------------------------------


class SagaCoordinator:
    """Drives and recovers sagas: intent persisted, THEN the effect.

    The ordering is the whole point (the mr_reservations lesson): the
    store write precedes every provider call, so an interrupted saga
    always resumes from a row that says what was INTENDED — and the
    marker-keyed reconciliation decides adopt / re-issue / park from
    evidence, never from a guess.
    """

    def __init__(self, store: SagaStore, provider: PublicationProvider) -> None:
        self._store = store
        self._provider = provider

    async def run(
        self, saga: PublicationSaga, *, current_publication_epoch: int
    ) -> PublicationSaga:
        """Drive every repo to a decided state (or an honest stop).

        Already-decided repos are never resubmitted — one repository's
        failure does not re-run the others (NXT-24). A
        :class:`ProviderUnavailableError` propagates: the pass stops
        fail-closed with the saga persisted at its last honest state.
        """
        saga = fence_check(saga, current_publication_epoch)
        await self._store.save(saga)
        for repo in saga.repos:
            current = saga.repo(repo.repository_id)
            assert current is not None  # __post_init__ guarantees membership
            if current.status == "preparing":
                saga = await self._publish(saga, current.repository_id)
            elif current.status in _RECONCILE:
                saga = await self._reconcile(saga, current.repository_id)
            elif current.status == "committed":
                saga = await self._verify_and_record(saga, current.repository_id)
            elif current.status == "verified":
                saga = await self._record_review(saga, current.repository_id)
            # decided rungs (ready_for_review, human_merged, failed,
            # parked_human, superseded) are untouched — a re-run spends
            # nothing on them.
        return saga

    async def recover(
        self, saga_id: str, *, current_publication_epoch: int
    ) -> PublicationSaga | None:
        """Resume an interrupted saga from its persisted intents."""
        saga = await self._store.load(saga_id)
        if saga is None:
            return None
        return await self.run(saga, current_publication_epoch=current_publication_epoch)

    # -- the per-repo steps -------------------------------------------------

    async def _publish(self, saga: PublicationSaga, repository_id: str) -> PublicationSaga:
        """fence-check -> commit-intent (persisted) -> provider commit."""
        repo = saga.repo(repository_id)
        assert repo is not None
        checked = _journaled_fence_check(saga, repository_id, saga.publication_epoch)
        intended = record_commit_intent(checked, repository_id)
        await self._store.save(intended)  # WRITE-AHEAD: intent durable BEFORE the call
        return await self._provider_commit(intended, repository_id)

    async def _provider_commit(self, saga: PublicationSaga, repository_id: str) -> PublicationSaga:
        repo = saga.repo(repository_id)
        assert repo is not None
        try:
            head = await self._provider.commit(repo.repository_id, repo.branch, saga.commit_marker)
        except ProviderRejectedError as exc:
            failed = provider_failed(saga, repository_id, f"provider refused: {exc}")
            await self._store.save(failed)  # prior WIP retained — never compensated away
            return failed
        except TimeoutError:
            unknown = outcome_unknown(saga, repository_id, "commit response lost")
            await self._store.save(unknown)
            return unknown
        except ProviderUnavailableError:
            # the surface is unreadable — the intent stays persisted, the
            # caller retries the pass; nothing is created blind.
            raise
        committed = provider_committed(saga, repository_id, head)
        await self._store.save(committed)
        return await self._verify_and_record(committed, repository_id)

    async def _verify_and_record(
        self, saga: PublicationSaga, repository_id: str
    ) -> PublicationSaga:
        verified_saga = await self._verify(saga, repository_id)
        repo_after = verified_saga.repo(repository_id)
        assert repo_after is not None
        if repo_after.status != "verified":
            return verified_saga  # booked failed/parked — the honest stop
        return await self._record_review(verified_saga, repository_id)

    async def _verify(self, saga: PublicationSaga, repository_id: str) -> PublicationSaga:
        """Read the effect back: the head must be OURS and unmoved."""
        repo = saga.repo(repository_id)
        assert repo is not None
        try:
            head_now = await self._provider.remote_head(repo.repository_id, repo.branch)
            ours = await self._provider.head_carries_marker(
                repo.repository_id, repo.branch, saga.commit_marker
            )
        except ProviderUnavailableError:
            raise  # fail closed — an unreadable surface proves nothing
        if head_now == repo.head_oid and ours:
            outcome = verified(saga, repository_id)
            await self._store.save(outcome)
            return outcome
        if head_now == repo.expected_base_oid:
            # our commit is not visible and nothing else moved the branch —
            # a definitive miss, safely retryable as a new attempt.
            missed = provider_failed(
                saga, repository_id, "committed head not visible at the expected base"
            )
            await self._store.save(missed)
            return missed
        moved = park_for_human(
            saga,
            repository_id,
            f"remote head {head_now} moved past the saga's expected base "
            f"{repo.expected_base_oid} after the commit — a human edit stands, "
            "never force-overwritten",
        )
        await self._store.save(moved)
        return moved

    async def _record_review(self, saga: PublicationSaga, repository_id: str) -> PublicationSaga:
        repo = saga.repo(repository_id)
        assert repo is not None
        try:
            review_url = await self._provider.open_review(
                repo.repository_id, repo.branch, saga.commit_marker
            )
        except ProviderRejectedError as exc:
            failed = provider_failed(saga, repository_id, f"review refused: {exc}")
            await self._store.save(failed)
            return failed
        except TimeoutError:
            unknown = outcome_unknown(saga, repository_id, "review response lost")
            await self._store.save(unknown)
            return unknown
        recorded = review_opened(saga, repository_id, review_url)
        await self._store.save(recorded)
        return recorded

    # -- recovery ------------------------------------------------------------

    async def _reconcile(self, saga: PublicationSaga, repository_id: str) -> PublicationSaga:
        """The adopt-or-reissue-or-park decision for an open effect window.

        Compares the remote head to the saga's expected base (the
        human-edit protection): at the base — nothing landed, re-issue
        the SAME intent (one intent per repo, marker-idempotent provider
        — no duplicate); moved but carrying our marker — OUR effect,
        adopt; moved without it — a HUMAN edit, park.
        """
        repo = saga.repo(repository_id)
        assert repo is not None
        try:
            remote_head = await self._provider.remote_head(repo.repository_id, repo.branch)
            ours = (
                remote_head != repo.expected_base_oid
                and await self._provider.head_carries_marker(
                    repo.repository_id, repo.branch, saga.commit_marker
                )
            )
        except ProviderUnavailableError:
            raise  # fail closed: an outage proves nothing — no create, no park
        if remote_head == repo.expected_base_oid:
            return await self._provider_commit(saga, repository_id)  # re-issue the intent
        if ours:
            adopted = adopt_remote_effect(saga, repository_id, remote_head)
            await self._store.save(adopted)
            return await self._verify_and_record(adopted, repository_id)
        parked = park_for_human(
            saga,
            repository_id,
            f"remote head {remote_head} moved past the saga's expected base "
            f"{repo.expected_base_oid} without this saga's marker — a human edit "
            "stands, never force-overwritten",
        )
        await self._store.save(parked)
        return parked


def _journaled_fence_check(
    saga: PublicationSaga, repository_id: str, epoch: int
) -> PublicationSaga:
    """Journal the per-repo fence-check step (the epoch the run stands under)."""
    entry: dict[str, str] = {
        "repo": repository_id,
        "step": "fence_check",
        "epoch": str(epoch),
    }
    return replace(saga, steps=saga.steps + (entry,))
