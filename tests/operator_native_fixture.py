"""Authenticated operator HTTP against the installed schema and concrete native ports."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from forge.runtime.app import create_app
from forge.runtime.assembly import database, open_runtime
from forge.store.schema import initialize
from tests import gitlab_engine_fixture as native
from tests.fakes.ports import FixedClock
from tests.runtime_fixture import configuration
from tests.test_budget_engine import NativeAdmin
from tests.test_runtime_app import auth


@pytest.fixture
async def operator_native(tmp_path):
    config, values = configuration(tmp_path)
    db = database(values["SYNTHETIC_DATABASE_URL"])
    await initialize(db)
    await db.dispose()
    clock = FixedClock(datetime.now(timezone.utc))
    peer, gateway = native.Wire(), NativeAdmin(clock)

    @asynccontextmanager
    async def opener(settings):
        async with open_runtime(
            settings,
            environ=values,
            clock=clock,
            native_transport=httpx.MockTransport(peer),
            gateway_transport=httpx.MockTransport(gateway),
        ) as service:
            yield service

    app = create_app(config, opener=opener)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://forge.invalid"
        ) as client:
            yield SimpleNamespace(
                app=app,
                client=client,
                service=app.state.runtime,
                headers=auth(),
                config=config,
                values=values,
                peer=peer,
                gateway=gateway,
            )
    assert app.state.runtime is None and app.state.lane_v1_service is None
