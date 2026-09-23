"""Q35-07: the composition types adopted at the GitHub dispatch entry.

The production adoption of ADR-0029's boundary values — pinned at both
levels the issue demands:

- the SEAM (``forge.adaptive.composition_adoption``): repository-context
  construction (connection vs numeric repository identity, no delimiter
  collisions), the coordinate-axis separation guard (a checkpoint
  sequence is never an attempt identity), envelope composition refusals
  naming the exact field, the pre-A18 legacy profile adapter, and the
  redispatch digest-equality verification;
- the PRODUCTION CALLER (``GitHubRunService._advance_harness`` over the
  fake client): the /go dispatch persists the composed ``attempt_start``
  evidence beside a real workflow_dispatch, a missing source field
  refuses with ZERO native calls, settings mutated between dispatches
  never move the envelope, a legacy run (no persisted envelope) logs the
  ``composition.legacy_attempts`` marker, and a /retry carries the
  persisted continuation decision's mode.
"""

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive import composition_adoption
from forge.adaptive.composition_adoption import (
    assert_axis_separation,
    compose_attempt_start,
    github_repository_context,
)
from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, FlowStatus
from forge.integrations.github_flow import GitHubAgents
from forge.runs.composition import CompositionBoundaryError
from forge.runs.github_service import GitHubRunService
from forge.runs.stubs import StubImplementer, StubPlanner
from tests.fixtures.fake_github import FakeGitHub

FIXTURE_REPO = "acme/acme-widget"
PROJECT_ID = 70010
ISSUE = 42
ISSUE_TITLE = "Add password reset"
ISSUE_DESC = "Users cannot reset their password."
BASE_HEAD = "1" * 40  # a full SHA that happens to be all digits
HARNESS_WORKFLOW = "forge-harness.github.yml"
HARNESS_MODEL = "glm-5.3-flash[1m]"
HEX64 = "ab" * 32


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_GITHUB_HARNESS_WORKFLOW=HARNESS_WORKFLOW,
        FORGE_HARNESS_MODEL=HARNESS_MODEL,
        FORGE_VERIFICATION_GRACE_SECONDS=0,
    )
    values.update(overrides)
    return Settings(**values)


def make_stack(fake: FakeGitHub) -> GitHubAgents:
    from forge.integrations.github_flow import GitHubPublishFlow

    implementer = StubImplementer()
    return GitHubAgents(
        client=fake,
        reader=fake,
        planner=StubPlanner(),
        implementer=implementer,
        reviewer=_StubReviewer(),
        flow=GitHubPublishFlow(fake, proposer=implementer, base_branch="main"),
    )


class _StubReviewer:
    async def review(self, **kwargs):
        from forge.factory.reviewer import ReviewVerdict

        return ReviewVerdict(verdict="ok", summary="clean", findings=())


def make_service(
    db,
    fake: FakeGitHub,
    *,
    settings: Settings | None = None,
    repo: str = FIXTURE_REPO,
) -> GitHubRunService:
    return GitHubRunService(
        db,
        settings or make_settings(),
        ForgeConfig(),
        stack=make_stack(fake),
        repo_full_name=repo,
    )


@pytest.fixture()
async def db():
    from forge.models.base import Base

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
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(FIXTURE_REPO, {"src/app.py": "print('hi')\n"})
    github.heads[FIXTURE_REPO]["main"] = BASE_HEAD
    github.seed_issue(FIXTURE_REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
    return github


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def start(service: GitHubRunService, author: str = "alice") -> str:
    return await service.start_run(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        issue_title=ISSUE_TITLE,
        issue_description=ISSUE_DESC,
        author_username=author,
    )


async def go(service: GitHubRunService, run_id: str) -> None:
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=f"/go {run_id}",
        author_username="alice",
        now=datetime.now(timezone.utc),
    )


def a_composition(**overrides):
    """The seam's full-arguments call, with one knob per test overridden."""
    kwargs = dict(
        run_id="run-1",
        repo_full_name=FIXTURE_REPO,
        project_id=PROJECT_ID,
        attempt_oid=BASE_HEAD,
        authority_epoch=2,
        profile_digest=HEX64,
        fallback_profile_digest="cd" * 32,
        resume_mode="fresh",
        lease_id="lease-9",
        prior_document=None,
    )
    kwargs.update(overrides)
    return compose_attempt_start(**kwargs)


# -- the repository context ----------------------------------------------------


