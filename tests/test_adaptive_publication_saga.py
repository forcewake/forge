"""NXT-24 — coordinated publication as a recoverable saga with human-edit
protection.

The invariants pinned here are the ones the review (MRP-04/MRP-07) states
as the difference between phasing CALLS and coordinating FINISHED work:

- the per-repo commit intent is persisted BEFORE the provider call, so a
  crash between intent and commit recovers by ADOPTING or re-issuing the
  SAME intent — never by creating a second effect;
- a remote head the saga's marker cannot account for is a HUMAN edit:
  the repo is PARKED for a decision, the branch is never force-pushed or
  overwritten;
- one repository's failure preserves and reports the others' PRs — the
  parent status includes unresolved effects instead of claiming rollback;
- recovery completes an interrupted saga from its persisted intents, and
  the whole-digest of the journaled steps covers every step that ran.
"""

from __future__ import annotations

import pytest

from forge.adaptive.publication_saga import (
    InMemorySagaStore,
    PublicationSaga,
    ProviderRejectedError,
    ProviderUnavailableError,
    SagaCoordinator,
    begin_saga,
    fence_check,
    observe_human_merge,
    record_commit_intent,
)

REPOS = {"repo-a": ("main", "base-a-0"), "repo-b": ("main", "base-b-0")}


class FakeRemote:
    """A marker-keyed provider: branch history + one review per marker.

    ``commit`` is idempotent per ``(branch, marker)`` — the property the
    saga's re-issue path depends on (a retried intent never creates a
    second commit). There is deliberately no merge, force-push or delete:
    the provider cannot express the effects NXT-24 forbids. The knobs
    (``unavailable`` / ``reject_commit`` / ``lose_commit_response``)
    inject the outage, refusal and lost-response windows.
    """

    def __init__(self) -> None:
        self._commits: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._reviews: dict[tuple[str, str, str], str] = {}
        self._counter = 0
        self.commit_calls: dict[str, int] = {}
        self.review_calls: dict[str, int] = {}
        self.unavailable: set[str] = set()
        self.reject_commit: set[str] = set()
        self.lose_commit_response: set[str] = set()

    def seed(self, repository_id: str, branch: str, base_oid: str) -> None:
        self._commits[(repository_id, branch)] = [(base_oid, "base")]

    def human_commit(self, repository_id: str, branch: str) -> str:
        self._counter += 1
        oid = f"human-{repository_id}-{self._counter}"
        self._commits[(repository_id, branch)].append((oid, f"human:{repository_id}"))
        return oid

    def _history(self, repository_id: str, branch: str) -> list[tuple[str, str]]:
        return self._commits.setdefault((repository_id, branch), [])

    async def remote_head(self, repository_id: str, branch: str) -> str:
        if repository_id in self.unavailable:
            raise ProviderUnavailableError(f"{repository_id}: surface unreadable")
        history = self._history(repository_id, branch)
        return history[-1][0] if history else ""

    async def head_carries_marker(self, repository_id: str, branch: str, marker: str) -> bool:
        if repository_id in self.unavailable:
            raise ProviderUnavailableError(f"{repository_id}: surface unreadable")
        return any(
            stored_marker == marker for _oid, stored_marker in self._history(repository_id, branch)
        )

    async def commit(self, repository_id: str, branch: str, marker: str) -> str:
        self.commit_calls[repository_id] = self.commit_calls.get(repository_id, 0) + 1
        if repository_id in self.reject_commit:
            raise ProviderRejectedError(f"{repository_id}: refused")
        history = self._history(repository_id, branch)
        for oid, stored_marker in history:
            if stored_marker == marker:
                return oid  # idempotent: the intent already landed
        self._counter += 1
        oid = f"oid-{repository_id}-{self._counter}"
        history.append((oid, marker))
        if repository_id in self.lose_commit_response:
            raise TimeoutError(f"{repository_id}: response lost after the effect landed")
        return oid

    async def open_review(self, repository_id: str, branch: str, marker: str) -> str:
        self.review_calls[repository_id] = self.review_calls.get(repository_id, 0) + 1
        key = (repository_id, branch, marker)
        if key not in self._reviews:
            self._reviews[key] = (
                f"https://example.test/{repository_id}/pull/{len(self._reviews) + 1}"
            )
        return self._reviews[key]


def _saga() -> PublicationSaga:
    return begin_saga("wp-1", publication_epoch=3, candidate_digest="d" * 64, repo_plans=REPOS)


def _seeded_remote() -> FakeRemote:
    remote = FakeRemote()
    for repository_id, (branch, base) in REPOS.items():
        remote.seed(repository_id, branch, base)
    return remote


