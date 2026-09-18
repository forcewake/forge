"""Security findings: ingestion + forge-owned triage state (v0.7).

Public surface:

- :mod:`forge.findings.models` — the ``security_findings`` table, the
  ``security_scan_executions`` completeness ledger (R26), the
  ``security_finding_actions`` governance journal (R25), source /
  severity / status enums, severity normalization.
- :mod:`forge.findings.fingerprints` — the dedupe-key rules (research
  §3/§4): forge-computed sha256 for GitLab, alert number for GitHub.
- :mod:`forge.findings.ingest` — GitLab ``gl-*-report.json`` artifact
  parsing and GitHub alert-API pulls over the DB-native ON CONFLICT
  upsert, with per-pass scan completeness records (R26).
- :mod:`forge.findings.triage` — the durable ``/security`` command step:
  AI verdicts land as SUGGESTIONS with optimistic binding; authorized
  actors (or the explicit auto-accept opt-in) confirm; remote dismissal
  is a separate privileged, intent-journaled action (R25).
"""

from forge.findings.fingerprints import gitlab_fingerprint, github_fingerprint
from forge.findings.ingest import (
    IngestResult,
    NormalizedFinding,
    ingest_gitlab_pipeline,
    ingest_github_alerts,
    mark_scan_presence,
    record_scan_execution,
    upsert_findings,
)
from forge.findings.models import (
    FINDING_SEVERITIES,
    FINDING_SOURCES,
    FINDING_STATUSES,
    SUGGESTED_VERDICTS,
    ScanExecution,
    SecurityFinding,
    SecurityFindingAction,
    normalize_severity,
)
from forge.findings.triage import (
    apply_triage_verdicts,
    authorized_triagers,
    confirm_finding_verdict,
    observe_batch,
    open_findings_for_scope,
)

__all__ = [
    "FINDING_SEVERITIES",
    "FINDING_SOURCES",
    "FINDING_STATUSES",
    "IngestResult",
    "NormalizedFinding",
    "SUGGESTED_VERDICTS",
    "ScanExecution",
    "SecurityFinding",
    "SecurityFindingAction",
    "apply_triage_verdicts",
    "authorized_triagers",
    "confirm_finding_verdict",
    "gitlab_fingerprint",
    "github_fingerprint",
    "ingest_gitlab_pipeline",
    "ingest_github_alerts",
    "mark_scan_presence",
    "normalize_severity",
    "observe_batch",
    "open_findings_for_scope",
    "record_scan_execution",
    "upsert_findings",
]
