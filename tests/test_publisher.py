"""Retained publisher lessons against the sole engine and immutable native snapshots."""

from dataclasses import replace

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from forge.api.lane_auth_values import LaneAuthError
from forge.api.lane_v1_values import LaneConflict
from forge.domain.candidate import (
    CandidateBundle,
    CreateFile,
    ValidatedCandidate,
    PolicyViolation,
    validate_candidate_bundle,
)
from forge.domain.patch import CandidateError
from forge.domain.paths import resolve_write_policy
from forge.domain.states import Handler, State
from forge.engine.runner import LeaseHolder, StepContext
from forge.port_values import ReadUnavailable
from forge.providers.fake import Blob, Fault
from forge.providers.fake.state import commit_value
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineApprovedInput, EngineRunSpec
from forge.store.records import StaleLease
from tests.test_engine_commands import commands as commands, envelope
from tests.test_engine_delivery import delivery as delivery, completed, full_patch
from tests.test_engine_lane_handlers import case as case, env as env, settings as settings, started
from tests.test_engine_operator import operator as operator
from tests.test_publication_boundary import creation, frozen_policy, oid


def deletion(path="a.py", raw=b"old\n"):
    return (
        (
            f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n"
            f"index {oid(raw)}..{'0' * 40}\n--- a/{path}\n+++ /dev/null\n@@ -1 +0,0 @@\n"
        ).encode()
        + b"-"
        + raw
    )


async def awaiting_publish(delivery, diff=None):
    run, frozen, pending = await started(delivery)
    await completed(delivery, pending, diff=diff)
    return run, frozen, pending


def native_writes(env):
    return [args for name, args in env.fake.calls if name == "commit"]


class TestAccepts:
    @pytest.mark.parametrize("operation", ("create", "modify", "delete"))
    async def test_exact_native_content_and_expected_parent(self, delivery, operation):
        diff = {
            "create": creation("feature.py", b"feature\n"),
            "modify": full_patch(b"old\n", b"new\n"),
            "delete": deletion(),
        }[operation]
        run, _, pending = await awaiting_publish(delivery, diff)
        env = delivery["env"]
        assert (await env.runner.tick()).completed == 1
        assert (await env.runs.load(run.id)).state == State.VERIFYING
        (args,) = native_writes(env)
        assert args[2] == pending.dispatch.base_sha
        candidate = args[1]
        assert isinstance(candidate, ValidatedCandidate)
        sha = await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id)
        native = env.fake.repo(env.intent.task.subject.repo).commits[sha]
        assert native.parent == pending.dispatch.base_sha
        files = dict(native.files)
        expected = (
            {"a.py": b"old\n", "feature.py": b"feature\n"}
            if operation == "create"
            else {"a.py": b"new\n"}
            if operation == "modify"
            else {}
        )
        assert {path: blob.content for path, blob in files.items()} == expected
        assert len(env.fake.drafts) == 1


class TestRejections:
    async def test_diff_base_mismatch_never_enters_publication(self, delivery):
        env = delivery["env"]
        _, _, pending = await started(delivery)
        with pytest.raises(LaneConflict):
            await completed(delivery, pending, claimed_base="f" * 40)
        assert not native_writes(env) and not env.fake.drafts

    async def test_empty_candidate_never_enters_publication(self, delivery):
        env = delivery["env"]
        _, _, pending = await started(delivery)
        with pytest.raises((ValidationError, LaneAuthError)):
            await completed(delivery, pending, diff=b"")
        assert not native_writes(env) and not env.fake.drafts

    @pytest.mark.parametrize("tamper", ("spec_digest", "consumed_approval"))
    async def test_publication_requires_exact_current_consumed_authority(self, delivery, tamper):
        run, _, _ = await awaiting_publish(delivery)
        env = delivery["env"]
        async with env.factory() as session, session.begin():
            if tamper == "spec_digest":
                row = await session.scalar(
                    select(EngineRunSpec).where(EngineRunSpec.run_id == run.id)
                )
                row.digest = "0" * 64
            else:
                await session.execute(
                    delete(EngineApprovedInput).where(EngineApprovedInput.run_id == run.id)
                )
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).state == State.BLOCKED
        assert not native_writes(env) and not env.fake.drafts

    @pytest.mark.parametrize(
        "tamper", ("old_oid", "new_oid", "hunk", "missing_index", "delete_missing")
    )
    async def test_invalid_patch_never_reaches_native_commit(self, delivery, tamper):
        diff = full_patch(b"old\n", b"new\n")
        if tamper == "old_oid":
            diff = diff.replace(oid(b"old\n").encode(), b"f" * 40)
        elif tamper == "new_oid":
            diff = diff.replace(oid(b"new\n").encode(), b"d" * 40)
        elif tamper == "hunk":
            diff = diff.replace(b"-old\n", b"-wrong\n")
        elif tamper == "missing_index":
            diff = b"\n".join(line for line in diff.split(b"\n") if not line.startswith(b"index "))
        else:
            diff = deletion("absent.py")
        run, _, _ = await awaiting_publish(delivery, diff)
        env = delivery["env"]
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason.startswith("candidate_")
        assert not native_writes(env) and not env.fake.drafts

    async def test_oversized_authoritative_base_refuses_without_commit(self, delivery):
        env = delivery["env"]
        repo = env.intent.task.subject.repo
        raw = b"x" * 400_000 + b"\n"
        native = commit_value({"a.py": Blob(raw)}, None, "Large authoritative base")
        remote = env.fake.repo(repo)
        remote.commits[native.sha] = native
        remote.branches["main"] = native.sha
        primary = env.intent.snapshot.primary.model_copy(update={"sha": native.sha})
        env.intent = env.intent.model_copy(
            update={"snapshot": env.intent.snapshot.model_copy(update={"primary": primary})}
        )
        run, _, _ = await awaiting_publish(delivery, full_patch(raw, b"new\n"))
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason.startswith("candidate_")
        assert not native_writes(env)

    async def test_unknown_commit_outcome_is_retained_without_blind_retry(self, delivery):
        run, _, _ = await awaiting_publish(delivery)
        env = delivery["env"]
        env.fake.fail_next("commit", Fault(unknown_after_success=True))
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason == "effect_unknown:commit"
        assert len(native_writes(env)) == 1 and not env.fake.drafts
        assert (await env.runner.tick()).claimed == 0
        assert len(native_writes(env)) == 1
        effect = await env.effects.store.get(f"{run.id}:1:1:publish:commit")
        assert effect.state == "unknown"
        recovered = await env.effects.recover(effect)
        assert recovered.state == "adopted"
        assert len(native_writes(env)) == 1


class TestPathScope:
    @pytest.mark.parametrize("path,allowed", (("services/new.py", True), ("other/new.py", False)))
    async def test_frozen_scope_is_enforced_at_publication(self, delivery, path, allowed):
        env = delivery["env"]
        frozen_policy(env, replace(env.intent.policy.write_policy, allowed_globs=("services/**",)))
        run, _, _ = await awaiting_publish(delivery, creation(path))
        tick = await env.runner.tick()
        assert bool(tick.completed) == allowed
        assert (await env.runs.load(run.id)).state == (
            State.VERIFYING if allowed else State.BLOCKED
        )
        assert len(native_writes(env)) == int(allowed)


class TestWriteProfiles:
    @pytest.mark.parametrize(
        "path,allowed", (("vendor/helper.py", False), ("src/feature.py", True))
    )
    async def test_custom_profile_is_frozen_and_enforced(self, delivery, path, allowed):
        env = delivery["env"]
        policy = resolve_write_policy(
            "vendor_locked",
            custom_profiles={"vendor_locked": {"denied_paths": ["vendor/**"], "allowed_paths": []}},
        )
        frozen_policy(env, policy)
        run, _, _ = await awaiting_publish(delivery, creation(path))
        tick = await env.runner.tick()
        assert bool(tick.completed) == allowed
        assert len(native_writes(env)) == int(allowed)
        assert (await env.runs.load(run.id)).state == (
            State.VERIFYING if allowed else State.BLOCKED
        )

    async def test_ci_change_requires_the_frozen_consumed_approval(self, delivery):
        env = delivery["env"]
        frozen_policy(env, resolve_write_policy("ci_change"))
        run, frozen, _ = await awaiting_publish(delivery, creation(".github/workflows/feature.yml"))
        assert frozen.approved.spec_digest == frozen.spec.digest
        assert frozen.spec.write_policy.require_special_approval
        assert (await env.runner.tick()).completed == 1
        assert (await env.runs.load(run.id)).state == State.VERIFYING
        assert len(native_writes(env)) == 1

    async def test_unknown_profile_refuses_before_admission(self, delivery):
        with pytest.raises(ValueError, match="unknown write profile"):
            frozen_policy(delivery["env"], resolve_write_policy("unrecognized"))
        assert not delivery["env"].fake.calls


class TestBoundaryEntry:
    def test_pure_check_issues_only_a_validated_wrapper(self):
        candidate = validate_candidate_bundle(
            CandidateBundle("a" * 40, "success", (CreateFile("x.py", "x\n"),)),
            base_contents={},
            branch="forge/run",
            commit_message="Exact",
        )
        assert isinstance(candidate, ValidatedCandidate)
        assert candidate.manifest[0].content_bytes == b"x\n"
        assert candidate.changeset.changes[0].path == "x.py"

    def test_pure_check_preserves_typed_policy_refusal(self):
        with pytest.raises(PolicyViolation):
            validate_candidate_bundle(
                CandidateBundle("a" * 40, "success", (CreateFile(".gitlab-ci.yml", "x\n"),)),
                base_contents={},
                branch="forge/run",
                commit_message="Exact",
            )
        with pytest.raises(CandidateError, match="no changes"):
            validate_candidate_bundle(
                CandidateBundle("a" * 40, "success", ()),
                base_contents={},
                branch="forge/run",
                commit_message="Exact",
            )

    async def test_publication_routes_through_the_boundary_entry(self, delivery, monkeypatch):
        import forge.engine.publish as publisher

        original = publisher.validate_candidate_bundle
        observed = []

        def validate(*args, **kwargs):
            candidate = original(*args, **kwargs)
            observed.append(candidate)
            return candidate

        monkeypatch.setattr(publisher, "validate_candidate_bundle", validate)
        await awaiting_publish(delivery)
        assert (await delivery["env"].runner.tick()).completed == 1
        assert len(observed) == 1 and native_writes(delivery["env"])[0][1] is observed[0]


class TestGrantGeneration:
    async def test_commit_landing_during_cancel_retains_fact_without_continuation(
        self, delivery, commands, monkeypatch
    ):
        run, _, _ = await awaiting_publish(delivery)
        env = delivery["env"]
        original = env.fake.commit

        async def cancel_after_commit(*args):
            outcome = await original(*args)
            await commands["commands"].submit(envelope(commands, "cancel"))
            return outcome

        monkeypatch.setattr(env.fake, "commit", cancel_after_commit)
        assert (await env.runner.tick()).lost == 1
        assert (await env.runs.load(run.id)).state == State.CANCELLED
        assert len(native_writes(env)) == 1 and not env.fake.drafts
        effect = await env.effects.store.get(f"{run.id}:1:1:publish:commit")
        assert effect.state == "committed"


class TestAuthoritativeBaseReads:
    @pytest.mark.parametrize("reason", ("forbidden", "unavailable", "incomplete"))
    @pytest.mark.parametrize("operation", ("modify", "delete"))
    async def test_read_failures_never_become_missing_or_publish(
        self, delivery, monkeypatch, reason, operation
    ):
        run, _, _ = await awaiting_publish(delivery, deletion() if operation == "delete" else None)
        env = delivery["env"]

        async def unavailable(*args):
            raise ReadUnavailable(reason, "sanitized native read failure")

        monkeypatch.setattr(env.fake, "read_blob", unavailable)
        assert (await env.runner.tick()).rescheduled == 1
        assert (await env.runs.load(run.id)).state == State.PUBLISHING
        assert not native_writes(env) and not env.fake.drafts

    async def test_native_tree_and_blob_bytes_must_agree(self, delivery, monkeypatch):
        run, _, _ = await awaiting_publish(delivery)
        env = delivery["env"]

        async def wrong(*args):
            return b"wrong native bytes\n"

        monkeypatch.setattr(env.fake, "read_blob", wrong)
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason == "candidate_stale_base"
        assert not native_writes(env)


class TestClaimOwnedDispatch:
    async def test_expired_or_replaced_claim_never_publishes(self, delivery):
        run, _, _ = await awaiting_publish(delivery)
        env = delivery["env"]
        (lease,) = await env.steps.claim_due("original", env.clock.now(), 60)
        async with env.factory() as session, session.begin():
            step = await session.get(EngineStep, lease.step_id)
            step.lease_epoch += 1
            step.lease_owner = "replacement"
        context = StepContext(LeaseHolder(lease), await env.steps.load(lease.step_id), env.effects)
        with pytest.raises(StaleLease):
            await env.runner.handlers[Handler.PUBLISH](context)
        assert not native_writes(env) and (await env.runs.load(run.id)).state == State.PUBLISHING

    async def test_recovered_publication_reuses_original_effect_and_exact_manifest(
        self, delivery, operator
    ):
        run, _, _ = await awaiting_publish(delivery)
        env = delivery["env"]
        env.fake.fail_next("commit", Fault(unknown_after_success=True))
        assert (await env.runner.tick()).parked == 1
        effect = await env.effects.store.get(f"{run.id}:1:1:publish:commit")
        assert (await env.effects.recover(effect)).state == "adopted"
        assert (
            await operator["commands"].submit(envelope(operator, "retry"))
        ).reason == "retry_scheduled"
        assert (await env.runner.tick()).completed == 1
        assert len(native_writes(env)) == 1 and len(env.fake.drafts) == 1
        assert (await env.runs.load(run.id)).state == State.VERIFYING
