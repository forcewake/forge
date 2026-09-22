"""forge lane_driver — the claude-sdk-lane's runner-side entry (EXE-02).

The lane job (``ci/templates/claude-sdk-lane.gitlab-ci.yml``) installs
forge's ``interactive`` extra inside the ephemeral runner and runs
``python -m forge.lane_driver``. This module is the INTERACTIVE twin of
the batch harness (``claude -p`` one-shot): instead of a scripted CLI
invocation it drives ONE task through the REAL interactive driver client
(:class:`forge.adaptive.drivers.claude_sdk.ClaudeSDKDriverClient`,
built via :func:`claude_sdk_client_from_env`) — ``start_session`` →
poll-drain ``query()`` until a piece carries the ``ResultMessage``
fingerprint → ``close`` — inside the SAME proposal-only lane the batch
template established:

- the brief comes from ``.forge/brief.md`` (written by the lane job from
  ``$FORGE_PLAN``) — NEVER ``/tmp``: the brief is run context the
  artifacts/story must be able to point at, and ``/tmp`` violates the
  sandbox lesson the lane lives by;
- no credentials beyond the gateway env (``ANTHROPIC_BASE_URL`` /
  ``ANTHROPIC_AUTH_TOKEN`` ride ambient), no push (the lane job's git
  push is FORBIDDEN at the remote), and the mechanical commit/push deny
  already lives in the driver client — this module adds no capability;
- the candidate contract is BYTE-COMPATIBLE with the batch lane: the
  working-tree edits ARE the candidate; the template stages and diffs
  them against the frozen attempt base, and THIS module writes the meta
  the worker's candidate collection consumes
  (``forge.runs.backends.CIHarnessBackend._collect_candidate``):
  ``.forge/candidate.meta.json`` with ``attempt_base`` / ``driver`` /
  ``model`` / ``exit`` / ``usage`` (+ the additive ``terminal_reason``
  audit field) and the ``.forge/usage.json`` receipt beside it.

Exit-code semantics mirror the batch lane: 0 = the turn completed
cleanly (``ResultMessage.terminal_reason == "completed"`` and no error
subtype); nonzero = anything else (aborted/error/budget), with the
reason recorded in the meta. The template swallows the nonzero exit (||
FORGE_DRIVER_EXIT="failed") so the job stays green, the artifacts still
upload (``when: always``), and the WORKER classifies the run from the
meta's ``exit`` — never from the trace.

Usage receipt honesty (F22 lite, same doctrine as the batch lane):
tokens and cost come ONLY from the drained ``ResultMessage``
(``usage`` / ``total_cost_usd`` / ``model_usage`` where present);
anything absent stays absent — unknown is never zero. No receipt fields
are fabricated from anywhere else.

The SAME contract now drives two more REAL interactive clients
(``--driver codex | opencode``, or ``FORGE_LANE_DRIVER``; ``claude``
stays the default and its path is unchanged):

- ``codex`` — :class:`forge.adaptive.drivers.codex_app.CodexAppDriverClient`
  (built via :func:`codex_app_client_from_env`; the spawned ``codex
  app-server`` inherits auth from ``~/.codex/auth.json`` or
  ``OPENAI_API_KEY``/``CODEX_API_KEY``). ``start_thread`` answers at turn
  ACCEPTANCE, so the lane poll-watches the client's buffered notifications
  for the thread's ``turn/completed`` (status ``completed`` → exit
  completed; ``interrupted``/``failed`` → failed, the vendor status rides
  the meta) and takes usage from the LAST ``thread/tokenUsage/updated``.
- ``opencode`` — a lane-local ``opencode serve`` spawned by
  :class:`forge.adaptive.drivers.opencode_serve.OpenCodeServer` plus the
  real HTTP client (:func:`opencode_client_from_env` with the spawner's
  loopback URL + pinned password merged over the ambient env; the
  zai-coding-plan-style route is ``OPENCODE_PROVIDER_ID`` /
  ``OPENCODE_MODEL_ID``, the generic BYOK provider key is
  ``OPENCODE_PROVIDER_API_KEY``). The client waits the turn inside
  ``start_session``; the lane then reads the buffered
  ``session.execution.succeeded|failed`` verdict (transcript-reconciled
  assistant messages as the fallback when the event stream was
  uncovered). There is no cost API: a receipt built from
  ``session.usage.updated`` tokens (when the event was seen) carries
  ``completeness: "unknown"`` — input/output at best, never a fabricated
  cost, and no event means no receipt at all. Permission prompts are
  answered ``once`` (LIVE-found: the factory default ``reject`` starves
  every tool call in a task lane).

Both lanes keep the claude lane's budget doctrine: one overall
``FORGE_LANE_BUDGET_SECONDS`` wall clock; on expiry the running turn is
interrupted (codex ``turn/interrupt`` — opencode's timed-out
``start_session`` never yields the session id, so the serve teardown
bounds the orphaned turn), a short grace window still honors the
vendor's own terminal verdict, and only a turn that produces nothing at
all ends as ``budget_exceeded``. The process exits nonzero and the
reason rides the meta, exactly like the claude path.

NXT-11 — the steering bridge is attached to the lane lifecycle. With
``FORGE_STEERING_ENABLED`` truthy (default OFF), each driven turn runs one
:class:`forge.adaptive.lane_control.LaneSteeringSession` over the SAME
client object the lane drives: the vendor session/thread id is bound
the moment it exists (claude: right after ``start_session``; codex:
after ``start_thread`` answers at turn acceptance; opencode: after
``start_session`` returns at turn completion — the only id-existence
point that lane has), the bounded mailbox drain runs CONCURRENTLY with
the turn (the bridge's async-context seam), and the append-only
``steering_journal`` rides the meta beside the usage receipt. A lane
with steering disabled runs byte-for-byte the old path — no session, no
drain task, no journal key.

NXT-10 — the outbound leg. The steering attach's mailbox is
:func:`steering_service_from_env`'s single seam: when the dispatch env
carries ``FORGE_LANE_CONTROL_URL`` + ``FORGE_LANE_CONTROL_TOKEN``, the
drain's pending/authorize/apply/checkpoint calls run against the
control plane's durable rows through
:class:`forge.adaptive.lane_channel.LaneControlChannel` (the lane dials
OUT — EXE-04; the channel's fetch loop is bounded by the driven turn
via :func:`_steering_scope`, and its error rows ride the meta after the
actions). Without the pair the lane-local in-memory mailbox remains —
the honest default, never a half-configured dial-out.

NXT-28 — every driven outcome carries an ``episode`` timing breakdown
in the meta (:data:`EPISODE_PHASE_KEYS`): seconds spent in startup
(session/thread/server start), the driven turn, the interrupt+grace
window, and teardown. The values are wall-clock deltas between the
timestamps the lane ALREADY takes at its own phase boundaries —
structured and recorded, never invented: a phase this lane never
reached records ``null`` (interrupt_grace_s on a turn that completed
without an interrupt; the opencode lane has no separate interrupt
phase at all), never ``0.0``. Outcomes that fail BEFORE any driving
starts (brief_missing, sdk_missing, driver_setup_error ...) carry no
``episode`` key — there was no episode to time. This measures the full
interactive episode the budget doctrine describes: startup cannot hide
inside the turn, teardown cannot hide after the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from forge.adaptive.adapters import (
    ClaudeSDKAdapter,
    CodexAppAdapter,
    OpenCodeAdapter,
)
from forge.adaptive.drivers.claude_sdk import (
    ClaudeSDKDriverClient,
    claude_sdk_client_from_env,
)
from forge.adaptive.drivers.codex_app import (
    CodexAppError,
    codex_app_client_from_env,
)
from forge.adaptive.drivers.opencode import opencode_client_from_env
from forge.adaptive.drivers.opencode_serve import opencode_server_from_env
from forge.adaptive.lane_channel import LaneControlChannel, lane_channel_from_env
from forge.adaptive.lane_control import LaneSteeringSession
from forge.adaptive.wiring import OperatorControlService

__all__ = [
    "CODEX_LANE_DRIVER_ID",
    "EPISODE_PHASE_KEYS",
    "LANE_DRIVER_ID",
    "LANE_DRIVER_IDS",
    "NO_COMMIT_ADDENDUM",
    "OPENCODE_LANE_DRIVER_ID",
    "STEERING_ENV",
    "LaneOutcome",
    "build_task",
    "classify_codex_turn",
    "classify_opencode_events",
    "classify_result",
    "codex_usage_receipt",
    "drive_codex_lane",
    "drive_lane",
    "main",
    "opencode_usage_receipt",
    "run_opencode_lane",
    "steering_enabled",
    "steering_service_from_env",
    "usage_receipt",
    "write_artifacts",
]

#: The harness id this lane registers under
#: (:data:`forge.runs.harness_selection.SHIPPED_DRIVERS`) — the id the
#: worker dispatches as ``FORGE_HARNESS_DRIVER`` and the meta's ``driver``.
LANE_DRIVER_ID = "claude-sdk-lane"

#: The codex lane's registered harness id — the codex twin of
#: :data:`LANE_DRIVER_ID` (one GitLab template, one driver id, same meta).
CODEX_LANE_DRIVER_ID = "codex-sdk-lane"

#: The opencode lane's registered harness id.
OPENCODE_LANE_DRIVER_ID = "opencode-sdk-lane"

#: ``--driver`` key → registered harness id (the meta ``driver`` and the
#: template filter value). The worker dispatches the id; the lane takes
#: the short key on the CLI.
LANE_DRIVER_IDS: dict[str, str] = {
    "claude": LANE_DRIVER_ID,
    "codex": CODEX_LANE_DRIVER_ID,
    "opencode": OPENCODE_LANE_DRIVER_ID,
}

#: The brief file the lane job writes (``$FORGE_PLAN``) and the agent
#: works from. In-repo control directory, never /tmp.
_BRIEF_PATH = ".forge/brief.md"

#: The artifacts this module writes (the template uploads them; the diff
#: itself is staged/diffed by the template like the batch lane's).
_META_PATH = ".forge/candidate.meta.json"
_USAGE_PATH = ".forge/usage.json"

#: Overall wall-clock budget for the ONE turn (default: the lane job's
#: 30m timeout / the worker's harness deadline, whichever binds first).
_DEFAULT_BUDGET_S = 1800.0
#: Grace window for the ABORTED turn's ResultMessage after a budget
#: interrupt (the vendor guarantees one — bounded anyway, the lane never
#: hangs on a missing ack).
_DEFAULT_GRACE_S = 60.0
#: Drain poll cadence (each ``query()`` drains everything buffered so far).
_POLL_INTERVAL_S = 0.25

#: The unattended posture sentence appended to the task — the same words
#: the batch lane's ``-p`` prompt carries. The brief is the contract; this
#: is the lane's one reminder that the working tree IS the deliverable.
NO_COMMIT_ADDENDUM = (
    "Work in the current repository. Do NOT commit and do NOT push: leave "
    "your changes in the working tree for collection."
)

#: The episode timing breakdown's keys (NXT-28) — the phases of ONE
#: driven interactive episode, recorded in the meta as the ``episode``
#: dict. Values are wall-clock seconds measured at the lane's OWN phase
#: boundaries; a phase the lane never reached is ``None`` (never 0.0 —
#: an unmeasured phase is not a zero-length phase).
EPISODE_PHASE_KEYS: tuple[str, ...] = (
    "startup_s",  # session/thread/server start (before the turn is driven)
    "turn_s",  # the driven turn, up to its terminal verdict or budget expiry
    "interrupt_grace_s",  # the interrupt request + grace window after budget expiry
    "teardown_s",  # client close / server teardown after the verdict
)


def _episode(
    startup_s: float,
    turn_s: float,
    interrupt_grace_s: float | None,
    teardown_s: float,
) -> dict[str, float | None]:
    """Assemble the ``episode`` breakdown (rounded, phase keys fixed)."""
    return {
        "startup_s": round(startup_s, 6),
        "turn_s": round(turn_s, 6),
        "interrupt_grace_s": None if interrupt_grace_s is None else round(interrupt_grace_s, 6),
        "teardown_s": round(teardown_s, 6),
    }


# ---------------------------------------------------------------------------
# The steering attach (NXT-11) — the driven turn + the control consumer
# ---------------------------------------------------------------------------

#: The honest-rollout switch for the lane-side steering consumer. Default
#: OFF. With the flag on, the consumer's backing store is decided by the
#: dispatch env: ``FORGE_LANE_CONTROL_URL`` + ``FORGE_LANE_CONTROL_TOKEN``
#: swap in the remote :class:`~forge.adaptive.lane_channel.LaneControlChannel`
#: (NXT-10's outbound leg — the control plane's durable rows, dialed out);
#: without the pair it stays the lane-local (in-memory) mailbox. Truthy
#: spellings are the same closed set the driver factories use for their
#: booleans; anything else fails CLOSED (off).
STEERING_ENV = "FORGE_STEERING_ENABLED"
_STEERING_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})

#: driver key -> the adapter that wraps the SAME client object the lane
#: drives (the pairing rule the bridge checks at construction).
_STEERING_ADAPTERS: dict[str, type] = {
    "claude": ClaudeSDKAdapter,
    "codex": CodexAppAdapter,
    "opencode": OpenCodeAdapter,
}


def steering_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``FORGE_STEERING_ENABLED`` is truthy in *env* (default: off)."""
    source = os.environ if env is None else env
    return source.get(STEERING_ENV, "").strip().lower() in _STEERING_TRUTHY


