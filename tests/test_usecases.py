"""ADR-0027 slice 2 (A15): the shared ObserveVerification use case.

Pins the second consolidation slice — the ``evaluate_waiting_ci_one``
triplication extracted into :mod:`forge.runs.usecases`:

- :func:`forge.runs.usecases.observe_verification` — the ONE verdict
  semantics (completeness, positive proof over the frozen contract, waiver
  handling, provider-head binding) — with each provider's conclusion
  vocabulary translating at the adapter (parametrized over the GitHub,
  Azure DevOps and GitLab-shaped vocabularies: identical normalized inputs
  yield identical decisions, modulo the producer identity that is the
  provider's signature by design);
- the import boundary — ``forge.runs.usecases`` never imports from
  ``forge.integrations.*`` / ``forge.gateway.*`` (the same enforceable fence
  as ``tests/test_consistency.py`` for slice 1);
- the documented GitLab boundary — the two empty-contract semantics and the
  missing-required-job semantics are deliberately OPPOSITE (honesty over
  forced unification), pinned here so a future unification must reconcile
  them consciously instead of silently flipping a lane;
- the two real gates agree — the GitHub and Azure ``evaluate_waiting_ci_one``
  legs produce identical decision classes, verification statuses, tested_oid
  bindings and ready reasons for equivalent evidence (the R02 property).

Behavior-neutrality proof: the pre-existing A01 suites
(``tests/test_github_runs.py``, ``tests/test_azure_runs.py``,
``tests/test_github_harness.py``) pass UNMODIFIED — the extraction moved
code, not semantics.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import FlowRun, FlowStatus
from forge.gitlab.schemas import Job, Pipeline
from forge.models.base import Base
from forge.runs.usecases import (
    OUTCOME_BLOCK,
    OUTCOME_REPAIR,
    OUTCOME_REVIEW,
    OUTCOME_WAIT,
    VerificationDecision,
    observe_verification,
)
from forge.runs.verification import (
    AZURE_CODE_FAILURE_RESULTS,
    AZURE_INFRA_RESULTS,
    AZURE_SUCCESS_RESULTS,
    GITHUB_CODE_FAILURE_CONCLUSIONS,
    GITHUB_INFRA_CONCLUSIONS,
    GITHUB_SUCCESS_CONCLUSIONS,
    PRODUCER_AZURE_BUILD,
    PRODUCER_GITHUB_CHECKS,
    VerificationProfile,
    evaluate,
)

CANDIDATE = "c" * 40
OBSERVED_HEAD = "9" * 40
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Vocabulary:
    """One provider's conclusion vocabulary — the adapter-side translation.

    The use case is vocabulary-blind: the adapter spells each SEMANTIC class
    (``proven`` / ``code_red`` / ``infra_red`` / ``inconclusive``) in its
    native conclusions and hands the classifier its native sets.
    """

    producer: str
    success: frozenset[str]
    code_failure: frozenset[str]
    infra: frozenset[str]
    proven: str
    code_red: str
    infra_red: str
    inconclusive: str


KIT_GITHUB = Vocabulary(
    PRODUCER_GITHUB_CHECKS,
    GITHUB_SUCCESS_CONCLUSIONS,
    GITHUB_CODE_FAILURE_CONCLUSIONS,
    GITHUB_INFRA_CONCLUSIONS,
    "success",
    "failure",
    "cancelled",
    "skipped",
)
KIT_AZURE = Vocabulary(
    PRODUCER_AZURE_BUILD,
    AZURE_SUCCESS_RESULTS,
    AZURE_CODE_FAILURE_RESULTS,
    AZURE_INFRA_RESULTS,
    "succeeded",
    "partiallySucceeded",
    "canceled",
    "skipped",
)
# The GitLab job-status vocabulary — the shape its pipeline gate WOULD
# normalize to. GitLab's gate deliberately does NOT call the use case (the
# structural reasons live in the forge.runs.usecases docstring and are pinned
# by TestGitLabBoundary); the kit proves the decision core would still treat
# it like any other provider.
KIT_GITLAB = Vocabulary(
    "gitlab-pipeline",
    frozenset({"success"}),
    frozenset({"failed"}),
    frozenset({"canceled"}),
    "success",
    "failed",
    "canceled",
    "skipped",
)

KITS = [KIT_GITHUB, KIT_AZURE, KIT_GITLAB]


def _spec(required: tuple[str, ...]) -> SimpleNamespace:
    """A stand-in for the frozen spec's contract surface (R04)."""
    return SimpleNamespace(required_jobs=required)


