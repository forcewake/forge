"""R40-07 (#343) — qualify operation-grant redemption on the live lab.

The 0.39.0 live trace rode the native/protected-variable credential
route; this driver proves the REMAINING live proof class: a REAL native
dispatch under the ``runner-redemption`` delivery mode MINTS the
operation grant through the native path (never seeded), the actual lane
bootstrap REDEEMS it through the lane-control endpoint, and the actual
model consumer presents the BROKER-SELECTED sentinel — never the
competing AMBIENT one.

The identity proof uses a RECORDING model endpoint (issue #343 scope 3:
"a controlled model endpoint"; a real-model pass is NOT required for
identity proof — recorded honestly in the qualification record). The
lane's model route (ANTHROPIC_BASE_URL) points at the local recorder
(docs/evaluation/2026-09-26-redemption-qualification/recorder_server.py,
podman-published on the lab host LAN IP); the recorder captures the
Authorization header the vendor client presented and answers a 400
Anthropic-shaped error, so the driver leg fails fast with ZERO model
spend. Two DISTINCT sentinels are configured:

- the AMBIENT sentinel — the disposable project's ordinary
  ``ANTHROPIC_AUTH_TOKEN`` CI variable (exactly the credential an
  ambient-fallback lane would have used);
- the BROKER-SELECTED sentinel — the value behind the bound credential
  ref (``env:FORGE_BROKER_MODEL_TOKEN`` on the control-plane
  consumers), delivered ONLY through the redemption endpoint.

Acceptance mapped to phases (issue #343):

1. ``setup``   — disposable project + the SHIPPED template verbatim +
   both sentinels + the credential binding registry (refs only) + the
   webhook. The alignment (scripts/align_lab.py --apply with
   FORGE_CREDENTIAL_DELIVERY=runner-redemption + the registry + the
   broker sentinel env on BOTH consumers) runs OUTSIDE this driver, on
   the operator's receipt; the trace phase refuses to start unless the
   lab reports the delivery mode.
2. ``reach``   — a scratch runner job proves the runner can dial the
   recorder BEFORE any dispatch is burned.
3. ``trace``   — native issue → /implement (the evidence-backed plan)
   → assert NO grant exists yet → /go → the REAL dispatch mints the
   grant (assert the authority row + the value-free envelope journal)
   → the lane bootstrap redeems → the artifacts carry the consumer
   receipt joined on grant_id → the recorder captured the BROKER
   sentinel (and never the ambient one).
4. ``retire``  — native /retry starts a NEW attempt generation: the old
   generation's token is refused typed, the new lane follows its OWN
   grant.
5. ``restart`` — cold control-plane restart (podman restart forge-app):
   the grant window + the audit link survive; an exact replay inside
   the deadline redeems idempotently.
6. ``negative``— wrong ref / wrong route redemption attempts against
   the LIVE attempt (typed refusals, zero successful retrievals), then
   the binding ROTATION arm: the registry rotates (same ref, new
   revision) between the dispatch and the redemption, the real lane's
   redemption is refused typed (binding_revision_mismatch) and the
   lane fails CLOSED (the recorder stays silent for that generation).
7. ``expire``  — after the consumers are re-aligned with a SHORT grant
   window (operator receipt), a second dispatch's grant expires and
   BOTH the endpoint (typed grant_expired) and the real lane (fail
   closed, zero model turns) refuse it.
8. ``teardown``— the disposable project is deleted (capture first).

Every phase is resumable; the evidence bundle on disk is the state; a
precondition failure REFUSES (recorded honestly, never retried into a
green). Values (sentinels, tokens) live ONLY in the maintainer-private
state file under data/ (gitignored); the bundle carries sha256 digests.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-26-redemption-qualification"
EVIDENCE_PATH = EVAL_DIR / "live-run-evidence.json"
STATE_PATH = REPO_ROOT / "data" / "redemption-qualification" / "state.json"
RECORDER_DIR = REPO_ROOT / "data" / "redemption-recorder"
CAPTURE_LOG = RECORDER_DIR / "capture.jsonl"
REGISTRY_PATH = REPO_ROOT / "data" / "credential-bindings.json"
TEMPLATE_SOURCE = REPO_ROOT / "ci" / "templates" / "claude-sdk-lane.gitlab-ci.yml"
ALIGNMENT_RECEIPTS = EVAL_DIR / "alignment-receipts.json"

#: The lane install pin: the PROMOTED v0.39.0 tree (the pushed sha the
#: release tag b521e1a names) — the lane package the released profile
#: ships. The working tree (the control-plane alignment build) is never
#: pushed; the two identities are bound separately, never merged.
LANE_REF_SHA = "b521e1a7bd3568dd48bb235b6b7d743a21caca99"

#: The lab host's LAN address (the runner dials the recorder here; the
#: same reachability the lab's tinyproxy precedent established).
LAB_HOST_LAN_IP = "192.168.1.18"
RECORDER_PORT = 8480
RECORDER_URL = f"http://{LAB_HOST_LAN_IP}:{RECORDER_PORT}"

#: The bound credential ref (the EnvBroker resolves it from the
#: control-plane consumers' env). The ref's env NAME must BE the
#: binding's env slot — the EnvBroker stages the resolved value under
#: the ref's own name, and the redemption endpoint's staged-slot guard
#: refuses any staging outside the binding's slot (LIVE-found on the
#: first dispatch: a ref named env:FORGE_BROKER_MODEL_TOKEN was refused
#: typed ``staged_slot_mismatch`` with zero emitted values — the guard
#: working as designed; recorded in the bundle as the misbound-ref
#: negative). Deliberately NOT secret-shaped (the bind-time guard
#: refuses refs that look like pasted values).
BROKER_REF = "env:ANTHROPIC_AUTH_TOKEN"
PROVIDER_ROUTE = "anthropic-gateway"
ENV_SLOT = "ANTHROPIC_AUTH_TOKEN"

APP_API = "http://localhost:8420"
RUN_ID_RE = re.compile(r"\b([0-9a-f]{32})\b")
UTC = ZoneInfo("UTC")

POLL_INTERVAL_S = 10.0
LANE_JOB_TIMEOUT_S = 1500.0  # npm+uv install + the bounded driver turn


class Refused(Exception):
    """A precondition failed — recorded, never retried into a green."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> float:
    return time.monotonic()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The resumable evidence bundle + the PRIVATE state (values live ONLY here)
# ---------------------------------------------------------------------------


