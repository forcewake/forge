"""R37-14 / #295 — the durable two-writer saga through REAL native
provider effects: real PostgreSQL + the live lab GitLab CE.

R36-18 (``test_two_writer_durable.py``) ran the SAME durable saga over
the provider-shaped ``NativeShapedRemote`` — an in-process reference
with chosen duplicate/CAS semantics. This file is the issue's own
negative-test core: the REAL :class:`DurablePublicationEntry` +
:class:`SagaCoordinator` over the REAL
:class:`forge.gitlab.client.GitLabClient` (the existing native client,
injected behind :class:`forge.adaptive.saga_native.GitLabNativeEffects`),
against DISPOSABLE lab projects created and deleted by the fixture
(``forge-tw-<uuid>-producer``/``-consumer`` — bounded to the token's own
namespace, never project 68 or anything else the lab owns):

- **the kill matrix**: death at the store's COMMIT BOUNDARY after every
  saga step of the producer (``fence_check`` shares ``commit_intent``'s
  save — the real coordinator's own batching) through the REAL
  coordinator + the NATIVE transport; every cell must restart-and-
  converge with exactly ONE commit carrying the marker in the ACTUAL
  listed history, ONE merge request per repository, prefix-preserved
  histories and an inert third pass.
- **lost first-repo commit response**: the response dies after GitLab
  accepted the commit; the restarted drive ADOPTS through native
  correlation (the listed commit's message marker + parent oid) — no
  duplicate commit in the actual history, exactly one provider write.
- **human branch edit between partial publication and recovery**: a
  real commit pushed to the consumer's publication branch while the
  coordinator is dead; recovery PARKS the repository (the conflict
  decision is surfaced), the human commit stays the head, nothing is
  forced anywhere.
- **two recovering processes / a definitive second-repo failure**: the
  consumer's review targets its own source branch — GitLab's REAL 400
  ("You can't use same project/branch for source and target") — leaving
  the producer's reviewable effect standing as an honest durable
  PARTIAL state; the second recovering process spends nothing and
  duplicates nothing.
- **the bot never merges**: every review the run created stays OPEN and
  DRAFT with an empty ``merged_at`` — asserted from the provider's own
  listing, not from local state.

Gating is honest: real PostgreSQL under ``FORGE_PG_TEST_URL`` (the FI
convention — point it at a DISPOSABLE database, the pe_db fixture
resets its whole public schema per test) AND the live GitLab under
``FORGE_GITLAB_LIVE_URL``/``FORGE_GITLAB_LIVE_TOKEN``. Missing either
skips visibly; nothing runs half-native.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from typing import Any, NamedTuple

import httpx
import pytest

from forge.adaptive.saga_durable import (
    DurablePublicationEntry,
    ProcessDied,
    kill_at_boundary,
)
from forge.adaptive.saga_native import GitLabNativeEffects
from forge.adaptive.two_writer_qualification import TwoWriterScenario, default_scenario
from forge.adaptive.workpackage_service import ChildSubject
from forge.gitlab.client import GitLabClient

pytestmark = pytest.mark.production_entry

_LIVE_URL = os.environ.get("FORGE_GITLAB_LIVE_URL", "")
_LIVE_TOKEN = os.environ.get("FORGE_GITLAB_LIVE_TOKEN", "")
_LIVE_REASON = (
    "FORGE_PG_TEST_URL and FORGE_GITLAB_LIVE_URL/FORGE_GITLAB_LIVE_TOKEN not both"
    " set — the R37-14 native failpoint matrix runs only against a disposable real"
    " Postgres AND the live lab GitLab (disposable forge-tw-* projects)"
)

#: The producer-arm kill cells: every journaled step except ``fence_check``,
#: which shares ``commit_intent``'s durable save (the coordinator's own
#: batching — see ``kill_at_boundary``).
KILL_STEPS = ("prepare", "commit_intent", "provider_commit", "verify", "record")

PRODUCER = "repo-producer"
CONSUMER = "repo-consumer"

#: Short step codes for the kill matrix's scenario ids: ``FlowRun.id`` is a
#: varchar(32) and the durable parent run id is ``run-<scenario_id>``, so the
#: scenario id budget is 28 characters (SQLite does not enforce the length —
#: real PostgreSQL does, which is exactly what the live run is for).
_STEP_CODE = {
    "prepare": "pr",
    "commit_intent": "ci",
    "provider_commit": "pc",
    "verify": "vf",
    "record": "rc",
}


# ---------------------------------------------------------------------------
# The disposable live lab: two throwaway projects, deleted on teardown.
# ---------------------------------------------------------------------------


class LiveGitLabLab:
    """The live lab handle: the REAL client plus two disposable projects."""

    def __init__(self, client: GitLabClient, producer_project: int, consumer_project: int):
        self.client = client
        self.producer_project = producer_project
        self.consumer_project = consumer_project

    def projects(self) -> dict[str, int]:
        return {PRODUCER: self.producer_project, CONSUMER: self.consumer_project}

    async def new_publication_branch(self, project: int, label: str) -> tuple[str, str]:
        """A FRESH publication branch off ``main`` — ``(branch, base head)``.

        A dedicated branch per arm keeps the live matrix isolated without
        touching ``main`` of anything: the publication commits and MRs ride
        branches the fixture owns."""
        branch = f"forge/tw-native/{label}/{uuid.uuid4().hex[:8]}"
        await self.client.create_branch(project, branch, ref="main")
        base = await self.client.get_branch_head(project, branch)
        return branch, base


class LiveProjects(NamedTuple):
    """The module's two disposable projects (created sync, deleted sync)."""

    url: str
    token: str
    producer: int
    consumer: int


