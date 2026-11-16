"""Human-push reviews have separate authority, exact input and conservative recovery."""

from decimal import Decimal
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from forge.domain.budget import Resources
from forge.domain.run_spec import RunPolicy
from forge.domain.states import Handler, State
from forge.engine.native_review import NativeReviewHandler, NativeReviewReconciler
from forge.ingress_values import IngressEvent
from forge.port_values import ReviewVerdict
from forge.providers.fake import Blob, Fault
from forge.providers.fake.state import commit_value
from forge.review.native import ReactiveReviews
from forge.review.native_values import NativeReviewInput, NativeReviewPolicy
from forge.store.command_runs import CommandRuns
from forge.store.models_execution import EngineCandidate, EngineEffect
from forge.store.models_review import EngineReactiveReview, EngineReviewCursor
from forge.store.models_runs import EngineApprovedInput
from forge.store.native_review_admission import NativeReviewAdmission
from forge.store.native_reviews import NativeReviewStore
from forge.store.records import StoreConflict
from tests.test_engine_delivery import (
    case as case,
    check,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)


class NativeReviewer:
    def __init__(self, case):
        self.case, self.calls, self.fail, self.wrong = case, [], False, False
        self.observer = None
        self.lease = None

    async def review(self, diff, request):
        assert isinstance(request, NativeReviewInput)
        assert (
            await self.case["native_store"].inputs(self.lease(), now=self.case["env"].clock.now())
            == request
        )
        async with self.case["env"].factory() as session:
            row = await session.get(EngineReactiveReview, request.request_id)
            assert row.state == "model_requested"
            assert row.input_json == request.canonical_bytes.decode()
            assert request.diff_sha256 == hashlib.sha256(diff).hexdigest()
        assert (
            await self.case["budgets"].load(request.request_id, "closing")
        ).balance.held.usd == 1
        self.calls.append((diff, request))
        if self.observer:
            await self.observer(request)
        if self.fail:
            raise TimeoutError("synthetic lost model response")
        return ReviewVerdict(
            candidate_sha="0" * 40 if self.wrong else request.head_sha,
            body="Read the exact native change.",
            decision="changes_requested",
            evidence="test:model",
        )


@pytest.fixture
async def native(delivery):
    env = delivery["env"]
    run, frozen, pending, sha = await published(delivery)
    check(delivery, sha)
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    draft = next(iter(env.fake.drafts.values()))
    policy = NativeReviewPolicy(
        budget=RunPolicy.model_validate_json(
            json.dumps(
                {name: frozen.spec.model_dump(mode="json")[name] for name in RunPolicy.model_fields}
            )
        ),
        requested=Resources(usd=Decimal("1"), calls=1, tokens=500),
        minimum=Resources(usd=Decimal(".01"), calls=1, tokens=1),
    )
    admission, store = NativeReviewAdmission(env.factory), NativeReviewStore(env.factory)
    reviewer = NativeReviewer(delivery)
    service = ReactiveReviews(
        admission, env.fake, env.fake, env.clock, lambda repo: env.fake, lambda repo: policy
    )

    def reviewer_for(lease, alias):
        reviewer.lease = lease
        return reviewer

    handler = NativeReviewHandler(
        store, env.fake, env.fake, delivery["budget"], reviewer_for, env.clock
    )
    env.runner.handlers[Handler.FEEDBACK_REVIEW] = handler
    delivery.update(
        root=run,
        sha=sha,
        draft=draft,
        native_policy=policy,
        native_admission=admission,
        native_store=store,
        native_reviewer=reviewer,
        reactive=service,
        native_handler=handler,
        native_reconciler=NativeReviewReconciler(store, env.fake, env.fake, env.effects, env.clock),
    )
    return delivery


def push(case, content=b"human\n", *, files=None, actor=None):
    env, review = case["env"], case["draft"].ref
    repo = env.fake.repo(review.repo)
    branch = "forge/" + case["root"].id
    commit = commit_value(files or {"a.py": Blob(content)}, repo.branches[branch], "human push")
    repo.commits[commit.sha], repo.branches[branch] = commit, commit.sha
    return IngressEvent(
        delivery_key=env.fake.provider + ":test:" + str(uuid4()),
        event="pull_request" if env.fake.provider == "github" else "merge_request",
        action="synchronize" if env.fake.provider == "github" else "update",
        repo=review.repo,
        actor=actor or env.intent.actor,
        target=review,
        head_sha=commit.sha,
    )


