"""The actual signed hook exposes only configured native read-only review authority."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from forge.domain.budget import Resources
from forge.domain.states import Handler
from forge.engine.native_review import NativeReviewHandler
from forge.review.native_values import NativeReviewPolicy
from forge.runtime.reactive import ReactiveServices
from forge.runtime.settings import RuntimeSettings
from forge.runtime.worker import serve
from forge.store.models_review import EngineReactiveReview
from tests import gitlab_engine_fixture as native
from tests.runtime_fixture import WEBHOOK_SECRET, configuration, native_server
from tests.test_runtime_app import application, auth, command


def opt_in(config, *, model=None):
    document = config.model_dump(mode="json", by_alias=True)
    repository = document["connections"][0]["repositories"][0]
    policy = NativeReviewPolicy(
        budget=config.connections[0].repositories[0].policy,
        requested=Resources(usd=Decimal("2"), calls=2, tokens=60000),
        minimum=Resources(usd=Decimal("0.72"), calls=1, tokens=1),
    ).model_dump(mode="json")
    if model is not None:
        policy["budget"]["model"] = model
    repository["native_review"] = policy
    return RuntimeSettings.model_validate_json(json.dumps(document))


def push():
    return {
        "object_kind": "merge_request",
        "project": {"id": 123, "path_with_namespace": "new/repository"},
        "user": native.user(False),
        "object_attributes": {
            **native.mr(),
            "action": "update",
            "last_commit": {"id": native.SHA},
        },
    }


@pytest.mark.parametrize("enabled", [False, True])
async def test_signed_hook_requires_optin_and_proven_owned_draft_before_queue_or_paid_work(
    tmp_path, enabled
):
    with native_server(tmp_path) as (peer, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        if enabled:
            config = opt_in(config)
        async with application(config, values, peer) as (app, client, gateway):
            response = await client.post(
                "/hooks/ce-1",
                content=json.dumps(push()),
                headers={
                    "X-Gitlab-Token": WEBHOOK_SECRET,
                    "X-Gitlab-Event": "Merge Request Hook",
                    "X-Gitlab-Event-UUID": "12345678-1234-1234-1234-123456789abc",
                },
            )
            assert response.status_code == 202, response.text
            assert response.json()["commands"] == []
            assert response.json()["review"]["reason"] == (
                "native_review_root_unproven" if enabled else "native_review_disabled"
            )
            service = app.state.runtime
            assert isinstance(
                service.worker.runner.handlers[Handler.FEEDBACK_REVIEW], NativeReviewHandler
            )
            async with service.factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(EngineReactiveReview))
                    == 0
                )
            assert not gateway.writes and not peer.calls
            denied = await client.post(
                "/api/v1/commands", headers=auth(), json=command("native_review")
            )
            assert denied.status_code == 422


async def test_startup_checks_every_opted_in_review_model_before_any_native_or_model_call(tmp_path):
    with native_server(tmp_path) as (peer, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        config = opt_in(config, model="unpriced-model")
        with pytest.raises(ValueError, match="explicit native tariff"):
            async with application(config, values, peer):
                pytest.fail("an unpriced configured review model cannot start")
        assert not peer.calls


async def test_actual_worker_recovers_review_receipts_before_activating_new_heads():
    events, stop = [], asyncio.Event()

    class Reconciler:
        async def tick(self, *, limit):
            assert limit == 20
            events.append("recover")
            return 0

    class Reviews:
        async def activate_pending(self, *, limit):
            assert limit == 20
            events.append("activate")
            return 0

    class Commands:
        async def tick(self, *, limit):
            events.append("commands")
            return 0

    class Worker:
        async def tick(self):
            events.append("engine")
            stop.set()
            return SimpleNamespace(execution=SimpleNamespace(claimed=1))

    class Wakeups:
        @asynccontextmanager
        async def listen(self, url):
            yield

    class Lane:
        async def reap_terminal_blobs(self):
            return 0

    runtime = SimpleNamespace(
        engine=SimpleNamespace(url=SimpleNamespace(render_as_string=lambda **kw: "owned")),
        wakeups=Wakeups(),
        reactive=ReactiveServices(Reviews(), Reconciler()),
        commands=Commands(),
        worker=Worker(),
        lane=Lane(),
    )
    await serve(runtime, stop)
    assert events == ["recover", "activate", "commands", "engine"]
