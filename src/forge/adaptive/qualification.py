"""Identity-bound expected test reports consumed by runtime verification."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

from forge.runs.spec import canonical_json_digest

IDENTITY_SIDECAR_SUFFIX = ".identity.json"


REPORT_PASSED = "passed"


REPORT_FAILED = "failed"


REPORT_MISSING = "missing_report"


REPORT_STALE = "stale_report"


REPORT_UNPARSEABLE = "unparseable_report"


REPORT_VERDICT_VALUES = (
    REPORT_PASSED,
    REPORT_FAILED,
    REPORT_MISSING,
    REPORT_STALE,
    REPORT_UNPARSEABLE,
)


_TRX_NAMESPACE = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"


_TRX_FAILURE_COUNTERS = ("failed", "error", "timeout", "aborted")


@dataclass(frozen=True)
class ExpectedReport:
    """One test project's expected report file, bound to the candidate
    identity and test bundle digest it must carry.

    ``report_path`` is the file name within the report directory (the
    recipe's ``report_prefix`` + project convention, e.g.
    ``forge_Api.Tests_net9.0.trx``). ``candidate_id`` identifies the
    qualification run; ``bundle_digest`` pins the exact test bundle
    that was executed. Together they are the identity a found report
    must MATCH — anything else in the directory is a leftover.
    """

    test_project: str
    report_path: str
    candidate_id: str
    bundle_digest: str

    def __post_init__(self) -> None:
        for name in ("test_project", "report_path", "candidate_id", "bundle_digest"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty — the report binding names it")


@dataclass(frozen=True)
class ExpectedReports:
    """The expected report set: one row per test project, no overlaps.

    ``of_project``/``of_path`` are the lookups the reconciliation and
    the tests use; a duplicate project or path at construction is a
    modelling error (two rows could then answer for one report file).
    """

    reports: tuple[ExpectedReport, ...]

    def __post_init__(self) -> None:
        if not self.reports:
            raise ValueError("a qualification expects at least one test report")
        projects = [report.test_project for report in self.reports]
        paths = [report.report_path for report in self.reports]
        if len(set(projects)) != len(projects):
            raise ValueError("one expected report per test project, no duplicates")
        if len(set(paths)) != len(paths):
            raise ValueError("one expected report per report path, no duplicates")

    def of_path(self, report_path: str) -> ExpectedReport | None:
        for report in self.reports:
            if report.report_path == report_path:
                return report
        return None


@dataclass(frozen=True)
class ReportVerdict:
    """One test project's report verdict. Counts are ``None`` unless
    the report was found, identity-bound AND parsed — a missing or
    stale report NEVER reads as zero failures."""

    test_project: str
    report_path: str
    verdict: str
    detail: str
    total: int | None = None
    executed: int | None = None
    passed: int | None = None
    failed: int | None = None

    def __post_init__(self) -> None:
        if self.verdict not in REPORT_VERDICT_VALUES:
            raise ValueError(
                f"unknown report verdict {self.verdict!r}; vocabulary is {REPORT_VERDICT_VALUES}"
            )

    def __str__(self) -> str:  # pragma: no cover — trivial rendering
        return f"[{self.verdict}] {self.test_project}: {self.detail}"


@dataclass(frozen=True)
class ReportReconciliation:
    """Every expected report's verdict, in expected order — the
    aggregate that surfaces ALL problems distinctly."""

    verdicts: tuple[ReportVerdict, ...]

    @property
    def problems(self) -> tuple[str, ...]:
        """One line per non-passing verdict — the failing project, the
        missing report AND the stale leftover all appear; a passing TRX
        file beside them cancels none of them."""
        return tuple(
            f"{verdict.test_project}: {verdict.verdict} — {verdict.detail}"
            for verdict in self.verdicts
            if verdict.verdict != REPORT_PASSED
        )

    @property
    def is_green(self) -> bool:
        """True only when every expected report was found, identity-
        bound, parsed and failure-free (and at least one was expected —
        :class:`ExpectedReports` enforces that at construction)."""
        return bool(self.verdicts) and all(
            verdict.verdict == REPORT_PASSED for verdict in self.verdicts
        )

    def to_document(self) -> dict:
        return {
            "aggregate": REPORTS_ALL_PASSED if self.is_green else REPORTS_PROBLEMS,
            "verdicts": [
                {
                    "test_project": verdict.test_project,
                    "report_path": verdict.report_path,
                    "verdict": verdict.verdict,
                    "total": verdict.total,
                    "executed": verdict.executed,
                    "passed": verdict.passed,
                    "failed": verdict.failed,
                    "detail": verdict.detail,
                }
                for verdict in self.verdicts
            ],
            "problems": list(self.problems),
        }


def _parse_trx_counters(path: Path) -> dict[str, int] | None:
    """The TRX ``ResultSummary/Counters`` as ints, or None when the
    file is not a parseable TRX with counters (unparseable IS a
    verdict, never zero failures)."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError):
        return None
    counters = root.find(f"./{_TRX_NAMESPACE}ResultSummary/{_TRX_NAMESPACE}Counters")
    if counters is None:
        return None

    def _count(name: str) -> int:
        raw = counters.get(name, "0") or "0"
        try:
            return int(float(raw))
        except ValueError:
            return 0

    values = {
        name: _count(name)
        for name in ("total", "executed", "passed", "failed", "error", "timeout", "aborted")
        + ("inconclusive",)
    }
    values["failures"] = sum(values[name] for name in _TRX_FAILURE_COUNTERS)
    return values


