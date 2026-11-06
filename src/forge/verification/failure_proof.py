"""Positive bounded test failures, distinct from process/setup/reporting errors."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import cast
from xml.etree import ElementTree

MAX_REPORT_BYTES = 65535
MAX_FAILURE_AGE_SECONDS = 3600


@dataclass(frozen=True)
class FailureProof:
    format: str
    digest: str
    tests: int
    failures: int
    names: tuple[str, ...]

    def encoded(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))


def trusted_jobs(values: Collection[str]) -> frozenset[str]:
    if (
        isinstance(values, (str, bytes))
        or len(values) > 128
        or len(set(values)) != len(values)
        or any(
            not isinstance(name, str)
            or not 1 <= len(name) <= 256
            or name.strip() != name
            or not name.isprintable()
            for name in values
        )
    ):
        raise ValueError("verification failure jobs must be unique explicit bounded names")
    return frozenset(values)


def finished(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64 or now.tzinfo is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if (
            result.tzinfo is None
            or not 0 <= (now - result).total_seconds() <= MAX_FAILURE_AGE_SECONDS
        ):
            return None
        return result
    except ValueError:
        return None


def counts(value: object, names: tuple[str, ...]) -> tuple[int, ...] | None:
    if not isinstance(value, dict):
        return None
    values = tuple(value.get(name) for name in names)
    if any(type(item) is not int or not 0 <= item <= 1_000_000 for item in values):
        return None
    return cast(tuple[int, ...], values)


def junit_failure(value: object) -> FailureProof | None:
    if not isinstance(value, str):
        return None
    try:
        body = value.encode("utf-8")
    except UnicodeError:
        return None
    if not body or len(body) > MAX_REPORT_BYTES or b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        return None
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError):
        return None
    nodes = list(root.iter())
    if root.tag not in {"testsuites", "testsuite"} or len(nodes) > 4096:
        return None
    allowed = {
        "testsuite",
        "testsuites",
        "testcase",
        "failure",
        "skipped",
        "properties",
        "property",
        "system-out",
        "system-err",
    }
    if any(node.tag not in allowed for node in nodes):
        return None
    suites = [node for node in nodes if node.tag == "testsuite"]
    if not suites:
        return None
    totals = (0, 0, 0)
    names: list[str] = []
    for suite in suites:
        cases = suite.findall("testcase")
        children = suite.findall("testsuite")
        if children and cases:
            return None
        if children:
            return None  # A flat complete report has one count owner for each testcase.
        actual = (
            len(cases),
            sum(bool(case.findall("failure")) for case in cases),
            sum(bool(case.findall("skipped")) for case in cases),
        )
        if any(
            not case.get("name", "").strip()
            or (case.findall("failure") and case.findall("skipped"))
            for case in cases
        ):
            return None
        if not suite_counts(suite, actual):
            return None
        totals = (totals[0] + actual[0], totals[1] + actual[1], totals[2] + actual[2])
        names.extend(
            (case.get("classname", "") + "." + case.get("name", ""))[:256]
            for case in cases
            if case.findall("failure")
        )
    if root.tag == "testsuites" and any(
        key in root.attrib for key in ("tests", "failures", "errors", "skipped")
    ):
        if not suite_counts(root, totals):
            return None
    if sum(node.tag == "testcase" for node in nodes) != totals[0]:
        return None
    if totals[1] == 0 or any(not name.strip(".") for name in names):
        return None
    return FailureProof(
        "junit", hashlib.sha256(body).hexdigest(), totals[0], totals[1], tuple(names[:16])
    )


def suite_counts(node: ElementTree.Element, actual: tuple[int, int, int]) -> bool:
    try:
        values = [node.attrib[name] for name in ("tests", "failures", "errors", "skipped")]
        if any(not value.isascii() or not value.isdecimal() or len(value) > 7 for value in values):
            return False
        tests, failures, errors, skipped = (int(value) for value in values)
        return (tests, failures, skipped) == actual and errors == 0 and failures + skipped <= tests
    except KeyError:
        return False


def gitlab_failure(value: object, job_id: str) -> FailureProof | None:
    if not job_id.isascii() or not job_id.isdecimal() or len(job_id) > 20 or int(job_id) <= 0:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("test_suites"), list):
        return None
    suites = value["test_suites"]
    if not 1 <= len(suites) <= 100:
        return None
    all_counts = [0, 0, 0, 0, 0]
    own_tests = own_failures = 0
    names: list[str] = []
    for suite in suites:
        row = counts(
            suite, ("total_count", "success_count", "failed_count", "skipped_count", "error_count")
        )
        if (
            row is None
            or row[0] != sum(row[1:])
            or suite.get("suite_error") is not None
            or row[4] != 0
        ):
            return None
        ids = suite.get("build_ids")
        if not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids):
            return None
        if len(set(ids)) != len(ids):
            return None
        all_counts = [a + b for a, b in zip(all_counts, row)]
        if str(int(job_id)) in {str(i) for i in ids}:
            if (
                ids != [int(job_id)]
                or not isinstance(suite.get("name"), str)
                or not suite["name"].strip()
            ):
                return None
            own_tests += row[0]
            own_failures += row[2]
            if row[2]:
                names.append(suite["name"][:256])
    total = value.get("total")
    observed = counts(total, ("count", "success", "failed", "skipped", "error"))
    if (
        not isinstance(total, dict)
        or observed is None
        or tuple(all_counts) != observed
        or total.get("suite_error") is not None
        or not own_failures
    ):
        return None
    try:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        return None
    if len(body) > 131072:
        return None
    return FailureProof(
        "gitlab-junit", hashlib.sha256(body).hexdigest(), own_tests, own_failures, tuple(names[:16])
    )
