"""Route authenticated native operator commands to work-scoped control (NXT-10).

The review's finding (§7): the driver APIs for steering/interrupt are real and
the mailbox bridge exists, but a human's ``/pause``/``/steer``/``/answer``/
``/resume`` comment never reached it — the three provider gateways parsed only
the classic verb set, so adaptive commands died unrouted. This module is the
missing application assembly between the two halves:

- the provider gateways (GitLab router, GitHub webhook, Azure DevOps webhook)
  parse the four adaptive verbs alongside the classic set and hand the note
  metadata to :func:`route_adaptive_command_note` — the SAME authenticated
  ingress path every other command travels (webhook signature first, parse
  second; never a chat text that happens to reach a model);
- authority is the SAME approver set as ``/go``
  (:func:`forge.runs.admission.approvers_for` — trusted configuration, never
  authorship): a non-approver's command is refused WITH an operator-visible
  note, exactly like a non-approver's ``/go``;
- the command is WORK-SCOPED before anything is recorded: the run id parsed
  from the note (``/pause <run-id>``, optional) resolves through the SAME
  predicates as ``/go`` — provider + project + the note's issue — with an
  unambiguous ≥8-hex short prefix allowed (the plan heading's form). Unknown,
  ambiguous and wrong-issue targets are answered with the candidate runs, never
  silently adopted; a bare command targets the issue's latest non-terminal run;
- the decision itself is a :class:`~forge.adaptive.wiring.OperatorControlService`
  mailbox record (:meth:`~forge.adaptive.wiring.OperatorControlService.pause` /
  ``resume`` / ``steer`` / ``answer``) — the surface a running lane's steering
  bridge drains. The service is constructed lazily (the discovery-stage
  pattern) and kept as ONE process-shared instance, because the reference
  mailbox is in-memory (NXT-09/12 own the durable Postgres swap; the
  ``MailboxSurface`` protocol is the seam);
- every applied OR refused command journals exactly ONE operator-visible reply
  note (``ActionLog`` intent/outcome rows, deduped by the note's delivery id —
  the /retry A11 pattern), so a redelivered webhook earns one reply, not a
  storm.

Rollout is honest: the whole surface sits behind
``FORGE_ADAPTIVE_COMMANDS_ENABLED`` (default OFF — the lanes run with steering
OFF too, ``FORGE_STEERING_ENABLED``). With the flag off the gateways do not
parse the verbs at all: zero routing, the classic workflow byte for byte.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.adaptive.wiring import OperatorControlService
from forge.durable.controller import TERMINAL_STATUSES
from forge.durable.models import ActionLog, FlowRun
from forge.runs.admission import approvers_for

logger = logging.getLogger(__name__)

__all__ = [
    "ADAPTIVE_NOTE_COMMANDS",
    "ADAPTIVE_RUN_COMMAND",
    "ADAPTIVE_VERBS",
    "ControlCommandRouter",
    "FORGE_ADAPTIVE_COMMANDS_ENV",
    "ParsedAdaptiveCommand",
    "RefusedControlRun",
    "ResolvedControlRun",
    "adaptive_commands_enabled",
    "adaptive_command_set",
    "parse_adaptive_command",
    "reset_shared_control_service",
    "route_adaptive_command_note",
    "shared_control_service",
]

#: The four native operator verbs this router owns. The gateways union this
#: set into their parsed command sets ONLY while the rollout flag is on.
ADAPTIVE_NOTE_COMMANDS: Final[frozenset[str]] = frozenset(
    {"/pause", "/resume", "/steer", "/answer"}
)

#: The normalized run-command name the gateways stamp on adaptive note
#: metadata (the classic names — ``go``, ``status``, … — stay untouched).
ADAPTIVE_RUN_COMMAND: Final = "adaptive_control"

#: The bare verb forms (``"/pause"[1:]``).
ADAPTIVE_VERBS: Final[frozenset[str]] = frozenset(
    {command[1:] for command in ADAPTIVE_NOTE_COMMANDS}
)

#: The env var that turns the whole surface on. Default OFF in this slice.
FORGE_ADAPTIVE_COMMANDS_ENV: Final = "FORGE_ADAPTIVE_COMMANDS_ENABLED"

#: Truthy spellings (the same closed set the discovery stage and the steering
#: attach use; anything else fails CLOSED).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: The journaled kind of an adaptive command's reply note (one per note id).
_NOTE_REPLY_KIND: Final = "adaptive_command_note"

#: How many candidate runs an unknown/ambiguous refusal lists (most recent
#: first — enough to pick from, never a wall).
_MAX_CANDIDATES: Final = 5


def adaptive_commands_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the adaptive command surface is turned on (default: off).

    Reads :data:`FORGE_ADAPTIVE_COMMANDS_ENV`; the closed truthy set mirrors
    ``FORGE_DISCOVERY_ENABLED``/``FORGE_STEERING_ENABLED``.
    """
    source = os.environ if env is None else env
    return str(source.get(FORGE_ADAPTIVE_COMMANDS_ENV, "")).strip().lower() in _TRUTHY


