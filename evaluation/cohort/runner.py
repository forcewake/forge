"""The cohort runner — drives forge's public surface, records everything (A17).

This is the evaluation HARNESS script, not part of the forge service. It
speaks to forge the way an operator does — an issue, ``/implement``, a read
of the plan comment, ``/go <run-id>``, ``/status`` probes, ``/cancel`` /
``/retry`` — over the GitHub ``gh`` CLI against ONE configured lab repo.
Stdlib + subprocess only: it never imports ``forge`` and never touches the
control plane's database; usage arrives from the R23 receipt-ledger EXPORT
(``--export``, the JSON shape documented in
docs/operations/delivery-cohort.md) and is attached per attempt.

The record-keeping contract: EVERY attempt is appended to the ledger —
failed, blocked, cancelled and superseded attempts keep their receipts — and
acceptance is a HUMAN verdict (``accept`` subcommand) against the
predeclared checks, never the agent's self-reported success.

Subcommands::

    new        --repo owner/lab [--driver d --model m] --ledger L
    run        UNIT --repo owner/lab --ledger L [--deadline-s S] [--poll-s S]
    observe    --ledger L [--export export.json] [--all | UNIT ...]
    checks     UNIT --ledger L --workdir DIR
    accept     UNIT --ledger L --verdict accepted|rejected|superseded|cancelled
               [--decided-by NAME] [--notes ...]
    report     --ledger L [--pricebook prices.json] [--out report.json]

A live cohort pass needs ``gh auth setup-git`` once (the seed push uses
git-over-HTTPS); nothing here runs in the unit tests — they cover the pure
parts (parsing, export attachment, check evaluation, aggregation).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.cohort import aggregate
from evaluation.cohort import ledger as cohort_ledger
from evaluation.cohort.tasks import (
    ACCEPTANCE_TIMEOUT_S,
    COHORT_TASKS,
    TASKS_BY_ID,
    AcceptanceCheck,
    CohortTask,
    materialize_seed,
)

__all__ = [
    "GhCli",
    "RunnerError",
    "attach_export",
    "evaluate_checks",
    "extract_run_id",
    "main",
    "parse_status_reply",
]

#: forge run ids are 32-hex; the plan comment instructs ``/go <run-id>``.
_RUN_ID_RE = re.compile(r"/go\s+([0-9a-f]{32})")

#: The ``/status`` reply's one durable fact (``forge.runs.revival.format_status_reply``).
_STATUS_RE = re.compile(r"- \*\*Status:\*\* `([a-z_]+)`")

READY_STATUS = "ready_for_human"
TERMINAL_STATUSES = frozenset({READY_STATUS, "blocked", "failed", "cancelled"})


class RunnerError(RuntimeError):
    """The harness could not drive or observe a unit."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def extract_run_id(plan_comment: str) -> str | None:
    """The full run id from a plan comment, or None (never a guess)."""
    match = _RUN_ID_RE.search(plan_comment or "")
    return match.group(1) if match else None


