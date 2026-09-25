"""R38-05 (#306) — qualify useful-WIP cross-runner continuation, live.

The R37-08 (#289) interrupted arm paused BEFORE file edits existed
(checkpoint ``files=0`` after a ~5 s blind pause), so the resumed real-model
turns delivered EMPTY diffs — control/transport mechanics proven, useful-WIP
preservation NOT. This driver re-runs the interrupted arm with the pause
sampled AFTER observed useful work, through the NOW-SHIPPED composition:

- the #302 finalization is the SHIPPED template (``ci/templates/
  claude-sdk-lane.gitlab-ci.yml`` — phased driver/collection/final-status,
  the packaged collector with ``--require-generation`` on resume), inlined
  verbatim into a NEW disposable project ``forge-wip-<date>`` (project 68's
  template is never edited by hand; the generation flows from the repo);
- the bounded task exercises THREE file shapes — a NEW file
  (``src/utils/text.py::slugify``), a MODIFICATION (``src/app.py`` drops the
  deprecated import and uses slugify) and a deliberate DELETION
  (``src/utils/legacy.py``) — with the independent precommitted oracle
  (the repo's own ``smoke`` CI job) committed BEFORE any run;
- the observed-work waiter: the shipped lane exposes NO pre-pause
  edit-event channel (verified against the R37-08 live traces: the job log
  is silent while the vendor turn runs, and checkpoints only land from the
  pause drain's capture), so the pause/checkpoint transaction IS the
  sampling instrument. The waiter samples at an ADAPTIVE LADDER of
  observed anchors — each rung fires at a fixed offset AFTER the OBSERVED
  driver-phase start (a GitLab trace timestamp, never a blind job-start
  sleep), and the ladder advances ONLY on an observed EMPTY checkpoint
  (the R37-08 ``files=0`` failure mode, now measured, re-entered through
  the native ``/retry`` continuation). Never a fixed five-second pause.
- once a checkpoint reports ``files>0`` with the manifest's new/modified/
  deleted evidence: job-level cancel (never container-level), the honest
  blocked classification, ``/retry`` → the second lane restores the EXACT
  authorized checkpoint (``--require-generation`` guards the generation)
  and continues with the real model; the FINAL candidate (preserved WIP +
  legitimate continuation) is collected by the promoted template's
  finalization, verified by the independent oracle on the exact candidate
  sha, and left as a Draft MR (a human merges — forge never does).

Every identity is recorded (checkpoint id/digest, decision id, envelope
digest, both job ids, the collected diff digest, the oracle run, the SDK
spend receipts, the timings, every failed attempt and its root cause).
The R37-08 empty-WIP trace stays referenced as superseded negative
evidence — never overwritten.

Phases are resumable; the evidence bundle on disk is the state. Default
mode for every phase is a REFUSAL on any precondition failure — a failure
is recorded honestly, never retried into a green.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

from forge.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-25-useful-wip-resume"
EVIDENCE_PATH = EVAL_DIR / "live-run-evidence.json"
#: The evidence record's home: the drill's own evaluation directory, beside
#: its ``live-run-evidence.json`` bundle (the R37-08 precedent for committed
#: drill evidence). NOT under ``qualification/`` — BOTH of its stores are
#: typed and fail-closed (``load_profile_records`` refuses the whole load on
#: any ``records/*.json`` without the ``forge.profile.qualification/1``
#: stamp; the traces loader does the same for ``forge.trace/1``): this
#: drill's record is a different type and must not weaken those guards.
RECORD_PATH = EVAL_DIR / "useful-wip-resume-2026-09-25.json"
ALIGNMENT_RECEIPTS = EVAL_DIR / "alignment-receipts.json"
APP_API = "http://localhost:8420"

#: The shipped lane template (the #302 finalization) — inlined VERBATIM
#: into the disposable project's CI (never a hand-edited copy; the file
#: IS the recipe).
TEMPLATE_SOURCE = REPO_ROOT / "ci" / "templates" / "claude-sdk-lane.gitlab-ci.yml"

#: The immutable lane install pin: the repo sha pushed to origin that
#: carries the R36 generation-aware collector (``--require-generation``)
#: and the R37-07 dispatch envelope the shipped template drives.
LANE_REF_SHA = "59ba869a312e9c13c120b82034b90f669a894ce6"

#: The R37-08 empty-WIP trace — SUPERSEDED NEGATIVE EVIDENCE, referenced
#: verbatim, never overwritten by this run.
SUPERSEDED_EMPTY_WIP = {
    "works_index": "data/checkpoints/works/905194f0e4314e9aa126325b37637d1b.json",
    "work_id": "905194f0e4314e9aa126325b37637d1b",
    "checkpoint_id": "3cb49a16540d08e146037d164190600961e43890e7493dc739e3fa3fb2f16d2d",
    "files": 0,
    "uploaded_at": "2026-09-24T12:34:49+00:00",
    "record": "docs/evaluation/2026-09-24-live-single-writer/README.md §4",
    "note": "kept verbatim as negative historical evidence (R38-05 scope item 5)",
}

#: The spend cap (issue #306: "$2 total") and the probe-ladder's own
#: guardrail — a new rung refuses once recorded lane spend crosses this.
SPEND_CAP_USD = 2.0
SPEND_LADDER_GUARD_USD = 1.6

#: The frozen acceptance task (R38-05): THREE file shapes.
SLUGIFY_CASES: tuple[tuple[str, str], ...] = (
    ("Hello, World!", "hello-world"),
    ("Forge WIP--resume __2026", "forge-wip-resume-2026"),
    ("   spaces   everywhere   ", "spaces-everywhere"),
    ("already-slugged", "already-slugged"),
    ("MIXED Case 123", "mixed-case-123"),
    ("!!!leading and trailing!!!", "leading-and-trailing"),
)

#: The three shapes, exactly as the oracle and the diff verification
#: assert them: (path, shape) — shape ∈ {"new", "modified", "deleted"}.
TASK_SHAPES: tuple[tuple[str, str], ...] = (
    ("src/utils/text.py", "new"),
    ("src/app.py", "modified"),
    ("src/utils/legacy.py", "deleted"),
)

#: The observed-work waiter's rung ladder, in seconds AFTER the OBSERVED
#: driver-phase start. Anchored on MEASURED turn timelines — the R37-08
#: lane's driver leg ran 133 s wall; THIS drill's first live lane (job 762,
#: 2026-09-24) ran a measured 87.3 s turn, so the rungs sample the turn's
#: middle first (edits for a three-shape task land in the first ~2/3). A
#: rung advances the ladder ONLY after the previous rung's checkpoint was
#: observed EMPTY — the adaptive record, never a blind constant pause.
PROBE_RUNGS_S: tuple[float, ...] = (45.0, 75.0, 105.0)

#: The bounds of the whole drill.
MAX_POLL_SECONDS_DEFAULT = 1200
POLL_INTERVAL_S = 10.0
RUN_ID_RE = re.compile(r"go ([0-9a-f]{32})")

#: GitLab job-trace line shape: ``2026-09-24T12:28:02.515131Z 01O <body>``
#: (the stream id and the O/E type letter form one token, e.g. ``01O``).
_TRACE_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+\d+[OE]\s?(.*)$")
#: The driver-phase start: GitLab echoes the collapsed multi-line script
#: block (its FIRST line) when it BEGINS executing — the turn is starting
#: at this stamp. LIVE-found (job 762): the #302 template's driver block
#: opens with a COMMENT line (``# before_script created .forge/; …``);
#: the pre-#302 template opened with ``FORGE_DRIVER_EXIT=``; a defensive
#: third spelling matches the block's first real command.
_DRIVER_PHASE_MARKERS: tuple[str, ...] = (
    "before_script created .forge",
    "FORGE_DRIVER_EXIT=",
    "mkdir -p .forge # collapsed",
)

MANIFEST_SCHEMA = "forge.wip.manifest/2"
RECORD_SCHEMA = "forge.useful-wip-resume/1"

#: The per-token price class of the lane route (glm-5.3-flash through the
#: customer gateway) — ONLY used to FLAG spend when the SDK receipt
#: carries no ``total_cost_usd``; the recorded spend prefers the SDK's own
#: cost field everywhere it exists (the R37-08 receipts carried it).
FALLBACK_PRICE_PER_MTOK = {"input": 0.60, "cached_input": 0.07, "output": 2.20}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> float:
    return time.monotonic()


class Refused(Exception):
    """A precondition failed or a bounded wait expired — recorded, never retried into a green."""


# ---------------------------------------------------------------------------
# The task fixture — three file shapes + the independent precommitted oracle
# ---------------------------------------------------------------------------


def task_shapes() -> tuple[tuple[str, str], ...]:
    """The three file shapes the drill requires (path, shape)."""
    return TASK_SHAPES


def issue_title() -> str:
    return "Replace the legacy helper: slugify() in src/utils/text.py, rewire src/app.py, delete src/utils/legacy.py"


def issue_body() -> str:
    cases = "\n".join(f"  slugify({text!r}) == {expected!r}" for text, expected in SLUGIFY_CASES)
    return f"""\