class TestGitHubRepositoryContext:
    def test_constructs_the_documented_gateway_convention(self):
        context = github_repository_context(FIXTURE_REPO, PROJECT_ID)
        assert context.provider_family == "github"
        assert context.connection_id == "github:acme/acme-widget"
        assert context.repository_id == "70010"
        assert context.subject_key() == "github:github:acme/acme-widget:70010"
        assert len(context.digest()) == 64

    def test_equivalent_contexts_serialize_identically(self):
        one = github_repository_context(FIXTURE_REPO, PROJECT_ID)
        two = github_repository_context(FIXTURE_REPO, PROJECT_ID)
        assert one.digest() == two.digest()
        assert one.to_document() == two.to_document()

    def test_the_same_numeric_repo_id_on_two_connections_never_collides(self):
        """The negative collision test from the issue: two connections
        (two bound repositories) holding the SAME numeric repository id
        must compose distinct, non-interchangeable contexts — structured
        comparison, never a delimiter concatenation of loose parts."""
        ours = github_repository_context(FIXTURE_REPO, PROJECT_ID)
        rival = github_repository_context("rival/acme-widget", PROJECT_ID)
        assert ours.repository_id == rival.repository_id
        assert ours.subject_key() != rival.subject_key()
        assert ours.digest() != rival.digest()
        ours_envelope = a_composition().spec.envelope_digest()
        rival_envelope = a_composition(repo_full_name="rival/acme-widget").spec.envelope_digest()
        assert ours_envelope != rival_envelope

    @pytest.mark.parametrize("malformed", ["", "acme", "/widget", "acme/", "   "])
    def test_a_repo_without_owner_and_name_refuses_naming_the_field(self, malformed):
        with pytest.raises(CompositionBoundaryError, match="connection_id"):
            github_repository_context(malformed, PROJECT_ID)

    @pytest.mark.parametrize("bad_id", [0, -1, None, True])
    def test_a_missing_numeric_repository_identity_refuses(self, bad_id):
        with pytest.raises(CompositionBoundaryError, match="repository_id"):
            github_repository_context(FIXTURE_REPO, bad_id)


# -- the coordinate-axis separation guard ---------------------------------------


class TestAssertAxisSeparation:
    def test_an_oid_and_an_epoch_pass(self):
        assert assert_axis_separation(attempt_oid=BASE_HEAD, authority_epoch=3) is None

    def test_a_full_all_digit_sha_is_not_mistaken_for_a_sequence(self):
        """A SHA that happens to be all digits is still an OID (40/64 hex
        chars) — the guard refuses the SHORT counter shape, never a real
        attempt base."""
        assert assert_axis_separation(attempt_oid="f" * 40, authority_epoch=0) is None
        assert assert_axis_separation(attempt_oid="9" * 64, authority_epoch=1) is None

    @pytest.mark.parametrize("sequence", ["0", "7", "42", "0013", "12345678901234"])
    def test_a_checkpoint_sequence_on_the_attempt_axis_is_rejected(self, sequence):
        with pytest.raises(CompositionBoundaryError, match="checkpoint sequence"):
            assert_axis_separation(attempt_oid=sequence, authority_epoch=0)

    @pytest.mark.parametrize("bad_epoch", [True, "3", 2.0, -1])
    def test_a_malformed_authority_epoch_is_rejected(self, bad_epoch):
        with pytest.raises(CompositionBoundaryError, match="authority axis"):
            assert_axis_separation(attempt_oid=BASE_HEAD, authority_epoch=bad_epoch)


# -- the envelope composition ---------------------------------------------------