def _decision(
    kit: Vocabulary,
    required: tuple[str, ...],
    observed: dict[str, str | None],
    *,
    waived: frozenset[str] = frozenset(),
    pending: bool = False,
    subject: str | None = OBSERVED_HEAD,
    now: datetime | None = NOW,
) -> VerificationDecision:
    """One observe_verification call through *kit*'s vocabulary."""
    return observe_verification(
        spec=_spec(required),
        provider=kit.producer,
        observations=observed,
        candidate_sha=CANDIDATE,
        subject_head_oid=subject or "",
        pending=pending,
        success_conclusions=kit.success,
        code_failure_conclusions=kit.code_failure,
        infra_conclusions=kit.infra,
        waived_conclusions=waived,
        now=now,
    )


def _projection(decision: VerificationDecision) -> tuple:
    """The decision modulo the producer identity.

    ``producer`` is the one field that is the provider's SIGNATURE by design
    (R02: an operator must be able to tell WHO claimed a check passed) —
    every other field must be identical for identical normalized inputs.
    """
    verdict = decision.verdict
    return (
        decision.outcome,
        decision.failing,
        decision.reason,
        None
        if verdict is None
        else (verdict.status, verdict.tested_oid, verdict.summary, verdict.surface),
    )


# ----------------------------------------------------------------------
# Import boundary: the use case is core, like consistency before it
# ----------------------------------------------------------------------


class TestImportBoundary:
    def test_usecases_never_imports_integrations_or_gateway(self):
        """ADR-0027 slice 2's enforceable half: forge.runs.usecases is core —
        no provider SDK package (forge.integrations.*) and no gateway
        transport (forge.gateway.*) may appear in its imports."""
        source = Path(__import__("forge.runs.usecases", fromlist=["__file__"]).__file__).read_text(
            encoding="utf-8"
        )
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
            ), f"forge.runs.usecases must not import {name!r} (ADR-0027 boundary)"
        # ...and it builds the verdict on the shared R02 vocabulary.
        assert any(name == "forge.runs.verification" for name in imported)


# ----------------------------------------------------------------------
# The use case itself, through every provider's vocabulary
# ----------------------------------------------------------------------


