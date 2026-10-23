"""A single-turn ACP generation with cancellation provenance and no auth fallback."""

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from forge_protocol import Brief

from ..driver import Capability, DriverEnv, DriverEvent
from ..errors import ExitCode, LaneFailure
from .claude_policy import FilePolicy, redact
from .copilot_transport import StdioTransport, Transport


class CopilotCLIDriver:
    name = "copilot_cli"
    capabilities: frozenset[Capability] = frozenset({"interrupt"})
    requires_model_key = False

    def __init__(self, binary: str = "copilot", transport_factory: Any = None):
        self.binary = binary
        self.factory = transport_factory or StdioTransport.spawn

    async def start(self, brief: Brief, workspace: Path, env: DriverEnv) -> "CopilotSession":
        token = os.environ.get("COPILOT_GITHUB_TOKEN", "")
        if not token:
            raise LaneFailure(
                ExitCode.AUTHENTICATION,
                "copilot_token_required",
                "Provide an explicit CI Copilot token; repository tokens and stored logins are not used",
            )
        config = tempfile.TemporaryDirectory(prefix="forge-copilot-")
        session = CopilotSession(workspace, token, config)
        try:
            session.transport = await self.factory(
                self.binary,
                workspace,
                Path(config.name),
                token,
                env.model,
                lambda text: session.emit(DriverEvent("message", text=text)),
            )
            session.reader = asyncio.create_task(session.read())
            await session.initialize()
            prompt = (
                "Approved implementation request:\n" + brief.brief_text + "\n\n" + env.resume_note
            )
            prompt += "\n\nAvailable workspace files:\n" + session.policy.inventory()
            prompt += (
                "\nUse file tools only. Shell, MCP, authority files and repository APIs are denied."
            )
            session.turn = asyncio.create_task(session.run_turn(prompt))
            return session
        except BaseException:
            await session.cleanup()
            raise


