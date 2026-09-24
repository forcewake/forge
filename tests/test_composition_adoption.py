"""Q35-07 + R36-06: the composition types adopted at the GitHub dispatch entry.

The production adoption of ADR-0029's boundary values — pinned at both
levels the issue demands:

- the SEAM (``forge.adaptive.composition_adoption``): repository-context
  construction (connection vs numeric repository identity, no delimiter
  collisions), the coordinate-axis separation guard (a checkpoint
  sequence or command watermark is never an execution identity or a
  source OID), envelope composition refusals naming the exact field, the
  pre-A18 legacy profile adapter, the redispatch digest-equality
  verification, the derived EXECUTION identity (R36-06: same source
  commit, distinct executions) and the v1-envelope compat read;
- the PRODUCTION CALLER (``GitHubRunService._advance_harness`` over the
  fake client): the /go dispatch persists the composed ``attempt_start``
  evidence beside a real workflow_dispatch, a missing source field
  refuses with ZERO native calls, settings mutated between dispatches
  never move the envelope, a legacy run (no persisted envelope) logs the
  ``composition.legacy_attempts`` marker, a /retry carries the
  persisted continuation decision's mode AND its pinned checkpoint, and
  two attempts from the same commit hold distinct execution identities
  while a re-drive of one attempt reconstructs the identical envelope.
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
    derive_execution_attempt_id,
    github_repository_context,
    legacy_attempt_start_view,
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
        attempt_ordinal=2,
        profile_digest=HEX64,
        fallback_profile_digest="cd" * 32,
        resume_mode="fresh",
        lease_id="lease-9",
        continuation_ref_digest="",
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
    def test_an_oid_an_epoch_and_a_derived_identity_pass(self):
        identity = derive_execution_attempt_id(
            run_id="run-1", attempt_ordinal=0, source_base_oid=BASE_HEAD
        )
        assert (
            assert_axis_separation(
                attempt_oid=BASE_HEAD, authority_epoch=3, execution_attempt_id=identity
            )
            is None
        )

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

    @pytest.mark.parametrize("sequence", ["0", "7", "42", "0013", "12345678901234"])
    def test_a_checkpoint_sequence_or_command_watermark_as_the_execution_identity_is_rejected(
        self, sequence
    ):
        """R36-06: a counter is never laundered into an execution
        identity — the identity axis carries only the derived durable
        id."""
        with pytest.raises(CompositionBoundaryError, match="identity axis"):
            assert_axis_separation(
                attempt_oid=BASE_HEAD, authority_epoch=0, execution_attempt_id=sequence
            )

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_an_empty_execution_identity_is_rejected(self, bad):
        with pytest.raises(CompositionBoundaryError, match="identity axis"):
            assert_axis_separation(
                attempt_oid=BASE_HEAD, authority_epoch=0, execution_attempt_id=bad
            )

    def test_the_source_oid_is_never_the_execution_identity(self):
        shared = "e" * 64
        with pytest.raises(CompositionBoundaryError, match="never a code revision"):
            assert_axis_separation(
                attempt_oid=shared, authority_epoch=0, execution_attempt_id=shared
            )

    def test_a_malformed_execution_identity_shape_is_rejected(self):
        with pytest.raises(CompositionBoundaryError, match="hex64"):
            assert_axis_separation(
                attempt_oid=BASE_HEAD, authority_epoch=0, execution_attempt_id="e" * 40
            )

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
        assert spec.execution_attempt_id == derive_execution_attempt_id(
            run_id="run-1", attempt_ordinal=2, source_base_oid=BASE_HEAD
        )
        assert spec.source_base_oid == BASE_HEAD
        assert spec.execution_attempt_id != BASE_HEAD  # identity ≠ revision (R36-06)
        assert spec.authority_epoch == 2
        assert spec.repository is composed.context
        assert spec.profile_digest == HEX64
        assert spec.resume_mode == "fresh"
        assert spec.lease_id == "lease-9"
        document = composed.document
        assert document["version"] == composition_adoption.ATTEMPT_START_VERSION == 2
        assert document["envelope_digest"] == spec.envelope_digest()
        assert document["context_digest"] == composed.context.digest()
        assert document["subject_key"] == "github:github:acme/acme-widget:70010"
        assert document["attempt_base"] == document["source_base_oid"] == BASE_HEAD
        assert document["execution_attempt_id"] == spec.execution_attempt_id
        assert document["attempt_ordinal"] == 2
        assert document["authority_epoch"] == 2
        assert document["continuation_ref_digest"] == ""
        assert document["profile_source"] == "execution_profile"
        assert document["legacy"] is True  # no prior document → legacy shape
        assert composed.legacy is True

    @pytest.mark.parametrize(
        ("knob", "value", "field"),
        [
            ("run_id", "", "run_id"),
            ("attempt_oid", "", "source_base_oid"),
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
        pinned = "cd" * 32
        composed = a_composition(resume_mode="required", continuation_ref_digest=pinned)
        assert composed.spec.resume_mode == "required"
        assert composed.spec.continuation_ref_digest == pinned
        assert composed.document["resume_mode"] == "required"
        assert composed.document["continuation_ref_digest"] == pinned

    def test_a_required_mode_without_a_pinned_continuation_refuses(self):
        with pytest.raises(CompositionBoundaryError, match="continuation_ref_digest"):
            a_composition(resume_mode="required")

    def test_two_attempts_from_the_same_source_commit_hold_distinct_identities(self):
        """AT-07 core (seam level): the same source OID, two durable
        attempt ordinals — different execution ids, same source base, and
        neither id equals the source. The later attempt supersedes the
        earlier one's envelope."""
        first = a_composition(attempt_ordinal=1)
        retry = a_composition(attempt_ordinal=2, prior_document=first.document)
        assert first.spec.source_base_oid == retry.spec.source_base_oid == BASE_HEAD
        assert first.spec.execution_attempt_id != retry.spec.execution_attempt_id
        assert retry.unchanged is False
        assert retry.document["supersedes"] == first.document["envelope_digest"]

    def test_a_repeat_delivery_reconstructs_identical_authority_fields(self):
        """R36-06 acceptance: the same durable rows — what a restart
        between envelope construction and the native start re-reads —
        reconstruct the identical envelope, digest and identity."""
        first = a_composition(prior_document=None)
        again = a_composition(prior_document=first.document)
        assert again.unchanged is True
        assert again.legacy is False
        assert again.document["legacy"] is False
        assert "supersedes" not in again.document
        assert again.spec == first.spec
        assert again.document["envelope_digest"] == first.document["envelope_digest"]
        assert again.spec.execution_attempt_id == first.spec.execution_attempt_id
        assert again.spec.authority_epoch == first.spec.authority_epoch
        assert again.spec.continuation_ref_digest == first.spec.continuation_ref_digest
        assert again.spec.lease_id == first.spec.lease_id

    def test_a_changed_authority_epoch_changes_the_authority_digest(self):
        """The epoch is INSIDE the v2 envelope digest: source, profile,
        lease and ordinal unchanged, the digest still moves."""
        low = a_composition(authority_epoch=1, attempt_ordinal=1)
        high = a_composition(authority_epoch=2, attempt_ordinal=1)
        assert low.spec.source_base_oid == high.spec.source_base_oid
        assert low.spec.profile_digest == high.spec.profile_digest
        assert low.spec.lease_id == high.spec.lease_id
        assert low.document["envelope_digest"] != high.document["envelope_digest"]

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