def steering_service_from_env(
    env: Mapping[str, str] | None = None,
) -> OperatorControlService | None:
    """The lane's control consumer when steering is enabled, else None.

    NXT-10's outbound leg: when the dispatch env carries BOTH
    ``FORGE_LANE_CONTROL_URL`` and ``FORGE_LANE_CONTROL_TOKEN``
    (:func:`forge.adaptive.lane_channel.lane_channel_from_env`), the
    service's mailbox is the :class:`~forge.adaptive.lane_channel.
    LaneControlChannel` — the SAME sync drain surface over the control
    plane's durable ``control_commands`` rows, polled out through the
    lane-dials-out API. The lane process still receives NO control-plane
    credentials beyond its own work-scoped token. Otherwise (the flag
    off, or the pair unset) this stays the lane-local in-memory mailbox —
    the honest default, never a half-configured dial-out.
    """
    if not steering_enabled(env):
        return None
    channel = lane_channel_from_env(env)
    if channel is not None:
        return channel.service()
    return OperatorControlService()


def _steering_session(
    control: OperatorControlService | None,
    *,
    driver_key: str,
    client: Any,
    env: Mapping[str, str] | None = None,
) -> LaneSteeringSession | None:
    """Build the lane's steering session over the SAME client the lane drives.

    Identity comes from the dispatch env: ``FORGE_RUN_ID`` (always set by
    the lane templates' rules — it scopes run-scoped commands) and
    ``FORGE_WORK_ID``, falling back to the run id until the dispatch
    template carries a distinct work id (integration note: the control
    plane books commands per work; align ``FORGE_WORK_ID`` with it before
    the flag goes ON outside tests). No usable work id at all → None:
    a mailbox with nothing to scope to stays honestly detached rather
    than attached under a guessed key.
    """
    if control is None:
        return None
    source = os.environ if env is None else env
    run_id = (source.get("FORGE_RUN_ID") or "").strip()
    work_id = (source.get("FORGE_WORK_ID") or run_id).strip()
    if not work_id:
        return None
    return LaneSteeringSession(
        service=control,
        driver=_STEERING_ADAPTERS[driver_key](client=client),
        driver_kind=driver_key,  # type: ignore[arg-type]  (keyed by LANE_DRIVER_IDS)
        run_id=run_id,
        work_id=work_id,
    )


