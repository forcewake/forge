#!/usr/bin/env python3
"""R38-12 (#313 / AT-10 composed) — the COMBINED live steering trace.

The two halves R37-10 proved separately — the scripted process-level
trace (commands + revisions through the real machinery) and a real-model
conversation pair — are COMPOSED here on ONE live lab run:

- a bounded real task with a MID-WORK decision point: the issue plans
  approach X (the entrypoint ``check``); the operator's NATIVE ``/steer``
  lands while the REAL claude-sdk lane is running the REAL model
  (litellm ``fast``) and redirects it to approach Y (the entrypoint
  ``validate_email``) — an independently checkable transformation whose
  final-diff shape the grader grades, not a judgment call;
- the observed-work waiter (the #306 pattern): the steer fires at a
  FIXED OFFSET AFTER the OBSERVED driver-phase start in the job trace,
  never a blind sleep — and only while the job is observably running;
- the causal milestones are SEPARATE durable records: the note → the
  mailbox row's audit journal (received/authorized/dispatching, one DB
  clock) → the lane's steering journal (the vendor application the lane
  observed — WHICH steering shape the driver delivered is recorded
  honestly: mid-turn or next-turn) → the checkpoint transaction → the
  material revision's activation records → the post-revision dispatch
  envelope;
- the counterfactual: the SAME task re-run WITHOUT the steer (a second
  capped lane on a second issue of the same disposable project) — its
  candidate is the unsteered edit set the grader's arm 2/3 compare
  against (model nondeterminism acknowledged, one arm, never pooled);
- the urgent-pause interleaving (backlog negative test 1): a second
  ordinary steer queued IMMEDIATELY before an urgent ``/pause`` while
  the model works — the interrupt-class pause applies first, the queued
  guidance's fate is recorded from the durable rows;
- the material revision: revision 1 (the active plan) and revision 2
  (the operator's scope extension — ``src/validators/__init__.py``
  exports join the plan) are staged through the REAL revisions module
  INSIDE the app container (the proposal-emitter leg the live GitLab
  planner has not grown yet — PE-7 covers it with a seam); the APPROVAL
  is native (``/approve-revision`` through the real ingress) and the
  activation is the REAL durable transaction: the three-way digest
  equality, the WIP reuse decision (preserve) and the re-bound gate;
- the resume: job-level cancel → the honest blocked classification →
  ``/retry`` → the fresh worker's dispatch is a REQUIRED resume over the
  PRESERVED checkpoint (the envelope names checkpoint + decision +
  digest in the worker journal) — the lane restores the Y WIP and the
  final candidate is verified by the INDEPENDENT precommitted oracle on
  the exact candidate sha; the MR stays Draft.

Phases are resumable (the evidence bundle on disk is the state); every
phase refuses on any precondition failure — a failure is recorded
honestly, never retried into a green. Spend cap: $2 TOTAL across every
lane leg (the guard refuses a new leg once recorded spend crosses
$1.60).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forge.adaptive import steering_causality as sc  # noqa: E402
from forge.config import Settings  # noqa: E402

# ---------------------------------------------------------------------------
# The record's homes
# ---------------------------------------------------------------------------

OUT_DIR = REPO_ROOT / "evaluation" / "steering" / "combined-2026-09-25"
EVIDENCE_PATH = OUT_DIR / "live-run-evidence.json"
TRACE_PATH = OUT_DIR / "combined-trace.json"
REPORT_PATH = OUT_DIR / "report.json"
APP_API = "http://localhost:8420"

#: The shipped lane template (#302 finalization) — inlined VERBATIM into
#: the disposable project's CI (never a hand-edited copy).
TEMPLATE_SOURCE = REPO_ROOT / "ci" / "templates" / "claude-sdk-lane.gitlab-ci.yml"

#: The immutable lane install pin (#306's receipts): the repo sha pushed
#: to origin carrying the generation-aware collector + the dispatch
#: envelope + the steering consumer the lane drives.
LANE_REF_SHA = "59ba869a312e9c13c120b82034b90f669a894ce6"

#: The spend cap (issue #313: "$2 total") and the guard that refuses a
#: new paid lane leg once recorded spend crosses it.
SPEND_CAP_USD = 2.0
SPEND_GUARD_USD = 1.6

#: The task's decision point: approach X (the plan's naming) vs the
#: operator's approach Y (the steer's naming).
VALIDATOR_PATH = "src/validators/email.py"
X_NAME = "check"
Y_NAME = "validate_email"
BULK_NAME = "validate_many"

#: The steer text — carries the grader's live grammar verbatim
#: (``entrypoint <new> (not <old>) in <path>``) beside its plain words.
STEER_TEXT = (
    f"Operator steer, mid-work direction change: rename the validator's public "
    f"entrypoint — entrypoint {Y_NAME} (not {X_NAME}) in {VALIDATOR_PATH}. Define "
    f"`{Y_NAME}(email: str) -> bool` as the public API and remove the `{X_NAME}` "
    "definition; keep the validation semantics, the bulk entrypoint and every "
    "other requirement of the plan unchanged."
)

#: The second (interleaving) steer: ordinary guidance queued immediately
#: before the urgent pause — deliberately cosmetic so it cannot disturb
#: the Y check while still exercising the queue-vs-interrupt race.
STEER2_TEXT = (
    f"Operator steer: give {VALIDATOR_PATH} a one-line module docstring naming "
    f"its public entrypoint."
)

#: The standing-direction re-assertion (cycle 2+): the LIVE-found failure
#: mode of cycle 1 — the resumed lane obeys the SPEC-FROZEN brief (which
#: still names approach X) over the restored WIP's naming, so the Y
#: transformation landed in the pause checkpoint and was then REVERTED in
#: the final candidate. The operator's native channel for a direction
#: that must outlive a continuation is the DURABLE guidance itself: a
#: steer posted while the run is blocked sits PENDING and the resumed
#: lane's drain delivers it mid-turn (the control plane's pending view
#: serves received/authorized rows to the continuation's cursor).
STEER_REASSERT_TEXT = (
    "Operator steer, standing direction for this continuation: the public "
    f"entrypoint of {VALIDATOR_PATH} is {Y_NAME} — entrypoint {Y_NAME} (not {X_NAME}) "
    f"in {VALIDATOR_PATH}. The restored WIP already carries that naming; keep it, "
    "finish the remaining work under it, and leave no "
    f"`{X_NAME}` definition behind."
)

#: The observed-work waiter's rung ladder for the STEER, in seconds AFTER
#: the OBSERVED driver-phase start (the #306 anchor discipline; the first
#: live lane's measured turn was ~87 s with edits landing in the first
#: half, so the steer samples the turn's middle — early enough that the
#: model has not finished, late enough that work is observably underway).
STEER_RUNGS_S: tuple[float, ...] = (35.0, 60.0)

#: How long to let the model KEEP working after the steer's vendor
#: application before the interleaving arm pauses the turn (the Y work
#: needs time to land in the working tree the checkpoint captures).
POST_STEER_DWELL_S = 45.0

#: The bounds of the whole drill.
MAX_POLL_SECONDS_DEFAULT = 1200
POLL_INTERVAL_S = 10.0
RUN_ID_RE = re.compile(r"go ([0-9a-f]{32})")

#: GitLab job-trace line shape + the driver-phase markers (#306's
#: LIVE-found spellings, reused verbatim).
_TRACE_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+\d+[OE]\s?(.*)$")
_DRIVER_PHASE_MARKERS: tuple[str, ...] = (
    "before_script created .forge",
    "FORGE_DRIVER_EXIT=",
    "mkdir -p .forge # collapsed",
)

#: The per-token price class of the lane route (glm-5.3-flash through the
#: customer gateway) — ONLY used to FLAG spend when the SDK receipt
#: carries no ``total_cost_usd``; the recorded spend prefers the SDK's
#: own cost field everywhere it exists.
FALLBACK_PRICE_PER_MTOK = {"input": 0.60, "cached_input": 0.07, "output": 2.20}

TRACE_SCHEMA = "forge.steering.combined-trace/1"
REPORT_SCHEMA = "forge.steering.combined/1"

VARIABLES_FROM_LAB = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "FORGE_BOT_READ_TOKEN",
    "FORGE_HARNESS_HTTPS_PROXY",
)


class Refused(Exception):
    """The drill hit a condition it refuses to paper over."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# The task fixture — approach X vs approach Y + the precommitted oracle
# ---------------------------------------------------------------------------


def issue_title() -> str:
    return "Add email validation with the check() entrypoint"


def issue_body() -> str:
    return f"""## Task (single-address validation, approach: one entrypoint)

Implement email validation in `{VALIDATOR_PATH}` (create the file; the
package `src/validators/` already exists):

1. The public entrypoint is `{X_NAME}(email: str) -> bool` — True iff the
   address is syntactically valid: exactly one `@`, a non-empty local
   part, a domain with at least one dot and no spaces anywhere.
2. Also provide `{BULK_NAME}(emails: list[str]) -> list[bool]` mapping the
   same verdict over a list (one result per input, same order).

### Bounds

- Create/modify ONLY `{VALIDATOR_PATH}`.
- `tests/` and `.gitlab-ci.yml` are frozen (the precommitted oracle
  lives there); a candidate that touches them fails acceptance.

### Acceptance

The repository's `smoke` CI job (committed before any run) is the
independent verifier: whichever entrypoint exists must satisfy its exact
cases, and the bulk entrypoint must exist and be consistent.
"""


def _seed_validator_stub() -> str:
    """The frozen base of the decision point: approach X's SHAPE, unimplemented."""
    return (
        '"""Email validation — the planned entrypoint lands per the plan."""\n'
        "\n"
        "\n"
        f"def {X_NAME}(email: str) -> bool:\n"
        '    raise NotImplementedError("implemented by the plan")\n'
    )


def _seed_init() -> str:
    return '"""The validators package (seed; the implementation lands in email.py)."""\n'


def _seed_readme(name: str) -> str:
    return (
        f"# {name}\n\nA DISPOSABLE repository for the R38-12 (#313) combined\n"
        "steering qualification: the native /steer + the REAL running SDK lane +\n"
        "the material revision, verified by the `smoke` oracle committed before\n"
        "any run. This project is deleted after the evidence is captured.\n"
    )


#: The oracle's exact cases — the INDEPENDENT contract, committed before
#: any run; steering can choose the entrypoint NAME, never the semantics.
ORACLE_VALID: tuple[str, ...] = (
    "user@example.com",
    "first.last@sub.domain.org",
    "a+b_tag@ex-ample.co",
)
ORACLE_INVALID: tuple[str, ...] = (
    "",
    "no-at-sign",
    "@no-local.org",
    "no-domain@",
    "user@@example.com",
    "user example@example.com",
    "user@example",
)


def smoke_oracle_script() -> str:
    valid = "".join(f'    "{item}",\n' for item in ORACLE_VALID)
    invalid = "".join(f'    "{item}",\n' for item in ORACLE_INVALID)
    return (
        "python3 - <<'PY'\n"
        "import importlib.util\n"
        "from pathlib import Path\n"
        f"path = Path({VALIDATOR_PATH!r})\n"
        "assert path.is_file(), 'src/validators/email.py must exist'\n"
        "spec = importlib.util.spec_from_file_location('email_validator', path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "entry = None\n"
        "for name in ('check', 'validate_email'):\n"
        "    fn = getattr(module, name, None)\n"
        "    if callable(fn):\n"
        "        entry = fn\n"
        "assert entry is not None, 'neither check() nor validate_email() exists'\n"
        f"VALID = [\n{valid}]\n"
        f"INVALID = [\n{invalid}]\n"
        "assert [entry(e) for e in VALID] == [True] * len(VALID), 'valid cases failed'\n"
        "assert [entry(e) for e in INVALID] == [False] * len(INVALID), 'invalid cases failed'\n"
        f"many = getattr(module, {BULK_NAME!r}, None)\n"
        "assert callable(many), 'the contract requires validate_many(emails)'\n"
        "mixed = VALID + INVALID\n"
        "assert many(mixed) == [True] * len(VALID) + [False] * len(INVALID)\n"
        "print('email oracle: entrypoint semantics + bulk scope OK')\n"
        "PY\n"
    )


