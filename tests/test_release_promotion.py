"""R32-19 — promote release artifacts only from qualified profile evidence.

The promotion gate (:mod:`forge.release_promotion`) is a QUERY over evidence
recorded before promotion, never a re-run. These tests hold the contract:

- a FAILED required check blocks promotion EVEN IF the boot canary passed;
- an unexecuted/unknown required check blocks fail-closed, with the check's
  provenance named in the reason;
- a failed-then-passed retry records BOTH attempts — conditional pass, the
  failure is never overwritten and never silently green;
- the record binds the exact digest/wheel identity (different digest =>
  different record);
- the gaps query finds capabilities claiming qualification with no fresh
  evidence and clears when qualified evidence lands;
- the committed archive round-trips and stays immutable;
- scripts/generate_template_pins.py is idempotent and --check detects drift;
- the seeded-upgrade canary's SQL is schema-accurate at the N-1 head and the
  upgrade preserves the rows (real Postgres, FORGE_PG_TEST_URL-gated — the
  tests/test_failure_injection.py pattern).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from forge import release_promotion as rp
from forge.release_manifest import ENTRIES
from forge.release_promotion import (
    PROMOTION_STAMP,
    REQUIRED_CHECKS,
    CanaryResult,
    CheckAttempt,
    FileIdentity,
    PromotionDecision,
    PromotionIntegrityError,
    PromotionRecord,
    RequiredCheck,
    WheelIdentity,
    archive_release_evidence,
    evaluate_promotion,
    group_attempts,
    latest_promotion_record,
    load_promotion_records,
    qualification_gaps,
)

ROOT = Path(__file__).resolve().parents[1]

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

PASSING_CHECKS = tuple(
    RequiredCheck.from_result(spec.name, spec.provenance, "success", "run-1")
    for spec in REQUIRED_CHECKS
)
GREEN_CANARY = (
    CanaryResult("fresh", "release-artifact-canary", "pass"),
    CanaryResult("migrate", "release-canary/previous-release-upgrade", "pass"),
)


def _check(name: str) -> RequiredCheck:
    """The REQUIRED_CHECKS entry called *name* (provenance kept intact)."""
    return next(
        RequiredCheck(spec.name, spec.provenance) for spec in REQUIRED_CHECKS if spec.name == name
    )


# ---------------------------------------------------------------------------
# The gate: a failed check beats a green canary
# ---------------------------------------------------------------------------


def test_a_failed_required_check_blocks_despite_a_green_canary() -> None:
    checks = list(PASSING_CHECKS)
    checks[1] = RequiredCheck.from_result(checks[1].name, checks[1].provenance, "failure", "run-9")
    decision = evaluate_promotion(tuple(checks), GREEN_CANARY)
    assert decision.verdict == "block"
    assert not decision.qualified
    assert any(
        checks[1].name in reason and "canary" in reason.lower() for reason in decision.reasons
    ), decision.reasons


def test_an_unexecuted_check_blocks_fail_closed_with_provenance_named() -> None:
    checks = list(PASSING_CHECKS)
    checks[0] = _check(checks[0].name)  # zero attempts — never executed
    decision = evaluate_promotion(tuple(checks), GREEN_CANARY)
    assert decision.verdict == "block"
    assert any("no recorded result" in reason for reason in decision.reasons)
    assert any(checks[0].provenance in reason for reason in decision.reasons), (
        "the blocking reason must name the check's provenance so the operator knows "
        f"where to look: {decision.reasons}"
    )
    verdict = dict((name, verdict) for name, verdict, _ in decision.check_verdicts)
    assert verdict[checks[0].name] == "not_executed"


@pytest.mark.parametrize("conclusion", [None, "skipped", "stale", "neutral"])
def test_an_unknown_conclusion_blocks_fail_closed(conclusion: str | None) -> None:
    checks = list(PASSING_CHECKS)
    checks[2] = RequiredCheck.from_result(checks[2].name, checks[2].provenance, conclusion, "run-2")
    decision = evaluate_promotion(tuple(checks), GREEN_CANARY)
    assert decision.verdict == "block"
    verdict = dict((name, verdict) for name, verdict, _ in decision.check_verdicts)
    assert verdict[checks[2].name] == "unknown"


def test_an_empty_profile_blocks_fail_closed() -> None:
    decision = evaluate_promotion((), GREEN_CANARY)
    assert decision.verdict == "block"
    assert any("no required checks" in reason for reason in decision.reasons)


def test_no_canary_evidence_blocks_fail_closed() -> None:
    decision = evaluate_promotion(PASSING_CHECKS, ())
    assert decision.verdict == "block"
    assert any("no canary results" in reason for reason in decision.reasons)


def test_a_failed_canary_stage_blocks_even_with_all_checks_green() -> None:
    canary = GREEN_CANARY + (CanaryResult("fresh", "release-artifact-canary", "fail"),)
    decision = evaluate_promotion(PASSING_CHECKS, canary)
    assert decision.verdict == "block"


def test_all_green_promotes() -> None:
    decision = evaluate_promotion(PASSING_CHECKS, GREEN_CANARY)
    assert decision.verdict == "promote"
    assert decision.qualified
    assert decision.check_verdicts and all(
        verdict == "pass" for _, verdict, _ in decision.check_verdicts
    )


# ---------------------------------------------------------------------------
# Retry semantics: a retry never overwrites a failure
# ---------------------------------------------------------------------------


def test_a_retry_pass_is_conditional_and_keeps_the_failure_on_record() -> None:
    spec = next(s for s in REQUIRED_CHECKS if s.name == "test (3.13)")
    check = RequiredCheck(
        name=spec.name,
        provenance=spec.provenance,
        attempts=(
            CheckAttempt("failure", "run-1", "2026-09-23T01:00:00Z"),
            CheckAttempt("success", "run-2", "2026-09-23T02:00:00Z"),
        ),
    )
    decision = evaluate_promotion(
        tuple(c if c.name != spec.name else check for c in PASSING_CHECKS), GREEN_CANARY
    )
    assert decision.verdict == "conditional_promote"
    assert decision.qualified
    verdict = dict((name, verdict) for name, verdict, _ in decision.check_verdicts)
    assert verdict[spec.name] == "conditional_pass"
    assert any("run-1" in reason and "conditional" in reason for reason in decision.reasons)
    # The failure stays in the record — never overwritten:
    document = check.to_json()
    assert [a["conclusion"] for a in document["attempts"]] == ["failure", "success"]


def test_a_pass_then_later_failure_blocks() -> None:
    spec = next(s for s in REQUIRED_CHECKS if s.name == "lint")
    check = RequiredCheck(
        name=spec.name,
        provenance=spec.provenance,
        attempts=(
            CheckAttempt("success", "run-1", "2026-09-23T01:00:00Z"),
            CheckAttempt("failure", "run-2", "2026-09-23T02:00:00Z"),
        ),
    )
    decision = evaluate_promotion(
        tuple(c if c.name != spec.name else check for c in PASSING_CHECKS), GREEN_CANARY
    )
    assert decision.verdict == "block"


def test_a_skipped_canary_stage_is_never_counted_as_a_pass() -> None:
    canary = (
        CanaryResult("fresh", "release-artifact-canary", "pass"),
        CanaryResult("migrate", "release-canary/previous-release-upgrade", "skip"),
    )
    decision = evaluate_promotion(PASSING_CHECKS, canary)
    assert decision.verdict == "conditional_promote"
    assert any("self-skipped" in reason for reason in decision.reasons)


def test_an_unknown_canary_outcome_blocks() -> None:
    canary = (CanaryResult("fresh", "release-artifact-canary", "weird"),)
    decision = evaluate_promotion(PASSING_CHECKS, canary)
    assert decision.verdict == "block"


# ---------------------------------------------------------------------------
# Digest binding: the record names the exact artifacts
# ---------------------------------------------------------------------------


def _record(**overrides: object) -> PromotionRecord:
    values: dict = dict(
        version="0.34.0",
        image_ref="ghcr.io/forcewake/forge",
        image_digest=DIGEST_A,
        wheel=WheelIdentity(
            sdist=FileIdentity("forge-0.34.0.tar.gz", "f" * 64),
            wheel=FileIdentity("forge-0.34.0-py3-none-any.whl", "e" * 64),
        ),
        ci_run_id="42",
        head_sha="0" * 40,
        required_checks=PASSING_CHECKS,
        canary=GREEN_CANARY,
        decision=PromotionDecision(verdict="promote"),
        promoted_at="2026-09-23T00:00:00Z",
    )
    values.update(overrides)
    return PromotionRecord(**values)


def test_different_digest_means_a_different_record() -> None:
    assert _record().to_json() != _record(image_digest=DIGEST_B).to_json()
    assert _record(image_digest=DIGEST_B).to_json()["image_digest"] == DIGEST_B
    # wheel identity is part of the binding too:
    assert _record().to_json() != _record(wheel=WheelIdentity(note="none built")).to_json()


def test_record_carries_the_versioned_stamp_and_unknown_wheel_stays_unknown() -> None:
    record = _record(wheel=WheelIdentity(note="image-only release"))
    document = record.to_json()
    assert document["stamp"] == PROMOTION_STAMP
    assert document["wheel"]["sdist"] is None
    assert document["wheel"]["wheel"] is None
    assert document["wheel"]["note"] == "image-only release"


# ---------------------------------------------------------------------------
# The gaps query
# ---------------------------------------------------------------------------

_CAP = "release-artifact-canary"
_UPGRADE = "release-canary/previous-release-upgrade"
_QUALIFICATION_ENTRIES = tuple(
    entry for entry in ENTRIES if entry.evidence_class in rp.QUALIFICATION_CLASSES
)


def _qualified_record(version: str, capability: str) -> PromotionRecord:
    return _record(
        version=version,
        canary=(CanaryResult("fresh", capability, "pass"),),
        decision=PromotionDecision(verdict="promote"),
    )


def test_the_gaps_query_finds_capabilities_without_fresh_evidence() -> None:
    gaps = qualification_gaps(_QUALIFICATION_ENTRIES, (_qualified_record("0.34.0", _CAP),))
    reasons = {gap.capability: gap.reason for gap in gaps}
    assert _UPGRADE in reasons, "a qualification-class capability with no tagged evidence gaps"
    assert "no promotion record tags canary evidence" in reasons[_UPGRADE]


def test_a_gap_clears_when_qualified_evidence_lands_for_that_version() -> None:
    records = (
        _qualified_record("0.34.0", _CAP),
        _qualified_record("0.34.0", _UPGRADE),
    )
    gaps = qualification_gaps(_QUALIFICATION_ENTRIES, records, version="0.34.0")
    assert not [gap for gap in gaps if gap.capability in {_CAP, _UPGRADE}]
    # ...and goes stale again when checking a newer version with no evidence:
    gaps = qualification_gaps(_QUALIFICATION_ENTRIES, records, version="0.35.0")
    assert {gap.capability for gap in gaps} >= {_CAP, _UPGRADE}
    assert any("stale" in gap.reason for gap in gaps)


def test_a_blocked_record_does_not_clear_a_gap() -> None:
    blocked = _record(
        version="0.34.0",
        canary=(CanaryResult("fresh", _CAP, "pass"),),
        decision=PromotionDecision(verdict="block", reasons=("typecheck failed",)),
    )
    gaps = qualification_gaps(_QUALIFICATION_ENTRIES, (blocked,))
    cap_gap = next(gap for gap in gaps if gap.capability == _CAP)
    assert "BLOCKED" in cap_gap.reason


def test_no_records_at_all_is_an_explicit_gap_not_a_crash() -> None:
    gaps = qualification_gaps(_QUALIFICATION_ENTRIES, ())
    assert gaps
    assert all("no promotion records archived" in gap.reason for gap in gaps)


def test_the_real_v0330_archive_gaps_are_the_honest_ones() -> None:
    """The committed v0.33.0 record is BLOCKED, so NOTHING clears from it —
    and the manual/none-gated live capabilities stay gaps, exactly as the
    manifest records them. (Scoped to the v0.33.0 record: newer PROMOTE
    records in the archive clear their own gaps — that is the design.)"""
    records = load_promotion_records(ROOT)
    v0330 = [r for r in records if r.version == "0.33.0"]
    assert v0330, "docs/releases/evidence/v0.33.0/promotion.json must be committed"
    gaps = qualification_gaps(ENTRIES, tuple(v0330))
    capabilities = {gap.capability for gap in gaps}
    assert _CAP in capabilities and _UPGRADE in capabilities  # blocked record
    assert "real-provider-e2e" in capabilities  # no CI-reproducible evidence
    assert "release-canary/target-harness-upload" not in capabilities  # evidence_class none


def test_the_real_v0330_record_evaluates_to_block() -> None:
    """The retrospective demonstration: v0.33.0 shipped with a red typecheck
    on its sha while the canary passed — under this gate it does not qualify.
    (Pinned to the v0.33.0 record; the archive's LATEST record moves on with
    each release — that is the design.)"""
    record = next((r for r in load_promotion_records(ROOT) if r.version == "0.33.0"), None)
    assert record is not None, "docs/releases/evidence/v0.33.0/promotion.json must be committed"
    assert record.image_digest.startswith("sha256:")
    assert record.ci_run_id == "35852868044"
    reevaluated = evaluate_promotion(record.required_checks, record.canary)
    assert reevaluated.verdict == "block"
    verdicts = {name: verdict for name, verdict, _ in reevaluated.check_verdicts}
    assert verdicts["typecheck"] == "fail"
    assert verdicts["test (3.13)"] == "conditional_pass"


# ---------------------------------------------------------------------------
# The committed archive: deterministic paths, round-trip, immutability
# ---------------------------------------------------------------------------


def test_archive_round_trips_readable_json(tmp_path: Path) -> None:
    record = _record()
    written = archive_release_evidence("0.34.0", tmp_path, record=record)
    promotion = written["promotion.json"]
    assert promotion == tmp_path / "docs" / "releases" / "evidence" / "v0.34.0" / "promotion.json"
    document = json.loads(promotion.read_text(encoding="utf-8"))
    assert document["stamp"] == PROMOTION_STAMP
    assert document["image_digest"] == DIGEST_A
    loaded = load_promotion_records(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].image_digest == DIGEST_A
    assert loaded[0].decision.verdict == "promote"
    assert [c.name for c in loaded[0].required_checks] == [c.name for c in PASSING_CHECKS]
    assert latest_promotion_record(tmp_path).version == "0.34.0"


def test_archive_is_idempotent_for_identical_content(tmp_path: Path) -> None:
    record = _record()
    archive_release_evidence("0.34.0", tmp_path, record=record)
    before = (tmp_path / "docs/releases/evidence/v0.34.0/promotion.json").read_bytes()
    archive_release_evidence("0.34.0", tmp_path, record=record)
    assert (tmp_path / "docs/releases/evidence/v0.34.0/promotion.json").read_bytes() == before


def test_archive_refuses_to_silently_replace_different_evidence(tmp_path: Path) -> None:
    archive_release_evidence("0.34.0", tmp_path, record=_record())
    with pytest.raises(PromotionIntegrityError, match="immutable"):
        archive_release_evidence("0.34.0", tmp_path, record=_record(image_digest=DIGEST_B))
    # deliberate supersede (a re-tag) must pass the flag, not sneak around it:
    archive_release_evidence(
        "0.34.0", tmp_path, record=_record(image_digest=DIGEST_B), replace=True
    )
    assert load_promotion_records(tmp_path)[0].image_digest == DIGEST_B


def test_archive_rejects_malformed_versions(tmp_path: Path) -> None:
    with pytest.raises(PromotionIntegrityError, match="bad release version"):
        archive_release_evidence("../escape", tmp_path, record=_record())


def test_loading_refuses_a_foreign_stamp(tmp_path: Path) -> None:
    target = tmp_path / "docs/releases/evidence/v0.9.9"
    target.mkdir(parents=True)
    (target / "promotion.json").write_text(json.dumps({"stamp": "other/9"}), encoding="utf-8")
    with pytest.raises(PromotionIntegrityError, match="stamp"):
        load_promotion_records(tmp_path)


def test_the_real_archive_manifest_matches_the_recorded_release() -> None:
    """Each archived release's manifest and promotion record agree — checked
    for every version in the archive, not just the latest."""
    for version_dir in sorted((ROOT / "docs/releases/evidence").glob("v*")):
        manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
        record = next(
            (r for r in load_promotion_records(ROOT) if r.version == version_dir.name[1:]),
            None,
        )
        assert record is not None, f"{version_dir.name}: promotion record missing"
        assert manifest["version"] == record.version
        # The snapshot was generated FROM the tagged tree, not the working tree:
        assert manifest["sha"] == record.head_sha, version_dir.name


# ---------------------------------------------------------------------------
# The required-check profile: explicit, provenance-bound
# ---------------------------------------------------------------------------


def test_required_checks_are_explicit_literal_names_bound_to_real_workflows() -> None:
    assert len(REQUIRED_CHECKS) >= 5
    for spec in REQUIRED_CHECKS:
        assert spec.name and not set(spec.name) & {"*", "?", "["}, (
            f"{spec.name!r} looks like a wildcard — required checks are explicit names "
            "(matrix legs are distinct literal names, e.g. 'test (3.13)')"
        )
        assert spec.provenance.startswith(".github/workflows/")
        assert (ROOT / spec.provenance.split("#")[0]).is_file(), (
            f"{spec.provenance} names a workflow file that must exist"
        )
    names = [spec.name for spec in REQUIRED_CHECKS]
    assert len(names) == len(set(names))  # no duplicates
    # the matrix legs are separate explicit checks:
    assert "test (3.13)" in names and "test (3.14)" in names


def test_group_attempts_keeps_retries_ordered_and_ignores_foreign_checks() -> None:
    payload = {
        "check_runs": [
            {
                "name": "typecheck",
                "conclusion": "success",
                "id": 2,
                "completed_at": "T2",
                "details_url": "https://github.com/o/r/actions/runs/111/job/2",
            },
            {
                "name": "typecheck",
                "conclusion": "failure",
                "id": 1,
                "completed_at": "T1",
                "details_url": "https://github.com/o/r/actions/runs/110/job/1",
            },
            {
                "name": "Analyze (python)",
                "conclusion": "success",
                "id": 3,
                "completed_at": "T3",
                "details_url": "https://github.com/o/r/actions/runs/9",
            },
            {
                "name": "test (3.13)",
                "conclusion": None,
                "id": 4,
                "completed_at": "",
                "details_url": "",
            },
        ]
    }
    grouped = group_attempts(payload, tuple(s.name for s in REQUIRED_CHECKS))
    assert [a.conclusion for a in grouped["typecheck"]] == ["failure", "success"]
    assert grouped["typecheck"][0].run_id == "110"
    assert grouped["test (3.13)"][0].conclusion is None  # still pending: not a pass
    assert "Analyze (python)" not in grouped  # profile is explicit
    assert grouped["integration"] == ()  # absent check: zero attempts -> fail-closed


# ---------------------------------------------------------------------------
# The CLI gate: reads recorded evidence, exits non-zero when blocked
# ---------------------------------------------------------------------------


def _write_gate_inputs(tmp_path: Path, *, failing: bool) -> tuple[Path, Path]:
    checks = []
    for spec in REQUIRED_CHECKS:
        result = "failure" if (failing and spec.name == "typecheck") else "success"
        checks.append(
            {
                "name": spec.name,
                "provenance": spec.provenance,
                "attempts": [{"conclusion": result, "run_id": "7", "completed_at": "T"}],
            }
        )
    checks_file = tmp_path / "checks.json"
    checks_file.write_text(json.dumps(checks), encoding="utf-8")
    canary_file = tmp_path / "canary.json"
    canary_file.write_text(
        json.dumps({"image": "ref", "stages": [r.to_json() for r in GREEN_CANARY]}),
        encoding="utf-8",
    )
    return checks_file, canary_file


def test_cli_gate_promotes_and_archives_the_record(tmp_path: Path) -> None:
    checks_file, canary_file = _write_gate_inputs(tmp_path, failing=False)
    code = rp.main(
        [
            "gate",
            "--version",
            "0.34.0",
            "--image-ref",
            "ghcr.io/forcewake/forge",
            "--digest",
            DIGEST_A,
            "--ci-run-id",
            "55",
            "--head-sha",
            "1" * 40,
            "--checks-json",
            str(checks_file),
            "--canary-json",
            str(canary_file),
            "--out",
            str(tmp_path / "promotion.json"),
            "--archive-root",
            str(tmp_path),
            "--archive",
        ]
    )
    assert code == 0
    document = json.loads((tmp_path / "promotion.json").read_text(encoding="utf-8"))
    assert document["decision"]["verdict"] == "promote"
    assert document["image_digest"] == DIGEST_A
    archived = tmp_path / "docs/releases/evidence/v0.34.0/promotion.json"
    assert archived.is_file()
    # no wheel/sdist facts given -> unknown stays unknown with a note:
    assert document["wheel"]["sdist"] is None
    assert document["wheel"]["note"]


def test_cli_gate_records_the_lane_artifact_identity_when_built(tmp_path: Path) -> None:
    """Q35-08: a release that builds the wheel set records BOTH halves of
    its identity — filenames + digests (the legacy wheel block) AND the
    published URLs + sha256 (the additive fields the target templates pin
    their install defaults from)."""
    checks_file, canary_file = _write_gate_inputs(tmp_path, failing=False)
    wheel_url = (
        "https://github.com/forcewake/forge/releases/download/v0.34.0/forge-0.34.0-py3-none-any.whl"
    )
    sdist_url = "https://github.com/forcewake/forge/releases/download/v0.34.0/forge-0.34.0.tar.gz"
    code = rp.main(
        [
            "gate",
            "--version",
            "0.34.0",
            "--image-ref",
            "ghcr.io/forcewake/forge",
            "--digest",
            DIGEST_A,
            "--checks-json",
            str(checks_file),
            "--canary-json",
            str(canary_file),
            "--sdist",
            "forge-0.34.0.tar.gz",
            "--sdist-sha256",
            "f" * 64,
            "--sdist-url",
            sdist_url,
            "--wheel",
            "forge-0.34.0-py3-none-any.whl",
            "--wheel-sha256",
            "e" * 64,
            "--wheel-url",
            wheel_url,
            "--out",
            str(tmp_path / "promotion.json"),
        ]
    )
    assert code == 0
    document = json.loads((tmp_path / "promotion.json").read_text(encoding="utf-8"))
    assert document["wheel"]["wheel"] == {
        "name": "forge-0.34.0-py3-none-any.whl",
        "sha256": "e" * 64,
    }
    assert document["wheel_sha256"] == "e" * 64
    assert document["sdist_sha256"] == "f" * 64
    assert document["wheel_url"] == wheel_url
    assert document["sdist_url"] == sdist_url


def test_cli_gate_blocks_and_exits_non_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    checks_file, canary_file = _write_gate_inputs(tmp_path, failing=True)
    code = rp.main(
        [
            "gate",
            "--version",
            "0.34.0",
            "--image-ref",
            "ghcr.io/forcewake/forge",
            "--digest",
            DIGEST_A,
            "--checks-json",
            str(checks_file),
            "--canary-json",
            str(canary_file),
        ]
    )
    assert code == 1
    assert "BLOCKED" in capsys.readouterr().err


def test_cli_gaps_exports_the_real_archive(tmp_path: Path) -> None:
    code = rp.main(["gaps", "--root", str(ROOT)])
    assert code == 0  # gaps are information, not a CI failure, without --fail-on-gap
    code = rp.main(["gaps", "--root", str(ROOT), "--fail-on-gap"])
    assert code == 1  # the real archive has gaps (blocked v0.33.0 + manual e2e lanes)


# ---------------------------------------------------------------------------
# scripts/generate_template_pins.py — idempotent, drift-detecting
# ---------------------------------------------------------------------------


def _load_pins_module():
    spec = importlib.util.spec_from_file_location(
        "generate_template_pins", ROOT / "scripts" / "generate_template_pins.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_template_pins"] = module
    spec.loader.exec_module(module)
    return module


_LEGACY_README = """\
# forge

