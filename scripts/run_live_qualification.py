"""R37-08 (#289) — ONE live single-writer qualification run, real everything.

The aligned lab (see ``scripts/align_lab.py`` + the alignment receipts) now
runs the repo's own head with numerical budget caps. This driver executes
the R37-08 acceptance trace through NATIVE surfaces only — every operator
action is a GitLab issue note by the configured approver, every dispatch
is the app's real webhook path, every lane job is a REAL runner executing
the REAL harness (claude-code 2.1.273 via ``forge.lane_driver``) against
the REAL model route (glm-5.3-flash through the z.ai Anthropic-compatible
gateway):

- ``setup`` — a DISPOSABLE project ``forge-live-qual-<date>`` seeded with
  the frozen acceptance task + its INDEPENDENT oracle (a ``smoke`` CI job
  asserting six exact slugify cases, committed BEFORE any run and never
  touched by a candidate); the lane credentials copied from the lab
  project; the forge webhook; the lane pinned to the immutable repo sha
  that carries the #288 dispatch envelope;
- ``preflight`` — the app's OWN doctor against the new project + the
  inventory's alignment axes; refuses before ANY paid call;
- ``flow`` (arm 1, paid) — native issue → ``@forge /implement`` → the
  evidence-backed plan → ``@forge /go <run-id>`` → the REAL lane dispatch
  → candidate artifacts → Draft MR (never merged) → the oracle green on
  the exact candidate sha;
- ``interrupt`` (arm 2, paid) — the SAME bounded task re-run with a
  deliberate interruption: ``/pause`` mid-lane → verified WIP checkpoint
  → the CI JOB cancelled through the API (job-level, never
  container-level) → the honest blocked classification → ``/retry`` → a
  fresh lane carrying the required-resume envelope → restored WIP →
  Draft MR 2 with the oracle green;
- ``collect`` — every identity into the evidence bundle (run/plan/pipeline/
  job/MR ids, the dispatch envelope, checkpoint lineage, usage receipts,
  candidate digests, timings);
- ``teardown`` — delete the disposable project (evidence first).

Every phase is resumable: the evidence bundle on disk is the state. All
waits are bounded. A refusal/failure is recorded honestly, never retried
into a green.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from forge.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_PATH = (
    REPO_ROOT / "docs" / "evaluation" / "2026-09-24-live-single-writer" / "live-run-evidence.json"
)
POSTGRES_CONTAINER = "forge-app-postgres-probe"  # never used as a name; see psql()
APP_API = "http://localhost:8420"

#: The immutable lane ref: the repo sha pushed to origin that carries the
#: R37-07 (#288) GitLab dispatch envelope — never a mutable branch/tag.
LANE_REF_SHA = "4af6b331f703ac2b662edcedd9e1fc1dd80a5059"

#: The template include ref (the promoted release — the profile's pin).
TEMPLATE_REF = "v0.36.0"

#: The frozen acceptance task (R37-08): bounded, requires real
#: implementation reasoning, independent oracle committed up front.
SLUGIFY_CASES: tuple[tuple[str, str], ...] = (
    ("Hello, World!", "hello-world"),
    ("Forge LIVE--qual __2026", "forge-live-qual-2026"),
    ("   spaces   everywhere   ", "spaces-everywhere"),
    ("already-slugged", "already-slugged"),
    ("MIXED Case 123", "mixed-case-123"),
    ("!!!leading and trailing!!!", "leading-and-trailing"),
)

ISSUE_TITLE = "Add slugify() to src/utils/text.py"

ISSUE_BODY = """\
## Task

Add a `slugify(text: str) -> str` function to `src/utils/text.py` (create
the package/directory if it does not exist yet).

### Exact contract

- lowercase the input;
- every RUN of non-alphanumeric characters becomes ONE `-`;
- no leading or trailing `-`;
- an empty (or all-separator) input returns `""`.

### The six acceptance cases (exact)

```
slugify("Hello, World!")                 == "hello-world"
slugify("Forge LIVE--qual __2026")       == "forge-live-qual-2026"
slugify("   spaces   everywhere   ")     == "spaces-everywhere"
slugify("already-slugged")               == "already-slugged"
slugify("MIXED Case 123")                == "mixed-case-123"
slugify("!!!leading and trailing!!!")    == "leading-and-trailing"
```

The repository's `smoke` CI job asserts exactly these six cases against
`src/utils/text.py` — it is the independent oracle and it is NOT part of
your change.

### Bounds