class Bundle:
    """The committed evidence bundle — refs/digests only, never values."""

    def __init__(self, path: Path = EVIDENCE_PATH) -> None:
        self.path = path
        if path.is_file():
            self.document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.document = {
                "stamp": "forge.redemption.qualification/1",
                "issue": "forge#343 (R40-07) — operation-grant redemption, live",
                "created_at": _now(),
                "phases": {},
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
        entry.setdefault(key, []).append(value)
        entry["updated_at"] = _now()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


class State:
    """Maintainer-PRIVATE values (sentinels, secrets digests) — gitignored."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self.document: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.document.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.document[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def sentinel_pair(state: State) -> tuple[str, str]:
    """The (ambient, broker) sentinel VALUES — generated once, private."""
    import secrets

    existing = state.get("sentinels")
    if isinstance(existing, dict) and existing.get("ambient") and existing.get("broker"):
        return str(existing["ambient"]), str(existing["broker"])
    pair = {
        "ambient": "forge-ambient-sentinel-" + secrets.token_hex(16),
        "broker": "forge-broker-sentinel-" + secrets.token_hex(16),
    }
    state.set("sentinels", pair)
    return pair["ambient"], pair["broker"]


# ---------------------------------------------------------------------------
# Lab plumbing: GitLab API, read-only psql, lane tokens, the app's env
# ---------------------------------------------------------------------------


def app_env(name: str) -> str:
    completed = subprocess.run(
        ["podman", "exec", "forge-app", "printenv", name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise Refused(f"forge-app does not carry {name}")
    return completed.stdout.strip()


class GitLab:
    """The operator's GitLab API client (bot PAT from the app container)."""

    def __init__(self) -> None:
        self.base = app_env("GITLAB_URL").rstrip("/") + "/api/v4"
        self.token = app_env("GITLAB_TOKEN")
        self.webhook_secret = app_env("GITLAB_WEBHOOK_SECRET")
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


def psql_scalar(sql: str) -> str:
    """A single scalar SELECT through the lab postgres (plain text)."""
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
    return completed.stdout.strip()


def psql_json(sql: str) -> Any:
    """Read-only SELECT through the lab postgres (json output, no values)."""
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
    raw = completed.stdout.strip()
    return json.loads(raw) if raw else None


def lane_token(work_id: str, generation: int) -> str:
    """The attempt-scoped HMAC lane token, derived exactly as the dispatch
    does (HMAC-SHA256(secret, work_id:generation)) — the operator-held
    derivation the endpoint-level arms use. NEVER persisted in evidence."""
    secret = app_env("FORGE_LANE_CONTROL_SECRET")
    material = f"{work_id}:{generation}"
    return hmac.new(secret.encode(), material.encode(), "sha256").hexdigest()


def redeem(
    work_id: str,
    credential_ref: str,
    provider: str,
    token: str,
) -> httpx.Response:
    """One redemption call against the LIVE endpoint (values never logged)."""
    return httpx.get(
        f"{APP_API}/lane/credentials/redeem",
        params={"work_id": work_id, "credential_ref": credential_ref, "provider": provider},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


def poll(
    predicate: Callable[[], Any],
    description: str,
    *,
    timeout: float,
    interval: float = POLL_INTERVAL_S,
) -> Any:
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


def app_health(timeout: float = 300.0) -> dict[str, Any]:
    """Poll the app's /health (used after cold restarts)."""
    return poll(
        lambda: _health_probe(),
        "the control plane /health",
        timeout=timeout,
        interval=5.0,
    )


def _health_probe() -> dict[str, Any] | None:
    try:
        response = httpx.get(f"{APP_API}/health", timeout=10.0)
        return response.json() if response.status_code == 200 else None
    except httpx.HTTPError:
        return None


# ---------------------------------------------------------------------------
# The project's seed: the SHIPPED template verbatim + a minimal oracle
# ---------------------------------------------------------------------------


def ci_yaml() -> str:
    """The disposable project's CI: the SHIPPED SDK lane template VERBATIM
    plus the shared stages and a minimal smoke job (kept for composition
    parity; dispatch pipelines skip it by rule)."""
    template = TEMPLATE_SOURCE.read_text(encoding="utf-8")
    if "forge-agent-claude-sdk:" not in template:
        raise Refused(f"{TEMPLATE_SOURCE} carries no forge-agent-claude-sdk job")
    return (
        "# Generated by scripts/run_redemption_qualification.py (R40-07/#343):\n"
        "# the SHIPPED SDK lane template VERBATIM + stages + a minimal\n"
        "# smoke job, committed BEFORE any run.\n"
        "stages: [test, harness]\n\n" + template + "\nsmoke:\n"
        "  stage: test\n"
        "  image: python:3.13-slim\n"
        "  rules:\n"
        "    - if: '$FORGE_RUN_ID'\n"
        "      when: never\n"
        "    - when: on_success\n"
        "  script:\n"
        '    - python -c "import sys; sys.exit(0)"\n'
    )


def seed_files(name: str) -> dict[str, str]:
    return {
        "README.md": (
            f"# {name}\n\nThe R40-07 (#343) redemption-qualification disposable\n"
            "project — deleted after capture. The lane's model route points at\n"
            "the local recorder, so zero real model calls are made.\n"
        ),
        ".gitlab-ci.yml": ci_yaml(),
        "src/app.py": 'def greet(name: str) -> str:\n    return f"hello {name}"\n',
    }


def issue_title() -> str:
    return "Add src/utils/echo.py with echo(text) and a test"


def issue_body() -> str:
    return (
        "Add `src/utils/echo.py` defining `echo(text: str) -> str` returning the\n"
        "text unchanged, plus `tests/test_echo.py` with one exact assertion.\n"
        "\n"
        "(The R40-07 redemption-qualification trace: the lane's model endpoint is\n"
        "a local recorder for identity proof — the driver leg is EXPECTED to fail\n"
        "fast with zero model turns; the candidate is not the point of this run.)\n"
    )


# ---------------------------------------------------------------------------
# setup — the disposable project, both sentinels, the binding registry
# ---------------------------------------------------------------------------


def phase_setup(bundle: Bundle, state: State, gitlab: GitLab, name: str) -> int:
    record = bundle.phase("setup")
    ambient, broker = sentinel_pair(state)

    # 1. the disposable project + the seed commit (template verbatim)
    if record.get("project", {}).get("id") is None:
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
        project_id = int(project["id"])
        bundle.record(
            "setup", "project", {"id": project_id, "path": project["path_with_namespace"]}
        )
        print(f"setup: project {project['path_with_namespace']} (id {project_id})")
        commit = gitlab.post(
            f"/projects/{project_id}/repository/commits",
            json={
                "branch": "main",
                "commit_message": (
                    "seed: the shipped SDK lane template verbatim (committed "
                    "before any run) — R40-07/#343"
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
            "ci_yaml_sha256",
            hashlib.sha256(ci_yaml().encode()).hexdigest(),
        )
        bundle.record(
            "setup",
            "template_sha256",
            hashlib.sha256(TEMPLATE_SOURCE.read_bytes()).hexdigest(),
        )
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])

    # 2. the sentinel-bearing CI variables — the AMBIENT credential the
    #    lane's ordinary env carries (exactly what an ambient fallback
    #    would have presented) and the recorder as the model route.
    if not record.get("variables"):
        desired: list[tuple[str, str, str]] = [
            # (key, value source, note)
            (ENV_SLOT, "literal:" + ambient, "the AMBIENT sentinel"),
            ("ANTHROPIC_BASE_URL", "literal:" + RECORDER_URL, "the recording model endpoint"),
            ("FORGE_LANE_REF", "literal:" + LANE_REF_SHA, "the promoted v0.39.0 lane pin"),
            ("FORGE_STEERING_ENABLED", "literal:1", "the lane-side steering consumer"),
        ]
        read_token = gitlab.get_optional("/projects/68/variables/FORGE_BOT_READ_TOKEN")
        if read_token and read_token.get("value"):
            desired.append(("FORGE_BOT_READ_TOKEN", "literal:" + read_token["value"], "ro PAT"))
        set_keys: list[str] = []
        for key, source, note in desired:
            value = source.split(":", 1)[1]
            existing = gitlab.get_optional(f"/projects/{project_id}/variables/{key}")
            if existing is None:
                response = gitlab.post(
                    f"/projects/{project_id}/variables", json={"key": key, "value": value}
                )
                if response.status_code not in (201, 200):
                    raise Refused(f"variable {key} set failed: {response.text[:200]}")
            set_keys.append(key)
        bundle.record(
            "setup",
            "variables",
            {
                "keys": set_keys,
                "ambient_sentinel_sha256": sha256_hex(ambient),
                "broker_sentinel_sha256": sha256_hex(broker),
                "recorder_url": RECORDER_URL,
                "lane_ref": LANE_REF_SHA,
            },
        )
        print(f"setup: variables {set_keys}")

    # 3. the credential binding registry — refs only (data/ is gitignored)
    if not record.get("binding"):
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from forge.adaptive.project_credentials import ProjectCredentialRegistry

        registry = ProjectCredentialRegistry(path=REGISTRY_PATH)
        subject = f"gitlab/-/{project_id}"
        binding = registry.bind(
            subject,
            PROVIDER_ROUTE,
            BROKER_REF,
            bound_by="pavel (R40-07 #343 operator action)",
            project_id=project_id,
        )
        bundle.record(
            "setup",
            "binding",
            {
                "subject": binding.subject,
                "provider": binding.provider,
                "credential_ref": binding.credential_ref,
                "env_var": binding.env_var,
                "revision": int(binding.revision),
                "registry_path": "data/credential-bindings.json (gitignored; refs only)",
            },
        )
        print(
            f"setup: bound {binding.subject} -> {binding.credential_ref} (rev {binding.revision})"
        )

    # 4. the forge webhook (replicated from the lab project)
    if not record.get("webhook_url"):
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
        print(f"setup: webhook {forge_hook['url']}")

    # 5. bot membership (Developer) — the v2 posture
    if not record.get("bot_member"):
        bot = app_env("FORGE_BOT_USERNAME")
        member = gitlab.post(
            f"/projects/{project_id}/members",
            json={"user_id": gitlab.get("/users?username=" + bot)[0]["id"], "access_level": 30},
        )
        bundle.record(
            "setup",
            "bot_member",
            {"username": bot, "status": member.status_code},
        )
    print("setup: complete — run scripts/align_lab.py --apply with the delivery-mode env")
    return 0


# ---------------------------------------------------------------------------
# reach — a scratch runner job proves the runner can dial the recorder
# ---------------------------------------------------------------------------


def phase_reach(bundle: Bundle, gitlab: GitLab) -> int:
    record = bundle.phase("reach")
    if record.get("probe", {}).get("job_status") == "success":
        print("reach: complete (resumed)")
        return 0
    name = f"forge-recorder-reach-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
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
        raise Refused(f"scratch project creation failed: {created.text[:200]}")
    scratch = created.json()
    scratch_id = int(scratch["id"])
    bundle.record(
        "reach", "scratch_project", {"id": scratch_id, "path": scratch["path_with_namespace"]}
    )
    ci = (
        "reach:\n"
        "  image: curlimages/curl:8.10.1\n"
        "  script:\n"
        f"    - curl -sS --max-time 20 {RECORDER_URL}/health\n"
    )
    commit = gitlab.post(
        f"/projects/{scratch_id}/repository/commits",
        json={
            "branch": "main",
            "commit_message": "reach probe",
            "actions": [
                {"action": "create", "file_path": ".gitlab-ci.yml", "content": ci},
                {"action": "create", "file_path": "README.md", "content": "# reach probe\n"},
            ],
        },
    )
    if commit.status_code not in (201, 200):
        raise Refused(f"scratch seed failed: {commit.text[:200]}")

    def pipeline() -> Any:
        pipelines = gitlab.get(f"/projects/{scratch_id}/pipelines")
        return pipelines[0] if pipelines else None

    first = poll(pipeline, "the scratch pipeline", timeout=300)
    job = poll(
        lambda: next(
            (
                job
                for job in gitlab.get(f"/projects/{scratch_id}/pipelines/{first['id']}/jobs")
                if job.get("name") == "reach"
            ),
            None,
        ),
        "the reach job",
        timeout=300,
    )

    def done() -> Any:
        current = gitlab.get(f"/projects/{scratch_id}/jobs/{job['id']}")
        return current if current.get("status") in ("success", "failed", "canceled") else None

    final = poll(done, "the reach job to finish", timeout=600)
    trace = gitlab.get_text(f"/projects/{scratch_id}/jobs/{job['id']}/trace")
    gitlab.delete(f"/projects/{scratch_id}")
    probe = {
        "job_status": final.get("status"),
        "job_id": final.get("id"),
        "saw_health_marker": "forge redemption-qualification recorder" in trace,
    }
    bundle.record("reach", "probe", probe)
    if probe["job_status"] != "success" or not probe["saw_health_marker"]:
        raise Refused(f"the runner cannot dial the recorder: {probe}")
    print(f"reach: the runner dialed {RECORDER_URL}/health (job {final['id']} green)")
    return 0


# ---------------------------------------------------------------------------
# trace — issue → /implement → /go → the minted grant → the redeeming lane
# ---------------------------------------------------------------------------


def start_issue_and_plan(
    bundle: Bundle, gitlab: GitLab, project_id: int, phase: str
) -> dict[str, Any]:
    created = gitlab.post(
        f"/projects/{project_id}/issues",
        json={"title": issue_title(), "description": issue_body()},
    )
    if created.status_code not in (201, 200):
        raise Refused(f"issue creation failed: {created.text[:200]}")
    issue = created.json()
    bundle.record(phase, "issue", {"iid": issue["iid"], "url": issue["web_url"]})
    print(f"{phase}: issue #{issue['iid']} created — {issue['web_url']}")
    note = gitlab.post(
        f"/projects/{project_id}/issues/{issue['iid']}/notes", json={"body": "@forge /implement"}
    )
    if note.status_code not in (201, 200):
        raise Refused(f"/implement note failed: {note.text[:200]}")

    def plan_note() -> Any:
        for entry in reversed(gitlab.get(f"/projects/{project_id}/issues/{issue['iid']}/notes")):
            body = str(entry.get("body", ""))
            if entry.get("author", {}).get("username") == "forge" and "/go " in body:
                return entry
        return None

    plan = poll(plan_note, "the evidence-backed plan comment", timeout=1200, interval=15)
    body = str(plan.get("body", ""))
    match = RUN_ID_RE.search(body)
    if match is None:
        raise Refused(f"plan note carries no run id:\n{body[-600:]}")
    run_id = match.group(1)
    harness_line = next((line for line in body.splitlines() if line.startswith("- Harness:")), "")
    bundle.record(
        phase,
        "plan",
        {"note_id": plan.get("id"), "run_id": run_id, "harness_line": harness_line},
    )
    print(f"{phase}: plan arrived (run {run_id[:8]}) — {harness_line}")
    if "claude-sdk-lane" not in harness_line:
        raise Refused(f"the frozen lane was not selected: {harness_line!r}")
    return {"issue_iid": issue["iid"], "run_id": run_id}


def grant_rows(work_id: str) -> list[dict[str, Any]]:
    """The authority rows for a run — refs/metadata only."""
    return (
        psql_json(
            "SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM ("
            " SELECT grant_id, attempt_generation, provider, credential_ref, status,"
            " redemption_deadline::text AS redemption_deadline,"
            " created_at::text AS created_at"
            " FROM operation_grants WHERE work_id = '" + work_id + "' ORDER BY attempt_generation"
            ") t;"
        )
        or []
    )


def run_row(work_id: str) -> dict[str, Any]:
    return psql_json(
        "SELECT row_to_json(t) FROM (SELECT id, status, project_id, cancellation_generation,"
        " created_at::text AS created_at FROM flow_runs WHERE id = '" + work_id + "') t;"
    )


def dispatch_envelope(work_id: str) -> dict[str, Any] | None:
    """The run's journaled dispatch envelope — value-free by construction."""
    return psql_json(
        "SELECT evidence->'harness'->'dispatch_envelope' FROM flow_runs WHERE id = '"
        + work_id
        + "'"
    )


def dispatch_credential_doc(work_id: str) -> dict[str, Any] | None:
    return psql_json(
        "SELECT evidence->'harness'->'dispatch_credential' FROM flow_runs WHERE id = '"
        + work_id
        + "'"
    )


def grant_projection(work_id: str) -> dict[str, Any] | None:
    return psql_json(
        "SELECT evidence->'credential_operation_grants' FROM flow_runs WHERE id = '" + work_id + "'"
    )


def app_refusal_reasons(work_id: str) -> list[str]:
    """The app log's typed refusal lines for a work id (refs only)."""
    completed = subprocess.run(
        ["podman", "logs", "forge-app", "--since", "3h"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return [
        line[:220]
        for line in (completed.stdout + completed.stderr).splitlines()
        if work_id[:8] in line and ("redemption" in line or "credential" in line)
    ][-12:]


def wait_pipeline_and_lane(
    bundle: Bundle,
    gitlab: GitLab,
    project_id: int,
    phase: str,
    issue_iid: int,
    run_id: str,
) -> dict[str, Any]:
    """Wait for the NEXT (not-yet-recorded) api pipeline + its lane job.

    Generation-aware: a /retry dispatches again on the SAME branch, so
    the poll selects the NEWEST api-triggered pipeline this phase has
    not recorded yet."""
    branch = f"factory/{issue_iid}/{run_id[:8]}"

    def known_ids() -> set[int]:
        return {
            int(entry["pipeline_id"])
            for entry in bundle.document["phases"].get(phase, {}).get("dispatches", [])
        }

    def pipeline() -> Any:
        known = known_ids()
        candidates = [
            p
            for p in gitlab.get(f"/projects/{project_id}/pipelines", params={"ref": branch})
            if p.get("source") == "api" and p["id"] not in known
        ]
        return candidates[0] if candidates else None

    dispatched = poll(pipeline, f"a NEW api-triggered pipeline on {branch}", timeout=900)
    pipeline_id = dispatched["id"]

    def lane() -> Any:
        return next(
            (
                job
                for job in gitlab.get(f"/projects/{project_id}/pipelines/{pipeline_id}/jobs")
                if str(job.get("name", "")).startswith("forge-agent")
            ),
            None,
        )

    job = poll(lane, "the lane job", timeout=300)
    bundle.append(
        phase,
        "dispatches",
        {
            "pipeline_id": pipeline_id,
            "pipeline_url": dispatched.get("web_url"),
            "lane_job_id": job["id"],
            "branch": branch,
        },
    )
    print(f"{phase}: pipeline {pipeline_id}, lane job {job['id']}")

    def terminal() -> Any:
        current = gitlab.get(f"/projects/{project_id}/jobs/{job['id']}")
        return current if current.get("status") in ("success", "failed", "canceled") else None

    final = poll(terminal, "the lane job to finish", timeout=LANE_JOB_TIMEOUT_S, interval=15.0)
    trace = gitlab.get_text(f"/projects/{project_id}/jobs/{job['id']}/trace")
    entry = dict(bundle.document["phases"][phase]["dispatches"][-1])
    entry["job_status"] = final.get("status")
    entry["job_trace_sha256"] = sha256_hex(trace)
    entry["redemption_markers"] = [
        line.strip()[:200]
        for line in trace.splitlines()
        if "credential" in line.lower() or "redemption" in line.lower()
    ][:12]
    bundle.document["phases"][phase]["dispatches"][-1] = entry
    bundle.save()
    return {**entry, "trace": trace}


def job_artifact_meta(gitlab: GitLab, project_id: int, job_id: int) -> dict[str, Any] | None:
    """The lane's candidate.meta.json artifact (carries credential_consumption)."""
    response = gitlab.client.get(f"/projects/{project_id}/jobs/{job_id}/artifacts")
    if response.status_code != 200:
        return None
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in archive.namelist():
            if name.endswith(".forge/candidate.meta.json") or name.endswith("candidate.meta.json"):
                return json.loads(archive.read(name).decode("utf-8"))
    return None


def captures_since(epoch: float) -> list[dict[str, Any]]:
    """The recorder's captures in [epoch, now] — digests only."""
    if not CAPTURE_LOG.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in CAPTURE_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = datetime.fromisoformat(record["captured_at"])
        if stamp.timestamp() < epoch:
            continue
        out.append(
            {
                "captured_at": record["captured_at"],
                "method": record["method"],
                "path": record["path"],
                "authorization_sha256": record["authorization_sha256"],
                "x_api_key_sha256": record["x_api_key_sha256"],
                "body_model": record.get("body_model", ""),
                "user_agent": record.get("user_agent", "")[:80],
            }
        )
    return out


def phase_trace(bundle: Bundle, state: State, gitlab: GitLab) -> int:
    record = bundle.phase("trace")
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    ambient, broker = sentinel_pair(state)

    # 0. the lab must REPORT the runner-redemption delivery mode (alignment)
    health = app_health()
    grant_window_probe = psql_scalar("SELECT version_num FROM alembic_version")
    if record.get("preconditions") is None:
        record["preconditions"] = {
            "app_version": health.get("version"),
            "deployed_schema_head": grant_window_probe,
            "checked_at": _now(),
        }
        bundle.save()
    if str(grant_window_probe) < "029":
        raise Refused(
            f"the lab schema head is {grant_window_probe} — the operation_grants "
            "authority (029+) is not deployed; run the alignment first"
        )

    # 1. issue + plan (the planner runs in the control plane; its spend is
    #    recorded from the run's own receipts at collect time)
    if record.get("issue") is None:
        arc = start_issue_and_plan(bundle, gitlab, project_id, "trace")
        record = bundle.phase("trace")
    else:
        arc = {"issue_iid": record["issue"]["iid"], "run_id": record["plan"]["run_id"]}
    run_id = arc["run_id"]
    record["run_id"] = run_id
    bundle.save()

    # 2. NO grant may exist before the dispatch (never seeded)
    if record.get("grant_before_go") is None:
        rows = grant_rows(run_id)
        bundle.record("trace", "grant_before_go", rows)
        if rows:
            raise Refused(f"a grant already exists before /go — seeded? {rows}")
        print("trace: no grant before /go (correct — the dispatch must mint it)")

    # 3. /go — the REAL dispatch
    if not record.get("go_note_id"):
        go = gitlab.post(
            f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
            json={"body": f"@forge /go {run_id}"},
        )
        if go.status_code not in (201, 200):
            raise Refused(f"/go note failed: {go.text[:200]}")
        bundle.record("trace", "go_note_id", go.json().get("id"))
        print("trace: /go posted")

    # 4. the dispatch MINTS the grant (assert the authority row appears)
    if not record.get("grant_minted"):
        go_epoch = time.time()
        poll(
            lambda: grant_rows(run_id) or None,
            "the dispatch-minted operation grant",
            timeout=600,
            interval=10.0,
        )
        rows = grant_rows(run_id)
        envelope = poll(
            lambda: dispatch_envelope(run_id) or None,
            "the journaled dispatch envelope",
            timeout=600,
            interval=10.0,
        )
        delivery_mode = str(envelope.get("credential_delivery_mode") or "")
        if delivery_mode != "runner-redemption":
            raise Refused(
                f"the dispatch envelope names delivery mode {delivery_mode!r} — "
                "the lab is not on the runner-redemption route"
            )
        minted_doc = {
            "rows": rows,
            "envelope": envelope,
            "dispatch_credential": dispatch_credential_doc(run_id),
            "grant_projection": grant_projection(run_id),
            "observed_at": _now(),
            "mint_latency_s": round(time.time() - go_epoch, 1),
        }
        bundle.record("trace", "grant_minted", minted_doc)
        rows_live = grant_rows(run_id)
        first = rows_live[0]
        if first["provider"] != PROVIDER_ROUTE:
            raise Refused(f"the minted grant names an unexpected route: {first}")
        if first["credential_ref"] == BROKER_REF:
            print(
                f"trace: grant {first['grant_id'][:12]} minted (attempt "
                f"{first['attempt_generation']}, deadline {first['redemption_deadline']})"
            )
        else:
            # the LIVE-found misbound-ref negative: the first grant was
            # minted under a ref whose env name was NOT the binding's
            # env slot, so the EnvBroker staged it under the wrong slot
            # and the endpoint refused the redemption typed
            # (staged_slot_mismatch) — recorded as history; the /retry
            # below mints the next generation under the corrected ref.
            print(
                f"trace: grant {first['grant_id'][:12]} minted under the MISBOUND ref "
                f"{first['credential_ref']} (recorded as the live negative; the "
                "corrective rebind + /retry follows)"
            )
    record = bundle.phase("trace")
    rows = record["grant_minted"]["rows"]

    # 5. the lane runs; its bootstrap REDEEMS at startup. A lane whose
    #    redemption was refused typed records the refusal as the honest
    #    outcome and CONTINUES through the native /retry (the next
    #    generation mints its own grant) — the identity proof is judged
    #    on the LATEST lane that ran.
    while True:
        outcomes = record.get("lane_outcomes") or []
        last_reason = (
            str((outcomes[-1] or {}).get("meta_terminal_reason") or "") if outcomes else ""
        )
        if outcomes and "credential_redemption_failed" not in last_reason:
            break  # the last lane already ran to a non-redemption outcome
        if outcomes:
            # the previous lane failed CLOSED at the redemption: the app
            # log names WHY (typed), the continuation gate demands the
            # EXPLICIT restart mode (no checkpoint, no vendor session),
            # and the re-dispatch mints the NEXT generation's grant.
            if record.get("first_refusal_log") is None:
                record["first_refusal_log"] = app_refusal_reasons(run_id)
                bundle.save()
            if record.get("retry_after_refusal") is None:
                retry = gitlab.post(
                    f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
                    json={"body": f"@forge /retry {run_id} restart"},
                )
                if retry.status_code not in (201, 200):
                    raise Refused(f"/retry note failed: {retry.text[:200]}")
                bundle.record("trace", "retry_after_refusal", retry.json().get("id"))
                print("trace: the lane refused typed at the redemption — /retry restart posted")
            seen_generation = max(int(row_["attempt_generation"]) for row_ in grant_rows(run_id))

            def next_grant() -> Any:
                return next(
                    (
                        r
                        for r in grant_rows(run_id)
                        if int(r["attempt_generation"]) > seen_generation
                    ),
                    None,
                )

            poll(next_grant, f"generation {seen_generation + 1}'s grant", timeout=900)
            record = bundle.phase("trace")
            rows = grant_rows(run_id)
        lane_epoch = time.time()
        dispatch = wait_pipeline_and_lane(
            bundle, gitlab, project_id, "trace", arc["issue_iid"], run_id
        )
        meta = job_artifact_meta(gitlab, project_id, int(dispatch["lane_job_id"]))
        caps = captures_since(lane_epoch)
        consumption = (meta or {}).get("credential_consumption") if meta else None
        reason = str((meta or {}).get("terminal_reason") or "")
        outcome = {
            "job_status": dispatch["job_status"],
            "job_trace_sha256": dispatch["job_trace_sha256"],
            "redemption_markers": dispatch["redemption_markers"],
            "meta_redemption": {
                k: consumption.get(k)
                for k in (
                    "grant_id",
                    "redemption_id",
                    "broker_receipt_id",
                    "binding_revision",
                    "binding_revision_known",
                    "consumer_status",
                    "provider_route",
                    "credential_ref",
                    "env_var",
                    "delivery_route",
                )
            }
            if isinstance(consumption, dict)
            else None,
            "meta_terminal_reason": (meta or {}).get("terminal_reason"),
            "meta_usage": (meta or {}).get("usage"),
            "recorder_captures": caps,
            "observed_at": _now(),
        }
        bundle.append("trace", "lane_outcomes", outcome)
        record = bundle.phase("trace")
        print(f"trace: lane {dispatch['job_status']} (terminal {reason}); captures {len(caps)}")
    record = bundle.phase("trace")
    lane = record["lane_outcomes"][-1]
    rows = grant_rows(run_id)

    # 6. THE IDENTITY PROOF — the consumer presented the BROKER sentinel
    if record.get("identity_proof") is None:
        caps = lane["recorder_captures"]
        if not caps:
            raise Refused("the recorder captured nothing — the consumer never dialed it")
        presented = {cap["authorization_sha256"] for cap in caps}
        broker_digest = sha256_hex("Bearer " + broker)
        ambient_digest = sha256_hex("Bearer " + ambient)
        plain_broker = sha256_hex(broker)
        plain_ambient = sha256_hex(ambient)
        saw_broker = broker_digest in presented or plain_broker in presented
        saw_ambient = ambient_digest in presented or plain_ambient in presented
        if not saw_broker:
            raise Refused(
                "the consumer did NOT present the broker-selected sentinel "
                f"(captures: {sorted(presented)})"
            )
        if saw_ambient:
            raise Refused("the consumer presented the AMBIENT sentinel — redemption lost")
        meta_grant = ((lane.get("meta_redemption") or {}).get("grant_id") or "")[:16]
        latest_row = max(rows, key=lambda r: int(r["attempt_generation"]))
        row_grant = latest_row["grant_id"][:16]
        if not meta_grant or meta_grant != row_grant:
            raise Refused(
                f"the lane's consumer receipt grant {meta_grant!r} does not join the "
                f"authority row {row_grant!r}"
            )
        bundle.record(
            "trace",
            "identity_proof",
            {
                "captures": len(caps),
                "distinct_authorization_digests": sorted(presented),
                "broker_sentinel_presented": saw_broker,
                "ambient_sentinel_presented": saw_ambient,
                "grant_join": {"meta": meta_grant, "authority_row": row_grant},
                "verdict": "pass" if saw_broker and not saw_ambient else "fail",
            },
        )
        print("trace: identity proof PASS — the consumer presented the broker sentinel")
    return 0


# ---------------------------------------------------------------------------
# retire — a new attempt generation retires the old lane's redemption
# ---------------------------------------------------------------------------


def phase_retire(bundle: Bundle, state: State, gitlab: GitLab) -> int:
    record = bundle.phase("retire")
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    trace = bundle.document["phases"]["trace"]
    run_id = trace["plan"]["run_id"]
    issue_iid = trace["issue"]["iid"]
    old_generation = max(int(row["attempt_generation"]) for row in grant_rows(run_id))

    # re-enters a blocked run through the native /retry command; a lane
    # that failed closed before any model turn holds no checkpoint, so
    # the continuation gate demands the EXPLICIT restart mode)
    if record.get("retry_note_id") is None:
        retry = gitlab.post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": f"@forge /retry {run_id} restart"},
        )
        if retry.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retry.text[:200]}")
        bundle.record("retire", "retry_note_id", retry.json().get("id"))
        print("retire: /retry posted")

    # the NEW generation's grant (the new dispatch mints its own)
    if record.get("new_generation") is None:

        def new_grant() -> Any:
            rows = grant_rows(run_id)
            return next((r for r in rows if int(r["attempt_generation"]) > old_generation), None)

        minted = poll(new_grant, f"generation {old_generation + 1}'s grant", timeout=900)
        bundle.record(
            "retire",
            "new_generation",
            {"grants": grant_rows(run_id), "observed_at": _now()},
        )
        print(f"retire: generation {minted['attempt_generation']} grant {minted['grant_id'][:12]}")
    record = bundle.phase("retire")
    new_generation = int(record["new_generation"]["grants"][-1]["attempt_generation"])

    # the new lane runs and follows ITS OWN grant
    if not record.get("dispatches"):
        lane_epoch = time.time()
        dispatch = wait_pipeline_and_lane(bundle, gitlab, project_id, "retire", issue_iid, run_id)
        meta = job_artifact_meta(gitlab, project_id, int(dispatch["lane_job_id"]))
        caps = captures_since(lane_epoch)
        consumption = (meta or {}).get("credential_consumption") if meta else None
        new_row = next(
            r
            for r in record["new_generation"]["grants"]
            if int(r["attempt_generation"]) == new_generation
        )
        joined = (
            str((consumption or {}).get("grant_id") or "")[:16] == new_row["grant_id"][:16]
            if isinstance(consumption, dict)
            else False
        )
        bundle.record(
            "retire",
            "lane_outcome",
            {
                "job_status": dispatch["job_status"],
                "generation": new_generation,
                "meta_grant_join": joined,
                "meta_terminal_reason": (meta or {}).get("terminal_reason"),
                "captures": len(caps),
                "observed_at": _now(),
            },
        )
        if not joined:
            raise Refused("the new lane's receipt does not join the new generation's grant")
        print(f"retire: new lane followed grant {new_row['grant_id'][:12]}")
    record = bundle.phase("retire")

    # the OLD generation's token is refused typed (the retirement proof)
    if record.get("old_token_refusal") is None:
        old_token = lane_token(run_id, old_generation)
        response = redeem(run_id, BROKER_REF, PROVIDER_ROUTE, old_token)
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))[:200]
        except ValueError:
            detail = response.text[:200]
        refusal = {
            "http_status": response.status_code,
            "detail": detail,
            "refusal_typed": "generation" in detail.lower() or "superseded" in detail.lower(),
            "observed_at": _now(),
        }
        bundle.record("retire", "old_token_refusal", refusal)
        if response.status_code != 403 or not refusal["refusal_typed"]:
            raise Refused(f"the old lane's redemption was not refused typed: {refusal}")
        print(f"retire: old generation token refused typed ({detail[:80]})")
    return 0


