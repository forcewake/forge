"""Production planning/review factories bind SQL admission to scoped model HTTP."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from forge.budget.server_models import ServerModels
from forge.budget.strict import TrustedPolicy
from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import BudgetLimits, Resources
from forge.domain.run_spec import RunPolicy
from forge.domain.states import Handler
from forge.engine.model_services import ServerPlanners, ServerReviewers
from forge.engine.plan_values import PlanIntent
from forge.planning.limits import PlanningRefused
from forge.port_values import (
    Actor,
    NeighborSnapshot,
    PlanningSnapshot,
    RepoRef,
    SubjectRef,
    TaskSnapshot,
)
from forge.providers.fake import Blob, FakeProvider
from forge.store.budget_calls import ModelCallStore
from forge.store.model_inputs import ModelInputStore
from forge.store.models_budget import EngineBudgetPool, EngineGatewayKey, EngineUsage
from forge.store.models_execution import EngineCandidate, EngineStep
from forge.store.models_lane import EngineLaneSession
from forge.store.models_runs import EngineApprovedInput, EngineRunSpec
from forge.store.records import StepRequest, StoreConflict
from forge.store.steps import StepStore
from forge_protocol import CandidateBundle, LaneDispatch, RunIdentity, canonical_json, digest
from tests.test_budget_engine import NativeAdmin, case as case, prepare
from tests.test_domain_approved_input import spec
from tests.test_server_models import wire


async def frozen_budget(case):
    frozen = spec(
        limits=BudgetLimits(usd=Decimal("10"), calls=20, tokens=200000, wallclock_seconds=600),
        closing_reserve=Resources(usd=Decimal("2"), calls=2, tokens=60000),
        planning_allowance=Resources(usd=Decimal("1"), calls=3, tokens=40000),
    )
    async with case["factory"]() as session, session.begin():
        row = await session.scalar(select(EngineRunSpec))
        row.document_json, row.digest = frozen.spec_json, frozen.digest
    case["frozen"] = frozen
    return frozen


async def planning_input(case):
    frozen = await frozen_budget(case)
    policy = RunPolicy.model_validate_json(
        json.dumps({name: frozen.model_dump(mode="json")[name] for name in RunPolicy.model_fields})
    )
    repo = RepoRef(identity=frozen.plan.steps[0].citations[0].repository, full_name="owner/project")
    fake = FakeProvider(provider="github", connection_id=repo.identity.connection_id)
    base = fake.seed_repository(repo, {"a.py": Blob(b"old\n")})
    task = TaskSnapshot(
        subject=SubjectRef(repo=repo, number=1),
        title="Change",
        description="Retain source",
        version="v1",
    )
    intent = PlanIntent(
        actor=Actor(
            provider="github",
            connection_id=repo.identity.connection_id,
            native_id="owner",
            login="owner",
            is_bot=False,
        ),
        task=task,
        snapshot=PlanningSnapshot(primary=NeighborSnapshot(repo=repo, sha=base)),
        policy=policy,
    )
    async with case["factory"]() as session, session.begin():
        await session.execute(delete(EngineRunSpec))
        step = await session.get(EngineStep, case["lease"].step_id)
        step.payload_json = canonical_json(intent.model_dump(mode="json")).decode()
        step.created_at = case["clock"].now()
    return intent, fake


@asynccontextmanager
async def models_for(case, handler):
    models = ServerModels(
        "https://gateway.invalid",
        case["keys"],
        ModelCallStore(case["factory"]),
        case["gateway"],
        TrustedPolicy.parse(case["native"].policy),
        case["clock"],
        transport=httpx.MockTransport(handler),
    )
    try:
        yield models
    finally:
        await models.aclose()


def planner_reply(intent, index):
    if index == 1:
        return {
            "kind": "tools",
            "calls": [
                {
                    "tool": "read_file",
                    "repo": "primary",
                    "path": "a.py",
                    "start_line": 1,
                    "line_count": 1,
                }
            ],
        }
    return {
        "kind": "plan",
        "plan": {
            "summary": "Correct source",
            "steps": [
                {
                    "step_id": "change",
                    "title": "Change",
                    "instructions": "Retain exact source",
                    "files": ["a.py"],
                    "citations": [
                        {
                            "repository": intent.task.subject.repo.identity.model_dump(mode="json"),
                            "source_sha": intent.snapshot.primary.sha,
                            "path": "a.py",
                            "start_line": 1,
                            "end_line": 1,
                        }
                    ],
                }
            ],
        },
    }


def text_wire(value):
    return wire(
        body={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(value)},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }
    )


@pytest.mark.parametrize("exact_native_counts", [False, True])
async def test_production_planner_reserves_before_research_and_blocks_after_exact_read_plan(
    case, monkeypatch, exact_native_counts
):
    original = NativeAdmin.__call__

    def native_counts(self, request):
        result = original(self, request)
        if exact_native_counts and request.url.path == "/forge/budget/usage":
            return httpx.Response(
                200, json={**result.json(), "settled_usd": "0.00006", "calls": 2, "tokens": 28}
            )
        return result

    monkeypatch.setattr(NativeAdmin, "__call__", native_counts)
    intent, fake = await planning_input(case)
    requests = []

    async def upstream(request):
        async with case["factory"]() as session:
            rows = (
                await session.scalars(select(EngineUsage).order_by(EngineUsage.created_at))
            ).all()
            assert len(rows) == len(requests) + 1 and not rows[-1].final
            pools = (await session.scalars(select(EngineBudgetPool))).all()
            assert len(pools) == 2
        requests.append(request)
        if len(requests) == 2:
            prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
            assert prompt["observations"][0]["result"]["complete"] is True
            assert "old" in json.dumps(prompt["observations"])
        return text_wire(planner_reply(intent, len(requests)))

    async with models_for(case, upstream) as models:
        factory = ServerPlanners(
            fake, models, case["service"], ModelInputStore(case["factory"]), case["clock"]
        )
        result = await factory(lambda: case["lease"], intent.policy).plan(
            intent.task, intent.snapshot
        )
    assert result.summary == "Correct source" and len(requests) == 2
    assert case["native"].writes == ["/key/generate", "/key/block"]
    async with case["factory"]() as session:
        key = await session.scalar(select(EngineGatewayKey))
        assert key.purpose == "planning" and key.state == "purged"
        rows = (await session.scalars(select(EngineUsage))).all()
        assert len(rows) == 2 and all(row.final for row in rows)
    pool = await case["store"].load(case["run"].id, "implementation")
    assert len(pool.reservations) == 1
    if exact_native_counts:
        assert pool.balance.held == Resources()
        assert pool.balance.settled == Resources(usd=Decimal("0.00006"), calls=2, tokens=28)
    else:
        assert pool.balance.held.usd == Decimal("1")


async def test_planner_input_mismatch_refuses_before_any_grant_or_paid_request(case):
    intent, fake = await planning_input(case)
    requests = []
    async with models_for(case, lambda request: requests.append(request) or wire()) as models:
        planner = ServerPlanners(
            fake, models, case["service"], ModelInputStore(case["factory"]), case["clock"]
        )(lambda: case["lease"], intent.policy)
        with pytest.raises(StoreConflict, match="immutable admission"):
            await planner.plan(
                intent.task.model_copy(update={"description": "changed"}), intent.snapshot
            )
    assert not requests and not case["native"].writes


async def test_planner_lost_response_is_unknown_and_retry_cannot_reset_or_extend_grant(case):
    intent, fake = await planning_input(case)
    requests = []

    def dropped(request):
        requests.append(request)
        raise httpx.ReadTimeout("synthetic loss", request=request)

    async with models_for(case, dropped) as models:
        planner = ServerPlanners(
            fake, models, case["service"], ModelInputStore(case["factory"]), case["clock"]
        )(lambda: case["lease"], intent.policy)
        with pytest.raises(PlanningRefused, match="planner_model_unavailable"):
            await planner.plan(intent.task, intent.snapshot)
        case["clock"].instant += timedelta(seconds=1)
        with pytest.raises((PlanningRefused, StoreConflict)):
            await planner.plan(intent.task, intent.snapshot)
    assert len(requests) == 1 and case["native"].writes == ["/key/generate", "/key/block"]
    pool = await case["store"].load(case["run"].id, "implementation")
    assert len(pool.reservations) == 1 and pool.balance.held.usd == Decimal("1")
    async with case["factory"]() as session:
        usage = await session.scalar(select(EngineUsage))
        assert not usage.final and usage.usd is usage.calls is usage.tokens is None


async def test_planner_expired_frozen_deadline_never_mints(case):
    intent, fake = await planning_input(case)
    case["clock"].instant += timedelta(seconds=91)
    async with models_for(case, lambda request: pytest.fail("unexpected paid request")) as models:
        planner = ServerPlanners(
            fake, models, case["service"], ModelInputStore(case["factory"]), case["clock"]
        )(lambda: case["lease"], intent.policy)
        with pytest.raises((PlanningRefused, ValueError), match="expir|future"):
            await planner.plan(intent.task, intent.snapshot)
    assert not case["native"].writes


async def review_input(case):
    frozen = await frozen_budget(case)
    approved = ApprovedInput.from_spec(frozen)
    raw, sha = b"diff --git a/a.py b/a.py\n-old\n+new\n", "b" * 40
    steps = StepStore(case["factory"])
    await steps.complete_and_enqueue(
        case["lease"],
        "{}",
        (StepRequest(case["run"].id, Handler.REVIEW, "review", "{}", case["clock"].now()),),
        now=case["clock"].now(),
    )
    (case["lease"],) = await steps.claim_due("review-worker", case["clock"].now(), 600)
    wire_candidate = CandidateBundle(
        forge_run_id=case["run"].id,
        attempt=case["lease"].attempt,
        generation=case["lease"].generation,
        diff_sha256=hashlib.sha256(raw).hexdigest(),
        diff_size_bytes=len(raw),
        base_sha=frozen.base_sha,
        approved_digest=approved.approved_digest,
        driver=frozen.driver,
        model=frozen.model,
        terminal_reason="completed",
    )
    async with case["factory"]() as session, session.begin():
        session.add(
            EngineApprovedInput(
                id=uuid4().hex,
                run_id=case["run"].id,
                revision=approved.revision,
                digest=approved.approved_digest,
                spec_digest=approved.spec_digest,
                base_sha=approved.base_sha,
                document_json=approved.canonical_bytes.decode(),
            )
        )
        session.add(
            EngineCandidate(
                id=uuid4().hex,
                run_id=case["run"].id,
                attempt=case["lease"].attempt,
                generation=case["lease"].generation,
                digest=digest(wire_candidate),
                base_sha=frozen.base_sha,
                approved_digest=approved.approved_digest,
                manifest_json=wire_candidate.model_dump_json(),
                state="published",
                published_sha=sha,
            )
        )
        session.add(review_session(case, approved, frozen, wire_candidate))
    return approved, raw, sha


def review_session(case, approved, frozen, candidate):
    identity = RunIdentity(
        forge_run_id=case["run"].id,
        attempt=case["lease"].attempt,
        generation=case["lease"].generation,
    )
    nonce = "synthetic-current-review-nonce-at-least32bytes"
    dispatch = LaneDispatch(
        **identity.model_dump(),
        server_url="https://forge.example",
        provider=case["run"].provider,
        repository="owner/project",
        ref="forge/" + case["run"].id,
        base_sha=approved.base_sha,
        approved_digest=approved.approved_digest,
        driver=frozen.driver,
        model=frozen.model,
        timeout_seconds=600,
        nonce=nonce,
    )
    return EngineLaneSession(
        id=uuid4().hex,
        run_id=case["run"].id,
        attempt=identity.attempt,
        generation=identity.generation,
        cancellation_generation=case["lease"].cancellation_generation,
        nonce_hash=hashlib.sha256(("lane-oidc-nonce:" + nonce).encode()).hexdigest(),
        expires_at=case["clock"].now() + timedelta(minutes=10),
        dispatch_json=dispatch.model_dump_json(),
        brief_json=approved.to_brief(identity, frozen).model_dump_json(),
        provider_binding_json=json.dumps({"repository_id": case["run"].repository_id}),
        api_state_json=json.dumps({"candidate": candidate.model_dump(mode="json")}),
    )


async def test_review_input_store_returns_only_exact_published_diff_sha_and_consumed_approval(case):
    approved, raw, sha = await review_input(case)
    frozen = await ModelInputStore(case["factory"]).review(
        case["lease"], approved, raw, sha, now=case["clock"].now()
    )
    assert frozen == case["frozen"]


@pytest.mark.parametrize(
    "tamper", ["sha", "diff", "approval", "wire", "wire_digest", "state", "spec", "model"]
)
async def test_review_input_store_rejects_every_changed_candidate_authority_before_model(
    case, tamper
):
    approved, raw, sha = await review_input(case)
    async with case["factory"]() as session, session.begin():
        row = await session.scalar(select(EngineCandidate))
        if tamper == "sha":
            sha = "c" * 40
        elif tamper == "diff":
            raw += b"changed"
        elif tamper == "approval":
            await session.execute(delete(EngineApprovedInput))
        elif tamper == "wire":
            wire_candidate = CandidateBundle.model_validate_json(row.manifest_json).model_copy(
                update={"generation": case["lease"].generation + 1}
            )
            row.manifest_json, row.digest = wire_candidate.model_dump_json(), digest(wire_candidate)
        elif tamper == "wire_digest":
            row.digest = "0" * 64
        elif tamper == "state":
            row.state = "validated"
        elif tamper == "model":
            candidate = CandidateBundle.model_validate_json(row.manifest_json).model_copy(
                update={"model": "other-model"}
            )
            row.manifest_json, row.digest = candidate.model_dump_json(), digest(candidate)
        else:
            row = await session.scalar(select(EngineRunSpec))
            row.digest = "0" * 64
    async with models_for(case, lambda request: pytest.fail("unexpected paid request")) as models:
        reviewer = ServerReviewers(models, ModelInputStore(case["factory"]), case["clock"])(
            lambda: case["lease"], "unused"
        )
        with pytest.raises(StoreConflict):
            await reviewer.review(raw, approved, candidate_sha=sha)
    assert not case["native"].writes


async def test_production_reviewer_uses_prepared_closing_key_and_exact_frozen_bytes(case):
    approved, raw, sha = await review_input(case)
    prepared = await prepare(case, "closing", case["frozen"].closing_reserve)
    requests = []

    async def upstream(request):
        requests.append(request)
        document = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert document["candidate_diff"].encode() == raw
        assert document["approved_input"] == approved.model_dump(mode="json")
        assert document["frozen_specification"] == case["frozen"].model_dump(mode="json")
        async with case["factory"]() as session:
            row = await session.scalar(select(EngineUsage))
            assert row.alias.endswith(":closing") and not row.final
        return text_wire(
            {
                "candidate_sha": sha,
                "approved_digest": approved.approved_digest,
                "decision": "approve",
                "body": "Exact diff reviewed.",
            }
        )

    async with models_for(case, upstream) as models:
        reviewer = ServerReviewers(models, ModelInputStore(case["factory"]), case["clock"])(
            lambda: case["lease"], prepared.value.alias
        )
        verdict = await reviewer.review(raw, approved, candidate_sha=sha)
    assert verdict.decision == "approve" and verdict.candidate_sha == sha and len(requests) == 1
    assert case["native"].writes == ["/key/generate"]
    async with case["factory"]() as session:
        usage = await session.scalar(select(EngineUsage))
        assert usage.final and usage.tokens == 14


async def test_live_model_client_cannot_adopt_another_step_owner_or_epoch(case):
    prepared = await prepare(case, "planning", case["frozen"].planning_allowance)
    async with models_for(case, lambda request: pytest.fail("unexpected paid request")) as models:
        client = models.client(
            lambda: case["lease"],
            alias=prepared.value.alias,
            model=case["frozen"].model,
            purpose="planning",
        )
        case["lease"] = replace(case["lease"], epoch=case["lease"].epoch + 1)
        with pytest.raises(StoreConflict, match="lease owner or epoch"):
            await client.complete(
                model=case["frozen"].model,
                system="x",
                prompt="x",
                credential_alias=prepared.value.alias,
                max_output_tokens=10,
            )
