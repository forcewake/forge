"""Snapshot the entire working tree without trusting or modifying its index."""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from forge_protocol import CheckpointEntry

from .errors import ExitCode, LaneFailure


@dataclass(frozen=True)
class Limits:
    blob_bytes: int = 16 * 1024 * 1024
    total_bytes: int = 64 * 1024 * 1024
    entries: int = 4096
    manifest_bytes: int = 1024 * 1024
    diff_bytes: int = 16 * 1024 * 1024


class Workspace:
    def __init__(self, path: Path, base_sha: str, limits: Limits = Limits()):
        self.path = path.resolve()
        self.base_sha = base_sha
        self.limits = limits
        self.write_allowed: Callable[[str], bool] | None = None

    async def git(self, *arguments: str, index: str | None = None) -> bytes:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        if index is not None:
            env["GIT_INDEX_FILE"] = index
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.path),
                *arguments,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                output = await asyncio.wait_for(self._output(process), timeout=60)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                raise
        except (OSError, asyncio.TimeoutError):
            raise LaneFailure(
                ExitCode.GIT, "git_failed", "Git could not complete the workspace operation"
            ) from None
        if process.returncode:
            raise LaneFailure(ExitCode.GIT, "git_failed", "Git refused the workspace operation")
        return output

    async def _output(self, process: asyncio.subprocess.Process) -> bytes:
        async def read(stream: asyncio.StreamReader, limit: int) -> bytes:
            chunks, total = [], 0
            while chunk := await stream.read(65536):
                total += len(chunk)
                if total > limit:
                    raise LaneFailure(
                        ExitCode.GIT, "git_output_too_large", "Git output exceeds byte limit"
                    )
                chunks.append(chunk)
            return b"".join(chunks)

        assert process.stdout is not None and process.stderr is not None
        readers = [
            asyncio.create_task(read(stream, limit))
            for stream, limit in (
                (process.stdout, max(self.limits.total_bytes, self.limits.diff_bytes)),
                (process.stderr, 1024 * 1024),
            )
        ]
        try:
            output, _ = await asyncio.gather(*readers)
            await process.wait()
            return output
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    async def validate(self) -> None:
        top = (await self.git("rev-parse", "--show-toplevel")).decode().strip()
        head = (await self.git("rev-parse", "HEAD")).decode().strip()
        if Path(top).resolve() != self.path or head != self.base_sha:
            raise LaneFailure(
                ExitCode.GIT, "wrong_checkout", "Workspace must be the dispatch base checkout"
            )
        dirty = await self.git(
            "status", "--porcelain", "--untracked-files=all", "--", ".", ":!.forge"
        )
        if dirty:
            raise LaneFailure(
                ExitCode.GIT, "dirty_checkout", "Lane requires a clean initial checkout"
            )

    @asynccontextmanager
    async def index(self) -> AsyncIterator[str]:
        with tempfile.TemporaryDirectory(prefix="forge-lane-index-") as directory:
            index = str(Path(directory) / "index")
            await self.git("read-tree", self.base_sha, index=index)
            await self.git("add", "-A", "--", ".", ":!.forge", index=index)
            if self.write_allowed is not None:
                changed = await self.git(
                    "diff",
                    "--cached",
                    "--no-renames",
                    "--name-only",
                    "-z",
                    self.base_sha,
                    index=index,
                )
                try:
                    paths = [
                        raw.decode("utf-8", errors="strict") for raw in changed.split(b"\0") if raw
                    ]
                    if any(not self.write_allowed(path) for path in paths):
                        raise ValueError("write scope refused")
                except ValueError:
                    raise LaneFailure(
                        ExitCode.APPROVAL,
                        "write_policy_refused",
                        "Candidate is outside the approved write scope",
                    ) from None
            yield index

    async def diff(self) -> bytes:
        async with self.index() as index:
            data = await self.git(
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-renames",
                "--no-ext-diff",
                self.base_sha,
                index=index,
            )
        if len(data) > self.limits.diff_bytes:
            raise LaneFailure(
                ExitCode.GIT, "candidate_too_large", "Candidate exceeds the byte limit"
            )
        return data

    async def capture(self) -> tuple[tuple[CheckpointEntry, ...], dict[str, bytes]]:
        async with self.index() as index:
            changes = await self.git(
                "diff",
                "--cached",
                "--name-status",
                "-z",
                "--no-renames",
                self.base_sha,
                index=index,
            )
            staged = await self.git("ls-files", "--stage", "-z", index=index)
            modes = {
                record.split(b"\t", 1)[1]: record.split(b" ", 1)[0].decode()
                for record in staged.split(b"\0")
                if record
            }
            return await self._entries(changes, modes, index)

    async def _entries(
        self, changes: bytes, modes: dict[bytes, str], index: str
    ) -> tuple[tuple[CheckpointEntry, ...], dict[str, bytes]]:
        records = changes.rstrip(b"\0").split(b"\0") if changes else []
        if len(records) // 2 > self.limits.entries:
            raise LaneFailure(
                ExitCode.CHECKPOINT, "checkpoint_too_large", "Too many checkpoint entries"
            )
        entries, blobs = [], {}
        total = 0
        for status, raw_path in zip(records[::2], records[1::2], strict=True):
            try:
                path = raw_path.decode("utf-8", errors="strict")
                states: dict[bytes, Literal["new", "modified", "deleted"]] = {
                    b"A": "new",
                    b"M": "modified",
                    b"D": "deleted",
                    b"T": "modified",
                }
                state = states[status]
                data = (
                    b"" if state == "deleted" else await self.git("show", f":{path}", index=index)
                )
                total += len(data)
                if len(data) > self.limits.blob_bytes or total > self.limits.total_bytes:
                    raise LaneFailure(
                        ExitCode.CHECKPOINT,
                        "checkpoint_too_large",
                        "Checkpoint exceeds byte limits",
                    )
                identity = None if state == "deleted" else sha256(data).hexdigest()
                entry = CheckpointEntry.model_validate(
                    dict(
                        path=path,
                        mode=modes.get(raw_path, "100644"),
                        sha256=identity,
                        state=state,
                        size_bytes=len(data),
                    )
                )
            except (ValueError, KeyError):
                raise LaneFailure(
                    ExitCode.CHECKPOINT, "unsupported_file", "Workspace file cannot be checkpointed"
                ) from None
            entries.append(entry)
            if identity is not None:
                blobs[identity] = data
        return tuple(sorted(entries, key=lambda entry: entry.path)), blobs