@pytest.mark.parametrize("kit", KITS, ids=["github", "azure", "gitlab"])
class TestObserveVerification:
    """The A01 verdict rules, identical under every provider's vocabulary."""

    def test_proven_required_checks_verify(self, kit):
        decision = _decision(kit, ("tests",), {"tests": kit.proven})

        assert decision.outcome == OUTCOME_REVIEW
        assert decision.verified is True
        assert decision.verdict is not None
        assert decision.verdict.status == "passed"
        assert decision.verdict.tested_oid == OBSERVED_HEAD  # the provider-verified head
        assert decision.verdict.summary == "required checks succeeded for the tested sha"
        assert decision.reason == "required checks passed"

    def test_green_optional_never_substitutes_for_a_missing_required(self, kit):
        """A01 AC1: the required `tests` check never ran — a green optional
        check proves nothing and keeps the run waiting (honest unknown)."""
        decision = _decision(kit, ("tests",), {"documentation": kit.proven})

        assert decision.outcome == OUTCOME_WAIT
        assert decision.verified is False
        assert decision.verdict is not None
        assert decision.verdict.status == "unknown"
        assert "tests" in decision.verdict.summary and "not run" in decision.verdict.summary

    def test_inconclusive_required_conclusion_is_unknown_not_green(self, kit):
        """A01 AC2: a skipped required check is no proof without the waiver."""
        decision = _decision(kit, ("tests",), {"tests": kit.inconclusive})

        assert decision.outcome == OUTCOME_WAIT
        assert decision.verdict is not None and decision.verdict.status == "unknown"

    def test_waiver_is_the_only_bridge_over_an_inconclusive_required(self, kit):
        decision = _decision(
            kit,
            ("tests", "lint"),
            {"tests": kit.inconclusive, "lint": kit.proven},
            waived=frozenset({kit.inconclusive}),
        )

        assert decision.outcome == OUTCOME_REVIEW
        assert decision.verified is True

    def test_code_failure_is_a_repair_that_records_no_verdict(self, kit):
        """ADR-0008: a conclusion blaming the change drives the bounded
        repair loop — and, exactly as before the extraction, the pass
        records NO verification evidence (the repair is the answer)."""
        decision = _decision(kit, ("tests",), {"tests": kit.code_red})

        assert decision.outcome == OUTCOME_REPAIR
        assert decision.verdict is None
        assert decision.evidence() == {}
        assert decision.failing == ("tests",)

    def test_infra_failure_blocks_and_never_repairs(self, kit):
        """A01 AC3: the execution died — the honest unknown verdict is
        recorded, the offending check is named, and the repair budget is
        never spent."""
        decision = _decision(kit, ("tests",), {"tests": kit.infra_red})

        assert decision.outcome == OUTCOME_BLOCK
        assert decision.failing == ("tests",)
        assert decision.verdict is not None
        assert decision.verdict.status == "unknown"
        assert "tests" in decision.verdict.summary
        assert "cancelled or timed out" in decision.verdict.summary

    def test_no_ci_with_required_jobs_waits_never_reads_unverified(self, kit):
        """B07: a NON-EMPTY frozen required list makes missing checks a
        missing MANDATORY GATE — the run waits (the R17 deadline blocks
        it), never an unverified READY."""
        decision = _decision(kit, ("tests",), {})

        assert decision.outcome == OUTCOME_WAIT
        assert decision.verdict is None

    def test_no_ci_without_required_jobs_reviews_honestly_unverified(self, kit):
        """Best-effort mode (no frozen required checks): nothing observed
        → the not_configured verdict (R02: never presented as verified),
        bound to the CANDIDATE sha, walking to the review leg."""
        decision = _decision(kit, (), {})

        assert decision.outcome == OUTCOME_REVIEW
        assert decision.verified is False
        assert decision.verdict is not None
        assert decision.verdict.status == "not_configured"
        assert decision.verdict.tested_oid == CANDIDATE
        assert decision.reason == "no CI configured — unverified"

    def test_pending_records_no_verdict_at_all(self, kit):
        """Completeness: a check the provider still reports as running gets
        no verdict — the run keeps waiting for it to conclude."""
        decision = _decision(kit, ("tests",), {"tests": kit.proven}, pending=True)

        assert decision.outcome == OUTCOME_WAIT
        assert decision.verdict is None
        assert decision.evidence() == {}

    def test_empty_contract_makes_observed_checks_the_proof_set(self, kit):
        """A01 AC2: with NO frozen contract, every OBSERVED check is the
        proof set — a green surface verifies, a lone skipped one never does."""
        decision = _decision(kit, (), {"ci": kit.proven})
        assert decision.outcome == OUTCOME_REVIEW and decision.verified is True

        decision = _decision(kit, (), {"ci": kit.inconclusive})
        assert decision.outcome == OUTCOME_WAIT and decision.verified is False

    def test_verdict_binds_the_provider_head_with_candidate_fallback(self, kit):
        """ADR-0008: the verdict binds to the head the PROVIDER observed;
        a verdict for another commit never ships. No observed head → the
        candidate sha is the honest fallback."""
        decision = _decision(kit, ("tests",), {"tests": kit.proven}, subject=OBSERVED_HEAD)
        assert decision.verdict is not None and decision.verdict.tested_oid == OBSERVED_HEAD

        decision = _decision(kit, ("tests",), {"tests": kit.proven}, subject=None)
        assert decision.verdict is not None and decision.verdict.tested_oid == CANDIDATE

    def test_identical_inputs_decide_identically(self, kit):
        """The stamp comes from the caller's observation instant, so two
        identical passes produce byte-identical decisions (the parity
        suite's foundation)."""
        first = _decision(kit, ("tests",), {"tests": kit.proven, "lint": kit.code_red})
        second = _decision(kit, ("tests",), {"tests": kit.proven, "lint": kit.code_red})

        assert first == second

    def test_conclusion_matching_is_case_insensitive(self, kit):
        """The Azure vocabulary is camelCase (``partiallySucceeded``); the
        GitHub vocabulary is lowercase — matching never depends on case."""
        decision = _decision(kit, ("tests",), {"tests": kit.proven.upper()})
        assert decision.verified is True

        decision = _decision(kit, ("tests",), {"tests": kit.code_red.upper()})
        assert decision.outcome == OUTCOME_REPAIR


