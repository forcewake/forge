"""Experimental Codex app-server driver with client-mediated confined file tools."""

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from forge_protocol import Brief

from ..driver import Capability, DriverEnv, DriverEvent
from ..errors import LaneFailure
from . import require_enabled
from .common import FILE_SCHEMAS, EventQueue, SafeFiles, Stdio, environment, failure, grant_key


class CodexDriver:
    name = "codex"
    capabilities: frozenset[Capability] = frozenset({"interrupt", "steer"})
    requires_model_key = True

    def __init__(self, binary="codex", transport_factory=None):
        self.binary, self.factory = binary, transport_factory or Stdio.spawn

    async def start(self, brief: Brief, workspace: Path, env: DriverEnv) -> "CodexSession":
        key = grant_key(brief, env)
        assert env.model_key is not None
        home = tempfile.TemporaryDirectory(prefix="forge-codex-")
        directory = Path(home.name)
        (directory / "codex").mkdir()
        child = environment(directory)
        child.update(CODEX_HOME=str(directory / "codex"), FORGE_MODEL_KEY=key)
        provider = {
            "name": "Forge attempt gateway",
            "base_url": str(env.model_key.gateway_url),
            "env_key": "FORGE_MODEL_KEY",
            "wire_api": "responses",
            "requires_openai_auth": False,
        }
        overrides: dict[str, Any] = {
            "model_provider": "forge",
            "model_providers.forge": provider,
            "features.shell_tool": False,
            "features.shell_snapshot": False,
            "features.apply_patch_freeform": False,
            "web_search": "disabled",
            "apps._default.enabled": False,
            "mcp_servers": {},
            f"projects.{json.dumps(str(workspace.resolve()))}.trust_level": "untrusted",
        }
        # JSON scalar syntax is TOML-compatible; the provider table is written privately.
        lines = ['model_provider = "forge"', 'web_search = "disabled"', "[model_providers.forge]"]
        lines.extend(f"{name} = {json.dumps(value)}" for name, value in provider.items())
        (directory / "codex" / "config.toml").write_text("\n".join(lines) + "\n")
        argv = [self.binary]
        for name, value in overrides.items():
            if name == "model_providers.forge":
                for field, setting in value.items():
                    argv.extend(["-c", f"{name}.{field}={json.dumps(setting)}"])
            else:
                argv.extend(["-c", f"{name}={json.dumps(value)}"])
        argv.append("app-server")
        session = CodexSession(workspace, home, key)
        try:
            session.transport = await self.factory(argv, workspace, child)
            session.reader = asyncio.create_task(session.read())
            await session.initialize(brief, env)
            session.owner = asyncio.create_task(session.finish())
            return session
        except BaseException:
            await session.interrupt()
            raise