def _steering_journal(steering: LaneSteeringSession) -> list[dict[str, Any]]:
    """The session's append-only journal as the meta's ``steering_journal``.

    Over the remote channel, the CHANNEL's own error/evidence rows ride
    after the actions — an unreachable control plane is visible in the
    meta (steering degraded, never silently dead), while the turn's own
    journal stays the actions it actually took.
    """
    rows = [asdict(action) for action in steering.journal]
    mailbox = getattr(steering.service, "mailbox", None)
    if isinstance(mailbox, LaneControlChannel):
        rows.extend(mailbox.channel_journal)
    return rows


def _bind_steering(steering: LaneSteeringSession | None, vendor_id: str) -> None:
    """Bind the vendor id to the session AND the channel it drains over.

    The channel carries the vendor session id on its dispatch acks (the
    correlation a recovery pass probes); the local path binds only the
    session, exactly as before.
    """
    if steering is None:
        return
    steering.bind(vendor_id)
    mailbox = getattr(steering.service, "mailbox", None)
    if isinstance(mailbox, LaneControlChannel):
        mailbox.bind_vendor_session(vendor_id)


@contextlib.asynccontextmanager
async def _steering_scope(steering: LaneSteeringSession | None, poll_s: float):
    """Enter the steering attach — and the remote channel's fetch loop.

    The channel's poller is bounded by the driven turn's own lifetime:
    entered beside the attach, torn down with it. The lane-local path
    (no channel behind the service) enters the attach exactly as before.
    """
    if steering is None:
        yield
        return
    mailbox = getattr(steering.service, "mailbox", None)
    channel_cm = mailbox if isinstance(mailbox, LaneControlChannel) else contextlib.nullcontext()
    async with channel_cm:
        async with steering.attach(poll_interval=poll_s):
            yield


@dataclass(frozen=True)
class LaneOutcome:
    """What the ONE driven turn came to, plus its usage receipt."""

    #: ``completed`` | ``failed`` — the meta ``exit`` classification the
    #: worker reads (never the process exit code alone).
    exit_status: str
    #: The vendor ``terminal_reason`` (``completed`` / ``aborted_*`` / ...)
    #: or the lane's own reason (``budget_exceeded`` / ``brief_missing`` /
    #: ``sdk_missing`` / ``driver_error`` ...).
    terminal_reason: str
    #: The usage receipt built from the ResultMessage, or None (unknown).
    usage: dict[str, Any] | None = None
    #: Setup/driver failure detail (audit only, truncated by the writer).
    error: str = ""
    #: The agent's last visible text (bounded) — diagnosis for
    #: completed-but-empty turns (LIVE-found: a 16 s "completed" turn
    #: with no file changes needs its answer visible in the meta).
    reply_excerpt: str | None = None
    #: The steering bridge's append-only journal (NXT-11) — carried into
    #: the meta as ``steering_journal`` ONLY when the lane ran with
    #: steering attached; None (key absent) when the gate was off.
    steering_journal: list[dict[str, Any]] | None = None
    #: The episode timing breakdown (NXT-28) — :data:`EPISODE_PHASE_KEYS`
    #: seconds for THIS outcome, carried into the meta as ``episode``;
    #: None (key absent) when the lane failed before any driving started
    #: (no episode existed to time).
    episode: dict[str, float | None] | None = None


def build_task(brief_text: str, issue_iid: str = "") -> str:
    """The ONE task handed to ``start_session``: the brief plus the posture.

    The batch lane points the CLI at ``.forge/brief.md`` with a one-liner;
    the interactive driver takes the task directly, so the brief bytes are
    inlined (no dependency on a Read tool call to find the contract) and
    the unattended posture (:data:`NO_COMMIT_ADDENDUM`) is appended — the
    same sentence the batch ``-p`` prompt carries.
    """
    header = "Implement the approved task below"
    iid = str(issue_iid or "").strip()
    if iid:
        header += f" for issue #{iid}"
    brief = str(brief_text or "").strip()
    return f"{header}.\n\n{brief}\n\n{NO_COMMIT_ADDENDUM}"


