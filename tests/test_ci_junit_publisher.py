"""The sealed standalone producer writes only its exact native check over real TLS."""

from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from tests import runtime_fixture

SCRIPT = Path(__file__).resolve().parents[1] / "tools/ci/github_junit.py"
SHA = "a" * 40
REPORT = (
    '<testsuite tests="1" failures="1" errors="0" skipped="0">'
    '<testcase name="answer"><failure>41 != 42</failure></testcase></testsuite>'
)


class NativeCheck:
    def __init__(self, fault=None):
        self.fault, self.requests, self.output = fault, [], None
        self.url = "https://unused.invalid"

    def __call__(self, request):
        self.requests.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer synthetic-native-verifier"
        prefix = "/repos/owner/project"
        job = {
            "id": 33,
            "name": "unit",
            "run_id": 11,
            "run_attempt": 1,
            "head_sha": "f" * 40 if self.fault == "job_sha" else SHA,
            "status": "in_progress",
            "check_run_url": self.url + prefix + "/check-runs/44",
        }
        if self.fault == "absent_job_attempt":
            del job["run_attempt"]
        if self.fault == "job_attempt":
            job["run_attempt"] = 2
        if self.fault == "foreign_url":
            job["check_run_url"] = "https://foreign.invalid/steal"
        if request.url.path == prefix + "/actions/runs/11":
            return httpx.Response(
                200,
                json={
                    "id": 11,
                    "run_attempt": 2 if self.fault == "attempt" else 1,
                    "head_sha": SHA,
                    "repository": {"full_name": "owner/project"},
                    "check_suite_id": 22,
                },
            )
        if request.url.path == prefix + "/actions/runs/11/attempts/1/jobs":
            rows = [job, {**job, "id": 34}] if self.fault == "duplicate" else [job]
            return httpx.Response(200, json={"total_count": len(rows), "jobs": rows})
        if request.url.path == prefix + "/actions/jobs/33":
            return httpx.Response(200, json=job)
        if request.url.path == prefix + "/check-runs/44":
            if request.method == "PATCH":
                document = json.loads(request.content)
                assert set(document) == {"output"}
                assert document["output"]["text"] == REPORT
                self.output = document["output"]
                return httpx.Response(200, json={"id": 44})
            return httpx.Response(
                200,
                json={
                    "id": 44,
                    "name": "unit",
                    "head_sha": SHA,
                    "status": "in_progress",
                    "check_suite": {"id": 99 if self.fault == "suite" else 22},
                    "app": {"slug": "other-app" if self.fault == "app" else "github-actions"},
                },
            )
        return httpx.Response(404, json={"detail": "unexpected native request"})


@pytest.mark.parametrize(
    "fault",
    (
        None,
        "absent_job_attempt",
        "job_attempt",
        "job_sha",
        "foreign_url",
        "attempt",
        "duplicate",
        "suite",
        "app",
    ),
)
def test_standalone_reporter_preserves_native_status_and_exact_execution(
    tmp_path, monkeypatch, fault
):
    wire = NativeCheck(fault)
    monkeypatch.setattr(runtime_fixture.native, "Wire", lambda: wire)
    monkeypatch.setattr(
        BaseHTTPRequestHandler, "do_PATCH", lambda self: self.handle_request(), raising=False
    )
    report = tmp_path / "junit.xml"
    report.write_text(REPORT)
    with runtime_fixture.native_server(tmp_path) as (_, url, ca):
        wire.url = url
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "--report",
                str(report),
                "--job-name",
                "unit",
                "--ca",
                str(ca),
            ],
            env={
                "PATH": os.environ.get("PATH", ""),
                "GITHUB_TOKEN": "synthetic-native-verifier",
                "GITHUB_API_URL": url,
                "GITHUB_REPOSITORY": "owner/project",
                "GITHUB_RUN_ID": "11",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_SHA": SHA,
                "HTTPS_PROXY": "http://127.0.0.1:1",
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
    assert "synthetic-native-verifier" not in result.stdout + result.stderr
    if fault in (None, "absent_job_attempt"):
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "reported": True,
            "job_id": 33,
            "check_run_id": 44,
            "sha": SHA,
        }
        assert wire.output["text"] == REPORT
        assert [method for method, _ in wire.requests].count("PATCH") == 1
    else:
        assert result.returncode == 1
        assert wire.output is None and all(method == "GET" for method, _ in wire.requests)


@pytest.mark.parametrize("body", ("<testsuite/>", "<!DOCTYPE bad><testsuite/>", "x" * 65536))
def test_invalid_or_unbounded_local_report_never_contacts_native_api(tmp_path, body):
    from tools.ci.github_junit import ReportRefused, report_text

    report = tmp_path / "bad.xml"
    report.write_text(body)
    with pytest.raises((ReportRefused, ValueError)):
        report_text(report)
