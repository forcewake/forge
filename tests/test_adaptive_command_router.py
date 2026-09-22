"""NXT-10 — authenticated native commands reach work-scoped control decisions.

The chain this file pins: the three provider gateways parse the adaptive
verbs (only while ``FORGE_ADAPTIVE_COMMANDS_ENABLED`` is on — default OFF is
ZERO routing, not parse-then-refuse), and :class:`ControlCommandRouter`
turns one authenticated note into exactly one work-scoped mailbox record
plus ONE operator-visible journaled reply:

- authority is the SAME approver set as /go (never authorship) — a
  non-approver is refused WITH a note;
- the run reference resolves through the /go predicates (provider + project
  + the note's issue; ≥8-hex unambiguous short prefix); unknown/ambiguous
  targets are answered with the candidate runs, a wrong-issue target is
  never silently adopted, and a bare command picks the issue's latest
  non-terminal run;
- the decision is an ``OperatorControlService`` mailbox record (pause /
  resume / steer / answer) carrying the note-keyed idempotency key;
- the reply is journaled like /go's refusal notes (intent row → provider
  write → outcome row) and deduped by the note id (A11): a redelivered
  webhook earns exactly one reply and no second mailbox command.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import forge.adaptive.command_router as command_router
from forge.adaptive.command_router import (
    ADAPTIVE_NOTE_COMMANDS,
    ControlCommandRouter,
    adaptive_commands_enabled,
    adaptive_command_set,
    parse_adaptive_command,
    reset_shared_control_service,
    shared_control_service,
)
from forge.adaptive.wiring import OperatorControlService
from forge.config import Settings
from forge.durable.models import ActionLog, FlowRun
from forge.gateway.parser import parse_webhook
from forge.models.base import Base

FIXTURES = Path(__file__).parent / "fixtures"

PROJECT_ID = 42
ISSUE_IID = 7

#: Two runs of ONE issue sharing an 8-hex prefix (the ambiguity case).
RUN_A = "feedface" + "1" * 24  # active
RUN_B = "feedface" + "2" * 24  # terminal — one ACTIVE run per issue max
#: A run of a DIFFERENT issue in the same project (the wrong-issue case).
RUN_OTHER_ISSUE = "abcd0001" + "3" * 24

_NOTE_REPLY_KIND = "adaptive_command_note"

T0 = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        FORGE_BOT_USERNAME="forge-bot",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_APPROVERS="alice",
    )
    values.update(overrides)
    return Settings(**values)


class Posted:
    """The fake provider note channel: records every reply body."""

    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def __call__(self, body: str) -> dict[str, int]:
        self.bodies.append(body)
        return {"id": len(self.bodies)}


async def _seed_run(
    session_factory,
    run_id: str,
    *,
    status: str = "planning",
    issue: int = ISSUE_IID,
    project_id: int = PROJECT_ID,
    provider: str = "gitlab",
    created_at: datetime | None = None,
) -> None:
    async with session_factory() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=project_id,
                issue_iid=issue,
                provider=provider,
                status=status,
                created_at=created_at or T0,
            )
        )
        await session.commit()


def _gitlab_note(text: str, *, verb: str = "pause", note_id: int = 5001, author: str = "alice"):
    return {
        "command": "adaptive_control",
        "provider": "gitlab",
        "adaptive_verb": verb,
        "project_id": PROJECT_ID,
        "issue_iid": ISSUE_IID,
        "author_username": author,
        "note_text": text,
        "note_id": note_id,
    }


async def _journal(session_factory) -> list[ActionLog]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(ActionLog).where(ActionLog.action_kind == _NOTE_REPLY_KIND)
                )
            )
            .scalars()
            .all()
        )


@pytest.fixture()
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
def control() -> OperatorControlService:
    reset_shared_control_service()
    service = OperatorControlService()
    yield service
    reset_shared_control_service()


@pytest.fixture()
def posted() -> Posted:
    return Posted()


@pytest.fixture()
def router(session_factory, control, posted) -> ControlCommandRouter:
    return ControlCommandRouter(
        session_factory=session_factory, settings=_settings(), post_note=posted, control=control
    )


# ---------------------------------------------------------------------------
# The rollout flag — default OFF means zero routing
# ---------------------------------------------------------------------------


def test_flag_defaults_off_and_fails_closed():
    assert adaptive_commands_enabled({}) is False
    assert adaptive_commands_enabled({"FORGE_ADAPTIVE_COMMANDS_ENABLED": ""}) is False
    assert adaptive_commands_enabled({"FORGE_ADAPTIVE_COMMANDS_ENABLED": "yes please"}) is False
    assert adaptive_command_set({}) == frozenset()
    for spelling in ("1", "true", "yes", "on"):
        assert adaptive_commands_enabled({"FORGE_ADAPTIVE_COMMANDS_ENABLED": spelling}) is True
    assert adaptive_command_set({"FORGE_ADAPTIVE_COMMANDS_ENABLED": "on"}) == ADAPTIVE_NOTE_COMMANDS


# ---------------------------------------------------------------------------
# Note parsing (pure)
# ---------------------------------------------------------------------------


def test_parse_pause_and_resume_forms():
    assert parse_adaptive_command("pause", "/pause") == (
        command_router.ParsedAdaptiveCommand(verb="pause", run_ref=None),
        "",
    )
    assert parse_adaptive_command("pause", "/pause feedface1 trailing text")[0].run_ref == (
        "feedface1"
    )
    parsed, _ = parse_adaptive_command("resume", "@forge /resume FEEDFACE1")
    assert parsed.run_ref is not None
    assert parsed.run_ref.lower() == "feedface1"


def test_parse_steer_requires_text_and_optionally_scopes_the_run():
    parsed, error = parse_adaptive_command("steer", "/steer fix the failing assertion first")
    assert error == ""
    assert parsed.run_ref is None
    assert parsed.text == "fix the failing assertion first"
    parsed, _ = parse_adaptive_command("steer", "/steer feedface1 use the existing helper")
    assert parsed.run_ref == "feedface1"
    assert parsed.text == "use the existing helper"
    parsed, error = parse_adaptive_command("steer", "/steer   ")
    assert parsed is None
    assert "guidance text" in error


def test_parse_answer_names_its_question():
    parsed, error = parse_adaptive_command("answer", "/answer q-3 use option B")
    assert error == ""
    assert parsed.question_id == "q-3"
    assert parsed.text == "use option B"
    parsed, error = parse_adaptive_command("answer", "/answer")
    assert parsed is None
    assert "question id" in error


# ---------------------------------------------------------------------------
# Routing: authority, scoping, mailbox, reply journal
# ---------------------------------------------------------------------------


async def test_pause_records_a_work_scoped_mailbox_command(
    router, session_factory, control, posted
):
    await _seed_run(session_factory, RUN_A)

    outcome = await router.handle(_gitlab_note("/pause"))

    assert outcome == {"status": "applied", "verb": "pause", "run_id": RUN_A}
    (command,) = await control.pending(RUN_A)
    assert command.kind == "pause"
    assert command.work_id == RUN_A
    assert command.actor_ref == "alice"
    assert command.actor_origin == "server_authenticated_human"
    assert command.idempotency_key == f"adaptive:pause:{RUN_A}:5001"
    # The pause fence is on record for the work (CTL-05 ordering is the
    # service's own leg — the router only guarantees the mailbox record).
    assert control.pause_states[RUN_A].pause_requested is True
    # ONE operator-visible reply, journaled intent→outcome with the note id.
    assert len(posted.bodies) == 1
    assert RUN_A[:8] in posted.bodies[0]
    rows = await _journal(session_factory)
    assert [row.status for row in rows] == ["succeeded"]
    # R28-09: the reply key carries the SUBJECT namespace (provider +
    # project + issue) — equal numeric note ids on different works never
    # suppress one another.
    assert rows[0].idempotency_key == "adaptive-note:gitlab:42:7:5001"
    assert rows[0].flow_run_id == RUN_A


async def test_pause_raises_the_durable_publication_fence_resume_clears_it(
    router, session_factory, control
):
    """R28-08: control processing leaves ONE persisted authority state the
    publisher reads — raised with the epoch the pause bumped, cleared by
    resume under a NEW epoch."""
    from forge.adaptive.pause_fence import pause_fence_decision

    await _seed_run(session_factory, RUN_A)
    await router.handle(_gitlab_note("/pause", note_id=5013))

    decision = await pause_fence_decision(session_factory, RUN_A)
    assert decision.fenced is True
    assert decision.publication_epoch_bumped == 1  # the epoch CTL-05 bumped to

    # CTL-06's gate: resume only from a confirmed checkpoint — until then
    # the fence stands even though the mailbox holds the pause.
    state = control.pause_states[RUN_A]
    control.pause_states[RUN_A] = replace(state, checkpoint_captured=True)
    applied = await router.handle(_gitlab_note("/resume", verb="resume", note_id=5018))
    assert applied["status"] == "applied"

    cleared = await pause_fence_decision(session_factory, RUN_A)
    assert cleared.fenced is False
    assert cleared.resumed_publication_epoch == 2  # the NEW epoch resume opened


async def test_pause_short_id_resolves_like_go(router, session_factory, control):
    await _seed_run(session_factory, RUN_A)

    outcome = await router.handle(_gitlab_note(f"/pause {RUN_A[:9]}", note_id=5002))

    assert outcome["run_id"] == RUN_A
    assert (await control.pending(RUN_A))[0].idempotency_key == f"adaptive:pause:{RUN_A}:5002"


async def test_ambiguous_prefix_is_refused_with_candidates(
    router, session_factory, control, posted
):
    await _seed_run(session_factory, RUN_A, created_at=T0)
    await _seed_run(session_factory, RUN_B, status="failed", created_at=T0 + timedelta(minutes=1))

    outcome = await router.handle(_gitlab_note(f"/pause {RUN_A[:8]}", note_id=5003))

    assert outcome["status"] == "refused"
    assert await control.pending(RUN_A) == []
    body = posted.bodies[0]
    assert "ambiguous" in body
    assert RUN_A in body and RUN_B in body  # the candidate list names full ids


async def test_unknown_run_is_refused_with_candidates(router, session_factory, control, posted):
    await _seed_run(session_factory, RUN_A)

    outcome = await router.handle(_gitlab_note("/pause ffffffffffffffff", note_id=5004))

    assert outcome["status"] == "refused"
    assert await control.pending(RUN_A) == []
    body = posted.bodies[0]
    assert "matched no run" in body
    assert RUN_A in body  # the issue's runs are listed as candidates


async def test_foreign_and_wrong_issue_targets_are_never_adopted(
    router, session_factory, control, posted
):
    await _seed_run(session_factory, RUN_OTHER_ISSUE, issue=99)

    outcome = await router.handle(_gitlab_note(f"/pause {RUN_OTHER_ISSUE}", note_id=5005))

    assert outcome["status"] == "refused"
    assert "different issue" in posted.bodies[0]
    assert await control.pending(RUN_OTHER_ISSUE) == []


async def test_bare_command_targets_the_latest_active_run(router, session_factory, control, posted):
    await _seed_run(session_factory, RUN_A, created_at=T0)
    # A NEWER but TERMINAL run must not steal the bare command.
    newer_terminal = T0 + timedelta(hours=1)
    await _seed_run(session_factory, RUN_B, status="ready_for_human", created_at=newer_terminal)

    outcome = await router.handle(_gitlab_note("/pause", note_id=5006))

    assert outcome["run_id"] == RUN_A


async def test_bare_command_without_any_active_run_is_refused_with_candidates(
    router, session_factory, control, posted
):
    await _seed_run(session_factory, RUN_B, status="failed")

    outcome = await router.handle(_gitlab_note("/resume", verb="resume", note_id=5007))

    assert outcome["status"] == "refused"
    assert "no active run" in posted.bodies[0]
    assert RUN_B in posted.bodies[0]
    assert await control.pending(RUN_B) == []


async def test_non_approver_is_refused_with_a_note(router, session_factory, control, posted):
    await _seed_run(session_factory, RUN_A)

    outcome = await router.handle(_gitlab_note("/pause", note_id=5008, author="mallory"))

    assert outcome["status"] == "refused"
    assert "mallory" in posted.bodies[0]
    assert "configured approvers" in posted.bodies[0]
    assert await control.pending(RUN_A) == []
    # The refusal is journaled like every reply (one per note id).
    rows = await _journal(session_factory)
    assert [row.status for row in rows] == ["succeeded"]


async def test_redelivery_is_a_deduplicated_noop(router, session_factory, control, posted):
    await _seed_run(session_factory, RUN_A)
    note = _gitlab_note("/pause", note_id=5009)

    first = await router.handle(note)
    second = await router.handle(note)

    assert first["status"] == "applied"
    assert second == {"status": "deduplicated"}
    assert len(posted.bodies) == 1
    assert len(await control.pending(RUN_A)) == 1


async def test_a_second_distinct_note_pauses_once_but_records_both_commands(
    router, session_factory, control, posted
):
    """A genuinely NEW note (different delivery id) is not swallowed by the
    reply dedup — it records its own mailbox command and its own reply."""
    await _seed_run(session_factory, RUN_A)

    await router.handle(_gitlab_note("/pause", note_id=5010))
    await router.handle(_gitlab_note("/pause", note_id=5011))

    assert len(posted.bodies) == 2
    kinds = [command.kind for command in await control.pending(RUN_A)]
    assert kinds == ["pause", "pause"]


async def test_steer_records_payload_and_run_scoping(router, session_factory, control, posted):
    await _seed_run(session_factory, RUN_A)

    outcome = await router.handle(
        _gitlab_note("/steer fix the failing assertion first", verb="steer", note_id=5012)
    )

    assert outcome["status"] == "applied"
    (command,) = await control.pending(RUN_A)
    assert command.kind == "steer"
    assert command.payload["text"] == "fix the failing assertion first"
    assert command.payload["run_id"] == RUN_A
    assert "never grants authority" in posted.bodies[0]


async def test_acceptance_weakening_steer_is_rejected_without_a_mailbox_command(
    router, session_factory, control, posted
):
    await _seed_run(session_factory, RUN_A)

    outcome = await router.handle(
        _gitlab_note("/steer skip the tests please", verb="steer", note_id=5013)
    )

    assert outcome["status"] == "refused"
    assert "revision gate" in posted.bodies[0]
    assert await control.pending(RUN_A) == []


async def test_resume_refused_without_a_confirmed_checkpoint_then_applied(
    router, session_factory, control, posted
):
    await _seed_run(session_factory, RUN_A)
    await router.handle(_gitlab_note("/pause", note_id=5014))
    posted.bodies.clear()

    refused = await router.handle(_gitlab_note("/resume", verb="resume", note_id=5015))

    assert refused["status"] == "refused"
    assert "confirmed checkpoint" in posted.bodies[0]

    # CTL-06's gate: stand the work on a captured checkpoint, then resume.
    state = control.pause_states[RUN_A]
    control.pause_states[RUN_A] = replace(state, checkpoint_captured=True)
    applied = await router.handle(_gitlab_note("/resume", verb="resume", note_id=5016))

    assert applied == {"status": "applied", "verb": "resume", "run_id": RUN_A}
    kinds = [command.kind for command in control.mailbox.commands.values()]
    assert kinds == ["pause", "resume"]


async def test_answer_records_the_question_scoped_command(router, session_factory, control, posted):
    await _seed_run(session_factory, RUN_A)

    outcome = await router.handle(
        _gitlab_note("/answer q-3 use option B", verb="answer", note_id=5017)
    )

    assert outcome["status"] == "applied"
    (command,) = await control.pending(RUN_A)
    assert command.kind == "answer"
    assert command.payload["question_id"] == "q-3"
    assert command.payload["text"] == "use option B"
    assert "recorded" in posted.bodies[0]


async def test_duplicate_answer_for_the_same_question_is_reported_not_rerecorded(
    router, session_factory, control, posted
):
    await _seed_run(session_factory, RUN_A)
    await router.handle(_gitlab_note("/answer q-3 use option B", verb="answer", note_id=5018))
    posted.bodies.clear()

    outcome = await router.handle(_gitlab_note("/answer q-3 again", verb="answer", note_id=5019))

    assert outcome["status"] == "applied"
    assert "already on record" in posted.bodies[0]
    # The answer key is work+question scoped: one mailbox command, not two.
    answers = [c for c in control.mailbox.commands.values() if c.kind == "answer"]
    assert len(answers) == 1


async def test_malformed_note_gets_a_usage_reply_not_silence(
    router, session_factory, control, posted
):
    await _seed_run(session_factory, RUN_A)

    outcome = await router.handle(_gitlab_note("/steer   ", verb="steer", note_id=5020))

    assert outcome["status"] == "refused"
    assert "could not be applied" in posted.bodies[0]
    assert await control.pending(RUN_A) == []


async def test_provider_scoped_approvers_apply_github_and_azure(session_factory, control, posted):
    """Authority comes from the PROVIDER's approver set (FORGE_GITHUB_APPROVERS
    / FORGE_AZDO_APPROVERS with the FORGE_APPROVERS fallback) — never from
    the note's authorship."""
    github_router = ControlCommandRouter(
        session_factory=session_factory,
        settings=_settings(FORGE_GITHUB_APPROVERS="octocat"),
        post_note=posted,
        control=control,
    )
    await _seed_run(
        session_factory,
        "99887766" + "4" * 24,
        provider="github",
        project_id=70010,
        issue=42,
    )
    note = {
        "command": "adaptive_control",
        "provider": "github",
        "adaptive_verb": "pause",
        "project_id": 70010,
        "issue_number": 42,
        "repo_full_name": "acme/acme-widget",
        "author_username": "octocat",
        "note_text": "/pause",
        "note_id": 9011,
    }

    assert (await github_router.handle(note))["status"] == "applied"

    note["author_username"] = "alice"  # a GitLab-listed approver, not a GitHub one
    note["note_id"] = 9012
    assert (await github_router.handle(note))["status"] == "refused"
    assert "ignored" in posted.bodies[-1]


