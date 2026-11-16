"""Crash recovery uses the actual SQL journal and never repeats an uncertain write."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.domain.states import Handler
from forge.domain.verdicts import Unknown
from forge.engine.effects import Effects
from forge.store.base import Base
from forge.port_values import Applied, CommentRef, DefinitelyAbsent, Found, Inconclusive
from forge.store.effects import EffectStore
from forge.store.models_execution import EngineStep
from forge.store.records import RunRequest, StepRequest, StaleLease, StoreConflict
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests.fakes.ports import FixedClock, ScriptedPorts
from tests.test_ports import REPO, SUBJECT


@pytest.fixture
async def case(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'effects.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run = await RunStore(factory).admit(
        RunRequest(uuid4().hex, "github", "install-1", "42", "issue", "1")
    )
    now = datetime.now(timezone.utc)
    await StepStore(factory).enqueue(StepRequest(run.id, Handler.PLAN, "plan", "{}", now))
    (lease,) = await StepStore(factory).claim_due("first", now, 60)
    try:
        yield factory, run, lease, FixedClock(now)
    finally:
        await engine.dispose()


def effects(factory, provider, clock, hook=None):
    return Effects(
        EffectStore(factory),
        provider,
        provider,
        provider,
        provider,
        provider,
        provider,
        clock,
        hook=hook,
    )


def applied():
    return Applied[CommentRef](
        value=CommentRef(subject=SUBJECT, native_id="9"), evidence='{"native_id":9}'
    )


def killed_at(point):
    def hook(actual, record):
        if actual == point:
            raise SystemExit(point)

    return hook


async def reclaim(factory, lease, clock):
    clock.instant = lease.expires_at + timedelta(seconds=1)
    async with factory.begin() as session:
        await session.execute(
            update(EngineStep)
            .where(EngineStep.id == lease.step_id)
            .values(lease_expires_at=clock.instant - timedelta(seconds=1))
        )
    (renewed,) = await StepStore(factory).claim_due("second", clock.now(), 60)
    return renewed


@pytest.mark.parametrize("point", ["after_t1", "after_t2", "after_call", "after_t3"])
async def test_crash_boundaries_recover_without_duplicate_write(case, point):
    factory, _, lease, clock = case
    provider = ScriptedPorts((("post_comment", applied()),))
    if point == "after_t2":
        provider.script.appendleft(
            ("probe_comment", DefinitelyAbsent(evidence="no call was issued"))
        )
    with pytest.raises(SystemExit):
        await effects(factory, provider, clock, killed_at(point)).post_comment(
            lease, "plan-note", SUBJECT, "plan", "marker"
        )
    row = await EffectStore(factory).get("plan-note")
    assert row is not None
    if point == "after_t2":
        lease = await reclaim(factory, lease, clock)
    if point == "after_call":
        provider.script.append(("probe_comment", Found(value=applied(), evidence="marker found")))
    result = await effects(factory, provider, clock).post_comment(
        lease, "plan-note", SUBJECT, "plan", "marker"
    )
    assert result == applied()
    assert sum(name == "post_comment" for name, _ in provider.calls) == 1
    assert not provider.script
    assert (await EffectStore(factory).get("plan-note")).state in ("committed", "adopted")


async def test_intent_is_committed_before_call_and_no_database_transaction_spans_network(case):
    factory, _, lease, clock = case

    class Observing(ScriptedPorts):
        async def post_comment(self, subject, body, marker):
            row = await EffectStore(factory).get("note")
            assert row is not None and row.state == "dispatched"
            await RunStore(factory).admit(
                RunRequest(uuid4().hex, "github", "install-1", "42", "issue", "2")
            )
            return await super().post_comment(subject, body, marker)

    provider = Observing((("post_comment", applied()),))
    engine = effects(factory, provider, clock)
    assert await engine.post_comment(lease, "note", SUBJECT, "plan", "marker") == applied()
    assert await engine.post_comment(lease, "note", SUBJECT, "plan", "marker") == applied()
    assert len(provider.calls) == 1
    with pytest.raises(StoreConflict, match="frozen inputs"):
        await engine.post_comment(lease, "note", SUBJECT, "changed", "marker")
    assert len(provider.calls) == 1


async def test_unknown_requires_explicit_reconcile_and_preserves_native_outcome(case):
    factory, _, lease, clock = case
    provider = ScriptedPorts(
        (
            ("post_comment", Unknown(evidence="lost response")),
            ("probe_comment", Inconclusive(reason="settling", evidence="read incomplete")),
            ("probe_comment", Found(value=applied(), evidence="marker found")),
        )
    )
    engine = effects(factory, provider, clock)
    assert isinstance(await engine.post_comment(lease, "note", SUBJECT, "plan", "marker"), Unknown)
    assert isinstance(await engine.post_comment(lease, "note", SUBJECT, "plan", "marker"), Unknown)
    assert len(provider.calls) == 2
    row = await EffectStore(factory).get("note")
    row = await engine.recover(row)
    assert row.state == "adopted"
    assert await engine.post_comment(lease, "note", SUBJECT, "plan", "marker") == applied()
    assert sum(name == "post_comment" for name, _ in provider.calls) == 1


async def test_early_negative_probe_cannot_redispatch_inflight_call(case):
    factory, _, lease, clock = case
    provider = ScriptedPorts((("probe_comment", DefinitelyAbsent(evidence="not visible yet")),))
    engine = effects(factory, provider, clock)
    with pytest.raises(SystemExit):
        await effects(factory, provider, clock, killed_at("after_t2")).post_comment(
            lease, "note", SUBJECT, "plan", "marker"
        )
    assert isinstance(await engine.post_comment(lease, "note", SUBJECT, "plan", "marker"), Unknown)
    assert [name for name, _ in provider.calls] == ["probe_comment"]
    assert (await EffectStore(factory).get("note")).state == "unknown"


async def test_absent_comment_after_the_dispatch_window_is_posted_again(case):
    factory, _, lease, clock = case
    provider = ScriptedPorts(
        (
            ("probe_comment", DefinitelyAbsent(evidence="complete scan found no marker")),
            ("post_comment", applied()),
        )
    )
    engine = effects(factory, provider, clock)
    with pytest.raises(SystemExit):
        await effects(factory, provider, clock, killed_at("after_t2")).post_comment(
            lease, "note", SUBJECT, "plan", "marker"
        )
    lease = await reclaim(factory, lease, clock)
    result = await engine.post_comment(lease, "note", SUBJECT, "plan", "marker")
    assert result == applied()
    assert [name for name, _ in provider.calls] == ["probe_comment", "post_comment"]


async def test_lost_fence_after_intent_prevents_any_write(case):
    factory, run, lease, clock = case
    provider = ScriptedPorts()
    with pytest.raises(SystemExit):
        await effects(factory, provider, clock, killed_at("after_t1")).post_comment(
            lease, "note", SUBJECT, "plan", "marker"
        )
    await RunStore(factory).request_cancel(run.id, expected_version=run.version)
    with pytest.raises(StaleLease):
        await effects(factory, provider, clock).post_comment(
            lease, "note", SUBJECT, "plan", "marker"
        )
    assert not provider.calls


async def test_late_observation_is_recorded_after_cancellation(case):
    factory, run, lease, clock = case

    class Cancelling(ScriptedPorts):
        async def post_comment(self, subject, body, marker):
            await RunStore(factory).request_cancel(run.id, expected_version=run.version)
            return await super().post_comment(subject, body, marker)

    provider = Cancelling((("post_comment", applied()),))
    assert (
        await effects(factory, provider, clock).post_comment(
            lease, "note", SUBJECT, "plan", "marker"
        )
        == applied()
    )
    assert (await EffectStore(factory).get("note")).state == "committed"


def operation(kind, run, lease):
    from forge_protocol import LaneDispatch, RunIdentity
    from forge.domain.budget import Resources
    from forge.domain.candidate import CandidateBundle, CreateFile, validate_candidate_bundle
    from forge.domain.verdicts import Committed
    from forge.port_values import (
        BranchRef,
        FeedbackNote,
        GatewayKeyRef,
        LabelState,
        LaneHandle,
        ReviewBody,
        ReviewRef,
    )
    from tests.test_ports import SHA

    review = ReviewRef(repo=REPO, number=9)
    comment = CommentRef(subject=review, native_id="9")
    expiry = lease.expires_at
    key = kind
    common = Applied(value=comment, evidence="native")
    if kind == "commit":
        candidate = validate_candidate_bundle(
            CandidateBundle(
                attempt_base_oid=SHA, driver_exit="success", entries=(CreateFile("x.py", "ok\n"),)
            ),
            base_contents={},
            branch="forge/run",
            commit_message="change",
        )
        return (
            key,
            "commit",
            "probe_commit",
            Committed(sha="b" * 40, evidence="native"),
            lambda e: e.commit(lease, key, REPO, candidate, SHA, "marker"),
        )
    if kind == "ensure_branch":
        result = Applied(value=BranchRef(repo=REPO, branch="forge/run", sha=SHA), evidence="native")
        return (
            key,
            kind,
            "probe_branch",
            result,
            lambda e: e.ensure_branch(lease, key, REPO, "forge/run", SHA),
        )
    if kind == "ensure_draft":
        result = Applied(value=review, evidence="native")
        return (
            f"{run.id}:ensure_draft",
            kind,
            "probe_draft",
            result,
            lambda e: e.ensure_draft(lease, run.id, REPO, "forge/run", "title", "body"),
        )
    if kind == "post_comment":
        return (
            key,
            kind,
            "probe_comment",
            common,
            lambda e: e.post_comment(lease, key, SUBJECT, "body", "marker"),
        )
    if kind == "update_comment":
        return (
            key,
            kind,
            "probe_comment_update",
            common,
            lambda e: e.update_comment(lease, key, comment, "body"),
        )
    if kind == "set_label":
        result = Applied(
            value=LabelState(subject=SUBJECT, label="forge", present=False), evidence="native"
        )
        return (
            key,
            kind,
            "probe_label",
            result,
            lambda e: e.set_label(lease, key, SUBJECT, "forge", False),
        )
    if kind == "post_review":
        body = ReviewBody(candidate_sha=SHA, body="review", decision="approve")
        return (
            key,
            kind,
            "probe_review",
            common,
            lambda e: e.post_review(lease, key, review, body, "marker"),
        )
    if kind == "reply":
        from forge.port_values import Actor

        note = FeedbackNote(
            ref=comment,
            author=Actor(
                provider="github",
                connection_id="install-1",
                native_id="4",
                login="owner",
                is_bot=False,
            ),
            body="fix",
            cursor="1",
            created_at=expiry,
        )
        return (
            key,
            kind,
            "probe_reply",
            common,
            lambda e: e.reply(lease, key, note, "reply", "marker"),
        )
    if kind == "dispatch_lane":
        dispatch = LaneDispatch(
            forge_run_id=run.id,
            attempt=1,
            generation=1,
            server_url="https://forge.example",
            provider="github",
            repository=REPO.full_name,
            ref="forge/run",
            base_sha=SHA,
            approved_digest="a" * 64,
            driver="claude_code",
            model="model",
            timeout_seconds=60,
            nonce="single-use-nonce",
        )
        result = Applied(
            value=LaneHandle(repo=REPO, native_id="1", attempt=1, correlation_id=dispatch.nonce),
            evidence="native",
        )
        return (
            key,
            kind,
            "probe_dispatch",
            result,
            lambda e: e.dispatch_lane(lease, key, REPO, dispatch),
        )
    result = Applied(
        value=GatewayKeyRef(
            alias="alias",
            model="model",
            allowance=Resources(calls=1),
            expires_at=expiry,
            blocked=kind == "gw_block",
        ),
        evidence="native",
    )
    if kind == "gw_mint":
        identity = RunIdentity(forge_run_id=run.id, attempt=1, generation=1)
        return (
            key,
            "mint",
            "probe_mint",
            result,
            lambda e: e.gw_mint(lease, key, identity, "alias", "model", Resources(calls=1), expiry),
        )
    if kind == "gw_update":
        return (
            key,
            "update",
            "probe_update",
            result,
            lambda e: e.gw_update(lease, key, "alias", Resources(calls=1)),
        )
    assert kind == "gw_block"
    return key, "block", "probe_block", result, lambda e: e.gw_block(lease, key, "alias")


@pytest.mark.parametrize(
    "kind",
    [
        "commit",
        "ensure_branch",
        "ensure_draft",
        "post_comment",
        "update_comment",
        "set_label",
        "post_review",
        "reply",
        "dispatch_lane",
        "gw_mint",
        "gw_update",
        "gw_block",
    ],
)
async def test_every_write_kind_recovers_from_only_the_persisted_intent(case, kind):
    factory, run, lease, clock = case
    key, method, probe, result, call = operation(kind, run, lease)
    provider = ScriptedPorts(((method, result),))
    with pytest.raises(SystemExit):
        await call(effects(factory, provider, clock, killed_at("after_call")))
    row = await EffectStore(factory).get(key)
    assert row is not None and row.state == "dispatched"
    fresh = ScriptedPorts(((probe, Found(value=result, evidence="native marker")),))
    restored = await effects(factory, fresh, clock).recover(row)
    assert restored.state == "adopted"
    assert await call(effects(factory, fresh, clock)) == result
    assert provider.calls[0][0] == method and len(provider.calls) == len(fresh.calls) == 1
