#!/usr/bin/env python
"""R37-06 (#287) — READ-ONLY inventory of the REAL GitLab CE lab.

Issue #287: the lab qualification record says the live flow was REFUSED at
preflight (deployed control plane 0.28.0, pinned wheel 0.35.0, no numerical
budget caps). This script turns that recorded basis into OBSERVED fact —
every stage is a read-only probe against the actual deployment, never an
inference from source HEAD:

- ``--stage control-plane`` — HTTP GET the app's real health surface
  (``GET /health`` on the gateway router mounted by ``forge.main``) and
  record the version it REPORTS, plus the container's image identity via
  ``podman inspect`` (read-only).
- ``--stage schema`` — the deployed ``alembic_version`` head from the lab
  Postgres (port 5433, ``forge``/``forge``) via a read-only SELECT through
  ``podman exec``, against the repo's migration chain head.
- ``--stage lane`` — the PINNED lane wheel from the latest promotion
  record under ``docs/releases/evidence/``, and the INSTALLED target
  template state (the GitLab project's ``.gitlab-ci.yml`` via the API,
  credentials read ONLY through ``forge.config.Settings`` — never parsed
  out of ``.env`` by hand). Unreachable prerequisites are recorded
  refused-with-reason, never guessed.
- ``--stage runner`` — image identities of the worker/runner containers
  via ``podman inspect``, plus the GitLab runner availability the lane
  actually dispatches onto (read-only API).
- ``--stage caps`` — the app/worker container env (``podman inspect``) for
  the budget-cap variables: present-with-numerical-value vs absent.

The report (``qualification/inventory-<date>.json``) carries every
observed identity plus a DERIVED ``compatibility_verdict`` against the
intended profile: image version == pinned release? schema at head? caps
numerical? — each mismatch naming its resolution in the runbook
(``docs/operations/lab-alignment-runbook.md``).

STRICTLY read-only: no container is started, stopped or recreated, no
database row is written, no paid call is made. Exit code 0 means the
inventory RAN (a misaligned lab is a valid observation, not a tool
failure); exit 2 means a usage/probe error.

Run from the repository root:

    uv run python scripts/inventory_lab.py --stage all --out qualification/inventory-<date>.json
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Versioned stamp of the inventory document.
INVENTORY_STAMP = "forge.lab.inventory/1"

#: The lab's containers (podman; recreate via podman, NOT docker-compose —
#: see docs/operations/lab-alignment-runbook.md).
APP_CONTAINER = "forge-app"
WORKER_CONTAINER = "forge-worker"
POSTGRES_CONTAINER = "forge-postgres"

#: The app's real health surface: the gateway router mounted by
#: ``forge.main:create_app`` exposes ``GET /health`` and reports the
#: RUNNING ``forge.__version__`` — the honest deployed-version probe.
DEFAULT_APP_HEALTH_URL = "http://localhost:8420/health"

#: The budget-cap variables the caps stage looks for (the real names read
#: by src/forge — see the runbook §caps for the enforcement points).
BUDGET_CAP_ENV_NAMES: tuple[str, ...] = (
    "FORGE_BUDGET_PROFILES",  # Settings -> RunService grant refusals + durable budgets
    "FORGE_LANE_BUDGET_SECONDS",  # lane_driver: per-lane wall clock
)

#: The disposable integration project used by the qualification driver.
DEFAULT_GITLAB_PROJECT_ID = 68

DEFAULT_LANE_VENV = Path("/tmp/forge-lane-venv")

STAGES: tuple[str, ...] = ("control-plane", "schema", "lane", "runner", "caps")


class ProbeError(Exception):
    """A read-only probe could not reach its target (recorded, never fatal)."""


# ---------------------------------------------------------------------------
# The probe boundary: everything the script does to the lab goes through
# here, so tests drive the whole inventory with fakes (no network, no podman).
# ---------------------------------------------------------------------------


class LabProbe:
    """The real probe: httpx for HTTP, subprocess for read-only podman."""

    def __init__(self, app_health_url: str = DEFAULT_APP_HEALTH_URL) -> None:
        self.app_health_url = app_health_url

    def http_get_json(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        import httpx

        try:
            response = httpx.get(
                url, headers=dict(headers or {}), params=dict(params or {}), timeout=10.0
            )
        except httpx.HTTPError as exc:
            raise ProbeError(f"GET {url} unreachable: {exc}") from exc
        if response.status_code != 200:
            raise ProbeError(f"GET {url} answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise ProbeError(f"GET {url} did not answer JSON: {exc}") from exc

    def podman(self, *args: str) -> str:
        completed = subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=60, check=False
        )
        if completed.returncode != 0:
            raise ProbeError(
                f"podman {' '.join(args)} failed ({completed.returncode}): "
                f"{completed.stderr.strip()[:200]}"
            )
        return completed.stdout


# ---------------------------------------------------------------------------
# The intended profile, read from the repo's own evidence (never guessed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntendedProfile:
    """What the lab SHOULD be running, per the repo's promotion evidence."""

    source: str
    release_version: str
    image_digest: str
    wheel_sha256: str
    wheel_url: str
    schema_head: str
    required_caps: tuple[str, ...] = BUDGET_CAP_ENV_NAMES

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "release_version": self.release_version,
            "image_digest": self.image_digest,
            "wheel_sha256": self.wheel_sha256,
            "wheel_url": self.wheel_url,
            "schema_head": self.schema_head,
            "required_caps": list(self.required_caps),
        }


