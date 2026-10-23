"""Shared confined file tools, isolated child environments and process ownership."""

import asyncio
import json
import os
import stat
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge_protocol import Brief

from ..driver import DriverEnv, DriverEvent
from ..drivers.claude_policy import FilePolicy, redact
from ..drivers.copilot_transport import spawn_owned, stop_group
from ..errors import ExitCode, LaneFailure

MAX_FRAME = 4 * 1024 * 1024
PRIVATE = {".codex", ".grok", ".opencode", ".mcp.json", "opencode.json", "opencode.jsonc"}
FILE_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    "read_file": {"file_path": {"type": "string"}},
    "write_file": {
        "file_path": {"type": "string"},
        "content": {"type": "string", "maxLength": 1048576},
    },
    "edit_file": {
        "file_path": {"type": "string"},
        "old_string": {"type": "string", "minLength": 1},
        "new_string": {"type": "string", "maxLength": 1048576},
    },
}


def failure(kind: str, detail: str) -> LaneFailure:
    return LaneFailure(ExitCode.DRIVER, kind, detail)


def grant_key(brief: Brief, env: DriverEnv) -> str:
    grant = env.model_key
    if (
        grant is None
        or grant.expires_at <= datetime.now(timezone.utc)
        or grant.model != env.model
        or (grant.forge_run_id, grant.attempt, grant.generation)
        != (brief.forge_run_id, brief.attempt, brief.generation)
    ):
        raise LaneFailure(
            ExitCode.AUTHENTICATION,
            "model_key_required",
            "Contrib BYOK requires this attempt's gateway grant",
        )
    return grant.key


def environment(home: Path) -> dict[str, str]:
    keep = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}
    result = {key: value for key, value in os.environ.items() if key in keep}
    result.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_CACHE_HOME=str(home / "cache"),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    return result


class SafeFiles(FilePolicy):
    def permitted(self, name: str, arguments: dict[str, Any]) -> bool:
        raw = arguments.get("file_path", "")
        if not isinstance(raw, str):
            return False
        target = Path(raw) if Path(raw).is_absolute() else self.workspace / raw
        try:
            if any(
                part.casefold() in PRIVATE
                for path in (target, target.resolve())
                for part in path.parts
            ):
                return False
        except (OSError, RuntimeError):
            return False
        return super().permitted(name, arguments)

    def invoke(self, tool: str, arguments: dict) -> str:
        fields = FILE_SCHEMAS.get(tool)
        if fields is None:
            raise failure("driver_policy_refused", "Only confined file tools are available")
        if not isinstance(arguments, dict) or set(arguments) != set(fields):
            raise failure("driver_policy_refused", "Invalid file tool arguments")
        for field, limits in fields.items():
            value = arguments[field]
            if not isinstance(value, str) or not limits.get("minLength", 0) <= len(
                value
            ) <= limits.get("maxLength", 1048576):
                raise failure("driver_policy_refused", "Invalid file tool arguments")
        name = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit"}[tool]
        if not self.permitted(name, arguments):
            raise failure("driver_policy_refused", "Tool path is outside the approved workspace")
        path = Path(arguments["file_path"])
        path = path if path.is_absolute() else self.workspace / path
        if tool == "read_file" or tool == "edit_file":
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise failure("driver_policy_refused", "File tool requires a regular file")
                data = stream.read(1048577)
            if len(data) > 1048576:
                raise failure("driver_policy_refused", "File tool exceeds the byte limit")
            text = data.decode("utf-8")
            if tool == "read_file":
                return text
            if text.count(arguments["old_string"]) != 1:
                raise failure("driver_policy_refused", "Edit requires exactly one matching block")
            content = text.replace(arguments["old_string"], arguments["new_string"], 1)
        else:
            content = arguments["content"]
        if len(content.encode()) > 1048576:
            raise failure("driver_policy_refused", "File tool exceeds the byte limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode) & 0o777 if path.exists() else 0o644
        fd, temporary = tempfile.mkstemp(prefix=".forge-edit-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(content)
            if not self.permitted(name, arguments):
                raise failure("driver_policy_refused", "Tool path changed during write")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return "File updated"


class Stdio:
    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.lock = asyncio.Lock()

    @classmethod
    async def spawn(
        cls, argv: list[str], workspace: Path, env: dict[str, str], *, server: bool = False
    ) -> "Stdio":
        if os.name != "posix":
            raise failure("unsupported_platform", "Contrib drivers require POSIX process groups")
        try:
            process = await spawn_owned(
                *argv,
                cwd=workspace,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL if server else asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_FRAME,
                start_new_session=True,
            )
        except OSError:
            raise failure("driver_unavailable", "Contrib executable could not start") from None
        return cls(process)

    @classmethod
    async def server(cls, argv: list[str], workspace: Path, env: dict[str, str]) -> "Stdio":
        return await cls.spawn(argv, workspace, env, server=True)

    async def send(self, frame: dict) -> None:
        assert self.process.stdin is not None
        body = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(body) > MAX_FRAME:
            raise failure("driver_protocol_error", "Contrib frame exceeds the byte limit")
        async with self.lock:
            self.process.stdin.write(body)
            await self.process.stdin.drain()

    async def receive(self) -> dict | None:
        assert self.process.stdout is not None
        try:
            line = await self.process.stdout.readline()
            if not line:
                return None
            value = json.loads(line)
            if len(line) > MAX_FRAME or not isinstance(value, dict):
                raise ValueError("invalid frame")
            return value
        except (ValueError, UnicodeError):
            raise failure("driver_protocol_error", "Invalid contrib frame") from None

    async def close(self) -> None:
        await stop_group(self.process)
        if self.process.stdin:
            self.process.stdin.close()


class EventQueue:
    def __init__(self, secret: str):
        self.secret = secret
        self.queue: asyncio.Queue[DriverEvent | LaneFailure | None] = asyncio.Queue(maxsize=256)

    def emit(self, value: DriverEvent | LaneFailure | None) -> None:
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(value)

    async def events(self) -> AsyncIterator[DriverEvent]:
        cleanup_reported = False
        try:
            while (value := await self.queue.get()) is not None:
                if isinstance(value, LaneFailure):
                    cleanup_reported = value.classification == "driver_cleanup_failed"
                    raise value
                yield value
        finally:
            try:
                await self.interrupt()
            except Exception:
                if not cleanup_reported:
                    raise

    async def interrupt(self) -> None:
        raise NotImplementedError

    async def steer(self, text: str) -> None:
        raise failure("driver_capability_unavailable", "This contrib driver cannot steer")

    def message(self, text: str) -> None:
        self.emit(DriverEvent("message", text=redact(text, self.secret)))