- Only create/modify `src/utils/text.py` (a `src/utils/__init__.py` is
  fine too if needed for the import path `utils.text`).
- Do NOT modify `.gitlab-ci.yml`, `tests/`, or any other file.
- Do not commit or push; leave changes in the working tree.
"""

ISSUE_BODY_INTERRUPT = (
    ISSUE_BODY
    + """
### Context

This is a RE-RUN of the same task: a previous delivery exists as a Draft
MR that was never merged, so `main` does NOT carry `slugify` — implement
it independently from `main`.
"""
)

#: Guards: the driver refuses the paid arms unless these hold.
MAX_POLL_SECONDS_DEFAULT = 1200
RUN_ID_RE = "go ([0-9a-f]{32})"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> float:
    return time.monotonic()


class Refused(Exception):
    """A precondition failed or a bounded wait expired — recorded, never retried into a green."""


# ---------------------------------------------------------------------------
# Clients: GitLab (native surfaces), the app's read API, read-only psql
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
    interval: float = 10.0,
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


# ---------------------------------------------------------------------------
# The evidence bundle — resumable state, honest appends
# ---------------------------------------------------------------------------


class Bundle:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_file():
            self.document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.document = {
                "stamp": "forge.live.qualification/1",
                "issue": "R37-08 (#289) live single-writer qualification",
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

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# setup — the disposable project, its oracle, credentials, webhook
# ---------------------------------------------------------------------------


def _slugify_oracle_script() -> str:
    cases = "\n".join(f'    ("{text}", "{expected}"),' for text, expected in SLUGIFY_CASES)
    return (
        "python3 - <<'PY'\n"
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "from utils.text import slugify\n"
        "CASES = [\n" + cases + "\n]\n"
        "for text, expected in CASES:\n"
        "    got = slugify(text)\n"
        "    assert got == expected, (text, got, expected)\n"
        "print('slugify oracle: %d/%d OK' % (len(CASES), len(CASES)))\n"
        "PY\n"
    )


TEMPLATE_URL = (
    f"https://raw.githubusercontent.com/forcewake/forge/{TEMPLATE_REF}"
    "/ci/templates/claude-sdk-lane.gitlab-ci.yml"
)

#: The v0.36.0 SDK-lane template bug, LIVE-found in the first paid run
#: (2026-09-24, job 723): the script block ends with an UNCONDITIONAL
#: ``exit "$_driver_rc"`` — on a SUCCESSFUL driver run (rc=0) the job exits
#: before ``.forge/candidate.diff`` is ever built, so the control plane
#: classifies the run ``blocked: harness_artifact_missing``. The template's
#: own comment documents the intent (a FAILURE fails the job); the patch
#: guards the exit so only a nonzero driver rc exits early. This override
#: is a MANUAL RESCUE, recorded in the qualification record — the upstream
#: template must be fixed for the un-patched claim.
_BUGGY_EXIT = '      exit "$_driver_rc"\n'
_PATCHED_EXIT = (
    '      if [ "$_driver_rc" -ne 0 ]; then\n'
    "        # v0.36.0 template bug (R37-08 live-found): exit only on a\n"
    "        # FAILED driver — a successful run must continue to build the\n"
    "        # candidate artifacts below.\n"
    '        exit "$_driver_rc"\n'
    "      fi\n"
)


def _patched_lane_override() -> str:
    """The released template, fetched at its pinned ref, plus the one
    guarded-exit patch — inlined as the local job (GitLab: a local job
    definition overrides the included one with the same name)."""
    response = httpx.get(TEMPLATE_URL, timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    template = response.text
    if template.count(_BUGGY_EXIT) != 1:
        raise Refused(
            "the pinned lane template no longer carries the expected exit line — "
            "re-diagnose before patching"
        )
    return template.replace(_BUGGY_EXIT, _PATCHED_EXIT)


def _ci_yaml() -> str:
    return (
        f"include:\n"
        f"  - remote: '{TEMPLATE_URL}'\n"
        f"\n"
        f"# MANUAL RESCUE (R37-08 live-found, recorded in the qualification\n"
        f"# record): the {TEMPLATE_REF} SDK lane template exits the job on a\n"
        f"# SUCCESSFUL driver run before .forge/candidate.diff is built. The\n"
        f"# local job below is the pinned template verbatim with ONE patch —\n"
        f"# the driver exit is guarded so only a failed driver ends the job.\n"
        f"# Remove this override when the upstream template fixes the exit.\n"
        + _patched_lane_override()
        + "\nstages: [test, harness]\n"
        "\n"
        "# The INDEPENDENT verification contract (R37-08): six exact\n"
        "# slugify cases against src/utils/text.py, committed BEFORE any\n"
        "# run. A candidate that weakens or bypasses this job is a FAILED\n"
        "# candidate (the qualification driver also asserts the candidate\n"
        "# diff does not touch this file).\n"
        "smoke:\n"
        "  stage: test\n"
        "  image: python:3.13-slim\n"
        "  rules:\n"
        "    - if: '$FORGE_RUN_ID'   # the dispatch pipeline carries no\n"
        "      when: never           # candidate yet — nothing to verify\n"
        "    - when: on_success\n"
        "  script:\n"
        "    - |\n"
        + "\n".join("      " + line for line in _slugify_oracle_script().splitlines())
        + "\n"
    )


def _tests_file() -> str:
    cases = "\n".join(f'    ("{text}", "{expected}"),' for text, expected in SLUGIFY_CASES)
    return (
        '"""The independent slugify oracle, mirrored as a test file.\n\n'
        "Committed before any qualification run; the smoke CI job asserts\n"
        "the same six cases — this file is for the human reviewer.\n"
        '"""\n'
        "import sys\n"
        "from pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n\n"
        "from utils.text import slugify\n\n"
        "CASES = [\n" + cases + "\n]\n\n"
        "def test_slugify_oracle() -> None:\n"
        "    for text, expected in CASES:\n"
        "        assert slugify(text) == expected, (text, slugify(text), expected)\n"
    )


