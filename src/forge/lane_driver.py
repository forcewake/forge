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

The SAME contract now drives three more REAL interactive clients
(``--driver codex | opencode | copilot``, or ``FORGE_LANE_DRIVER``;
``claude`` stays the default and its path is unchanged):

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
- ``copilot`` — :class:`forge.adaptive.drivers.copilot_acp.
  CopilotACPDriverClient` (built via :func:`copilot_acp_client_from_env`;
  a spawned ``copilot --acp --stdio`` child reading the ambient
  ``COPILOT_GITHUB_TOKEN``). ``start_session`` answers at ``session/new``
  — the id exists BEFORE any turn — so the lane poll-watches the
  client's terminal record (the turn ends when a ``stopReason``
  arrives). The client's cancel LEDGER owns the #4561 workaround: a
  canceled turn whose wire answers ``stopReason: "end_turn"`` reports
  ``interrupted_by_ledger``, never ``completed``. No mid-turn steer
  exists on ACP (the client refuses a second in-flight prompt); no
  per-turn usage crosses the wire, so the receipt stays honestly
  unknown (None — never a zeroed dict).

All lanes keep the claude lane's budget doctrine: one overall
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

NEXT-14 — the drive cycle is SUPERVISED. ``drive_lane`` /
``drive_codex_lane`` / ``run_opencode_lane`` submit their turn (and,
when steering is attached, the steering drain) to ONE
:class:`forge.adaptive.lane_supervisor.LaneSupervisor` which owns the
race: turn completion cancels the drain (bounded, its final pass still
consumes late commands), an applied interrupt-class control suspends
the turn (classified ``operator_pause`` — never misread as a vendor
completion), and the terminal outcome is written exactly once by the
supervisor's classifier. Additive by construction: with no steering
attached the composed path is the old one turn-for-turn — only the
task bookkeeping moved.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import asdict, dataclass, replace
import json
import os
import re
import socket
import sys
import uuid
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
from forge.adaptive.drivers.copilot_acp import (
    CopilotACPError,
    copilot_acp_client_from_env,
)
from forge.adaptive.drivers.opencode import opencode_client_from_env
from forge.adaptive.drivers.opencode_serve import opencode_server_from_env
from forge.adaptive.lane_channel import LaneControlChannel, lane_channel_from_env
from forge.adaptive.lane_control import INTERRUPT_KINDS, LaneSteeringSession
from forge.adaptive.lane_supervisor import (
    TERMINAL_SUSPENDED,
    LaneSupervisor,
    TerminalEvent,
)
from forge.adaptive.wiring import OperatorControlService

__all__ = [
    "CODEX_LANE_DRIVER_ID",
    "CONSUMPTION_STATUS_CONSUMED",
    "CONSUMPTION_STATUS_UNRESOLVED",
    "COPILOT_LANE_DRIVER_ID",
    "CREDENTIAL_REF_ENV",
    "EPISODE_PHASE_KEYS",
    "LANE_CREDENTIAL_REDEEM_ROUTE",
    "LANE_DRIVER_ID",
    "LANE_DRIVER_IDS",
    "NO_COMMIT_ADDENDUM",
    "OPENCODE_LANE_DRIVER_ID",
    "REDEEM_FLAG_ENV",
    "RESUME_ENV",
    "STEERING_ENV",
    "LaneCredentialRedemptionError",
    "LaneOutcome",
    "apply_redeemed_credential",
    "build_task",
    "classify_codex_turn",
    "classify_copilot_turn",
    "classify_opencode_events",
    "classify_result",
    "codex_usage_receipt",
    "consumer_identity",
    "consumption_status_for",
    "copilot_usage_receipt",
    "credential_consumption_record",
    "drive_codex_lane",
    "drive_copilot_lane",
    "drive_lane",
    "main",
    "native_delivery_requested",
    "opencode_usage_receipt",
    "redeem_lane_credential",
    "redemption_requested",
    "resume_requested",
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

#: The copilot lane's registered harness id — the interactive ACP twin of
#: the scripted ``copilot`` batch lane (``copilot --acp`` over stdio).
COPILOT_LANE_DRIVER_ID = "copilot-sdk-lane"

#: ``--driver`` key → registered harness id (the meta ``driver`` and the
#: template filter value). The worker dispatches the id; the lane takes
#: the short key on the CLI.
LANE_DRIVER_IDS: dict[str, str] = {
    "claude": LANE_DRIVER_ID,
    "codex": CODEX_LANE_DRIVER_ID,
    "opencode": OPENCODE_LANE_DRIVER_ID,
    "copilot": COPILOT_LANE_DRIVER_ID,
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

#: The explicit WIP-continuity marker (R28-03). A dispatch that sets it
#: (the ``/retry`` after a pause) declares this run MUST continue from
#: the control-plane checkpoint: the pre-turn restore is REQUIRED, and a
#: failed download or restore halts the lane BEFORE any vendor client or
#: session exists — exit nonzero, the failure in the meta and the
#: steering sidecar, zero model turns. A run WITHOUT the marker is a
#: fresh attempt: no held checkpoint (404) is normal there, the restore
#: is best-effort, and its report rides the sidecar either way.
RESUME_ENV = "FORGE_LANE_RESUME"
_RESUME_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})

#: NEXT-03's third mode: ``FORGE_LANE_RESUME=restart`` — an EXPLICIT
#: discard-WIP restart. The checkpoint is intentionally NOT downloaded:
#: the lane runs on the dispatched base and the restore report SAYS the
#: WIP was discarded (a distinct, documented outcome — never a silent
#: "latest fallback" and never a required-restore halt).
_RESUME_RESTART = "restart"


# ---------------------------------------------------------------------------
# R38-02 (#303) — runner-time model-credential redemption (profile b):
# the lane exchanges its EXISTING attempt-scoped lane-control token for
# the bound model credential at startup and sets EXACTLY ONE env var.
# ---------------------------------------------------------------------------

#: The dispatch's redemption-mode flag (``FORGE_CREDENTIAL_REDEEM=1`` —
#: the non-secret flag the delivery plan put on the envelope/inputs).
REDEEM_FLAG_ENV = "FORGE_CREDENTIAL_REDEEM"

#: The dispatched credential REF (non-secret; the ref the binding
#: resolved — the redemption endpoint re-checks it against the work's
#: own live binding).
CREDENTIAL_REF_ENV = "FORGE_CREDENTIAL_REF"

#: The redemption route on the lane-control router (mirror of
#: :data:`forge.api_lane_control.LANE_CREDENTIAL_REDEEM_ROUTE` — mirrored
#: locally because the lane package never imports the control plane).
LANE_CREDENTIAL_REDEEM_ROUTE = "/lane/credentials/redeem"

#: One bounded wait for the redemption call (the same bound the
#: resume-spec read uses — a slow control plane delays the lane, never
#: selects an ambient credential).
_REDEEM_TIMEOUT_S = 10.0

#: Delivered credential var → the closed scrub set: the SAME provider
#: family's other credential variables, whose documented precedence
#: (cloud → ANTHROPIC_AUTH_TOKEN → ANTHROPIC_API_KEY → apiKeyHelper →
#: OAuth → federation, research doc 05 §2) would otherwise let a stray
#: runner-image variable silently outrank the deliberately delivered
#: credential. Exactly ONE variable is set; the strays are unset.
CREDENTIAL_SCRUB_VARS: dict[str, tuple[str, ...]] = {
    "ANTHROPIC_AUTH_TOKEN": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "ANTHROPIC_API_KEY": ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
}