class CopilotSession:
    def __init__(self, workspace: Path, token: str, config: tempfile.TemporaryDirectory):
        self.workspace, self.token, self.config = workspace, token, config
        self.policy = FilePolicy(workspace, None)
        self.transport: Transport | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.sequence = 0
        self.session_id = ""
        self.queue: asyncio.Queue[DriverEvent | LaneFailure | None] = asyncio.Queue(maxsize=256)
        self.reader: asyncio.Task | None = None
        self.turn: asyncio.Task | None = None
        self.cancelled = False
        self.closed = False
        self.policy_refused = False
        self.read_failure: LaneFailure | None = None
        self.cleanup_failure: LaneFailure | None = None
        self.early_updates: list[dict[str, Any]] = []
        self.cleanup_lock = asyncio.Lock()

    def emit(self, value: DriverEvent | LaneFailure | None) -> None:
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(value)

    async def send(self, frame: dict[str, Any]) -> None:
        assert self.transport is not None
        await self.transport.send({"jsonrpc": "2.0", **frame})

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = 20
    ) -> dict[str, Any]:
        if self.read_failure is not None:
            raise self.read_failure
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.send({"id": identity, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise LaneFailure(
                ExitCode.DRIVER, "driver_protocol_timeout", "Copilot ACP request timed out"
            ) from None
        finally:
            self.pending.pop(identity, None)
            if not future.done():
                future.cancel()

    async def initialize(self) -> None:
        result = await self.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "forge-lane", "version": "0.1.0"},
            },
        )
        if type(result.get("protocolVersion")) is not int or result["protocolVersion"] != 1:
            raise LaneFailure(
                ExitCode.DRIVER, "driver_protocol_error", "Copilot did not negotiate ACP v1"
            )
        result = await self.call("session/new", {"cwd": str(self.workspace), "mcpServers": []})
        identity = result.get("sessionId")
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise LaneFailure(
                ExitCode.DRIVER, "driver_protocol_error", "Copilot returned no session identity"
            )
        self.session_id = identity
        for frame in self.early_updates:
            await self.server_message(frame)
        self.early_updates.clear()

    async def read(self) -> None:
        try:
            assert self.transport is not None
            while (frame := await self.transport.receive()) is not None:
                if "method" in frame:
                    await self.server_message(frame)
                else:
                    identity = frame.get("id")
                    if type(identity) is not int or identity not in self.pending:
                        raise LaneFailure(
                            ExitCode.DRIVER,
                            "driver_protocol_error",
                            "Unexpected Copilot response identity",
                        )
                    future = self.pending[identity]
                    if "error" in frame:
                        future.set_exception(
                            LaneFailure(
                                ExitCode.DRIVER,
                                "driver_rpc_error",
                                "Copilot refused the ACP request",
                            )
                        )
                    elif isinstance(frame.get("result"), dict):
                        future.set_result(frame["result"])
                    else:
                        raise LaneFailure(
                            ExitCode.DRIVER, "driver_protocol_error", "Invalid Copilot response"
                        )
            raise LaneFailure(
                ExitCode.DRIVER,
                "driver_process_error",
                "Copilot exited before completing the protocol",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = (
                exc
                if isinstance(exc, LaneFailure)
                else LaneFailure(
                    ExitCode.DRIVER, "driver_protocol_error", "Copilot protocol failed"
                )
            )
            self.read_failure = failure
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)

    async def server_message(self, frame: dict[str, Any]) -> None:
        method, params = frame["method"], frame.get("params", {})
        if not self.session_id and method == "session/update" and "id" not in frame:
            if len(self.early_updates) >= 128:
                raise LaneFailure(
                    ExitCode.DRIVER, "driver_protocol_error", "Too many startup updates"
                )
            self.early_updates.append(frame)
            return
        if not isinstance(params, dict) or params.get("sessionId") != self.session_id:
            raise LaneFailure(
                ExitCode.DRIVER, "driver_protocol_error", "Copilot message has the wrong session"
            )
        if method == "session/request_permission" and "id" in frame:
            await self.permission(frame["id"], params)
        elif method == "session/update" and "id" not in frame:
            update = params.get("update", {})
            if not isinstance(update, dict):
                raise LaneFailure(
                    ExitCode.DRIVER, "driver_protocol_error", "Invalid Copilot update"
                )
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    self.emit(DriverEvent("message", text=redact(content["text"], self.token)))
        elif "id" in frame:
            self.policy_refused = True
            await self.send(
                {
                    "id": frame["id"],
                    "error": {"code": -32601, "message": "Client capability is not available"},
                }
            )

    async def permission(self, identity: Any, params: dict[str, Any]) -> None:
        tool = params.get("toolCall", {})
        kind = tool.get("kind") if isinstance(tool, dict) else None
        locations = tool.get("locations", []) if isinstance(tool, dict) else []
        operation = "Read" if kind == "read" else "Write" if kind == "edit" else ""
        raw = tool.get("rawInput") if isinstance(tool, dict) else None
        raw_paths = self.permission_paths(raw)
        permitted = bool(
            operation
            and raw_paths
            and all(self.policy.permitted(operation, {"file_path": path}) for path in raw_paths)
        )
        if permitted and locations:
            actual = {(self.workspace / path).resolve() for path in raw_paths}
            permitted = isinstance(locations, list) and all(
                isinstance(location, dict)
                and isinstance(location.get("path"), str)
                and (self.workspace / location["path"]).resolve() in actual
                for location in locations
            )
        options = params.get("options", [])
        choice = (
            next(
                (
                    option.get("optionId")
                    for option in options
                    if isinstance(option, dict)
                    and option.get("kind") == "allow_once"
                    and isinstance(option.get("optionId"), str)
                ),
                None,
            )
            if isinstance(options, list)
            else None
        )
        if permitted and choice is not None:
            outcome = {"outcome": "selected", "optionId": choice}
        else:
            self.policy_refused = True
            outcome = {"outcome": "cancelled"}
        await self.send({"id": identity, "result": {"outcome": outcome}})

    def permission_paths(self, raw: Any) -> list[str]:
        if not isinstance(raw, dict) or any(
            key in raw for key in ("command", "url", "script", "executable")
        ):
            return []
        path = raw.get("path", raw.get("file_path"))
        if isinstance(path, str):
            return [path]
        patch = raw.get("input", raw.get("patch"))
        if (
            not isinstance(patch, str)
            or not patch.startswith("*** Begin Patch\n")
            or not patch.rstrip().endswith("*** End Patch")
        ):
            return []
        return re.findall(
            r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.MULTILINE
        )

    async def run_turn(self, prompt: str) -> None:
        terminal: DriverEvent | LaneFailure | None = None
        try:
            result = await self.call(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": [{"type": "text", "text": prompt}]},
                timeout=None,
            )
            if self.cancelled:
                return
            if self.policy_refused:
                raise LaneFailure(
                    ExitCode.DRIVER,
                    "tool_policy_refused",
                    "Copilot requested an unsupported tool or path",
                )
            reason = result.get("stopReason")
            if reason != "end_turn":
                kinds = {
                    "cancelled": "driver_cancelled",
                    "refusal": "driver_refused",
                    "max_tokens": "driver_token_limit",
                    "max_turns": "driver_turn_limit",
                }
                raise LaneFailure(
                    ExitCode.DRIVER,
                    kinds.get(reason, "driver_protocol_error")
                    if isinstance(reason, str)
                    else "driver_protocol_error",
                    "Copilot did not complete the approved request",
                )
            terminal = DriverEvent(
                "completed", terminal_reason="Copilot completed; usage unmetered"
            )
        except asyncio.CancelledError:
            pass
        except LaneFailure as exc:
            terminal = exc
        except Exception:
            terminal = LaneFailure(ExitCode.DRIVER, "driver_protocol_error", "Copilot turn failed")
        finally:
            try:
                await self.cleanup()
            except LaneFailure as exc:
                terminal = exc
            if terminal is not None and not self.cancelled:
                self.emit(terminal)
            self.emit(None)

    async def cleanup(self) -> None:
        async with self.cleanup_lock:
            if self.closed:
                if self.cleanup_failure is not None:
                    raise self.cleanup_failure
                return
            self.closed = True
            try:
                if self.transport is not None:
                    await self.transport.close()
            except Exception:
                self.cleanup_failure = LaneFailure(
                    ExitCode.DRIVER, "driver_cleanup_failed", "Copilot cleanup failed"
                )
                raise self.cleanup_failure from None
            finally:
                if self.reader is not None:
                    self.reader.cancel()
                    await asyncio.gather(self.reader, return_exceptions=True)
                self.config.cleanup()

    async def events(self):
        while (event := await self.queue.get()) is not None:
            if isinstance(event, LaneFailure):
                raise event
            yield event

    async def interrupt(self) -> None:
        self.cancelled = True
        if self.transport is not None and not self.closed and self.session_id:
            try:
                await asyncio.wait_for(
                    self.send(
                        {"method": "session/cancel", "params": {"sessionId": self.session_id}}
                    ),
                    timeout=0.5,
                )
            except Exception:
                pass
        if self.turn is not None and not self.turn.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.turn), timeout=0.25)
            except asyncio.TimeoutError:
                self.turn.cancel()
                await self.turn
        await self.cleanup()

    async def steer(self, text: str) -> None:
        raise LaneFailure(
            ExitCode.DRIVER, "steer_unsupported", "Copilot ACP does not support mid-turn steering"
        )


def create_driver() -> CopilotCLIDriver:
    return CopilotCLIDriver()