# -- the v1 compat adapter ------------------------------------------------------


class TestLegacyAttemptStartView:
    def the_v1_document(self) -> dict:
        """A persisted pre-R36-06 ``attempt_start`` document (v1: the
        attempt axis WAS the source OID, the epoch rode beside)."""
        return {
            "version": 1,
            "envelope_digest": "e" * 64,
            "context_digest": "d" * 64,
            "subject_key": "github:github:acme/acme-widget:70010",
            "resume_mode": "fresh",
            "attempt_base": BASE_HEAD,
            "authority_epoch": 0,
            "profile_source": "execution_profile",
            "legacy": False,
        }

    def test_a_v1_document_reads_as_the_weaker_identity(self):
        view = legacy_attempt_start_view(self.the_v1_document())
        assert view == {
            "envelope_version": 1,
            "execution_attempt_id": None,  # never manufactured
            "source_base_oid": BASE_HEAD,
            "authority_epoch": 0,
            "identity_strength": "source_oid_only",
        }

    def test_a_v2_document_is_not_legacy(self):
        composed = a_composition()
        assert legacy_attempt_start_view(composed.document) is None
        assert composed.prior_identity is None

    def test_a_missing_document_is_not_legacy(self):
        assert legacy_attempt_start_view(None) is None

    def test_a_dispatch_over_a_v1_prior_records_the_legacy_read_and_refuses_unchanged(self):
        """The compat policy at the seam: a v1 prior is superseded by the
        v2 envelope (no authority claim over the weaker identity), the
        legacy read is recorded on the document, and ``unchanged`` is
        refused even though the source/profile/lease match."""
        v1 = self.the_v1_document()
        composed = a_composition(prior_document=v1)
        assert composed.unchanged is False
        assert composed.prior_identity == legacy_attempt_start_view(v1)
        assert composed.document["legacy_read"]["execution_attempt_id"] is None
        assert composed.document["legacy_read"]["identity_strength"] == "source_oid_only"
        assert composed.document["supersedes"] == v1["envelope_digest"]
        assert composed.legacy is False  # the prior envelope exists — it is v1, not absent


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
        assert document["attempt_base"] == document["source_base_oid"] == BASE_HEAD
        assert document["version"] == 2
        # R36-06: the persisted envelope carries the separated identity
        # axes — a derived execution id distinct from the source OID.
        assert document["execution_attempt_id"] != document["source_base_oid"]
        assert len(document["execution_attempt_id"]) == 64
        assert document["execution_attempt_id"] == derive_execution_attempt_id(
            run_id=run_id, attempt_ordinal=0, source_base_oid=BASE_HEAD
        )
        assert document["attempt_ordinal"] == 0
        assert isinstance(document["authority_epoch"], int)
        assert document["continuation_ref_digest"] == ""
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
            run.base_sha = ""  # the source axis loses its revision identity
            await session.commit()
        fake.dispatch_inputs.clear()

        with caplog.at_level(logging.WARNING, logger="forge.runs.github_service"):
            await service._advance_harness(run_id, project_id=PROJECT_ID, issue_number=ISSUE)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "composition_boundary" in str(run.status_reason)
        assert "source_base_oid" in str(run.status_reason)  # the field is named
        assert fake.dispatch_inputs == []  # zero workflow_dispatch calls
        # The refused dispatch overwrote nothing: the persisted envelope is
        # still the authorized one from dispatch 1 (audit trail intact).
        document = run.evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        assert document["source_base_oid"] == BASE_HEAD
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

    async def test_a_restart_between_envelope_commit_and_native_start_reconstructs_the_same_envelope(
        self, db, fake
    ):
        """R36-06 negative: the process dies AFTER the envelope was
        persisted but BEFORE the native start, then re-drives. The SAME
        run rows reconstruct the IDENTICAL authority-bearing envelope —
        the same execution identity (the deterministic derivation), the
        same digest — never a fresh identity for one intent."""
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # envelope committed, native start done
        first = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )
        fake.dispatch_inputs.clear()

        # The re-drive after the "restart": same durable rows, same lease.
        await service._advance_harness(run_id, project_id=PROJECT_ID, issue_number=ISSUE)

        second = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )
        assert second["envelope_digest"] == first["envelope_digest"]
        assert second["execution_attempt_id"] == first["execution_attempt_id"]
        assert second["authority_epoch"] == first["authority_epoch"]
        assert second["attempt_ordinal"] == first["attempt_ordinal"]

    async def test_two_dispatches_from_the_same_source_commit_carry_distinct_identities(
        self, db, fake
    ):
        """AT-07 core at the production entry: the /go attempt and the
        /retry attempt re-implement from the SAME source commit (no
        candidate landed, the operator explicitly restarted the WIP), so
        the source OID is shared — but the retry bumped the durable
        attempt generation, and the two envelopes carry DISTINCT
        execution identities over the same source."""
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # attempt 1 from BASE_HEAD
        first = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )

        # The attempt dies with NO candidate (nothing landed)…
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = "harness_start_failed: boom"
            await session.commit()
        fake.dispatch_inputs.clear()

        # …so the operator's explicit restart re-implements from the SAME
        # committed base (R36-02's EXPLICIT_RESTART override).
        await service.handle_retry(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/retry {run_id} restart",
            author_username="alice",
            delivery_id="retry-at07-1",
        )

        second = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )
        assert first["source_base_oid"] == second["source_base_oid"] == BASE_HEAD
        assert first["execution_attempt_id"] != second["execution_attempt_id"]
        assert second["supersedes"] == first["envelope_digest"]
        assert second["attempt_ordinal"] > first["attempt_ordinal"]
        assert second["authority_epoch"] > first["authority_epoch"]

    async def test_a_stale_callback_from_the_preceding_attempt_is_refused_at_the_publication_entry(
        self, db, fake
    ):
        """R36-06 publication check (service level): two dispatches from
        the same source, then the FIRST attempt's callback arrives after
        the second. The publication entry check — the one the publish leg
        calls before any effect — refuses it on the execution identity
        even though the source OID matches, and nothing publishes (no
        Draft PR, no candidate publication; the run keeps attempt 2's
        envelope)."""
        from forge.runs.composition import assert_publication_identity

        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # attempt 1
        first = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = "harness_start_failed: boom"
            await session.commit()
        fake.dispatch_inputs.clear()

        await service.handle_retry(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/retry {run_id} restart",
            author_username="alice",
            delivery_id="retry-stale-callback-1",
        )

        run = await get_run(db, run_id)
        current = run.evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        assert current["execution_attempt_id"] != first["execution_attempt_id"]
        assert current["source_base_oid"] == first["source_base_oid"]  # the trap
        # The stale callback presents attempt 1's identity + the shared source.
        with pytest.raises(CompositionBoundaryError, match="attempt.identity_mismatch"):
            assert_publication_identity(
                persisted=current,
                presented_execution_attempt_id=first["execution_attempt_id"],
                presented_source_base_oid=first["source_base_oid"],
                presented_authority_epoch=first["authority_epoch"],
            )
        # Zero publication: the refusal happens before any effect, and the
        # run's persisted envelope is still attempt 2's.
        assert fake.pull_requests == {}
        assert not any(call[0] == "create_draft_pr" for call in fake.calls)
        after = (await get_run(db, run_id)).evidence[
            composition_adoption.ATTEMPT_START_EVIDENCE_KEY
        ]
        assert after["execution_attempt_id"] == current["execution_attempt_id"]

    async def test_a_v1_persisted_envelope_dispatches_through_the_legacy_read(
        self, db, fake, caplog
    ):
        """The v1 compat policy at the production entry: a run whose
        persisted ``attempt_start`` predates R36-06 (v1: source-only
        identity, epoch beside the digest) dispatches with the
        ``composition.legacy_read`` marker, the weaker prior identity
        recorded on the new v2 document, and the v1 digest superseded —
        never silently treated as the same authorization."""
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        v2 = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )
        # Rewrite the persisted envelope into the historical v1 shape.
        v1 = {
            "version": 1,
            "envelope_digest": "e" * 64,
            "context_digest": v2["context_digest"],
            "subject_key": v2["subject_key"],
            "resume_mode": "fresh",
            "attempt_base": BASE_HEAD,
            "authority_epoch": 0,
            "profile_source": "execution_profile",
            "legacy": False,
        }
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.evidence = {**(run.evidence or {}), "attempt_start": v1}
            await session.commit()
        fake.dispatch_inputs.clear()

        with caplog.at_level(logging.INFO, logger="forge.runs.github_service"):
            await service._advance_harness(run_id, project_id=PROJECT_ID, issue_number=ISSUE)

        assert any("composition.legacy_read" in m for m in caplog.messages)
        after = dict(
            (await get_run(db, run_id)).evidence[composition_adoption.ATTEMPT_START_EVIDENCE_KEY]
        )
        assert after["version"] == 2
        assert after["supersedes"] == v1["envelope_digest"]
        assert after["legacy_read"] == {
            "envelope_version": 1,
            "execution_attempt_id": None,
            "source_base_oid": BASE_HEAD,
            "authority_epoch": 0,
            "identity_strength": "source_oid_only",
        }
        assert len(fake.dispatch_inputs) == 1  # the dispatch itself still happens

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

        from forge.adaptive import checkpoint_repository as _cr

        async def _r36_lookup(run_id, **_):
            return _cr.CheckpointLookupOutcome.exact("e" * 64, authority="test")

        monkeypatch.setattr(revival, "durable_checkpoint_outcome", _r36_lookup)
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
        # R36-06: the envelope pins the EXACT continuation identity the
        # persisted decision approved (its checkpoint content address).
        assert document["continuation_ref_digest"] == decision["checkpoint_digest"]
        assert len(document["continuation_ref_digest"]) == 64
        # The authority axis is INSIDE the v2 envelope and stays separate
        # from the source revision (the retry opened a new generation).
        assert document["authority_epoch"] >= 1
        assert document["attempt_base"] != "7"  # an OID, never a sequence