class LaneCredentialRedemptionError(RuntimeError):
    """Startup redemption failed — the lane fails CLOSED: no ambient
    fallback credential, zero model turns. The message carries the HTTP
    status or transport class ONLY — never a response body (a refusal
    body could echo credential material into the job log)."""


def redemption_requested(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``FORGE_CREDENTIAL_REDEEM`` is truthy in *env* (default off)."""
    source = os.environ if env is None else env
    return source.get(REDEEM_FLAG_ENV, "").strip().lower() in _STEERING_TRUTHY


def _lane_provider_route(env: Mapping[str, str]) -> str:
    """The provider route THIS lane's driver consumes (for the redemption
    registry check) — the same table the dispatch used."""
    from forge.adaptive.project_credentials import provider_route_for_driver

    raw = (env.get("FORGE_LANE_DRIVER") or env.get("FORGE_HARNESS_DRIVER") or "").strip().lower()
    # The lane's short driver key (claude | codex | opencode | copilot)
    # resolves through the registered harness ids first.
    return provider_route_for_driver(LANE_DRIVER_IDS.get(raw, raw) or "claude")


def redeem_lane_credential(env: MutableMapping[str, str] | None = None) -> dict[str, Any]:
    """Redeem the attempt's model credential over the lane-control channel.

    Reads the SAME dial-out pair the steering/checkpoint channels use
    (``FORGE_LANE_CONTROL_URL`` + ``FORGE_LANE_CONTROL_TOKEN`` — the
    attempt-scoped HMAC the dispatch minted) plus the dispatched
    ``FORGE_CREDENTIAL_REF``, and exchanges them at
    :data:`LANE_CREDENTIAL_REDEEM_ROUTE` for the bound credential,
    TTL-bounded to the attempt. On success the credential is APPLIED
    (:func:`apply_redeemed_credential` — exactly one env var, strays
    scrubbed) and the VALUE-FREE redemption record is returned for the
    lane's evidence. Any failure raises :class:`LaneCredentialRedemptionError`
    — the caller halts the lane; an ambient credential is NEVER used.
    """
    import httpx

    source = os.environ if env is None else env
    work_id = (source.get("FORGE_WORK_ID") or source.get("FORGE_RUN_ID") or "").strip()
    url = (source.get("FORGE_LANE_CONTROL_URL") or "").strip()
    token = (source.get("FORGE_LANE_CONTROL_TOKEN") or "").strip()
    credential_ref = (source.get(CREDENTIAL_REF_ENV) or "").strip()
    provider = _lane_provider_route(source)
    missing = [
        name
        for name, value in (
            ("FORGE_CREDENTIAL_REF", credential_ref),
            ("FORGE_LANE_CONTROL_URL", url),
            ("FORGE_LANE_CONTROL_TOKEN", token),
            ("FORGE_WORK_ID", work_id),
            ("provider-route", provider),
        )
        if not value
    ]
    if missing:
        raise LaneCredentialRedemptionError(
            "the redemption channel is not configured (" + ", ".join(missing) + ")"
            " — FORGE_CREDENTIAL_REDEEM=1 demands the ref, the lane-control"
            " dial-out pair and the work id; an ambient credential is never used"
        )
    try:
        response = httpx.get(
            url.rstrip("/") + LANE_CREDENTIAL_REDEEM_ROUTE,
            params={
                "work_id": work_id,
                "credential_ref": credential_ref,
                "provider": provider,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=_REDEEM_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        # Transport failure — the outcome is undecidable but the posture is
        # not: fail closed. The error names the transport class only.
        raise LaneCredentialRedemptionError(
            f"the redemption endpoint was unreachable ({exc.__class__.__name__}) —"
            " the lane fails closed rather than fall back to another credential"
        ) from exc
    if response.status_code != 200:
        # NEVER surface the response body (it may echo credential material).
        raise LaneCredentialRedemptionError(
            f"the redemption endpoint refused the attempt (HTTP {response.status_code})"
            " — no credential was redeemed and none is substituted"
        )
    try:
        document = response.json()
    except ValueError as exc:
        raise LaneCredentialRedemptionError(
            "the redemption response was not a JSON document — no credential was applied"
        ) from exc
    for field in ("value", "env_var", "expires_at", "redemption_id"):
        if not str(document.get(field) or "").strip():
            raise LaneCredentialRedemptionError(
                f"the redemption response is missing the {field!r} field — no credential was applied"
            )
    return apply_redeemed_credential(document, env=source)


def apply_redeemed_credential(
    document: Mapping[str, Any],
    *,
    env: MutableMapping[str, str] | None = None,
) -> dict[str, Any]:
    """Apply one redeemed credential: set EXACTLY ONE env var (the
    binding's slot — e.g. ``ANTHROPIC_AUTH_TOKEN``, doc 05's bearer
    surface), unset the closed scrub set's strays, and return the
    VALUE-FREE redemption record (the receipt the lane's evidence and
    the consumer proof cite).

    R38-04: the record also carries the redemption's CORRELATION fields
    (``broker_receipt_id``, ``resolved_version``/``_kind``,
    ``attempt_generation``, ``credential_policy``) — absent on pre-#305
    documents, echoed empty — so the consumer receipt can join broker
    id ↔ redemption id ↔ attempt ↔ consumer without another call."""
    target = os.environ if env is None else env
    env_var = str(document["env_var"])
    value = str(document["value"])
    scrubbed = [stray for stray in CREDENTIAL_SCRUB_VARS.get(env_var, ()) if target.get(stray)]
    for stray in scrubbed:
        target.pop(stray, None)
    target[env_var] = value
    return {
        "env_var": env_var,
        "credential_ref": str(document.get("credential_ref") or ""),
        "provider": str(document.get("provider") or ""),
        "redemption_id": str(document.get("redemption_id") or ""),
        "expires_at": str(document.get("expires_at") or ""),
        "binding_revision": document.get("binding_revision"),
        "resolver_identity": str(document.get("resolver_identity") or ""),
        "scrubbed_env_vars": scrubbed,
        # The R38-04 correlation join (refs/metadata only, as ever).
        "broker_receipt_id": str(document.get("broker_receipt_id") or ""),
        "resolved_version": str(document.get("resolved_version") or ""),
        "resolved_version_kind": str(document.get("resolved_version_kind") or ""),
        "attempt_generation": document.get("attempt_generation"),
        "credential_policy": str(document.get("credential_policy") or ""),
    }


# ---------------------------------------------------------------------------
# R38-04 (#305) — the CONSUMER receipt: the trusted runner bootstrap's
# value-free proof that the delivered credential was staged for THIS
# attempt, joined with the broker/redemption receipts. Emitted after
# `apply_redeemed_credential` (redemption mode) or after the template's
# native-secret resolution step (native mode — where the lane process is
# the first forge-owned code that runs). Rides the candidate meta AND
# `.forge/steering.json` (the lane's two durable journals — both upload
# `when: always`), with an honest `consumer_status`: ``consumed`` only
# when the driven turn completed, ``staged-unresolved`` otherwise — NO
# model-usage claim merely because the broker returned.
# ---------------------------------------------------------------------------

#: The consumption status while no completed turn backs the delivery.
CONSUMPTION_STATUS_UNRESOLVED = "staged-unresolved"
#: The consumption status when the driven turn completed on this credential.
CONSUMPTION_STATUS_CONSUMED = "consumed"


def consumer_identity(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """WHO consumed — the runner/job id where the CI provider exposes
    one (GitLab ``CI_JOB_ID``, GitHub ``GITHUB_RUN_ID``, Azure
    ``BUILD_BUILDID``), else the host#pid of the lane process."""
    source = os.environ if env is None else env
    for var, kind in (
        ("CI_JOB_ID", "ci-job"),
        ("GITHUB_RUN_ID", "github-run"),
        ("BUILD_BUILDID", "azure-build"),
    ):
        value = (source.get(var) or "").strip()
        if value:
            return {"kind": kind, "id": value, "via": var}
    return {"kind": "process", "id": f"{socket.gethostname()}#{os.getpid()}"}


