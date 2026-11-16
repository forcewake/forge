"""Promotion keeps exact artifact, native-provider and migration evidence."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from tools.release.artifacts import report
from tests.test_release_flow import candidate as candidate, release_proofs

ROOT = Path(__file__).resolve().parents[1]


def test_a_failed_required_check_blocks_despite_a_green_canary(candidate):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    smokes[1]["status"] = "failed"
    with pytest.raises(ValueError, match="live smoke did not pass"):
        report(manifest, canary, smokes, junit)


@pytest.mark.parametrize("status", [None, "unknown", "skipped", "cancelled"])
def test_an_unexecuted_check_blocks_fail_closed(candidate, status):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    smokes[0]["status"] = status
    with pytest.raises(ValueError):
        report(manifest, canary, smokes, junit)


def test_an_empty_profile_blocks_fail_closed(candidate):
    directory, manifest = candidate
    canary, _, junit = release_proofs(directory, manifest)
    with pytest.raises(ValueError, match="both provider smokes"):
        report(manifest, canary, [], junit)


@pytest.mark.parametrize("tag", ["failure", "error", "skipped"])
def test_a_nonpassing_invariant_is_not_hidden_by_a_green_canary(candidate, tag):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    junit.write_text(
        f"<testsuites><testsuite><testcase><{tag}/></testcase></testsuite></testsuites>"
    )
    with pytest.raises(ValueError, match="zero-skip"):
        report(manifest, canary, smokes, junit)


def test_no_canary_evidence_blocks_fail_closed(candidate):
    directory, manifest = candidate
    _, smokes, junit = release_proofs(directory, manifest)
    with pytest.raises(ValueError, match="fresh-database boot"):
        report(manifest, {"image": manifest["image"], "stages": []}, smokes, junit)


def test_a_failed_then_successful_canary_preserves_the_failure(candidate):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    canary["stages"].insert(0, {"stage": "fresh", "outcome": "fail"})
    original = copy.deepcopy(canary)
    with pytest.raises(ValueError, match="stage failed"):
        report(manifest, canary, smokes, junit)
    assert canary == original


def test_required_quality_and_both_smokes_precede_promotion():
    jobs = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    assert jobs["candidate"]["needs"] == "quality"
    assert jobs["quality"]["uses"] == "./.github/workflows/ci.yml"
    assert set(jobs["live-smoke"]["strategy"]["matrix"]["provider"]) == {"github", "gitlab"}
    assert set(jobs["promote"]["needs"]) == {"candidate", "live-smoke"}
    assert all("if" not in jobs[name] for name in ("candidate", "live-smoke", "promote"))


def test_canary_installs_through_the_packaged_schema_command() -> None:
    from tools.canary_probes import PROBE

    assert "forge-schema" in PROBE
    assert "forge.migrate" not in PROBE
    assert "cli('forge','--config',str(root/'forge.toml'),'install')" in PROBE
    assert "cli('forge-schema','check'" in PROBE
