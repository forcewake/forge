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

from forge.durable import ActionLog, Controller, FlowRun, RunSpec, bind_claim
from forge.durable.identity import factory_branch
from forge.gitlab.blob_reads import BlobReadResult
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.runs.candidate import _blob_digests, CandidateError, parse_unified_diff
from forge.runs.publisher import (
    PolicyViolation,
    PublishResult,
    ValidatedCandidate,
    publish_candidate,
    publish_validated_candidate,
    validate_candidate_bundle,
)
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
            session.add(RunSpec(run_id=run.id, document={"plan": "v1"}, digest="the-digest"))
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
        await self._assert_rejected(db, fake_gitlab, run, bundle_for(diff), "patch_does_not_apply")

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
        await self._assert_rejected(db, fake_gitlab, run, bundle_for(diff), "changeset_invalid")

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
        result = await publish(db, fake_gitlab, run, bundle_for(_create_diff("x.md", "hi\n")))

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


def _modify_diff(path: str, old: str, new: str) -> str:
    """A `git diff` fragment replacing one line of an existing file."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


class TestPathScope:
    """RunSpec-driven path scoping at the publisher (v0.7 monorepo)."""

    async def _persist_scoped_spec(self, db, run: FlowRun, globs: list[str]) -> None:
        async with db() as session:
            session.add(
                RunSpec(
                    run_id=run.id,
                    document={"allowed_paths": globs},
                    digest="digest-with-scope",
                )
            )
            await session.commit()

    async def test_out_of_scope_candidate_is_rejected(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        await self._persist_scoped_spec(db, run, ["services/**"])

        result = await publish(db, fake_gitlab, run, bundle_for(_create_diff("web/x.ts", "hi\n")))

        assert not result.ok
        assert "outside the allowed scope" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_in_scope_candidate_is_published(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        await self._persist_scoped_spec(db, run, ["services/**"])

        result = await publish(
            db, fake_gitlab, run, bundle_for(_create_diff("services/api/x.py", "hi\n"))
        )

        assert result.ok

    async def test_run_without_a_spec_keeps_whole_repo_scope(self, db, fake_gitlab):
        # Legacy/unscoped: no spec row, no allowed_paths — unchanged behavior.
        run = await persisted(db, make_run())
        result = await publish(db, fake_gitlab, run, bundle_for(_create_diff("any/where.md", "x")))
        assert result.ok


class TestWriteProfiles:
    """R18 at the boundary: profile threading, the operator-approval gate,
    provider-sensitive pipeline paths and duplicate-path rejection.

    The default profile is byte-compatible with the hardcoded behavior
    (``TestRejections`` above IS its matrix); these tests cover the knobs
    a caller can now pass through the boundary.
    """

    @staticmethod
    async def _noop_fetch(ref: str, paths: list[str]) -> dict[str, str]:
        return {}

    @staticmethod
    async def _recording_publish(commit_shas: list[str]):
        async def native(candidate: ValidatedCandidate) -> PublishResult:
            commit_shas.append(candidate.changeset.branch)
            return PublishResult(True, commit_sha="d" * 40)

        return native

    async def test_dependency_update_publishes_a_lockfile(self, db, fake_gitlab):
        """The R18 point: lockfile work is legitimate — under
        ``dependency_update`` it publishes; the default denies it."""
        run = await persisted(db, make_run())
        bundle = bundle_for(_create_diff("package-lock.json", "{}\n"))

        denied = await publish(db, fake_gitlab, run, bundle)
        assert not denied.ok
        assert "lockfiles are denylisted" in denied.reason

        writer = ChangesetWriter(fake_gitlab, db, PROJECT_ID)
        allowed = await publish_candidate(
            gitlab=fake_gitlab,
            session_factory=db,
            writer=writer,
            run=run,
            bundle=bundle,
            write_profile="dependency_update",
        )
        assert allowed.ok
        (commit_call,) = fake_gitlab.calls_of("create_commit")
        (action,) = commit_call[1][2]
        assert (action["action"], action["file_path"]) == ("create", "package-lock.json")

    async def test_dependency_update_still_denies_ci_config(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        result = await publish(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff(".gitlab-ci.yml", "rogue: true\n")),
            write_profile="dependency_update",
        )
        assert not result.ok
        assert "denylisted" in result.reason

    async def test_spelling_variant_lockfile_is_still_denied(self, fake_gitlab):
        with pytest.raises(PolicyViolation) as exc:
            validate_candidate_bundle(
                bundle_for(_create_diff("web\\package-lock.json", "{}\n")),
                base_contents={},
                branch="forge/7/abcd1234",
                commit_message="m",
            )
        assert any("lockfiles are denylisted" in v for v in exc.value.violations)

    async def test_ci_change_without_approval_is_refused(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        result = await publish(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff(".gitlab-ci.yml", "stages: [x]\n")),
            write_profile="ci_change",
        )
        assert not result.ok
        assert "special_approval_required" in result.reason
        assert "ci_change" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_ci_change_with_explicit_approval_publishes(self, fake_gitlab):
        reached = []

        async def native(candidate: ValidatedCandidate) -> PublishResult:
            reached.append(candidate)
            return PublishResult(True, commit_sha="d" * 40)

        result = await publish_validated_candidate(
            make_run(),
            bundle_for(_create_diff(".gitlab-ci.yml", "stages: [x]\n")),
            fetch_base_contents=self._noop_fetch,
            native_publish=native,
            write_profile="ci_change",
            operator_approved=True,
        )
        assert result.ok
        (candidate,) = reached
        assert [c.path for c in candidate.changeset.changes] == [".gitlab-ci.yml"]

    async def test_ci_change_session_run_proves_approval_with_its_frozen_spec(
        self, db, fake_gitlab
    ):
        """An operator /go froze the RunSpec — that row IS the approval; a
        session-backed ci_change run without any spec is refused."""
        approved_run = await persisted(db, make_run(run_id="a" * 32, spec_digest="approved"))
        async with db() as session:
            session.add(RunSpec(run_id=approved_run.id, document={}, digest="approved"))
            await session.commit()
        ok = await publish(
            db,
            fake_gitlab,
            approved_run,
            bundle_for(_create_diff(".gitlab-ci.yml", "stages: [x]\n")),
            write_profile="ci_change",
        )
        assert ok.ok

        unapproved = await persisted(db, make_run(run_id="b" * 32, issue_iid=8))
        refused = await publish(
            db,
            fake_gitlab,
            unapproved,
            bundle_for(_create_diff(".gitlab-ci.yml", "stages: [x]\n")),
            write_profile="ci_change",
        )
        assert not refused.ok
        assert "special_approval_required" in refused.reason

    async def test_forge_config_stays_denied_under_ci_change(self, fake_gitlab):
        result = await publish_validated_candidate(
            make_run(),
            bundle_for(_create_diff(".forge.yml", "rogue: true\n")),
            fetch_base_contents=self._noop_fetch,
            native_publish=await self._recording_publish([]),
            write_profile="ci_change",
            operator_approved=True,
        )
        assert not result.ok
        assert "denylisted" in result.reason

    async def test_pipeline_entrypoint_is_denied_under_every_profile(self, db, fake_gitlab):
        """The Azure hook: the onboarding-provided pipeline entrypoint is a
        sensitive path — even ci_change cannot publish it."""
        run = await persisted(db, make_run())
        bundle = bundle_for(_create_diff("ci/build.yml", "steps: []\n"))

        for profile in (None, "ci_change", "dependency_update"):
            result = await publish(
                db,
                fake_gitlab,
                run,
                bundle,
                write_profile=profile,
                sensitive_paths=["ci/build.yml"],
                operator_approved=True,
            )
            assert not result.ok, profile
            assert "protected pipeline entrypoint" in result.reason
            assert fake_gitlab.calls_of("create_commit") == []

    async def test_unknown_profile_fails_the_publication_closed(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        result = await publish(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff("x.md", "hi\n")),
            write_profile="typo_profile",
        )
        assert not result.ok
        assert "write_policy_config_error" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_duplicate_bundle_paths_are_rejected(self, fake_gitlab):
        diff = _create_diff("a.md", "one\n") + _create_diff("./a.md", "two\n")
        with pytest.raises(CandidateError) as exc:
            validate_candidate_bundle(
                bundle_for(diff),
                base_contents={},
                branch="forge/7/abcd1234",
                commit_message="m",
            )
        assert exc.value.reason == "duplicate_path"

    async def test_duplicate_paths_reject_the_publication(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        diff = _create_diff("a.md", "one\n") + _create_diff("./a.md", "two\n")
        result = await publish(db, fake_gitlab, run, bundle_for(diff))
        assert not result.ok
        assert "duplicate_path" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_custom_profile_threads_through_the_boundary(self, db, fake_gitlab):
        """A forge.yml-defined profile reaches the boundary as parsed
        config (the service passes ``config.write_profiles`` through)."""
        run = await persisted(db, make_run())
        custom = {"vendor_locked": {"denied_paths": ["vendor/**"], "allowed_paths": []}}
        denied = await publish(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff("vendor/helper.py", "x\n")),
            write_profile="vendor_locked",
            custom_write_profiles=custom,
        )
        assert not denied.ok
        assert "denylisted pattern" in denied.reason

        allowed = await publish(
            db,
            fake_gitlab,
            run,
            bundle_for(_create_diff("src/app.py", "x = 1\n")),
            write_profile="vendor_locked",
            custom_write_profiles=custom,
        )
        assert allowed.ok


class TestBoundaryEntry:
    """publish_validated_candidate / validate_candidate_bundle (ADR-0026).

    The application-service entry owns run-state → materialization → policy
    → the injected native adapter; the adapter receives a ValidatedCandidate
    or nothing at all.
    """

    @staticmethod
    async def _noop_fetch(ref: str, paths: list[str]) -> dict[str, str]:
        return {}

    async def test_pure_check_returns_the_validated_wrapper(self, fake_gitlab):
        candidate = validate_candidate_bundle(
            bundle_for(_create_diff("forge-demo/x.md", "hello\n")),
            base_contents={},
            branch="forge/7/abcd1234",
            commit_message="forge: implement 7",
        )
        assert isinstance(candidate, ValidatedCandidate)
        assert [c.path for c in candidate.changeset.changes] == ["forge-demo/x.md"]
        assert candidate.manifest[0].new_content == "hello\n"
        assert candidate.allowed_paths == ()

    async def test_pure_check_raises_candidate_error_on_bad_patch(self, fake_gitlab):
        fake_gitlab.seed_file("src/mod.py", "reality\n")
        with pytest.raises(CandidateError) as exc:
            validate_candidate_bundle(
                bundle_for(_modify_diff("src/mod.py", "fantasy", "changed")),
                base_contents={"src/mod.py": "reality\n"},
                branch="forge/7/abcd1234",
                commit_message="m",
            )
        assert exc.value.reason == "patch_does_not_apply"

    async def test_pure_check_raises_policy_violation_on_denylist(self, fake_gitlab):
        with pytest.raises(PolicyViolation) as exc:
            validate_candidate_bundle(
                bundle_for(_create_diff(".github/workflows/pwn.yml", "x\n")),
                base_contents={},
                branch="forge/7/abcd1234",
                commit_message="m",
            )
        assert any("denylisted prefix" in v for v in exc.value.violations)

    async def test_adapter_receives_a_validated_candidate(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        seen = []

        async def native(candidate: ValidatedCandidate) -> PublishResult:
            seen.append(candidate)
            return PublishResult(True, commit_sha="d" * 40)

        result = await publish_validated_candidate(
            run,
            bundle_for(_create_diff("forge-demo/x.md", "hello\n")),
            session_factory=db,
            fetch_base_contents=self._noop_fetch,
            native_publish=native,
        )

        assert result.ok and result.commit_sha == "d" * 40
        (candidate,) = seen
        assert isinstance(candidate, ValidatedCandidate)
        assert candidate.changeset.changes[0].path == "forge-demo/x.md"

    async def test_rejected_candidate_never_reaches_the_adapter(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        reached = []

        async def native(candidate: ValidatedCandidate) -> PublishResult:
            reached.append(candidate)
            return PublishResult(True, commit_sha="d" * 40)

        result = await publish_validated_candidate(
            run,
            bundle_for(_create_diff("package-lock.json", "{}\n")),
            session_factory=db,
            fetch_base_contents=self._noop_fetch,
            native_publish=native,
        )

        assert not result.ok
        assert "changeset_invalid" in result.reason
        assert reached == []  # the violating candidate never reached a write

    async def test_publisher_routes_through_the_boundary_entry(self, db, fake_gitlab, monkeypatch):
        real = publish_validated_candidate
        calls = []

        async def spy(run, candidate, **kwargs):
            calls.append((run.id, candidate))
            return await real(run, candidate, **kwargs)

        monkeypatch.setattr("forge.runs.publisher.publish_validated_candidate", spy)
        run = await persisted(db, make_run())
        result = await publish(
            db, fake_gitlab, run, bundle_for(_create_diff("forge-demo/x.md", "hello\n"))
        )

        assert result.ok
        (seen_run_id, seen_bundle) = calls[0]
        assert seen_run_id == run.id
        assert seen_bundle.attempt_base_oid == BASE_SHA


class TestGrantGeneration:
    """R10: the publication grant is generation-fenced and re-checked at the
    reservation point — and a commit that already landed when the grant dies
    comes back ``superseded``, never as a fresh publish."""

    async def test_commit_landing_during_cancel_comes_back_superseded(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        writer = ChangesetWriter(fake_gitlab, db, PROJECT_ID)
        real_apply = writer.apply

        async def cancelling_apply(run_id, changeset, **kwargs):
            # The cancel lands WHILE the remote commit is in flight.
            async with db() as session:
                await Controller(session).request_cancel(run_id)
                await session.commit()
            return await real_apply(run_id, changeset, **kwargs)

        writer.apply = cancelling_apply  # type: ignore[method-assign]

        result = await publish_candidate(
            gitlab=fake_gitlab,
            session_factory=db,
            writer=writer,
            run=run,
            bundle=bundle_for(_create_diff("forge-demo/x.md", "hello\n")),
        )

        # Best-effort: the commit is NOT rolled back …
        assert result.ok
        assert len(fake_gitlab.calls_of("create_commit")) == 1
        # … but its completion is superseded evidence, never a fresh publish.
        assert result.superseded is True
        async with db() as session:
            final = await session.get(FlowRun, run.id)
        assert final is not None
        assert (final.evidence or {})["superseded"]["reason"] == "cancelled_during_publication"
        assert final.status != "ready_for_human"

    async def test_uncancelled_commit_is_a_plain_publish(self, db, fake_gitlab):
        run = await persisted(db, make_run())
        result = await publish(
            db, fake_gitlab, run, bundle_for(_create_diff("forge-demo/x.md", "hello\n"))
        )
        assert result.ok
        assert result.superseded is False


class TestAuthoritativeBaseReads:
    """R14 at the publication boundary: the base reads are TYPED.

    Only a provider-confirmed ``not_found`` may make a path absent — a
    ``forbidden`` / ``unavailable`` / ``incomplete`` read fails validation
    (``authoritative_read_failed``) BEFORE any remote effect, instead of
    forging existence facts (a create-over-existing flip). The R08/R09
    digest verification keeps running on every path that survives.
    """

    def stub_read_blob(self, fake_gitlab, monkeypatch, result_for):
        """Replace the fake's typed read with one answering *result_for(path)*."""

        async def _read(project_id, file_path, ref="HEAD"):
            return result_for(file_path)

        monkeypatch.setattr(fake_gitlab, "read_blob", _read)

    async def test_forbidden_base_read_rejects_without_commit(self, db, fake_gitlab, monkeypatch):
        fake_gitlab.seed_file("src/mod.py", "keep\nold\ntail\n")
        self.stub_read_blob(
            fake_gitlab,
            monkeypatch,
            lambda path: BlobReadResult.forbidden(f"gitlab api error 403: forbidden for {path!r}"),
        )
        run = await persisted(db, make_run())
        diff = _modify_diff("src/mod.py", "old", "new")

        result = await publish(db, fake_gitlab, run, bundle_for(diff))

        assert not result.ok
        assert "authoritative_read_failed" in result.reason
        assert "forbidden" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_unavailable_base_read_rejects_without_commit(self, db, fake_gitlab, monkeypatch):
        fake_gitlab.seed_file("src/mod.py", "keep\nold\ntail\n")
        self.stub_read_blob(
            fake_gitlab,
            monkeypatch,
            lambda path: BlobReadResult.unavailable("gitlab transport error: timed out"),
        )
        run = await persisted(db, make_run())

        result = await publish(
            db, fake_gitlab, run, bundle_for(_modify_diff("src/mod.py", "old", "new"))
        )

        assert not result.ok
        assert "authoritative_read_failed" in result.reason
        assert "unavailable" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_incomplete_base_read_rejects_without_commit(self, db, fake_gitlab, monkeypatch):
        self.stub_read_blob(
            fake_gitlab,
            monkeypatch,
            lambda path: BlobReadResult.incomplete(f"{path!r}: content is not valid UTF-8"),
        )
        run = await persisted(db, make_run())

        result = await publish(
            db, fake_gitlab, run, bundle_for(_modify_diff("src/mod.py", "old", "new"))
        )

        assert not result.ok
        assert "authoritative_read_failed" in result.reason
        assert "incomplete" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_read_failure_never_reports_a_missing_file_for_delete(
        self, db, fake_gitlab, monkeypatch
    ):
        # A delete whose base read fails must be blocked as a read failure —
        # NOT waved through as "file does not exist" (or the reverse: silently
        # deleted against unreadable evidence).
        self.stub_read_blob(
            fake_gitlab,
            monkeypatch,
            lambda path: BlobReadResult.unavailable("gitlab api error 500: exploded"),
        )
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

        assert not result.ok
        assert "authoritative_read_failed" in result.reason
        assert "file does not exist" not in result.reason
        assert fake_gitlab.calls_of("create_commit") == []


