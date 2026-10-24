"""Complete native recovery reads share one bound and preserve unknown authority."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from forge.domain.verdicts import Unknown
from forge.engine.effects import Effects
from forge.store.effects import EffectStore
from tests import gitlab_engine_fixture as gl
from tests import test_github_rest as gh
from tests.fakes.ports import FixedClock
from tests.test_engine_store import store as store, claimed
from tests.test_github_rest import app_key as app_key, provider as provider
from tests.test_gitlab_engine_ports import case as gitlab  # noqa: F401


@pytest.fixture
async def case(store):
    run, lease = await claimed(store)
    return store, run, lease, FixedClock(lease.expires_at - timedelta(seconds=60))


@pytest.fixture(params=["github", "gitlab"])
def native(request):
    if request.param == "github":
        writer, review, ci, wire, subject = request.getfixturevalue("provider")
        return SimpleNamespace(
            writer=writer,
            review=review,
            repo=gh.REPO,
            branch=gh.DISPATCH.ref,
            transport=writer.api._transport,
            wire=wire,
            subject=subject,
            ci=ci,
            metadata="/installation/repositories",
            listing="/repos/new/repository/pulls",
        )
    wire, api, repo, review, subject, ci, writer = request.getfixturevalue("gitlab")
    return SimpleNamespace(
        writer=writer,
        review=review,
        repo=gl.REPO,
        branch=gl.DISPATCH.ref,
        transport=api.transport,
        wire=wire,
        subject=subject,
        ci=ci,
        metadata="/api/v4/projects/123",
        listing="/api/v4/projects/123/merge_requests",
    )


def bounded(case, native):
    factory, _, _, clock = case
    return Effects(
        EffectStore(factory),
        native.writer,
        native.writer,
        native.subject,
        native.review,
        native.ci,
        native.writer,
        clock,
        call_timeout=0.05,
    )


def hang_native_request(native, monkeypatch, path):
    entered, stopped = asyncio.Event(), asyncio.Event()
    transport = native.transport._http._transport
    original = transport.handle_async_request

    async def request(value):
        if value.url.path != path:
            return await original(value)
        native.wire.calls.append(value)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(transport, "handle_async_request", request)
    return entered, stopped


@pytest.mark.parametrize("stage", ["metadata", "listing"])
async def test_hung_native_probe_is_bounded_without_erasing_or_redispatching_unknown(
    case, native, monkeypatch, stage
):
    factory, run, lease, _ = case
    engine = bounded(case, native)
    entered, stopped = hang_native_request(native, monkeypatch, getattr(native, stage))
    # The complete native ensure includes discovery; a timed-out discovery is no create proof.
    result = await engine.ensure_draft(
        lease, run.id, native.repo, native.branch, "Work", "Approved work"
    )
    assert isinstance(result, Unknown) and entered.is_set() and stopped.is_set()
    row = await EffectStore(factory).get(run.id + ":ensure_draft")
    assert row.state == "unknown"
    saved = row.observations
    entered.clear()
    stopped.clear()
    fresh = bounded(case, native)
    recovered = await asyncio.wait_for(fresh.recover(row), 1)
    assert entered.is_set() and stopped.is_set() and recovered.state == "unknown"
    assert recovered.observations[: len(saved)] == saved
    assert recovered.payload_json == row.payload_json
    assert recovered.dispatched_at == row.dispatched_at
    assert recovered.dispatch_deadline_at == row.dispatch_deadline_at
    assert "TimeoutError" in recovered.outcome_json
    assert not [
        request
        for request in native.wire.calls
        if request.method != "GET" and not request.url.path.endswith("/access_tokens")
    ]


async def test_external_probe_cancellation_propagates_without_authorizing_a_retry(
    case, native, monkeypatch
):
    factory, run, lease, _ = case
    engine = bounded(case, native)
    entered, _ = hang_native_request(native, monkeypatch, native.listing)
    assert isinstance(
        await engine.ensure_draft(lease, run.id, native.repo, native.branch, "Work", "Approved"),
        Unknown,
    )
    row = await EffectStore(factory).get(run.id + ":ensure_draft")
    entered.clear()
    engine.call_timeout = 1
    task = asyncio.create_task(engine.recover(row))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unchanged = await EffectStore(factory).get(row.key)
    assert unchanged == row
    assert not [
        request
        for request in native.wire.calls
        if request.method != "GET" and not request.url.path.endswith("/access_tokens")
    ]
