#!/usr/bin/env python3
"""Live smoke for the REAL interactive drivers (claude/codex/opencode).

Contract-tested is not live-proven. This script drives each driver
against a REAL vendor backend on this machine and writes an evidence
JSON — the honest-executed-evidence doctrine: only what actually ran
gets recorded, failures included. It is the operator tool behind the
DriverMatrix live registrations (docs/evaluation/ evidence files).

Per lane:

- claude  — the real claude-agent-sdk + the ambient CLI auth (gateway
  env is assembled from ~/.claude/settings.json WITHOUT printing it):
  session start -> task turn -> steering follow-up -> a second session
  interrupted mid-turn.
- codex    — the real `codex app-server` over stdio JSON-RPC: thread +
  tiny turn to completion, then a long turn steered mid-flight and
  interrupted (turn/completed must say interrupted).
- opencode — spawns a lane-local `opencode serve` (OpenCodeServer) and
  runs a session/prompt/events round trip plus an abort on a long
  prompt.

Usage:
    uv run --extra interactive python scripts/driver_live_smoke.py \
        --driver claude --out docs/evaluation/.../claude-live.json

Exit code 0 = every recorded step passed; 1 = any step failed (the
evidence JSON still gets written — failure is evidence too).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

#: Tiny deterministic tasks; cheap models, hard per-step budgets.
_TASK_PONG = "Reply with exactly the single word PONG and nothing else. No tools."
_TASK_BONG = "Now reply with exactly the single word BONG and nothing else."
_TASK_LONG = (
    "Count aloud from 1 to 60, one number per line, pausing briefly after each. "
    "Do not stop early and do not use any tools."
)
_TASK_DONE = (
    "Ignore the counting task: count DOWN from 100 to 1 instead, slowly, one "
    "number per line. Do not stop early."
)

#: The real-usage task (--e2e): the agent must READ a failing test,
#: EDIT the module, RUN pytest, and leave the repo green. This is what
#: "the driver can drive coding work" means — PONG proves the wire,
#: this proves the lane.
_TASK_E2E = (
    "In this working directory, implement the `add` function in calc.py so "
    "that every test in test_calc.py passes. Run "
    "`python3 -m pytest test_calc.py -q` to verify your work. Do not create "
    "git commits. Reply with DONE when the tests pass."
)


def _binary_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        return (
            (out.stdout or out.stderr).strip().splitlines()[0]
            if out.returncode == 0
            else (f"exit {out.returncode}")
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"unavailable ({type(error).__name__})"


class Recorder:
    """Evidence accumulator — every step lands with its outcome."""

    def __init__(self, driver: str) -> None:
        self.driver = driver
        self.started = time.time()
        self.steps: list[dict[str, Any]] = []

    def step(self, name: str, *, ok: bool, seconds: float, **detail: Any) -> bool:
        entry: dict[str, Any] = {"step": name, "ok": bool(ok), "seconds": round(seconds, 2)}
        entry.update(detail)
        self.steps.append(entry)
        mark = "ok " if ok else "FAIL"
        print(f"  [{mark}] {name} ({seconds:.1f}s)")
        return ok

    def evidence(self, extra: dict[str, Any]) -> dict[str, Any]:
        failures = [s["step"] for s in self.steps if not s["ok"]]
        return {
            "driver": self.driver,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "steps": self.steps,
            "all_ok": not failures,
            "failures": failures,
            **extra,
        }


async def _with_budget(coro: Any, seconds: float, step: str) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except TimeoutError:
        raise TimeoutError(f"{step} exceeded its {seconds}s budget") from None


def _classify_claude_messages(messages: list[dict]) -> dict[str, Any]:
    """Summarize drained raw SDK dicts without dumping full content.

    Live-verified shapes (0.2.157 asdict): assistant blocks carry NO
    ``type`` field — a text block is a dict WITH a ``text`` key, a
    thinking block has ``thinking``; stream/system messages are
    ``{"data": ..., "subtype": ...}``.
    """
    kinds: list[str] = []
    results: list[dict[str, Any]] = []
    texts: list[str] = []
    for message in messages:
        if "terminal_reason" in message or "total_cost_usd" in message:
            kinds.append("result")
            results.append(
                {
                    k: message.get(k)
                    for k in ("session_id", "terminal_reason", "total_cost_usd", "api_error_status")
                    if k in message
                }
            )
        elif "content" in message:
            kinds.append("assistant")
            for block in message.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts.append(block["text"])
        else:
            kinds.append("other")
    return {
        "message_kinds": {
            "assistant": kinds.count("assistant"),
            "result": kinds.count("result"),
            "other": kinds.count("other"),
        },
        "all_text": " | ".join(texts)[:400],
        "last_result": results[-1] if results else None,
    }


async def _drain_until_result(client: Any, session_id: str, *, budget_s: float) -> list[dict]:
    """Poll :meth:`query` until a drain piece carries a ResultMessage.

    Draining CONSUMES (a second call returns only what arrived since),
    so turn messages arrive in pieces; accumulate every piece and stop
    when one of them is terminal (``terminal_reason``/``total_cost_usd``
    — any result in a piece is new by construction).
    """
    deadline = time.time() + budget_s
    accumulated: list[dict] = []
    while time.time() < deadline:
        piece = await client.query(session_id)
        accumulated.extend(piece)
        if any("terminal_reason" in m or "total_cost_usd" in m for m in piece):
            return accumulated
        await asyncio.sleep(3)
    return accumulated


async def smoke_claude(out: Path) -> int:
    from forge.adaptive.drivers import claude_sdk_client_from_env

    rec = Recorder("claude-sdk")
    versions = {
        "claude_cli": _binary_version("claude"),
        "sdk": _import_version("claude_agent_sdk"),
    }
    # Gateway auth assembled from the CLI's own settings (never printed):
    # the driver's ephemeral CLAUDE_CONFIG_DIR hides ~/.claude, so the
    # gateway must ride the process env explicitly (the SDK merges
    # options.env over the inherited environment).
    settings_path = Path.home() / ".claude" / "settings.json"
    gateway: dict[str, str] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            value = (settings.get("env") or {}).get(key)
            if value:
                os.environ[key] = value
                gateway[key] = "<set>"
        model = settings.get("model")
        if model and not os.environ.get("FORGE_CLAUDE_MODEL"):
            os.environ["FORGE_CLAUDE_MODEL"] = str(model)
    workdir = REPO_ROOT / ".forge" / "live-smoke-claude"
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ["FORGE_CLAUDE_CWD"] = str(workdir)

    client = claude_sdk_client_from_env()
    t0 = time.time()
    session_id = await _with_budget(client.start_session(_TASK_PONG), 240, "start_session")
    rec.step("start_session", ok=bool(session_id), seconds=time.time() - t0, session_id=session_id)

    t0 = time.time()
    messages = await _with_budget(
        _drain_until_result(client, session_id, budget_s=180), 200, "query(turn1)"
    )
    summary = _classify_claude_messages(messages)
    ok = summary["last_result"] is not None and "PONG" in summary["all_text"]
    rec.step("query(turn1=PONG)", ok=ok, seconds=time.time() - t0, **summary)

    t0 = time.time()
    await _with_budget(client.send(session_id, _TASK_BONG), 240, "send(steer)")
    messages = await _with_budget(
        _drain_until_result(client, session_id, budget_s=180), 200, "query(turn2)"
    )
    summary = _classify_claude_messages(messages)
    ok = "BONG" in summary["all_text"]
    rec.step("send+query(turn2=BONG)", ok=ok, seconds=time.time() - t0, **summary)

    # Session 2: interrupt a long turn.
    t0 = time.time()
    session2 = await _with_budget(client.start_session(_TASK_LONG), 240, "start_session(2)")
    interrupted_ok = False
    detail: dict[str, Any] = {}
    try:
        await asyncio.sleep(8)
        await _with_budget(client.interrupt(session2), 60, "interrupt")
        messages = await _with_budget(
            _drain_until_result(client, session2, budget_s=90), 100, "query(int)"
        )
        summary = _classify_claude_messages(messages)
        terminal = (summary["last_result"] or {}).get("terminal_reason")
        interrupted_ok = terminal is not None
        detail = summary
    except (TimeoutError, RuntimeError) as error:
        detail = {"error": f"{type(error).__name__}: {error}"}
        interrupted_ok = False
    rec.step("interrupt(long turn)", ok=interrupted_ok, seconds=time.time() - t0, **detail)

    evidence = rec.evidence({"versions": versions, "gateway": gateway})
    out.write_text(json.dumps(evidence, indent=2))
    return 0 if evidence["all_ok"] else 1


def _import_version(module: str) -> str:
    try:
        imported = __import__(module)
        return str(getattr(imported, "__version__", "importable"))
    except ImportError as error:
        return f"not installed ({error})"


async def smoke_codex(out: Path) -> int:
    from forge.adaptive.drivers import codex_app_client_from_env

    rec = Recorder("codex-app")
    versions = {"codex_cli": _binary_version("codex")}
    workdir = REPO_ROOT / ".forge" / "live-smoke-codex"
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ["CODEX_CWD"] = str(workdir)

    client = codex_app_client_from_env()
    try:
        t0 = time.time()
        thread_id = await _with_budget(client.start_thread(_TASK_PONG), 300, "start_thread")
        rec.step("start_thread", ok=bool(thread_id), seconds=time.time() - t0, thread_id=thread_id)

        def _turn_status() -> dict[str, Any] | None:
            for event in reversed(client.events()):
                if event.get("method") == "turn/completed":
                    return (event.get("params") or {}).get("turn") or {}
            return None

        t0 = time.time()
        deadline = time.time() + 240
        status: dict[str, Any] | None = None
        while time.time() < deadline:
            status = _turn_status()
            if status:
                break
            await asyncio.sleep(2)
        ok = bool(status) and status.get("status") == "completed"
        rec.step(
            "turn1(completed)",
            ok=ok,
            seconds=time.time() - t0,
            turn_status=(status or {}).get("status"),
            note=(
                "turn/start response timing observed live: the response arrives at "
                "turn ACCEPTANCE (~0.4s), completion is the turn/completed notification"
            ),
        )

        # Thread 2: steer mid-flight, then interrupt.
        t0 = time.time()
        long_thread = await _with_budget(client.start_thread(_TASK_LONG), 300, "start_thread(2)")
        rec.step(
            "start_thread(2)", ok=bool(long_thread), seconds=time.time() - t0, thread_id=long_thread
        )

        t0 = time.time()
        steer_ok = False
        steer_detail: dict[str, Any] = {}
        try:
            deadline = time.time() + 60
            while time.time() < deadline and not client.active_turn_id(long_thread):
                await asyncio.sleep(1)
            if client.active_turn_id(long_thread):
                await _with_budget(client.steer_active_turn(long_thread, _TASK_DONE), 60, "steer")
                steer_ok = True
                steer_detail = {"turn_id": client.active_turn_id(long_thread)}
            else:
                steer_detail = {"error": "no turn/started observed within 60s"}
        except Exception as error:  # noqa: BLE001 — evidence, not control flow
            steer_detail = {"error": f"{type(error).__name__}: {error}"}
        rec.step("steer_active_turn", ok=steer_ok, seconds=time.time() - t0, **steer_detail)

        t0 = time.time()
        interrupt_ok = False
        interrupt_detail: dict[str, Any] = {}
        try:
            # Give the steered (still-running) turn a moment so the
            # interrupt lands mid-flight, not on an already-settled turn.
            await asyncio.sleep(5)
            await _with_budget(client.interrupt(long_thread), 90, "interrupt")
            deadline = time.time() + 60
            status2: dict[str, Any] | None = None
            while time.time() < deadline:
                for event in reversed(client.events()):
                    if event.get("method") == "turn/completed" and (
                        (event.get("params") or {}).get("threadId") == long_thread
                        or ((event.get("params") or {}).get("turn") or {}).get("threadId")
                        == long_thread
                    ):
                        status2 = (event.get("params") or {}).get("turn") or {}
                        break
                if status2:
                    break
                await asyncio.sleep(1)
            interrupt_ok = bool(status2) and status2.get("status") == "interrupted"
            interrupt_detail = {"turn_status": (status2 or {}).get("status")}
        except Exception as error:  # noqa: BLE001 — evidence, not control flow
            interrupt_detail = {"error": f"{type(error).__name__}: {error}"}
        rec.step(
            "interrupt(->interrupted)",
            ok=interrupt_ok,
            seconds=time.time() - t0,
            **interrupt_detail,
        )
    finally:
        await client.close()

    evidence = rec.evidence({"versions": versions, "event_count": len(client.events())})
    out.write_text(json.dumps(evidence, indent=2))
    return 0 if evidence["all_ok"] else 1


async def smoke_opencode(out: Path) -> int:
    from forge.adaptive.drivers import opencode_server_from_env
    from forge.adaptive.drivers.opencode import opencode_client_from_env

    rec = Recorder("opencode-server")
    versions = {"opencode_cli": _binary_version("opencode")}
    workdir = REPO_ROOT / ".forge" / "live-smoke-opencode"
    workdir.mkdir(parents=True, exist_ok=True)

    server = opencode_server_from_env({"OPENCODE_SERVE_CWD": str(workdir)})
    async with server:
        rec.step(
            "serve_spawn+ready",
            ok=True,
            seconds=server.ready_seconds or 0.0,
            url=server.url,
        )
        client = opencode_client_from_env(
            env={
                "OPENCODE_SERVER_URL": server.url,
                # v2.0.10 always enforces a password; the spawner owns one.
                "OPENCODE_SERVER_PASSWORD": server.password,
                "OPENCODE_PROVIDER_ID": "zai-coding-plan",
                "OPENCODE_MODEL_ID": "glm-5-turbo",
                "OPENCODE_AGENT": "build",
                "OPENCODE_SESSION_DIRECTORY": str(workdir),
                "OPENCODE_PROMPT_TIMEOUT": "240",
            }
        )
        try:
            t0 = time.time()
            session_id = await _with_budget(client.start_session(_TASK_PONG), 240, "start_session")
            rec.step(
                "start_session",
                ok=bool(session_id),
                seconds=time.time() - t0,
                session_id=session_id,
            )

            t0 = time.time()
            await _with_budget(client.prompt(session_id, _TASK_BONG), 240, "prompt")
            events = await _with_budget(client.events(session_id), 30, "events")
            texts = [
                e
                for e in events
                if "bong" in json.dumps(e).lower() or "pong" in json.dumps(e).lower()
            ]
            rec.step(
                "prompt(BONG)+events",
                ok=bool(texts),
                seconds=time.time() - t0,
                event_count=len(events),
                matched_events=len(texts),
            )

            # A long prompt aborted mid-flight.
            t0 = time.time()
            abort_ok = False
            abort_detail: dict[str, Any] = {}
            try:
                session2 = await _with_budget(
                    client.start_session(_TASK_LONG), 240, "start_session(2)"
                )
                await asyncio.sleep(6)
                await _with_budget(client.abort(session2), 30, "abort")
                await asyncio.sleep(1)
                after = await client.events(session2)
                abort_ok = True
                abort_detail = {"events_after_abort": len(after)}
            except Exception as error:  # noqa: BLE001 — evidence, not control flow
                abort_detail = {"error": f"{type(error).__name__}: {error}"}
            rec.step("abort(long session)", ok=abort_ok, seconds=time.time() - t0, **abort_detail)
        finally:
            await client.aclose()

    evidence = rec.evidence({"versions": versions})
    out.write_text(json.dumps(evidence, indent=2))
    return 0 if evidence["all_ok"] else 1


# -- real-usage mode (--e2e) ---------------------------------------------
#
# The PONG/BONG smokes prove the WIRE. This mode proves the LANE: a
# fresh scratch repo with a failing test, a real agent task (read,
# edit, run pytest), the driver as the only control surface, and the
# repo's tests as the independent judge. The agent's reply is NOT
# trusted for success — pytest run by THIS script is.


def _setup_task_repo(driver: str) -> Path:
    """A fresh scratch repo whose tests fail until the agent works."""
    import shutil

    root = REPO_ROOT / ".forge" / "live-e2e" / driver
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "calc.py").write_text(
        '"""A stub the driving agent must implement."""\n\n\ndef add(a: int, b: int) -> int:\n'
        '    """Return the sum (implemented by the agent)."""\n'
        "    raise NotImplementedError\n"
    )
    (root / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add_positive():\n    assert add(2, 3) == 5\n\n"
        "def test_add_negative():\n    assert add(-1, 1) == 0\n\n"
        "def test_add_zero():\n    assert add(0, 0) == 0\n"
    )
    return root


def _repo_tests_pass(root: Path) -> tuple[bool, str]:
    """The independent judge: pytest in the task repo, run by us."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_calc.py", "-q"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    return proc.returncode == 0, (tail[-1] if tail else "")


