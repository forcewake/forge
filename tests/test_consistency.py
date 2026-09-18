"""R27 / ADR-0027: one lifecycle — the shared READY/finalization invariants.

Pins the three legs of the consolidation slice:

- :mod:`forge.runs.consistency` — the ONE source for the ready evidence
  shape, the ready reason wording ("unverified — …" prefix rule, "checks
  passed; merge is a human decision" tail) and the iron finalization checks
  (exact-string tables raise on any drift);
- the import boundary — ``forge.runs.consistency`` never imports from
  ``forge.integrations.*`` / ``forge.gateway.*`` (core stays provider-neutral;
  the test parses the module's imports);
- the three provider services produce IDENTICAL ready reasons and the SAME
  evidence shape for the same inputs (parametrized over
  gitlab/github/azure, driven through each service's real finalization leg).
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import FlowRun, FlowStatus
from forge.models.base import Base
from forge.runs.consistency import (
    READY_STATUS,
    ReadyInvariantError,
    assert_ready_invariants,
    ready_closing_line,
    ready_evidence,
    ready_reason,
    verification_bound_sha,
    verified_verdict,
)
from forge.runs.stubs import StubReviewer, factory_branch
from forge.runs.verification import PRODUCER_GITLAB_PIPELINE

CANDIDATE = "c" * 40
OTHER = "d" * 40


def _fragment() -> dict:
    return {
        "status": "passed",
        "tested_oid": CANDIDATE,
        "observed_at": "2026-09-17T00:00:00+00:00",
        "producer": "gitlab-pipeline",
    }


# ----------------------------------------------------------------------
# ready_evidence: the unified R02 fragment
# ----------------------------------------------------------------------


class TestReadyEvidence:
    def test_verified_is_the_passed_verdict_shape(self):
        fragment = ready_evidence(True, CANDIDATE, "gitlab-pipeline", summary="ok")
        assert fragment["status"] == "passed"
        assert fragment["tested_oid"] == CANDIDATE
        assert fragment["producer"] == "gitlab-pipeline"
        assert fragment["summary"] == "ok"
        assert fragment["observed_at"]  # ISO-8601 stamp present
        # The exact unified VerificationResult keys — every provider, always.
        assert fragment.keys() >= {"status", "tested_oid", "observed_at", "producer"}

    def test_unverified_is_honestly_labeled(self):
        fragment = ready_evidence(False, CANDIDATE, "azure-build")
        assert fragment["status"] == "unverified"
        assert "summary" not in fragment  # empty summary is omitted, as always

    def test_not_configured_override_stays_honest(self):
        fragment = ready_evidence(
            False, CANDIDATE, "github-checks", status="not_configured"
        )
        assert fragment["status"] == "not_configured"

    def test_contradictions_raise(self):
        with pytest.raises(ReadyInvariantError):
            ready_evidence(True, CANDIDATE, "p", status="not_configured")
        with pytest.raises(ReadyInvariantError):
            ready_evidence(False, CANDIDATE, "p", status="passed")

    def test_surface_is_copied(self):
        fragment = ready_evidence(
            True,
            CANDIDATE,
            "p",
            surface=({"name": "test", "status": "success"},),
        )
        assert fragment["surface"] == [{"name": "test", "status": "success"}]


# ----------------------------------------------------------------------
# ready_reason: the ONE wording source
# ----------------------------------------------------------------------


class TestReadyReason:
    @pytest.mark.parametrize(
        ("verified", "verdict", "summary", "expected"),
        [
            (True, "ok", "", "checks passed; merge is a human decision"),
            (True, "ok", "ignored when verified", "checks passed; merge is a human decision"),
            (True, "concerns", "", "review raised concerns — merge is a human decision"),
            (False, "ok", "no CI configured", "unverified — no CI configured · "
             "merge is a human decision"),
            (
                False,
                "concerns",
                "no CI configured",
                "unverified — no CI configured · "
                "review raised concerns — merge is a human decision",
            ),
        ],
    )
    def test_exact_strings(self, verified, verdict, summary, expected):
        assert ready_reason(verified, verdict, summary) == expected

    def test_unverified_never_claims_checks_passed(self):
        for verdict in ("ok", "concerns"):
            reason = ready_reason(False, verdict, "no verification profile configured")
            assert "checks passed" not in reason
            assert reason.startswith("unverified — ")

    def test_verified_reason_is_identical_across_providers(self):
        # The whole point of R27: the same inputs, the same string, on every
        # lane — GitLab's old "… · checks passed; …" vs GitHub/Azure's bare
        # "merge is a human decision" drift is dead.
        assert (
            ready_reason(True, "ok")
            == ready_reason(True, "ok")
            == "checks passed; merge is a human decision"
        )


class TestReadyClosingLine:
    def test_pair(self):
        assert (
            ready_closing_line(True) == "All checks passed for this exact SHA. "
            "Merging is a human decision."
        )
        assert "**unverified**" in ready_closing_line(False)
        assert "checks passed" not in ready_closing_line(False)


# ----------------------------------------------------------------------
# verified_verdict / verification_bound_sha: the one fragment reading
# ----------------------------------------------------------------------


class TestVerdictReading:
    def test_passed_bound_to_candidate_verifies(self):
        assert verified_verdict(_fragment(), CANDIDATE) is True

    def test_wrong_sha_never_verifies(self):
        assert verified_verdict(_fragment(), OTHER) is False

    def test_non_passed_never_verifies(self):
        fragment = {**_fragment(), "status": "unverified"}
        assert verified_verdict(fragment, CANDIDATE) is False

    def test_legacy_github_candidate_sha_key_is_tolerated(self):
        legacy = {"status": "passed", "candidate_sha": CANDIDATE}
        assert verification_bound_sha(legacy) == CANDIDATE
        assert verified_verdict(legacy, CANDIDATE) is True

    def test_garbage_is_false_not_a_crash(self):
        assert verified_verdict(None, CANDIDATE) is False
        assert verified_verdict({}, CANDIDATE) is False
        assert verification_bound_sha(None) == ""


# ----------------------------------------------------------------------
# assert_ready_invariants: the iron checks
# ----------------------------------------------------------------------


class TestAssertReadyInvariants:
    def test_happy_path_passes(self):
        assert_ready_invariants(
            READY_STATUS,
            {"verification": _fragment()},
            CANDIDATE,
            CANDIDATE,
            reason="checks passed; merge is a human decision",
        )

    @pytest.mark.parametrize("terminal", ["blocked", "failed", "cancelled", "superseded"])
    def test_terminal_runs_never_finalize(self, terminal):
        with pytest.raises(ReadyInvariantError, match="never finalize"):
            assert_ready_invariants(terminal, {"verification": _fragment()}, CANDIDATE, CANDIDATE)

    def test_ready_without_candidate_sha_raises(self):
        with pytest.raises(ReadyInvariantError, match="without a candidate sha"):
            assert_ready_invariants(READY_STATUS, {"verification": _fragment()}, "", CANDIDATE)

    def test_ready_without_review_sha_raises(self):
        with pytest.raises(ReadyInvariantError, match="without a recorded review sha"):
            assert_ready_invariants(READY_STATUS, {"verification": _fragment()}, CANDIDATE, "")

    def test_review_for_another_sha_raises(self):
        with pytest.raises(ReadyInvariantError, match="bound to a different sha"):
            assert_ready_invariants(READY_STATUS, {"verification": _fragment()}, CANDIDATE, OTHER)

    def test_verdict_for_another_commit_raises(self):
        fragment = {**_fragment(), "tested_oid": OTHER}
        with pytest.raises(ReadyInvariantError, match="verdict for a different commit"):
            assert_ready_invariants(READY_STATUS, {"verification": fragment}, CANDIDATE, CANDIDATE)

    def test_unverified_reason_never_says_checks_passed(self):
        fragment = {**_fragment(), "status": "unverified"}
        with pytest.raises(ReadyInvariantError, match="checks passed"):
            assert_ready_invariants(
                READY_STATUS,
                {"verification": fragment},
                CANDIDATE,
                CANDIDATE,
                reason="unverified — no CI configured · checks passed; merge is a human decision",
            )

    def test_unverified_honest_reason_passes(self):
        fragment = {**_fragment(), "status": "not_configured"}
        assert_ready_invariants(
            READY_STATUS,
            {"verification": fragment},
            CANDIDATE,
            CANDIDATE,
            reason="unverified — no CI configured · merge is a human decision",
        )

    def test_unknown_verification_status_raises(self):
        fragment = {**_fragment(), "status": "lgtm"}
        with pytest.raises(ReadyInvariantError, match="unknown verification status"):
            assert_ready_invariants(READY_STATUS, {"verification": fragment}, CANDIDATE, CANDIDATE)

    def test_missing_verification_fragment_is_tolerated_for_resume(self):
        # A crash-resume may re-drive a run whose verification fragment was
        # never recorded — the sha-binding checks still gate the finalize.
        assert_ready_invariants(READY_STATUS, {}, CANDIDATE, CANDIDATE)
        assert_ready_invariants(READY_STATUS, None, CANDIDATE, CANDIDATE)


# ----------------------------------------------------------------------
# Import boundary: core stays provider-neutral
# ----------------------------------------------------------------------


class TestImportBoundary:
    def test_consistency_never_imports_integrations_or_gateway(self):
        """R27's enforceable half today: forge.runs.consistency is core —
        no provider SDK package (forge.integrations.*) and no gateway
        transport (forge.gateway.*) may appear in its imports, direct or
        via relative-import resolution."""
        source = Path(
            __import__("forge.runs.consistency", fromlist=["__file__"]).__file__
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                prefix = "." * max(node.level - 1, 0)
                imported.append(f"{prefix}{module}")
        assert imported, "the module parsed but imported nothing — broken test?"
        for name in imported:
            assert not name.startswith(
                ("forge.integrations", "forge.gateway", "integrations", "gateway")
            ), f"forge.runs.consistency must not import {name!r} (ADR-0027 boundary)"
        # ...and it does pull the shared verdict vocabulary from core.
        assert any(name == "forge.runs.verification" for name in imported)


# ----------------------------------------------------------------------
# The three services agree: identical ready outputs for the same inputs
# ----------------------------------------------------------------------

GITLAB_PROJECT, ISSUE_IID = 42, 7
GITHUB_REPO = "acme/acme-widget"
GITHUB_PROJECT, GITHUB_ISSUE = 70010, 42
AZURE_PROJECT_GUID = "9f8e7d6c-0000-0000-0000-000000000009"
AZURE_REPO, AZURE_WORK_ITEM = "core", 142


async def _add_run(
    db,
    *,
    provider: str,
    project_id: int,
    issue_iid: int,
    status: str = FlowStatus.REVIEWING.value,
    evidence: dict | None = None,
    **extra,
) -> str:
    run_id = uuid4().hex
    async with db() as session:
        session.add(
            FlowRun(
                id=run_id,
                provider=provider,
                project_id=project_id,
                issue_iid=issue_iid,
                status=status,
                candidate_shas=[CANDIDATE],
                base_sha="1" * 40,
                plan_digest="a" * 64,
                evidence=evidence,
                **extra,
            )
        )
        await session.commit()
    return run_id


async def _get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


async def _finalize_gitlab(db, verified: bool) -> str:
    """Drive the GitLab service's real finalization leg to ready."""
    from tests.fixtures.fake_gitlab import FakeGitLab
    from tests.test_runs_verification import make_service as make_gitlab_service
    from tests.test_runs_verification import make_settings as gitlab_settings

    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, "Add a widget", "Widgets make the app better.")
    fragment = ready_evidence(verified, CANDIDATE, PRODUCER_GITLAB_PIPELINE)
    run_id = await _add_run(
        db,
        provider="gitlab",
        project_id=GITLAB_PROJECT,
        issue_iid=ISSUE_IID,
        evidence={"verification": fragment},  # the gate already recorded its verdict
    )
    fake.seed_commit(factory_branch(ISSUE_IID, run_id), CANDIDATE, "candidate")
    service = make_gitlab_service(
        db, fake, settings=gitlab_settings(), reviewer=StubReviewer()
    )
    await service._review_and_ready(
        run_id,
        project_id=GITLAB_PROJECT,
        issue_iid=ISSUE_IID,
        mr_iid=None,
        candidate_sha=CANDIDATE,
        base_sha="1" * 40,
        pipeline=SimpleNamespace(id=9, status="success", web_url="https://gitlab.test/-/pipelines/9"),
        plan_digest="a" * 64,
        verified=verified,
        verification_evidence=fragment,
    )
    return run_id