## Task (three changes, exactly)

1. **NEW file `src/utils/text.py`** — implement:

```python
def slugify(text: str) -> str
```

Lowercase the input; every RUN of non-alphanumeric characters becomes ONE
`-`; no leading or trailing `-`; an empty (or all-separator) input returns
`""`.

{cases}

2. **MODIFY `src/app.py`** — stop importing the deprecated helper:
remove the `utils.legacy` import and the `shout()` call; import
`slugify` from `utils.text` and use it so `greet()` still returns a
greeting string (slugified where `shout` was used).

3. **DELETE `src/utils/legacy.py`** — the deprecated module is removed
entirely (no references may remain).

### Bounds

- Only `src/utils/text.py` (new), `src/app.py` (modified) and
  `src/utils/legacy.py` (deleted) may change.
- Do NOT modify `.gitlab-ci.yml`, `tests/`, `README.md` or any other file.
- Do not commit or push; leave changes in the working tree.

The repository's `smoke` CI job asserts all three shapes — it is the
independent oracle and it is NOT part of your change.
"""


def _slugify_impl() -> str:
    return (
        "import re\n"
        "\n"
        "\n"
        "def slugify(text: str) -> str:\n"
        '    """Lowercase; non-alphanumeric runs collapse to one "-"; no edges."""\n'
        '    slugged = re.sub(r"[^a-z0-9]+", "-", text.lower())\n'
        '    return slugged.strip("-")\n'
    )


def smoke_oracle_script() -> str:
    """The independent oracle — six exact slugify cases PLUS the three
    file shapes (app.py rewired, legacy.py gone). Committed BEFORE any
    run; a candidate that touches it fails the drill."""
    cases = "\n".join(f'    ("{text}", "{expected}"),' for text, expected in SLUGIFY_CASES)
    return (
        "python3 - <<'PY'\n"
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, 'src')\n"
        "from utils.text import slugify\n"
        "CASES = [\n" + cases + "\n]\n"
        "for text, expected in CASES:\n"
        "    got = slugify(text)\n"
        "    assert got == expected, (text, got, expected)\n"
        "print('slugify oracle: %d/%d OK' % (len(CASES), len(CASES)))\n"
        "app = Path('src/app.py').read_text(encoding='utf-8')\n"
        "assert 'legacy' not in app, 'src/app.py still references legacy'\n"
        "assert 'slugify' in app, 'src/app.py does not use slugify'\n"
        "assert not Path('src/utils/legacy.py').exists(), 'src/utils/legacy.py still exists'\n"
        "print('shape oracle: app rewired, legacy deleted')\n"
        "PY\n"
    )


def _seed_readme(name: str) -> str:
    return (
        f"# {name}\n\nA DISPOSABLE repository for the R38-05 (#306) live\n"
        "useful-WIP cross-runner continuation qualification. The acceptance task:\n"
        "NEW `src/utils/text.py::slugify`, MODIFY `src/app.py` to use it, DELETE\n"
        "`src/utils/legacy.py` — asserted by the `smoke` oracle committed before\n"
        "any run. This project is deleted after the qualification evidence is\n"
        "captured.\n"
    )


def _seed_app_py() -> str:
    return (
        '"""The app entry — still on the deprecated helper (to be rewired)."""\n'
        "\n"
        "from utils.legacy import shout\n"
        "\n"
        "\n"
        "def greet(name: str) -> str:\n"
        '    return shout(f"hello {name}")\n'
    )


def _seed_legacy_py() -> str:
    return (
        '"""DEPRECATED shouting helper — scheduled for deletion (R38-05)."""\n'
        "\n"
        "\n"
        "def shout(text: str) -> str:\n"
        "    return text.upper() + '!'\n"
    )


def _tests_file() -> str:
    cases = "\n".join(f'    ("{text}", "{expected}"),' for text, expected in SLUGIFY_CASES)
    return (
        '"""The independent three-shape oracle, mirrored as a test file.\n\n'
        "Committed before any qualification run; the smoke CI job asserts\n"
        "the same cases — this file is for the human reviewer.\n"
        '"""\n'
        "import sys\n"
        "from pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n\n"
        "from utils.text import slugify\n\n"
        "CASES = [\n" + cases + "\n]\n\n"
        "def test_slugify_oracle() -> None:\n"
        "    for text, expected in CASES:\n"
        "        assert slugify(text) == expected, (text, slugify(text), expected)\n\n\n"
        "def test_shapes() -> None:\n"
        "    root = Path(__file__).resolve().parents[1]\n"
        "    app = (root / 'src' / 'app.py').read_text(encoding='utf-8')\n"
        "    assert 'legacy' not in app\n"
        "    assert 'slugify' in app\n"
        "    assert not (root / 'src' / 'utils' / 'legacy.py').exists()\n"
    )


def ci_yaml() -> str:
    """The disposable project's CI: the SHIPPED template job VERBATIM (the
    #302 finalization) + the stages + the independent smoke oracle.

    The template file is read from the repo at generation time (never a
    hand-edited copy); the only additions are the shared ``stages`` list
    and the ``smoke`` oracle job the template expects the target repo to
    own.
    """
    template = TEMPLATE_SOURCE.read_text(encoding="utf-8")
    if "forge-agent-claude-sdk:" not in template:
        raise Refused(f"{TEMPLATE_SOURCE} carries no forge-agent-claude-sdk job — regenerate")
    if "--require-generation" not in template:
        raise Refused(
            f"{TEMPLATE_SOURCE} carries no --require-generation collector flag — "
            "the #302 finalization is not in the tree; REFUSING to ship a stale recipe"
        )
    return (
        "# Generated by scripts/run_useful_wip_resume.py (R38-05/#306): the\n"
        "# SHIPPED SDK lane template VERBATIM (the #302 phased finalization —\n"
        "# driver / collection / final-status, the packaged collector with\n"
        "# --require-generation on resume) plus this project's independent\n"
        "# three-shape smoke oracle, committed BEFORE any run.\n"
        "stages: [test, harness]\n\n"
        + template
        + "\n# The INDEPENDENT verification contract (R38-05): the six exact\n"
        "# slugify cases AND the three file shapes, committed BEFORE any run.\n"
        "# A candidate that weakens or bypasses this job is a FAILED candidate\n"
        "# (the qualification driver also asserts the candidate diff does not\n"
        "# touch this file).\n"
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
    """The seed tree: the deprecated baseline the three shapes transform."""
    return {
        "README.md": _seed_readme(name),
        ".gitlab-ci.yml": ci_yaml(),
        "tests/test_text_utils.py": _tests_file(),
        "src/app.py": _seed_app_py(),
        "src/utils/__init__.py": "",
        "src/utils/legacy.py": _seed_legacy_py(),
    }


VARIABLES_FROM_LAB = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "FORGE_BOT_READ_TOKEN",
    "FORGE_HARNESS_HTTPS_PROXY",
)


# ---------------------------------------------------------------------------
# The observed-work waiter — offline-testable machinery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeRung:
    """One pause-sample rung: an offset AFTER the observed driver start."""

    index: int
    offset_s: float

    @property
    def label(self) -> str:
        return f"rung-{self.index + 1}@{self.offset_s:.0f}s"


def probe_rungs(offsets: Sequence[float] = PROBE_RUNGS_S) -> tuple[ProbeRung, ...]:
    if not offsets:
        raise ValueError("at least one probe rung is required")
    return tuple(ProbeRung(index=i, offset_s=float(o)) for i, o in enumerate(offsets))