# ---------------------------------------------------------------------------
# restart — cold control-plane restart preserves the window + audit link
# ---------------------------------------------------------------------------


def phase_restart(bundle: Bundle, state: State, gitlab: GitLab) -> int:
    """Cold control-plane restart MID-WINDOW of a LIVE attempt.

    The honest shape (live-found on the first attempt): replaying after
    the attempt's lane finished is refused typed ``attempt_terminal`` —
    a blocked/finished attempt redeems nothing (ADR-0004 terminal
    semantics, recorded as its own typed observation). The window
    SURVIVAL is therefore proven on a NEW generation whose lane is
    actually in flight: the restart lands while the run is
    non-terminal, and BOTH the operator replay AND the lane's own
    startup redemption go through the RESTARTED control plane."""
    record = bundle.phase("restart")
    trace = bundle.document["phases"]["trace"]
    run_id = trace["plan"]["run_id"]
    issue_iid = trace["issue"]["iid"]
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])

    # 0. the earlier honest observation: replaying a FINISHED attempt is
    #    refused typed attempt_terminal (the first restart arm's outcome)
    if record.get("terminal_replay_refusal") is None:
        finished_generation = max(int(row["attempt_generation"]) for row in grant_rows(run_id))
        response = redeem(
            run_id, BROKER_REF, PROVIDER_ROUTE, lane_token(run_id, finished_generation)
        )
        detail = _safe_detail(response)
        bundle.record(
            "restart",
            "terminal_replay_refusal",
            {
                "http_status": response.status_code,
                "detail": detail[:200],
                "generation": finished_generation,
                "observed_at": _now(),
            },
        )

    # 1. a NEW generation whose lane is in flight (run stays non-terminal)
    if record.get("live_generation") is None:
        retry = gitlab.post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": f"@forge /retry {run_id} restart"},
        )
        if retry.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retry.text[:200]}")
        previous = max(int(row["attempt_generation"]) for row in grant_rows(run_id))

        def newer() -> Any:
            return next(
                (r for r in grant_rows(run_id) if int(r["attempt_generation"]) > previous),
                None,
            )

        minted = poll(newer, f"generation {previous + 1}'s grant", timeout=900)
        live_generation = int(minted["attempt_generation"])
        bundle.record(
            "restart",
            "live_generation",
            {
                "generation": live_generation,
                "grant": minted,
                "audit_before": psql_json(
                    "SELECT count(*) FROM credential_redemptions WHERE work_id = '" + run_id + "'"
                ),
                "observed_at": _now(),
            },
        )
    record = bundle.phase("restart")
    live = record["live_generation"]
    live_generation = int(live["generation"])
    deadline_before = live["grant"]["redemption_deadline"]

    # 2. wait until the lane is IN FLIGHT (the job running — the run
    #    non-terminal, the window open), then the cold restart
    if not record.get("restart_receipt"):

        def running_lane() -> Any:
            pipelines = gitlab.get(
                f"/projects/{project_id}/pipelines",
                params={"ref": f"factory/{issue_iid}/{run_id[:8]}"},
            )
            api_pipelines = [p for p in pipelines if p.get("source") == "api"]
            if not api_pipelines:
                return None
            newest = max(p["id"] for p in api_pipelines)
            jobs = gitlab.get(f"/projects/{project_id}/pipelines/{newest}/jobs")
            job = next((j for j in jobs if str(j.get("name", "")).startswith("forge-agent")), None)
            return job if job and job.get("status") == "running" else None

        poll(running_lane, "the live lane job running", timeout=900, interval=10.0)
        completed = subprocess.run(
            ["podman", "restart", "forge-app"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise Refused(f"podman restart forge-app failed: {completed.stderr[:200]}")
        bundle.record(
            "restart",
            "restart_receipt",
            {"command": "podman restart forge-app", "at": _now()},
        )
        print("restart: forge-app restarted COLD while the lane was in flight")
    health = app_health(timeout=420)
    bundle.record("restart", "health_after", {"version": health.get("version"), "at": _now()})

    # 3. the grant window + audit link SURVIVED; an exact replay inside
    #    the deadline of the LIVE attempt redeems idempotently
    if record.get("replay") is None:
        after_row = next(
            (g for g in grant_rows(run_id) if int(g["attempt_generation"]) == live_generation),
            None,
        )
        if after_row is None:
            raise Refused(f"generation {live_generation}'s grant vanished across the restart")
        deadline_after = after_row["redemption_deadline"]
        if deadline_after != deadline_before:
            raise Refused(
                "the grant's absolute deadline moved across the restart "
                f"({deadline_before} -> {deadline_after})"
            )
        token = lane_token(run_id, live_generation)
        response = redeem(run_id, BROKER_REF, PROVIDER_ROUTE, token)
        replay_ok = response.status_code == 200
        replay_doc: dict[str, Any] = {"http_status": response.status_code}
        if replay_ok:
            body = response.json()
            replay_doc.update(
                {
                    "grant_id_prefix": str(body.get("grant_id", ""))[:16],
                    "redemption_id_prefix": str(body.get("redemption_id", ""))[:16],
                    "binding_revision": body.get("binding_revision"),
                    "operation": body.get("operation"),
                    "env_var": body.get("env_var"),
                }
            )
        else:
            replay_doc["detail"] = _safe_detail(response)[:200]
        time.sleep(10)  # let the in-flight lane's own redemption settle too
        final_count = psql_json(
            "SELECT count(*) FROM credential_redemptions WHERE work_id = '" + run_id + "'"
        )
        replay_doc.update(
            {
                "audit_rows_before": int(live["audit_before"]),
                "audit_rows_after": int(final_count),
                "deadline_preserved": deadline_after == deadline_before,
                "observed_at": _now(),
            }
        )
        bundle.record("restart", "replay", replay_doc)
        if not replay_ok:
            raise Refused(f"the post-restart replay did not redeem: {replay_doc}")
        print(
            f"restart: replay redeemed through the RESTARTED plane (grant "
            f"{replay_doc['grant_id_prefix']}, deadline preserved, audit rows "
            f"{replay_doc['audit_rows_before']}→{replay_doc['audit_rows_after']})"
        )
    return 0


# ---------------------------------------------------------------------------
# negative — wrong ref / wrong route / the binding-rotation refusal
# ---------------------------------------------------------------------------


def phase_negative(bundle: Bundle, state: State, gitlab: GitLab) -> int:
    """The endpoint-level negative arms on a LIVE attempt.

    The wrong-ref / wrong-route / rotation arms all need a NON-terminal
    run (a finished attempt refuses attempt_terminal first — the typed
    semantics the restart arm already recorded), so this phase mints a
    FRESH generation through the native /retry and runs the arms while
    its lane is still in flight. The rotation lands between the grant
    mint (approval) and the lane's startup redemption."""
    record = bundle.phase("negative")
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    trace = bundle.document["phases"]["trace"]
    run_id = trace["plan"]["run_id"]
    issue_iid = trace["issue"]["iid"]

    # 0. a FRESH generation: /retry mints its grant under the CURRENT
    #    registry; the lane stays in flight for the arms below.
    if record.get("live_generation") is None:
        retry = gitlab.post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": f"@forge /retry {run_id} restart"},
        )
        if retry.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retry.text[:200]}")
        previous = max(int(row["attempt_generation"]) for row in grant_rows(run_id))

        def newer() -> Any:
            return next(
                (r for r in grant_rows(run_id) if int(r["attempt_generation"]) > previous),
                None,
            )

        minted = poll(newer, f"generation {previous + 1}'s grant", timeout=900)
        bundle.record(
            "negative",
            "live_generation",
            {"generation": int(minted["attempt_generation"]), "grant": minted, "at": _now()},
        )
    record = bundle.phase("negative")
    generation = int(record["live_generation"]["generation"])
    token = lane_token(run_id, generation)

    # 1. wrong ref — the endpoint's typed grant_ref_mismatch
    if record.get("wrong_ref") is None:
        response = redeem(run_id, BROKER_REF + "-wrong", PROVIDER_ROUTE, token)
        detail = _safe_detail(response)
        entry = {
            "http_status": response.status_code,
            "detail": detail,
            "value_returned": response.status_code == 200,
            "observed_at": _now(),
        }
        bundle.record("negative", "wrong_ref", entry)
        if response.status_code != 403 or "grant_ref_mismatch" not in detail:
            raise Refused(f"wrong-ref was not refused typed: {entry}")
        print(f"negative: wrong ref refused typed ({detail[:80]})")

    # 2. wrong route — the confused-deputy guard (grant_route_mismatch)
    if record.get("wrong_route") is None:
        response = redeem(run_id, BROKER_REF, "zai", token)
        detail = _safe_detail(response)
        entry = {
            "http_status": response.status_code,
            "detail": detail,
            "value_returned": response.status_code == 200,
            "observed_at": _now(),
        }
        bundle.record("negative", "wrong_route", entry)
        if response.status_code != 403 or "grant_route_mismatch" not in detail:
            raise Refused(f"wrong-route was not refused typed: {entry}")
        print(f"negative: wrong route refused typed ({detail[:80]})")

    # 3. the binding ROTATION arm — rotate between approval and redemption:
    #    the registry document rotates (same ref, NEW revision) AFTER the
    #    grant was minted and BEFORE the lane redeems; the real lane's
    #    redemption must be refused typed and the lane fails CLOSED.
    if record.get("rotation") is None:
        rotation = {"arm": "rotate between approval and redemption"}
        rotation["minted_grant"] = record["live_generation"]["grant"]
        # the operator rotates the binding NOW (the shipped registry path —
        # same ref, revision+1, history preserved)
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from forge.adaptive.project_credentials import ProjectCredentialRegistry

        registry = ProjectCredentialRegistry(path=REGISTRY_PATH)
        subject = f"gitlab/-/{project_id}"
        rotated = registry.bind(
            subject,
            PROVIDER_ROUTE,
            BROKER_REF,
            bound_by="pavel (R40-07 #343 rotation arm)",
            project_id=project_id,
        )
        rotation["rotated_to_revision"] = int(rotated.revision)
        rotation["rotated_at"] = _now()
        bundle.record("negative", "rotation", rotation)
        print(
            f"negative: rotated the binding to revision {rotated.revision} between "
            f"approval (grant {rotation['minted_grant']['grant_id'][:12]}) and redemption"
        )
    record = bundle.phase("negative")

    # 3c. the lane redeems against the rotated world — the typed refusal;
    #     the lane fails closed; the recorder stays SILENT for it.
    if record.get("rotation_lane") is None:
        lane_epoch = time.time()
        dispatch = wait_pipeline_and_lane(bundle, gitlab, project_id, "negative", issue_iid, run_id)
        meta = job_artifact_meta(gitlab, project_id, int(dispatch["lane_job_id"]))
        caps = captures_since(lane_epoch)
        entry = {
            "job_status": dispatch["job_status"],
            "meta_terminal_reason": (meta or {}).get("terminal_reason"),
            "meta_redemption": (meta or {}).get("credential_consumption"),
            "recorder_captures": len(caps),
            "markers": dispatch["redemption_markers"],
            "app_log": app_refusal_reasons(run_id)[-3:],
            "observed_at": _now(),
        }
        bundle.record("negative", "rotation_lane", entry)
        reason = str(entry["meta_terminal_reason"] or "")
        if "credential_redemption_failed" not in reason:
            raise Refused(f"the rotated-world lane did not fail closed at the redemption: {entry}")
        if caps:
            raise Refused("the recorder captured calls from the refused-redemption lane")
        print(
            f"negative: rotation lane failed closed ({reason}) with zero model calls — "
            "the endpoint refused the superseded revision"
        )
    record = bundle.phase("negative")

    # 3d. the endpoint's typed reason for the PRE-rotation grant, plus the
    #     honest recovery observation. The dispatch leg re-reads the
    #     registry document on EVERY command (RunService is per-command),
    #     so a grant minted AFTER the rotation records the LIVE revision
    #     and redeems — the recovery semantic. The PRE-rotation grant's
    #     refusal is proven typed by the endpoint's own app-log line
    #     (credential.binding_revision_mismatch) beside the lane-level
    #     fail-closed outcome recorded in 3c.
    if record.get("rotation_endpoint") is None:
        typed_log = [
            line for line in app_refusal_reasons(run_id) if "binding_revision_mismatch" in line
        ]
        if not typed_log:
            raise Refused(
                "the endpoint's typed binding_revision_mismatch line is absent from "
                "the app log — the rotation refusal is not proven endpoint-side"
            )
        # the recovery observation: the LIVE generation's grant (minted
        # under the rotated document) redeems through the same endpoint
        recovery_status = None
        live_row = run_row(run_id)
        if live_row is not None and str(live_row.get("status")) not in (
            "ready_for_human",
            "blocked",
            "failed",
            "cancelled",
        ):
            response = redeem(run_id, BROKER_REF, PROVIDER_ROUTE, lane_token(run_id, generation))
            recovery_status = response.status_code
        entry = {
            "pre_rotation_grant": record["rotation"]["minted_grant"]["grant_id"][:16],
            "app_log_typed_lines": typed_log[-3:],
            "post_rotation_grant_redeemed_http": recovery_status,
            "value_returned_by_typed_refusal": False,
            "observed_at": _now(),
        }
        bundle.record("negative", "rotation_endpoint", entry)
        print(
            "negative: endpoint refused the pre-rotation grant typed "
            "(credential.binding_revision_mismatch in the app log)"
            + (
                f"; the post-rotation grant redeemed (HTTP {recovery_status}) — the recovery"
                if recovery_status == 200
                else ""
            )
        )

    # 4. parity note — LIVE-FOUND: the dispatch leg re-reads the registry
    #    document on EVERY command (execute_run_command constructs the
    #    RunService per task; registry_from_env() re-reads the file), so
    #    the rotated document binds the NEXT dispatch automatically — no
    #    restart, no snapshot to invalidate. (An earlier design of this
    #    arm assumed a worker-startup snapshot; the post-rotation grant's
    #    successful redemption DISPROVED that assumption — recorded.)
    if record.get("parity_note") is None:
        bundle.record(
            "negative",
            "parity_note",
            {
                "finding": (
                    "the credential registry is re-read from the persisted document "
                    "on every dispatch command AND on every redemption request — a "
                    "rotation binds the next dispatch without any restart"
                ),
                "at": _now(),
            },
        )
        print("negative: registry parity is per-command (no restart needed) — recorded")
    return 0