class CodexSession(EventQueue):
    def __init__(self, workspace, home, key):
        super().__init__(key)
        self.workspace, self.home, self.policy = workspace, home, SafeFiles(workspace, None)
        self.transport = self.reader = self.owner = None
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence, self.thread_id, self.turn_id = 0, "", ""
        self.finished, self.closed = asyncio.Event(), False
        self.status, self.problem = "", None
        self.cleanup_lock = asyncio.Lock()
        self.cleanup_task: asyncio.Task | None = None

    async def call(self, method, params):
        assert self.transport is not None
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            await self.transport.send({"id": number, "method": method, "params": params})
            return await asyncio.wait_for(future, 20)
        except TimeoutError:
            raise failure("driver_protocol_timeout", "Codex app-server request timed out") from None
        finally:
            self.pending.pop(number, None)

    async def initialize(self, brief, env):
        assert self.transport is not None
        assert env.model_key is not None
        await self.call(
            "initialize",
            {
                "clientInfo": {"name": "forge-lane", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.transport.send({"method": "initialized", "params": {}})
        config = (await self.call("config/read", {})).get("config", {})
        provider = config.get("model_providers", {}).get("forge", {})
        if (
            config.get("features", {}).get("shell_tool") is not False
            or config.get("web_search") != "disabled"
            or config.get("mcp_servers", {})
            or config.get("apps", {}).get("_default", {}).get("enabled") is not False
            or config.get("model_provider") != "forge"
            or provider.get("base_url") != str(env.model_key.gateway_url)
            or provider.get("env_key") != "FORGE_MODEL_KEY"
        ):
            raise failure(
                "driver_policy_refused",
                "Codex effective configuration does not enforce lane isolation",
            )
        tools = [
            {
                "name": name,
                "description": "Confined approved workspace file operation",
                "inputSchema": {
                    "type": "object",
                    "properties": fields,
                    "required": list(fields),
                    "additionalProperties": False,
                },
            }
            for name, fields in FILE_SCHEMAS.items()
        ]
        response = await self.call(
            "thread/start",
            {
                "model": env.model,
                "modelProvider": "forge",
                "cwd": str(self.workspace),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "dynamicTools": tools,
            },
        )
        self.thread_id = response.get("thread", {}).get("id", "")
        if not isinstance(self.thread_id, str) or not self.thread_id:
            raise failure("driver_protocol_error", "Codex returned no thread identity")
        prompt = (
            brief.execution_brief_text
            + "\n\n"
            + env.resume_note
            + "\n\nUse only read_file, write_file and edit_file. Files:\n"
            + self.policy.inventory()
        )
        response = await self.call(
            "turn/start",
            {
                "threadId": self.thread_id,
                "model": env.model,
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "readOnly",
                    "access": {
                        "type": "restricted",
                        "includePlatformDefaults": True,
                        "readableRoots": [],
                    },
                },
                "input": [{"type": "text", "text": prompt}],
            },
        )
        self.turn_id = response.get("turn", {}).get("id", "")
        if not isinstance(self.turn_id, str) or not self.turn_id:
            raise failure("driver_protocol_error", "Codex returned no turn identity")

    async def read(self):
        assert self.transport is not None
        try:
            while (frame := await self.transport.receive()) is not None:
                if "method" in frame:
                    await self.notification(frame)
                else:
                    future = self.pending.get(frame.get("id"))
                    if future is None or future.done():
                        continue
                    if "error" in frame:
                        future.set_exception(
                            failure("driver_rpc_error", "Codex refused the request")
                        )
                    else:
                        future.set_result(frame.get("result", {}))
            if not self.closed and not self.finished.is_set():
                raise failure(
                    "driver_connection_closed", "Codex stream ended before turn completion"
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.problem = failure("driver_protocol_error", "Codex conversation failed")
            self.finished.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.problem)

    async def notification(self, frame):
        assert self.transport is not None
        method, params = frame["method"], frame.get("params", {})
        if "id" in frame:
            success, text = False, "Tool is outside the approved file capability"
            if (
                method == "item/tool/call"
                and params.get("threadId") == self.thread_id
                and params.get("turnId") == self.turn_id
            ):
                try:
                    text = self.policy.invoke(params.get("tool"), params.get("arguments", {}))
                    success = True
                except (LaneFailure, OSError, ValueError):
                    self.problem = failure(
                        "driver_policy_refused", "Codex requested a forbidden file operation"
                    )
            else:
                self.problem = failure(
                    "driver_policy_refused", "Codex requested an unapproved operation"
                )
            result = {"success": success, "contentItems": [{"type": "inputText", "text": text}]}
            if method != "item/tool/call":
                result = {"decision": "decline"}
            await self.transport.send({"id": frame["id"], "result": result})
            if self.problem:
                self.finished.set()
        elif params.get("threadId") == self.thread_id:
            if method == "turn/started":
                self.turn_id = params.get("turn", {}).get("id", "")
            elif method == "turn/completed" and params.get("turn", {}).get("id") == self.turn_id:
                self.status = params["turn"].get("status", "")
                self.finished.set()
            elif method == "item/agentMessage/delta":
                self.message(str(params.get("delta", "")))

    async def finish(self):
        await self.finished.wait()
        try:
            await self.cleanup()
        except Exception:
            self.emit(failure("driver_cleanup_failed", "Contrib process did not stop safely"))
        else:
            if self.problem:
                self.emit(self.problem)
            elif self.status == "completed":
                self.emit(DriverEvent("completed", terminal_reason="completed"))
            elif self.status:
                self.emit(DriverEvent("failed", terminal_reason="codex_turn_" + self.status))
        finally:
            self.emit(None)

    async def cleanup(self):
        async with self.cleanup_lock:
            if self.cleanup_task is None:
                self.closed = True
                self.cleanup_task = asyncio.create_task(self.dispose())
            cleanup = self.cleanup_task
        await asyncio.shield(cleanup)

    async def dispose(self):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(failure("driver_connection_closed", "Codex session closed"))
        try:
            if self.transport:
                await self.transport.close()
        finally:
            if self.reader:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            self.home.cleanup()

    async def interrupt(self):
        if not self.finished.is_set() and self.turn_id and self.transport:
            try:
                await asyncio.wait_for(
                    self.transport.send(
                        {
                            "id": -1,
                            "method": "turn/interrupt",
                            "params": {"threadId": self.thread_id, "turnId": self.turn_id},
                        }
                    ),
                    2,
                )
            except (TimeoutError, OSError):
                pass
        self.finished.set()
        await self.cleanup()
        if self.owner:
            await asyncio.shield(self.owner)

    async def steer(self, text):
        if self.finished.is_set() or not self.turn_id:
            raise failure("driver_capability_unavailable", "Codex has no active turn to steer")
        await self.call(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": self.turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )


def create_driver():
    require_enabled()
    return CodexDriver()
