"""Execute only installed entry points, outside the checkout and its import path."""

import asyncio
import hashlib
from importlib.metadata import distribution, version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import forge
import httpx
from forge.runtime.app import create_app
from forge.runtime.assembly import open_runtime
from forge.runtime.settings import RuntimeSettings


def schema(command, url):
    return subprocess.run(
        [str(Path(sys.executable).with_name("forge-schema")), command, "--database-url", url],
        capture_output=True,
        text=True,
        timeout=30,
    )


def tables(path):
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


async def boot_application(config):
    calls = []

    def forbidden(request):
        calls.append(str(request.url))
        raise AssertionError("Application liveness cannot call providers or models")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def opener(settings):
        async with open_runtime(
            settings,
            native_transport=httpx.MockTransport(forbidden),
            gateway_transport=httpx.MockTransport(forbidden),
        ) as runtime:
            yield runtime

    app = create_app(config, opener=opener)
    async with app.router.lifespan_context(app):
        assert app.state.runtime is not None and app.state.lane_v1_service is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://local"
        ) as client:
            response = await client.get("/healthz")
            assert response.status_code == 200 and response.json() == {
                "service": "forge",
                "version": "1.0.0",
            }
    assert app.state.runtime is None and app.state.lane_v1_service is None
    assert not calls


def main():
    installed = Path(forge.__file__).resolve()
    assert installed.is_relative_to(Path(sys.prefix)), installed
    assert forge.__version__ == version("forge") == "1.0.0"
    entries = {entry.name: entry.value for entry in distribution("forge").entry_points}
    assert entries == {
        "forge": "forge.runtime.cli:main",
        "forge-schema": "forge.store.schema.__main__:main",
    }
    for module in (
        "main",
        "cli",
        "doctor",
        "config",
        "database",
        "migrate",
        "adaptive",
        "durable",
        "runs",
        "worker",
        "gateway",
        "repository",
        "integrations",
        "gitlab",
        "lane_driver",
        "harness_entry",
        "api.lane_auth_store",
        "api.lane_v1_store",
        "api.lane_engine",
    ):
        assert find_spec("forge." + module) is None, module
    path = Path.cwd() / "fresh.db"
    url = "sqlite+aiosqlite:///" + str(path)
    assert schema("check", url).returncode == 1
    assert not tables(path)
    installed_schema = schema("init", url)
    assert installed_schema.returncode == 0, installed_schema.stderr
    names = tables(path)
    assert {
        "runs",
        "run_specs",
        "approved_inputs",
        "gates",
        "steps",
        "effects",
        "candidates",
        "budget_pools",
        "step_timings",
        "review_cursors",
        "reactive_reviews",
        "alembic_version",
    } <= names
    assert not any(name.startswith("eng_") for name in names)
    assert not {"flow_runs", "step_runs", "outbox", "agent_runs", "conversations"} & names
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    checked = schema("check", url)
    assert checked.returncode == 0, checked.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    repeated = schema("init", url)
    assert repeated.returncode == 1 and hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect("old.db") as db:
        db.execute("CREATE TABLE flow_runs(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO flow_runs VALUES('preserved-old-run')")
    old = Path("old.db")
    old_before = hashlib.sha256(old.read_bytes()).hexdigest()
    assert schema("init", "sqlite+aiosqlite:///" + str(old.resolve())).returncode == 1
    assert hashlib.sha256(old.read_bytes()).hexdigest() == old_before
    values = json.loads(Path(sys.argv[1]).read_text())
    os.environ.update(values["environment"])
    config = RuntimeSettings.model_validate_json(json.dumps(values["config"]))
    os.environ[config.database_url_env] = url
    asyncio.run(boot_application(config))
    result = subprocess.run(
        [str(Path(sys.executable).with_name("forge")), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and result.stdout.strip() == "forge 1.0.0"
    print(
        json.dumps(
            {
                "version": version("forge"),
                "fresh_bootstrap": True,
                "zero_mutation_check": True,
                "asgi_startup_and_health": True,
                "provider_writes": 0,
                "retired_runtime_files": 0,
                "tables": sorted(names),
            }
        )
    )


if __name__ == "__main__":
    main()