# ----------------------------------------------------------------------
# Cross-vocabulary parity: the translation happens at the adapter
# ----------------------------------------------------------------------


def _spell(kit: Vocabulary, observed: dict[str, str | None]) -> dict[str, str | None]:
    """Translate the scenario's semantic classes into the kit's native
    conclusions — what the provider adapter does before calling."""
    return {check: getattr(kit, cls) for check, cls in observed.items()}


PARITY_SCENARIOS: list[tuple[str, dict]] = [
    ("all required proven", dict(required=("tests",), observed={"tests": "proven"})),
    (
        "green optional never substitutes",
        dict(required=("tests",), observed={"documentation": "proven"}),
    ),
    (
        "inconclusive required is unproven",
        dict(required=("tests",), observed={"tests": "inconclusive"}),
    ),
    (
        "waiver flips the inconclusive",
        dict(
            required=("tests", "lint"),
            observed={"tests": "inconclusive", "lint": "proven"},
            waived=frozenset({"skipped"}),
        ),
    ),
    ("code failure repairs", dict(required=("tests",), observed={"tests": "code_red"})),
    ("infra failure blocks", dict(required=("tests",), observed={"tests": "infra_red"})),
    ("no CI observed", dict(required=("tests",), observed={})),
    (
        "pending keeps waiting",
        dict(required=("tests",), observed={"tests": "proven"}, pending=True),
    ),
    (
        "empty contract takes observed checks",
        dict(required=(), observed={"ci": "proven", "docs": "proven"}),
    ),
]


@pytest.mark.parametrize(
    ("name", "scenario"), PARITY_SCENARIOS, ids=[s[0] for s in PARITY_SCENARIOS]
)
class TestProviderParity:
    """The R27 property for this slice: identical normalized inputs yield
    identical decisions under EVERY provider's vocabulary — the conclusion
    translation happens at the adapter, the semantics happen once."""

    def test_all_three_vocabularies_decide_identically(self, name, scenario):
        github = _decision(
            KIT_GITHUB, scenario["required"], _spell(KIT_GITHUB, scenario["observed"])
        )
        azure = _decision(KIT_AZURE, scenario["required"], _spell(KIT_AZURE, scenario["observed"]))
        gitlab = _decision(
            KIT_GITLAB, scenario["required"], _spell(KIT_GITLAB, scenario["observed"])
        )

        assert _projection(github) == _projection(azure) == _projection(gitlab), name


# ----------------------------------------------------------------------
# The documented GitLab boundary — honesty over forced unification
# ----------------------------------------------------------------------


def _pipeline(status: str) -> Pipeline:
    return Pipeline.model_validate({"id": 9, "status": status})


def _job(name: str, status: str) -> Job:
    return Job.model_validate({"id": 1, "name": name, "status": status})


