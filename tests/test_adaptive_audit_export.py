"""R28-25 + NEXT-19 (#207): the exportable audit trail — the SIEM-ready
surface.

A full run lifecycle (creation, transitions, gate approval + consumption,
pause/resume steering, publication intent with an honest UNKNOWN
outcome, an external-write action) must come out as ONE chronological
trail with per-entry actor/action/timestamp/outcome, valid JSON, no
other project's rows, and no credential values. NEXT-19 adds the
schema-based allowlist: a credential binding/proof/receipt document
serializes ONLY its declared fields, and every dropped field is named as
a ``credential.export_field_dropped`` finding.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import forge.adaptive.audit_export  # noqa: F401 — pulls mailbox_db (control_commands) into metadata
from forge.adaptive.audit_export import (
    AUDIT_SCHEMA,
    EXPORT_FIELD_DROPPED,
    SYSTEM_ACTOR,
    audit_trail_for_project,
)
from forge.adaptive.mailbox_db import ControlCommandRow
from forge.durable import ActionLog, FlowRun, GateApproval, Outbox, PublicationIntent
from forge.models.base import Base

PROJECT_ID = 42
OTHER_PROJECT_ID = 999

#: Planted credential canaries — must survive NOWHERE in the export.
CANARY_VALUE = "sk-canary-0123456789abcdef"
CANARY_TOKEN = "glpat-canary-0123456789"
#: A sentinel under an unexpected NESTED key inside a schema'd document.
CANARY_NESTED = "ghp_nestedcanary0123456789"

RUN_ID = "run-" + uuid4().hex[:12]
T0 = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_lifecycle(db) -> None:
    """One full run lifecycle across every journaled surface."""
    async with db() as session:
        session.add(
            FlowRun(
                id=RUN_ID,
                project_id=PROJECT_ID,
                issue_iid=7,
                provider="gitlab",
                status="ready_for_human",
                evidence={
                    "requested_by": "alice",
                    # the canary: a credential VALUE smuggled into evidence
                    # must be redacted out of the export.
                    "api_key": CANARY_VALUE,
                    "note": f"token={CANARY_TOKEN}",
                },
                created_at=_at(0),
            )
        )
        # A stranger's run — project isolation must keep it out.
        session.add(
            FlowRun(
                id="run-other-" + uuid4().hex[:8],
                project_id=OTHER_PROJECT_ID,
                issue_iid=1,
                provider="gitlab",
                status="planning",
                created_at=_at(1),
            )
        )
        for index, (status_from, status_to) in enumerate(
            [("accepted", "preflight"), ("preflight", "planning")]
        ):
            session.add(
                Outbox(
                    flow_run_id=RUN_ID,
                    event_type="flow.transition",
                    payload={"flow_run_id": RUN_ID, "from": status_from, "to": status_to},
                    created_at=_at(2 + index),
                )
            )
        session.add(
            GateApproval(
                flow_run_id=RUN_ID,
                generation=1,
                plan_digest="p" * 64,
                base_sha="b" * 40,
                policy_digest="y" * 64,
                spec_digest="s" * 64,
                approver_user_id=17,
                source_event_id="evt-1",
                expires_at=_at(60),
                consumed_at=_at(5),
                created_at=_at(4),
            )
        )
        for sequence, (kind, status, minutes) in enumerate(
            [("pause", "applied", 6), ("resume", "applied", 8)], start=1
        ):
            session.add(
                ControlCommandRow(
                    id=f"cmd-{kind}",
                    work_id="wp-demo-1",
                    run_id=RUN_ID,
                    kind=kind,
                    payload={"run_id": RUN_ID},
                    status=status,
                    sequence=sequence,
                    dedup_key=f"note:{sequence}",
                    actor_ref="human:reviewer-17",
                    actor_origin="server_authenticated_human",
                    journal=[{"at": _iso(_at(minutes)), "to": status}],
                    created_at=_at(minutes),
                    applied_at=_at(minutes + 0.5),
                )
            )
        session.add(
            PublicationIntent(
                run_id=RUN_ID,
                provider="gitlab",
                repo="group/project",
                operation="commit",
                target_ref="refs/heads/forge/run",
                idempotency_scope="cycle-1",
                operation_key="op-key-1",
                commit_cycle=1,
                status="unknown",  # the honest unknown effect
                created_at=_at(10),
                updated_at=_at(11),
            )
        )
        session.add(
            ActionLog(
                flow_run_id=RUN_ID,
                action_kind="post_note",
                status="unknown_outcome",  # the honest unknown write
                correlation_id="corr-1",
                created_at=_at(12),
            )
        )
        await session.commit()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


#: The adversarial credential run's id (separate from the happy run).
CRED_RUN_ID = "run-cred-" + uuid4().hex[:8]


async def _seed_adversarial_credentials(db) -> None:
    """A run whose evidence carries schema'd credential documents under
    adversarial pressure: sentinels under UNEXPECTED nested keys, inside
    an error payload, in the broker receipt itself — and once inside a
    DECLARED field (the redaction backstop's arm)."""
    async with db() as session:
        session.add(
            FlowRun(
                id=CRED_RUN_ID,
                project_id=PROJECT_ID,
                issue_iid=9,
                provider="gitlab",
                status="waiting_harness",
                evidence={
                    "harness": {
                        "dispatch_credential": {
                            "schema": "forge.project.dispatch-credential-proof/2",
                            "subject": "gitlab/gitlab.example/90210",
                            "provider": "anthropic-gateway",
                            "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
                            "env_var": "ANTHROPIC_AUTH_TOKEN",
                            "binding_revision": 2,
                            "bound_at": _iso(T0),
                            "bound_by": "ops@a",
                            "resolved_at": _iso(T0),
                            # A sentinel under an unexpected nested key…
                            "debug_echo": CANARY_VALUE,
                            # …inside an error payload…
                            "error": {"message": f"boom: {CANARY_TOKEN}", "retry": True},
                            # …and in the broker receipt itself.
                            "receipt": {
                                "schema": "forge.credential.broker-receipt/1",
                                "resolver_identity": "env",
                                "provider_route": "anthropic-gateway",
                                "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
                                "env_var": "ANTHROPIC_AUTH_TOKEN",
                                "env_present": True,
                                "resolved_at": _iso(T0),
                                # undeclared key carrying the sentinel…
                                "resolved_value": CANARY_NESTED,
                                # …and a declared field the backstop must
                                # still redact when someone pastes a value
                                # into it (the ghp_ shape is what the
                                # value regex provably catches).
                                "resolved_version": "v-ghp_canary12345678",
                            },
                        },
                        "dispatch_envelope": {
                            "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
                            "credential_resolved_version": "env:ANTHROPIC_AUTH_TOKEN:present",
                        },
                    }
                },
                created_at=_at(3),
            )
        )
        await session.commit()


class TestSchemaAllowlist:
    async def test_schema_documents_serialize_only_declared_fields(self, db):
        await _seed_adversarial_credentials(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        exported = json.loads(trail.to_json())
        proof = next(
            entry["detail"]["evidence"]["harness"]["dispatch_credential"]
            for entry in exported["entries"]
            if entry["run_id"] == CRED_RUN_ID
        )
        assert proof["subject"] == "gitlab/gitlab.example/90210"
        assert proof["binding_revision"] == 2
        assert proof["resolver_identity"] == "unknown"  # absent → honest unknown
        assert set(proof) == {
            "schema",
            "subject",
            "provider",
            "credential_ref",
            "env_var",
            "binding_revision",
            "resolved_at",
            "bound_at",
            "bound_by",
            "resolver_identity",
            "resolved_version",
            "receipt",
        }
        receipt = proof["receipt"]
        assert set(receipt) == {
            "schema",
            "resolver_identity",
            "provider_route",
            "credential_ref",
            "env_var",
            "env_present",
            "resolved_version",
            "resolved_at",
        }

    async def test_every_dropped_field_is_a_named_finding(self, db):
        await _seed_adversarial_credentials(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        document = trail.as_document()
        findings = document["findings"]
        assert findings
        for finding in findings:
            assert finding.startswith(EXPORT_FIELD_DROPPED + ":")
        assert (
            "credential.export_field_dropped:forge.project.dispatch-credential-proof/2:debug_echo"
            in findings
        )
        assert (
            "credential.export_field_dropped:forge.project.dispatch-credential-proof/2:error"
            in findings
        )
        assert (
            "credential.export_field_dropped:forge.credential.broker-receipt/1:resolved_value"
            in (findings)
        )
        # The findings name FIELDS, never content.
        assert CANARY_VALUE not in json.dumps(findings)

    async def test_adversarial_sentinels_never_appear_anywhere(self, db):
        """The layered guard: the allowlist drops the unexpected keys,
        the redaction backstop catches a sentinel pasted into a DECLARED
        field — none of the three canaries survives the export."""
        await _seed_adversarial_credentials(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        exported = trail.to_json()
        assert CANARY_VALUE not in exported
        assert CANARY_TOKEN not in exported
        assert CANARY_NESTED not in exported

    async def test_a_sentinel_in_a_declared_field_is_redacted_not_dropped(self, db):
        await _seed_adversarial_credentials(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        exported = json.loads(trail.to_json())
        proof = next(
            entry["detail"]["evidence"]["harness"]["dispatch_credential"]
            for entry in exported["entries"]
            if entry["run_id"] == CRED_RUN_ID
        )
        # The declared field survives (as a field), its VALUE does not.
        assert proof["receipt"]["resolved_version"] == "[redacted]"
        assert "ghp_canary" not in json.dumps(proof)

    async def test_unresolved_attribution_stays_unknown(self, db):
        """A proof with no resolver/version exports them as ``unknown`` —
        never promoted to a claim, never silently omitted."""
        await _seed_adversarial_credentials(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        exported = json.loads(trail.to_json())
        proof = next(
            entry["detail"]["evidence"]["harness"]["dispatch_credential"]
            for entry in exported["entries"]
            if entry["run_id"] == CRED_RUN_ID
        )
        assert proof["resolver_identity"] == "unknown"
        assert proof["resolved_version"] == "unknown"


class TestCompleteness:
    async def test_every_major_event_type_is_present(self, db):
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        actions = set(trail.actions())
        assert {
            "run.recorded",
            "run.transition",
            "gate.approval_recorded",
            "gate.approval_consumed",
            "control.pause",
            "control.resume",
            "publication.intent",
            "action.post_note",
        } <= actions

    async def test_pause_resume_and_approvals_come_out_in_order(self, db):
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        stamps = [entry.timestamp for entry in trail.entries]
        assert stamps == sorted(stamps)
        actions = trail.actions()
        assert actions.index("control.pause") < actions.index("control.resume")
        assert actions.index("gate.approval_recorded") < actions.index("gate.approval_consumed")

    async def test_the_unknown_effect_is_exported_as_unknown(self, db):
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        by_action = {entry.action: entry for entry in trail.entries}
        assert by_action["publication.intent"].outcome == "unknown"
        assert by_action["action.post_note"].outcome == "unknown_outcome"

    async def test_the_other_projects_rows_never_appear(self, db):
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        assert {entry.run_id for entry in trail.entries} == {RUN_ID}

    async def test_an_empty_project_exports_an_empty_valid_trail(self, db):
        trail = await audit_trail_for_project(OTHER_PROJECT_ID, db)
        assert trail.entries == ()
        document = trail.as_document()
        assert document["entry_count"] == 0
        assert document["entries"] == []


class TestActorsAndOutcomes:
    async def test_every_entry_names_an_actor_action_timestamp_outcome(self, db):
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        assert trail.entries
        for entry in trail.entries:
            assert entry.actor, entry
            assert entry.action, entry
            assert entry.timestamp, entry
            assert entry.outcome, entry
            assert entry.run_id == RUN_ID
            assert entry.source

    async def test_human_surfaces_name_their_humans(self, db):
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        by_action = {entry.action: entry for entry in trail.entries}
        assert by_action["run.recorded"].actor == "alice"
        assert by_action["gate.approval_recorded"].actor == "approver:17"
        assert by_action["control.pause"].actor == "human:reviewer-17"
        assert by_action["run.transition"].actor == SYSTEM_ACTOR

    async def test_transition_entries_carry_from_and_reason(self, db):
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        transitions = [entry for entry in trail.entries if entry.action == "run.transition"]
        assert [entry.outcome for entry in transitions] == ["preflight", "planning"]
        assert transitions[0].detail["from"] == "accepted"


class TestExportFormat:
    async def test_to_json_is_valid_json_with_the_required_fields(self, db):
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        document = json.loads(trail.to_json())
        assert document["schema"] == AUDIT_SCHEMA
        assert document["project_id"] == PROJECT_ID
        assert document["generated_at"]
        assert document["entry_count"] == len(document["entries"])
        for entry in document["entries"]:
            assert {"timestamp", "actor", "action", "outcome", "run_id", "source"} <= set(entry)
            assert isinstance(entry["detail"], dict)

    async def test_no_credential_value_survives_the_export(self, db):
        """The secret-scanning canary: values planted in run evidence
        must come out redacted, keys included."""
        await _seed_lifecycle(db)
        trail = await audit_trail_for_project(PROJECT_ID, db)
        exported = trail.to_json()
        assert CANARY_VALUE not in exported
        assert CANARY_TOKEN not in exported
        recorded = next(e for e in trail.entries if e.action == "run.recorded")
        assert recorded.as_document()["detail"]["evidence"]["api_key"] == "[redacted]"
        assert recorded.as_document()["detail"]["evidence"]["note"] == "[redacted]"