VARIABLES_FROM_LAB = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "FORGE_BOT_READ_TOKEN",
    "FORGE_HARNESS_HTTPS_PROXY",
)


def phase_setup(bundle: Bundle, gitlab: GitLab, name: str, settings: Settings) -> int:
    record = bundle.phase("setup")
    if record.get("project", {}).get("id") is None:
        _create_project_and_seed(bundle, gitlab, name)
    _ensure_bot_member(bundle, gitlab, settings)
    _ensure_ci_patch(bundle, gitlab)
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


def _ensure_ci_patch(bundle: Bundle, gitlab: GitLab) -> None:
    """Install (once) the patched lane job — the MANUAL RESCUE for the
    v0.36.0 template's unconditional driver exit (see _patched_lane_override)."""
    record = bundle.phase("setup")
    project_id = record["project"]["id"]
    if record.get("ci_patch_sha"):
        return
    content = _ci_yaml()
    updated = gitlab.put(
        f"/projects/{project_id}/repository/files/.gitlab-ci.yml",
        json={
            "branch": "main",
            "content": content,
            "commit_message": (
                "lab(R37-08): guarded driver exit in the SDK lane job — the v0.36.0 "
                "template exits a SUCCESSFUL lane before candidate.diff is built "
                "(manual rescue, root-caused in the qualification record)"
            ),
        },
    )
    if updated.status_code not in (200, 201):
        raise Refused(f"CI patch commit failed: {updated.text[:300]}")
    record["ci_patch_sha"] = updated.json().get("commit_id") or updated.json().get("file_path")
    record["ci_patch_content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
    bundle.save()
    print(f"setup: lane-job patch committed ({record['ci_patch_sha']}) — MANUAL RESCUE recorded")


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

    commit = gitlab.post(
        f"/projects/{project_id}/repository/commits",
        json={
            "branch": "main",
            "commit_message": "seed: the frozen slugify acceptance task + its independent oracle",
            "actions": [
                {
                    "action": "create",
                    "file_path": "README.md",
                    "content": (
                        f"# {name}\n\nA DISPOSABLE repository for the R37-08 (#289) live\n"
                        "single-writer qualification. The acceptance task: implement\n"
                        "`slugify` in `src/utils/text.py` so the six-case oracle\n"
                        "(`.gitlab-ci.yml` smoke job / `tests/test_slugify.py`) passes.\n"
                        "This project is deleted after the qualification evidence is\n"
                        "captured.\n"
                    ),
                },
                {"action": "create", "file_path": ".gitlab-ci.yml", "content": _ci_yaml()},
                {
                    "action": "create",
                    "file_path": "tests/test_slugify.py",
                    "content": _tests_file(),
                },
            ],
        },
    )
    if commit.status_code not in (201, 200):
        raise Refused(f"seed commit failed: {commit.status_code} {commit.text[:300]}")
    seed_sha = commit.json().get("id")
    bundle.record("setup", "seed_commit_sha", seed_sha)
    bundle.record(
        "setup",
        "template_content_sha256",
        hashlib.sha256(_ci_yaml().encode()).hexdigest(),
    )
    print(f"setup: seed commit {seed_sha[:12]} (oracle committed before any run)")

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
        ("FORGE_LANE_REF", LANE_REF_SHA),  # immutable lane identity (#288 envelope)
        ("FORGE_STEERING_ENABLED", "1"),  # the lane-side pause consumer
    ):
        response = gitlab.post(
            f"/projects/{project_id}/variables", json={"key": key, "value": value}
        )
        if response.status_code not in (201, 200):
            raise Refused(f"variable {key} set failed: {response.text[:200]}")
        copied.append(f"{key}={value}")
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
    """The BOT identity must be a project member: the worker speaks GitLab
    with FORGE_BOT_TOKEN (never the approver's), and a private project is
    invisible to it until invited (LIVE-found: start_run 404s otherwise)."""
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
        json={"user_id": bot_user_id, "access_level": 30},  # Developer: notes + commits
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


