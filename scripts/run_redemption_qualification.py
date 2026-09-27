"""R41-10 (#365) — the CURRENT pairing's grant-consumer trace, live.

The R40-07 (#343) record proved the redemption transport with a
CONTROLLED recorder and an OLDER lane package (the promoted v0.39.0
tree b521e1a) against newer control-plane code — strong transport
evidence, but not a qualification of the CURRENT runner-side checks or
the CURRENT pairing. This driver re-runs the sentinel consumer trace
with the SELECTED pair #363's manifest pins:

- the CONTROL PLANE from the CURRENT TREE — the working-tree alignment
  build (scripts/align_lab.py, schema head 032, the #341/#342/#365
  machinery INCLUDING this cycle's locator-to-env-slot preflight);
- the LANE installing the CURRENT TREE'S LANE PACKAGE — the pinned
  wheel of the same composition (``dist/forge-0.41.0-py3-none-any.whl``
  at the freeze's sha256), sha256-verified INSIDE the lane job before
  install and echoed into the trace, so the pairing is proven by the
  lane's own bytes, never asserted by the driver.

The shipped SDK-lane template is included VERBATIM (its exact bytes
committed into the disposable project and included by local include);
the ONE local override is the install seam — the template's
``git+…@${FORGE_LANE_REF}`` line swapped for the pinned wheel, the way
the GitHub template's FORGE_LANE_WHEEL ladder does it (Q35-08/R36-07).
The identity proof keeps #343's controlled-endpoint allowance: the
lane's model route points at the RECORDING endpoint (zero real model
calls); the recorder captures the Authorization header the vendor
client presented, and the driver asserts the BROKER-SELECTED sentinel
arrived while the competing AMBIENT one never did.

Phases (issue #365 acceptance mapped):

1. ``setup``    — disposable project + the shipped template verbatim +
   the wheel-override CI + both sentinels + the binding registry (refs
   only) + the webhook. The lab alignment runs OUTSIDE this driver.
2. ``reach``    — a scratch runner job proves the runner dials the
   recorder AND fetches the pinned wheel (sha256 verified in-job).
3. ``reject``   — the CURRENT runner's response fence, proven BY THE
   INSTALLED WHEEL on the real runner: doctored redemption responses
   (wrong attempt/route/ref/slot/binding-revision/expiry) are refused
   typed before any vendor client exists; the lane-side preflight unit
   checks ride the same job.
4. ``trace``    — native issue → /implement → NO grant before /go →
   /go MINTS the grant through the native path → the lane bootstrap
   REDEEMS it → the consumer receipt joins the authority row → the
   recorder captured EXACTLY the broker sentinel. The lane trace must
   carry the wheel-identity echo (the pairing proof).
5. ``preflight``— the locator-to-env-slot preflight, live: the bind-time
   refusal (typed, at the operator's bind moment), then the #343
   incident shape (a misbound binding hand-written past the bind seam)
   dispatched at a CURRENT lane — which refuses at ITS OWN preflight
   BEFORE the redemption call — then the corrective rebind and the
   recovered lane (the rotation runbook, live).
6. ``rotate``   — a same-ref binding revision change MID-FLIGHT (rev
   N→N+1): the endpoint refuses the old grant typed
   binding_revision_mismatch (zero emitted values), the lane fails
   closed, and a post-rotation grant redeems (the recovery semantic);
   wrong-ref / wrong-route probes refuse typed on a live attempt.
7. ``retire``   — a new attempt generation: the old lane's token refuses
   typed (superseded), the new lane follows its OWN grant.
8. ``expire``   — short-window grants: the endpoint AND the real lane
   refuse typed grant_expired; the deadline is ABSOLUTE across a cold
   control-plane restart mid-window (deadline unchanged, idempotent
   replay inside the window, typed refusal after it).
9. ``teardown`` — the disposable project is deleted (capture first).

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
EVAL_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-27-redemption-pairing"
EVIDENCE_PATH = EVAL_DIR / "live-run-evidence.json"
STATE_PATH = REPO_ROOT / "data" / "redemption-pairing" / "state.json"
RECORDER_DIR = REPO_ROOT / "data" / "redemption-recorder"
CAPTURE_LOG = RECORDER_DIR / "capture.jsonl"
REGISTRY_PATH = REPO_ROOT / "data" / "credential-bindings.json"
TEMPLATE_SOURCE = REPO_ROOT / "ci" / "templates" / "claude-sdk-lane.gitlab-ci.yml"
ALIGNMENT_RECEIPTS = EVAL_DIR / "alignment-receipts.json"

#: The CURRENT pairing's lane package: the pinned wheel of the current
#: tree's build (the composition the #363 freeze pins; rebuilt this
#: cycle WITH the R41-10 preflight — the re-freeze records the new sha).
#: Served from the lab host (the recorder precedent's LAN route) and
#: sha256-verified inside the lane job BEFORE install.
LANE_WHEEL_NAME = "forge-0.41.0-py3-none-any.whl"
LANE_WHEEL_PATH = REPO_ROOT / "dist" / LANE_WHEEL_NAME
LANE_WHEEL_SHA256 = "2616d22130f32eb8f8b09a5e0e74d988aa23746efcaecb3640108b4fd85a8fe4"

#: The lab host's LAN address (the runner dials the recorder and the
#: wheel host here; the same reachability the lab's tinyproxy precedent
#: established).
LAB_HOST_LAN_IP = "192.168.1.18"
RECORDER_PORT = 8480
RECORDER_URL = f"http://{LAB_HOST_LAN_IP}:{RECORDER_PORT}"
WHEEL_HOST_PORT = 8481
WHEEL_URL = f"http://{LAB_HOST_LAN_IP}:{WHEEL_HOST_PORT}/{LANE_WHEEL_NAME}"

#: The bound credential ref (the EnvBroker resolves it from the
#: control-plane consumers' env). The ref's env NAME must BE the
#: binding's env slot — the R41-10 preflight now refuses anything else
#: at BIND time, and the lane-side twin refuses it at lane boot.
BROKER_REF = "env:ANTHROPIC_AUTH_TOKEN"
PROVIDER_ROUTE = "anthropic-gateway"
ENV_SLOT = "ANTHROPIC_AUTH_TOKEN"

#: The misbound locator of the #343 incident (the preflight arm's shape).
MISBOUND_REF = "env:FORGE_BROKER_MODEL_TOKEN"

APP_API = "http://localhost:8420"
RUN_ID_RE = re.compile(r"\b([0-9a-f]{32})\b")
UTC = ZoneInfo("UTC")

POLL_INTERVAL_S = 10.0
LANE_JOB_TIMEOUT_S = 1800.0  # npm+wheel install + the bounded driver turn


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
                "stamp": "forge.redemption.pairing/1",
                "issue": "forge#365 (R41-10) — the current pairing, live",
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
    """The (ambient, broker) sentinel VALUES — private, generated once.

    The AMBIENT sentinel is FRESH this cycle. The BROKER sentinel is the
    value the lab's consumers already hold in ``ANTHROPIC_AUTH_TOKEN``
    (carried over from the #343 cycle — the redemption machinery
    delivers whatever value the broker holds; the identity proof is
    about WHICH value arrives, so the carried value is recorded
    honestly in the bundle and a FRESH ambient competitor guarantees
    the distinction)."""
    import secrets

    existing = state.get("sentinels")
    if isinstance(existing, dict) and existing.get("ambient") and existing.get("broker"):
        return str(existing["ambient"]), str(existing["broker"])
    broker = ""
    source = None
    previous = REPO_ROOT / "data" / "redemption-qualification" / "state.json"
    if previous.is_file():
        try:
            carried = json.loads(previous.read_text(encoding="utf-8")).get("sentinels") or {}
            broker = str(carried.get("broker") or "")
            source = "carried from the #343 cycle (the consumers' live ANTHROPIC_AUTH_TOKEN)"
        except json.JSONDecodeError:
            broker = ""
    if not broker:
        # fall back to the value actually held by the consumers
        completed = subprocess.run(
            ["podman", "exec", "forge-app", "printenv", ENV_SLOT],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            broker = completed.stdout.strip()
            source = "read from the consumers' live env at setup"
    pair = {
        "ambient": "forge-ambient-sentinel-" + secrets.token_hex(16),
        "broker": broker,
        "broker_provenance": source or "generated fresh (no carried value found)",
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


def app_health(timeout: float = 420.0) -> dict[str, Any]:
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
# The CURRENT pairing's CI: the shipped template VERBATIM (committed,
# locally included) + the ONE install-seam override (the pinned wheel)
# ---------------------------------------------------------------------------


#: The wheel-verification + install steps that REPLACE the template's
#: single ``uv pip install … git+…@${FORGE_LANE_REF}`` line (the same
#: ladder the GitHub template ships for FORGE_LANE_WHEEL — Q35-08).
#: NOTE: an f-string — shell ``${VAR}`` spellings are brace-escaped.
_WHEEL_INSTALL_STEPS = (
    f"    # R41-10 (#365) — the CURRENT pairing's lane package: the pinned\n"
    f"    # wheel of the qualified composition, sha256-verified BEFORE install\n"
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

#: The marker the lane trace must carry for the driver to accept the
#: pairing (the lane's OWN echo of the installed package identity).
PAIRING_MARKER_RE = re.compile(
    r"forge lane package version=(?P<version>\S+) wheel=(?P<wheel>\S+) sha256=(?P<sha>[0-9a-f]{64})"
)


def ci_yaml() -> str:
    """The disposable project's CI: the shipped SDK lane template included
    VERBATIM from the committed copy, plus the ONE override — the lane
    job's ``before_script`` with the install seam swapped to the pinned
    wheel of the current composition (rules/variables/script/artifacts
    stay the included template's)."""
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
        "# Generated by scripts/run_redemption_qualification.py (R41-10/#365):\n"
        "# the SHIPPED SDK lane template VERBATIM (committed under\n"
        "# .forge-template/ and included BY local include) + the ONE override:\n"
        "# the lane job's before_script install seam carries the pinned wheel\n"
        "# of the CURRENT composition instead of git+${FORGE_LANE_REF}.\n"
        "include:\n"
        "  - local: '.forge-template/claude-sdk-lane.gitlab-ci.yml'\n"
        "stages: [test, harness]\n\n"
        "forge-agent-claude-sdk:\n"
        "  before_script:" + overridden.rstrip("\n") + "\n\nsmoke:\n"
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
            f"# {name}\n\nThe R41-10 (#365) current-pairing redemption\n"
            "qualification disposable project — deleted after capture. The lane\n"
            "installs the pinned wheel of the current composition; the model route\n"
            "points at the local recorder, so zero real model calls are made.\n"
        ),
        ".gitlab-ci.yml": ci_yaml(),
        ".forge-template/claude-sdk-lane.gitlab-ci.yml": TEMPLATE_SOURCE.read_text(
            encoding="utf-8"
        ),
        "src/app.py": 'def greet(name: str) -> str:\n    return f"hello {name}"\n',
    }