async def request_row(case, head):
    async with case["env"].factory() as session:
        return await session.scalar(
            select(EngineReactiveReview).where(EngineReactiveReview.head_sha == head)
        )


async def test_human_push_is_exact_incremental_readonly_review_with_own_budget_and_no_approval(
    native,
):
    env = native["env"]
    candidates, approvals = await env.count(EngineCandidate), await env.count(EngineApprovedInput)
    initial_commits = sum(name == "commit" for name, _ in env.fake.calls)
    event = push(native)
    accepted = await native["reactive"].submit(event)
    assert accepted.status == "accepted", accepted
    row = await request_row(native, event.head_sha)
    assert row.run_id is not None and row.base_sha == native["sha"]
    assert (await env.runs.load(row.run_id)).subject_kind == "review"
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(row.run_id)).state == State.READY_FOR_HUMAN
    diff, value = native["native_reviewer"].calls[0]
    assert value.base_sha == native["sha"] and value.head_sha == event.head_sha
    assert b"-new\n" in diff and b"+human\n" in diff
    assert (
        await env.count(EngineCandidate) == candidates
        and await env.count(EngineApprovedInput) == approvals
    )
    assert sum(name == "commit" for name, _ in env.fake.calls) == initial_commits
    async with env.factory() as session:
        cursor = await session.scalar(select(EngineReviewCursor))
        assert cursor.last_reviewed_sha == event.head_sha
        effects = list(
            await session.scalars(select(EngineEffect).where(EngineEffect.run_id == row.run_id))
        )
        assert {effect.kind for effect in effects} == {"gw_mint", "gw_block", "post_comment"}
    duplicate = await native["reactive"].submit(event)
    assert duplicate.reason == "native_review_duplicate"
    assert len(native["native_reviewer"].calls) == 1
    second = push(native, b"second\n")
    assert (await native["reactive"].submit(second)).status == "accepted"
    assert (await env.runner.tick()).completed == 1
    next_diff, next_value = native["native_reviewer"].calls[-1]
    assert next_value.base_sha == event.head_sha
    assert b"-human\n" in next_diff and b"+second\n" in next_diff
    assert "read_only_review" in await CommandRuns(env.factory).snapshot(
        await env.runs.load(row.run_id)
    )
    assert await CommandRuns(env.factory).subject(row.run_id) == env.intent.task.subject


async def test_bot_push_disabled_repo_and_non_push_facts_spend_nothing(native):
    env = native["env"]
    before = len(native["native"].writes)
    event = push(native, actor=env.fake.bot_identity())
    assert (await native["reactive"].submit(event)).reason == "bot_authored"
    event = event.model_copy(update={"actor": env.intent.actor, "event": "issue_comment"})
    assert (await native["reactive"].submit(event)).reason == "native_push_fact_required"
    native["reactive"].policy_for = lambda repo: None
    assert (await native["reactive"].submit(event)).reason == "native_review_disabled"
    assert not native["native_reviewer"].calls and len(native["native"].writes) == before
    assert await env.count(EngineReactiveReview) == 0


async def test_new_head_queues_durably_and_stale_work_never_spends(native):
    env = native["env"]
    first, second = push(native, b"first\n"), None
    assert (await native["reactive"].submit(first)).status == "accepted"
    second = push(native, b"second\n")
    assert (await native["reactive"].submit(second)).status == "accepted"
    assert (await request_row(native, second.head_sha)).run_id is None
    before = len(native["native"].writes)
    assert (await env.runner.tick()).parked == 1
    assert not native["native_reviewer"].calls and len(native["native"].writes) == before
    assert await native["reactive"].activate_pending() == 1
    assert (await env.runner.tick()).completed == 1
    assert len(native["native_reviewer"].calls) == 1
    assert native["native_reviewer"].calls[0][1].head_sha == second.head_sha


async def test_unknown_model_is_held_and_never_called_again(native):
    native["native_reviewer"].fail = True
    event = push(native)
    await native["reactive"].submit(event)
    assert (await native["env"].runner.tick()).parked == 1
    row = await request_row(native, event.head_sha)
    assert row.state == "model_requested" and row.result_json is None
    assert (await native["budgets"].load(row.run_id, "closing")).balance.held.usd == 0
    await native["reactive"].submit(event)
    await native["native_reconciler"].tick()
    assert len(native["native_reviewer"].calls) == 1