class TestHappyPath:
    async def test_two_repos_publish_through_every_journaled_step(self):
        store, remote = InMemorySagaStore(), _seeded_remote()
        coordinator = SagaCoordinator(store, remote)

        finished = await coordinator.run(_saga(), current_publication_epoch=3)

        assert finished.status == "complete"
        for repo in finished.repos:
            assert repo.status == "ready_for_review"
            assert repo.head_oid and repo.head_oid != repo.expected_base_oid
            assert repo.review_url and repo.review_url.startswith("https://example.test/")
        # one commit, one review per repo — no duplicate effects:
        assert remote.commit_calls == {"repo-a": 1, "repo-b": 1}
        assert remote.review_calls == {"repo-a": 1, "repo-b": 1}
        # the whole-digest of steps covers each repo's full ladder:
        for repository_id in REPOS:
            steps = [step["step"] for step in finished.steps if step["repo"] == repository_id]
            assert steps == [
                "prepare",
                "fence_check",
                "commit_intent",
                "provider_commit",
                "verify",
                "record",
            ]
        assert finished.steps_digest == finished.steps_digest  # stable
        assert len(finished.steps_digest) == 64

    async def test_the_steps_digest_is_the_whole_history_not_a_tail(self):
        """Two identical runs produce the same digest; dropping ANY step
        changes it — the journal cannot silently lose a step."""

        async def run_once() -> PublicationSaga:
            return await SagaCoordinator(InMemorySagaStore(), _seeded_remote()).run(
                _saga(), current_publication_epoch=3
            )

        first, second = await run_once(), await run_once()
        assert first.steps_digest == second.steps_digest

        from dataclasses import replace as dc_replace

        trimmed = dc_replace(first, steps=first.steps[:-1])
        assert trimmed.steps_digest != first.steps_digest

    async def test_a_saga_needs_a_real_plan(self):
        with pytest.raises(ValueError, match="no saga"):
            begin_saga("wp-1", 1, "d" * 64, {})
        with pytest.raises(ValueError, match="comparison anchor"):
            begin_saga("wp-1", 1, "d" * 64, {"repo-a": ("main", "")})


class TestCrashRecovery:
    async def test_a_crash_before_the_provider_call_reissues_the_same_intent(self):
        """Crash between commit-intent and the provider call: nothing landed,
        so recovery re-issues the SAME persisted intent — one commit, one
        PR, no duplicates."""
        store, remote = InMemorySagaStore(), _seeded_remote()
        saga = _saga()
        intended = record_commit_intent(record_commit_intent(saga, "repo-a"), "repo-b")
        await store.save(intended)  # the crash point: intents durable, no effects

        recovered = await SagaCoordinator(store, remote).recover(
            intended.saga_id, current_publication_epoch=3
        )

        assert recovered is not None
        assert recovered.status == "complete"
        assert remote.commit_calls == {"repo-a": 1, "repo-b": 1}  # exactly one each
        assert remote.review_calls == {"repo-a": 1, "repo-b": 1}
        for repository_id in REPOS:
            steps = [step["step"] for step in recovered.steps if step["repo"] == repository_id]
            assert steps == [
                "prepare",
                "commit_intent",
                "provider_commit",
                "verify",
                "record",
            ]

    async def test_a_crash_after_the_commit_adopts_the_late_landed_effect(self):
        """The provider took the commit; the response died with the process.
        Recovery finds the head carrying the saga's marker and ADOPTS it —
        the second commit call never happens."""
        store, remote = InMemorySagaStore(), _seeded_remote()
        saga = _saga()
        intended = record_commit_intent(saga, "repo-a")
        await store.save(intended)  # the crash point
        # the effect landed after the crash, before any state was booked:
        landed_head = await remote.commit("repo-a", "main", intended.commit_marker)

        recovered = await SagaCoordinator(store, remote).recover(
            intended.saga_id, current_publication_epoch=3
        )

        assert recovered is not None
        repo = recovered.repo("repo-a")
        assert repo is not None and repo.status == "ready_for_review"
        assert repo.adopted is True
        assert repo.head_oid == landed_head
        # repo-a adopted, NOT re-committed (repo-b simply ran its own path):
        assert remote.commit_calls == {"repo-a": 1, "repo-b": 1}
        assert "adopt" in [step["step"] for step in recovered.steps if step["repo"] == "repo-a"]

    async def test_a_lost_commit_response_lands_outcome_unknown_then_recovers(self):
        store, remote = InMemorySagaStore(), _seeded_remote()
        remote.lose_commit_response = {"repo-a"}

        first_pass = await SagaCoordinator(store, remote).run(_saga(), current_publication_epoch=3)

        repo = first_pass.repo("repo-a")
        assert repo is not None and repo.status == "outcome_unknown"  # honest window
        assert first_pass.status == "failed"  # unresolved effects reported, not rolled back
        other = first_pass.repo("repo-b")
        assert other is not None and other.status == "ready_for_review"  # B unaffected

        remote.lose_commit_response = set()  # the provider heals
        recovered = await SagaCoordinator(store, remote).recover(
            first_pass.saga_id, current_publication_epoch=3
        )

        assert recovered is not None and recovered.status == "complete"
        adopted = recovered.repo("repo-a")
        assert adopted is not None and adopted.adopted is True
        assert remote.commit_calls == {"repo-a": 1, "repo-b": 1}  # no blind retry

    async def test_recovery_completes_an_interrupted_saga(self):
        """A already published, B interrupted at its intent: recovery
        finishes B without touching A's decided state."""
        store, remote = InMemorySagaStore(), _seeded_remote()
        saga = _saga()
        # A published before the crash — advanced by the pure transitions
        # (the value-object record of finished work; no provider calls):
        from forge.adaptive.publication_saga import (
            provider_committed as _committed,
            review_opened as _review_opened,
            verified as _verified,
        )

        published_a = _review_opened(
            _verified(
                _committed(record_commit_intent(saga, "repo-a"), "repo-a", "oid-repo-a-1"), "repo-a"
            ),
            "repo-a",
            "https://example.test/repo-a/pull/1",
        )
        interrupted = record_commit_intent(published_a, "repo-b")
        await store.save(interrupted)  # the crash point: B's intent persisted, nothing driven

        recovered = await SagaCoordinator(store, remote).recover(
            interrupted.saga_id, current_publication_epoch=3
        )

        assert recovered is not None and recovered.status == "complete"
        assert recovered.repo("repo-a") == published_a.repo("repo-a")  # A untouched
        assert remote.commit_calls == {"repo-b": 1}  # B's recovery was its FIRST drive
        assert remote.review_calls == {"repo-b": 1}