def issue_title() -> str:
    return "Add src/utils/echo.py with echo(text) and a test"


def issue_body() -> str:
    return (
        "Add `src/utils/echo.py` defining `echo(text: str) -> str` returning the\n"
        "text unchanged, plus `tests/test_echo.py` with one exact assertion.\n"
        "\n"
        "(The R41-10 current-pairing trace: the lane's model endpoint is a local\n"
        "recorder for identity proof — the driver leg is EXPECTED to fail fast\n"
        "with zero model turns; the candidate is not the point of this run.)\n"
    )


# ---------------------------------------------------------------------------
# The scratch-jobs' probes (reach + reject): the files the jobs run
# ---------------------------------------------------------------------------


REJECT_PROBE = '''"""R41-10 (#365) — the CURRENT runner's response fence, proven BY THE
INSTALLED WHEEL on the real runner: doctored redemption responses are
refused typed BEFORE any vendor client exists. Prints one
FORGE_REJECT:<axis>:ok marker per axis; exits nonzero on any miss."""
import os
import sys
from datetime import datetime, timedelta, timezone

from forge.lane_driver import (
    LaneCredentialRedemptionError,
    lane_binding_slot_preflight,
    verify_redemption_response,
)

NOW = datetime.now(timezone.utc)
EXPECTED = {
    "work_id": "run-reject-probe",
    "provider": "anthropic-gateway",
    "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
    "env_var": "ANTHROPIC_AUTH_TOKEN",
    "attempt_generation": 2,
    "binding_revision": 1,
}


def document(**overrides):
    fields = {
        "value": "delivered",
        "env_var": "ANTHROPIC_AUTH_TOKEN",
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "redemption_id": "r1",
        "grant_id": "g1",
        "work_id": "run-reject-probe",
        "provider": "anthropic-gateway",
        "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
        "attempt_generation": 2,
        "operation": "credential-redemption",
        "redemption_deadline": (NOW + timedelta(hours=2)).isoformat(),
        "binding_revision": 1,
    }
    fields.update(overrides)
    return fields


failures = []


def probe(axis, mutation):
    try:
        verify_redemption_response(document(**mutation), expected=EXPECTED)
    except LaneCredentialRedemptionError as exc:
        marker = "credential_redemption_failed" in str(exc)
        print(f"FORGE_REJECT:{axis}:{'ok' if marker else 'UNTYPED: ' + str(exc)[:120]}")
        if not marker:
            failures.append(axis)
        return
    print(f"FORGE_REJECT:{axis}:NOT-REFUSED")
    failures.append(axis)


probe("wrong-attempt", {"attempt_generation": 9})
probe("wrong-route", {"provider": "openai"})
probe("wrong-ref", {"credential_ref": "env:OPENAI_API_KEY"})
probe("wrong-slot", {"env_var": "OPENAI_API_KEY"})
probe("wrong-binding-revision", {"binding_revision": 2})
probe("expired", {"expires_at": (NOW - timedelta(seconds=1)).isoformat()})
probe("wrong-operation", {"operation": "credential-exfiltration"})
verify_redemption_response(document(), expected=EXPECTED)
print("FORGE_REJECT:correct-document:ok")

reason = lane_binding_slot_preflight(
    {
        "FORGE_WORK_ID": "run-reject-probe",
        "FORGE_LANE_DRIVER": "claude",
        "FORGE_CREDENTIAL_REF": "env:FORGE_BROKER_MODEL_TOKEN",
    }
)
ok = reason is not None and reason.startswith("preflight.binding_slot_mismatch")
print(f"FORGE_REJECT:lane-preflight-misbound:{'ok' if ok else 'NOT-REFUSED'}")
if not ok:
    failures.append("lane-preflight-misbound")

if failures:
    print("FORGE_REJECT:all:" + ",".join(failures))
    sys.exit(1)
print("FORGE_REJECT:all:ok")
'''


