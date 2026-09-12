from forge.context.security_report import (
    SecurityFinding,
    format_findings_for_prompt,
    parse_gitlab_security_report,
    prioritize_findings,
)


def _make_sast_report() -> dict:
    return {
        "version": "15.0.0",
        "scan": {
            "type": "sast",
            "scanner": {"name": "semgrep", "version": "1.0"},
        },
        "vulnerabilities": [
            {
                "id": "vuln-1",
                "name": "SQL Injection",
                "description": "User input used in SQL query",
                "severity": "High",
                "confidence": "Medium",
                "location": {
                    "file": "app/db.py",
                    "start_line": 42,
                    "end_line": 42,
                },
                "identifiers": [
                    {"type": "CWE", "name": "CWE-89", "value": "89"},
                ],
                "solution": "Use parameterized queries",
            },
            {
                "id": "vuln-2",
                "name": "Hardcoded password",
                "description": "Password found in source code",
                "severity": "Medium",
                "confidence": "High",
                "location": {
                    "file": "config.py",
                    "start_line": 10,
                },
                "identifiers": [],
            },
        ],
    }


def _make_dependency_report() -> dict:
    return {
        "version": "15.0.0",
        "scan": {
            "type": "dependency_scanning",
            "scanner": {"name": "gemnasium", "version": "3.0"},
        },
        "vulnerabilities": [
            {
                "id": "dep-1",
                "name": "CVE-2024-1234",
                "description": "Remote code execution in lodash",
                "severity": "Critical",
                "confidence": "High",
                "location": {"file": "package.json"},
                "identifiers": [
                    {"type": "CVE", "name": "CVE-2024-1234", "value": "CVE-2024-1234"},
                ],
                "solution": "Upgrade lodash to >= 4.17.21",
            },
        ],
    }


class TestParseGitlabSecurityReport:
    def test_parse_sast_report(self):
        report = parse_gitlab_security_report(_make_sast_report())
        assert report.scan_type == "sast"
        assert report.scanner_name == "semgrep"
        assert len(report.findings) == 2
        assert report.findings[0].name == "SQL Injection"
        assert report.findings[0].severity == "High"
        assert report.findings[0].file == "app/db.py"
        assert report.findings[0].start_line == 42
        assert len(report.findings[0].identifiers) == 1

    def test_parse_dependency_report(self):
        report = parse_gitlab_security_report(_make_dependency_report())
        assert report.scan_type == "dependency_scanning"
        assert report.scanner_name == "gemnasium"
        assert len(report.findings) == 1
        assert report.findings[0].severity == "Critical"

    def test_explicit_scan_type_overrides(self):
        report = parse_gitlab_security_report(_make_sast_report(), scan_type="custom")
        assert report.scan_type == "custom"

    def test_empty_vulnerabilities(self):
        report = parse_gitlab_security_report(
            {
                "version": "15.0.0",
                "scan": {"scanner": {"name": "test"}},
                "vulnerabilities": [],
            }
        )
        assert len(report.findings) == 0
        assert not report.errors

    def test_malformed_report_not_dict(self):
        report = parse_gitlab_security_report("not a dict")  # type: ignore[arg-type]
        assert len(report.errors) == 1
        assert "not a JSON object" in report.errors[0]

    def test_malformed_vulnerabilities_not_list(self):
        report = parse_gitlab_security_report(
            {
                "version": "15.0.0",
                "vulnerabilities": "not a list",
            }
        )
        assert len(report.errors) == 1

    def test_missing_fields_use_defaults(self):
        report = parse_gitlab_security_report(
            {
                "vulnerabilities": [
                    {"id": "v1", "name": "Test vuln"},
                ],
            }
        )
        assert len(report.findings) == 1
        f = report.findings[0]
        assert f.severity == "Unknown"
        assert f.confidence == "Unknown"
        assert f.file == ""

    def test_solution_parsed(self):
        report = parse_gitlab_security_report(_make_sast_report())
        assert report.findings[0].solution == "Use parameterized queries"


class TestPrioritizeFindings:
    def test_sorts_by_severity(self):
        findings = [
            SecurityFinding(id="1", name="low", description="", severity="Low"),
            SecurityFinding(id="2", name="crit", description="", severity="Critical"),
            SecurityFinding(id="3", name="high", description="", severity="High"),
        ]
        result = prioritize_findings(findings)
        assert result[0].severity == "Critical"
        assert result[1].severity == "High"
        assert result[2].severity == "Low"

    def test_limits_count(self):
        findings = [
            SecurityFinding(id=str(i), name=f"f{i}", description="", severity="Low")
            for i in range(30)
        ]
        result = prioritize_findings(findings, max_count=5)
        assert len(result) == 5


class TestFormatFindingsForPrompt:
    def test_empty_findings(self):
        result = format_findings_for_prompt([])
        assert "No findings" in result

    def test_formats_finding_details(self):
        findings = [
            SecurityFinding(
                id="v1",
                name="SQL Injection",
                description="User input in query",
                severity="High",
                confidence="Medium",
                scanner="semgrep",
                file="app/db.py",
                start_line=42,
                identifiers=[{"type": "CWE", "name": "CWE-89"}],
            ),
        ]
        result = format_findings_for_prompt(findings)
        assert "SQL Injection" in result
        assert "app/db.py:42" in result
        assert "CWE-89" in result
        assert "High" in result
