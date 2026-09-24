"""R37-07 (issue #288): the GitLab dispatch-envelope parity — unit layer.

The GitLab ``ci_harness`` dispatch now carries the SAME lane-resume /
lane-control contract the GitHub lane dispatches (R32-04):

- every pipeline the dispatch triggers carries the ENVELOPE variables
  (``FORGE_LANE_RESUME`` in ``forge.lane_driver.resume_mode``'s exact env
  spelling, the mode word, the pinned checkpoint digest, the attempt
  generation, the decision id, the control URL and the attempt-scoped
  lane-control token) beside the backend's own brief variables;
- the mode is the one the PERSISTED continuation decision selected
  (:mod:`forge.adaptive.continuation` — the three arms map to the three
  modes; UNCERTAIN dispatches nothing);
- the credentials are ATTEMPT-SCOPED (a retry opens a new generation, so
  the re-dispatched token differs and the dead attempt's credential
  retires) and NEVER include a control-plane root secret or a publication
  token;
- a ``required`` resume whose decision pins no checkpoint digest is a
  corrupt dispatch contract — refused before any provider I/O.

These tests run the REAL RunService over the FakeGitLab client (no LLM,
no HTTP); the production-entry trace
(``tests/production_entry/test_gitlab_dispatch_parity.py``) proves the
same contract against the real transport.
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import ActionLog, FlowRun, FlowStatus
from forge.durable.controller import Controller
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.runs import RunService, revival
from forge.runs.service import (
    LANE_ATTEMPT_VARIABLE,
    LANE_CHECKPOINT_VARIABLE,
    LANE_CONTROL_TOKEN_VARIABLE,
    LANE_CONTROL_URL_VARIABLE,
    LANE_DECISION_VARIABLE,
    LANE_RESUME_ENV_SPELLING,
    LANE_RESUME_MODE_REQUIRED,
    LANE_RESUME_MODE_VARIABLE,
    LANE_RESUME_VARIABLE,
)
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.fake_gitlab import FakeGitLab

from forge.adaptive import checkpoint_repository as _cr
from forge.adaptive import continuation

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."

#: The lane-control shared secret the dispatch derives the attempt-scoped
#: token under (a test fixture value, never a real secret).
LANE_SECRET = "gitlab-parity-lane-secret"  # noqa: S105

#: The exact committed checkpoint the fixtures' authority answers with.
CHECKPOINT_DIGEST = "c" * 64


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-root-never-dispatched",  # noqa: S105 — the asserted-absent root
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_IMPLEMENTER_BACKEND="ci_harness:claude-code",
        FORGE_LANE_CONTROL_SECRET=LANE_SECRET,
    )
    values.update(overrides)
    return Settings(**values)


def make_service(db, fake_gitlab, **overrides) -> RunService:
    values = dict(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        writer_class=ChangesetWriter,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )
    values.update(overrides)
    return RunService(**values)


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


@pytest.fixture()
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


@pytest.fixture()
def service(db, fake_gitlab):
    return make_service(db, fake_gitlab)


def exact_checkpoint(digest: str = CHECKPOINT_DIGEST):
    """A monkeypatched authority answering an EXACT committed checkpoint."""

    async def _lookup(run_id, **_):
        return _cr.CheckpointLookupOutcome.exact(digest, authority="test")

    return _lookup


def absent_checkpoint():
    """A monkeypatched authority answering a PROVEN checkpoint absence."""

    async def _lookup(run_id, **_):
        return _cr.CheckpointLookupOutcome.missing("absent", authority="test")

    return _lookup


def variables_of(fake_gitlab: FakeGitLab, index: int = -1) -> dict[str, str]:
    pipeline = fake_gitlab.pipelines[index]
    return {v["key"]: v["value"] for v in pipeline["variables"]}


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def start_and_go(service) -> str:
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    run = await get_run(service._session_factory, run_id)
    assert run.status == FlowStatus.WAITING_HARNESS.value
    return run_id


async def kill_the_attempt(
    db,
    run_id: str,
    *,
    status_reason: str,
    candidates: list[str] | None = None,
) -> None:
    """Park the live attempt the way the reconciler does after a runner loss."""
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        run.status = FlowStatus.BLOCKED.value
        run.status_reason = status_reason
        run.candidate_shas = candidates or []
        await session.commit()


# ----------------------------------------------------------------------
# The envelope on the initial (fresh) dispatch
# ----------------------------------------------------------------------


class TestFreshDispatchEnvelope:
    async def test_go_dispatch_carries_the_full_envelope(self, service, fake_gitlab, db):
        from forge.api_lane_control import lane_control_token

        run_id = await start_and_go(service)
        run = await get_run(db, run_id)

        variables = variables_of(fake_gitlab)
        assert variables["FORGE_RUN_ID"] == run_id
        assert variables[LANE_RESUME_VARIABLE] == ""
        assert variables[LANE_RESUME_MODE_VARIABLE] == "fresh"
        assert variables[LANE_CHECKPOINT_VARIABLE] == ""  # nothing restores
        assert variables[LANE_ATTEMPT_VARIABLE] == str(int(run.cancellation_generation))
        assert variables[LANE_DECISION_VARIABLE] == ""
        assert variables[LANE_CONTROL_URL_VARIABLE] == ""  # no URL configured here
        # NXT-10/NEXT-01: the attempt-scoped HMAC — computed by the
        # dispatch, never stored, exactly what both lane APIs verify.
        assert variables[LANE_CONTROL_TOKEN_VARIABLE] == lane_control_token(
            LANE_SECRET, run_id, generation=int(run.cancellation_generation)
        )
        # ...and NOT the legacy work-only derivation.
        assert variables[LANE_CONTROL_TOKEN_VARIABLE] != lane_control_token(LANE_SECRET, run_id)

        # The journaled envelope: the CONTRACT, never the token value.
        envelope = run.evidence["harness"]["dispatch_envelope"]
        assert envelope["resume_mode"] == "fresh"
        assert envelope["checkpoint_digest"] == ""
        assert envelope["attempt_generation"] == int(run.cancellation_generation)
        assert envelope["token_dispatched"] is True
        assert len(envelope["digest"]) == 64  # gitlab.dispatch_envelope_digest
        assert LANE_CONTROL_TOKEN_VARIABLE in envelope["variable_keys"]
        assert variables[LANE_CONTROL_TOKEN_VARIABLE] not in json.dumps(run.evidence)

    async def test_the_control_url_rides_the_envelope_when_configured(
        self, db, fake_gitlab, monkeypatch
    ):
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", "https://forge.test")
        service = make_service(db, fake_gitlab)
        await start_and_go(service)

        assert variables_of(fake_gitlab)[LANE_CONTROL_URL_VARIABLE] == "https://forge.test"

    async def test_no_secret_leaves_the_token_empty(self, db, fake_gitlab):
        service = make_service(
            db, fake_gitlab, settings=make_settings(FORGE_LANE_CONTROL_SECRET=None)
        )
        await start_and_go(service)

        assert variables_of(fake_gitlab)[LANE_CONTROL_TOKEN_VARIABLE] == ""
        # The brief variables are untouched beside the (empty) envelope.
        assert variables_of(fake_gitlab)["FORGE_RUN_ID"]

    async def test_no_root_secret_ever_enters_the_variable_set(
        self, service, fake_gitlab, db, monkeypatch
    ):
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", "https://forge.test")
        run_id = await start_and_go(service)

        every_value = [v["value"] for p in fake_gitlab.pipelines for v in p["variables"]]
        for secret_value in (LANE_SECRET, "glpat-root-never-dispatched", "whsec"):
            assert secret_value not in every_value
        token = variables_of(fake_gitlab)[LANE_CONTROL_TOKEN_VARIABLE]
        assert token and token != LANE_SECRET
        # The evidence journal carries no credential value either.
        run = await get_run(db, run_id)
        assert token not in json.dumps(run.evidence)


# ----------------------------------------------------------------------
# The three decision arms → the three envelope modes
# ----------------------------------------------------------------------


class TestEnvelopeModes:
    def test_the_continuation_arms_map_onto_the_lane_env_spelling(self):
        """The three decision arms (forge.adaptive.continuation) map onto
        exactly the three env spellings lane_driver's resume_mode() reads."""
        arms = {
            continuation.ContinuationMode.COMMITTED_BASELINE: ("fresh", ""),
            continuation.ContinuationMode.EXACT_WIP: ("required", "1"),
            continuation.ContinuationMode.EXPLICIT_RESTART: ("restart", "restart"),
        }
        for mode, (expected_mode, expected_env) in arms.items():
            decision = continuation.decide_continuation(
                continuation.ContinuationEvidence(checkpoint_committed=True)
                if mode is continuation.ContinuationMode.EXACT_WIP
                else (
                    continuation.ContinuationEvidence(vendor_started=False)
                    if mode is continuation.ContinuationMode.COMMITTED_BASELINE
                    else continuation.ContinuationEvidence(operator_discard_requested=True)
                )
            )
            assert decision.mode is mode
            assert decision.resume_mode() == expected_mode
            assert LANE_RESUME_ENV_SPELLING[decision.resume_mode()] == expected_env
        # UNCERTAIN selects NO dispatch, never a mode the lane would
        # misread as authority.
        uncertain = continuation.decide_continuation(continuation.ContinuationEvidence())
        assert uncertain.uncertain
        with pytest.raises(ValueError):
            uncertain.resume_mode()

    async def test_required_resume_carries_the_exact_checkpoint_reference(
        self, service, fake_gitlab, db, monkeypatch
    ):
        run_id = await start_and_go(service)
        first_token = variables_of(fake_gitlab)[LANE_CONTROL_TOKEN_VARIABLE]
        first_pipeline_id = fake_gitlab.pipelines[0]["id"]
        # The runner dies mid-turn with its checkpoint committed (the pause).
        await kill_the_attempt(
            db, run_id, status_reason="harness_infrastructure: harness job canceled"
        )
        monkeypatch.setattr(revival, "durable_checkpoint_outcome", exact_checkpoint())

        await service.handle_retry_note(
            PROJECT_ID, f"@forge /retry {run_id}", "alice", ISSUE_IID, delivery_id="d-1"
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        variables = variables_of(fake_gitlab)  # the SECOND pipeline
        assert fake_gitlab.pipelines[0]["id"] == first_pipeline_id  # untouched
        assert variables[LANE_RESUME_VARIABLE] == "1"
        assert variables[LANE_RESUME_MODE_VARIABLE] == LANE_RESUME_MODE_REQUIRED
        assert variables[LANE_CHECKPOINT_VARIABLE] == CHECKPOINT_DIGEST
        # NEXT-01: the retry opened a NEW attempt — the token is scoped to
        # it and the dead attempt's credential is different.
        assert variables[LANE_ATTEMPT_VARIABLE] == "1"
        assert variables[LANE_CONTROL_TOKEN_VARIABLE] != first_token
        # The persisted decision and the journaled envelope agree on the
        # exact pin; the ack NAMES the continuation source.
        document = (run.evidence or {}).get("continuation")
        assert document["mode"] == "required"
        assert document["checkpoint_digest"] == CHECKPOINT_DIGEST
        assert document["continuation_decision_id"]
        assert variables[LANE_DECISION_VARIABLE] == document["continuation_decision_id"]
        envelope = run.evidence["harness"]["dispatch_envelope"]
        assert envelope["resume_mode"] == "required"
        assert envelope["checkpoint_digest"] == CHECKPOINT_DIGEST
        assert envelope["attempt_generation"] == 1
        assert fake_gitlab.notes_containing("exact WIP checkpoint")

    async def test_restart_verb_discards_the_wip(self, service, fake_gitlab, db, monkeypatch):
        run_id = await start_and_go(service)
        await kill_the_attempt(
            db, run_id, status_reason="harness_infrastructure: harness job canceled"
        )
        monkeypatch.setattr(revival, "durable_checkpoint_outcome", exact_checkpoint())

        await service.handle_retry_note(
            PROJECT_ID, f"@forge /retry {run_id} restart", "alice", ISSUE_IID, delivery_id="d-2"
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value
        variables = variables_of(fake_gitlab)
        assert variables[LANE_RESUME_VARIABLE] == "restart"
        assert variables[LANE_RESUME_MODE_VARIABLE] == "restart"
        # The decision's recorded reference rides for lineage (exactly like
        # the GitHub envelope); the restart contract itself downloads
        # NOTHING — lane_driver's restart arm never consults it.
        assert variables[LANE_CHECKPOINT_VARIABLE] == CHECKPOINT_DIGEST
        document = (await get_run(db, run_id)).evidence["continuation"]
        assert document["mode"] == "restart"

    async def test_a_proven_no_wip_death_retries_fresh(self, service, fake_gitlab, db, monkeypatch):
        run_id = await start_and_go(service)
        # A bootstrap death PROVES no vendor session ever existed.
        await kill_the_attempt(
            db,
            run_id,
            status_reason=(
                "harness_infrastructure: harness_bootstrap_failed (uv sync --frozen died)"
            ),
        )
        monkeypatch.setattr(revival, "durable_checkpoint_outcome", absent_checkpoint())

        await service.handle_retry_note(
            PROJECT_ID, f"@forge /retry {run_id}", "alice", ISSUE_IID, delivery_id="d-3"
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value
        variables = variables_of(fake_gitlab)
        assert variables[LANE_RESUME_VARIABLE] == ""
        assert variables[LANE_RESUME_MODE_VARIABLE] == "fresh"
        assert variables[LANE_CHECKPOINT_VARIABLE] == ""

    async def test_an_uncertain_death_dispatches_nothing(self, service, fake_gitlab, db):
        run_id = await start_and_go(service)
        pipelines_before = len(fake_gitlab.pipelines)
        # A plain dispatch failure with no proof either way and no
        # checkpoint: absence of a checkpoint never proves no WIP.
        await kill_the_attempt(db, run_id, status_reason="harness_start_failed: 502 boom")

        await service.handle_retry_note(
            PROJECT_ID, f"@forge /retry {run_id}", "alice", ISSUE_IID, delivery_id="d-4"
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value  # untouched — no attempt opened
        assert len(fake_gitlab.pipelines) == pipelines_before  # ZERO dispatches
        assert run.commit_cycle == 1  # no operator cycle granted
        assert int(run.cancellation_generation) == 0  # no new attempt minted
        assert fake_gitlab.notes_containing("retry needs an operator decision")


# ----------------------------------------------------------------------
# Idempotency, corrupt contracts, and the revival re-drive
# ----------------------------------------------------------------------


class TestRetryDiscipline:
    async def test_a_redelivered_retry_is_one_logical_command(
        self, service, fake_gitlab, db, monkeypatch
    ):
        run_id = await start_and_go(service)
        await kill_the_attempt(
            db, run_id, status_reason="harness_infrastructure: harness job canceled"
        )
        monkeypatch.setattr(revival, "durable_checkpoint_outcome", exact_checkpoint())

        for _ in range(2):
            await service.handle_retry_note(
                PROJECT_ID, f"@forge /retry {run_id}", "alice", ISSUE_IID, delivery_id="same"
            )

        # ONE re-dispatch, ONE ack, ONE cycle bump — the second delivery is
        # a no-op before any decision or dispatch runs.
        assert len(fake_gitlab.pipelines) == 2
        assert len(fake_gitlab.notes_containing("retried by @alice")) == 1
        assert (await get_run(db, run_id)).commit_cycle == 2
        async with db() as session:
            attempts = (
                (
                    await session.execute(
                        select(ActionLog).where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "retry_requested",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(attempts) == 1

    async def test_a_required_resume_without_a_pinned_digest_refuses_to_dispatch(
        self, service, fake_gitlab, db, monkeypatch
    ):
        """The dispatch-side half of zero-model-turns: a required mode whose
        persisted decision pins NO checkpoint digest is a corrupt contract —
        blocked before any provider I/O (no branch write, no pipeline)."""
        run_id = await start_and_go(service)
        await kill_the_attempt(
            db, run_id, status_reason="harness_infrastructure: harness job canceled"
        )

        # A typed EXACT outcome whose checkpoint carries NO usable
        # content address (a rotted/corrupt manifest identity): the
        # decision approves a resume the dispatch cannot pin.
        async def _addressless(run_id, **_):
            return _cr.CheckpointLookupOutcome.exact("", authority="test")

        monkeypatch.setattr(revival, "durable_checkpoint_outcome", _addressless)

        await service.handle_retry_note(
            PROJECT_ID, f"@forge /retry {run_id}", "alice", ISSUE_IID, delivery_id="d-5"
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "continuation_ref_missing" in (run.status_reason or "")
        assert len(fake_gitlab.pipelines) == 1  # the initial dispatch only

    async def test_the_revival_redrive_carries_the_decisions_mode(
        self, service, fake_gitlab, db, monkeypatch
    ):
        run_id = await start_and_go(service)
        await kill_the_attempt(
            db, run_id, status_reason="harness_infrastructure: harness job canceled"
        )
        monkeypatch.setattr(revival, "durable_checkpoint_outcome", exact_checkpoint())
        async with db() as session:
            controller = Controller(session)
            await controller.revive_transition(
                run_id, reason="test revival", authorized_by="operator:test"
            )
            await session.commit()

        await service._redispatch_revival(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        variables = variables_of(fake_gitlab)
        assert variables[LANE_RESUME_VARIABLE] == "1"
        assert variables[LANE_RESUME_MODE_VARIABLE] == "required"
        assert variables[LANE_CHECKPOINT_VARIABLE] == CHECKPOINT_DIGEST
        assert variables[LANE_ATTEMPT_VARIABLE] == "1"


# ----------------------------------------------------------------------
# Callbacks bound to the CURRENT attempt's pipeline
# ----------------------------------------------------------------------


class TestCallbackBinding:
    async def test_a_delayed_old_pipeline_completion_never_becomes_the_wip_source(
        self, service, fake_gitlab, db, monkeypatch
    ):
        from tests.fixtures.candidate import create_diff, seed_candidate

        run_id = await start_and_go(service)
        first_pipeline = fake_gitlab.pipelines[0]["id"]
        await kill_the_attempt(
            db, run_id, status_reason="harness_infrastructure: harness job canceled"
        )
        monkeypatch.setattr(revival, "durable_checkpoint_outcome", exact_checkpoint())
        await service.handle_retry_note(
            PROJECT_ID, f"@forge /retry {run_id}", "alice", ISSUE_IID, delivery_id="d-6"
        )

        # The durable handle (what the reconciler polls) is bound to the
        # NEW attempt's pipeline — the occupancy correlation from #241 keys
        # the resume: an old pipeline id cannot become the WIP source.
        run = await get_run(db, run_id)
        second_pipeline = fake_gitlab.pipelines[1]["id"]
        assert run.evidence["harness"]["pipeline_id"] == second_pipeline
        assert second_pipeline != first_pipeline

        # The OLD job now completes late with a well-formed candidate of
        # its own — the strongest form of the attack.
        old_job_id = 901
        fake_gitlab.set_pipeline_jobs(
            first_pipeline, [{"id": old_job_id, "name": "forge-agent", "status": "success"}]
        )
        seed_candidate(
            fake_gitlab,
            old_job_id,
            attempt_base="base-sha-1",
            diff=create_diff("forge-demo/stale.md", "stale attempt content\n"),
        )
        # The new attempt's job is still RUNNING.
        fake_gitlab.set_pipeline_jobs(
            second_pipeline, [{"id": 902, "name": "forge-agent", "status": "running"}]
        )

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value  # still on the new attempt
        assert run.candidate_shas in (None, [])  # the stale candidate never landed
        assert fake_gitlab.merge_requests == {}