class TestComposeAttemptStart:
    def test_happy_path_composes_the_envelope_and_the_document(self):
        composed = a_composition()
        spec = composed.spec
        assert spec.run_id == "run-1"
        assert spec.attempt_id == BASE_HEAD
        assert spec.repository is composed.context
        assert spec.profile_digest == HEX64
        assert spec.resume_mode == "fresh"
        assert spec.lease_id == "lease-9"
        document = composed.document
        assert document["version"] == composition_adoption.ATTEMPT_START_VERSION
        assert document["envelope_digest"] == spec.envelope_digest()
        assert document["context_digest"] == composed.context.digest()
        assert document["subject_key"] == "github:github:acme/acme-widget:70010"
        assert document["attempt_base"] == BASE_HEAD
        assert document["authority_epoch"] == 2
        assert document["profile_source"] == "execution_profile"
        assert document["legacy"] is True  # no prior document → legacy shape
        assert composed.legacy is True

    @pytest.mark.parametrize(
        ("knob", "value", "field"),
        [
            ("run_id", "", "run_id"),
            ("attempt_oid", "", "attempt_id"),
            ("lease_id", "", "lease_id"),
            ("resume_mode", "urgent", "resume_mode"),
        ],
    )
    def test_a_missing_or_invalid_field_refuses_naming_it(self, knob, value, field):
        with pytest.raises(CompositionBoundaryError, match=field):
            a_composition(**{knob: value})

    def test_both_profile_digests_missing_refuses_naming_the_field(self):
        """An empty A18 digest falls back to the frozen spec digest (the
        legacy adapter) — only when BOTH are missing is the profile axis
        refused, naming it."""
        with pytest.raises(CompositionBoundaryError, match="profile_digest"):
            a_composition(profile_digest="", fallback_profile_digest="")

    def test_a_pre_a18_spec_uses_the_explicit_legacy_profile_adapter(self):
        """A spec frozen before A18 carries no execution-profile section;
        the envelope pins the run's frozen spec digest instead — the
        observable legacy adapter, never silent."""
        composed = a_composition(profile_digest="", fallback_profile_digest="ef" * 32)
        assert composed.spec.profile_digest == "ef" * 32
        assert composed.document["profile_source"] == "spec_digest"

    def test_the_persisted_continuation_mode_rides_the_envelope_unchanged(self):
        composed = a_composition(resume_mode="required")
        assert composed.spec.resume_mode == "required"
        assert composed.document["resume_mode"] == "required"

    def test_a_redispatch_over_the_same_facts_verifies_digest_equality(self):
        first = a_composition()
        second = a_composition(prior_document=first.document)
        assert second.unchanged is True
        assert second.legacy is False
        assert second.document["legacy"] is False
        assert "supersedes" not in second.document

    def test_a_legitimately_new_attempt_records_what_it_supersedes(self):
        first = a_composition()
        retry = a_composition(attempt_oid="e" * 40, prior_document=first.document)
        assert retry.unchanged is False
        assert retry.document["supersedes"] == first.document["envelope_digest"]

    def test_a_corrupt_prior_document_is_recomposed_never_trusted(self):
        composed = a_composition(prior_document={"envelope_digest": "not-a-digest"})
        assert composed.legacy is False  # a prior envelope EXISTS…
        assert composed.unchanged is False  # …and does not match — a new one

    def test_a_checkpoint_sequence_as_the_attempt_identity_is_rejected(self):
        with pytest.raises(CompositionBoundaryError, match="checkpoint sequence"):
            a_composition(attempt_oid="7")


# -- the production caller ------------------------------------------------------


