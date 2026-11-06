"""Publish bounded JUnit only to the exact current native Actions job's output."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    build_opener,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
)
from xml.etree import ElementTree

LIMIT = 65535


class ReportRefused(ValueError):
    pass


def positive(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise ReportRefused("native execution identity is invalid")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class API:
    def __init__(self, url: str, token: str, ca: Path | None):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(part in (".", "..") for part in parsed.path.split("/"))
        ):
            raise ReportRefused("select an explicit HTTPS native API")
        self.url, self.token, self.deadline = url.rstrip("/"), token, time.monotonic() + 45
        context = ssl.create_default_context()
        if ca is not None:
            context.load_verify_locations(cafile=ca)
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReportRefused("native reporting deadline expired")
        request = Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener.open(request, timeout=min(20, remaining)) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ReportRefused("native response exceeded the reporting bound")
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise ReportRefused("native report request failed") from error
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ReportRefused("native response must be an object")
        return value


def report_text(path: Path) -> str:
    with path.open("rb") as source:
        raw = source.read(LIMIT + 1)
    if not raw or len(raw) > LIMIT or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ReportRefused("JUnit must be bounded XML without a DTD or entity declaration")
    value = raw.decode("utf-8")
    root = ElementTree.fromstring(value)
    if root.tag not in ("testsuite", "testsuites") or not list(root.iter("failure")):
        raise ReportRefused("JUnit contains no test failure")
    if list(root.iter("error")):
        raise ReportRefused("JUnit execution errors do not establish a code failure")
    return value


def jobs(api: API, repo: str, run_id: int, attempt: int) -> list[dict]:
    found, total = [], None
    for page in range(1, 9):
        document = api.request(
            "GET",
            f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}",
        )
        count, rows = document.get("total_count"), document.get("jobs")
        if type(count) is not int or count < 0 or not isinstance(rows, list):
            raise ReportRefused("native job page is invalid")
        if total is not None and count != total:
            raise ReportRefused("native job list changed during discovery")
        total = count
        if len(rows) > 100 or not all(isinstance(row, dict) for row in rows):
            raise ReportRefused("native job page is invalid")
        found.extend(rows)
        if len(found) == total:
            if len({positive(row.get("id")) for row in found}) != len(found):
                raise ReportRefused("native job identity is duplicated")
            return found
        if not rows or len(found) > total:
            break
    raise ReportRefused("native job list is incomplete")


def select_execution(api: API, repo: str, run_id: int, attempt: int, name: str, sha: str):
    run = api.request("GET", f"/repos/{repo}/actions/runs/{run_id}")
    if (
        run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("head_sha") != sha
        or run.get("repository", {}).get("full_name", "").casefold() != repo.casefold()
    ):
        raise ReportRefused("current native workflow differs from this execution")
    matches = [row for row in jobs(api, repo, run_id, attempt) if row.get("name") == name]
    if len(matches) != 1:
        raise ReportRefused("current verification job must resolve uniquely")
    job_id = positive(matches[0].get("id"))
    job = api.request("GET", f"/repos/{repo}/actions/jobs/{job_id}")
    if (
        job.get("id") != job_id
        or job.get("run_id") != run_id
        or ("run_attempt" in job and positive(job["run_attempt"]) != attempt)
        or job.get("head_sha") != sha
        or job.get("name") != name
        or job.get("status") != "in_progress"
    ):
        raise ReportRefused("reporting must belong to the currently executing native job")
    prefix = api.url + f"/repos/{repo}/check-runs/"
    check_url = job.get("check_run_url")
    if not isinstance(check_url, str) or not check_url.startswith(prefix):
        raise ReportRefused("native check reference is outside this repository")
    suffix = check_url[len(prefix) :]
    if not re.fullmatch(r"[1-9][0-9]{0,18}", suffix):
        raise ReportRefused("native check identity is invalid")
    return run, job, positive(int(suffix))


def publish(api: API, repo: str, run_id: int, attempt: int, name: str, sha: str, value: str):
    run, job, check_id = select_execution(api, repo, run_id, attempt, name, sha)
    path = f"/repos/{repo}/check-runs/{check_id}"
    check = api.request("GET", path)
    if (
        check.get("id") != check_id
        or check.get("head_sha") != sha
        or check.get("name") != name
        or check.get("status") != "in_progress"
        or check.get("check_suite", {}).get("id") != positive(run.get("check_suite_id"))
        or check.get("app", {}).get("slug") != "github-actions"
    ):
        raise ReportRefused("native check does not belong to the current Actions execution")
    # The runner retains status/conclusion authority; this writes only test evidence.
    api.request(
        "PATCH",
        path,
        {
            "output": {
                "title": "Forge JUnit verification",
                "summary": "Machine-readable test failures from this exact native job.",
                "text": value,
            }
        },
    )
    return {"reported": True, "job_id": job["id"], "check_run_id": check_id, "sha": sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument(
        "--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--ca", type=Path)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) or any(
            part in (".", "..") for part in args.repository.split("/")
        ):
            raise ReportRefused("select one canonical repository")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.sha) or not args.job_name.strip():
            raise ReportRefused("select the exact native source and job")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", args.token_env):
            raise ReportRefused("select an explicit token environment name")
        token = os.environ.get(args.token_env, "")
        if not token or "\r" in token or "\n" in token:
            raise ReportRefused("verification job authentication is unavailable")
        value = report_text(args.report)
        api = API(args.api_url, token, args.ca)
        result = publish(
            api,
            args.repository,
            positive(int(args.run_id)),
            positive(int(args.attempt)),
            args.job_name,
            args.sha,
            value,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, AttributeError, ElementTree.ParseError):
        print(
            "forge verification report refused; inspect the selected native job and JUnit",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