async def test_lost_comment_response_is_probed_and_adopted_without_model_or_comment_replay(native):
    env = native["env"]
    event = push(native)
    await native["reactive"].submit(event)
    env.fake.fail_next("post_comment", Fault(unknown_after_success=True))
    assert (await env.runner.tick()).parked == 1
    before = sum(method == "post_comment" for method, _ in env.fake.calls)
    assert await native["native_reconciler"].tick() == 1
    assert sum(method == "post_comment" for method, _ in env.fake.calls) == before
    assert len(native["native_reviewer"].calls) == 1
    row = await request_row(native, event.head_sha)
    assert (await env.runs.load(row.run_id)).state == State.READY_FOR_HUMAN
    async with env.factory() as session:
        assert (
            await session.scalar(select(EngineReviewCursor))
        ).last_reviewed_sha == event.head_sha


async def test_head_drift_after_native_comment_preserves_history_without_advancing_cursor(
    native, monkeypatch
):
    env = native["env"]
    event = push(native)
    await native["reactive"].submit(event)
    original = env.fake.post_comment

    async def move_after(*args):
        outcome = await original(*args)
        push(native, b"newer human\n")
        return outcome

    monkeypatch.setattr(env.fake, "post_comment", move_after)
    assert (await env.runner.tick()).parked == 1
    row = await request_row(native, event.head_sha)
    assert row.state == "commented"
    async with env.factory() as session:
        assert (await session.scalar(select(EngineReviewCursor))).last_reviewed_sha == native["sha"]
        effect = await session.scalar(
            select(EngineEffect).where(
                EngineEffect.run_id == row.run_id, EngineEffect.kind == "post_comment"
            )
        )
        assert effect.state == "committed"
    assert await native["native_reconciler"].tick() == 0


@pytest.mark.parametrize("fault", ("binary", "large", "wrong_head"))
async def test_unsupported_or_wrong_native_content_never_advances_or_codes(native, fault):
    env = native["env"]
    event = push(
        native,
        b"\0binary" if fault == "binary" else b"x" * 131073 if fault == "large" else b"human\n",
    )
    native["native_reviewer"].wrong = fault == "wrong_head"
    await native["reactive"].submit(event)
    assert (await env.runner.tick()).parked == 1
    assert len(native["native_reviewer"].calls) == (1 if fault == "wrong_head" else 0)
    async with env.factory() as session:
        assert (await session.scalar(select(EngineReviewCursor))).last_reviewed_sha == native["sha"]


async def test_review_lease_cannot_commit_dispatch_draft_change_labels_or_expand_its_key(native):
    env = native["env"]
    event = push(native)
    await native["reactive"].submit(event)
    (lease,) = await env.steps.claim_due("security-proof", env.clock.now(), 60)
    for kind in (
        "commit",
        "ensure_branch",
        "ensure_draft",
        "set_label",
        "dispatch_lane",
        "gw_update",
    ):
        with pytest.raises(StoreConflict, match="cannot authorize implementation"):
            await env.effects.store.request_once(
                lease,
                key="forbidden:" + kind,
                kind=kind,
                payload_json="{}",
                payload_digest=hashlib.sha256(b"{}").hexdigest(),
                repo_key=event.repo.identity.digest,
                head_branch="forge/" + native["root"].id,
            )
    with pytest.raises(StoreConflict, match="exact stored comment"):
        await env.effects.store.request_once(
            lease,
            key="forged",
            kind="post_comment",
            payload_json="{}",
            payload_digest=hashlib.sha256(b"{}").hexdigest(),
        )


async def test_killed_after_result_commit_resumes_cached_output_without_another_model_call(native):
    from datetime import timedelta

    class Death(BaseException):
        pass

    env = native["env"]
    event = push(native)
    await native["reactive"].submit(event)

    def die(point, row):
        if point == "after_t1" and row.kind == "post_comment":
            raise Death()

    env.effects.hook = die
    with pytest.raises(Death):
        await env.runner.tick()
    row = await request_row(native, event.head_sha)
    assert row.result_json is not None and len(native["native_reviewer"].calls) == 1
    env.effects.hook = None
    env.clock.instant += timedelta(seconds=61)
    env.fake.now = env.clock.now()
    assert (await env.runner.tick()).completed == 1
    assert len(native["native_reviewer"].calls) == 1
    assert (await env.runs.load(row.run_id)).state == State.READY_FOR_HUMAN


