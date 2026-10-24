"""Reduce independent native check attempts without inventing verification."""

from datetime import datetime
import json
from typing import Literal

from forge.domain.verdicts import CheckVerdict
from forge.port_values import CheckResult


def verdict(
    checks: tuple[CheckResult, ...],
    sha: str,
    required: tuple[str, ...],
    *,
    now: datetime,
    max_age_seconds: int,
    missing_is_final: bool = False,
) -> CheckVerdict:
    independent = [item for item in checks if item.role == "verification" and item.name in required]
    selected: list[CheckResult] = []
    ambiguous = False
    for name in required:
        matches = [item for item in independent if item.name == name]
        if not matches:
            continue
        if len({(item.workflow_path, item.native_run_id) for item in matches}) != 1:
            ambiguous = True
        newest = max(item.attempt for item in matches)
        latest = [item for item in matches if item.attempt == newest]
        if (
            len(
                {
                    (item.native_id, item.status, item.tested_sha, item.failure_kind)
                    for item in latest
                }
            )
            != 1
        ):
            ambiguous = True
        selected.append(latest[0])
    stale = any(
        item.tested_sha != sha
        or not 0 <= (now - item.observed_at).total_seconds() <= max_age_seconds
        for item in selected
    )
    successful = tuple(sorted(item.name for item in selected if item.status == "passed"))
    status: Literal["pending", "passed", "failed", "unknown", "not_configured", "stale"]
    if stale:
        status = "stale"
    elif ambiguous:
        status = "unknown"
    elif any(item.status == "failed" for item in selected):
        status = "failed"
    elif any(item.status in ("cancelled", "skipped", "unknown") for item in selected):
        status = "unknown"
    elif len(selected) != len(required):
        status = "not_configured" if missing_is_final else "pending"
    elif any(item.status == "pending" for item in selected):
        status = "pending"
    else:
        status = "passed"
    evidence = json.dumps(
        {"checks": [item.model_dump(mode="json") for item in selected]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return CheckVerdict(
        status=status,
        tested_sha=sha,
        required=required,
        successful=successful,
        observed_at=now,
        evidence=evidence,
    )


def code_failure(checks: tuple[CheckResult, ...], result: CheckVerdict) -> bool:
    if result.status != "failed":
        return False
    selected = tuple(
        CheckResult.model_validate_json(json.dumps(item))
        for item in json.loads(result.evidence)["checks"]
    )
    failed = [item for item in selected if item.status == "failed"]
    return bool(failed) and all(item.failure_kind == "code" for item in failed)