async def test_a_redelivered_steer_after_a_failed_reply_records_one_command(
    session_factory, control
):
    """R28-09: the crash/reply-failure window after the mailbox commit.

    The first delivery's mailbox insert SUCCEEDS but the operator reply
    FAILS, so the reply-journal dedup (which keys on a SUCCEEDED reply)
    cannot suppress the redelivery — the router re-runs steer. The
    mailbox must still hold ONE steer command: its idempotency key is
    the NATIVE event identity (verb + work + delivery id), not a fresh
    random key per attempt. Replacing the native key with uuid4 fails
    the count assertion (mutation)."""
    await _seed_run(session_factory, RUN_A)

    class FailingThenWorking:
        def __init__(self) -> None:
            self.bodies: list[str] = []
            self.failed_once = False

        async def __call__(self, body: str) -> dict[str, int]:
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError("provider unreachable")  # the FIRST reply leg dies
            self.bodies.append(body)
            return {"id": 1}

    poster = FailingThenWorking()
    flaky_router = ControlCommandRouter(
        session_factory=session_factory, settings=_settings(), post_note=poster, control=control
    )
    note = _gitlab_note("/steer fix the parser first", verb="steer", note_id=5031)

    first = await flaky_router.handle(note)
    replay = await flaky_router.handle(note)

    assert first["status"] == "applied"  # the mailbox record stood
    assert replay["status"] == "applied"  # the redelivery completed the reply
    steers = [c for c in control.mailbox.commands.values() if c.kind == "steer"]
    assert len(steers) == 1  # ONE logical steer — the native identity deduped it
    assert len(poster.bodies) == 1  # and ONE eventual operator reply


