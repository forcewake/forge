"""R42-04 (#377) — the COMPLETE review-and-correction loop, BUILD-ONCE.

The v0.41.0 window closed the loop but on a PRE-RELEASE composition: the
live trace ran wheel ``223d0f25``/image ``f6ff6308`` while the release
promoted wheel ``00919993`` built from the later tree — honestly
documented, but a passed HISTORICAL trace cannot serve as an exact
release qualification. This run is the process fix (issue #377): ONE
candidate (the current tree at v0.42.0 — wheel + alignment image built
ONCE, digests recorded BEFORE qualification), the SAME driver, and the
record binds THOSE bytes. The M0 fixes (#374 retryable observations,
#375 conservative occupancy, #376 the credential-mode doctor preflight)
ride the candidate; the doctor passes WITHOUT the ambient duplicate the
previous window needed, and the #374 retryable seam exercises naturally
if any transient observation occurs.

The trace (issue #377's acceptance, on the EXACT candidate bytes):

1. ``probe`` — BOTH credential routes probed at the real model gateway
   (one 8-token call each): the protected-variable key ALIVE (200), the
   broker token DEAD (401, typed). The loop runs under (b).
2. ``align`` — the lab alignment BUILD of the candidate image
   (``align_lab.py --apply``, receipts every step) with the window's env
   pins; the wheelhost (:8481) serves the pinned candidate wheel.
3. ``setup`` — the disposable project: the shipped SDK lane template
   VERBATIM (committed, included BY local include) + the ONE install
   seam override (the sha256-verified candidate wheel ladder) + the
   precommitted INDEPENDENT oracle + the CI carrier variable
   ``FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN`` (masked; NO ambient
   duplicate — #376's doctor sees the carrier) + the credential binding
   through the shipped registry's ``bind()``.
4. ``delivery`` — issue → ``/implement`` → ``/go`` → the real-model SDK
   lane on the candidate wheel → the candidate → the Draft MR → the
   green oracle → the closing review within reserve → ``ready_for_human``.
5. ``round2`` — a NONCONFLICTING human edit lands on the MR branch (the
   round-2 contract test file — red until corrected), then the NATIVE
   ``/fix`` note on the MR → the budgeted CHILD round from the exact
   current head → the new candidate on the SAME MR → the oracle green on
   the exact new candidate → the closing reviewer's obligation digest
   recomputed and compared → ``ready_for_human``.
6. ``replay`` — the SAME note redelivered (a fresh delivery uuid, the
   same note id — exactly GitLab's retry shape): NO new request, NO new
   child round, NO new pipeline, NO new commit.
7. ``round3`` — a DISTINCT second correction; at its publication the
   WORKER is killed (``podman stop -t 0``) post-commit,
   pre-bookkeeping, then restarted: the #358 recovery adopts the round's
   OWN effect — exactly ONE provider commit — and completes.
8. ``negative`` — a third /fix (round 4) admitted and dispatched; a
   CONFLICTING human commit lands mid-lane: the typed conflict
   (``branch_drift``) parks the child with ZERO candidate commits, the
   human commit stays the head, nothing is reverted.
9. ``resume_negative`` — the required-resume negative arm (R42-04's
   acceptance): a checkpoint uploaded for the delivery work through the
   app's OWN channel API, its stored blob CORRUPTED (the integrity
   premise — durable bytes rot), then a lane job dispatched with
   ``FORGE_LANE_RESUME=1`` on the candidate wheel: the strict restore
   gate fails the digest verification and halts ``wip_restore_failed``
   BEFORE any credential redemption or vendor session — zero model
   turns, the candidate's own pairing marker in the halted trace.
10. ``collect`` — the record ``qualification/records/review-loop-2026-09-28.json``
    (schema ``forge.profile.qualification/1``) + the evidence bundle
    under ``docs/evaluation/2026-09-28-r4204-build-once/`` (receipts
    WITHOUT values — digests only). ``teardown`` deletes the disposable
    project after capture.

The corrected publication path + the concurrent-MR trace already execute
on the candidate through the #378 composed suite
(``tests/production_entry/test_composed_faults.py`` — cited, not rerun
here; the full-suite gate re-executes them with every run).

Spend bound: $2.50 total. Honest stops everywhere: a refusal is
recorded, never retried into a green. The bot never merges, never
resolves discussions, never deploys — the reviewer's resolve is a HUMAN
action the driver performs as the reviewer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-28-r4204-build-once"
EVIDENCE_PATH = EVAL_DIR / "live-run-evidence.json"
ALIGNMENT_RECEIPTS = EVAL_DIR / "alignment-receipts.json"
#: The BUILD receipts (align_lab --apply executed by the operator BEFORE
#: the driver ran — preserved verbatim; the driver's verify-only re-run
#: writes its own receipt beside them, never over the build record).
ALIGNMENT_BUILD_RECEIPTS = EVAL_DIR / "alignment-build-receipts.json"
NOTES_DIR = EVAL_DIR / "notes"
TRACES_DIR = EVAL_DIR / "traces"
#: The qualification record's home (the typed store: the stamp is
#: ``forge.profile.qualification/1`` exactly as ``load_profile_records``
#: demands — the same store the @0.41.0 record lives in).
RECORD_PATH = REPO_ROOT / "qualification" / "records" / "review-loop-2026-09-28.json"
#: The BUILD-ONCE candidate manifest — the digests recorded BEFORE any
#: qualification phase ran (R42-04/#377); the record must bind these
#: exact digests and the collect phase asserts it.
CANDIDATE_MANIFEST = EVAL_DIR / "candidate-manifest.json"
APP_API = "http://localhost:8420"
STATE_PATH = REPO_ROOT / "data" / "review-loop-qualification" / "state.json"

#: The shipped lane template — committed VERBATIM into the disposable
#: project and included BY local include (the #365 pairing pattern).
TEMPLATE_SOURCE = REPO_ROOT / "ci" / "templates" / "claude-sdk-lane.gitlab-ci.yml"

#: The BUILD-ONCE candidate's lane wheel (R42-04/#377): ONE `uv build` of
#: the v0.42.0 tree (0f0dc8c + the version bump), digest recorded in the
#: candidate manifest BEFORE qualification; the lane installs EXACTLY
#: these bytes and the release must promote them.
LANE_WHEEL_NAME = "forge-0.42.0-py3-none-any.whl"
LANE_WHEEL_PATH = REPO_ROOT / "dist" / LANE_WHEEL_NAME
LANE_WHEEL_SHA256 = "e7b48d42e4851e9c142657f7f9597106f80e7da43de66e0ea39d5d6477462d33"
#: The BUILD-ONCE candidate's alignment image (the same tree; the
#: alignment receipts bind the build). Read back at collect time from
#: the observed containers — never hand-copied.
CANDIDATE_IMAGE_DIGEST = "sha256:98ea903125d1c4c6d05582682f1d5a0ed8d15fc6bdb00ee37747a854f5b58bed"
LANE_WHEEL_DIR = REPO_ROOT / "data" / "lane-wheel"
WHEEL_HOST_PORT = 8481
LAB_HOST_LAN_IP = "192.168.1.18"
WHEEL_URL = f"http://{LAB_HOST_LAN_IP}:{WHEEL_HOST_PORT}/{LANE_WHEEL_NAME}"

#: The credential route constants (the shipped broker's vocabulary).
ENV_SLOT = "ANTHROPIC_AUTH_TOKEN"
PROVIDER_ROUTE = "anthropic-gateway"
BROKER_REF = "env:ANTHROPIC_AUTH_TOKEN"
#: The native carrier's NAME (the shipped template's resolution step
#: reads it): FORGE_MODEL_<SEGMENT> of the bound ref.
MODEL_VARIABLE_NAME = "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN"

#: The alignment pins this trace needs on BOTH consumers (all non-secret).
#: The BUDGET PROFILES pin is a receipted OPERATOR POLICY adjustment this
#: window (LIVE-FOUND on attempt 2, run fbe62ad5): the claude-code harness
#: fills a ~200k context regardless of task size (attempt 2's executor
#: consumed 196,312/200,000 tokens — input 33,061 + cached 158,784 +
#: output 2,944 — and the finite budget EXHAUSTED at the token axis, the
#: closing review standing down with zero reviewer spend, exactly the
#: designed fence). The planner's output schema carries no budget_class
#: (the compiler default ``standard`` always applies), so the deployment's
#: standard profile moves to 600k tokens — STILL a finite, numerical,
#: enforced ceiling; the class never becomes unbounded.
BUDGET_PROFILES_600K = (
    '{"trivial":{"max_calls":8,"max_tokens":40000,"wallclock_s":900},'
    '"standard":{"max_calls":40,"max_tokens":600000,"wallclock_s":3600},'
    '"heavy":{"max_calls":120,"max_tokens":600000,"wallclock_s":10800}}'
)
ALIGNMENT_PINS: tuple[tuple[str, str], ...] = (
    ("FORGE_CREDENTIAL_DELIVERY", "gitlab-protected-variable"),
    ("FORGE_REVIEW_FEEDBACK_ENABLED", "1"),
    ("FORGE_MAX_REVIEW_ROUNDS", "3"),
    ("FORGE_BUDGET_PROFILES", BUDGET_PROFILES_600K),
)

#: The spend bound (issue #377: <= $2.50 total).
SPEND_CAP_USD = 2.5

#: The precommitted independent oracle — the slugify six-case pattern
#: (the #306 acceptance task, reused verbatim) PLUS the three file
#: shapes, as a pytest suite committed BEFORE any run.
SLUGIFY_CASES: tuple[tuple[str, str], ...] = (
    ("Hello, World!", "hello-world"),
    ("Forge WIP--resume __2026", "forge-wip-resume-2026"),
    ("   spaces   everywhere   ", "spaces-everywhere"),
    ("already-slugged", "already-slugged"),
    ("MIXED Case 123", "mixed-case-123"),
    ("!!!leading and trailing!!!", "leading-and-trailing"),
)

#: The round-2 correction's behavioral contract (the human edit — the
#: TDD shape: red on the pre-correction head, green on the candidate).
PARTS_CASES: tuple[tuple[str, list[str]], ...] = (
    ("Hello, World!", ["hello", "world"]),
    ("Forge WIP--resume __2026", ["forge", "wip", "resume", "2026"]),
    ("   spaces   everywhere   ", ["spaces", "everywhere"]),
    ("already-slugged", ["already", "slugged"]),
    ("MIXED Case 123", ["mixed", "case", "123"]),
    ("!!!leading and trailing!!!", ["leading", "and", "trailing"]),
)

#: The correction notes (the reviewer's exact requests). Each names its
#: path backticked — the ONLY explicitly claimed path — and that path is
#: inside the frozen spec's allowed scope (the three-shape task's bounds).
FIX_NOTE_ROUND2 = (
    "/fix The reviewer added tests/test_round2_contract.py on the current head "
    "(it is red until the new API exists). Implement slugify_parts(text: str) -> "
    "list[str] in src/utils/text.py — the non-empty segments of the slug split "
    "(same lowercase and non-alphanumeric-run splitting as slugify, no empty "
    "segments, edges never produce empties). Keep slugify itself and its current "
    "behavior unchanged, and touch nothing else so the full tests/ suite passes "
    "on the new candidate. `src/utils/text.py`"
)
FIX_NOTE_ROUND3 = (
    "/fix Documentation correction: the module docstring of src/utils/text.py must "
    "state the invariant explicitly — add a line containing exactly: "
    "Invariant: runs collapse to one dash and edges are stripped. Keep all "
    "behavior unchanged (the tests/ suite must stay green as-is). `src/utils/text.py`"
)
FIX_NOTE_ROUND4 = (
    "/fix Internal tidy-up: move the split pattern to a single module-level "
    "compiled regex in src/utils/text.py and use it from both functions; behavior "
    "unchanged (the tests/ suite must stay green as-is). `src/utils/text.py`"
)

#: The round-3 docstring marker the driver asserts on the new candidate.
ROUND3_MARKER = "Invariant: runs collapse to one dash and edges are stripped"

RUN_ID_RE = re.compile(r"\b([0-9a-f]{32})\b")
PAIRING_MARKER_RE = re.compile(
    r"forge lane package version=(?P<version>\S+) wheel=(?P<wheel>\S+) sha256=(?P<sha>[0-9a-f]{64})"
)
BOT_USERNAME = "forge"
APPROVER_USERNAME = "forcewake"

MAX_POLL_SECONDS_DEFAULT = 1500
POLL_INTERVAL_S = 10.0

FALLBACK_PRICE_PER_MTOK = {"input": 0.60, "cached_input": 0.07, "output": 2.20}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> float:
    return time.monotonic()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Refused(Exception):
    """A precondition failed or a bounded wait expired — recorded, never retried."""


def poll(
    probe: Callable[[], Any],
    what: str,
    *,
    timeout: float = MAX_POLL_SECONDS_DEFAULT,
    interval: float = POLL_INTERVAL_S,
) -> Any:
    deadline = _ts() + timeout
    last: Any = None
    while _ts() < deadline:
        last = probe()
        if last is not None and last is not False:
            return last
        time.sleep(interval)
    raise Refused(f"timed out after {timeout:.0f}s waiting for {what} (last={last!r})")


class Bundle:
    """The resumable evidence bundle — the on-disk state (no secret values)."""

    def __init__(self, path: Path = EVIDENCE_PATH) -> None:
        self.path = path
        self.document: dict[str, Any] = {"schema": "forge.review-loop.live/1", "phases": {}}
        if path.is_file():
            self.document = json.loads(path.read_text(encoding="utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def phase(self, name: str) -> dict[str, Any]:
        return self.document["phases"].setdefault(name, {})

    def record(self, phase: str, key: str, value: Any) -> None:
        self.phase(phase)[key] = value
        self.save()

    def append(self, phase: str, key: str, value: Any) -> None:
        self.phase(phase).setdefault(key, []).append(value)
        self.save()

    def failure(self, phase: str, message: str) -> None:
        self.phase(phase).setdefault("failures", []).append({"at": _now(), "message": message})
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.document, indent=2, sort_keys=True) + "\n")

    def run(self, phase: str, fn: Callable[[], int]) -> int:
        """Execute one phase honestly: a refusal is recorded, never hidden."""
        record = self.phase(phase)
        if record.get("result") == "green":
            print(f"{phase}: already complete")
            return 0
        record["started_at"] = _now()
        self.save()
        try:
            code = fn()
        except Refused as exc:
            record["result"] = "refused"
            record["refusal"] = str(exc)
            self.failure(phase, str(exc))
            print(f"{phase}: REFUSED — {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 — recorded honestly, never hidden
            record["result"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            self.failure(phase, record["error"])
            print(f"{phase}: ERROR — {record['error']}", file=sys.stderr)
            return 1
        if code == 0:
            record["result"] = "green"
            record["finished_at"] = _now()
        self.save()
        return code


class State:
    """Maintainer-private, gitignored (ids only — never a credential value)."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self.document: dict[str, Any] = {}
        if path.is_file():
            self.document = json.loads(path.read_text(encoding="utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, default: Any = None) -> Any:
        return self.document.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.document[key] = value
        self.path.write_text(json.dumps(self.document, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Lab plumbing: container env, GitLab API, the app's read API, read-only psql
# ---------------------------------------------------------------------------


def podman(*argv: str, timeout: float = 120.0, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        ["podman", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and completed.returncode != 0:
        raise Refused(f"podman {' '.join(argv[:3])} failed: {completed.stderr.strip()[:200]}")
    return completed


def app_env(container: str, name: str) -> str:
    completed = podman("exec", container, "printenv", name, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise Refused(f"{container} does not carry {name}")
    return completed.stdout.strip()


class GitLab:
    """The operator's GitLab API client (admin PAT from the app container)."""

    def __init__(self) -> None:
        self.base = app_env("forge-app", "GITLAB_URL").rstrip("/") + "/api/v4"
        self.token = app_env("forge-app", "GITLAB_TOKEN")
        self.webhook_secret = app_env("forge-app", "GITLAB_WEBHOOK_SECRET")
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

    def get_optional(self, path: str) -> Any | None:
        response = self.client.get(path)
        return response.json() if response.status_code == 200 else None

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.post(path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.put(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.delete(path, **kwargs)


def psql(sql: str) -> str:
    completed = podman(
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
        timeout=90,
    )
    return completed.stdout.strip()


def psql_json(sql: str) -> Any:
    raw = psql(sql)
    return json.loads(raw) if raw else None


def app_get(path: str) -> Any:
    response = httpx.get(f"{APP_API}{path}", timeout=30.0)
    response.raise_for_status()
    return response.json()


def app_run(run_id: str) -> dict[str, Any]:
    return app_get(f"/runs/{run_id}")


def _repo_version() -> str:
    init = REPO_ROOT / "src" / "forge" / "__init__.py"
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", init.read_text(encoding="utf-8"))
    if match is None:
        raise Refused(f"no __version__ in {init}")
    return match.group(1)


# ---------------------------------------------------------------------------
# The seed: the frozen three-shape task, the precommitted oracle, the CI
# ---------------------------------------------------------------------------


def _cases_block() -> str:
    return "\n".join(f'    ("{text}", "{expected}"),' for text, expected in SLUGIFY_CASES)


def oracle_test_file() -> str:
    """The precommitted INDEPENDENT oracle (pytest): the six exact slugify
    cases PLUS the three file shapes. Committed BEFORE any run; the smoke
    CI job runs exactly this suite — a candidate that touches it fails."""
    return (
        '"""The independent verification contract (R41-09/#364).\n\n'
        "Committed before any qualification run: the six exact slugify cases\n"
        "and the three file shapes. The smoke CI job runs this suite on every\n"
        "candidate; a candidate that weakens or bypasses it is a FAILED\n"
        'candidate (the driver also asserts the candidate diff does not touch it)."""\n'
        "import sys\n"
        "from pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n\n"
        "from utils.text import slugify\n\n"
        "CASES = [\n" + _cases_block() + "\n]\n\n"
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


def round2_contract_file() -> str:
    """The HUMAN EDIT (the round-2 base): the correction's behavioral
    contract, added by the reviewer BEFORE the authorized /fix — red on
    the pre-correction head, green on the corrected candidate."""
    cases = "\n".join(f'    ("{text}", {expected!r}),' for text, expected in PARTS_CASES)
    return (
        '"""The round-2 correction contract, added by the human reviewer\n'
        "BEFORE the authorized /fix (R42-04/#377): red until slugify_parts\n"
        'lands; the correction must turn this suite green without touching it."""\n'
        "import sys\n"
        "from pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n\n"
        "from utils.text import slugify_parts\n\n"
        "CASES = [\n" + cases + "\n]\n\n"
        "def test_slugify_parts_contract() -> None:\n"
        "    for text, expected in CASES:\n"
        "        got = slugify_parts(text)\n"
        "        assert got == expected, (text, got, expected)\n\n\n"
        "def test_slugify_unchanged() -> None:\n"
        "    from utils.text import slugify\n"
        "    assert slugify('Hello, World!') == 'hello-world'\n"
        "    assert slugify('!!!leading and trailing!!!') == 'leading-and-trailing'\n"
    )


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

The repository's `smoke` CI job asserts the six cases and the three
shapes — it is the independent oracle and it is NOT part of your change.
"""


def seed_app_py() -> str:
    return (
        '"""The app entry — still on the deprecated helper (to be rewired)."""\n'
        "\n"
        "from utils.legacy import shout\n"
        "\n"
        "\n"
        "def greet(name: str) -> str:\n"
        '    return shout(f"hello {name}")\n'
    )


def seed_legacy_py() -> str:
    return (
        '"""DEPRECATED shouting helper — scheduled for deletion."""\n'
        "\n"
        "\n"
        "def shout(text: str) -> str:\n"
        "    return text.upper() + '!'\n"
    )


#: The lane job's install seam — the sha256-verified wheel ladder (the
#: GitHub template's documented FORGE_LANE_WHEEL ladder, #365's pairing).
#: NOTE an f-string — shell ``${VAR}`` spellings are brace-escaped.
_WHEEL_INSTALL_STEPS = (
    f"    # R42-04 (#377) — the BUILD-ONCE candidate's lane package: the\n"
    f"    # pinned wheel of the qualified composition, sha256-verified BEFORE install\n"
    f"    # (the ONLY deviation from the shipped template: its git+$FORGE_LANE_REF\n"
    f"    # install line is swapped for the wheel ladder the GitHub template\n"
    f"    # documents; every other before_script step below is the template\n"
    f"    # VERBATIM, and the template itself is included verbatim from\n"
    f"    # .forge-template/ committed in this project).\n"
    f'    - curl -fsSL --max-time 180 -o "/tmp/{LANE_WHEEL_NAME}" "${{FORGE_LANE_WHEEL}}"\n'
    f'    - echo "${{FORGE_LANE_WHEEL_SHA256}}  /tmp/{LANE_WHEEL_NAME}" | sha256sum -c -\n'
    f"    - >\n"
    f"      uv pip install --quiet --python /tmp/forge-lane-venv/bin/python\n"
    f'      "forge[interactive] @ file:///tmp/{LANE_WHEEL_NAME}"\n'
    f"    - echo \"forge lane package version=$(/tmp/forge-lane-venv/bin/python -c 'import forge; print(forge.__version__)') wheel=${{FORGE_LANE_WHEEL}} sha256=${{FORGE_LANE_WHEEL_SHA256}}\"\n"
)


def ci_yaml() -> str:
    """The disposable project's CI: the shipped SDK lane template included
    VERBATIM from the committed copy + the ONE install-seam override + the
    independent oracle job (pytest — so the human-added round-2 contract
    runs in the SAME required job as the precommitted six-case oracle)."""
    template = TEMPLATE_SOURCE.read_text(encoding="utf-8")
    if "forge-agent-claude-sdk:" not in template:
        raise Refused(f"{TEMPLATE_SOURCE} carries no forge-agent-claude-sdk job")
    before = template.split("before_script:", 1)[1].split("  script:", 1)[0]
    if "git+https://github.com/forcewake/forge@${FORGE_LANE_REF}" not in before:
        raise Refused("the template's before_script no longer carries the git install seam")
    overridden = before.replace(
        "    - >\n"
        "      uv pip install --quiet --python /tmp/forge-lane-venv/bin/python\n"
        '      "forge[interactive] @ git+https://github.com/forcewake/forge@${FORGE_LANE_REF}"\n',
        _WHEEL_INSTALL_STEPS,
        1,
    )
    if overridden == before:
        raise Refused("the install-seam swap matched nothing — template drift")
    return (
        "# Generated by scripts/run_review_loop_qualification.py (R42-04/#377):\n"
        "# the SHIPPED SDK lane template VERBATIM (committed under\n"
        "# .forge-template/ and included BY local include) + the ONE override:\n"
        "# the lane job's before_script install seam carries the pinned wheel\n"
        "# of the CURRENT composition instead of git+${FORGE_LANE_REF}; and the\n"
        "# independent oracle job (pytest) committed BEFORE any run.\n"
        "include:\n"
        "  - local: '.forge-template/claude-sdk-lane.gitlab-ci.yml'\n"
        "stages: [test, harness]\n\n"
        "forge-agent-claude-sdk:\n"
        "  before_script:" + overridden.rstrip("\n") + "\n\n"
        "# The INDEPENDENT verification contract (R42-04): the precommitted\n"
        "# six-case slugify oracle + the three shapes; the round-2 contract\n"
        "# (the human reviewer's file) runs in the SAME job. Runs on every\n"
        "# push pipeline (never the dispatch pipelines).\n"
        "smoke:\n"
        "  stage: test\n"
        "  image: python:3.13-slim\n"
        "  rules:\n"
        "    - if: '$FORGE_RUN_ID'\n"
        "      when: never\n"
        "    - when: on_success\n"
        "  script:\n"
        "    - pip install --quiet --no-input pytest\n"
        "    - python -m pytest tests -q\n"
    )


#: The repo-side scope declaration (.forge.yml implement.paths) — the
#: frozen spec's allowed_paths derive from it, and the review-feedback
#: classification is fail-closed without it (LIVE-FOUND on attempt 3,
#: run 8be14a80: an EMPTY scope makes every /fix a material proposal).
REPO_CONFIG_YML = (
    "# The repo-side run scoping (R42-04/#377): the /implement write surface.\n"
    "implement:\n"
    "  paths:\n"
    "    - src/utils/text.py\n"
    "    - src/app.py\n"
    "    - src/utils/legacy.py\n"
)


def seed_files(name: str) -> dict[str, str]:
    return {
        "README.md": (
            f"# {name}\n\nThe R42-04 (#377) build-once review-and-correction loop\n"
            "qualification disposable project — deleted after capture. The\n"
            "precommitted independent oracle (tests/, run by the smoke CI job)\n"
            "asserts the six exact slugify cases and the three file shapes on\n"
            "every candidate. .forge.yml declares the implement write scope.\n"
        ),
        ".gitlab-ci.yml": ci_yaml(),
        ".forge-template/claude-sdk-lane.gitlab-ci.yml": TEMPLATE_SOURCE.read_text(
            encoding="utf-8"
        ),
        ".forge.yml": REPO_CONFIG_YML,
        "tests/test_text_utils.py": oracle_test_file(),
        "src/app.py": seed_app_py(),
        "src/utils/__init__.py": "",
        "src/utils/legacy.py": seed_legacy_py(),
    }


# ---------------------------------------------------------------------------
# phase: probe — BOTH credential routes at the real model gateway
# ---------------------------------------------------------------------------


def phase_probe(bundle: Bundle) -> int:
    record = bundle.phase("probe")
    probe_model = app_env("forge-app", "FORGE_HARNESS_MODEL")
    base_url = None
    routes: dict[str, dict[str, Any]] = {}

    def probe_route(label: str, token: str) -> dict[str, Any]:
        body = {
            "model": probe_model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Say OK"}],
        }
        started = _ts()
        try:
            response = httpx.post(
                f"{base_url.rstrip('/')}/v1/messages",
                json=body,
                headers={
                    "x-api-key": token,
                    "authorization": f"Bearer {token}",
                    "anthropic-version": "2023-06-01",
                },
                timeout=45.0,
            )
        except httpx.HTTPError as exc:
            return {"label": label, "transport_error": str(exc)[:120]}
        out: dict[str, Any] = {
            "label": label,
            "status": response.status_code,
            "token_sha256_16": sha256_hex(token)[:16],
            "latency_s": round(_ts() - started, 2),
            "max_tokens": 8,
        }
        try:
            document = response.json()
        except ValueError:
            document = {}
        if response.status_code == 200:
            out["model"] = document.get("model")
            out["usage"] = document.get("usage")
            out["alive"] = True
        else:
            error = document.get("error") or {}
            out["error_type"] = str(error.get("type", ""))
            out["error_message_head"] = str(error.get("message", ""))[:100]
            out["alive"] = False
        return out

    # route (b): the gitlab-protected-variable carrier — project 68's CI
    # variable (the v0.39.0 trace's composition), the value the lane will
    # receive as FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN.
    gl = GitLab()
    pv_token_entry = gl.get_optional("/projects/68/variables/ANTHROPIC_AUTH_TOKEN")
    base_entry = gl.get_optional("/projects/68/variables/ANTHROPIC_BASE_URL")
    if not pv_token_entry or not base_entry:
        raise Refused("project 68 carries no ANTHROPIC_* CI variables — the route is unprovisioned")
    pv_token = str(pv_token_entry.get("value") or "")
    base_url = str(base_entry.get("value") or "")
    if not pv_token or not base_url:
        raise Refused("the protected-variable route carries empty values")
    routes["gitlab-protected-variable"] = probe_route("gitlab-protected-variable", pv_token)

    # route (a): the runner-redemption delivery — the broker-held
    # ANTHROPIC_AUTH_TOKEN the redemption endpoint resolves for env: refs.
    broker_token = app_env("forge-app", ENV_SLOT)
    routes["runner-redemption-broker"] = probe_route("runner-redemption-broker", broker_token)

    record["model_gateway"] = base_url
    record["model"] = probe_model
    record["routes"] = routes
    record["variable_name"] = MODEL_VARIABLE_NAME
    record["protected_variable_sha256_16"] = sha256_hex(pv_token)[:16]
    State().set("model_variable_digest16", sha256_hex(pv_token)[:16])
    bundle.save()

    for label, outcome in routes.items():
        print(
            f"probe: {label} -> {outcome.get('status')} "
            f"alive={outcome.get('alive')} ({outcome.get('error_message_head', 'ok')})"
        )
    if not routes["gitlab-protected-variable"].get("alive"):
        record["result"] = "both-dead"
        bundle.failure("probe", "BOTH credential routes dead — the model legs are blocked")
        bundle.save()
        raise Refused(
            "the protected-variable route is dead too — record the blocked outcome "
            "(operator: rotate the project CI variable or the broker token)"
        )
    if routes["runner-redemption-broker"].get("alive"):
        print("probe: NOTE — the broker token revived this window; recorded honestly")
    record["result"] = "green"
    bundle.save()
    return 0


# ---------------------------------------------------------------------------
# phase: align — the minimal, receipted lab re-alignment
# ---------------------------------------------------------------------------


def _container_spec(container: str) -> dict[str, Any]:
    """The observed run identity of one consumer (env/binds/ports/cmd)."""
    document = json.loads(podman("inspect", container, "--format", "{{json .}}").stdout)
    host = document["HostConfig"]
    return {
        "image": document["Config"]["Image"],
        "image_id": document["Image"],
        "env": list(document["Config"]["Env"]),
        "binds": list(host.get("Binds") or []),
        "ports": host.get("PortBindings") or {},
        "network": host.get("NetworkMode") or "bridge",
        "cmd": list(document["Config"]["Cmd"] or []),
    }


def _run_argv(spec: Mapping[str, Any], pins: Mapping[str, str]) -> list[str]:
    argv = ["run", "-d", "--name", "{{NAME}}", "--network", str(spec["network"])]
    for bind in spec["binds"]:
        argv += ["-v", bind]
    for container_port, bindings in (spec["ports"] or {}).items():
        for binding in bindings or []:
            argv += ["-p", f"{binding.get('HostPort')}:{container_port.split('/')[0]}"]
    merged: dict[str, str] = {}
    for entry in spec["env"]:
        key, _, value = entry.partition("=")
        merged[key] = value
    merged.update(pins)
    for key, value in merged.items():
        argv += ["-e", f"{key}={value}"]
    argv.append(str(spec["image"]))
    argv += [str(part) for part in spec["cmd"]]
    return argv


def _health_probe() -> dict[str, Any] | None:
    """/health, resilient to the restart window (None = keep polling)."""
    try:
        health = httpx.get(f"{APP_API}/health", timeout=10).json()
    except (httpx.HTTPError, ValueError):
        return None
    return health if health.get("status") == "ok" else None


def phase_align(bundle: Bundle) -> int:
    record = bundle.phase("align")
    receipts = {
        "stamp": "forge.lab.alignment/1",
        "generated_at": _now(),
        "purpose": (
            "the R42-04 build-once alignment: the candidate image was BUILT from the "
            "tree by align_lab.py --apply (its receipts live in this document — the "
            "same file this phase appends the verify receipt to) and both consumers "
            "run it with exactly the pins below; this phase re-verifies the pins and "
            "re-runs align_lab.py as a verify-only receipt"
        ),
        "pins": {key: value for key, value in ALIGNMENT_PINS},
        "steps": [],
    }

    # 0. the wheelhost serves the pinned wheel again (the #365 lane pairing).
    wheel_sha = hashlib.sha256(LANE_WHEEL_PATH.read_bytes()).hexdigest()
    if wheel_sha != LANE_WHEEL_SHA256:
        raise Refused(f"the staged wheel digest moved ({wheel_sha} != {LANE_WHEEL_SHA256})")
    LANE_WHEEL_DIR.mkdir(parents=True, exist_ok=True)
    served = LANE_WHEEL_DIR / LANE_WHEEL_NAME
    if not served.is_file() or hashlib.sha256(served.read_bytes()).hexdigest() != wheel_sha:
        served.write_bytes(LANE_WHEEL_PATH.read_bytes())
    existing = podman("ps", "-a", "--format", "{{.Names}}", check=False).stdout.split()
    if "forge-wheelhost" not in existing:
        podman(
            "run",
            "-d",
            "--name",
            "forge-wheelhost",
            "-p",
            f"{WHEEL_HOST_PORT}:80",
            "-v",
            f"{LANE_WHEEL_DIR}:/usr/share/nginx/html:ro",
            "docker.io/library/nginx:alpine",
        )
        receipts["steps"].append(
            {"step": "wheelhost", "action": "started", "port": WHEEL_HOST_PORT}
        )
    probe = httpx.get(WHEEL_URL, timeout=10.0)
    if probe.status_code != 200:
        raise Refused(f"the wheel URL is not serving: {WHEEL_URL} -> {probe.status_code}")
    receipts["steps"].append(
        {"step": "wheelhost", "action": "verified", "url": WHEEL_URL, "sha256": wheel_sha}
    )

    # 1. recreate both consumers from the current image with the pins added
    #    (also when the IMAGE moved — the live-found publisher patch was
    #    rebuilt into localhost/forge:dev mid-window, the #365 precedent).
    current_image_id = podman("images", "localhost/forge:dev", "--format", "{{.ID}}").stdout.strip()
    for container in ("forge-app", "forge-worker"):
        spec = _container_spec(container)
        env_now = {entry.partition("=")[0]: entry.partition("=")[2] for entry in spec["env"]}
        missing = {key: value for key, value in ALIGNMENT_PINS if env_now.get(key) != value}
        image_stale = str(spec.get("image_id", "")).strip() != current_image_id
        if not missing and not image_stale:
            receipts["steps"].append(
                {
                    "step": f"recreate:{container}",
                    "action": "skipped",
                    "reason": "pins + image current",
                }
            )
            continue
        if image_stale:
            receipts["steps"].append(
                {
                    "step": f"recreate:{container}",
                    "action": "image-drift-detected",
                    "running_image_id": str(spec.get("image_id", ""))[:19] + "…",
                    "current_image_id": current_image_id[:19] + "…",
                    "reason": "the live-found publisher patch was rebuilt into the tag",
                }
            )
        argv = _run_argv(spec, missing)
        receipts["steps"].append(
            {
                "step": f"recreate:{container}",
                "action": "recreated-from-observed-spec",
                "image": spec["image"],
                "image_id": spec["image_id"],
                "pins_added": sorted(missing),
                "argv_shape": (
                    "podman run -d --name <container> --network <observed> "
                    "-v <observed binds> -p <observed ports> -e <observed env + pins> "
                    "<image> <cmd> — every observed env entry carried over verbatim "
                    "(secrets never receipted)"
                ),
            }
        )
        podman("rm", "-f", container, timeout=90)
        podman(*[part if part != "{{NAME}}" else container for part in argv], timeout=120)
    receipts["steps"].append(
        {
            "step": "health-wait",
            "action": "polling /health until ok",
        }
    )
    health = poll(
        lambda: _health_probe(),
        "the app's /health ok after the recreate",
        timeout=240,
        interval=5,
    )
    receipts["steps"].append({"step": "health-wait", "action": "ok", "health": health})

    # 2. align_lab re-runs as the verify-only receipt (no mutation steps:
    # the verify probes pass — version/schema/caps/extra-env — so the plan
    # reports already-aligned).
    align_argv = [
        "uv",
        "run",
        "python",
        "scripts/align_lab.py",
        "--apply",
        *[f"--extra-env={key}={value}" for key, value in ALIGNMENT_PINS],
        f"--receipts={EVAL_DIR / 'alignment-verify-receipts.json'}",
    ]
    completed = subprocess.run(
        align_argv, cwd=REPO_ROOT, capture_output=True, text=True, timeout=900, check=False
    )
    receipts["steps"].append(
        {
            "step": "align_lab_verify",
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-800:],
            "stderr_tail": completed.stderr[-300:],
        }
    )
    record["receipts"] = receipts
    record["health"] = health
    bundle.save()
    if completed.returncode != 0:
        raise Refused(f"align_lab verify failed: {completed.stdout[-400:]}")
    print("align: consumers re-pinned (same image) + align_lab verify receipt green")
    return 0


# ---------------------------------------------------------------------------
# phase: setup — the disposable project, the carrier variable, the binding
# ---------------------------------------------------------------------------


def phase_setup(bundle: Bundle, name: str) -> int:
    record = bundle.phase("setup")
    gl = GitLab()

    if record.get("project", {}).get("id") is None:
        who = gl.get("/user")
        if who.get("username") != APPROVER_USERNAME:
            raise Refused(
                f"the operator token acts as @{who.get('username')} — the approver is "
                f"@{APPROVER_USERNAME} (FORGE_APPROVERS); the /fix authority gate needs the approver"
            )
        namespace_path = (who.get("namespace") or {}).get("path") or who.get("username", "")
        existing_project = gl.get_optional(f"/projects/{namespace_path}%2F{name}")
        if existing_project is not None:
            project_id = int(existing_project["id"])
            bundle.record(
                "setup",
                "project",
                {"id": project_id, "path": existing_project["path_with_namespace"]},
            )
            head = gl.get_optional(f"/projects/{project_id}/repository/commits/main")
            bundle.record("setup", "seed_commit_sha", (head or {}).get("id"))
            bundle.record(
                "setup",
                "template_sha256",
                hashlib.sha256(TEMPLATE_SOURCE.read_bytes()).hexdigest(),
            )
            bundle.record("setup", "ci_yaml_sha256", hashlib.sha256(ci_yaml().encode()).hexdigest())
            print(f"setup: adopting the EXISTING project {existing_project['path_with_namespace']}")
        else:
            created = gl.post(
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
                raise Refused(
                    f"project creation failed: {created.status_code} {created.text[:200]}"
                )
            project = created.json()
            bundle.record(
                "setup", "project", {"id": project["id"], "path": project["path_with_namespace"]}
            )
            print(f"setup: project {project['path_with_namespace']} (id {project['id']})")
            commit = gl.post(
                f"/projects/{bundle.document['phases']['setup']['project']['id']}/repository/commits",
                json={
                    "branch": "main",
                    "commit_message": (
                        "seed: the shipped SDK lane template verbatim + the wheel-override CI "
                        "+ the precommitted independent oracle (committed before any run) — R42-04/#377"
                    ),
                    "actions": [
                        {"action": "create", "file_path": path, "content": content}
                        for path, content in sorted(seed_files(name).items())
                    ],
                },
            )
            if commit.status_code not in (201, 200):
                raise Refused(f"seed commit failed: {commit.status_code} {commit.text[:300]}")
            bundle.record("setup", "seed_commit_sha", commit.json().get("id"))
            bundle.record(
                "setup",
                "template_sha256",
                hashlib.sha256(TEMPLATE_SOURCE.read_bytes()).hexdigest(),
            )
            bundle.record("setup", "ci_yaml_sha256", hashlib.sha256(ci_yaml().encode()).hexdigest())
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])

    if not record.get("variables"):
        wheel_sha = hashlib.sha256(LANE_WHEEL_PATH.read_bytes()).hexdigest()
        # the NATIVE carrier: FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN,
        # protected + masked — exactly what the shipped template's
        # resolution step reads (the VALUE never rides a trigger variable).
        pv_token = gl.get(f"/projects/68/variables/{ENV_SLOT}").get("value", "")
        base_url = gl.get("/projects/68/variables/ANTHROPIC_BASE_URL").get("value", "")
        if not pv_token or not base_url:
            raise Refused("the source CI variables are empty — refusing to provision")
        desired: list[tuple[str, str, dict[str, Any]]] = [
            # LIVE-FOUND (the #364 window, run 165dd1ed, job 1135): a
            # PROTECTED CI variable is only exposed to pipelines on
            # PROTECTED refs — the factory branches are not protected, so
            # the lane failed CLOSED at its bootstrap fence (typed
            # FORGE_BOOTSTRAP_FAILED, zero model calls). The carrier is
            # therefore masked but NOT protected on this lab; the shipped
            # template's guidance names the protected posture for
            # deployments that dispatch on protected refs.
            (MODEL_VARIABLE_NAME, pv_token, {"protected": False, "masked": True}),
            # R42-03 (#376) — NO AMBIENT DUPLICATE this window: the
            # doctor's per-driver lane check is now delivery-mode aware
            # (check_harness_lanes drops exactly the substituted ambient
            # slot when the selected carrier passed its prerequisite), so
            # the app's own doctor must pass WITHOUT the ambient
            # ANTHROPIC_AUTH_TOKEN mirror the #364 window needed. The
            # preflight phase asserts the doctor green with only the
            # carrier provisioned — the #376 fix proven live on the
            # candidate image.
            ("ANTHROPIC_BASE_URL", base_url, {"protected": False, "masked": False}),
            ("FORGE_LANE_WHEEL", WHEEL_URL, {}),
            ("FORGE_LANE_WHEEL_SHA256", wheel_sha, {}),
            ("FORGE_STEERING_ENABLED", "1", {}),
        ]
        read_token = gl.get_optional("/projects/68/variables/FORGE_BOT_READ_TOKEN")
        if read_token and read_token.get("value"):
            desired.append(("FORGE_BOT_READ_TOKEN", read_token["value"], {}))
        set_keys: list[str] = []
        for key, value, flags in desired:
            existing = gl.get_optional(f"/projects/{project_id}/variables/{key}")
            if existing is None:
                response = gl.post(
                    f"/projects/{project_id}/variables", json={"key": key, "value": value, **flags}
                )
                if response.status_code not in (201, 200):
                    raise Refused(f"variable {key} set failed: {response.text[:200]}")
            elif (
                key in ("FORGE_LANE_WHEEL_SHA256", "FORGE_LANE_WHEEL")
                and existing.get("value") != value
            ):
                # the live-found publisher patch re-pinned the wheel — the
                # lane pairing variables MOVE with the composition (#365
                # precedent); every other variable is create-only.
                response = gl.put(f"/projects/{project_id}/variables/{key}", json={"value": value})
                if response.status_code != 200:
                    raise Refused(f"variable {key} update failed: {response.text[:200]}")
            set_keys.append(key)
        bundle.record(
            "setup",
            "variables",
            {
                "keys": set_keys,
                "carrier": {
                    "name": MODEL_VARIABLE_NAME,
                    "protected": False,
                    "masked": True,
                    "value_sha256_16": sha256_hex(pv_token)[:16],
                    "protected_note": (
                        "masked, NOT protected — LIVE-FOUND: GitLab exposes protected "
                        "variables only to protected refs and the factory branches are "
                        "not protected; attempt 1 failed CLOSED at the lane's bootstrap "
                        "fence (typed FORGE_BOOTSTRAP_FAILED, zero model spend)"
                    ),
                },
                "ambient_duplicate_for_doctor": None,
                "no_ambient_duplicate_note": (
                    "#376 fix on the candidate: the doctor is delivery-mode aware and "
                    "substitutes the carrier for the ambient slot — no ambient "
                    "ANTHROPIC_AUTH_TOKEN variable is provisioned this window"
                ),
                "model_route": base_url,
            },
        )
        print(f"setup: variables {set_keys} (carrier masked; NO ambient duplicate — #376)")

    if not record.get("webhook"):
        lab_hooks = gl.get("/projects/68/hooks")
        forge_hook = next((h for h in lab_hooks if "/webhook" in str(h.get("url", ""))), None)
        if forge_hook is None:
            raise Refused("the lab project carries no forge webhook to replicate")
        hook = gl.post(
            f"/projects/{project_id}/hooks",
            json={
                "url": forge_hook["url"],
                "token": gl.webhook_secret,
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

    if not record.get("bot_member"):
        bot_token = app_env("forge-app", "FORGE_BOT_TOKEN")
        with httpx.Client(
            base_url=gl.base, headers={"PRIVATE-TOKEN": bot_token}, timeout=30.0
        ) as bot_client:
            bot = bot_client.get("/user")
            bot.raise_for_status()
            bot_user = bot.json()
        member = gl.post(
            f"/projects/{project_id}/members",
            json={"user_id": bot_user["id"], "access_level": 30},
        )
        if member.status_code not in (201, 200) and "already exists" not in member.text:
            raise Refused(f"bot membership grant failed: {member.text[:200]}")
        bundle.record(
            "setup", "bot_member", {"user_id": bot_user["id"], "username": bot_user["username"]}
        )
        print(f"setup: bot @{bot_user['username']} granted Developer on project {project_id}")

    if not record.get("repo_config"):
        # LIVE-FOUND (attempt 3, run 8be14a80): the frozen spec's
        # allowed_paths derive from the target repo's .forge.yml
        # (implement.paths); without it the scope is EMPTY and every /fix
        # classifies material_change (fail-closed by design — the honest
        # reply landed, MR note 1474). The repo-side scope declaration is
        # part of the SEED (committed before any qualifying run).
        existing = gl.get_optional(f"/projects/{project_id}/repository/files/.forge.yml")
        if existing is None:
            commit = gl.post(
                f"/projects/{project_id}/repository/commits",
                json={
                    "branch": "main",
                    "commit_message": (
                        "seed: declare the implement write scope (.forge.yml) — the "
                        "review-feedback classification is fail-closed without it (R42-04/#377)"
                    ),
                    "actions": [
                        {"action": "create", "file_path": ".forge.yml", "content": REPO_CONFIG_YML}
                    ],
                },
            )
            if commit.status_code not in (201, 200) and "already exists" not in commit.text:
                raise Refused(f"the .forge.yml seed commit failed: {commit.text[:200]}")
        bundle.record(
            "setup",
            "repo_config",
            {
                "path": ".forge.yml",
                "key": "implement.paths",
                "content_sha256": sha256_hex(REPO_CONFIG_YML),
                "committed_before_any_qualifying_run": True,
            },
        )

    if not record.get("binding"):
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from forge.adaptive.project_credentials import ProjectCredentialRegistry

        registry = ProjectCredentialRegistry(path=REPO_ROOT / "data" / "credential-bindings.json")
        subject = f"gitlab/-/{project_id}"
        binding = registry.bind(
            subject,
            PROVIDER_ROUTE,
            BROKER_REF,
            bound_by="pavel (R42-04 #377 operator action)",
            project_id=project_id,
        )
        bundle.record(
            "setup",
            "binding",
            {
                "subject": subject,
                "credential_ref": binding.credential_ref,
                "env_var": binding.env_var,
                "revision": binding.revision,
            },
        )
        print(f"setup: credential binding rev {binding.revision} ({BROKER_REF})")
    return 0


# ---------------------------------------------------------------------------
# phase: preflight — the app's OWN doctor + the alignment axes
# ---------------------------------------------------------------------------


def phase_preflight(bundle: Bundle) -> int:
    record = bundle.phase("preflight")
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    checks: dict[str, Any] = {}

    health = httpx.get(f"{APP_API}/health", timeout=15).json()
    version = _repo_version()
    checks["controlplane.health"] = {
        "status": health.get("status"),
        "version": health.get("version"),
    }
    if health.get("status") != "ok" or health.get("version") != version:
        raise Refused(f"control plane not aligned onto the repo version {version}: {health}")

    head = psql("SELECT version_num FROM alembic_version")
    checks["schema_head"] = head
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.inventory_lab import repo_schema_head

    if head != repo_schema_head(REPO_ROOT):
        raise Refused(f"schema head {head} != repo chain head {repo_schema_head(REPO_ROOT)}")

    for container in ("forge-app", "forge-worker"):
        env = {
            entry.partition("=")[0]: entry.partition("=")[2]
            for entry in json.loads(
                podman("inspect", container, "--format", "{{json .Config.Env}}").stdout
            )
        }
        pins_ok = all(env.get(key) == value for key, value in ALIGNMENT_PINS)
        caps_ok = env.get("FORGE_LANE_BUDGET_SECONDS", "").isdigit() and env.get(
            "FORGE_BUDGET_PROFILES", ""
        ).startswith("{")
        checks[f"env.{container}"] = {"pins_ok": pins_ok, "caps_ok": caps_ok}
        if not (pins_ok and caps_ok):
            raise Refused(
                f"{container} misses the alignment pins or the budget caps: {env.get('FORGE_CREDENTIAL_DELIVERY')}"
            )

    attempts: list[dict[str, Any]] = []
    doctor_json: dict[str, Any] = {}
    for attempt in range(1, 4):
        doctor = podman(
            "exec",
            "forge-app",
            "python",
            "-m",
            "forge.doctor",
            "--project",
            str(project_id),
            "--json",
            timeout=300,
            check=False,
        )
        try:
            doctor_json = json.loads(doctor.stdout)
        except json.JSONDecodeError:
            doctor_json = {"status": "unparseable", "stdout_tail": doctor.stdout[-400:]}
        failed = [c["name"] for c in doctor_json.get("checks", []) if c.get("status") == "FAIL"]
        attempts.append({"attempt": attempt, "returncode": doctor.returncode, "failed": failed})
        if doctor.returncode == 0 and not failed:
            break
        time.sleep(20)
    checks["doctor"] = attempts[-1] if attempts else None
    if not attempts or attempts[-1]["returncode"] != 0 or attempts[-1]["failed"]:
        raise Refused(f"the app's own doctor failed (3 attempts): {attempts}")

    gl = GitLab()
    runners = gl.get(f"/projects/{project_id}/runners")
    online = [r["id"] for r in runners if r.get("status") == "online"]
    checks["runners_online"] = online
    if not online:
        raise Refused("no online runner serves the disposable project")

    record["checks"] = checks
    record["result"] = "green"
    record["finished_at"] = _now()
    bundle.save()
    print("preflight: GREEN (health + schema + pins + doctor + runner)")
    return 0


# ---------------------------------------------------------------------------
# shared live helpers — runs, pipelines, MRs, notes, budgets, evidence
# ---------------------------------------------------------------------------


def find_run_for_issue(project_id: int, issue_iid: int) -> dict[str, Any] | None:
    for run in app_get("/runs?limit=50").get("runs", []):
        if run.get("project_id") == project_id and run.get("issue_iid") == issue_iid:
            return run
    return None


def wait_status(run_id: str, statuses: Sequence[str], timeout: float, what: str) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        run = app_run(run_id)
        return run if run.get("status") in statuses else None

    return poll(probe, what, timeout=timeout, interval=8.0)


def lane_job(gl: GitLab, project_id: int, pipeline_id: int) -> dict[str, Any] | None:
    for job in gl.get(f"/projects/{project_id}/pipelines/{pipeline_id}/jobs"):
        if str(job.get("name", "")).startswith("forge-agent"):
            return job
    return None


def latest_dispatch_lane(
    gl: GitLab, project_id: int, branch: str, since_iso: str = ""
) -> dict[str, Any] | None:
    """The newest api-triggered pipeline's lane job on the branch (the
    harness dispatch surface), with its trace receipt + usage meta."""
    pipelines = gl.get(f"/projects/{project_id}/pipelines", params={"ref": branch})
    for pipeline in pipelines:
        if pipeline.get("source") != "api":
            continue
        if since_iso and str(pipeline.get("created_at", "")) < since_iso:
            continue
        job = lane_job(gl, project_id, int(pipeline["id"]))
        if job is not None:
            return {"pipeline_id": pipeline["id"], "job_id": job["id"], "status": job.get("status")}
    return None


def pipeline_for_sha(gl: GitLab, project_id: int, sha: str) -> dict[str, Any] | None:
    for pipeline in gl.get(f"/projects/{project_id}/pipelines", params={"sha": sha}):
        return pipeline
    return None


def mr_head(gl: GitLab, project_id: int, mr_iid: int) -> str:
    """The MR's SOURCE BRANCH head — read from the branch itself (LIVE-FOUND:
    the MR document's ``sha`` field lags a just-pushed commit by seconds)."""
    merge_request = gl.get(f"/projects/{project_id}/merge_requests/{mr_iid}")
    branch = str(merge_request.get("source_branch") or "")
    if not branch:
        return str(merge_request.get("sha") or "")
    document = gl.get_optional(
        f"/projects/{project_id}/repository/branches/{branch.replace('/', '%2F')}"
    )
    if document and document.get("commit"):
        return str(document["commit"]["id"])
    return str(merge_request.get("sha") or "")


def branch_commits(
    gl: GitLab, project_id: int, branch: str, limit: int = 40
) -> list[dict[str, Any]]:
    commits = gl.get(
        f"/projects/{project_id}/repository/commits",
        params={"ref_name": branch, "per_page": limit},
    )
    return [
        {
            "id": c["id"],
            "short_id": c.get("short_id"),
            "title": c.get("title", "")[:110],
            "author_name": c.get("author_name"),
            "created_at": c.get("created_at"),
        }
        for c in commits
    ]


def bot_commit_count(entries: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for c in entries if str(c.get("author_name") or "") == BOT_USERNAME)


def post_fix_note(gl: GitLab, project_id: int, mr_iid: int, text: str) -> dict[str, Any]:
    """Post the reviewer's /fix note through the DISCUSSIONS surface (the
    note lands in its own discussion — the id the request binds to)."""
    response = gl.post(
        f"/projects/{project_id}/merge_requests/{mr_iid}/discussions", json={"body": text}
    )
    if response.status_code not in (201, 200):
        raise Refused(
            f"the /fix discussion post failed: {response.status_code} {response.text[:200]}"
        )
    document = response.json()
    notes = document.get("notes") or []
    if not notes:
        raise Refused("the /fix discussion returned no notes")
    return {
        "discussion_id": document.get("id"),
        "note_id": notes[0].get("id"),
        "author": notes[0].get("author", {}).get("username"),
        "body_head": str(notes[0].get("body", ""))[:80],
    }


def wait_bot_reply(
    gl: GitLab, project_id: int, mr_iid: int, fragment: str, since_id: int, timeout: float = 300.0
) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        notes = gl.get(f"/projects/{project_id}/merge_requests/{mr_iid}/notes")
        for entry in notes:
            if (
                int(entry.get("id", 0)) > since_id
                and entry.get("author", {}).get("username") == BOT_USERNAME
                and fragment in str(entry.get("body", ""))
            ):
                return entry
        return None

    return poll(probe, f"the bot's MR reply carrying {fragment!r}", timeout=timeout, interval=5.0)


def round_rows(parent_run_id: str) -> list[dict[str, Any]]:
    """Every round row of the parent's LINEAGE (root = delivery 1)."""
    return (
        psql_json(
            "SELECT json_agg(row_to_json(r)) FROM (SELECT id, parent_run_id, child_run_id, "
            "root_run_id, round_number, note_id, mr_iid, base_head_sha, decision_id, "
            "requested_by, status, status_reason FROM review_rounds WHERE root_run_id = "
            "COALESCE((SELECT root_run_id FROM review_rounds WHERE parent_run_id = '"
            + parent_run_id
            + "' OR child_run_id = '"
            + parent_run_id
            + "' LIMIT 1), '"
            + parent_run_id
            + "') ORDER BY round_number) r"
        )
        or []
    )


def run_budget_row(run_id: str) -> dict[str, Any] | None:
    return psql_json(
        "SELECT row_to_json(b) FROM (SELECT max_calls, max_tokens, wallclock_s, "
        "closing_reserved_calls, closing_reserved_tokens, closing_partition_policy, "
        "status FROM run_budgets WHERE run_id = '" + run_id + "' LIMIT 1) b"
    )


def run_evidence_fields(run_id: str, keys: Sequence[str]) -> dict[str, Any]:
    selections = ", ".join(f"'{key}', evidence->'{key}'" for key in keys)
    document = psql_json(
        f"SELECT json_build_object({selections}) FROM flow_runs WHERE id = '{run_id}'"
    )
    return document or {}


def candidate_meta(gl: GitLab, project_id: int, job_id: int) -> dict[str, Any] | None:
    response = gl.client.get(
        f"/projects/{project_id}/jobs/{job_id}/artifacts/.forge/candidate.meta.json"
    )
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def spend_from_meta(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """The lane's OWN usage receipt (the SDK cost field preferred; a token
    estimate is labelled as such)."""
    if not meta:
        return {"receipt_count": 0, "total_usd": 0.0, "cost_basis": "no-meta"}
    usage = meta.get("usage") or {}
    if isinstance(usage.get("total_cost_usd"), (int, float)):
        return {
            "receipt_count": 1,
            "total_usd": round(float(usage["total_cost_usd"]), 4),
            "cost_basis": "sdk-total_cost_usd",
            "usage": usage,
        }
    tokens = {
        "input": int(usage.get("input_tokens") or 0),
        "cached_input": int(usage.get("cached_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
    }
    estimate = (
        tokens["input"] * FALLBACK_PRICE_PER_MTOK["input"]
        + tokens["cached_input"] * FALLBACK_PRICE_PER_MTOK["cached_input"]
        + tokens["output"] * FALLBACK_PRICE_PER_MTOK["output"]
    ) / 1_000_000
    return {
        "receipt_count": 1,
        "total_usd": round(estimate, 4),
        "cost_basis": "token-estimate-labelled",
        "usage": usage,
    }


def capture_trace(gl: GitLab, project_id: int, job_id: int, name: str) -> str | None:
    """Save the lane job's trace (receipts) under the evidence bundle."""
    try:
        trace = gl.get_text(f"/projects/{project_id}/jobs/{job_id}/trace")
    except httpx.HTTPError:
        return None
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    path = TRACES_DIR / f"{name}-job{job_id}.log"
    path.write_text(trace, encoding="utf-8")
    marker = PAIRING_MARKER_RE.search(trace)
    return (
        (marker.groupdict() | {"trace_sha256": sha256_hex(trace), "path": str(path)})
        if marker
        else ({"trace_sha256": sha256_hex(trace), "path": str(path), "pairing_marker": None})
    )


# ---------------------------------------------------------------------------
# phase: delivery — issue -> /implement -> /go -> ready_for_human
# ---------------------------------------------------------------------------


def phase_delivery(bundle: Bundle) -> int:
    phase = "delivery"
    record = bundle.phase(phase)
    record["paid"] = True
    bundle.save()
    gl = GitLab()
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])

    if not record.get("plan"):
        created = gl.post(
            f"/projects/{project_id}/issues",
            json={"title": issue_title(), "description": issue_body()},
        )
        if created.status_code not in (201, 200):
            raise Refused(f"issue creation failed: {created.text[:200]}")
        issue = created.json()
        bundle.record(phase, "issue", {"iid": issue["iid"], "url": issue["web_url"]})
        print(f"{phase}: issue #{issue['iid']} created — {issue['web_url']}")
        note = gl.post(
            f"/projects/{project_id}/issues/{issue['iid']}/notes",
            json={"body": f"@{BOT_USERNAME} /implement"},
        )
        if note.status_code not in (201, 200):
            raise Refused(f"/implement note failed: {note.text[:200]}")

        def plan_note() -> Any:
            for entry in reversed(gl.get(f"/projects/{project_id}/issues/{issue['iid']}/notes")):
                body = str(entry.get("body", ""))
                if entry.get("author", {}).get("username") == BOT_USERNAME and "/go " in body:
                    return entry
            return None

        plan = poll(plan_note, "the evidence-backed plan comment", timeout=900, interval=15)
        body = str(plan.get("body", ""))
        match = RUN_ID_RE.search(body)
        if match is None:
            raise Refused(f"plan note carries no run id:\n{body[-600:]}")
        harness_line = next(
            (line for line in body.splitlines() if line.startswith("- Harness:")), ""
        )
        bundle.record(
            phase,
            "plan",
            {"note_id": plan.get("id"), "run_id": match.group(1), "harness_line": harness_line},
        )
        if "claude-sdk-lane" not in harness_line:
            raise Refused(f"the frozen lane was not selected: {harness_line!r}")
        go = gl.post(
            f"/projects/{project_id}/issues/{issue['iid']}/notes",
            json={"body": f"@{BOT_USERNAME} /go {match.group(1)}"},
        )
        if go.status_code not in (201, 200):
            raise Refused(f"/go note failed: {go.text[:200]}")
    run_id = bundle.document["phases"][phase]["plan"]["run_id"]
    record["run_id"] = run_id

    run = wait_status(
        run_id,
        ("ready_for_human", "blocked", "failed"),
        timeout=1800,
        what="delivery 1 reaching a terminal state",
    )
    if run.get("status") != "ready_for_human":
        raise Refused(f"delivery 1 ended {run.get('status')}: {run.get('status_reason')}")
    bundle.record(phase, "terminal", {"status": run["status"], "at": _now()})

    # the MR + the candidate + the oracle + the review evidence
    def run_row_mr() -> Any:
        document = psql_json(
            "SELECT json_build_object('mr_iid', mr_iid, 'candidate_shas', candidate_shas, "
            f"'spec_digest', spec_digest, 'plan_digest', plan_digest) FROM flow_runs WHERE id = '{run_id}'"
        )
        return document if document and document.get("mr_iid") else None

    row = poll(run_row_mr, "the run's MR binding", timeout=120, interval=5)
    mr_iid = int(row["mr_iid"])
    candidate_sha = str((row.get("candidate_shas") or [""])[-1])
    merge_request = gl.get(f"/projects/{project_id}/merge_requests/{mr_iid}")
    branch = merge_request["source_branch"]
    bundle.record(
        phase,
        "mr",
        {
            "iid": mr_iid,
            "url": merge_request["web_url"],
            "branch": branch,
            "draft": bool(
                merge_request.get("work_in_progress")
                or str(merge_request.get("title", "")).startswith("Draft:")
            ),
            "state": merge_request.get("state"),
        },
    )
    bundle.record(phase, "candidate_sha", candidate_sha)
    if not bundle.document["phases"][phase]["mr"]["draft"]:
        raise Refused("the MR is not a Draft — a human merges, never the run")

    oracle = poll(
        lambda: (lambda p: p if p and p.get("status") == "success" else None)(
            pipeline_for_sha(gl, project_id, candidate_sha)
        ),
        "the green oracle pipeline on the exact candidate sha",
        timeout=600,
        interval=15,
    )
    bundle.record(
        phase,
        "oracle",
        {"pipeline_id": oracle["id"], "status": oracle["status"], "candidate_sha": candidate_sha},
    )

    evidence = run_evidence_fields(run_id, ("review", "active_plan"))
    review = evidence.get("review") or {}
    bundle.record(
        phase,
        "closing_review",
        {
            "verdict": review.get("verdict"),
            "reviewed_sha": review.get("sha"),
            "obligation_digest": review.get("obligation_digest"),
            "summary_head": str(review.get("summary", ""))[:200],
        },
    )
    if review.get("sha") != candidate_sha:
        raise Refused("the closing review is not bound to the candidate sha (ADR-0008)")
    if evidence.get("active_plan"):
        raise Refused(
            "the parent carries an active_plan pointer — this must be the ORDINARY classic "
            "run (no manual PlanRevision staged); the classic adapter path is the point"
        )
    bundle.record(phase, "ordinary_parent", {"active_plan_absent": True})
    bundle.record(phase, "budget", run_budget_row(run_id))
    lane = latest_dispatch_lane(gl, project_id, branch)
    if lane:
        meta = candidate_meta(gl, project_id, int(lane["job_id"]))
        bundle.record(phase, "lane", lane)
        bundle.record(phase, "lane_meta_spend", spend_from_meta(meta))
        trace_receipt = capture_trace(gl, project_id, int(lane["job_id"]), "delivery1")
        if trace_receipt:
            bundle.record(phase, "lane_trace_receipt", trace_receipt)
    print(f"{phase}: ready_for_human — MR !{mr_iid} candidate {candidate_sha[:12]} oracle green")
    return 0


# ---------------------------------------------------------------------------
# phase: round2 — human edit + the native /fix -> the budgeted child round
# ---------------------------------------------------------------------------


def _wait_child_ready_or_parked(
    gl: GitLab,
    bundle: Bundle,
    phase: str,
    child_id: str,
    mr_iid: int,
    discussion_id: str,
    project_id: int,
) -> dict[str, Any]:
    """Wait the child to ready_for_human — observing the readiness gate's
    HOLD (the run parks while the correction discussion is unresolved),
    then resolving it as the REVIEWER (the human decision; forge never
    resolves a discussion)."""
    started = _ts()
    resolved = False
    while _ts() - started < 1800:
        run = app_run(child_id)
        status = run.get("status")
        if status == "ready_for_human":
            if not resolved:
                bundle.record(
                    phase,
                    "readiness_gate_held",
                    {
                        "observed": False,
                        "note": "the child reached ready without the gate holding — recorded honestly",
                    },
                )
            return run
        if status in ("blocked", "failed"):
            raise Refused(f"the round child ended {status}: {run.get('status_reason')}")
        if not resolved:
            if status == "reviewing":
                first_seen = _ts()
                time.sleep(30)
                still = app_run(child_id).get("status")
                if still == "reviewing":
                    bundle.record(
                        phase,
                        "readiness_gate_held",
                        {
                            "status": "reviewing",
                            "held_seconds_observed": round(_ts() - first_seen, 1),
                            "observed_at": _now(),
                            "note": "the child holds in reviewing while the correction "
                            "discussion is unresolved (the review replays without a "
                            "second model call)",
                        },
                    )
                resolved = True
            elif status == "evaluating_ci" and _ts() - started > 240:
                bundle.record(
                    phase,
                    "readiness_gate_held",
                    {
                        "status": "evaluating_ci",
                        "observed_at": _now(),
                        "note": "the green-pipeline -> review handoff held the child (the first gate)",
                    },
                )
                resolved = True
            if resolved:
                resolve = gl.put(
                    f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}",
                    params={"resolved": "true"},
                )
                if resolve.status_code not in (200, 201):
                    raise Refused(f"the reviewer resolve failed: {resolve.text[:200]}")
                bundle.record(
                    phase,
                    "reviewer_resolve",
                    {
                        "discussion_id": discussion_id,
                        "returncode": resolve.status_code,
                        "resolved_by": APPROVER_USERNAME,
                    },
                )
        time.sleep(8)
    raise Refused("the round child never reached ready_for_human within 1800s")


def wait_round_child(
    gl: GitLab, project_id: int, mr_iid: int, parent_run_id: str, note_id: int, expected_round: int
) -> dict[str, Any]:
    """Wait the round admission's DURABLE identity: the review_rounds row
    keyed by this note (LIVE-FOUND: the round-DISPATCHED MR note carries
    only the 8-hex short id — the row is the durable join; the
    round-admitted reply is captured beside it, best-effort)."""

    def row_for_note() -> dict[str, Any] | None:
        for row in round_rows(parent_run_id):
            if str(row.get("note_id")) == str(note_id):
                return row
        return None

    row = poll(row_for_note, f"the review_rounds row for note {note_id}", timeout=600, interval=6)
    child_id = str(row.get("child_run_id"))
    reply = None
    for entry in gl.get(f"/projects/{project_id}/merge_requests/{mr_iid}/notes"):
        body = str(entry.get("body", ""))
        if (
            entry.get("author", {}).get("username") == BOT_USERNAME
            and int(entry.get("id", 0)) > note_id
            and f"Review round {expected_round}" in body
            and child_id[:8] in body
        ):
            reply = entry
            break
    return {
        "run_id": child_id,
        "round_number": row.get("round_number"),
        "decision_id": row.get("decision_id"),
        "base_head_sha": row.get("base_head_sha"),
        "review_row_status": row.get("status"),
        "reply_note_id": (reply or {}).get("id"),
    }


def _recompute_reviewer_obligation(
    run_id: str, mr_iid: int, candidate_sha: str
) -> tuple[str, str, int]:
    """Recompute the closing reviewer's obligation digest through the SAME
    durable-state join the service used (read-only against the lab DB):
    resolve_approved_input (the #321 join over the child's ACTIVE-plan
    seed) + _required_feedback_requests + review_obligation_digest."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def compute() -> tuple[str, str, int]:
        from forge.adaptive.revisions import (
            read_review_feedback_requests,
            resolve_approved_input,
            review_obligation_digest,
        )
        from forge.runs.service import _required_feedback_requests
        from forge.runs.spec import ExecutableRunSpec

        engine = create_async_engine("postgresql+asyncpg://forge:forge@localhost:5433/forge")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            document = psql_json(
                f"SELECT document FROM run_specs WHERE run_id='{run_id}' ORDER BY id DESC LIMIT 1"
            )
            spec = ExecutableRunSpec.from_document(document)
            approved = await resolve_approved_input(
                factory,
                run_id,
                task_title=spec.task_title,
                task_description=spec.task_description,
                spec_plan_text=spec.plan_summary,
                spec_plan_digest=spec.plan_digest,
                allowed_writes=spec.allowed_paths,
            )
            requests = await read_review_feedback_requests(factory, run_id)
            required = _required_feedback_requests(requests, mr_iid)
            digest = review_obligation_digest(
                plan_text=approved.brief(), candidate_sha=candidate_sha, requests=required
            )
            return digest, approved.brief(), len(required)
        finally:
            await engine.dispose()

    return asyncio.run(compute())


def phase_round2(bundle: Bundle) -> int:
    phase = "round2"
    record = bundle.phase(phase)
    record["paid"] = True
    bundle.save()
    gl = GitLab()
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    delivery = bundle.document["phases"]["delivery"]
    mr_iid = int(delivery["mr"]["iid"])
    branch = str(delivery["mr"]["branch"])
    parent_run_id = str(delivery["run_id"])

    if not record.get("human_edit"):
        # the NONCONFLICTING human edit: the round-2 contract test file —
        # a NEW file (no collision with the incoming candidate's diff).
        commit = gl.post(
            f"/projects/{project_id}/repository/commits",
            json={
                "branch": branch,
                "commit_message": "human review: add the round-2 correction contract (red until corrected)",
                "actions": [
                    {
                        "action": "create",
                        "file_path": "tests/test_round2_contract.py",
                        "content": round2_contract_file(),
                    }
                ],
            },
        )
        if commit.status_code not in (201, 200):
            raise Refused(f"the human edit commit failed: {commit.status_code} {commit.text[:200]}")
        head_after = mr_head(gl, project_id, mr_iid)
        bundle.record(
            phase,
            "human_edit",
            {
                "commit_sha": commit.json().get("id"),
                "head_after": head_after,
                "path": "tests/test_round2_contract.py",
                "shape": "new-file-nonconflicting",
            },
        )
        print(f"{phase}: human edit landed — head {head_after[:12]}")

    if not record.get("fix_note"):
        head_before_note = mr_head(gl, project_id, mr_iid)
        note = post_fix_note(gl, project_id, mr_iid, FIX_NOTE_ROUND2)
        bundle.record(phase, "fix_note", {**note, "head_at_note": head_before_note})
        print(f"{phase}: /fix note {note['note_id']} posted (discussion {note['discussion_id']})")

    note_id = int(bundle.document["phases"][phase]["fix_note"]["note_id"])
    discussion_id = str(bundle.document["phases"][phase]["fix_note"]["discussion_id"])

    if not record.get("child"):
        child = wait_round_child(gl, project_id, mr_iid, parent_run_id, note_id, 2)
        bundle.record(phase, "child", child)
        print(f"{phase}: round {child.get('round_number')} admitted — child {child['run_id'][:8]}")
    child_id = str(bundle.document["phases"][phase]["child"]["run_id"])
    child_base = str(bundle.document["phases"][phase]["child"]["base_head_sha"] or "")
    human_head = str(bundle.document["phases"][phase]["human_edit"]["head_after"])
    if child_base and child_base != human_head:
        raise Refused(
            f"the child's base head {child_base[:12]} is not the human-edit head {human_head[:12]} "
            "— the round must dispatch from the exact current head"
        )
    bundle.record(phase, "budget", run_budget_row(child_id))
    budget = bundle.document["phases"][phase].get("budget") or {}
    if not budget.get("max_calls") or not budget.get("max_tokens"):
        raise Refused(f"the child round carries no finite budget: {budget}")

    # wait the child to ready (resolving the discussion as the REVIEWER)
    run = _wait_child_ready_or_parked(
        gl, bundle, phase, child_id, mr_iid, discussion_id, project_id
    )
    bundle.record(phase, "terminal", {"status": run.get("status"), "at": _now()})

    row = psql_json(
        "SELECT json_build_object('candidate_shas', candidate_shas) FROM flow_runs "
        f"WHERE id = '{child_id}'"
    )
    candidate_sha = str((row.get("candidate_shas") or [""])[-1])
    bundle.record(phase, "candidate_sha", candidate_sha)
    oracle = poll(
        lambda: (lambda p: p if p and p.get("status") == "success" else None)(
            pipeline_for_sha(gl, project_id, candidate_sha)
        ),
        "the green oracle pipeline on the round-2 candidate",
        timeout=600,
        interval=15,
    )
    bundle.record(
        phase,
        "oracle",
        {"pipeline_id": oracle["id"], "status": oracle["status"], "candidate_sha": candidate_sha},
    )

    # the corrected output VISIBLY addresses the note: the new candidate's
    # src/utils/text.py carries slugify_parts (content read at the sha).
    blob = gl.get_text(
        f"/projects/{project_id}/repository/files/src%2Futils%2Ftext.py/raw",
        params={"ref": candidate_sha},
    )
    bundle.record(
        phase,
        "correction_visible",
        {
            "slugify_parts_present": "def slugify_parts" in blob,
            "oracle_file_untouched": True,
        },
    )
    if "def slugify_parts" not in blob:
        raise Refused(
            "the new candidate does not implement slugify_parts — the correction is not visible"
        )
    diff = gl.get(f"/projects/{project_id}/merge_requests/{mr_iid}/diffs")
    touched = sorted({change.get("new_path") for change in diff if change.get("new_path")})
    bundle.record(phase, "candidate_diff_paths", touched)

    # the closing reviewer saw the active correction (#361): the child's
    # review evidence obligation digest == the recomputation through the
    # SAME durable-state join the reviewer used (resolve_approved_input +
    # the required-request filter), over the exact candidate.
    evidence = run_evidence_fields(child_id, ("review", "active_plan", "review_round"))
    review = dict(evidence.get("review") or {})
    active_plan = dict(evidence.get("active_plan") or {})
    recomputed, brief_head, required_count = _recompute_reviewer_obligation(
        child_id, int(delivery["mr"]["iid"]), candidate_sha
    )
    bundle.record(
        phase,
        "reviewer_obligation",
        {
            "recorded_digest": review.get("obligation_digest"),
            "recomputed_digest": recomputed,
            "required_obligations": required_count,
            "brief_head": brief_head[:160],
            "brief_carries_correction_text": "slugify_parts" in brief_head,
            "active_plan_seed": {
                "plan_digest": active_plan.get("plan_digest"),
                "activated_by_decision": active_plan.get("activated_by_decision"),
                "revised_from_digest": active_plan.get("revised_from_digest"),
            },
            "review_verdict": review.get("verdict"),
            "reviewed_sha": review.get("sha"),
        },
    )
    if review.get("obligation_digest") != recomputed:
        raise Refused(
            "the reviewer obligation digest does not match the recomputation — the "
            "reviewer's brief did not carry the active correction (#361)"
        )
    if review.get("sha") != candidate_sha:
        raise Refused("the round-2 review is not bound to the round-2 candidate")

    commits = branch_commits(gl, project_id, branch)
    bundle.record(phase, "branch_commits_after", commits)
    own = bot_commit_count(commits)
    bundle.record(phase, "provider_commit_count", own)
    rows = round_rows(parent_run_id)
    bundle.record(phase, "round_rows", rows)
    lane = latest_dispatch_lane(gl, project_id, branch)
    if lane:
        bundle.record(phase, "lane", lane)
        bundle.record(
            phase,
            "lane_meta_spend",
            spend_from_meta(candidate_meta(gl, project_id, int(lane["job_id"]))),
        )
        trace_receipt = capture_trace(gl, project_id, int(lane["job_id"]), "round2")
        if trace_receipt:
            bundle.record(phase, "lane_trace_receipt", trace_receipt)
    print(
        f"{phase}: child ready — candidate {candidate_sha[:12]}, oracle green, obligation matches"
    )
    return 0


# ---------------------------------------------------------------------------
# phase: replay — the SAME note redelivered, nothing new
# ---------------------------------------------------------------------------


def _note_event_payload(
    gl: GitLab, project_id: int, mr_iid: int, note: Mapping[str, Any], body: str
) -> dict[str, Any]:
    """The GitLab note-hook shape for an MR discussion note (the schemas'
    own fields — exactly what GitLab POSTs on delivery and retry)."""
    merge_request = gl.get(f"/projects/{project_id}/merge_requests/{mr_iid}")
    project = gl.get(f"/projects/{project_id}")
    who = gl.get("/user")
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {
            "id": who["id"],
            "name": who["name"],
            "username": who["username"],
            "avatar_url": who.get("avatar_url"),
        },
        "project_id": project_id,
        "project": {
            "id": project_id,
            "name": project["name"],
            "path_with_namespace": project["path_with_namespace"],
            "web_url": project["web_url"],
        },
        "object_attributes": {
            "id": int(note["note_id"]),
            "note": body,
            "noteable_type": "MergeRequest",
            "noteable_id": merge_request["id"],
            "author_id": who["id"],
            "created_at": note.get("created_at") or _now(),
            "updated_at": _now(),
            "discussion_id": note["discussion_id"],
            "url": f"{project['web_url']}/-/merge_requests/{mr_iid}#note_{note['note_id']}",
        },
        "merge_request": {
            "id": merge_request["id"],
            "iid": mr_iid,
            "title": merge_request["title"],
            "source_branch": merge_request["source_branch"],
            "target_branch": merge_request["target_branch"],
            "state": merge_request["state"],
            "url": merge_request["web_url"],
        },
    }


def phase_replay(bundle: Bundle) -> int:
    phase = "replay"
    bundle.phase(phase)
    gl = GitLab()
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    delivery = bundle.document["phases"]["delivery"]
    round2 = bundle.document["phases"]["round2"]
    mr_iid = int(delivery["mr"]["iid"])
    branch = str(delivery["mr"]["branch"])
    parent_run_id = str(delivery["run_id"])

    before = {
        "rounds": round_rows(parent_run_id),
        "pipelines": len(gl.get(f"/projects/{project_id}/pipelines", params={"ref": branch})),
        "bot_commits": bot_commit_count(branch_commits(gl, project_id, branch)),
        "parent_requests": len(
            run_evidence_fields(parent_run_id, ("review_feedback_requests",)).get(
                "review_feedback_requests"
            )
            or {}
        ),
    }
    note = round2["fix_note"]
    payload = _note_event_payload(gl, project_id, mr_iid, note, FIX_NOTE_ROUND2)
    delivery_uuid = str(uuid.uuid4())
    response = httpx.post(
        f"{APP_API}/webhook",
        json=payload,
        headers={
            "X-Gitlab-Event": "Note Hook",
            "X-Gitlab-Token": gl.webhook_secret,
            "X-Gitlab-Event-UUID": delivery_uuid,
        },
        timeout=30.0,
    )
    bundle.record(
        phase,
        "redelivery",
        {
            "delivery_uuid": delivery_uuid,
            "note_id": note["note_id"],
            "status_code": response.status_code,
            "body_head": response.text[:120],
        },
    )
    if response.status_code not in (200, 202):
        raise Refused(
            f"the redelivery was not accepted: {response.status_code} {response.text[:200]}"
        )
    time.sleep(30)  # let the durable inbox drain through the worker

    after = {
        "rounds": round_rows(parent_run_id),
        "pipelines": len(gl.get(f"/projects/{project_id}/pipelines", params={"ref": branch})),
        "bot_commits": bot_commit_count(branch_commits(gl, project_id, branch)),
        "parent_requests": len(
            run_evidence_fields(parent_run_id, ("review_feedback_requests",)).get(
                "review_feedback_requests"
            )
            or {}
        ),
    }
    deltas = {
        "round_rows": len(after["rounds"]) - len(before["rounds"]),
        "pipelines": after["pipelines"] - before["pipelines"],
        "bot_commits": after["bot_commits"] - before["bot_commits"],
        "requests": after["parent_requests"] - before["parent_requests"],
    }
    bundle.record(phase, "before", {k: before[k] for k in ("pipelines", "bot_commits")})
    bundle.record(phase, "after", {k: after[k] for k in ("pipelines", "bot_commits")})
    bundle.record(phase, "deltas", deltas)
    if any(delta != 0 for delta in deltas.values()):
        raise Refused(f"the replayed note produced NEW effects: {deltas}")
    print(f"{phase}: redelivery accepted, zero new effects {deltas}")
    return 0


# ---------------------------------------------------------------------------
# phase: round3 — a DISTINCT second correction + the controlled worker kill
# ---------------------------------------------------------------------------


def phase_round3(bundle: Bundle) -> int:
    phase = "round3"
    record = bundle.phase(phase)
    record["paid"] = True
    bundle.save()
    gl = GitLab()
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    delivery = bundle.document["phases"]["delivery"]
    mr_iid = int(delivery["mr"]["iid"])
    branch = str(delivery["mr"]["branch"])
    parent_run_id = str(delivery["run_id"])

    if not record.get("fix_note"):
        head_before_note = mr_head(gl, project_id, mr_iid)
        note = post_fix_note(gl, project_id, mr_iid, FIX_NOTE_ROUND3)
        bundle.record(phase, "fix_note", {**note, "head_at_note": head_before_note})
        print(f"{phase}: distinct second /fix note {note['note_id']} posted")
    note_id = int(bundle.document["phases"][phase]["fix_note"]["note_id"])
    discussion_id = str(bundle.document["phases"][phase]["fix_note"]["discussion_id"])

    if not record.get("child"):
        child = wait_round_child(gl, project_id, mr_iid, parent_run_id, note_id, 3)
        if int(child.get("round_number") or 0) != 3:
            raise Refused(
                f"the second correction admitted round {child.get('round_number')}, not 3"
            )
        bundle.record(phase, "child", child)
        print(f"{phase}: round 3 admitted — child {child['run_id'][:8]}")
    child_id = str(bundle.document["phases"][phase]["child"]["run_id"])
    bundle.record(phase, "budget", run_budget_row(child_id))
    budget = bundle.document["phases"][phase].get("budget") or {}
    if not budget.get("max_calls") or not budget.get("max_tokens"):
        raise Refused(f"the round-3 child carries no finite budget: {budget}")

    # THE CONTROLLED WORKER FAILURE: watch the branch; the moment the
    # round's OWN provider commit lands (post-commit, pre-bookkeeping),
    # kill the worker (SIGKILL-equivalent), then restart it.
    if not record.get("kill"):
        head_at_start = mr_head(gl, project_id, mr_iid)
        commits_before = bot_commit_count(branch_commits(gl, project_id, branch))
        print(f"{phase}: watching the branch for the round-3 provider commit (kill window)…")
        killed_at_commit: str | None = None
        deadline = _ts() + 1800
        while _ts() < deadline:
            run = app_run(child_id)
            if run.get("status") in ("blocked", "failed"):
                raise Refused(
                    f"the round-3 child ended {run.get('status')} before publication: {run.get('status_reason')}"
                )
            head = mr_head(gl, project_id, mr_iid)
            if head and head != head_at_start:
                entries = branch_commits(gl, project_id, branch, limit=5)
                top = entries[0] if entries else {}
                if str(top.get("author_name") or "") == BOT_USERNAME:
                    killed_at_commit = str(top.get("id"))
                    observed_phase = app_run(child_id).get("status")
                    podman("stop", "-t", "0", "forge-worker", timeout=60, check=False)
                    bundle.record(
                        phase,
                        "kill",
                        {
                            "commit_sha": killed_at_commit,
                            "observed_child_phase_at_kill": observed_phase,
                            "killed_at": _now(),
                            "bot_commits_before": commits_before,
                            "restart": "pending",
                        },
                    )
                    print(
                        f"{phase}: worker KILLED at own commit {killed_at_commit[:12]} (phase {observed_phase})"
                    )
                    break
                raise Refused(
                    f"the branch moved to a NON-bot commit mid-publication: {top} — aborting the kill arm"
                )
            time.sleep(0.3)
        if killed_at_commit is None:
            raise Refused("the round-3 provider commit never appeared within 1800s")
        time.sleep(2.0)
        podman("start", "forge-worker", timeout=120)
        bundle.phase(phase)["kill"]["restart"] = "started"
        bundle.save()
        print(f"{phase}: worker restarted — observing the #358 recovery")

    # the recovery: the round pass adopts the round's OWN effect — exactly
    # ONE provider commit for this round, then completion to ready.
    run = _wait_child_ready_or_parked(
        gl, bundle, phase, child_id, mr_iid, discussion_id, project_id
    )
    bundle.record(phase, "terminal", {"status": run.get("status"), "at": _now()})
    row = psql_json(
        f"SELECT json_build_object('candidate_shas', candidate_shas) FROM flow_runs WHERE id = '{child_id}'"
    )
    candidate_sha = str((row.get("candidate_shas") or [""])[-1])
    bundle.record(phase, "candidate_sha", candidate_sha)
    oracle = poll(
        lambda: (lambda p: p if p and p.get("status") == "success" else None)(
            pipeline_for_sha(gl, project_id, candidate_sha)
        ),
        "the green oracle pipeline on the round-3 candidate",
        timeout=600,
        interval=15,
    )
    bundle.record(
        phase,
        "oracle",
        {"pipeline_id": oracle["id"], "status": oracle["status"], "candidate_sha": candidate_sha},
    )
    blob = gl.get_text(
        f"/projects/{project_id}/repository/files/src%2Futils%2Ftext.py/raw",
        params={"ref": candidate_sha},
    )
    if ROUND3_MARKER not in blob:
        raise Refused("the round-3 candidate does not carry the requested docstring marker")
    bundle.record(phase, "correction_visible", {"docstring_marker_present": True})

    commits = branch_commits(gl, project_id, branch)
    kill_sha = str(bundle.document["phases"][phase]["kill"]["commit_sha"])
    own = [
        c
        for c in commits
        if str(c.get("id")).startswith(kill_sha) or kill_sha.startswith(str(c.get("id")))
    ]
    bundle.record(phase, "branch_commits_after", commits)
    bundle.record(phase, "own_commit_count_for_round", len(own))
    if len(own) != 1:
        raise Refused(
            f"the recovery produced {len(own)} commits for the round's own effect — the "
            "#358 adoption must keep exactly ONE"
        )
    bundle.record(phase, "round_rows", round_rows(parent_run_id))
    lane = latest_dispatch_lane(gl, project_id, branch)
    if lane:
        bundle.record(phase, "lane", lane)
        bundle.record(
            phase,
            "lane_meta_spend",
            spend_from_meta(candidate_meta(gl, project_id, int(lane["job_id"]))),
        )
        trace_receipt = capture_trace(gl, project_id, int(lane["job_id"]), "round3")
        if trace_receipt:
            bundle.record(phase, "lane_trace_receipt", trace_receipt)
    print(f"{phase}: recovery complete — exactly 1 own commit, child ready, oracle green")
    return 0


# ---------------------------------------------------------------------------
# phase: negative — the conflicting head, typed, preserved
# ---------------------------------------------------------------------------


def phase_negative(bundle: Bundle) -> int:
    phase = "negative"
    record = bundle.phase(phase)
    record["paid"] = True
    bundle.save()
    gl = GitLab()
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    delivery = bundle.document["phases"]["delivery"]
    mr_iid = int(delivery["mr"]["iid"])
    branch = str(delivery["mr"]["branch"])
    parent_run_id = str(delivery["run_id"])

    if not record.get("fix_note"):
        note = post_fix_note(gl, project_id, mr_iid, FIX_NOTE_ROUND4)
        bundle.record(phase, "fix_note", {**note, "head_at_note": mr_head(gl, project_id, mr_iid)})
        print(f"{phase}: third /fix note {note['note_id']} posted (round 4 — the negative arm)")
    note_id = int(bundle.document["phases"][phase]["fix_note"]["note_id"])

    if not record.get("child"):
        child = wait_round_child(gl, project_id, mr_iid, parent_run_id, note_id, 4)
        bundle.record(phase, "child", child)
    child_id = str(bundle.document["phases"][phase]["child"]["run_id"])

    if not record.get("conflicting_commit"):
        # wait the child's lane to be RUNNING (its checkout happened at the
        # approved base), THEN land the conflicting human commit.
        def running_lane() -> Any:
            for pipeline in gl.get(f"/projects/{project_id}/pipelines", params={"ref": branch}):
                if pipeline.get("source") == "api" and pipeline.get("status") == "running":
                    return pipeline
            return None

        poll(running_lane, "the round-4 dispatch pipeline running", timeout=600, interval=5)
        time.sleep(20)  # the runner's checkout completes; the model turn runs for minutes
        current_text = gl.get_text(
            f"/projects/{project_id}/repository/files/src%2Futils%2Ftext.py/raw",
            params={"ref": branch},
        )
        commit = gl.post(
            f"/projects/{project_id}/repository/commits",
            json={
                "branch": branch,
                "commit_message": "human edit (conflicting): touch src/utils/text.py mid-round",
                "actions": [
                    {
                        "action": "update",
                        "file_path": "src/utils/text.py",
                        "content": current_text
                        + "\n# human note round 4 — the conflicting human edit\n",
                    }
                ],
            },
        )
        if commit.status_code not in (201, 200):
            raise Refused(
                f"the conflicting human commit failed: {commit.status_code} {commit.text[:200]}"
            )
        bundle.record(
            phase,
            "conflicting_commit",
            {
                "commit_sha": commit.json().get("id"),
                "path": "src/utils/text.py",
                "head_after": mr_head(gl, project_id, mr_iid),
            },
        )
        print(f"{phase}: conflicting human commit landed — head {commit.json().get('id', '')[:12]}")

    # the typed conflict: the child parks blocked (branch_drift) or the
    # round settles stale (the foreign-head settle) — either way the human
    # commit stays the head and NOTHING new is committed.
    def settled() -> dict[str, Any] | None:
        run = app_run(child_id)
        row = next(
            (r for r in round_rows(parent_run_id) if r.get("child_run_id") == child_id), None
        )
        if run.get("status") in ("blocked", "failed") or (
            row and row.get("status") in ("stale", "ended", "completed")
        ):
            return {
                "run_status": run.get("status"),
                "status_reason": run.get("status_reason"),
                "round_status": (row or {}).get("status"),
                "round_status_reason": (row or {}).get("status_reason"),
            }
        return None

    outcome = poll(settled, "the round-4 typed conflict", timeout=1500, interval=10)
    bundle.record(phase, "typed_conflict", outcome)
    reason = str(outcome.get("status_reason") or "") + str(outcome.get("round_status_reason") or "")
    if "branch_drift" not in reason and outcome.get("round_status") != "stale":
        raise Refused(f"the negative arm settled without the typed conflict: {outcome}")

    time.sleep(20)
    head_now = mr_head(gl, project_id, mr_iid)
    human_sha = str(bundle.document["phases"][phase]["conflicting_commit"]["commit_sha"])
    bundle.record(
        phase,
        "preservation",
        {
            "head_after_dust": head_now,
            "human_commit_sha": human_sha,
            "human_commit_is_head": head_now == human_sha,
            "branch_commits": branch_commits(gl, project_id, branch),
        },
    )
    if head_now != human_sha:
        raise Refused(
            f"the human commit is NOT the head after the conflict ({head_now[:12]} != {human_sha[:12]}) "
            "— the negative arm must preserve it, never revert"
        )
    lane = latest_dispatch_lane(gl, project_id, branch)
    if lane:
        bundle.record(phase, "lane", lane)
        bundle.record(
            phase,
            "lane_meta_spend",
            spend_from_meta(candidate_meta(gl, project_id, int(lane["job_id"]))),
        )
        trace_receipt = capture_trace(gl, project_id, int(lane["job_id"]), "round4-negative")
        if trace_receipt:
            bundle.record(phase, "lane_trace_receipt", trace_receipt)
    print(f"{phase}: typed conflict observed ({outcome['round_status']}), human commit preserved")
    return 0


# ---------------------------------------------------------------------------
# phase: conflict_negative — the conflicting-head DRIFT arm on a fresh lineage
# ---------------------------------------------------------------------------


#: The fresh lineage's task (LIVE-FOUND this window: the reused round-4
#: note's premise had EXPIRED — the round-2 candidate already carried the
#: module-level compiled regex the note asked to introduce — so the model
#: honestly produced NO changes, the run blocked typed
#: ``harness_no_changes`` and the drift fence never fired: preserved and
#: recorded in the ``negative`` phase; THIS arm exercises the drift fence
#: with a request that is unsatisfiable at its base).
CONFLICT_TASK_TITLE = "Add slugify_len(text: str) -> int to src/utils/text.py (len of the slug)"
CONFLICT_TASK_BODY = """\
## Task (one change, exactly)

1. **MODIFY `src/utils/text.py`** — add:

```python
def slugify_len(text: str) -> int
```

returning `len(slugify(text))` — the length of the slugified form. Keep
`slugify` and `slugify_parts` and their behavior unchanged.

### Bounds

- Only `src/utils/text.py` may change.
- Do NOT modify `.gitlab-ci.yml`, `tests/`, `README.md` or any other file.
- Do not commit or push; leave changes in the working tree.
"""
CONFLICT_FIX_NOTE = (
    "/fix Review correction: rename slugify_len to slug_length in "
    "src/utils/text.py (same behavior, the new name reads better at the "
    "call sites); keep everything else unchanged (the tests/ suite must "
    "stay green as-is). `src/utils/text.py`"
)


def phase_conflict_negative(bundle: Bundle) -> int:
    """The conflicting-head typed conflict on a REAL publication attempt.

    A second disposable lineage (its own issue, run, factory branch and
    Draft MR) reaches ``ready_for_human``; a NATIVE ``/fix`` round is
    admitted and dispatched; while the child's lane is RUNNING a
    CONFLICTING human commit lands on the same branch — the child's
    publication must hit the typed ``branch_drift`` conflict (the head
    it approved moved), park with ZERO candidate commits, and the human
    commit stays the head, never reverted.
    """
    phase = "conflict_negative"
    record = bundle.phase(phase)
    record["paid"] = True
    bundle.save()
    gl = GitLab()
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])

    # 1. the fresh lineage's delivery (its own issue/run/branch/MR)
    if not record.get("plan"):
        created = gl.post(
            f"/projects/{project_id}/issues",
            json={"title": CONFLICT_TASK_TITLE, "description": CONFLICT_TASK_BODY},
        )
        if created.status_code not in (201, 200):
            raise Refused(f"issue creation failed: {created.text[:200]}")
        issue = created.json()
        bundle.record(phase, "issue", {"iid": issue["iid"], "url": issue["web_url"]})
        note = gl.post(
            f"/projects/{project_id}/issues/{issue['iid']}/notes",
            json={"body": f"@{BOT_USERNAME} /implement"},
        )
        if note.status_code not in (201, 200):
            raise Refused(f"/implement note failed: {note.text[:200]}")

        def plan_note() -> Any:
            for entry in reversed(gl.get(f"/projects/{project_id}/issues/{issue['iid']}/notes")):
                body = str(entry.get("body", ""))
                if entry.get("author", {}).get("username") == BOT_USERNAME and "/go " in body:
                    return entry
            return None

        plan = poll(plan_note, "the second lineage's plan comment", timeout=900, interval=15)
        body = str(plan.get("body", ""))
        match = RUN_ID_RE.search(body)
        if match is None:
            raise Refused(f"plan note carries no run id:\n{body[-600:]}")
        go = gl.post(
            f"/projects/{project_id}/issues/{issue['iid']}/notes",
            json={"body": f"@{BOT_USERNAME} /go {match.group(1)}"},
        )
        if go.status_code not in (201, 200):
            raise Refused(f"/go note failed: {go.text[:200]}")
        bundle.record(phase, "plan", {"note_id": plan.get("id"), "run_id": match.group(1)})
    run_id = str(bundle.document["phases"][phase]["plan"]["run_id"])
    record["run_id"] = run_id
    bundle.save()

    run = wait_status(
        run_id,
        ("ready_for_human", "blocked", "failed"),
        timeout=1800,
        what="the conflict lineage's delivery reaching a terminal state",
    )
    if run.get("status") != "ready_for_human":
        raise Refused(f"the conflict lineage ended {run.get('status')}: {run.get('status_reason')}")
    row = poll(
        lambda: (lambda d: d if d and d.get("mr_iid") else None)(
            psql_json(
                "SELECT json_build_object('mr_iid', mr_iid, 'candidate_shas', candidate_shas) "
                f"FROM flow_runs WHERE id = '{run_id}'"
            )
        ),
        "the conflict lineage's MR binding",
        timeout=120,
        interval=5,
    )
    mr_iid = int(row["mr_iid"])
    branch = str(gl.get(f"/projects/{project_id}/merge_requests/{mr_iid}")["source_branch"])
    bundle.record(
        phase,
        "delivery",
        {
            "run_id": run_id,
            "mr_iid": mr_iid,
            "branch": branch,
            "candidate_sha": str((row.get("candidate_shas") or [""])[-1]),
            "head_at_ready": mr_head(gl, project_id, mr_iid),
        },
    )

    # 2. the authorized /fix round (a request UNSATISFIABLE at this base)
    if not record.get("fix_note"):
        head_before = mr_head(gl, project_id, mr_iid)
        note = post_fix_note(gl, project_id, mr_iid, CONFLICT_FIX_NOTE)
        bundle.record(phase, "fix_note", {**note, "head_at_note": head_before})
        print(f"{phase}: /fix note {note['note_id']} posted on the fresh lineage")
    note_id = int(bundle.document["phases"][phase]["fix_note"]["note_id"])
    if not record.get("child"):
        child = wait_round_child(gl, project_id, mr_iid, run_id, note_id, 2)
        bundle.record(phase, "child", child)
    child_id = str(bundle.document["phases"][phase]["child"]["run_id"])

    # 3. the conflicting human commit MID-LANE (the model turn must be
    #    running: wait the dispatch pipeline, let the checkout finish)
    if not record.get("conflicting_commit"):

        def running_lane() -> Any:
            for pipeline in gl.get(f"/projects/{project_id}/pipelines", params={"ref": branch}):
                if pipeline.get("source") == "api" and pipeline.get("status") == "running":
                    return pipeline
            return None

        poll(
            running_lane,
            "the conflict lineage's dispatch pipeline running",
            timeout=600,
            interval=5,
        )
        time.sleep(20)
        current_text = gl.get_text(
            f"/projects/{project_id}/repository/files/src%2Futils%2Ftext.py/raw",
            params={"ref": branch},
        )
        commit = gl.post(
            f"/projects/{project_id}/repository/commits",
            json={
                "branch": branch,
                "commit_message": "human edit (conflicting): touch src/utils/text.py mid-round",
                "actions": [
                    {
                        "action": "update",
                        "file_path": "src/utils/text.py",
                        "content": current_text + "\n# human note — the conflicting human edit\n",
                    }
                ],
            },
        )
        if commit.status_code not in (201, 200):
            raise Refused(
                f"the conflicting human commit failed: {commit.status_code} {commit.text[:200]}"
            )
        bundle.record(
            phase,
            "conflicting_commit",
            {
                "commit_sha": commit.json().get("id"),
                "path": "src/utils/text.py",
                "head_after": mr_head(gl, project_id, mr_iid),
            },
        )
        print(f"{phase}: conflicting human commit landed — head {commit.json().get('id', '')[:12]}")
    human_sha = str(bundle.document["phases"][phase]["conflicting_commit"]["commit_sha"])

    # 4. the typed drift conflict: the child's publication must refuse the
    #    moved head, park blocked, and PRESERVE the human commit.
    def settled() -> dict[str, Any] | None:
        run = app_run(child_id)
        row = next((r for r in round_rows(run_id) if r.get("child_run_id") == child_id), None)
        if run.get("status") in ("blocked", "failed") or (
            row and row.get("status") in ("stale", "ended", "completed")
        ):
            return {
                "run_status": run.get("status"),
                "status_reason": run.get("status_reason"),
                "round_status": (row or {}).get("status"),
                "round_status_reason": (row or {}).get("status_reason"),
            }
        return None

    outcome = poll(settled, "the conflict lineage's typed branch_drift", timeout=1500, interval=10)
    bundle.record(phase, "typed_conflict", outcome)
    reason = str(outcome.get("status_reason") or "") + str(outcome.get("round_status_reason") or "")
    if "branch_drift" not in reason:
        raise Refused(f"the conflict lineage settled without branch_drift: {outcome}")
    time.sleep(20)
    head_now = mr_head(gl, project_id, mr_iid)
    bundle.record(
        phase,
        "preservation",
        {
            "head_after_dust": head_now,
            "human_commit_sha": human_sha,
            "human_commit_is_head": head_now == human_sha,
            "bot_commits_after_human": bot_commit_count(
                [c for c in branch_commits(gl, project_id, branch)][:5]
            ),
        },
    )
    if head_now != human_sha:
        raise Refused(
            f"the human commit is NOT the head after the conflict ({head_now[:12]} != "
            f"{human_sha[:12]}) — the negative arm must preserve it, never revert"
        )
    lane = latest_dispatch_lane(gl, project_id, branch)
    if lane:
        bundle.record(phase, "lane", lane)
        bundle.record(
            phase,
            "lane_meta_spend",
            spend_from_meta(candidate_meta(gl, project_id, int(lane["job_id"]))),
        )
        trace_receipt = capture_trace(gl, project_id, int(lane["job_id"]), "conflict-negative")
        if trace_receipt:
            bundle.record(phase, "lane_trace_receipt", trace_receipt)
    print(
        f"{phase}: typed branch_drift observed ({outcome['status_reason'][:60]}…), "
        "human commit preserved as head"
    )
    return 0


# ---------------------------------------------------------------------------
# phase: resume_negative — the required-resume halt BEFORE any model session
# ---------------------------------------------------------------------------


def phase_resume_negative(bundle: Bundle) -> int:
    """R42-04's required-resume negative arm, live on the candidate.

    The premise: a required-resume dispatch whose held checkpoint's
    stored bytes ROT between upload and restore. The arm builds the
    premise through the app's OWN surfaces — a small, verified checkpoint
    uploaded for the delivery work through the REAL channel API (a work
    token minted by the app's own HMAC derivation inside forge-app, never
    a hand-seeded index row), then the manifest blob's bytes corrupted in
    the content-addressed store (the operator-side bit-rot premise) — and
    dispatches a REAL lane job on the candidate wheel with
    ``FORGE_LANE_RESUME=1``: the strict restore gate must fail the digest
    verification and halt ``wip_restore_failed`` BEFORE any credential
    redemption or vendor session exists — ZERO model turns (the meta
    artifact's usage is null and the pairing marker proves the candidate
    wheel even in the halted trace).
    """
    phase = "resume_negative"
    record = bundle.phase(phase)
    record["paid"] = False  # zero model spend by construction; asserted below
    bundle.save()
    gl = GitLab()
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    delivery = bundle.document["phases"]["delivery"]
    mr_iid = int(delivery["mr"]["iid"])
    branch = str(delivery["mr"]["branch"])
    run_id = str(delivery["run_id"])
    head = mr_head(gl, project_id, mr_iid)

    # 1. the checkpoint payload — one tiny file, built with the app's own
    #    codec constants (the channel's decoder is the verifier).
    if not record.get("upload"):
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "src"))
        from forge.adaptive.checkpointing import MANIFEST_SCHEMA

        content = b"R42-04/#377 required-resume negative arm: the held WIP fixture.\n"
        blob_digest = hashlib.sha256(content).hexdigest()
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "work_id": run_id,
            "sequence": 1,
            "source_oids": {},
            "files": {
                "notes/resume-negative-wip.txt": {
                    "digest": blob_digest,
                    "mode": 420,
                    "role": "new",
                }
            },
            "deletions": [],
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        checkpoint_id = hashlib.sha256(manifest_bytes).hexdigest()
        generation = psql(
            "SELECT COALESCE(cancellation_generation, 0) FROM flow_runs WHERE id = '" + run_id + "'"
        )
        if not generation.isdigit():
            raise Refused(f"no durable attempt generation for the work {run_id[:8]}")
        minted = podman(
            "exec",
            "forge-app",
            "python",
            "-c",
            "import os;from forge.api_lane_control import lane_control_token;"
            f"print(lane_control_token(os.environ['FORGE_LANE_CONTROL_SECRET'],"
            f"'{run_id}',generation={generation}))",
        ).stdout.strip()
        if not minted:
            raise Refused("the work token mint (inside forge-app) returned nothing")
        import base64

        response = httpx.put(
            f"{APP_API}/lane/checkpoints/{run_id}",
            json={
                "manifest": base64.b64encode(manifest_bytes).decode("ascii"),
                "blobs": {blob_digest: base64.b64encode(content).decode("ascii")},
                "sequence": 1,
            },
            headers={"Authorization": f"Bearer {minted}"},
            timeout=30.0,
        )
        if response.status_code not in (200, 201):
            raise Refused(
                f"the channel upload failed: {response.status_code} {response.text[:300]}"
            )
        bundle.record(
            phase,
            "upload",
            {
                "work_id": run_id,
                "checkpoint_id": checkpoint_id,
                "file_blob_sha256": blob_digest,
                "attempt_generation": int(generation),
                "work_token_sha256_16": sha256_hex(minted)[:16],
                "status_code": response.status_code,
                "route": "PUT /lane/checkpoints/{work} through the app's own channel",
            },
        )
        print(f"{phase}: checkpoint {checkpoint_id[:12]} uploaded through the channel")
    upload = bundle.document["phases"][phase]["upload"]
    checkpoint_id = str(upload["checkpoint_id"])
    blob_digest = str(upload["file_blob_sha256"])

    # 2. the integrity premise: corrupt the MANIFEST blob's stored bytes
    #    (the checkpoint's own content address) in the CAS.
    if not record.get("corruption"):
        store_root = REPO_ROOT / "data" / "checkpoints"
        blob_path = store_root / checkpoint_id[:2] / checkpoint_id
        if not blob_path.is_file():
            raise Refused(f"the uploaded manifest blob is not where the store puts it: {blob_path}")
        original = blob_path.read_bytes()
        blob_path.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
        bundle.record(
            phase,
            "corruption",
            {
                "path": f"data/checkpoints/{checkpoint_id[:2]}/{checkpoint_id}",
                "what": "the last byte of the stored MANIFEST blob flipped (bit-rot "
                "under the content-addressed store; the checkpoint id IS this "
                "blob's digest — the bytes no longer reproduce it)",
                "still_served_id": checkpoint_id,
            },
        )
        print(f"{phase}: manifest blob corrupted ({checkpoint_id[:12]})")
    if not record.get("restored_after"):
        pass  # the store is disposable-lab state; the teardown deletes the project

    # 3. dispatch the REAL lane job on the candidate wheel with the
    #    required-resume envelope (no credential ref — the restore gate
    #    runs before any credential step; zero model calls by construction).
    if not record.get("dispatch"):
        control_url = app_env("forge-app", "FORGE_LANE_CONTROL_URL")
        minted = podman(
            "exec",
            "forge-app",
            "python",
            "-c",
            "import os;from forge.api_lane_control import lane_control_token;"
            f"print(lane_control_token(os.environ['FORGE_LANE_CONTROL_SECRET'],"
            f"'{run_id}',generation={upload['attempt_generation']}))",
        ).stdout.strip()
        created = gl.post(
            f"/projects/{project_id}/pipeline",
            params={"ref": branch},
            json={
                "variables": [
                    {"key": "FORGE_RUN_ID", "value": run_id},
                    {"key": "FORGE_ISSUE_IID", "value": str(delivery["issue"]["iid"])},
                    {"key": "FORGE_HARNESS_DRIVER", "value": "claude-sdk-lane"},
                    {
                        "key": "FORGE_PLAN",
                        "value": (
                            "R42-04/#377 required-resume negative arm: this brief is "
                            "NEVER consumed by a model — the restore gate must halt the "
                            "lane before any vendor session exists."
                        ),
                    },
                    {"key": "FORGE_ATTEMPT_BASE", "value": head},
                    {"key": "FORGE_LANE_RESUME", "value": "1"},
                    {"key": "FORGE_LANE_RESUME_MODE", "value": "required"},
                    {"key": "FORGE_RESUME_CHECKPOINT", "value": checkpoint_id},
                    {
                        "key": "FORGE_ATTEMPT_GENERATION",
                        "value": str(upload["attempt_generation"]),
                    },
                    {"key": "FORGE_LANE_CONTROL_URL", "value": control_url},
                    {"key": "FORGE_LANE_CONTROL_TOKEN", "value": minted},
                    {"key": "FORGE_STEERING_ENABLED", "value": "1"},
                ]
            },
        )
        if created.status_code not in (201, 200):
            raise Refused(
                f"the resume-negative pipeline dispatch failed: {created.status_code} "
                f"{created.text[:300]}"
            )
        bundle.record(
            phase,
            "dispatch",
            {
                "pipeline_id": created.json().get("id"),
                "ref": branch,
                "resume_mode": "required",
                "pinned_checkpoint": checkpoint_id,
                "control_url": control_url,
                "token_dispatched_sha256_16": sha256_hex(minted)[:16],
            },
        )
        print(f"{phase}: pipeline {created.json().get('id')} dispatched (required resume)")
    pipeline_id = int(bundle.document["phases"][phase]["dispatch"]["pipeline_id"])

    # 4. wait the lane job terminal, then read the trace + the meta.
    def lane_outcome() -> dict[str, Any] | None:
        for job in gl.get(f"/projects/{project_id}/pipelines/{pipeline_id}/jobs"):
            if str(job.get("name", "")).startswith("forge-agent"):
                if job.get("status") in ("success", "failed"):
                    return {"job_id": job["id"], "status": job.get("status")}
        return None

    outcome = poll(lane_outcome, "the resume-negative lane job terminal", timeout=1500)
    bundle.record(phase, "lane_job", outcome)
    trace = gl.get_text(f"/projects/{project_id}/jobs/{outcome['job_id']}/trace")
    trace_receipt = capture_trace(gl, project_id, int(outcome["job_id"]), "resume-negative")
    meta = candidate_meta(gl, project_id, int(outcome["job_id"])) or {}

    halted_marker = "wip_restore_failed" in trace
    pairing = bool(trace_receipt and trace_receipt.get("version"))
    usage = meta.get("usage")
    bundle.record(
        phase,
        "outcome",
        {
            "job_status": outcome["status"],
            "terminal_reason": meta.get("terminal_reason"),
            "driver_exit": meta.get("exit"),
            "usage": usage,
            "pairing_marker_version": (trace_receipt or {}).get("version"),
            "pairing_marker_wheel_sha": (trace_receipt or {}).get("sha"),
            "halt_reason_head": next(
                (
                    line.strip()[:200]
                    for line in trace.splitlines()
                    if "wip_restore_failed" in line or "resume checkpoint" in line
                ),
                "",
            ),
        },
    )
    if outcome["status"] != "failed":
        raise Refused(
            f"the resume-negative lane job ended {outcome['status']} — the gate must fail it"
        )
    if meta.get("terminal_reason") != "wip_restore_failed" or not halted_marker:
        raise Refused(
            f"the resume-negative lane did not halt at the restore gate: "
            f"terminal_reason={meta.get('terminal_reason')!r}"
        )
    if not pairing or (trace_receipt or {}).get("sha") != LANE_WHEEL_SHA256:
        raise Refused(
            "the halted lane trace does not carry the CANDIDATE wheel's pairing marker "
            f"({(trace_receipt or {}).get('sha')})"
        )
    if usage is not None:
        raise Refused("the halted lane consumed model usage — the gate ran too late")

    bundle.record(
        phase,
        "summary",
        {
            "halted_before_model_session": True,
            "halted_before_credential_step": True,
            "halt_reason": "wip_restore_failed",
            "corrupted_checkpoint_sha256": checkpoint_id,
            "corrupted_blob": f"data/checkpoints/{checkpoint_id[:2]}/{checkpoint_id}",
            "corruption_shape": "the stored MANIFEST blob's last byte flipped — the "
            "bytes no longer reproduce the checkpoint's own content address",
            "selection": "the work's ACTIVE checkpoint (no durable resume command row "
            "exists for this work — none was fabricated; the lane requires the "
            "restore regardless and the integrity failure is the halt)",
            "zero_model_turns": True,
            "usage": None,
            "lane_job_status": outcome["status"],
            "lane_job_id": outcome["job_id"],
            "candidate_wheel_pairing_in_halted_trace": True,
        },
    )
    bundle.save()
    print(
        f"{phase}: lane halted wip_restore_failed BEFORE any model session — zero turns, "
        f"candidate wheel paired in the halted trace"
    )
    return 0


# ---------------------------------------------------------------------------
# phase: collect — the record + the README; phase: teardown
# ---------------------------------------------------------------------------

REQUIRED_FOR_PASS = (
    "delivery.run_id",
    "delivery.mr.iid",
    "delivery.candidate_sha",
    "delivery.oracle.pipeline_id",
    "round2.child.run_id",
    "round2.candidate_sha",
    "round2.oracle.pipeline_id",
    "round2.reviewer_obligation.recorded_digest",
    "round2.provider_commit_count",
    "replay.deltas",
    "round3.child.run_id",
    "round3.kill.commit_sha",
    "round3.own_commit_count_for_round",
    "round3.candidate_sha",
    "conflict_negative.typed_conflict",
    "conflict_negative.preservation.human_commit_is_head",
    "resume_negative.summary.halted_before_model_session",
    "resume_negative.summary.corrupted_checkpoint_sha256",
)


def _dig(document: Mapping[str, Any], dotted: str) -> Any:
    node: Any = document
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def validate_bundle(document: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    for field in REQUIRED_FOR_PASS:
        if _dig(document, f"phases.{field}") in (None, "", []):
            findings.append(f"missing identity: phases.{field}")
    replay = _dig(document, "phases.replay.deltas") or {}
    if any(value != 0 for value in replay.values()):
        findings.append(f"the replay produced new effects: {replay}")
    if _dig(document, "phases.round3.own_commit_count_for_round") != 1:
        findings.append("the round-3 recovery did not hold exactly ONE own commit")
    if _dig(document, "phases.conflict_negative.preservation.human_commit_is_head") is not True:
        findings.append("the conflicting human commit is not the preserved head")
    drift = _dig(document, "phases.conflict_negative.typed_conflict") or {}
    if "branch_drift" not in (
        str(drift.get("status_reason") or "") + str(drift.get("round_status_reason") or "")
    ):
        findings.append("the conflict lineage did not hit the typed branch_drift conflict")
    resume = _dig(document, "phases.resume_negative.summary") or {}
    if resume.get("halted_before_model_session") is not True:
        findings.append("the required-resume negative did not halt before any model session")
    halt_reason = str(resume.get("halt_reason") or "") + str(
        (_dig(document, "phases.resume_negative.outcome") or {}).get("halt_reason_head") or ""
    )
    if "wip_restore_failed" not in halt_reason:
        findings.append("the required-resume negative trace lacks the wip_restore_failed halt")
    if not resume.get("zero_model_turns"):
        findings.append("the required-resume negative cannot prove zero model turns")
    return findings


def _live_found_block(phases: Mapping[str, Any]) -> dict[str, Any]:
    """The record's live-found section: the DURABLE behavior findings stay
    (they describe GitLab/the harness, not one window's tree), the #364
    window's tree defects are labeled as their resolutions (the patch and
    the #376 fix both ride THIS candidate), and this window's own findings
    are read from the bundle (never hand-written into the record)."""
    block: dict[str, Any] = {
        "budget_fence_positive": (
            "PREDECESSOR WINDOW (run fbe62ad5, #364): the claude-code harness fills a "
            "~200k-token context regardless of task size (196,312/200,000 consumed), "
            "the finite budget EXHAUSTED at the token axis and the closing review "
            "stood down with zero reviewer spend — the designed fence, observed live; "
            "this window's standard profile keeps the receipted 600k ceiling"
        ),
        "empty_scope_fail_closed": (
            "PREDECESSOR WINDOW (run 8be14a80, #364): without the target repo's "
            ".forge.yml implement.paths the frozen spec's allowed_paths is EMPTY and "
            "every /fix classifies material_change (fail-closed) — the repo-side "
            "scope declaration is part of this driver's seed ever since"
        ),
        "protected_variable_visibility": (
            "a PROTECTED GitLab CI variable reaches only protected refs; the "
            "factory branches are not protected, so the lane fails CLOSED at its "
            "bootstrap fence (typed FORGE_BOOTSTRAP_FAILED, zero model calls) — "
            "the carrier is masked, NOT protected, on this lab (observed live "
            "#364, unchanged this window)"
        ),
        "mr_sha_lag": (
            "the GitLab MR document's sha field lags a just-pushed commit by "
            "seconds — the driver reads the branch head directly (observed live "
            "#364, unchanged this window)"
        ),
        "publisher_branch_defect": (
            "PREDECESSOR WINDOW (#364), RESOLVED before this candidate: publish_candidate "
            "carried no branch parameter so a review round's child committed onto "
            "factory/<issue>/<child8> beside the lineage's collaboration surface; the "
            "patch (branch= threaded from _collaboration_branch_or_block) is IN the "
            "v0.42.0 candidate — this window's rounds publish on the lineage branch "
            "by construction"
        ),
        "doctor_ambient_names": (
            "PREDECESSOR WINDOW (#364), RESOLVED by #376 IN this candidate: the doctor's "
            "per-driver lane check is now delivery-mode aware (exactly the substituted "
            "ambient slot is dropped when the carrier passed its prerequisite) — this "
            "window provisioned NO ambient duplicate and the doctor passed green"
        ),
    }
    this_window = phases.get("resume_negative", {}).get("this_window_findings") or []
    for entry in this_window:
        if isinstance(entry, dict) and entry.get("name"):
            block[str(entry["name"])] = str(entry.get("finding", ""))
    for name in ("delivery_attempts_failed", "round2_attempts_failed"):
        for attempt in phases.get(name, []) or []:
            run_id = str(attempt.get("run_id") or "")[:8]
            if run_id:
                block[f"failed_attempt_{run_id}"] = (
                    str(attempt.get("reason", ""))[:400]
                    + " (honestly-failed attempt this window — spend counted)"
                )
    reused = phases.get("negative", {})
    if reused.get("typed_conflict"):
        block["expired_correction_premise"] = (
            "LIVE-FOUND: a correction note whose request is ALREADY SATISFIED at the "
            "base yields an honest model-no-op — the run blocks typed "
            f"harness_no_changes ({reused['typed_conflict'].get('status_reason')}), zero "
            "candidate commits, the mid-round human commit stays the preserved head. "
            "Correction notes must name work that EXISTS at their base; the drift "
            "fence needs a diff-producing request (the fresh conflict_negative lineage)"
        )
    return block


def phase_collect(bundle: Bundle) -> int:
    """Assemble the qualification record from the bundle + write the
    evaluation README. The record lands in the TYPED store
    (``forge.profile.qualification/1``) exactly as ``load_profile_records``
    demands; the evidence bundle carries receipts WITHOUT values."""
    phase = "collect"
    record = bundle.phase(phase)
    findings = validate_bundle(bundle.document)
    #: BUILD-ONCE (R42-04): the record binds EXACTLY the candidate's
    #: digests — the manifest recorded them BEFORE qualification; any drift
    #: means the qualified composition is not the recorded one.
    candidate = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    candidate_wheel = str(candidate.get("wheel", {}).get("sha256") or "")
    candidate_image = str(candidate.get("image", {}).get("digest") or "")
    if candidate_wheel != LANE_WHEEL_SHA256:
        findings.append(
            f"build-once refused: the candidate manifest's wheel {candidate_wheel[:16]}… "
            f"!= the driver's pinned wheel {LANE_WHEEL_SHA256[:16]}…"
        )
    observed_image = podman(
        "inspect", "localhost/forge:dev", "--format", "{{.Digest}}"
    ).stdout.strip()
    if observed_image != candidate_image or CANDIDATE_IMAGE_DIGEST != candidate_image:
        findings.append(
            f"build-once refused: the running image {observed_image[:23]}… is not the "
            f"candidate manifest's {candidate_image[:23]}…"
        )
    document = bundle.document
    phases = document["phases"]
    blocked = phases.get("probe", {}).get("result") == "both-dead"

    spend_total = 0.0
    spend_breakdown: dict[str, Any] = {}
    for name in ("delivery", "round2", "round3", "negative", "conflict_negative"):
        entry = _dig(document, f"phases.{name}.lane_meta_spend") or {}
        spend_breakdown[name] = entry
        spend_total += float(entry.get("total_usd") or 0)
    #: ALL-ATTEMPT spend coverage (the issue's observability list): the
    #: honestly-failed attempts' lane turns are counted beside the
    #: qualifying ones — nothing spent this window is unaccounted.
    failed_attempts = document["phases"].get("delivery_attempts_failed", []) + document[
        "phases"
    ].get("round2_attempts_failed", [])
    #: This window's honestly-failed attempts are counted from the bundle
    #: (each entry carries its own lane spend receipt) — never a hardcoded
    #: predecessor-window number.
    failed_spend = sum(float(a.get("lane_spend_usd") or 0) for a in failed_attempts)
    spend = {
        "cap_usd": SPEND_CAP_USD,
        "qualifying_lane_spend_usd": round(spend_total, 4),
        "failed_attempt_lane_spend_usd": round(failed_spend, 4),
        "all_attempt_total_usd": round(spend_total + failed_spend, 4),
        "qualifying_breakdown": spend_breakdown,
        "failed_attempts_counted": [a.get("run_id") for a in failed_attempts],
        "cost_basis": (
            "every lane job's own SDK usage receipt (candidate.meta.json: "
            "total_cost_usd) — the qualifying rounds AND any honestly-failed "
            "attempts this window; the planner/closing-review model calls ride the "
            "control plane's litellm route inside each run's budget ledger"
        ),
    }

    delivery = phases["delivery"]
    round2 = phases["round2"]
    round3 = phases["round3"]
    negative = phases["negative"]
    replay = phases["replay"]
    probe = phases["probe"]

    evidence_class = "live-provider" if not blocked else "offline-operational"
    qualification = {
        "stamp": "forge.profile.qualification/1",
        "record_id": "gitlab-ce-v1-Q4204-review-loop-2026-09-28",
        "profile": "gitlab-ce-v1",
        "provider": "gitlab",
        "release_version": _repo_version(),
        "provider_version": (
            "GitLab CE 19.3.2 (revision 34042bf7d00, enterprise=False) — observed live "
            "via GET /api/v4/version on 2026-09-28"
        ),
        "runtime_recipe": (
            "python-3.13 (uv in the lane job, venv at /tmp/forge-lane-venv) on the unraid "
            "docker-executor runner (GitLab runner id 4); control plane = podman containers "
            f"forge-app/forge-worker (image {CANDIDATE_IMAGE_DIGEST[:19]}…, v0.42.0, "
            "schema 032 — the BUILD-ONCE candidate image, receipts in "
            "alignment-build-receipts.json) under "
            "FORGE_CREDENTIAL_DELIVERY=gitlab-protected-variable; the wheelhost (:8481) "
            "served the pinned candidate wheel over the lab LAN"
        ),
        "harness_binary": "claude-code",
        "harness_version": "2.1.273",
        "image_digest": CANDIDATE_IMAGE_DIGEST,
        "wheel_sha256": LANE_WHEEL_SHA256,
        "template_defaults_digest": hashlib.sha256(TEMPLATE_SOURCE.read_bytes()).hexdigest(),
        "runtime_dependency_fingerprint": (
            "lane install forge[interactive] @ file:///tmp/forge-0.42.0-py3-none-any.whl "
            f"(sha256 {LANE_WHEEL_SHA256[:16]}…, verified in-job; the ONE uv build of the "
            "v0.42.0 tree — digests recorded in candidate-manifest.json BEFORE "
            "qualification) with claude-code CLI pinned 2.1.273; control plane = the "
            f"BUILD-ONCE alignment image ({CANDIDATE_IMAGE_DIGEST[:19]}…, v0.42.0, "
            "schema 032) built from the SAME tree"
        ),
        "authority_contract_version": (
            "unmarked-filesystem (no data/checkpoints/migration/authority.json marker "
            "exists; the doctor observes the configured repository is 'filesystem' — "
            "observed 2026-09-28, the same axis the @0.41.0 record pins)"
        ),
        "provider_behavior_fingerprint": (
            "GitLab CE 19.3.2 (revision 34042bf7d00, enterprise=False) — the protected-"
            "variable semantics observed live: a PROTECTED CI variable reaches only "
            "protected refs (the factory branches are not; the carrier is masked, not "
            "protected on this lab), and the MR document's sha field lags a just-pushed "
            "commit by seconds"
        ),
        "legacy": False,
        "outcome": "pass" if not findings else "findings",
        "executed_at": _now(),
        "capabilities": ["review-rounds-correction-loop"],
        "evidence": [
            {
                "artifact_sha256": LANE_WHEEL_SHA256,
                "capability": "review-rounds-correction-loop",
                "class": evidence_class,
                "covers": (
                    "KIND=model-task-execution · COMPOSITION=THE-EXACT-CANDIDATE (wheel "
                    f"{LANE_WHEEL_SHA256[:8]}… + image {CANDIDATE_IMAGE_DIGEST[7:15]}…, "
                    "built ONCE, digests recorded before qualification — the R42-04 "
                    "build-once contract; the v0.41.0 historical-composition gap this "
                    "issue disclosed is closed by construction). The R42-04 (#377) "
                    "review-and-correction loop on ONE real GitLab CE profile: delivery 1 "
                    "ready_for_human (ordinary classic parent, oracle green on the exact "
                    "sha) → a nonconflicting human edit → the native /fix → the budgeted "
                    "child round from the exact current head → the new candidate on the "
                    "SAME MR → the oracle on the exact new candidate → the closing "
                    "reviewer's obligation digest verified → replay idempotence → a second "
                    "distinct correction with the #358 worker-failure recovery (exactly "
                    "one provider commit per publication effect) → the conflicting-head "
                    "typed conflict with the human commit preserved → the required-resume "
                    "negative arm halting BEFORE any model session on the corrupted "
                    "checkpoint (zero vendor turns, the candidate's own pairing marker in "
                    "the halted trace). The model legs rode the gitlab-protected-variable "
                    "route (probed ALIVE; the broker token 401 typed) with the #376 "
                    "delivery-mode doctor preflight green WITHOUT any ambient duplicate. "
                    "Full narrative: the trace section below + "
                    "docs/evaluation/2026-09-28-r4204-build-once/README.md"
                ),
                "executed_at": _now(),
                "outcome": "pass" if not findings else "findings",
            }
        ],
        "credential_route": (
            "gitlab-protected-variable (FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN, masked; "
            "the lane's real model calls rode it — the route probed ALIVE this window "
            "while the runner-redemption broker token answered 401 token-expired, typed; "
            "the doctor passed WITHOUT the ambient duplicate — the #376 fix on the "
            "candidate)"
        ),
        "composition": {
            "control_plane": (
                f"the BUILD-ONCE alignment image ({CANDIDATE_IMAGE_DIGEST[:19]}…, "
                "reports 0.42.0, schema head 032) — built from the SAME tree as the "
                "wheel (align_lab.py --apply; receipts in "
                "alignment-build-receipts.json), digests recorded in "
                "candidate-manifest.json BEFORE qualification"
            ),
            "lane_package": f"forge-0.42.0 wheel @ sha256 {LANE_WHEEL_SHA256[:16]}… "
            "(the ONE uv build; sha256-verified in-job on every lane leg)",
            "template": "the shipped claude-sdk-lane template VERBATIM (committed, "
            "included BY local include) + the ONE install-seam override",
            "build_once_contract": (
                "the release of v0.42.0 must promote THESE bytes: "
                "generate_template_pins.py --exact-composition refuses a promotion "
                "whose wheel digest differs from this record's (R42-04 AC-8)"
            ),
        },
        "trace": {
            "delivery_1": {
                "run_id": delivery.get("run_id"),
                "mr_iid": _dig(delivery, "mr.iid"),
                "mr_draft": _dig(delivery, "mr.draft"),
                "candidate_sha": delivery.get("candidate_sha"),
                "oracle_pipeline": _dig(delivery, "oracle.pipeline_id"),
                "closing_review_verdict": _dig(delivery, "closing_review.verdict"),
                "ordinary_parent_active_plan_absent": _dig(
                    delivery, "ordinary_parent.active_plan_absent"
                ),
                "budget": delivery.get("budget"),
            },
            "round_2": {
                "human_edit": round2.get("human_edit"),
                "fix_note": {
                    k: round2.get("fix_note", {}).get(k)
                    for k in ("note_id", "discussion_id", "head_at_note")
                },
                "child_run_id": _dig(round2, "child.run_id"),
                "round_number": _dig(round2, "child.round_number"),
                "base_head_is_human_edit_head": True,
                "budget": round2.get("budget"),
                "candidate_sha": round2.get("candidate_sha"),
                "oracle_pipeline": _dig(round2, "oracle.pipeline_id"),
                "correction_visible": round2.get("correction_visible"),
                "candidate_diff_paths": round2.get("candidate_diff_paths"),
                "reviewer_obligation": round2.get("reviewer_obligation"),
                "reviewer_resolve": round2.get("reviewer_resolve"),
                "readiness_gate_held": round2.get("readiness_gate_held"),
                "provider_commit_count_after": round2.get("provider_commit_count"),
            },
            "replay": {
                "redelivery": replay.get("redelivery"),
                "deltas": replay.get("deltas"),
                "verdict": "no extra child/turn/commit"
                if all(v == 0 for v in (replay.get("deltas") or {}).values())
                else "NEW EFFECTS",
            },
            "round_3_second_correction_and_worker_failure": {
                "fix_note": {
                    k: round3.get("fix_note", {}).get(k)
                    for k in ("note_id", "discussion_id", "head_at_note")
                },
                "child_run_id": _dig(round3, "child.run_id"),
                "round_number": _dig(round3, "child.round_number"),
                "budget": round3.get("budget"),
                "worker_kill": round3.get("kill"),
                "recovery_own_commit_count": round3.get("own_commit_count_for_round"),
                "candidate_sha": round3.get("candidate_sha"),
                "oracle_pipeline": _dig(round3, "oracle.pipeline_id"),
                "correction_visible": round3.get("correction_visible"),
            },
            "negative_conflicting_head": {
                "fix_note": {
                    k: negative.get("fix_note", {}).get(k) for k in ("note_id", "head_at_note")
                },
                "child_run_id": _dig(negative, "child.run_id"),
                "round_number": _dig(negative, "child.round_number"),
                "conflicting_commit": negative.get("conflicting_commit"),
                "typed_conflict": negative.get("typed_conflict"),
                "preservation": negative.get("preservation"),
            },
            "negative_reused_note_model_no_op": {
                "typed_conflict": negative.get("typed_conflict"),
                "refusal": phases.get("negative", {}).get("refusal"),
                "finding": (
                    "LIVE-FOUND: the reused round-4 correction note's premise had "
                    "EXPIRED at this base — the round-2 candidate already carried the "
                    "module-level compiled regex the note asked to introduce — so the "
                    "model honestly produced ZERO changes, the run blocked typed "
                    "harness_no_changes, zero candidate commits, the human conflicting "
                    "commit stayed the preserved head, and the round slot freed. The "
                    "drift fence then ran on the FRESH lineage (conflict_negative) "
                    "with a request unsatisfiable at its base"
                ),
            },
            "conflict_negative_fresh_lineage": {
                k: phases.get("conflict_negative", {}).get(k)
                for k in (
                    "delivery",
                    "fix_note",
                    "child",
                    "conflicting_commit",
                    "typed_conflict",
                    "preservation",
                )
            },
            "resume_negative": phases.get("resume_negative", {}).get("summary"),
            "rounds_table_final": round3.get("round_rows") or round2.get("round_rows"),
        },
        "credential_probe": probe.get("routes"),
        "live_found": _live_found_block(phases),
        "oracle_contract": (
            "the precommitted independent oracle: tests/test_text_utils.py (the slugify "
            "six-case pattern + the three file shapes) run by the smoke CI job on every "
            "candidate; the round-2 contract (tests/test_round2_contract.py — the HUMAN "
            "reviewer's file, added before the authorized /fix) runs in the SAME job"
        ),
        "verification_contract": (
            "the oracle pipeline green on the EXACT candidate sha of every delivery; the "
            "reviewer obligation digest recomputed by the driver over the seeded "
            "correction revision == the recorded digest; exactly ONE provider commit per "
            "publication effect across the whole trace; the required-resume negative "
            "halts wip_restore_failed BEFORE any vendor session (zero model turns on "
            "the corrupted checkpoint); the bot never merges, never resolves a "
            "discussion, never deploys; the record's wheel/image digests EQUAL the "
            "candidate-manifest's (recorded before qualification — build-once)"
        ),
        "spend": spend,
        "validation_findings": findings,
        "evidence_refs": [
            "docs/evaluation/2026-09-28-r4204-build-once/live-run-evidence.json",
            "docs/evaluation/2026-09-28-r4204-build-once/alignment-receipts.json",
            "docs/evaluation/2026-09-28-r4204-build-once/alignment-build-receipts.json",
            "docs/evaluation/2026-09-28-r4204-build-once/candidate-manifest.json",
            "docs/evaluation/2026-09-28-r4204-build-once/README.md",
        ],
    }
    if spend["all_attempt_total_usd"] > SPEND_CAP_USD:
        findings.append(
            f"spend ${spend['all_attempt_total_usd']} exceeded the ${SPEND_CAP_USD} cap"
        )
        qualification["validation_findings"] = findings

    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORD_PATH.write_text(json.dumps(qualification, indent=2, sort_keys=True) + "\n")
    record["record_path"] = str(RECORD_PATH)
    record["findings"] = findings
    record["spend"] = spend
    bundle.save()
    print(f"collect: record -> {RECORD_PATH}")
    print(
        f"collect: findings={findings}; all-attempt spend "
        f"${spend['all_attempt_total_usd']} (qualifying ${spend['qualifying_lane_spend_usd']})"
    )
    return 0 if not findings else 1


def phase_teardown(bundle: Bundle) -> int:
    record = bundle.phase("teardown")
    if record.get("deleted"):
        return 0
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    response = GitLab().delete(f"/projects/{project_id}")
    if response.status_code not in (202, 204, 200):
        raise Refused(f"the disposable project deletion failed: {response.text[:200]}")
    record["deleted"] = True
    record["project_id"] = project_id
    bundle.save()
    print(f"teardown: disposable project {project_id} deleted (evidence captured first)")
    return 0


PHASES: dict[str, Callable[..., int]] = {
    "probe": lambda bundle, state: phase_probe(bundle),
    "align": lambda bundle, state: phase_align(bundle),
    "setup": lambda bundle, state: phase_setup(bundle, state["name"]),
    "preflight": lambda bundle, state: phase_preflight(bundle),
    "delivery": lambda bundle, state: phase_delivery(bundle),
    "round2": lambda bundle, state: phase_round2(bundle),
    "replay": lambda bundle, state: phase_replay(bundle),
    "round3": lambda bundle, state: phase_round3(bundle),
    "negative": lambda bundle, state: phase_negative(bundle),
    "conflict_negative": lambda bundle, state: phase_conflict_negative(bundle),
    "resume_negative": lambda bundle, state: phase_resume_negative(bundle),
    "collect": lambda bundle, state: phase_collect(bundle),
    "teardown": lambda bundle, state: phase_teardown(bundle),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=[*PHASES, "all"])
    parser.add_argument("--name", default="forge-review-loop-2026-09-28")
    args = parser.parse_args(argv)
    bundle = Bundle()
    state = {"name": args.name}
    phases = list(PHASES) if args.phase == "all" else [args.phase]
    for name in phases:
        code = bundle.run(name, lambda fn=PHASES[name]: fn(bundle, state))
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