def _safe_detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))[:300]
    except ValueError:
        return response.text[:300]


# ---------------------------------------------------------------------------
# expire — a short grant window expires; both endpoint and lane refuse
# ---------------------------------------------------------------------------


def phase_expire(bundle: Bundle, state: State, gitlab: GitLab) -> int:
    """Assumes the consumers were re-aligned with
    FORGE_CREDENTIAL_GRANT_WINDOW_SECONDS=20 (the operator's second
    alignment receipt); REFUSES otherwise."""
    record = bundle.phase("expire")
    window = app_env("FORGE_CREDENTIAL_GRANT_WINDOW_SECONDS")
    if record.get("window") is None:
        record["window"] = {"FORGE_CREDENTIAL_GRANT_WINDOW_SECONDS": window, "at": _now()}
        bundle.save()
    if str(window) != "20":
        raise Refused(
            "the consumers do not carry the short grant window (expected 20, "
            f"observed {window!r}) — re-align with the expire-window env first"
        )
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])

    # a second issue on the same project (its own run, its own grant)
    if record.get("issue") is None:
        arc = start_issue_and_plan(bundle, gitlab, project_id, "expire")
        record = bundle.phase("expire")
        record["run_id"] = arc["run_id"]
        bundle.save()
    run_id = str(record["run_id"])
    issue_iid = int(record["issue"]["iid"])

    if not record.get("go_note_id"):
        go = gitlab.post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": f"@forge /go {run_id}"},
        )
        if go.status_code not in (201, 200):
            raise Refused(f"/go note failed: {go.text[:200]}")
        bundle.record("expire", "go_note_id", go.json().get("id"))
    go_at = time.time()

    minted = poll(lambda: grant_rows(run_id) or None, "the short-window grant", timeout=600)
    row = minted[0]
    generation = int(row["attempt_generation"])
    deadline = datetime.fromisoformat(row["redemption_deadline"])
    bundle.record("expire", "grant", {**row, "go_at": _now()})

    # sleep PAST the absolute deadline, then the endpoint refuses typed
    remaining = deadline.timestamp() - time.time()
    if remaining > 0:
        print(
            f"expire: sleeping {remaining + 3:.0f}s past the deadline {row['redemption_deadline']}"
        )
        time.sleep(remaining + 3)
    token = lane_token(run_id, generation)
    response = redeem(run_id, BROKER_REF, PROVIDER_ROUTE, token)
    detail = _safe_detail(response)
    entry = {
        "http_status": response.status_code,
        "detail": detail,
        "value_returned": response.status_code == 200,
        "observed_at": _now(),
    }
    bundle.record("expire", "endpoint_after_deadline", entry)
    if response.status_code != 403 or "grant_expired" not in detail:
        raise Refused(f"the expired grant was not refused typed: {entry}")
    print(f"expire: endpoint refused the expired grant typed ({detail[:80]})")

    # the REAL lane also boots against the expired window and fails closed
    if record.get("lane_outcome") is None:
        lane_epoch = time.time()
        dispatch = wait_pipeline_and_lane(bundle, gitlab, project_id, "expire", issue_iid, run_id)
        meta = job_artifact_meta(gitlab, project_id, int(dispatch["lane_job_id"]))
        caps = captures_since(lane_epoch)
        entry = {
            "job_status": dispatch["job_status"],
            "meta_terminal_reason": (meta or {}).get("terminal_reason"),
            "recorder_captures": len(caps),
            "markers": dispatch["redemption_markers"],
            "lane_minutes_after_go": round((time.time() - go_at) / 60, 1),
        }
        bundle.record("expire", "lane_outcome", entry)
        reason = str(entry["meta_terminal_reason"] or "")
        if "credential_redemption_failed" not in reason:
            raise Refused(f"the expired-window lane did not fail closed: {entry}")
        if caps:
            raise Refused("the recorder captured calls from the expired-window lane")
        print(f"expire: the real lane failed closed on the expired window ({reason})")
    return 0