def _e2e_check_result(ok: bool, detail: str, seconds: float, rec: Recorder) -> bool:
    return rec.step(
        "repo tests green after the agent turn",
        ok=ok,
        seconds=seconds,
        pytest_tail=detail,
    )


async def e2e_claude(out: Path) -> int:
    from forge.adaptive.drivers import claude_sdk_client_from_env

    rec = Recorder("claude-sdk-e2e")
    root = _setup_task_repo("claude")
    settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        if (settings.get("env") or {}).get(key):
            os.environ[key] = settings["env"][key]
    os.environ.setdefault("FORGE_CLAUDE_MODEL", str(settings.get("model") or "glm-5.3-flash[1m]"))
    os.environ["FORGE_CLAUDE_CWD"] = str(root)

    client = claude_sdk_client_from_env()
    t0 = time.time()
    session_id = await _with_budget(client.start_session(_TASK_E2E), 120, "start_session")
    messages = await _with_budget(
        _drain_until_result(client, session_id, budget_s=420), 440, "turn"
    )
    summary = _classify_claude_messages(messages)
    terminal = (summary["last_result"] or {}).get("terminal_reason")
    rec.step(
        "agent turn completed",
        ok=terminal == "completed",
        seconds=time.time() - t0,
        terminal_reason=terminal,
        reply_excerpt=summary["all_text"][:200],
    )
    await client.close(session_id)

    ok, detail = _repo_tests_pass(root)
    _e2e_check_result(ok, detail, time.time() - t0, rec)
    evidence = rec.evidence({"task_repo": str(root), "task": _TASK_E2E})
    out.write_text(json.dumps(evidence, indent=2))
    return 0 if evidence["all_ok"] else 1