def _read_identity(path: Path) -> dict[str, str] | None:
    """The identity sidecar's (candidate_id, bundle_digest), or None
    when absent/unreadable (a sidecar that cannot be read cannot bind
    the report to anything)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    return document


def reconcile_reports(expected: ExpectedReports, found_dir: Path) -> ReportReconciliation:
    """Reconcile the expected reports against one report directory.

    Per expected report, in order:

    - the report file is ABSENT → ``missing_report`` — the project ran
      no report at all, which is never zero failures;
    - the file is present but its identity sidecar (``<report>
      .identity.json``) is absent/unreadable, or names another
      candidate/bundle → ``stale_report`` — a leftover from another
      run answering in this one's place;
    - the file is present and identity-bound but is not a parseable
      TRX with counters → ``unparseable_report`` — unknown counts, and
      unknown is never green;
    - parsed → ``passed``/``failed`` with the counts (failures fold in
      error/timeout/aborted; a report that executed ZERO tests is a
      failure — it proves nothing).
    """
    verdicts: list[ReportVerdict] = []
    for report in expected.reports:
        report_path = found_dir / report.report_path
        sidecar_path = found_dir / (report.report_path + IDENTITY_SIDECAR_SUFFIX)
        if not report_path.is_file():
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_MISSING,
                    detail=(
                        f"no report at {report.report_path} — the test project "
                        "produced nothing this run, and a missing report is "
                        "never zero failures"
                    ),
                )
            )
            continue
        identity = _read_identity(sidecar_path)
        if identity is None:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_STALE,
                    detail=(
                        f"{sidecar_path.name} is absent or unreadable — the "
                        "report cannot be bound to this candidate/bundle and is "
                        "treated as a leftover from another run"
                    ),
                )
            )
            continue
        found_candidate = str(identity.get("candidate_id") or "")
        found_bundle = str(identity.get("bundle_digest") or "")
        if found_candidate != report.candidate_id or found_bundle != report.bundle_digest:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_STALE,
                    detail=(
                        f"the report belongs to candidate {found_candidate or '?'}/"
                        f"bundle {found_bundle[:12] or '?'} — this run is "
                        f"{report.candidate_id}/{report.bundle_digest[:12]}; a "
                        "leftover from another run cannot answer for this one"
                    ),
                )
            )
            continue
        counters = _parse_trx_counters(report_path)
        if counters is None:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_UNPARSEABLE,
                    detail=(
                        f"{report.report_path} is not a parseable TRX with "
                        "ResultSummary counters — its counts are unknown, and "
                        "unknown is never green"
                    ),
                )
            )
            continue
        failures = counters["failures"]
        if counters["executed"] <= 0:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_FAILED,
                    total=counters["total"],
                    executed=counters["executed"],
                    passed=counters["passed"],
                    failed=failures,
                    detail=(
                        "the report parsed but executed zero tests — it proves "
                        "nothing about the test project"
                    ),
                )
            )
        elif failures > 0:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_FAILED,
                    total=counters["total"],
                    executed=counters["executed"],
                    passed=counters["passed"],
                    failed=failures,
                    detail=(
                        f"{failures} failing test(s) of {counters['executed']} "
                        "executed"
                        + (
                            f" ({counters['inconclusive']} inconclusive)"
                            if counters["inconclusive"]
                            else ""
                        )
                    ),
                )
            )
        else:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_PASSED,
                    total=counters["total"],
                    executed=counters["executed"],
                    passed=counters["passed"],
                    failed=failures,
                    detail=f"{counters['passed']}/{counters['executed']} passed",
                )
            )
    return ReportReconciliation(verdicts=tuple(verdicts))


REPORT_INVENTORY_SCHEMA = "forge.verification.report-inventory/1"


def freeze_report_inventory(expected: ExpectedReports, *, contract_digest: str) -> dict:
    """Freeze the expected-report inventory WITH the work contract.

    R36-14: the report set a verified verdict must show is decided at
    DISPATCH time — from the qualification's :class:`ExpectedReports`
    machinery and the digest of the work contract (the executable
    spec) it ships with — and recorded as one document on the run. At
    VERDICT time the observed reports reconcile against THIS frozen
    document, never against a live recomputation: a post-hoc recipe
    edit cannot shrink the expected set under a verdict, and a missing
    report / skipped required check / report from an older attempt can
    never produce ``verified_ready``.

    The document carries its own ``inventory_digest`` (canonical-JSON
    sha256 over schema + contract digest + rows) so a tampered row is
    detectable against the digest that was recorded with it.
    """
    rows = [
        {
            "test_project": report.test_project,
            "report_path": report.report_path,
            "candidate_id": report.candidate_id,
            "bundle_digest": report.bundle_digest,
        }
        for report in expected.reports
    ]
    inventory_digest = canonical_json_digest(
        {
            "schema": REPORT_INVENTORY_SCHEMA,
            "contract_digest": str(contract_digest or ""),
            "reports": rows,
        }
    )
    return {
        "schema": REPORT_INVENTORY_SCHEMA,
        "contract_digest": str(contract_digest or ""),
        "reports": rows,
        "inventory_digest": inventory_digest,
    }


REPORTS_ALL_PASSED = "all_passed"


REPORTS_PROBLEMS = "problems"
