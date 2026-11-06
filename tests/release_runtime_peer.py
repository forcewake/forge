"""Owned loopback TLS native peer and real application process, never live acceptance."""

import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
from threading import Thread
from urllib.parse import urlsplit

import httpx
import uvicorn

from forge.runtime.app import create_app
from forge.runtime.assembly import open_runtime
from forge.runtime.settings import RuntimeSettings
from tests.fakes.ports import FixedClock
from tests.test_budget_engine import NativeAdmin


class Native:
    def __init__(self, provider, url):
        self.provider, self.url, self.notes, self.issues, self.calls = provider, url, [], {}, []
        self.repo = dict(
            id=123,
            full_name="new/repository",
            path_with_namespace="new/repository",
            default_branch="main",
            archived=False,
            disabled=False,
            ci_config_path=None,
        )
        self.human = dict(id=42, login="reviewer", username="reviewer", type="User", bot=False)
        self.bot = dict(
            id=99, login="forge[bot]", username="project_123_bot_forge", type="Bot", bot=True
        )

    def request(self, method, path, headers, raw):
        self.calls.append(dict(method=method, path=path))
        github = self.provider == "github"
        data = json.loads(raw) if raw else {}
        if path.endswith("/access_tokens"):
            return 201, dict(
                token="synthetic-app-token",
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                repository_selection="selected",
                repositories=[self.repo],
                permissions=data["permissions"],
            )
        if path == "/installation/repositories":
            return 200, dict(total_count=1, repositories=[self.repo])
        if path in {"/repositories/123", "/api/v4/projects/123"}:
            return 200, self.repo
        if path in {"/user", "/api/v4/user"}:
            return 200, self.bot if headers.get(
                "Private-Token"
            ) == "synthetic-write" else self.human
        prefix = "/repos/new/repository/" if github else "/api/v4/projects/123/"
        if not path.startswith(prefix):
            return 404, {}
        resource = path.removeprefix(prefix)
        if resource == "collaborators/reviewer/permission":
            return 200, dict(permission="write", user=self.human)
        if resource == "members/all/42":
            return 200, dict(**self.human, state="active", access_level=30, expires_at=None)
        if resource in {"git/ref/heads/main", "repository/branches/main"}:
            return 200, dict(
                ref="refs/heads/main",
                object=dict(type="commit", sha="a" * 40),
                name="main",
                protected=True,
                commit=dict(id="a" * 40),
            )
        if resource == "issues" and method == "POST":
            number = 7 + len(self.issues)
            issue = dict(
                id=70 + number,
                number=number,
                iid=number,
                project_id=123,
                labels=[],
                state="open",
                updated_at=datetime.now(timezone.utc).isoformat(),
                **data,
            )
            self.issues[str(number)] = issue
            return 201, issue
        parts = resource.split("/")
        if len(parts) == 2 and parts[0] == "issues":
            return 200, self.issues[parts[1]]
        if len(parts) == 3 and parts[2] in {"notes", "comments"}:
            if method == "GET":
                return 200, [note for note in self.notes if note["noteable_iid"] == int(parts[1])]
            actor = (
                self.human
                if headers.get("Authorization") == "Bearer synthetic-actor"
                or headers.get("Private-Token") == "synthetic-actor"
                else self.bot
            )
            note = dict(
                id=100 + len(self.notes),
                body=data["body"],
                user=actor,
                author=actor,
                system=False,
                noteable_type="Issue",
                noteable_iid=int(parts[1]),
                noteable_id=self.issues[parts[1]]["id"],
                project_id=123,
                issue_url=self.url + f"/repos/new/repository/issues/{parts[1]}",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.notes.append(note)
            return 201, note
        return 404, {}


def native_server(native, port, cert, key):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status, value = native.request(
                self.command, urlsplit(self.path).path, self.headers, raw
            )
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("document", type=Path)
    args = parser.parse_args()
    document = json.loads(args.document.read_text())
    config = RuntimeSettings.model_validate_json(json.dumps(document["config"]))
    connection = config.connections[0]
    peer = Native(connection.provider, connection.api_url)
    server, thread = native_server(
        peer, urlsplit(connection.api_url).port, document["cert"], document["key"]
    )
    clock = FixedClock(datetime.now(timezone.utc))
    gateway = NativeAdmin(clock)

    @asynccontextmanager
    async def opener(settings):
        async with open_runtime(
            settings,
            environ=document["environment"],
            gateway_transport=httpx.MockTransport(gateway),
            clock=clock,
        ) as runtime:
            yield runtime

    try:
        uvicorn.run(
            create_app(config, opener=opener),
            host="127.0.0.1",
            port=urlsplit(config.public_url).port,
            ssl_certfile=document["cert"],
            ssl_keyfile=document["key"],
            access_log=False,
            log_level="warning",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        Path(document["receipt"]).write_text(
            json.dumps(
                {
                    "scope": "owned-synthetic-native-and-guard",
                    "calls": peer.calls,
                    "notes": len(peer.notes),
                    "issues": len(peer.issues),
                    "model_calls": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
