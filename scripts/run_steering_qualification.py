#!/usr/bin/env python3
"""R37-10 (issue #291 / AT-10) — the steering-causality qualification runner.

Two capture modes, never pooled (the discovery-live discipline):

- ``--scripted`` (deterministic, always runnable) — the REACTIVE
  SCRIPTED VENDOR (``forge.adaptive.steering_causality``'s executable
  half, ``scripted-causal``) on the REAL lane seam: a real lane
  subprocess on the real codex app-server wire, a real durable control
  plane over real HTTP, the operator's steer through the REAL durable
  operator surface — and the counterfactual arm as a second real run
  with steering disabled. Both arms are graded by the same pure grader
  (:func:`forge.adaptive.steering_causality.grade_causality`).

- ``--live`` (one attempt, hard caps) — the same task driven through
  the lab's litellm gateway as a REAL model conversation: the model
  implements the task's first edit, the operator's steer is injected
  mid-conversation, and the model's NEXT output is graded by the same
  three arms (the counterfactual via a second capped run without the
  steer — only if the first arm succeeded inside its caps). Caps: 2
  calls per arm, bounded tokens per call, wall clock per arm, and a
  HARD $1 spend cap enforced against the repo's lab rate-card
  estimates. An unreachable/unconfigured gateway is recorded as a
  REFUSAL with its reason — live provenance is never fabricated.

The revision / WIP-reuse / old-epoch-replay legs of AT-10 are NOT this
script's: they are proven at the process/DB level by
``tests/production_entry/test_causal_steering.py`` over the REAL
revision lifecycle — the report points there.

Usage::

    uv run python scripts/run_steering_qualification.py --scripted \\
        --out evaluation/steering/
    uv run python scripts/run_steering_qualification.py --live \\
        --out evaluation/steering/ [--gateway-url http://localhost:4000 \\
        --gateway-model fast]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # seam reuse by import needs the repo root
    sys.path.insert(0, str(REPO_ROOT))

from forge.adaptive import steering_causality as sc  # noqa: E402
from forge.adaptive.checkpoint_channel import work_scoped_token  # noqa: E402

# The production-entry seams, reused BY IMPORT (the run_lab_pilot precedent).
from tests.production_entry import conftest as pe  # noqa: E402
from tests.production_entry import test_production_entry as pe_helpers  # noqa: E402

REPORT_SCHEMA = "forge.steering.qualification/1"
RUN_SCHEMA = "forge.steering.run/1"
OPERATOR = "human:operator"

#: The vendor executable — the reactive scripted vendor IS the harness module.
REACTIVE_VENDOR = REPO_ROOT / "src" / "forge" / "adaptive" / "steering_causality.py"

#: Where each mode's captured run + grade lands.
SCRIPTED_RUN_FILE = "scripted-run.json"
LIVE_RUN_FILE = "live-run.json"
REPORT_FILE = "report.json"

#: The live arm's hard caps (R37-10: one attempt, never an uncapped paid run).
LIVE_MAX_CALLS_PER_ARM = 2
LIVE_MAX_TOKENS_PER_CALL = 700
LIVE_WALL_S_PER_ARM = 180.0
LIVE_SPEND_CAP_USD = 1.0

#: The lab rate-card estimates every live spend figure is priced with
#: (evaluation/economics/rate-card-lab-v1.json — an ESTIMATE version,
#: never a billing record).
RATE_CARD_VERSION = "lab-estimate-v1"
RATE_INPUT_PER_MTOK_USD = 2.5
RATE_OUTPUT_PER_MTOK_USD = 10.0

#: The env the lab gateway URL lives in (the developer environment's
#: litellm proxy; read-only fallback for --gateway-url).
GATEWAY_URL_ENV = "LITELLM_URL"


class QualificationError(RuntimeError):
    """The runner hit a condition it refuses to paper over."""


# ---------------------------------------------------------------------------
# The scripted-causal arms (real lane subprocess, real HTTP control plane)
# ---------------------------------------------------------------------------


class SteeringEnvironment:
    """One qualification's real infrastructure: a durable sqlite control DB
    and the lane-control + checkpoint-channel routers over real HTTP (the
    lab pilot's LabEnvironment discipline, minimal)."""

    def __init__(self, root: Path, work_id: str) -> None:
        self.root = root
        self.work_id = work_id
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "control.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.engine: Any = None
        self.factory: Any = None
        self.control: pe.ControlPlane | None = None

    async def __aenter__(self) -> SteeringEnvironment:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        self.engine = create_async_engine(self.db_url)
        await pe_helpers.create_control_schema(self.engine)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.control = await pe.start_control_plane(self.db_url)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.control is not None:
            self.control.stop()
        if self.engine is not None:
            await self.engine.dispose()

    @property
    def control_url(self) -> str:
        if self.control is None:
            raise QualificationError("environment not entered")
        return self.control.base_url

    def token(self) -> str:
        return work_scoped_token(pe.PE_LANE_SECRET, self.work_id)

    def durable_control(self):
        """The REAL durable operator control service over this DB."""
        from forge.adaptive.checkpoint_repository import resolve_repository
        from forge.adaptive.mailbox_db import PostgresMailbox
        from forge.adaptive.wiring import OperatorControlService

        assert self.factory is not None
        return OperatorControlService(
            mailbox=PostgresMailbox(self.factory),
            checkpoint_repository=resolve_repository(session_factory=self.factory),
        )

    async def steer_row(self, command_id: str):
        """The durable steer row (evidence read; None when absent)."""
        from sqlalchemy import select

        from forge.adaptive.mailbox_db import ControlCommandRow

        assert self.factory is not None
        async with self.factory() as session:
            return (
                await session.execute(
                    select(ControlCommandRow).where(ControlCommandRow.id == command_id)
                )
            ).scalar_one_or_none()


def _make_task_checkout(parent: Path, name: str) -> tuple[Path, str]:
    """A REAL git checkout at its frozen base, carrying the frozen task file."""
    checkout = parent / name
    checkout.mkdir(parents=True)
    pe_helpers._git(checkout, "init", "-q", "-b", "main")
    pe_helpers._git(checkout, "config", "user.email", "lane@example.com")
    pe_helpers._git(checkout, "config", "user.name", "forge lane")
    (checkout / sc.POLICY_PATH).write_text(sc.BASE_POLICY_CONTENT, encoding="utf-8")
    (checkout / "README.md").write_text("base readme\n", encoding="utf-8")
    pe_helpers._git(checkout, "add", "-A")
    pe_helpers._git(checkout, "commit", "-q", "-m", "frozen base")
    base_oid = pe_helpers._git(checkout, "rev-parse", "HEAD").stdout.strip()
    exclude = checkout / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "\n.forge/\n__pycache__/\n")
    return checkout, base_oid