def adaptive_command_set(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The adaptive verbs a gateway may parse right now (empty when off).

    The gateways union this into their accepted command sets per delivery —
    the disabled default therefore means the verbs are not even recognized,
    which is what "zero routing" must mean (not parse-then-refuse).
    """
    return ADAPTIVE_NOTE_COMMANDS if adaptive_commands_enabled(env) else frozenset()


# ---------------------------------------------------------------------------
# Note parsing — verb arguments, /go-shaped run references
# ---------------------------------------------------------------------------

#: An optional run reference: the full 32-hex id or an unambiguous prefix of
#: at least 8 hex chars (the SAME forms ``/go`` accepts).
_RUN_REF: Final = r"(?:\s+([0-9a-fA-F]{8,32})\b)?"

_PAUSE_RE = re.compile(r"/pause" + _RUN_REF, re.IGNORECASE)
_RESUME_RE = re.compile(r"/resume" + _RUN_REF, re.IGNORECASE)
#: ``/steer [<run-id>] <text...>`` — the text is the payload; a leading
#: ≥8-hex token is read as the run reference.
_STEER_RE = re.compile(r"/steer" + _RUN_REF + r"\s*(.+)", re.IGNORECASE | re.DOTALL)
#: ``/answer <question-id> <text...>`` — the question id is explicit (the
#: NXT-07 rule: an answer must name the question it answers). The run is the
#: issue's latest one (questions belong to that work's discovery).
_ANSWER_RE = re.compile(r"/answer\s+(\S+)\s*(.*)", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class ParsedAdaptiveCommand:
    """One parsed adaptive note: the verb, its run reference, its payload."""

    verb: str
    run_ref: str | None = None
    text: str = ""
    question_id: str = ""


def parse_adaptive_command(verb: str, note_text: str) -> tuple[ParsedAdaptiveCommand | None, str]:
    """Parse *verb*'s arguments out of the note text.

    Returns ``(parsed, "")`` on success and ``(None, reason)`` when the note
    is malformed — the reason feeds the operator-visible usage reply (an
    unknown shape is answered, never a silent no-op).
    """
    text = str(note_text or "").strip()
    if verb in ("pause", "resume"):
        match = (_PAUSE_RE if verb == "pause" else _RESUME_RE).search(text)
        if match is None:  # pragma: no cover — the gateway matched the verb
            return None, f"/{verb} could not be parsed"
        return ParsedAdaptiveCommand(verb=verb, run_ref=match.group(1)), ""
    if verb == "steer":
        match = _STEER_RE.search(text)
        if match is None:
            return None, "/steer needs the guidance text: `/steer <run-id> <text>`"
        body = match.group(2).strip()
        if not body:
            return None, "/steer needs the guidance text: `/steer <run-id> <text>`"
        return ParsedAdaptiveCommand(verb=verb, run_ref=match.group(1), text=body), ""
    if verb == "answer":
        match = _ANSWER_RE.search(text)
        if match is None:
            return (
                None,
                "/answer needs the question id and the answer text: `/answer <question-id> <text>`",
            )
        body = match.group(2).strip()
        if not body:
            return None, "/answer needs the answer text: `/answer <question-id> <text>`"
        return ParsedAdaptiveCommand(verb=verb, question_id=match.group(1), text=body), ""
    return None, f"unknown adaptive verb {verb!r}"


# ---------------------------------------------------------------------------
# Reply bodies (operator-visible; one per note id)
# ---------------------------------------------------------------------------


def _automated() -> str:
    return "\n\n*This is an automated message.*"


def _run_line(run_id: str) -> str:
    return f"- `{run_id}` (short id `{run_id[:8]}`)"


def _candidates_block(run_ids: list[str]) -> str:
    if not run_ids:
        return "This issue has no runs yet."
    listed = "\n".join(_run_line(run_id) for run_id in run_ids[:_MAX_CANDIDATES])
    return f"Runs on this issue (most recent first):\n\n{listed}"


def _not_approver_body(verb: str, author: str) -> str:
    return (
        f"`/{verb}` from @{author} was ignored — operator control is restricted to "
        "the configured approvers (the same gate as `/go`: FORGE_APPROVERS, or the "
        f"provider-scoped list).{_automated()}"
    )


def _usage_body(verb: str, problem: str) -> str:
    return (
        f"`/{verb}` could not be applied: {problem}. Forms: `/pause <run-id>` "
        "(the run id is optional — bare targets the issue's latest run), "
        "`/steer <run-id> <text>`, `/answer <question-id> <text>`, "
        f"`/resume <run-id>`.{_automated()}"
    )


def _unknown_run_body(verb: str, requested: str, run_ids: list[str]) -> str:
    return (
        f"`/{verb} {requested}` matched no run on this issue. "
        f"{_candidates_block(run_ids)} Use the full 32-hex run id or an "
        f"unambiguous prefix of at least 8 hex characters.{_automated()}"
    )


def _ambiguous_run_body(verb: str, requested: str, run_ids: list[str]) -> str:
    listed = "\n".join(_run_line(run_id) for run_id in run_ids[:_MAX_CANDIDATES])
    return (
        f"`/{verb} {requested}` matches {len(run_ids)} runs on this issue — the "
        f"prefix is ambiguous. Use the full id:\n\n{listed}{_automated()}"
    )


def _wrong_issue_body(verb: str, requested: str) -> str:
    return (
        f"`/{verb} {requested[:8]}` targets a run planned on a different issue — "
        "control commands are scoped to the run's own issue "
        f"(`/status` here shows this issue's runs).{_automated()}"
    )


def _no_steerable_run_body(verb: str, run_ids: list[str]) -> str:
    return (
        f"`/{verb}` found no active run on this issue to target. "
        f"{_candidates_block(run_ids)} Name one explicitly with "
        f"`/{verb} <run-id>` if it should be controlled anyway.{_automated()}"
    )


def _pause_applied_body(run_id: str, pause_status: str) -> str:
    return (
        f"Pause recorded for run `{run_id[:8]}` — the command is in the run's "
        f"control mailbox (pause status: `{pause_status}`); the fence and the "
        f"interrupt ordering follow CTL-05.{_automated()}"
    )


def _resume_applied_body(run_id: str) -> str:
    return (
        f"Resume recorded for run `{run_id[:8]}` — the command is in the run's "
        f"control mailbox and resumes from the confirmed checkpoint.{_automated()}"
    )


def _resume_refused_body(run_id: str) -> str:
    return (
        f"`/resume` for run `{run_id[:8]}` was refused — there is no confirmed "
        "checkpoint to resume from (CTL-06: resume only stands on a captured "
        f"checkpoint). Pause the work first, or let it finish.{_automated()}"
    )


def _steer_accepted_body(run_id: str, classification: str) -> str:
    return (
        f"Steering recorded for run `{run_id[:8]}` (classified `{classification}`) — "
        "guidance is delivered at the next checkpoint boundary and never grants "
        f"authority.{_automated()}"
    )


def _steer_rejected_body(run_id: str) -> str:
    return (
        f"`/steer` for run `{run_id[:8]}` was rejected — the text tries to change "
        "acceptance policy, which requires the revision gate, not the steering "
        f"channel.{_automated()}"
    )


def _answer_recorded_body(run_id: str, question_id: str, created: bool) -> str:
    state = "recorded" if created else "already on record (this question was answered before)"
    return (
        f"Answer {state} for question `{question_id}` on run `{run_id[:8]}` — the "
        f"command is in the run's control mailbox.{_automated()}"
    )


# ---------------------------------------------------------------------------
# Run resolution — the /go predicates, provider-generic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedControlRun:
    """The note's run reference narrowed to exactly one run of this issue."""

    run_id: str


@dataclass(frozen=True)
class RefusedControlRun:
    """A resolution failure answered with one operator-visible reply."""

    body: str


async def _issue_runs(
    session: AsyncSession, *, provider: str, project_id: int, issue: int, active_only: bool
) -> list[FlowRun]:
    query = (
        select(FlowRun)
        .where(
            FlowRun.provider == provider,
            FlowRun.project_id == project_id,
            FlowRun.issue_iid == issue,
        )
        .order_by(FlowRun.created_at.desc(), FlowRun.id.desc())
    )
    if active_only:
        terminal = {status.value for status in TERMINAL_STATUSES}
        query = query.where(FlowRun.status.notin_(terminal))
    return list((await session.execute(query)).scalars().all())


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

#: The process-shared control service. The reference mailbox is in-memory
#: (review §8; the durable swap is NXT-09/12's leg), so /pause and /resume
#: arriving in different webhook deliveries must land on the SAME instance
#: or the second would not see the first's state — hence one lazily-built
#: process singleton, exactly how lane_driver mounts its steering service.
_SHARED_CONTROL: OperatorControlService | None = None


def shared_control_service() -> OperatorControlService:
    """The lazily-constructed, process-shared operator control service."""
    global _SHARED_CONTROL
    if _SHARED_CONTROL is None:
        _SHARED_CONTROL = OperatorControlService()
    return _SHARED_CONTROL


def reset_shared_control_service() -> None:
    """Drop the shared instance (the test isolation hook)."""
    global _SHARED_CONTROL
    _SHARED_CONTROL = None


NotePoster = Callable[[str], Awaitable[Any]]
"""async (body) -> provider reply — the provider-bound note channel."""


@dataclass
class ControlCommandRouter:
    """One adaptive command note → one work-scoped mailbox decision + reply.

    Composed per delivery by :func:`route_adaptive_command_note` (and directly
    by tests): the *post_note* callable is the provider-bound reply channel,
    *control* defaults to the process-shared
    :class:`~forge.adaptive.wiring.OperatorControlService`.
    """

    session_factory: Any
    settings: Any
    post_note: NotePoster
    control: OperatorControlService = field(default_factory=shared_control_service)

    async def handle(self, note: dict[str, Any]) -> dict[str, Any]:
        """Authorize, scope, record and answer one adaptive command note."""
        verb = str(note.get("adaptive_verb") or "")
        if verb not in ADAPTIVE_VERBS:
            logger.warning("Adaptive command note without a known verb — ignoring")
            return {"status": "ignored", "reason": "unknown verb"}

        # A note whose reply already succeeded is DONE (redelivery): the
        # journal check precedes everything, so a replayed webhook cannot
        # spend a second mailbox slot or a second reply.
        if await self._reply_delivered(self._note_key(note)):
            return {"status": "deduplicated"}

        provider = str(note.get("provider") or "gitlab")
        author = str(note.get("author_username") or "")
        if author not in approvers_for(provider, self.settings):
            logger.info("/%s from @%s who is not an approver — refusing with a note", verb, author)
            return await self._refuse(note, _not_approver_body(verb, author))

        parsed, problem = parse_adaptive_command(verb, str(note.get("note_text") or ""))
        if parsed is None:
            assert problem
            return await self._refuse(note, _usage_body(verb, problem))

        resolution = await self._resolve_run(note, parsed)
        if isinstance(resolution, RefusedControlRun):
            return await self._refuse(note, resolution.body)

        run_id = resolution.run_id
        applied = True
        if verb == "pause":
            state = self.control.pause(run_id, author, self._idempotency_key(verb, note, run_id))
            body = _pause_applied_body(run_id, state.pause_status)
        elif verb == "resume":
            resumed = self.control.resume(run_id, author, self._idempotency_key(verb, note, run_id))
            applied = resumed
            body = _resume_applied_body(run_id) if resumed else _resume_refused_body(run_id)
        elif verb == "steer":
            outcome = self.control.steer(run_id, author, parsed.text, run_id=run_id)
            if outcome.get("status") == "rejected":
                applied = False
                body = _steer_rejected_body(run_id)
            else:
                body = _steer_accepted_body(run_id, str(outcome.get("classification") or "steer"))
        else:  # answer
            created = self.control.answer(
                run_id, author, parsed.question_id, parsed.text, run_id=run_id
            )
            body = _answer_recorded_body(run_id, parsed.question_id, created)
        await self._reply(note, body, run_id=run_id)
        if applied:
            return {"status": "applied", "verb": verb, "run_id": run_id}
        return {"status": "refused", "verb": verb, "run_id": run_id, "reason": "no mailbox record"}

    # -- scoping ----------------------------------------------------------

    async def _resolve_run(
        self, note: dict[str, Any], parsed: ParsedAdaptiveCommand
    ) -> ResolvedControlRun | RefusedControlRun:
        """Narrow the note's run reference to one run of THIS issue.

        Provider, project and issue predicates are identical for every
        identifier form (the /go rules, R03/A07 subject scoping): a foreign
        run reads as unknown, a same-project run of a different issue reads
        as wrong-issue — never silently adopted. A bare command targets the
        issue's latest non-terminal run; an explicit id may name any run of
        the issue (the operator said which work to control).
        """
        provider = str(note.get("provider") or "gitlab")
        project_id = int(note.get("project_id") or 0)
        issue = int(note.get("issue_iid") or note.get("issue_number") or 0)
        requested = (parsed.run_ref or "").lower()
        async with self.session_factory() as session:
            if not requested:
                active = await _issue_runs(
                    session, provider=provider, project_id=project_id, issue=issue, active_only=True
                )
                if active:
                    return ResolvedControlRun(active[0].id)
                candidates = await _issue_runs(
                    session,
                    provider=provider,
                    project_id=project_id,
                    issue=issue,
                    active_only=False,
                )
                return RefusedControlRun(
                    _no_steerable_run_body(parsed.verb, [run.id for run in candidates])
                )
            if len(requested) == 32:
                run = await session.get(FlowRun, requested)
                if run is None or run.provider != provider or run.project_id != project_id:
                    candidates = await _issue_runs(
                        session,
                        provider=provider,
                        project_id=project_id,
                        issue=issue,
                        active_only=False,
                    )
                    logger.info(
                        "/%s references unknown run %s — refusing", parsed.verb, requested[:8]
                    )
                    return RefusedControlRun(
                        _unknown_run_body(parsed.verb, requested, [r.id for r in candidates])
                    )
                if run.issue_iid != issue:
                    logger.info(
                        "/%s for run %s posted on a different issue — refusing",
                        parsed.verb,
                        run.id[:8],
                    )
                    return RefusedControlRun(_wrong_issue_body(parsed.verb, requested))
                return ResolvedControlRun(run.id)
            matches = (
                (
                    await session.execute(
                        select(FlowRun)
                        .where(
                            FlowRun.provider == provider,
                            FlowRun.project_id == project_id,
                            FlowRun.issue_iid == issue,
                            FlowRun.id.like(f"{requested}%"),
                        )
                        .order_by(FlowRun.created_at.desc(), FlowRun.id.desc())
                    )
                )
                .scalars()
                .all()
            )
            if len(matches) == 1:
                return ResolvedControlRun(matches[0].id)
            if not matches:
                candidates = await _issue_runs(
                    session,
                    provider=provider,
                    project_id=project_id,
                    issue=issue,
                    active_only=False,
                )
                logger.info(
                    "/%s id %s matches no run on this issue — refusing", parsed.verb, requested[:8]
                )
                return RefusedControlRun(
                    _unknown_run_body(parsed.verb, requested, [r.id for r in candidates])
                )
            logger.info(
                "/%s prefix %s matches %d runs — refusing", parsed.verb, requested[:8], len(matches)
            )
            return RefusedControlRun(
                _ambiguous_run_body(parsed.verb, requested, [r.id for r in matches])
            )

    # -- the operator-visible reply (A11: one per note id) -----------------

    @staticmethod
    def _note_key(note: dict[str, Any]) -> str:
        note_id = str(note.get("note_id") or "")
        provider = str(note.get("provider") or "")
        return f"adaptive-note:{provider}:{note_id}" if note_id else ""

    @staticmethod
    def _idempotency_key(verb: str, note: dict[str, Any], run_id: str) -> str:
        """The work-scoped mailbox dedup key: verb + work + delivery identity."""
        return f"adaptive:{verb}:{run_id}:{note.get('note_id') or ''}"

    async def _reply_delivered(self, key: str) -> bool:
        if not key:
            return False
        async with self.session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ActionLog.id)
                        .where(
                            ActionLog.action_kind == _NOTE_REPLY_KIND,
                            ActionLog.idempotency_key == key,
                            ActionLog.status == "succeeded",
                        )
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        return row is not None

    async def _refuse(self, note: dict[str, Any], body: str) -> dict[str, Any]:
        await self._reply(note, body)
        return {"status": "refused", "reason": "operator-visible refusal"}

    async def _reply(self, note: dict[str, Any], body: str, *, run_id: str | None = None) -> None:
        """Journal and post ONE reply note — deduped by the note's id (A11).

        The /go refusal pattern: a ``requested`` ActionLog row (carrying the
        note's delivery identity in ``idempotency_key``) precedes the provider
        write, and the outcome completes it. A redelivered webhook finds the
        succeeded row and posts nothing.
        """
        key = self._note_key(note)
        if key and await self._reply_delivered(key):
            return
        provider = str(note.get("provider") or "")
        issue = int(note.get("issue_iid") or note.get("issue_number") or 0)
        async with self.session_factory() as session:
            action = ActionLog(
                flow_run_id=run_id,
                action_kind=_NOTE_REPLY_KIND,
                correlation_id=f"{provider}-issue-{issue}"[:100],
                idempotency_key=key or None,
                status="requested",
            )
            session.add(action)
            await session.commit()
            action_id = action.id
        try:
            await self.post_note(body)
        except Exception:
            logger.warning(
                "Adaptive command reply (note %s) could not be posted",
                note.get("note_id"),
                exc_info=True,
            )
            await self._complete_action(action_id, "failed")
            return
        await self._complete_action(action_id, "succeeded")

    async def _complete_action(self, action_id: int, status: str) -> None:
        async with self.session_factory() as session:
            row = await session.get(ActionLog, action_id)
            if row is not None:  # pragma: no cover — the row was just written
                row.status = status
                await session.commit()


