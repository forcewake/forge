"""Claude Agent SDK lifecycle with frozen file and public HTTPS MCP authority."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from forge_protocol import Brief, Usage

from ..driver import Capability, DriverEnv, DriverEvent
from ..errors import ExitCode, LaneFailure
from ..mcp import (
    for_claude,
    public_servers,
    unavailable,
    verify_native_status,
    verify_public_servers,
)
from .claude_policy import DENIED, TOOLS, FilePolicy, child_environment, redact
from .claude_process import OwnedCLI


class ClaudeCodeDriver:
    name = "claude_code"
    capabilities: frozenset[Capability] = frozenset({"interrupt", "steer"})
    requires_model_key = True

    def __init__(self, sdk: Any = None, *, mcp_transport: httpx.AsyncBaseTransport | None = None):
        self.sdk = sdk
        self.mcp_transport = mcp_transport
        self.prepared: tuple[str, Path] | None = None

    async def prepare(self, brief: Brief, workspace: Path) -> None:
        policy = brief.execution_policy
        servers = public_servers(policy)
        for_claude(policy)
        if servers and self.prepared != (brief.approved_digest, workspace.resolve()):
            if self.sdk is None:
                try:
                    import claude_agent_sdk

                    self.sdk = claude_agent_sdk
                except ImportError:
                    raise unavailable() from None
            if not callable(getattr(self.sdk.ClaudeSDKClient, "get_mcp_status", None)):
                raise unavailable()
            await verify_public_servers(policy, transport=self.mcp_transport)
        self.prepared = (brief.approved_digest, workspace.resolve())

    async def start(self, brief: Brief, workspace: Path, env: DriverEnv) -> "ClaudeSession":
        await self.prepare(brief, workspace)
        grant = env.model_key
        if (
            grant is None
            or grant.model != env.model
            or grant.expires_at <= datetime.now(timezone.utc)
        ):
            raise LaneFailure(
                ExitCode.AUTHENTICATION,
                "model_key_required",
                "Claude requires this attempt's gateway grant",
            )
        try:
            sdk = self.sdk
            if not sdk:
                import claude_agent_sdk

                sdk = claude_agent_sdk
        except ImportError:
            raise LaneFailure(
                ExitCode.DRIVER, "driver_unavailable", "Install forge-lane[claude] for Claude Code"
            ) from None
        session = ClaudeSession(sdk, brief, workspace, env)
        try:
            await session.ready.wait()
        except BaseException:
            await session.interrupt()
            raise
        if session.failure is not None:
            raise session.failure
        return session


class ClaudeSession:
    def __init__(self, sdk: Any, brief: Brief, workspace: Path, env: DriverEnv):
        self.sdk, self.brief, self.workspace, self.env = sdk, brief, workspace, env
        self.secret = env.model_key.key if env.model_key is not None else ""
        self.owned = OwnedCLI(sdk)
        spec = brief.frozen_spec
        self.policy = FilePolicy(
            workspace,
            sdk,
            scope=spec.write_policy,
            execution=brief.execution_policy,
        )
        self.ready, self.done, self.stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.queue: asyncio.Queue[DriverEvent | LaneFailure | None] = asyncio.Queue(maxsize=256)
        self.failure: LaneFailure | None = None
        self.client: Any = None
        self.lock = asyncio.Lock()
        self.issued, self.received = 1, 0
        self.finishing = False
        self.input_tokens, self.output_tokens = 0, 0
        self.seen_results: set[str] = set()
        self.usage: Usage | None = None
        self.usage_unknown = False
        self.task = asyncio.create_task(self._owner())

    def _emit(self, value: DriverEvent | LaneFailure | None) -> None:
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(value)

    def _options(self) -> Any:
        grant = self.env.model_key
        assert grant is not None
        return self.sdk.ClaudeAgentOptions(
            cwd=str(self.workspace),
            cli_path=str(self.owned.wrapper),
            model=self.env.model,
            tools=list(TOOLS),
            allowed_tools=[],
            disallowed_tools=list(DENIED),
            permission_mode="default",
            can_use_tool=self.policy.permission,
            hooks={"PreToolUse": [self.sdk.HookMatcher(hooks=[self.policy.pre_tool])]},
            mcp_servers=for_claude(self.brief.execution_policy),
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            plugins=[],
            max_turns=200,
            max_buffer_size=4 * 1024 * 1024,
            env=child_environment(self.owned.path, str(grant.gateway_url), grant.key),
            stderr=lambda line: self._emit(DriverEvent("message", text=redact(line, grant.key))),
        )

    async def _owner(self) -> None:
        terminal: DriverEvent | LaneFailure | None = None
        receive = stop = None
        try:
            self.client = self.sdk.ClaudeSDKClient(options=self._options())
            await self.client.connect()
            if self.brief.execution_policy.mcp_servers:
                verify_native_status(
                    self.brief.execution_policy, await self.client.get_mcp_status()
                )
            prompt = "Approved implementation request:\n" + self.brief.execution_brief_text
            prompt += (
                "\n\n"
                + self.env.resume_note
                + "\n\nAvailable workspace files:\n"
                + self.policy.inventory()
            )
            prompt += "\nOnly confined file tools and the explicitly approved MCP tools are available. Shell, repository writes and authority files are denied."
            await self.client.query(prompt)
            self.ready.set()
            receive = asyncio.create_task(self._receive())
            stop = asyncio.create_task(self.stopping.wait())
            done, _ = await asyncio.wait([receive, stop], return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                await self._interrupt_request()
            else:
                terminal = receive.result()
        except asyncio.CancelledError:
            pass
        except LaneFailure as exc:
            terminal = self.failure = exc
        except Exception as exc:
            terminal = self.failure = self._classify(exc)
        finally:
            for task in (receive, stop):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (receive, stop) if task is not None), return_exceptions=True
            )
            try:
                await self.owned.terminate()
                if self.client is not None:
                    await self.client.disconnect()
            except Exception:
                terminal = self.failure = LaneFailure(
                    ExitCode.DRIVER, "driver_cleanup_failed", "Claude cleanup failed"
                )
            finally:
                self.owned.cleanup()
                self.ready.set()
                if terminal is not None and not self.stopping.is_set():
                    self._emit(terminal)
                self._emit(None)
                self.done.set()

    async def _interrupt_request(self) -> None:
        request = asyncio.create_task(self.client.interrupt())
        try:
            await asyncio.wait_for(request, timeout=1.0)
        except Exception:
            pass

    async def _receive(self) -> DriverEvent:
        async for message in self.client.receive_messages():
            if isinstance(message, self.sdk.ResultMessage):
                if message.origin is not None and message.origin.get("kind") != "human":
                    continue
                async with self.lock:
                    identity = getattr(message, "uuid", None)
                    if identity and identity in self.seen_results:
                        continue
                    if identity:
                        self.seen_results.add(identity)
                    self.received += 1
                    self._record_usage(message)
                    if message.is_error or message.subtype != "success":
                        raise LaneFailure(
                            ExitCode.DRIVER,
                            "tool_policy_refused" if self.policy.denials else "driver_result_error",
                            "Claude could not complete the approved request",
                        )
                    if message.terminal_reason not in (None, "completed"):
                        raise LaneFailure(
                            ExitCode.DRIVER,
                            "driver_result_incomplete",
                            "Claude did not report a complete terminal turn",
                        )
                    if self.received == self.issued:
                        self.finishing = True
                        return DriverEvent(
                            "completed", terminal_reason="Claude completed", usage=self.usage
                        )
            else:
                for block in getattr(message, "content", ()):
                    self._trace_block(block)
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        self._emit(DriverEvent("message", text=redact(text, self.secret)))
        raise LaneFailure(
            ExitCode.DRIVER, "driver_stream_ended", "Claude stream ended without a result"
        )

    def _trace_block(self, block: Any) -> None:
        name, arguments = getattr(block, "name", None), getattr(block, "input", None)
        if isinstance(name, str) and isinstance(arguments, dict):
            hint = arguments.get("file_path", "")
            self._emit(
                DriverEvent(
                    "tool_start",
                    text=redact(hint if isinstance(hint, str) else "", self.secret),
                    tool_name=redact(name, self.secret),
                )
            )
        elif isinstance(getattr(block, "tool_use_id", None), str):
            content = getattr(block, "content", "")
            if isinstance(content, list):
                content = " ".join(
                    item["text"]
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
            self._emit(
                DriverEvent(
                    "tool_result",
                    text=redact(content if isinstance(content, str) else "", self.secret),
                    is_error=getattr(block, "is_error", False) is True,
                )
            )

    def _record_usage(self, message: Any) -> None:
        usage = message.usage
        if usage is None:
            self.usage_unknown = True
            if self.usage is None:
                return
            raise self._invalid_usage()
        counters = ("input_tokens", "output_tokens")
        caches = ("cache_creation_input_tokens", "cache_read_input_tokens")
        if (
            self.usage_unknown
            or not isinstance(usage, dict)
            or any(type(usage.get(key)) is not int or usage[key] < 0 for key in counters)
            or any(type(usage.get(key, 0)) is not int or usage.get(key, 0) < 0 for key in caches)
        ):
            raise self._invalid_usage()
        cost = message.total_cost_usd
        if cost is not None:
            try:
                if type(cost) not in (int, float, Decimal):
                    raise ValueError("invalid cost type")
                cost = Decimal(str(cost))
                if (
                    not cost.is_finite()
                    or cost < 0
                    or (
                        self.usage is not None
                        and self.usage.cost_usd is not None
                        and cost < self.usage.cost_usd
                    )
                ):
                    raise ValueError("invalid cumulative cost")
            except (ValueError, ArithmeticError):
                raise self._invalid_usage() from None
        self.input_tokens += usage["input_tokens"] + sum(usage.get(key, 0) for key in caches)
        self.output_tokens += usage["output_tokens"]
        self.usage = Usage(
            input_tokens=self.input_tokens, output_tokens=self.output_tokens, cost_usd=cost
        )
        self._emit(DriverEvent("message", usage=self.usage))

    @staticmethod
    def _invalid_usage() -> LaneFailure:
        return LaneFailure(
            ExitCode.DRIVER,
            "driver_usage_invalid",
            "Claude usage does not establish complete nonnegative counters and finite cost",
        )

    def _classify(self, exc: Exception) -> LaneFailure:
        name = type(exc).__name__
        kind = {
            "CLINotFoundError": "driver_unavailable",
            "CLIConnectionError": "driver_connection_failed",
            "ProcessError": "driver_process_error",
            "CLIJSONDecodeError": "driver_protocol_error",
            "MessageParseError": "driver_protocol_error",
        }.get(name, "driver_error")
        return LaneFailure(ExitCode.DRIVER, kind, "Claude SDK execution failed")

    async def events(self) -> AsyncIterator[DriverEvent]:
        while (event := await self.queue.get()) is not None:
            if isinstance(event, LaneFailure):
                raise event
            yield event

    async def interrupt(self) -> None:
        if self.done.is_set():
            if self.failure is not None and self.failure.classification == "driver_cleanup_failed":
                raise self.failure
            return
        self.stopping.set()
        if not self.ready.is_set():
            await self.owned.terminate()
            self.task.cancel()
        await asyncio.shield(self.task)
        if self.failure is not None and self.failure.classification == "driver_cleanup_failed":
            raise self.failure

    async def steer(self, text: str) -> None:
        async with self.lock:
            if self.done.is_set() or self.stopping.is_set() or self.finishing:
                raise LaneFailure(
                    ExitCode.DRIVER, "driver_stopped", "Cannot steer a stopped Claude session"
                )
            self.issued += 1
            await self.client.query(
                "Operator guidance; approval and permissions are unchanged:\n" + text
            )


def create_driver() -> ClaudeCodeDriver:
    return ClaudeCodeDriver()