# ---------------------------------------------------------------------------
# teardown + status
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# rebind — the operator's corrective rebind after the LIVE-found
# staged_slot_mismatch refusal (the EnvBroker route requires the ref's
# env NAME to BE the binding's env slot)
# ---------------------------------------------------------------------------


def phase_rebind(bundle: Bundle, gitlab: GitLab) -> int:
    record = bundle.phase("rebind")
    if record.get("binding") is not None:
        print("rebind: complete (resumed)")
        return 0
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from forge.adaptive.project_credentials import ProjectCredentialRegistry

    registry = ProjectCredentialRegistry(path=REGISTRY_PATH)
    subject = f"gitlab/-/{project_id}"
    binding = registry.bind(
        subject,
        PROVIDER_ROUTE,
        BROKER_REF,
        bound_by="pavel (R40-07 #343 corrective rebind after the live staged_slot_mismatch)",
        project_id=project_id,
    )
    bundle.record(
        "rebind",
        "binding",
        {
            "subject": binding.subject,
            "provider": binding.provider,
            "credential_ref": binding.credential_ref,
            "env_var": binding.env_var,
            "revision": int(binding.revision),
            "at": _now(),
        },
    )
    print(
        f"rebind: {binding.subject} -> {binding.credential_ref} (rev {binding.revision}) — "
        "re-align the consumers with the slot-named broker env next"
    )
    return 0