# ---------------------------------------------------------------------------
# The gateway dispatch target
# ---------------------------------------------------------------------------


def _build_note_poster(settings: Any, note: dict[str, Any]) -> NotePoster | None:
    """The provider-bound reply channel for one adaptive command note.

    Clients are constructed lazily inside the poster (nothing network-ish
    happens at parse time), mirroring how the run-command executors build
    their per-task clients.
    """
    provider = str(note.get("provider") or "")

    if provider == "gitlab":
        project_id = int(note.get("project_id") or 0)
        issue_iid = int(note.get("issue_iid") or 0)

        async def post_gitlab(body: str) -> Any:
            from forge.gitlab.client import GitLabClient
            from forge.runs.service import forge_token

            async with GitLabClient(
                base_url=settings.GITLAB_URL, token=forge_token(settings)
            ) as gitlab:
                return await gitlab.create_issue_note(project_id, issue_iid, body)

        return post_gitlab

    if provider == "github":
        repo_full_name = str(note.get("repo_full_name") or "")
        if "/" not in repo_full_name:
            return None
        owner, repo = repo_full_name.split("/", 1)
        number = int(note.get("issue_number") or 0)

        async def post_github(body: str) -> Any:
            from forge.integrations.github import GitHubClient
            from forge.integrations.github_flow import credentials_from_settings

            client = GitHubClient(
                base_url=str(
                    getattr(settings, "FORGE_GITHUB_API_URL", "https://api.github.com")
                    or "https://api.github.com"
                ),
                token_provider=credentials_from_settings(settings),
            )
            try:
                return await client.create_issue_comment(owner, repo, number, body)
            finally:
                await client.aclose()

        return post_github

    if provider == "azure_devops":
        project = str(note.get("project") or "")
        work_item = int(note.get("issue_number") or 0)
        if not project or not work_item:
            return None

        async def post_azure(body: str) -> Any:
            # The hidden marker is what the Azure ingress's self-trigger
            # guard keys on (single-PAT deployments) — every forge-authored
            # comment must carry it.
            from forge.gateway.azure_webhook import FORGE_NOTE_MARKER
            from forge.integrations.azure import AzureDevOpsClient
            from forge.runs.azure_service import azure_credentials_from_settings

            client = AzureDevOpsClient(
                base_url=str(getattr(settings, "FORGE_AZDO_ORG_URL", "") or ""),
                token=azure_credentials_from_settings(settings),
            )
            try:
                return await client.add_work_item_comment(
                    project, work_item, f"{body}\n\n{FORGE_NOTE_MARKER}"
                )
            finally:
                await client.aclose()

        return post_azure

    return None


async def route_adaptive_command_note(
    settings: Any,
    session_factory: Any,
    note: dict[str, Any],
) -> dict[str, Any]:
    """The gateways' background dispatch target for one adaptive note.

    Builds the provider reply channel and the router, then runs the
    authorize → scope → record → answer pipeline. The gateways answer the
    webhook ``202`` BEFORE this runs (a Starlette background task), so a
    failure here is logged and reported in the result — never an unhandled
    exception after the response. Durability is the mailbox record plus the
    A11-journaled reply; a lost task is recovered by the provider's webhook
    redelivery, which re-enters here idempotently.
    """
    if session_factory is None:
        logger.warning("Adaptive command without a database — dropped")
        return {"status": "dropped", "reason": "no database"}
    poster = _build_note_poster(settings, note)
    if poster is None:
        logger.warning(
            "Adaptive command on provider %r without a reply channel — dropped",
            note.get("provider"),
        )
        return {"status": "dropped", "reason": "no reply channel"}
    router = ControlCommandRouter(
        session_factory=session_factory, settings=settings, post_note=poster
    )
    try:
        return await router.handle(note)
    except Exception:
        logger.exception("Adaptive command routing failed for note %s", note.get("note_id"))
        return {"status": "error", "reason": "routing failed"}