def _is_result_piece(piece: Any) -> bool:
    """The ResultMessage fingerprint: ``is_error`` + ``num_turns``.

    The drained pieces are ``asdict`` dumps of the vendor dataclasses
    (no type information survives), so recognition is the same duck check
    the driver's own ``_is_result_message`` falls back to and its tests
    use — no normalized schema is invented here either.
    """
    return isinstance(piece, dict) and "is_error" in piece and "num_turns" in piece


def classify_result(result: dict[str, Any]) -> tuple[str, str]:
    """(exit classification, terminal reason) for a drained ResultMessage.

    ``completed`` requires ``terminal_reason == "completed"`` (the
    LIVE-verified value on a clean turn) and no error markers. A missing
    ``terminal_reason`` on an otherwise-successful result stays completed
    (pre-0.2.118 stacks); everything else — aborted_*, error subtypes —
    classifies failed with the reason carried into the meta.
    """
    terminal = str(result.get("terminal_reason") or "").strip()
    subtype = str(result.get("subtype") or "").strip()
    if result.get("is_error") is True or subtype.startswith("error_"):
        # Error markers beat a terminal_reason that claims completion —
        # the vendor error subtype is the honest reason for the meta.
        reason = subtype if subtype.startswith("error_") else (terminal or subtype or "failed")
        return "failed", reason
    if terminal in ("", "completed"):
        return "completed", terminal or "completed"
    return "failed", terminal


def usage_receipt(result: dict[str, Any]) -> dict[str, Any] | None:
    """The meta ``usage`` receipt from a drained ResultMessage.

    Maps the vendor's Anthropic-shaped ``usage`` counters onto the
    receipt shape the worker parses (:meth:`forge.runs.candidate.
    HarnessUsage.from_meta`): ``input_tokens`` already EXCLUDES the cache
    (disjoint counters, never folded), ``cache_read_input_tokens`` →
    ``cached_input_tokens``, ``cache_creation_input_tokens`` →
    ``cache_write_tokens``. ``total_cost_usd`` and ``model_usage`` ride
    along verbatim where present (additive telemetry). Anything absent or
    malformed is dropped — unknown stays unknown, never zero — and a
    result with nothing reportable yields None (not a zeroed dict).
    """
    raw = result.get("usage")
    source: dict[str, Any] = raw if isinstance(raw, dict) else {}

    def _token(key: str) -> int | None:
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    receipt: dict[str, Any] = {}
    for vendor_key, meta_key in (
        ("input_tokens", "input_tokens"),
        ("cache_read_input_tokens", "cached_input_tokens"),
        ("cache_creation_input_tokens", "cache_write_tokens"),
        ("output_tokens", "output_tokens"),
    ):
        token = _token(vendor_key)
        if token is not None:
            receipt[meta_key] = token
    cost = result.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        receipt["total_cost_usd"] = float(cost)
    model_usage = result.get("model_usage")
    if isinstance(model_usage, dict) and model_usage:
        receipt["model_usage"] = model_usage
    if not receipt:
        return None
    receipt["driver"] = LANE_DRIVER_ID
    receipt["completeness"] = "aggregate"
    receipt["source"] = "claude-agent-sdk"
    return receipt


async def _drain_until_result(
    client: ClaudeSDKDriverClient,
    session_id: str,
    *,
    deadline: float,
    poll_s: float,
) -> dict[str, Any] | None:
    """Poll-drain ``query()`` until a ResultMessage piece or *deadline*.

    Draining CONSUMES (the driver's contract), so every poll takes what
    the background pump buffered and keeps only the LAST result piece of
    a batch — the terminal one when a vendor ever stacks them.
    """
    loop = asyncio.get_running_loop()
    result: dict[str, Any] | None = None
    while True:
        for piece in await client.query(session_id):
            if _is_result_piece(piece):
                result = piece
        if result is not None or loop.time() >= deadline:
            return result
        await asyncio.sleep(poll_s)


async def drive_lane(
    client: ClaudeSDKDriverClient,
    *,
    task: str,
    budget_s: float,
    grace_s: float = _DEFAULT_GRACE_S,
    poll_s: float = _POLL_INTERVAL_S,
    control: OperatorControlService | None = None,
) -> LaneOutcome:
    """Drive the ONE task to its terminal ResultMessage, bounded.

    ``start_session(task)`` → poll-drain until the turn's ResultMessage,
    the WHOLE turn bounded by *budget_s*; on budget expiry the running
    turn is interrupted (the driver bounds the ack itself) and gets a
    short *grace_s* window to produce the aborted turn's ResultMessage
    (the vendor emits one even when the ack never returns) — only a turn
    that produces nothing at all ends as ``budget_exceeded``. The session
    is ALWAYS closed: teardown is bounded inside the driver, so no path
    out of here hangs the lane.

    NXT-11: with *control* supplied, one
    :class:`~forge.adaptive.lane_control.LaneSteeringSession` rides the
    SAME client — bound to the session id the moment ``start_session``
    answers, its bounded mailbox drain running CONCURRENTLY with the
    turn (the async-context seam), its journal returned on the outcome.

    NXT-28: the outcome carries the ``episode`` breakdown — startup
    (``start_session``), the drained turn, the interrupt+grace window
    (only when the budget expired it into one) and teardown (``close``).
    """
    loop = asyncio.get_running_loop()
    startup_started = loop.time()
    session_id = await client.start_session(task)
    startup_s = loop.time() - startup_started
    steering = _steering_session(control, driver_key="claude", client=client)
    _bind_steering(steering, session_id)

    async def _turn() -> tuple[LaneOutcome, float, float | None]:
        turn_started = loop.time()
        deadline = turn_started + budget_s
        result = await _drain_until_result(client, session_id, deadline=deadline, poll_s=poll_s)
        interrupt_grace_s: float | None = None
        if result is None:
            await client.interrupt(session_id)
            grace_started = loop.time()
            result = await _drain_until_result(
                client, session_id, deadline=grace_started + grace_s, poll_s=poll_s
            )
            interrupt_grace_s = loop.time() - grace_started
            turn_s = loop.time() - turn_started
            if result is None:
                return (
                    LaneOutcome(exit_status="failed", terminal_reason="budget_exceeded"),
                    turn_s,
                    interrupt_grace_s,
                )
        else:
            turn_s = loop.time() - turn_started
        exit_status, reason = classify_result(result)
        return (
            LaneOutcome(
                exit_status=exit_status,
                terminal_reason=reason,
                usage=usage_receipt(result),
            ),
            turn_s,
            interrupt_grace_s,
        )

    try:
        async with _steering_scope(steering, poll_s):
            outcome, turn_s, interrupt_grace_s = await _turn()
    finally:
        teardown_started = loop.time()
        await client.close(session_id)
        teardown_s = loop.time() - teardown_started
    if steering is not None:
        outcome = replace(outcome, steering_journal=_steering_journal(steering))
    return replace(
        outcome,
        episode=_episode(startup_s, turn_s, interrupt_grace_s, teardown_s),
    )


