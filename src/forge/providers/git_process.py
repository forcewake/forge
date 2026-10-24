"""Bounded Git object operations in an isolated directory without ambient credentials."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import shutil


class GitFailure(Exception):
    def __init__(self, reason: str, *, stale: bool = False):
        self.reason, self.stale = reason, stale
        super().__init__(reason)


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: bytes
    stderr: bytes


class GitProcess:
    def __init__(
        self,
        root: Path,
        authorization: str,
        author: str,
        email: str,
        stamp: int,
        *,
        ca_path: Path | None = None,
    ):
        executable = shutil.which("git")
        if executable is None:
            raise GitFailure("git_unavailable")
        self.root, self.executable = root, executable
        config = {
            "http.extraHeader": "Authorization: " + authorization,
            "http.followRedirects": "false",
            "http.sslVerify": "true",
            "credential.helper": "",
            "core.hooksPath": "/dev/null",
            "core.autocrlf": "false",
            "core.fileMode": "true",
            "protocol.file.allow": "never",
            "protocol.ext.allow": "never",
            "protocol.ssh.allow": "never",
            "submodule.recurse": "false",
            "gc.auto": "0",
            "maintenance.auto": "false",
            "fetch.fsckObjects": "true",
            "transfer.fsckObjects": "true",
        }
        if ca_path is not None:
            if not ca_path.is_absolute() or not ca_path.is_file():
                raise GitFailure("git_trusted_ca_unavailable")
            config["http.sslCAInfo"] = str(ca_path)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root),
            "LANG": "C",
            "TZ": "UTC",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_AUTHOR_NAME": author,
            "GIT_COMMITTER_NAME": author,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE": f"{stamp} +0000",
            "GIT_COMMITTER_DATE": f"{stamp} +0000",
            "GIT_CONFIG_COUNT": str(len(config)),
        }
        for number, (key, value) in enumerate(config.items()):
            self.env[f"GIT_CONFIG_KEY_{number}"] = key
            self.env[f"GIT_CONFIG_VALUE_{number}"] = value

    async def run(self, *args: str, data: bytes | None = None, timeout: float = 15) -> GitResult:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            cwd=self.root,
            env=self.env,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        tasks: list[asyncio.Task[object]] = []
        try:
            assert (
                process.stdout is not None
                and process.stderr is not None
                and process.stdin is not None
            )

            async def feed() -> None:
                assert process.stdin is not None
                if data:
                    process.stdin.write(data)
                    await process.stdin.drain()
                process.stdin.close()

            stdout = asyncio.create_task(_read(process.stdout))
            stderr = asyncio.create_task(_read(process.stderr))
            writer = asyncio.create_task(feed())
            monitor = asyncio.create_task(_disk_bound(self.root))
            tasks.extend((stdout, stderr, writer, monitor))
            async with asyncio.timeout(timeout):
                finished = asyncio.create_task(process.wait())
                tasks.append(finished)

                async def collect() -> GitResult:
                    code, _, output, errors = await asyncio.gather(finished, writer, stdout, stderr)
                    return GitResult(code, output, errors)

                operation = asyncio.create_task(collect())
                tasks.append(operation)
                done, _ = await asyncio.wait(
                    (operation, monitor), return_when=asyncio.FIRST_COMPLETED
                )
                if monitor in done:
                    await monitor
                return await operation
        except (TimeoutError, OSError, BrokenPipeError, ConnectionResetError):
            raise GitFailure("git_transport_unavailable") from None
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.returncode is None:
                await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _read(stream: asyncio.StreamReader) -> bytes:
    result = bytearray()
    while chunk := await stream.read(8192):
        if len(result) + len(chunk) > 1024 * 1024:
            raise GitFailure("git_output_too_large")
        result.extend(chunk)
    return bytes(result)


async def _disk_bound(root: Path) -> None:
    while True:
        total = 0
        for directory, _, names in os.walk(root):
            for name in names:
                try:
                    total += (Path(directory) / name).stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue
                if total > 128 * 1024 * 1024:
                    raise GitFailure("git_workspace_too_large")
        await asyncio.sleep(0.05)
