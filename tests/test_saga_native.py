"""#295 / R37-14 + #314 / R38-13 — the saga's native effect adapters over
the REAL provider clients' semantics.

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

R38-13 (#314) pins the qualification on top:

- **payload preconditions**: ``GitLabNativeEffects.commit`` records the
  payload base and affected-file versions beside the expected head, and
  RECHECKS them both before the apply (every drift shape — changed
  content, created-under-a-create, deleted-under-an-update — refuses
  with the typed :class:`ContentConflictError` while the concurrent
  content is still the head) and after it (a commit that landed on a
  different base gets its payload read back AT THE PARENT revision —
  the full-file-replacement hazard surfaces typed, never silently);
- **single-writer exclusivity**: a second writer entering the same
  branch's window is refused with the typed
  :class:`WriterExclusivityError`, never interleaved (the policy
  provider's stand-in for a branch-wide CAS);
- **content-level adoption**: a commit counts as carrying the marker
  only when the payload file's CONTENT at that commit is the intended
  payload — a forged message with wrong/absent content is never adopted;
- **no blind redispatch**: a mutation whose response was lost stays
  unresolved; a negative probe refuses the re-send fail-closed until a
  content-verified probe adopts.

The fakes are the existing in-memory clients (``tests/fixtures``) whose
commit/PR semantics mirror the parsed provider behavior, plus
``_SnapshottedGitLab`` — a subclass modelling the PER-REF file semantics
the real provider has (create_commit APPLIES its actions; a blob read at
a commit sha sees that commit's tree) — the REAL-HTTP GitLab exercise
lives in ``tests/production_entry/test_two_writer_native.py``.
"""

from __future__ import annotations

import asyncio
import base64
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
    record_commit_intent,
)
from forge.adaptive.saga_durable import NativeShapedRemote, saga_from_document
from forge.adaptive.saga_native import (
    CONTENT_CONFLICT_TAG,
    DEFAULT_PAYLOAD_PATH,
    GitLabNativeEffects,
    GitHubNativeEffects,
    SagaEffectSurface,
    WRITER_EXCLUSIVITY_TAG,
    ContentConflictError,
    WriterExclusivityError,
    payload_document,
    payload_digest,
)
from forge.gitlab.client import CommitOutcomeUnknown, GitLabAPIError
from forge.gitlab.schemas import RepositoryFile
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


def _seeded_gitlab() -> "_SnapshottedGitLab":
    fake = _SnapshottedGitLab()
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


