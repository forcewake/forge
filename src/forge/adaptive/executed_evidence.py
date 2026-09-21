"""Executed-evidence release claims (OPS-06, review 05868e9).

The release manifest's ``finding_evidence`` says WHERE a claim lives
(the test file); this module says WHETHER IT RAN for the pinned SHA —
performed/skipped/unsupported distinctly, with the executed run ids,
jobs, and artifact digests. A file containing a finding ID cannot alone
close that finding; a boot canary is never represented as multi-repo
SDLC e2e.

The evidence source is the GitHub Actions runs for the pinned commit
(``gh`` CLI — the same tool the maintainers use; read-only). Unknown
and unavailable evidence is visible, never treated as a free pass.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

#: The evidence classes the release vocabulary distinguishes.
EVIDENCE_CLASSES = ("executed", "skipped", "unsupported", "not_run", "unknown")

#: Jobs whose SUCCESS at boot proves nothing about the SDLC path — they
#: are explicitly excluded from "executed" claims (a boot canary is not
#: multi-repo e2e).
_NON_SDLC_JOBS = frozenset({"release-canary"})


@dataclass(frozen=True)
class ExecutedEvidence:
    """One check's executed result for the pinned SHA."""

    name: str
    run_id: int
    run_url: str
    head_sha: str
    conclusion: str
    evidence_class: str  # EVIDENCE_CLASSES member
    detail: str = ""


def _gh_json(*args: str) -> Any:
    completed = subprocess.run(["gh", *args, "--json"], capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {completed.stderr[:200]}")
    return json.loads(completed.stdout or "null")


def fetch_commit_checks(repo: str, sha: str) -> list[ExecutedEvidence]:
    """The Actions check runs for *sha* (executed evidence, read-only).

    Unavailable evidence (gh failure, no runs) surfaces as ONE
    evidence_class="unknown" row — visible, never a free pass.
    """
    try:
        runs = _gh_json("run", "list", "--repo", repo, "--commit", sha, "--limit", "30")
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return [
            ExecutedEvidence(
                name="commit-checks",
                run_id=0,
                run_url="",
                head_sha=sha,
                conclusion="",
                evidence_class="unknown",
                detail="gh run list unavailable for this commit",
            )
        ]
    evidence: list[ExecutedEvidence] = []
    for run in runs if isinstance(runs, list) else []:
        name = str(run.get("name") or "check")
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        database_id = int(run.get("databaseId") or 0)
        url = str(run.get("url") or "")
        if status != "completed":
            evidence_class = "unknown"
            detail = f"status={status}"
        elif conclusion == "success" and name not in _NON_SDLC_JOBS:
            evidence_class = "executed"
            detail = ""
        elif conclusion in {"skipped", "cancelled"}:
            evidence_class = "skipped"
            detail = f"conclusion={conclusion}"
        elif name in _NON_SDLC_JOBS:
            evidence_class = "not_run"
            detail = "boot canary is not SDLC e2e evidence"
        else:
            evidence_class = "unsupported"
            detail = f"conclusion={conclusion}"
        evidence.append(
            ExecutedEvidence(
                name=name,
                run_id=database_id,
                run_url=url,
                head_sha=sha,
                conclusion=conclusion,
                evidence_class=evidence_class,
                detail=detail,
            )
        )
    if not evidence:
        return [
            ExecutedEvidence(
                name="commit-checks",
                run_id=0,
                run_url="",
                head_sha=sha,
                conclusion="",
                evidence_class="not_run",
                detail="no Actions runs for this commit",
            )
        ]
    return evidence


@dataclass
class ReleaseClaims:
    """Claims assembled from declared capabilities + executed evidence."""

    version: str
    head_sha: str
    image_digest: str = ""
    evidence: list[ExecutedEvidence] = field(default_factory=list)
    migration_results: dict[str, str] = field(default_factory=dict)
    compatibility_results: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "forge.release.executed-evidence/1",
            "version": self.version,
            "head_sha": self.head_sha,
            "image_digest": self.image_digest,
            "checks": [
                {
                    "name": e.name,
                    "run_id": e.run_id,
                    "run_url": e.run_url,
                    "head_sha": e.head_sha,
                    "conclusion": e.conclusion,
                    "evidence_class": e.evidence_class,
                    "detail": e.detail,
                }
                for e in self.evidence
            ],
            "migration_results": dict(self.migration_results),
            "compatibility_results": dict(self.compatibility_results),
        }

    def summary(self) -> dict[str, int]:
        """Counts per evidence class — the honest scoreboard."""
        counts = {klass: 0 for klass in EVIDENCE_CLASSES}
        for entry in self.evidence:
            counts[entry.evidence_class] += 1
        return counts

    def may_claim(self, capability: str, *, requires: str = "executed") -> bool:
        """A promoted customer capability requires RELEASE-SPECIFIC evidence.

        A capability whose supporting checks are all skipped/not_run/
        unknown may NOT be claimed — regardless of what the declared
        manifest says.
        """
        matching = [e for e in self.evidence if capability in e.name.lower()]
        if not matching:
            return False
        return any(e.evidence_class == requires for e in matching)


def build_executed_evidence(
    *, version: str, head_sha: str, repo: str, image_digest: str = ""
) -> ReleaseClaims:
    """Assemble the executed-evidence claims for the pinned SHA."""
    evidence = fetch_commit_checks(repo, head_sha)
    return ReleaseClaims(
        version=version,
        head_sha=head_sha,
        image_digest=image_digest,
        evidence=evidence,
        migration_results={"alembic_chain": "see release canary"},
        compatibility_results={"legacy_run_replay": "see validate_release"},
    )


def readme_claims_source(version: str, test_count: int, image: str) -> dict[str, str]:
    """README version/image/test-count generated from ONE source (OPS-06).

    The README sync tests pin the shape; this function is the single
    place the numbers come from at release time.
    """
    return {
        "status_line": f"**v{version}**",
        "image_tag": f"ghcr.io/forcewake/forge:{version}",
        "test_count": f"{test_count} tests",
    }
