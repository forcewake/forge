"""Experimental Grok headless CLI; explicit subscription auth and strict file sandbox."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from forge_protocol import Brief

from ..driver import Capability, DriverEnv, DriverEvent
from ..drivers.claude_policy import PRIVATE_NAMES, PRIVATE_PARTS, redact
from ..errors import ExitCode, LaneFailure
from . import require_enabled
from .common import PRIVATE, EventQueue, Stdio, environment, failure


class GrokDriver:
    name = "grok"
    capabilities: frozenset[Capability] = frozenset({"interrupt"})
    requires_model_key = False

    def __init__(self, binary="grok", transport_factory=None):
        self.binary, self.factory = binary, transport_factory or Stdio.spawn

    async def start(self, brief: Brief, workspace: Path, env: DriverEnv) -> "GrokSession":
        auth = os.environ.get("FORGE_GROK_AUTH", "")
        try:
            bundle = json.loads(auth)
            if not isinstance(bundle, dict) or not bundle or len(auth.encode()) > 65536:
                raise ValueError("invalid bundle")
        except ValueError:
            raise LaneFailure(
                ExitCode.AUTHENTICATION,
                "grok_subscription_required",
                "Provide an explicit masked Grok OAuth bundle; ambient accounts and API keys are not used",
            ) from None
        home = tempfile.TemporaryDirectory(prefix="forge-grok-")
        root = Path(home.name)
        config = root / ".grok"
        config.mkdir(mode=0o700)
        (config / "auth.json").write_text(json.dumps(bundle))
        (config / "auth.json").chmod(0o600)
        denied = sorted(PRIVATE_PARTS | PRIVATE_NAMES | PRIVATE | {".env*", "*.pem", "*.key"})
        patterns = ["**/" + name + "/**" for name in denied] + ["**/" + name for name in denied]
        profile = (
            '[profiles.forge_contrib]\nextends = "strict"\nrestrict_network = true\ndeny = '
            + json.dumps(patterns)
            + "\n"
        )
        (config / "sandbox.toml").write_text(profile)
        (config / "config.toml").write_text(
            "[cli]\nauto_update = false\n[compat.claude]\nhooks = false\n[compat.cursor]\nhooks = false\n"
        )
        child = environment(root)
        child.update(GROK_HOME=str(config))
        argv = [
            self.binary,
            "--no-auto-update",
            "--no-alt-screen",
            "--no-plan",
            "--no-subagents",
            "--no-memory",
            "--disable-web-search",
            "--sandbox",
            "forge_contrib",
            "--tools",
            "Read,Write,Edit",
            "--max-turns",
            "200",
            "--model",
            env.model,
            "--output-format",
            "streaming-json",
        ]
        for tool in ("Read", "Write", "Edit"):
            argv.extend(["--allow", tool])
            for name in denied:
                argv.extend(["--deny", f"{tool}(**/{name})", "--deny", f"{tool}(**/{name}/**)"])
        for tool in ("Bash", "MCPTool", "WebFetch", "WebSearch", "Agent", "Task"):
            argv.extend(["--deny", tool])
        argv.extend(
            [
                "-p",
                brief.execution_brief_text
                + "\n\n"
                + env.resume_note
                + "\nEdit only this workspace; leave changes uncommitted.",
            ]
        )
        session = GrokSession(home, auth)
        try:
            session.transport = await self.factory(argv, workspace, child)
            session.owner = asyncio.create_task(session.run())
            return session
        except BaseException:
            await session.interrupt()
            raise


class GrokSession(EventQueue):
    def __init__(self, home, auth):
        super().__init__(auth)
        self.secrets = [auth]

        def leaves(value):
            if isinstance(value, str) and value:
                self.secrets.append(value)
            elif isinstance(value, dict):
                for child in value.values():
                    leaves(child)
            elif isinstance(value, list):
                for child in value:
                    leaves(child)

        leaves(json.loads(auth))
        self.home, self.transport, self.owner = home, None, None
        self.closed = False
        self.cleanup_lock = asyncio.Lock()
        self.cleanup_task: asyncio.Task | None = None

    def message(self, text):
        for secret in self.secrets:
            text = redact(text, secret)
        self.emit(DriverEvent("message", text=text))

    async def run(self):
        assert self.transport is not None
        terminal: DriverEvent | LaneFailure | None = None
        try:
            reason = None
            while (frame := await self.transport.receive()) is not None:
                event = frame.get("update", frame.get("params", {}).get("update", frame))
                if event.get("type") in ("text", "assistant_text"):
                    self.message(str(event.get("text", event.get("content", ""))))
                elif event.get("type") == "end":
                    if reason is not None:
                        raise failure(
                            "driver_protocol_error", "Grok emitted more than one terminal event"
                        )
                    reason = event.get("stopReason")
            process = getattr(self.transport, "process", None)
            if process is not None:
                code = await asyncio.wait_for(process.wait(), 5)
                if code != 0:
                    raise failure("driver_process_failed", "Grok process exited unsuccessfully")
            if reason not in ("end_turn", "stop", "completed"):
                raise failure(
                    "driver_completion_unconfirmed",
                    "Grok did not confirm a successful terminal result",
                )
            terminal = DriverEvent("completed", terminal_reason="completed")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            terminal = (
                exc
                if isinstance(exc, LaneFailure)
                else failure("driver_protocol_error", "Grok conversation failed")
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
            if self.transport:
                await self.transport.close()
        finally:
            self.home.cleanup()

    async def interrupt(self):
        if self.owner and self.owner is not asyncio.current_task():
            self.owner.cancel()
            await asyncio.gather(self.owner, return_exceptions=True)
        await self.cleanup()


def create_driver():
    require_enabled()
    return GrokDriver()
