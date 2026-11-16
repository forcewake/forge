"""SIGKILL a real forge worker mid-command and mid-lane, then recover on restart."""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.command_values import CommandEnvelope
from forge.domain.plan import RepositoryIdentity
from forge.ingress_values import Command
from forge.port_values import Actor, RepoRef, SubjectRef
from forge.runtime.assembly import open_runtime
from forge.runtime.settings import load
from forge.store.commands import CommandStore
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineStep
from tests.fi_os.lab import install_schema
from tests.fi_os.worker import OSWorker, worker_env
from tests.runtime_fixture import certificate, configuration

pytestmark = pytest.mark.slow
SHA = "a" * 40


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if "E" in value or "e" in value:
            try:
                from decimal import Decimal

                normalized = format(Decimal(value), "f")
                if "." in normalized:
                    normalized = normalized.rstrip("0").rstrip(".")
                value = normalized or "0"
            except Exception:
                pass
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(type(value))


def toml_dump(document, prefix=""):
    lines, tables, arrays = [], [], []
    for key, value in document.items():
        if isinstance(value, dict):
            tables.append((prefix + key, value))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            arrays.append((prefix + key, value))
        elif value is not None:
            lines.append(f"{key} = {toml_value(value)}")
    for name, value in tables:
        lines.append(f"\n[{name}]")
        lines.append(toml_dump(value, name + "."))
    for name, items in arrays:
        for item in items:
            lines.append(f"\n[[{name}]]")
            lines.append(toml_dump(item, name + "."))
    return "\n".join(line for line in lines if line != "") + "\n"


class Hold(BaseHTTPRequestHandler):
    hold = threading.Event()
    seen = []

    def log_message(self, *args):
        return None

    def do_GET(self):
        self.finish_json()

    def do_POST(self):
        self.finish_json()

    def do_PUT(self):
        self.finish_json()

    def finish_json(self):
        Hold.seen.append(self.path)
        if not Hold.hold.is_set():
            Hold.hold.wait(20)
        path = self.path
        if "members/all/" in path:
            payload = {"id": 42, "username": "reviewer", "state": "active", "access_level": 40}
        elif "/issues/" in path:
            payload = {
                "id": 7,
                "iid": 1,
                "project_id": 123,
                "title": "Ship the fix",
                "description": "details",
                "labels": [],
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        elif "/repository/branches/" in path:
            payload = {"name": "main", "commit": {"id": SHA}}
        else:
            payload = {
                "id": 123,
                "path_with_namespace": "new/repository",
                "default_branch": "main",
                "archived": False,
                "username": "project_123_bot_forge",
                "bot": True,
            }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(cert, key):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = ThreadingHTTPServer(("127.0.0.1", port), Hold)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


async def rows(url, model):
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return list(await session.scalars(select(model)))
    finally:
        await engine.dispose()


async def expire(url):
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            for row in await session.scalars(select(EngineCommand)):
                if row.state == "running":
                    row.lease_expires_at = past
            for row in await session.scalars(select(EngineStep)):
                if row.state == "running":
                    row.lease_expires_at = past
                    row.due_at = past
    finally:
        await engine.dispose()


async def wait_until(predicate, timeout, detail):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        time.sleep(0.05)
    raise AssertionError(detail() if callable(detail) else detail)


async def command_running(database):
    return any(row.state == "running" for row in await rows(database, EngineCommand))


async def command_settled(database):
    return any(
        row.state in {"resolved", "completed"} for row in await rows(database, EngineCommand)
    )


async def step_running(database):
    return any(row.state == "running" for row in await rows(database, EngineStep))


async def steps_idle(database, worker):
    idle = all(row.state != "running" for row in await rows(database, EngineStep))
    return idle and worker.alive


async def test_sigkill_mid_command_and_mid_lane_recovers_on_restart(tmp_path):
    key, cert = certificate(tmp_path)
    Hold.hold.clear()
    Hold.seen.clear()
    server, port = serve(cert, key)
    native = f"https://127.0.0.1:{port}"
    config, values = configuration(tmp_path, native_url=native, ca=cert)
    document = config.model_dump(mode="json", by_alias=True)
    document["gateway"]["url"] = native
    toml = tmp_path / "forge.toml"
    toml.write_text(toml_dump(document))
    loaded = load(toml)
    assert loaded.public_url == config.public_url
    database = values["SYNTHETIC_DATABASE_URL"]
    install_schema(database)
    identity = RepositoryIdentity(provider="gitlab", connection_id="ce-1", repository_id="123")
    repo = RepoRef(identity=identity, full_name="new/repository")
    envelope = CommandEnvelope(
        source="api",
        native_id="os-kill-1",
        actor=Actor(
            provider="gitlab",
            connection_id="ce-1",
            native_id="42",
            login="reviewer",
            is_bot=False,
        ),
        target=SubjectRef(repo=repo, number=1),
        command=Command(kind="implement"),
    )
    async with open_runtime(loaded, environ=values) as runtime:
        await CommandStore(runtime.factory).accept(envelope, now=datetime.now(timezone.utc))
    worker = OSWorker("killed", worker_env(values), tmp_path, toml)
    restarted = OSWorker("restarted", worker_env(values), tmp_path, toml)
    try:
        worker.start()
        await wait_until(lambda: command_running(database), 20, worker.tail)
        worker.sigkill()
        assert worker.exit_code == -9 or worker.exit_code == 137 or not worker.alive
        await expire(database)
        Hold.hold.set()
        restarted.start()
        await wait_until(lambda: command_settled(database), 20, restarted.tail)
        assert restarted.alive
        Hold.hold.clear()
        await wait_until(lambda: step_running(database), 20, restarted.tail)
        restarted.sigkill()
        assert not restarted.alive
        await expire(database)
        Hold.hold.set()
        recovered = OSWorker("recovered", worker_env(values), tmp_path, toml)
        recovered.start()
        try:
            await wait_until(lambda: steps_idle(database, recovered), 20, recovered.tail)
            assert recovered.alive
            assert await command_settled(database)
        finally:
            if recovered.alive:
                recovered.sigterm()
    finally:
        if worker.alive:
            worker.sigkill()
        if restarted.alive:
            restarted.sigkill()
        server.shutdown()