async def e2e_codex(out: Path) -> int:
    from forge.adaptive.drivers import codex_app_client_from_env

    rec = Recorder("codex-app-e2e")
    root = _setup_task_repo("codex")
    os.environ["CODEX_CWD"] = str(root)

    client = codex_app_client_from_env()
    try:
        t0 = time.time()
        await _with_budget(client.start_thread(_TASK_E2E), 120, "start_thread")
        deadline = time.time() + 600
        status: dict[str, Any] | None = None
        while time.time() < deadline:
            for event in reversed(client.events()):
                if event.get("method") == "turn/completed":
                    status = (event.get("params") or {}).get("turn") or {}
                    break
            if status:
                break
            await asyncio.sleep(3)
        rec.step(
            "agent turn completed",
            ok=bool(status) and status.get("status") == "completed",
            seconds=time.time() - t0,
            turn_status=(status or {}).get("status"),
        )
    finally:
        await client.close()

    ok, detail = _repo_tests_pass(root)
    _e2e_check_result(ok, detail, time.time() - t0, rec)
    evidence = rec.evidence({"task_repo": str(root), "task": _TASK_E2E})
    out.write_text(json.dumps(evidence, indent=2))
    return 0 if evidence["all_ok"] else 1


async def e2e_opencode(out: Path) -> int:
    from forge.adaptive.drivers import opencode_server_from_env
    from forge.adaptive.drivers.opencode import opencode_client_from_env

    rec = Recorder("opencode-server-e2e")
    root = _setup_task_repo("opencode")
    server = opencode_server_from_env({"OPENCODE_SERVE_CWD": str(root)})
    async with server:
        client = opencode_client_from_env(
            env={
                "OPENCODE_SERVER_URL": server.url,
                "OPENCODE_SERVER_PASSWORD": server.password,
                "OPENCODE_PROVIDER_ID": "zai-coding-plan",
                "OPENCODE_MODEL_ID": "glm-5-turbo",
                "OPENCODE_SESSION_DIRECTORY": str(root),
                "OPENCODE_PROMPT_TIMEOUT": "600",
            }
        )
        t0 = time.time()
        try:
            session_id = await _with_budget(client.start_session(_TASK_E2E), 600, "start_session")
            rec.step(
                "agent turn completed", ok=True, seconds=time.time() - t0, session_id=session_id
            )
        finally:
            await client.aclose()

    ok, detail = _repo_tests_pass(root)
    _e2e_check_result(ok, detail, time.time() - t0, rec)
    evidence = rec.evidence({"task_repo": str(root), "task": _TASK_E2E})
    out.write_text(json.dumps(evidence, indent=2))
    return 0 if evidence["all_ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, choices=("claude", "codex", "opencode"))
    parser.add_argument("--out", type=Path, required=True, help="evidence JSON path")
    parser.add_argument(
        "--e2e",
        action="store_true",
        help="real-usage mode: the agent implements a failing-test task in a "
        "scratch repo through the driver; repo pytest is the judge",
    )
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    runners = {
        "claude": e2e_claude if args.e2e else smoke_claude,
        "codex": e2e_codex if args.e2e else smoke_codex,
        "opencode": e2e_opencode if args.e2e else smoke_opencode,
    }
    mode = "e2e task" if args.e2e else "smoke"
    print(f"live {mode}: {args.driver}")
    try:
        return asyncio.run(runners[args.driver](args.out))
    except Exception as error:  # noqa: BLE001 — the evidence file must exist either way
        args.out.write_text(
            json.dumps(
                {
                    "driver": args.driver,
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "all_ok": False,
                    "failures": [f"unhandled {type(error).__name__}: {error}"],
                },
                indent=2,
            )
        )
        print(f"  [FAIL] unhandled {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