def _tests_file() -> str:
    valid = "".join(f'    ("{item}", True),\n' for item in ORACLE_VALID)
    invalid = "".join(f'    ("{item}", False),\n' for item in ORACLE_INVALID)
    return (
        '"""The independent oracle, mirrored as a test file for the reviewer.\n\n'
        "Committed before any qualification run; the smoke CI job asserts the\n"
        "same cases on the exact candidate sha.\n"
        '"""\n'
        "import importlib.util\n"
        "from pathlib import Path\n\n"
        "MODULE = Path(__file__).resolve().parents[1] / 'src' / 'validators' / 'email.py'\n"
        "\n\n"
        "def _module():\n"
        "    spec = importlib.util.spec_from_file_location('email_validator', MODULE)\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(module)\n"
        "    return module\n"
        "\n\n"
        "CASES = [\n" + valid + invalid + "]\n\n"
        "def test_entrypoint_semantics() -> None:\n"
        "    module = _module()\n"
        "    entry = next(\n"
        "        (getattr(module, name) for name in ('check', 'validate_email')\n"
        "         if callable(getattr(module, name, None))),\n"
        "        None,\n"
        "    )\n"
        "    assert entry is not None\n"
        "    for email, expected in CASES:\n"
        "        assert entry(email) is expected, (email, expected)\n"
        "\n\n"
        "def test_bulk_scope() -> None:\n"
        "    many = getattr(_module(), 'validate_many', None)\n"
        "    assert callable(many)\n"
        "    verdicts = [expected for _, expected in CASES]\n"
        "    assert many([email for email, _ in CASES]) == verdicts\n"
    )


def ci_yaml() -> str:
    """The shipped template VERBATIM + the stages + the smoke oracle job."""
    template = TEMPLATE_SOURCE.read_text(encoding="utf-8")
    if "forge-agent-claude-sdk:" not in template:
        raise Refused(f"{TEMPLATE_SOURCE} carries no forge-agent-claude-sdk job — regenerate")
    if "--require-generation" not in template:
        raise Refused(
            f"{TEMPLATE_SOURCE} carries no --require-generation collector flag — "
            "the #302 finalization is not in the tree; REFUSING to ship a stale recipe"
        )
    return (
        "# Generated by scripts/run_combined_steering.py (R38-12/#313): the\n"
        "# SHIPPED SDK lane template VERBATIM (the #302 phased finalization)\n"
        "# plus this project's independent smoke oracle, committed BEFORE any\n"
        "# run.\n"
        "stages: [test, harness]\n\n"
        + template
        + "\n# The INDEPENDENT verification contract (R38-12): whichever\n"
        "# entrypoint exists must satisfy the exact cases; the bulk entrypoint\n"
        "# must exist and be consistent. Committed BEFORE any run; a candidate\n"
        "# that weakens or bypasses this job is a FAILED candidate.\n"
        "smoke:\n"
        "  stage: test\n"
        "  image: python:3.13-slim\n"
        "  rules:\n"
        "    - if: '$FORGE_RUN_ID'   # the dispatch pipeline carries no\n"
        "      when: never           # candidate yet — nothing to verify\n"
        "    - when: on_success\n"
        "  script:\n"
        "    - |\n"
        + "\n".join("      " + line for line in smoke_oracle_script().splitlines())
        + "\n"
    )


def seed_files(name: str) -> dict[str, str]:
    return {
        "README.md": _seed_readme(name),
        ".gitlab-ci.yml": ci_yaml(),
        "tests/test_email_validator.py": _tests_file(),
        "src/validators/__init__.py": _seed_init(),
        VALIDATOR_PATH: _seed_validator_stub(),
    }


# ---------------------------------------------------------------------------
# Live clients — GitLab, read-only psql, the app's read API
# ---------------------------------------------------------------------------


