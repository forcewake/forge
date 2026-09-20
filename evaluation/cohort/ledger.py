"""The cohort ledger — every attempt, kept (A17).

The ledger is the append-only record of ONE cohort pass: per unit, EVERY
attempt ever driven (failed, blocked, cancelled and superseded attempts stay
in the ledger forever — a later successful sibling never erases a failure,
mirroring :data:`forge.runs.metrics.TERMINAL_FAILURE_STATUSES`), the R23
usage receipts and ``llm_calls`` rows attached to each attempt, the wall-clock
phase stamps, and the explicit HUMAN acceptance verdict.

Shape (``forge.cohort.ledger/1``)::

    {
      "schema": "forge.cohort.ledger/1",
      "contract_version": "forge.delivery-cohort/1",
      "repo": "owner/lab-repo",
      "profile": {"driver": "...", "model": "...", "budget_class": "..."},
      "opened_at": "2026-09-17T00:00:00+00:00",
      "units": {
        "CU-05-repair-temperature": {
          "unit_id": "...", "axis": "repair", "title": "...",
          "path_scope": [],
          "attempts": [AttemptRecord, ...],
          "acceptance": {"verdict": "pending", "decided_by": "", ...}
        }
      }
    }

Everything is plain JSON so the artifact survives without this module; the
functions here only construct, extend and validate it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.cohort.tasks import CONTRACT_VERSION, CohortTask

__all__ = [
    "LEDGER_SCHEMA",
    "VERDICTS",
    "CohortError",
    "attempt_record",
    "load_ledger",
    "new_ledger",
    "open_unit",
    "record_acceptance",
    "record_attempt",
    "save_ledger",
]

LEDGER_SCHEMA = "forge.cohort.ledger/1"

#: The explicit acceptance verdicts. Only a human ``accept`` moves a unit off
#: ``pending`` — the agent's self-reported success is never a verdict.
VERDICTS: tuple[str, ...] = ("pending", "accepted", "rejected", "superseded", "cancelled")

#: Attempt kinds — the retry linkage the review asked to keep visible.
ATTEMPT_KINDS: tuple[str, ...] = ("initial", "retry", "replan")


class CohortError(RuntimeError):
    """A cohort harness contract violation (bad ledger shape, bad verdict)."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_ledger(repo: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """A fresh ledger for ONE cohort pass against one configured lab repo.

    ``profile`` records the implementation shape under test (driver, model,
    budget class) — the harness-comparison label. Two passes compare only
    when the tasks AND the contract version AND the question asked match.
    """
    return {
        "schema": LEDGER_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "repo": repo,
        "profile": dict(profile or {}),
        "opened_at": _utc_now_iso(),
        "units": {},
    }


def _unit(ledger: dict[str, Any], unit_id: str) -> dict[str, Any]:
    units = ledger.get("units")
    if not isinstance(units, dict) or unit_id not in units:
        raise CohortError(f"unit {unit_id!r} is not open in this ledger")
    return units[unit_id]


def open_unit(ledger: dict[str, Any], task: CohortTask) -> dict[str, Any]:
    """Register a cohort task as a unit with verdict ``pending``."""
    units = ledger.setdefault("units", {})
    if task.unit_id in units:
        raise CohortError(f"unit {task.unit_id!r} already open in this ledger")
    unit: dict[str, Any] = {
        "unit_id": task.unit_id,
        "axis": task.axis,
        "title": task.title,
        "path_scope": list(task.path_scope),
        "attempts": [],
        "acceptance": {
            "verdict": "pending",
            "decided_by": "",
            "decided_at": None,
            "notes": "",
            "checks": [],
        },
    }
    units[task.unit_id] = unit
    return unit


def attempt_record(
    *,
    run_id: str,
    kind: str = "initial",
    started_at: str | None = None,
    go_posted_at: str | None = None,
    plan_seen_at: str | None = None,
    candidate_seen_at: str | None = None,
    terminal_seen_at: str | None = None,
    ci_concluded_at: str | None = None,
    terminal_status: str | None = None,
    rework_of: str | None = None,
    superseded_by: str | None = None,
    commit_cycle: int | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """One attempt row. Usage stays EMPTY until an export is attached.

    ``receipts``/``llm_calls`` are filled only from the R23 receipt-ledger
    export (:func:`evaluation.cohort.runner.attach_export`) — an attempt with
    no export attached counts as UNMEASURED spend in aggregation, never as
    zero spend.
    """
    if kind not in ATTEMPT_KINDS:
        raise CohortError(f"unknown attempt kind {kind!r}")
    return {
        "run_id": run_id,
        "kind": kind,
        "recorded_at": _utc_now_iso(),
        "started_at": started_at,
        "plan_seen_at": plan_seen_at,
        "go_posted_at": go_posted_at,
        "candidate_seen_at": candidate_seen_at,
        "ci_concluded_at": ci_concluded_at,
        "terminal_seen_at": terminal_seen_at,
        "terminal_status": terminal_status,
        "rework_of": rework_of,
        "superseded_by": superseded_by,
        "commit_cycle": commit_cycle,
        "receipts": [],
        "llm_calls": [],
        "export_attached": False,
        "notes": notes,
    }


def record_attempt(ledger: dict[str, Any], unit_id: str, attempt: dict[str, Any]) -> int:
    """Append an attempt (append-only: nothing ever removed). Returns the 1-based attempt_no."""
    unit = _unit(ledger, unit_id)
    unit["attempts"].append(attempt)
    return len(unit["attempts"])


def upsert_attempt(ledger: dict[str, Any], unit_id: str, attempt: dict[str, Any]) -> int:
    """Append the attempt, or pass through when it is already the last row.

    ``drive_unit`` records at drive start and re-stamps after ``/go`` with
    the same dict; appending twice DUPLICATED every attempt row, which
    desynced ``cancel_unit``'s attempt indexing into posting a stale run id
    (LIVE-found 2026-09-20). The row is appended exactly once; later calls
    persist the caller's in-place stamps.
    """
    unit = _unit(ledger, unit_id)
    attempts = unit["attempts"]
    if not (attempts and attempts[-1] is attempt):
        attempts.append(attempt)
    return len(attempts)


def record_acceptance(
    ledger: dict[str, Any],
    unit_id: str,
    verdict: str,
    *,
    decided_by: str,
    notes: str = "",
    checks: list[dict[str, Any]] | None = None,
) -> None:
    """Record the explicit HUMAN verdict for a unit (replaces ``pending``).

    The verdict is the operator's decision against the predeclared checks —
    the only acceptance the cohort recognizes. Re-recording overwrites the
    earlier verdict (with a fresh timestamp); the attempts ledger is never
    touched here.
    """
    if verdict not in VERDICTS:
        raise CohortError(f"unknown verdict {verdict!r}; expected one of {VERDICTS}")
    unit = _unit(ledger, unit_id)
    unit["acceptance"] = {
        "verdict": verdict,
        "decided_by": decided_by,
        "decided_at": _utc_now_iso(),
        "notes": notes,
        "checks": list(checks if checks is not None else unit["acceptance"].get("checks", [])),
    }


def save_ledger(ledger: dict[str, Any], path: Path) -> None:
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise CohortError(f"not a {LEDGER_SCHEMA} ledger: {ledger.get('schema')!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def load_ledger(path: Path) -> dict[str, Any]:
    """Load and validate a ledger file (schema + per-unit structure)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != LEDGER_SCHEMA:
        raise CohortError(f"{path}: not a {LEDGER_SCHEMA} ledger")
    units = raw.get("units")
    if not isinstance(units, dict):
        raise CohortError(f"{path}: ledger carries no units mapping")
    for unit_id, unit in units.items():
        if unit.get("unit_id") != unit_id:
            raise CohortError(
                f"{path}: unit key {unit_id!r} mismatches unit_id {unit.get('unit_id')!r}"
            )
        if unit.get("acceptance", {}).get("verdict") not in VERDICTS:
            raise CohortError(f"{path}: unit {unit_id} carries an unknown verdict")
        for attempt in unit.get("attempts", []):
            if attempt.get("kind") not in ATTEMPT_KINDS:
                raise CohortError(f"{path}: unit {unit_id} has an attempt with unknown kind")
    if raw.get("contract_version") != CONTRACT_VERSION:
        raise CohortError(
            f"{path}: contract {raw.get('contract_version')!r} != {CONTRACT_VERSION!r} "
            "(passes are only comparable under the same contract)"
        )
    return raw