def driver_phase_started_at(trace: str) -> str | None:
    """The GitLab trace timestamp when the driver-phase script block began.

    GitLab echoes a collapsed multi-line script block when it STARTS
    executing; the shipped template's driver block begins with
    ``mkdir -p .forge`` (the pre-#302 template began with
    ``FORGE_DRIVER_EXIT=`` — both spellings recognized). The turn — the
    only window in which file edits can exist — starts at this stamp.
    """
    for line in trace.splitlines():
        match = _TRACE_LINE_RE.match(line)
        if match is None:
            continue
        stamp, body = match.groups()
        if "# collapsed multi-line command" not in body:
            continue
        if any(marker in body for marker in _DRIVER_PHASE_MARKERS):
            return stamp
    return None


def trace_anchor_epoch(stamp: str) -> float:
    """Parse a GitLab trace timestamp (UTC, Z-suffixed) to a POSIX epoch."""
    return (
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    )


def works_index_path(store_root: Path, work_id: str) -> Path:
    return store_root / "works" / f"{work_id}.json"


def read_works_index(store_root: Path, work_id: str) -> dict[str, Any] | None:
    path = works_index_path(store_root, work_id)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if document.get("work_id") != work_id:
        return None
    return document


def latest_checkpoint_entry(index: Mapping[str, Any]) -> dict[str, Any] | None:
    """The works index's highest-sequence checkpoint entry, or None."""
    entries = [dict(e) for e in (index.get("checkpoints") or []) if isinstance(e, Mapping)]
    if not entries:
        return None
    return max(entries, key=lambda e: int(e.get("sequence") or 0))