async def test_model_input_is_immutable_and_cannot_be_read_before_its_intent(native):
    from forge.review.native_values import NativeReviewInput

    env = native["env"]
    event = push(native)
    await native["reactive"].submit(event)
    (lease,) = await env.steps.claim_due("input-boundary", env.clock.now(), 60)
    store = native["native_store"]
    with pytest.raises(StoreConflict, match="not durably requested"):
        await store.inputs(lease, now=env.clock.now())
    work = await store.load(lease, now=env.clock.now())
    value = NativeReviewInput(
        request_id=work.request_id,
        review=work.review,
        base_sha=work.base_sha,
        head_sha=work.head_sha,
        actor=work.actor,
        diff_sha256="a" * 64,
        policy_digest=work.policy.digest,
    )
    with pytest.raises(StoreConflict, match="signed request"):
        await store.prepare_input(
            lease, value.model_copy(update={"base_sha": "0" * 40}), now=env.clock.now()
        )
    with pytest.raises(StoreConflict, match="before paid"):
        await store.start_model(lease, now=env.clock.now())
    await store.prepare_input(lease, value, now=env.clock.now())
    with pytest.raises(StoreConflict, match="immutable input"):
        await store.prepare_input(
            lease, value.model_copy(update={"diff_sha256": "b" * 64}), now=env.clock.now()
        )
    assert await store.start_model(lease, now=env.clock.now())
    assert not await store.start_model(lease, now=env.clock.now())
    result = ReviewVerdict(
        candidate_sha=work.head_sha,
        body="Observed exact bytes.",
        decision="inconclusive",
        evidence="test:result",
    )
    with pytest.raises(StoreConflict, match="another head"):
        await store.record(
            lease, result.model_copy(update={"candidate_sha": "0" * 40}), now=env.clock.now()
        )
    await store.record(lease, result, now=env.clock.now())
    with pytest.raises(StoreConflict, match="cannot be replaced"):
        await store.record(
            lease, result.model_copy(update={"body": "different"}), now=env.clock.now()
        )
    assert await store.inputs(lease, now=env.clock.now()) == value
    with pytest.raises(StoreConflict, match="durable receipt"):
        await store.commented(lease, now=env.clock.now())
    assert not await store.advance(lease, observed_head=work.head_sha, now=env.clock.now())


@pytest.mark.parametrize(
    "fault", ("blob_oid", "missing_blob", "tree_sha", "gitlink", "files", "bytes", "non_utf8")
)
async def test_native_diff_refuses_incomplete_or_unsupported_native_evidence_before_model(
    native, monkeypatch, fault
):
    from forge.port_values import TreeEntry

    env = native["env"]
    event = push(
        native,
        b"\xff" if fault == "non_utf8" else b"human\n",
        files={"a.py": Blob(b"human\n"), "b.py": Blob(b"added\n")} if fault == "files" else None,
    )
    if fault == "blob_oid":

        async def wrong(*args):
            return b"substituted\n"

        monkeypatch.setattr(env.fake, "read_blob", wrong)
    elif fault == "missing_blob":

        async def missing(*args):
            return None

        monkeypatch.setattr(env.fake, "read_blob", missing)
    elif fault in ("tree_sha", "gitlink"):
        original = env.fake.read_tree

        async def tree(*args):
            value = await original(*args)
            if fault == "tree_sha":
                return value.model_copy(update={"sha": "0" * 40})
            if value.sha != event.head_sha:
                return value
            return value.model_copy(
                update={
                    "entries": (TreeEntry(path="a.py", sha="0" * 40, kind="commit", mode="160000"),)
                }
            )

        monkeypatch.setattr(env.fake, "read_tree", tree)
    elif fault in ("files", "bytes"):
        policy = native["native_policy"].model_copy(
            update={"max_files": 1} if fault == "files" else {"max_bytes": 1}
        )
        native["reactive"].policy_for = lambda repo: policy
    await native["reactive"].submit(event)
    assert (await env.runner.tick()).parked == 1
    assert not native["native_reviewer"].calls


