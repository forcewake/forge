"""#295 / R37-14 — the saga's native effect adapters over the REAL
provider clients' semantics.

The durable two-writer saga (#277) ran against ``NativeShapedRemote`` —
an in-process reference with CHOSEN duplicate/CAS semantics. These tests
pin the adapters that put the SAME effect interface over the providers'
ACTUAL preconditions, driven at three levels:

- **the seam**: ``NativeShapedRemote`` (the reference) and both native
  adapters satisfy :class:`SagaEffectSurface` — the effect interface
  ``DurablePublicationEntry`` consumes — structurally AND behaviorally
  (the pin, the 422-shaped refusal, the marker-keyed correlation);
- **GitLab** (no native CAS): the client-side expected-head check, the
  authoritative create-vs-update payload read, the lost-response window
  reconciled by NATIVE correlation (listed sha/parent/message — never a
  marker-keyed provider dedup, which GitLab does not have), and MR
  idempotency by the native ``(project, source branch, target)`` key;
- **GitHub** (native CAS): ``createCommitOnBranch``'s ``expectedHeadOid``
  refuses a moved head SERVER-side, the marker rides the headline (the
  line every listing shape carries), and PRs are adopted by the native
  ``(head, base)`` key as drafts.

The fakes are the existing in-memory clients (``tests/fixtures``) whose
commit/PR semantics mirror the parsed provider behavior — the REAL-HTTP
GitLab exercise lives in
``tests/production_entry/test_two_writer_native.py``.
"""

from __future__ import annotations

import httpx
import pytest

from forge.adaptive.publication_saga import (
    InMemorySagaStore,
    PublicationProvider,
    PublicationSaga,
    ProviderRejectedError,
    ProviderUnavailableError,
    SagaCoordinator,
    begin_saga,
)
from forge.adaptive.saga_durable import NativeShapedRemote
from forge.adaptive.saga_native import (
    DEFAULT_PAYLOAD_PATH,
    GitLabNativeEffects,
    GitHubNativeEffects,
    SagaEffectSurface,
    payload_document,
)
from forge.gitlab.client import GitLabAPIError
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.fixtures.fake_github import FakeGitHub

PRODUCER_REPO = "repo-producer"
CONSUMER_REPO = "repo-consumer"
PRODUCER_BRANCH = "forge/pub/producer"
CONSUMER_BRANCH = "forge/pub/consumer"
PRODUCER_BASE = "a" * 40
CONSUMER_BASE = "b" * 40
MARKER = "forge-saga:saga-native-1:0123456789abcdef"


def _begun_saga() -> PublicationSaga:
    return begin_saga(
        "wp-native-1",
        publication_epoch=1,
        candidate_digest="d" * 64,
        repo_plans={
            PRODUCER_REPO: (PRODUCER_BRANCH, PRODUCER_BASE),
            CONSUMER_REPO: (CONSUMER_BRANCH, CONSUMER_BASE),
        },
    )


def _seeded_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
    fake.seed_commit(CONSUMER_BRANCH, CONSUMER_BASE, "base", [])
    return fake


def _gitlab_effects(fake: FakeGitLab, **kwargs) -> GitLabNativeEffects:
    effects = GitLabNativeEffects(fake, {PRODUCER_REPO: 101, CONSUMER_REPO: 102}, **kwargs)
    effects.pin_expected_head(PRODUCER_REPO, PRODUCER_BRANCH, PRODUCER_BASE)
    effects.pin_expected_head(CONSUMER_REPO, CONSUMER_BRANCH, CONSUMER_BASE)
    return effects


class _FlakyHeadGitLab(FakeGitLab):
    """A fake whose branch-head read fails on demand (the fail-closed arm)."""

    def __init__(self) -> None:
        super().__init__()
        self.head_error: GitLabAPIError | None = None

    async def get_branch_head(self, project_id: int, branch_name: str) -> str:
        if self.head_error is not None:
            raise self.head_error
        return await super().get_branch_head(project_id, branch_name)


class _ForbiddenReadGitLab(FakeGitLab):
    """A fake whose payload read is forbidden (R14: never forge absence)."""

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD"):
        if file_path == DEFAULT_PAYLOAD_PATH:
            raise GitLabAPIError(403, "forbidden")
        return await super().get_file(project_id, file_path, ref)


