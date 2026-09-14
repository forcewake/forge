"""Security findings: ingestion + forge-owned triage state (v0.7).

Public surface:

- :mod:`forge.findings.models` — the ``security_findings`` table, source /
  severity / status enums, severity normalization.
- :mod:`forge.findings.fingerprints` — the dedupe-key rules (research
  §3/§4): forge-computed sha256 for GitLab, alert number for GitHub.
- :mod:`forge.findings.ingest` — GitLab ``gl-*-report.json`` artifact
  parsing and GitHub alert-API pulls over the shared upsert.
- :mod:`forge.findings.triage` — the durable ``/security`` command step.
"""

from forge.findings.fingerprints import gitlab_fingerprint, github_fingerprint
from forge.findings.ingest import (
    IngestResult,
    NormalizedFinding,
    ingest_gitlab_pipeline,
    ingest_github_alerts,
    upsert_findings,
)
from forge.findings.models import (
    FINDING_SEVERITIES,
    FINDING_SOURCES,
    FINDING_STATUSES,
    SecurityFinding,
    normalize_severity,
)

__all__ = [
    "FINDING_SEVERITIES",
    "FINDING_SOURCES",
    "FINDING_STATUSES",
    "IngestResult",
    "NormalizedFinding",
    "SecurityFinding",
    "gitlab_fingerprint",
    "github_fingerprint",
    "ingest_gitlab_pipeline",
    "ingest_github_alerts",
    "normalize_severity",
    "upsert_findings",
]