def native_delivery_requested(env: Mapping[str, str] | None = None) -> bool:
    """Whether the lane was dispatched under a NATIVE delivery mode: the
    envelope carries the credential ref and the redemption flag is OFF —
    the template's resolution step (the shipped snippet) already mapped
    the provider-held secret into the env slot before this process ran."""
    source = os.environ if env is None else env
    ref = (source.get(CREDENTIAL_REF_ENV) or "").strip()
    return bool(ref) and not redemption_requested(source)


def _native_delivery_transport(env: Mapping[str, str]) -> str:
    """Which native transport's runner this lane runs on (best-effort
    detection from the CI provider's own marker variables; an
    undetectable runner names ``native-unknown`` honestly)."""
    if (env.get("GITHUB_RUN_ID") or "").strip():
        return "github-native-secret"
    if (env.get("CI_JOB_ID") or "").strip() and (env.get("GITLAB_CI") or "").strip():
        return "gitlab-protected-variable"
    if (env.get("TF_BUILD") or "").strip():
        return "azure-variable-group"
    return "native-unknown"


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def credential_consumption_record(
    *,
    redemption_record: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Build the lane's CONSUMER receipt (schema
    ``forge.credential.consumer-receipt/1``) — refs/metadata ONLY.

    Redemption mode (a *redemption_record* from
    :func:`apply_redeemed_credential`): the full join — binding
    revision, resolver identity, route, work, attempt generation,
    consumer identity, the delivered env-slot NAME, the redemption id
    and the broker receipt id. Native mode (no record, ref on the
    envelope, redemption flag off): the runner-side bootstrap CANNOT see
    the control plane's binding revision — the receipt records it as
    unknown (``binding_revision`` null, ``binding_revision_known``
    false) rather than guessing; the join rides the transport and the
    dispatched ref. Returns None when no delivery applies to this lane.
    The record starts ``staged-unresolved`` — ``main`` promotes it to
    ``consumed`` only for a completed driven turn."""
    from forge.adaptive.credential_broker import (
        consumer_receipt_document,
        credential_policy,
    )

    source = os.environ if env is None else env
    work_id = (source.get("FORGE_WORK_ID") or source.get("FORGE_RUN_ID") or "").strip()
    identity = consumer_identity(source)
    if redemption_record is not None:
        return {
            **consumer_receipt_document(
                consumer_receipt_id=uuid.uuid4().hex,
                env_var=str(redemption_record.get("env_var") or ""),
                consumer_identity=identity,
                delivery_route=LANE_CREDENTIAL_REDEEM_ROUTE,
                resolver_identity=str(redemption_record.get("resolver_identity") or ""),
                credential_policy=str(
                    redemption_record.get("credential_policy") or credential_policy(source)
                ),
                binding_revision=_optional_int(redemption_record.get("binding_revision")),
                provider_route=str(redemption_record.get("provider") or ""),
                credential_ref=str(redemption_record.get("credential_ref") or ""),
                work_id=work_id,
                attempt_generation=_optional_int(redemption_record.get("attempt_generation")),
                redemption_id=str(redemption_record.get("redemption_id") or ""),
                broker_receipt_id=str(redemption_record.get("broker_receipt_id") or ""),
                resolved_version=str(redemption_record.get("resolved_version") or ""),
                resolved_version_kind=str(redemption_record.get("resolved_version_kind") or ""),
            ),
            "binding_revision_known": True,
            "consumer_status": CONSUMPTION_STATUS_UNRESOLVED,
        }
    if native_delivery_requested(source):
        credential_ref = (source.get(CREDENTIAL_REF_ENV) or "").strip()
        route = _lane_provider_route(source)
        from forge.adaptive.project_credentials import PROVIDER_ENV_VARS

        transport = _native_delivery_transport(source)
        return {
            **consumer_receipt_document(
                consumer_receipt_id=uuid.uuid4().hex,
                env_var=PROVIDER_ENV_VARS.get(route, ""),
                consumer_identity=identity,
                delivery_route=transport,
                resolver_identity=transport,
                credential_policy=credential_policy(source),
                binding_revision=None,
                provider_route=route,
                credential_ref=credential_ref,
                work_id=work_id,
                attempt_generation=None,
            ),
            "binding_revision_known": False,
            "consumer_status": CONSUMPTION_STATUS_UNRESOLVED,
        }
    return None


def consumption_status_for(exit_status: str) -> str:
    """The consumption status a lane outcome earns: ``consumed`` only
    for a cleanly completed turn — every other exit keeps the honest
    ``staged-unresolved`` (the credential was staged, no model usage is
    claimed merely because the broker returned)."""
    if exit_status == "completed":
        return CONSUMPTION_STATUS_CONSUMED
    return CONSUMPTION_STATUS_UNRESOLVED


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


def resume_requested(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``FORGE_LANE_RESUME`` is truthy in *env* (default: off)."""
    source = os.environ if env is None else env
    return source.get(RESUME_ENV, "").strip().lower() in _RESUME_TRUTHY


def resume_mode(env: Mapping[str, str] | None = None) -> str:
    """NEXT-03's three DISTINCT dispatch modes, from ``FORGE_LANE_RESUME``.

    - ``"fresh"`` (absent/unknown): no checkpoint needed — a first run;
      a 404-shaped restore failure is normal and never blocks the turn.
    - ``"required"`` (truthy): the resume's exact ResumeSpec MUST
      restore — a failed download, refused binding or corrupt manifest
      halts the lane before any vendor client exists.
    - ``"restart"`` (the literal ``restart``): the WIP is INTENTIONALLY
      discarded — no download at all, the report says so.
    """
    source = os.environ if env is None else env
    raw = (source.get(RESUME_ENV) or "").strip().lower()
    if raw == _RESUME_RESTART:
        return "restart"
    if raw in _RESUME_TRUTHY:
        return "required"
    return "fresh"


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
    # NXT-15 wave C: the pause drain's capture capability — the git
    # tracked baseline + a local content-addressed store in .forge/ (the
    # checkpoint channel uploads from there when the control plane is
    # reachable; otherwise the local copy is still verified+retained).
    capture = _lane_capture_capability(work_id)
    return LaneSteeringSession(
        service=control,
        driver=_STEERING_ADAPTERS[driver_key](client=client),
        driver_kind=driver_key,  # type: ignore[arg-type]  (keyed by LANE_DRIVER_IDS)
        run_id=run_id,
        work_id=work_id,
        capture=capture,
    )


class _LaneUploadChannel:
    """The WipUploadChannel protocol over the lane's checkpoint API."""

    def __init__(self, api: Any) -> None:
        self._api = api

    def upload_checkpoint(self, store: Any, work_id: str) -> object:
        from forge.adaptive.checkpoint_channel import upload_checkpoint

        return upload_checkpoint(store, work_id, self._api)


