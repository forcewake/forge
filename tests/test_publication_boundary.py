"""The single publication boundary refuses invalid work before any native write."""

from dataclasses import replace
import hashlib

import pytest

from forge.domain.candidate import (
    ValidatedCandidate,
)
from forge.domain.change_types import Change, ChangeSet, Operation
from forge.domain.paths import resolve_write_policy
from forge.domain.states import State
from forge.port_values import ReadUnavailable
from tests.test_engine_delivery import delivery as delivery, completed, full_patch
from tests.test_engine_lane_handlers import case as case, env as env, settings as settings, started


def oid(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def creation(path, raw=b"x\n"):
    lines = raw.splitlines(keepends=True)
    content = b"".join(b"+" + line for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        f"index {'0' * 40}..{oid(raw)}\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
    ).encode() + content


def frozen_policy(env, policy):
    env.intent = env.intent.model_copy(
        update={"policy": env.intent.policy.model_copy(update={"write_policy": policy})}
    )


SCENARIOS = (
    ("ci", creation(".gitlab-ci.yml")),
    ("workflow", creation(".github/workflows/pwn.yml")),
    ("lockfile", creation("package-lock.json")),
    ("duplicate", creation("dup.py") + creation("dup.py")),
    ("absolute", creation("/etc/evil.py")),
    ("traversal", creation("../escape.py")),
    ("scope", creation("outside.py")),
    ("too_many", b"".join(creation(f"file{n}.py") for n in range(21))),
    ("oversized", creation("large.py", b"x" * (256 * 1024 + 1) + b"\n")),
)


class TestZeroWritesOnRejection:
    @pytest.mark.parametrize("name,diff", SCENARIOS, ids=[name for name, _ in SCENARIOS])
    async def test_publisher_refuses_and_never_commits(self, delivery, name, diff):
        env = delivery["env"]
        if name == "scope":
            frozen_policy(
                env, replace(env.intent.policy.write_policy, allowed_globs=("services/**",))
            )
        run, _, pending = await started(delivery)
        before = await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id)
        await completed(delivery, pending, diff=diff)
        tick = await env.runner.tick()
        current = await env.runs.load(run.id)
        assert tick.parked == 1 and current.state == State.BLOCKED, current.reason
        assert current.reason.startswith("candidate_")
        assert not any(name in {"commit", "ensure_draft"} for name, _ in env.fake.calls)
        assert not env.fake.drafts
        assert await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id) == before


class TestValidatedWrapperGuard:
    async def test_write_leg_refuses_a_raw_changeset_before_journaling(self, delivery):
        env = delivery["env"]
        run, _, pending = await started(delivery)
        await completed(delivery, pending)
        (lease,) = await env.steps.claim_due("boundary", env.clock.now(), 60)
        raw = ChangeSet(
            branch="forge/" + run.id,
            commit_message="raw",
            changes=(Change("x.py", Operation.CREATE, "x\n"),),
        )
        assert not isinstance(raw, ValidatedCandidate)
        with pytest.raises(TypeError, match="ValidatedCandidate"):
            await env.effects.commit(
                lease,
                "raw-commit",
                env.intent.task.subject.repo,
                raw,
                pending.dispatch.base_sha,
                "raw",
            )
        from forge.providers.git_commit import GitTarget, publish

        with pytest.raises(TypeError, match="ValidatedCandidate"):
            await publish(
                GitTarget("https://example.invalid/repository.git", "synthetic"),
                raw,
                pending.dispatch.base_sha,
                "raw",
                stamp=0,
            )
        assert await env.effects.store.get("raw-commit") is None
        assert not any(name == "commit" for name, _ in env.fake.calls)


class TestPositiveControl:
    async def test_publishes_exactly_the_validated_manifest(self, delivery):
        env = delivery["env"]
        run, _, pending = await started(delivery)
        diff = full_patch(b"old\n", b"new\n") + creation("feature.py", b"VALUE = 1\n")
        await completed(delivery, pending, diff=diff)
        assert (await env.runner.tick()).completed == 1
        assert (await env.runs.load(run.id)).state == State.VERIFYING
        commits = [args for name, args in env.fake.calls if name == "commit"]
        assert len(commits) == 1 and isinstance(commits[0][1], ValidatedCandidate)
        sha = await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id)
        native = env.fake.repo(env.intent.task.subject.repo).commits[sha]
        files = dict(native.files)
        assert set(files) == {"a.py", "feature.py"}
        assert files["a.py"].content == b"new\n"
        assert files["feature.py"].content == b"VALUE = 1\n"
        assert len(env.fake.drafts) == 1


class TestAuthoritativeEvidence:
    @pytest.mark.parametrize("reason", ("forbidden", "unavailable", "incomplete"))
    async def test_unreadable_base_never_becomes_absence(self, delivery, monkeypatch, reason):
        env = delivery["env"]
        run, _, pending = await started(delivery)
        await completed(delivery, pending)

        async def unreadable(*args):
            raise ReadUnavailable(reason, "sanitized native read failure")

        monkeypatch.setattr(env.fake, "read_blob", unreadable)
        tick = await env.runner.tick()
        current = await env.runs.load(run.id)
        assert tick.rescheduled == 1 and current.state == State.PUBLISHING
        assert not any(name == "commit" for name, _ in env.fake.calls)
        assert not env.fake.drafts

    async def test_confirmed_absence_is_the_only_honest_missing(self, delivery):
        env = delivery["env"]
        run, _, pending = await started(delivery)
        await completed(
            delivery, pending, diff=full_patch(b"old\n", b"new\n").replace(b"a.py", b"missing.py")
        )
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).state == State.BLOCKED
        assert not any(name == "commit" for name, _ in env.fake.calls)

    async def test_create_over_confirmed_existing_is_refused(self, delivery):
        env = delivery["env"]
        run, _, pending = await started(delivery)
        await completed(delivery, pending, diff=creation("a.py"))
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason == "candidate_write_policy_refused"
        assert not any(name == "commit" for name, _ in env.fake.calls)


class TestWriteProfileBoundary:
    async def test_dependency_update_publishes_the_lockfile(self, delivery):
        env = delivery["env"]
        frozen_policy(env, resolve_write_policy("dependency_update"))
        run, _, pending = await started(delivery)
        await completed(delivery, pending, diff=creation("package-lock.json", b"{}\n"))
        assert (await env.runner.tick()).completed == 1
        assert (await env.runs.load(run.id)).state == State.VERIFYING
        assert sum(name == "commit" for name, _ in env.fake.calls) == 1

    @pytest.mark.parametrize(
        "profile", ("no_dependencies", "code_only", "dependency_update", "ci_change")
    )
    async def test_pipeline_entrypoint_is_refused_under_every_profile(self, delivery, profile):
        env = delivery["env"]
        frozen_policy(
            env, resolve_write_policy(profile, extra_denied_paths=("pipelines/build.yml",))
        )
        run, _, pending = await started(delivery)
        await completed(delivery, pending, diff=creation("pipelines/build.yml"))
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason == "candidate_write_policy_refused"
        assert not any(name == "commit" for name, _ in env.fake.calls)

    async def test_later_ambient_policy_cannot_expand_the_approved_profile(self, delivery):
        env = delivery["env"]
        run, _, pending = await started(delivery)
        frozen_policy(env, resolve_write_policy("dependency_update"))
        await completed(delivery, pending, diff=creation("package-lock.json"))
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason == "candidate_write_policy_refused"
        assert not any(name == "commit" for name, _ in env.fake.calls)