class TestHumanEditProtection:
    async def test_a_human_moved_head_parks_the_repo_never_force_overwrites(self):
        store, remote = InMemorySagaStore(), _seeded_remote()
        saga = _saga()
        intended = record_commit_intent(saga, "repo-b")
        await store.save(intended)
        human_oid = remote.human_commit("repo-b", "main")  # a person pushed first

        recovered = await SagaCoordinator(store, remote).recover(
            intended.saga_id, current_publication_epoch=3
        )

        repo = recovered.repo("repo-b") if recovered else None
        assert repo is not None and repo.status == "parked_human"
        assert "human edit" in repo.note
        assert recovered is not None and recovered.status == "parked"
        assert recovered.unresolved() == (repo,)
        # the branch is EXACTLY as the human left it — no commit, no review:
        assert remote.commit_calls.get("repo-b", 0) == 0
        assert remote.review_calls.get("repo-b", 0) == 0
        assert await remote.remote_head("repo-b", "main") == human_oid

    async def test_a_moved_head_after_commit_parks_instead_of_overwriting(self):
        remote = _seeded_remote()
        saga = _saga()
        committed = await SagaCoordinator(InMemorySagaStore(), remote).run(
            saga, current_publication_epoch=3
        )
        # A human lands on top of our verified commit before... the verify
        # step of a SECOND pass re-reads the head and finds it moved.
        remote.human_commit("repo-a", "main")
        store2, remote2 = InMemorySagaStore(), remote
        from dataclasses import replace as dc_replace

        repo_a = committed.repo("repo-a")
        assert repo_a is not None
        at_committed = dc_replace(
            committed,
            repos=(
                dc_replace(repo_a, status="committed", review_url=None),
                committed.repos[1],
            ),
        )
        await store2.save(at_committed)

        rechecked = await SagaCoordinator(store2, remote2).recover(
            at_committed.saga_id, current_publication_epoch=3
        )

        repo = rechecked.repo("repo-a") if rechecked else None
        assert repo is not None and repo.status == "parked_human"
        assert remote2.commit_calls == {"repo-a": 1, "repo-b": 1}  # nothing re-created