# ---------------------------------------------------------------------------
# setup — the disposable project, both sentinels, the binding registry
# ---------------------------------------------------------------------------


def phase_setup(bundle: Bundle, state: State, gitlab: GitLab, name: str) -> int:
    record = bundle.phase("setup")
    ambient, broker = sentinel_pair(state)
    wheel_sha = hashlib.sha256(LANE_WHEEL_PATH.read_bytes()).hexdigest()
    if wheel_sha != LANE_WHEEL_SHA256:
        raise Refused(
            f"the staged wheel digest moved ({wheel_sha} != {LANE_WHEEL_SHA256}) — "
            "the pairing pin and the served bytes diverged"
        )

    # 1. the disposable project + the seed commit (template verbatim +
    #    the wheel-override CI)
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
                    "seed: the shipped SDK lane template verbatim + the wheel-override "
                    "CI (committed before any run) — R41-10/#365"
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
        bundle.record(
            "setup",
            "ci_yaml_sha256",
            hashlib.sha256(ci_yaml().encode()).hexdigest(),
        )
        bundle.record(
            "setup",
            "install_seam",
            {
                "route": "wheel (the current composition's pinned lane package)",
                "wheel_name": LANE_WHEEL_NAME,
                "wheel_sha256": wheel_sha,
                "wheel_url": WHEEL_URL,
                "template_included": ".forge-template/claude-sdk-lane.gitlab-ci.yml "
                "(the shipped bytes, committed; the local include carries "
                "rules/variables/script/artifacts)",
                "override": "before_script install seam only "
                "(git+${FORGE_LANE_REF} -> the sha256-verified wheel ladder)",
            },
        )
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])

    # 2. the sentinel-bearing CI variables — the AMBIENT credential the
    #    lane's ordinary env carries (exactly what an ambient fallback
    #    would have presented), the recorder as the model route, and the
    #    pinned wheel the lane job installs.
    if not record.get("variables"):
        desired: list[tuple[str, str, str]] = [
            (ENV_SLOT, "literal:" + ambient, "the AMBIENT sentinel (fresh this cycle)"),
            ("ANTHROPIC_BASE_URL", "literal:" + RECORDER_URL, "the recording model endpoint"),
            ("FORGE_LANE_WHEEL", "literal:" + WHEEL_URL, "the current pairing's lane wheel"),
            ("FORGE_LANE_WHEEL_SHA256", "literal:" + wheel_sha, "the wheel pin"),
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
                "broker_provenance": state.get("sentinels", {}).get("broker_provenance"),
                "recorder_url": RECORDER_URL,
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
            bound_by="pavel (R41-10 #365 operator action)",
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

    # 6. CI-config validation — a manual pipeline on main proves the
    #    include+override merge parses server-side (the smoke job runs
    #    python-exit-0; the lane job's rules skip without FORGE_RUN_ID)
    #    BEFORE any dispatch is burned on a broken composition.
    if record.get("config_pipeline") is None:
        created_pipeline = gitlab.post(f"/projects/{project_id}/pipeline?ref=main")
        entry: dict[str, Any] = {"http_status": created_pipeline.status_code}
        if created_pipeline.status_code in (201, 200):
            entry["pipeline_id"] = created_pipeline.json().get("id")
            entry["status"] = created_pipeline.json().get("status")
        else:
            entry["error"] = created_pipeline.text[:300]
        bundle.record("setup", "config_pipeline", entry)
        if created_pipeline.status_code not in (201, 200):
            raise Refused(
                "the composed CI (template include + the wheel-override merge) was "
                f"refused by GitLab: {entry}"
            )
        print(f"setup: CI config validated (pipeline {entry['pipeline_id']} created)")
    print("setup: complete — ensure the recorder + wheel host are up before `reach`")
    return 0


# ---------------------------------------------------------------------------
# reach — the runner dials the recorder AND fetches the pinned wheel
# ---------------------------------------------------------------------------


def phase_reach(bundle: Bundle, gitlab: GitLab) -> int:
    record = bundle.phase("reach")
    if record.get("probe", {}).get("job_status") == "success":
        print("reach: complete (resumed)")
        return 0
    name = f"forge-pairing-reach-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
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
        f"    - curl -fsSL --max-time 120 -o /tmp/forge-lane.whl {WHEEL_URL}\n"
        f'    - echo "{LANE_WHEEL_SHA256}  /tmp/forge-lane.whl" | sha256sum -c -\n'
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
        "wheel_sha_verified": "OK" in trace and LANE_WHEEL_SHA256[:16] in trace,
    }
    bundle.record("reach", "probe", probe)
    if probe["job_status"] != "success" or not probe["saw_health_marker"]:
        raise Refused(f"the runner cannot dial the recorder: {probe}")
    if not probe["wheel_sha_verified"]:
        raise Refused(f"the runner could not fetch/verify the pinned wheel: {probe}")
    print(
        f"reach: the runner dialed {RECORDER_URL}/health and verified the wheel "
        f"sha256 {LANE_WHEEL_SHA256[:16]}… (job {final['id']} green)"
    )
    return 0


# ---------------------------------------------------------------------------
# reject — the CURRENT runner's response fence, by the installed wheel
# ---------------------------------------------------------------------------