# ---------------------------------------------------------------------------
# The codex lane (CodexAppDriverClient over ``codex app-server`` stdio)
# ---------------------------------------------------------------------------


def _last_codex_turn_completed(events: list[dict[str, Any]], thread_id: str) -> dict | None:
    """Params of the LAST ``turn/completed`` for *thread_id*, or None.

    §7.1: turn events carry ``threadId`` context; a frame that names a
    DIFFERENT thread is skipped (the client only tracks its own threads,
    but the lane never assumes it is the only one on the connection).
    """
    found: dict | None = None
    for event in events:
        if not isinstance(event, dict) or event.get("method") != "turn/completed":
            continue
        params = event.get("params")
        params = params if isinstance(params, dict) else {}
        owner = params.get("threadId")
        if owner not in (None, thread_id):
            continue
        found = params
    return found


def classify_codex_turn(params: dict | None) -> tuple[str, str, str]:
    """(exit classification, terminal reason, error detail) for a
    ``turn/completed``.

    The LIVE-verified statuses are ``completed`` / ``interrupted`` /
    ``failed`` (docs/research/2026-09-21-codex-app-server.md §7.1 + the 0.153.4
    correction): only ``completed`` exits clean; the vendor status itself
    rides the meta as the reason; a frame without a status is classified
    from what it DID carry, never guessed.
    """
    turn = params.get("turn") if isinstance(params, dict) else None
    turn = turn if isinstance(turn, dict) else {}
    status = str(turn.get("status") or "").strip()
    error = ""
    err = turn.get("error")
    if isinstance(err, dict):
        error = str(err.get("message") or "")
    if status == "completed":
        return "completed", "completed", error
    if status:
        return "failed", status, error
    return "failed", "turn_end_unobserved", error


def _int_counter(value: Any) -> int | None:
    """A non-negative int receipt value; anything else is dropped."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


#: ``thread/tokenUsage/updated`` counter spellings → meta receipt keys,
#: first hit per category (the payload shape is not in the research doc —
#: both the camelCase and snake_case vendor spellings are accepted, and
#: nothing else is guessed).
_CODEX_TOKEN_KEYS: tuple[tuple[str, str], ...] = (
    ("inputTokens", "input_tokens"),
    ("input_tokens", "input_tokens"),
    ("cachedInputTokens", "cached_input_tokens"),
    ("cacheReadInputTokens", "cached_input_tokens"),
    ("cached_input_tokens", "cached_input_tokens"),
    ("cacheCreationInputTokens", "cache_write_tokens"),
    ("cacheWriteInputTokens", "cache_write_tokens"),
    ("cache_write_tokens", "cache_write_tokens"),
    ("outputTokens", "output_tokens"),
    ("output_tokens", "output_tokens"),
)


def _codex_last_agent_text(events: list[dict[str, Any]]) -> str | None:
    """The last agentMessage delta text, bounded — turn diagnosis."""
    text: str | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        params = event.get("params") or {}
        item = params.get("item") or {}
        if item.get("itemType") == "agentMessage" or params.get("itemType") == "agentMessage":
            delta = item.get("delta") or params.get("delta")
            if isinstance(delta, str) and delta.strip():
                text = delta
    return text[:400] if text else None


def codex_usage_receipt(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The meta ``usage`` receipt from the LAST ``thread/tokenUsage/updated``.

    The event is a per-thread running aggregate, so the last one seen
    wins (never a sum — summing a running total double-counts). The
    counters may ride the params directly or one dict deep; anything
    unparseable is dropped and a stream without the event yields None —
    unknown stays unknown, never zero.
    """
    params: dict | None = None
    for event in events:
        if isinstance(event, dict) and event.get("method") == "thread/tokenUsage/updated":
            raw = event.get("params")
            params = raw if isinstance(raw, dict) else {}
    if params is None:
        return None
    receipt: dict[str, Any] = {}
    # The counters may ride the params directly or one dict deep (shape
    # unverified) — every nesting is scanned, first key hit per category.
    for nested in (params, params.get("usage"), params.get("tokenUsage")):
        if not isinstance(nested, dict):
            continue
        for vendor_key, meta_key in _CODEX_TOKEN_KEYS:
            if meta_key in receipt:
                continue
            token = _int_counter(nested.get(vendor_key))
            if token is not None:
                receipt[meta_key] = token
    if not receipt:
        return None
    receipt["driver"] = CODEX_LANE_DRIVER_ID
    receipt["completeness"] = "aggregate"
    receipt["source"] = "codex-app-server"
    return receipt


async def _poll_codex_completion(
    client: Any,
    thread_id: str,
    *,
    deadline: float,
    poll_s: float,
) -> dict | None:
    """Poll the client's buffered notifications for the thread's
    ``turn/completed`` (``events()`` re-reads the whole buffer — it does
    not consume — so every poll re-scans cheaply and keeps the LAST
    verdict)."""
    loop = asyncio.get_running_loop()
    while True:
        params = _last_codex_turn_completed(client.events(), thread_id)
        if params is not None or loop.time() >= deadline:
            return params
        await asyncio.sleep(poll_s)


