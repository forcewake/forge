"""Run only with an installed wheel's isolated Python, outside the checkout."""

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import forge
from forge.config import Settings
from forge.database import dispose_engine, init_db
from forge.main import create_app
from forge.migrate import build_alembic_config


def check(url):
    return subprocess.run(
        [sys.executable, "-I", "-m", "forge.migrate", "--check", "--database-url", url],
        capture_output=True,
        text=True,
        timeout=30,
    )


def tables(path):
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


async def bootstrap(url):
    try:
        await init_db(url)
    finally:
        await dispose_engine()


class ReadOnlyHealth(BaseHTTPRequestHandler):
    reads = []

    def do_GET(self):
        type(self).reads.append(self.path)
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


async def boot_application(url, address):
    settings = Settings(
        _env_file=None,
        DATABASE_URL=url,
        REDIS_URL=None,
        FORGE_GITLAB_ENABLED=False,
        FORGE_GITHUB_ENABLED=False,
        LITELLM_URL=address,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.session_factory is not None
        assert app.state.redis_manager is None and app.state.task_queue is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            response = await client.get("/health")
            assert response.status_code == 200, response.text
            assert response.json()["database"] == response.json()["litellm"] == "ok"


def main():
    installed = Path(forge.__file__).resolve()
    assert installed.is_relative_to(Path(sys.prefix)), installed
    scripts = Path(build_alembic_config().get_main_option("script_location")).resolve()
    assert scripts.is_relative_to(installed.parent), scripts
    assert "_migrations" in scripts.parts
    path = Path.cwd() / "fresh.db"
    url = "sqlite+aiosqlite:///" + str(path)
    refused = check(url)
    assert refused.returncode == 1 and "not bootstrapped" in refused.stderr, refused
    assert not tables(path)
    asyncio.run(bootstrap(url))
    assert {
        "flow_runs",
        "step_runs",
        "event_inbox",
        "run_budgets",
        "agent_runs",
        "conversations",
        "review_states",
        "security_findings",
        "alembic_version",
    } <= tables(path)
    assert {
        "eng_runs",
        "eng_run_specs",
        "eng_approved_inputs",
        "eng_gates",
        "eng_steps",
        "eng_effects",
        "eng_candidates",
        "eng_budget_pools",
        "eng_budget_reservations",
        "eng_budget_amendments",
        "eng_usage",
        "eng_gateway_keys",
        "eng_control_commands",
        "eng_checkpoints",
        "eng_blobs",
        "eng_review_rounds",
        "eng_lane_sessions",
    } <= tables(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    accepted = check(url)
    assert accepted.returncode == 0, accepted.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReadOnlyHealth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(boot_application(url, f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert not thread.is_alive() and ReadOnlyHealth.reads == ["/health"]
    print(
        json.dumps(
            {
                "migration_chain": str(scripts),
                "fresh_bootstrap": True,
                "zero_mutation_check": True,
                "tables": sorted(tables(path)),
                "asgi_startup_and_health": True,
                "provider_writes": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