def _lane_capture_capability(work_id: str) -> "Callable[[], Any] | None":
    """Build the cooperative capture for the lane checkout, or None.

    None (the honest absent capability → paused_partial) when the git
    baseline is unreadable; the upload channel rides when the control
    URL+token are in env (the same env the steering channel uses). The
    manifest's ``source_oids`` names the APPROVED base the lane works
    on (``FORGE_ATTEMPT_BASE``) — the checkpoint declares what tree its
    delta applies on top of, and a later reader can verify it (R28-04).
    """
    from pathlib import Path as _P

    from forge.adaptive.artifact_store import ContentAddressedStore
    from forge.adaptive.checkpointing import cooperative_capture

    baseline, baseline_modes = _tracked_baseline()
    if not baseline:
        return None
    attempt_base = (os.environ.get("FORGE_ATTEMPT_BASE") or "").strip()
    source_oids = {"attempt_base": attempt_base} if attempt_base else None
    store_dir = _P(".forge/checkpoints")
    store_dir.mkdir(parents=True, exist_ok=True)
    store = ContentAddressedStore(root=store_dir, tenant=work_id)
    upload = None
    url = (os.environ.get("FORGE_LANE_CONTROL_URL") or "").strip()
    token = (os.environ.get("FORGE_LANE_CONTROL_TOKEN") or "").strip()
    if url and token:
        try:
            from forge.adaptive.checkpoint_channel import LaneControlAPI

            api = LaneControlAPI(base_url=url, work_token=token)
            upload = _LaneUploadChannel(api)
        except Exception:  # noqa: BLE001 — the checkpoint survives locally
            upload = None
    return cooperative_capture(
        work_id=work_id,
        root=_P.cwd(),
        store=store,
        tracked_baseline=baseline,
        baseline_modes=baseline_modes,
        source_oids=source_oids,
        upload=upload,
    )


