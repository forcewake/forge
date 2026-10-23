"""Retained native-adapter contracts after the test-only saga is removed."""

import pytest

from forge.durable import op_marker
from forge.integrations.github_flow import GitHubPublishFlow, github_factory_branch
from forge.repository import ChangesetWriter
from forge.repository.writer import BranchDriftError
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.publication_ports import (
    BASE,
    BRANCH,
    PROJECT,
    RUN_ID,
    changeset,
    dispatched_intent,
    publication_db as publication_db,
    read_intent,
    remote,
)

REPO = "acme/widgets"
GH_BRANCH = github_factory_branch(7, RUN_ID)


def github():
    fake = FakeGitHub()
    fake.seed_repo(REPO, {"src/app.py": "VALUE = 0\n"})
    fake.heads[REPO]["main"] = BASE
    return fake


async def publish(fake, *, key="nativeeffect123", path="src/feature.py"):
    return await GitHubPublishFlow(fake).publish_changeset(
        "acme",
        "widgets",
        issue_number=7,
        run_id=RUN_ID,
        changeset=changeset(GH_BRANCH, path),
        expected_head=BASE,
        operation_key=key,
    )


async def test_gitlab_human_tip_is_preserved_before_dispatch(publication_db):
    fake = remote()
    fake.seed_commit(BRANCH, "human", "human edit", parents=[BASE])
    with pytest.raises(BranchDriftError):
        await ChangesetWriter(fake, publication_db, PROJECT).apply(
            RUN_ID,
            changeset(),
            expected_head=BASE,
        )
    assert fake.branches[BRANCH][0]["sha"] == "human"
    assert not fake.calls_of("create_commit")


async def test_recovery_foreign_tip_resolves_duplicated_without_overwrite(publication_db):
    fake = remote()
    intent = await dispatched_intent(publication_db)
    fake.seed_commit(BRANCH, "human", "human edit", parents=[BASE])
    with pytest.raises(BranchDriftError):
        await ChangesetWriter(fake, publication_db, PROJECT).apply(
            RUN_ID,
            changeset(),
            expected_head=BASE,
        )
    assert (await read_intent(publication_db, intent.id)).status == "duplicated"
    assert [row["sha"] for row in fake.branches[BRANCH]] == ["human", BASE]
    assert not fake.calls_of("create_commit")


async def test_github_landed_response_is_adopted_by_marker_and_parent():
    fake = github()
    first = await publish(fake)
    second = await publish(fake, path="src/recovery.py")
    assert first.ok and not first.adopted
    assert second.ok and second.adopted
    assert second.commit_oid == first.commit_oid
    assert len(fake.commits[REPO][GH_BRANCH]) == 1
    assert len(fake.prs_for(REPO, GH_BRANCH)) == 1


async def test_github_human_tip_is_never_adopted_or_overwritten():
    fake = github()
    fake.seed_commit(REPO, GH_BRANCH, "human", "human edit", parents=[BASE])
    outcome = await publish(fake)
    assert not outcome.ok and outcome.drift and not outcome.adopted
    assert fake.heads[REPO][GH_BRANCH] == "human"
    assert len(fake.calls_of("create_commit_on_branch")) == 1


@pytest.mark.parametrize("parent", [BASE, "wrong-parent"])
async def test_github_ambiguous_or_wrong_parent_marker_does_not_adopt(parent):
    fake = github()
    fake.seed_commit(REPO, GH_BRANCH, "candidate-1", op_marker("nativeeffect123"), parents=[parent])
    if parent == BASE:
        fake.seed_commit(
            REPO, GH_BRANCH, "candidate-2", op_marker("nativeeffect123"), parents=[BASE]
        )
    outcome = await publish(fake)
    assert not outcome.ok and outcome.drift and not outcome.adopted
    assert len(fake.calls_of("create_commit_on_branch")) == 1


async def test_github_native_cas_refuses_a_human_edit_inside_write_window():
    fake = github()
    original = fake.create_commit_on_branch

    async def racing(*args, **kwargs):
        fake.seed_commit(REPO, GH_BRANCH, "human", "human edit", parents=[BASE])
        return await original(*args, **kwargs)

    fake.create_commit_on_branch = racing
    outcome = await publish(fake)
    assert not outcome.ok and outcome.drift
    assert fake.heads[REPO][GH_BRANCH] == "human"
    assert len(fake.commits[REPO][GH_BRANCH]) == 1
    assert len(fake.calls_of("create_commit_on_branch")) == 1


async def test_delayed_github_write_is_fenced_when_another_write_lands():
    fake = github()
    fake.delayed_apply = True
    first = await publish(fake)
    assert first.ok and fake.heads[REPO][GH_BRANCH] == BASE
    fake.delayed_apply = False
    second = await publish(fake, path="src/recovery.py")
    assert second.ok and not second.adopted
    fake.flush_delayed_apply(GH_BRANCH)
    assert fake.delayed_refused == 1
    assert len(fake.commits[REPO][GH_BRANCH]) == 1
    assert fake.heads[REPO][GH_BRANCH] == second.commit_oid
