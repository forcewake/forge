"""A current bot display must still contain the exact prepared bytes at /go."""

from datetime import datetime, timezone
import json

import httpx
import pytest
from sqlalchemy import select

from forge.domain.states import State
from forge.engine.approval_comment import displayed_plan
from forge.port_values import CommentRef, DefinitelyAbsent, ReadUnavailable
from forge.providers.fake.state import ref_key
from forge.store.approval_values import PreparedPlan
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_runs import EngineGate
from tests.test_engine_approval import env as env
from tests.test_github_rest import app_key as app_key, provider as provider
from tests.test_domain_approved_input import spec


def prepared_plan():
    return PreparedPlan(
        "a" * 32, "b" * 32, spec(), datetime(2026, 10, 25, tzinfo=timezone.utc), "open"
    )


@pytest.mark.parametrize(
    "change",
    (
        "body",
        "implementation",
        "marker",
        "missing",
        "native_id",
        "receipt_kind",
        "receipt_digest",
        "receipt_owner",
    ),
)
async def test_go_refuses_edited_missing_or_replaced_plan_without_consuming_or_dispatching(
    env, change
):
    run, prepared = await env.planned()
    comment = next(iter(env.fake.comments.values()))
    if change == "body":
        comment.body += "\nUnexpected edit"
    elif change == "implementation":
        comment.body = comment.body.replace(prepared.approved.approved_digest, "0" * 64)
    elif change == "marker":
        comment.marker = "removed"
    elif change == "missing":
        env.fake.comments.clear()
    elif change == "native_id":
        env.fake.comments.clear()
        comment.ref = comment.ref.model_copy(update={"native_id": "999"})
        env.fake.comments[f"{ref_key(comment.ref.subject)}:999"] = comment
    else:
        async with env.factory.begin() as session:
            effect = await session.scalar(
                select(EngineEffect).where(EngineEffect.key == f"{run.id}:plan:comment")
            )
            if change == "receipt_kind":
                value = json.loads(effect.outcome_json)
                value["value"]["kind"] = "review_comment"
                effect.outcome_json = json.dumps(value)
            elif change == "receipt_owner":
                effect.step_id = (
                    await session.scalar(
                        select(EngineStep).where(EngineStep.key == f"{run.id}:plan")
                    )
                ).id
            else:
                effect.payload_digest = "0" * 64
    await env.approve(run.id, await env.claim(run, prepared), command_key="go")
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "approval_plan_comment_changed"
    async with env.factory() as session:
        gate = await session.get(EngineGate, prepared.gate_id)
        assert gate.state == "open" and gate.consumed_at is None
    assert not any(
        name in ("ensure_branch", "commit", "dispatch_lane") for name, _ in env.fake.calls
    )


@pytest.mark.parametrize("method", ("find_comment", "probe_comment_update"))
async def test_native_display_read_outage_retries_boundedly_without_consumption(
    env, monkeypatch, method
):
    run, prepared = await env.planned()

    async def unavailable(*args):
        raise ReadUnavailable("read_unavailable", "synthetic:native")

    monkeypatch.setattr(env.fake, method, unavailable)
    await env.approve(run.id, await env.claim(run, prepared), command_key="go")
    assert (await env.runner.tick()).rescheduled == 1
    assert (await env.runs.load(run.id)).state == State.AWAITING_APPROVAL
    env.clock.instant += __import__("datetime").timedelta(seconds=60)
    assert (await env.runner.tick()).rescheduled == 1
    env.clock.instant += __import__("datetime").timedelta(seconds=120)
    assert (await env.runner.tick()).parked == 1
    assert (
        await env.runs.load(run.id)
    ).reason == "retry_exhausted:approval_plan_comment_unavailable"
    async with env.factory() as session:
        assert (await session.get(EngineGate, prepared.gate_id)).consumed_at is None


async def test_current_display_proof_consumes_once_without_parsing_or_rewriting_plan(env):
    run, prepared = await env.planned()
    before = next(iter(env.fake.comments.values())).body
    await env.approve(run.id, await env.claim(run, prepared), command_key="go")
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.RUNNING
    assert (await env.approvals.load(run.id)).approved == prepared.approved
    assert next(iter(env.fake.comments.values())).body == before


@pytest.mark.parametrize(
    "fault",
    ("body", "author", "native_id", "missing", "list_unavailable", "body_unavailable", "exact"),
)
async def test_github_current_native_display_keeps_exact_bytes_bot_and_identity(provider, fault):
    from tests.test_github_reads import SUBJECT, comment

    _, _, _, wire, subjects = provider
    prepared = prepared_plan()
    row = comment(body=prepared.comment + "\n\n" + prepared.marker)
    wire.routes["issues/7/comments"] = [row]
    wire.routes["issues/comments/10"] = row
    if fault == "body":
        row["body"] = "Edited implementation\n\n" + prepared.marker
    elif fault == "author":
        row["user"] = {"id": 42, "login": "human", "type": "User"}
    elif fault == "native_id":
        row["id"] = 11
    elif fault == "missing":
        wire.routes["issues/7/comments"] = []
    elif fault == "list_unavailable":
        wire.routes["issues/7/comments"] = httpx.Response(503)
    elif fault == "body_unavailable":
        wire.routes["issues/comments/10"] = httpx.Response(503)
    expected = CommentRef(subject=SUBJECT, native_id="10")
    if fault.endswith("unavailable"):
        with pytest.raises(ReadUnavailable):
            await displayed_plan(subjects, expected, prepared)
    else:
        assert await displayed_plan(subjects, expected, prepared) == (fault == "exact")
    if fault == "missing":
        assert isinstance(await subjects.probe_comment(SUBJECT, prepared.marker), DefinitelyAbsent)
    assert all(
        request.method == "GET" or request.url.path.endswith("/access_tokens")
        for request in wire.calls
    )


@pytest.mark.parametrize(
    "fault",
    ("body", "author", "native_id", "missing", "list_unavailable", "body_unavailable", "exact"),
)
async def test_gitlab_current_native_display_keeps_exact_bytes_bot_and_identity(fault):
    from tests import gitlab_engine_fixture as f

    wire = f.Wire()
    api, _, _, subjects, *_ = f.adapters(wire)
    prepared = prepared_plan()
    row = f.note(body=prepared.comment + "\n\n" + prepared.marker)
    wire.routes["issues/7/notes"] = [row]
    wire.routes["issues/7/notes/10"] = row
    if fault == "body":
        row["body"] = "Edited implementation\n\n" + prepared.marker
    elif fault == "author":
        row["author"] = f.user(False)
    elif fault == "native_id":
        row["id"] = 11
    elif fault == "missing":
        wire.routes["issues/7/notes"] = []
    elif fault == "list_unavailable":
        wire.routes["issues/7/notes"] = httpx.Response(503)
    elif fault == "body_unavailable":
        wire.routes["issues/7/notes/10"] = httpx.Response(503)
    try:
        expected = CommentRef(subject=f.SUBJECT, native_id="10")
        if fault.endswith("unavailable"):
            with pytest.raises(ReadUnavailable):
                await displayed_plan(subjects, expected, prepared)
        else:
            assert await displayed_plan(subjects, expected, prepared) == (fault == "exact")
        if fault == "missing":
            assert isinstance(
                await subjects.probe_comment(f.SUBJECT, prepared.marker), DefinitelyAbsent
            )
        assert all(request.method == "GET" for request in wire.calls)
    finally:
        await api.transport.close()