async def test_cursor_compare_and_swap_refuses_concurrent_authority_change(native):
    env = native["env"]
    event = push(native)
    await native["reactive"].submit(event)

    async def move_cursor(request):
        async with env.factory.begin() as session:
            cursor = await session.scalar(select(EngineReviewCursor))
            cursor.last_reviewed_sha = "f" * 40

    native["native_reviewer"].observer = move_cursor
    assert (await env.runner.tick()).parked == 1
    row = await request_row(native, event.head_sha)
    assert row.state == "commented"
    assert (await env.runs.load(row.run_id)).reason == "native_review_cursor_conflict"


async def test_noop_native_push_and_bounded_activation_do_not_spend(native):
    env = native["env"]
    event = push(native, b"new\n")
    await native["reactive"].submit(event)
    assert (await env.runner.tick()).parked == 1
    assert not native["native_reviewer"].calls
    with pytest.raises(ValueError, match="bounded"):
        await native["reactive"].activate_pending(limit=0)
    with pytest.raises(ValueError, match="bounded"):
        await native["native_reconciler"].tick(limit=101)


async def test_complete_native_create_delete_executable_and_symlink_diff_reaches_model(native):
    event = push(
        native,
        files={"script.sh": Blob(b"#!/bin/sh\n", "100755"), "link": Blob(b"script.sh", "120000")},
    )
    assert (await native["reactive"].submit(event)).status == "accepted"
    assert (await native["env"].runner.tick()).completed == 1
    diff, value = native["native_reviewer"].calls[0]
    assert b'File "a.py": 100644 -> absent' in diff
    assert b'File "script.sh": absent -> 100755' in diff
    assert b'File "link": absent -> 120000' in diff
    assert b"No newline at end of file" in diff
    assert value.head_sha == event.head_sha


async def test_already_reviewed_exact_native_head_does_not_queue_or_spend(native):
    env = native["env"]
    event = IngressEvent(
        delivery_key=env.fake.provider + ":test:" + str(uuid4()),
        event="pull_request" if env.fake.provider == "github" else "merge_request",
        action="synchronize" if env.fake.provider == "github" else "update",
        repo=native["draft"].ref.repo,
        target=native["draft"].ref,
        actor=env.intent.actor,
        head_sha=native["sha"],
    )
    result = await native["reactive"].submit(event)
    assert result.reason == "native_review_current"
    assert await env.count(EngineReactiveReview) == 0
    assert not native["native_reviewer"].calls


async def test_repository_display_rename_keeps_native_review_scope_and_receipts(native):
    event = push(native)
    repo = event.repo.model_copy(update={"full_name": "renamed/repository"})
    event = event.model_copy(
        update={"repo": repo, "target": event.target.model_copy(update={"repo": repo})}
    )
    result = await native["reactive"].submit(event)
    assert result.status == "accepted", result
    assert (await native["env"].runner.tick()).completed == 1
    assert native["native_reviewer"].calls[0][1].review.repo.identity == event.repo.identity


@pytest.mark.parametrize("change", ("kind", "review", "display"))
async def test_native_comment_receipt_requires_its_kind_and_native_review_identity(native, change):
    from forge.store.native_reviews import comment_proof, work_value

    env = native["env"]
    event = push(native)
    await native["reactive"].submit(event)
    assert (await env.runner.tick()).completed == 1
    async with env.factory.begin() as session:
        row = await session.scalar(
            select(EngineReactiveReview).where(EngineReactiveReview.head_sha == event.head_sha)
        )
        work = await work_value(session, row)
        effect = await session.scalar(
            select(EngineEffect).where(
                EngineEffect.run_id == row.run_id, EngineEffect.kind == "post_comment"
            )
        )
        receipt = json.loads(effect.outcome_json)
        if change == "kind":
            receipt["value"]["kind"] = "review"
        elif change == "review":
            receipt["value"]["subject"]["number"] += 1
        else:
            receipt["value"]["subject"]["repo"]["full_name"] = "later/display-name"
        effect.outcome_json = json.dumps(receipt)
        if change == "display":
            await comment_proof(session, row, work)
        else:
            with pytest.raises(StoreConflict, match="another review"):
                await comment_proof(session, row, work)
