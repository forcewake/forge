"""Actual server model admission remains read-only and bound to one native push."""

import asyncio
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

import pytest
import httpx
from sqlalchemy import select

from forge.domain.budget import Observation, Resources
from forge.domain.plan import RepositoryIdentity
from forge.domain.run_spec import RunPolicy
from forge.domain.states import Handler
from forge.engine.native_model_services import ServerNativeReviewers
from forge.port_values import Actor, ModelReply, RepoRef, ReviewRef
from forge.review.native_reviewer import NativeTextReviewer
from forge.review.native_values import NativeReviewInput, NativeReviewPolicy
from forge.review.reviewer import ReviewLimits
from forge.store.models_budget import EngineUsage
from forge.store.models_review import EngineReactiveReview, EngineReviewCursor
from forge.store.native_reviews import NativeReviewStore
from forge.store.records import RunRequest, StepRequest, StoreConflict
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests.test_budget_engine import case as case
from tests.test_server_model_services import frozen_budget, models_for, text_wire

DIFF = b"diff --git a/a.py b/a.py\n-old\n+new\n"


def policy_and_request(frozen, *, request_id=None, repo=None):
    policy = NativeReviewPolicy(
        budget=RunPolicy.model_validate_json(
            json.dumps(
                {name: frozen.model_dump(mode="json")[name] for name in RunPolicy.model_fields}
            )
        ),
        requested=Resources(usd=Decimal("2"), calls=2, tokens=60000),
        minimum=Resources(usd=Decimal("0.72"), calls=1, tokens=1),
    )
    repo = repo or RepoRef(
        identity=frozen.plan.steps[0].citations[0].repository, full_name="owner/repo"
    )
    request = NativeReviewInput(
        request_id=request_id or uuid4().hex,
        review=ReviewRef(repo=repo, number=2),
        base_sha="a" * 40,
        head_sha="b" * 40,
        actor=Actor(
            provider=repo.identity.provider,
            connection_id=repo.identity.connection_id,
            native_id="human",
            login="owner",
            is_bot=False,
        ),
        diff_sha256=hashlib.sha256(DIFF).hexdigest(),
        policy_digest=policy.digest,
    )
    return policy, request


def answer(request, **changes):
    return {
        "request_id": request.request_id,
        "head_sha": request.head_sha,
        "diff_sha256": request.diff_sha256,
        "policy_digest": request.policy_digest,
        "decision": "changes_requested",
        "body": "Review the exact changed line in a.py.",
        **changes,
    }


class Model:
    def __init__(self, response=None, failure=None):
        self.response, self.failure, self.calls = response, failure, []

    async def complete(self, **arguments):
        self.calls.append(arguments)
        if self.failure:
            raise self.failure
        request = NativeReviewInput.model_validate(json.loads(arguments["prompt"])["native_review"])
        return ModelReply(
            text=self.response or json.dumps(answer(request)),
            usage=Observation(usd=Decimal("0.01"), calls=1, tokens=14),
            evidence="synthetic",
        )


async def prepared(case):
    frozen = await frozen_budget(case)
    child = await RunStore(case["factory"]).admit(
        RunRequest(
            uuid4().hex, "github", "app-1", "42", "review", "2", case["run"].id, case["run"].id
        )
    )
    policy, request = policy_and_request(
        frozen,
        request_id=child.id,
        repo=RepoRef(
            identity=RepositoryIdentity(
                provider="github", connection_id="app-1", repository_id="42"
            ),
            full_name="owner/repo",
        ),
    )
    await StepStore(case["factory"]).complete_and_enqueue(
        case["lease"],
        "{}",
        now=case["clock"].now(),
    )
    await StepStore(case["factory"]).enqueue(
        StepRequest(
            child.id, Handler.FEEDBACK_REVIEW, child.id + ":review", "{}", case["clock"].now()
        )
    )
    (case["lease"],) = await StepStore(case["factory"]).claim_due(
        "native-model", case["clock"].now(), 600
    )
    expires = case["clock"].now() + timedelta(seconds=policy.seconds)
    review_key = request.review.repo.identity.digest + ":2"
    async with case["factory"].begin() as session:
        session.add(
            EngineReviewCursor(
                root_review_ref=review_key,
                root_run_id=case["run"].id,
                review_json=request.review.canonical_bytes.decode(),
                last_reviewed_sha=request.base_sha,
            )
        )
        await session.flush()
        session.add(
            EngineReactiveReview(
                id=child.id,
                root_run_id=case["run"].id,
                root_review_ref=review_key,
                run_id=child.id,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                actor_json=request.actor.canonical_bytes.decode(),
                policy_json=policy.canonical_bytes.decode(),
                expires_at=expires,
            )
        )
    store = NativeReviewStore(case["factory"])
    await store.prepare_input(case["lease"], request, now=case["clock"].now())
    grant = await case["service"].prepare(
        case["lease"], policy.budget, "closing", policy.requested, policy.minimum, expires
    )
    assert await store.start_model(case["lease"], now=case["clock"].now())
    return store, grant.value.alias, policy, request


