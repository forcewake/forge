"""OPS-06: release claims from versioned EXECUTED evidence.

A file containing a finding ID cannot alone close that finding; a boot
canary is never multi-repo SDLC e2e; unknown evidence is visible.
"""

from __future__ import annotations

from forge.adaptive.executed_evidence import (
    EVIDENCE_CLASSES,
    ExecutedEvidence,
    ReleaseClaims,
    fetch_commit_checks,
    readme_claims_source,
)


def _e(name: str, klass: str, conclusion: str = "success") -> ExecutedEvidence:
    return ExecutedEvidence(
        name=name,
        run_id=1,
        run_url="https://ci",
        head_sha="a" * 40,
        conclusion=conclusion,
        evidence_class=klass,
    )


class TestEvidenceClasses:
    def test_the_vocabulary_is_closed_and_distinct(self):
        assert set(EVIDENCE_CLASSES) == {
            "executed",
            "skipped",
            "unsupported",
            "not_run",
            "unknown",
        }

    def test_a_boot_canary_is_never_executed_evidence(self):
        """The release canary proves the image boots — it is NOT SDLC e2e."""
        claims = ReleaseClaims(
            version="0.19.0",
            head_sha="a" * 40,
            evidence=[_e("release-canary", "not_run", "success")],
        )
        counts = claims.summary()
        assert counts["executed"] == 0  # a canary alone claims NOTHING executed
        assert counts["not_run"] == 1  # and is visible as such

    def test_may_claim_requires_matching_executed_evidence(self):
        claims = ReleaseClaims(
            version="0.19.0",
            head_sha="a" * 40,
            evidence=[
                _e("test (3.13)", "executed"),
                _e("integration", "executed"),
                _e("integration-os", "skipped", "skipped"),
            ],
        )
        assert claims.may_claim("test") is True
        assert claims.may_claim("integration-os") is False  # skipped ≠ executed
        assert claims.may_claim("nonexistent") is False  # no evidence at all

    def test_unknown_evidence_is_visible_never_free(self):
        claims = ReleaseClaims(
            version="0.19.0",
            head_sha="a" * 40,
            evidence=[_e("ci", "unknown", "")],
        )
        counts = claims.summary()
        assert counts["unknown"] == 1
        assert counts["executed"] == 0

    def test_the_json_manifest_carries_run_ids_and_shas(self):
        claims = ReleaseClaims(
            version="0.19.0",
            head_sha="b" * 40,
            image_digest="sha256:" + "c" * 64,
            evidence=[_e("test (3.13)", "executed")],
        )
        doc = claims.to_json()
        assert doc["schema"] == "forge.release.executed-evidence/1"
        assert doc["head_sha"] == "b" * 40
        assert doc["checks"][0]["run_id"] == 1
        assert doc["image_digest"].startswith("sha256:")
        assert "migration_results" in doc and "compatibility_results" in doc


class TestFetchCommitChecks:
    def test_a_gh_failure_surfaces_as_unknown(self, monkeypatch):
        def failing(*args, **kwargs):
            raise RuntimeError("gh run list failed: auth error")

        monkeypatch.setattr("forge.adaptive.executed_evidence._gh_json", failing)
        evidence = fetch_commit_checks("owner/repo", "a" * 40)
        assert len(evidence) == 1
        assert evidence[0].evidence_class == "unknown"
        assert "unavailable" in evidence[0].detail

    def test_no_runs_for_the_commit_is_not_run(self, monkeypatch):
        monkeypatch.setattr("forge.adaptive.executed_evidence._gh_json", lambda *a: [])
        evidence = fetch_commit_checks("owner/repo", "a" * 40)
        assert evidence[0].evidence_class == "not_run"
        assert "no Actions runs" in evidence[0].detail

    def test_in_progress_runs_are_unknown(self, monkeypatch):
        monkeypatch.setattr(
            "forge.adaptive.executed_evidence._gh_json",
            lambda *a: [
                {
                    "name": "CI",
                    "status": "in_progress",
                    "conclusion": None,
                    "databaseId": 5,
                    "url": "u",
                }
            ],
        )
        evidence = fetch_commit_checks("owner/repo", "a" * 40)
        assert evidence[0].evidence_class == "unknown"


class TestReadmeClaimsSource:
    def test_one_source_generates_the_readme_claims(self):
        source = readme_claims_source("0.19.0", 3748, "ghcr.io/forcewake/forge:0.19.0")
        assert source["status_line"] == "**v0.19.0**"
        assert source["image_tag"] == "ghcr.io/forcewake/forge:0.19.0"
        assert source["test_count"] == "3748 tests"