async def _finalize_github(db, verified: bool) -> str:
    """Drive the GitHub service's real finalization leg to ready."""
    from tests.fixtures.fake_github import FakeGitHub
    from tests.test_github_runs import make_service as make_github_service
    from tests.test_github_runs import make_settings as github_settings
    from tests.test_github_runs import make_stack as make_github_stack

    fake = FakeGitHub()
    fake.seed_repo(GITHUB_REPO, {"src/app.py": "print('hi')\n"})
    fake.heads[GITHUB_REPO]["main"] = "1" * 40
    fake.seed_issue(GITHUB_REPO, GITHUB_ISSUE, "Add password reset", "Users cannot reset.")
    fragment = ready_evidence(verified, CANDIDATE, "github-checks")
    run_id = await _add_run(
        db,
        provider="github",
        project_id=GITHUB_PROJECT,
        issue_iid=GITHUB_ISSUE,
        evidence={"verification": fragment},  # the gate already recorded its verdict
        github_repo_full_name=GITHUB_REPO,
        github_issue_number=GITHUB_ISSUE,
        mr_iid=101,
    )
    service = make_github_service(
        db, fake, settings=github_settings(), stack=make_github_stack(fake)
    )
    await service._review_and_ready(
        run_id,
        project_id=GITHUB_PROJECT,
        issue_number=GITHUB_ISSUE,
        pr_number=101,
        candidate_sha=CANDIDATE,
        base_sha="1" * 40,
        verified=verified,
        verification_evidence=fragment,
    )
    return run_id