class TestProductionDispatchAdoption:
    """The dispatch entry itself, over the fake client — not a constructor
    unit test: every test below crosses the full /implement → /go →
    workflow_dispatch path with the composition boundary in place."""

    async def test_the_go_dispatch_persists_the_composed_envelope(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["run_id"] == run_id
        run = await get_run(db, run_id)
        document = (run.evidence or {})[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        assert document["subject_key"] == "github:github:acme/acme-widget:70010"
        assert len(document["envelope_digest"]) == 64
        assert len(document["context_digest"]) == 64
        assert document["resume_mode"] == "fresh"
        assert document["attempt_base"] == BASE_HEAD
        assert isinstance(document["authority_epoch"], int)
        assert document["profile_source"] == "execution_profile"

    async def test_a_missing_source_field_refuses_dispatch_with_zero_native_calls(
        self, db, fake, caplog
    ):
        """The issue's acceptance: a missing source field prevents dispatch
        construction and produces zero native calls. The gate has already
        bound the approved base, so the missing identity is injected AFTER
        approval (a stranding redispatch whose attempt base was wiped) —
        the composition boundary is the only check left standing."""
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # dispatch 1: the envelope persisted
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.base_sha = ""  # the attempt axis loses its source identity
            await session.commit()
        fake.dispatch_inputs.clear()

        with caplog.at_level(logging.WARNING, logger="forge.runs.github_service"):
            await service._advance_harness(run_id, project_id=PROJECT_ID, issue_number=ISSUE)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "composition_boundary" in str(run.status_reason)
        assert "attempt_id" in str(run.status_reason)  # the field is named
        assert fake.dispatch_inputs == []  # zero workflow_dispatch calls
        # The refused dispatch overwrote nothing: the persisted envelope is
        # still the authorized one from dispatch 1 (audit trail intact).
        document = run.evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        assert document["attempt_base"] == BASE_HEAD
        assert any("composition.refusal_by_field" in m for m in caplog.messages)

    async def test_a_sequence_shaped_attempt_base_is_refused_at_dispatch(self, db, fake, caplog):
        """The axis-separation guard at the production caller: a poisoned
        candidate row shaped like a checkpoint sequence (a bare counter
        where the attempt base OID belongs) refuses the redispatch before
        any provider call."""
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.commit_cycle = 2  # the repair shape: attempt base = last candidate
            run.candidate_shas = ["7"]  # a checkpoint sequence, not an OID
            await session.commit()
        fake.dispatch_inputs.clear()

        with caplog.at_level(logging.WARNING, logger="forge.runs.github_service"):
            await service._advance_harness(run_id, project_id=PROJECT_ID, issue_number=ISSUE)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "checkpoint sequence" in str(run.status_reason)
        assert fake.dispatch_inputs == []
        assert any("composition.refusal_by_field" in m for m in caplog.messages)

    async def test_the_envelope_survives_settings_mutation_between_dispatches(self, db, fake):
        """Restart semantics (the issue's negative test): the process
        restarts between input resolution and dispatch with MODIFIED
        settings — the redispatched envelope must carry the ORIGINAL
        approved values, because it composes from the frozen spec, not
        from live defaults."""
        settings = make_settings()
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        await go(service, run_id)
        first = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )
        (first_dispatch,) = fake.dispatch_inputs

        # The "restart": live defaults drift after approval.
        settings.FORGE_HARNESS_MODEL = "post-gate-model"
        settings.FORGE_GITHUB_HARNESS_WORKFLOW = "some-other-workflow.yml"
        fake.dispatch_inputs.clear()

        await service._advance_harness(run_id, project_id=PROJECT_ID, issue_number=ISSUE)

        second = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )
        (second_dispatch,) = fake.dispatch_inputs
        assert second["envelope_digest"] == first["envelope_digest"]
        assert second["context_digest"] == first["context_digest"]
        assert second_dispatch["inputs"]["model"] == first_dispatch["inputs"]["model"]
        assert second_dispatch["workflow"] == first_dispatch["workflow"]

    async def test_a_run_without_a_persisted_envelope_logs_the_legacy_marker(
        self, db, fake, caplog
    ):
        """Compat, observable and never silent: a run whose evidence
        carries no ``attempt_start`` key (every run persisted before
        Q35-07) dispatches through the explicit on-the-fly construction
        with the ``composition.legacy_attempts`` marker."""
        service = make_service(db, fake)
        run_id = await start(service)

        with caplog.at_level(logging.INFO, logger="forge.runs.github_service"):
            await go(service, run_id)

        assert any("composition.legacy_attempts" in m for m in caplog.messages)
        first = (await get_run(db, run_id)).evidence[
            composition_adoption.ATTEMPT_START_EVIDENCE_KEY
        ]
        assert first["legacy"] is True

        # The second dispatch is no longer legacy: the envelope persisted
        # by the first one is the comparison baseline now.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="forge.runs.github_service"):
            await service._advance_harness(run_id, project_id=PROJECT_ID, issue_number=ISSUE)
        assert not any("composition.legacy_attempts" in m for m in caplog.messages)
        second = (await get_run(db, run_id)).evidence[
            composition_adoption.ATTEMPT_START_EVIDENCE_KEY
        ]
        assert second["legacy"] is False
        assert second["envelope_digest"] == first["envelope_digest"]

    async def test_a_retry_envelope_carries_the_continuation_decision(self, db, fake, monkeypatch):
        """The /retry redispatch composes its envelope from the PERSISTED
        continuation decision (Q35-02): a committed checkpoint selects the
        ``required`` contract, and the envelope + evidence agree with it."""
        from forge.runs import revival

        monkeypatch.setattr(revival, "_has_durable_checkpoint", lambda run_id: True)
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = "harness_start_failed: boom"
            run.candidate_shas = ["c1"]
            await session.commit()
        fake.dispatch_inputs.clear()

        await service.handle_retry(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/retry {run_id}",
            author_username="alice",
            delivery_id="retry-composition-1",
        )

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"
        run = await get_run(db, run_id)
        document = run.evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        decision = run.evidence["continuation"]
        assert document["resume_mode"] == decision["mode_selected"] == "required"
        # The authority axis rides beside the envelope and stays separate
        # from the attempt identity (the retry opened a new generation).
        assert document["authority_epoch"] >= 1
        assert document["attempt_base"] != "7"  # an OID, never a sequence