class _SnapshottedGitLab(FakeGitLab):
    """A fake whose repository files are PER-REF — the semantics the REAL
    provider has and the shared run-loop fake simplifies away:

    - ``create_commit`` APPLIES its actions (create/update/delete) to the
      working tree — the full-file replacement the content preconditions
      guard against is visible;
    - every commit the fake itself CREATES records the tree snapshot it
      produced, so a blob read at ``ref=<sha>`` sees the content as of
      THAT commit (the post-apply recheck and the adoption content check
      read at commit shas);
    - reads at a branch name, or at a sha with no snapshot (a seeded
      commit), fall back to the CURRENT working tree — the shared fake's
      single-snapshot behavior, kept so seeded files stay visible;
    - ``delayed_apply`` branches accept a commit WITHOUT applying it (the
      response times out, the outcome is unknown) until
      :meth:`flush_delayed_apply` lands it — the provider window in which
      an accepted mutation is not yet visible, where a probe is honestly
      negative while the effect exists.
    """

    def __init__(self) -> None:
        super().__init__()
        self._snapshots: dict[str, dict[str, str]] = {}  # sha -> tree snapshot
        #: Branches whose commits are ACCEPTED but not applied yet.
        self.delayed_apply: set[str] = set()
        self._delayed: list[tuple[str, list[dict], str]] = []  # (branch, actions, message)

    # -- the per-ref read surface -------------------------------------------

    def _tree_at(self, ref: str) -> dict[str, str] | None:
        """The tree a read at *ref* resolves to (None = the provider
        spelling of "file not found there")."""
        snapshot = self._snapshots.get(ref)
        if snapshot is not None:
            return snapshot
        return self.files  # a branch/HEAD/unsnapshotted sha: the current tree

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD") -> RepositoryFile:
        self.calls.append(("get_file", (project_id, file_path, ref)))
        tree = self._tree_at(ref)
        if tree is None or file_path not in tree:
            raise GitLabAPIError(404, f"file {file_path} not found")
        content = tree[file_path]
        return RepositoryFile.model_validate(
            {
                "file_name": file_path.rsplit("/", 1)[-1],
                "file_path": file_path,
                "size": len(content.encode("utf-8", errors="surrogateescape")),
                "encoding": "base64",
                "content": base64.b64encode(
                    content.encode("utf-8", errors="surrogateescape")
                ).decode("ascii"),
                "ref": ref,
            }
        )

    # -- commits apply their actions, and snapshot their tree -----------------

    def _apply_actions(self, actions: list[dict]) -> None:
        for action in actions:
            path = str(action.get("file_path") or "")
            if not path:
                continue
            kind = str(action.get("action") or "create")
            if kind == "delete":
                self.files.pop(path, None)
            else:  # create | update — the full-file replacement semantics
                self.files[path] = str(action.get("content") or "")

    def _snapshot_head(self, branch: str) -> None:
        commits = self.branches.get(branch) or []
        if commits:
            self._snapshots[str(commits[0]["sha"])] = dict(self.files)

    async def create_commit(
        self,
        project_id: int,
        branch: str,
        actions: list[dict],
        commit_message: str,
        start_branch: str | None = None,
    ) -> dict:
        if branch in self.delayed_apply:
            # ACCEPT the mutation, apply it later — the outcome-unknown
            # window where the branch keeps its old head.
            self.calls.append(("create_commit", (project_id, branch, actions, commit_message)))
            self._delayed.append((branch, list(actions), commit_message))
            raise CommitOutcomeUnknown("create_commit timed out; outcome unknown")
        try:
            result = await super().create_commit(
                project_id, branch, actions, commit_message, start_branch
            )
        except CommitOutcomeUnknown:
            # the shared fake APPLIED the commit before the timeout fired
            self._apply_actions(actions)
            self._snapshot_head(branch)
            raise
        self._apply_actions(actions)
        self._snapshot_head(branch)
        return result

    def flush_delayed_apply(self, branch: str) -> None:
        """Land the accepted-but-delayed commits on *branch* (in order),
        with the message each carried when accepted — the marker stays
        correlatable exactly as a real delayed apply would keep it."""
        pending = [item for item in self._delayed if item[0] == branch]
        self._delayed = [item for item in self._delayed if item[0] != branch]
        for _branch, actions, message in pending:
            sha = f"sha-{self._id()}"
            head = self.branches.get(branch, [])
            record = {
                "sha": sha,
                "short_id": sha,
                "message": message,
                "parent_ids": [head[0]["sha"]] if head else [],
            }
            self.branches.setdefault(branch, []).insert(0, record)
            self._apply_actions(actions)
            self._snapshot_head(branch)


class _RacingGitLab(_SnapshottedGitLab):
    """The server-side half of the read/apply race: a HUMAN commit lands
    while the publication's create_commit POST is in flight — the server
    then applies the publication ON TOP of it. This is the window NO
    client-side check can close; only the post-apply recheck can see it."""

    def __init__(self) -> None:
        super().__init__()
        #: Actions of the human commit that races the next publication POST.
        self.racing_human_actions: list[dict] = []

    async def create_commit(
        self,
        project_id: int,
        branch: str,
        actions: list[dict],
        commit_message: str,
        start_branch: str | None = None,
    ) -> dict:
        if self.racing_human_actions and "forge: publish candidate" in commit_message:
            # the publication's POST is now in flight: the human commit lands
            # server-side FIRST, and the server applies ours ON TOP of it
            racing, self.racing_human_actions = self.racing_human_actions, []
            await super().create_commit(
                project_id, branch, racing, "human edit racing the publication POST"
            )
        return await super().create_commit(
            project_id, branch, actions, commit_message, start_branch
        )


class _ForbiddenReadGitLab(_SnapshottedGitLab):
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


async def _read_payload(fake: _SnapshottedGitLab, ref: str) -> str | None:
    """The payload file's content at *ref* (None when the provider proves
    it absent) — the CONTENT-level assertion helper (a blob read, not a
    history scan)."""
    blob = await fake.read_blob(101, DEFAULT_PAYLOAD_PATH, ref=ref)
    if blob.status == "not_found":
        return None
    assert blob.status == "found", blob.detail
    return str(blob.content)


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