def phase_preflight(bundle: Bundle, gitlab: GitLab) -> int:
    record = bundle.phase("preflight")
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    checks: dict[str, Any] = {}

    health = app_get("/health")
    checks["controlplane.health"] = {
        "status": health.get("status"),
        "version": health.get("version"),
    }
    if health.get("status") != "ok" or health.get("version") != "0.36.0":
        raise Refused(f"control plane not aligned: {health}")

    head = psql("SELECT version_num FROM alembic_version").strip()
    checks["schema_head"] = head
    if head != "027":
        raise Refused(f"schema head {head} != 027")

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

    # The app's OWN doctor, run on the exact command a customer would. The
    # litellm /health probe rides its 5 s timeout boundary (observed 1.6–2.2 s
    # typical, occasional spikes) — a read-only re-probe is honest, every
    # attempt is recorded, and NO check is weakened.
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
        "checks": [
            {k: c.get(k) for k in ("name", "status", "detail")}
            for c in doctor_json.get("checks", [])
        ],
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
# shared flow mechanics
# ---------------------------------------------------------------------------


def find_run_for_issue(gitlab: GitLab, project_id: int, issue_iid: int) -> dict[str, Any] | None:
    runs = app_get("/runs?limit=50").get("runs", [])
    for run in runs:
        if run.get("project_id") == project_id and run.get("issue_iid") == issue_iid:
            return run
    return None


def latest_run_detail(gitlab: GitLab, project_id: int, issue_iid: int) -> dict[str, Any] | None:
    run = find_run_for_issue(gitlab, project_id, issue_iid)
    if run is None:
        return None
    try:
        return app_get(f"/runs/{run['id']}")
    except httpx.HTTPError:
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