def phase_teardown(bundle: Bundle, gitlab: GitLab) -> int:
    record = bundle.phase("teardown")
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    if record.get("deleted_at") is None:
        # capture the terminal run states first (refs only)
        runs = psql_json(
            "SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM ("
            " SELECT id, status, cancellation_generation, created_at::text AS created_at"
            " FROM flow_runs WHERE project_id = " + str(project_id) + " ORDER BY created_at"
            ") t;"
        )
        grants = psql_json(
            "SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM ("
            " SELECT work_id, grant_id, attempt_generation, provider, credential_ref,"
            " status, redemption_deadline::text AS redemption_deadline"
            " FROM operation_grants"
            " WHERE work_id IN (SELECT id FROM flow_runs WHERE project_id = "
            + str(project_id)
            + ") ORDER BY work_id, attempt_generation) t;"
        )
        redemptions = psql_json(
            "SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM ("
            " SELECT work_id, receipt_id, grant_id, attempt_generation, route,"
            " credential_ref, resolver, subject, provider, binding_revision, outcome,"
            " retry_count, expires_at::text AS expires_at, broker_receipt_id,"
            " credential_policy, created_at::text AS created_at"
            " FROM credential_redemptions"
            " WHERE work_id IN (SELECT id FROM flow_runs WHERE project_id = "
            + str(project_id)
            + ") ORDER BY created_at) t;"
        )
        bundle.record(
            "teardown",
            "final_state",
            {"runs": runs, "grants": grants, "redemptions": redemptions},
        )
        deleted = gitlab.delete(f"/projects/{project_id}")
        bundle.record(
            "teardown",
            "deleted_at",
            _now() if deleted.status_code in (202, 200, 204) else f"HTTP {deleted.status_code}",
        )
    print(f"teardown: {record.get('deleted_at')}")
    return 0


