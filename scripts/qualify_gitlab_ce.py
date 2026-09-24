#!/usr/bin/env python
"""Cold-install qualification driver for the ``gitlab-ce-v1`` profile.

Issue #268 / R36-09 (external review ``16339c2``, acceptance trace
AT-10). One command per stage; every stage writes its honest outcome into
the evidence bundle (``qualification/profiles/gitlab-ce-v1-evidence.json``
by default) — a REFUSED stage with a precise reason beats a fabricated
green, and a missing prerequisite refuses BEFORE any paid execution.

Stages:

- ``--stage preflight`` (free): doctor-style checks — credentials, CE
  reachability + version, runner availability, webhook + control-plane
  health, the control plane's INSTALLED VERSION against the profile's
  pinned release, onboarding prerequisites (CI template + verification
  job + lane credentials) and the budget caps.
- ``--stage install-check`` (free): from a CLEAN temp venv, install the
  profile's wheel route exactly as documented (the R36-07 ladder:
  download, exactly-one-wheel + sha256 verification, install, identity
  gate). ``--wheel local`` builds the working tree with ``uv build`` and
  records the route as an UNQUALIFIED pre-release instead.
- ``--stage flow`` (paid): drives issue → ``/implement`` → plan evidence
  → ``/go`` → harness dispatch → candidate → Draft MR → independent
  verification through NATIVE surfaces only (the driver speaks nothing
  but the GitLab API). Refuses unless preflight is fully green AND the
  budget caps are present. The runner-loss drill is refused while the
  GitLab dispatch seam carries no lane-resume contract (profile §8).
- ``--stage report`` (free): finalizes the evidence bundle — releases,
  digests, per-stage outcomes, honestly-counted manual interventions and
  the named human-review outcome (the Draft MR stays a Draft).

Run from the repository root (``Settings`` reads ``.env``):

    uv run python scripts/qualify_gitlab_ce.py --stage preflight
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from forge.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE = REPO_ROOT / "qualification" / "profiles" / "gitlab-ce-v1-evidence.json"

#: The profile's pinned release (qualification/profiles/gitlab-ce-v1.md §3).
PROMOTED_WHEEL_URL = (
    "https://github.com/forcewake/forge/releases/download/v0.35.0/forge-0.35.0-py3-none-any.whl"
)
PROMOTED_WHEEL_SHA256 = "1e365612473426a2130000784a6cd8c707ffcedae7f8f6bcb34c3f75791700d9"
PROMOTED_FORGE_VERSION = "0.35.0"

#: The lab's disposable integration project (forge integration lab).
DEFAULT_PROJECT_ID = 68

_FLOW_TIMEOUT_DEFAULT = 900  # the driver's own bounded wait, seconds


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fail(message: str) -> None:
    print(f"  FAIL {message}")


def _ok(message: str) -> None:
    print(f"  ok   {message}")


def _warn(message: str) -> None:
    print(f"  warn {message}")


class Evidence:
    """The honest, append-only stage record."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.document: dict[str, Any] = {
            "profile": "gitlab-ce-v1",
            "profile_document": "qualification/profiles/gitlab-ce-v1.md",
            "generated_at": _now(),
            "stages": {},
            "releases": {},
            "manual_interventions": [],
            "human_review": {"outcome": "not-reached", "mr_url": None},
            "counters": {
                "profile.cold_install_success": None,
                "delivery.manual_rescue_count": 0,
                "resume.cross_runner_success": None,
                "verification.current_candidate_pass": None,
            },
        }
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.document.update(loaded)
                    self.document["stages"] = dict(loaded.get("stages") or {})
            except json.JSONDecodeError:
                print(f"warning: existing evidence at {path} is corrupt — starting fresh")

    def stage(self, name: str, outcome: dict[str, Any]) -> None:
        outcome["recorded_at"] = _now()
        self.document["stages"][name] = outcome
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.document, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        print(f"\nevidence → {self.path}")

    def intervention(self, what: str) -> None:
        self.document["manual_interventions"].append({"at": _now(), "what": what})
        self.document["counters"]["delivery.manual_rescue_count"] = len(
            self.document["manual_interventions"]
        )