class TestGitLabBoundary:
    """GitLab's profile-based gate deliberately does NOT call
    observe_verification — its pipeline evidence is structurally different,
    and the semantics CONFLICT on the two inputs below. These pins make the
    conflict explicit so a future unification must reconcile them
    consciously, never silently flip a lane."""

    def test_empty_contract_semantics_are_opposite_by_design(self):
        # GitLab (R02): an empty profile NEVER verifies — a green pipeline
        # with no verification profile is recorded honestly UNVERIFIED.
        ok, reason = evaluate(_pipeline("success"), [], VerificationProfile(required_jobs=()))
        assert ok is True
        assert reason == "no verification profile configured (warning)"

        # A01: an empty contract makes every OBSERVED check the proof set —
        # the same green surface VERIFIES on the GitHub/Azure lanes.
        decision = _decision(KIT_GITHUB, (), {"ci": KIT_GITHUB.proven})
        assert decision.outcome == OUTCOME_REVIEW
        assert decision.verified is True

    def test_missing_required_job_blocks_on_gitlab_and_waits_on_a01_lanes(self):
        # GitLab evaluates a FINISHED pipeline: a missing required job is a
        # terminal quality-contract block (CI concluded — waiting is pointless).
        ok, reason = evaluate(
            _pipeline("success"),
            [_job("docs", "success")],
            VerificationProfile(required_jobs=("tests",)),
        )
        assert ok is False
        assert "tests" in reason

        # An A01 lane observes a surface that may still be registering: the
        # same input KEEPS WAITING (the R17 deadline is the bound).
        decision = _decision(KIT_GITHUB, ("tests",), {"docs": KIT_GITHUB.proven})
        assert decision.outcome == OUTCOME_WAIT


# ----------------------------------------------------------------------
# The two real gates agree: identical decisions for equivalent evidence
# ----------------------------------------------------------------------

GITHUB_REPO = "acme/acme-widget"
GITHUB_ISSUE = 42
AZURE_WORK_ITEM = 142


@dataclass(frozen=True)
class GateOutcome:
    """The decision-level result of one real gate pass (the shared
    semantics; the producer identity and surface key spellings stay
    provider-native by design)."""

    decision: str
    verification_status: str | None
    tested_oid: str | None
    producer: str | None
    fragment_keys: frozenset[str]
    status: str
    reason: str


def _classify(status: str, reason: str) -> str:
    if status == FlowStatus.READY_FOR_HUMAN.value:
        return "ready_unverified" if reason.startswith("unverified") else "ready_verified"
    if status == FlowStatus.WAITING_CI.value:
        return "waiting"
    if status == FlowStatus.BLOCKED.value:
        return (
            "blocked_infra"
            if reason.startswith("verification_infrastructure")
            else "blocked_quality"
        )
    return f"other:{status}"


async def _get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


def _outcome(run: FlowRun) -> GateOutcome:
    verification = (run.evidence or {}).get("verification") or {}
    reason = run.status_reason or ""
    return GateOutcome(
        _classify(run.status, reason),
        verification.get("status"),
        verification.get("tested_oid"),
        verification.get("producer"),
        frozenset(verification),
        run.status,
        reason,
    )


async def _github_gate(db, conclusions: list[tuple[str, str | None]]) -> GateOutcome:
    """Drive the real GitHub gate: start_run → /go → waiting_ci → observe."""
    from tests.fixtures.fake_github import FakeGitHub
    from tests.test_github_runs import (
        go as github_go,
        make_service as make_github_service,
        make_settings as github_settings,
        make_stack as make_github_stack,
        start as github_start,
        workflow_run,
    )

    fake = FakeGitHub()
    fake.seed_repo(GITHUB_REPO, {"src/app.py": "print('hi')\n"})
    fake.heads[GITHUB_REPO]["main"] = "1" * 40
    fake.seed_issue(GITHUB_REPO, GITHUB_ISSUE, "Add password reset", "Users cannot reset.")
    service = make_github_service(
        db,
        fake,
        settings=github_settings(FORGE_REQUIRED_JOBS="tests"),
        stack=make_github_stack(fake),
    )
    run_id = await github_start(service)
    await github_go(service, run_id)
    candidate = (await _get_run(db, run_id)).candidate_shas[-1]
    fake.seed_workflow_runs(
        [workflow_run(candidate, name, conclusion) for name, conclusion in conclusions]
    )

    await service.evaluate_waiting_ci_one(run_id)

    return _outcome(await _get_run(db, run_id))