@pytest.fixture(scope="module")
def live_projects():
    """Two DISPOSABLE projects under the token's own namespace (never an
    existing lab project such as 68); every review/commit this module
    creates lives inside them and the projects are DELETED at teardown.

    Project create/delete rides a SYNC admin client in the module-scoped
    fixture on purpose: a module-scoped ASYNC teardown would run on a
    closed function-scoped event loop. The async GitLabClient lives in
    the per-test ``live_lab`` fixture, which tears down on its own loop."""
    if not (_LIVE_URL and _LIVE_TOKEN):
        pytest.skip(_LIVE_REASON)
    suffix = uuid.uuid4().hex[:8]
    admin = httpx.Client(
        base_url=f"{_LIVE_URL.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": _LIVE_TOKEN},
        timeout=60.0,
    )
    created: list[int] = []
    try:
        namespace_id = int(admin.get("/user").json()["namespace_id"])
        for role in ("producer", "consumer"):
            response = admin.post(
                "/projects",
                json={
                    "name": f"forge-tw-{suffix}-{role}",
                    "namespace_id": namespace_id,
                    "initialize_with_readme": True,
                    "description": (
                        "R37-14 disposable two-writer-native failpoint matrix project"
                        " — deleted by the fixture that created it"
                    ),
                },
            )
            response.raise_for_status()
            created.append(int(response.json()["id"]))
        yield LiveProjects(_LIVE_URL, _LIVE_TOKEN, created[0], created[1])
    finally:
        for project_id in created:
            admin.delete(f"/projects/{project_id}")  # 202 Accepted
        admin.close()


@pytest.fixture()
async def live_lab(live_projects: LiveProjects):
    """The per-test handle: the REAL GitLabClient over the disposable
    projects (a fresh client per test — closed on the test's own loop)."""
    client = GitLabClient(base_url=live_projects.url, token=live_projects.token, timeout=60.0)
    try:
        yield LiveGitLabLab(client, live_projects.producer, live_projects.consumer)
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Scenario/entry builders over the live branches.
# ---------------------------------------------------------------------------