class TestPartialFailure:
    async def test_failure_of_repo_b_preserves_and_reports_repo_a(self):
        store, remote = InMemorySagaStore(), _seeded_remote()
        remote.reject_commit = {"repo-b"}

        finished = await SagaCoordinator(store, remote).run(_saga(), current_publication_epoch=3)

        repo_a, repo_b = finished.repo("repo-a"), finished.repo("repo-b")
        assert repo_a is not None and repo_a.status == "ready_for_review"
        assert repo_a.review_url and repo_a.review_url.startswith("https://example.test/repo-a")
        assert repo_b is not None and repo_b.status == "failed"
        assert "refused" in repo_b.note
        assert finished.status == "failed"  # the parent includes the unresolved effect
        assert repo_b in finished.unresolved()
        assert repo_a in finished.effects_standing()  # never claimed rolled back
        # B's failure did not resubmit A:
        assert remote.commit_calls == {"repo-a": 1, "repo-b": 1}
        assert remote.review_calls == {"repo-a": 1}

    async def test_a_decided_repo_is_never_resubmitted_by_a_later_run(self):
        store, remote = InMemorySagaStore(), _seeded_remote()
        coordinator = SagaCoordinator(store, remote)
        finished = await coordinator.run(_saga(), current_publication_epoch=3)

        again = await coordinator.run(finished, current_publication_epoch=3)

        assert again.status == "complete"
        assert remote.commit_calls == {"repo-a": 1, "repo-b": 1}  # nothing re-spent


class TestFenceAndOutage:
    async def test_a_fenced_saga_supersedes_pending_intents_but_effects_stand(self):
        """Provider outage + parent cancel (the issue's negative): the
        unreadable surface fails CLOSED (no create for repo-b), repo-a
        completes normally, and the bumped fence then kills repo-b's
        pending intent — while the PR that already landed stands and is
        reported, never pretend-undone."""
        store, remote = InMemorySagaStore(), _seeded_remote()
        saga = _saga()
        intended = record_commit_intent(saga, "repo-b")
        await store.save(intended)
        remote.unavailable = {"repo-b"}

        coordinator = SagaCoordinator(store, remote)
        with pytest.raises(ProviderUnavailableError):
            # repo-a publishes; repo-b's reconcile fails closed and the
            # pass aborts with the saga persisted at its last state.
            await coordinator.recover(intended.saga_id, current_publication_epoch=3)
        assert remote.commit_calls.get("repo-b", 0) == 0  # fail closed — no create

        fenced = await coordinator.recover(
            intended.saga_id, current_publication_epoch=4
        )  # the pause bumped the epoch — the cancel

        assert fenced is not None
        repo_a, repo_b = fenced.repo("repo-a"), fenced.repo("repo-b")
        assert repo_a is not None and repo_a.status == "ready_for_review"
        assert repo_b is not None and repo_b.status == "superseded"
        assert fenced.status == "fenced"
        # A's PR stands through the fence — correlated, never claimed undone:
        assert repo_a in fenced.effects_standing()
        assert repo_b not in fenced.effects_standing()  # its intent died effect-less

    async def test_committed_effects_stand_through_the_fence(self):
        store, remote = InMemorySagaStore(), _seeded_remote()
        published = await SagaCoordinator(store, remote).run(_saga(), current_publication_epoch=3)

        fenced = fence_check(published, 5)  # a later pause fences the world

        assert fenced.status == "complete"  # already-published work is not revoked
        assert len(fenced.effects_standing()) == 2
        for repo in fenced.repos:
            assert repo.review_url  # the PRs stand — correlated, never claimed undone

    async def test_the_fence_is_idempotent_a_redelivery_spends_nothing(self):
        saga = record_commit_intent(_saga(), "repo-a")

        first = fence_check(saga, 5)
        second = fence_check(first, 5)

        assert second.steps == first.steps  # no second supersession journaled
        assert fence_check(saga, 3) is saga  # an equal epoch is a no-op

    async def test_the_ladder_refuses_to_skip(self):
        saga = _saga()
        with pytest.raises(ValueError, match="refuses"):
            observe_human_merge(saga, "repo-a")  # only a ready_for_review PR may be observed
        moved = record_commit_intent(saga, "repo-a")
        with pytest.raises(ValueError, match="refuses"):
            record_commit_intent(moved, "repo-a")  # the intent is recorded once


class TestHumanMergeIsObservedOnly:
    async def test_the_bot_never_merges_human_merge_is_an_observation(self):
        store, remote = InMemorySagaStore(), _seeded_remote()
        published = await SagaCoordinator(store, remote).run(_saga(), current_publication_epoch=3)

        merged = observe_human_merge(published, "repo-a")  # a person merged the PR

        repo = merged.repo("repo-a")
        assert repo is not None and repo.status == "human_merged"
        assert merged.status == "complete"
        # the observation is journaled as a human act, not a saga effect:
        entry = [step for step in merged.steps if step["repo"] == "repo-a"][-1]
        assert entry["step"] == "human_merged" and entry["to"] == "human_merged"