## Status

**v0.34.0** — the thing. (`ghcr.io/forcewake/forge:0.34.0`). 5462 tests;

## Quick start

### 1. Run the image (or build from source)

```bash
docker run -d --name forge -p 8420:8420 \\
  --env-file .env ghcr.io/forcewake/forge:0.34.0
# or from source:
git clone https://github.com/forcewake/forge && cd forge
uv sync
```
"""


def _pins_root(tmp_path: Path, version: str = "0.34.0") -> Path:
    root = tmp_path / "repo"
    (root / "docs/releases/evidence" / f"v{version}").mkdir(parents=True)
    (root / "docs/releases/evidence" / f"v{version}" / "promotion.json").write_text(
        json.dumps(
            {
                "version": version,
                "image_digest": DIGEST_A,
                "ci_run_id": "42",
                "decision": {
                    "verdict": "promote",
                    "reasons": [],
                    "checks": [
                        {"name": "lint", "verdict": "pass", "reason": "ok"},
                    ],
                },
                "canary": [
                    {
                        "stage": "fresh",
                        "capability": "release-artifact-canary",
                        "outcome": "pass",
                        "detail": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text(_LEGACY_README, encoding="utf-8")
    # Q35-08: the default pins run renders the LANE PIN into the workflow
    # templates too — the fixture root carries the shipped files (fences
    # and all) exactly as the repo does.
    for relpath in (
        "ci/templates/forge-harness.github.yml",
        ".github/workflows/forge-harness.yml",
    ):
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relpath).read_text(encoding="utf-8"))
    return root


def test_pins_generator_takes_over_legacy_pins_and_is_idempotent(tmp_path: Path) -> None:
    pins = _load_pins_module()
    root = _pins_root(tmp_path)
    assert pins.main(["--root", str(root)]) == 0
    first = (root / "README.md").read_text(encoding="utf-8")
    assert pins.BEGIN in first and pins.END in first
    assert "ghcr.io/forcewake/forge:0.34.0" in first
    assert DIGEST_A in first  # the qualified digest, not just the tag
    assert "docs/releases/evidence/v0.34.0/promotion.json" in first
    # idempotent: a second run changes nothing
    assert pins.main(["--root", str(root)]) == 0
    assert (root / "README.md").read_text(encoding="utf-8") == first
    # and --check passes on the synced tree
    assert pins.main(["--root", str(root), "--check"]) == 0


def test_pins_check_mode_exits_non_zero_on_drift(tmp_path: Path) -> None:
    pins = _load_pins_module()
    root = _pins_root(tmp_path)
    assert pins.main(["--root", str(root)]) == 0
    readme = root / "README.md"
    drifted = readme.read_text(encoding="utf-8").replace(
        "ghcr.io/forcewake/forge:0.34.0", "ghcr.io/forcewake/forge:0.99.0"
    )
    readme.write_text(drifted, encoding="utf-8")
    assert pins.main(["--root", str(root), "--check"]) == 1
    # a plain run repairs it:
    assert pins.main(["--root", str(root)]) == 0
    assert "0.99.0" not in readme.read_text(encoding="utf-8")


def test_pins_render_the_blocked_verdict_honestly(tmp_path: Path) -> None:
    pins = _load_pins_module()
    root = _pins_root(tmp_path)
    record = json.loads(
        (root / "docs/releases/evidence/v0.34.0/promotion.json").read_text(encoding="utf-8")
    )
    record["decision"] = {
        "verdict": "block",
        "reasons": ["no"],
        "checks": [{"name": "typecheck", "verdict": "fail", "reason": "red"}],
    }
    (root / "docs/releases/evidence/v0.34.0/promotion.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    assert pins.main(["--root", str(root)]) == 0
    content = (root / "README.md").read_text(encoding="utf-8")
    assert "verdict `blocked` (typecheck)" in content


def test_the_real_readme_pins_are_in_sync_with_the_archive() -> None:
    """The committed README block must match what the evidence renders —
    hand-edited pins are exactly the drift this script removes."""
    pins = _load_pins_module()
    assert pins.main(["--root", str(ROOT), "--check"]) == 0


# ---------------------------------------------------------------------------
# The seeded upgrade canary: schema-accurate seeding at N-1, preserved rows
# (real Postgres — the tests/test_failure_injection.py gating pattern)
# ---------------------------------------------------------------------------

PG_TEST_URL = os.environ.get("FORGE_PG_TEST_URL", "")

_pg_reason = (
    "FORGE_PG_TEST_URL not set — the seeded-upgrade canary runs only against a "
    "disposable real Postgres (its schema is dropped)"
)


def _load_canary_smoke():
    spec = importlib.util.spec_from_file_location(
        "canary_smoke", ROOT / "scripts" / "canary_smoke.py"
    )
    canary_smoke = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(canary_smoke)
    return canary_smoke


async def _fingerprint(engine, canary_smoke) -> dict[str, str]:
    """The canary's per-table preservation fingerprint, via SQLAlchemy."""
    from sqlalchemy import text

    fingerprint: dict[str, str] = {}
    async with engine.begin() as conn:
        for table, expression in canary_smoke._SEED_FINGERPRINTS:
            count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            digest = (
                await conn.execute(
                    text(
                        "SELECT encode(sha256(convert_to("
                        "coalesce(string_agg(x, E'|' ORDER BY x), ''), 'UTF8')), 'hex') "
                        f"FROM (SELECT {expression} AS x FROM {table}) s"
                    )
                )
            ).scalar_one()
            fingerprint[table] = f"{count} {digest}"
    return fingerprint


