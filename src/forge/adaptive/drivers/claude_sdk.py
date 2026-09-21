"""The REAL Claude Agent SDK driver client for the execution lane (EXE-02).

This is the client :class:`~forge.adaptive.adapters.ClaudeSDKAdapter`
injects: it satisfies the :class:`~forge.adaptive.adapters.ClaudeSDKClient`
Protocol against the actual ``claude-agent-sdk`` (Python) package. It runs
in the EXECUTION LANE next to its runner — never inside the privileged API
process that owns credentials — and steering travels as guidance only:
nothing on this class changes what a turn is permitted to do.

Vendor surface mapped (verified against the ``claude-agent-sdk==0.2.157``
wheel; ``docs/research/claude-sdk-python.md`` — where doc-site summaries
and the package disagreed, the package won):

- ``start_session`` → ``ClaudeSDKClient(options=...)`` + ``connect()`` +
  ``query(task)``; the vendor session id is captured from the init
  ``SystemMessage`` and re-persisted from every ``ResultMessage`` (§2.2,
  §3, §6.2).
- ``send`` → ``query(text)`` on the LIVE client: the SDK auto-continues
  its session across ``query()`` calls, so the follow-up lands in the SAME
  vendor session (§3, §6.2). Before re-querying, the driver drains to the
  previous turn's ``ResultMessage`` — the §7.3 buffering discipline —
  bounded, because steering must never ADD a hang the scripted lane did
  not have.
- ``interrupt`` → ``interrupt()``, awaited with a timeout well under the
  SDK's internal 60s control-request timeout: an un-acked interrupt must
  not deadlock the lane (issue #1094 / PR #1095); completion keys off the
  aborted turn's terminal ``ResultMessage`` instead of the missing ack
  (§7).
- ``query`` → drains the raw SDK messages accumulated by the background
  receive pump as ``list[dict]`` (``dataclasses.asdict`` of the vendor
  dataclasses — field names exactly as the SDK defines them, no
  normalized schema invented).

Hacks ported from ``docs/research/forge-harness-hacks.md`` (each one is
LIVE-found doctrine, not preference):

- ephemeral per-session ``CLAUDE_CONFIG_DIR`` (commit e2adf02: agent
  auto-memory must never bleed into the next run — the brief IS the run's
  memory); forced LAST in the env merge so operator env cannot relocate
  the lane back into a shared ``~/.claude``;
- ``setting_sources=[]`` — the ``--setting-sources ''`` isolation; the
  SDK default (None) already loads nothing from disk, and the explicit
  empty list PINS that isolation against SDK default drift;
- ``permission_mode="bypassPermissions"`` for the headless lane (the
  allowlist whack-a-mole was declared unfixable after three LIVE denial
  waves — commit ce88e99), WITH the ``can_use_tool`` programmatic policy
  hook as the principled EXE-08 replacement the hacks doc calls for;
- the mechanical deny ``Bash(git commit:*)`` / ``Bash(git push:*)`` is
  UNIONED into ``disallowed_tools`` and cannot be removed by the operator
  — deny beats every permission mode, including bypass; the push/network
  denial itself is ARCHITECTURAL (no write credentials, push FORBIDDEN at
  the remote), and this client never silently widens it;
- allowlist rules stay ONE RULE PER LITERAL, comma-joined only at the
  operator env boundary (the A09 missing-comma bug: adjacent string
  literals once glued into a single unmatchable rule);
- ``ANTHROPIC_BASE_URL`` gateway passthrough (the LiteLLM/BYOK route)
  and ambient proxy vars flow into the subprocess ``env`` (Python merges
  ``options.env`` OVER the inherited environment);
- R5 vendor timeout budgets (``API_TIMEOUT_MS`` and friends) and the
  ``max_turns=200`` runaway bound.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

__all__ = ["ClaudeSDKDriverClient", "claude_sdk_client_from_env"]

logger = logging.getLogger(__name__)

try:
    # The vendor package is OPTIONAL: CI installs no vendor packages, and
    # this module must import cleanly without it — the actionable error
    # fires only when a client is actually CONSTRUCTED against a missing SDK.
    import claude_agent_sdk as _claude_agent_sdk
except ModuleNotFoundError:
    _claude_agent_sdk = None

_SDK_INSTALL_HINT = (
    "the claude-agent-sdk package is not importable — the Claude driver "
    "needs it (pip install 'claude-agent-sdk>=0.2.118'; import name "
    "claude_agent_sdk). There is no Claude Agent SDK surface to drive "
    "without it."
)

#: The mechanical deny (hacks doc): deny beats EVERY permission mode,
#: including bypassPermissions (vendor evaluation order: hooks → deny
#: rules → ask rules → permission mode → allow rules). The lane's real
#: push/network boundary is ARCHITECTURAL — no write credentials, push
#: FORBIDDEN at the remote — and this client never widens it.
_MECHANICAL_DENY_TOOLS: tuple[str, ...] = (
    "Bash(git commit:*)",
    "Bash(git push:*)",
)

#: Default allowlist: the lane's read-navigation core, ONE RULE PER
#: LITERAL (A09: adjacent string literals once glued into one unmatchable
#: merged rule — never concatenate rule literals; join only at a boundary).
#: Under bypassPermissions this list is documentation and defense-in-depth,
#: exactly as in the scripted lane.
_DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Glob",
    "Grep",
    "LS",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git -C * diff:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(grep:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(which:*)",
)

#: Ambient variables copied explicitly into the subprocess env. The SDK
#: merges ``options.env`` over the inherited environment, so these flow
#: anyway; naming them pins the passthrough against transport changes.
#: The live-lab proxy lesson: long streaming turns crawl without the
#: fast-path proxy (226s direct vs 2s proxied on an identical prompt).
_PASSTHROUGH_ENV: tuple[str, ...] = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)

#: R5 vendor timeout budgets (hacks doc): long API turns and long bash
#: tool calls must not die at client defaults mid-run.
_DEFAULT_ENV_BUDGETS: dict[str, str] = {
    "API_TIMEOUT_MS": "3000000",
    "BASH_DEFAULT_TIMEOUT_MS": "300000",
    "BASH_MAX_TIMEOUT_MS": "600000",
}


def _split_rules(value: str) -> list[str]:
    """Split a comma-joined rule string into rules (the A09 boundary).

    The comma-joined form is the TRANSPORT encoding operators write in
    env vars; this split is the only place a comma is ever interpreted,
    so a glued rule cannot smuggle through as one merged literal.
    """
    return [rule.strip() for rule in value.split(",") if rule.strip()]


def _message_to_dict(message: Any) -> dict:
    """One raw SDK message as a dict — the vendor field names exactly.

    The SDK's messages are dataclasses; ``asdict`` preserves their field
    names and nesting verbatim. No normalized schema is invented (the
    Protocol asks for raw SDK message dicts); the fallback only marks a
    non-dataclass object the SDK never documents.
    """
    try:
        return dataclasses.asdict(message)
    except TypeError:
        return {"type": type(message).__name__}


def _vendor_session_id_of(message: Any) -> str | None:
    """The vendor session id carried by *message*, if any.

    ``ResultMessage.session_id`` (and the streaming messages' attribute)
    is the Claude Code session UUID to persist every turn (§6.2); the
    init ``SystemMessage`` nests it inside ``data`` (§5). Extraction
    only — never a rewritten shape.
    """
    vendor_id = getattr(message, "session_id", None)
    if isinstance(vendor_id, str) and vendor_id:
        return vendor_id
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        nested = data.get("session_id")
        if isinstance(nested, str) and nested:
            return nested
        payload = data.get("result")
        if isinstance(payload, dict):
            nested = payload.get("session_id")
            if isinstance(nested, str) and nested:
                return nested
    return None


def _is_result_message(sdk: ModuleType, message: Any) -> bool:
    """Recognize the vendor ``ResultMessage`` (a top-level SDK export)."""
    result_cls = getattr(sdk, "ResultMessage", None)
    if result_cls is not None:
        return isinstance(message, result_cls)
    # Defensive duck check for stand-ins that do not export the class.
    return hasattr(message, "is_error") and hasattr(message, "num_turns")


def _log_stderr_line(line: str) -> None:
    # Without a stderr callback subprocess errors are invisible (§12).
    logger.debug("claude sdk stderr: %s", line)


@dataclasses.dataclass
class _LiveSession:
    """One live vendor conversation owned by this driver."""

    vendor_session_id: str
    sdk_client: Any
    config_dir: str
    owns_config_dir: bool
    messages: list[dict] = dataclasses.field(default_factory=list)
    pump: asyncio.Task[None] | None = None
    pump_error: BaseException | None = None
    interrupt_acked: bool | None = None
    session_id_seen: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    all_results_settled: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    results_issued: int = 0
    results_consumed: int = 0


class ClaudeSDKDriverClient:
    """The Claude Agent SDK client behind :class:`ClaudeSDKAdapter` (EXE-02).

    Satisfies the :class:`~forge.adaptive.adapters.ClaudeSDKClient`
    Protocol against the real ``claude_agent_sdk`` surface. One instance
    may own several vendor sessions; each gets its own ephemeral config
    dir, its own SDK client, and a single background receive task that
    owns the message iterator (the SDK's async-context stickiness: one
    owning task, everything else talks to flags and buffers).

    Permission posture: the default ``bypassPermissions`` is the honest
    headless-lane mode BECAUSE the lane is credential-less (the real
    boundary is architectural: no write credentials, push FORBIDDEN,
    candidate-as-artifact). The principled EXE-08 posture is
    ``can_use_tool=...`` with a non-bypass ``permission_mode`` — policy
    decidable in code instead of CLI prefix enumeration; under
    bypassPermissions the CLI short-circuits before consulting the
    callback, so choose deliberately. Either way the mechanical
    commit/push deny is unioned in and cannot be removed: deny beats
    every mode.

    ``config_root`` + ``resume`` opt INTO cross-instance continuation
    (repair re-dispatch on a resumed session, §6.2): a pinned root keeps
    the transcript findable by ``options.resume``. They must be given
    together, and they trade away the per-session cold start — the
    default is the ephemeral tmpdir.
    """

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        model: str | None = None,
        permission_mode: str = "bypassPermissions",
        allowed_tools: Sequence[str] = _DEFAULT_ALLOWED_TOOLS,
        disallowed_tools: Sequence[str] = (),
        can_use_tool: Callable[..., Awaitable[Any]] | None = None,
        base_url: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        passthrough_env: Sequence[str] = _PASSTHROUGH_ENV,
        max_turns: int = 200,
        config_root: str | Path | None = None,
        resume: str | None = None,
        interrupt_timeout: float = 10.0,
        drain_timeout: float = 300.0,
        session_id_timeout: float = 30.0,
        disconnect_timeout: float = 15.0,
        stderr_sink: Callable[[str], None] | None = None,
        sdk: ModuleType | None = None,
    ) -> None:
        resolved_sdk = sdk if sdk is not None else _claude_agent_sdk
        if resolved_sdk is None:
            raise RuntimeError(_SDK_INSTALL_HINT)
        if resume is not None and config_root is None:
            raise ValueError(
                "resume needs config_root: an ephemeral config dir cannot hold the "
                "transcript of the session being resumed (cold start is the default; "
                "continuation is an explicit operator choice)"
            )
        self._sdk = resolved_sdk
        self._cwd = cwd
        self._model = model
        self._permission_mode = permission_mode
        self._allowed_tools = tuple(allowed_tools)
        self._disallowed_tools = tuple(disallowed_tools)
        self._can_use_tool = can_use_tool
        self._base_url = base_url
        self._extra_env = dict(extra_env or {})
        self._passthrough_env = tuple(passthrough_env)
        self._max_turns = max_turns
        self._config_root = config_root
        self._resume = resume
        self._interrupt_timeout = interrupt_timeout
        self._drain_timeout = drain_timeout
        self._session_id_timeout = session_id_timeout
        self._disconnect_timeout = disconnect_timeout
        self._stderr_sink = stderr_sink if stderr_sink is not None else _log_stderr_line
        self._live: dict[str, _LiveSession] = {}

    # -- the ClaudeSDKClient Protocol ------------------------------------

    async def start_session(self, task: str) -> str:
        """Begin an SDK conversation for *task*; returns the vendor session id.

        The vendor id surfaces in the init ``SystemMessage`` (and again
        on every ``ResultMessage``); we wait for it bounded — a session
        whose id never surfaces cannot be steered or resumed, so we tear
        the subprocess down and fail closed instead of guessing.
        """
        config_dir, owns_config_dir = self._new_config_dir()
        options = self._options_for(config_dir)
        sdk_client = self._sdk.ClaudeSDKClient(options=options)
        await sdk_client.connect()
        session = _LiveSession(
            vendor_session_id="",
            sdk_client=sdk_client,
            config_dir=config_dir,
            owns_config_dir=owns_config_dir,
        )
        session.all_results_settled.set()
        session.pump = asyncio.create_task(
            self._pump_messages(session), name="forge-claude-receive-pump"
        )
        try:
            await sdk_client.query(task)
            self._note_turn_issued(session)
            await asyncio.wait_for(session.session_id_seen.wait(), timeout=self._session_id_timeout)
        except BaseException:
            await self._teardown(session)
            raise
        self._live[session.vendor_session_id] = session
        return session.vendor_session_id

    async def send(self, session_id: str, text: str) -> None:
        """Mid-conversation user input on a RUNNING session (steering).

        The SDK's honest continuous interaction is ``query()`` on the LIVE
        streaming client: the session auto-continues across ``query()``
        calls, so the follow-up lands in the same vendor session whether
        the previous turn is still running or already settled. Re-querying
        before the previous turn's ``ResultMessage`` was consumed can
        queue the prompt without executing it (the §7.3 buffering
        discipline), so we drain to that terminal state first — bounded,
        and on timeout the follow-up is sent anyway: steering must never
        ADD a hang the scripted lane did not have.
        """
        session = self._require(session_id)
        if session.results_issued > session.results_consumed:
            try:
                await asyncio.wait_for(
                    session.all_results_settled.wait(), timeout=self._drain_timeout
                )
            except TimeoutError:
                logger.warning(
                    "claude session %s: previous turn still unsettled after %.1fs; "
                    "queueing the follow-up anyway (the steer channel never hangs)",
                    session_id,
                    self._drain_timeout,
                )
        await session.sdk_client.query(text)
        self._note_turn_issued(session)

    async def interrupt(self, session_id: str) -> None:
        """Interrupt the running turn — defensively (issue #1094 / PR #1095).

        ``interrupt()`` awaits the CLI's control ack, which is documented
        to hang when a disconnect races it (fixed mid-2026; the lane
        cannot rely on the floor being installed). We bound the wait well
        under the SDK's internal 60s control timeout; on timeout we do
        NOT deadlock: the aborted turn still produces a ``ResultMessage``
        (§7.2), so completion keys off that terminal state instead of the
        missing ack — and even that wait is bounded.
        """
        session = self._require(session_id)
        try:
            await asyncio.wait_for(session.sdk_client.interrupt(), timeout=self._interrupt_timeout)
            session.interrupt_acked = True
            return
        except TimeoutError:
            session.interrupt_acked = False
        if session.results_issued > session.results_consumed:
            try:
                await asyncio.wait_for(
                    session.all_results_settled.wait(), timeout=self._interrupt_timeout
                )
            except TimeoutError:
                logger.warning(
                    "claude session %s: interrupt un-acked and no terminal result "
                    "after %.1fs — returning without deadlocking; the turn may "
                    "still be running",
                    session_id,
                    self._interrupt_timeout,
                )

    async def query(self, session_id: str) -> list[dict]:
        """Drain the raw SDK messages accumulated for *session_id* so far.

        Raw vendor message dicts (``dataclasses.asdict`` of the SDK's
        dataclasses — field names exactly as the SDK defines them).
        Draining CONSUMES: a second call returns only what arrived since.
        """
        session = self._require(session_id)
        drained, session.messages = session.messages, []
        return drained

    # -- lifecycle beyond the Protocol -----------------------------------

    async def close(self, session_id: str) -> None:
        """Tear down one session: pump, subprocess, ephemeral config dir."""
        session = self._live.pop(session_id, None)
        if session is not None:
            await self._teardown(session)

    async def aclose(self) -> None:
        """Tear down every live session this driver owns."""
        for session_id in list(self._live):
            await self.close(session_id)

    # -- internals ---------------------------------------------------------

    def _require(self, session_id: str) -> _LiveSession:
        session = self._live.get(session_id)
        if session is None:
            raise KeyError(
                f"unknown claude session {session_id!r}: start_session must return "
                f"before the session can be steered, interrupted or drained"
            )
        return session

    def _new_config_dir(self) -> tuple[str, bool]:
        """A config dir plus whether we own (and will remove) it."""
        if self._config_root is not None:
            # Operator-pinned root (resume/repair continuity): reused
            # across driver instances so options.resume can find the
            # transcript. This OPTS INTO sharing state between sessions;
            # the cold-start default is the ephemeral per-session tmpdir.
            return str(self._config_root), False
        return tempfile.mkdtemp(prefix="forge-claude-config-"), True

    def _options_for(self, config_dir: str) -> Any:
        env: dict[str, str] = dict(self._extra_env)
        if self._base_url is not None:
            # LiteLLM/BYOK gateway route (§9): Python MERGES options.env
            # over the inherited environment, so ambient credentials
            # survive and the explicit base URL wins on collision.
            env["ANTHROPIC_BASE_URL"] = self._base_url
        for name in self._passthrough_env:
            ambient = os.environ.get(name)
            if ambient is not None:
                env.setdefault(name, ambient)
        for name, value in _DEFAULT_ENV_BUDGETS.items():
            env.setdefault(name, value)
        # Forced LAST and never overridable: the ephemeral config dir IS
        # the cold-start isolation mechanism — operator env must not be
        # able to relocate the lane back into a shared ~/.claude.
        env["CLAUDE_CONFIG_DIR"] = config_dir
        return self._sdk.ClaudeAgentOptions(
            cwd=str(self._cwd) if self._cwd is not None else None,
            model=self._model,
            permission_mode=self._permission_mode,
            allowed_tools=list(self._allowed_tools),
            # Union, never replace: the mechanical deny survives every
            # operator configuration — deny beats every permission mode.
            disallowed_tools=sorted(set(self._disallowed_tools) | set(_MECHANICAL_DENY_TOOLS)),
            setting_sources=[],
            max_turns=self._max_turns,
            env=env,
            can_use_tool=self._can_use_tool,
            resume=self._resume,
            stderr=self._stderr_sink,
        )

    def _note_turn_issued(self, session: _LiveSession) -> None:
        session.results_issued += 1
        session.all_results_settled.clear()

    def _note_result(self, session: _LiveSession) -> None:
        session.results_consumed += 1
        if session.results_consumed >= session.results_issued:
            session.all_results_settled.set()

    async def _pump_messages(self, session: _LiveSession) -> None:
        """The single task that owns the receive iterator (§10.1/§12).

        Everything else reads the buffer and the flags; nothing else
        touches the iterator. The pump dying is recorded and logged —
        it must never take the lane down silently.
        """
        try:
            async for message in session.sdk_client.receive_messages():
                session.messages.append(_message_to_dict(message))
                vendor_id = _vendor_session_id_of(message)
                if vendor_id:
                    if not session.vendor_session_id:
                        session.vendor_session_id = vendor_id
                        session.session_id_seen.set()
                    else:
                        # Persist the handle from every turn (§6.2).
                        session.vendor_session_id = vendor_id
                if _is_result_message(self._sdk, message):
                    self._note_result(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.pump_error = exc
            logger.error("claude session pump failed: %s", exc)

    async def _teardown(self, session: _LiveSession) -> None:
        if session.pump is not None:
            session.pump.cancel()
            try:
                await session.pump
            except (asyncio.CancelledError, Exception):
                pass  # our own pump; its exit state is already recorded
        try:
            # disconnect() during a pending interrupt used to hang for the
            # SDK's full 60s control timeout (#1094 / PR #1095) — bounded
            # here so teardown can never wedge the lane either.
            await asyncio.wait_for(
                session.sdk_client.disconnect(), timeout=self._disconnect_timeout
            )
        except TimeoutError:
            logger.warning("claude session disconnect timed out; torn down anyway")
        except Exception as exc:
            logger.warning("claude session disconnect raised: %s", exc)
        if session.owns_config_dir:
            shutil.rmtree(session.config_dir, ignore_errors=True)


def _env_int(name: str, default: int) -> int:
    """A positive int from env, or *default*; malformed values fail CLOSED."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _env_float(name: str, default: float) -> float:
    """A positive float from env, or *default*; malformed values fail CLOSED."""
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


def claude_sdk_client_from_env(sdk: ModuleType | None = None) -> ClaudeSDKDriverClient:
    """Build a :class:`ClaudeSDKDriverClient` from the lane environment.

    Environment (all optional; documented defaults):

    - ``FORGE_CLAUDE_CWD`` — working dir (default: the SDK's process cwd).
    - ``FORGE_CLAUDE_MODEL`` — model alias/name (default: the SDK/CLI
      choice; a gateway only knows ITS OWN names — remember the ``[1m]``
      suffix lesson for the z.ai catalog).
    - ``FORGE_CLAUDE_PERMISSION_MODE`` — default ``bypassPermissions``
      (the headless-lane posture; pass e.g. ``acceptEdits`` to run the
      ``can_use_tool`` posture instead).
    - ``FORGE_CLAUDE_ALLOWED_TOOLS`` / ``FORGE_CLAUDE_DISALLOWED_TOOLS``
      — comma-joined rule strings (defaults: the built-in read-navigation
      rules / none). The mechanical deny is always unioned in and cannot
      be removed from here.
    - ``FORGE_CLAUDE_MAX_TURNS`` — default ``200`` (the runaway bound).
    - ``FORGE_CLAUDE_INTERRUPT_TIMEOUT`` / ``FORGE_CLAUDE_DRAIN_TIMEOUT``
      — seconds (defaults ``10`` / ``300``).
    - ``FORGE_CLAUDE_CONFIG_ROOT`` + ``FORGE_CLAUDE_RESUME`` — pinned
      config root and vendor session id for continuation (both or
      neither; refused closed when only one is given).
    - ``ANTHROPIC_BASE_URL`` — ambient gateway passthrough (the
      LiteLLM/BYOK route).

    Malformed numbers fail CLOSED — a typo must never silently downgrade
    to a default bound (the lane's fail-closed config-parsing doctrine).
    """
    allowed_raw = os.environ.get("FORGE_CLAUDE_ALLOWED_TOOLS")
    disallowed_raw = os.environ.get("FORGE_CLAUDE_DISALLOWED_TOOLS")
    return ClaudeSDKDriverClient(
        cwd=os.environ.get("FORGE_CLAUDE_CWD") or None,
        model=os.environ.get("FORGE_CLAUDE_MODEL") or None,
        permission_mode=os.environ.get("FORGE_CLAUDE_PERMISSION_MODE") or "bypassPermissions",
        allowed_tools=_split_rules(allowed_raw)
        if allowed_raw is not None
        else _DEFAULT_ALLOWED_TOOLS,
        disallowed_tools=_split_rules(disallowed_raw) if disallowed_raw is not None else (),
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
        max_turns=_env_int("FORGE_CLAUDE_MAX_TURNS", 200),
        config_root=os.environ.get("FORGE_CLAUDE_CONFIG_ROOT") or None,
        resume=os.environ.get("FORGE_CLAUDE_RESUME") or None,
        interrupt_timeout=_env_float("FORGE_CLAUDE_INTERRUPT_TIMEOUT", 10.0),
        drain_timeout=_env_float("FORGE_CLAUDE_DRAIN_TIMEOUT", 300.0),
        sdk=sdk,
    )