def _live_scenario(
    scenario_id: str,
    producer: tuple[str, str],
    consumer: tuple[str, str],
) -> TwoWriterScenario:
    """The default two-writer scenario with the WRITERS on live branches:
    real base oids (the branch heads), real branch names, a unique id per
    arm (a unique durable parent run). The read-only members stay
    synthetic — no remote effect ever names them."""
    base = default_scenario()
    repos = []
    for repo in base.repos:
        if repo.kind == "producer":
            repos.append(replace(repo, branch=producer[0], base_oid=producer[1]))
        elif repo.kind == "consumer":
            repos.append(replace(repo, branch=consumer[0], base_oid=consumer[1]))
        else:
            repos.append(repo)
    return replace(base, scenario_id=scenario_id, repos=tuple(repos))


def _subjects(lab: LiveGitLabLab) -> dict[str, ChildSubject]:
    return {
        PRODUCER: ChildSubject(project_id=lab.producer_project, provider="gitlab"),
        CONSUMER: ChildSubject(project_id=lab.consumer_project, provider="gitlab"),
    }


def _entry(pe_db, lab: LiveGitLabLab, scenario: TwoWriterScenario, effects, **kwargs):
    return DurablePublicationEntry(
        pe_db.worker_factory(),
        scenario,
        remote=effects,
        subjects=_subjects(lab),
        **kwargs,
    )


async def _assert_one_reviewable_effect_per_repo(effects: GitLabNativeEffects, saga) -> None:
    """ONE commit carrying the marker in the ACTUAL listed history (parent ==
    the expected base — native identity, not marker-text idempotency), ONE
    created review per publication branch, and every review in the project
    OPEN/DRAFT/unmerged — the bot never merges (AT-12)."""
    for repository_id in (PRODUCER, CONSUMER):
        repo = saga.repo(repository_id)
        assert repo is not None
        landed = await effects.commits_carrying(repository_id, repo.branch, saga.commit_marker)
        assert len(landed) == 1, f"{repository_id}: duplicated logical effect in the real history"
        assert landed[0].parent == repo.expected_base_oid, (
            f"{repository_id}: the adopted commit's parent is not the expected base —"
            " correlation must be by native identity"
        )
        assert effects.commit_calls[repository_id] == 1
        assert effects.merge_request_creates(repository_id) == 1
        opened = await effects.merge_requests(repository_id)
        on_branch = [review for review in opened if review["source_branch"] == repo.branch]
        assert len(on_branch) == 1, f"{repository_id}: expected exactly one review on {repo.branch}"
        assert on_branch[0]["state"] == "opened"
        assert not on_branch[0]["merged_at"]
        assert on_branch[0]["draft"] or on_branch[0]["title"].startswith("Draft:")
        assert on_branch[0]["target_branch"] == "main"
        # every review the run EVER created in this project is still open,
        # unmerged and draft-shaped — nothing anywhere was merged
        for review in opened:
            assert review["state"] == "opened" and not review["merged_at"]
    assert effects.destructive_operations() == []


# ---------------------------------------------------------------------------
# The positive trace: one reviewable candidate per repository, live.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("FORGE_PG_TEST_URL") and _LIVE_URL and _LIVE_TOKEN),
    reason=_LIVE_REASON,
)
class TestNativePositiveTrace:
    async def test_the_full_change_publishes_two_reviewable_candidates(self, pe_db, live_lab):
        producer = await live_lab.new_publication_branch(live_lab.producer_project, "pos-p")
        consumer = await live_lab.new_publication_branch(live_lab.consumer_project, "pos-c")
        scenario = _live_scenario(f"tw2n-pos-{uuid.uuid4().hex[:6]}", producer, consumer)
        effects = GitLabNativeEffects(live_lab.client, live_lab.projects())
        entry = _entry(pe_db, live_lab, scenario, effects)
        state = await entry.start()
        assert state["phases"] == [["pinned-baseline", "producer"], ["consumer"]]
        report = await entry.drive()
        saga = await entry._saga_state()
        assert report.package_state == "complete", report.refusal
        assert str(saga.status) == "complete"
        await _assert_one_reviewable_effect_per_repo(effects, saga)
        # the consumer child launched only AFTER the producer's PERSISTED outcome
        assert [launch["item_id"] for launch in entry.child_launches] == [
            scenario.PRODUCER_ITEM,
            scenario.CONSUMER_ITEM,
        ]
        trail = await entry.outbox_events()
        producer_outcome = next(
            index
            for index, event in enumerate(trail)
            if event["event_type"] == "workpackage.outcome_recorded"
            and event["payload"]["item_id"] == scenario.PRODUCER_ITEM
        )
        consumer_intent = next(
            index
            for index, event in enumerate(trail)
            if event["event_type"] == "workpackage.child_intent"
            and event["payload"]["item_id"] == scenario.CONSUMER_ITEM
        )
        assert producer_outcome < consumer_intent


