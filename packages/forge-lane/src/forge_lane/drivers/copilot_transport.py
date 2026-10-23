"""Pinned Copilot CLI stdio transport, private auth environment and group cleanup."""

import asyncio
import json
import os
import re
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..errors import ExitCode, LaneFailure
from .claude_policy import PRIVATE_NAMES, PRIVATE_PARTS, redact

COPILOT_VERSION = "1.0.86"
MAX_FRAME = 4 * 1024 * 1024


class Transport(Protocol):
    async def send(self, frame: dict[str, Any]) -> None: ...
    async def receive(self) -> dict[str, Any] | None: ...
    async def close(self) -> None: ...


def environment(config: Path, token: str) -> dict[str, str]:
    retained = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
    env = {name: value for name, value in os.environ.items() if name in retained}
    env.update(
        HOME=str(config),
        XDG_CONFIG_HOME=str(config),
        XDG_CACHE_HOME=str(config / "cache"),
        COPILOT_HOME=str(config / "copilot"),
        COPILOT_AUTO_UPDATE="false",
        COPILOT_GITHUB_TOKEN=token,
        COPILOT_ALLOW_ALL="false",
        GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS="false",
    )
    return env


async def stop_group(process: asyncio.subprocess.Process) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except OSError:
            raise LaneFailure(
                ExitCode.DRIVER, "driver_cleanup_failed", "Could not stop Copilot process group"
            ) from None
        await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        raise LaneFailure(
            ExitCode.DRIVER, "driver_cleanup_failed", "Copilot process did not exit"
        ) from None


async def spawn_owned(*arguments: str, **kwargs: Any) -> asyncio.subprocess.Process:
    creation = asyncio.create_task(asyncio.create_subprocess_exec(*arguments, **kwargs))
    try:
        return await asyncio.shield(creation)
    except asyncio.CancelledError:
        try:
            process = await creation
        except OSError:
            raise asyncio.CancelledError from None
        await stop_group(process)
        raise


class StdioTransport:
    def __init__(
        self, process: asyncio.subprocess.Process, token: str, emit: Callable[[str], None]
    ):
        self.process, self.token, self.emit = process, token, emit
        self.lock = asyncio.Lock()
        self.stderr_task = asyncio.create_task(self.stderr())
        self.closed = False

    @classmethod
    async def spawn(
        cls,
        binary: str,
        workspace: Path,
        config: Path,
        token: str,
        model: str,
        emit: Callable[[str], None],
    ) -> "StdioTransport":
        if os.name != "posix":
            raise LaneFailure(
                ExitCode.DRIVER,
                "unsupported_platform",
                "Copilot lane requires POSIX process groups",
            )
        await cls.verify_version(binary, config)
        argv = [
            binary,
            "--acp",
            "--stdio",
            f"--model={model}",
            "--available-tools=view,edit,create,apply_patch",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--no-ask-user",
            "--disallow-temp-dir",
        ]
        for tool in ("shell", "url", "memory"):
            argv.append(f"--deny-tool={tool}")
        for path in sorted(
            PRIVATE_PARTS | PRIVATE_NAMES | {".env", ".env.local", ".env.production"}
        ):
            for action in ("read", "write"):
                argv.append(f"--deny-tool={action}({path})")
        try:
            process = await spawn_owned(
                *argv,
                cwd=workspace,
                env=environment(config, token),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_FRAME,
                start_new_session=True,
            )
        except OSError:
            raise LaneFailure(
                ExitCode.DRIVER, "driver_unavailable", "Pinned Copilot CLI could not start"
            ) from None
        return cls(process, token, emit)

    @staticmethod
    async def verify_version(binary: str, config: Path) -> None:
        process = None
        try:
            process = await spawn_owned(
                binary,
                "--version",
                env=environment(config, ""),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=65536,
                start_new_session=True,
            )
            assert process.stdout is not None
            output = await asyncio.wait_for(process.stdout.read(65536), timeout=10)
            await asyncio.wait_for(process.wait(), timeout=2)
            if process.returncode or not re.search(
                rf"\b{re.escape(COPILOT_VERSION)}\b", output.decode("utf-8", errors="strict")
            ):
                raise LaneFailure(
                    ExitCode.DRIVER,
                    "driver_version_mismatch",
                    "Copilot CLI does not match the pinned version",
                )
        except (OSError, ValueError, asyncio.TimeoutError):
            raise LaneFailure(
                ExitCode.DRIVER, "driver_unavailable", "Could not verify pinned Copilot CLI"
            ) from None
        finally:
            if process is not None:
                await stop_group(process)

    async def stderr(self) -> None:
        assert self.process.stderr is not None
        buffer = b""
        discarding = False
        while chunk := await self.process.stderr.read(4096):
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not discarding:
                    self.emit(redact(line.decode("utf-8", errors="replace"), self.token))
                discarding = False
            if len(buffer) > 8192:
                buffer, discarding = b"", True
                self.emit("[oversized stderr omitted]")
        if buffer and not discarding:
            self.emit(redact(buffer.decode("utf-8", errors="replace"), self.token))

    async def send(self, frame: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        data = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_FRAME:
            raise LaneFailure(
                ExitCode.DRIVER, "driver_frame_too_large", "ACP frame exceeds byte limit"
            )
        async with self.lock:
            try:
                self.process.stdin.write(data)
                await self.process.stdin.drain()
            except (OSError, ConnectionError):
                raise LaneFailure(
                    ExitCode.DRIVER, "driver_connection_failed", "Copilot input stream closed"
                ) from None

    async def receive(self) -> dict[str, Any] | None:
        assert self.process.stdout is not None
        try:
            line = await self.process.stdout.readline()
            if not line:
                return None
            if len(line) > MAX_FRAME:
                raise ValueError("oversized frame")
            frame = json.loads(line.decode("utf-8", errors="strict"))
            if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                raise ValueError("invalid frame")
            return frame
        except (ValueError, UnicodeError):
            raise LaneFailure(
                ExitCode.DRIVER, "driver_protocol_error", "Invalid Copilot ACP frame"
            ) from None

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await stop_group(self.process)
        finally:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
            if self.process.stdin is not None:
                self.process.stdin.close()