#: A run of ANOTHER issue in the same project (the equal-note-id subject).
RUN_OTHER_SUBJECT = "abcd0002" + "5" * 24


async def test_equal_note_ids_on_different_works_never_suppress_one_another(
    router, session_factory, control, posted
):
    """R28-09's namespace rule: the dedup keys carry the WORK (mailbox) and
    the SUBJECT (reply journal), so two works receiving notes with the
    SAME numeric delivery id are two independent commands with two
    replies — equal note ids alone must not dedup either effect."""
    await _seed_run(session_factory, RUN_A)
    await _seed_run(session_factory, RUN_OTHER_SUBJECT, issue=8)

    first = await router.handle(
        _gitlab_note(f"/steer {RUN_A} fix the parser", verb="steer", note_id=5032)
    )
    second_note = _gitlab_note(
        f"/steer {RUN_OTHER_SUBJECT} fix the docs", verb="steer", note_id=5032
    )
    second_note["issue_iid"] = 8
    second = await router.handle(second_note)

    assert first["status"] == second["status"] == "applied"
    steer_a = [c for c in control.mailbox.commands.values() if c.work_id == RUN_A]
    steer_b = [c for c in control.mailbox.commands.values() if c.work_id == RUN_OTHER_SUBJECT]
    assert len(steer_a) == 1 and len(steer_b) == 1
    assert len(posted.bodies) == 2  # both subjects earned their reply


