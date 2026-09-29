"""Exit a test helper process as soon as the process that spawned it dies.

The fake servers, the ingress server, the matrix worker and the fake vendor
run as separate processes. When pytest is killed abruptly its fixtures never
run their teardown, the children are re-parented to init, and they would
otherwise run forever. ``install()`` starts a daemon thread that polls the
parent pid and ends the process when it changes.
"""

from __future__ import annotations

import os
import threading
import time

POLL_SECONDS = 0.5


def install(poll_seconds: float = POLL_SECONDS) -> threading.Thread:
    parent = os.getppid()

    def watch() -> None:
        while True:
            if os.getppid() != parent:
                os._exit(0)
            time.sleep(poll_seconds)

    thread = threading.Thread(target=watch, name="orphan-guard", daemon=True)
    thread.start()
    return thread
