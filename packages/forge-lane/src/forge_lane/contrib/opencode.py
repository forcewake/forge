"""Experimental owned OpenCode v2 server; transcript completion and guarded permissions."""

import asyncio
import json
import re
import secrets
import socket
import tempfile
from pathlib import Path

import httpx
from forge_protocol import Brief

from ..driver import Capability, DriverEnv, DriverEvent
from ..errors import LaneFailure
from . import require_enabled
from .common import EventQueue, SafeFiles, Stdio, environment, failure, grant_key

RULES = [{"action": "*", "resource": "*", "effect": "ask"}]
ROUTES = (
    "/api/session",
    "/api/session/{sessionID}/model",
    "/api/session/{sessionID}/prompt",
    "/api/session/{sessionID}/message",
    "/api/session/{sessionID}/permission",
    "/api/session/{sessionID}/permission/{requestID}/reply",
    "/api/session/{sessionID}/interrupt",
)


class OpenCodeDriver:
    name = "opencode"
    capabilities: frozenset[Capability] = frozenset({"interrupt"})
    requires_model_key = True

    def __init__(self, binary="opencode", server_factory=None, transport=None):
        self.binary, self.factory, self.transport = (
            binary,
            server_factory or Stdio.server,
            transport,
        )

    async def start(self, brief: Brief, workspace: Path, env: DriverEnv) -> "OpenCodeSession":
        key = grant_key(brief, env)
        assert env.model_key is not None
        home = tempfile.TemporaryDirectory(prefix="forge-opencode-")
        directory = Path(home.name)
        password = secrets.token_urlsafe(32)
        child = environment(directory)
        config = {
            "provider": {
                "forge": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Forge gateway",
                    "options": {
                        "baseURL": str(env.model_key.gateway_url),
                        "apiKey": "{env:FORGE_MODEL_KEY}",
                    },
                    "models": {env.model: {"name": env.model}},
                }
            },
            "enabled_providers": ["forge"],
            "model": "forge/" + env.model,
            "plugin": [],
            "mcp": {},
        }
        child.update(
            FORGE_MODEL_KEY=key,
            OPENCODE_SERVER_PASSWORD=password,
            OPENCODE_CONFIG_CONTENT=json.dumps(config),
            OPENCODE_DISABLE_AUTOUPDATE="true",
            OPENCODE_DISABLE_DEFAULT_PLUGINS="1",
            OPENCODE_DISABLE_PROJECT_CONFIG="1",
            OPENCODE_PURE="1",
            OPENCODE_DISABLE_EXTERNAL_SKILLS="1",
            OPENCODE_DISABLE_CLAUDE_CODE_SKILLS="1",
            OPENCODE_CONFIG_DIR=str(directory / "config"),
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        http = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            auth=httpx.BasicAuth("opencode", password),
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )
        session = OpenCodeSession(workspace, home, key, http)
        try:
            session.process = await self.factory(
                [self.binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                workspace,
                child,
            )
            await session.ready()
            await session.initialize(brief, env)
            session.owner = asyncio.create_task(session.run())
            return session
        except BaseException:
            await session.interrupt()
            raise


class OpenCodeSession(EventQueue):
    def __init__(self, workspace, home, key, http):
        super().__init__(key)
        self.workspace, self.home, self.http = workspace, home, http
        self.policy = SafeFiles(workspace, None)
        self.process = self.owner = None
        self.session_id = ""
        self.baseline: set[str] = set()
        self.closed = False
        self.cleanup_lock = asyncio.Lock()
        self.cleanup_task: asyncio.Task | None = None

    async def request(self, method, path, **kwargs):
        try:
            async with self.http.stream(method, path, **kwargs) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise failure(
                            "driver_protocol_error", "OpenCode response exceeds byte limit"
                        )
                return json.loads(body) if body else None
        except httpx.TransportError:
            raise
        except (httpx.HTTPError, ValueError):
            raise failure("driver_protocol_error", "OpenCode HTTP request failed") from None

    async def ready(self):
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            try:
                spec = await self.request("GET", "/openapi.json")
                if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
                    raise failure(
                        "driver_protocol_error", "OpenCode returned an invalid capability document"
                    )
                paths = {re.sub(r"\{[^}]+\}", "{}", path) for path in spec.get("paths", {})}
                if not all(re.sub(r"\{[^}]+\}", "{}", route) in paths for route in ROUTES):
                    raise failure(
                        "driver_protocol_error", "OpenCode server lacks required v2 safety routes"
                    )
                return
            except httpx.TransportError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise failure(
                        "driver_protocol_timeout", "OpenCode server did not become ready"
                    ) from None
                await asyncio.sleep(0.1)

    async def initialize(self, brief, env):
        result = await self.request(
            "POST",
            "/api/session",
            json={"title": "Forge approved implementation", "permissions": RULES},
        )
        session = result.get("data", {})
        self.session_id = session.get("id", "")
        if (
            not isinstance(self.session_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", self.session_id)
            or session.get("permissions") != RULES
        ):
            raise failure(
                "driver_policy_refused", "OpenCode did not confirm session permission isolation"
            )
        location = session.get("directory", session.get("location", {}).get("directory"))
        if location != str(self.workspace.resolve()):
            raise failure(
                "driver_policy_refused", "OpenCode session is outside the approved workspace"
            )
        await self.request(
            "POST", self.path("/model"), json={"model": {"providerID": "forge", "id": env.model}}
        )
        self.baseline = {
            message["id"]
            for message in await self.transcript()
            if isinstance(message.get("id"), str)
        }
        text = (
            brief.execution_brief_text
            + "\n\n"
            + env.resume_note
            + "\nOnly confined file tools are permitted. No shell, MCP, repository APIs or authority files.\n"
            + self.policy.inventory()
        )
        await self.request("POST", self.path("/prompt"), json={"text": text})

    def path(self, suffix):
        return "/api/session/" + self.session_id + suffix

    async def transcript(self):
        payload = await self.request(
            "GET", self.path("/message"), params={"order": "desc", "limit": "100"}
        )
        messages = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(messages, list) or len(messages) > 100:
            raise failure("driver_protocol_error", "OpenCode returned an invalid transcript")
        return [message for message in messages if isinstance(message, dict)]

    async def permissions(self):
        payload = await self.request("GET", self.path("/permission"))
        values = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(values, list) or len(values) > 256:
            raise failure("driver_policy_refused", "OpenCode permission requests are unavailable")
        for request in values:
            if not isinstance(request, dict):
                raise failure("driver_policy_refused", "Invalid OpenCode permission request")
            if request.get("sessionID") != self.session_id:
                raise failure(
                    "driver_policy_refused", "OpenCode permission belongs to a different session"
                )
            action, resources = request.get("action"), request.get("resources")
            name = {"read": "Read", "write": "Write", "edit": "Edit"}.get(str(action))
            allowed = False
            if name is not None and isinstance(resources, list) and resources:
                allowed = all(
                    isinstance(path, str)
                    and not any(c in path for c in "*?[]\x00\r\n")
                    and self.policy.permitted(name, {"file_path": path})
                    and not (self.workspace / path).is_dir()
                    for path in resources
                )
            request_id = request.get("id")
            if not isinstance(request_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,256}", request_id
            ):
                raise failure("driver_policy_refused", "OpenCode permission identity is invalid")
            await self.request(
                "POST",
                self.path("/permission/" + request_id + "/reply"),
                json={"decision": "once" if allowed else "reject"},
            )
            if not allowed:
                raise failure("driver_policy_refused", "OpenCode requested an unapproved operation")

    async def run(self):
        terminal: DriverEvent | LaneFailure | None = None
        try:
            while True:
                await self.permissions()
                messages = await self.transcript()
                complete = [
                    message
                    for message in messages
                    if message.get("id") not in self.baseline
                    and message.get("type") == "assistant"
                    and message.get("finish") in ("stop", "error")
                ]
                if complete:
                    final = complete[0]
                    for item in final.get("content", []):
                        if item.get("type") == "text":
                            self.message(str(item.get("text", "")))
                    terminal = DriverEvent(
                        "completed" if final["finish"] == "stop" else "failed",
                        terminal_reason="completed"
                        if final["finish"] == "stop"
                        else "opencode_execution_failed",
                    )
                    break
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            terminal = (
                exc
                if isinstance(exc, LaneFailure)
                else failure("driver_protocol_error", "OpenCode conversation failed")
            )
        finally:
            try:
                await self.cleanup()
            except Exception:
                terminal = failure("driver_cleanup_failed", "Contrib process did not stop safely")
            if terminal is not None:
                self.emit(terminal)
            self.emit(None)

    async def cleanup(self):
        async with self.cleanup_lock:
            if self.cleanup_task is None:
                self.closed = True
                self.cleanup_task = asyncio.create_task(self.dispose())
            cleanup = self.cleanup_task
        await asyncio.shield(cleanup)

    async def dispose(self):
        try:
            if self.process:
                await self.process.close()
        finally:
            try:
                await self.http.aclose()
            finally:
                self.home.cleanup()

    async def interrupt(self):
        if self.session_id and not self.closed:
            try:
                await asyncio.wait_for(self.request("POST", self.path("/interrupt")), 2)
            except Exception:
                pass
        if self.owner and self.owner is not asyncio.current_task():
            self.owner.cancel()
            await asyncio.gather(self.owner, return_exceptions=True)
        await self.cleanup()


def create_driver():
    require_enabled()
    return OpenCodeDriver()