def start_issue_and_plan(
    bundle: Bundle,
    gitlab: GitLab,
    project_id: int,
    phase: str,
    issue_body: str,
    poll_timeout: float,
) -> dict[str, Any]:
    """Native issue → /implement → the plan note; returns the arc's handles."""
    created = gitlab.post(
        f"/projects/{project_id}/issues",
        json={"title": ISSUE_TITLE, "description": issue_body},
    )
    if created.status_code not in (201, 200):
        raise Refused(f"issue creation failed: {created.text[:200]}")
    issue = created.json()
    issue_iid = issue["iid"]
    bundle.record(phase, "issue", {"iid": issue_iid, "url": issue["web_url"]})
    print(f"{phase}: issue #{issue_iid} created — {issue['web_url']}")

    import re

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
    note_id = plan.get("id")
    match = re.search(RUN_ID_RE, body)
    if match is None:
        raise Refused(f"plan note carries no run id:\n{body[-600:]}")
    run_id = match.group(1)
    harness_line = next((line for line in body.splitlines() if line.startswith("- Harness:")), "")
    bundle.record(
        phase,
        "plan",
        {
            "note_id": note_id,
            "run_id": run_id,
            "harness_line": harness_line,
            "plan_digest_line": next(
                (line for line in body.splitlines() if "digest" in line.lower()), ""
            ),
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


def approve_and_dispatch(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, arc: dict[str, Any]
) -> dict[str, Any]:
    issue_iid, run_id = arc["issue_iid"], arc["run_id"]
    approved = gitlab.post(
        f"/projects/{project_id}/issues/{issue_iid}/notes",
        json={"body": f"@forge /go {run_id}"},
    )
    if approved.status_code not in (201, 200):
        raise Refused(f"/go note failed: {approved.text[:200]}")
    branch = f"factory/{issue_iid}/{run_id[:8]}"

    def pipeline() -> Any:
        pipelines = gitlab.get(f"/projects/{project_id}/pipelines", params={"ref": branch})
        return pipelines[0] if pipelines else None

    dispatched = poll(pipeline, f"the harness pipeline on {branch}", timeout=600, interval=10)
    pipeline_id = dispatched["id"]
    job = poll(
        lambda: lane_job(gitlab, project_id, pipeline_id),
        "the lane job",
        timeout=300,
        interval=10,
    )
    bundle.record(
        phase,
        "dispatch",
        {
            "branch": branch,
            "pipeline_id": pipeline_id,
            "pipeline_url": dispatched.get("web_url"),
            "lane_job_id": job["id"],
            "lane_job_status_at_capture": job.get("status"),
        },
    )
    print(f"{phase}: pipeline {pipeline_id}, lane job {job['id']} ({job.get('status')})")
    return {"branch": branch, "pipeline_id": pipeline_id, "job_id": job["id"]}


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

    green = poll(verified, "the current candidate's green pipeline", timeout=1500, interval=20)

    # oracle independence: the candidate must not touch the oracle files
    diffs = gitlab.get(f"/projects/{project_id}/merge_requests/{mr_iid}/diffs")
    touched = [str(d.get("new_path")) for d in diffs]
    forbidden = [
        path for path in touched if path in (".gitlab-ci.yml",) or path.startswith("tests/")
    ]
    current = gitlab.get(f"/projects/{project_id}/merge_requests/{mr_iid}")
    result = {
        "mr_iid": mr_iid,
        "mr_url": merge_request.get("web_url"),
        "title": merge_request.get("title"),
        "draft": bool(
            merge_request.get("work_in_progress")
            or merge_request.get("title", "").startswith("Draft:")
        ),
        "candidate_sha": green["sha"],
        "verification_pipeline_id": green["pipeline_id"],
        "changed_paths": touched,
        "oracle_tampering": forbidden,
        "pipeline_status": current.get("pipeline_status"),
    }
    if forbidden:
        raise Refused(f"the candidate touched the oracle files: {forbidden}")
    bundle.record(phase, "mr", result)
    print(f"{phase}: Draft MR !{mr_iid} green on candidate {green['sha'][:12]} ({touched})")
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
    bundle.record(phase, "durable_state", captured)
    tokens_in = sum(int(row.get("input_tokens") or 0) for row in usage)
    tokens_out = sum(int(row.get("output_tokens") or 0) for row in usage)
    calls = len(usage)
    print(
        f"{phase}: run {run_id[:8]} status={detail.get('status')} "
        f"usage: {calls} receipts, {tokens_in} in / {tokens_out} out tokens"
    )
    return captured


def capture_lane_trace(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str, job_id: int
) -> dict[str, Any]:
    trace = job_trace(gitlab, project_id, job_id)
    markers = {
        "claude_version": grep_lines(trace, "claude --version") or grep_lines(trace, "2.1.273"),
        "lane_install": grep_lines(trace, "forge @ git+")
        or grep_lines(trace, "forge[interactive]"),
        "install_receipt": grep_lines(trace, "lane_install"),
        "identity": grep_lines(trace, "install-identity"),
        "dispatch_envelope": grep_lines(trace, "forge dispatch envelope"),
        "candidate_marker": grep_lines(trace, "FORGE_CANDIDATE:"),
        "resume_markers": grep_lines(trace, "resume"),
        "restore_markers": grep_lines(trace, "restor"),
        "checkpoint_markers": grep_lines(trace, "checkpoint"),
        "usage_markers": grep_lines(trace, "usage"),
    }
    captured = {
        "job_id": job_id,
        "trace_sha256": hashlib.sha256(trace.encode()).hexdigest(),
        "markers": markers,
        "tail": trace[-2500:],
    }
    bundle.record(phase, "lane_trace", captured)
    print(f"{phase}: lane job {job_id} trace captured ({len(trace)} bytes)")
    return captured


# ---------------------------------------------------------------------------
# arm 1 — the uninterrupted flow
# ---------------------------------------------------------------------------


def phase_flow(bundle: Bundle, gitlab: GitLab) -> int:
    phase = "flow"
    if bundle.document["phases"].get(phase, {}).get("mr"):
        print("flow: already complete — resuming collect-only")
        return 0
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    record = bundle.phase(phase)
    record["paid"] = True
    bundle.save()

    arc = start_issue_and_plan(bundle, gitlab, project_id, phase, ISSUE_BODY, poll_timeout=900)
    arc.update(approve_and_dispatch(bundle, gitlab, project_id, phase, arc))
    lane = lane_job(gitlab, project_id, arc["pipeline_id"])
    print(f"flow: waiting for lane job {lane['id']} to finish…")
    poll(
        lambda: (lambda j: j and j.get("status") in ("success", "failed", "canceled"))(
            lane_job(gitlab, project_id, arc["pipeline_id"])
        ),
        "lane job completion",
        timeout=2100,
        interval=30,
    )
    final_job = lane_job(gitlab, project_id, arc["pipeline_id"])
    bundle.record(
        phase,
        "lane_outcome",
        {"job_status": final_job.get("status"), "failure_reason": final_job.get("failure_reason")},
    )
    if final_job.get("status") != "success":
        trace = job_trace(gitlab, project_id, final_job["id"])
        bundle.record(phase, "lane_failure_trace_tail", trace[-4000:])
        capture_run_evidence(bundle, phase, arc["run_id"])
        raise Refused(
            f"lane job {final_job['id']} ended {final_job.get('status')} "
            f"({final_job.get('failure_reason')}) — honest failure, trace captured"
        )
    capture_lane_trace(bundle, gitlab, project_id, phase, final_job["id"])
    wait_mr_and_verify(bundle, gitlab, project_id, phase, arc["branch"])
    capture_run_evidence(bundle, phase, arc["run_id"])
    bundle.document["phases"][phase]["result"] = "green"
    bundle.document["phases"][phase]["finished_at"] = _now()
    bundle.save()
    print("flow: GREEN — Draft MR left for human review (forge never merges)")
    return 0


# ---------------------------------------------------------------------------
# arm 2 — the deliberate interruption
# ---------------------------------------------------------------------------


def phase_interrupt(bundle: Bundle, gitlab: GitLab) -> int:
    phase = "interrupt"
    if bundle.document["phases"].get(phase, {}).get("mr"):
        print("interrupt: already complete — resuming collect-only")
        return 0
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    record = bundle.phase(phase)
    record["paid"] = True
    bundle.save()

    if record.get("plan") and record.get("dispatch") and not record.get("mr"):
        # RESUME an interrupted interrupt-drill (the phase is re-runnable):
        # the arc already dispatched; continue from the recorded stage.
        arc = {
            "issue_iid": record["issue"]["iid"],
            "run_id": record["plan"]["run_id"],
            **{
                k: record["dispatch"][k]
                for k in ("branch", "pipeline_id", "job_id")
                if k in record["dispatch"]
            },
        }
        print(f"interrupt: resuming run {arc['run_id'][:8]} from the recorded arc")
    else:
        arc = start_issue_and_plan(
            bundle, gitlab, project_id, phase, ISSUE_BODY_INTERRUPT, poll_timeout=900
        )
        arc.update(approve_and_dispatch(bundle, gitlab, project_id, phase, arc))

    def _works_index_path() -> Path:
        return REPO_ROOT / "data" / "checkpoints" / "works" / f"{arc['run_id']}.json"

    def _filesystem_checkpoint() -> dict[str, Any] | None:
        works_index = _works_index_path()
        if works_index.is_file():
            document = json.loads(works_index.read_text(encoding="utf-8"))
            entries = document.get("checkpoints") or []
            if entries:
                return {"authority": "filesystem", **entries[-1]}
        return None

    if not record.get("checkpoint"):
        # 1. wait until the lane job is RUNNING, then PAUSE mid-work. On a
        # RESUMED drill a checkpoint may already exist (the fence ended the
        # job) — that state is the pause's outcome, never a re-pause.
        def running_or_checkpointed() -> Any:
            existing = _filesystem_checkpoint()
            if existing:
                return "already-checkpointed"
            job = lane_job(gitlab, project_id, arc["pipeline_id"])
            return job if job and job.get("status") == "running" else None

        state = poll(
            running_or_checkpointed,
            "the lane job to run (or a recorded checkpoint on resume)",
            timeout=900,
            interval=10,
        )
        if state == "already-checkpointed":
            bundle.record(
                phase,
                "pause",
                {"posted_at": "pre-recorded (drill resumed after the fence ended the job)"},
            )
        else:
            paused_at = _now()
            note = gitlab.post(
                f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
                json={"body": "@forge /pause"},
            )
            if note.status_code not in (201, 200):
                raise Refused(f"/pause note failed: {note.text[:200]}")
            bundle.record(phase, "pause", {"posted_at": paused_at})
            print(f"interrupt: /pause posted at {paused_at} (lane mid-work)")

    if not record.get("checkpoint"):
        # 2. wait for the verified WIP checkpoint. The lab's checkpoint
        # authority is the FILESYSTEM store (data/checkpoints) — the works
        # index IS the durable lineage; postgres checkpoint_metadata stays
        # empty until a postgres-authority cutover (doctor observes the
        # same). Both surfaces are probed, honestly recorded.
        def checkpoint_row() -> Any:
            existing = _filesystem_checkpoint()
            if existing:
                return existing
            rows = psql(
                "SELECT row_to_json(t)::text FROM (SELECT * FROM checkpoint_metadata WHERE "
                f"work_id = '{arc['run_id']}' ORDER BY sequence) t"
            )
            lines = [line for line in rows.splitlines() if line.strip()]
            return json.loads(lines[-1]) if lines else None

        checkpoint = poll(checkpoint_row, "the verified WIP checkpoint", timeout=900, interval=15)
        bundle.record(
            phase,
            "checkpoint",
            {
                "id": checkpoint.get("checkpoint_id") or checkpoint.get("id"),
                "authority": checkpoint.get("authority", "postgres"),
                "sequence": checkpoint.get("sequence"),
                "files": checkpoint.get("files"),
                "uploaded_at": checkpoint.get("uploaded_at"),
                "row": checkpoint,
            },
        )
        print(f"interrupt: checkpoint verified ({json.dumps(checkpoint)[:160]}…)")

    if not record.get("job_cancel"):
        # 3. kill the runner context at the JOB level (never container
        # level). NOTE (live-found): a mid-turn /pause may itself END the
        # lane job (the fence stops the turn and the driver exits nonzero
        # — the patched template fails the job); cancelling an already
        # terminal job is then moot and recorded as such.
        job = lane_job(gitlab, project_id, arc["pipeline_id"]) or {"id": arc.get("job_id")}
        cancelled = gitlab.post(f"/projects/{project_id}/jobs/{job['id']}/cancel")
        body = {}
        try:
            body = cancelled.json()
        except ValueError:
            body = {}
        bundle.record(
            phase,
            "job_cancel",
            {
                "job_id": job["id"],
                "attempted_at": _now(),
                "http_status": cancelled.status_code,
                "status_after": body.get("status") or body.get("message"),
                "moot": cancelled.status_code not in (200, 201),
            },
        )
        print(
            f"interrupt: CI job {job['id']} cancel → HTTP {cancelled.status_code} "
            f"({body.get('status') or body.get('message')})"
        )

    # 4. the honest blocked classification (worker, from the job event)
    def blocked() -> Any:
        run = find_run_for_issue(gitlab, project_id, arc["issue_iid"])
        if run and run.get("status") == "blocked":
            return run
        return None

    if not record.get("blocked_classification"):
        blocked_run = poll(blocked, "the run's blocked classification", timeout=1200, interval=20)
        bundle.record(phase, "blocked_classification", blocked_run)
        print(f"interrupt: run blocked ({blocked_run.get('status_reason')})")

    # 5. the operator's /retry — the re-dispatch with the resume envelope
    if not record.get("resume_dispatch"):
        retried = gitlab.post(
            f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
            json={"body": "@forge /retry"},
        )
        if retried.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retried.text[:200]}")

        def resumed_pipeline() -> Any:
            pipelines = gitlab.get(
                f"/projects/{project_id}/pipelines", params={"ref": arc["branch"]}
            )
            # only a NEW API-triggered pipeline is the re-dispatch — the
            # branch-cut PUSH pipeline (no FORGE_RUN_ID, no lane job) is not
            candidates = [
                p for p in pipelines if p["id"] != arc["pipeline_id"] and p.get("source") == "api"
            ]
            return candidates[0] if candidates else None

        resume = poll(
            resumed_pipeline,
            "the resumed dispatch (attempt generation 2)",
            timeout=900,
            interval=15,
        )
        resumed_job = poll(
            lambda: lane_job(gitlab, project_id, resume["id"]),
            "the resumed lane job",
            timeout=300,
            interval=10,
        )
        bundle.record(
            phase,
            "resume_dispatch",
            {
                "pipeline_id": resume["id"],
                "lane_job_id": resumed_job["id"],
                "pipeline_url": resume.get("web_url"),
            },
        )
        print(f"interrupt: resumed pipeline {resume['id']}, lane job {resumed_job['id']}")

    resume_id = record["resume_dispatch"]["pipeline_id"]
    resumed_job_id = record["resume_dispatch"]["lane_job_id"]

    if not record.get("resume_envelope_lines"):
        # capture the resume envelope from the resumed job's trace EARLY (the
        # envelope line prints before the driver runs)
        time.sleep(45)
        early_trace = job_trace(gitlab, project_id, resumed_job_id)
        bundle.record(
            phase, "resume_envelope_lines", grep_lines(early_trace, "forge dispatch envelope")
        )

    if not record.get("resumed_lane_outcome"):
        poll(
            lambda: (lambda j: j and j.get("status") in ("success", "failed", "canceled"))(
                lane_job(gitlab, project_id, resume_id)
            ),
            "the resumed lane job completion",
            timeout=2100,
            interval=30,
        )
        final_job = lane_job(gitlab, project_id, resume_id)
        bundle.record(
            phase,
            "resumed_lane_outcome",
            {
                "job_status": final_job.get("status"),
                "failure_reason": final_job.get("failure_reason"),
            },
        )
    final_status = record["resumed_lane_outcome"]["job_status"]
    if final_status != "success":
        trace = job_trace(gitlab, project_id, resumed_job_id)
        bundle.record(phase, "resumed_lane_failure_trace_tail", trace[-4000:])
        capture_run_evidence(bundle, phase, arc["run_id"])
        raise Refused(f"resumed lane job ended {final_status} — honest failure, trace captured")
    capture_lane_trace(bundle, gitlab, project_id, phase, resumed_job_id)
    wait_mr_and_verify(bundle, gitlab, project_id, phase, arc["branch"])
    capture_run_evidence(bundle, phase, arc["run_id"])
    bundle.document["phases"][phase]["result"] = "green"
    bundle.document["phases"][phase]["finished_at"] = _now()
    bundle.save()
    print("interrupt: GREEN — WIP restored on a second runner, Draft MR 2 for human review")
    return 0


# ---------------------------------------------------------------------------
# collect + teardown
# ---------------------------------------------------------------------------


def phase_collect(bundle: Bundle, gitlab: GitLab) -> int:
    project_id = bundle.document["phases"]["setup"]["project"]["id"]
    record = bundle.phase("collect")
    for phase in ("flow", "interrupt"):
        run_id = bundle.document["phases"].get(phase, {}).get("plan", {}).get("run_id")
        if run_id:
            capture_run_evidence(bundle, f"collect-{phase}", run_id)
    template = gitlab.get(
        f"/projects/{project_id}/repository/files/.gitlab-ci.yml",
        params={"ref": "main"},
    )
    record["installed_template"] = {
        "content_sha256": template.get("content_sha256"),
        "last_commit_id": template.get("last_commit_id"),
    }
    version = gitlab.get("/version")
    record["gitlab_version"] = {
        "version": version.get("version"),
        "revision": version.get("revision"),
        "enterprise": version.get("enterprise"),
    }
    record["finished_at"] = _now()
    bundle.save()
    print("collect: durable state refreshed for both arms")
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
        prog="python scripts/run_live_qualification.py",
        description=(
            "R37-08 (#289): ONE live single-writer qualification — real tools, real "
            "model, real provider, through native surfaces only. Phases are "
            "resumable; the evidence bundle on disk is the state."
        ),
    )
    parser.add_argument(
        "phase", choices=["setup", "preflight", "flow", "interrupt", "collect", "teardown"]
    )
    parser.add_argument("--evidence", type=Path, default=EVIDENCE_PATH)
    parser.add_argument(
        "--project-name",
        default=f"forge-live-qual-{datetime.now(timezone.utc):%Y-%m-%d}",
    )
    args = parser.parse_args(argv)

    settings = Settings()  # type: ignore[call-arg]
    gitlab = GitLab(settings)
    bundle = Bundle(args.evidence)
    handlers: dict[str, Callable[..., int]] = {
        "setup": lambda: phase_setup(bundle, gitlab, args.project_name, settings),
        "preflight": lambda: phase_preflight(bundle, gitlab),
        "flow": lambda: phase_flow(bundle, gitlab),
        "interrupt": lambda: phase_interrupt(bundle, gitlab),
        "collect": lambda: phase_collect(bundle, gitlab),
        "teardown": lambda: phase_teardown(bundle, gitlab),
    }
    try:
        return handlers[args.phase]()
    except Refused as exc:
        bundle.record(args.phase, "refused", {"reason": str(exc), "at": _now()})
        print(f"{args.phase}: REFUSED — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
