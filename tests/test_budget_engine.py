"""Real SQL pool/key persistence plus the pinned admin HTTP contract, without models."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import forge.store.models  # noqa: F401
from forge.budget.credentials import CredentialUnavailable, CredentialVault, KeyScope
from forge.budget.gateway import LiteLLMGateway
from forge.budget.strict import ALLOWED_ROUTES, GUARD_DIGEST, TrustedPolicy
from forge.budget.model_client import BudgetedModelClient
from forge.budget.service import BudgetService, alias_for
from forge.budget.settlement import Settlement
from forge.domain.budget import BudgetExhausted, Observation, Resources
from forge.domain.states import Handler, State
from forge.domain.verdicts import Unknown
from forge.engine.effects import Effects
from forge.store.base import Base
from forge.port_values import Applied, ModelReply
from forge.store.budget_calls import ModelCallStore
from forge.store.budgets import BudgetStore
from forge.store.effects import EffectStore
from forge.store.gateway_keys import GatewayKeyStore
from forge.store.models_budget import EngineBudgetAmendment, EngineGatewayKey, EngineUsage
from forge.store.records import RunRequest, StepRequest, StoreConflict
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from forge_protocol import RunIdentity
from tests.fakes.ports import FixedClock, ScriptedPorts
from tests.test_domain_approved_input import spec


class NativeAdmin:
    def __init__(self, clock):
        self.clock, self.keys, self.writes = clock, {}, []
        self.drop_mint, self.unavailable, self.tamper = False, False, None
        self.unavailable_write = False
        self.policy = json.dumps(
            {
                "schema": "forge.strict-budget/1",
                "litellm_version": "1.106.2",
                "models": {
                    "test-model": {
                        "native_model": "anthropic/approved",
                        "input_rate": "0.000002",
                        "output_rate": "0.000005",
                        "max_input_tokens": 200000,
                        "max_output_tokens": 64000,
                    }
                },
            }
        ).encode()
        self.artifact = "a" * 64

    def __call__(self, request):
        if self.unavailable or (self.unavailable_write and request.method == "POST"):
            raise httpx.ConnectError("synthetic unreachable", request=request)
        path = request.url.path
        if path == "/forge/budget/readiness":
            return httpx.Response(
                200,
                json={
                    "schema": "forge.strict-budget-readiness/1",
                    "nonce": request.url.params["nonce"],
                    "guard_digest": GUARD_DIGEST,
                    "policy_digest": TrustedPolicy.parse(self.policy).digest,
                    "artifact_digest": self.artifact,
                    "litellm_version": "1.106.2",
                    "models": ["test-model"],
                    "routes": list(ALLOWED_ROUTES),
                    "durable_unknown_holds": True,
                },
            )
        if path == "/forge/budget/usage":
            row = self.keys[request.url.params["token"]]
            assert row["blocked"] is True
            return httpx.Response(
                200,
                json={
                    "schema": "forge.strict-budget-usage/1",
                    "token_digest": row["token"],
                    "policy_digest": TrustedPolicy.parse(self.policy).digest,
                    "settled_usd": str(row["spend"]),
                    "held_usd": "0",
                    "complete": True,
                    "calls": None,
                    "tokens": None,
                },
            )
        if path == "/key/list":
            assert request.url.params["return_full_object"] == "true"
            assert request.url.params["size"] == "2"
            alias = request.url.params["key_alias"]
            rows = [dict(row) for row in self.keys.values() if row["key_alias"] == alias]
            if self.tamper and rows:
                rows[0].update(self.tamper)
            return httpx.Response(200, json={"keys": rows, "total_count": len(rows)})
        data = json.loads(request.content)
        self.writes.append(path)
        if path == "/key/generate":
            assert data["key_type"] == "default" and data["budget_duration"] is None
            assert data["blocked"] is False
            token = hashlib.sha256(data["key"].encode()).hexdigest()
            assert token not in self.keys
            self.keys[token] = {
                **{
                    key: value
                    for key, value in data.items()
                    if key not in ("key", "key_type", "duration")
                },
                "token": token,
                "blocked": False,
                "spend": 0,
                "allowed_routes": list(ALLOWED_ROUTES),
                "expires": (
                    self.clock.now() + timedelta(seconds=int(data["duration"][:-1]))
                ).isoformat(),
            }
            if self.drop_mint:
                self.drop_mint = False
                raise httpx.ReadTimeout("synthetic dropped response", request=request)
            return httpx.Response(200, json={"key": data["key"]})
        row = self.keys[data["key"]]
        if path == "/key/update":
            row["max_budget"], row["metadata"] = data["max_budget"], data["metadata"]
        elif path == "/key/block":
            row["blocked"] = True
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=row)


@pytest.fixture
async def case(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run = await RunStore(factory).admit(
        RunRequest(uuid4().hex, "github", "app-1", "42", "issue", "1")
    )
    clock = FixedClock(datetime.now(timezone.utc))
    await StepStore(factory).enqueue(StepRequest(run.id, Handler.PLAN, "plan", "{}", clock.now()))
    (lease,) = await StepStore(factory).claim_due("worker", clock.now(), 600)
    frozen = spec()
    await RunStore(factory).put_spec_once(
        run.id, document_json=frozen.spec_json, digest=frozen.digest
    )
    keys = GatewayKeyStore(factory, CredentialVault(b"synthetic-server-secret-32-bytes!!"))
    store = BudgetStore(factory)
    native = NativeAdmin(clock)
    gateway = LiteLLMGateway(
        "https://gateway.invalid",
        SecretStr("synthetic-admin"),
        keys,
        clock,
        transport=httpx.MockTransport(native),
        strict_policy=native.policy,
        strict_artifact_digest=native.artifact,
    )
    unused = ScriptedPorts(())
    effects = Effects(EffectStore(factory), unused, unused, unused, unused, unused, gateway, clock)
    settlement = Settlement(gateway, store, window=0)

    class TestAdmission:
        async def readiness(self, model):
            return None

    admission = TestAdmission()
    service = BudgetService(store, keys, effects, settlement, clock, admission)
    context = dict(
        factory=factory,
        run=run,
        lease=lease,
        clock=clock,
        frozen=frozen,
        keys=keys,
        store=store,
        native=native,
        gateway=gateway,
        effects=effects,
        service=service,
        admission=admission,
        expires=clock.now() + timedelta(seconds=300),
    )
    try:
        yield context
    finally:
        await gateway.aclose()
        await engine.dispose()


async def prepare(case, purpose="implementation", requested=None):
    return await case["service"].prepare(
        case["lease"],
        case["frozen"],
        purpose,
        requested or Resources(usd=Decimal("2"), calls=4, tokens=2000),
        Resources(usd=Decimal("0.01"), calls=1, tokens=1),
        case["expires"],
    )


def identity(case):
    return RunIdentity(
        forge_run_id=case["run"].id,
        attempt=case["lease"].attempt,
        generation=case["lease"].generation,
    )


async def test_mint_preseals_before_http_and_lost_response_probes_same_material(case):
    case["native"].drop_mint = True
    outcome = await prepare(case)
    assert isinstance(outcome, Unknown)
    alias = alias_for(identity(case), "implementation")
    record = await case["keys"].get(alias)
    secret = case["keys"].reveal(record)
    assert secret.encode() not in record.ciphertext
    effect = await EffectStore(case["factory"]).get(f"{alias}:mint")
    assert secret not in effect.payload_json + effect.outcome_json
    assert (await case["effects"].recover(effect)).state == "adopted"
    adopted = await prepare(case)
    assert isinstance(adopted, Applied) and adopted.value.enforced_axes == ("usd",)
    assert case["native"].writes == ["/key/generate"]
    first = await case["keys"].grant(
        identity(case), "https://gateway.invalid", now=case["clock"].now()
    )
    second = await case["keys"].grant(
        identity(case), "https://gateway.invalid", now=case["clock"].now()
    )
    assert first.key == second.key == secret
    assert secret not in repr(first) + repr(record) + adopted.model_dump_json()


async def test_crash_after_remote_success_adopts_without_second_mint(case):
    def crash(point, record):
        if point == "after_call":
            raise SystemExit("synthetic crash")

    case["effects"].hook = crash
    with pytest.raises(SystemExit):
        await prepare(case)
    case["effects"].hook = None
    assert isinstance(await prepare(case), Applied)
    assert case["native"].writes.count("/key/generate") == 1


@pytest.mark.parametrize(
    "tamper",
    [
        {"token": "f" * 64},
        {"models": []},
        {"max_budget": None},
        {"budget_duration": "1d"},
        {"team_id": "ambient"},
        {"metadata": {}},
        {"allowed_routes": []},
        {"max_parallel_requests": 9},
    ],
)
async def test_scope_drift_never_activates_or_delivers_key(case, tamper):
    case["native"].tamper = tamper
    assert isinstance(await prepare(case), Unknown)
    with pytest.raises(CredentialUnavailable):
        await case["keys"].grant(identity(case), "https://gateway.invalid", now=case["clock"].now())
    assert case["native"].writes == ["/key/generate"]


async def test_unreachable_gateway_keeps_hold_without_remint(case):
    case["native"].unavailable_write = True
    assert isinstance(await prepare(case), Unknown)
    assert isinstance(await prepare(case), Unknown)
    pool = await case["store"].load(case["run"].id, "implementation")
    assert pool.balance.held.usd == Decimal("2")
    assert case["native"].writes == []


async def test_new_generation_blocks_old_before_mint_and_uses_remaining_pool(case):
    assert isinstance(await prepare(case), Applied)
    old = identity(case)
    blocked = await RunStore(case["factory"]).transition(
        case["run"].id, expected_version=1, target=State.BLOCKED, failed_handler=Handler.PLAN
    )
    resumed = await RunStore(case["factory"]).resume(
        blocked.id, expected_version=blocked.version, handler=Handler.PLAN
    )
    await StepStore(case["factory"]).enqueue(
        StepRequest(resumed.id, Handler.PLAN, "retry", "{}", case["clock"].now())
    )
    (lease,) = await StepStore(case["factory"]).claim_due("retry-worker", case["clock"].now(), 600)
    case["lease"] = lease
    result = await prepare(case, requested=Resources(usd=Decimal("8"), calls=20, tokens=10000))
    assert isinstance(result, Applied)
    assert result.value.allowance == Resources(usd=Decimal("6"), calls=14, tokens=7000)
    assert case["native"].writes == ["/key/generate", "/key/block", "/key/generate"]
    assert (await case["keys"].get(alias_for(old, "implementation"))).ciphertext is None
    with pytest.raises(CredentialUnavailable):
        await case["keys"].grant(old, "https://gateway.invalid", now=case["clock"].now())


async def test_settlement_native_partial_axes_stay_fully_held_and_purge(case):
    await prepare(case)
    assert await case["service"].finish(case["lease"], "implementation") == "accrued"
    alias = alias_for(identity(case), "implementation")
    record = await case["keys"].get(alias)
    assert record.state == "purged" and record.ciphertext is None
    pool = await case["store"].load(case["run"].id, "implementation")
    assert pool.reservations[0].observed == Observation(usd=Decimal(0), calls=None, tokens=None)
    assert pool.balance.held.usd == Decimal("2")
    with pytest.raises(CredentialUnavailable):
        await case["keys"].grant(identity(case), "https://gateway.invalid", now=case["clock"].now())


@pytest.mark.parametrize(
    "reads,expected",
    [
        (("0.2", "0.2"), "settled"),
        (("0.2", "0.3"), "accrued"),
        (("0.3", "0.2"), "accrued"),
    ],
)
async def test_settlement_requires_two_equal_complete_nonregressing_reads(case, reads, expected):
    await prepare(case)
    values = iter(reads)

    async def observed(alias):
        return Observation(usd=Decimal(next(values)), calls=1, tokens=100)

    case["gateway"].read_spend = observed
    assert await case["service"].finish(case["lease"], "implementation") == expected
    pool = await case["store"].load(case["run"].id, "implementation")
    assert pool.reservations[0].status == expected
    if expected != "settled":
        assert pool.balance.held.usd == Decimal("2")


async def test_amendment_durable_before_gateway_and_applied_once(case):
    await prepare(case)
    amount = Resources(usd=Decimal("1"), calls=1, tokens=100)
    first = await case["service"].amend(
        case["lease"], "implementation", "cmd-1", amount, "owner-9", "authorized repair"
    )
    assert isinstance(first, Applied)
    again = await case["service"].amend(
        case["lease"], "implementation", "cmd-1", amount, "owner-9", "authorized repair"
    )
    assert again == first
    assert case["native"].writes == ["/key/generate", "/key/update"]
    pool = await case["store"].load(case["run"].id, "implementation")
    assert len(pool.amendments) == 1 and pool.reservations[0].allowance.usd == Decimal("3")
    async with case["factory"]() as session:
        row = await session.scalar(select(EngineBudgetAmendment))
        assert row.state == "applied" and row.actor == "owner-9" and row.target_json
    with pytest.raises(StoreConflict, match="changed"):
        await case["service"].amend(
            case["lease"], "implementation", "cmd-1", amount, "other", "changed"
        )


async def test_copilot_unmetered_requires_canonical_policy_and_does_not_reserve(case):
    with pytest.raises(StoreConflict):
        await case["service"].copilot(case["lease"], case["frozen"])
    frozen = spec(driver="copilot_cli", metering="copilot-scoped-unmetered")
    async with case["factory"].begin() as session:
        from forge.store.models_runs import EngineRunSpec

        row = await session.scalar(select(EngineRunSpec))
        row.document_json, row.digest = frozen.spec_json, frozen.digest
    await case["service"].copilot(case["lease"], frozen)
    await case["service"].copilot(case["lease"], frozen)
    assert case["native"].writes == []
    async with case["factory"]() as session:
        rows = (await session.scalars(select(EngineUsage))).all()
        assert len(rows) == 1 and rows[0].completeness == "unmetered"
        assert rows[0].usd is rows[0].calls is rows[0].tokens is None


async def test_server_planning_admits_each_call_before_model_and_does_not_reset(case):
    result = await prepare(case, "planning", Resources(usd=Decimal("1"), calls=3, tokens=2000))
    assert isinstance(result, Applied)

    class Model:
        calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            async with case["factory"]() as session:
                rows = (await session.scalars(select(EngineUsage))).all()
                assert len(rows) == self.calls and rows[-1].completeness == "unknown"
            return ModelReply(
                text="answer",
                usage=Observation(usd=Decimal("0.1"), calls=1, tokens=100),
                evidence="synthetic",
            )

    inner = Model()
    facade = BudgetedModelClient(
        inner,
        ModelCallStore(case["factory"]),
        lambda: case["lease"],
        case["clock"],
        case["admission"],
    )
    inputs = dict(
        model=case["frozen"].model,
        system="system",
        prompt="prompt",
        credential_alias=result.value.alias,
        max_output_tokens=100,
    )
    for _ in range(3):
        await facade.complete(**inputs)
    with pytest.raises(BudgetExhausted):
        await facade.complete(**inputs)
    assert inner.calls == 3
    with pytest.raises(StoreConflict):
        await facade.complete(**{**inputs, "model": "different"})


async def test_unknown_model_call_fully_holds_and_prevents_oversized_next_context(case):
    result = await prepare(case, "planning", Resources(usd=Decimal("1"), calls=3, tokens=2000))

    class Lost:
        calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            raise TimeoutError("synthetic lost response")

    inner = Lost()
    facade = BudgetedModelClient(
        inner,
        ModelCallStore(case["factory"]),
        lambda: case["lease"],
        case["clock"],
        case["admission"],
    )
    inputs = dict(
        model=case["frozen"].model,
        system="",
        prompt="x" * 900,
        credential_alias=result.value.alias,
        max_output_tokens=200,
    )
    with pytest.raises(TimeoutError):
        await facade.complete(**inputs)
    with pytest.raises(BudgetExhausted):
        await facade.complete(**inputs)
    assert inner.calls == 1


def test_vault_binds_every_identity_purpose_model_alias_expiry_and_authenticates_bytes():
    vault = CredentialVault(b"synthetic-server-secret-32-bytes!!")
    scope = KeyScope(
        identity=RunIdentity(forge_run_id="a" * 32, attempt=1, generation=1),
        purpose="planning",
        alias="forge:one",
        model="model",
        expires_at=datetime.now(timezone.utc),
    )
    secret = vault.generate()
    sealed = vault.seal(scope, secret)
    assert vault.open(scope, sealed) == secret
    changes = [
        {"purpose": "closing"},
        {"alias": "other"},
        {"model": "other"},
        {"expires_at": scope.expires_at + timedelta(seconds=1)},
        {"identity": RunIdentity(forge_run_id="b" * 32, attempt=1, generation=1)},
        {"identity": RunIdentity(forge_run_id="a" * 32, attempt=2, generation=1)},
        {"identity": RunIdentity(forge_run_id="a" * 32, attempt=1, generation=2)},
    ]
    for change in changes:
        with pytest.raises(CredentialUnavailable):
            vault.open(KeyScope(**{**scope.model_dump(), **change}), sealed)
    for corrupt in (sealed[:-1], sealed[:15] + b"x" + sealed[16:], b"\x02" + sealed[1:]):
        with pytest.raises(CredentialUnavailable):
            vault.open(scope, corrupt)


async def test_real_adapter_refuses_strict_budget_before_any_pool_key_or_model_request(case):
    case["gateway"].artifact_digest = None
    case["service"].admission = case["gateway"]
    result = await prepare(case)
    assert result.kind == "refused" and result.reason == "strict_budget_enforcement_unavailable"
    assert case["native"].writes == []
    async with case["factory"]() as session:
        assert await session.scalar(select(EngineGatewayKey)) is None
    inner = ScriptedPorts(())
    facade = BudgetedModelClient(
        inner,
        ModelCallStore(case["factory"]),
        lambda: case["lease"],
        case["clock"],
        case["gateway"],
    )
    with pytest.raises(BudgetExhausted, match="strict_budget_enforcement_unavailable"):
        await facade.complete(
            model="model", system="", prompt="", credential_alias="unknown", max_output_tokens=1
        )
    assert inner.calls == []


async def test_planning_reservation_clips_without_resetting_or_changing_retry_input(case):
    requested = Resources(usd=Decimal("2"), calls=6, tokens=4000)
    result = await prepare(case, "planning", requested)
    assert result.value.allowance == case["frozen"].planning_allowance
    assert (await prepare(case, "planning", requested)).value == result.value
    assert case["native"].writes == ["/key/generate"]


async def test_reentry_after_amendment_returns_current_grant_without_changing_mint(case):
    await prepare(case)
    result = await case["service"].amend(
        case["lease"], "implementation", "amend", Resources(usd=Decimal("1")), "owner", "repair"
    )
    again = await prepare(case)
    assert again.value == result.value
    assert case["native"].writes == ["/key/generate", "/key/update"]


async def test_planning_uses_persisted_admission_policy_before_plan_exists(case):
    from sqlalchemy import delete
    from forge.domain.run_spec import RunPolicy
    from forge.store.models_runs import EngineRunSpec
    from forge.store.models_execution import EngineStep

    policy = {name: case["frozen"].model_dump(mode="json")[name] for name in RunPolicy.model_fields}
    async with case["factory"].begin() as session:
        await session.execute(delete(EngineRunSpec))
        row = await session.scalar(select(EngineStep))
        row.payload_json = json.dumps({"policy": policy}, sort_keys=True, separators=(",", ":"))
    result = await prepare(case, "planning", case["frozen"].planning_allowance)
    assert isinstance(result, Applied)
    altered = case["frozen"].model_copy(update={"model": "unbound"})
    with pytest.raises(StoreConflict, match="persisted policy"):
        await case["store"].initialize(case["lease"], altered, now=case["clock"].now())


def test_metering_is_canonical_and_only_copilot_can_be_explicitly_unmetered():
    from pydantic import ValidationError
    from forge.domain.approved_input import ApprovedInput

    frozen = spec()
    assert '"metering":"metered"' in frozen.spec_json
    copilot = spec(driver="copilot_cli", metering="copilot-scoped-unmetered")
    assert copilot.digest != frozen.digest
    with pytest.raises(ValidationError):
        spec(driver="copilot_cli")
    with pytest.raises(ValidationError):
        spec(metering="copilot-scoped-unmetered")
    brief = ApprovedInput.from_spec(copilot).to_brief(
        RunIdentity(forge_run_id="run", attempt=1, generation=1), copilot
    )
    assert brief.frozen_spec.metering == "copilot-scoped-unmetered"


async def test_concurrent_server_calls_cannot_both_spend_one_context_hold(case):
    import asyncio

    result = await prepare(case, "planning", case["frozen"].planning_allowance)
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting:
        calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            entered.set()
            await release.wait()
            return ModelReply(
                text="done",
                usage=Observation(usd=Decimal("0.1"), calls=1, tokens=100),
                evidence="synthetic",
            )

    inner = Waiting()
    facade = BudgetedModelClient(
        inner,
        ModelCallStore(case["factory"]),
        lambda: case["lease"],
        case["clock"],
        case["admission"],
    )
    inputs = dict(
        model=case["frozen"].model,
        system="",
        prompt="x" * 1000,
        credential_alias=result.value.alias,
        max_output_tokens=100,
    )
    first = asyncio.create_task(facade.complete(**inputs))
    await entered.wait()
    try:
        with pytest.raises(BudgetExhausted):
            await facade.complete(**inputs)
        assert inner.calls == 1
    finally:
        release.set()
        await first


async def test_budget_scope_survives_refused_initialization_and_destructive_downgrade(case):
    from forge.store.schema import initialize, SchemaMismatch
    from forge.store.schema import baseline
    from forge.store.schema.migration import script

    assert isinstance(await prepare(case), Applied)
    async with case["factory"]() as session:
        row = await session.scalar(select(EngineGatewayKey))
        original = {
            column.name: getattr(row, column.name) for column in EngineGatewayKey.__table__.columns
        }
    with pytest.raises(SchemaMismatch, match="empty database"):
        await initialize(case["factory"].kw["bind"])
    with pytest.raises(RuntimeError, match="destructive downgrade"):
        script().get_revision(baseline.REVISION).module.downgrade()
    async with case["factory"]() as session:
        row = await session.scalar(select(EngineGatewayKey))
        assert {
            column.name: getattr(row, column.name) for column in EngineGatewayKey.__table__.columns
        } == original
        assert row.reference_json and row.ciphertext and row.aad_digest
        assert row.purpose == "implementation"


async def test_applied_gateway_receipt_without_active_durable_material_is_unknown(case):
    from forge.port_values import GatewayKeyRef

    async def unsealed(lease, key, identity, alias, model, allowance, expires_at):
        return Applied(
            value=GatewayKeyRef(
                alias=alias, model=model, allowance=allowance, expires_at=expires_at, blocked=False
            ),
            evidence="synthetic",
        )

    case["effects"].gw_mint = unsealed
    outcome = await prepare(case)
    assert isinstance(outcome, Unknown)
    assert "durable_gateway_grant_unavailable" in outcome.evidence
    with pytest.raises(CredentialUnavailable):
        await case["keys"].grant(identity(case), "https://gateway.invalid", now=case["clock"].now())


async def test_applied_gateway_receipt_cannot_change_durable_grant_model(case):
    result = await prepare(case)

    async def foreign(*args):
        return Applied(
            value=result.value.model_copy(update={"model": "unapproved"}), evidence="synthetic"
        )

    case["effects"].gw_mint = foreign
    outcome = await prepare(case)
    assert isinstance(outcome, Unknown)
    assert "durable_gateway_grant_unavailable" in outcome.evidence


@pytest.mark.parametrize("axis", ["requested", "minimum"])
async def test_count_only_allowance_never_mints_an_unlimited_native_key(case, axis):
    requested = Resources(usd=Decimal("1"), calls=3, tokens=100)
    minimum = Resources(usd=Decimal("0.01"), calls=1, tokens=1)
    if axis == "requested":
        requested = Resources(calls=3, tokens=100)
    else:
        minimum = Resources(calls=1, tokens=1)
    result = await case["service"].prepare(
        case["lease"], case["frozen"], "implementation", requested, minimum, case["expires"]
    )
    assert result.kind == "refused" and result.reason == "positive_metered_usd_required"
    assert case["native"].writes == []