def parse_status_reply(body: str) -> str | None:
    """The run status word from a ``/status`` reply body, or None."""
    match = _STATUS_RE.search(body or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# The gh CLI boundary (thin; injectable executor keeps it honest in review)
# ---------------------------------------------------------------------------


class GhCli:
    """The only forge-facing surface the cohort uses: comments and reads."""

    def __init__(
        self,
        repo: str,
        execute: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.repo = repo
        self._execute = execute or _run_quiet

    def _gh(self, *argv: str, stdin: str | None = None) -> str:
        completed = self._execute(["gh", *argv])
        if completed.returncode != 0:
            raise RunnerError(
                f"gh {' '.join(argv[:3])}... failed ({completed.returncode}): "
                f"{(completed.stderr or '').strip()[:400]}"
            )
        return completed.stdout or ""

    def create_issue(self, title: str, body: str) -> int:
        out = self._gh("issue", "create", "--repo", self.repo, "--title", title, "--body", body)
        tail = out.strip().rstrip("/").rsplit("/", 1)[-1]
        if not tail.isdigit():
            raise RunnerError(f"could not parse an issue number out of {out.strip()!r}")
        return int(tail)

    def add_comment(self, number: int, body: str) -> None:
        self._gh(
            "issue",
            "comment",
            str(number),
            "--repo",
            self.repo,
            "--body",
            body,
        )

    def comments(self, number: int) -> list[dict[str, Any]]:
        raw = self._gh(
            "api", f"repos/{self.repo}/issues/{number}/comments", "--paginate", "--slurp"
        )
        pages = json.loads(raw) if raw.strip() else []
        return [comment for page in pages for comment in page] if pages else []

    def issue(self, number: int) -> dict[str, Any]:
        return dict(json.loads(self._gh("api", f"repos/{self.repo}/issues/{number}")))

    def pr_for_branch(self, branch: str) -> dict[str, Any] | None:
        raw = self._gh(
            "api",
            f"repos/{self.repo}/pulls?head={self.repo.split('/')[0]}:{branch}&state=all",
        )
        pulls = json.loads(raw) if raw.strip() else []
        return dict(pulls[0]) if pulls else None

    def pr_checks_concluded(self, pr_number: int) -> bool | None:
        """True/False once every rollup check left pending; None when no PR."""
        raw = self._gh("api", f"repos/{self.repo}/pulls/{pr_number}")
        pr = json.loads(raw)
        head_sha = str(pr.get("head", {}).get("sha") or "")
        if not head_sha:
            return None
        raw_runs = self._gh("api", f"repos/{self.repo}/commits/{head_sha}/check-runs?per_page=100")
        runs = json.loads(raw_runs)
        check_runs = [run for run in runs.get("check_runs", []) if run.get("name")]
        if not check_runs:
            return None
        return all(run.get("status") == "completed" for run in check_runs)


def _run_quiet(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True)  # noqa: S603


# ---------------------------------------------------------------------------
# Acceptance checks (mechanical, worktree-local)
# ---------------------------------------------------------------------------


def evaluate_checks(
    worktree: Path,
    checks: Sequence[AcceptanceCheck],
    *,
    python: str = sys.executable,
    timeout_s: float = ACCEPTANCE_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Run every predeclared check in *worktree*; exit 0 == pass.

    Independent by construction: checks read only the worktree the candidate
    produced (oracles recompute their expected values from seed data), never
    the agent's claims. A crashing check FAILS — an error is not a pass.
    """
    results: list[dict[str, Any]] = []
    for check in checks:
        argv = [python if arg == "{python}" else arg for arg in check.argv]
        try:
            completed = subprocess.run(  # noqa: S603
                argv,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            passed = completed.returncode == 0
            detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({"name": check.name, "passed": passed, "detail": detail})
    return results


# ---------------------------------------------------------------------------
# The R23 receipt-ledger export attachment
# ---------------------------------------------------------------------------


def load_export(path: Path) -> dict[str, Any]:
    """Load and shape-check a receipt-ledger export (see the ops doc)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != "forge.cohort.export/1":
        raise RunnerError(f"{path}: not a forge.cohort.export/1 export")
    if not isinstance(raw.get("runs"), dict):
        raise RunnerError(f"{path}: export carries no runs mapping")
    return raw


def attach_export(
    ledger_data: dict[str, Any],
    unit_id: str,
    attempt_no: int,
    export: Mapping[str, Any],
) -> int:
    """Attach one run's receipts + llm_calls to its attempt; returns receipts.

    Honesty: a run missing from the export attaches NOTHING — the attempt
    keeps ``export_attached: false`` and aggregation counts its spend as
    unknown instead of fabricating zeros.
    """
    units = ledger_data.get("units")
    if not isinstance(units, dict) or unit_id not in units:
        raise RunnerError(f"unit {unit_id!r} is not in this ledger")
    attempts = units[unit_id].get("attempts")
    if not isinstance(attempts, list) or not 1 <= attempt_no <= len(attempts):
        raise RunnerError(f"{unit_id}: no attempt #{attempt_no}")
    attempt = attempts[attempt_no - 1]
    run = export.get("runs", {}).get(str(attempt.get("run_id")))
    if not isinstance(run, Mapping):
        attempt["export_attached"] = False
        return 0
    attempt["receipts"] = list(run.get("usage_receipts") or [])
    attempt["llm_calls"] = list(run.get("llm_calls") or [])
    attempt["export_attached"] = True
    if attempt.get("commit_cycle") is None and run.get("commit_cycle") is not None:
        attempt["commit_cycle"] = run.get("commit_cycle")
    if not attempt.get("terminal_status") and run.get("status"):
        attempt["terminal_status"] = run.get("status")
    return len(attempt["receipts"])


# ---------------------------------------------------------------------------
# The drive loop (impure: gh + git + clock; never exercised by unit tests)
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _render_ci_workflow(task: CohortTask) -> str:
    """A per-unit CI workflow rendered FROM the predeclared checks.

    Shipping forge's own ``ci.yml`` into a fixture repo guaranteed a
    permanently red baseline — a fixture carries no ``pyproject.toml``, so
    every forge job dies at "Install dependencies" and the PR's checks can
    never go green (LIVE-found 2026-09-20: CU-01 burned repair lanes in a
    loop no candidate could close). The seed instead ships a workflow that
    proves exactly the unit's acceptance contract: one step per predeclared
    check, nothing else.
    """
    lines = [
        "name: cohort-checks",
        "",
        "on:",
        "  pull_request:",
        "  push:",
        "",
        "permissions: {}",
        "",
        "jobs:",
        "  checks:",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 10",
        "    steps:",
        "      - uses: actions/checkout@v4",
    ]
    for check in task.checks:
        argv = ["python3" if arg == "{python}" else arg for arg in check.argv]
        lines.append(f"      - name: {check.name}")
        lines.append("        run: |")
        # An argv element may itself be multi-line (e.g. a ``-c`` payload);
        # shlex.join keeps it one shell word across the newlines, and every
        # line must carry the block-scalar indent or the YAML breaks.
        for command_line in shlex.join(argv).splitlines():
            lines.append("          " + command_line)
    return "\n".join(lines) + "\n"


def _seed_branch(task: CohortTask, repo: str) -> None:
    """Force-push the unit's fixture seed onto the lab repo's default branch.

    Units run SEQUENTIALLY: the seed must be what the run's base branch
    holds when the issue is opened. This is why ``run`` refuses to start
    while another unit of the same pass is still in flight.
    """
    with tempfile.TemporaryDirectory(prefix=f"forge-cohort-{task.unit_id}-") as tmp:
        seed_dir = Path(tmp) / "seed"
        materialize_seed(task, seed_dir)

        def git(*argv: str) -> None:
            _git(seed_dir, *argv)

        git("init", "-b", "main")  # -b: git >= 2.28; --branch is not a git init option
        # The lane workflow MUST ship with the seed: without
        # .github/workflows/forge-harness.yml the dispatch 422s
        # ("workflow does not have 'workflow_dispatch' trigger") and the
        # unit dies before its agent starts (LIVE-found twice).
        workflows_src = _REPO_ROOT / ".github" / "workflows"
        workflows_dst = seed_dir / ".github" / "workflows"
        workflows_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workflows_src / "forge-harness.yml", workflows_dst / "forge-harness.yml")
        # The PR checks must prove the UNIT's contract, not forge's: the
        # rendered ci.yml runs exactly the predeclared checks (see
        # _render_ci_workflow for why forge's own ci.yml is never shipped).
        (workflows_dst / "ci.yml").write_text(_render_ci_workflow(task), encoding="utf-8")
        git("add", ".")
        git(
            "-c",
            "user.name=forge-cohort",
            "-c",
            "user.email=cohort@forge.local",
            "commit",
            "--no-verify",
            "-m",
            f"cohort seed {task.unit_id}",
        )
        git("remote", "add", "origin", f"https://github.com/{repo}.git")
        git("push", "--force", "origin", "main")


def _git(cwd: Path, *argv: str) -> None:
    completed = subprocess.run(  # noqa: S603
        ["git", *argv], cwd=cwd, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise RunnerError(
            f"git {' '.join(argv[:2])}... failed ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout or '').strip()[:400]}"
        )


def _wait_until(deadline_epoch: float, poll_s: float, probe: Callable[[], str | None]) -> str:
    """Poll *probe* until it returns a value or the deadline passes."""
    while True:
        value = probe()
        if value is not None:
            return value
        if time.time() > deadline_epoch:
            raise TimeoutError("observation deadline hit")
        time.sleep(poll_s)


def _latest_plan_run_id(gh: GhCli, issue_number: int) -> str | None:
    for comment in reversed(gh.comments(issue_number)):
        body = str(comment.get("body") or "")
        if "Forge plan" in body:
            return extract_run_id(body)
    return None


def _probe_status(gh: GhCli, issue_number: int, run_id: str) -> str | None:
    """Post ``/status <run-id>`` and read the reply — the public probe."""
    gh.add_comment(issue_number, f"/status {run_id}")
    time.sleep(2.0)
    for comment in reversed(gh.comments(issue_number)):
        body = str(comment.get("body") or "")
        if f"run `{run_id[:8]}` status" in body:
            return parse_status_reply(body)
    return None


def _record_attempt(
    ledger_path: Path, ledger_data: dict[str, Any], unit_id: str, attempt: dict
) -> int:
    attempt_no = cohort_ledger.upsert_attempt(ledger_data, unit_id, attempt)
    cohort_ledger.save_ledger(ledger_data, ledger_path)
    return attempt_no


def drive_unit(
    task: CohortTask,
    gh: GhCli,
    ledger_path: Path,
    ledger_data: dict[str, Any],
    *,
    deadline_s: float = 3600.0,
    poll_s: float = 15.0,
) -> int:
    """One full public-surface drive: seed → issue → /implement → /go → record.

    Records the attempt REGARDLESS of outcome; a timeout mid-drive appends
    the attempt with whatever stamps are known and re-raises. Cancels (CU-14)
    and lane deaths (CU-13) are operator procedures layered ON this drive —
    the ledger keeps their attempts exactly the same way.
    """
    if task.unit_id not in ledger_data.get("units", {}):
        cohort_ledger.open_unit(ledger_data, task)
        cohort_ledger.save_ledger(ledger_data, ledger_path)
    deadline_epoch = time.time() + deadline_s
    attempt = cohort_ledger.attempt_record(run_id="", started_at=_utc_now_iso())
    attempt_no = _record_attempt(ledger_path, ledger_data, task.unit_id, attempt)

    try:
        _seed_branch(task, gh.repo)
        issue_number = gh.create_issue(task.title, task.issue_body)
        gh.add_comment(issue_number, "/implement")
        run_id = _wait_until(deadline_epoch, poll_s, lambda: _latest_plan_run_id(gh, issue_number))
        attempt["run_id"] = run_id
        attempt["plan_seen_at"] = _utc_now_iso()
        gh.add_comment(issue_number, f"/go {run_id}")
        attempt["go_posted_at"] = _utc_now_iso()
        _record_attempt(ledger_path, ledger_data, task.unit_id, attempt)

        def candidate_or_terminal() -> str | None:
            status = _probe_status(gh, issue_number, run_id)
            if status == READY_STATUS:
                attempt["candidate_seen_at"] = attempt.get("candidate_seen_at") or _utc_now_iso()
                attempt["terminal_status"] = status
                return status
            if status in TERMINAL_STATUSES:
                attempt["terminal_seen_at"] = _utc_now_iso()
                attempt["terminal_status"] = status
                return status
            return None

        _wait_until(deadline_epoch, poll_s, candidate_or_terminal)
    except (TimeoutError, RunnerError) as exc:
        attempt["notes"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        cohort_ledger.save_ledger(ledger_data, ledger_path)
    return attempt_no


def cancel_unit(
    gh: GhCli,
    ledger_path: Path,
    ledger_data: dict[str, Any],
    unit_id: str,
    attempt_no: int,
    issue_number: int,
) -> None:
    """The CU-14 procedure: ``/cancel <run-id>`` mid-run, stamp the attempt.

    The cancelled attempt is always the IN-FLIGHT drive — the ledger's
    last row: indexing by *attempt_no* posted a stale historical run id
    when earlier rows were duplicated, and forge correctly ignored the
    unknown-run cancel (LIVE-found 2026-09-20).
    """
    attempts = ledger_data["units"][unit_id]["attempts"]
    attempt = attempts[-1]
    gh.add_comment(issue_number, f"/cancel {attempt['run_id']}")
    attempt["terminal_seen_at"] = _utc_now_iso()
    attempt["terminal_status"] = "cancelled"
    cohort_ledger.save_ledger(ledger_data, ledger_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_or_exit(path: Path) -> dict[str, Any]:
    try:
        return cohort_ledger.load_ledger(path)
    except (OSError, ValueError, cohort_ledger.CohortError) as exc:
        raise RunnerError(str(exc)) from exc


def _cmd_new(args: argparse.Namespace) -> int:
    ledger_data = cohort_ledger.new_ledger(
        args.repo,
        {"driver": args.driver, "model": args.model, "budget_class": args.budget_class},
    )
    for task in COHORT_TASKS:
        cohort_ledger.open_unit(ledger_data, task)
    cohort_ledger.save_ledger(ledger_data, Path(args.ledger))
    print(f"opened {len(COHORT_TASKS)} units in {args.ledger}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger_data = _load_or_exit(ledger_path)
    task = TASKS_BY_ID[args.unit]
    in_flight = [
        unit["unit_id"]
        for unit in ledger_data["units"].values()
        if unit["acceptance"]["verdict"] == "pending" and unit["attempts"]
    ]
    if in_flight:
        raise RunnerError(
            f"units still in flight ({', '.join(in_flight)}) — units run SEQUENTIALLY "
            "(the seed force-pushes the default branch); observe/accept them first"
        )
    gh = GhCli(args.repo)
    drive_unit(task, gh, ledger_path, ledger_data, deadline_s=args.deadline_s, poll_s=args.poll_s)
    print(f"{task.unit_id}: attempt recorded")
    return 0


def _cmd_observe(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger_data = _load_or_exit(ledger_path)
    export = load_export(Path(args.export)) if args.export else None
    unit_ids = list(ledger_data["units"]) if args.all else args.units
    attached = 0
    for unit_id in unit_ids:
        unit = ledger_data["units"].get(unit_id)
        if unit is None:
            raise RunnerError(f"unit {unit_id!r} is not in this ledger")
        for attempt_no, attempt in enumerate(unit["attempts"], start=1):
            if export is not None and not attempt.get("export_attached"):
                attached += bool(attach_export(ledger_data, unit_id, attempt_no, export))
    if export is not None:
        cohort_ledger.save_ledger(ledger_data, ledger_path)
    print(f"attached receipts to {attached} attempt(s)")
    return 0


def _cmd_checks(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger_data = _load_or_exit(ledger_path)
    task = TASKS_BY_ID[args.unit]
    results = evaluate_checks(Path(args.workdir), task.checks)
    unit = ledger_data["units"].setdefault(
        args.unit,
        {"unit_id": args.unit, "axis": task.axis, "title": task.title, "attempts": []},
    )
    acceptance = unit.setdefault(
        "acceptance",
        {"verdict": "pending", "decided_by": "", "decided_at": None, "notes": "", "checks": []},
    )
    acceptance["checks"] = results
    cohort_ledger.save_ledger(ledger_data, ledger_path)
    for result in results:
        print(
            f"{'PASS' if result['passed'] else 'FAIL'} {result['name']}: {result['detail'][:120]}"
        )
    return 0 if all(result["passed"] for result in results) else 1


def _cmd_accept(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger_data = _load_or_exit(ledger_path)
    cohort_ledger.record_acceptance(
        ledger_data,
        args.unit,
        args.verdict,
        decided_by=args.decided_by,
        notes=args.notes or "",
    )
    cohort_ledger.save_ledger(ledger_data, ledger_path)
    print(f"{args.unit}: verdict {args.verdict}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    ledger_data = _load_or_exit(Path(args.ledger))
    pricebook = (
        json.loads(Path(args.pricebook).read_text(encoding="utf-8")) if args.pricebook else None
    )
    report = aggregate.cohort_report(ledger_data, pricebook)
    rendered = json.dumps(report, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    counts = report["counts"]
    print(
        f"units={counts['units']} accepted={counts['accepted_units']} "
        f"attempts={counts['attempts']} (report schema {aggregate.REPORT_SCHEMA})",
        file=sys.stderr,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.cohort.runner",
        description="Bounded delivery-cohort harness (A17): drive forge's public "
        "surface over 14 predeclared units and aggregate accepted-work economics.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="open a fresh ledger with all 14 units")
    new.add_argument("--repo", required=True, help="owner/lab GitHub repo")
    new.add_argument("--ledger", required=True, help="ledger JSON path")
    new.add_argument("--driver", default="", help="implementer driver under test")
    new.add_argument("--model", default="", help="model under test")
    new.add_argument("--budget-class", default="", help="budget class under test")
    new.set_defaults(func=_cmd_new)

    run = sub.add_parser("run", help="drive one unit end to end (sequential only)")
    run.add_argument("unit", choices=list(TASKS_BY_ID))
    run.add_argument("--repo", required=True)
    run.add_argument("--ledger", required=True)
    run.add_argument("--deadline-s", type=float, default=3600.0)
    run.add_argument("--poll-s", type=float, default=15.0)
    run.set_defaults(func=_cmd_run)

    observe = sub.add_parser("observe", help="attach receipt exports to attempts")
    observe.add_argument("--ledger", required=True)
    observe.add_argument("--export", help="forge.cohort.export/1 JSON from the R23 ledger")
    observe.add_argument("units", nargs="*", help="unit ids (default: none)")
    observe.add_argument("--all", action="store_true", help="attach across every unit")
    observe.set_defaults(func=_cmd_observe)

    checks = sub.add_parser("checks", help="run the predeclared checks in a candidate worktree")
    checks.add_argument("unit", choices=list(TASKS_BY_ID))
    checks.add_argument("--ledger", required=True)
    checks.add_argument("--workdir", required=True, help="checkout of the candidate branch")
    checks.set_defaults(func=_cmd_checks)

    accept = sub.add_parser("accept", help="record the HUMAN acceptance verdict")
    accept.add_argument("unit", choices=list(TASKS_BY_ID))
    accept.add_argument("--ledger", required=True)
    accept.add_argument("--verdict", required=True, choices=cohort_ledger.VERDICTS)
    accept.add_argument("--decided-by", default="")
    accept.add_argument("--notes", default="")
    accept.set_defaults(func=_cmd_accept)

    report = sub.add_parser("report", help="aggregate the ledger into the report artifact")
    report.add_argument("--ledger", required=True)
    report.add_argument("--pricebook", help="USD-per-Mtoken pricebook JSON (optional)")
    report.add_argument("--out", help="write the report JSON here (default: stdout)")
    report.set_defaults(func=_cmd_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (RunnerError, cohort_ledger.CohortError) as exc:
        print(f"cohort: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
