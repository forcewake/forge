"""The credential-broker dispatch trace (issue #207 / NEXT-19).

The recorded gap this module closes is ADR-0030's recurring finding: a
correct credential substrate with ZERO production callers. Every
provider dispatch leg now resolves its project's bound credential
through the broker under the active execution grant BEFORE the provider
call — proven here at production-entry discipline: the REAL
:class:`forge.runs.service.RunService` and the REAL :class:`GitLabClient`
over real HTTP to the fake native server, real durable state, the
dispatch ledger and the run's own evidence as the assertion surfaces.

Traces (all against the GitLab ``ci_harness:claude-code`` lane):

- **CD-1** — the happy path: the broker's staged selection rides the
  dispatch envelope's variable set (and NOT the ambient env value —
  staging is the broker's decision, not the process's), the extended
  proof (/2) rides the run evidence beside the envelope, and no root
  secret ever enters the variable set.
- **CD-2** — a revoked binding: ZERO provider dispatches (the native
  ledger is empty), the run parks ``blocked(credential_refused:…)``.
- **CD-3** — a wrong-subject ref presented by the prior dispatch's
  durable proof: typed ``wrong_project_ref``, zero dispatches — the
  cross-tenant leak refused at the real seam, with the colliding
  numeric-id subject's OWN ref never staged for this run.
- **CD-4** — rotation between dispatch and the repair re-dispatch:
  typed ``rotated`` refusal, zero dispatches for the refused leg; the
  operator's ``/retry`` (a NEW attempt generation) re-resolves the
  rotated-in version and dispatches it.
- **CD-5** — the in-flight snapshot: a credential rotated at the broker
  SOURCE between two dispatches of the same ref never rewrites the
  first dispatch's recorded variables; the next dispatch re-resolves.
- **CD-6 (the mutation arm)** — with the dispatch-leg resolution call
  monkeypatched OUT, the staged variable and the proof DISAPPEAR: the
  CD-1 invariants genuinely depend on the wired call. If production
  ever drops the call again (the ADR-0030 recurring finding), CD-1
  fails — this trace documents the dependency from the other side.
- **CD-7** — the same seam on the GITHUB lane (the Actions dispatch
  leg): the staged selection rides the workflow inputs, the proof rides
  the evidence beside the harness handle, and a revoked binding means
  zero workflow dispatches.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from forge.adaptive.credential_broker import StagedBroker
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import ProjectCredentialRegistry
from forge.durable import FlowRun, FlowStatus

from .conftest import (
    GL_ISSUE_DESC,
    GL_ISSUE_IID,
    GL_ISSUE_TITLE,
    GL_PROJECT_ID,
    gl_settings,
)

pytestmark = pytest.mark.production_entry

#: The run's canonical subject as the service derives it from the run
#: row (gitlab family, unrecorded connection, the numeric project id).
RUN_SUBJECT = CanonicalSubject(
    provider_family="gitlab", connection="-", native_id=str(GL_PROJECT_ID)
)

#: A DIFFERENT self-managed instance with the SAME numeric project id —
#: the collision the canonical-subject key exists to prevent.
COLLIDING_SUBJECT = CanonicalSubject(
    provider_family="gitlab", connection="gitlab.other.example", native_id=str(GL_PROJECT_ID)
)

#: The bound credential refs and their staged generations.
REF_V1 = "env:ANTHROPIC_AUTH_TOKEN"
REF_V2 = "vault:kv/eng#42"
STAGED_VALUE_V1 = "pe-broker-selection-generation-one"  # noqa: S105 — a test fixture value
STAGED_VALUE_V2 = "pe-broker-selection-generation-two"  # noqa: S105 — a test fixture value

#: The ambient value that must NEVER be staged while the broker holds a
#: different selection (staging is the broker's decision, not the
#: process environment's).
AMBIENT_VALUE = "pe-ambient-never-staged"  # noqa: S105 — a test fixture value


def make_bound_service(session_factory, gitlab, *, registry: ProjectCredentialRegistry, broker):
    """A REAL RunService carrying the credential registry + broker (the
    constructor seams the dispatch legs resolve under)."""
    from forge.config import ForgeConfig
    from forge.runs.service import RunService
    from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

    return RunService(
        session_factory,
        gitlab=gitlab,
        settings=gl_settings(),
        config=ForgeConfig(),
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
        credential_registry=registry,
        credential_broker=broker,
    )


def _bound_lab(*, ref: str = REF_V1, value: str = STAGED_VALUE_V1, version: str = "v1") -> tuple:
    """The bound world: a registry with THIS subject's live binding plus
    the colliding instance's, and a staged broker holding the pinned
    generations (never the process env)."""
    registry = ProjectCredentialRegistry()
    registry.bind(RUN_SUBJECT, "anthropic-gateway", ref, bound_by="ops@a")
    registry.bind(
        COLLIDING_SUBJECT, "anthropic-gateway", "vault:kv/other-instance#1", bound_by="ops@b"
    )
    broker = StagedBroker()
    broker.stage(ref, value, env_var="ANTHROPIC_AUTH_TOKEN", version=version)
    broker.stage(
        "vault:kv/other-instance#1", "other-instance-material", env_var="ANTHROPIC_AUTH_TOKEN"
    )
    broker.stage(REF_V2, STAGED_VALUE_V2, env_var="ANTHROPIC_AUTH_TOKEN", version="v2")
    return registry, broker


async def get_run(session_factory, run_id: str) -> FlowRun:
    async with session_factory() as session:
        return await session.get(FlowRun, run_id)


async def get_run_evidence(session_factory, run_id: str) -> dict[str, Any]:
    run = await get_run(session_factory, run_id)
    return dict(run.evidence or {})


async def drive_go(service, run_id: str) -> None:
    await service.handle_command_note(
        GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
    )


def dispatched_variables(gitlab_native, index: int) -> dict[str, str]:
    dispatch = gitlab_native.dispatches()[index]
    return {v["key"]: v["value"] for v in dispatch["variables"]}


async def _start_run(pe_db, gitlab_native, gitlab_client, *, registry, broker, monkeypatch):
    """issue → plan → the parked gate (the REAL planning leg, stubbed
    models). Returns (service, factory, run_id)."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", AMBIENT_VALUE)
    gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
    factory = pe_db.worker_factory()
    service = make_bound_service(factory, gitlab_client, registry=registry, broker=broker)
    run_id = await service.start_run(
        GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
    )
    return service, factory, run_id