async def test_the_steer_key_is_the_stable_native_identity_not_a_random_one(
    router, session_factory, control
):
    """The key the router hands the mailbox is DERIVED (verb + work +
    note id) — deterministic across calls, so a durable mailbox index
    can actually dedup on it."""
    await _seed_run(session_factory, RUN_A)
    note = _gitlab_note("/steer keep going", verb="steer", note_id=5033)

    assert (
        ControlCommandRouter._idempotency_key("steer", note, RUN_A)
        == ControlCommandRouter._idempotency_key("steer", note, RUN_A)
        == f"adaptive:steer:{RUN_A}:5033"
    )
    await router.handle(note)
    (command,) = await control.pending(RUN_A)
    assert command.idempotency_key == f"adaptive:steer:{RUN_A}:5033"


# ---------------------------------------------------------------------------
# Gateway parse gating — disabled default is zero routing
# ---------------------------------------------------------------------------


def _note_event(text: str):
    payload = json.loads((FIXTURES / "note_issue.json").read_text())
    payload["user"]["username"] = "alice"
    payload["object_attributes"]["note"] = text
    return parse_webhook("Note Hook", payload)


def test_gitlab_match_run_command_gates_adaptive_verbs(monkeypatch):
    from forge.gateway.router import _match_run_command

    settings = _settings()
    monkeypatch.delenv(command_router.FORGE_ADAPTIVE_COMMANDS_ENV, raising=False)
    assert _match_run_command(_note_event("/pause"), settings) is None

    monkeypatch.setenv(command_router.FORGE_ADAPTIVE_COMMANDS_ENV, "1")
    command = _match_run_command(_note_event("@forge /pause feedface1"), settings)
    assert command is not None
    assert command["command"] == "adaptive_control"
    assert command["adaptive_verb"] == "pause"
    assert command["provider"] == "gitlab"
    assert command["project_id"] == PROJECT_ID
    assert command["issue_iid"] == 5
    assert command["author_username"] == "alice"
    assert command["note_id"] == 501
    assert command["note_text"] == "@forge /pause feedface1"