async def _azure_gate(db, results: list[tuple[str, str | None]]) -> GateOutcome:
    """Drive the real Azure DevOps gate: start_run → /go → observe."""
    from tests.test_azure_runs import (
        FakeAzureDevOps,
        drive_to_waiting_ci as azure_drive,
        make_service as make_azure_service,
        make_settings as azure_settings,
        make_stack as make_azure_stack,
    )

    fake = FakeAzureDevOps()
    fake.seed_work_item(AZURE_WORK_ITEM, "Ship the flux capacitor", "<p>Users cannot reset.</p>")
    service = make_azure_service(
        db,
        fake,
        settings=azure_settings(FORGE_REQUIRED_JOBS="tests"),
        stack=make_azure_stack(fake),
    )
    run_id, candidate = await azure_drive(service, fake)
    for name, result in results:
        fake.seed_build(source_version=candidate, definition_name=name, result=result)

    await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

    return _outcome(await _get_run(db, run_id))


GATE_SCENARIOS = [
    # (id, github checks, azure build results, decision class, verdict status)
    # The check NAME is part of the normalized input — identical on both
    # adapters; only the conclusion SPELLING is provider-native.
    ("green", [("tests", "success")], [("tests", "succeeded")], "ready_verified", "passed"),
    ("red", [("tests", "failure")], [("tests", "failed")], "blocked_quality", None),
    ("infra", [("tests", "cancelled")], [("tests", "canceled")], "blocked_infra", "unknown"),
    ("no CI configured", [], [], "waiting", None),
    (
        "unproven required",
        [("documentation", "success")],
        [("documentation", "succeeded")],
        "waiting",
        "unknown",
    ),
]


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


@pytest.mark.parametrize(
    ("name", "github_checks", "azure_results", "decision", "verification_status"),
    GATE_SCENARIOS,
    ids=[scenario[0] for scenario in GATE_SCENARIOS],
)
class TestGatesAgree:
    """The GitHub and Azure gates — now two adapters over ONE use case —
    produce identical decisions for equivalent evidence (the R02 property
    that makes provider-specific verdict drift impossible)."""

    async def test_gates_decide_identically(
        self, db, name, github_checks, azure_results, decision, verification_status
    ):
        github = await _github_gate(db, conclusions=github_checks)
        azure = await _azure_gate(db, results=azure_results)

        assert github.decision == azure.decision == decision, name
        assert github.verification_status == azure.verification_status == verification_status, name
        # Both bind the verdict to the provider-verified candidate (or record
        # nothing at all — the code-failure repair case).
        assert github.tested_oid == azure.tested_oid, name
        # The fragment is the ONE unified R02 shape on both adapters — and
        # the code-failure repair case records NO fragment at all (the
        # repair is the answer), identically on both.
        if verification_status is not None:
            assert github.fragment_keys >= {"status", "tested_oid", "observed_at", "producer"}
            assert azure.fragment_keys >= {"status", "tested_oid", "observed_at", "producer"}
            # The producer identity stays provider-native — the adapter's
            # signature.
            assert github.producer == PRODUCER_GITHUB_CHECKS
            assert azure.producer == PRODUCER_AZURE_BUILD
        else:
            assert not github.fragment_keys and not azure.fragment_keys

    async def test_ready_reasons_are_word_for_word_shared(
        self, db, name, github_checks, azure_results, decision, verification_status
    ):
        if not decision.startswith("ready"):
            pytest.skip(f"{name} does not reach the ready transition")
        github = await _github_gate(db, conclusions=github_checks)
        azure = await _azure_gate(db, results=azure_results)

        # The slice-1 property holds through the slice-2 use case: the ready
        # reason is byte-identical for the same inputs on every provider.
        assert github.reason == azure.reason
        assert ("unverified — " in github.reason) == (decision == "ready_unverified")