async def _seed_prior_credential_proof(
    pe_db, run_id: str, *, ref: str, generation: int = 0
) -> None:
    """Seed the durable record of a PRIOR dispatch's staged credential —
    exactly the evidence shape the dispatch leg writes (the re-dispatch
    presents it; a rotation in between is the typed refusal)."""
    async with pe_db.worker_factory()() as session:
        run = await session.get(FlowRun, run_id)
        evidence = dict(run.evidence or {})
        harness = dict(evidence.get("harness") or {})
        harness["dispatch_credential"] = {
            "schema": "forge.project.dispatch-credential-proof/2",
            "credential_ref": ref,
            "attempt_generation": generation,
        }
        evidence["harness"] = harness
        run.evidence = evidence
        await session.commit()


# ----------------------------------------------------------------------
# CD-1 — the happy path: the broker's selection rides the dispatch
# ----------------------------------------------------------------------


class TestCD1BrokerSelectionRidesTheDispatch:
    async def test_the_envelope_stages_the_brokers_selection_and_the_proof_rides_the_evidence(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        assert len(gitlab_native.dispatches()) == 1

        variables = dispatched_variables(gitlab_native, 0)
        # The staged slot is the BROKER's selection — not the ambient env
        # value the process carries (staging is a broker decision).
        assert variables["ANTHROPIC_AUTH_TOKEN"] == STAGED_VALUE_V1
        assert variables["ANTHROPIC_AUTH_TOKEN"] != AMBIENT_VALUE
        # No root secret ever enters the variable set.
        every_value = [v["value"] for d in gitlab_native.dispatches() for v in d["variables"]]
        for secret_value in ("glpat-test", "whsec", STAGED_VALUE_V2):
            assert secret_value not in every_value

        evidence = await get_run_evidence(factory, run_id)
        proof = evidence["harness"]["dispatch_credential"]
        assert proof["schema"] == "forge.project.dispatch-credential-proof/2"
        assert proof["subject"] == RUN_SUBJECT.subject_id()
        assert proof["provider"] == "anthropic-gateway"
        assert proof["credential_ref"] == REF_V1
        assert proof["binding_revision"] == 1
        assert proof["resolver_identity"] == "staged"
        assert proof["resolved_version"] == "v1"
        assert proof["grant"]["run_id"] == run_id
        assert proof["grant"]["attempt_generation"] == "0"
        assert proof["receipt"]["schema"] == "forge.credential.broker-receipt/1"
        # The VALUE never appears in the evidence — the proof cites the
        # resolved version, never the material.
        assert STAGED_VALUE_V1 not in json.dumps(evidence)
        # The envelope journal names the receipt identity beside the digest.
        envelope = evidence["harness"]["dispatch_envelope"]
        assert envelope["credential_ref"] == REF_V1
        assert envelope["credential_resolved_version"] == "v1"
        assert envelope["credential_resolver"] == "staged"
        assert "ANTHROPIC_AUTH_TOKEN" in envelope["variable_keys"]
        # The real client never fell off the modeled API surface.
        assert gitlab_native.unknown_paths() == []


# ----------------------------------------------------------------------
# CD-2 — a revoked binding: zero provider dispatches
# ----------------------------------------------------------------------


class TestCD2RevokedBinding:
    async def test_a_revoked_binding_parks_the_run_with_zero_provider_dispatches(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        registry, broker = _bound_lab()
        registry.revoke(RUN_SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "credential_refused: revoked" in str(run.status_reason)
        # THE ledger proof: zero pipelines were ever created.
        assert gitlab_native.dispatches() == []
        evidence = await get_run_evidence(factory, run_id)
        assert "dispatch_credential" not in (evidence.get("harness") or {})
        assert gitlab_native.unknown_paths() == []


# ----------------------------------------------------------------------
# CD-3 — a wrong-subject ref: the cross-tenant leak refused at the seam
# ----------------------------------------------------------------------


class TestCD3WrongSubjectRef:
    async def test_a_foreign_subjects_ref_is_refused_with_zero_dispatches(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
        )
        # The durable record of a prior dispatch claims the OTHER
        # instance's ref (the colliding numeric id): the dispatch seam
        # must refuse it — this run's lane can never stage it.
        await _seed_prior_credential_proof(
            pe_db, run_id, ref="vault:kv/other-instance#1", generation=0
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "credential_refused: wrong_project_ref" in str(run.status_reason)
        assert gitlab_native.dispatches() == []

        # And the colliding instance's own ref resolves ONLY for its own
        # subject: this run's binding (REF_V1) is what a clean dispatch
        # would stage — the other instance's material never crosses.
        assert registry.binding_for(RUN_SUBJECT, "anthropic-gateway").credential_ref == REF_V1
        assert (
            registry.binding_for(COLLIDING_SUBJECT, "anthropic-gateway").credential_ref
            == "vault:kv/other-instance#1"
        )


# ----------------------------------------------------------------------
# CD-4 — rotation: typed refusal, the retry re-resolves the new version
# ----------------------------------------------------------------------


class TestCD4Rotation:
    async def test_rotation_between_dispatch_and_repair_refuses_typed_and_the_retry_re_resolves(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        # The /retry's checkpoint consultation reads a REAL (empty) store —
        # a proven absence, never an unavailable authority.
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "cd-store"))
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
        )
        await drive_go(service, run_id)
        assert len(gitlab_native.dispatches()) == 1

        # The operator rotates the binding between the first dispatch
        # and a re-dispatch of the SAME attempt (the crashed-advance /go
        # resume and the fallback advance both re-enter the leg at the
        # same generation — driven here through the dispatch entry
        # itself, exactly what those callers run).
        registry.bind(RUN_SUBJECT, "anthropic-gateway", REF_V2, bound_by="ops@a")

        # The same-generation re-dispatch presents the prior dispatch's
        # ref → typed `rotated` refusal, zero provider dispatches for
        # the refused leg.
        await service._advance_harness(GL_PROJECT_ID, run_id)
        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "credential_refused: rotated" in str(run.status_reason)
        assert len(gitlab_native.dispatches()) == 1  # the ledger proof

        # The operator's /retry opens a NEW attempt generation: it
        # re-resolves the rotated-in version and dispatches it. The
        # continuation decision needs a proven source — the lane's
        # RECORDED bootstrap classification ("failed": no vendor session
        # ever started) makes the committed baseline the authorized one.
        async with pe_db.worker_factory()() as session:
            run_row = await session.get(FlowRun, run_id)
            retry_evidence = dict(run_row.evidence or {})
            retry_evidence["bootstrap"] = "failed"
            run_row.evidence = retry_evidence
            await session.commit()
        await service.handle_retry_note(
            GL_PROJECT_ID,
            f"@forge /retry {run_id}",
            "alice",
            GL_ISSUE_IID,
            delivery_id="cd-retry-1",
        )
        assert len(gitlab_native.dispatches()) == 2
        variables = dispatched_variables(gitlab_native, 1)
        assert variables["ANTHROPIC_AUTH_TOKEN"] == STAGED_VALUE_V2
        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        proof = (await get_run_evidence(factory, run_id))["harness"]["dispatch_credential"]
        assert proof["credential_ref"] == REF_V2
        assert proof["resolved_version"] == "v2"
        assert proof["binding_revision"] == 2
        assert proof["attempt_generation"] == 1
        assert gitlab_native.unknown_paths() == []


# ----------------------------------------------------------------------
# CD-5 — the in-flight snapshot semantics
# ----------------------------------------------------------------------


class TestCD5InFlightSnapshot:
    async def test_a_source_rotation_never_rewrites_the_dispatched_generation(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
        )
        await drive_go(service, run_id)
        assert len(gitlab_native.dispatches()) == 1

        # The credential rotates AT THE BROKER SOURCE under the SAME ref
        # while the first lane is in flight: the recorded dispatch (the
        # in-flight lane's env) keeps its staged generation…
        broker.stage(
            REF_V1,
            "pe-broker-selection-rotated-source",
            env_var="ANTHROPIC_AUTH_TOKEN",
            version="v1",
        )
        assert dispatched_variables(gitlab_native, 0)["ANTHROPIC_AUTH_TOKEN"] == STAGED_VALUE_V1

        # …and only the NEXT dispatch re-resolves (a same-attempt
        # re-dispatch through the leg entry — same ref, so no rotation
        # refusal: the credential GENERATION moved, the binding did not).
        await service._advance_harness(GL_PROJECT_ID, run_id)
        run = await get_run(factory, run_id)
        assert len(gitlab_native.dispatches()) == 2, run.status_reason
        assert dispatched_variables(gitlab_native, 0)["ANTHROPIC_AUTH_TOKEN"] == STAGED_VALUE_V1
        assert dispatched_variables(gitlab_native, 1)["ANTHROPIC_AUTH_TOKEN"] == (
            "pe-broker-selection-rotated-source"
        )


# ----------------------------------------------------------------------
# CD-6 — the mutation arm (the ADR-0030 recurring-finding guard)
# ----------------------------------------------------------------------


class TestCD6MutationArm:
    async def test_unwiring_the_dispatch_leg_resolution_undoes_the_staging(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        """Monkeypatch OUT the dispatch-leg resolution call: the staged
        variable and the proof DISAPPEAR from the dispatch. This is the
        guard's other side — CD-1's assertions hold ONLY because the
        call is wired; if the call ever vanishes from the production
        leg again, CD-1 fails while this trace keeps proving why."""
        import forge.runs.service as service_module

        async def _unwired(registry, broker, **_kwargs):  # type: ignore[no-untyped-def]
            return None  # the mutation: "resolution never happened"

        monkeypatch.setattr(service_module, "stage_dispatch_credential", _unwired)
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value  # ambient behavior returns
        variables = dispatched_variables(gitlab_native, 0)
        assert "ANTHROPIC_AUTH_TOKEN" not in variables  # nothing staged
        evidence = await get_run_evidence(factory, run_id)
        assert "dispatch_credential" not in (evidence.get("harness") or {})
        # The unwired seam is OBSERVABLE: these are exactly the CD-1
        # invariants that break — the mutation arm stands or falls with
        # them.
        assert variables["FORGE_LANE_RESUME_MODE"] == "fresh"  # the dispatch itself is intact


# ----------------------------------------------------------------------
# CD-7 — the same seam on the GITHUB lane (the Actions dispatch leg)
# ----------------------------------------------------------------------


class TestCD7GitHubLane:
    """The GitHub `_advance_harness` leg resolves through the SAME seam
    (real HTTP to the fake native server's GitHub mode, real durable
    state): the staged selection rides the workflow_dispatch inputs and
    the proof rides the evidence beside the harness handle."""

    @staticmethod
    def _github_service(session_factory, client, reader, *, registry, broker):
        from forge.config import ForgeConfig
        from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
        from forge.runs.github_service import GitHubRunService
        from forge.runs.stubs import StubImplementer, StubPlanner
        from .conftest import PE_BASE_BRANCH, PE_REPO, StubPRReviewer, pe_settings

        stack = GitHubAgents(
            client=client,
            reader=reader,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubPRReviewer(),
            flow=GitHubPublishFlow(client, proposer=StubImplementer(), base_branch=PE_BASE_BRANCH),
        )
        return GitHubRunService(
            session_factory,
            pe_settings(),
            ForgeConfig(),
            stack=stack,
            repo_full_name=PE_REPO,
            credential_registry=registry,
            credential_broker=broker,
        )

    async def _drive(self, pe_db, native, native_client, monkeypatch, *, revoke: bool):
        from .conftest import PE_OWNER, PE_REPO_NAME

        GH_SUBJECT = CanonicalSubject(
            provider_family="github", connection="-", native_id="acme/forge-pe"
        )
        registry, broker = _bound_lab()
        # Rebind under the github subject (the numeric gitlab subject is a
        # different key — equal ids never collide across families either).
        registry.bind(GH_SUBJECT, "anthropic-gateway", REF_V1, bound_by="ops@gh")
        if revoke:
            registry.revoke(GH_SUBJECT, "anthropic-gateway", revoked_by="ops@gh")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", AMBIENT_VALUE)
        native.seed_issue(42, "Add the widget", "Body")
        client, reader = native_client
        factory = pe_db.worker_factory()
        service = self._github_service(factory, client, reader, registry=registry, broker=broker)
        assert PE_OWNER and PE_REPO_NAME  # the conftest identity this leg runs on
        run_id = await service.start_run(
            project_id=42,
            issue_number=42,
            issue_title="Add the widget",
            issue_description="Body",
            author_username="alice",
        )
        await service.handle_go(
            project_id=42,
            issue_number=42,
            note_text=f"@forge /go {run_id}",
            author_username="alice",
        )
        return service, factory, run_id, GH_SUBJECT

    async def test_the_staged_selection_rides_the_workflow_inputs_and_the_proof_the_evidence(
        self, pe_db, native, native_client, monkeypatch
    ):
        service, factory, run_id, subject = await self._drive(
            pe_db, native, native_client, monkeypatch, revoke=False
        )
        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        assert len(native.dispatches()) == 1
        branch = native.dispatches()[0]["ref"]
        (inputs,) = native.dispatch_inputs(ref=branch)
        # The broker's selection — never the ambient process value.
        assert inputs["ANTHROPIC_AUTH_TOKEN"] == STAGED_VALUE_V1
        assert inputs["ANTHROPIC_AUTH_TOKEN"] != AMBIENT_VALUE
        proof = (await get_run_evidence(factory, run_id))["harness"]["dispatch_credential"]
        assert proof["schema"] == "forge.project.dispatch-credential-proof/2"
        assert proof["subject"] == subject.subject_id()
        assert proof["resolver_identity"] == "staged"
        assert proof["resolved_version"] == "v1"
        assert STAGED_VALUE_V1 not in json.dumps(await get_run_evidence(factory, run_id))
        assert native.unknown_paths() == []

    async def test_a_revoked_binding_means_zero_workflow_dispatches(
        self, pe_db, native, native_client, monkeypatch
    ):
        service, factory, run_id, subject = await self._drive(
            pe_db, native, native_client, monkeypatch, revoke=True
        )
        del service, subject
        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "credential_refused: revoked" in str(run.status_reason)
        assert native.dispatches() == []
        assert native.unknown_paths() == []