def phase_reject(bundle: Bundle, gitlab: GitLab) -> int:
    """The response-rejection arm ON THE CURRENT PAIRING: a scratch lane
    job installs the SAME pinned wheel (sha-verified) and runs the
    doctored-response matrix against the wheel's OWN verification — the
    exact bytes the qualifying lane runs, on the real runner. The
    offline #342 suites remain the named coverage for the full matrix;
    this arm proves the INSTALLED runner carries the fence."""
    record = bundle.phase("reject")
    if record.get("probe", {}).get("job_status") == "success":
        print("reject: complete (resumed)")
        return 0
    name = f"forge-pairing-reject-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
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
        "reject", "scratch_project", {"id": scratch_id, "path": scratch["path_with_namespace"]}
    )
    ci = (
        "reject:\n"
        "  image: python:3.13-bookworm\n"
        "  script:\n"
        f'    - curl -fsSL --max-time 180 -o "/tmp/{LANE_WHEEL_NAME}" {WHEEL_URL}\n'
        f'    - echo "{LANE_WHEEL_SHA256}  /tmp/{LANE_WHEEL_NAME}" | sha256sum -c -\n'
        "    - python3 -m pip install --quiet --break-system-packages uv\n"
        "    - uv venv /tmp/reject-venv --python 3.13\n"
        "    - >\n"
        "      uv pip install --quiet --python /tmp/reject-venv/bin/python\n"
        f'      "forge[interactive] @ file:///tmp/{LANE_WHEEL_NAME}"\n'
        "    - /tmp/reject-venv/bin/python -c 'import forge; print(\"reject probe on forge\", forge.__version__)'\n"
        "    - /tmp/reject-venv/bin/python reject_probe.py\n"
    )
    commit = gitlab.post(
        f"/projects/{scratch_id}/repository/commits",
        json={
            "branch": "main",
            "commit_message": "the current runner's response fence (R41-10/#365)",
            "actions": [
                {"action": "create", "file_path": ".gitlab-ci.yml", "content": ci},
                {"action": "create", "file_path": "reject_probe.py", "content": REJECT_PROBE},
                {"action": "create", "file_path": "README.md", "content": "# reject probe\n"},
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
                if job.get("name") == "reject"
            ),
            None,
        ),
        "the reject job",
        timeout=300,
    )

    def done() -> Any:
        current = gitlab.get(f"/projects/{scratch_id}/jobs/{job['id']}")
        return current if current.get("status") in ("success", "failed", "canceled") else None

    final = poll(done, "the reject job to finish", timeout=900)
    trace = gitlab.get_text(f"/projects/{scratch_id}/jobs/{job['id']}/trace")
    gitlab.delete(f"/projects/{scratch_id}")
    # GitLab wraps trace lines with timestamps + ANSI escapes; the markers
    # are extracted from the RAW text wherever they appear.
    markers = sorted(set(re.findall(r"FORGE_REJECT:[A-Za-z0-9,-]+:[^\s\x1b]*", trace)))
    version_match = re.search(r"reject probe on forge (\S+)", trace)
    probe = {
        "job_status": final.get("status"),
        "job_id": final.get("id"),
        "markers": markers,
        "all_ok": "FORGE_REJECT:all:ok" in markers,
        "installed_version": version_match.group(1) if version_match else "",
        "job_trace_sha256": sha256_hex(trace),
    }
    bundle.record("reject", "probe", probe)
    if probe["job_status"] != "success" or not probe["all_ok"]:
        raise Refused(f"the installed runner's fence was not proven: {probe}")
    print(
        f"reject: the INSTALLED wheel refused every doctored response typed "
        f"({len(markers) - 1} axes; job {final['id']} green)"
    )
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