async def drive_codex_lane(
    client: Any,
    *,
    task: str,
    budget_s: float,
    grace_s: float = _DEFAULT_GRACE_S,
    poll_s: float = _POLL_INTERVAL_S,
    control: OperatorControlService | None = None,
) -> LaneOutcome:
    """Drive ONE codex thread to its ``turn/completed``, bounded.

    ``start_thread(task)`` returns at turn ACCEPTANCE (LIVE-verified:
    the ``turn/start`` response is not the turn result), so the lane
    watches the notification buffer until the thread's verdict. On budget
    expiry the turn is interrupted — completion keyed off the vendor's
    ``turn/completed(interrupted)`` notification, never the method
    response (§5.3) — and a short grace window still honors that verdict;
    only a turn that produces no verdict at all ends ``budget_exceeded``.
    The client connection is ALWAYS closed (teardown is bounded inside
    the driver, so no path out of here hangs the lane).

    NXT-11: with *control* supplied, the steering session binds the
    thread id at acceptance (the vendor id's existence point — commands
    arriving earlier are refused by the bridge, never guessed at) and
    drains CONCURRENTLY with the poll loop. The notification buffer is a
    non-consuming re-read, so the bridge's vendor calls never compete
    with the lane's own drain for the event stream.

    NXT-28: the outcome carries the ``episode`` breakdown — startup
    (``start_thread`` to acceptance), the polled turn, the
    interrupt+grace window (only when the budget expired it into one)
    and teardown (``close``).
    """
    loop = asyncio.get_running_loop()
    startup_started = loop.time()
    thread_id = await client.start_thread(task)
    startup_s = loop.time() - startup_started
    steering = _steering_session(control, driver_key="codex", client=client)
    _bind_steering(steering, thread_id)

    async def _turn() -> tuple[LaneOutcome, float, float | None]:
        turn_started = loop.time()
        deadline = turn_started + budget_s
        params = await _poll_codex_completion(client, thread_id, deadline=deadline, poll_s=poll_s)
        interrupt_grace_s: float | None = None
        if params is None:
            try:
                await asyncio.wait_for(client.interrupt(thread_id), timeout=grace_s)
            except TimeoutError:
                pass  # the grace poll below is the turn's last chance
            except CodexAppError:
                # e.g. the turn ended by itself at the deadline — the
                # buffered verdict (if any) decides, never the error.
                pass
            grace_started = loop.time()
            params = await _poll_codex_completion(
                client, thread_id, deadline=grace_started + grace_s, poll_s=poll_s
            )
            interrupt_grace_s = loop.time() - grace_started
            turn_s = loop.time() - turn_started
            if params is None:
                return (
                    LaneOutcome(exit_status="failed", terminal_reason="budget_exceeded"),
                    turn_s,
                    interrupt_grace_s,
                )
        else:
            turn_s = loop.time() - turn_started
        exit_status, reason, error = classify_codex_turn(params)
        return (
            LaneOutcome(
                exit_status=exit_status,
                terminal_reason=reason,
                usage=codex_usage_receipt(client.events()),
                error=error,
                reply_excerpt=_codex_last_agent_text(client.events()),
            ),
            turn_s,
            interrupt_grace_s,
        )

    try:
        async with _steering_scope(steering, poll_s):
            outcome, turn_s, interrupt_grace_s = await _turn()
    finally:
        teardown_started = loop.time()
        await client.close()
        teardown_s = loop.time() - teardown_started
    if steering is not None:
        outcome = replace(outcome, steering_journal=_steering_journal(steering))
    return replace(
        outcome,
        episode=_episode(startup_s, turn_s, interrupt_grace_s, teardown_s),
    )


# ---------------------------------------------------------------------------
# The opencode lane (lane-local ``opencode serve`` + the real HTTP client)
# ---------------------------------------------------------------------------

#: The v2.0.10 turn verdicts (LIVE-verified — opencode.py LIVE CORRECTION).
_OPENCODE_TURN_DONE_EVENT_TYPES = ("session.execution.succeeded", "session.execution.failed")


def _opencode_event_data(event: Any) -> dict[str, Any]:
    data = event.get("data") if isinstance(event, dict) else None
    return data if isinstance(data, dict) else {}


def _opencode_verdict_seen(events: list[dict[str, Any]], session_id: str) -> bool:
    """Whether the buffered stream already carries a classifiable verdict:
    the session's ``session.execution.*`` event, or (when the stream was
    uncovered) a terminally-finished assistant message in a reconciled
    transcript."""
    if any(
        isinstance(event, dict)
        and event.get("type") in _OPENCODE_TURN_DONE_EVENT_TYPES
        and _opencode_event_data(event).get("sessionID") == session_id
        for event in events
    ):
        return True
    return classify_opencode_events(events, session_id)[1] != "turn_end_unobserved"


def classify_opencode_events(events: list[dict[str, Any]], session_id: str) -> tuple[str, str]:
    """(exit classification, terminal reason) from the session's events.

    The buffered ``session.execution.succeeded|failed`` verdict wins (last
    one for the session). Without it — the SSE stream was uncovered and
    completion was reconciled from the transcript — the LAST terminal
    assistant message decides: ``finish: "stop"`` completed,
    ``"error"`` failed (the interrupted turn ends ``error`` with empty
    content; there is no dedicated interrupted value). Nothing observable
    at all stays honestly unobserved.
    """
    verdict = ""
    for event in events:
        if (
            isinstance(event, dict)
            and event.get("type") in _OPENCODE_TURN_DONE_EVENT_TYPES
            and _opencode_event_data(event).get("sessionID") == session_id
        ):
            verdict = str(event.get("type"))
    if verdict == "session.execution.succeeded":
        return "completed", "session.execution.succeeded"
    if verdict == "session.execution.failed":
        return "failed", "session.execution.failed"
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "transcript.reconciled":
            continue
        messages = _opencode_event_data(event).get("messages")
        for message in reversed(messages if isinstance(messages, list) else []):
            if not isinstance(message, dict) or message.get("type") != "assistant":
                continue
            finish = message.get("finish")
            if finish == "stop":
                return "completed", "transcript.stop"
            if finish == "error":
                return "failed", "transcript.error"
    return "failed", "turn_end_unobserved"


#: ``session.usage.updated`` token spellings → meta receipt keys, first hit
#: per category (input/output only — there is no cost API to read).
_OPENCODE_TOKEN_KEYS: tuple[tuple[str, str], ...] = (
    ("input", "input_tokens"),
    ("inputTokens", "input_tokens"),
    ("input_tokens", "input_tokens"),
    ("output", "output_tokens"),
    ("outputTokens", "output_tokens"),
    ("output_tokens", "output_tokens"),
)