# ---------------------------------------------------------------------------
# R38-13 / #314 — payload preconditions: recorded beside the expected head,
# rechecked before AND after the server apply, typed on every drift shape.
# ---------------------------------------------------------------------------


class TestPayloadPreconditions:
    async def test_the_commit_journals_payload_preconditions_beside_the_expected_head(self):
        fake = _seeded_gitlab()
        fake.seed_file(DEFAULT_PAYLOAD_PATH, "the previous attempt's payload")
        effects = _gitlab_effects(fake)
        await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        (entry,) = effects.effects_for(PRODUCER_REPO)
        assert entry["parent"] == PRODUCER_BASE  # the expected head, as ever
        assert entry["payload_base"] == payload_digest("the previous attempt's payload")
        assert entry["file_versions"] == {
            DEFAULT_PAYLOAD_PATH: payload_digest("the previous attempt's payload")
        }
        assert "content_conflict" not in entry  # clean effect, journaled clean

    async def test_the_preconditions_record_proved_absence_for_a_create(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        (entry,) = effects.effects_for(PRODUCER_REPO)
        assert entry["payload_base"] == ""  # the preflight read PROVED absence
        assert entry["file_versions"] == {DEFAULT_PAYLOAD_PATH: ""}

    async def test_a_same_file_window_edit_is_refused_before_any_effect(self):
        """The review's named arm: the human edits THE SAME FILE between the
        preflight read and the server apply — refused BEFORE any effect, the
        human's content still the branch HEAD (verified by a blob read, not
        by scanning the commit history)."""
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        human = "a person edited the payload file inside the window\n"
        effects.in_window_human_edits = {PRODUCER_REPO: ((DEFAULT_PAYLOAD_PATH, human),)}
        with pytest.raises(ContentConflictError, match="refusing before any effect"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        # CONTENT preserved at the head — a blob read at the HEAD COMMIT,
        # not a commit-history scan
        head_sha = (await effects.branch_history(PRODUCER_REPO, PRODUCER_BRANCH))[-1]
        assert await _read_payload(fake, head_sha) == human
        # the history: base + the human commit, nothing of the saga's
        history = await effects.branch_history(PRODUCER_REPO, PRODUCER_BRANCH)
        assert len(history) == 2
        assert MARKER not in (await effects.list_commits(PRODUCER_REPO, PRODUCER_BRANCH))[0].message
        assert effects.merge_request_creates(PRODUCER_REPO) == 0

    async def test_a_changed_payload_under_an_update_is_a_content_conflict(self):
        fake = _seeded_gitlab()
        fake.seed_file(DEFAULT_PAYLOAD_PATH, "the previous attempt's payload")
        effects = _gitlab_effects(fake)
        effects.in_window_human_edits = {
            PRODUCER_REPO: ((DEFAULT_PAYLOAD_PATH, "a person rewrote it mid-window\n"),)
        }
        with pytest.raises(ContentConflictError, match="changed inside the publication"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)

    async def test_a_payload_created_under_a_proved_absence_is_a_content_conflict(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        # the preflight PROVED the file absent (payload_base ""); the window
        # edit creates it underneath — the create's base no longer holds
        effects.in_window_human_edits = {
            PRODUCER_REPO: ((DEFAULT_PAYLOAD_PATH, "created underneath the create\n"),)
        }
        with pytest.raises(ContentConflictError, match="was absent at preflight"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)

    async def test_a_payload_deleted_under_an_update_is_a_content_conflict(self):
        fake = _seeded_gitlab()
        fake.seed_file(DEFAULT_PAYLOAD_PATH, "the previous attempt's payload")
        effects = _gitlab_effects(fake)
        effects.in_window_human_edits = {PRODUCER_REPO: ((DEFAULT_PAYLOAD_PATH, None),)}
        with pytest.raises(ContentConflictError, match="deleted inside the window"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)

    async def test_a_same_file_race_that_lands_server_side_surfaces_typed(self):
        """The window NO client-side check can close: the human commit lands
        while the POST is in flight, so the server applies the publication ON
        TOP of it. The post-apply recheck reads the payload back AT THE
        PARENT revision and surfaces the typed conflict on the landed effect
        — never a silent full-file replacement, never claimed clean."""
        fake = _RacingGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        fake.seed_file(DEFAULT_PAYLOAD_PATH, "the previous attempt's payload")
        effects = _gitlab_effects(fake)
        fake.racing_human_actions = [
            {
                "action": "update",
                "file_path": DEFAULT_PAYLOAD_PATH,
                "content": "the human's same-file edit\n",
            }
        ]
        with pytest.raises(ContentConflictError, match="landed"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        history = await effects.branch_history(PRODUCER_REPO, PRODUCER_BRANCH)
        assert len(history) == 3  # base -> human -> the conflicted publication commit
        human_sha = history[1]
        # the concurrent CONTENT is preserved AT THE PARENT revision — a blob
        # read at that sha, not a commit-history assertion
        assert await _read_payload(fake, human_sha) == "the human's same-file edit\n"
        # the landed effect is journaled as what it is: conflicted, never clean
        (entry,) = effects.effects_for(PRODUCER_REPO)
        assert entry["content_conflict"] is True
        assert entry["parent"] == human_sha

    async def test_an_unrelated_race_lands_with_the_change_preserved(self):
        """The negative schedule: the human edits an UNRELATED file inside
        the window — the publication PROCEEDS on top of the human commit and
        the unrelated content survives (content-verified at the head), with
        the preconditions proving the payload file itself never drifted."""
        fake = _RacingGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        fake.seed_file(DEFAULT_PAYLOAD_PATH, "the previous attempt's payload")
        effects = _gitlab_effects(fake)
        fake.racing_human_actions = [
            {"action": "create", "file_path": "HUMAN_WINDOW_EDIT.md", "content": "kept\n"}
        ]
        sha = await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        history = await effects.branch_history(PRODUCER_REPO, PRODUCER_BRANCH)
        assert history == (PRODUCER_BASE, history[1], sha)  # oldest first
        human_sha = history[1]
        (entry,) = effects.effects_for(PRODUCER_REPO)
        assert entry["parent"] == human_sha  # applied on the human's commit
        assert entry["payload_base"] == payload_digest("the previous attempt's payload")
        assert "content_conflict" not in entry
        # CONTENT assertions, independently of history: the unrelated change
        # is still readable at the head, and the payload is the intended one
        blob = await fake.read_blob(101, "HUMAN_WINDOW_EDIT.md", ref=PRODUCER_BRANCH)
        assert blob.status == "found" and str(blob.content) == "kept\n"
        assert await _read_payload(fake, PRODUCER_BRANCH) == payload_document(MARKER)

    async def test_the_same_file_window_conflict_books_typed_and_opens_no_review(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        effects.in_window_human_edits = {
            PRODUCER_REPO: ((DEFAULT_PAYLOAD_PATH, "the human's edit\n"),)
        }
        store = InMemorySagaStore()
        saga = _begun_saga()
        await store.save(saga)
        finished = await SagaCoordinator(store, effects).run(saga, current_publication_epoch=1)
        producer = finished.repo(PRODUCER_REPO)
        assert producer is not None
        assert producer.status == "failed"
        assert CONTENT_CONFLICT_TAG in producer.note  # the explicit conflict
        assert effects.merge_request_creates(PRODUCER_REPO) == 0  # never claimed published
        head_sha = (await effects.branch_history(PRODUCER_REPO, PRODUCER_BRANCH))[-1]
        assert await _read_payload(fake, head_sha) == "the human's edit\n"


# ---------------------------------------------------------------------------
# R38-13 — single-writer exclusivity: the policy provider's CAS stand-in.
# ---------------------------------------------------------------------------


class _SlowApplyGitLab(_SnapshottedGitLab):
    """A fake whose apply yields — the second writer enters while the first
    still holds the preflight→apply→recheck window (a deterministic
    interleaving the no-await fakes cannot produce)."""

    async def create_commit(
        self,
        project_id: int,
        branch: str,
        actions: list[dict],
        commit_message: str,
        start_branch: str | None = None,
    ) -> dict:
        await asyncio.sleep(0)
        return await super().create_commit(
            project_id, branch, actions, commit_message, start_branch
        )


class TestWriterExclusivity:
    async def test_a_second_writer_on_the_same_branch_is_refused_typed(self):
        fake = _SlowApplyGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        effects = _gitlab_effects(fake)

        async def _first_writer() -> str:
            return await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)

        async def _second_writer():
            with pytest.raises(WriterExclusivityError, match=WRITER_EXCLUSIVITY_TAG):
                await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)

        first, _ = await asyncio.gather(_first_writer(), _second_writer())
        assert first  # the first writer completed
        assert len(fake.calls_of("create_commit")) == 1  # exactly one mutation

    async def test_the_window_is_per_branch_not_global(self):
        fake = _SlowApplyGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        fake.seed_commit(CONSUMER_BRANCH, CONSUMER_BASE, "base", [])
        effects = _gitlab_effects(fake)
        producer_sha, consumer_sha = await asyncio.gather(
            effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER),
            effects.commit(CONSUMER_REPO, CONSUMER_BRANCH, MARKER),
        )
        assert producer_sha and consumer_sha  # different branches never interleave-block

    async def test_a_second_writer_parks_typed_through_the_coordinator(self):
        fake = _SlowApplyGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        fake.seed_commit(CONSUMER_BRANCH, CONSUMER_BASE, "base", [])
        effects = _gitlab_effects(fake)
        store = InMemorySagaStore()
        saga = _begun_saga()
        await store.save(saga)
        first = asyncio.create_task(effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER))
        await asyncio.sleep(0)  # the first writer now holds the producer's window
        booked = await SagaCoordinator(store, effects).run(saga, current_publication_epoch=1)
        producer = booked.repo(PRODUCER_REPO)
        assert producer is not None
        assert producer.status == "failed"
        assert WRITER_EXCLUSIVITY_TAG in producer.note  # typed, surfaced, tracked
        assert await first  # the in-flight writer completed its single mutation
        producer_commits = [call for call in fake.calls_of("create_commit") if call[1][0] == 101]
        assert len(producer_commits) == 1  # exactly one mutation on the contended branch


# ---------------------------------------------------------------------------
# R38-13 — content-level adoption: a matching commit message proves nothing.
# ---------------------------------------------------------------------------


class TestAdoptionContentVerification:
    async def test_a_forged_message_with_wrong_content_is_not_carried(self):
        fake = _seeded_gitlab()
        forged = "f" * 40
        fake.seed_file(DEFAULT_PAYLOAD_PATH, "tampered payload content\n")
        fake.seed_commit(
            PRODUCER_BRANCH, forged, f"forge: publish candidate\n\n({MARKER})", [PRODUCER_BASE]
        )
        effects = _gitlab_effects(fake)
        # the MESSAGE carries the marker; the CONTENT does not match —
        # correlation must refuse to see the effect
        listed = await effects.list_commits(PRODUCER_REPO, PRODUCER_BRANCH)
        assert MARKER in listed[-1].message  # newest first in the oldest-first listing
        assert await effects.commits_carrying(PRODUCER_REPO, PRODUCER_BRANCH, MARKER) == ()
        assert await effects.head_carries_marker(PRODUCER_REPO, PRODUCER_BRANCH, MARKER) is False

    async def test_a_forged_message_with_no_payload_is_not_carried(self):
        fake = _seeded_gitlab()
        fake.seed_commit(
            PRODUCER_BRANCH, "f" * 40, f"forge: publish candidate\n\n({MARKER})", [PRODUCER_BASE]
        )
        effects = _gitlab_effects(fake)
        assert await effects.head_carries_marker(PRODUCER_REPO, PRODUCER_BRANCH, MARKER) is False

    async def test_a_forged_marker_commit_is_parked_not_adopted(self):
        """The booking that matters: recovery facing a forged (or foreign)
        marker-message commit with the wrong content cannot ADOPT it — the
        repository parks for a human instead of claiming the effect."""
        fake = _seeded_gitlab()
        fake.seed_file(DEFAULT_PAYLOAD_PATH, "tampered payload content\n")
        fake.seed_commit(
            PRODUCER_BRANCH, "f" * 40, f"forge: publish candidate\n\n({MARKER})", [PRODUCER_BASE]
        )
        effects = _gitlab_effects(fake)
        store = InMemorySagaStore()
        saga = _begun_saga()
        intended = record_commit_intent(saga, PRODUCER_REPO)
        await store.save(intended)
        recovered = await SagaCoordinator(store, effects).run(intended, current_publication_epoch=1)
        producer = recovered.repo(PRODUCER_REPO)
        assert producer is not None
        assert producer.status == "parked_human"  # uncertain, never claimed adopted
        assert producer.adopted is False
        assert producer.review_url is None
        assert effects.merge_request_creates(PRODUCER_REPO) == 0

    async def test_a_content_verified_commit_is_carried_and_adopts(self):
        fake = _seeded_gitlab()
        effects = _gitlab_effects(fake)
        store = InMemorySagaStore()
        saga = _begun_saga()
        await store.save(saga)
        finished = await SagaCoordinator(store, effects).run(saga, current_publication_epoch=1)
        landed = await effects.commits_carrying(
            PRODUCER_REPO, PRODUCER_BRANCH, finished.commit_marker
        )
        assert len(landed) == 1  # our own commit: message AND content verified

    def test_the_durable_note_tags_are_pinned_to_the_typed_errors(self):
        from forge.adaptive import saga_durable

        assert saga_durable._CONTENT_CONFLICT_NOTE == CONTENT_CONFLICT_TAG
        assert saga_durable._WRITER_EXCLUSIVITY_NOTE == WRITER_EXCLUSIVITY_TAG


# ---------------------------------------------------------------------------
# R38-13 — no blind redispatch: a lost response stays unresolved until a
# content-verified probe resolves it (ADR-0005).
# ---------------------------------------------------------------------------


class TestNoBlindRedispatch:
    async def test_a_delayed_apply_beyond_the_timeout_is_never_redispatched(self):
        fake = _SnapshottedGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        fake.seed_commit(CONSUMER_BRANCH, CONSUMER_BASE, "base", [])
        fake.delayed_apply = {PRODUCER_BRANCH}  # accepted, applied LATER
        effects = _gitlab_effects(fake)
        store = InMemorySagaStore()
        saga = _begun_saga()
        await store.save(saga)

        # pass 1: the response times out with the mutation accepted but not
        # yet applied — the honest outcome_unknown
        first = await SagaCoordinator(store, effects).run(saga, current_publication_epoch=1)
        producer = first.repo(PRODUCER_REPO)
        assert producer is not None and producer.status == "outcome_unknown"
        assert effects.commit_calls[PRODUCER_REPO] == 1

        # pass 2: the probe is negative (nothing has landed yet) — the same
        # mutation is NOT blindly redispatched; the pass stops fail-closed
        with pytest.raises(ProviderUnavailableError, match="blindly redispatch"):
            await SagaCoordinator(store, effects).run(first, current_publication_epoch=1)
        # ONE provider mutation, still (the refused re-entry never reached
        # the transport — commit_calls counts attempts, the fake counts sends)
        producer_sends = [call for call in fake.calls_of("create_commit") if call[1][0] == 101]
        assert len(producer_sends) == 1

        # the delayed apply finally lands — and ONLY now does recovery move,
        # adopting by content-verified correlation (no second commit, ever)
        fake.flush_delayed_apply(PRODUCER_BRANCH)
        recovered = await SagaCoordinator(store, effects).run(first, current_publication_epoch=1)
        producer = recovered.repo(PRODUCER_REPO)
        assert producer is not None
        assert producer.status == "ready_for_review"
        assert producer.adopted is True
        assert len(producer_sends) == 1  # exactly ONE mutation was ever sent
        landed = await effects.commits_carrying(
            PRODUCER_REPO, PRODUCER_BRANCH, recovered.commit_marker
        )
        assert len(landed) == 1

    async def test_a_definitive_new_attempt_with_a_new_marker_may_proceed(self):
        """The operator's unstick route: an explicit NEW attempt (a new
        marker) is a different mutation and is never refused."""
        fake = _SnapshottedGitLab()
        fake.seed_commit(PRODUCER_BRANCH, PRODUCER_BASE, "base", [])
        effects = GitLabNativeEffects(fake, {PRODUCER_REPO: 101})
        effects.pin_expected_head(PRODUCER_REPO, PRODUCER_BRANCH, PRODUCER_BASE)
        effects._mark_mutation_unresolved(
            PRODUCER_REPO, PRODUCER_BRANCH, MARKER, "create_commit outcome unknown"
        )
        with pytest.raises(ProviderUnavailableError, match="blindly redispatch"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        sha = await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, "forge-saga:new-attempt:abcd")
        assert sha


# ---------------------------------------------------------------------------
# R38-13 — the observability of the typed conflicts (outbox events).
# ---------------------------------------------------------------------------


class TestConflictObservability:
    def _document(self, status: str, note: str) -> dict:
        return {
            "schema": "forge.saga.durable/1",
            "saga_id": "saga-x",
            "work_id": "wp",
            "publication_epoch": 1,
            "candidate_digest": "d" * 64,
            "repos": [
                {
                    "repository_id": PRODUCER_REPO,
                    "branch": PRODUCER_BRANCH,
                    "expected_base_oid": PRODUCER_BASE,
                    "status": status,
                    "note": note,
                }
            ],
            "steps": [],
        }

    def test_a_failed_booking_with_the_conflict_tag_emits_the_typed_event(self):
        from forge.adaptive.saga_durable import _observability_events

        before = self._document("intent_recorded", "")
        after = self._document("failed", f"provider refused: {CONTENT_CONFLICT_TAG} (422) …")
        events = _observability_events(before, saga_from_document(after))
        assert events[0][0] == "saga.content_conflict"
        payload = events[0][1]
        assert payload["repository_id"] == PRODUCER_REPO
        assert CONTENT_CONFLICT_TAG in payload["conflict"]

    def test_a_failed_booking_with_the_exclusivity_tag_emits_the_typed_event(self):
        from forge.adaptive.saga_durable import _observability_events

        before = self._document("preparing", "")
        after = self._document("failed", f"provider refused: {WRITER_EXCLUSIVITY_TAG}: …")
        events = _observability_events(before, saga_from_document(after))
        assert events[0][0] == "saga.writer_exclusivity"

    def test_a_plain_refusal_emits_no_typed_conflict_event(self):
        from forge.adaptive.saga_durable import _observability_events

        before = self._document("intent_recorded", "")
        after = self._document("failed", "provider refused: gitlab refused (400): boom")
        assert _observability_events(before, saga_from_document(after)) == []


# ---------------------------------------------------------------------------
# R38-13 — the GitHub row of the matrix: the window is closed NATIVELY.
# ---------------------------------------------------------------------------


class TestGithubNativeWindow:
    async def test_the_window_race_is_refused_by_the_server_cas(self):
        """No client-side precondition dance on GitHub: a human commit inside
        the window moves the head, and the SERVER refuses the publication
        commit outright (STALE_DATA) — nothing of ours lands, nothing of the
        human's is touched."""
        fake = _seeded_github(FakeGitHub())
        effects = _github_effects(fake)
        fake.seed_commit(
            "acme/producer", PRODUCER_BRANCH, "c" * 40, "human edit in the window", [PRODUCER_BASE]
        )
        with pytest.raises(ProviderRejectedError, match="native createCommitOnBranch CAS"):
            await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        assert DEFAULT_PAYLOAD_PATH not in fake.files.get("acme/producer", {})
        heads = fake.commits["acme/producer"][PRODUCER_BRANCH]
        assert [commit["sha"] for commit in heads] == ["c" * 40, PRODUCER_BASE]

    async def test_the_journal_records_the_intended_payload_beside_the_cas_token(self):
        """GitHub's client offers no repository blob read, so no preflight
        base exists to record — what the journal keeps is the INTENDED
        payload digest beside the CAS token (the audit's half of the
        content story), and the matrix says adoption stays marker+parent."""
        fake = _seeded_github(FakeGitHub())
        effects = _github_effects(fake)
        sha = await effects.commit(PRODUCER_REPO, PRODUCER_BRANCH, MARKER)
        (entry,) = effects.effects_for(PRODUCER_REPO)
        assert entry["sha"] == sha
        assert entry["parent"] == PRODUCER_BASE  # the CAS token WAS the base
        intended = payload_digest(payload_document(MARKER))
        assert entry["payload_base"] == intended
        assert entry["file_versions"] == {DEFAULT_PAYLOAD_PATH: intended}