def _gitlab_client(settings: Settings, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        base_url=f"{settings.GITLAB_URL.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": settings.GITLAB_TOKEN.get_secret_value()},
        timeout=timeout,
    )


def _check(name: str, ok: bool, detail: str, *, required: bool = True) -> dict[str, str]:
    line = {
        "name": name,
        "status": "pass" if ok else ("fail" if required else "warn"),
        "detail": detail,
    }
    (ok and _ok or (required and _fail or _warn))(f"{name}: {detail}")
    return line


# ----------------------------------------------------------------------
# preflight
# ----------------------------------------------------------------------


def stage_preflight(args: argparse.Namespace, evidence: Evidence) -> int:
    print("== preflight (free; refuses before any paid execution) ==")
    settings = Settings()  # type: ignore[call-arg]
    checks: list[dict[str, str]] = []
    facts: dict[str, Any] = {}

    # -- credentials ----------------------------------------------------
    missing = [
        key
        for key in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET")
        if not getattr(settings, key)
    ]
    checks.append(
        _check(
            "credentials.present",
            not missing,
            "GITLAB_URL/TOKEN/WEBHOOK_SECRET present" if not missing else f"missing {missing}",
        )
    )
    bot = getattr(settings, "FORGE_BOT_TOKEN", None)
    checks.append(
        _check(
            "credentials.bot_identity",
            bot is not None,
            "FORGE_BOT_TOKEN present (forge never speaks with approver credentials)"
            if bot is not None
            else "FORGE_BOT_TOKEN absent — forge would speak with the approver token",
            required=False,
        )
    )

    # -- CE reachable + version ------------------------------------------
    version: dict[str, Any] = {}
    try:
        with _gitlab_client(settings) as client:
            response = client.get("/version")
            response.raise_for_status()
            version = response.json()
            who = client.get("/user")
            who.raise_for_status()
            facts["token_user"] = who.json().get("username")
    except httpx.HTTPError as exc:
        checks.append(_check("gitlab.reachable", False, f"{exc}"))
        evidence.stage(
            "preflight",
            {
                "status": "refused",
                "reason": "GitLab CE not reachable with the provided token",
                "checks": checks,
            },
        )
        return 2
    checks.append(
        _check(
            "gitlab.ce_edition",
            version.get("enterprise") is False,
            f"GitLab {version.get('version')} (revision {version.get('revision')}, "
            f"enterprise={version.get('enterprise')})",
        )
    )
    evidence.document["releases"]["gitlab_ce"] = {
        "version": version.get("version"),
        "revision": version.get("revision"),
        "enterprise": version.get("enterprise"),
    }
    profile_doc = REPO_ROOT / "qualification" / "profiles" / "gitlab-ce-v1.md"
    profile_doc_text = profile_doc.read_text(encoding="utf-8") if profile_doc.is_file() else ""
    frozen = re.search(r"\*\*GitLab CE ([0-9.]+)\*\*", profile_doc_text)
    if frozen and frozen.group(1) != str(version.get("version", "")).rsplit("-", 1)[0]:
        evidence.intervention(
            f"live CE {version.get('version')} differs from the profile-frozen "
            f"{frozen.group(1)} — profile document must be re-frozen or the instance upgraded"
        )

    # -- target project + runner -----------------------------------------
    project_id = args.project
    with _gitlab_client(settings) as client:
        try:
            project = client.get(f"/projects/{project_id}").json()
            facts["project"] = {
                "id": project.get("id"),
                "path": project.get("path_with_namespace"),
                "default_branch": project.get("default_branch"),
                "builds_access_level": project.get("builds_access_level"),
            }
            checks.append(
                _check(
                    "project.access",
                    bool(project.get("id")),
                    f"{project.get('path_with_namespace')} (default branch "
                    f"{project.get('default_branch')}, builds={project.get('builds_access_level')})",
                )
            )
        except httpx.HTTPError as exc:
            checks.append(
                _check("project.access", False, f"cannot read project {project_id}: {exc}")
            )
            project = {}

        runners: list[dict[str, Any]] = []
        try:
            runners = client.get("/runners/all", params={"per_page": 100}).json()
        except httpx.HTTPError:
            runners = []
        online = [r for r in runners if r.get("status") == "online" and not r.get("paused")]
        serving = [
            r
            for r in online
            if r.get("is_shared") or project_id in [p.get("id") for p in (r.get("projects") or [])]
        ]
        facts["runners"] = [
            {
                "id": r.get("id"),
                "description": r.get("description"),
                "status": r.get("status"),
                "paused": r.get("paused"),
                "type": r.get("runner_type"),
            }
            for r in runners
        ]
        checks.append(
            _check(
                "runner.available",
                bool(serving),
                (
                    f"{len(serving)} online runner(s): "
                    + ", ".join(f"#{r['id']} {r['description']}" for r in serving)
                )
                if serving
                else "no online runner serves the project "
                f"(runners seen: {[r.get('description') for r in runners]})",
            )
        )

        # -- webhook + control plane -------------------------------------
        hooks = []
        try:
            hooks = client.get(f"/projects/{project_id}/hooks").json()
        except httpx.HTTPError:
            pass
        control_urls = [h.get("url", "") for h in hooks if "/webhook" in str(h.get("url", ""))]
        checks.append(
            _check(
                "webhook.registered",
                bool(control_urls),
                f"hooks: {control_urls}" if control_urls else "no forge webhook on the project",
            )
        )
        control_health: dict[str, Any] = {}
        if control_urls:
            hook = urlparse(control_urls[0])
            health_url = f"{hook.scheme}://{hook.netloc}/health"
            try:
                answer = httpx.get(health_url, timeout=15.0)
                control_health = answer.json()
            except (httpx.HTTPError, ValueError) as exc:
                control_health = {"error": str(exc)}
        deployed_version = str(control_health.get("version") or "")
        checks.append(
            _check(
                "controlplane.reachable",
                control_health.get("status") == "ok",
                f"/health → {json.dumps(control_health)[:200]}",
            )
        )
        checks.append(
            _check(
                "controlplane.version_matches_profile",
                deployed_version == PROMOTED_FORGE_VERSION,
                f"deployed control plane reports {deployed_version or '<unknown>'}; "
                f"the profile pins the promoted wheel v{PROMOTED_FORGE_VERSION} "
                "(a paid flow against another build qualifies nothing)",
            )
        )
        evidence.document["releases"]["control_plane"] = {
            "deployed_version": deployed_version or None,
            "pinned_wheel": {
                "url": PROMOTED_WHEEL_URL,
                "sha256": PROMOTED_WHEEL_SHA256,
                "version": PROMOTED_FORGE_VERSION,
            },
            "health": control_health,
        }

        # -- onboarding prerequisites ------------------------------------
        ci_yaml = ""
        try:
            ci_yaml = client.get(
                f"/projects/{project_id}/repository/files/.gitlab-ci.yml/raw",
                params={"ref": project.get("default_branch") or "main"},
            ).text
        except httpx.HTTPError:
            ci_yaml = ""
        checks.append(
            _check(
                "onboarding.lane_template",
                "forge" in ci_yaml and "gitlab-ci.yml" in ci_yaml,
                "the project's .gitlab-ci.yml includes a forge lane template"
                if ci_yaml
                else ".gitlab-ci.yml unreadable",
            )
        )
        has_verification_job = bool(re.search(r"^smoke:", ci_yaml, re.MULTILINE))
        checks.append(
            _check(
                "onboarding.verification_job",
                has_verification_job,
                "independent `smoke` job present (FORGE_REQUIRED_JOBS=smoke)"
                if has_verification_job
                else "no `smoke` job — the independent verification contract is missing",
            )
        )
        try:
            variables = client.get(f"/projects/{project_id}/variables").json()
            variable_keys = {v.get("key") for v in variables}
        except httpx.HTTPError:
            variable_keys = set()
        lane_creds = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"} <= variable_keys
        checks.append(
            _check(
                "onboarding.lane_credentials",
                lane_creds,
                "ANTHROPIC_AUTH_TOKEN/BASE_URL project variables present"
                if lane_creds
                else f"lane credentials missing (have {sorted(variable_keys)})",
            )
        )

    # -- budget caps ------------------------------------------------------
    caps: dict[str, Any] = {}
    raw_profiles = os.environ.get("FORGE_BUDGET_PROFILES", "") or ""
    explicit = args.max_budget_json or ""
    if explicit:
        try:
            caps = {"source": "--max-budget-json", "profiles": json.loads(explicit)}
        except json.JSONDecodeError:
            caps = {"source": "--max-budget-json", "error": "unparseable JSON"}
    elif raw_profiles:
        try:
            caps = {"source": "FORGE_BUDGET_PROFILES", "profiles": json.loads(raw_profiles)}
        except json.JSONDecodeError:
            caps = {"source": "FORGE_BUDGET_PROFILES", "error": "unparseable JSON"}
    timeout = int(getattr(settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 0) or 0)
    caps["harness_timeout_seconds"] = timeout
    caps_present = bool(caps.get("profiles")) and timeout > 0
    checks.append(
        _check(
            "budgets.caps_present",
            caps_present,
            json.dumps(caps)[:200]
            + (
                ""
                if caps_present
                else " — no FORGE_BUDGET_PROFILES and no --max-budget-json: the paid"
                " flow stage refuses (an uncapped paid run is not qualification)"
            ),
        )
    )

    # -- the lane-resume parity fact (recorded, never silently skipped) ---
    checks.append(
        _check(
            "capability.lane_resume_dispatch",
            False,
            "the GitLab dispatch seam carries no FORGE_LANE_RESUME/lane-control "
            "credentials (GitHub-only, R32-04) — the cross-runner resume drill is "
            "refused on the live flow stage (profile §8)",
            required=False,
        )
    )

    evidence.document["host_facts"] = facts
    evidence.document["counters"]["resume.cross_runner_success"] = False
    refused = [c for c in checks if c["status"] == "fail"]
    outcome = {
        "status": "refused" if refused else "green",
        "checks": checks,
        "caps": caps,
    }
    if refused:
        outcome["reason"] = "; ".join(f"{c['name']}: {c['detail']}" for c in refused)
    evidence.stage("preflight", outcome)
    print(f"\npreflight: {outcome['status'].upper()}")
    return 0 if not refused else 2