def app_redemption_lines(since: str = "3h") -> list[str]:
    """Every /lane/credentials/redeem request line in the app log window."""
    completed = subprocess.run(
        ["podman", "logs", "forge-app", "--since", since],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return [
        line[:220]
        for line in (completed.stdout + completed.stderr).splitlines()
        if "/lane/credentials/redeem" in line
    ]


def pairing_marker(trace: str) -> dict[str, str] | None:
    """The lane's OWN echo of the installed package identity, from its trace."""
    for line in trace.splitlines():
        match = PAIRING_MARKER_RE.search(line)
        if match:
            return match.groupdict()
    return None


def wait_pipeline_and_lane(
    bundle: Bundle,
    gitlab: GitLab,
    project_id: int,
    phase: str,
    issue_iid: int,
    run_id: str,
) -> dict[str, Any]:
    """Wait for the NEXT (not-yet-recorded) api pipeline + its lane job."""
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
    entry["pairing_marker"] = pairing_marker(trace)
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

    # 0. the lab must REPORT the runner-redemption delivery mode + the
    #    current pairing's alignment (schema 032, the current version)
    health = app_health()
    schema_probe = psql_scalar("SELECT version_num FROM alembic_version")
    if record.get("preconditions") is None:
        record["preconditions"] = {
            "app_version": health.get("version"),
            "deployed_schema_head": schema_probe,
            "delivery_mode": app_env("FORGE_CREDENTIAL_DELIVERY"),
            "checked_at": _now(),
        }
        bundle.save()
    if str(schema_probe) < "032":
        raise Refused(
            f"the lab schema head is {schema_probe} — the current pairing runs "
            "schema 032; run the alignment first"
        )

    # 1. issue + plan
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
        first = rows[0]
        if first["provider"] != PROVIDER_ROUTE or first["credential_ref"] != BROKER_REF:
            raise Refused(f"the minted grant names an unexpected route/ref: {first}")
        revision_doc = dispatch_credential_doc(run_id) or {}
        print(
            f"trace: grant {first['grant_id'][:12]} minted (attempt "
            f"{first['attempt_generation']}, binding revision "
            f"{revision_doc.get('binding_revision')}, deadline {first['redemption_deadline']})"
        )
    record = bundle.phase("trace")

    # 5. the lane runs; its bootstrap REDEEMS at startup. A lane whose
    #    redemption was refused typed records the refusal and CONTINUES
    #    through the native /retry; a lane that crashed BEFORE the driver
    #    ran (no pairing marker, no meta — an install-level miss) is
    #    equally retryable — the identity proof is judged on the LATEST
    #    lane that ran, bounded to three install-level retries.
    install_retries = 0
    while True:
        outcomes = record.get("lane_outcomes") or []
        if outcomes:
            last = outcomes[-1]
            reason = str(last.get("meta_terminal_reason") or "")
            marker_missing = last.get("pairing_marker") is None
            if "credential_redemption_failed" not in reason and not marker_missing:
                break  # the last lane RAN (pairing proven) to a non-redemption outcome
            if marker_missing and "credential_redemption_failed" not in reason:
                install_retries += 1
                if install_retries > 3:
                    raise Refused(
                        "the lane keeps crashing before its driver runs (no pairing "
                        "marker, no meta) — an install-level defect, not an arm outcome"
                    )
                print(
                    "trace: the lane crashed before its driver ran (install-level) — "
                    "/retry restart posted"
                )
            if record.get("first_refusal_log") is None:
                record["first_refusal_log"] = app_refusal_reasons(run_id)
                bundle.save()
            # each retryable outcome gets its OWN continuation note (the
            # dispatch leg mints the NEXT generation's grant; a lane that
            # failed closed before any model turn holds no checkpoint, so
            # the continuation gate demands the EXPLICIT restart mode)
            retry = gitlab.post(
                f"/projects/{project_id}/issues/{arc['issue_iid']}/notes",
                json={"body": f"@forge /retry {run_id} restart"},
            )
            if retry.status_code not in (201, 200):
                raise Refused(f"/retry note failed: {retry.text[:200]}")
            bundle.append("trace", "retry_notes", retry.json().get("id"))
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
            "pairing_marker": dispatch.get("pairing_marker"),
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
                    "operation",
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

    # 6. THE PAIRING PROOF — the lane's own trace names the wheel
    if record.get("pairing_proof") is None:
        marker = lane.get("pairing_marker") or {}
        if marker.get("sha") != LANE_WHEEL_SHA256:
            raise Refused(
                "the lane's trace does not name the pinned wheel "
                f"({marker!r} != sha {LANE_WHEEL_SHA256[:16]}…) — the pairing is unproven"
            )
        bundle.record(
            "trace",
            "pairing_proof",
            {
                "lane_echo": marker,
                "expected_wheel_sha256": LANE_WHEEL_SHA256,
                "verdict": "pass" if marker.get("sha") == LANE_WHEEL_SHA256 else "fail",
            },
        )
        print(f"trace: pairing proof PASS — the lane ran the wheel {LANE_WHEEL_SHA256[:16]}…")

    # 7. THE IDENTITY PROOF — the consumer presented the BROKER sentinel
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
# preflight — the locator-to-env-slot fence, live (bind-time + lane boot)
# ---------------------------------------------------------------------------


def _registry_module() -> Any:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from forge.adaptive import project_credentials

    return project_credentials


def phase_preflight(bundle: Bundle, state: State, gitlab: GitLab) -> int:
    """The R41-10 preflight, live, in three acts (the rotation runbook):

    1. the BIND-TIME refusal — the shipped registry's bind() refuses the
       misbound locator typed (``binding_slot_mismatch`` /
       ``preflight.binding_slot_mismatch``) at the operator's bind
       moment, before any dispatch;
    2. the #343 INCIDENT SHAPE — a misbound binding hand-written PAST
       the bind seam (the persisted document edited directly, the only
       remaining route now that bind() refuses) dispatched at a CURRENT
       lane: the lane refuses at ITS OWN preflight BEFORE the redemption
       call (zero endpoint traffic for that work id, zero model calls);
    3. the CORRECTIVE REBIND — the slot-named ref re-bound (a NEW
       revision), a new grant minted under it, and the lane RECOVERS
       (redeems; the recorder captures the broker sentinel)."""
    record = bundle.phase("preflight")
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])
    trace = bundle.document["phases"]["trace"]
    run_id = trace["plan"]["run_id"]
    issue_iid = trace["issue"]["iid"]
    subject = f"gitlab/-/{project_id}"

    # 1. the bind-time refusal (typed, at the bind moment)
    if record.get("bind_time_refusal") is None:
        module = _registry_module()
        registry = module.ProjectCredentialRegistry(path=REGISTRY_PATH)
        entry: dict[str, Any] = {"attempted_ref": MISBOUND_REF}
        try:
            registry.bind(
                subject, PROVIDER_ROUTE, MISBOUND_REF, bound_by="pavel (R41-10 preflight arm)"
            )
        except module.CredentialRefusal as refusal:
            entry.update(
                {
                    "reason": refusal.reason,
                    "detail": refusal.detail,
                    "observability": refusal.detail.get("observability"),
                    "typed": refusal.reason == "binding_slot_mismatch",
                }
            )
        else:
            entry["typed"] = False
            entry["note"] = "the misbound bind SUCCEEDED — the preflight did not fire"
        bundle.record("preflight", "bind_time_refusal", entry)
        if not entry.get("typed"):
            raise Refused(f"the bind-time preflight did not refuse the misbound ref: {entry}")
        print(f"preflight: bind-time refusal typed ({entry['reason']}) — the earliest fence held")

    # 2. the incident shape: hand-write the misbound binding past the bind
    #    seam, dispatch a generation, watch the CURRENT lane refuse at ITS
    #    OWN preflight (before the redemption call).
    if record.get("misbound_registry") is None:
        document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        live = next(
            (
                b
                for b in document.get("bindings", [])
                if b.get("subject") == subject and b.get("provider") == PROVIDER_ROUTE
            ),
            None,
        )
        if live is None:
            raise Refused(f"no live binding for {subject} — run setup first")
        misbound = dict(live)
        misbound["credential_ref"] = MISBOUND_REF
        misbound["bound_by"] = "hand-edit past the bind seam (the R41-10 incident drill)"
        misbound["bound_at"] = _now()
        document["bindings"] = [misbound if b is live else b for b in document.get("bindings", [])]
        REGISTRY_PATH.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        bundle.record(
            "preflight",
            "misbound_registry",
            {
                "subject": subject,
                "hand_edited_to": MISBOUND_REF,
                "revision_kept": int(live.get("revision") or 0),
                "note": "the persisted document was edited DIRECTLY (the operator-error "
                "shape of the #343 incident) — bind() refuses this ref now, so the "
                "hand-edit is the only remaining route; the lane-side preflight is "
                "the fence that must catch it",
                "at": _now(),
            },
        )
        print(f"preflight: registry hand-edited to the misbound ref {MISBOUND_REF}")

    if record.get("misbound_retry_note_id") is None:
        retry = gitlab.post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": f"@forge /retry {run_id} restart"},
        )
        if retry.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retry.text[:200]}")
        bundle.record("preflight", "misbound_retry_note_id", retry.json().get("id"))
        previous = max(int(r["attempt_generation"]) for r in grant_rows(run_id))
        poll(
            lambda: next(
                (r for r in grant_rows(run_id) if int(r["attempt_generation"]) > previous), None
            ),
            f"the misbound generation {previous + 1}'s grant",
            timeout=900,
        )
        print("preflight: misbound generation dispatched")

    if record.get("misbound_lane") is None:
        lane_epoch = time.time()
        redeem_lines_before = set(app_redemption_lines())
        dispatch = wait_pipeline_and_lane(
            bundle, gitlab, project_id, "preflight", issue_iid, run_id
        )
        meta = job_artifact_meta(gitlab, project_id, int(dispatch["lane_job_id"]))
        caps = captures_since(lane_epoch)
        trace_text = dispatch["trace"]
        marker_lines = [
            line.strip()[:220]
            for line in trace_text.splitlines()
            if "preflight.binding_slot_mismatch" in line
        ]
        new_redeem_lines = [
            line
            for line in app_redemption_lines()
            if line not in redeem_lines_before and run_id[:8] in line
        ]
        entry = {
            "job_status": dispatch["job_status"],
            "meta_terminal_reason": (meta or {}).get("terminal_reason"),
            "meta_redemption": (meta or {}).get("credential_consumption"),
            "lane_preflight_marker_lines": marker_lines,
            "endpoint_calls_for_this_work": new_redeem_lines,
            "recorder_captures": len(caps),
            "observed_at": _now(),
        }
        bundle.record("preflight", "misbound_lane", entry)
        reason = str(entry["meta_terminal_reason"] or "")
        if "credential_redemption_failed" not in reason:
            raise Refused(f"the misbound lane did not fail closed: {entry}")
        if not marker_lines:
            raise Refused(
                "the lane's trace carries no preflight.binding_slot_mismatch marker — "
                f"the lane-side preflight did not fire: {entry}"
            )
        if new_redeem_lines:
            raise Refused(
                "the endpoint SAW a redemption call for the misbound work — the "
                "lane-side preflight fired too late"
            )
        if caps:
            raise Refused("the recorder captured calls from the misbound lane")
        print(
            "preflight: the CURRENT lane refused the misbound ref at ITS OWN preflight "
            f"({reason}) — zero endpoint calls, zero model calls"
        )

    # 3. the corrective rebind + the recovered lane
    if record.get("corrective_rebind") is None:
        module = _registry_module()
        registry = module.ProjectCredentialRegistry(path=REGISTRY_PATH)
        binding = registry.bind(
            subject,
            PROVIDER_ROUTE,
            BROKER_REF,
            bound_by="pavel (R41-10 corrective rebind after the preflight refusal)",
            project_id=project_id,
        )
        bundle.record(
            "preflight",
            "corrective_rebind",
            {
                "credential_ref": binding.credential_ref,
                "revision": int(binding.revision),
                "at": _now(),
            },
        )
        print(f"preflight: corrective rebind to {binding.credential_ref} (rev {binding.revision})")

    if record.get("recovery_retry_note_id") is None:
        retry = gitlab.post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": f"@forge /retry {run_id} restart"},
        )
        if retry.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retry.text[:200]}")
        bundle.record("preflight", "recovery_retry_note_id", retry.json().get("id"))
        previous = max(int(r["attempt_generation"]) for r in grant_rows(run_id))
        poll(
            lambda: next(
                (r for r in grant_rows(run_id) if int(r["attempt_generation"]) > previous), None
            ),
            f"the recovery generation {previous + 1}'s grant",
            timeout=900,
        )

    if record.get("recovery_lane") is None:
        ambient, broker = sentinel_pair(state)
        lane_epoch = time.time()
        dispatch = wait_pipeline_and_lane(
            bundle, gitlab, project_id, "preflight", issue_iid, run_id
        )
        meta = job_artifact_meta(gitlab, project_id, int(dispatch["lane_job_id"]))
        caps = captures_since(lane_epoch)
        consumption = (meta or {}).get("credential_consumption") if meta else None
        presented = {cap["authorization_sha256"] for cap in caps}
        broker_digest = sha256_hex("Bearer " + broker)
        joined = (
            str((consumption or {}).get("grant_id") or "")[:16]
            == max(grant_rows(run_id), key=lambda r: int(r["attempt_generation"]))["grant_id"][:16]
        )
        entry = {
            "job_status": dispatch["job_status"],
            "meta_terminal_reason": (meta or {}).get("terminal_reason"),
            "meta_grant_join": joined,
            "binding_revision": (consumption or {}).get("binding_revision")
            if isinstance(consumption, dict)
            else None,
            "recorder_captures": len(caps),
            "broker_sentinel_presented": broker_digest in presented
            or sha256_hex(broker) in presented,
            "pairing_marker": dispatch.get("pairing_marker"),
            "observed_at": _now(),
        }
        bundle.record("preflight", "recovery_lane", entry)
        if not entry["meta_grant_join"] or not entry["broker_sentinel_presented"]:
            raise Refused(f"the corrective-rebind lane did not recover: {entry}")
        print(
            f"preflight: RECOVERY — the re-bound lane redeemed (rev "
            f"{entry['binding_revision']}) and presented the broker sentinel"
        )
    return 0