def opencode_usage_receipt(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The meta ``usage`` receipt from ``session.usage.updated``, if seen.

    opencode serves no cost API and the event may never fire (or may miss
    tool rounds) — the receipt is completeness ``unknown`` by doctrine:
    input/output tokens at best, never a fabricated cost, and no event
    means no receipt at all (None, not a zeroed dict).
    """
    data: dict[str, Any] | None = None
    for event in events:
        if isinstance(event, dict) and event.get("type") == "session.usage.updated":
            data = _opencode_event_data(event)
    if data is None:
        return None
    sources: list[Any] = [data, data.get("tokens"), data.get("usage")]
    receipt: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for vendor_key, meta_key in _OPENCODE_TOKEN_KEYS:
            if meta_key in receipt:
                continue
            token = _int_counter(source.get(vendor_key))
            if token is not None:
                receipt[meta_key] = token
    if not receipt:
        return None
    receipt["driver"] = OPENCODE_LANE_DRIVER_ID
    receipt["completeness"] = "unknown"
    receipt["source"] = "session.usage.updated"
    return receipt


async def run_opencode_lane(
    *,
    task: str,
    budget_s: float,
    grace_s: float = _DEFAULT_GRACE_S,
    poll_s: float = _POLL_INTERVAL_S,
    env: dict[str, str] | None = None,
    control: OperatorControlService | None = None,
) -> LaneOutcome:
    """Spawn the lane-local server, drive ONE task, classify + receipt.

    The server (:class:`OpenCodeServer`) is lane-owned: loopback-bound,
    password-pinned by the spawner, torn down deterministically. The
    client factory env merges the spawner's URL/password over the ambient
    environment — ``OPENCODE_PROVIDER_ID`` / ``OPENCODE_MODEL_ID`` (and
    the generic BYOK ``OPENCODE_PROVIDER_API_KEY``) ride ambient — with
    two lane decisions: permission prompts are answered ``once``
    (LIVE-found: the factory default ``reject`` starves every tool call
    in a task lane) and the client's own turn-wait budget IS the lane
    budget. ``start_session`` returns only at turn completion, so a
    timed-out turn never yields its session id — there is nothing to
    abort by id, and the server teardown bounds the orphaned turn; that
    case is recorded honestly as ``budget_exceeded``.

    NXT-11: the opencode client yields the vendor session id only AT
    turn completion, so with *control* supplied the steering session
    binds there — before the events poll, the one poll loop this lane
    has — and the drain runs beside it. Mid-turn steering is honestly
    absent on this profile (no ``live_input``): the attached bridge
    journals the refusals rather than guessing a capability.

    NXT-28: the ``episode`` breakdown for this lane — startup is the
    server spawn + client build; ``turn_s`` spans the ``start_session``
    wait (the client waits the turn INSIDE it) through the verdict
    poll; ``interrupt_grace_s`` is always None here: a timed-out turn
    never yields its session id, so there is no interrupt-by-id phase
    (the server teardown bounds the orphaned turn) — recorded as the
    None it is, never a zero.
    """
    source = os.environ if env is None else env
    steering: LaneSteeringSession | None = None
    server = opencode_server_from_env(env=source)
    loop = asyncio.get_running_loop()
    startup_started = loop.time()
    async with server:
        client_env = {
            **source,
            "OPENCODE_SERVER_URL": server.url,
            "OPENCODE_SERVER_PASSWORD": server.password,
            "OPENCODE_SESSION_DIRECTORY": source.get("OPENCODE_SESSION_DIRECTORY") or os.getcwd(),
            "OPENCODE_PERMISSION_RESPONSE": source.get("OPENCODE_PERMISSION_RESPONSE") or "once",
            "OPENCODE_PROMPT_TIMEOUT": str(budget_s),
        }
        client = opencode_client_from_env(
            source.get("OPENCODE_PROVIDER_API_KEY") or None, env=client_env
        )
        startup_s = loop.time() - startup_started
        turn_started = loop.time()
        try:
            try:
                # Belt over the client's own budget: even an
                # operator-overridden OPENCODE_PROMPT_TIMEOUT can never
                # unbind the lane.
                session_id = await asyncio.wait_for(
                    client.start_session(task), timeout=budget_s + grace_s
                )
            except TimeoutError:
                turn_s = loop.time() - turn_started
                outcome = LaneOutcome(exit_status="failed", terminal_reason="budget_exceeded")
            else:
                steering = _steering_session(
                    control, driver_key="opencode", client=client, env=source
                )
                if steering is not None:
                    # the id-existence point for this vendor: the turn is
                    # already complete — bind, then drain beside the poll.
                    _bind_steering(steering, session_id)
                deadline = loop.time() + grace_s

                async def _poll_events() -> list[dict[str, Any]]:
                    while True:
                        events = await client.events(session_id)
                        if _opencode_verdict_seen(events, session_id) or loop.time() >= deadline:
                            return events
                        await asyncio.sleep(poll_s)

                async with _steering_scope(steering, poll_s):
                    events = await _poll_events()
                turn_s = loop.time() - turn_started
                exit_status, reason = classify_opencode_events(events, session_id)
                outcome = LaneOutcome(
                    exit_status=exit_status,
                    terminal_reason=reason,
                    usage=opencode_usage_receipt(events),
                )
        finally:
            teardown_started = loop.time()
            await client.aclose()
    teardown_s = loop.time() - teardown_started
    if steering is not None:
        outcome = replace(outcome, steering_journal=_steering_journal(steering))
    return replace(
        outcome,
        episode=_episode(startup_s, turn_s, None, teardown_s),
    )


def write_artifacts(
    outcome: LaneOutcome,
    *,
    attempt_base: str,
    model: str,
    meta_path: str = _META_PATH,
    usage_path: str = _USAGE_PATH,
    driver_id: str = LANE_DRIVER_ID,
) -> dict[str, Any]:
    """Write the batch lane's meta + usage contract for *outcome*.

    The meta keys are the ones ``_collect_candidate`` reads
    (``attempt_base`` / ``driver`` / ``model`` / ``exit`` / ``usage``),
    plus the additive ``terminal_reason`` audit field; ``.forge/usage.json``
    carries the same receipt beside it (the batch lane's layout). Unknown
    usage stays ``null`` — never a zeroed dict. *driver_id* is the lane's
    registered harness id (:data:`LANE_DRIVER_IDS` value).
    """
    meta: dict[str, Any] = {
        "attempt_base": str(attempt_base or ""),
        "driver": str(driver_id or LANE_DRIVER_ID),
        "model": str(model or ""),
        "exit": outcome.exit_status,
        "terminal_reason": outcome.terminal_reason,
        "usage": outcome.usage,
    }
    if outcome.error:
        meta["error"] = outcome.error[:500]
    if getattr(outcome, "reply_excerpt", None):
        meta["reply_excerpt"] = outcome.reply_excerpt
    if outcome.steering_journal is not None:
        # NXT-11: the steering bridge's append-only evidence — present
        # ONLY when the lane ran with the bridge attached (empty list =
        # attached, nothing arrived; absent = the gate was off).
        meta["steering_journal"] = outcome.steering_journal
    if outcome.episode is not None:
        # NXT-28: the episode timing breakdown — present whenever a turn
        # was driven (any exit classification); absent only for failures
        # that preceded the episode itself (brief_missing, sdk_missing...).
        meta["episode"] = outcome.episode
    meta_file = Path(meta_path)
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    usage_file = Path(usage_path)
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    usage_file.write_text(json.dumps(outcome.usage, indent=2, sort_keys=True) + "\n")
    return meta


def _env_seconds(name: str, default: float) -> float:
    """A positive seconds value from env, or *default*; malformed fails CLOSED.

    Same doctrine as the driver factory's env parsing: a typo must never
    silently downgrade a bound.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _parse_driver_flag(argv: list[str]) -> str | None:
    """The ``--driver`` CLI key (claude | codex | opencode), or None."""
    parser = argparse.ArgumentParser(
        prog="python -m forge.lane_driver",
        description="Drive ONE interactive-driver lane turn (EXE-02).",
    )
    parser.add_argument(
        "--driver",
        default=None,
        choices=tuple(LANE_DRIVER_IDS),
        help="lane driver key (default: $FORGE_LANE_DRIVER, else claude)",
    )
    return parser.parse_args(argv).driver


def _lane_model(driver_key: str) -> str:
    """The meta ``model`` route per lane, from the dispatch env."""
    if driver_key == "codex":
        return os.environ.get("FORGE_CODEX_MODEL") or os.environ.get("CODEX_MODEL") or ""
    if driver_key == "opencode":
        return (
            os.environ.get("FORGE_OPENCODE_MODEL")
            or os.environ.get("OPENCODE_MODEL_ID")
            or os.environ.get("FORGE_HARNESS_MODEL")
            or ""
        )
    return os.environ.get("FORGE_CLAUDE_MODEL") or os.environ.get("FORGE_HARNESS_MODEL") or ""


def main(
    argv: list[str] | None = None,
    *,
    sdk: ModuleType | None = None,
) -> int:
    """Entry point: brief → ONE driven turn → the candidate meta contract.

    *argv* is parsed only when passed (the ``__main__`` entry hands it
    ``sys.argv[1:]``; in-process callers and the tests dispatch via
    ``FORGE_LANE_DRIVER`` instead). *sdk* is the claude test seam
    (:func:`claude_sdk_client_from_env` accepts the module stand-in); the
    lane job runs with no argument and the real import-guarded package.
    Every failure path still writes the meta (the template uploads
    ``when: always``; the worker classifies from it) and returns nonzero —
    the process exit mirrors the batch lane's claude exit: 0 only for a
    cleanly completed turn.
    """
    flag_driver = _parse_driver_flag(argv) if argv is not None else None
    driver_key = (
        flag_driver or os.environ.get("FORGE_LANE_DRIVER") or ""
    ).strip().lower() or "claude"
    driver_id = LANE_DRIVER_IDS.get(driver_key, driver_key)

    brief_path = Path(os.environ.get("FORGE_BRIEF") or _BRIEF_PATH)
    attempt_base = os.environ.get("FORGE_ATTEMPT_BASE", "")
    model = _lane_model(driver_key)

    def _fail(reason: str, detail: str = "") -> int:
        outcome = LaneOutcome(exit_status="failed", terminal_reason=reason, error=detail)
        write_artifacts(outcome, attempt_base=attempt_base, model=model, driver_id=driver_id)
        print(f"lane_driver: {reason}" + (f" ({detail})" if detail else ""), file=sys.stderr)
        return 1

    if driver_key not in LANE_DRIVER_IDS:
        return _fail(
            "unknown_driver",
            f"{driver_key!r} (expected one of {', '.join(sorted(LANE_DRIVER_IDS))})",
        )

    if not brief_path.is_file():
        return _fail("brief_missing", f"brief file {str(brief_path)!r} is missing")

    try:
        budget_s = _env_seconds("FORGE_LANE_BUDGET_SECONDS", _DEFAULT_BUDGET_S)
        grace_s = _env_seconds("FORGE_LANE_GRACE_SECONDS", _DEFAULT_GRACE_S)
        poll_s = _env_seconds("FORGE_LANE_POLL_SECONDS", _POLL_INTERVAL_S)
        task = build_task(
            brief_path.read_text(errors="replace"),
            os.environ.get("FORGE_ISSUE_IID", ""),
        )
    except ValueError as exc:
        return _fail("driver_setup_error", str(exc))

    # NXT-11: the lane-local control consumer, honestly OFF unless the
    # dispatch env turned it on (see steering_service_from_env).
    control = steering_service_from_env()

    if driver_key == "claude":
        try:
            # Ambient env carries the gateway (ANTHROPIC_BASE_URL/AUTH_TOKEN),
            # FORGE_CLAUDE_MODEL, FORGE_CLAUDE_CWD=repo and IS_SANDBOX; the
            # factory owns every knob and its fail-closed parsing.
            client = claude_sdk_client_from_env(sdk=sdk)
        except (RuntimeError, ValueError) as exc:
            # RuntimeError here is the driver's actionable missing-SDK hint.
            reason = "sdk_missing" if "claude-agent-sdk" in str(exc) else "driver_setup_error"
            return _fail(reason, str(exc))
        drive = functools.partial(
            drive_lane,
            client,
            task=task,
            budget_s=budget_s,
            grace_s=grace_s,
            poll_s=poll_s,
            control=control,
        )
    elif driver_key == "codex":
        # Ambient env carries CODEX_BINARY/CODEX_CWD/CODEX_MODEL and the
        # inherited auth (~/.codex/auth.json or OPENAI_API_KEY/CODEX_API_KEY).
        try:
            client = codex_app_client_from_env()
        except (RuntimeError, ValueError) as exc:
            return _fail("driver_setup_error", str(exc))
        drive = functools.partial(
            drive_codex_lane,
            client,
            task=task,
            budget_s=budget_s,
            grace_s=grace_s,
            poll_s=poll_s,
            control=control,
        )
    else:  # opencode — the server + client are built inside the coroutine
        # (the spawner owns the loopback listener's lifetime).
        drive = functools.partial(
            run_opencode_lane,
            task=task,
            budget_s=budget_s,
            grace_s=grace_s,
            poll_s=poll_s,
            control=control,
        )

    try:
        outcome = asyncio.run(drive())
    except Exception as exc:  # noqa: BLE001 — the lane always emits its meta
        outcome = LaneOutcome(exit_status="failed", terminal_reason="driver_error", error=str(exc))

    write_artifacts(outcome, attempt_base=attempt_base, model=model, driver_id=driver_id)
    print(
        f"lane_driver: {driver_id} exit={outcome.exit_status} reason={outcome.terminal_reason}",
        file=sys.stderr,
    )
    return 0 if outcome.exit_status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