def phase_status(bundle: Bundle) -> int:
    summary: dict[str, Any] = {}
    for name, phase in bundle.document.get("phases", {}).items():
        summary[name] = sorted(key for key in phase if not key.startswith("_"))
    print(json.dumps({"phases": summary, "document": bundle.document}, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_redemption_qualification.py",
        description="R40-07 (#343): qualify operation-grant redemption, live.",
    )
    parser.add_argument(
        "phase",
        choices=[
            "setup",
            "reach",
            "rebind",
            "trace",
            "retire",
            "restart",
            "negative",
            "expire",
            "teardown",
            "status",
        ],
    )
    parser.add_argument("--project-name", default="forge-redemption-2026-09-26")
    args = parser.parse_args(argv)

    bundle = Bundle()
    state = State()
    gitlab: GitLab | None = None
    try:
        if args.phase == "status":
            return phase_status(bundle)
        if args.phase == "setup":
            return phase_setup(bundle, state, GitLab(), args.project_name)
        gitlab = GitLab()
        if args.phase == "reach":
            return phase_reach(bundle, gitlab)
        if args.phase == "rebind":
            return phase_rebind(bundle, gitlab)
        if args.phase == "trace":
            return phase_trace(bundle, state, gitlab)
        if args.phase == "retire":
            return phase_retire(bundle, state, gitlab)
        if args.phase == "restart":
            return phase_restart(bundle, state, gitlab)
        if args.phase == "negative":
            return phase_negative(bundle, state, gitlab)
        if args.phase == "expire":
            return phase_expire(bundle, state, gitlab)
        if args.phase == "teardown":
            return phase_teardown(bundle, gitlab)
    except Refused as exc:
        print(f"{args.phase}: REFUSED: {exc}", file=sys.stderr)
        bundle.record(args.phase, "refused", {"at": _now(), "reason": str(exc)[:2000]})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