# ---------------------------------------------------------------------------
# rotate — a same-ref binding revision change MID-FLIGHT
# ---------------------------------------------------------------------------


def _safe_detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))[:300]
    except ValueError:
        return response.text[:300]


def phase_rotate(bundle: Bundle, state: State, gitlab: GitLab) -> int:
    """The same-ref binding revision change (rev N→N+1 at the SAME
    locator) MID-FLIGHT, on a LIVE attempt:

    - a fresh generation's grant mints under the live revision N;
    - the wrong-ref / wrong-route probes refuse typed (the confused-deputy
      guards) while the attempt is live;
    - the registry ROTATES (same ref, new revision N+1) between the mint
      and the lane's redemption: the ENDPOINT refuses the old grant typed
      ``binding_revision_mismatch`` (zero emitted values), and the real
      lane fails CLOSED (zero model calls — the recorder stays silent);
    - the RECOVERY: a post-rotation grant (minted under N+1) redeems and
      its lane presents the broker sentinel."""
    record = bundle.phase("rotate")
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
        previous = max(int(r["attempt_generation"]) for r in grant_rows(run_id))

        def newer() -> Any:
            return next(
                (r for r in grant_rows(run_id) if int(r["attempt_generation"]) > previous),
                None,
            )

        minted = poll(newer, f"generation {previous + 1}'s grant", timeout=900)
        revision_doc = dispatch_credential_doc(run_id) or {}
        bundle.record(
            "rotate",
            "live_generation",
            {
                "generation": int(minted["attempt_generation"]),
                "grant": minted,
                "minted_under_binding_revision": revision_doc.get("binding_revision"),
                "at": _now(),
            },
        )
    record = bundle.phase("rotate")
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
        bundle.record("rotate", "wrong_ref", entry)
        if response.status_code != 403 or "grant_ref_mismatch" not in detail:
            raise Refused(f"wrong-ref was not refused typed: {entry}")
        print(f"rotate: wrong ref refused typed ({detail[:80]})")

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
        bundle.record("rotate", "wrong_route", entry)
        if response.status_code != 403 or "grant_route_mismatch" not in detail:
            raise Refused(f"wrong-route was not refused typed: {entry}")
        print(f"rotate: wrong route refused typed ({detail[:80]})")

    # 3. the ROTATION: same ref, revision N+1 — between the mint and the
    #    lane's redemption.
    if record.get("rotation") is None:
        rotation = {"arm": "same-ref rebind, revision N->N+1, between mint and redemption"}
        rotation["minted_grant"] = record["live_generation"]["grant"]
        rotation["minted_under_binding_revision"] = record["live_generation"][
            "minted_under_binding_revision"
        ]
        module = _registry_module()
        registry = module.ProjectCredentialRegistry(path=REGISTRY_PATH)
        subject = f"gitlab/-/{project_id}"
        rotated = registry.bind(
            subject,
            PROVIDER_ROUTE,
            BROKER_REF,
            bound_by="pavel (R41-10 #365 rotation arm)",
            project_id=project_id,
        )
        rotation["rotated_to_revision"] = int(rotated.revision)
        rotation["rotated_at"] = _now()
        bundle.record("rotate", "rotation", rotation)
        print(
            f"rotate: binding rotated to revision {rotated.revision} between the mint "
            f"(grant {rotation['minted_grant']['grant_id'][:12]}, rev "
            f"{rotation['minted_under_binding_revision']}) and the redemption"
        )
    record = bundle.phase("rotate")

    # 3b. the ENDPOINT refuses the OLD grant typed — the direct probe
    #     (zero emitted values; the refusal detail names the type).
    if record.get("old_grant_endpoint_refusal") is None:
        response = redeem(run_id, BROKER_REF, PROVIDER_ROUTE, token)
        detail = _safe_detail(response)
        entry = {
            "http_status": response.status_code,
            "detail": detail,
            "typed": "binding_revision_mismatch" in detail,
            "value_returned": response.status_code == 200,
            "observed_at": _now(),
        }
        bundle.record("rotate", "old_grant_endpoint_refusal", entry)
        if response.status_code != 403 or not entry["typed"]:
            raise Refused(f"the old-revision grant was not refused typed by the endpoint: {entry}")
        print(f"rotate: endpoint refused the old-revision grant typed ({detail[:100]})")

    # 3c. the lane redeems against the rotated world — the typed refusal;
    #     the lane fails closed; the recorder stays SILENT for it.
    if record.get("rotation_lane") is None:
        lane_epoch = time.time()
        dispatch = wait_pipeline_and_lane(bundle, gitlab, project_id, "rotate", issue_iid, run_id)
        meta = job_artifact_meta(gitlab, project_id, int(dispatch["lane_job_id"]))
        caps = captures_since(lane_epoch)
        entry = {
            "job_status": dispatch["job_status"],
            "meta_terminal_reason": (meta or {}).get("terminal_reason"),
            "meta_redemption": (meta or {}).get("credential_consumption"),
            "recorder_captures": len(caps),
            "markers": dispatch["redemption_markers"],
            "app_log": app_refusal_reasons(run_id)[-3:],
            "pairing_marker": dispatch.get("pairing_marker"),
            "observed_at": _now(),
        }
        bundle.record("rotate", "rotation_lane", entry)
        reason = str(entry["meta_terminal_reason"] or "")
        if "credential_redemption_failed" not in reason:
            raise Refused(f"the rotated-world lane did not fail closed at the redemption: {entry}")
        if caps:
            raise Refused("the recorder captured calls from the refused-redemption lane")
        print(
            f"rotate: the lane failed closed ({reason}) with zero model calls — the "
            "endpoint refused the superseded revision"
        )
    record = bundle.phase("rotate")

    # 4. the RECOVERY — a post-rotation grant redeems; the lane presents
    #    the broker sentinel again (the rotation runbook's happy end).
    if record.get("recovery_retry_note_id") is None:
        # the run must PARK first (a /retry against a waiting_harness run
        # is refused by the continuation gate — live-found: the lane's
        # terminal report takes a beat to land in the run status)
        poll(
            lambda: (
                run_row(run_id)
                if str((run_row(run_id) or {}).get("status") or "")
                in ("blocked", "failed", "ready_for_human")
                else None
            ),
            "the rotated run to park (blocked/failed)",
            timeout=600,
            interval=10.0,
        )
        retry = gitlab.post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": f"@forge /retry {run_id} restart"},
        )
        if retry.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retry.text[:200]}")
        bundle.record("rotate", "recovery_retry_note_id", retry.json().get("id"))
        previous = max(int(r["attempt_generation"]) for r in grant_rows(run_id))

        def newer2() -> Any:
            return next(
                (r for r in grant_rows(run_id) if int(r["attempt_generation"]) > previous),
                None,
            )

        minted = poll(newer2, f"the post-rotation generation {previous + 1}'s grant", timeout=900)
        revision_doc = dispatch_credential_doc(run_id) or {}
        bundle.record(
            "rotate",
            "post_rotation_grant",
            {
                "generation": int(minted["attempt_generation"]),
                "grant": minted,
                "minted_under_binding_revision": revision_doc.get("binding_revision"),
                "at": _now(),
            },
        )
    record = bundle.phase("rotate")

    if record.get("recovery_lane") is None:
        ambient, broker = sentinel_pair(state)
        lane_epoch = time.time()
        dispatch = wait_pipeline_and_lane(bundle, gitlab, project_id, "rotate", issue_iid, run_id)
        meta = job_artifact_meta(gitlab, project_id, int(dispatch["lane_job_id"]))
        caps = captures_since(lane_epoch)
        consumption = (meta or {}).get("credential_consumption") if meta else None
        presented = {cap["authorization_sha256"] for cap in caps}
        post_gen = int(record["post_rotation_grant"]["generation"])
        joined = str((consumption or {}).get("grant_id") or "")[:16] == next(
            r["grant_id"][:16]
            for r in grant_rows(run_id)
            if int(r["attempt_generation"]) == post_gen
        )
        entry = {
            "job_status": dispatch["job_status"],
            "meta_terminal_reason": (meta or {}).get("terminal_reason"),
            "meta_grant_join": joined,
            "binding_revision": (consumption or {}).get("binding_revision")
            if isinstance(consumption, dict)
            else None,
            "recorder_captures": len(caps),
            "broker_sentinel_presented": sha256_hex("Bearer " + broker) in presented
            or sha256_hex(broker) in presented,
            "pairing_marker": dispatch.get("pairing_marker"),
            "observed_at": _now(),
        }
        bundle.record("rotate", "recovery_lane", entry)
        if not entry["meta_grant_join"] or not entry["broker_sentinel_presented"]:
            raise Refused(f"the post-rotation lane did not redeem: {entry}")
        print(
            f"rotate: RECOVERY — the post-rotation grant redeemed (rev "
            f"{entry['binding_revision']}) and the lane presented the broker sentinel"
        )
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

    if record.get("retry_note_id") is None:
        retry = gitlab.post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": f"@forge /retry {run_id} restart"},
        )
        if retry.status_code not in (201, 200):
            raise Refused(f"/retry note failed: {retry.text[:200]}")
        bundle.record("retire", "retry_note_id", retry.json().get("id"))
        print("retire: /retry posted")

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
                "pairing_marker": dispatch.get("pairing_marker"),
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
        detail = _safe_detail(response)
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
# expire — short-window grants: typed refusal + absoluteness across a
# cold restart mid-window
# ---------------------------------------------------------------------------