def test_github_normalize_issue_comment_gates_adaptive_verbs():
    from forge.gateway.github_webhook import normalize_issue_comment

    payload = {
        "issue": {"number": 42, "id": 99},
        "comment": {"id": 9010, "body": "/pause", "user": {"login": "octocat"}},
        "repository": {"id": 70010, "full_name": "acme/acme-widget"},
        "installation": {"id": 777},
    }

    # Flag off (the gateway passes no extra commands): not a command at all.
    assert normalize_issue_comment(payload) is None

    env = {"FORGE_ADAPTIVE_COMMANDS_ENABLED": "on"}
    command = normalize_issue_comment(payload, extra_commands=adaptive_command_set(env))
    assert command is not None
    assert command["command"] == "adaptive_control"
    assert command["adaptive_verb"] == "pause"
    assert command["provider"] == "github"
    assert command["note_id"] == 9010


def test_azure_parse_gates_adaptive_verbs():
    from forge.gateway.azure_webhook import normalize_workitem_comment

    payload = {
        "eventType": "workitem.commented",
        "resource": {
            "id": 142,
            "rev": 10,
            "fields": {
                "System.TeamProject": "Fabrikam",
                "System.ChangedBy": "dev@fabrikam.example",
                "System.History": "/pause",
            },
        },
        "resourceContainers": {
            "project": {"id": "9f8e7d6c-0000-0000-0000-000000000009"},
            "collection": {"baseUrl": "https://dev.azure.com/fabrikam/"},
        },
    }

    assert normalize_workitem_comment(payload) is None

    env = {"FORGE_ADAPTIVE_COMMANDS_ENABLED": "1"}
    command = normalize_workitem_comment(payload, adaptive_commands=adaptive_command_set(env))
    assert command is not None
    assert command["command"] == "adaptive_control"
    assert command["adaptive_verb"] == "pause"
    assert command["provider"] == "azure_devops"
    assert command["issue_number"] == 142