def _first_edit_actions() -> list[dict[str, str]]:
    """The vendor's pre-instruction edit: the task's ordinary change."""
    return [{"op": "write", "path": sc.POLICY_PATH, "content": sc.DEFAULT_FIRST_EDIT}]


def _default_followup_actions() -> list[dict[str, str]]:
    """The unsteered trajectory's deterministic follow-up."""
    return [{"op": "write", "path": sc.FOLLOWUP_PATH, "content": sc.DEFAULT_FOLLOWUP_CONTENT}]


def _run_reactive_lane(
    checkout: Path,
    env: SteeringEnvironment,
    eventlog: Path,
    *,
    steer_wait_s: float,
    steering_enabled: bool,
    resume: str = "",
    first_edit: list[dict[str, str]] | None = None,
    followup: list[dict[str, str]] | None = None,
    timeout: int = 240,
) -> subprocess.CompletedProcess[str]:
    """One REAL lane subprocess driving the REACTIVE vendor on the wire."""
    brief = checkout / ".forge" / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(sc.STEERING_TASK_BRIEF, encoding="utf-8")
    run_env = {
        **os.environ,
        "FORGE_LANE_DRIVER": "codex",
        "CODEX_BINARY": str(REACTIVE_VENDOR),
        "CODEX_CWD": str(checkout),
        "FORGE_BRIEF": str(brief),
        "FORGE_ATTEMPT_BASE": pe_helpers._git(checkout, "rev-parse", "HEAD").stdout.strip(),
        "FORGE_ISSUE_IID": "42",
        "FORGE_RUN_ID": env.work_id,
        "FORGE_WORK_ID": env.work_id,
        "FORGE_STEERING_ENABLED": "1" if steering_enabled else "0",
        "FORGE_LANE_RESUME": resume,
        "FORGE_LANE_CONTROL_URL": env.control_url,
        "FORGE_LANE_CONTROL_TOKEN": env.token(),
        "FORGE_LANE_CONTROL_POLL_SECONDS": "0.05",
        "FORGE_CHECKPOINT_STORE_DIR": str(env.root / "checkpoint-store"),
        "FORGE_LANE_CONTROL_SECRET": pe.PE_LANE_SECRET,
        "FORGE_LANE_LEGACY_TOKEN_DEADLINE": pe.PE_LEGACY_DEADLINE,
        sc.EVENTLOG_ENV: str(eventlog),
        sc.ACTIONS_ENV: json.dumps(first_edit if first_edit is not None else _first_edit_actions()),
        sc.DEFAULT_ACTIONS_ENV: json.dumps(
            followup if followup is not None else _default_followup_actions()
        ),
        sc.STEER_WAIT_S_ENV: str(steer_wait_s),
        sc.STEER_POLL_INTERVAL_S_ENV: "0.05",
    }
    if not steering_enabled:
        run_env[sc.STEERING_DISABLED_ENV] = "1"
    return subprocess.run(
        [sys.executable, "-m", "forge.lane_driver", "--driver", "codex"],
        cwd=checkout,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def _wait_for_event(eventlog: Path, kind: str, *, timeout_s: float = 60.0) -> dict[str, Any]:
    """Poll the vendor's append-only log until one event of *kind* lands.

    Async on purpose: the lane subprocess rides this loop's default
    executor, and a blocking poll starves it.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if eventlog.is_file():
            for line in eventlog.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("kind") == kind:
                    return entry
        await asyncio.sleep(0.02)
    raise QualificationError(f"the vendor never logged a {kind!r} event within {timeout_s}s")


def _observed_edits(checkout: Path) -> sc.EditSet:
    """The arm's OBSERVED edit set: the frozen base vs the post-turn bytes."""
    names = pe_helpers._git(checkout, "ls-files").stdout.split()
    base: dict[str, str] = {}
    for name in names:
        blob = subprocess.run(
            ["git", "-C", str(checkout), "show", f"HEAD:{name}"],
            capture_output=True,
            timeout=60,
            check=True,
        ).stdout
        base[name] = blob.decode("utf-8", errors="replace")
    files = {
        name: (checkout / name).read_text(encoding="utf-8")
        for name in [*base, sc.POLICY_PATH, sc.FOLLOWUP_PATH]
        if (checkout / name).is_file()
    }
    return sc.edit_set_of(base, files)


def _run_document(
    run: sc.SteeringRun, grade: sc.CausalityGrade, *, received_to_applied_ms: int | None
) -> dict[str, Any]:
    return {
        "schema": RUN_SCHEMA,
        **run.as_document(),
        "grade": grade.as_document(),
        "observability": {
            "control.received_to_applied_ms": received_to_applied_ms,
            "control.effect_outcome": (run.command.status if run.command is not None else None),
        },
    }


async def run_scripted_qualification(root: Path) -> dict[str, Any]:
    """The scripted-causal arms: the steered run, the counterfactual, the grade.

    Everything a customer drives is REAL here: a real lane subprocess on
    the real codex app-server wire whose vendor is the reactive script,
    a real durable control plane over real HTTP, the operator's steer
    through the real durable operator surface, real git checkouts. The
    counterfactual is a SECOND real run of the same task with steering
    disabled — the reactive script's determinism makes it runnable, and
    the grade's counterfactual arm reads its captured edits.
    """
    work_id = "steer-qual-scripted"
    async with SteeringEnvironment(root, work_id) as env:
        # -- arm 1 (steered): the vendor is mid-turn when the steer lands --
        steered_checkout, _base = _make_task_checkout(root, "steered-workspace")
        steered_log = root / "steered-vendor-events.jsonl"
        lane_started = time.monotonic()
        lane_task = asyncio.create_task(
            asyncio.to_thread(
                _run_reactive_lane,
                steered_checkout,
                env,
                steered_log,
                steer_wait_s=15.0,
                steering_enabled=True,
            )
        )
        await _wait_for_event(steered_log, "vendor_edits")  # the pre-instruction edit landed
        service = env.durable_control()
        accepted = await service.steer(
            work_id, OPERATOR, sc.STEERING_TASK_INSTRUCTION, idempotency_key=f"{work_id}:steer:1"
        )
        if accepted.get("status") != "accepted":
            raise QualificationError(f"the durable steer was not accepted: {accepted}")
        steer_accepted_at = datetime.now(timezone.utc).isoformat()
        outcome = await lane_task
        lane_wall_s = time.monotonic() - lane_started
        if outcome.returncode != 0:
            raise QualificationError(f"the steered lane failed: {outcome.stderr[-2000:]}")

        row = await env.steer_row(str(accepted["command_id"]))
        if row is None:
            raise QualificationError("the durable steer row vanished")
        command = sc.SteeringCommandEvidence.of_row(row)
        received_to_applied_ms = None
        if command.received_at:
            with contextlib.suppress(ValueError):
                received_to_applied_ms = int(
                    (
                        datetime.fromisoformat(command.authorized_at or command.received_at)
                        - datetime.fromisoformat(command.received_at)
                    ).total_seconds()
                    * 1000
                )

        # -- arm 2 (counterfactual): the SAME task, steering disabled --------
        counter_checkout, _counter_base = _make_task_checkout(root, "counterfactual-workspace")
        counter_log = root / "counterfactual-vendor-events.jsonl"
        counter_outcome = _run_reactive_lane(
            counter_checkout,
            env,
            counter_log,
            steer_wait_s=0.0,
            steering_enabled=False,
        )
        if counter_outcome.returncode != 0:
            raise QualificationError(
                f"the counterfactual lane failed: {counter_outcome.stderr[-2000:]}"
            )

        steered_run = sc.SteeringRun(
            arm="steered",
            provenance=sc.SCRIPTED_CAUSAL_PROVENANCE,
            command=command,
            vendor_events=tuple(sc.read_vendor_events(steered_log)),
            edits=_observed_edits(steered_checkout),
            counterfactual_edits=_observed_edits(counter_checkout),
        )
        grade = sc.grade_causality(steered_run)
        return {
            "schema": RUN_SCHEMA,
            "mode": "scripted",
            **steered_run.as_document(),
            "grade": grade.as_document(),
            "observability": {
                "control.received_to_applied_ms": received_to_applied_ms,
                "control.effect_outcome": command.status,
                "steer_accepted_at": steer_accepted_at,
                "lane_wall_s": round(lane_wall_s, 3),
            },
            "counterfactual_vendor_events": [
                {"at": event.at, "kind": event.kind, **event.details}
                for event in sc.read_vendor_events(counter_log)
            ],
        }


# ---------------------------------------------------------------------------
# The live arm (one attempt through the lab gateway, hard caps)
# ---------------------------------------------------------------------------


@dataclass
class SpendTracker:
    """The live arm's hard caps: calls, tokens, wall clock and dollars.

    Every cap is enforced BEFORE the next call: a projected overrun
    refuses the call (the record says ``capped`` with the reason), never
    an uncapped continuation. Spend is an ESTIMATE against the lab rate
    card (``cost_state`` says so — never a billing record).
    """

    max_calls: int = LIVE_MAX_CALLS_PER_ARM
    max_tokens_per_call: int = LIVE_MAX_TOKENS_PER_CALL
    spend_cap_usd: float = LIVE_SPEND_CAP_USD
    #: the CURRENT arm's call count (the per-arm cap binds this one).
    calls: int = 0
    #: the WHOLE live session's call count (the record reports this one).
    total_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    def note_call(self, tokens_in: int, tokens_out: int) -> None:
        self.calls += 1
        self.total_calls += 1
        self.tokens_in += max(0, int(tokens_in))
        self.tokens_out += max(0, int(tokens_out))

    @property
    def estimate_usd(self) -> float:
        return round(
            self.tokens_in / 1_000_000 * RATE_INPUT_PER_MTOK_USD
            + self.tokens_out / 1_000_000 * RATE_OUTPUT_PER_MTOK_USD,
            6,
        )

    def refusal(self, wall_s: float, wall_cap_s: float) -> str | None:
        if self.calls >= self.max_calls:
            return f"call cap reached ({self.max_calls})"
        if wall_s >= wall_cap_s:
            return f"wall cap reached ({wall_cap_s:.0f}s)"
        projected = self.estimate_usd + (
            self.max_tokens_per_call / 1_000_000 * RATE_OUTPUT_PER_MTOK_USD
        )
        if projected > self.spend_cap_usd:
            return (
                f"spend cap would be exceeded (projected ${projected:.4f} > ${self.spend_cap_usd})"
            )
        return None

    def next_arm(self) -> None:
        """Start the next ARM: the per-arm call/wall budgets reset, the
        WHOLE-SESSION token and dollar spend never does (the $1 cap binds
        the entire live attempt, not each arm)."""
        self.calls = 0


async def _gateway_completion(
    url: str, model: str, messages: list[dict[str, str]], *, max_tokens: int
) -> dict[str, Any]:
    """One OpenAI-compatible chat completion through the lab gateway."""
    import httpx

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{url.rstrip('/')}/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "max_tokens": int(max_tokens),
                "temperature": 0,
            },
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()