def phase_expire(bundle: Bundle, state: State, gitlab: GitLab) -> int:
    """Assumes the consumers were re-aligned with the short grant window
    named in the phase's own window probe (the operator's alignment
    receipt); REFUSES otherwise. Arms:

    - ``endpoint_after_deadline`` — the typed grant_expired refusal once
      the absolute deadline passed;
    - ``lane_outcome`` — the REAL lane boots against the expired window
      and fails closed (zero model calls);
    - ``restart_mid_window`` — a cold forge-app restart INSIDE a live
      window: the deadline row is byte-identical after the restart, an
      exact replay inside the window redeems IDEMPOTENTLY through the
      restarted plane, and the same grant refuses typed grant_expired
      once the deadline passed (the expiry is ABSOLUTE, never restarted)."""
    record = bundle.phase("expire")
    window = app_env("FORGE_CREDENTIAL_GRANT_WINDOW_SECONDS")
    if record.get("window") is None:
        record["window"] = {"FORGE_CREDENTIAL_GRANT_WINDOW_SECONDS": window, "at": _now()}
        bundle.save()
    if str(window) not in ("20", "240"):
        raise Refused(
            "the consumers do not carry a short grant window (expected 20 for the "
            f"refusal arms or 240 for the mid-window restart arm, observed {window!r}) "
            "— re-align with the expire-window env first"
        )
    project_id = int(bundle.document["phases"]["setup"]["project"]["id"])

    # ARM C (operator sets record["arm_c"]=true before the run, at window
    # 20): the typed expiry refusal THROUGH THE RESTARTED plane on a LIVE
    # attempt — the restart lands BEFORE the mint (the restart downtime
    # would otherwise eat the short window, and the mid-window
    # deadline-preserved + idempotent-replay proof is arm B's at 240 s);
    # a FRESH issue keeps the probe inside the lane's boot window (a
    # reused /retry's lane parks a warm runner inside ~90 s — live-found).
    if record.get("arm_c") and record.get("restart_receipt") is None:
        completed = subprocess.run(
            ["podman", "restart", "forge-app"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise Refused(f"podman restart forge-app failed: {completed.stderr[:200]}")
        health = app_health(timeout=420)
        bundle.record(
            "expire",
            "restart_receipt",
            {
                "command": "podman restart forge-app (BEFORE the mint — arm B carries "
                "the mid-window restart)",
                "at": _now(),
                "health_version_after": health.get("version"),
            },
        )
        print("expire: forge-app restarted COLD before the short-window mint")

    # a fresh issue on the project (its own run, its own grant)
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
    # a /retry-reused run carries EARLIER generations' grants too — the
    # arm's grant is the generation ABOVE the reuse boundary (else the
    # newest when the run is fresh)
    prev_seen = record.get("reused_prev_max_generation")
    candidates = [
        r for r in minted if prev_seen is None or int(r["attempt_generation"]) > int(prev_seen)
    ] or minted
    row = max(candidates, key=lambda r: int(r["attempt_generation"]))
    generation = int(row["attempt_generation"])
    deadline = datetime.fromisoformat(row["redemption_deadline"])
    bundle.record("expire", "grant", {**row, "go_at": _now()})
    token = lane_token(run_id, generation)

    if str(window) == "240":
        # ---- arm B: absoluteness across a cold restart mid-window ----
        if record.get("restart_receipt") is None:
            # restart while the window is OPEN (the run stays non-terminal;
            # the grant's deadline is an absolute column, not runtime state)
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
                "expire",
                "restart_receipt",
                {"command": "podman restart forge-app", "at": _now()},
            )
            print("expire: forge-app restarted COLD inside the grant window")
        health = app_health(timeout=420)
        bundle.record("expire", "health_after", {"version": health.get("version"), "at": _now()})

        if record.get("replay_in_window") is None:
            after_row = next(
                (g for g in grant_rows(run_id) if int(g["attempt_generation"]) == generation),
                None,
            )
            if after_row is None:
                raise Refused(f"generation {generation}'s grant vanished across the restart")
            if after_row["redemption_deadline"] != row["redemption_deadline"]:
                raise Refused(
                    "the grant's absolute deadline moved across the restart "
                    f"({row['redemption_deadline']} -> {after_row['redemption_deadline']})"
                )
            response = redeem(run_id, BROKER_REF, PROVIDER_ROUTE, token)
            replay_doc: dict[str, Any] = {
                "http_status": response.status_code,
                "deadline_preserved": True,
            }
            if response.status_code == 200:
                body = response.json()
                replay_doc.update(
                    {
                        "grant_id_prefix": str(body.get("grant_id", ""))[:16],
                        "redemption_id_prefix": str(body.get("redemption_id", ""))[:16],
                        "binding_revision": body.get("binding_revision"),
                        "operation": body.get("operation"),
                    }
                )
            else:
                replay_doc["detail"] = _safe_detail(response)[:200]
            replay_doc["seconds_to_deadline"] = round(deadline.timestamp() - time.time(), 1)
            bundle.record("expire", "replay_in_window", replay_doc)
            if response.status_code != 200:
                raise Refused(f"the in-window post-restart replay did not redeem: {replay_doc}")
            print(
                f"expire: replay redeemed through the RESTARTED plane (grant "
                f"{replay_doc['grant_id_prefix']}, deadline preserved)"
            )

    # sleep PAST the absolute deadline, then the endpoint refuses typed
    remaining = deadline.timestamp() - time.time()
    if remaining > 0:
        print(
            f"expire: sleeping {remaining + 3:.0f}s past the deadline {row['redemption_deadline']}"
        )
        time.sleep(remaining + 3)
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
            "pairing_marker": dispatch.get("pairing_marker"),
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
        description="R41-10 (#365): the current pairing's grant-consumer trace, live.",
    )
    parser.add_argument(
        "phase",
        choices=[
            "setup",
            "reach",
            "reject",
            "trace",
            "preflight",
            "rotate",
            "retire",
            "expire",
            "teardown",
            "status",
        ],
    )
    parser.add_argument("--project-name", default="forge-redemption-pairing-2026-09-27")
    args = parser.parse_args(argv)

    bundle = Bundle()
    state = State()
    try:
        if args.phase == "status":
            return phase_status(bundle)
        if args.phase == "setup":
            return phase_setup(bundle, state, GitLab(), args.project_name)
        gitlab = GitLab()
        if args.phase == "reach":
            return phase_reach(bundle, gitlab)
        if args.phase == "reject":
            return phase_reject(bundle, gitlab)
        if args.phase == "trace":
            return phase_trace(bundle, state, gitlab)
        if args.phase == "preflight":
            return phase_preflight(bundle, state, gitlab)
        if args.phase == "rotate":
            return phase_rotate(bundle, state, gitlab)
        if args.phase == "retire":
            return phase_retire(bundle, state, gitlab)
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