# ---------------------------------------------------------------------------
# The shared-service factory hook — FORGE_CONTROL_MAILBOX mounts the durable mailbox
# ---------------------------------------------------------------------------


def test_shared_control_service_mounts_postgres_when_the_env_asks(monkeypatch, tmp_path):
    """The one-line hook: shared_control_service() builds through
    control_service_from_env, so FORGE_CONTROL_MAILBOX=postgres plus a
    DATABASE_URL mounts the durable PostgresMailbox into the SAME
    process-shared singleton the router reads."""
    from forge.adaptive.mailbox_db import PostgresMailbox
    from forge.adaptive.wiring import FORGE_CONTROL_MAILBOX_ENV

    reset_shared_control_service()
    monkeypatch.setenv(FORGE_CONTROL_MAILBOX_ENV, "postgres")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'control.db'}")
    try:
        service = shared_control_service()
        assert isinstance(service.mailbox, PostgresMailbox)
        assert service.surface is service.mailbox
        # one lazily-built singleton: the second call returns the same mount
        assert shared_control_service() is service
    finally:
        reset_shared_control_service()
        monkeypatch.delenv(FORGE_CONTROL_MAILBOX_ENV, raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)


def test_shared_control_service_stays_in_memory_without_the_flag(monkeypatch):
    from forge.adaptive.control import Mailbox
    from forge.adaptive.wiring import FORGE_CONTROL_MAILBOX_ENV

    reset_shared_control_service()
    monkeypatch.delenv(FORGE_CONTROL_MAILBOX_ENV, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        service = shared_control_service()
        assert isinstance(service.mailbox, Mailbox)  # the honest default
    finally:
        reset_shared_control_service()
