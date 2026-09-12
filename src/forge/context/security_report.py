from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {
    "Critical": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
    "Info": 4,
    "Unknown": 5,
}


@dataclass
class SecurityFinding:
    """A single finding from a security scan."""

    id: str
    name: str
    description: str
    severity: str  # Critical, High, Medium, Low, Info, Unknown
    confidence: str = "Unknown"  # High, Medium, Low, Unknown
    scanner: str = ""
    file: str = ""
    start_line: int | None = None
    end_line: int | None = None
    identifiers: list[dict[str, str]] = field(default_factory=list)
    solution: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SecurityReport:
    """Aggregated security report from a scan."""

    findings: list[SecurityFinding] = field(default_factory=list)
    scan_type: str = ""  # sast, dependency_scanning, dast, secret_detection
    scanner_name: str = ""
    scanner_version: str = ""
    errors: list[str] = field(default_factory=list)


def parse_gitlab_security_report(raw_json: dict[str, Any], scan_type: str = "") -> SecurityReport:
    """Parse a GitLab-format security report JSON.

    Handles GitLab report schema versions 14.x and 15.x.
    Reports have: {"version": "...", "vulnerabilities": [...], "scan": {...}}.
    """
    if not isinstance(raw_json, dict):
        return SecurityReport(errors=["Report is not a JSON object"])

    # Extract scan metadata
    scan_info = raw_json.get("scan", {}) or {}
    scanner_info = scan_info.get("scanner", {}) or {}
    scanner_name = scanner_info.get("name", "")
    scanner_version = scanner_info.get("version", "")

    # Auto-detect scan type from report if not provided
    if not scan_type:
        scan_type = scan_info.get("type", "")

    # Parse vulnerabilities
    vulns = raw_json.get("vulnerabilities", [])
    if not isinstance(vulns, list):
        return SecurityReport(
            scan_type=scan_type,
            scanner_name=scanner_name,
            scanner_version=scanner_version,
            errors=["'vulnerabilities' field is not an array"],
        )

    findings: list[SecurityFinding] = []
    errors: list[str] = []

    for vuln in vulns:
        if not isinstance(vuln, dict):
            continue
        try:
            finding = _parse_vulnerability(vuln, scanner_name)
            findings.append(finding)
        except Exception as exc:
            errors.append(f"Failed to parse vulnerability: {exc}")

    return SecurityReport(
        findings=findings,
        scan_type=scan_type,
        scanner_name=scanner_name,
        scanner_version=scanner_version,
        errors=errors,
    )


def prioritize_findings(
    findings: list[SecurityFinding], max_count: int = 20
) -> list[SecurityFinding]:
    """Sort findings by severity (Critical first) and limit count."""
    sorted_findings = sorted(findings, key=lambda f: _SEVERITY_ORDER.get(f.severity, 5))
    return sorted_findings[:max_count]


def format_findings_for_prompt(findings: list[SecurityFinding]) -> str:
    """Format findings into a structured string for the LLM prompt."""
    if not findings:
        return "No findings to report."

    parts: list[str] = []
    for i, f in enumerate(findings, 1):
        location = f.file
        if f.start_line is not None:
            location += f":{f.start_line}"
            if f.end_line is not None and f.end_line != f.start_line:
                location += f"-{f.end_line}"

        ids_str = ""
        if f.identifiers:
            id_parts = []
            for ident in f.identifiers:
                id_type = ident.get("type", "")
                id_name = ident.get("name", ident.get("value", ""))
                if id_type and id_name:
                    id_parts.append(f"{id_type}: {id_name}")
                elif id_name:
                    id_parts.append(id_name)
            if id_parts:
                ids_str = f"\n  Identifiers: {', '.join(id_parts)}"

        parts.append(
            f"### Finding #{i}: {f.name}\n"
            f"  Severity: {f.severity}\n"
            f"  Confidence: {f.confidence}\n"
            f"  Scanner: {f.scanner}\n"
            f"  File: {location}\n"
            f"  Description: {f.description}"
            f"{ids_str}"
        )
        if f.solution:
            parts.append(f"  Suggested solution: {f.solution}")

    return "\n\n".join(parts)


def _parse_vulnerability(vuln: dict[str, Any], default_scanner: str = "") -> SecurityFinding:
    """Parse a single vulnerability entry from the report."""
    # Location extraction — varies by scanner type
    location = vuln.get("location", {}) or {}
    file_path = location.get("file", "")
    start_line = location.get("start_line")
    end_line = location.get("end_line")

    # Some scanners put location in different places
    if not file_path and "file" in vuln:
        file_path = vuln["file"]

    # Identifiers (CVE, CWE, etc.)
    raw_ids = vuln.get("identifiers", []) or []
    identifiers = []
    for ident in raw_ids:
        if isinstance(ident, dict):
            identifiers.append(
                {
                    "type": ident.get("type", ""),
                    "name": ident.get("name", ""),
                    "value": ident.get("value", ""),
                    "url": ident.get("url", ""),
                }
            )

    # Scanner info — may be in the vulnerability itself
    scanner_info = vuln.get("scanner", {}) or {}
    scanner = scanner_info.get("name", default_scanner)

    # Normalize severity to title case
    severity = (vuln.get("severity", "Unknown") or "Unknown").capitalize()
    if severity not in _SEVERITY_ORDER:
        severity = "Unknown"

    return SecurityFinding(
        id=vuln.get("id", ""),
        name=vuln.get("name", vuln.get("message", "Unknown finding")),
        description=vuln.get("description", ""),
        severity=severity,
        confidence=vuln.get("confidence", "Unknown") or "Unknown",
        scanner=scanner,
        file=file_path,
        start_line=start_line,
        end_line=end_line,
        identifiers=identifiers,
        solution=vuln.get("solution", "") or "",
        raw=vuln,
    )