class _LosingGitHub(FakeGitHub):
    """A fake whose commit RESPONSE dies after the effect landed."""

    def __init__(self) -> None:
        super().__init__()
        self.lose_response = False

    async def create_commit_on_branch(self, *args, **kwargs) -> dict:
        created = await super().create_commit_on_branch(*args, **kwargs)
        if self.lose_response:
            raise httpx.ReadTimeout("response lost after the effect landed")
        return created


# ---------------------------------------------------------------------------
# The effect-interface seam — every surface satisfies the same Protocol.
# ---------------------------------------------------------------------------


class TestEffectSurfaceCompat:
    def test_the_reference_remote_satisfies_the_effect_interface(self):
        remote = NativeShapedRemote()
        assert isinstance(remote, SagaEffectSurface)
        assert isinstance(remote, PublicationProvider)

    def test_the_native_adapters_satisfy_the_effect_interface(self):
        gitlab = GitLabNativeEffects(FakeGitLab(), {})
        github = GitHubNativeEffects(FakeGitHub(), {})
        assert isinstance(gitlab, SagaEffectSurface)
        assert isinstance(gitlab, PublicationProvider)
        assert isinstance(github, SagaEffectSurface)
        assert isinstance(github, PublicationProvider)

    async def test_the_reference_remote_keeps_the_contract_shape(self):
        """The in-process reference behaves exactly as the interface
        promises: pin, commit, 422 on a moved head, marker correlation."""
        remote = NativeShapedRemote()
        remote.seed(PRODUCER_REPO, PRODUCER_BRANCH, PRODUCER_BASE)
        remote.pin_expected_head(PRODUCER_REPO, PRODUCER_BRANCH, PRODUCER_BASE)
        sha = await remote.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert sha
        assert await remote.head_carries_marker(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        # the expected-head precondition: a moved head refuses before any effect
        remote.human_commit(PRODUCER_REPO, PRODUCER_BRANCH)
        with pytest.raises(ProviderRejectedError, match="422 commit"):
            await remote.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        # the one-shot expected_head kwarg pins the same guard (#295 seam)
        remote.create_commit(
            PRODUCER_REPO, PRODUCER_BRANCH, "x", author="human", expected_head="0" * 40
        )
        with pytest.raises(ProviderRejectedError, match="422 commit"):
            await remote.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)

    async def test_a_missing_repository_mapping_refuses_loudly(self):
        effects = GitLabNativeEffects(FakeGitLab(), {})
        with pytest.raises(KeyError, match="no GitLab project mapping"):
            await effects.remote_head("repo-unknown", "main")
        github = GitHubNativeEffects(FakeGitHub(), {})
        with pytest.raises(KeyError, match="no GitHub repository mapping"):
            await github.remote_head("repo-unknown", "main")


# ---------------------------------------------------------------------------
# GitLabNativeEffects — no native CAS: client-side precondition + correlation.
# ---------------------------------------------------------------------------


