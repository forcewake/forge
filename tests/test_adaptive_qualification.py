"""R32-13 — qualify one runtime-recipe + harness combination end to end.

The issue's refusals, pinned as tests: a qualified combination is ONE
frozen record whose digest changes on ANY mutation (never "close
enough"); the installed toolchain either matches the declared closure
EXACTLY or the fingerprint leg refuses, listing every divergence; the
test reports reconcile per test project so a failing project, a
missing report and a stale leftover are each surfaced distinctly — a
missing report is NEVER zero failures and a passing TRX beside them
cancels nothing; the egress probe PAIR distinguishes denied-by-policy
from denied-unexpectedly from declared-but-not-enforced (the research
case: disable the network policy while RETAINING its env variable —
the trace must detect the difference); and the assembled trace stays
honest — any leg unknown or partial keeps the verdict at
``not_qualified`` with a problem line saying why.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from forge.adaptive.capability_profiles import CAPABILITIES
from forge.adaptive.qualification import (
    ACCEPTANCE_MAX_SHARE,
    EGRESS_DENIED_BY_POLICY,
    EGRESS_DENIED_UNEXPECTED,
    EGRESS_PERMITTED_BLOCKED,
    EGRESS_PERMITTED_REACHABLE,
    EGRESS_POLICY_ABSENT,
    EGRESS_POLICY_NOT_ENFORCED,
    EGRESS_PROBE_INDETERMINATE,
    EgressPolicy,
    ExpectedReport,
    ExpectedReports,
    FingerprintDivergence,
    FingerprintMismatch,
    HarnessFeatureSupport,
    LAYER_ORDER,
    LayerBinding,
    ProbeDestination,
    QualificationLayer,
    QualificationProfile,
    QualificationTrace,
    ReportVerdict,
    TRACE_SCHEMA,
    acceptance_within_budget,
    assemble_qualification_trace,
    profile_staleness,
    probe_egress_pair,
    reconcile_reports,
    recipe_document_digest,
    verify_installed_fingerprints,
)
from forge.runs.execution_profile import RUNTIME_RECIPES

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "qualification"

#: The identity of the CURRENT qualification run the fixtures encode.
CANDIDATE_ID = "cand-e2e-226"
CURRENT_BUNDLE = "9a6523b1570416565c0dd37ea4901dabadd91c888aa4461a51cf76a34a59e3f3"
STALE_BUNDLE = "f377c36ae0ace28509cbe1660ab2598920af9e764e9bb96ade7e9abdf0fbfbd4"

_API_TRX = "forge_Api.Tests_net9.0.trx"
_DOMAIN_TRX = "forge_Domain.Tests_net9.0.trx"
_INFRA_TRX = "forge_Infra.Tests_net9.0.trx"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _features(**overrides: bool) -> HarnessFeatureSupport:
    verdicts = {"interrupt": True, "steer": False, "restore": True}
    verdicts.update(overrides)
    return HarnessFeatureSupport(**verdicts)


def _profile(**overrides: object) -> QualificationProfile:
    fields: dict[str, object] = {
        "lane_code_ref": "0d9f7c1e2b",
        "recipe_id": "dotnet-9",
        "recipe_digest": recipe_document_digest(RUNTIME_RECIPES["dotnet-9"]),
        "harness_id": "claude-code",
        "model_route": "zai-anthropic-gateway",
        "credential_mode": "byok-env-token",
        "test_invocation": ("dotnet", "test", "--no-build"),
        "bootstrap_closure": (
            ("dotnet-sdk", "9.0.100"),
            ("forge", "0.32.0"),
        ),
        "feature_support": _features(),
        "layer_bindings": (
            LayerBinding(
                layer=QualificationLayer.SMOKE,
                evidence=("tests/test_adaptive_driver_claude_sdk.py",),
            ),
            LayerBinding(
                layer=QualificationLayer.CONTRACT,
                evidence=("tests/test_adaptive_contracts.py",),
            ),
        ),
    }
    fields.update(overrides)
    return QualificationProfile(**fields)  # type: ignore[arg-type]


def _expected_reports() -> ExpectedReports:
    return ExpectedReports(
        reports=(
            ExpectedReport(
                test_project="Forge.Api.Tests",
                report_path=_API_TRX,
                candidate_id=CANDIDATE_ID,
                bundle_digest=CURRENT_BUNDLE,
            ),
            ExpectedReport(
                test_project="Forge.Domain.Tests",
                report_path=_DOMAIN_TRX,
                candidate_id=CANDIDATE_ID,
                bundle_digest=CURRENT_BUNDLE,
            ),
            ExpectedReport(
                test_project="Forge.Infra.Tests",
                report_path=_INFRA_TRX,
                candidate_id=CANDIDATE_ID,
                bundle_digest=CURRENT_BUNDLE,
            ),
        )
    )


def _write_report(
    found_dir: Path,
    report_path: str,
    *,
    total: int,
    executed: int,
    passed: int,
    failed: int,
    candidate_id: str = CANDIDATE_ID,
    bundle_digest: str = CURRENT_BUNDLE,
) -> None:
    """Write one TRX + its identity sidecar into *found_dir*."""
    (found_dir / report_path).write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<TestRun id="00000000-0000-4000-8000-000000000000" '
        'name="synthetic" '
        'xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">\n'
        '  <ResultSummary outcome="Completed">\n'
        f'    <Counters total="{total}" executed="{executed}" passed="{passed}" '
        f'failed="{failed}" error="0" timeout="0" aborted="0" inconclusive="0" '
        'passedRunLevel="0" failedRunLevel="0" />\n'
        "  </ResultSummary>\n"
        "</TestRun>\n",
        encoding="utf-8",
    )
    (found_dir / (report_path + ".identity.json")).write_text(
        json.dumps({"candidate_id": candidate_id, "bundle_digest": bundle_digest}),
        encoding="utf-8",
    )


def _connectors(reachable: set[str]):
    """A connector double: hosts in *reachable* connect, everything else
    refuses (OSError) — every verdict proven without a network."""

    def dial(host: str, port: int, timeout_s: float) -> None:
        if host in reachable:
            return
        raise OSError(f"refused: {host}")

    return dial


_PERMITTED = "api.z.ai:443"
_DENIED = "example.com:443"
_POLICY = EgressPolicy(allowlist=("api.z.ai", "nuget.org"))
_ENV_HOOK = {"FORGE_EGRESS_ALLOWLIST": "api.z.ai,nuget.org"}


# ---------------------------------------------------------------------------
# The profile record and its digest
# ---------------------------------------------------------------------------


class TestQualificationProfileDigest:
    def test_deterministic_over_identical_records(self):
        assert _profile().qualification_digest == _profile().qualification_digest

    @pytest.mark.parametrize(
        "field,value",
        [
            ("lane_code_ref", "0d9f7c1e3c"),
            ("recipe_id", "python-3-13"),
            ("recipe_digest", "b" * 64),
            ("harness_id", "codex"),
            ("model_route", "openai-direct"),
            ("credential_mode", "platform-key"),
            ("test_invocation", ("dotnet", "test")),
            (
                "bootstrap_closure",
                (("dotnet-sdk", "9.0.101"), ("forge", "0.32.0")),
            ),
            ("feature_support", _features(steer=True)),
            ("feature_support", _features(restore=False)),
            ("feature_support", _features(interrupt=False)),
            (
                "layer_bindings",
                (
                    LayerBinding(layer=QualificationLayer.SMOKE, evidence=("tests/other.py",)),
                    LayerBinding(
                        layer=QualificationLayer.CONTRACT,
                        evidence=("tests/test_adaptive_contracts.py",),
                    ),
                ),
            ),
        ],
    )
    def test_any_field_change_is_a_different_digest(self, field, value):
        baseline = _profile()
        mutated = replace(baseline, **{field: value})
        assert mutated.qualification_digest != baseline.qualification_digest

    def test_the_freeze_round_trip_preserves_the_record(self):
        profile = _profile()
        frozen = json.loads(json.dumps(profile.to_document()))
        thawed = QualificationProfile.from_document(frozen)
        assert thawed == profile
        assert thawed.qualification_digest == profile.qualification_digest

    def test_a_tampered_freeze_never_reproduces_the_digest(self):
        document = _profile().to_document()
        document["credential_mode"] = "attacker-key"
        assert (
            QualificationProfile.from_document(document).qualification_digest
            != _profile().qualification_digest
        )

    def test_bootstrap_closure_ordering_is_not_a_digest_axis(self):
        reordered = _profile(
            bootstrap_closure=(("forge", "0.32.0"), ("dotnet-sdk", "9.0.100")),
        )
        assert reordered.qualification_digest == _profile().qualification_digest


class TestQualificationProfileValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("lane_code_ref", ""),
            ("recipe_id", ""),
            ("recipe_digest", ""),
            ("harness_id", ""),
            ("model_route", ""),
            ("credential_mode", ""),
        ],
    )
    def test_the_identity_fields_are_non_empty(self, field, value):
        with pytest.raises(ValueError, match=field):
            _profile(**{field: value})

    def test_the_recipe_digest_must_be_a_sha256(self):
        with pytest.raises(ValueError, match="sha256"):
            _profile(recipe_digest="not-a-digest")

    def test_an_empty_test_invocation_is_unexecutable(self):
        with pytest.raises(ValueError, match="test_invocation"):
            _profile(test_invocation=())

    def test_a_duplicate_closure_pin_is_a_modelling_error(self):
        with pytest.raises(ValueError, match="duplicate"):
            _profile(
                bootstrap_closure=(("dotnet-sdk", "9.0.100"), ("dotnet-sdk", "9.0.101")),
            )

    def test_a_qualification_binds_at_least_one_layer(self):
        with pytest.raises(ValueError, match="at least one layer"):
            _profile(layer_bindings=())

    def test_layers_stack_bottom_up_with_no_gaps(self):
        with pytest.raises(ValueError, match="no gaps"):
            _profile(
                layer_bindings=(
                    LayerBinding(layer=QualificationLayer.SMOKE, evidence=("tests/s.py",)),
                    LayerBinding(layer=QualificationLayer.INTEGRATION, evidence=("tests/i.py",)),
                ),
            )

    def test_duplicate_layer_bindings_are_refused(self):
        with pytest.raises(ValueError, match="duplicate"):
            _profile(
                layer_bindings=(
                    LayerBinding(layer=QualificationLayer.SMOKE, evidence=("tests/s.py",)),
                    LayerBinding(layer=QualificationLayer.SMOKE, evidence=("tests/s2.py",)),
                ),
            )

    def test_staleness_flags_a_recipe_that_changed_since_qualification(self):
        problems = profile_staleness(_profile(recipe_digest="c" * 64))
        assert any("changed since the qualification" in problem for problem in problems)

    def test_staleness_flags_an_unknown_harness(self):
        problems = profile_staleness(_profile(harness_id="ghost-agent"))
        assert any("ghost-agent" in problem for problem in problems)

    def test_a_current_profile_is_not_stale(self):
        assert profile_staleness(_profile()) == ()


# ---------------------------------------------------------------------------
# The layer vocabulary
# ---------------------------------------------------------------------------


class TestLayerVocabulary:
    def test_the_canonical_stack_order(self):
        assert [layer.value for layer in LAYER_ORDER] == [
            "smoke",
            "contract",
            "integration",
            "acceptance",
        ]

    def test_a_binding_requires_the_enum_and_evidence(self):
        with pytest.raises(ValueError, match="QualificationLayer"):
            LayerBinding(layer="smoke", evidence=("tests/s.py",))
        with pytest.raises(ValueError, match="no evidence"):
            LayerBinding(layer=QualificationLayer.SMOKE, evidence=())

    def test_acceptance_within_the_curated_budget(self):
        ok, reason = acceptance_within_budget(3, 100)
        assert ok and "3/100" in reason

    def test_acceptance_at_the_boundary_is_still_within(self):
        ok, _ = acceptance_within_budget(50, 1000)  # exactly 5 %
        assert ok

    def test_acceptance_over_the_curated_cap_is_refused(self):
        ok, reason = acceptance_within_budget(60, 1000)
        assert not ok and f"{ACCEPTANCE_MAX_SHARE:.0%}" in reason

    def test_an_uncomputable_budget_is_refused(self):
        ok, reason = acceptance_within_budget(0, 0)
        assert not ok and "uncomputable" in reason


class TestHarnessFeatureSupport:
    def test_every_feature_is_stated_explicitly(self):
        support = HarnessFeatureSupport(interrupt=True, steer=False, restore=True)
        assert support.to_document() == {"interrupt": True, "steer": False, "restore": True}

    def test_an_unstated_feature_is_never_defaulted(self):
        document = {"interrupt": True, "steer": False}  # restore missing
        with pytest.raises(ValueError, match="restore"):
            HarnessFeatureSupport.from_document(document)

    def test_unknown_feature_names_are_refused(self):
        support = HarnessFeatureSupport(interrupt=False, steer=False, restore=False)
        with pytest.raises(ValueError, match="vocabulary"):
            support.supports("vibe_check")

    def test_the_feature_vocabulary_maps_onto_the_capability_vocabulary(self):
        mapping = {
            "interrupt": "interrupt",
            "steer": "live_input",
            "restore": "checkpoint_export",
        }
        assert set(mapping.values()) <= set(CAPABILITIES)
        support = HarnessFeatureSupport(interrupt=True, steer=False, restore=True)
        assert support.supports("interrupt") and not support.supports("steer")


# ---------------------------------------------------------------------------
# Installed fingerprints
# ---------------------------------------------------------------------------


class TestInstalledFingerprints:
    _DECLARED = {"dotnet-sdk": "9.0.100", "forge": "0.32.0", "node": "22.11.0"}

    def test_an_exact_installation_matches(self):
        match = verify_installed_fingerprints(self._DECLARED, dict(self._DECLARED))
        assert match.fingerprints == tuple(sorted(self._DECLARED.items()))

    def test_a_version_mismatch_refuses(self):
        installed = dict(self._DECLARED, **{"dotnet-sdk": "9.0.101"})
        with pytest.raises(FingerprintMismatch) as excinfo:
            verify_installed_fingerprints(self._DECLARED, installed)
        assert "dotnet-sdk: declared '9.0.100', installed '9.0.101'" in str(excinfo.value)

    def test_a_missing_pin_refuses(self):
        installed = dict(self._DECLARED)
        del installed["forge"]
        with pytest.raises(FingerprintMismatch, match="NOT INSTALLED"):
            verify_installed_fingerprints(self._DECLARED, installed)

    def test_an_undeclared_installation_refuses(self):
        with pytest.raises(FingerprintMismatch, match="NOT DECLARED"):
            verify_installed_fingerprints(self._DECLARED, dict(self._DECLARED, ghost="1.0"))

    def test_every_divergence_is_listed_no_partial_pass(self):
        installed = dict(self._DECLARED, **{"dotnet-sdk": "10.0.100", "ghost": "1.0"})
        del installed["node"]
        with pytest.raises(FingerprintMismatch) as excinfo:
            verify_installed_fingerprints(self._DECLARED, installed)
        divergences = excinfo.value.divergences
        assert {(d.kind, d.name) for d in divergences} == {
            ("version_mismatch", "dotnet-sdk"),
            ("missing_installed", "node"),
            ("undeclared_installed", "ghost"),
        }
        # each divergence named in the refusal message, none absorbed
        for divergence in divergences:
            assert divergence.name in str(excinfo.value)

    def test_a_single_mismatch_among_matches_still_refuses(self):
        installed = dict(self._DECLARED, **{"forge": "0.33.0"})
        with pytest.raises(FingerprintMismatch):
            verify_installed_fingerprints(self._DECLARED, installed)

    def test_a_divergence_outside_the_vocabulary_is_a_modelling_error(self):
        with pytest.raises(ValueError, match="vocabulary"):
            FingerprintDivergence(kind="close_enough", name="x", declared="1", installed="1")


# ---------------------------------------------------------------------------
# Report reconciliation — the 3-project fixture scenario
# ---------------------------------------------------------------------------


class TestReportReconciliation:
    def _found_dir(self, tmp_path: Path) -> Path:
        found = tmp_path / "reports"
        found.mkdir()
        for name in (_API_TRX, _DOMAIN_TRX):
            shutil.copy(FIXTURES / name, found / name)
            shutil.copy(FIXTURES / f"{name}.identity.json", found / f"{name}.identity.json")
        # Forge.Infra.Tests is deliberately absent: its report is MISSING.
        return found

    def test_the_three_project_scenario_surfaces_each_problem_distinctly(self, tmp_path):
        reconciliation = reconcile_reports(_expected_reports(), self._found_dir(tmp_path))
        by_project = {v.test_project: v for v in reconciliation.verdicts}
        assert by_project["Forge.Api.Tests"].verdict == "failed"
        assert by_project["Forge.Api.Tests"].failed == 2
        assert by_project["Forge.Infra.Tests"].verdict == "missing_report"
        assert by_project["Forge.Domain.Tests"].verdict == "stale_report"
        # the aggregate is NOT green, and every problem is listed — the
        # passing 12/12 TRX inside the stale Domain report cancels nothing
        assert not reconciliation.is_green
        problems = reconciliation.problems
        assert any("Forge.Api.Tests" in p and "failed" in p for p in problems)
        assert any("Forge.Infra.Tests" in p and "missing_report" in p for p in problems)
        assert any("Forge.Domain.Tests" in p and "stale_report" in p for p in problems)

    def test_a_missing_report_is_never_zero_failures(self, tmp_path):
        reconciliation = reconcile_reports(_expected_reports(), self._found_dir(tmp_path))
        infra = next(v for v in reconciliation.verdicts if v.test_project == "Forge.Infra.Tests")
        assert infra.verdict == "missing_report"
        assert infra.failed is None  # unknown, NOT zero
        assert infra.total is None and infra.executed is None and infra.passed is None
        assert "never zero failures" in infra.detail

    def test_a_failing_project_cannot_disappear_behind_a_passing_trx(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _DOMAIN_TRX, total=12, executed=12, passed=12, failed=0)
        _write_report(found, _API_TRX, total=12, executed=12, passed=10, failed=2)
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Domain.Tests",
                    report_path=_DOMAIN_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        reconciliation = reconcile_reports(expected, found)
        assert not reconciliation.is_green
        assert reconciliation.problems == (
            "Forge.Api.Tests: failed — 2 failing test(s) of 12 executed",
        )

    def test_a_stale_leftover_names_both_identities(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(
            found,
            _DOMAIN_TRX,
            total=12,
            executed=12,
            passed=12,
            failed=0,
            candidate_id="cand-e2e-000",  # the PREVIOUS run's identity
            bundle_digest=STALE_BUNDLE,
        )
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Domain.Tests",
                    report_path=_DOMAIN_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        verdict = reconcile_reports(expected, found).verdicts[0]
        assert verdict.verdict == "stale_report"
        assert verdict.failed is None
        assert "cand-e2e-000" in verdict.detail and CANDIDATE_ID in verdict.detail

    def test_a_report_without_an_identity_sidecar_is_a_leftover(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _API_TRX, total=4, executed=4, passed=4, failed=0)
        (found / f"{_API_TRX}.identity.json").unlink()
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        verdict = reconcile_reports(expected, found).verdicts[0]
        assert verdict.verdict == "stale_report"
        assert "identity" in verdict.detail

    def test_an_unparseable_report_is_unknown_never_green(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        (found / _API_TRX).write_text("<not-a-trx>", encoding="utf-8")
        (found / f"{_API_TRX}.identity.json").write_text(
            json.dumps({"candidate_id": CANDIDATE_ID, "bundle_digest": CURRENT_BUNDLE}),
            encoding="utf-8",
        )
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        verdict = reconcile_reports(expected, found).verdicts[0]
        assert verdict.verdict == "unparseable_report"
        assert verdict.failed is None and verdict.total is None

    def test_a_report_that_executed_nothing_proves_nothing(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _API_TRX, total=4, executed=0, passed=0, failed=0)
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        verdict = reconcile_reports(expected, found).verdicts[0]
        assert verdict.verdict == "failed"
        assert "zero tests" in verdict.detail

    def test_an_all_green_reconciliation_is_green_with_counts(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _API_TRX, total=12, executed=12, passed=12, failed=0)
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        reconciliation = reconcile_reports(expected, found)
        assert reconciliation.is_green
        assert reconciliation.problems == ()
        assert reconciliation.verdicts[0].verdict == "passed"
        assert reconciliation.verdicts[0].passed == 12

    def test_expected_reports_refuse_overlaps_and_emptiness(self):
        one = ExpectedReport(
            test_project="A", report_path="a.trx", candidate_id="c", bundle_digest="d"
        )
        with pytest.raises(ValueError, match="at least one"):
            ExpectedReports(reports=())
        with pytest.raises(ValueError, match="per test project"):
            ExpectedReports(reports=(one, replace(one, report_path="b.trx")))
        with pytest.raises(ValueError, match="per report path"):
            ExpectedReports(reports=(one, replace(one, test_project="B")))

    def test_an_expected_report_names_every_binding(self):
        with pytest.raises(ValueError, match="candidate_id"):
            ExpectedReport(
                test_project="A", report_path="a.trx", candidate_id="", bundle_digest="d"
            )

    def test_a_verdict_outside_the_vocabulary_is_refused(self):
        with pytest.raises(ValueError, match="vocabulary"):
            ReportVerdict(test_project="A", report_path="a.trx", verdict="greenish", detail="")

    def test_of_path_finds_the_expected_row(self):
        assert _expected_reports().of_path(_API_TRX) is not None
        assert _expected_reports().of_path("nope.trx") is None


# ---------------------------------------------------------------------------
# The egress control-probe pair
# ---------------------------------------------------------------------------


class TestEgressProbePair:
    def test_the_consistent_pair(self):
        pair = probe_egress_pair(
            _POLICY,
            _PERMITTED,
            _DENIED,
            env=_ENV_HOOK,
            connector=_connectors({"api.z.ai"}),
        )
        assert pair.permitted.status == EGRESS_PERMITTED_REACHABLE
        assert pair.denied.status == EGRESS_DENIED_BY_POLICY
        assert pair.denied.connection_outcome == "unreachable"
        assert pair.consistent and pair.problems == ()
        # the honesty bound rides the verdict, verify_network_egress style
        assert "may exist, never that it does" in pair.denied.detail
        assert "producer" in pair.permitted.detail  # the producer evidence

    def test_policy_disabled_with_env_retained_is_detected(self):
        """The research case: disable the network policy while RETAINING
        its env variable — the denied destination now ANSWERS, and the
        pair must say policy_declared_but_not_enforced, not verified."""
        pair = probe_egress_pair(
            _POLICY,
            _PERMITTED,
            _DENIED,
            env=_ENV_HOOK,  # the env hook RETAINED
            connector=_connectors({"api.z.ai", "example.com"}),  # filter gone
        )
        assert pair.policy_declared  # the declaration survived
        assert pair.denied.status == EGRESS_POLICY_NOT_ENFORCED
        assert pair.denied.connection_outcome == "connected"
        assert not pair.consistent
        assert any("not enforced" in problem for problem in pair.problems)

    def test_a_denial_without_the_policy_is_unexpected(self):
        pair = probe_egress_pair(
            _POLICY,
            _PERMITTED,
            _DENIED,
            env={"FORGE_LANE_PROFILE": "v2"},  # no egress hook at all
            connector=_connectors({"api.z.ai"}),  # yet the dial still refused
        )
        assert pair.denied.status == EGRESS_DENIED_UNEXPECTED
        assert "something else" in pair.denied.detail
        assert not pair.consistent

    def test_no_policy_and_an_open_destination_is_policy_absent(self):
        pair = probe_egress_pair(
            _POLICY,
            _PERMITTED,
            _DENIED,
            env={},
            connector=_connectors({"api.z.ai", "example.com"}),
        )
        assert pair.denied.status == EGRESS_POLICY_ABSENT
        assert not pair.consistent

    def test_an_allowlisted_denied_destination_cannot_falsify(self):
        pair = probe_egress_pair(
            _POLICY,
            _PERMITTED,
            "nuget.org:443",  # the denied probe target is actually granted
            env=_ENV_HOOK,
            connector=_connectors({"api.z.ai", "nuget.org"}),
        )
        assert pair.denied.status == EGRESS_PROBE_INDETERMINATE
        assert not pair.consistent

    def test_a_blocked_permitted_destination_breaks_producer_traffic(self):
        pair = probe_egress_pair(
            _POLICY,
            _PERMITTED,
            _DENIED,
            env=_ENV_HOOK,
            connector=_connectors(set()),  # everything refuses
        )
        assert pair.permitted.status == EGRESS_PERMITTED_BLOCKED
        assert any("permitted leg" in problem for problem in pair.problems)

    def test_a_reachable_destination_the_policy_never_grants_is_mis_specified(self):
        pair = probe_egress_pair(
            _POLICY,
            "evil.example.com:443",  # reachable, but not in the allowlist
            _DENIED,
            env=_ENV_HOOK,
            connector=_connectors({"evil.example.com"}),
        )
        assert pair.permitted.status == EGRESS_PERMITTED_REACHABLE
        assert not pair.consistent
        assert any("mis-specified" in problem for problem in pair.problems)

    def test_destination_parsing(self):
        assert str(ProbeDestination.of("example.com:443")) == "example.com:443"
        assert ProbeDestination.of(ProbeDestination("h", 1)).port == 1
        with pytest.raises(ValueError, match="host:port"):
            ProbeDestination.of("no-port")
        with pytest.raises(ValueError, match="port"):
            ProbeDestination.of("host:https")

    def test_a_policy_declares_its_patterns(self):
        with pytest.raises(ValueError, match="patterns"):
            EgressPolicy(allowlist=())
        assert _POLICY.permits("api.z.ai")
        assert not _POLICY.permits("example.com")


# ---------------------------------------------------------------------------
# The assembled trace
# ---------------------------------------------------------------------------


class TestQualificationTrace:
    def _green_reports(self, tmp_path: Path) -> object:
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _API_TRX, total=12, executed=12, passed=12, failed=0)
        return reconcile_reports(
            ExpectedReports(
                reports=(
                    ExpectedReport(
                        test_project="Forge.Api.Tests",
                        report_path=_API_TRX,
                        candidate_id=CANDIDATE_ID,
                        bundle_digest=CURRENT_BUNDLE,
                    ),
                )
            ),
            found,
        )

    def _green_egress(self) -> object:
        return probe_egress_pair(
            _POLICY,
            _PERMITTED,
            _DENIED,
            env=_ENV_HOOK,
            connector=_connectors({"api.z.ai"}),
        )

    def test_every_leg_passing_qualifies(self, tmp_path):
        trace = assemble_qualification_trace(
            _profile(),
            fingerprint=verify_installed_fingerprints(
                {"dotnet-sdk": "9.0.100"}, {"dotnet-sdk": "9.0.100"}
            ),
            reports=self._green_reports(tmp_path),
            egress=self._green_egress(),
            started_at="2026-09-23T10:00:00+00:00",
            finished_at="2026-09-23T10:09:00+00:00",
        )
        assert trace.schema == TRACE_SCHEMA == "forge.qualification.trace/1"
        assert trace.verdict == "qualified"
        assert trace.problems == ()
        assert trace.profile_digest == _profile().qualification_digest

    @pytest.mark.parametrize(
        "drop",
        ["fingerprint", "reports", "egress", "timestamps"],
    )
    def test_any_unknown_or_partial_leg_is_not_qualified(self, tmp_path, drop):
        legs: dict[str, object] = {
            "fingerprint": verify_installed_fingerprints(
                {"dotnet-sdk": "9.0.100"}, {"dotnet-sdk": "9.0.100"}
            ),
            "reports": self._green_reports(tmp_path),
            "egress": self._green_egress(),
            "started_at": "2026-09-23T10:00:00+00:00",
            "finished_at": "2026-09-23T10:09:00+00:00",
        }
        if drop == "fingerprint":
            legs["fingerprint"] = None
        elif drop == "reports":
            legs["reports"] = None
        elif drop == "egress":
            legs["egress"] = None
        elif drop == "timestamps":
            legs["started_at"] = ""
            legs["finished_at"] = ""
        trace = assemble_qualification_trace(_profile(), **legs)
        assert trace.verdict == "not_qualified"
        assert trace.problems  # the honesty line names the partial leg
        joined = "\n".join(trace.problems)
        assert "partial" in joined or "unknown" in joined

    def test_a_refused_fingerprint_is_recorded_not_swallowed(self):
        try:
            verify_installed_fingerprints({"dotnet-sdk": "9.0.100"}, {"dotnet-sdk": "9.0.101"})
            raise AssertionError("expected FingerprintMismatch")
        except FingerprintMismatch as mismatch:
            trace = assemble_qualification_trace(_profile(), fingerprint=mismatch)
        assert trace.fingerprint_status == "mismatch"
        assert any("dotnet-sdk" in problem for problem in trace.problems)
        assert trace.verdict == "not_qualified"

    def test_the_three_project_scenario_end_to_end_is_not_qualified(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        for name in (_API_TRX, _DOMAIN_TRX):
            shutil.copy(FIXTURES / name, found / name)
            shutil.copy(FIXTURES / f"{name}.identity.json", found / f"{name}.identity.json")
        trace = assemble_qualification_trace(
            _profile(),
            fingerprint=verify_installed_fingerprints(
                {"dotnet-sdk": "9.0.100"}, {"dotnet-sdk": "9.0.100"}
            ),
            reports=reconcile_reports(_expected_reports(), found),
            egress=probe_egress_pair(
                _POLICY,
                _PERMITTED,
                _DENIED,
                env=_ENV_HOOK,
                connector=_connectors({"api.z.ai", "example.com"}),  # unenforced
            ),
            started_at="2026-09-23T10:00:00+00:00",
            finished_at="2026-09-23T10:09:00+00:00",
        )
        assert trace.verdict == "not_qualified"
        joined = "\n".join(trace.problems)
        # every problem surfaces distinctly in ONE trace: the failing
        # project, the missing report, the stale leftover AND the
        # unenforced egress policy
        assert "Forge.Api.Tests" in joined and "failed" in joined
        assert "Forge.Infra.Tests" in joined and "missing_report" in joined
        assert "Forge.Domain.Tests" in joined and "stale_report" in joined
        assert "policy_declared_but_not_enforced" in joined

    def test_unsupported_harness_features_are_explicit_in_profile_and_trace(self, tmp_path):
        profile = _profile(feature_support=_features(steer=False, restore=False))
        document = profile.to_document()
        assert document["feature_support"] == {
            "interrupt": True,
            "steer": False,  # unsupported, STATED — never inferred
            "restore": False,
        }
        trace = assemble_qualification_trace(
            profile,
            fingerprint=verify_installed_fingerprints(
                {"dotnet-sdk": "9.0.100"}, {"dotnet-sdk": "9.0.100"}
            ),
            reports=self._green_reports(tmp_path),
            egress=self._green_egress(),
            started_at="2026-09-23T10:00:00+00:00",
            finished_at="2026-09-23T10:09:00+00:00",
        )
        assert trace.to_document()["layer_bindings"] == [
            {"layer": "smoke", "evidence": ["tests/test_adaptive_driver_claude_sdk.py"]},
            {"layer": "contract", "evidence": ["tests/test_adaptive_contracts.py"]},
        ]
        assert trace.verdict == "qualified"  # unsupported features don't fail;
        # they are QUALIFIED AS UNSUPPORTED — the record never claims them

    def test_the_trace_document_carries_the_versioned_stamp(self, tmp_path):
        trace = assemble_qualification_trace(_profile())
        document = trace.to_document()
        assert document["schema"] == TRACE_SCHEMA
        assert document["verdict"] == "not_qualified"
        assert len(document["problems"]) >= 3  # partial legs said honestly

    def test_a_wrong_schema_stamp_is_refused(self):
        with pytest.raises(ValueError, match="schema"):
            QualificationTrace(schema="forge.qualification.trace/0", profile_digest="x")

    def test_a_status_outside_the_vocabulary_is_refused(self):
        with pytest.raises(ValueError, match="fingerprint status"):
            QualificationTrace(schema=TRACE_SCHEMA, profile_digest="x", fingerprint_status="vibes")