# ---------------------------------------------------------------------------
# The kill matrix: death at the store's commit boundary, native transport.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("FORGE_PG_TEST_URL") and _LIVE_URL and _LIVE_TOKEN),
    reason=_LIVE_REASON,
)
class TestNativeKillMatrix:
    @pytest.mark.parametrize("step", KILL_STEPS)
    async def test_death_after_every_step_restarts_and_converges(self, pe_db, live_lab, step):
        producer = await live_lab.new_publication_branch(live_lab.producer_project, f"km-p-{step}")
        consumer = await live_lab.new_publication_branch(live_lab.consumer_project, f"km-c-{step}")
        scenario = _live_scenario(
            f"tw2n-km-{_STEP_CODE[step]}-{uuid.uuid4().hex[:6]}", producer, consumer
        )
        effects = GitLabNativeEffects(live_lab.client, live_lab.projects())

        # process 1: the pre-crash coordinator, dying at its own save boundary
        entry_a = _entry(
            pe_db,
            live_lab,
            scenario,
            effects,
            on_boundary=kill_at_boundary(PRODUCER, step),
        )
        await entry_a.start()
        with pytest.raises(ProcessDied):
            await entry_a.drive()
        histories_at_death = {
            repository_id: await effects.branch_history(
                repository_id, (await entry_a._saga_state()).repo(repository_id).branch
            )
            for repository_id in (PRODUCER, CONSUMER)
        }

        # process 2: a genuinely fresh engine over the same rows — recovery
        entry_b = _entry(pe_db, live_lab, scenario, effects)
        report = await entry_b.drive()
        saga = await entry_b._saga_state()
        assert report.package_state == "complete", report.refusal
        assert str(saga.status) == "complete"
        await _assert_one_reviewable_effect_per_repo(effects, saga)

        # the actual histories only grew — prefix-preserved, nothing forced
        for repository_id, was in histories_at_death.items():
            now = await effects.branch_history(repository_id, saga.repo(repository_id).branch)
            assert now[: len(was)] == was
        # no duplicate MODEL work either: each child launched exactly once
        launched = [
            launch["item_id"] for launch in (*entry_a.child_launches, *entry_b.child_launches)
        ]
        assert launched.count(scenario.PRODUCER_ITEM) == 1
        assert launched.count(scenario.CONSUMER_ITEM) == 1

        # process 3: an inert third pass — no effects, no outbox rows
        journal_size = len(effects.journal)
        digest = saga.steps_digest
        events = await entry_b.outbox_events()
        entry_c = _entry(pe_db, live_lab, scenario, effects)
        await entry_c.drive()
        await entry_c.drive()
        assert len(effects.journal) == journal_size
        assert entry_c.child_launches == ()
        assert (await entry_c._saga_state()).steps_digest == digest
        assert await entry_c.outbox_events() == events


