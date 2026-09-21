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
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from forge.adaptive.drivers.claude_sdk import (
    ClaudeSDKDriverClient,
    claude_sdk_client_from_env,
)

__all__ = [
    "LANE_DRIVER_ID",
    "NO_COMMIT_ADDENDUM",
    "LaneOutcome",
    "build_task",
    "classify_result",
    "drive_lane",
    "main",
    "usage_receipt",
    "write_artifacts",
]

#: The harness id this lane registers under
#: (:data:`forge.runs.harness_selection.SHIPPED_DRIVERS`) — the id the
#: worker dispatches as ``FORGE_HARNESS_DRIVER`` and the meta's ``driver``.
LANE_DRIVER_ID = "claude-sdk-lane"

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
    """
    session_id = await client.start_session(task)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        result = await _drain_until_result(client, session_id, deadline=deadline, poll_s=poll_s)
        if result is None:
            await client.interrupt(session_id)
            result = await _drain_until_result(
                client, session_id, deadline=loop.time() + grace_s, poll_s=poll_s
            )
            if result is None:
                return LaneOutcome(exit_status="failed", terminal_reason="budget_exceeded")
        exit_status, reason = classify_result(result)
        return LaneOutcome(
            exit_status=exit_status,
            terminal_reason=reason,
            usage=usage_receipt(result),
        )
    finally:
        await client.close(session_id)


def write_artifacts(
    outcome: LaneOutcome,
    *,
    attempt_base: str,
    model: str,
    meta_path: str = _META_PATH,
    usage_path: str = _USAGE_PATH,
) -> dict[str, Any]:
    """Write the batch lane's meta + usage contract for *outcome*.

    The meta keys are the ones ``_collect_candidate`` reads
    (``attempt_base`` / ``driver`` / ``model`` / ``exit`` / ``usage``),
    plus the additive ``terminal_reason`` audit field; ``.forge/usage.json``
    carries the same receipt beside it (the batch lane's layout). Unknown
    usage stays ``null`` — never a zeroed dict.
    """
    meta: dict[str, Any] = {
        "attempt_base": str(attempt_base or ""),
        "driver": LANE_DRIVER_ID,
        "model": str(model or ""),
        "exit": outcome.exit_status,
        "terminal_reason": outcome.terminal_reason,
        "usage": outcome.usage,
    }
    if outcome.error:
        meta["error"] = outcome.error[:500]
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


def main(*, sdk: ModuleType | None = None) -> int:
    """Entry point: brief → ONE driven turn → the candidate meta contract.

    *sdk* is the test seam (:func:`claude_sdk_client_from_env` accepts the
    module stand-in); the lane job runs with no argument and the real
    import-guarded package. Every failure path still writes the meta (the
    template uploads ``when: always``; the worker classifies from it) and
    returns nonzero — the process exit mirrors the batch lane's claude
    exit: 0 only for a cleanly completed turn.
    """
    brief_path = Path(os.environ.get("FORGE_BRIEF") or _BRIEF_PATH)
    attempt_base = os.environ.get("FORGE_ATTEMPT_BASE", "")
    model = os.environ.get("FORGE_CLAUDE_MODEL") or os.environ.get("FORGE_HARNESS_MODEL") or ""

    def _fail(reason: str, detail: str = "") -> int:
        outcome = LaneOutcome(exit_status="failed", terminal_reason=reason, error=detail)
        write_artifacts(outcome, attempt_base=attempt_base, model=model)
        print(f"lane_driver: {reason}" + (f" ({detail})" if detail else ""), file=sys.stderr)
        return 1

    if not brief_path.is_file():
        return _fail("brief_missing", f"brief file {str(brief_path)!r} is missing")

    try:
        budget_s = _env_seconds("FORGE_LANE_BUDGET_SECONDS", _DEFAULT_BUDGET_S)
        grace_s = _env_seconds("FORGE_LANE_GRACE_SECONDS", _DEFAULT_GRACE_S)
        poll_s = _env_seconds("FORGE_LANE_POLL_SECONDS", _POLL_INTERVAL_S)
        # Ambient env carries the gateway (ANTHROPIC_BASE_URL/AUTH_TOKEN),
        # FORGE_CLAUDE_MODEL, FORGE_CLAUDE_CWD=repo and IS_SANDBOX; the
        # factory owns every knob and its fail-closed parsing.
        client = claude_sdk_client_from_env(sdk=sdk)
        task = build_task(
            brief_path.read_text(errors="replace"),
            os.environ.get("FORGE_ISSUE_IID", ""),
        )
    except (RuntimeError, ValueError) as exc:
        # RuntimeError here is the driver's actionable missing-SDK hint.
        reason = "sdk_missing" if "claude-agent-sdk" in str(exc) else "driver_setup_error"
        return _fail(reason, str(exc))

    try:
        outcome = asyncio.run(
            drive_lane(client, task=task, budget_s=budget_s, grace_s=grace_s, poll_s=poll_s)
        )
    except Exception as exc:  # noqa: BLE001 — the lane always emits its meta
        outcome = LaneOutcome(exit_status="failed", terminal_reason="driver_error", error=str(exc))

    write_artifacts(outcome, attempt_base=attempt_base, model=model)
    print(
        f"lane_driver: {LANE_DRIVER_ID} exit={outcome.exit_status} "
        f"reason={outcome.terminal_reason}",
        file=sys.stderr,
    )
    return 0 if outcome.exit_status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