@pytest.mark.skipif(not PG_TEST_URL, reason=_pg_reason)
async def test_seeded_real_data_survives_the_n_minus_1_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canary's seed SQL is schema-accurate at the PREVIOUS head, and the
    delta upgrade preserves every seeded row: counts and the sha256
    fingerprints over ordered row identity — the exact scenario
    ``scripts/canary_smoke.py --seed-real-data`` drives in CI."""
    import asyncio

    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from forge.migrate import build_alembic_config

    canary_smoke = _load_canary_smoke()
    script = ScriptDirectory.from_config(build_alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, heads
    head = heads[0]
    previous = script.get_revision(head).down_revision
    assert previous is not None, "the chain must have an N-1 revision to upgrade from"

    def _upgrade(revision: str) -> None:
        # alembic's env.py drives asyncio.run() internally — off the pytest
        # loop — and resolves the URL from DATABASE_URL (the forge.migrate
        # contract); pin it to the DISPOSABLE test DB so a developer's .env
        # can never leak a live database in here.
        monkeypatch.setenv("DATABASE_URL", PG_TEST_URL)
        command.upgrade(build_alembic_config(PG_TEST_URL), revision)

    async def upgrade(revision: str) -> None:
        await asyncio.to_thread(_upgrade, revision)

    engine = create_async_engine(PG_TEST_URL)
    try:
        # A disposable database: drop everything (the PostgresLab pattern).
        async with engine.begin() as conn:
            names = (
                (
                    await conn.execute(
                        text("select tablename from pg_tables where schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
            for name in names:
                await conn.execute(text(f'drop table if exists "{name}" cascade'))
        await engine.dispose()

        # 1. The PREVIOUS release's chain (schema at the N-1 head), then the
        #    canary's raw seed SQL against exactly that schema.
        await upgrade(previous)
        async with engine.begin() as conn:
            for statement in canary_smoke._SEED_SQL.split(";\n\n"):
                if statement.strip():
                    await conn.execute(text(statement))
        fingerprint_before = await _fingerprint(engine, canary_smoke)
        assert fingerprint_before, "the seed SQL must have inserted rows"
        await engine.dispose()

        # 2. This release's delta upgrade (never create_all).
        await upgrade(head)

        # 3. Every seeded row survived: identical per-table count + sha256.
        fingerprint_after = await _fingerprint(engine, canary_smoke)
        assert fingerprint_after == fingerprint_before
    finally:
        await engine.dispose()


@pytest.mark.skipif(not PG_TEST_URL, reason=_pg_reason)
async def test_the_seeded_tables_exist_at_the_previous_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every table the canary seeds must already exist one revision back —
    seeding a table the previous release never had would be a fiction."""
    import asyncio

    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from forge.migrate import build_alembic_config

    canary_smoke = _load_canary_smoke()
    script = ScriptDirectory.from_config(build_alembic_config())
    head = script.get_heads()[0]
    previous = script.get_revision(head).down_revision

    engine = create_async_engine(PG_TEST_URL)
    try:
        async with engine.begin() as conn:
            names = (
                (
                    await conn.execute(
                        text("select tablename from pg_tables where schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
            for name in names:
                await conn.execute(text(f'drop table if exists "{name}" cascade'))
        await engine.dispose()
        monkeypatch.setenv("DATABASE_URL", PG_TEST_URL)
        await asyncio.to_thread(command.upgrade, build_alembic_config(PG_TEST_URL), previous)
        async with engine.begin() as conn:
            tables = set(
                (
                    await conn.execute(
                        text("select tablename from pg_tables where schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
        seeded = {table for table, _ in canary_smoke._SEED_FINGERPRINTS}
        assert seeded <= tables, (
            f"the canary seeds tables that do not exist at the previous head {previous}: "
            f"{sorted(seeded - tables)}"
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# canary_smoke argument discipline (no containers needed)
# ---------------------------------------------------------------------------


def test_seed_real_data_requires_the_migrate_stage(capsys: pytest.CaptureFixture) -> None:
    canary_smoke = _load_canary_smoke()
    code = canary_smoke.main(
        [
            "localhost/forge:canary",
            "--allow-unpinned",
            "--stages",
            "fresh",
            "--seed-real-data",
        ]
    )
    assert code == 2
    assert "--seed-real-data needs the migrate stage" in capsys.readouterr().err


def test_stage_migrate_records_the_skip_when_the_previous_image_is_not_pullable() -> None:
    canary_smoke = _load_canary_smoke()

    class Unpullable:
        def try_pull(self, image: str) -> bool:
            return False

    recorder = canary_smoke.Recorder("img", None)
    shipped = canary_smoke.stage_migrate(
        Unpullable(),
        "img",
        "ghcr.io/forcewake/forge:latest",
        seed_real_data=True,
        recorder=recorder,
    )
    assert shipped is False
    outcomes = {(row["stage"], row["outcome"]) for row in recorder.stages}
    assert ("migrate", "skip") in outcomes
    assert ("seed-real-data", "skip") in outcomes


def test_the_recorder_writes_fail_rows_for_a_failed_stage(tmp_path: Path) -> None:
    canary_smoke = _load_canary_smoke()
    path = tmp_path / "results.json"
    recorder = canary_smoke.Recorder("img", str(path))
    recorder.add("fresh", "pass")
    recorder.note_failure("migrate", "boom")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["image"] == "img"
    assert [row["outcome"] for row in document["stages"]] == ["pass", "fail"]
    assert document["stages"][1]["capability"] == "release-canary/previous-release-upgrade"
    # a failure recorded by the stage itself is not duplicated:
    recorder.note_failure("migrate", "boom again")
    assert len(recorder.stages) == 2


def test_psql_exec_passes_stdin_through_docker_i() -> None:
    """``docker exec`` drops piped stdin without ``-i``: psql then reads an
    EMPTY script, runs nothing, exits 0 — the count parses as ``''`` (the
    v0.34.0 release-canary bite). The exec argv must carry ``-i``."""
    canary_smoke = _load_canary_smoke()
    canary = canary_smoke.Canary(runtime="podman")
    argv: list[str] = []

    def fake_run(*args: str, input: str | None = None, **_kwargs: object) -> str:
        argv.extend(args)
        assert input is not None, "the SQL must ride stdin, not -c"
        return "42\n"

    canary.run = fake_run  # type: ignore[method-assign]
    assert canary_smoke._psql(canary, "SELECT 1;") == "42\n"
    exec_at = argv.index("exec")
    assert argv[exec_at + 1] == "-i", f"exec must pass -i before the container: {argv}"
    assert canary_smoke._psql_count(canary, "t") == 42