_LIVE_SYSTEM = (
    "You are the implementer agent for a tiny repository. You reply with "
    "EXACTLY one fenced python code block containing the complete new "
    "content of policy.py — no prose outside the block."
)


def _live_task_prompt(policy: str) -> str:
    return (
        "The repository's policy.py currently is:\n\n```python\n"
        + policy
        + "```\n\nTask: add a `clamp(amount)` helper that bounds an amount to "
        "the existing policy knob. Reply with the complete new policy.py."
    )


def _live_finalize_prompt(policy: str) -> str:
    return (
        "The repository's policy.py currently is:\n\n```python\n"
        + policy
        + "```\n\nTask: finalize the change — make any small consistency fix "
        "you still need, keeping the module's existing naming. Reply with the "
        "complete new policy.py."
    )


async def run_live_qualification(
    root: Path, gateway_url: str, gateway_model: str
) -> dict[str, Any]:
    """One capped live attempt: the steer injected mid-conversation.

    The causal question is the grader's: does the model's NEXT output
    (after the injected operator instruction) show the instruction's
    transformation, where the unsteered conversation's next output does
    not? The counterfactual runs only when the steered arm succeeded
    inside its caps. An unreachable gateway is an honest REFUSAL.
    """
    import re

    tracker = SpendTracker()
    arm_started = {"t": time.monotonic()}

    def _policy_of(message: str) -> str:
        """The final ``policy.py`` content the model returned (last fenced block)."""
        blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", message, flags=re.S)
        content = blocks[-1] if blocks else message
        return content.strip() + "\n"

    async def complete(messages: list[dict[str, str]]) -> str:
        wall = time.monotonic() - arm_started["t"]
        refusal = tracker.refusal(wall, LIVE_WALL_S_PER_ARM)
        if refusal is not None:
            raise QualificationError(f"live arm capped before a call: {refusal}")
        body = await _gateway_completion(
            gateway_url, gateway_model, messages, max_tokens=tracker.max_tokens_per_call
        )
        usage = body.get("usage") or {}
        tracker.note_call(
            int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        )
        choice = (body.get("choices") or [{}])[0]
        return str(((choice.get("message") or {}).get("content")) or "")

    def event(at_epoch: float, kind: str, **details: Any) -> dict[str, Any]:
        """One synthesized vendor event at the REAL wall instant it stands for."""
        at = datetime.fromtimestamp(at_epoch, tz=timezone.utc)
        return {"at": at.isoformat(), "kind": kind, **details}

    try:
        # -- the steered conversation -------------------------------------
        first = await complete(
            [
                {"role": "system", "content": _LIVE_SYSTEM},
                {"role": "user", "content": _live_task_prompt(sc.BASE_POLICY_CONTENT)},
            ]
        )
        first_edit_at = time.time()
        steer_command_id = f"live-steer-{int(first_edit_at)}"
        steer_accepted_at = datetime.fromtimestamp(first_edit_at, tz=timezone.utc).isoformat()
        steer_injected_at = first_edit_at + 0.05  # the operator message enters the conversation
        steered_second = await complete(
            [
                {"role": "system", "content": _LIVE_SYSTEM},
                {"role": "user", "content": _live_task_prompt(sc.BASE_POLICY_CONTENT)},
                {"role": "assistant", "content": first},
                {"role": "user", "content": f"[operator steer] {sc.STEERING_TASK_INSTRUCTION}"},
            ]
        )
        steered_final = _policy_of(steered_second)
        steered_edit_at = time.time()

        # -- the counterfactual (only because the first arm fit the caps;
        #    the per-arm budgets reset, the dollar cap never does) ---------
        tracker.next_arm()
        arm_started["t"] = time.monotonic()
        counter_first = await complete(
            [
                {"role": "system", "content": _LIVE_SYSTEM},
                {"role": "user", "content": _live_task_prompt(sc.BASE_POLICY_CONTENT)},
            ]
        )
        counter_second = await complete(
            [
                {"role": "system", "content": _LIVE_SYSTEM},
                {"role": "user", "content": _live_task_prompt(sc.BASE_POLICY_CONTENT)},
                {"role": "assistant", "content": counter_first},
                {"role": "user", "content": _live_finalize_prompt(_policy_of(counter_first))},
            ]
        )
        counter_final = _policy_of(counter_second)
    except (QualificationError, OSError) as exc:
        return {
            "schema": RUN_SCHEMA,
            "mode": "live",
            "status": "capped",
            "reason": str(exc)[:400],
            "gateway": {"url": gateway_url, "model": gateway_model},
            "spend": _spend_of(tracker, capped=True),
        }
    except Exception as exc:  # noqa: BLE001 — an honest refusal, never a crash
        return {
            "schema": RUN_SCHEMA,
            "mode": "live",
            "status": "refused",
            "reason": f"{type(exc).__name__}: {exc}"[:400],
            "gateway": {"url": gateway_url, "model": gateway_model},
            "spend": _spend_of(tracker, capped=True),
        }

    base = {sc.POLICY_PATH: sc.BASE_POLICY_CONTENT}
    events = [
        event(first_edit_at, "vendor_edits", touched=[sc.POLICY_PATH], phase="pre-instruction"),
        event(
            steer_injected_at,
            "steer_consumed",
            command_id=steer_command_id,
            text=sc.STEERING_TASK_INSTRUCTION,
            source="conversation-injection",
        ),
        event(
            steered_edit_at, "vendor_edits_after_steer", touched=[sc.POLICY_PATH], applied="live"
        ),
    ]
    steered_run = sc.SteeringRun(
        arm="steered",
        provenance=f"live-model:{gateway_model}",
        command=sc.SteeringCommandEvidence(
            command_id=steer_command_id,
            kind="steer",
            text=sc.STEERING_TASK_INSTRUCTION,
            status="received",
            received_at=steer_accepted_at,
        ),
        vendor_events=tuple(sc.read_vendor_events([json.dumps(entry) for entry in events])),
        edits=sc.edit_set_of(base, {sc.POLICY_PATH: steered_final}),
        counterfactual_edits=sc.edit_set_of(base, {sc.POLICY_PATH: counter_final}),
    )
    grade = sc.grade_causality(steered_run)
    return {
        "schema": RUN_SCHEMA,
        "mode": "live",
        "status": "graded",
        "gateway": {"url": gateway_url, "model": gateway_model},
        "grade": grade.as_document(),
        "run": steered_run.as_document(),
        "spend": _spend_of(tracker, capped=False),
        "counterfactual_status": "ran within caps",
    }