async def test_native_factory_uses_prepared_grant_exact_durable_input_and_unknown_before_http(case):
    store, alias, policy, request = await prepared(case)
    requests = []

    async def upstream(wire):
        requests.append(wire)
        document = json.loads(json.loads(wire.content)["messages"][1]["content"])
        assert document == {
            "native_review": request.model_dump(mode="json"),
            "incremental_diff": DIFF.decode(),
        }
        assert "approved_input" not in document and "tools" not in json.loads(wire.content)
        async with case["factory"]() as session:
            usage = await session.scalar(select(EngineUsage))
            assert usage.alias == alias and not usage.final and usage.usd is None
        return text_wire(answer(request))

    async with models_for(case, upstream) as models:
        reviewer = ServerNativeReviewers(models, store, case["clock"])(lambda: case["lease"], alias)
        verdict = await reviewer.review(DIFF, request)
        assert verdict.decision == "changes_requested" and verdict.candidate_sha == request.head_sha
        await store.record(case["lease"], verdict, now=case["clock"].now())
        with pytest.raises(StoreConflict, match="durable requested head"):
            await reviewer.review(DIFF, request)
    assert len(requests) == 1 and case["native"].writes == ["/key/generate"]


async def test_lost_native_model_response_stays_unknown_and_saved_refusal_cannot_recall(case):
    store, alias, policy, request = await prepared(case)
    requests = []

    def dropped(wire):
        requests.append(wire)
        raise httpx.ReadTimeout("synthetic private provider detail", request=wire)

    async with models_for(case, dropped) as models:
        factory = ServerNativeReviewers(models, store, case["clock"])
        verdict = await factory(lambda: case["lease"], alias).review(DIFF, request)
        assert verdict.decision == "inconclusive"
        assert "private provider" not in verdict.body + verdict.evidence
        await store.record(case["lease"], verdict, now=case["clock"].now())
        with pytest.raises(StoreConflict):
            await factory(lambda: case["lease"], alias).review(DIFF, request)
    async with case["factory"]() as session:
        usage = await session.scalar(select(EngineUsage))
        assert not usage.final and usage.usd is usage.calls is usage.tokens is None
    pool = await case["store"].load(case["lease"].run_id, "closing")
    assert pool.balance.held.usd == policy.requested.usd and len(requests) == 1


@pytest.mark.parametrize("tamper", ["diff", "head", "policy", "alias", "expiry"])
async def test_changed_native_authority_never_reaches_paid_model(case, tamper):
    store, alias, policy, request = await prepared(case)
    diff = DIFF
    if tamper == "diff":
        diff += b"foreign"
    elif tamper == "head":
        request = request.model_copy(update={"head_sha": "c" * 40})
    elif tamper == "policy":
        request = request.model_copy(update={"policy_digest": "c" * 64})
    elif tamper == "alias":
        alias += ":foreign"
    else:
        case["clock"].instant += timedelta(seconds=121)
    async with models_for(case, lambda wire: pytest.fail("unexpected paid request")) as models:
        reviewer = ServerNativeReviewers(models, store, case["clock"])(lambda: case["lease"], alias)
        with pytest.raises(StoreConflict):
            await reviewer.review(diff, request)


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "c" * 40},
        {"request_id": "c" * 32},
        {"diff_sha256": "c" * 64},
        {"policy_digest": "c" * 64},
        {"decision": "merge"},
        {"implementation_approved": True},
    ],
)
async def test_native_model_output_cannot_change_head_authority_or_closed_shape(case, changes):
    frozen = await frozen_budget(case)
    policy, request = policy_and_request(frozen)
    result = await NativeTextReviewer(
        policy, Model(json.dumps(answer(request, **changes))), credential_alias="closing"
    ).review(DIFF, request)
    assert result.decision == "inconclusive" and result.candidate_sha == request.head_sha


@pytest.mark.parametrize("response", ["not JSON", '{"decision":NaN}', '{"x":1,"x":2}', "x" * 32769])
async def test_unknown_native_model_output_cannot_manufacture_approval(case, response):
    policy, request = policy_and_request(await frozen_budget(case))
    result = await NativeTextReviewer(policy, Model(response), credential_alias="closing").review(
        DIFF, request
    )
    assert result.decision == "inconclusive"


async def test_native_input_bounds_refuse_without_truncation_or_model_and_cancel_propagates(case):
    policy, request = policy_and_request(await frozen_budget(case))
    for diff, limits in (
        (DIFF, ReviewLimits(diff_bytes=1)),
        (DIFF, ReviewLimits(prompt_bytes=1)),
        (b"\xff", ReviewLimits()),
    ):
        model = Model()
        result = await NativeTextReviewer(
            policy, model, credential_alias="closing", limits=limits
        ).review(diff, request)
        assert result.decision == "inconclusive" and model.calls == []
    cancelled = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await NativeTextReviewer(
            policy, Model(failure=cancelled), credential_alias="closing"
        ).review(DIFF, request)