def manifest_path(store_root: Path, checkpoint_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", checkpoint_id):
        raise Refused(f"not a checkpoint content address: {checkpoint_id!r}")
    return store_root / checkpoint_id[:2] / checkpoint_id


def read_manifest(store_root: Path, checkpoint_id: str) -> dict[str, Any]:
    path = manifest_path(store_root, checkpoint_id)
    if not path.is_file():
        raise Refused(f"checkpoint manifest {checkpoint_id} is absent from the store")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise Refused(
            f"checkpoint {checkpoint_id} carries schema {manifest.get('schema')!r} "
            f"(expected {MANIFEST_SCHEMA})"
        )
    if (
        manifest.get("work_id")
        and checkpoint_id
        != hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    ):
        raise Refused(f"checkpoint {checkpoint_id} manifest does not hash to its own address")
    return manifest


def useful_wip(entry: Mapping[str, Any]) -> bool:
    """The waiter's predicate: the checkpoint reports files>0.

    The R37-08 failure mode was ``files=0`` — the pause sampled a tree
    with no edits. Anything above zero is useful WIP worth preserving.
    """
    try:
        return int(entry.get("files") or 0) > 0
    except (TypeError, ValueError):
        return False


def manifest_evidence(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project a checkpoint manifest into the drill's evidence shape:
    per-path roles + digests, deletions, and COVERAGE of the task's three
    file shapes (the acceptance wants new+modified+deleted evidence)."""
    files: dict[str, dict[str, Any]] = {
        str(path): {
            "role": str(entry.get("role")),
            "digest": str(entry.get("digest")),
            "mode": entry.get("mode"),
        }
        for path, entry in (manifest.get("files") or {}).items()
        if isinstance(entry, Mapping)
    }
    deletions = sorted(str(path) for path in (manifest.get("deletions") or []))
    coverage: dict[str, str] = {}
    for path, shape in task_shapes():
        if shape == "deleted":
            coverage[path] = "deleted" if path in deletions else "missing"
        else:
            entry = files.get(path)
            if entry is None:
                coverage[path] = "missing"
            elif entry["role"] == shape:
                coverage[path] = "present"
            else:
                coverage[path] = f"wrong-role:{entry['role']}"
    return {
        "files": files,
        "deletions": deletions,
        "file_count": len(files),
        "digests_hex64": all(
            re.fullmatch(r"[0-9a-f]{64}", entry["digest"]) for entry in files.values()
        ),
        "shape_coverage": coverage,
        "all_shapes_present": all(value in ("present", "deleted") for value in coverage.values()),
    }


def evaluate_probe(rung: ProbeRung, checkpoint: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ladder's verdict for ONE sampled checkpoint (offline-testable):

    - ``useful`` — files>0: the drill proceeds to cancel + the real resume;
    - ``empty`` — the R37-08 failure mode, measured: advance the ladder
      (a next rung exists) or exhaust it;
    - ``no-checkpoint`` — the pause produced nothing durable: refused
      (never guessed), the drill records and stops.
    """
    if checkpoint is None:
        return {"rung": rung.label, "verdict": "no-checkpoint", "advance": False}
    entry = dict(checkpoint)
    if useful_wip(entry):
        return {"rung": rung.label, "verdict": "useful", "advance": False, "checkpoint": entry}
    return {
        "rung": rung.label,
        "verdict": "empty",
        "advance": rung.index + 1 < len(probe_rungs()),
        "checkpoint": entry,
    }


def spend_from_receipts(receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum the lane's SDK usage receipts into the drill's spend record.

    Prefers each receipt's own ``total_cost_usd`` (the SDK's cost field —
    the R37-08 receipts carried it); when absent, estimates from token
    counts on the recorded price class and LABELS the estimate as such
    (never an unmarked guess).
    """
    total = 0.0
    estimated = False
    tokens = {"input": 0, "cached_input": 0, "output": 0}
    for receipt in receipts:
        cost = receipt.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            total += float(cost)
            continue
        estimated = True
        tokens["input"] += int(receipt.get("input_tokens") or 0)
        tokens["cached_input"] += int(receipt.get("cached_input_tokens") or 0)
        tokens["output"] += int(receipt.get("output_tokens") or 0)
    if estimated:
        total += (
            tokens["input"] * FALLBACK_PRICE_PER_MTOK["input"]
            + tokens["cached_input"] * FALLBACK_PRICE_PER_MTOK["cached_input"]
            + tokens["output"] * FALLBACK_PRICE_PER_MTOK["output"]
        ) / 1_000_000
    return {
        "receipt_count": len(receipts),
        "total_usd": round(total, 4),
        "cost_basis": "sdk-total_cost_usd+token-estimate" if estimated else "sdk-total_cost_usd",
        "tokens": tokens,
    }


# ---------------------------------------------------------------------------
# The evidence record — schema, build, validate (offline-testable)
# ---------------------------------------------------------------------------


def new_record(project: str, created_at: str) -> dict[str, Any]:
    return {
        "schema": RECORD_SCHEMA,
        "issue": "R38-05 (#306) useful-WIP cross-runner continuation",
        "created_at": created_at,
        "project": project,
        "superseded_negative_evidence": dict(SUPERSEDED_EMPTY_WIP),
        "probe_ladder": [],
        "spend": {"cap_usd": SPEND_CAP_USD, "jobs": {}, "total_usd": None},
        "failures": [],
        "outcome": "incomplete",
    }


#: The identity fields every COMPLETED record must carry (the drill's
#: own observability contract — issue #306's list).
REQUIRED_IDENTITIES = (
    "useful_checkpoint.id",
    "useful_checkpoint.digest",
    "resume.decision_id",
    "resume.envelope_digest",
    "resume.pipeline_id",
    "resume.lane_job_id",
    "candidate.diff_digest",
    "oracle.pipeline_id",
    "oracle.candidate_sha",
    "mr.url",
)


def _dig(document: Mapping[str, Any], dotted: str) -> Any:
    node: Any = document
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def validate_record(record: Mapping[str, Any]) -> list[str]:
    """Findings, never exceptions: an empty list is a valid record."""
    findings: list[str] = []
    if record.get("schema") != RECORD_SCHEMA:
        findings.append(f"schema is {record.get('schema')!r}, expected {RECORD_SCHEMA}")
    outcome = record.get("outcome")
    if outcome == "useful-wip-continued":
        for field in REQUIRED_IDENTITIES:
            if _dig(record, field) in (None, ""):
                findings.append(f"missing identity: {field}")
        spend = record.get("spend") or {}
        if not isinstance(spend.get("total_usd"), (int, float)):
            findings.append("spend.total_usd missing (honest spend is mandatory)")
        elif float(spend["total_usd"]) > SPEND_CAP_USD:
            findings.append(
                f"spend.total_usd {spend['total_usd']} exceeded the ${SPEND_CAP_USD} cap"
            )
        mr = record.get("mr") or {}
        if mr.get("merged"):
            findings.append("the MR was MERGED — the bot never merges (ADR-0003)")
        if not mr.get("draft"):
            findings.append("the MR is not a Draft — a human merges, never the drill")
        oracle = record.get("oracle") or {}
        if oracle.get("status") != "success":
            findings.append(f"oracle status {oracle.get('status')!r} is not success")
        shapes = _dig(record, "candidate.shapes_present") or {}
        for path, shape in task_shapes():
            if shapes.get(path) != shape:
                findings.append(
                    f"candidate shape {path} ({shape}) not present: {shapes.get(path)!r}"
                )
        checkpoint_shapes = _dig(record, "useful_checkpoint.shape_coverage") or {}
        if not checkpoint_shapes:
            findings.append("useful_checkpoint.shape_coverage missing")
    elif outcome in (
        "empty-wip-exhausted",
        "restore-failed",
        "model-no-op",
        "resumed-turn-failed",
        "refused",
    ):
        if not record.get("failures"):
            findings.append(f"outcome {outcome!r} requires recorded failures with root causes")
    elif outcome == "incomplete":
        findings.append("outcome is incomplete")
    else:
        findings.append(f"unknown outcome {outcome!r}")
    return findings


def write_record(record: Mapping[str, Any], path: Path = RECORD_PATH) -> None:
    findings = validate_record(record)
    document = dict(record)
    document["validation_findings"] = findings
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Live clients — GitLab (native surfaces), the app's read API, read-only psql
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
                "stamp": "forge.useful-wip.live/1",
                "issue": "R38-05 (#306) live useful-WIP resume",
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


# ---------------------------------------------------------------------------
# setup — the disposable project, its oracle, credentials, webhook
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
                "seed: the frozen three-shape task + the shipped #302 lane template "
                "+ the independent oracle (committed before any run)"
            ),
            "actions": actions,
        },
    )
    if commit.status_code not in (201, 200):
        raise Refused(f"seed commit failed: {commit.status_code} {commit.text[:300]}")
    seed_sha = commit.json().get("id")
    bundle.record("setup", "seed_commit_sha", seed_sha)
    bundle.record(
        "setup",
        "ci_yaml_sha256",
        hashlib.sha256(ci_yaml().encode()).hexdigest(),
    )
    bundle.record(
        "setup",
        "template_sha256",
        hashlib.sha256(TEMPLATE_SOURCE.read_bytes()).hexdigest(),
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
        ("FORGE_LANE_REF", LANE_REF_SHA),  # immutable lane identity (collector-capable)
        ("FORGE_STEERING_ENABLED", "1"),  # the lane-side pause consumer
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
        f"/projects/{project_id}/members",
        json={"user_id": bot_user_id, "access_level": 30},
    )
    if member.status_code not in (201, 200):
        raise Refused(f"bot membership grant failed: {member.text[:200]}")
    bundle.record(
        "setup", "bot_member", {"user_id": bot_user_id, "username": bot_username, "level": 30}
    )
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
    checks["schema_head"] = head

    if str(REPO_ROOT) not in sys.path:  # `python scripts/…` puts scripts/ on sys.path
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.inventory_lab import repo_schema_head

    try:
        expected_head = repo_schema_head(REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 — the preflight refuses, never guesses
        raise Refused(f"repo schema head unreadable: {exc}") from exc
    if head != expected_head:
        raise Refused(f"schema head {head} != repo chain head {expected_head}")

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
        checks[f"caps.{container}"] = {"budget_profiles": profiles, "lane_budget": lane_budget}
        if not (profiles and lane_budget):
            raise Refused(f"budget caps missing on {container}")

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
    print("preflight: GREEN (doctor + alignment axes + runner)")
    for name, value in checks.items():
        print(f"  - {name}: {json.dumps(value)[:160]}")
    return 0


# ---------------------------------------------------------------------------
# the interrupted arm — the observed-work waiter, pause, cancel, resume
# ---------------------------------------------------------------------------


def find_run_for_issue(gitlab: GitLab, project_id: int, issue_iid: int) -> dict[str, Any] | None:
    runs = app_get("/runs?limit=50").get("runs", [])
    for run in runs:
        if run.get("project_id") == project_id and run.get("issue_iid") == issue_iid:
            return run
    return None


def lane_job(gitlab: GitLab, project_id: int, pipeline_id: int) -> dict[str, Any] | None:
    for job in gitlab.get(f"/projects/{project_id}/pipelines/{pipeline_id}/jobs"):
        if str(job.get("name", "")).startswith("forge-agent"):
            return job
    return None


def job_trace(gitlab: GitLab, project_id: int, job_id: int) -> str:
    return gitlab.get_text(f"/projects/{project_id}/jobs/{job_id}/trace")


def grep_lines(trace: str, needle: str, limit: int = 8) -> list[str]:
    return [line for line in trace.splitlines() if needle in line][:limit]


def checkpoint_store_root() -> Path:
    return REPO_ROOT / "data" / "checkpoints"


def _latest_works_checkpoint(work_id: str) -> dict[str, Any] | None:
    index = read_works_index(checkpoint_store_root(), work_id)
    if index is None:
        return None
    return latest_checkpoint_entry(index)


def _newer_checkpoint(work_id: str, previous_id: str | None) -> dict[str, Any] | None:
    """The works index's latest checkpoint WHEN it is not the one already
    sampled (an empty re-read of the same checkpoint advances nothing)."""
    entry = _latest_works_checkpoint(work_id)
    if entry is None:
        return None
    if previous_id and entry.get("checkpoint_id") == previous_id:
        return None
    return entry


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
            f"the frozen lane was not selected: {harness_line!r} — the interruption arm "
            "needs the exact-resume SDK lane (FORGE_HARNESS_PREFERENCE)"
        )
    return {"issue_iid": issue_iid, "run_id": run_id}


def dispatch_pipeline(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, arc: dict[str, Any], key: str
) -> dict[str, Any]:
    """Wait for the dispatch's pipeline + lane job (fresh /go or a /retry)."""
    branch = f"factory/{arc['issue_iid']}/{arc['run_id'][:8]}"

    def pipeline() -> Any:
        pipelines = gitlab.get(f"/projects/{project_id}/pipelines", params={"ref": branch})
        candidates = [p for p in pipelines if p.get("source") == "api"]
        already = {
            entry["pipeline_id"]
            for entry in bundle.document["phases"].get(phase, {}).get("dispatches", [])
        }
        fresh = [p for p in candidates if p["id"] not in already]
        return fresh[0] if fresh else None

    dispatched = poll(
        pipeline, f"a NEW api-triggered pipeline on {branch}", timeout=600, interval=10
    )
    pipeline_id = dispatched["id"]
    job = poll(
        lambda: lane_job(gitlab, project_id, pipeline_id),
        "the lane job",
        timeout=300,
        interval=10,
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
    """Observe the driver phase's START in the job trace (the anchor).

    Polls the trace until GitLab echoes the collapsed driver-phase script
    block; returns the trace timestamp + the observed job status. This is
    the OBSERVATION the ladder anchors on — never a job-start sleep.
    """
    deadline = _ts() + timeout
    last_status: str | None = None
    while _ts() < deadline:
        try:
            job = gitlab.get(f"/projects/{project_id}/jobs/{job_id}")
            last_status = str(job.get("status"))
            if last_status in ("success", "failed", "canceled"):
                trace = job_trace(gitlab, project_id, job_id)
                stamp = driver_phase_started_at(trace)
                if stamp is not None:
                    return {"anchor": stamp, "job_status": last_status, "post_hoc": True}
                return {"anchor": None, "job_status": last_status, "post_hoc": True}
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


def wait_checkpoint(
    work_id: str, previous_id: str | None, timeout: float = 600.0
) -> dict[str, Any] | None:
    """Wait for a NEW checkpoint entry in the works index (the sample)."""
    return poll(
        lambda: _newer_checkpoint(work_id, previous_id),
        f"a new WIP checkpoint for {work_id[:8]}",
        timeout=timeout,
        interval=10,
    )


def sample_probe(
    bundle: Bundle,
    gitlab: GitLab,
    project_id: int,
    phase: str,
    arc: dict[str, Any],
    rung: ProbeRung,
    previous_checkpoint_id: str | None,
) -> dict[str, Any]:
    """ONE ladder rung: observe the anchor, pause at the rung, read the sample.

    The pause fires at ``rung.offset_s`` AFTER the observed driver-phase
    start — the offset is the ladder's ADAPTIVE choice (anchored on the
    R37-08 measured timeline, advanced only on an observed empty sample);
    the anchor and the sample are both OBSERVED. The job may go terminal
    before the rung (the turn outlived the ladder): recorded honestly as
    ``turn-completed`` — never a blind pause on a dead job.
    """
    dispatches = bundle.document["phases"][phase]["dispatches"]
    dispatch = dispatches[-1]
    job_id = int(dispatch["lane_job_id"])

    anchor_info = wait_driver_phase_start(gitlab, project_id, job_id)
    anchor = anchor_info["anchor"]
    entry: dict[str, Any] = {
        "rung": rung.label,
        "offset_s": rung.offset_s,
        "driver_phase_anchor": anchor,
        "anchor_observed_at": _now(),
        "job_status_at_anchor": anchor_info["job_status"],
    }
    if anchor is None:
        entry.update({"verdict": "no-anchor", "advance": False})
        bundle.append(phase, "probe_ladder", entry)
        return entry
    anchor_epoch = trace_anchor_epoch(anchor)
    while True:
        job = gitlab.get(f"/projects/{project_id}/jobs/{job_id}")
        status = str(job.get("status"))
        if status in ("success", "failed", "canceled"):
            entry.update(
                {
                    "verdict": "turn-completed",
                    "job_status_at_pause_window": status,
                    "advance": False,
                }
            )
            bundle.append(phase, "probe_ladder", entry)
            return entry
        if time.time() >= anchor_epoch + rung.offset_s:
            break
        time.sleep(5)

    paused_at = _now()
    note = gitlab.post(
        f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
        json={"body": f"@forge /pause {arc['run_id']}"},
    )
    if note.status_code not in (201, 200):
        raise Refused(f"/pause note failed: {note.text[:200]}")
    entry["pause_posted_at"] = paused_at

    checkpoint = wait_checkpoint(arc["run_id"], previous_checkpoint_id)
    entry["checkpoint"] = dict(checkpoint)
    bundle.append(phase, "probe_ladder", entry)
    print(
        f"{phase}: {rung.label} sample — checkpoint "
        f"{str(checkpoint.get('checkpoint_id'))[:12]} files={checkpoint.get('files')}"
    )
    return entry


def _lane_spend_so_far(bundle: Bundle, phase: str) -> float:
    """Sum the recorded SDK spend receipts for the phase's finished lanes."""
    receipts: list[Mapping[str, Any]] = []
    for dispatch in bundle.document["phases"].get(phase, {}).get("dispatches", []):
        usage = dispatch.get("usage_receipt")
        if isinstance(usage, Mapping):
            receipts.append(usage)
    return float(spend_from_receipts(receipts)["total_usd"])


def _capture_lane_outcome(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, dispatch: Mapping[str, Any]
) -> dict[str, Any]:
    """Wait the lane job terminal; capture status, trace markers, the SDK
    usage receipt (spend) and the collector's outcome line."""
    pipeline_id = int(dispatch["pipeline_id"])
    job_id = int(dispatch["lane_job_id"])
    job = poll(
        lambda: (lambda j: j and j.get("status") in ("success", "failed", "canceled") and j)(
            lane_job(gitlab, project_id, pipeline_id)
        ),
        f"lane job {job_id} completion",
        timeout=2400,
        interval=30,
    )
    trace = job_trace(gitlab, project_id, job_id)
    record_key_dispatch = next(
        d for d in bundle.document["phases"][phase]["dispatches"] if d.get("lane_job_id") == job_id
    )
    record_key_dispatch["job_status"] = job.get("status")
    record_key_dispatch["failure_reason"] = job.get("failure_reason")
    record_key_dispatch["trace_sha256"] = hashlib.sha256(trace.encode()).hexdigest()
    record_key_dispatch["trace_tail"] = trace[-2500:]
    record_key_dispatch["envelope_lines"] = grep_lines(
        trace, "forge dispatch envelope"
    ) or grep_lines(trace, "dispatch envelope")
    record_key_dispatch["collector_lines"] = grep_lines(trace, "FORGE_LANE_OUTCOME:")
    record_key_dispatch["resume_lines"] = grep_lines(trace, "resume")
    record_key_dispatch["restore_lines"] = grep_lines(trace, "restor")
    record_key_dispatch["checkpoint_lines"] = grep_lines(trace, "checkpoint")
    # the SDK usage receipt rides the candidate meta artifact
    try:
        meta_raw = gitlab.get_text(
            f"/projects/{project_id}/jobs/{job_id}/artifacts/.forge/candidate.meta.json"
        )
        meta = json.loads(meta_raw)
        record_key_dispatch["candidate_meta"] = {
            "exit": meta.get("exit"),
            "terminal_reason": meta.get("terminal_reason"),
            "model": meta.get("model"),
            "driver": meta.get("driver"),
            "usage": meta.get("usage"),
            "workspace_generation": meta.get("workspace_generation"),
        }
        record_key_dispatch["usage_receipt"] = meta.get("usage")
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        record_key_dispatch["candidate_meta_error"] = str(exc)[:200]
    bundle.save()
    print(
        f"{phase}: lane job {job_id} ended {job.get('status')} "
        f"(failure_reason={job.get('failure_reason')})"
    )
    return dict(job)


def phase_interrupt(bundle: Bundle, gitlab: GitLab) -> int:
    phase = "interrupt"
    record = bundle.phase(phase)
    if record.get("result") == "green":
        print("interrupt: already complete — resuming collect-only")
        return 0
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    record["paid"] = True
    bundle.save()

    if not record.get("plan"):
        arc = start_issue_and_plan(bundle, gitlab, project_id, phase, poll_timeout=900)
        note = gitlab.post(
            f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
            json={"body": f"@forge /go {arc['run_id']}"},
        )
        if note.status_code not in (201, 200):
            raise Refused(f"/go note failed: {note.text[:200]}")
        dispatch_pipeline(bundle, gitlab, project_id, phase, arc, "dispatch-1")
    else:
        arc = {
            "issue_iid": record["issue"]["iid"],
            "run_id": record["plan"]["run_id"],
        }
        print(f"interrupt: resuming run {arc['run_id'][:8]} from the recorded arc")

    # -- the observed-work probe ladder -------------------------------------
    rungs = probe_rungs()
    ladder = record.setdefault("probe_ladder", [])
    # RESUME rehydration: a recorded useful checkpoint stands (the ladder
    # never re-samples past success), and the last sampled checkpoint id
    # keeps the newer-than predicate monotonic across process restarts —
    # the LIVE-found flaw: without it, a resumed driver posted a SPURIOUS
    # second pause on the already-dispatched resume lane and ended it.
    useful_checkpoint: dict[str, Any] | None = record.get("useful_checkpoint")
    previous_checkpoint_id: str | None = None
    for entry in ladder:
        sampled = entry.get("checkpoint") or {}
        if sampled.get("checkpoint_id"):
            previous_checkpoint_id = str(sampled["checkpoint_id"])

    while useful_checkpoint is None:
        rung_index = len(ladder)
        if rung_index >= len(rungs):
            record["result"] = "empty-wip-exhausted"
            bundle.save()
            _record_failure(
                bundle,
                f"the probe ladder exhausted {len(rungs)} rungs without a files>0 checkpoint — "
                "the pause samples landed before edits in every cycle (the R37-08 failure "
                "mode reproduced adaptively); honest stop, no blind extra dispatches",
            )
            raise Refused("probe ladder exhausted without useful WIP — recorded honestly")
        rung = rungs[rung_index]
        spent = _lane_spend_so_far(bundle, phase)
        if spent > SPEND_LADDER_GUARD_USD:
            record["result"] = "spend-guard"
            bundle.save()
            _record_failure(
                bundle,
                f"the ladder's spend guard refused rung {rung.label}: recorded lane spend "
                f"${spent:.2f} crossed ${SPEND_LADDER_GUARD_USD} (cap ${SPEND_CAP_USD})",
            )
            raise Refused("probe ladder refused by the spend guard")
        entry = sample_probe(bundle, gitlab, project_id, phase, arc, rung, previous_checkpoint_id)
        ladder = record["probe_ladder"]
        verdict = entry.get("verdict")
        if verdict in ("no-anchor", "turn-completed"):
            if verdict == "turn-completed":
                _capture_lane_outcome(
                    bundle,
                    gitlab,
                    project_id,
                    phase,
                    bundle.document["phases"][phase]["dispatches"][-1],
                )
            record["result"] = verdict
            bundle.save()
            _record_failure(
                bundle,
                f"rung {rung.label} could not sample: {verdict} "
                f"(anchor={entry.get('driver_phase_anchor')}, "
                f"status={entry.get('job_status_at_pause_window') or entry.get('job_status_at_anchor')})",
            )
            raise Refused(f"probe rung ended with {verdict} — recorded honestly")
        checkpoint = entry.get("checkpoint") or {}
        previous_checkpoint_id = checkpoint.get("checkpoint_id") or previous_checkpoint_id
        evaluation = evaluate_probe(rung, checkpoint)
        ladder[-1]["verdict"] = evaluation["verdict"]  # the sample's own verdict, stored
        bundle.save()
        if evaluation["verdict"] == "useful":
            useful_checkpoint = checkpoint
            break
        # empty sample: wait the lane job out, then re-enter natively (/retry)
        dispatch = bundle.document["phases"][phase]["dispatches"][-1]
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
        print(f"interrupt: run blocked ({blocked.get('status_reason')}) — re-entering via /retry")
        retried = gitlab.post(
            f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
            json={"body": "@forge /retry"},
        )
        if retried.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retried.text[:200]}")
        dispatch_pipeline(bundle, gitlab, project_id, phase, arc, f"dispatch-{len(ladder) + 1}")

    # -- the useful checkpoint's full evidence -------------------------------
    checkpoint_id = str(useful_checkpoint.get("checkpoint_id") or useful_checkpoint.get("id") or "")
    manifest = read_manifest(checkpoint_store_root(), checkpoint_id)
    evidence = manifest_evidence(manifest)
    useful_record = {
        "id": checkpoint_id,
        "digest": checkpoint_id,
        "files": int(useful_checkpoint.get("files") or 0),
        "sequence": useful_checkpoint.get("sequence"),
        "uploaded_at": useful_checkpoint.get("uploaded_at"),
        "source_oids": manifest.get("source_oids"),
        "manifest_evidence": evidence,
        "shape_coverage": evidence["shape_coverage"],
    }
    bundle.record(phase, "useful_checkpoint", useful_record)
    print(
        f"interrupt: USEFUL checkpoint {checkpoint_id[:12]} files={evidence['file_count']} "
        f"deletions={evidence['deletions']} coverage={evidence['shape_coverage']}"
    )

    # -- job-level cancel (never container-level) -----------------------------
    if not record.get("job_cancel"):
        dispatch = bundle.document["phases"][phase]["dispatches"][-1]
        job_id = int(dispatch["lane_job_id"])
        job = gitlab.get(f"/projects/{project_id}/jobs/{job_id}")
        cancelled = gitlab.post(f"/projects/{project_id}/jobs/{job_id}/cancel")
        body: dict[str, Any] = {}
        try:
            body = cancelled.json()
        except ValueError:
            body = {}
        bundle.record(
            phase,
            "job_cancel",
            {
                "job_id": job_id,
                "job_status_before": job.get("status"),
                "attempted_at": _now(),
                "http_status": cancelled.status_code,
                "status_after": body.get("status") or body.get("message"),
                "moot": cancelled.status_code not in (200, 201)
                or str(body.get("status")) in ("failed", "canceled", "success"),
            },
        )
        print(
            f"interrupt: CI job {job_id} cancel → HTTP {cancelled.status_code} "
            f"({body.get('status') or body.get('message')})"
        )

    # -- the honest blocked classification ------------------------------------
    if not record.get("blocked_classification"):
        blocked = poll(
            lambda: (lambda r: r and r.get("status") == "blocked" and r)(
                find_run_for_issue(gitlab, project_id, arc["issue_iid"])
            ),
            "the run's blocked classification",
            timeout=1200,
            interval=20,
        )
        bundle.record(phase, "blocked_classification", blocked)
        print(f"interrupt: run blocked ({blocked.get('status_reason')})")

    # -- the REAL resume: /retry → the second lane restores the exact WIP -----
    # BOUNDED legs: a resumed lane that ended failed WITHOUT a restore
    # refusal is an honest iteration point (the run is blocked again; the
    # same authorized checkpoint re-binds) — the operator may /retry once
    # more within the spend guard. A restore refusal never retries (the
    # WIP itself would be silently discarded).
    max_resume_legs = 2
    legs_used = len(record.get("resume_legs", []))
    while True:
        if not record.get("resume_dispatch"):
            retried = gitlab.post(
                f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
                json={"body": "@forge /retry"},
            )
            if retried.status_code not in (201, 200):
                raise Refused(f"/retry note failed: {retried.text[:200]}")
            dispatch = dispatch_pipeline(
                bundle, gitlab, project_id, phase, arc, f"resume-dispatch-{legs_used + 1}"
            )
            dispatch["lane_job_id"] = dispatch["job_id"]  # the stored spelling _capture reads
            bundle.record(phase, "resume_dispatch", dispatch)
        resume_dispatch = dict(record["resume_dispatch"])
        resume_dispatch.setdefault("lane_job_id", resume_dispatch.get("job_id"))
        resume_job = _capture_lane_outcome(bundle, gitlab, project_id, phase, dict(resume_dispatch))
        resume_meta = (
            next(
                (
                    d.get("candidate_meta")
                    for d in bundle.document["phases"][phase]["dispatches"]
                    if d.get("lane_job_id") == resume_dispatch["lane_job_id"]
                ),
                {},
            )
            or {}
        )
        if resume_job.get("status") == "success":
            break
        terminal = resume_meta.get("terminal_reason")
        if terminal == "wip_restore_failed":
            reason = (
                f"the resumed lane refused to start: the required WIP restore failed "
                f"({resume_meta.get('exit')}) — the model never ran (zero turns); "
                "never retried (the WIP must not be silently discarded)"
            )
            _record_failure(bundle, reason)
            capture_run_evidence(bundle, phase, arc["run_id"])
            record["result"] = "restore-failed"
            bundle.save()
            raise Refused(reason)
        legs_used += 1
        bundle.append(
            phase,
            "resume_legs",
            {
                "leg": legs_used,
                "lane_job_id": resume_dispatch["lane_job_id"],
                "job_status": resume_job.get("status"),
                "terminal_reason": terminal,
                "at": _now(),
            },
        )
        _record_failure(
            bundle,
            f"resumed lane leg {legs_used} (job {resume_dispatch['lane_job_id']}) ended "
            f"{resume_job.get('status')} (terminal_reason={terminal}) — iteration point, "
            "trace + artifacts captured",
        )
        if legs_used >= max_resume_legs:
            reason = (
                f"the resumed lane legs are exhausted ({max_resume_legs}) without a green "
                "continuation — honest stop"
            )
            _record_failure(bundle, reason)
            capture_run_evidence(bundle, phase, arc["run_id"])
            record["result"] = "resumed-turn-failed"
            bundle.save()
            raise Refused(reason)
        if _lane_spend_so_far(bundle, phase) > SPEND_LADDER_GUARD_USD:
            reason = "the resume legs refused by the spend guard"
            _record_failure(bundle, reason)
            record["result"] = "spend-guard"
            bundle.save()
            raise Refused(reason)
        record["resume_dispatch"] = None  # the next loop iteration /retries
        bundle.save()

    # the restore identity: the worker journal's dispatch decision + envelope
    decision = psql(
        f"SELECT row_to_json(t)::text FROM (SELECT * FROM flow_runs WHERE id = '{arc['run_id']}') t"
    )
    try:
        run_row = json.loads(decision.strip()) if decision.strip() else {}
    except json.JSONDecodeError:
        run_row = {"raw": decision[-1500:]}
    resume_record = {
        "pipeline_id": resume_dispatch["pipeline_id"],
        "lane_job_id": resume_dispatch["lane_job_id"],
        "restored_checkpoint_expected": checkpoint_id,
        "collector_lines": bundle.document["phases"][phase]["dispatches"][-1].get(
            "collector_lines"
        ),
        "restore_lines": bundle.document["phases"][phase]["dispatches"][-1].get("restore_lines"),
        "workspace_generation": (
            bundle.document["phases"][phase]["dispatches"][-1]
            .get("candidate_meta", {})
            .get("workspace_generation")
        ),
        "decision_id": (
            (run_row.get("evidence") or {}).get("continuation", {}).get("decision_id")
            if isinstance(run_row.get("evidence"), Mapping)
            else None
        ),
        "envelope_digest": (
            (run_row.get("evidence") or {}).get("gitlab", {}).get("dispatch_envelope_digest")
            if isinstance(run_row.get("evidence"), Mapping)
            else None
        ),
    }
    bundle.record(phase, "resume", resume_record)

    # -- the Draft MR + the independent oracle on the exact candidate ---------
    # A GREEN resumed job with a ZERO-CHANGE candidate is the distinct
    # model-no-op outcome (the R37-08 resumed-turn failure mode): the run
    # classifies blocked at the run level and NO MR appears — recorded as
    # its own outcome, never waited into a timeout.
    def mr_or_noop() -> Any:
        mrs = gitlab.get(
            f"/projects/{project_id}/merge_requests",
            params={"state": "opened", "source_branch": resume_dispatch["branch"]},
        )
        if mrs:
            return {"mr": mrs[0]}
        run = find_run_for_issue(gitlab, project_id, arc["issue_iid"])
        if run and run.get("status") in ("blocked", "failed"):
            return {"no_op": run}
        return None

    verdict = poll(
        mr_or_noop, "the Draft MR (or the honest no-op classification)", timeout=1500, interval=20
    )
    if "no_op" in verdict:
        reason = (
            f"the resumed turn COMPLETED GREEN but delivered a zero-change candidate — "
            f"the run classified {verdict['no_op'].get('status')} "
            f"({verdict['no_op'].get('status_reason')}); a no-op turn never became a "
            "false delivery (no MR exists)"
        )
        _record_failure(bundle, reason)
        capture_run_evidence(bundle, phase, arc["run_id"])
        record["result"] = "model-no-op"
        bundle.save()
        raise Refused(reason)

    mr = wait_mr_and_verify(bundle, gitlab, project_id, phase, resume_dispatch["branch"])
    assert mr["draft"] and not mr["merged"]  # the invariants the record re-checks
    capture_run_evidence(bundle, phase, arc["run_id"])
    record["result"] = "green"
    record["finished_at"] = _now()
    bundle.save()
    print("interrupt: GREEN — useful WIP restored cross-runner, Draft MR left for human review")
    return 0


def _record_failure(bundle: Bundle, reason: str) -> None:
    bundle.document.setdefault("failures", []).append({"at": _now(), "reason": reason})
    bundle.save()


def wait_mr_and_verify(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, branch: str
) -> dict[str, Any]:
    def mr() -> Any:
        mrs = gitlab.get(
            f"/projects/{project_id}/merge_requests",
            params={"state": "opened", "source_branch": branch},
        )
        return mrs[0] if mrs else None

    merge_request = poll(mr, f"the Draft MR on {branch}", timeout=1500, interval=20)
    mr_iid = merge_request["iid"]

    def verified() -> Any:
        current = gitlab.get(f"/projects/{project_id}/merge_requests/{mr_iid}")
        sha = current.get("sha") or ""
        if not sha:
            return None
        pipelines = gitlab.get(f"/projects/{project_id}/pipelines", params={"sha": sha})
        for entry in pipelines:
            if entry.get("status") == "success":
                return {"sha": sha, "pipeline_id": entry["id"]}
        return None

    green = poll(verified, "the current candidate's green pipeline", timeout=1800, interval=20)

    diffs = gitlab.get(f"/projects/{project_id}/merge_requests/{mr_iid}/diffs")
    touched = [str(d.get("new_path")) for d in diffs]
    deleted = [str(d.get("old_path")) for d in diffs if d.get("deleted_file")]
    forbidden = [
        path
        for path in touched
        if path in (".gitlab-ci.yml", "README.md") or path.startswith("tests/")
    ]
    shapes_present: dict[str, str] = {}
    for path, shape in task_shapes():
        if shape == "deleted":
            shapes_present[path] = "deleted" if path in deleted else "missing"
        else:
            shapes_present[path] = "present" if path in touched else "missing"
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
        "verification_pipeline_id": green["pipeline_id"],
        "changed_paths": touched,
        "deleted_paths": deleted,
        "shapes_present": shapes_present,
        "oracle_tampering": forbidden,
        "diff_digest": hashlib.sha256(diff_text.encode()).hexdigest(),
        "pipeline_status": current.get("pipeline_status"),
    }
    if forbidden:
        _record_failure(bundle, f"the candidate touched the oracle files: {forbidden}")
        raise Refused(f"the candidate touched the oracle files: {forbidden}")
    bundle.record(phase, "mr", result)
    print(
        f"{phase}: Draft MR !{mr_iid} green on candidate {green['sha'][:12]} "
        f"(shapes={shapes_present})"
    )
    return result


def capture_run_evidence(bundle: Bundle, phase: str, run_id: str) -> dict[str, Any]:
    detail = app_get(f"/runs/{run_id}")
    evidence_json = psql(f"SELECT evidence::text FROM flow_runs WHERE id = '{run_id}'")
    try:
        full_evidence = json.loads(evidence_json.strip()) if evidence_json.strip() else {}
    except json.JSONDecodeError:
        full_evidence = {"raw": evidence_json[-2000:]}
    usage_rows = psql(
        "SELECT row_to_json(t)::text FROM (SELECT * FROM usage_receipts WHERE run_id = "
        f"'{run_id}' ORDER BY id) t"
    )
    usage = [json.loads(line) for line in usage_rows.splitlines() if line.strip()]
    budget_rows = psql(
        f"SELECT row_to_json(t)::text FROM (SELECT * FROM run_budgets WHERE run_id = '{run_id}') t"
    )
    budgets = [json.loads(line) for line in budget_rows.splitlines() if line.strip()]
    checkpoints = [
        json.loads(line)
        for line in psql(
            "SELECT row_to_json(t)::text FROM (SELECT * FROM checkpoint_metadata WHERE "
            f"work_id = '{run_id}' ORDER BY sequence) t"
        ).splitlines()
        if line.strip()
    ]
    captured = {
        "run": {
            "id": detail.get("id"),
            "status": detail.get("status"),
            "status_reason": detail.get("status_reason"),
            "commit_cycle": detail.get("commit_cycle"),
            "steps": detail.get("steps"),
            "evidence_summary": detail.get("evidence"),
        },
        "evidence_full": full_evidence,
        "usage_receipts": usage,
        "run_budgets": budgets,
        "checkpoint_metadata": checkpoints,
    }
    bundle.record(f"{phase}-state", run_id[:8], captured)
    print(
        f"{phase}: run {run_id[:8]} status={detail.get('status')} ({detail.get('status_reason')})"
    )
    return captured


# ---------------------------------------------------------------------------
# collect + teardown
# ---------------------------------------------------------------------------


def phase_collect(bundle: Bundle, gitlab: GitLab) -> int:
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    record = bundle.phase("collect")
    interrupt = bundle.document["phases"].get("interrupt", {})
    run_id = interrupt.get("plan", {}).get("run_id")
    if run_id:
        capture_run_evidence(bundle, "interrupt", run_id)
        record["worker_envelope_lines"] = worker_envelope_lines(run_id)
    template = gitlab.get(
        f"/projects/{project_id}/repository/files/.gitlab-ci.yml", params={"ref": "main"}
    )
    record["installed_template"] = {
        "content_sha256": template.get("content_sha256"),
        "last_commit_id": template.get("last_commit_id"),
        "expected_sha256_of_generated": hashlib.sha256(ci_yaml().encode()).hexdigest(),
    }
    version = gitlab.get("/version")
    record["gitlab_version"] = {
        "version": version.get("version"),
        "revision": version.get("revision"),
        "enterprise": version.get("enterprise"),
    }

    # the superseded negative evidence stays UNTOUCHED — read it back
    empty_index = REPO_ROOT / SUPERSEDED_EMPTY_WIP["works_index"]
    if empty_index.is_file():
        preserved = json.loads(empty_index.read_text(encoding="utf-8"))
        record["superseded_negative_evidence_preserved"] = {
            "path": str(empty_index),
            "entries": len(preserved.get("checkpoints") or []),
            "files_still_zero": all(
                int(e.get("files") or 0) == 0 for e in preserved.get("checkpoints") or []
            ),
        }
    record["finished_at"] = _now()
    bundle.save()

    write_record(build_record(bundle.document))
    print(f"collect: evidence record written → {RECORD_PATH}")
    return 0


_ENVELOPE_LINE_RE = re.compile(
    r"gitlab\.dispatch_envelope_digest: run (?P<run>[0-9a-f]{8}) attempt (?P<attempt>\d+) "
    r"dispatched a (?P<kind>fresh|required) resume \(checkpoint (?P<checkpoint>\S+?), "
    # the journal escapes the em dash as \u2014 inside its JSON message
    r"decision (?P<decision>\S+?)\)(?: \\u2014| —) envelope (?P<envelope>[0-9a-f]+)"
)


def worker_envelope_lines(run_id: str) -> list[dict[str, str]]:
    """The worker journal's dispatch-envelope lines for ONE run (read-only).

    The envelope digest is computed at dispatch (never stored) and journaled
    by the worker — the only durable place the drill can cite it from.
    """
    completed = subprocess.run(
        ["podman", "logs", "forge-worker", "--since", "2026-09-24T00:00:00"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines: list[dict[str, str]] = []
    # LIVE-found: the worker's structured journal writes to STDERR (the
    # container's logging config) — scan both streams, read-only.
    for line in ((completed.stdout or "") + "\n" + (completed.stderr or "")).splitlines():
        if "dispatch_envelope_digest" not in line or f"run {run_id[:8]}" not in line:
            continue
        match = _ENVELOPE_LINE_RE.search(line)
        if match:
            lines.append(dict(match.groupdict()))
    return lines


def build_record(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Fold the evidence bundle into the qualification record (offline-testable)."""
    phases = bundle.get("phases", {})
    interrupt = phases.get("interrupt", {})
    setup = phases.get("setup", {})
    collect = phases.get("collect", {})
    spend_jobs: dict[str, Any] = {}
    # every DRILL-PAID lane leg carries its spend: the interrupted arm's
    # dispatches AND the (honestly recorded) uninterrupted attempt 1 leg
    for phase in ("interrupt", "uninterrupted-attempt-1"):
        for dispatch in phases.get(phase, {}).get("dispatches", []):
            receipt = dispatch.get("usage_receipt")
            if isinstance(receipt, Mapping):
                spend_jobs[str(dispatch.get("lane_job_id"))] = spend_from_receipts([receipt])
    if spend_jobs:
        total = round(sum(float(job["total_usd"]) for job in spend_jobs.values()), 4)
        basis = "+".join(sorted({str(job["cost_basis"]) for job in spend_jobs.values()}))
        total_spend = {"total_usd": total, "cost_basis": basis}
    else:
        total_spend = {"total_usd": 0.0, "cost_basis": "no-receipts-yet"}

    # the resume identities: the LAST required-resume envelope line names the
    # exact dispatched checkpoint + decision + digest of the final leg
    resume = dict(interrupt.get("resume") or {})
    envelopes = collect.get("worker_envelope_lines") or []
    required = [e for e in envelopes if e.get("kind") == "required"]
    if required and not resume.get("envelope_digest"):
        last = required[-1]
        resume["envelope_digest"] = last.get("envelope")
        resume["envelope_attempt"] = last.get("attempt")
        resume["dispatched_checkpoint"] = last.get("checkpoint")
        resume["decision_id"] = resume.get("decision_id") or last.get("decision")

    # the MR record: normalize the identity spellings the schema demands
    mr = dict(interrupt.get("mr") or {})
    if mr:
        mr.setdefault("url", mr.get("mr_url"))

    # the candidate shapes: translate the diff verdicts into the task's own
    # shape words ("present" on a new/modified shape == that shape delivered)
    mr_shapes = mr.get("shapes_present") or {}
    shapes: dict[str, str] = {}
    for path, shape in task_shapes():
        verdict = mr_shapes.get(path)
        if shape == "deleted":
            shapes[path] = verdict or "missing"
        else:
            shapes[path] = shape if verdict == "present" else (verdict or "missing")

    outcome = "incomplete"
    if interrupt.get("result") == "green" and interrupt.get("mr"):
        outcome = "useful-wip-continued"
    elif interrupt.get("result") == "empty-wip-exhausted":
        outcome = "empty-wip-exhausted"
    elif interrupt.get("result") == "restore-failed":
        outcome = "restore-failed"
    elif interrupt.get("result") in ("model-no-op", "resumed-turn-failed"):
        outcome = interrupt["result"]
    elif interrupt.get("result") in ("no-anchor", "turn-completed", "spend-guard"):
        outcome = "refused"

    record = new_record(
        str(setup.get("project", {}).get("path") or ""), str(bundle.get("created_at") or "")
    )
    record.update(
        {
            "finished_at": interrupt.get("finished_at"),
            "project": setup.get("project", {}),
            "task": {
                "shapes": [list(shape) for shape in task_shapes()],
                "oracle": "smoke CI job: six exact slugify cases + app rewired + legacy deleted",
                "committed_before_run_sha": setup.get("seed_commit_sha"),
                "lane_ref": LANE_REF_SHA,
                "template_sha256": setup.get("template_sha256"),
            },
            "issue": interrupt.get("issue"),
            "plan": interrupt.get("plan"),
            "probe_ladder": interrupt.get("probe_ladder", []),
            "useful_checkpoint": interrupt.get("useful_checkpoint"),
            "job_cancel": interrupt.get("job_cancel"),
            "blocked_classification": (
                {
                    "status": interrupt.get("blocked_classification", {}).get("status"),
                    "status_reason": interrupt.get("blocked_classification", {}).get(
                        "status_reason"
                    ),
                }
                if interrupt.get("blocked_classification")
                else None
            ),
            "resume": resume or None,
            "mr": mr or None,
            "candidate": (
                {
                    "diff_digest": mr.get("diff_digest"),
                    "shapes_present": shapes,
                    "changed_paths": mr.get("changed_paths"),
                    "deleted_paths": mr.get("deleted_paths"),
                }
                if mr
                else None
            ),
            "oracle": (
                {
                    "pipeline_id": interrupt.get("mr", {}).get("verification_pipeline_id"),
                    "status": "success",
                    "candidate_sha": interrupt.get("mr", {}).get("candidate_sha"),
                }
                if interrupt.get("mr")
                else None
            ),
            "spend": {
                "cap_usd": SPEND_CAP_USD,
                "jobs": spend_jobs,
                "total_usd": total_spend["total_usd"],
                "cost_basis": total_spend["cost_basis"],
            },
            "failures": list(bundle.get("failures", [])),
            "outcome": outcome,
        }
    )
    return record


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
        prog="python scripts/run_useful_wip_resume.py",
        description=(
            "R38-05 (#306): the live useful-WIP cross-runner continuation — observed-work "
            "pause, exact checkpoint restore on a second runner, the shipped #302 "
            "finalization, an independent oracle, a Draft MR. Phases are resumable; "
            "the evidence bundle on disk is the state."
        ),
    )
    parser.add_argument("phase", choices=["setup", "preflight", "interrupt", "collect", "teardown"])
    parser.add_argument("--evidence", type=Path, default=EVIDENCE_PATH)
    parser.add_argument(
        "--project-name", default=f"forge-wip-{datetime.now(timezone.utc):%Y-%m-%d}"
    )
    args = parser.parse_args(argv)

    settings = Settings()  # type: ignore[call-arg]
    gitlab = GitLab(settings)
    bundle = Bundle(args.evidence)
    handlers: dict[str, Callable[..., int]] = {
        "setup": lambda: phase_setup(bundle, gitlab, args.project_name, settings),
        "preflight": lambda: phase_preflight(bundle, gitlab),
        "interrupt": lambda: phase_interrupt(bundle, gitlab),
        "collect": lambda: phase_collect(bundle, gitlab),
        "teardown": lambda: phase_teardown(bundle, gitlab),
    }
    try:
        return handlers[args.phase]()
    except Refused as exc:
        bundle.record(args.phase, "refused", {"reason": str(exc), "at": _now()})
        _record_failure(bundle, f"{args.phase} refused: {exc}")
        print(f"{args.phase}: REFUSED — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