async def _finalize_azure(db, verified: bool) -> str:
    """Drive the Azure DevOps service's real finalization leg to ready."""
    from forge.gateway.azure_webhook import azure_project_key
    from tests.test_azure_runs import FakeAzureDevOps
    from tests.test_azure_runs import make_service as make_azure_service
    from tests.test_azure_runs import make_settings as azure_settings
    from tests.test_azure_runs import make_stack as make_azure_stack

    project_id = azure_project_key(AZURE_PROJECT_GUID)
    fake = FakeAzureDevOps()
    fake.seed_work_item(
        AZURE_WORK_ITEM, "Ship the flux capacitor", "<p>Users cannot reset.</p>"
    )
    fragment = ready_evidence(verified, CANDIDATE, "azure-build")
    run_id = await _add_run(
        db,
        provider="azure_devops",
        project_id=project_id,
        issue_iid=AZURE_WORK_ITEM,
        evidence={"verification": fragment},  # the gate already recorded its verdict
        mr_iid=501,
    )
    service = make_azure_service(
        db, fake, settings=azure_settings(), stack=make_azure_stack(fake)
    )
    await service._review_and_ready(
        run_id,
        project_id=project_id,
        issue_number=AZURE_WORK_ITEM,
        pr_id=501,
        candidate_sha=CANDIDATE,
        base_sha="1" * 40,
        verified=verified,
        verification_evidence=fragment,
    )
    return run_id


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