def latest_promotion_record(root: Path) -> Path | None:
    """The newest archived promotion record (``docs/releases/evidence/``)."""
    candidates = sorted(root.glob("docs/releases/evidence/v*/promotion.json"))
    return candidates[-1] if candidates else None


def repo_schema_head(root: Path) -> str:
    """The migration chain head parsed from ``alembic/versions/`` (read-only).

    The head is the revision no other migration names as its down_revision.
    """
    revisions: dict[str, str] = {}
    down_revisions: set[str] = set()
    pattern = re.compile(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
    down_pattern = re.compile(
        r"^down_revision(?::[^=]*)?\s*=\s*(?:[\"']([^\"']+)[\"']|None)", re.MULTILINE
    )
    for path in sorted((root / "alembic" / "versions").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        match = pattern.search(text)
        if not match:
            continue
        revisions[match.group(1)] = path.name
        down = down_pattern.search(text)
        if down and down.group(1):
            down_revisions.add(down.group(1))
    heads = [revision for revision in revisions if revision not in down_revisions]
    if len(heads) != 1:
        raise ProbeError(
            f"alembic/versions has {len(heads)} heads ({heads}) — a branched chain is "
            "not an intended profile"
        )
    return heads[0]


def load_intended_profile(root: Path) -> IntendedProfile:
    promotion_path = latest_promotion_record(root)
    if promotion_path is None:
        raise ProbeError("no promotion record under docs/releases/evidence/ — nothing pinned")
    document = json.loads(promotion_path.read_text(encoding="utf-8"))
    return IntendedProfile(
        source=str(promotion_path.relative_to(root)),
        release_version=str(document["version"]),
        image_digest=str(document.get("image_digest", "")),
        wheel_sha256=str(document.get("wheel_sha256", "")),
        wheel_url=str(document.get("wheel_url", "")),
        schema_head=repo_schema_head(root),
    )


# ---------------------------------------------------------------------------
# Stages — each returns its observation (or its refusal, with the reason)
# ---------------------------------------------------------------------------


def _refused(reason: str) -> dict[str, Any]:
    return {"status": "refused", "reason": reason}


def stage_control_plane(probe: LabProbe) -> dict[str, Any]:
    """The deployed control plane: the version /health REPORTS + image identity."""
    try:
        health = probe.http_get_json(probe.app_health_url)
    except ProbeError as exc:
        return _refused(str(exc))
    return {
        "status": "observed",
        "health_url": probe.app_health_url,
        "reported_version": str(health.get("version", "")),
        "health_status": str(health.get("status", "")),
        "components": {
            str(key): str(value)
            for key, value in health.items()
            if key not in ("version", "status")
        },
        "image": _container_image(probe, APP_CONTAINER),
    }


def _container_image(probe: LabProbe, container: str) -> dict[str, Any]:
    try:
        stdout = probe.podman("inspect", container, "--format", "{{.ImageName}} {{.ImageDigest}}")
    except ProbeError as exc:
        return _refused(f"podman inspect {container}: {exc}")
    parts = stdout.strip().split()
    if len(parts) != 2:
        return _refused(f"podman inspect {container}: unexpected output {stdout.strip()!r}")
    return {
        "status": "observed",
        "container": container,
        "image_name": parts[0],
        "image_digest": parts[1],
    }


def stage_schema(probe: LabProbe, root: Path) -> dict[str, Any]:
    """The deployed alembic head (read-only SELECT) vs the repo chain head."""
    try:
        stdout = probe.podman(
            "exec",
            POSTGRES_CONTAINER,
            "psql",
            "-U",
            "forge",
            "-d",
            "forge",
            "-t",
            "-A",
            "-c",
            "SELECT version_num FROM alembic_version",
        )
    except ProbeError as exc:
        return _refused(str(exc))
    deployed = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
    if not deployed:
        return _refused("alembic_version answered no row — schema state unobservable")
    try:
        head = repo_schema_head(root)
    except ProbeError as exc:
        return _refused(str(exc))
    return {
        "status": "observed",
        "deployed_head": deployed,
        "repo_chain_head": head,
        "method": (f"podman exec {POSTGRES_CONTAINER} psql (read-only SELECT on alembic_version)"),
    }


def _gitlab_settings() -> tuple[str, str, str]:
    """GitLab base URL + token + webhook host via the EXISTING settings loader.

    ``forge.config.Settings`` is the only reader of ``.env`` here — the
    token never leaves the API call header and is never written to output.
    """
    from forge.config import Settings

    settings = Settings()
    url = str(settings.GITLAB_URL).rstrip("/")
    token = settings.GITLAB_TOKEN.get_secret_value()
    return url, token, str(settings.FORGE_BOT_USERNAME)


def _gitlab_file(
    probe: LabProbe, base_url: str, token: str, project_id: int, path: str, ref: str
) -> dict[str, Any]:
    encoded = path.replace("/", "%2F")
    document = probe.http_get_json(
        f"{base_url}/api/v4/projects/{project_id}/repository/files/{encoded}",
        headers={"PRIVATE-TOKEN": token},
        params={"ref": ref},
    )
    content = base64.b64decode(str(document.get("content", ""))).decode("utf-8", "replace")
    return {
        "status": "observed",
        "project_id": project_id,
        "path": path,
        "ref": ref,
        "content_sha256": str(document.get("content_sha256", "")),
        "last_commit_id": str(document.get("last_commit_id", "")),
        "content": content,
    }


_TEMPLATE_REF_RE = re.compile(r"/forge/([^/]+)/ci/templates/")


def _template_include_refs(content: str) -> list[str]:
    """The git refs the installed template's remote includes pin."""
    return sorted(set(_TEMPLATE_REF_RE.findall(content)))


def stage_lane(
    probe: LabProbe,
    root: Path,
    intended: IntendedProfile,
    project_id: int = DEFAULT_GITLAB_PROJECT_ID,
    lane_venv: Path = DEFAULT_LANE_VENV,
) -> dict[str, Any]:
    """The pinned lane wheel + the INSTALLED target template state."""
    observation: dict[str, Any] = {
        "status": "observed",
        "pinned_wheel": {
            "source": intended.source,
            "url": intended.wheel_url,
            "sha256": intended.wheel_sha256,
            "release_version": intended.release_version,
        },
    }
    try:
        base_url, token, _ = _gitlab_settings()
        template = _gitlab_file(probe, base_url, token, project_id, ".gitlab-ci.yml", "main")
    except ProbeError as exc:
        observation["installed_template"] = _refused(str(exc))
    except Exception as exc:  # noqa: BLE001 — credentials/settings problems are refusals
        observation["installed_template"] = _refused(f"settings/GitLab API unreachable: {exc}")
    else:
        observation["installed_template"] = {
            key: value for key, value in template.items() if key != "content"
        }
        observation["installed_template"]["include_refs"] = _template_include_refs(
            template["content"]
        )
    observation["installed_lane_wheel"] = _installed_lane_wheel(lane_venv)
    return observation


def _installed_lane_wheel(lane_venv: Path) -> dict[str, Any]:
    """The lane venv's installed forge wheel — or an honest refusal.

    The lab's lane venv lives on the CI runner (created per job); from the
    control host the honest answer may be "not observable here".
    """
    if not lane_venv.is_dir():
        return _refused(
            f"lane venv {lane_venv} absent on this host — the lane installs per CI job "
            "on the runner; observe it there or via the next install-check"
        )
    dist_info = sorted(lane_venv.glob("lib/python3.*/site-packages/forge-*.dist-info"))
    if not dist_info:
        return _refused(f"lane venv {lane_venv} carries no forge dist-info — not installed")
    name = dist_info[-1].name
    return {
        "status": "observed",
        "dist_info": name,
        "version": name[len("forge-") : -len(".dist-info")],
    }


def stage_runner(
    probe: LabProbe, containers: Sequence[str] = (WORKER_CONTAINER,)
) -> dict[str, Any]:
    """Runner/worker container image identities + GitLab runner availability."""
    observation: dict[str, Any] = {
        "status": "observed",
        "containers": [_container_image(probe, container) for container in containers],
    }
    try:
        base_url, token, _ = _gitlab_settings()
        runners = probe.http_get_json(
            f"{base_url}/api/v4/projects/{DEFAULT_GITLAB_PROJECT_ID}/runners",
            headers={"PRIVATE-TOKEN": token},
        )
    except Exception as exc:  # noqa: BLE001 — availability is best-effort observation
        observation["gitlab_runners"] = _refused(str(exc))
    else:
        observation["gitlab_runners"] = {
            "status": "observed",
            "runners": [
                {
                    "id": runner.get("id"),
                    "description": runner.get("description"),
                    "status": runner.get("status"),
                    "active": runner.get("active"),
                }
                for runner in runners
            ],
        }
    return observation


def _parse_budget_profiles(value: str) -> dict[str, Any]:
    """Whether FORGE_BUDGET_PROFILES carries NUMERICAL ceilings (not just presence)."""
    text = value.strip()
    if not text:
        return {"present": False}
    try:
        profiles = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"present": True, "numerical": False, "error": f"unparseable JSON: {exc}"}
    if not isinstance(profiles, dict) or not profiles:
        return {"present": True, "numerical": False, "error": "no named budget profiles"}
    axes = ("max_calls", "max_tokens", "wallclock_s")
    per_profile: dict[str, Any] = {}
    numerical = True
    for name, entry in profiles.items():
        limits = {
            axis: entry.get(axis) for axis in axes if isinstance(entry, dict) and axis in entry
        }
        has_numerical = any(
            isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in limits.values()
        )
        per_profile[str(name)] = {"axes": limits, "numerical": has_numerical}
        numerical = numerical and has_numerical
    return {"present": True, "numerical": numerical, "profiles": per_profile}


def _container_env(probe: LabProbe, container: str) -> dict[str, str]:
    stdout = probe.podman("inspect", container, "--format", "{{json .Config.Env}}")
    entries = json.loads(stdout)
    env: dict[str, str] = {}
    for entry in entries:
        name, _, value = str(entry).partition("=")
        env[name] = value
    return env


def stage_caps(
    probe: LabProbe, containers: Sequence[str] = (APP_CONTAINER, WORKER_CONTAINER)
) -> dict[str, Any]:
    """Budget-cap env on the app/worker containers: numerical vs absent.

    Values are NEVER copied into the report (env carries secrets) — only
    the presence and numerical-ness of each cap variable is recorded.
    """
    observation: dict[str, Any] = {"status": "observed", "required": list(BUDGET_CAP_ENV_NAMES)}
    per_container: dict[str, Any] = {}
    for container in containers:
        try:
            env = _container_env(probe, container)
        except ProbeError as exc:
            per_container[container] = _refused(str(exc))
            continue
        observed: dict[str, Any] = {}
        for name in BUDGET_CAP_ENV_NAMES:
            value = env.get(name, "")
            if name == "FORGE_BUDGET_PROFILES":
                observed[name] = _parse_budget_profiles(value)
            else:
                observed[name] = (
                    {"present": True, "numerical": value.strip().isdigit()}
                    if value.strip()
                    else {"present": False}
                )
        per_container[container] = {"status": "observed", "caps": observed}
    observation["containers"] = per_container
    app_caps = per_container.get(APP_CONTAINER, {})
    if app_caps.get("status") == "observed":
        observation["caps_present_and_numerical"] = all(
            cap.get("numerical") for cap in app_caps["caps"].values()
        )
    else:
        observation["caps_present_and_numerical"] = None
    return observation


# ---------------------------------------------------------------------------
# The derived compatibility verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompatibilityCheck:
    check: str
    expected: str
    observed: str
    result: str  # "match" | "mismatch" | "unverified"
    resolution: str

    def to_json(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "expected": self.expected,
            "observed": self.observed,
            "result": self.result,
            "resolution": self.resolution,
        }


RESOLUTIONS: Mapping[str, str] = {
    "control-plane.version == pinned release": (
        "runbook §3 step 2 — pull the promoted image digest and recreate forge-app "
        "+ forge-worker via podman (NOT docker-compose)"
    ),
    "control-plane.image_digest == promoted image digest": (
        "runbook §3 step 2 — pin ghcr.io/forcewake/forge@<promoted digest>, not localhost/forge:dev"
    ),
    "schema at repo chain head": (
        "runbook §3 step 3 — python -m forge.migrate on the new image BEFORE the "
        "consumers start (026 -> 027)"
    ),
    "lane template pinned to the promoted release": (
        "runbook §5 — refresh the project's .gitlab-ci.yml remote includes to the "
        "v<promoted> tag refs"
    ),
    "numerical budget caps configured": (
        "runbook §4 — append the FORGE_BUDGET_PROFILES / FORGE_LANE_BUDGET_SECONDS "
        "block to .env and recreate the containers"
    ),
    "installed lane wheel observable": (
        "runbook §6 — observe the lane wheel on the runner (or via the qualification "
        "install-check) and record it in the next profile record"
    ),
}


def derive_compatibility(
    intended: IntendedProfile, stages: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """DERIVE the verdict — aligned only when every check matched observed fact."""
    checks: list[CompatibilityCheck] = []
    control = stages.get("control-plane", {})
    if control.get("status") != "observed":
        checks.append(
            CompatibilityCheck(
                check="control-plane.version == pinned release",
                expected=intended.release_version,
                observed=control.get("reason", "stage refused"),
                result="unverified",
                resolution=RESOLUTIONS["control-plane.version == pinned release"],
            )
        )
    else:
        reported = str(control.get("reported_version", ""))
        checks.append(
            CompatibilityCheck(
                check="control-plane.version == pinned release",
                expected=intended.release_version,
                observed=reported,
                result="match" if reported == intended.release_version else "mismatch",
                resolution=RESOLUTIONS["control-plane.version == pinned release"],
            )
        )
    image = control.get("image", {}) if isinstance(control.get("image"), dict) else {}
    checks.append(
        CompatibilityCheck(
            check="control-plane.image_digest == promoted image digest",
            expected=intended.image_digest,
            observed=(
                str(image.get("image_digest", ""))
                if image.get("status") == "observed"
                else image.get("reason", "image probe refused")
            ),
            result=(
                "match"
                if image.get("status") == "observed"
                and str(image.get("image_digest")) == intended.image_digest
                else ("mismatch" if image.get("status") == "observed" else "unverified")
            ),
            resolution=RESOLUTIONS["control-plane.image_digest == promoted image digest"],
        )
    )
    schema = stages.get("schema", {})
    if schema.get("status") == "observed":
        deployed = str(schema.get("deployed_head", ""))
        checks.append(
            CompatibilityCheck(
                check="schema at repo chain head",
                expected=intended.schema_head,
                observed=deployed,
                result="match" if deployed == intended.schema_head else "mismatch",
                resolution=RESOLUTIONS["schema at repo chain head"],
            )
        )
    else:
        checks.append(
            CompatibilityCheck(
                check="schema at repo chain head",
                expected=intended.schema_head,
                observed=schema.get("reason", "stage refused"),
                result="unverified",
                resolution=RESOLUTIONS["schema at repo chain head"],
            )
        )
    lane = stages.get("lane", {})
    template = lane.get("installed_template", {})
    if template.get("status") == "observed":
        include_refs = [str(ref) for ref in template.get("include_refs", ())]
        pinned_ref = f"v{intended.release_version}"
        observed_refs = ",".join(include_refs) or "(no remote includes)"
        checks.append(
            CompatibilityCheck(
                check="lane template pinned to the promoted release",
                expected=pinned_ref,
                observed=observed_refs,
                result="match" if include_refs == [pinned_ref] else "mismatch",
                resolution=RESOLUTIONS["lane template pinned to the promoted release"],
            )
        )
    else:
        checks.append(
            CompatibilityCheck(
                check="lane template pinned to the promoted release",
                expected=f"v{intended.release_version}",
                observed=template.get("reason", "stage refused"),
                result="unverified",
                resolution=RESOLUTIONS["lane template pinned to the promoted release"],
            )
        )
    wheel = lane.get("installed_lane_wheel", {})
    wheel_observed = wheel.get("status") == "observed"
    checks.append(
        CompatibilityCheck(
            check="installed lane wheel observable",
            expected=f"forge=={intended.release_version} in the lane venv",
            observed=(
                f"forge=={wheel.get('version', '?')}"
                if wheel_observed
                else wheel.get("reason", "stage refused")
            ),
            result=(
                "match"
                if wheel_observed and str(wheel.get("version")) == intended.release_version
                else ("mismatch" if wheel_observed else "unverified")
            ),
            resolution=RESOLUTIONS["installed lane wheel observable"],
        )
    )
    caps = stages.get("caps", {})
    caps_ok = caps.get("caps_present_and_numerical")
    checks.append(
        CompatibilityCheck(
            check="numerical budget caps configured",
            expected="all of " + ", ".join(BUDGET_CAP_ENV_NAMES) + " numerical",
            observed=(
                "present and numerical"
                if caps_ok is True
                else (
                    "absent or non-numerical"
                    if caps_ok is False
                    else caps.get("reason", "stage refused")
                )
            ),
            result="match"
            if caps_ok is True
            else ("mismatch" if caps_ok is False else "unverified"),
            resolution=RESOLUTIONS["numerical budget caps configured"],
        )
    )
    verdict = "aligned" if all(check.result == "match" for check in checks) else "misaligned"
    return {
        "verdict": verdict,
        "derived_from": "the intended profile vs the stages' observed facts",
        "checks": [check.to_json() for check in checks],
    }


# ---------------------------------------------------------------------------
# Orchestration + CLI
# ---------------------------------------------------------------------------


def run_inventory(
    probe: LabProbe,
    root: Path,
    stages: Sequence[str] = STAGES,
    gitlab_project_id: int = DEFAULT_GITLAB_PROJECT_ID,
    lane_venv: Path = DEFAULT_LANE_VENV,
) -> dict[str, Any]:
    """Run the requested stages read-only and derive the compatibility verdict."""
    unknown = [stage for stage in stages if stage not in STAGES]
    if unknown:
        raise ValueError(f"unknown stage {unknown[0]!r} — the vocabulary is {list(STAGES)}")
    intended = load_intended_profile(root)
    observations: dict[str, Any] = {}
    for stage in stages:
        if stage == "control-plane":
            observations[stage] = stage_control_plane(probe)
        elif stage == "schema":
            observations[stage] = stage_schema(probe, root)
        elif stage == "lane":
            observations[stage] = stage_lane(probe, root, intended, gitlab_project_id, lane_venv)
        elif stage == "runner":
            observations[stage] = stage_runner(probe)
        elif stage == "caps":
            observations[stage] = stage_caps(probe)
    return {
        "stamp": INVENTORY_STAMP,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "intended_profile": intended.to_json(),
        "stages": observations,
        "compatibility_verdict": derive_compatibility(intended, observations),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/inventory_lab.py",
        description=(
            "R37-06 (#287): read-only inventory of the real GitLab CE lab against the "
            "intended supported profile. No container is touched beyond read-only "
            "inspect/exec-SELECT; nothing is written outside --out."
        ),
    )
    parser.add_argument(
        "--stage",
        default="all",
        help=f"one of {STAGES} or 'all' (default: all)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default: qualification/inventory-<date>.json)",
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="the repository root")
    parser.add_argument(
        "--app-health-url", default=DEFAULT_APP_HEALTH_URL, help="the app's /health URL"
    )
    parser.add_argument(
        "--gitlab-project",
        type=int,
        default=DEFAULT_GITLAB_PROJECT_ID,
        help="the lab integration project id (default: 68)",
    )
    parser.add_argument(
        "--lane-venv",
        type=Path,
        default=DEFAULT_LANE_VENV,
        help="the lane venv path to observe (default: /tmp/forge-lane-venv)",
    )
    args = parser.parse_args(argv)

    stages = list(STAGES) if args.stage == "all" else [args.stage.strip()]
    out = args.out or (
        args.root / "qualification" / f"inventory-{datetime.now(timezone.utc):%Y-%m-%d}.json"
    )
    probe = LabProbe(app_health_url=args.app_health_url)
    try:
        document = run_inventory(
            probe,
            args.root,
            stages=stages,
            gitlab_project_id=args.gitlab_project,
            lane_venv=args.lane_venv,
        )
    except (ProbeError, ValueError) as exc:
        print(f"inventory_lab: REFUSED: {exc}", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verdict = document["compatibility_verdict"]
    print(f"inventory_lab: wrote {out}")
    print(f"inventory_lab: compatibility verdict: {verdict['verdict']}")
    for check in verdict["checks"]:
        print(f"  - {check['result']:>10}  {check['check']}: {check['observed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