def _tracked_baseline() -> tuple[dict[str, str], dict[str, int]]:
    """The lane checkout's tracked baseline in ONE canonical digest scheme.

    The caller's durable authority in production is the SnapshotSet; in
    the lane job the git index IS that authority (the attempt base's
    tree). R28-04: the DIGEST stored is the sha256 of the RAW file
    bytes — read through ONE ``git cat-file --batch`` over the index's
    blob OIDs — because that is the identity ``capture_wip`` compares
    the working tree against; a git blob OID is a different hash of a
    different wrapping and would make every unchanged file look changed
    (and drag the whole tree, the checkpoint store included, into every
    capture). The executable bit from the index rides beside it, so a
    mode-only change is still a captured delta.

    Returns ``(digests, modes)`` — path -> raw-content sha256 and path
    -> ``0o755``/``0o644``; both empty when the index is unreadable (the
    caller's honest no-capability).
    """
    import hashlib
    import subprocess

    try:
        listed = subprocess.run(
            ["git", "ls-files", "-s"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    entries: list[tuple[str, str, bool]] = []  # (path, blob oid, executable)
    for line in listed.stdout.splitlines():
        # <mode> <sha> <stage>\t<path>
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        meta = parts[0].split()
        if len(meta) >= 2:
            entries.append((parts[1], meta[1], meta[0] == "100755"))
    if not entries:
        return {}, {}

    # One batched read of every distinct blob's raw bytes (the same
    # content may back many paths — read it once).
    oids = sorted({oid for _path, oid, _executable in entries})
    try:
        batch = subprocess.run(
            ["git", "cat-file", "--batch"],
            input="".join(f"{oid}\n" for oid in oids).encode(),
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    raw: dict[str, bytes] = {}
    buffer = batch.stdout
    position = 0
    while position < len(buffer):
        newline = buffer.find(b"\n", position)
        if newline < 0:
            break
        header = buffer[position:newline].decode("utf-8", errors="replace").split()
        position = newline + 1
        if len(header) < 3 or not header[2].isdigit():
            break  # "<oid> missing" or malformed — the rest cannot be framed
        size = int(header[2])
        raw[header[0]] = buffer[position : position + size]
        position += size + 1  # payload + the terminating newline

    digests: dict[str, str] = {}
    modes: dict[str, int] = {}
    for path, oid, executable in entries:
        data = raw.get(oid)
        if data is None:
            continue
        digests[path] = hashlib.sha256(data).hexdigest()
        modes[path] = 0o755 if executable else 0o644
    return digests, modes


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


def _supervised_drain(
    steering: LaneSteeringSession,
    supervisor: LaneSupervisor[Any],
    poll_s: float,
) -> Any:
    """The steering drain task the supervisor owns (NEXT-14).

    The session's bounded mailbox poll (its async context — the remote
    channel's fetch loop entered inside it, torn down with it) PLUS the
    URGENT WATCH: the moment an interrupt-class command APPLIES, the
    drain asks the supervisor to suspend the turn — synchronously, in
    the same scheduling slice as the applied interrupt — so there is ONE
    owner of when the lane is finished instead of the drain's vendor
    interrupt silently racing the turn's own poll loop. Guidance that
    arrives while the turn runs keeps flowing through the session's
    ordinary drain cycles; nothing here replays the ladder.

    Teardown QUIESCES (``LaneSteeringSession.quiesce``) instead of
    hard-cancelling: an in-flight command's outcome is persisted on the
    ladder before the consumer is released — the turn completing must
    never tear a mid-cycle steer into ``delivery_unknown``.
    """

    async def _drain() -> None:
        seen: set[str] = set()
        mailbox = getattr(steering.service, "mailbox", None)
        channel_cm = (
            mailbox if isinstance(mailbox, LaneControlChannel) else contextlib.nullcontext()
        )
        async with channel_cm:
            async with steering.attach(poll_interval=poll_s):
                try:
                    while True:
                        for action in steering.journal:
                            if action.command_id in seen:
                                continue
                            seen.add(action.command_id)
                            if action.outcome == "applied" and action.kind in INTERRUPT_KINDS:
                                supervisor.request_urgent(
                                    action.kind,
                                    f"operator {action.kind} applied mid-turn — the "
                                    "supervisor suspends the driven turn",
                                )
                        await asyncio.sleep(poll_s)
                finally:
                    await steering.quiesce()

    return _drain()


def _operator_suspension(event: TerminalEvent) -> LaneOutcome:
    """The suspended-turn outcome (NEXT-14): an operator's urgent control
    ended the turn BEFORE its own verdict. ``failed`` — the turn produced
    no verdict — with the operator kind as the reason, never a vendor
    completion and never a silent budget expiry."""
    urgent = event.urgent
    kind = urgent.kind if urgent is not None else "interrupt"
    return LaneOutcome(
        exit_status="failed",
        terminal_reason=f"operator_{kind}",
        error=event.reason,
    )


def _driver_error_outcome(event: TerminalEvent) -> LaneOutcome:
    """The failed-turn outcome: the turn coroutine raised decisively, and
    the supervisor classified it instead of letting it escape the cycle
    unclassified (the same class ``main``'s catch-all writes)."""
    return LaneOutcome(
        exit_status="failed",
        terminal_reason="driver_error",
        error=event.reason,
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


#: A bare checkpoint id in a resume payload must be a 64-hex content
#: address — the same shape the remote ref's tail carries.
_RESUME_CHECKPOINT_ID = re.compile(r"^[0-9a-f]{64}$")

#: One bounded wait for the resume-spec read (NEXT-03). The spec fetch
#: gates the required restore; a slow control plane must delay it, not
#: silently select "latest".
_RESUME_SPEC_TIMEOUT_S = 10.0


def _resume_checkpoint_target(work_id: str, url: str, token: str) -> dict[str, Any]:
    """Resolve WHICH checkpoint the durable RESUME COMMAND binds (NEXT-03).

    Reads the work's LATEST ``resume`` command ROW off the control plane
    — ``GET /lane/controls/resume-spec``, the DURABLE record whatever
    rung it sits on — never the pending-command queue (a resume that has
    already been acknowledged leaves ``received``/``authorized``; its
    payload is still the immutable resume decision). The command's
    ResumeSpec carries the EXACT approved checkpoint — ``checkpoint_ref``
    as the durable ``<work_id>@<checkpoint_id>`` reference, or
    ``checkpoint_id`` as the bare digest — and that exact id is what the
    restore must download, never "whatever is latest now".

    Returns ``{"checkpoint_id": str | None, "resume_command": str | None,
    "note": str, "refused": bool, "lookup_failed": bool}``:

    - an explicit, well-formed, same-work reference → its checkpoint_id
      (``refused`` False, the exact binding);
    - a resume command WITHOUT a reference (the pre-NEXT-03 payload), no
      resume command at all → ``checkpoint_id`` None with an honest
      note — the caller falls back to the ACTIVE checkpoint, RECORDS
      that it did, and (required mode) the fallback is labelled legacy;
    - an unreachable or unparseable control plane → ``lookup_failed``
      True: the caller NEVER turns that into a "latest" guess on a
      required resume (NEXT-03: a control API timeout does not select
      another checkpoint);
    - a malformed or cross-work reference → ``refused`` True: an
      explicit binding that cannot be honored is a refusal with
      evidence, never a silent "latest" substitute.
    """
    import httpx

    from forge.adaptive.checkpoint_channel import parse_checkpoint_ref

    fallback = {
        "checkpoint_id": None,
        "resume_command": None,
        "note": "",
        "refused": False,
        "lookup_failed": False,
    }
    try:
        response = httpx.get(
            f"{url.rstrip('/')}/lane/controls/resume-spec",
            params={"work_id": work_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_RESUME_SPEC_TIMEOUT_S,
        )
        response.raise_for_status()
        document = response.json()
    except Exception as exc:  # noqa: BLE001 — the spec lookup degrades honestly
        return {
            **fallback,
            "lookup_failed": True,
            "note": f"resume command lookup unavailable ({exc}) — no checkpoint is selected by a timeout",
        }
    raw_command = document.get("command") if isinstance(document, dict) else None
    if not isinstance(raw_command, dict):
        return {
            **fallback,
            "note": (
                "no resume command carries an explicit checkpoint reference — "
                "the ACTIVE checkpoint is the fallback"
            ),
        }
    payload = raw_command.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    command_id = str(raw_command.get("command_id") or "")
    fallback["resume_command"] = command_id
    raw_ref = str(payload.get("checkpoint_ref") or "").strip()
    raw_id = str(payload.get("checkpoint_id") or "").strip()
    if not raw_ref and not raw_id:
        fallback["note"] = (
            f"resume command {command_id!r} carries no explicit checkpoint "
            "reference — the ACTIVE checkpoint is the fallback (legacy payload)"
        )
        return fallback

    if raw_ref:
        try:
            ref_work, checkpoint_id = parse_checkpoint_ref(raw_ref)
        except ValueError:
            return {
                **fallback,
                "note": f"resume command carries a malformed checkpoint reference ({raw_ref!r})",
                "refused": True,
            }
        if ref_work != work_id:
            return {
                **fallback,
                "note": (
                    f"resume command names work {ref_work!r}'s checkpoint — not this "
                    f"work's ({work_id!r}); an explicit binding to another work is refused"
                ),
                "refused": True,
            }
        return {**fallback, "checkpoint_id": checkpoint_id, "note": "exact", "refused": False}
    if not _RESUME_CHECKPOINT_ID.fullmatch(raw_id):
        return {
            **fallback,
            "note": f"resume command carries a malformed checkpoint id ({raw_id!r})",
            "refused": True,
        }
    return {**fallback, "checkpoint_id": raw_id, "note": "exact", "refused": False}


def _maybe_restore_wip(work_id: str) -> dict[str, Any] | None:
    """Wave D + NEXT-03: restore WIP from the control plane BEFORE the turn.

    The restore is BOUND to a checkpoint, not to "whatever is latest".
    The durable RESUME COMMAND's exact ResumeSpec
    (``work_id@checkpoint_id``, read from the command ROW — it may long
    have left the pending set) decides which checkpoint downloads:

    - ``checkpoint_selection: "exact"`` — the spec's checkpoint, the
      operator's approved resume point (a newer upload landing later
      changes nothing);
    - ``checkpoint_selection: "latest"`` — the labelled LEGACY migration
      fallback: no resume command exists, or its payload predates the
      ResumeSpec. The report SAYS so (``selection_note``), so no restore
      ever claims an exactness it did not have;
    - ``checkpoint_selection: "refused"`` — the spec's reference is
      malformed or names another work: restoring something the operator
      did not approve is worse than not restoring;
    - a required resume whose spec LOOKUP failed never guesses: the
      failure is returned and the lane's required-restore gate (R28-03)
      halts it with zero vendor sessions.

    A failed restore is RETURNED (the report carries ok=False +
    failures) — the lane runs the turn on the base, never silently
    pretends the WIP landed.
    """
    mode = resume_mode()

    if mode == "restart":
        # NEXT-03's explicit discard: no download, no fallback, documented.
        return {
            "restored": False,
            "files_restored": 0,
            "failures": [],
            "checkpoint_selection": "discarded",
            "note": (
                "explicit restart (FORGE_LANE_RESUME=restart): the held WIP is "
                "intentionally discarded — the lane runs on the dispatched base"
            ),
        }

    url = (os.environ.get("FORGE_LANE_CONTROL_URL") or "").strip()
    token = (os.environ.get("FORGE_LANE_CONTROL_TOKEN") or "").strip()
    if not url or not token:
        return None
    from pathlib import Path as _P

    from forge.adaptive.artifact_store import ContentAddressedStore
    from forge.adaptive.checkpoint_channel import (
        LaneControlAPI,
        download_checkpoint,
        format_checkpoint_ref,
    )
    from forge.adaptive.checkpointing import restore_wip

    target = _resume_checkpoint_target(work_id, url, token)
    selection = "exact" if target["checkpoint_id"] else "latest"
    if target["refused"]:
        return {
            "restored": False,
            "files_restored": 0,
            "failures": [f"resume checkpoint binding refused: {target['note']}"],
            "checkpoint_selection": "refused",
            "resume_command": target["resume_command"],
        }
    if target["lookup_failed"] and mode == "required":
        # A required resume never turns an unreachable spec lookup into a
        # "latest" guess (NEXT-03): the failure rides the report and the
        # lane's required-restore gate halts it.
        return {
            "restored": False,
            "files_restored": 0,
            "failures": [f"resume checkpoint binding unavailable: {target['note']}"],
            "checkpoint_selection": "unavailable",
            "resume_command": None,
        }
    try:
        api = LaneControlAPI(base_url=url, work_token=token)
        store_dir = _P(".forge/checkpoints")
        store_dir.mkdir(parents=True, exist_ok=True)
        store = ContentAddressedStore(root=store_dir, tenant=work_id)
        downloaded = download_checkpoint(work_id, api, store, checkpoint_id=target["checkpoint_id"])
        # R32-01: the lane process is INSIDE its target (``Path.cwd()``), so
        # the restore lands a STABLE GENERATION beside the checkout and
        # never renames or removes the directory the process (and the CI
        # shell waiting on it) is bound to. The report carries the landed
        # path; ``main`` chdirs into it before any vendor client exists.
        report = restore_wip(
            artifact_id=downloaded.artifact_id,
            store=store,
            target=_P.cwd(),
            principal=work_id,
            work_id=work_id,
            promote="generation",
        )
        restored: dict[str, Any] = {
            "restored": report.ok,
            "files_restored": len([f for f in report.files if f.outcome == "restored"])
            if report.files
            else 0,
            "failures": list(report.failures[:5]) if report.failures else [],
            "artifact": downloaded.artifact_id[:16] + "...",
            "checkpoint_ref": format_checkpoint_ref(work_id, downloaded.artifact_id),
            "checkpoint_sequence": downloaded.sequence,
            "checkpoint_selection": selection,
            "resume_command": target["resume_command"],
            # R32-01: the ACTIVE workspace generation — the sibling directory
            # the restored WIP lives in (empty when the restore failed; the
            # checkout's ``.forge/workspace-generation`` pointer names it for
            # the collector step).
            "workspace_generation": report.workspace_generation or None,
            # R32-02: recovery assets under the shared parent this restore did
            # NOT own (another workspace's) — inventory, never touched.
            "recovery_unrecognized": list(report.unrecognized[:5]),
        }
        if selection == "latest":
            # The honest fallback record: this restore was NOT bound to an
            # approved checkpoint — it took the active one by default.
            restored["selection_note"] = target["note"]
        return restored
    except Exception as exc:  # noqa: BLE001 — the report, never a crash
        return {
            "restored": False,
            "failures": [f"checkpoint download failed: {exc}"],
            "files_restored": 0,
            "checkpoint_selection": selection,
            "resume_command": target["resume_command"],
        }


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

    NEXT-14: the turn and the drain are SUBMITTED to one
    :class:`~forge.adaptive.lane_supervisor.LaneSupervisor` which owns
    the race and writes the terminal outcome exactly once — turn
    completion cancels the drain; an applied interrupt-class control
    suspends the turn and classifies ``operator_pause``.

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

    def _classify(
        event: TerminalEvent,
    ) -> tuple[LaneOutcome, float, float | None]:
        # NEXT-14: the supervisor is the ONLY writer of the terminal
        # outcome. Completion rides the turn's own tuple through; a
        # suspension (an operator's urgent control) and a decisive turn
        # failure classify HERE, never as a vendor completion.
        if event.kind == TERMINAL_SUSPENDED:
            return _operator_suspension(event), event.elapsed_s, None
        if event.result is None:  # a turn that failed without a verdict
            return _driver_error_outcome(event), event.elapsed_s, None
        return event.result

    supervisor: LaneSupervisor[tuple[LaneOutcome, float, float | None]] = LaneSupervisor(
        classify=_classify, name=LANE_DRIVER_ID
    )
    try:
        supervisor.submit_turn(_turn())
        if steering is not None:
            supervisor.submit_drain(_supervised_drain(steering, supervisor, poll_s))
        outcome, turn_s, interrupt_grace_s = await supervisor.run()
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

    NEXT-14: the turn and the drain are SUBMITTED to one
    :class:`~forge.adaptive.lane_supervisor.LaneSupervisor` — the same
    composed lifecycle the claude lane runs (turn completion cancels the
    drain; an applied interrupt-class control suspends the turn).

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

    def _classify(
        event: TerminalEvent,
    ) -> tuple[LaneOutcome, float, float | None]:
        # NEXT-14: the supervisor owns the terminal outcome — completion
        # rides the turn's own tuple; suspension and decisive failure
        # classify here.
        if event.kind == TERMINAL_SUSPENDED:
            return _operator_suspension(event), event.elapsed_s, None
        if event.result is None:  # a turn that failed without a verdict
            return _driver_error_outcome(event), event.elapsed_s, None
        return event.result

    supervisor: LaneSupervisor[tuple[LaneOutcome, float, float | None]] = LaneSupervisor(
        classify=_classify, name=CODEX_LANE_DRIVER_ID
    )
    try:
        supervisor.submit_turn(_turn())
        if steering is not None:
            supervisor.submit_drain(_supervised_drain(steering, supervisor, poll_s))
        outcome, turn_s, interrupt_grace_s = await supervisor.run()
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

                def _classify(
                    event: TerminalEvent,
                ) -> tuple[LaneOutcome | None, list[dict[str, Any]]]:
                    # NEXT-14: the supervisor owns the terminal outcome.
                    # Completion returns the events for the lane's own
                    # vendor classification below; a suspension or a
                    # decisive poll failure classifies here (the events
                    # are already beside it in the meta's evidence).
                    if event.kind == TERMINAL_SUSPENDED:
                        return _operator_suspension(event), []
                    if event.result is None:  # the poll failed without events
                        return _driver_error_outcome(event), []
                    return None, event.result

                supervisor: LaneSupervisor[tuple[LaneOutcome | None, list[dict[str, Any]]]] = (
                    LaneSupervisor(classify=_classify, name=OPENCODE_LANE_DRIVER_ID)
                )
                supervisor.submit_turn(_poll_events())
                if steering is not None:
                    supervisor.submit_drain(_supervised_drain(steering, supervisor, poll_s))
                verdict, events = await supervisor.run()
                turn_s = loop.time() - turn_started
                if verdict is not None:
                    outcome = verdict
                else:
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


# ---------------------------------------------------------------------------
# The copilot lane (CopilotACPDriverClient over ``copilot --acp`` stdio)
# ---------------------------------------------------------------------------


def classify_copilot_turn(result: dict | None) -> tuple[str, str]:
    """(exit classification, terminal reason) for a copilot ACP turn record.

    The record is the client's ledger-corrected terminal dict
    (``outcome`` is the honest verdict — ``end_turn`` after a recorded
    cancel already reads ``interrupted_by_ledger`` there, the #4561
    workaround); only ``outcome == completed`` exits clean, and the
    outcome itself rides the meta as the reason, the vendor ``stopReason``
    never silently replacing it. A record without an outcome is classified
    from what it DID carry, never guessed.
    """
    outcome = str((result or {}).get("outcome") or "").strip()
    if outcome == "completed":
        return "completed", "completed"
    if outcome:
        return "failed", outcome
    return "failed", "turn_end_unobserved"


def _copilot_agent_text(events: list[dict[str, Any]]) -> str | None:
    """The concatenated agent_message_chunk text, bounded — turn diagnosis."""
    chunks: list[str] = []
    for event in events:
        if not isinstance(event, dict) or event.get("method") != "session/update":
            continue
        update = (event.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") != "agent_message_chunk":
            continue
        content = update.get("content") or {}
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text)
    return "".join(chunks)[:400] if chunks else None


def copilot_usage_receipt(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The meta ``usage`` receipt — honestly absent on the ACP wire today.

    No practitioner has ever observed a ``usage_update`` over
    ``copilot --acp`` and the spec's optional ``usage_update`` carries
    context-window occupancy (``used``/``size``), not token metering —
    nothing is mapped onto ``input_tokens``/``output_tokens`` and no cost
    is fabricated. If a future binary DOES emit one, its counters ride
    verbatim under additive keys with ``completeness: "unknown"`` (the
    opencode-lane doctrine); no event means no receipt at all (None, not
    a zeroed dict).
    """
    update: dict[str, Any] | None = None
    for event in events:
        if not isinstance(event, dict) or event.get("method") != "session/update":
            continue
        candidate = (event.get("params") or {}).get("update") or {}
        if candidate.get("sessionUpdate") == "usage_update":
            update = candidate
    if update is None:
        return None

    def _int(key: str) -> int | None:
        value = update.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    receipt: dict[str, Any] = {
        "driver": COPILOT_LANE_DRIVER_ID,
        "completeness": "unknown",
        "source": "session/update usage_update",
    }
    context_used = _int("used")
    context_size = _int("size")
    if context_used is not None:
        receipt["context_used"] = context_used
    if context_size is not None:
        receipt["context_size"] = context_size
    cost = update.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        receipt["total_cost_usd"] = float(cost)
    return receipt


async def drive_copilot_lane(
    client: Any,
    *,
    task: str,
    budget_s: float,
    grace_s: float = _DEFAULT_GRACE_S,
    poll_s: float = _POLL_INTERVAL_S,
) -> LaneOutcome:
    """Drive ONE copilot ACP session to its terminal ``stopReason``, bounded.

    ``start_session(task)`` answers IMMEDIATELY (``session/new`` assigns
    the id before any turn resolves — this lane's id-existence advantage),
    so the lane poll-watches the client's terminal record — the turn ends
    when a ``stopReason`` arrives — with the whole turn bounded by
    *budget_s*. On expiry the turn is cancelled (``session/cancel``); the
    client's cancel LEDGER owns the #4561 workaround: a response that
    says ``end_turn`` after a recorded cancel reports
    ``interrupted_by_ledger``, never ``completed``. A short *grace_s*
    window still honors the vendor's own terminal verdict (including a
    truthful ``cancelled``); only a turn that produces no verdict at all
    ends ``budget_exceeded``. The connection is ALWAYS closed (teardown
    is bounded inside the driver).

    No steering attach rides this lane yet: ACP has no mid-turn steer
    (the client refuses a second prompt with ``TurnInProgressError``),
    and the bridge's copilot adapter does not exist — the ``control``
    seam stays with the claude/codex/opencode arms until one does.

    NXT-28: the outcome carries the ``episode`` breakdown — startup
    (handshake + session/new to the id), the polled turn, the
    cancel+grace window (only when the budget expired it into one) and
    teardown (``close``).
    """
    loop = asyncio.get_running_loop()
    startup_started = loop.time()
    session_id = await client.start_session(task)
    startup_s = loop.time() - startup_started

    async def _poll_verdict(deadline: float) -> dict | None:
        while True:
            result = client.turn_result(session_id)
            if result is not None or loop.time() >= deadline:
                return result
            await asyncio.sleep(poll_s)

    async def _turn() -> tuple[LaneOutcome, float, float | None]:
        turn_started = loop.time()
        result = await _poll_verdict(turn_started + budget_s)
        interrupt_grace_s: float | None = None
        if result is None:
            # #4561: the cancel itself is fire-and-forget (a notification);
            # the verdict — corrected by the client's ledger — is what the
            # grace window waits for, never the wire's lying stopReason.
            try:
                await client.interrupt(session_id)
            except CopilotACPError:
                # e.g. the pipe died at the deadline — the grace poll below
                # is the turn's last chance (a connection_lost record from
                # the driver decides, never the error).
                pass
            grace_started = loop.time()
            result = await _poll_verdict(grace_started + grace_s)
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
        events = await client.query(session_id)
        exit_status, reason = classify_copilot_turn(result)
        error = str(result.get("error") or "") if isinstance(result, dict) else ""
        return (
            LaneOutcome(
                exit_status=exit_status,
                terminal_reason=reason,
                usage=copilot_usage_receipt(events),
                error=error,
                reply_excerpt=_copilot_agent_text(events),
            ),
            turn_s,
            interrupt_grace_s,
        )

    try:
        outcome, turn_s, interrupt_grace_s = await _turn()
    finally:
        teardown_started = loop.time()
        await client.close()
        teardown_s = loop.time() - teardown_started
    return replace(
        outcome,
        episode=_episode(startup_s, turn_s, interrupt_grace_s, teardown_s),
    )


def write_artifacts(
    outcome: LaneOutcome,
    *,
    attempt_base: str,
    model: str,
    meta_path: str = _META_PATH,
    usage_path: str = _USAGE_PATH,
    driver_id: str = LANE_DRIVER_ID,
    workspace_generation: str = "",
    credential_consumption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the batch lane's meta + usage contract for *outcome*.

    The meta keys are the ones ``_collect_candidate`` reads
    (``attempt_base`` / ``driver`` / ``model`` / ``exit`` / ``usage``),
    plus the additive ``terminal_reason`` audit field; ``.forge/usage.json``
    carries the same receipt beside it (the batch lane's layout). Unknown
    usage stays ``null`` — never a zeroed dict. *driver_id* is the lane's
    registered harness id (:data:`LANE_DRIVER_IDS` value).
    *workspace_generation* (R32-01) records the ACTIVE workspace
    generation the restored WIP landed in — the absolute path the
    collector step resolves (also named by the checkout's
    ``.forge/workspace-generation`` pointer); the meta paths stay
    anchored at the STABLE checkout, never inside a generation the
    promotion may retire. *credential_consumption* (R38-04) is the
    value-free consumer receipt — it rides BOTH durable journals (the
    meta the collector consumes and the ``.forge/steering.json``
    sidecar), on every post-staging exit path, so a failed bootstrap
    still preserves its UNRESOLVED delivery record.
    """
    meta: dict[str, Any] = {
        "attempt_base": str(attempt_base or ""),
        "driver": str(driver_id or LANE_DRIVER_ID),
        "model": str(model or ""),
        "exit": outcome.exit_status,
        "terminal_reason": outcome.terminal_reason,
        "usage": outcome.usage,
    }
    if workspace_generation:
        meta["workspace_generation"] = workspace_generation
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
    if credential_consumption is not None:
        meta["credential_consumption"] = credential_consumption
    meta_file = Path(meta_path)
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    # The Actions emit step rebuilds the v2 meta from scratch — the journal
    # and episode ride this sidecar so harness_entry can pass them through.
    extras: dict[str, Any] = {}
    if outcome.steering_journal is not None:
        extras["steering_journal"] = outcome.steering_journal
    if outcome.episode is not None:
        extras["episode"] = outcome.episode
    if credential_consumption is not None:
        extras["credential_consumption"] = credential_consumption
    if extras:
        meta_file.parent.joinpath("steering.json").write_text(
            json.dumps(extras, indent=2, sort_keys=True) + "\n"
        )
    usage_file = Path(usage_path)
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    usage_file.write_text(json.dumps(outcome.usage, indent=2, sort_keys=True) + "\n")
    return meta


def _write_wip_restore_sidecar(report: dict[str, Any], *, root: Path | None = None) -> None:
    """Land the wip-restore report in ``.forge/steering.json`` (R28-03).

    The report rides the STEERING SIDECAR (the same ``.forge/steering.json``
    the emit step passes through — writing to the meta directly never
    reaches the uploaded artifact because the emit step rebuilds it at
    forge-output/). LIVE-found. Used by BOTH the halted-required-restore
    path (the failure IS the lane's verdict) and the normal post-turn
    record. *root* anchors the write at a STABLE directory (R32-01): the
    lane may have chdir'd into a restored workspace GENERATION, but the
    CI shell reads the sidecar from the checkout it started in.
    """
    sidecar = (Path.cwd() if root is None else root) / ".forge/steering.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(sidecar.read_text()) if sidecar.is_file() else {}
    except ValueError:
        existing = {}
    existing["wip_restore"] = report
    sidecar.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")


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
    if driver_key == "copilot":
        # Copilot's own model names only — FORGE_HARNESS_MODEL is
        # deliberately out of the chain (the codex lesson: it carries
        # gateway-specific names the vendor backend cannot run).
        return os.environ.get("FORGE_COPILOT_MODEL") or os.environ.get("COPILOT_MODEL") or ""
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

    # R38-04 (#305) — the lane's CONSUMER receipt, carried on EVERY
    # post-staging exit path (a failed consumer bootstrap still preserves
    # its UNRESOLVED delivery record; no model-usage claim merely because
    # the broker returned). None until a delivery actually staged.
    consumption: dict[str, Any] | None = None

    def _fail(reason: str, detail: str = "") -> int:
        outcome = LaneOutcome(exit_status="failed", terminal_reason=reason, error=detail)
        write_artifacts(
            outcome,
            attempt_base=attempt_base,
            model=model,
            driver_id=driver_id,
            credential_consumption=consumption,
        )
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

    # Wave D + R28-03 + NEXT-03: the dispatch's resume MODE decides the
    # WIP-continuity contract BEFORE any vendor client or session
    # exists. FORGE_LANE_RESUME=1 marks a dispatch whose continuity is
    # REQUIRED: there, a restore that did not land (failed download,
    # refused binding, corrupt manifest, missing blob, an unreachable
    # resume-spec lookup) halts the lane with ZERO model turns — nonzero
    # exit, the evidence in the meta and the sidecar — never a silent
    # start-over on a base that may be half-restored. The literal
    # ``restart`` (NEXT-03) is the EXPLICIT discard: no download at all,
    # the report says the WIP was intentionally dropped. A fresh run (no
    # marker) proceeds: a 404 is normal for it.
    resume_report: dict[str, Any] | None = None
    work_id_for_resume = (
        os.environ.get("FORGE_WORK_ID") or os.environ.get("FORGE_RUN_ID") or ""
    ).strip()
    mode = resume_mode()
    if mode == "restart" or (work_id_for_resume and steering_enabled()):
        resume_report = _maybe_restore_wip(work_id_for_resume)
    if resume_requested():
        required_ok = bool((resume_report or {}).get("restored"))
        if not required_ok:
            detail = "; ".join((resume_report or {}).get("failures") or []) or (
                "the resume channel is not configured (FORGE_LANE_CONTROL_URL/TOKEN "
                "absent or steering disabled) — the required WIP restore was "
                "never attempted"
            )
            _write_wip_restore_sidecar(
                resume_report
                if resume_report is not None
                else {
                    "restored": False,
                    "required": True,
                    "files_restored": 0,
                    "failures": [detail],
                }
            )
            return _fail("wip_restore_failed", detail)

    # R38-02 (#303) — the runner-time credential redemption (profile b):
    # BEFORE any vendor client or session exists. The lane exchanges its
    # EXISTING attempt-scoped lane token for the bound model credential
    # and sets EXACTLY ONE env var (scrubbing the closed precedence-stray
    # set). A refused/unreachable redemption halts the lane with ZERO
    # model turns — there is NO ambient fallback credential, ever.
    redemption_record: dict[str, Any] | None = None
    if redemption_requested():
        try:
            redemption_record = redeem_lane_credential()
        except LaneCredentialRedemptionError as exc:
            return _fail("credential_redemption_failed", str(exc))
        # R38-04 (#305) — the consumer receipt AFTER secret staging: the
        # join (broker id ↔ redemption id ↔ attempt ↔ consumer) is built
        # while the durable audit row already exists; it starts honestly
        # UNRESOLVED and is promoted only by a completed turn below.
        consumption = credential_consumption_record(redemption_record=redemption_record)
    elif native_delivery_requested():
        # Native mode: the template's resolution step (the shipped
        # snippet) staged the provider-held secret BEFORE this process;
        # the runner-side receipt records what the runner can honestly
        # know (binding revision unknowable here — recorded unknown).
        consumption = credential_consumption_record()

    # R32-01: a successful restore landed a STABLE GENERATION beside the
    # checkout (the checkout itself was never renamed or removed — the
    # reviewer's P03 proved the whole-tree switch leaves a process sitting
    # in the target with a deleted cwd and a stranded parent CI shell).
    # The lane ENTERS the generation before any vendor client exists: the
    # Python process chdir'd (the parent shell's cwd is untouched), the
    # vendor cwd envs are rebound to the generation so an ambient
    # ``*_CWD`` cannot drag the agent back into the retired checkout, and
    # the artifacts/sidecar stay anchored at the STABLE checkout while the
    # candidate meta NAMES the active generation. Subsequent CI steps
    # resolve the workspace through the checkout's
    # ``.forge/workspace-generation`` pointer, never through an inherited
    # directory inode.
    original_cwd = Path.cwd()
    workspace_generation = str((resume_report or {}).get("workspace_generation") or "")
    if workspace_generation and Path(workspace_generation).is_dir():
        os.chdir(workspace_generation)
        for var in ("FORGE_CLAUDE_CWD", "CODEX_CWD", "COPILOT_CWD"):
            os.environ[var] = workspace_generation

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
    elif driver_key == "copilot":
        # Ambient env carries COPILOT_BINARY/COPILOT_CWD and the inherited
        # auth (COPILOT_GITHUB_TOKEN fine-grained PAT — the child reads the
        # env itself; classic ghp_ tokens fail silently).
        try:
            client = copilot_acp_client_from_env()
        except (RuntimeError, ValueError) as exc:
            return _fail("driver_setup_error", str(exc))
        # No steering attach on this lane yet: ACP has no mid-turn steer
        # and the bridge has no copilot adapter — control stays unused.
        drive = functools.partial(
            drive_copilot_lane,
            client,
            task=task,
            budget_s=budget_s,
            grace_s=grace_s,
            poll_s=poll_s,
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

    # Wave D: the restore report (attempted above, BEFORE any vendor
    # client existed) rides the steering sidecar.
    try:
        outcome = asyncio.run(drive())
    except Exception as exc:  # noqa: BLE001 — the lane always emits its meta
        outcome = LaneOutcome(exit_status="failed", terminal_reason="driver_error", error=str(exc))

    # R38-04 — the consumption status a completed turn earns; every
    # other outcome keeps the honest staged-unresolved record.
    if consumption is not None:
        consumption["consumer_status"] = consumption_status_for(outcome.exit_status)
    write_artifacts(
        outcome,
        attempt_base=attempt_base,
        model=model,
        driver_id=driver_id,
        meta_path=str(original_cwd / _META_PATH),
        usage_path=str(original_cwd / _USAGE_PATH),
        workspace_generation=workspace_generation,
        credential_consumption=consumption,
    )
    if resume_report is not None:
        _write_wip_restore_sidecar(resume_report, root=original_cwd)
    if redemption_record is not None:
        # The VALUE-FREE redemption receipt rides the sidecar (never the
        # value — the sidecar uploads with the candidate).
        sidecar = original_cwd / ".forge/steering.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing_sidecar = json.loads(sidecar.read_text()) if sidecar.is_file() else {}
        except ValueError:
            existing_sidecar = {}
        existing_sidecar["credential_redemption"] = redemption_record
        sidecar.write_text(json.dumps(existing_sidecar, indent=2, sort_keys=True) + "\n")
    print(
        f"lane_driver: {driver_id} exit={outcome.exit_status} reason={outcome.terminal_reason}",
        file=sys.stderr,
    )
    return 0 if outcome.exit_status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