@pytest.mark.parametrize("finalizer", [_finalize_gitlab, _finalize_github, _finalize_azure])
class TestProvidersAgree:
    """The same finalization inputs must land the same outputs on every
    provider — the R27 property that makes the /retry-guard and
    GitHub-only-ship bug classes impossible."""

    async def test_verified_ready_reason_is_identical(self, db, finalizer):
        run_id = await finalizer(db, verified=True)
        run = await _get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert run.status_reason == "checks passed; merge is a human decision"

    async def test_review_evidence_is_bound_to_the_candidate(self, db, finalizer):
        run_id = await finalizer(db, verified=True)
        evidence = (await _get_run(db, run_id)).evidence
        assert evidence["review"]["sha"] == CANDIDATE
        assert evidence["review"]["verdict"] == "ok"

    async def test_unverified_ready_reason_follows_the_shared_rules(self, db, finalizer):
        run_id = await finalizer(db, verified=False)
        run = await _get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        reason = run.status_reason or ""
        assert reason.startswith("unverified — ")  # the prefix rule
        assert reason.endswith(" · merge is a human decision")  # the honest tail
        assert "checks passed" not in reason  # R02: never implied green


class TestGateEvidenceUnification:
    """The verification gates record the ONE unified evidence shape.

    GitLab's and Azure's gates are already pinned by their suites
    (tests/test_runs_verification.py, tests/test_azure_runs.py); this pins
    GitHub's, which used to hand-roll a ``candidate_sha``/``checks``
    variant — the drift that motivated ADR-0027's ready_evidence.
    """

    async def test_github_green_checks_record_unified_fragment_and_reason(self, db):
        from tests.fixtures.fake_github import FakeGitHub
        from tests.test_github_runs import make_service as make_github_service
        from tests.test_github_runs import make_settings as github_settings
        from tests.test_github_runs import make_stack as make_github_stack

        fake = FakeGitHub()
        fake.seed_repo(GITHUB_REPO, {"src/app.py": "print('hi')\n"})
        fake.heads[GITHUB_REPO]["main"] = "1" * 40
        fake.seed_issue(GITHUB_REPO, GITHUB_ISSUE, "Add password reset", "Users cannot reset.")
        fake.seed_workflow_runs(
            [
                {"head_sha": CANDIDATE, "name": "ci", "status": "completed",
                 "conclusion": "success"},
            ]
        )
        run_id = await _add_run(
            db,
            provider="github",
            project_id=GITHUB_PROJECT,
            issue_iid=GITHUB_ISSUE,
            status=FlowStatus.WAITING_CI.value,
            github_repo_full_name=GITHUB_REPO,
            github_issue_number=GITHUB_ISSUE,
            mr_iid=101,
        )
        service = make_github_service(
            db, fake, settings=github_settings(), stack=make_github_stack(fake)
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await _get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        # The shared verified ready tail — word-for-word the GitLab lane's.
        assert run.status_reason == "checks passed; merge is a human decision"
        verification = run.evidence["verification"]
        assert set(verification) >= {"status", "tested_oid", "observed_at", "producer"}
        assert verification["status"] == "passed"
        assert verification["tested_oid"] == CANDIDATE  # was "candidate_sha" pre-ADR-0027
        assert verification["producer"] == "github-checks"
        assert verification["surface"] == [{"name": "ci", "conclusion": "success"}]