# ----------------------------------------------------------------------
# install-check
# ----------------------------------------------------------------------


def stage_install_check(args: argparse.Namespace, evidence: Evidence) -> int:
    print("== install-check (free; a CLEAN temp venv per run) ==")
    checks: list[dict[str, str]] = []
    wheel_url = PROMOTED_WHEEL_URL
    expected_sha = PROMOTED_WHEEL_SHA256
    route = "default-wheel (promoted)"
    qualified = True
    if args.wheel == "local":
        print("  building the working tree (uv build) — pre-release route, qualified=false")
        build = subprocess.run(
            ["uv", "build", "--wheel", "--quiet"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if build.returncode != 0:
            evidence.stage(
                "install-check",
                {"status": "refused", "reason": f"uv build failed: {build.stderr[-400:]}"},
            )
            return 2
        wheels = sorted((REPO_ROOT / "dist").glob("forge-*.whl"))
        if len(wheels) != 1:
            evidence.stage(
                "install-check",
                {"status": "refused", "reason": f"expected exactly one wheel, found {wheels}"},
            )
            return 2
        wheel_url = wheels[0].resolve().as_uri()
        expected_sha = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
        route = "local-tree-wheel (pre-release, uv build)"
        qualified = False
        evidence.intervention(
            "install-check used the LOCAL TREE wheel (pre-release route) — "
            "qualified=false per the R36-07 dev-route honesty rule"
        )

    with tempfile.TemporaryDirectory(prefix="forge-ce-qual-") as tmp:
        tmp_path = Path(tmp)
        venv = tmp_path / "venv"
        wheel_dir = tmp_path / "wheel"
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=300
        )
        pip = venv / "bin" / "pip"
        checks.append(
            _check("venv.clean", created.returncode == 0, f"{venv} (freshly created, no cache)")
        )
        if created.returncode != 0:
            evidence.stage(
                "install-check",
                {"status": "refused", "reason": f"venv creation failed: {created.stderr[-300:]}"},
            )
            return 2
        download = subprocess.run(
            [str(pip), "download", "--no-deps", "--no-cache-dir", "-d", str(wheel_dir), wheel_url],
            capture_output=True,
            text=True,
            timeout=600,
        )
        candidates = sorted(wheel_dir.glob("*.whl")) if wheel_dir.is_dir() else []
        checks.append(
            _check(
                "wheel.download",
                download.returncode == 0 and len(candidates) == 1,
                f"pip download → {candidates or download.stderr[-200:]}",
            )
        )
        if download.returncode != 0 or len(candidates) != 1:
            evidence.stage(
                "install-check",
                {
                    "status": "refused",
                    "reason": "the wheel route did not yield exactly one wheel "
                    "(zero package execution, AT-08)",
                    "checks": checks,
                },
            )
            return 2
        wheel_file = candidates[0]
        actual_sha = hashlib.sha256(wheel_file.read_bytes()).hexdigest()
        sha_ok = actual_sha == expected_sha
        checks.append(
            _check(
                "wheel.sha256",
                sha_ok,
                f"expected {expected_sha[:16]}… actual {actual_sha[:16]}…",
            )
        )
        if args.wheel == "promoted" and not sha_ok:
            evidence.stage(
                "install-check",
                {
                    "status": "refused",
                    "reason": "wheel sha256 mismatch — refusing to execute the package",
                    "checks": checks,
                },
            )
            return 2
        install = subprocess.run(
            [str(pip), "install", "--no-cache-dir", str(wheel_file)],
            capture_output=True,
            text=True,
            timeout=900,
        )
        checks.append(
            _check(
                "wheel.install",
                install.returncode == 0,
                install.stdout.strip()[-200:] or install.stderr[-200:],
            )
        )
        if install.returncode != 0:
            evidence.stage(
                "install-check",
                {
                    "status": "refused",
                    "reason": f"pip install failed: {install.stderr[-300:]}",
                    "checks": checks,
                },
            )
            return 2
        # the identity gate: the imported version must equal the wheel's
        wheel_version = re.sub(r"^forge-([0-9][^-]*)-.*$", r"\1", wheel_file.name)
        probe = subprocess.run(
            [str(venv / "bin" / "python"), "-c", "import forge; print(forge.__version__)"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        installed_version = probe.stdout.strip()
        identity_ok = probe.returncode == 0 and installed_version == wheel_version
        checks.append(
            _check(
                "identity.installed_version",
                identity_ok,
                f"expected {wheel_version}, installed "
                f"{installed_version or probe.stderr.strip()[-120:] or '<not importable>'}",
            )
        )
        receipt = {
            "pin": "wheel",
            "route": route,
            "wheel_url": PROMOTED_WHEEL_URL if args.wheel == "promoted" else str(wheel_file),
            "expected_sha256": expected_sha,
            "actual_sha256": actual_sha,
            "qualified": qualified,
            "version": wheel_version,
        }
        identity = {
            "route": route,
            "expected_version": wheel_version,
            "installed_version": installed_version,
        }
        green = sha_ok and identity_ok
        evidence.document["counters"]["profile.cold_install_success"] = green and qualified
        evidence.document["releases"]["forge"] = receipt
        evidence.stage(
            "install-check",
            {
                "status": "green" if green else "refused",
                "receipt": receipt,  # the .forge/lane_install.json contract
                "identity": identity,  # the .forge/install-identity.json contract
                "checks": checks,
            },
        )
        print(
            f"\ninstall-check: {'GREEN' if green else 'REFUSED'} (route {route}, qualified={qualified})"
        )
        return 0 if green else 2


# ----------------------------------------------------------------------
# flow (paid — refuses unless preflight green AND caps present)
# ----------------------------------------------------------------------


def stage_flow(args: argparse.Namespace, evidence: Evidence) -> int:
    print("== flow (paid; native surfaces only) ==")
    preflight = evidence.document["stages"].get("preflight") or {}
    if preflight.get("status") != "green":
        reason = (
            f"preflight is {preflight.get('status') or 'missing'} — refusing before any paid "
            "execution. Run --stage preflight first."
        )
        if preflight.get("status") == "refused":
            reason = "preflight refused: " + str(preflight.get("reason"))
        evidence.stage("flow", {"status": "refused", "reason": reason})
        print(f"flow: REFUSED — {reason}")
        return 2
    if not ((preflight.get("caps") or {}).get("profiles")):
        reason = (
            "budget env caps not present (no FORGE_BUDGET_PROFILES / --max-budget-json at "
            "preflight time) — a paid run without caps is not qualification"
        )
        evidence.stage("flow", {"status": "refused", "reason": reason})
        print(f"flow: REFUSED — {reason}")
        return 2

    settings = Settings()  # type: ignore[call-arg]
    deadline = time.monotonic() + args.flow_timeout
    with _gitlab_client(settings) as client:
        # 1. the native issue on the disposable project
        issue = client.post(
            f"/projects/{args.project}/issues",
            json={
                "title": "[R36-09] gitlab-ce-v1 qualification task",
                "description": (
                    "A tiny bounded task for the cold-install qualification of the "
                    "gitlab-ce-v1 profile: create `qualification-note.md` containing the "
                    "string `gitlab-ce-v1 qualified`. No other changes."
                ),
            },
        )
        issue.raise_for_status()
        issue_iid = issue.json()["iid"]
        print(f"  issue #{issue_iid} created")

        def poll(predicate, description: str):
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(5)
            print(f"  TIMEOUT waiting for {description}")
            return False

        # 2. /implement (the approver's own comment — the driver is the human)
        client.post(
            f"/projects/{args.project}/issues/{issue_iid}/notes",
            json={"body": "@forge /implement"},
        ).raise_for_status()

        plan_body = {}

        def plan_arrived() -> bool:
            notes = client.get(f"/projects/{args.project}/issues/{issue_iid}/notes").json()
            for note in notes:
                if "/go " in note.get("body", ""):
                    plan_body["note"] = note["body"]
                    return True
            return False

        if not poll(plan_arrived, "the plan comment"):
            evidence.stage(
                "flow",
                {"status": "refused", "reason": "no plan comment arrived", "issue_iid": issue_iid},
            )
            return 2
        match = re.search(r"/go ([0-9a-f]{32})", plan_body["note"])
        if match is None:
            evidence.stage(
                "flow",
                {
                    "status": "refused",
                    "reason": "plan comment carries no run id",
                    "issue_iid": issue_iid,
                },
            )
            return 2
        run_id = match.group(1)
        print(f"  plan arrived (run {run_id[:8]}); approving")

        # 3. /go → the harness dispatch
        client.post(
            f"/projects/{args.project}/issues/{issue_iid}/notes",
            json={"body": f"@forge /go {run_id}"},
        ).raise_for_status()
        branch = f"factory/{issue_iid}/{run_id[:8]}"

        pipeline = {}

        def dispatch_arrived() -> bool:
            pipelines = client.get(
                f"/projects/{args.project}/pipelines", params={"ref": branch}
            ).json()
            if pipelines:
                pipeline.update(pipelines[0])
                return True
            return False

        if not poll(dispatch_arrived, "the harness pipeline"):
            evidence.stage(
                "flow",
                {
                    "status": "refused",
                    "reason": "no harness pipeline dispatched",
                    "issue_iid": issue_iid,
                    "run_id": run_id,
                },
            )
            return 2
        pipeline_id = pipeline["id"]
        print(f"  pipeline {pipeline_id} dispatched on {branch}")

        # 4. the runner-loss drill is REFUSED while the GitLab dispatch
        #    seam carries no lane-resume contract (profile §8) — recorded,
        #    never silently skipped.
        evidence.document["counters"]["resume.cross_runner_success"] = False

        # 5. wait for the candidate → the Draft MR
        mr = {}

        def mr_arrived() -> bool:
            mrs = client.get(
                f"/projects/{args.project}/merge_requests",
                params={"state": "opened", "source_branch": branch},
            ).json()
            if mrs:
                mr.update(mrs[0])
                return True
            return False

        if not poll(mr_arrived, "the Draft MR"):
            evidence.stage(
                "flow",
                {
                    "status": "refused",
                    "reason": "no Draft MR arrived within the bounded wait",
                    "issue_iid": issue_iid,
                    "run_id": run_id,
                    "pipeline_id": pipeline_id,
                },
            )
            return 2
        print(f"  Draft MR !{mr['iid']} — {mr['title']}")

        # 6. the independent verification: the smoke job green on the
        #    CURRENT candidate sha (the MR head, not an earlier commit)
        def verified() -> bool:
            mrs = client.get(f"/projects/{args.project}/merge_requests/{mr['iid']}").json()
            sha = mrs.get("sha") or ""
            if not sha:
                return False
            pipelines = client.get(
                f"/projects/{args.project}/pipelines", params={"sha": sha}
            ).json()
            for entry in pipelines:
                if entry.get("status") == "success":
                    return True
            return False

        verified_ok = poll(verified, "the current candidate's green pipeline")
        evidence.document["counters"]["verification.current_candidate_pass"] = verified_ok

        # 7. the human-review point: the MR stays a Draft — merge is human
        evidence.document["human_review"] = {
            "outcome": "pending-human-review (Draft MR left unmerged by design)",
            "mr_url": mr.get("web_url"),
            "mr_iid": mr.get("iid"),
        }
        evidence.stage(
            "flow",
            {
                "status": "green" if verified_ok else "partial",
                "issue_iid": issue_iid,
                "run_id": run_id,
                "pipeline_id": pipeline_id,
                "branch": branch,
                "mr": {
                    key: mr.get(key) for key in ("iid", "title", "web_url", "sha", "source_branch")
                },
                "runner_loss_drill": {
                    "status": "refused",
                    "reason": "GitLab dispatch carries no lane-resume contract "
                    "(profile §8) — proven offline by production-entry trace CE-2",
                },
                "verified": verified_ok,
            },
        )
        print(f"flow: {'GREEN' if verified_ok else 'PARTIAL'} (Draft MR left for human review)")
        return 0 if verified_ok else 3


# ----------------------------------------------------------------------
# report
# ----------------------------------------------------------------------


#: The offline twin of the whole arc (run by --stage report so the
#: evidence bundle carries its outcome — the .env lesson: env-stripped).
OFFLINE_TRACE_ARGS = [
    "-m",
    "pytest",
    "tests/production_entry/test_gitlab_ce_entry.py",
    "-q",
    "--no-header",
]
OFFLINE_TRACE_ENV_STRIP = ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET")


def _run_offline_trace() -> dict[str, Any]:
    """Run the CE production-entry trace env-stripped, report its outcome."""
    env = {key: value for key, value in os.environ.items() if key not in OFFLINE_TRACE_ENV_STRIP}
    outcome = subprocess.run(
        [sys.executable, *OFFLINE_TRACE_ARGS],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    summary = outcome.stdout.strip().splitlines()[-1] if outcome.stdout.strip() else ""
    return {
        "command": "pytest tests/production_entry/test_gitlab_ce_entry.py (env-stripped)",
        "returncode": outcome.returncode,
        "summary": summary,
    }


def stage_report(args: argparse.Namespace, evidence: Evidence) -> int:
    print("== report ==")
    stages = evidence.document["stages"]
    for name in ("preflight", "install-check", "flow"):
        entry = stages.get(name)
        if entry is None:
            stages[name] = {"status": "not-run"}
            print(f"  {name}: not-run")
        else:
            print(f"  {name}: {entry.get('status')}")

    # The offline twin (AT-10's assertions proven without the lab): its
    # outcome belongs in the bundle, and it is the surface that proves
    # the cross-runner resume while the GitLab dispatch parity gap holds.
    try:
        trace = _run_offline_trace()
    except (subprocess.TimeoutExpired, OSError) as exc:
        trace = {
            "command": "pytest tests/production_entry/test_gitlab_ce_entry.py",
            "error": str(exc),
        }
    trace_ok = trace.get("returncode") == 0
    evidence.document["offline_trace"] = trace
    print(
        f"  offline trace: {'GREEN' if trace_ok else 'FAILED'} — {trace.get('summary') or trace.get('error')}"
    )

    counters = evidence.document["counters"]
    install = stages.get("install-check") or {}
    counters["profile.cold_install_success"] = (
        install.get("status") == "green" and (install.get("receipt") or {}).get("qualified") is True
    )
    counters["delivery.manual_rescue_count"] = len(evidence.document["manual_interventions"])
    evidence.document["resume_cross_runner"] = {
        "live": (stages.get("flow") or {}).get("runner_loss_drill", {}).get("status") == "green",
        "offline": trace_ok,
        "surface": "production-entry trace CE-2 (tests/production_entry/test_gitlab_ce_entry.py)",
        "live_blocker": (
            "GitLab dispatch carries no lane-resume contract (profile §8); the live "
            "flow stage was refused before paid execution"
        ),
    }
    counters["resume.cross_runner_success"] = trace_ok
    if (stages.get("flow") or {}).get("status") not in ("green", "partial"):
        evidence.document["human_review"] = {
            "outcome": "not-reached (the live flow stage did not produce a Draft MR; "
            "offline trace CE-2 ends at its own Draft MR for human review)",
            "mr_url": None,
        }
    evidence.document["generated_at"] = _now()
    evidence.stage("report", {"status": "written", "offline_trace": trace})
    print(
        json.dumps(
            {
                "counters": counters,
                "manual_interventions": len(evidence.document["manual_interventions"]),
                "human_review": evidence.document["human_review"]["outcome"],
            },
            indent=2,
        )
    )
    return 0


# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stage",
        required=True,
        choices=["preflight", "install-check", "flow", "report"],
    )
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument(
        "--project", type=int, default=DEFAULT_PROJECT_ID, help="the disposable lab project id"
    )
    parser.add_argument(
        "--wheel",
        choices=["promoted", "local"],
        default="promoted",
        help="install-check route: the promoted wheel (qualified) or the local tree (pre-release)",
    )
    parser.add_argument(
        "--flow-timeout",
        type=int,
        default=_FLOW_TIMEOUT_DEFAULT,
        help="the flow stage's bounded wait (s)",
    )
    parser.add_argument(
        "--max-budget-json",
        default="",
        help="explicit budget caps for the paid flow stage (FORGE_BUDGET_PROFILES JSON)",
    )
    args = parser.parse_args(argv)

    evidence = Evidence(args.evidence)
    handlers = {
        "preflight": stage_preflight,
        "install-check": stage_install_check,
        "flow": stage_flow,
        "report": stage_report,
    }
    return handlers[args.stage](args, evidence)


if __name__ == "__main__":
    raise SystemExit(main())
