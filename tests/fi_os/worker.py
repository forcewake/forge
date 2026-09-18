"""A REAL ``python -m forge.worker`` OS process under the suite's control.

This is the R20 evidence the coroutine suite cannot produce: the worker is a
separate interpreter with its own connection pools, its own event loop and
its own PID — killed with SIGKILL (the wire-level shape of ``kill -9``, where
no failure is recorded and no cleanup runs) or SIGTERM (graceful shutdown),
never with ``task.cancel()``.

The env is built explicitly: a developer ``.env`` must not leak in, and every
remote seam points at the suite's local stubs.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Env keys that must NEVER leak from the developer shell / pytest run into a
#: worker subprocess — the suite pins every one of them explicitly.
_STRIPPED_PREFIXES = ("FORGE_", "GITLAB_", "LITELLM_", "AZURE_", "GITHUB_")
_STRIPPED_KEYS = {"DATABASE_URL", "REDIS_URL", "FORGE_CAPTURE_DIR"}

KILL_WAIT_SECONDS = 15.0


def worker_env(
    *,
    database_url: str,
    redis_url: str,
    gitlab_url: str,
    llm_url: str,
) -> dict[str, str]:
    """An isolated env for one worker subprocess: stubs in, leakage out."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _STRIPPED_KEYS
        and not any(key.startswith(prefix) for prefix in _STRIPPED_PREFIXES)
    }
    env.update(
        {
            "DATABASE_URL": database_url,
            "REDIS_URL": redis_url,
            "GITLAB_URL": gitlab_url,
            "GITLAB_TOKEN": "glpat-os-fi-stub",
            "GITLAB_WEBHOOK_SECRET": "os-fi-webhook-secret",
            "LITELLM_URL": llm_url,
            "FORGE_APPROVERS": "alice",
            "FORGE_IMPLEMENTER_BACKEND": "builtin",
            "FORGE_BOT_USERNAME": "forge-bot",
            "LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


class OSWorker:
    """One spawned ``python -m forge.worker`` process with a kill switch."""

    def __init__(self, name: str, env: dict[str, str], log_dir: Path) -> None:
        self.name = name
        self.env = env
        self.log_path = log_dir / f"worker-{name}.log"
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.killed_at: float | None = None
        self.exit_code: int | None = None

    def start(self) -> None:
        assert self.proc is None, f"worker {self.name} already started"
        log_handle = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "forge.worker"],
            cwd=str(REPO_ROOT),
            env=self.env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        self.started_at = time.monotonic()

    @property
    def pid(self) -> int:
        assert self.proc is not None, f"worker {self.name} never started"
        return self.proc.pid

    @property
    def owner_id(self) -> str:
        """The step-runtime lease owner this process claims steps with."""
        assert self.proc is not None, f"worker {self.name} never started"
        return f"worker-{self.proc.pid}"

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def sigkill(self) -> None:
        """Hard process death: no cleanup, no final writes, nothing recorded."""
        assert self.proc is not None, f"worker {self.name} never started"
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
        self.exit_code = self.proc.wait(timeout=KILL_WAIT_SECONDS)
        self.killed_at = time.monotonic()

    def sigterm(self, timeout: float = 45.0) -> int:
        """Graceful shutdown: the real signal path of a deployed worker."""
        assert self.proc is not None, f"worker {self.name} never started"
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        self.exit_code = self.proc.wait(timeout=timeout)
        return self.exit_code

    def tail(self, chars: int = 3000) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-chars:]
        except OSError:
            return f"<no log at {self.log_path}>"
