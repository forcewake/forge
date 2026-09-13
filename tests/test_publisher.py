"""Stage D: the trusted publisher (ADR-0016 §2).

The publisher is the single validation → publication boundary: base check,
grant check, spec-digest check, fence check, strict materialization against
authoritative base blobs, policy validation, then ONE journaled write pinned
to the frozen attempt base. Every rejection here leaves the remote untouched.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import pytest

from forge.durable import ActionLog, FlowRun, RunSpec
from forge.durable.identity import factory_branch
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.runs.candidate import parse_unified_diff
from forge.runs.publisher import publish_candidate
from tests.fixtures.fake_gitlab import FakeGitLab

PROJECT_ID = 42
ISSUE_IID = 7
BASE_SHA = "base-sha-1"


def make_run(run_id: str | None = None, **overrides) -> FlowRun:
    run = FlowRun(
        id=run_id or "0" * 32,
        project_id=PROJECT_ID,
        issue_iid=ISSUE_IID,
        base_sha=BASE_SHA,
    )
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


def bundle_for(diff: str, attempt_base: str = BASE_SHA):
    return parse_unified_diff(diff, attempt_base, "completed")


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_commit("main", BASE_SHA, "initial")
    return fake


async def persisted(db, run: FlowRun) -> FlowRun:
    async with db() as session:
        session.add(run)
        await session.commit()
    return run


async def publish(db, fake_gitlab, run, bundle, **kwargs):
    writer = ChangesetWriter(fake_gitlab, db, PROJECT_ID)
    return await publish_candidate(
        gitlab=fake_gitlab,
        session_factory=db,
        writer=writer,
        run=run,
        bundle=bundle,
        **kwargs,
    )


class TestAccepts:
    async def test_well_formed_create_is_committed_with_expected_head(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        result = await publish(
            db, fake_gitlab, run, bundle_for(_create_diff("forge-demo/x.md", "hello\n"))
        )

        assert result.ok
        branch = factory_branch(ISSUE_IID, run.id)
        head = fake_gitlab.branches[branch][0]
        assert result.commit_sha == head["sha"]
        # The published commit's parent IS the frozen attempt base: in the
        # proposal-only model nothing else ever pushed (ADR-0016 §4).
        assert head["parent_ids"] == [BASE_SHA]
        assert head["message"].startswith("forge: implement 7")

        # The journaled commit action carries the pinned expected head.
        async with db() as session:
            actions = (await session.execute(select(ActionLog))).scalars().all()
        (commit_action,) = [a for a in actions if a.action_kind == "commit"]
        assert commit_action.status == "succeeded"
        assert commit_action.remote_result["sha"] == result.commit_sha
        assert commit_action.remote_result["expected_head"] == BASE_SHA

    async def test_modify_materializes_full_content_against_base(self, db, fake_gitlab):
        fake_gitlab.seed_file("src/mod.py", "keep\nold\ntail\n")
        run = await persisted(db, make_run())
        diff = (
            "diff --git a/src/mod.py b/src/mod.py\n"
            "--- a/src/mod.py\n"
            "+++ b/src/mod.py\n"
            "@@ -1,3 +1,3 @@\n"
            " keep\n"
            "-old\n"
            "+new\n"
            " tail\n"
        )
        result = await publish(db, fake_gitlab, run, bundle_for(diff))

        assert result.ok
        (commit_call,) = fake_gitlab.calls_of("create_commit")
        (action,) = commit_call[1][2]
        assert action["action"] == "update"
        # FULL replacement text, not a hunk: the authoritative base with the
        # change applied (no truncation).
        assert action["content"] == "keep\nnew\ntail\n"

    async def test_delete_is_committed_without_content(self, db, fake_gitlab):
        fake_gitlab.seed_file("src/gone.py", "one\ntwo\n")
        run = await persisted(db, make_run())
        diff = (
            "diff --git a/src/gone.py b/src/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/src/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-one\n"
            "-two\n"
        )
        result = await publish(db, fake_gitlab, run, bundle_for(diff))

        assert result.ok
        (commit_call,) = fake_gitlab.calls_of("create_commit")
        (action,) = commit_call[1][2]
        assert action["action"] == "delete"
        assert "content" not in action

    async def test_fence_check_true_passes(self, db, fake_gitlab):
        run = await persisted(db, make_run())

        async def fence_ok() -> bool:
            return True

        result = await publish(
            db, fake_gitlab, run, bundle_for(_create_diff("f.md", "x\n")), fence_check=fence_ok
        )
        assert result.ok


class TestRejections:
    async def _assert_rejected(self, db, fake_gitlab, run, bundle, reason_fragment, **kwargs):
        result = await publish(db, fake_gitlab, run, bundle, **kwargs)
        assert not result.ok
        assert reason_fragment in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_diff_base_mismatch(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        await self._assert_rejected(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff("x.md", "hi\n"), attempt_base="another-base"),
            "candidate_base_mismatch",
        )

    async def test_denied_ci_path(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        await self._assert_rejected(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff(".gitlab-ci.yml", "rogue: true\n")),
            "changeset_invalid",
        )

    async def test_lockfile_path(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        await self._assert_rejected(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff("package-lock.json", "{}\n")),
            "changeset_invalid",
        )

    async def test_cancelled_run(self, db, fake_gitlab):
        run = await persisted(db, make_run(cancel_requested=True))
        await self._assert_rejected(
            db, fake_gitlab, run, bundle_for(_create_diff("x.md", "hi\n")), "publication_revoked"
        )

    async def test_cancelled_status(self, db, fake_gitlab):
        run = await persisted(db, make_run(status="cancelled"))
        await self._assert_rejected(
            db, fake_gitlab, run, bundle_for(_create_diff("x.md", "hi\n")), "publication_revoked"
        )

    async def test_empty_candidate(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        await self._assert_rejected(db, fake_gitlab, run, bundle_for(""), "empty_candidate")

    async def test_fence_mismatch(self, db, fake_gitlab):
        run = await persisted(db, make_run())

        async def fence_dead() -> bool:
            return False

        await self._assert_rejected(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff("x.md", "hi\n")),
            "fence_invalid",
            fence_check=fence_dead,
        )

    async def test_spec_digest_mismatch(self, db, fake_gitlab):
        run = await persisted(db, make_run(spec_digest="digest-of-approved-spec"))
        async with db() as session:
            session.add(
                RunSpec(
                    run_id=run.id,
                    document={"tampered": True},
                    digest="digest-of-something-else",
                )
            )
            await session.commit()
        await self._assert_rejected(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff("x.md", "hi\n")),
            "spec_digest_mismatch",
        )

    async def test_spec_digest_match_passes(self, db, fake_gitlab):
        run = await persisted(db, make_run(spec_digest="the-digest"))
        async with db() as session:
            session.add(
                RunSpec(run_id=run.id, document={"plan": "v1"}, digest="the-digest")
            )
            await session.commit()
        result = await publish(db, fake_gitlab, run, bundle_for(_create_diff("x.md", "hi\n")))
        assert result.ok

    async def test_patch_does_not_apply(self, db, fake_gitlab):
        fake_gitlab.seed_file("src/mod.py", "keep\nreality\ntail")
        run = await persisted(db, make_run())
        diff = (
            "diff --git a/src/mod.py b/src/mod.py\n"
            "--- a/src/mod.py\n"
            "+++ b/src/mod.py\n"
            "@@ -1,2 +1,2 @@\n"
            " keep\n"
            "-fantasy\n"
            "+changed\n"
        )
        await self._assert_rejected(
            db, fake_gitlab, run, bundle_for(diff), "patch_does_not_apply"
        )

    async def test_delete_of_missing_file(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        diff = (
            "diff --git a/src/gone.py b/src/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/src/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n"
            "-gone\n"
        )
        await self._assert_rejected(
            db, fake_gitlab, run, bundle_for(diff), "changeset_invalid"
        )

    async def test_oversized_base_content(self, db, fake_gitlab, monkeypatch):
        monkeypatch.setattr("forge.runs.publisher.FORGE_MATERIALIZE_MAX_FILE_CHARS", 10)
        fake_gitlab.seed_file("src/mod.py", "x" * 64)
        run = await persisted(db, make_run())
        diff = (
            "diff --git a/src/mod.py b/src/mod.py\n"
            "--- a/src/mod.py\n"
            "+++ b/src/mod.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-xxxxxxxx\n"
            "+yyyyyyyy\n"
        )
        await self._assert_rejected(db, fake_gitlab, run, bundle_for(diff), "file_too_large")

    async def test_unknown_commit_outcome_fails_without_retry(self, db, fake_gitlab):
        # Timeout where the commit did NOT land: reconciliation finds no
        # matching commit → the outcome stays unknown.
        fake_gitlab.create_commit_timeout_drops = True
        run = await persisted(db, make_run())
        result = await publish(
            db, fake_gitlab, run, bundle_for(_create_diff("x.md", "hi\n"))
        )

        assert not result.ok
        assert result.reason == "commit_unknown_outcome"
        assert result.unknown_outcome  # the commit MAY exist — caller blocks
        assert len(fake_gitlab.calls_of("create_commit")) == 1  # never retried


def _create_diff(path: str, content: str) -> str:
    """A `git diff` fragment creating one file (mirrors the artifact shape)."""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )
