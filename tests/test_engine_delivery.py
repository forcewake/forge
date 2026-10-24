"""Actual SQL lane-to-delivery scenarios over both provider ports."""

from datetime import timedelta
from decimal import Decimal
import hashlib

import pytest

from forge_protocol import CandidateBundle, RunIdentity
from forge.domain.budget import Resources
from forge.domain.states import Handler, State
from forge.engine.delivery_values import DeliverySettings
from forge.engine.publish import PublishHandler
from forge.engine.ready import ReadyHandler
from forge.engine.review import ReviewHandler
from forge.engine.verify import VerifyHandler
from forge.port_values import CheckResult, ReviewVerdict
from forge.providers.fake import Blob, Fault
from forge.providers.fake.state import commit_value
from forge.store.delivery import DeliveryStore
from tests.test_engine_lane_handlers import (
    advance,
    case as case,
    env as env,
    settings as settings,
    report,
    started,
)


class Blobs:
    def __init__(self, store):
        self.store = store

    async def get(self, digest):
        return await self.store.read(digest)


class Reviewer:
    def __init__(self):
        self.calls = []
        self.decision = "approve"
        self.wrong_sha = None

    async def review(self, diff, approved, *, candidate_sha):
        self.calls.append((diff, approved, candidate_sha))
        return ReviewVerdict(
            candidate_sha=self.wrong_sha or candidate_sha,
            body="Reviewed the exact candidate.",
            decision=self.decision,
            evidence="test:read_only",
        )


@pytest.fixture
async def delivery(case):
    env = case["env"]
    store, blobs, reviewer = DeliveryStore(env.factory), Blobs(case["api"].blobs), Reviewer()
    policy = DeliverySettings(
        poll_seconds=1,
        verification_seconds=60,
        missing_checks_grace_seconds=2,
        repair_limit=2,
        repair_minimum=Resources(usd=Decimal(".01"), calls=1, tokens=1),
        closing_minimum=Resources(usd=Decimal(".01"), calls=1, tokens=1),
    )
    env.runner.handlers.update(
        {
            Handler.PUBLISH: PublishHandler(
                case["store"], store, env.fake, blobs, env.clock, policy
            ),
            Handler.VERIFY: VerifyHandler(
                case["store"], store, env.fake, env.fake, env.fake, case["budgets"], env.clock
            ),
            Handler.REVIEW: ReviewHandler(
                case["store"],
                store,
                env.fake,
                env.fake,
                blobs,
                case["budget"],
                lambda lease, alias: reviewer,
                env.clock,
            ),
            Handler.READY: ReadyHandler(
                case["store"], store, env.fake, env.fake, env.fake, env.clock
            ),
        }
    )
    case.update(delivery_store=store, reviewer=reviewer, delivery_policy=policy)
    return case


def full_patch(old, new):
    def oid(body):
        return hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()

    return (
        (
            f"diff --git a/a.py b/a.py\nindex {oid(old)}..{oid(new)} 100644\n"
            "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n"
        ).encode()
        + b"-"
        + old
        + b"+"
        + new
    )


async def completed(case, pending, *, old=b"old\n", new=b"new\n", claimed_base=None, diff=None):
    diff = full_patch(old, new) if diff is None else diff
    key = hashlib.sha256(diff).hexdigest()

    async def chunks():
        yield diff

    await case["api"].blobs.put(key, chunks())
    await case["api"].record_blob(pending.identity, key, len(diff))
    value = CandidateBundle(
        **pending.identity.model_dump(),
        diff_sha256=key,
        diff_size_bytes=len(diff),
        base_sha=claimed_base or pending.dispatch.execution_base_sha,
        approved_digest=pending.dispatch.approved_digest,
        driver=pending.dispatch.driver,
        model=pending.dispatch.model,
        terminal_reason="completed",
    )
    await case["api"].candidate(pending.identity, value)
    await report(
        case,
        pending,
        status="completed",
        classification="completed",
        reason="completed",
        candidate=key,
    )
    assert (await case["env"].runner.tick()).completed == 1
    return value


async def published(case):
    run, frozen, pending = await started(case)
    await completed(case, pending)
    tick = await case["env"].runner.tick()
    current = await case["env"].runs.load(run.id)
    assert tick.completed == 1 and current.state == State.VERIFYING, current.reason
    sha = await case["env"].fake.head(
        pending.dispatch.provider and case["env"].intent.task.subject.repo, "forge/" + run.id
    )
    return run, frozen, pending, sha


def check(
    case,
    sha,
    *,
    status="passed",
    role="verification",
    tested_sha=None,
    age=0,
    failure_kind="unknown",
):
    repo = case["env"].intent.task.subject.repo
    value = CheckResult(
        name="unit",
        tested_sha=tested_sha or sha,
        native_id="check-1",
        attempt=1,
        role=role,
        status=status,
        observed_at=case["env"].clock.now() - timedelta(seconds=age),
        evidence="test:native-check",
        failure_kind=failure_kind,
    )
    case["env"].fake.checks[repo.identity.digest] = (value,)
    return value


async def test_green_ci_closes_under_separate_reserve_and_reaches_ready(delivery):
    env = delivery["env"]
    run, frozen, pending, sha = await published(delivery)
    check(delivery, sha)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    current = await env.runs.load(run.id)
    assert current.state == State.READY_FOR_HUMAN, current.reason
    assert len(env.fake.drafts) == 1 and len(delivery["reviewer"].calls) == 1
    assert delivery["reviewer"].calls[0][1] == frozen.approved
    assert delivery["reviewer"].calls[0][2] == sha
    assert (await delivery["budgets"].load(run.id, "implementation")).balance.held.usd == Decimal(
        "2"
    )
    assert (await delivery["budgets"].load(run.id, "closing")).balance.held.usd == Decimal("2")
    assert delivery["native"].writes.count("/key/generate") == 2
    assert sum(name == "commit" for name, _ in env.fake.calls) == 1


async def test_code_failure_repairs_exact_owned_head_without_reopening_approval(delivery):
    env = delivery["env"]
    run, frozen, pending, sha = await published(delivery)
    check(delivery, sha, status="failed", failure_kind="code")
    assert (await env.runner.tick()).completed == 1
    current = await env.runs.load(run.id)
    assert (current.state, current.attempt, current.generation) == (State.RUNNING, 2, 2)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    identity = RunIdentity(forge_run_id=run.id, attempt=2, generation=2)
    second = await delivery["store"].pending(identity)
    assert second.dispatch.base_sha == frozen.spec.base_sha
    assert second.dispatch.execution_base_sha == sha != frozen.spec.base_sha
    assert second.brief.repair == second.dispatch.repair
    assert second.brief.approved_digest == frozen.approved.approved_digest
    assert second.brief.spec_json == frozen.spec.spec_json
    assert "failed" in second.brief.repair.failure_context
    assert sum(name == "ensure_branch" for name, _ in env.fake.calls) == 1
    await completed(delivery, second, old=b"new\n", new=b"repaired\n")
    assert (await env.runner.tick()).completed == 1
    next_sha = await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id)
    check(delivery, next_sha)
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.READY_FOR_HUMAN
    assert (
        len(env.fake.drafts) == 1 and sum(name == "ensure_draft" for name, _ in env.fake.calls) == 1
    )
    assert len((await delivery["budgets"].load(run.id, "implementation")).reservations) == 2


@pytest.mark.parametrize("failure_kind", ["unknown", "infrastructure", "configuration"])
async def test_non_code_failure_never_spends_on_a_repair(delivery, failure_kind):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha, status="failed", failure_kind=failure_kind)
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert current.reason == "verification_non_code_failure"
    assert (current.attempt, current.generation) == (1, 1)
    assert len(env.fake.lanes) == 1 and not delivery["reviewer"].calls


async def test_exhausted_shared_pool_blocks_code_repair(delivery):
    env = delivery["env"]
    env.runner.handlers[Handler.DISPATCH].settings = delivery["policy"].model_copy(
        update={"requested": Resources(usd=Decimal("8"), calls=8, tokens=9000)}
    )
    run, _, _, sha = await published(delivery)
    check(delivery, sha, status="failed", failure_kind="code")
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "budget_exhausted"
    assert len(env.fake.lanes) == 1


@pytest.mark.parametrize("stage", ["verify", "ready"])
async def test_human_head_move_cannot_reach_readiness_or_spend_repair(delivery, stage):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha)
    if stage == "ready":
        assert (await env.runner.tick()).completed == 1
        assert (await env.runner.tick()).completed == 1
    repo = env.fake.repo(env.intent.task.subject.repo)
    human = commit_value({"a.py": Blob(b"human\n")}, sha, "Human change")
    repo.commits[human.sha] = human
    repo.branches["forge/" + run.id] = human.sha
    assert (await env.runner.tick()).parked == 1
    assert "branch_drift" in (await env.runs.load(run.id)).reason
    assert repo.branches["forge/" + run.id] == human.sha


@pytest.mark.parametrize("mode", ["lane", "wrong_sha", "stale", "future", "missing"])
async def test_readiness_needs_fresh_independent_required_checks(delivery, mode):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    if mode != "missing":
        check(
            delivery,
            sha,
            role="lane" if mode == "lane" else "verification",
            tested_sha="f" * 40 if mode == "wrong_sha" else None,
            age=4000 if mode == "stale" else -1 if mode == "future" else 0,
        )
    if mode in ("lane", "missing", "wrong_sha"):
        assert (await env.runner.tick()).rescheduled == 1
        advance(delivery, 2)
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).state == State.BLOCKED
    assert not delivery["reviewer"].calls


async def test_repair_artifact_from_original_base_is_refused(delivery):
    from forge.api.lane_v1_values import LaneConflict

    env = delivery["env"]
    run, frozen, _, sha = await published(delivery)
    check(delivery, sha, status="failed", failure_kind="code")
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    pending = await delivery["store"].pending(
        RunIdentity(forge_run_id=run.id, attempt=2, generation=2)
    )
    with pytest.raises(LaneConflict, match="dispatch"):
        await completed(delivery, pending, claimed_base=frozen.spec.base_sha)


async def test_moved_head_between_repair_authorization_and_dispatch_is_preserved(delivery):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha, status="failed", failure_kind="code")
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    repo = env.fake.repo(env.intent.task.subject.repo)
    human = commit_value({"a.py": Blob(b"human\n")}, sha, "Human change")
    repo.commits[human.sha], repo.branches["forge/" + run.id] = human, human.sha
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "branch_drift"
    assert len(env.fake.lanes) == 1


async def test_unknown_commit_recovers_same_native_write_after_process_loss(delivery):
    env = delivery["env"]
    run, _, pending = await started(delivery)
    await completed(delivery, pending)
    env.fake.fail_next("commit", Fault(unknown_after_success=True))
    assert (await env.runner.tick()).parked == 1
    effect = await env.effects.store.get(f"{run.id}:1:1:publish:commit")
    assert effect.state == "unknown"
    assert (await env.effects.recover(effect)).state == "adopted"
    assert sum(name == "commit" for name, _ in env.fake.calls) == 1


async def test_cached_commit_receipt_cannot_publish_a_draft_after_human_head_move(
    delivery, monkeypatch
):
    env = delivery["env"]
    run, _, pending = await started(delivery)
    await completed(delivery, pending)
    original = env.effects.commit

    async def moved(*args, **kwargs):
        receipt = await original(*args, **kwargs)
        repo = env.fake.repo(env.intent.task.subject.repo)
        human = commit_value({"a.py": Blob(b"human\n")}, receipt.sha, "Human change")
        repo.commits[human.sha], repo.branches["forge/" + run.id] = human, human.sha
        return receipt

    monkeypatch.setattr(env.effects, "commit", moved)
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "branch_drift"
    assert not env.fake.drafts


async def test_closing_wrong_candidate_cannot_publish_a_review(delivery):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha)
    delivery["reviewer"].wrong_sha = "f" * 40
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "closing_review_wrong_candidate"
    assert not any(name == "post_review" for name, _ in env.fake.calls)


async def test_native_commit_conflict_preserves_competing_head(delivery):
    env = delivery["env"]
    run, _, pending = await started(delivery)
    await completed(delivery, pending)
    env.fake.fail_next("commit", Fault(conflict=True))
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "branch_drift"
    repo = env.fake.repo(env.intent.task.subject.repo)
    assert repo.commits[repo.branches["forge/" + run.id]].message == "competing commit"
    assert not env.fake.drafts


async def test_known_commit_after_power_loss_still_checks_current_head(delivery):
    from forge.engine.runner import LeaseHolder, StepContext

    env = delivery["env"]
    run, _, pending = await started(delivery)
    await completed(delivery, pending)

    class PowerLoss(BaseException):
        pass

    def crash(point, effect):
        if point == "after_t3" and effect.kind == "commit":
            raise PowerLoss()

    env.effects.hook = crash
    (lease,) = await env.steps.claim_due("crashed", env.clock.now(), 60)
    context = StepContext(LeaseHolder(lease), await env.steps.load(lease.step_id), env.effects)
    with pytest.raises(PowerLoss):
        await env.runner.handlers[Handler.PUBLISH](context)
    env.effects.hook = None
    repo = env.fake.repo(env.intent.task.subject.repo)
    owned_sha = repo.branches["forge/" + run.id]
    human = commit_value({"a.py": Blob(b"human\n")}, owned_sha, "Human change")
    repo.commits[human.sha], repo.branches["forge/" + run.id] = human, human.sha
    advance(delivery, 61)
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "branch_drift"
    assert sum(name == "commit" for name, _ in env.fake.calls) == 1
    assert not env.fake.drafts


async def test_repair_identity_and_successor_roll_back_together(delivery, monkeypatch):
    import forge.store.steps as step_module

    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha, status="failed", failure_kind="code")
    original = step_module.enqueue_in

    async def crash(session, row, request):
        result = await original(session, row, request)
        if request.handler == Handler.DISPATCH:
            raise RuntimeError("synthetic repair transaction crash")
        return result

    with monkeypatch.context() as change:
        change.setattr(step_module, "enqueue_in", crash)
        with pytest.raises(RuntimeError, match="synthetic repair"):
            await env.runner.tick()
    current = await env.runs.load(run.id)
    assert (current.state, current.attempt, current.generation) == (State.VERIFYING, 1, 1)
    assert await env.steps.get(f"{run.id}:2:2:repair") is None
    advance(delivery, 61)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).generation == 2


async def test_foreign_published_candidate_cannot_authorize_repair(delivery):
    from sqlalchemy import select
    from forge.store.models_execution import EngineCandidate

    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha, status="failed", failure_kind="code")
    assert (await env.runner.tick()).completed == 1
    from forge.store.records import RunRequest

    await env.runs.admit(RunRequest("f" * 32, env.fake.provider, "test", "42", "issue", "2"))
    async with env.factory() as session, session.begin():
        row = await session.scalar(select(EngineCandidate).where(EngineCandidate.run_id == run.id))
        # A journal row from another run is never authority, even with an equal SHA.
        row.run_id = "f" * 32
    assert (await env.runner.tick()).parked == 1
    assert len(env.fake.lanes) == 1
    assert (await env.runs.load(run.id)).reason == "handler_error:LaneAuthError"