# ---------------------------------------------------------------------------
# Lost first-repo commit response — adoption through NATIVE correlation.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("FORGE_PG_TEST_URL") and _LIVE_URL and _LIVE_TOKEN),
    reason=_LIVE_REASON,
)
class TestNativeLostResponse:
    async def test_a_lost_commit_response_is_adopted_through_native_correlation(
        self, pe_db, live_lab
    ):
        producer = await live_lab.new_publication_branch(live_lab.producer_project, "lost-p")
        consumer = await live_lab.new_publication_branch(live_lab.consumer_project, "lost-c")
        scenario = _live_scenario(f"tw2n-lost-{uuid.uuid4().hex[:6]}", producer, consumer)
        effects = GitLabNativeEffects(live_lab.client, live_lab.projects())
        effects.lose_commit_response = {PRODUCER}  # the response dies, the effect lands
        entry = _entry(pe_db, live_lab, scenario, effects)
        await entry.start()
        report = await entry.drive()  # the drive reconciles within its passes
        saga = await entry._saga_state()
        assert report.package_state == "complete", report.refusal
        repo = saga.repo(PRODUCER)
        assert repo is not None and repo.adopted is True
        await _assert_one_reviewable_effect_per_repo(effects, saga)
        events = [event["event_type"] for event in await entry.outbox_events()]
        assert "saga.unknown_effects" in events
        assert "recovery.native_adoption" in events


# ---------------------------------------------------------------------------
# Human branch edit between partial publication and recovery.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("FORGE_PG_TEST_URL") and _LIVE_URL and _LIVE_TOKEN),
    reason=_LIVE_REASON,
)
class TestNativeHumanEdit:
    async def test_a_human_edit_is_preserved_and_parked_never_forced(self, pe_db, live_lab):
        producer = await live_lab.new_publication_branch(live_lab.producer_project, "human-p")
        consumer = await live_lab.new_publication_branch(live_lab.consumer_project, "human-c")
        scenario = _live_scenario(f"tw2n-hum-{uuid.uuid4().hex[:6]}", producer, consumer)
        effects = GitLabNativeEffects(live_lab.client, live_lab.projects())

        # process 1 dies INSIDE the consumer's open effect window (the intent
        # is durable, the commit never happened)
        entry_a = _entry(
            pe_db,
            live_lab,
            scenario,
            effects,
            on_boundary=kill_at_boundary(CONSUMER, "commit_intent"),
        )
        await entry_a.start()
        with pytest.raises(ProcessDied):
            await entry_a.drive()

        # while the coordinator is dead, a person pushes to the consumer's
        # PUBLICATION branch — a real commit through the real API
        human = await live_lab.client.create_commit(
            live_lab.consumer_project,
            consumer[0],
            [
                {
                    "action": "create",
                    "file_path": "HUMAN_EDIT.md",
                    "content": "a person edited this branch while the coordinator was dead\n",
                }
            ],
            "human edit while the coordinator was dead",
        )
        human_sha = str(human["id"])
        consumer_history_at_edit = await effects.branch_history(CONSUMER, consumer[0])
        assert consumer_history_at_edit[-1] == human_sha

        # process 2: recovery parks the consumer — the conflict decision is
        # surfaced, the branch is left exactly as the human left it
        entry_b = _entry(pe_db, live_lab, scenario, effects)
        report = await entry_b.drive()
        saga = await entry_b._saga_state()
        repo = saga.repo(CONSUMER)
        assert repo is not None and repo.status == "parked_human"
        assert "never force-overwritten" in repo.note
        assert report.package_state != "complete"
        events = [event["event_type"] for event in await entry_b.outbox_events()]
        assert "human_edit.conflicts" in events
        partial = await entry_b.partial_publication()
        assert [effect.repository_id for effect in partial.standing] == [PRODUCER]
        assert [effect.repository_id for effect in partial.outstanding] == [CONSUMER]
        # the producer's reviewable effect stands: one commit, one review
        producer_repo = saga.repo(PRODUCER)
        assert producer_repo is not None and producer_repo.status == "ready_for_review"
        producer_landed = await effects.commits_carrying(PRODUCER, producer[0], saga.commit_marker)
        assert len(producer_landed) == 1
        assert effects.merge_request_creates(PRODUCER) == 1
        assert effects.merge_request_creates(CONSUMER) == 0  # never got that far
        # NOTHING was forced: the human commit is still the head, prefix intact
        now = await effects.branch_history(CONSUMER, consumer[0])
        assert now == consumer_history_at_edit
        assert now[-1] == human_sha
        assert effects.destructive_operations() == []