class GitLab:
    def __init__(self, settings: Settings) -> None:
        self.base = str(settings.GITLAB_URL.rstrip("/")) + "/api/v4"
        self.token = settings.GITLAB_TOKEN.get_secret_value()
        self.webhook_secret = settings.GITLAB_WEBHOOK_SECRET.get_secret_value()
        self.client = httpx.Client(
            base_url=self.base, headers={"PRIVATE-TOKEN": self.token}, timeout=60.0
        )

    def get(self, path: str, **kwargs: Any) -> Any:
        response = self.client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_text(self, path: str, **kwargs: Any) -> str:
        response = self.client.get(path, **kwargs)
        response.raise_for_status()
        return response.text

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.post(path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.put(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.delete(path, **kwargs)


def psql(sql: str) -> str:
    """Read-only SELECT through the lab postgres container."""
    completed = subprocess.run(
        [
            "podman",
            "exec",
            "forge-postgres",
            "psql",
            "-U",
            "forge",
            "-d",
            "forge",
            "-t",
            "-A",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise Refused(f"read-only psql failed: {completed.stderr.strip()[:200]}")
    return completed.stdout


def app_get(path: str) -> Any:
    response = httpx.get(f"{APP_API}{path}", timeout=30.0)
    response.raise_for_status()
    return response.json()


def poll(
    predicate: Callable[[], Any],
    description: str,
    *,
    timeout: float = MAX_POLL_SECONDS_DEFAULT,
    interval: float = POLL_INTERVAL_S,
) -> Any:
    """Bounded wait — returns the predicate's truthy value or REFUSES."""
    deadline = _ts() + timeout
    last: Any = None
    while _ts() < deadline:
        try:
            last = predicate()
        except Exception as exc:  # noqa: BLE001 — transient probe errors keep polling
            last = f"probe error: {exc}"
        if last:
            return last
        time.sleep(interval)
    raise Refused(f"timed out after {timeout:.0f}s waiting for {description} (last: {last})")


class Bundle:
    """The resumable evidence bundle — the drill's state on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_file():
            self.document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.document = {
                "stamp": "forge.combined-steering.live/1",
                "issue": "R38-12 (#313) combined steering qualification",
                "created_at": _now(),
                "phases": {},
                "project": {},
            }

    def phase(self, name: str) -> dict[str, Any]:
        return self.document["phases"].setdefault(name, {"started_at": _now()})

    def record(self, phase: str, key: str, value: Any) -> None:
        entry = self.phase(phase)
        entry[key] = value
        entry["updated_at"] = _now()
        self.save()

    def append(self, phase: str, key: str, value: Any) -> None:
        entry = self.phase(phase)
        values = entry.setdefault(key, [])
        values.append(value)
        entry["updated_at"] = _now()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def _record_failure(bundle: Bundle, reason: str) -> None:
    bundle.document.setdefault("failures", []).append({"at": _now(), "reason": reason})
    bundle.save()


# ---------------------------------------------------------------------------
# The durable control-plane reads (the causal milestones' evidence)
# ---------------------------------------------------------------------------


def control_rows(work_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """The durable control_commands rows for one work, sequence order."""
    where = f"work_id = '{work_id}'"
    if kind:
        where += f" AND kind = '{kind}'"
    raw = psql(
        "SELECT row_to_json(t)::text FROM (SELECT id, kind, status, sequence, "
        "dedup_key, payload, journal, created_at FROM control_commands WHERE "
        f"{where} ORDER BY sequence) t"
    )
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _rungs(row: Mapping[str, Any]) -> list[str]:
    """The row's OWN guarded transitions, in order (the lane's appended
    evidence rows carry ``lane_ack`` instead and are read separately)."""
    return [str(entry.get("to") or "") for entry in (row.get("journal") or []) if entry.get("to")]


def _rung_at(row: Mapping[str, Any], rung: str) -> str:
    return next(
        (
            str(entry.get("at") or "")
            for entry in (row.get("journal") or [])
            if entry.get("to") == rung
        ),
        "",
    )


def _lane_ack_at(row: Mapping[str, Any], state: str) -> str:
    """The first lane-evidence append whose acked state matches."""
    for entry in row.get("journal") or []:
        if entry.get("lane_ack") == state:
            return str(entry.get("at") or "")
    return ""


def application_moments(row: Mapping[str, Any]) -> dict[str, str]:
    """The steer's delivery moments from one durable row (pure).

    ``applied_at`` — the lane's CAS-accepted delivery rung: the row's own
    ``dispatching`` transition (the gate the vendor effect is gated on).
    ``application_observed_at`` — the lane-observed vendor application:
    the lane's ``applied``/``checkpointed`` evidence append, falling back
    to the row's ``checkpointed`` rung. Both are DB-clock instants.
    """
    return {
        "received_at": _rung_at(row, "received") or str(row.get("created_at") or ""),
        "authorized_at": _rung_at(row, "authorized"),
        "applied_at": _rung_at(row, "dispatching"),
        "application_observed_at": (
            _lane_ack_at(row, "applied")
            or _rung_at(row, "checkpointed")
            or _lane_ack_at(row, "checkpointed")
        ),
    }


def steer_delivery_mode(row: Mapping[str, Any], meta_steering: Mapping[str, Any] | None) -> str:
    """WHICH steering shape the driver delivered (the honest label).

    Read from the lane's own steering journal (the applied action's
    ``delivery`` detail: ``mid-turn`` / ``queued_for_resume``), falling
    back to the durable row's ladder shape. Never guessed from success.
    """
    if meta_steering is not None:
        command_id = str(row.get("id") or row.get("command_id") or "")
        for action in meta_steering.get("steering_journal") or []:
            if (
                action.get("kind") == "steer"
                and action.get("command_id") == command_id
                and action.get("outcome") == "applied"
            ):
                detail = action.get("detail") or {}
                if detail.get("queued_for_resume"):
                    return "queued-for-resume"
                if detail.get("delivered") == "mid-turn":
                    return "mid-turn"
                return str(action.get("delivery") or "next-turn")
    rungs = _rungs(row)
    if "checkpointed" in rungs and "dispatching" in rungs:
        return "applied-before-checkpoint (mode unlabelled by this driver)"
    return ""


# ---------------------------------------------------------------------------
# The checkpoint store reads (#306's reader discipline, reused)
# ---------------------------------------------------------------------------


def checkpoint_store_root() -> Path:
    return REPO_ROOT / "data" / "checkpoints"


def works_index_path(store_root: Path, work_id: str) -> Path:
    return store_root / "works" / f"{work_id}.json"


def read_works_index(store_root: Path, work_id: str) -> dict[str, Any] | None:
    path = works_index_path(store_root, work_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def latest_checkpoint_entry(index: Mapping[str, Any]) -> dict[str, Any] | None:
    entries = index.get("checkpoints") or []
    return dict(entries[-1]) if entries else None


def _newer_checkpoint(work_id: str, previous_id: str | None) -> dict[str, Any] | None:
    index = read_works_index(checkpoint_store_root(), work_id)
    entry = latest_checkpoint_entry(index) if index else None
    if entry is None:
        return None
    if previous_id and entry.get("checkpoint_id") == previous_id:
        return None
    return entry


def wait_checkpoint(
    work_id: str, previous_id: str | None, timeout: float = 600.0
) -> dict[str, Any]:
    return poll(
        lambda: _newer_checkpoint(work_id, previous_id),
        f"a new WIP checkpoint for {work_id[:8]}",
        timeout=timeout,
        interval=10,
    )


def spend_from_receipts(receipts: list[Mapping[str, Any]]) -> dict[str, Any]:
    """The SDK spend receipts folded into one honest figure (#306's rule)."""
    total = 0.0
    basis = "no-receipts-yet"
    tokens = {"input": 0, "cached_input": 0, "output": 0}
    for receipt in receipts:
        usage = (receipt or {}).get("usage") or receipt or {}
        cost = usage.get("total_cost_usd")
        if cost is None:
            basis = "fallback-price-class(glm-5.3-flash-lab)"
            tokens["input"] += int(usage.get("input_tokens") or 0)
            tokens["cached_input"] += int(usage.get("cache_read_input_tokens") or 0)
            tokens["output"] += int(usage.get("output_tokens") or 0)
            total += (
                tokens["input"] / 1_000_000 * FALLBACK_PRICE_PER_MTOK["input"]
                + tokens["cached_input"] / 1_000_000 * FALLBACK_PRICE_PER_MTOK["cached_input"]
                + tokens["output"] / 1_000_000 * FALLBACK_PRICE_PER_MTOK["output"]
            )
        else:
            basis = "sdk-total_cost_usd"
            total += float(cost)
    return {"total_usd": round(total, 4), "cost_basis": basis, "tokens": tokens}


# ---------------------------------------------------------------------------
# setup — the disposable project, the oracle, the lane variables, webhook
# ---------------------------------------------------------------------------


def phase_setup(bundle: Bundle, gitlab: GitLab, name: str, settings: Settings) -> int:
    record = bundle.phase("setup")
    if record.get("project", {}).get("id") is None:
        _create_project_and_seed(bundle, gitlab, name)
    _ensure_bot_member(bundle, gitlab, settings)
    if record.get("labels_created"):
        print("setup: complete (resumed)")
        return 0
    for label in (
        {"name": "ai-reviewed", "color": "#34d399"},
        {"name": "ai-needs-changes", "color": "#f87171"},
        {"name": "security-critical", "color": "#ef4444"},
    ):
        gitlab.post(f"/projects/{record['project']['id']}/labels", json=label)
    record["labels_created"] = True
    bundle.save()
    print("setup: complete")
    return 0


def _create_project_and_seed(bundle: Bundle, gitlab: GitLab, name: str) -> None:
    who = gitlab.get("/user")
    created = gitlab.post(
        "/projects",
        json={
            "name": name,
            "path": name,
            "namespace_id": who.get("namespace_id"),
            "visibility": "private",
            "initialize_with_readme": False,
            "builds_access_level": "enabled",
        },
    )
    if created.status_code not in (201, 200):
        raise Refused(f"project creation failed: {created.status_code} {created.text[:200]}")
    project = created.json()
    project_id = project["id"]
    bundle.record("setup", "project", {"id": project_id, "path": project["path_with_namespace"]})
    print(f"setup: project {project['path_with_namespace']} (id {project_id})")

    actions = [
        {"action": "create", "file_path": path, "content": content}
        for path, content in sorted(seed_files(name).items())
    ]
    commit = gitlab.post(
        f"/projects/{project_id}/repository/commits",
        json={
            "branch": "main",
            "commit_message": (
                "seed: the approach-X task + the shipped #302 lane template + the "
                "independent oracle (committed before any run)"
            ),
            "actions": actions,
        },
    )
    if commit.status_code not in (201, 200):
        raise Refused(f"seed commit failed: {commit.status_code} {commit.text[:300]}")
    seed_sha = commit.json().get("id")
    bundle.record("setup", "seed_commit_sha", seed_sha)
    bundle.record("setup", "ci_yaml_sha256", hashlib.sha256(ci_yaml().encode()).hexdigest())
    bundle.record(
        "setup", "template_sha256", hashlib.sha256(TEMPLATE_SOURCE.read_bytes()).hexdigest()
    )
    bundle.record(
        "setup",
        "task",
        {
            "approach_x_entrypoint": X_NAME,
            "steer_y_entrypoint": Y_NAME,
            "steer_text": STEER_TEXT,
            "bulk_entrypoint": BULK_NAME,
            "validator_path": VALIDATOR_PATH,
        },
    )
    print(
        f"setup: seed commit {seed_sha[:12]} (oracle + shipped template committed before any run)"
    )

    copied: list[str] = []
    for key in VARIABLES_FROM_LAB:
        entry = gitlab.get(f"/projects/68/variables/{key}")
        value = entry.get("value", "")
        if not value:
            continue
        response = gitlab.post(
            f"/projects/{project_id}/variables", json={"key": key, "value": value}
        )
        if response.status_code not in (201, 200):
            raise Refused(f"variable {key} copy failed: {response.text[:200]}")
        copied.append(key)
    for key, value in (
        ("FORGE_LANE_REF", LANE_REF_SHA),  # immutable lane identity
        ("FORGE_STEERING_ENABLED", "1"),  # the lane-side steering consumer
        # The steer must ENTER the running turn, not queue behind it: the
        # driver's bounded drain (300 s default) would hold the follow-up
        # until the turn settles. 3 s fires it mid-turn — the documented
        # "on timeout the follow-up is sent anyway" path.
        ("FORGE_CLAUDE_DRAIN_TIMEOUT", "3"),
    ):
        response = gitlab.post(
            f"/projects/{project_id}/variables", json={"key": key, "value": value}
        )
        if response.status_code not in (201, 200):
            raise Refused(f"variable {key} set failed: {response.text[:200]}")
        copied.append(f"{key}={value[:12]}…")
    bundle.record("setup", "variables", copied)

    lab_hooks = gitlab.get("/projects/68/hooks")
    forge_hook = next((h for h in lab_hooks if "/webhook" in str(h.get("url", ""))), None)
    if forge_hook is None:
        raise Refused("the lab project carries no forge webhook to replicate")
    hook = gitlab.post(
        f"/projects/{project_id}/hooks",
        json={
            "url": forge_hook["url"],
            "token": gitlab.webhook_secret,
            "push_events": True,
            "merge_requests_events": True,
            "note_events": True,
            "pipeline_events": True,
            "job_events": True,
            "issues_events": True,
            "enable_ssl_verification": False,
        },
    )
    if hook.status_code not in (201, 200):
        raise Refused(f"webhook registration failed: {hook.text[:200]}")
    bundle.record("setup", "webhook_url", forge_hook["url"])
    print(f"setup: variables {copied}; webhook {forge_hook['url']}")


def _ensure_bot_member(bundle: Bundle, gitlab: GitLab, settings: Settings) -> None:
    record = bundle.phase("setup")
    project_id = record["project"]["id"]
    if record.get("bot_member"):
        return
    bot_token = settings.FORGE_BOT_TOKEN.get_secret_value()
    with httpx.Client(
        base_url=str(settings.GITLAB_URL.rstrip("/")) + "/api/v4",
        headers={"PRIVATE-TOKEN": bot_token},
        timeout=30.0,
    ) as bot_client:
        bot = bot_client.get("/user")
        bot.raise_for_status()
        bot_user_id = bot.json()["id"]
        bot_username = bot.json()["username"]
    member = gitlab.post(
        f"/projects/{project_id}/members", json={"user_id": bot_user_id, "access_level": 30}
    )
    if member.status_code not in (201, 200):
        raise Refused(f"bot membership grant failed: {member.text[:200]}")
    bundle.record("setup", "bot_member", {"user_id": bot_user_id, "username": bot_username})
    print(f"setup: bot @{bot_username} granted Developer on project {project_id}")


# ---------------------------------------------------------------------------
# preflight — the app's OWN doctor + the alignment axes; refuses pre-paid
# ---------------------------------------------------------------------------


def _repo_version() -> str:
    init = REPO_ROOT / "src" / "forge" / "__init__.py"
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", init.read_text(encoding="utf-8"))
    if match is None:
        raise Refused(f"no __version__ in {init}")
    return match.group(1)


def phase_preflight(bundle: Bundle, gitlab: GitLab) -> int:
    record = bundle.phase("preflight")
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    checks: dict[str, Any] = {}

    health = app_get("/health")
    version = _repo_version()
    checks["controlplane.health"] = {
        "status": health.get("status"),
        "version": health.get("version"),
    }
    if health.get("status") != "ok" or health.get("version") != version:
        raise Refused(f"control plane not aligned onto the repo version {version}: {health}")

    head = psql("SELECT version_num FROM alembic_version").strip()
    from scripts.inventory_lab import repo_schema_head

    expected_head = repo_schema_head(REPO_ROOT)
    if head != expected_head:
        raise Refused(f"schema head {head} != repo chain head {expected_head}")
    checks["schema_head"] = head

    for container in ("forge-app", "forge-worker"):
        env = subprocess.run(
            ["podman", "inspect", container, "--format", "{{json .Config.Env}}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
        profiles = any(entry.startswith("FORGE_BUDGET_PROFILES={") for entry in json.loads(env))
        lane_budget = any(
            entry.startswith("FORGE_LANE_BUDGET_SECONDS=") and entry.split("=", 1)[1].isdigit()
            for entry in json.loads(env)
        )
        adaptive = any(
            entry.startswith("FORGE_ADAPTIVE_COMMANDS_ENABLED=1") for entry in json.loads(env)
        )
        checks[f"caps.{container}"] = {
            "budget_profiles": profiles,
            "lane_budget": lane_budget,
            "adaptive_commands": adaptive,
        }
        if not (profiles and lane_budget and adaptive):
            raise Refused(f"caps missing on {container} (budgets / adaptive commands)")

    # The lab inventory, READ-ONLY (no align_lab re-runs — #306's receipts
    # stand). Its compatibility verdict is recorded verbatim; only the
    # axes THIS drill depends on (version, schema, steering flags) refuse.
    inventory = subprocess.run(
        [sys.executable, "scripts/inventory_lab.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    checks["inventory_lab"] = {
        "returncode": inventory.returncode,
        "stdout_tail": inventory.stdout[-1200:],
    }
    print(f"preflight: inventory_lab verdict recorded (rc={inventory.returncode})")

    attempts: list[dict[str, Any]] = []
    doctor_json: dict[str, Any] = {}
    for attempt in range(1, 4):
        doctor = subprocess.run(
            [
                "podman",
                "exec",
                "forge-app",
                "python",
                "-m",
                "forge.doctor",
                "--project",
                str(project_id),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        try:
            doctor_json = json.loads(doctor.stdout)
        except json.JSONDecodeError:
            doctor_json = {"status": "unparseable", "stdout_tail": doctor.stdout[-400:]}
        failed = [c["name"] for c in doctor_json.get("checks", []) if c.get("status") == "FAIL"]
        attempts.append(
            {
                "attempt": attempt,
                "returncode": doctor.returncode,
                "status": doctor_json.get("status"),
                "failed": failed,
            }
        )
        record["doctor_attempts"] = attempts
        bundle.save()
        if doctor.returncode == 0 and not failed:
            break
        time.sleep(20)
    checks["doctor"] = {
        "returncode": attempts[-1]["returncode"] if attempts else None,
        "status": doctor_json.get("status"),
        "failed": attempts[-1]["failed"] if attempts else None,
    }
    if not attempts or attempts[-1]["returncode"] != 0 or attempts[-1]["failed"]:
        raise Refused(f"the app's own doctor failed (3 attempts): {attempts}")

    runners = gitlab.get(f"/projects/{project_id}/runners")
    online = [r for r in runners if r.get("status") == "online"]
    checks["runners_online"] = [r["id"] for r in online]
    if not online:
        raise Refused("no online runner serves the disposable project")

    record["checks"] = checks
    record["result"] = "green"
    record["finished_at"] = _now()
    bundle.save()
    print("preflight: GREEN (doctor + alignment axes + adaptive commands + runner)")
    return 0


# ---------------------------------------------------------------------------
# the steered arm — issue → /implement → /go → the observed-work steer
# ---------------------------------------------------------------------------


def start_issue_and_plan(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, poll_timeout: float
) -> dict[str, Any]:
    created = gitlab.post(
        f"/projects/{project_id}/issues",
        json={"title": issue_title(), "description": issue_body()},
    )
    if created.status_code not in (201, 200):
        raise Refused(f"issue creation failed: {created.text[:200]}")
    issue = created.json()
    issue_iid = issue["iid"]
    bundle.record(phase, "issue", {"iid": issue_iid, "url": issue["web_url"]})
    print(f"{phase}: issue #{issue_iid} created — {issue['web_url']}")

    note = gitlab.post(
        f"/projects/{project_id}/issues/{issue_iid}/notes", json={"body": "@forge /implement"}
    )
    if note.status_code not in (201, 200):
        raise Refused(f"/implement note failed: {note.text[:200]}")

    def plan_note() -> Any:
        for entry in reversed(gitlab.get(f"/projects/{project_id}/issues/{issue_iid}/notes")):
            body = str(entry.get("body", ""))
            if entry.get("author", {}).get("username") == "forge" and "/go " in body:
                return entry
        return None

    plan = poll(plan_note, "the evidence-backed plan comment", timeout=poll_timeout, interval=15)
    body = str(plan.get("body", ""))
    match = RUN_ID_RE.search(body)
    if match is None:
        raise Refused(f"plan note carries no run id:\n{body[-600:]}")
    run_id = match.group(1)
    harness_line = next((line for line in body.splitlines() if line.startswith("- Harness:")), "")
    bundle.record(
        phase,
        "plan",
        {
            "note_id": plan.get("id"),
            "run_id": run_id,
            "harness_line": harness_line,
            "body_tail": body[-1500:],
        },
    )
    print(f"{phase}: plan arrived (run {run_id[:8]}) — {harness_line}")
    if "claude-sdk-lane" not in harness_line:
        raise Refused(
            f"the frozen lane was not selected: {harness_line!r} — this drill needs "
            "the exact-resume SDK lane (FORGE_HARNESS_PREFERENCE)"
        )
    return {"issue_iid": issue_iid, "run_id": run_id}


def lane_job(gitlab: GitLab, project_id: int, pipeline_id: int) -> dict[str, Any] | None:
    for job in gitlab.get(f"/projects/{project_id}/pipelines/{pipeline_id}/jobs"):
        if str(job.get("name", "")).startswith("forge-agent"):
            return job
    return None


def job_trace(gitlab: GitLab, project_id: int, job_id: int) -> str:
    return gitlab.get_text(f"/projects/{project_id}/jobs/{job_id}/trace")


def grep_lines(trace: str, needle: str, limit: int = 8) -> list[str]:
    return [line for line in trace.splitlines() if needle in line][:limit]


def driver_phase_started_at(trace: str) -> str | None:
    for line in trace.splitlines():
        match = _TRACE_LINE_RE.match(line.strip())
        if match and any(marker in match.group(2) for marker in _DRIVER_PHASE_MARKERS):
            return match.group(1)
    return None


def trace_anchor_epoch(stamp: str) -> float:
    return (
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    )


def find_run_for_issue(gitlab: GitLab, project_id: int, issue_iid: int) -> dict[str, Any] | None:
    for run in app_get("/runs?limit=50").get("runs", []):
        if run.get("project_id") == project_id and run.get("issue_iid") == issue_iid:
            return run
    return None


def dispatch_pipeline(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, arc: dict[str, Any]
) -> dict[str, Any]:
    """Wait for the dispatch's pipeline + lane job (a fresh /go, /retry or /re-go)."""
    branch = f"factory/{arc['issue_iid']}/{arc['run_id'][:8]}"

    def pipeline() -> Any:
        pipelines = gitlab.get(f"/projects/{project_id}/pipelines", params={"ref": branch})
        candidates = [p for p in pipelines if p.get("source") == "api"]
        # LIVE-found: the exclusion must span EVERY phase's recorded
        # dispatches — a per-phase filter once adopted this branch's OLD
        # /go pipeline as the "fresh" retry dispatch.
        already = {
            entry["pipeline_id"]
            for phase_doc in bundle.document["phases"].values()
            for entry in phase_doc.get("dispatches", [])
        }
        fresh = [p for p in candidates if p["id"] not in already]
        return fresh[0] if fresh else None

    dispatched = poll(
        pipeline, f"a NEW api-triggered pipeline on {branch}", timeout=600, interval=10
    )
    pipeline_id = dispatched["id"]
    job = poll(
        lambda: lane_job(gitlab, project_id, pipeline_id), "the lane job", timeout=300, interval=10
    )
    bundle.append(
        phase,
        "dispatches",
        {
            "pipeline_id": pipeline_id,
            "pipeline_url": dispatched.get("web_url"),
            "lane_job_id": job["id"],
            "lane_job_status_at_capture": job.get("status"),
            "branch": branch,
        },
    )
    print(f"{phase}: pipeline {pipeline_id}, lane job {job['id']} ({job.get('status')})")
    return {"pipeline_id": pipeline_id, "job_id": job["id"], "branch": branch}


def wait_driver_phase_start(
    gitlab: GitLab, project_id: int, job_id: int, timeout: float = 600.0
) -> dict[str, Any]:
    deadline = _ts() + timeout
    last_status: str | None = None
    while _ts() < deadline:
        try:
            job = gitlab.get(f"/projects/{project_id}/jobs/{job_id}")
            last_status = str(job.get("status"))
            if last_status in ("success", "failed", "canceled"):
                trace = job_trace(gitlab, project_id, job_id)
                stamp = driver_phase_started_at(trace)
                return {"anchor": stamp, "job_status": last_status, "post_hoc": True}
            trace = job_trace(gitlab, project_id, job_id)
            stamp = driver_phase_started_at(trace)
            if stamp is not None and last_status == "running":
                return {"anchor": stamp, "job_status": last_status, "post_hoc": False}
        except httpx.HTTPError:
            pass  # the trace endpoint 404s until the first output — keep polling
        time.sleep(10)
    raise Refused(
        f"the driver phase never started within {timeout:.0f}s (job {job_id} last {last_status})"
    )


def post_note(gitlab: GitLab, project_id: int, issue_iid: int, body: str) -> dict[str, Any]:
    """One operator note through the NATIVE ingress (returns posting facts)."""
    posted_at = _now()
    response = gitlab.post(f"/projects/{project_id}/issues/{issue_iid}/notes", json={"body": body})
    if response.status_code not in (201, 200):
        raise Refused(f"note failed ({body[:40]}…): {response.text[:200]}")
    return {"note_id": response.json().get("id"), "posted_at": posted_at, "body": body}


def wait_row_rung(
    work_id: str, command_id: str, rungs: tuple[str, ...], timeout: float = 180.0
) -> dict[str, Any]:
    """Wait until the durable row's OWN ladder carries one of *rungs*."""

    def row_with_rung() -> dict[str, Any] | None:
        for row in control_rows(work_id):
            if row.get("id") == command_id:
                if any(rung in _rungs(row) for rung in rungs):
                    return row
                return None
        return None

    return poll(
        row_with_rung, f"row {command_id[:12]}… reaching {rungs}", timeout=timeout, interval=3
    )


def find_command_id(work_id: str, kind: str, needle: str) -> str | None:
    for row in control_rows(work_id, kind=kind):
        if needle in json.dumps(row.get("payload") or {}):
            return str(row.get("id"))
    return None


def _capture_lane_outcome(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, dispatch: Mapping[str, Any]
) -> dict[str, Any]:
    """Wait the lane job terminal; capture status, trace markers, the SDK
    usage receipt (spend), the steering journal and the collector outcome."""
    job_id = int(dispatch["lane_job_id"])
    job = poll(
        lambda: (lambda j: j and j.get("status") in ("success", "failed", "canceled") and j)(
            lane_job_of(gitlab, project_id, job_id)
        ),
        f"lane job {job_id} completion",
        timeout=2400,
        interval=30,
    )
    trace = job_trace(gitlab, project_id, job_id)
    stored = next(
        (
            d
            for d in bundle.document["phases"][phase]["dispatches"]
            if d.get("lane_job_id") == job_id
        ),
        dict(dispatch),
    )
    stored["job_status"] = job.get("status")
    stored["failure_reason"] = job.get("failure_reason")
    stored["trace_sha256"] = hashlib.sha256(trace.encode()).hexdigest()
    stored["trace_tail"] = trace[-2500:]
    stored["envelope_lines"] = grep_lines(trace, "forge dispatch envelope")
    stored["collector_lines"] = grep_lines(trace, "FORGE_LANE_OUTCOME:")
    stored["candidate_lines"] = grep_lines(trace, "FORGE_CANDIDATE:")
    stored["resume_lines"] = grep_lines(trace, "resume")
    stored["restore_lines"] = grep_lines(trace, "restor")
    try:
        meta_raw = gitlab.get_text(
            f"/projects/{project_id}/jobs/{job_id}/artifacts/.forge/candidate.meta.json"
        )
        meta = json.loads(meta_raw)
        stored["candidate_meta"] = {
            "exit": meta.get("exit"),
            "terminal_reason": meta.get("terminal_reason"),
            "model": meta.get("model"),
            "driver": meta.get("driver"),
            "usage": meta.get("usage"),
            "workspace_generation": meta.get("workspace_generation"),
            "steering_journal": meta.get("steering_journal"),
        }
        stored["usage_receipt"] = meta.get("usage")
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        stored["candidate_meta_error"] = str(exc)[:200]
    bundle.save()
    print(
        f"{phase}: lane job {job_id} ended {job.get('status')} "
        f"(failure_reason={job.get('failure_reason')})"
    )
    return dict(job)


def lane_job_of(gitlab: GitLab, project_id: int, job_id: int) -> dict[str, Any] | None:
    try:
        return gitlab.get(f"/projects/{project_id}/jobs/{job_id}")
    except httpx.HTTPError:
        return None


def _lane_spend_so_far(bundle: Bundle) -> float:
    receipts = []
    for phase in bundle.document["phases"].values():
        for dispatch in phase.get("dispatches", []):
            usage = dispatch.get("usage_receipt")
            if isinstance(usage, Mapping):
                receipts.append(usage)
    return float(spend_from_receipts(receipts)["total_usd"])


def _spend_guard(bundle: Bundle, what: str) -> None:
    spent = _lane_spend_so_far(bundle)
    if spent > SPEND_GUARD_USD:
        _record_failure(
            bundle,
            f"the spend guard refused {what}: recorded lane spend ${spent:.2f} crossed "
            f"${SPEND_GUARD_USD} (cap ${SPEND_CAP_USD})",
        )
        raise Refused(f"spend guard refused {what} (${spent:.2f} recorded)")


def _key(base: str, cycle: int) -> str:
    """The bundle's phase key for one iteration cycle (1 → the bare name)."""
    return base if cycle <= 1 else f"{base}~c{cycle}"


def latest_cycle(bundle: Bundle) -> int:
    """The highest cycle any steer phase recorded (1 when none did)."""
    cycles = [
        int(name.split("~c", 1)[1])
        for name in bundle.document.get("phases", {})
        if name.startswith("steer~c") and name.split("~c", 1)[1].isdigit()
    ]
    return max([1, *cycles])


def phase_steer(bundle: Bundle, gitlab: GitLab, cycle: int = 1) -> int:
    phase = _key("steer", cycle)
    record = bundle.phase(phase)
    if record.get("result") == "green":
        print("steer: already complete — resuming revision phase input")
        return 0
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    record["paid"] = True
    bundle.save()
    _spend_guard(bundle, "the steered lane")

    if not record.get("plan"):
        arc = start_issue_and_plan(bundle, gitlab, project_id, phase, poll_timeout=900)
        post_note(gitlab, project_id, arc["issue_iid"], f"@forge /go {arc['run_id']}")
        dispatch_pipeline(bundle, gitlab, project_id, phase, arc)
    else:
        arc = {
            "issue_iid": record["issue"]["iid"],
            "run_id": record["plan"]["run_id"],
        }
        print(f"steer: resuming run {arc['run_id'][:8]} from the recorded arc")
    run_id = arc["run_id"]

    dispatch = bundle.document["phases"][phase]["dispatches"][-1]
    anchor_info = wait_driver_phase_start(gitlab, project_id, int(dispatch["lane_job_id"]))
    anchor = anchor_info["anchor"]
    if anchor is None or anchor_info["job_status"] != "running":
        _capture_lane_outcome(bundle, gitlab, project_id, phase, dispatch)
        raise Refused(
            f"the driver phase anchored post-hoc (status {anchor_info['job_status']}) — "
            "no live turn to steer; recorded honestly"
        )
    anchor_epoch = trace_anchor_epoch(anchor)
    bundle.record(
        phase,
        "driver_anchor",
        {"anchor": anchor, "observed_at": _now(), "job_status": anchor_info["job_status"]},
    )
    print(f"steer: driver phase anchor {anchor} (job running)")

    # -- the observed-work rung: wait to the rung offset, job still running --
    rung_offset = STEER_RUNGS_S[0]
    while True:
        job = gitlab.get(f"/projects/{project_id}/jobs/{dispatch['lane_job_id']}")
        status = str(job.get("status"))
        if status in ("success", "failed", "canceled"):
            _capture_lane_outcome(bundle, gitlab, project_id, phase, dispatch)
            raise Refused(
                f"the turn completed before the steer rung (job {status}) — recorded honestly"
            )
        if time.time() >= anchor_epoch + rung_offset:
            break
        time.sleep(5)
    bundle.record(phase, "steer_rung", {"offset_s": rung_offset, "reached_at": _now()})

    # -- THE NATIVE STEER: the operator's Y instruction, live turn ----------
    steer_note = post_note(
        gitlab, project_id, arc["issue_iid"], f"@forge /steer {run_id} {STEER_TEXT}"
    )
    bundle.record(phase, "steer_note", steer_note)
    command_id = poll(
        lambda: find_command_id(run_id, "steer", Y_NAME),
        "the durable steer row",
        timeout=120,
        interval=3,
    )
    row = wait_row_rung(
        run_id, command_id, ("dispatching", "applied", "checkpointed", "expired", "rejected")
    )
    moments = application_moments(row)
    bundle.record(
        phase,
        "steer_row",
        {
            "command_id": command_id,
            "status": row.get("status"),
            "rungs": _rungs(row),
            "moments": moments,
            "journal": row.get("journal"),
            "dedup_key": row.get("dedup_key"),
        },
    )
    print(f"steer: durable row {command_id[:12]}… status={row.get('status')} rungs={_rungs(row)}")
    if row.get("status") in ("expired", "rejected"):
        raise Refused(f"the steer was {row.get('status')} at the gate — recorded honestly")

    # the operator reply (the ingress's ack comment)
    def steer_reply() -> Any:
        for entry in reversed(
            gitlab.get(f"/projects/{project_id}/issues/{arc['issue_iid']}/notes")
        ):
            body = str(entry.get("body", ""))
            if entry.get("author", {}).get("username") == "forge" and "Steering recorded" in body:
                return {
                    "note_id": entry.get("id"),
                    "at": entry.get("created_at"),
                    "body": body[:400],
                }
        return None

    bundle.record(
        phase, "steer_reply", poll(steer_reply, "the steer ack reply", timeout=120, interval=5)
    )

    # -- dwell: the model applies Y while the turn keeps running -------------
    dwell_started = time.time()
    while time.time() - dwell_started < POST_STEER_DWELL_S:
        job = gitlab.get(f"/projects/{project_id}/jobs/{dispatch['lane_job_id']}")
        if str(job.get("status")) in ("success", "failed", "canceled"):
            break  # the turn ended early — the interleaving records what stands
        time.sleep(5)
    bundle.record(phase, "post_steer_dwell_s", round(time.time() - dwell_started, 1))

    # -- THE URGENT-PAUSE INTERLEAVING: a queued ordinary steer + the pause --
    steer2_note = post_note(
        gitlab, project_id, arc["issue_iid"], f"@forge /steer {run_id} {STEER2_TEXT}"
    )
    pause_note = post_note(gitlab, project_id, arc["issue_iid"], f"@forge /pause {run_id}")
    bundle.record(
        phase,
        "interleaving_notes",
        {"steer2": steer2_note, "pause": pause_note, "steer2_before_pause": True},
    )
    command2_id = poll(
        lambda: find_command_id(run_id, "steer", "docstring"),
        "the second (queued) steer row",
        timeout=120,
        interval=3,
    )
    pause_id = poll(
        lambda: next((str(row["id"]) for row in control_rows(run_id, kind="pause")), None),
        "the pause row",
        timeout=120,
        interval=3,
    )

    # the pause's checkpoint transaction — THE WIP evidence (must carry files)
    checkpoint = wait_checkpoint(run_id, None)
    pause_row = wait_row_rung(run_id, pause_id, ("checkpointed",), timeout=300)
    steer2_row = next((row for row in control_rows(run_id) if row.get("id") == command2_id), {})
    bundle.record(
        phase,
        "interleaving_rows",
        {
            "steer2": {
                "command_id": command2_id,
                "status": steer2_row.get("status"),
                "rungs": _rungs(steer2_row),
                "journal": steer2_row.get("journal"),
                "moments": application_moments(steer2_row),
            },
            "pause": {
                "command_id": pause_id,
                "status": pause_row.get("status"),
                "rungs": _rungs(pause_row),
                "journal": pause_row.get("journal"),
            },
            "sequence_order": {
                "steer2": steer2_row.get("sequence"),
                "pause": pause_row.get("sequence"),
            },
        },
    )
    bundle.record(phase, "pause_checkpoint", dict(checkpoint))
    print(
        f"steer: pause checkpoint {str(checkpoint.get('checkpoint_id'))[:12]} "
        f"files={checkpoint.get('files')} — steer2 status={steer2_row.get('status')}"
    )
    if not int(checkpoint.get("files") or 0):
        raise Refused("the pause's checkpoint carries ZERO files — no WIP to preserve")

    # -- job-level cancel + the honest blocked classification ----------------
    job_id = int(dispatch["lane_job_id"])
    job = gitlab.get(f"/projects/{project_id}/jobs/{job_id}")
    cancelled = gitlab.post(f"/projects/{project_id}/jobs/{job_id}/cancel")
    body: dict[str, Any] = {}
    with contextlib.suppress(ValueError):
        body = cancelled.json() or {}
    bundle.record(
        phase,
        "job_cancel",
        {
            "job_id": job_id,
            "job_status_before": job.get("status"),
            "http_status": cancelled.status_code,
            "status_after": body.get("status") or body.get("message"),
        },
    )
    _capture_lane_outcome(bundle, gitlab, project_id, phase, dispatch)
    blocked = poll(
        lambda: (lambda r: r and r.get("status") == "blocked" and r)(
            find_run_for_issue(gitlab, project_id, arc["issue_iid"])
        ),
        "the run's blocked classification",
        timeout=1200,
        interval=20,
    )
    bundle.record(phase, "blocked_classification", blocked)
    record["result"] = "green"
    record["finished_at"] = _now()
    bundle.save()
    print(f"steer: GREEN — run blocked ({blocked.get('status_reason')}) with the Y WIP checkpoint")
    return 0


# ---------------------------------------------------------------------------
# the material revision — staged via the app's own module, approved NATIVELY
# ---------------------------------------------------------------------------


#: The revision staging program, executed INSIDE the forge-app container
#: (the app's own code + DB): revision 1 becomes the durable ACTIVE plan
#: carrying the run's REAL plan digest (the world the live GitLab flow
#: froze at /go), then revision 2 — the operator's scope extension — is
#: staged PENDING through the REAL :func:`stage_pending_revision`. The
#: printed JSON is the evidence.
_STAGE_PROGRAM = f"""
import asyncio, json, os, sys
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.adaptive.models import PlanRevision, PlanStep
from forge.adaptive.revisions import (
    ACTIVE_PLAN_KEY,
    ActivePlanState,
    RevisionDecision,
    plan_digest,
    proposed_revision_identity,
    stage_pending_revision,
)

RUN_ID = sys.argv[1]
DECISION_ID = sys.argv[2]
WORK = RUN_ID
PLAN_ID = "plan-combined-" + RUN_ID[:8]
CONTRACT = sys.argv[3]
#: The run's REAL plan digest (the /go world) — the seeded ACTIVE plan
#: carries it so the activation's digest switch is exactly the approval.
ACTIVE_DIGEST = sys.argv[4]
EPOCH = 1


def step(step_id, objective, writes=None):
    return PlanStep(
        step_id=step_id,
        objective=objective,
        write_repository_id=writes,
        impact=["internal"],
        acceptance_refs=["AC-1"],
    )


S1 = step("S1", "Inspect the repository and the frozen oracle contract.")
S2 = step(
    "S2",
    "Implement {VALIDATOR_PATH}: the public entrypoint and the bulk entrypoint "
    "{BULK_NAME}.",
    writes="combined-steer/project",
)
S3 = step(
    "S3",
    "Export the public validator API from src/validators/__init__.py (the "
    "approved scope extension).",
    writes="combined-steer/project",
)

revision1 = PlanRevision(
    plan_id=PLAN_ID, work_id=WORK, revision=1, parent_revision=None,
    work_contract_digest=CONTRACT, snapshot_set_digest="0" * 64,
    summary="Single-address email validation (revision 1).",
    steps=[S1, S2],
)
revision2 = PlanRevision(
    plan_id=PLAN_ID, work_id=WORK, revision=2, parent_revision=1,
    work_contract_digest=CONTRACT, snapshot_set_digest="0" * 64,
    summary="Scope extended: the package surface joins the plan (revision 2).",
    steps=[S1, S2, S3],
)
decision = RevisionDecision(
    decision_id=DECISION_ID, work_id=WORK, parent_revision=1,
    proposed_revision_id=proposed_revision_identity(revision2),
    proposed_digest=plan_digest(revision2), work_contract_digest=CONTRACT,
    authorization_epoch=EPOCH,
)
current = ActivePlanState(
    work_id=WORK, plan_id=PLAN_ID, active_revision=1,
    work_contract_digest=CONTRACT, authorization_epoch=EPOCH, publication_epoch=1,
)


async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from forge.durable import FlowRun
    async with factory() as session:
        run = await session.get(FlowRun, RUN_ID)
        if run is None:
            raise SystemExit("run_not_found")
        spec_digest = str(run.spec_digest or "")
        merged = dict(run.evidence or {{}})
        merged[ACTIVE_PLAN_KEY] = {{
            "schema": "forge.revision.active-plan/1",
            "work_id": WORK, "plan_id": PLAN_ID, "active_revision": 1,
            "plan_digest": ACTIVE_DIGEST, "revised_from_digest": "",
            "work_contract_digest": CONTRACT, "authorization_epoch": EPOCH,
            "publication_epoch": 1, "activated_by_decision": "",
        }}
        run.evidence = merged
        await session.commit()
    await stage_pending_revision(
        factory, RUN_ID, decision, revision2, current, old=revision1
    )
    await engine.dispose()
    print(json.dumps({{
        "decision_id": DECISION_ID,
        "active_digest_seeded": ACTIVE_DIGEST,
        "revision1_digest": plan_digest(revision1),
        "revision2_digest": plan_digest(revision2),
        "staged_digest": decision.proposed_digest,
        "spec_digest": spec_digest,
        "contract_digest": CONTRACT,
        "work_id": WORK,
        "staged_at": datetime.now(timezone.utc).isoformat(),
    }}))


asyncio.run(main())
"""


def run_row(work_id: str) -> dict[str, Any]:
    raw = psql(
        "SELECT row_to_json(t)::text FROM (SELECT id, status, status_reason, "
        f"plan_digest, spec_digest, evidence FROM flow_runs WHERE id = '{work_id}') t"
    )
    return json.loads(raw.strip()) if raw.strip() else {}


def phase_revision(bundle: Bundle, gitlab: GitLab, cycle: int = 1) -> int:
    phase = _key("revision", cycle)
    record = bundle.phase(phase)
    if record.get("result") == "green":
        print("revision: already complete")
        return 0
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    steer = bundle.document["phases"][_key("steer", cycle)]
    run_id = steer["plan"]["run_id"]
    issue_iid = steer["issue"]["iid"]

    before = run_row(run_id)
    decision_id = f"dec-combined-{run_id[:12]}"
    contract = str(before.get("spec_digest") or "c" * 64)
    active_before = str(before.get("plan_digest") or "")
    if not active_before:
        raise Refused("the run carries no plan digest — the revision world has no anchor")
    record["before"] = {
        "run_plan_digest": active_before,
        "spec_digest": before.get("spec_digest"),
    }
    bundle.save()

    staged_raw = subprocess.run(
        [
            "podman",
            "exec",
            "forge-app",
            "python",
            "-c",
            _STAGE_PROGRAM,
            run_id,
            decision_id,
            contract,
            active_before,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if staged_raw.returncode != 0:
        raise Refused(f"revision staging failed: {staged_raw.stderr[-500:]}")
    staged = json.loads(staged_raw.stdout.strip().splitlines()[-1])
    record["staged"] = staged
    bundle.save()
    print(
        f"revision: staged decision {decision_id[:20]}… "
        f"rev2 digest {staged['revision2_digest'][:12]}…"
    )

    # THE NATIVE APPROVAL through the real ingress.
    approve_note = post_note(
        gitlab,
        project_id,
        issue_iid,
        f"@forge /approve-revision {run_id} {decision_id}",
    )
    bundle.record(phase, "approve_note", approve_note)

    def activated() -> dict[str, Any] | None:
        row = run_row(run_id)
        after = row.get("plan_digest") or ""
        if after and after == staged["revision2_digest"]:
            return row
        return None

    row = poll(
        activated, "the activation switching the durable plan digest", timeout=180, interval=5
    )
    evidence = row.get("evidence") or {}
    active_plan = evidence.get("active_plan") or {}
    reuse = evidence.get("checkpoint_reuse_decision") or {}
    record["after"] = {
        "run_plan_digest": row.get("plan_digest"),
        "active_plan_digest": active_plan.get("plan_digest"),
        "active_plan_revision": active_plan.get("active_revision"),
        "activated_by_decision": active_plan.get("activated_by_decision"),
        "reuse_route": reuse.get("route"),
        "reuse_route_reason": reuse.get("route_reason"),
        "preserved_checkpoint_id": next(
            (
                entry.get("artifact_id")
                for entry in reuse.get("artifacts", [])
                if entry.get("kind") == "checkpoint" and entry.get("decision") == "preserve"
            ),
            "",
        ),
        "status": row.get("status"),
    }
    bundle.save()
    print(
        f"revision: ACTIVATED — digest {row.get('plan_digest')[:12]}… route "
        f"{reuse.get('route')} preserved {(record['after']['preserved_checkpoint_id'] or 'NONE')[:12]}…"
    )
    if reuse.get("route") != "preserve":
        raise Refused(
            f"the activation routed the checkpoint {reuse.get('route')!r} "
            f"({reuse.get('route_reason')}) — the WIP was not preserved"
        )
    record["result"] = "green"
    record["finished_at"] = _now()
    bundle.save()
    return 0


# ---------------------------------------------------------------------------
# the resumed arm — /retry over the preserved checkpoint, the final candidate
# ---------------------------------------------------------------------------


def _file_at(gitlab: GitLab, project_id: int, path: str, sha: str) -> str | None:
    try:
        entry = gitlab.get(
            f"/projects/{project_id}/repository/files/{path.replace('/', '%2F')}",
            params={"ref": sha},
        )
        return base64.b64decode(entry.get("content") or "").decode("utf-8", errors="replace")
    except httpx.HTTPError:
        return None


def phase_resume(bundle: Bundle, gitlab: GitLab, cycle: int = 1) -> int:
    phase = _key("resume", cycle)
    record = bundle.phase(phase)
    if record.get("result") == "green":
        print("resume: already complete")
        return 0
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    steer = bundle.document["phases"][_key("steer", cycle)]
    revision = bundle.document["phases"][_key("revision", cycle)]
    run_id = steer["plan"]["run_id"]
    issue_iid = steer["issue"]["iid"]
    arc = {"issue_iid": issue_iid, "run_id": run_id}
    _spend_guard(bundle, "the resumed lane")

    retried = record.get("retry_note")
    if not retried:
        # Cycle 2+ — THE STANDING DIRECTION: a steer posted while the run
        # is blocked sits PENDING (received/authorized) and the resumed
        # lane's drain delivers it MID-TURN (the control plane's pending
        # view serves undelivered rows to the continuation's cursor). On
        # this driver the frozen brief cannot carry the operator's
        # direction across a continuation — the durable guidance can.
        if cycle >= 2 and not record.get("standing_direction_note"):
            standing = post_note(
                gitlab, project_id, issue_iid, f"@forge /steer {run_id} {STEER_REASSERT_TEXT}"
            )
            bundle.record(phase, "standing_direction_note", standing)
            standing_id = poll(
                lambda: find_command_id(run_id, "steer", "standing direction"),
                "the standing-direction steer row",
                timeout=120,
                interval=3,
            )
            bundle.record(
                phase,
                "standing_direction_row",
                {"command_id": standing_id, "status_at_post": "received"},
            )
        # RESUME re-entry: a run already waiting_harness carries its retry
        # dispatch — a second /retry note would be refused as an
        # in-flight attempt (A11), never re-posted blindly.
        current = find_run_for_issue(gitlab, project_id, issue_iid) or {}
        if current.get("status") != "waiting_harness":
            retried = post_note(gitlab, project_id, issue_iid, "@forge /retry")
            bundle.record(phase, "retry_note", retried)
        else:
            bundle.record(
                phase,
                "retry_note",
                {"note_id": None, "posted_at": None, "resumed_dispatch_already_live": True},
            )
    if not record.get("dispatches"):
        dispatch_pipeline(bundle, gitlab, project_id, phase, arc)
    dispatch = dict(bundle.document["phases"][phase]["dispatches"][-1])
    dispatch.setdefault("lane_job_id", dispatch.get("job_id"))
    _capture_lane_outcome(bundle, gitlab, project_id, phase, dispatch)
    resume_dispatch = bundle.document["phases"][phase]["dispatches"][-1]
    job_status = resume_dispatch.get("job_status")
    terminal = (resume_dispatch.get("candidate_meta") or {}).get("terminal_reason")
    if job_status != "success":
        _record_failure(
            bundle,
            f"the resumed lane ended {job_status} (terminal_reason={terminal}) — "
            "iteration point; see the trace tail",
        )
        record["result"] = "resumed-lane-failed"
        bundle.save()
        raise Refused(f"the resumed lane ended {job_status} (terminal_reason={terminal})")

    # the restore + envelope evidence: the dispatched checkpoint IS the
    # preserved one, the resume mode IS required.
    restore_lines = resume_dispatch.get("restore_lines") or []
    envelope_lines = resume_dispatch.get("envelope_lines") or []
    preserved_id = revision["after"]["preserved_checkpoint_id"]
    standing_id = str((record.get("standing_direction_row") or {}).get("command_id") or "")
    standing_fate: dict[str, Any] = {}
    if standing_id:
        fresh = next((row for row in control_rows(run_id) if row.get("id") == standing_id), {})
        journal = (resume_dispatch.get("candidate_meta") or {}).get("steering_journal") or []
        standing_fate = {
            "command_id": standing_id,
            "final_status": fresh.get("status"),
            "final_rungs": _rungs(fresh),
            "moments": application_moments(fresh),
            "lane_journal_entry": next(
                (entry for entry in journal if entry.get("command_id") == standing_id), None
            ),
        }
        bundle.record(phase, "standing_direction_fate", standing_fate)
    bundle.record(
        phase,
        "restore_evidence",
        {
            "restore_lines": restore_lines[:6],
            "envelope_lines": envelope_lines[:4],
            "workspace_generation": (resume_dispatch.get("candidate_meta") or {}).get(
                "workspace_generation"
            ),
            "preserved_checkpoint_expected": preserved_id,
            "restored_checkpoint_match": any(preserved_id[:24] in line for line in restore_lines)
            or bool((resume_dispatch.get("candidate_meta") or {}).get("workspace_generation")),
        },
    )

    # the worker journal's dispatch envelope (the next executor input
    # identity). The journal line is the PRIMARY source; a rotated log
    # falls back to the durable continuation document + the job trace's
    # envelope echo — the sources must AGREE on the preserved checkpoint.
    envelope = worker_envelope(run_id)
    if not envelope.get("checkpoint"):
        continuation = (run_row(run_id).get("evidence") or {}).get("continuation") or {}
        envelope = {
            "source": "evidence-continuation+trace-echo",
            "envelope_digest": "",
            "checkpoint": str(continuation.get("checkpoint_digest") or ""),
            "decision_id": str(continuation.get("decision_id") or ""),
            "resume_mode": "required"
            if any("resume=required" in line for line in envelope_lines)
            else "",
            "lines": [],
        }
    else:
        envelope["source"] = "worker-journal"
    bundle.record(phase, "resume_dispatch_envelope", envelope)
    # LIVE-found: the worker journal spells the checkpoint id as a 12-char
    # prefix — the equality accepts the journal's prefix of the FULL
    # preserved id (the lane trace's envelope echo carries the full one
    # and is recorded beside it in restore_evidence).
    dispatched = str(envelope.get("checkpoint") or "")
    if dispatched != preserved_id and not preserved_id.startswith(dispatched):
        raise Refused(
            f"the post-revision dispatch envelope names checkpoint "
            f"{dispatched[:12]}… not the preserved "
            f"{preserved_id[:12]}…"
        )

    # the final candidate: the Draft MR, the oracle green on the exact sha,
    # the diff carrying Y + the bulk scope.
    mr = wait_mr_and_verify(bundle, gitlab, project_id, phase, dispatch["branch"])
    final_validator = _file_at(gitlab, project_id, VALIDATOR_PATH, mr["candidate_sha"]) or ""
    bundle.record(
        phase,
        "final_candidate",
        {
            "validator_path": VALIDATOR_PATH,
            "validator_content_sha256": hashlib.sha256(final_validator.encode()).hexdigest(),
            "y_entrypoint_present": f"def {Y_NAME}(" in final_validator,
            "x_entrypoint_present": f"def {X_NAME}(" in final_validator,
            "bulk_entrypoint_present": f"def {BULK_NAME}(" in final_validator,
            "validator_content": final_validator,
        },
    )
    record["result"] = "green"
    record["finished_at"] = _now()
    bundle.save()
    print("resume: GREEN — the final candidate verified, Draft MR left for human review")
    return 0


def wait_mr_and_verify(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, branch: str
) -> dict[str, Any]:
    def mr() -> Any:
        mrs = gitlab.get(
            f"/projects/{project_id}/merge_requests",
            params={"state": "opened", "source_branch": branch},
        )
        return mrs[0] if mrs else None

    def mr_or_noop() -> Any:
        found = mr()
        if found:
            return {"mr": found}
        return None

    merge_request = poll(mr_or_noop, f"the Draft MR on {branch}", timeout=1500, interval=20)
    merge_request = merge_request["mr"]
    mr_iid = merge_request["iid"]

    def verified() -> Any:
        current = gitlab.get(f"/projects/{project_id}/merge_requests/{mr_iid}")
        sha = current.get("sha") or ""
        if not sha:
            return None
        for entry in gitlab.get(f"/projects/{project_id}/pipelines", params={"sha": sha}):
            if entry.get("status") == "success":
                return {"sha": sha, "pipeline_id": entry["id"]}
        return None

    green = poll(
        verified, "the current candidate's green oracle pipeline", timeout=1800, interval=20
    )

    diffs = gitlab.get(f"/projects/{project_id}/merge_requests/{mr_iid}/diffs")
    touched = [str(d.get("new_path")) for d in diffs]
    forbidden = [
        path
        for path in touched
        if path in (".gitlab-ci.yml", "README.md") or path.startswith("tests/")
    ]
    current = gitlab.get(f"/projects/{project_id}/merge_requests/{mr_iid}")
    diff_text = "\n".join(str(d.get("diff") or "") for d in diffs)
    result = {
        "mr_iid": mr_iid,
        "mr_url": merge_request.get("web_url"),
        "title": merge_request.get("title"),
        "draft": bool(
            merge_request.get("work_in_progress")
            or str(merge_request.get("title", "")).startswith("Draft:")
        ),
        "merged": bool(current.get("merged_at")),
        "candidate_sha": green["sha"],
        "oracle_pipeline_id": green["pipeline_id"],
        "changed_paths": touched,
        "oracle_tampering": forbidden,
        "diff_digest": hashlib.sha256(diff_text.encode()).hexdigest(),
    }
    if forbidden:
        _record_failure(bundle, f"the candidate touched the oracle files: {forbidden}")
        raise Refused(f"the candidate touched the oracle files: {forbidden}")
    bundle.record(phase, "mr", result)
    print(f"{phase}: Draft MR !{mr_iid} green on candidate {green['sha'][:12]} (touched={touched})")
    return result


# ---------------------------------------------------------------------------
# the counterfactual arm — the same task, unsteered
# ---------------------------------------------------------------------------


def phase_counterfactual(bundle: Bundle, gitlab: GitLab, cycle: int = 1) -> int:
    phase = _key("counterfactual", cycle)
    record = bundle.phase(phase)
    if record.get("result") == "green":
        print("counterfactual: already complete")
        return 0
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    _spend_guard(bundle, "the counterfactual lane")

    arc = start_issue_and_plan(bundle, gitlab, project_id, phase, poll_timeout=900)
    post_note(gitlab, project_id, arc["issue_iid"], f"@forge /go {arc['run_id']}")
    dispatch_pipeline(bundle, gitlab, project_id, phase, arc)
    dispatch = dict(bundle.document["phases"][phase]["dispatches"][-1])
    dispatch.setdefault("lane_job_id", dispatch.get("job_id"))
    _capture_lane_outcome(bundle, gitlab, project_id, phase, dispatch)
    job_status = bundle.document["phases"][phase]["dispatches"][-1].get("job_status")
    if job_status != "success":
        raise Refused(
            f"the counterfactual lane ended {job_status} — the unsteered edit set "
            "was not captured (recorded honestly)"
        )

    run_id = arc["run_id"]
    mr = wait_mr_and_verify(bundle, gitlab, project_id, phase, dispatch["branch"])
    unsteered_validator = _file_at(gitlab, project_id, VALIDATOR_PATH, mr["candidate_sha"]) or ""
    bundle.record(
        phase,
        "unsteered_candidate",
        {
            "validator_path": VALIDATOR_PATH,
            "validator_content_sha256": hashlib.sha256(unsteered_validator.encode()).hexdigest(),
            "y_entrypoint_present": f"def {Y_NAME}(" in unsteered_validator,
            "x_entrypoint_present": f"def {X_NAME}(" in unsteered_validator,
            "validator_content": unsteered_validator,
        },
    )
    steer_rows = control_rows(run_id, kind="steer")
    record["steer_rows_present"] = len(steer_rows)
    if steer_rows:
        raise Refused("the counterfactual arm carries steer rows — it is not an unsteered control")
    record["result"] = "green"
    record["finished_at"] = _now()
    bundle.save()
    print("counterfactual: GREEN — the unsteered edit set captured")
    return 0


# ---------------------------------------------------------------------------
# the worker journal's dispatch-envelope lines (read-only)
# ---------------------------------------------------------------------------

_ENVELOPE_LINE_RE = re.compile(
    r"gitlab\.dispatch_envelope_digest: run (?P<run>[0-9a-f]{8}) attempt (?P<attempt>\d+) "
    r"dispatched a (?P<kind>fresh|required) resume \(checkpoint (?P<checkpoint>\S+?), "
    r"decision (?P<decision>\S+?)\)(?: \\u2014| —) envelope (?P<envelope>[0-9a-f]+)"
)


def worker_envelope(run_id: str) -> dict[str, Any]:
    """The worker journal's dispatch-envelope lines for ONE run (read-only).

    The envelope digest is computed at dispatch (never stored) and journaled
    by the worker — the only durable place the drill can cite it from
    (#306's LIVE-found note: the structured journal writes to STDERR —
    both streams are scanned).
    """
    completed = subprocess.run(
        ["podman", "logs", "forge-worker", "--since", "2026-09-25T00:00:00"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines: list[dict[str, str]] = []
    for line in ((completed.stdout or "") + "\n" + (completed.stderr or "")).splitlines():
        if "dispatch_envelope_digest" not in line or f"run {run_id[:8]}" not in line:
            continue
        match = _ENVELOPE_LINE_RE.search(line)
        if match:
            lines.append(dict(match.groupdict()))
    selected = lines[-1] if lines else {}
    return {
        "lines": lines,
        "envelope_digest": selected.get("envelope", ""),
        "checkpoint": selected.get("checkpoint", ""),
        "decision_id": selected.get("decision", ""),
        "resume_mode": selected.get("kind", ""),
    }


# ---------------------------------------------------------------------------
# collect — fold the evidence into the graded combined trace
# ---------------------------------------------------------------------------


def build_trace(bundle: Mapping[str, Any], cycle: int = 1) -> dict[str, Any]:
    """Fold ONE cycle's evidence into the graded combined trace (pure)."""
    phases = bundle.get("phases", {})
    steer = phases.get(_key("steer", cycle), {})
    revision = phases.get(_key("revision", cycle), {})
    resume = phases.get(_key("resume", cycle), {})
    counter = phases.get(_key("counterfactual", cycle), {})

    seed_validator = _seed_validator_stub()
    base = {VALIDATOR_PATH: seed_validator}

    final = (resume.get("final_candidate") or {}).get("validator_content") or ""
    unsteered = (counter.get("unsteered_candidate") or {}).get("validator_content") or ""

    steer_row = steer.get("steer_row") or {}
    moments = steer_row.get("moments") or {}
    steering_journal = None
    for dispatch in steer.get("dispatches", []):
        meta = dispatch.get("candidate_meta") or {}
        if isinstance(meta.get("steering_journal"), list):
            steering_journal = meta["steering_journal"]
    command = sc.command_evidence_of_row(
        {
            "command_id": steer_row.get("command_id"),
            "kind": "steer",
            "payload": {"text": STEER_TEXT},
            "status": steer_row.get("status"),
            "journal": steer_row.get("journal") or [],
        }
    )
    application = (
        sc.SteerApplicationEvidence(
            command_id=str(steer_row.get("command_id") or ""),
            applied_at=str(moments.get("applied_at") or ""),
            application_observed_at=str(moments.get("application_observed_at") or ""),
            delivery_mode=steer_delivery_mode(steer_row, {"steering_journal": steering_journal}),
            journal_entry={},
        )
        if steer_row
        else None
    )
    pause_checkpoint = steer.get("pause_checkpoint") or {}
    checkpoints = (
        sc.CheckpointEvidence(
            checkpoint_id=str(pause_checkpoint.get("checkpoint_id") or ""),
            files=int(pause_checkpoint.get("files") or 0),
            uploaded_at=str(pause_checkpoint.get("uploaded_at") or ""),
        ),
    )
    revision_after = revision.get("after") or {}
    revision_staged = revision.get("staged") or {}
    revision_evidence = (
        sc.RevisionEvidence(
            decision_id=str(revision_staged.get("decision_id") or ""),
            staged_at=str(revision_staged.get("staged_at") or ""),
            approved_at=str((revision.get("approve_note") or {}).get("posted_at") or ""),
            recomputed_digest=str(revision_staged.get("revision2_digest") or ""),
            staged_digest=str(revision_staged.get("staged_digest") or ""),
            # The staged ACTIVE plan carries the run's REAL digest (the
            # world /go froze) — the pre-approval equality the grader's
            # arm 4 checks is against THAT, not revision 1's canonical
            # spelling (which stays in the trace for the audit).
            active_plan_digest_before=str(revision_staged.get("active_digest_seeded") or ""),
            active_plan_digest_after=str(revision_after.get("active_plan_digest") or ""),
            run_plan_digest_before=str((revision.get("before") or {}).get("run_plan_digest") or ""),
            run_plan_digest_after=str(revision_after.get("run_plan_digest") or ""),
            reuse_route=str(revision_after.get("reuse_route") or ""),
            preserved_checkpoint_id=str(revision_after.get("preserved_checkpoint_id") or ""),
        )
        if revision_staged
        else None
    )
    envelope = resume.get("resume_dispatch_envelope") or {}
    dispatch_evidence = (
        sc.ResumeDispatchEvidence(
            envelope_digest=str(envelope.get("envelope_digest") or ""),
            dispatched_checkpoint_id=str(envelope.get("checkpoint") or ""),
            decision_id=str(envelope.get("decision_id") or ""),
            resume_mode=str(envelope.get("resume_mode") or ""),
        )
        if envelope
        else None
    )
    trace = sc.CombinedTrace(
        command=command,
        application=application,
        edits=sc.edit_set_of(base, {VALIDATOR_PATH: final} if final else {}),
        counterfactual_edits=sc.edit_set_of(base, {VALIDATOR_PATH: unsteered} if unsteered else {}),
        checkpoints=checkpoints,
        revision=revision_evidence,
        resume_dispatch=dispatch_evidence,
        provenance="live-lane:claude-sdk-lane/glm-5.3-flash",
    )
    grade = sc.grade_combined_trace(trace)

    spend_jobs: dict[str, Any] = {}
    for phase_name, phase_doc in phases.items():
        for dispatch in phase_doc.get("dispatches", []):
            receipt = dispatch.get("usage_receipt")
            if isinstance(receipt, Mapping):
                spend_jobs[str(dispatch.get("lane_job_id"))] = spend_from_receipts([receipt])
    total = (
        round(sum(float(job["total_usd"]) for job in spend_jobs.values()), 4) if spend_jobs else 0.0
    )
    basis = (
        "+".join(sorted({str(job["cost_basis"]) for job in spend_jobs.values()}))
        or "no-receipts-yet"
    )

    steer_note = steer.get("steer_note") or {}
    pause_note = (steer.get("interleaving_notes") or {}).get("pause") or {}
    interleaving = steer.get("interleaving_rows") or {}
    document = {
        "schema": TRACE_SCHEMA,
        "generated_at": _now(),
        "provenance": trace.provenance,
        "task": {
            "approach_x_entrypoint": X_NAME,
            "steer_y_entrypoint": Y_NAME,
            "bulk_entrypoint": BULK_NAME,
            "validator_path": VALIDATOR_PATH,
            "steer_text": STEER_TEXT,
            "seed_commit_sha": phases.get("setup", {}).get("seed_commit_sha"),
            "lane_ref": LANE_REF_SHA,
            "template_sha256": phases.get("setup", {}).get("template_sha256"),
        },
        "milestones": {
            "command": {
                "note_id": steer_note.get("note_id"),
                "posted_at": steer_note.get("posted_at"),
                "command_id": steer_row.get("command_id"),
                "dedup_key": steer_row.get("dedup_key"),
                "received_at": command.received_at,
                "authorized_at": command.authorized_at,
                "applied_at": moments.get("applied_at"),
                "status": steer_row.get("status"),
                "rungs": steer_row.get("rungs"),
            },
            "application": {
                "application_observed_at": moments.get("application_observed_at"),
                "delivery_mode": application.delivery_mode if application else "",
            },
            "edit": {
                "checkpoint_id": pause_checkpoint.get("checkpoint_id"),
                "files": pause_checkpoint.get("files"),
                "uploaded_at": pause_checkpoint.get("uploaded_at"),
                "final_candidate": resume.get("final_candidate"),
            },
            "pause": {
                "command_id": (interleaving.get("pause") or {}).get("command_id"),
                "posted_at": pause_note.get("posted_at"),
                "rungs": (interleaving.get("pause") or {}).get("rungs"),
                "checkpointed_at": next(
                    (
                        str(entry.get("at") or "")
                        for entry in (interleaving.get("pause") or {}).get("journal") or []
                        if entry.get("to") == "checkpointed"
                    ),
                    "",
                ),
            },
            "interleaving": interleaving,
            "revision": {
                "decision_id": revision_staged.get("decision_id"),
                "staged_at": revision_staged.get("staged_at"),
                "approved_at": (revision.get("approve_note") or {}).get("posted_at"),
                "activated_digest": revision_after.get("run_plan_digest"),
                "revision1_digest": revision_staged.get("revision1_digest"),
                "revision2_digest": revision_staged.get("revision2_digest"),
                "reuse_route": revision_after.get("reuse_route"),
                "reuse_route_reason": revision_after.get("reuse_route_reason"),
                "preserved_checkpoint_id": revision_after.get("preserved_checkpoint_id"),
            },
            "resume_dispatch": {
                "envelope_digest": envelope.get("envelope_digest"),
                "checkpoint": envelope.get("checkpoint"),
                "decision_id": envelope.get("decision_id"),
                "resume_mode": envelope.get("resume_mode"),
                "retry_note": (resume.get("retry_note") or {}).get("posted_at"),
            },
            "standing_direction": (
                {
                    "note_id": (resume.get("standing_direction_note") or {}).get("note_id"),
                    "posted_at": (resume.get("standing_direction_note") or {}).get("posted_at"),
                    "fate": resume.get("standing_direction_fate"),
                }
                if resume.get("standing_direction_note")
                else None
            ),
            "counterfactual": counter.get("unsteered_candidate"),
            "mr": resume.get("mr"),
            "counterfactual_mr": counter.get("mr"),
            "blocked_classification": {
                "status": steer.get("blocked_classification", {}).get("status"),
                "status_reason": steer.get("blocked_classification", {}).get("status_reason"),
            },
        },
        "grade": grade.as_document(),
        "timings": {
            "command_to_ack": {
                "note_posted_at": steer_note.get("posted_at"),
                "received_at": command.received_at,
                "clocks": "note=driver-host, rungs=lab-postgres",
            },
            "command_to_applied": {
                "received_at": command.received_at,
                "applied_at": moments.get("applied_at"),
            },
            "command_to_vendor_application": {
                "applied_at": moments.get("applied_at"),
                "application_observed_at": moments.get("application_observed_at"),
            },
            "pause_to_checkpoint": {
                "pause_posted_at": pause_note.get("posted_at"),
                "checkpoint_uploaded_at": pause_checkpoint.get("uploaded_at"),
            },
        },
        "spend": {
            "cap_usd": SPEND_CAP_USD,
            "total_usd": total,
            "cost_basis": basis,
            "jobs": spend_jobs,
        },
        "failures": list(bundle.get("failures", [])),
        "honesty": {
            "delivery_mode": (application.delivery_mode if application else "unobserved"),
            "brief_freeze": (
                "the GitLab lane's brief is spec-frozen at /go: the material "
                "revision switches the durable plan identity (row digest + active "
                "plan + gate rebind) and the MR carries the revised digest, but "
                "the revised plan TEXT does not re-enter the lane brief on this "
                "driver — the PE-7 brief-rebind leg is the GitHub path; the "
                "revision's extension bytes therefore reach the model only if the "
                "frozen brief already carried the underlying scope"
            ),
            "counterfactual": (
                "ONE unsteered lane, model nondeterminism acknowledged, narrow "
                "task scope — the arm compares captured edits, never a guess"
            ),
            "staging_leg": (
                "revision 1 (active) and revision 2 (pending) were staged through "
                "the REAL revisions module inside the forge-app container — the "
                "proposal-emitter leg the live GitLab planner has not grown yet; "
                "the APPROVAL and the ACTIVATION are fully native"
            ),
        },
    }
    return document


def refresh_control_rows(bundle: Bundle, cycle: int = 1) -> None:
    """Re-read the durable control rows at collect time.

    The live phases snapshot each row the moment its key rung appeared —
    an EARLY ladder. The trace's causal chain reads the rows as they
    STAND (the steer's vendor_accepted/applied/checkpointed rungs land
    after the drain's cycle completes), so the collect fold refreshes
    every recorded row from the durable truth first.
    """
    steer = bundle.document["phases"].get(_key("steer", cycle)) or {}
    run_id = str((steer.get("plan") or {}).get("run_id") or "")
    if not run_id:
        return
    rows = {str(row.get("id")): row for row in control_rows(run_id)}
    recorded = steer.get("steer_row") or {}
    if str(recorded.get("command_id") or recorded.get("id")) in rows:
        fresh = rows[str(recorded.get("command_id") or recorded.get("id"))]
        recorded["status"] = fresh.get("status")
        recorded["rungs"] = _rungs(fresh)
        recorded["journal"] = fresh.get("journal")
        recorded["moments"] = application_moments(fresh)
    interleaving = steer.get("interleaving_rows") or {}
    for name, entry in interleaving.items():
        fresh = rows.get(str(entry.get("command_id") or ""))
        if fresh is not None:
            entry["status"] = fresh.get("status")
            entry["rungs"] = _rungs(fresh)
            entry["journal"] = fresh.get("journal")
            if "moments" in entry:
                entry["moments"] = application_moments(fresh)
    bundle.save()


def _prior_cycles(bundle: Mapping[str, Any], upto: int) -> list[dict[str, Any]]:
    """The earlier cycles' honest outcome summaries (never discarded)."""
    summaries: list[dict[str, Any]] = []
    for cycle in range(1, upto):
        trace = build_trace(dict(bundle), cycle)
        final = (trace["milestones"].get("edit") or {}).get("final_candidate") or {}
        summaries.append(
            {
                "cycle": cycle,
                "grade": trace["grade"],
                "run_id": (
                    bundle.get("phases", {})
                    .get(_key("steer", cycle), {})
                    .get("plan", {})
                    .get("run_id")
                ),
                "final_y_entrypoint_present": final.get("y_entrypoint_present"),
                "final_x_entrypoint_present": final.get("x_entrypoint_present"),
            }
        )
    return summaries


def phase_collect(bundle: Bundle) -> int:
    record = bundle.phase("collect")
    cycle = latest_cycle(bundle)
    for earlier in range(1, cycle + 1):
        refresh_control_rows(bundle, earlier)
    trace = build_trace(bundle.document, cycle)
    trace["cycle"] = cycle
    trace["prior_cycles"] = _prior_cycles(bundle.document, cycle)
    if cycle > 1:
        # The LIVE-found cycle-1 finding, stated beside its successor: the
        # resumed lane obeyed the spec-frozen brief over the restored WIP's
        # naming — the standing-direction steer is the operator's native
        # remedy and cycle 2's reason to exist.
        prior_final = (trace["prior_cycles"] or [{}])[-1]
        if prior_final.get("final_x_entrypoint_present"):
            trace["honesty"]["cycle1_revert_finding"] = (
                "cycle 1's resumed lane REVERTED the steered naming: the final "
                "candidate carried `def check(` again because the GitLab lane's "
                "brief is spec-frozen at /go (it still names approach X) and the "
                "resumed model obeyed the brief over the restored WIP — the "
                "checkpoint itself carried the Y rename (proven by its blob), so "
                "the steer's causal effect on the turn's work stood; the "
                "continuation semantics, not the steering, lost it. The remedy "
                "this driver natively offers — the operator's standing direction "
                "as a PENDING durable steer the resumed lane drains mid-turn — "
                "is what cycle 2 composes."
            )
    ok, missing = sc.combined_record_valid(trace)
    trace["record_valid"] = {"ok": ok, "missing": missing}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_PATH.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = {
        "schema": REPORT_SCHEMA,
        "issue": "R38-12 / #313",
        "generated_at": _now(),
        "grade": trace["grade"],
        "record_valid": trace["record_valid"],
        "spend": trace["spend"],
        "mr": trace["milestones"]["mr"],
        "counterfactual": {
            "mr": trace["milestones"]["counterfactual_mr"],
            "candidate": trace["milestones"]["counterfactual"],
        },
        "honesty": trace["honesty"],
        "trace_file": TRACE_PATH.name,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["result"] = "green"
    record["finished_at"] = _now()
    bundle.save()
    print(f"collect: combined trace written → {TRACE_PATH}")
    print(f"collect: grade causal={trace['grade']['causal']} spend=${trace['spend']['total_usd']}")
    return 0


def phase_teardown(bundle: Bundle, gitlab: GitLab) -> int:
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    record = bundle.phase("teardown")
    if not record.get("deleted"):
        response = gitlab.delete(f"/projects/{project_id}")
        if response.status_code not in (202, 200, 204):
            raise Refused(f"project delete failed: {response.status_code} {response.text[:200]}")
        record["deleted"] = True
        record["deleted_at"] = _now()
    bundle.save()
    print(f"teardown: disposable project {project_id} deleted (evidence retained)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_combined_steering.py",
        description=(
            "R38-12 (#313): the combined steering qualification — the native /steer "
            "into a REAL running SDK lane, the urgent-pause interleaving, the "
            "material revision, the preserved-WIP resume, the counterfactual arm, "
            "the independent oracle and a Draft MR. Phases are resumable."
        ),
    )
    parser.add_argument(
        "phase",
        choices=[
            "setup",
            "preflight",
            "steer",
            "revision",
            "resume",
            "counterfactual",
            "collect",
            "teardown",
        ],
    )
    parser.add_argument("--evidence", type=Path, default=EVIDENCE_PATH)
    parser.add_argument(
        "--project-name", default=f"forge-steer-{datetime.now(timezone.utc):%Y-%m-%d}"
    )
    parser.add_argument(
        "--cycle",
        type=int,
        default=1,
        help=(
            "the iteration cycle this invocation drives (1 = the first live "
            "attempt; 2+ = the honest follow-up cycles whose phases land "
            "under <phase>~cN keys, never over cycle 1's record)"
        ),
    )
    args = parser.parse_args(argv)

    bundle = Bundle(args.evidence)
    settings = Settings()
    gitlab = GitLab(settings)
    cycle = max(1, int(args.cycle))
    handlers = {
        "setup": lambda: phase_setup(bundle, gitlab, args.project_name, settings),
        "preflight": lambda: phase_preflight(bundle, gitlab),
        "steer": lambda: phase_steer(bundle, gitlab, cycle),
        "revision": lambda: phase_revision(bundle, gitlab, cycle),
        "resume": lambda: phase_resume(bundle, gitlab, cycle),
        "counterfactual": lambda: phase_counterfactual(bundle, gitlab, cycle),
        "collect": lambda: phase_collect(bundle),
        "teardown": lambda: phase_teardown(bundle, gitlab),
    }
    try:
        return handlers[args.phase]()
    except Refused as exc:
        _record_failure(bundle, str(exc))
        print(f"{args.phase}: REFUSED — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
