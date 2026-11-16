"""A real ``forge worker`` OS process. SIGKILL is process death, not task cancellation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
KILL_WAIT_SECONDS = 15.0


class OSWorker:
    def __init__(self, name: str, env: dict[str, str], log_dir: Path, config: Path) -> None:
        self.name = name
        self.env = env
        self.config = config
        self.log_path = log_dir / f"worker-{name}.log"
        self.proc: subprocess.Popen | None = None
        self.exit_code: int | None = None

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError(f"worker {self.name} already started")
        log_handle = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import asyncio, sys, traceback\n"
                "from forge.runtime.cli import execute, parser\n"
                "try:\n"
                " raise SystemExit(asyncio.run(execute(parser().parse_args(sys.argv[1:]))))\n"
                "except SystemExit:\n raise\n"
                "except Exception:\n traceback.print_exc()\n raise",
                "--config",
                str(self.config),
                "worker",
            ],
            cwd=str(REPO_ROOT),
            env=self.env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )

    @property
    def pid(self) -> int:
        if self.proc is None:
            raise RuntimeError(f"worker {self.name} never started")
        return self.proc.pid

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def sigkill(self) -> None:
        if self.proc is None:
            raise RuntimeError(f"worker {self.name} never started")
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
        self.exit_code = self.proc.wait(timeout=KILL_WAIT_SECONDS)

    def sigterm(self, timeout: float = 20.0) -> int:
        if self.proc is None:
            raise RuntimeError(f"worker {self.name} never started")
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        self.exit_code = self.proc.wait(timeout=timeout)
        return self.exit_code

    def tail(self, chars: int = 4000) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-chars:]
        except OSError:
            return f"<no log at {self.log_path}>"


def worker_env(values: dict[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FORGE_", "GITLAB_", "GITHUB_", "LITELLM_"))
    }
    env.update(values)
    env["PYTHONUNBUFFERED"] = "1"
    return env