# ---------------------------------------------------------------------------
# Two recovering processes + a definitive second-repo failure (real 400).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("FORGE_PG_TEST_URL") and _LIVE_URL and _LIVE_TOKEN),
    reason=_LIVE_REASON,
)
class TestNativeTwoRecoverers:
    async def test_one_durable_partial_state_with_a_real_second_repo_refusal(self, pe_db, live_lab):
        producer = await live_lab.new_publication_branch(live_lab.producer_project, "two-p")
        consumer = await live_lab.new_publication_branch(live_lab.consumer_project, "two-c")
        scenario = _live_scenario(f"tw2n-two-{uuid.uuid4().hex[:6]}", producer, consumer)
        # the definitive second-repo failure: the consumer's review targets its
        # OWN source branch — GitLab's REAL 400 ("You can't use same
        # project/branch for source and target"), through the native transport
        effects = GitLabNativeEffects(
            live_lab.client,
            live_lab.projects(),
            targets={CONSUMER: consumer[0]},
        )

        entry_a = _entry(pe_db, live_lab, scenario, effects)
        await entry_a.start()
        report_a = await entry_a.drive()
        saga_a = await entry_a._saga_state()
        producer_repo = saga_a.repo(PRODUCER)
        consumer_repo = saga_a.repo(CONSUMER)
        assert producer_repo is not None and producer_repo.status == "ready_for_review"
        assert consumer_repo is not None and consumer_repo.status == "failed"
        assert "same project/branch for source and target" in consumer_repo.note
        assert report_a.package_state != "complete"
        # the producer's effect STANDS as the honest partial: one commit, one
        # review, open and draft; the consumer has its commit but no review
        producer_landed = await effects.commits_carrying(
            PRODUCER, producer[0], saga_a.commit_marker
        )
        assert len(producer_landed) == 1 and producer_landed[0].parent == producer[1]
        assert effects.merge_request_creates(PRODUCER) == 1
        assert effects.merge_request_creates(CONSUMER) == 0
        partial_a = await entry_a.partial_publication()
        assert [effect.repository_id for effect in partial_a.standing] == [PRODUCER]
        assert [effect.repository_id for effect in partial_a.outstanding] == [CONSUMER]

        # process 2: a second recovering process over the same rows — ONE
        # durable partial state, no duplicate children, no duplicate effects
        commits_after_a = dict(effects.commit_calls)
        reviews_after_a = dict(effects.review_calls)
        journal_after_a = len(effects.journal)
        entry_b = _entry(pe_db, live_lab, scenario, effects)
        report_b = await entry_b.drive()
        saga_b = await entry_b._saga_state()
        assert report_b.package_state == report_a.package_state
        assert saga_b.steps_digest == saga_a.steps_digest
        assert dict(effects.commit_calls) == commits_after_a
        assert dict(effects.review_calls) == reviews_after_a
        assert len(effects.journal) == journal_after_a
        partial_b = await entry_b.partial_publication()
        assert partial_b == partial_a
        launched = [
            launch["item_id"] for launch in (*entry_a.child_launches, *entry_b.child_launches)
        ]
        assert launched.count(scenario.PRODUCER_ITEM) == 1
        assert launched.count(scenario.CONSUMER_ITEM) == 1
        # the bot still never merges: the one standing review on this arm's
        # publication branch is open + draft, and nothing anywhere was merged
        reviews: list[dict[str, Any]] = await effects.merge_requests(PRODUCER)
        on_branch = [review for review in reviews if review["source_branch"] == producer[0]]
        assert len(on_branch) == 1
        assert on_branch[0]["state"] == "opened" and not on_branch[0]["merged_at"]
        assert all(review["state"] == "opened" and not review["merged_at"] for review in reviews)
        assert effects.destructive_operations() == []