class TestClaimOwnedDispatch:
    """A04: the publisher arbitrates the ambient ExecutionClaim's step
    ownership at the reservation point — a LIVE owner dispatches normally."""

    async def test_bound_live_claim_still_publishes(self, db, fake_gitlab):
        """The rightful owner is never refused: a claim whose step row still
        shows its owner, fence token and live lease publishes exactly as an
        unbound transport call does."""
        from uuid import uuid4

        from forge.durable import StepRun
        from forge.worker.steps import claim_due_steps, execution_claim, schedule_command_step

        run_id = "1" * 32
        run = await persisted(db, make_run(run_id=run_id))
        async with db() as session, session.begin():
            step = await schedule_command_step(
                session,
                {"command": "advance", "project_id": PROJECT_ID},
                source_event_id=uuid4().hex,
            )
            step.flow_run_id = run_id
            step_id = step.id
        claimed = await claim_due_steps(db, "worker-a")
        assert [s.id for s in claimed] == [step_id]
        claim = execution_claim(claimed[0])
        async with db() as session:
            live = await session.get(StepRun, step_id)
        assert live is not None and live.lease_owner == "worker-a"

        with bind_claim(claim):
            result = await publish(
                db, fake_gitlab, run, bundle_for(_create_diff("forge-demo/x.md", "hello\n"))
            )
        assert result.ok
        assert result.superseded is False
        branch = factory_branch(ISSUE_IID, run_id)
        assert fake_gitlab.branches[branch], "the commit landed for the rightful owner"

    async def test_confirmed_not_found_still_allows_the_create(self, db, fake_gitlab):
        # The honest positive control: a provider-confirmed absence is exactly
        # what a create needs — nothing about it changed.
        run = await persisted(db, make_run())
        result = await publish(
            db, fake_gitlab, run, bundle_for(_create_diff("forge-demo/x.md", "hello\n"))
        )
        assert result.ok

    async def test_create_over_confirmed_existing_is_rejected(self, db, fake_gitlab):
        # The strict read FOUND the file — a create would overwrite it.
        fake_gitlab.seed_file("src/exists.py", "already here\n")
        run = await persisted(db, make_run())

        result = await publish(
            db, fake_gitlab, run, bundle_for(_create_diff("src/exists.py", "rogue\n"))
        )

        assert not result.ok
        assert "already exists in the base snapshot" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_stale_base_digest_still_rejects_the_modify(self, db, fake_gitlab):
        # R08 intact: a candidate diffed against a different base blob is
        # rejected (stale_base), never fuzzy-applied.
        fake_gitlab.seed_file("src/mod.py", "keep\nold\ntail\n")
        run = await persisted(db, make_run())
        diff = (
            "diff --git a/src/mod.py b/src/mod.py\n"
            f"index {'f' * 40}..{'e' * 40} 100644\n"
            "--- a/src/mod.py\n"
            "+++ b/src/mod.py\n"
            "@@ -2,1 +2,1 @@\n"
            "-old\n"
            "+new\n"
        )

        result = await publish(db, fake_gitlab, run, bundle_for(diff))

        assert not result.ok
        assert "stale_base" in result.reason
        assert fake_gitlab.calls_of("create_commit") == []

    async def test_intended_digest_verifies_the_materialized_result(self, db, fake_gitlab):
        # R09 intact end-to-end: the diff's own new blob OID must equal the
        # materialized result — a wrong claim is an internal error, never a
        # publish; the correct claim publishes exactly the expected text.
        base = "keep\nold\ntail\n"
        expected = "keep\nnew\ntail\n"
        fake_gitlab.seed_file("src/mod.py", base)
        run = await persisted(db, make_run())
        # _blob_digests returns TAGGED claims ("blob:<hex>") — the diff's
        # index line carries the raw hex.
        (old_oid, _) = _blob_digests(base)
        (new_oid, _) = _blob_digests(expected)
        old_hex, new_hex = old_oid.removeprefix("blob:"), new_oid.removeprefix("blob:")
        diff = (
            "diff --git a/src/mod.py b/src/mod.py\n"
            f"index {old_hex}..{'d' * 40} 100644\n"
            "--- a/src/mod.py\n"
            "+++ b/src/mod.py\n"
            "@@ -2,1 +2,1 @@\n"
            "-old\n"
            "+new\n"
        )

        bad = await publish(db, fake_gitlab, run, bundle_for(diff))
        assert not bad.ok
        assert "result_digest_mismatch" in bad.reason
        assert fake_gitlab.calls_of("create_commit") == []

        diff_ok = diff.replace(f"..{'d' * 40}", f"..{new_hex}")
        good = await publish(db, fake_gitlab, run, bundle_for(diff_ok))
        assert good.ok
        (commit_call,) = fake_gitlab.calls_of("create_commit")
        (action,) = commit_call[1][2]
        assert action["content"] == expected