def _spend_of(tracker: SpendTracker, *, capped: bool) -> dict[str, Any]:
    return {
        "calls": tracker.total_calls,
        "tokens_in": tracker.tokens_in,
        "tokens_out": tracker.tokens_out,
        "estimate_usd": tracker.estimate_usd,
        "cost_state": f"capped-estimate({RATE_CARD_VERSION})",
        "capped": capped,
        "spend_cap_usd": tracker.spend_cap_usd,
    }


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="run_steering_qualification.py",
        description="The steering-causality qualification (R37-10 / AT-10).",
    )
    parser.add_argument("--out", default=str(REPO_ROOT / "evaluation" / "steering"))
    parser.add_argument("--scripted", action="store_true", help="the scripted-causal arms")
    parser.add_argument("--live", action="store_true", help="one capped live gateway attempt")
    parser.add_argument("--gateway-url", default="", help="the lab gateway base URL")
    parser.add_argument("--gateway-model", default="", help="the gateway model name")
    args = parser.parse_args(argv)
    if not (args.scripted or args.live):
        parser.error("choose --scripted and/or --live")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "issue": "R37-10 / #291",
        "generated_at": _now_iso(),
        "modes": {},
    }

    if args.scripted:
        with tempfile.TemporaryDirectory(prefix="steer-qual-") as tmp:
            scripted = await run_scripted_qualification(Path(tmp))
        (out_dir / SCRIPTED_RUN_FILE).write_text(json.dumps(scripted, indent=2) + "\n")
        report["modes"]["scripted"] = {
            "status": "graded",
            "causal": scripted["grade"]["causal"],
            "provenance": scripted["provenance"],
            "grade": scripted["grade"],
            "run_file": SCRIPTED_RUN_FILE,
        }

    if args.live:
        gateway_url = args.gateway_url or os.environ.get(GATEWAY_URL_ENV, "")
        gateway_model = args.gateway_model or "fast"
        if not gateway_url.strip():
            live = {
                "schema": RUN_SCHEMA,
                "mode": "live",
                "status": "refused",
                "reason": (
                    f"no gateway URL (pass --gateway-url or set {GATEWAY_URL_ENV}) — "
                    "live provenance is never fabricated"
                ),
                "gateway": {"url": "", "model": gateway_model},
            }
        else:
            with tempfile.TemporaryDirectory(prefix="steer-live-") as tmp:
                live = await run_live_qualification(Path(tmp), gateway_url, gateway_model)
        (out_dir / LIVE_RUN_FILE).write_text(json.dumps(live, indent=2) + "\n")
        report["modes"]["live"] = {
            "status": live.get("status"),
            "causal": (live.get("grade") or {}).get("causal"),
            "provenance": (live.get("run") or {}).get("provenance", ""),
            "grade": live.get("grade"),
            "spend": live.get("spend"),
            "run_file": LIVE_RUN_FILE,
        }

    report["honesty"] = {
        "scripted": (
            "the scripted arm's vendor is the REACTIVE SCRIPT (scripted-causal): it "
            "provably reads the durable guidance mid-turn and its next edit depends "
            "on what it read — causal by construction, never a real-model result"
        ),
        "live": (
            "the live arm is ONE capped attempt through the lab gateway; spend "
            f"figures are estimates against {RATE_CARD_VERSION}, never billing records"
        ),
        "revision_legs": (
            "the revision / WIP-reuse / old-epoch-replay legs of AT-10 are proven by "
            "tests/production_entry/test_causal_steering.py over the real revision "
            "lifecycle (three-way digest equality, checkpoint reuse, stale-authority "
            "replay) — this report points there, it does not repeat them"
        ),
    }
    (out_dir / REPORT_FILE).write_text(json.dumps(report, indent=2) + "\n")
    print(f"steering qualification: {json.dumps(report['modes'], indent=2)}")
    return 0


def main(argv: list[str]) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