class TestGitLabNativeEffects:
    async def test_commit_lands_the_payload_with_native_identity(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        sha = await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert sha
        assert fake.branches[PRODUCER_BRANCH][0]["sha"] == sha
        assert MARKER in fake.branches[PRODUCER_BRANCH][0]["message"]
        assert fake.branches[PRODUCER_BRANCH][0]["parent_ids"] == [PRODUCER_BASE]
        # the payload content is deterministic and the action was a CREATE
        actions = [call[1][2] for call in fake.calls_of("create_commit")]
        assert actions[0][0]["action"] == "create"
        assert actions[0][0]["content"] == payload_document(MARKER)
        # correlation by native identity: the listed history carries it
        landed = await effects.commits_carrying(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert [commit.sha for commit in landed] == [sha]
        assert landed[0].parent == PRODUCER_BASE

    async def test_the_expected_head_precondition_is_client_side(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        # a human edit moves the head under the pin: refused BEFORE any effect
        fake.seed_commit(PRODUCER_BRANCH, "c" * 40, "human edit", [PRODUCER_BASE])
        with pytest.raises(ProviderRejectedError, match="client-side CAS"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert len(fake.branches[PRODUCER_BRANCH]) == 2  # base + human, nothing else
        assert effects.commit_calls[PRODUCER_REPO] == 1

    async def test_the_payload_action_follows_the_authoritative_read(self):
        fake = _seeded_gitlab()
        fake.seed_file(DEFAULT_PAYLOAD_PATH, "the previous attempt's payload")
        effects = _gitlab_effects(fake)
        await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        actions = [call[1][2] for call in fake.calls_of("create_commit")]
        assert actions[0][0]["action"] == "update"

    async def test_a_failed_payload_read_fails_closed(self):
        fake = _ForbiddenReadGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        effects = GitLabNativeEffects(fake, {PRODUCER_REPO: 101})
        effects.pin_expected_head(PRODUCER_REPO, PRODUCER_BRANCH, PRODUCER_BASE)
        with pytest.raises(ProviderUnavailableError, match="cannot be made"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert fake.calls_of("create_commit") == []  # nothing written blind

    async def test_a_definitive_refusal_maps_to_rejected(self):
        fake = _seeded_gitlab()
        fake.raise_on_create_commit = GitLabAPIError(400, "A file with this name exists")
        effects = _gitlab_effects(fake)
        with pytest.raises(ProviderRejectedError, match="gitlab refused \\(400\\)"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)

    async def test_an_unreadable_surface_fails_closed(self):
        fake = _FlakyHeadGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        fake.head_error = GitLabAPIError(503, "boom")
        effects = GitLabNativeEffects(fake, {PRODUCER_REPO: 101})
        with pytest.raises(ProviderUnavailableError, match="503"):
            await effects.remote_head(PRODUCER_REPO, PRODUCER_BRANCH)
        with pytest.raises(ProviderUnavailableError):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert fake.calls_of("create_commit") == []

    async def test_a_lost_response_is_reconciled_by_native_correlation(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        store = InMemorySagaStore()
        saga = _begun_saga()
        await store.save(saga)
        fake.create_commit_timeout_applies = True  # the effect lands, the answer dies
        first = await SagaCoordinator(store, effects).run(saga, current_publication_epoch=1)
        producer = first.repo(PRODUCER_REPO)
        assert producer is not None and producer.status == "outcome_unknown"
        assert effects.commit_calls[PRODUCER_REPO] == 1

        fake.create_commit_timeout_applies = False  # the provider heals
        recovered = await SagaCoordinator(store, effects).run(first, current_publication_epoch=1)
        producer = recovered.repo(PRODUCER_REPO)
        assert producer is not None
        assert producer.status == "ready_for_review"
        assert producer.adopted is True  # adopted by evidence, never re-created
        assert effects.commit_calls[PRODUCER_REPO] == 1  # exactly one provider write
        landed = await effects.commits_carrying(
            PRODUCER_REPO, PRODUCER_BRANCH, recovered.commit_marker
        )
        assert len(landed) == 1 and landed[0].parent == PRODUCER_BASE
        assert effects.merge_request_creates(PRODUCER_REPO) == 1

    async def test_reviews_are_idempotent_by_the_native_source_key(self):
        fake = _seeded_gitlab()
        seeded = fake.seed_merge_request(
            101, PRODUCER_BRANCH, "Draft: an earlier attempt", target="main", iid=9001
        )
        effects = _gitlab_effects(fake)
        first = await effects.open_review(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        again = await effects.open_review(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert first == again == seeded["web_url"]  # the SAME MR, adopted
        assert effects.merge_request_creates(PRODUCER_REPO) == 0
        assert [call for call in fake.calls_of("create_merge_request")] == []

    async def test_a_fresh_review_is_created_once_and_as_a_draft(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        first = await effects.open_review(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        again = await effects.open_review(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert first == again
        assert effects.merge_request_creates(PRODUCER_REPO) == 1
        create_args = [call[1] for call in fake.calls_of("create_merge_request")]
        assert create_args[0][3].startswith("Draft: forge publication")
        assert create_args[0][2] == "main"

    async def test_a_two_repo_publication_through_the_real_coordinator(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        store = InMemorySagaStore()
        saga = _begun_saga()
        await store.save(saga)
        finished = await SagaCoordinator(store, effects).run(saga, current_publication_epoch=1)
        assert str(finished.status) == "complete"
        for repository_id, branch in (
            (PRODUCER_REPO, PRODUCER_BRANCH),
            (CONSUMER_REPO, CONSUMER_BRANCH),
        ):
            landed = await effects.commits_carrying(repository_id, branch, finished.commit_marker)
            assert len(landed) == 1
            assert landed[0].parent in {PRODUCER_BASE, CONSUMER_BASE}
            assert effects.merge_request_creates(repository_id) == 1
            assert effects.destructive_operations() == []
        # the journal carries every effect with its native identity
        ops = {entry["op"] for entry in effects.journal}
        assert ops <= {"create_commit", "create_merge_request", "adopt_merge_request"}


# ---------------------------------------------------------------------------
# GitHubNativeEffects — NATIVE CAS: the server refuses the moved head.
# ---------------------------------------------------------------------------


def _seeded_github(fake: FakeGitHub) -> FakeGitHub:
    fake.seed_commit("acme/producer", PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
    fake.seed_commit("acme/consumer", CONSUMER_BRANCH, CONSUMER_BASE, "base", [])
    return fake


def _github_effects(fake: FakeGitHub) -> GitHubNativeEffects:
    effects = GitHubNativeEffects(
        fake,
        {PRODUCER_REPO: ("acme", "producer"), CONSUMER_REPO: ("acme", "consumer")},
    )
    effects.pin_expected_head(PRODUCER_REPO, PRODUCER_BRANCH, PRODUCER_BASE)
    effects.pin_expected_head(CONSUMER_REPO, CONSUMER_BRANCH, CONSUMER_BASE)
    return effects


class TestGitHubNativeEffects:
    async def test_the_cas_is_native_and_refuses_a_moved_head(self):
        fake = _seeded_github(FakeGitHub())
        effects = _github_effects(fake)
        # a human edit moves the head under the pin: the SERVER refuses
        fake.seed_commit("acme/producer", PRODUCER_BRANCH, "c" * 40, "human edit", [PRODUCER_BASE])
        with pytest.raises(ProviderRejectedError, match="native createCommitOnBranch CAS"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        heads = fake.commits["acme/producer"][PRODUCER_BRANCH]
        assert [commit["sha"] for commit in heads] == ["c" * 40, PRODUCER_BASE]

    async def test_the_marker_rides_the_headline_and_the_cas_token_is_the_parent(self):
        fake = _seeded_github(FakeGitHub())
        effects = _github_effects(fake)
        sha = await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        (head, base) = fake.commits["acme/producer"][PRODUCER_BRANCH]
        assert head["sha"] == sha
        assert MARKER in head["message"]  # the headline carries the marker
        assert head["parent_ids"] == [PRODUCER_BASE]  # the CAS token WAS the base
        assert fake.files["acme/producer"][DEFAULT_PAYLOAD_PATH] == payload_document(MARKER)

    async def test_a_lost_response_is_adopted_without_a_duplicate(self):
        fake = _LosingGitHub()
        _seeded_github(fake)
        effects = _github_effects(fake)
        store = InMemorySagaStore()
        saga = _begun_saga()
        await store.save(saga)
        fake.lose_response = True
        first = await SagaCoordinator(store, effects).run(saga, current_publication_epoch=1)
        producer = first.repo(PRODUCER_REPO)
        assert producer is not None and producer.status == "outcome_unknown"

        fake.lose_response = False
        recovered = await SagaCoordinator(store, effects).run(first, current_publication_epoch=1)
        producer = recovered.repo(PRODUCER_REPO)
        assert producer is not None
        assert producer.status == "ready_for_review" and producer.adopted is True
        assert effects.commit_calls[PRODUCER_REPO] == 1
        landed = await effects.commits_carrying(
            PRODUCER_REPO, PRODUCER_BRANCH, recovered.commit_marker
        )
        assert len(landed) == 1 and landed[0].parent == PRODUCER_BASE

    async def test_reviews_are_drafts_adopted_by_the_native_head_base_key(self):
        fake = _seeded_github(FakeGitHub())
        effects = _github_effects(fake)
        first = await effects.open_review(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        again = await effects.open_review(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert first == again
        assert effects.merge_request_creates(PRODUCER_REPO) == 1
        (pr,) = fake.pull_requests["acme/producer"]
        assert pr["draft"] is True
        assert pr["base"]["ref"] == "main"

    async def test_a_two_repo_publication_through_the_real_coordinator(self):
        fake = _seeded_github(FakeGitHub())
        effects = _github_effects(fake)
        store = InMemorySagaStore()
        saga = _begun_saga()
        await store.save(saga)
        finished = await SagaCoordinator(store, effects).run(saga, current_publication_epoch=1)
        assert str(finished.status) == "complete"
        for repository_id, branch in (
            (PRODUCER_REPO, PRODUCER_BRANCH),
            (CONSUMER_REPO, CONSUMER_BRANCH),
        ):
            landed = await effects.commits_carrying(repository_id, branch, finished.commit_marker)
            assert len(landed) == 1
            assert effects.merge_request_creates(repository_id) == 1
            assert effects.destructive_operations() == []
