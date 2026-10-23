"""Give the SDK's CLI an owned process group and a private config directory."""

import asyncio
import os
import signal
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..errors import ExitCode, LaneFailure


class OwnedCLI:
    def __init__(self, sdk: Any):
        if os.name != "posix":
            raise LaneFailure(
                ExitCode.DRIVER, "unsupported_platform", "Claude lane requires POSIX process groups"
            )
        self.directory = tempfile.TemporaryDirectory(prefix="forge-claude-")
        self.path = Path(self.directory.name)
        self.pid_file = self.path / "pid"
        self.cli = Path(sdk.__file__).parent / "_bundled" / "claude"
        if not self.cli.is_file():
            self.directory.cleanup()
            raise LaneFailure(
                ExitCode.DRIVER, "driver_unavailable", "Pinned SDK bundled CLI is missing"
            )
        self.wrapper = self.path / "claude-wrapper"
        script = (
            "import os, sys\n"
            "if sys.argv[1:] != ['--version']:\n"
            "    os.setsid()\n"
            f"    with open({str(self.pid_file)!r}, 'x') as receipt: receipt.write(str(os.getpid()))\n"
            f"os.execv({str(self.cli)!r}, [{str(self.cli)!r}, *sys.argv[1:]])\n"
        )
        self.wrapper.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(script)} "$@"\n'
        )
        self.wrapper.chmod(0o700)

    async def terminate(self) -> None:
        if not self.pid_file.exists():
            return
        try:
            pid = int(self.pid_file.read_text())
            if pid <= 1 or pid == os.getpgrp():
                raise ValueError("Invalid owned process group")
            os.killpg(pid, signal.SIGTERM)
            await asyncio.sleep(0.05)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        except (OSError, ValueError):
            raise LaneFailure(
                ExitCode.DRIVER,
                "driver_cleanup_failed",
                "Could not stop owned Claude process group",
            ) from None

    def cleanup(self) -> None:
        self.directory.cleanup()
