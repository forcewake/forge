"""R37-08 (#289) — the AUTHORIZED alignment of the real GitLab CE lab.

Issue #289 qualifies ONE live single-writer workflow with real tools,
model and provider. The recorded basis (``qualification/inventory-2026-09-24.json``,
verdict **misaligned**) refuses any paid flow: the deployed control plane
is a stale 0.28.0 dev build, the schema is one head behind, and the
numerical budget caps are absent. This script moves the lab onto the
repo's own head BEFORE the paid run — scripted, idempotent, receipted:

1. **backup** — ``pg_dump`` out of the lab Postgres (runbook §3 step 0)
   into the MAINTAINER-PRIVATE storage base (``FORGE_PRIVATE_BACKUP_DIR``,
   default ``~/forge-private/backups``), never into the repository — an
   in-repo backup destination is REFUSED (R38-03 / #304: operational
   database bytes are not publishable evidence; only a sanitized receipt
   is);
2. **rollback-tag** — the current image gets an addressable alias, so the
   pre-alignment build stays rollback-addressable (images are never
   pruned by this script);
3. **build** — ``podman build -t localhost/forge:dev .`` from the repo
   (the ``Containerfile``). NOTE: the runbook's default route is the
   promoted GHCR digest; the R37-08 qualification deliberately builds the
   WORKING TREE because the live flow needs this session's dispatch
   envelope (#288), which is newer than the promoted release. The digest
   axis therefore stays honestly *mismatched* (local dev build) in the
   inventory — recorded, never hidden;
4. **stop** — the consumers only (``forge-worker``, ``forge-app``); the
   shared Postgres/Redis/LiteLLM containers are never touched;
5. **migrate** — ``python -m forge.migrate`` on the NEW image via
   ``podman run --rm``, BEFORE the consumers start (runbook §3 step 3);
6. **recreate** — both consumers via ``podman run`` (NEVER
   docker-compose) with the SAME config observed by ``podman inspect``
   (every env entry, mount, port and the network) PLUS the numerical
   budget caps (runbook §4): ``FORGE_BUDGET_PROFILES`` /
   ``FORGE_LANE_BUDGET_SECONDS`` (+ grace/commit-cycle caps);
7. **verify** — ``/health`` reports the repo's ``forge.__version__``,
   the deployed alembic head equals the repo chain head, and both
   containers carry the caps (present and numerical).

Every executed step writes a receipt (``receipt_id`` + argv with env
values REDACTED + timings + tail) into the receipts document, so the
evaluation README can quote executed facts instead of intentions.

Idempotent: when the verify probes already pass, the mutation steps are
skipped with recorded reasons (a second run is verify-only).

Default mode is ``--dry-run`` (prints the plan, executes nothing);
``--apply`` executes. Secrets never appear in receipts or plan output.

Run from the repository root::

    uv run python scripts/align_lab.py --dry-run
    uv run python scripts/align_lab.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Versioned stamp of the alignment-receipts document.
ALIGNMENT_STAMP = "forge.lab.alignment/1"

#: The lab's consumers (recreated) and the shared, untouched containers.
APP_CONTAINER = "forge-app"
WORKER_CONTAINER = "forge-worker"
POSTGRES_CONTAINER = "forge-postgres"

#: The lab's podman network (observed on both consumers).
LAB_NETWORK = "podman"

#: The app's health surface (inventory_lab probes the same URL).
DEFAULT_APP_HEALTH_URL = "http://localhost:8420/health"

#: The image tag the lab runs (and this script rebuilds from the tree).
IMAGE_TAG = "localhost/forge:dev"

#: The numerical budget caps (runbook §4 — the documented example values).
DEFAULT_BUDGET_PROFILES = (
    '{"trivial":{"max_calls":8,"max_tokens":40000,"wallclock_s":900},'
    '"standard":{"max_calls":40,"max_tokens":200000,"wallclock_s":3600},'
    '"heavy":{"max_calls":120,"max_tokens":600000,"wallclock_s":10800}}'
)
DEFAULT_LANE_BUDGET_SECONDS = "1800"
DEFAULT_LANE_GRACE_SECONDS = "60"
DEFAULT_MAX_COMMIT_CYCLES = "3"

#: Caps that must be present AND numerical on BOTH recreated containers.
REQUIRED_CAPS: tuple[str, ...] = ("FORGE_BUDGET_PROFILES", "FORGE_LANE_BUDGET_SECONDS")

#: Env names podman injects per-container — never passed explicitly.
_RUNTIME_ENV = frozenset({"HOSTNAME"})

DEFAULT_RECEIPTS = (
    REPO_ROOT / "docs" / "evaluation" / "2026-09-24-live-single-writer" / "alignment-receipts.json"
)

#: R38-03 (#304): operational backups go to MAINTAINER-PRIVATE storage.
PRIVATE_BACKUP_ENV = "FORGE_PRIVATE_BACKUP_DIR"


def private_backup_base(env: Mapping[str, str] | None = None) -> Path:
    """The private storage base: ``FORGE_PRIVATE_BACKUP_DIR`` or the default."""
    if env is None:
        env = dict(os.environ)
    return Path(env.get(PRIVATE_BACKUP_ENV, "") or (Path.home() / "forge-private" / "backups"))


def resolve_backup_dir(
    root: Path,
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Where the pre-alignment pg_dump lands — private, NEVER inside the repo.

    An explicit ``--backup-dir`` is honored verbatim (the operator chose it)
    but STILL refused when it sits inside the repository tree: the default
    (``private_backup_base`` + the session's date subdirectory) exists so
    the R38-03 defect — an operational database backup committed under
    ``docs/`` — cannot recur.
    """
    base = explicit if explicit is not None else private_backup_base(env)
    # resolve() both sides so a relative or symlinked in-repo destination
    # cannot slip past the refusal
    resolved_root = root.expanduser().resolve()
    resolved = Path(base).expanduser().resolve()
    if resolved == resolved_root or resolved.is_relative_to(resolved_root):
        raise AlignmentError(
            f"backup destination {resolved} is INSIDE the repository {root} — refused "
            "(R38-03/#304: operational backups belong in private storage, e.g. "
            f"{PRIVATE_BACKUP_ENV}; only a sanitized receipt is publishable)"
        )
    if explicit is None:
        date = datetime.now(timezone.utc)
        resolved = resolved / f"{date:%Y-%m-%d}"
    return resolved


class AlignmentError(Exception):
    """An alignment step could not be planned or executed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_receipt_id() -> str:
    return f"r3708-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# The execution boundary — the ONLY thing that touches the lab (tests fake it)
# ---------------------------------------------------------------------------


class LabRunner:
    """Real executor: subprocess podman, httpx-free urllib health probe."""

    def podman(self, *args: str, timeout: float = 600.0) -> str:
        completed = subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
        if completed.returncode != 0:
            raise AlignmentError(
                f"podman {' '.join(args[:3])}… failed ({completed.returncode}): "
                f"{completed.stderr.strip()[:300]}"
            )
        return completed.stdout

    def http_get_json(self, url: str, timeout: float = 10.0) -> Any:
        import httpx

        try:
            response = httpx.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            raise AlignmentError(f"GET {url} unreachable: {exc}") from exc
        if response.status_code != 200:
            raise AlignmentError(f"GET {url} answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise AlignmentError(f"GET {url} did not answer JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Observed state (read-only) — what the recreate step must preserve
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerSpec:
    """Everything a recreate must reproduce, as OBSERVED via podman inspect."""

    name: str
    image: str
    env: tuple[str, ...] = ()  # runtime-set entries only (image defaults removed)
    binds: tuple[str, ...] = ()  # "src:dst" host binds
    ports: tuple[str, ...] = ()  # "host:container" publications
    network: str = LAB_NETWORK
    cmd: tuple[str, ...] = ()

    def run_argv(self, extra_env: Mapping[str, str]) -> list[str]:
        """The podman run argv reproducing this spec (+ extra env)."""
        argv = [
            "run",
            "-d",
            "--name",
            self.name,
            "--network",
            self.network,
        ]
        for bind in self.binds:
            argv += ["-v", bind]
        for publication in self.ports:
            argv += ["-p", publication]
        # last-wins dedupe by key: an extra_env pin that already sits in the
        # observed env (a re-run after a previous aligned recreate) replaces
        # it instead of duplicating the -e entry.
        merged: dict[str, str] = {}
        for entry in list(self.env) + [f"{key}={value}" for key, value in extra_env.items()]:
            key, _, value = entry.partition("=")
            merged[key] = value
        for key, value in merged.items():
            argv += ["-e", f"{key}={value}"]
        argv.append(self.image)
        argv += list(self.cmd)
        return argv


def _inspect(runner: LabRunner, container: str) -> dict[str, Any]:
    stdout = runner.podman("inspect", container, "--format", "json")
    document = json.loads(stdout)
    if isinstance(document, list):
        document = document[0]
    return document


def read_container_spec(
    runner: LabRunner, container: str, image_env: Sequence[str]
) -> ContainerSpec:
    """Observe a container's run configuration (env minus image defaults)."""
    document = _inspect(runner, container)
    config = document.get("Config") or {}
    host_config = document.get("HostConfig") or {}
    raw_env = [str(entry) for entry in (config.get("Env") or [])]
    image_defaults = set(image_env)
    env = tuple(
        entry
        for entry in raw_env
        if entry not in image_defaults and entry.partition("=")[0] not in _RUNTIME_ENV
    )
    binds = []
    for bind in host_config.get("Binds") or []:
        parts = str(bind).split(":")
        if len(parts) >= 2:
            binds.append(f"{parts[0]}:{parts[1]}")  # drop propagation options
    port_map = host_config.get("PortBindings") or {}
    ports = []
    for container_port, mappings in sorted(port_map.items()):
        for mapping in mappings or []:
            host_ip = str(mapping.get("HostIp") or "0.0.0.0")
            host_port = str(mapping.get("HostPort") or "")
            if host_port:
                ports.append(f"{host_ip}:{host_port}:{container_port.split('/')[0]}")
    networks = list((document.get("NetworkSettings") or {}).get("Networks") or {})
    network = networks[0] if networks else LAB_NETWORK
    cmd = tuple(str(part) for part in (config.get("Cmd") or []))
    return ContainerSpec(
        name=container,
        image=str(config.get("ImageName") or document.get("ImageName") or IMAGE_TAG),
        env=env,
        binds=tuple(binds),
        ports=tuple(ports),
        network=network,
        cmd=cmd,
    )


def _image_env(runner: LabRunner, image: str) -> list[str]:
    stdout = runner.podman("image", "inspect", image, "--format", "{{json .Config.Env}}")
    return [str(entry) for entry in json.loads(stdout)]


def worker_spec_binds(runner: LabRunner) -> tuple[str, ...]:
    """The worker's observed host binds ("src:dst")."""
    return read_container_spec(runner, WORKER_CONTAINER, _image_env(runner, IMAGE_TAG)).binds


def repo_version(root: Path) -> str:
    """The repo's ``forge.__version__`` — parsed, not imported (hermetic)."""
    init = root / "src" / "forge" / "__init__.py"
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", init.read_text(encoding="utf-8"))
    if match is None:
        raise AlignmentError(f"no __version__ in {init}")
    return match.group(1)


def repo_schema_head(root: Path) -> str:
    """The migration chain head (same derivation as inventory_lab)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.inventory_lab import ProbeError
    from scripts.inventory_lab import repo_schema_head as _head

    try:
        return _head(root)
    except ProbeError as exc:
        raise AlignmentError(str(exc)) from exc


def deployed_schema_head(runner: LabRunner) -> str:
    stdout = runner.podman(
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
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise AlignmentError("alembic_version answered no row")
    return lines[0]


def caps_state(runner: LabRunner) -> dict[str, dict[str, Any]]:
    """Present-and-numerical check per cap, per consumer container."""
    state: dict[str, dict[str, Any]] = {}
    for container in (APP_CONTAINER, WORKER_CONTAINER):
        document = _inspect(runner, container)
        env: dict[str, str] = {}
        for entry in (document.get("Config") or {}).get("Env") or []:
            name, _, value = str(entry).partition("=")
            env[name] = value
        observed: dict[str, Any] = {}
        for cap in REQUIRED_CAPS:
            value = env.get(cap, "").strip()
            if cap == "FORGE_BUDGET_PROFILES":
                try:
                    profiles = json.loads(value) if value else None
                except json.JSONDecodeError:
                    profiles = None
                numerical = (
                    isinstance(profiles, dict)
                    and bool(profiles)
                    and all(
                        isinstance(entry, dict)
                        and any(
                            isinstance(entry.get(axis), int)
                            and not isinstance(entry.get(axis), bool)
                            and entry[axis] > 0
                            for axis in ("max_calls", "max_tokens", "wallclock_s")
                        )
                        for entry in profiles.values()
                    )
                )
                observed[cap] = {"present": bool(value), "numerical": numerical}
            else:
                observed[cap] = {
                    "present": bool(value),
                    "numerical": value.isdigit() and int(value) > 0,
                }
        state[container] = observed
    return state


def health_version(runner: LabRunner, url: str = DEFAULT_APP_HEALTH_URL, attempts: int = 1) -> Any:
    """The /health document (polling helps right after a recreate)."""
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return runner.http_get_json(url)
        except AlignmentError as exc:
            last = exc
            time.sleep(3)
    raise last if last else AlignmentError("health unreachable")


# ---------------------------------------------------------------------------
# The plan: idempotent steps, each with redacted argv
# ---------------------------------------------------------------------------

_REDACT_SAFE = frozenset(REQUIRED_CAPS) | {
    "FORGE_LANE_GRACE_SECONDS",
    "FORGE_MAX_COMMIT_CYCLES",
}


def redact(argv: Sequence[str]) -> list[str]:
    """Redact ``-e KEY=VALUE`` values except the non-secret cap variables."""
    redacted: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("-e", "--env") and index + 1 < len(argv):
            key, _, value = argv[index + 1].partition("=")
            redacted.append(f"{item} {key}={value if key in _REDACT_SAFE else '***'}")
            index += 2
            continue
        redacted.append(item)
        index += 1
    return redacted


@dataclass
class Step:
    """One planned alignment action."""

    name: str
    description: str
    argv: list[list[str]] = field(default_factory=list)
    prepare_dirs: list[Path] = field(default_factory=list)  # host dirs created before argv

    def render(self) -> str:
        lines = [f"  {self.name}: {self.description}"]
        for command in self.argv:
            lines.append("    $ " + " ".join(redact(command)))
        return "\n".join(lines)


@dataclass
class Plan:
    """The steps to execute plus what was observed while planning."""

    steps: list[Step] = field(default_factory=list)
    already_aligned: bool = False
    observations: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        header = (
            "alignment plan (ALREADY ALIGNED — verify only):"
            if self.already_aligned
            else ("alignment plan:")
        )
        return "\n".join([header, *[step.render() for step in self.steps]])


def build_plan(
    runner: LabRunner,
    root: Path,
    *,
    budget_profiles: str = DEFAULT_BUDGET_PROFILES,
    lane_budget_seconds: str = DEFAULT_LANE_BUDGET_SECONDS,
    lane_grace_seconds: str = DEFAULT_LANE_GRACE_SECONDS,
    max_commit_cycles: str = DEFAULT_MAX_COMMIT_CYCLES,
    extra_env: Mapping[str, str] | None = None,
    worker_mounts: Sequence[str] = (),
    backup_dir: Path | None = None,
    app_health_url: str = DEFAULT_APP_HEALTH_URL,
) -> Plan:
    """Observe the lab read-only and derive the alignment steps.

    Idempotency: when health already reports the repo version, the schema
    is at the repo head, both containers carry numerical caps AND every
    ``extra_env`` key is present with the exact requested value, the plan
    is verify-only.
    """
    extra_env = dict(extra_env or {})
    caps = caps_state(runner)
    health_note = ""
    try:
        health = health_version(runner, app_health_url)
        deployed_version = str(health.get("version", ""))
    except AlignmentError as exc:
        # A stopped/unreachable control plane (a previous run interrupted
        # mid-alignment) is a PLANNABLE state, never a refusal: the plan's
        # recreate steps will bring it back.
        deployed_version = ""
        health_note = str(exc)
    head_repo = repo_schema_head(root)
    head_deployed = deployed_schema_head(runner)
    version = repo_version(root)
    version_ok = deployed_version == version
    schema_ok = head_deployed == head_repo
    caps_ok = all(
        cap["present"] and cap["numerical"]
        for per_container in caps.values()
        for cap in per_container.values()
    )
    # extra_env presence: every requested key must sit on BOTH consumers
    # with the exact value (a changed pin means the consumers predate it).
    container_env: dict[str, dict[str, str]] = {}
    for container in (APP_CONTAINER, WORKER_CONTAINER):
        document = _inspect(runner, container)
        container_env[container] = {
            str(entry).partition("=")[0]: str(entry).partition("=")[2]
            for entry in (document.get("Config") or {}).get("Env") or []
        }
    extra_env_ok = all(
        container_env.get(container, {}).get(key) == value
        for container in (APP_CONTAINER, WORKER_CONTAINER)
        for key, value in extra_env.items()
    )
    worker_binds = set(worker_spec_binds(runner))
    worker_mounts_ok = all(mount in worker_binds for mount in worker_mounts)
    observations = {
        "repo_version": version,
        "repo_schema_head": head_repo,
        "deployed_version": deployed_version,
        "deployed_schema_head": head_deployed,
        "controlplane_unreachable": health_note,
        "caps": caps,
        "version_ok": version_ok,
        "schema_ok": schema_ok,
        "caps_ok": caps_ok,
        "extra_env_ok": extra_env_ok,
        "extra_env_keys": sorted(extra_env),
        "worker_mounts_ok": worker_mounts_ok,
        "worker_mounts_requested": list(worker_mounts),
    }

    app_spec = read_container_spec(runner, APP_CONTAINER, _image_env(runner, IMAGE_TAG))
    worker_spec = read_container_spec(runner, WORKER_CONTAINER, _image_env(runner, IMAGE_TAG))
    if worker_mounts:
        existing = set(worker_spec.binds)
        worker_spec = replace(
            worker_spec,
            binds=tuple(dict.fromkeys((*worker_spec.binds, *worker_mounts)))
            if any(mount not in existing for mount in worker_mounts)
            else worker_spec.binds,
        )
    extra_env = {
        "FORGE_BUDGET_PROFILES": budget_profiles,
        "FORGE_LANE_BUDGET_SECONDS": lane_budget_seconds,
        "FORGE_LANE_GRACE_SECONDS": lane_grace_seconds,
        "FORGE_MAX_COMMIT_CYCLES": max_commit_cycles,
        **extra_env,
    }
    plan = Plan(observations=observations)
    plan.steps.append(
        Step(
            name="verify",
            description=(
                "/health reports the repo version, schema at the repo head, "
                "numerical caps on both consumers"
            ),
        )
    )
    if version_ok and schema_ok and caps_ok and extra_env_ok and worker_mounts_ok:
        plan.already_aligned = True
        return plan

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rollback_tag = f"localhost/forge:pre-r3708-{stamp}"
    dump_remote = f"/tmp/pre-r3708-alignment-{stamp}.dump"
    backup_target = resolve_backup_dir(root, backup_dir) / f"pre-r3708-alignment-{stamp}.dump"
    database_url = next(
        (entry.partition("=")[2] for entry in app_spec.env if entry.startswith("DATABASE_URL=")),
        "",
    )
    if not database_url:
        raise AlignmentError("the app container's env carries no DATABASE_URL to migrate with")

    plan.steps = [
        Step(
            name="backup",
            description=f"pg_dump out of {POSTGRES_CONTAINER} (runbook §3 step 0)",
            argv=[
                [
                    "exec",
                    POSTGRES_CONTAINER,
                    "pg_dump",
                    "-U",
                    "forge",
                    "-d",
                    "forge",
                    "-Fc",
                    "-f",
                    dump_remote,
                ],
                ["cp", f"{POSTGRES_CONTAINER}:{dump_remote}", str(backup_target)],
            ],
            prepare_dirs=[backup_target.parent],
        ),
        Step(
            name="rollback-tag",
            description=(
                f"keep the current image addressable as {rollback_tag} "
                "(images are never pruned — rollback stays possible)"
            ),
            argv=[["tag", IMAGE_TAG, rollback_tag]],
        ),
        Step(
            name="build",
            description=(
                f"podman build {IMAGE_TAG} from the repo (Containerfile) — the working "
                "tree carries the #288 dispatch envelope the promoted release predates"
            ),
            argv=[["build", "-t", IMAGE_TAG, str(root)]],
        ),
        Step(
            name="stop",
            description="stop the consumers only (postgres/redis/litellm untouched)",
            argv=[["stop", WORKER_CONTAINER, APP_CONTAINER]],
        ),
        Step(
            name="migrate",
            description=(
                f"python -m forge.migrate on the NEW image before consumers start "
                f"({head_deployed} -> {head_repo})"
            ),
            argv=[
                [
                    "run",
                    "--rm",
                    "--network",
                    LAB_NETWORK,
                    "-e",
                    f"DATABASE_URL={database_url}",
                    IMAGE_TAG,
                    "python",
                    "-m",
                    "forge.migrate",
                ]
            ],
        ),
        Step(
            name=f"recreate-{APP_CONTAINER}",
            description=(
                "remove the stopped consumer, then podman run with the OBSERVED "
                "config plus the budget caps"
            ),
            argv=[["rm", APP_CONTAINER], app_spec.run_argv(extra_env)],
        ),
        Step(
            name=f"recreate-{WORKER_CONTAINER}",
            description=(
                "remove the stopped consumer, then podman run with the OBSERVED "
                "config plus the budget caps"
            ),
            argv=[["rm", WORKER_CONTAINER], worker_spec.run_argv(extra_env)],
        ),
        Step(
            name="verify",
            description=(
                "/health reports the repo version, schema at the repo head, "
                "numerical caps on both consumers"
            ),
        ),
    ]
    return plan


# ---------------------------------------------------------------------------
# Execution: every step receipts what actually ran
# ---------------------------------------------------------------------------


def _run_step_argv(runner: LabRunner, argv: Sequence[str], timeout: float) -> dict[str, Any]:
    """Run ONE podman argv through the boundary, capturing the honest outcome."""
    started = _now()
    wall = time.monotonic()
    stdout = ""
    stderr = ""
    returncode = 0
    try:
        stdout = runner.podman(*argv, timeout=timeout)
    except AlignmentError as exc:
        stderr = str(exc)
        returncode = 1
    return {
        "argv": redact(list(argv)),
        "started_at": started,
        "finished_at": _now(),
        "wall_seconds": round(time.monotonic() - wall, 3),
        "returncode": returncode,
        "stdout_tail": stdout.strip()[-400:],
        "stderr_tail": stderr.strip()[-400:],
    }


def execute_verify(runner: LabRunner, root: Path, app_health_url: str) -> dict[str, Any]:
    """The verify probes: version, schema head, caps — honest pass/fail."""
    # the recreated app can take a couple of minutes through lifespan
    # startup (alembic context, agent registry, MCP session manager)
    health = health_version(runner, app_health_url, attempts=60)
    deployed_version = str(health.get("version", ""))
    version = repo_version(root)
    head_repo = repo_schema_head(root)
    head_deployed = deployed_schema_head(runner)
    caps = caps_state(runner)
    checks = {
        "health.status": str(health.get("status", "")),
        "controlplane.version == repo __version__": {
            "expected": version,
            "observed": deployed_version,
            "result": "match" if deployed_version == version else "mismatch",
        },
        "schema == repo chain head": {
            "expected": head_repo,
            "observed": head_deployed,
            "result": "match" if head_deployed == head_repo else "mismatch",
        },
        "budget caps numerical": {
            "expected": f"all of {', '.join(REQUIRED_CAPS)} numerical on both consumers",
            "observed": {
                container: {
                    cap: (
                        "present+numerical"
                        if state["present"] and state["numerical"]
                        else "absent-or-non-numerical"
                    )
                    for cap, state in per_container.items()
                }
                for container, per_container in caps.items()
            },
            "result": (
                "match"
                if all(
                    state["present"] and state["numerical"]
                    for per_container in caps.values()
                    for state in per_container.values()
                )
                else "mismatch"
            ),
        },
    }
    passed = (
        checks["controlplane.version == repo __version__"]["result"] == "match"
        and checks["schema == repo chain head"]["result"] == "match"
        and checks["budget caps numerical"]["result"] == "match"
    )
    return {"checks": checks, "result": "aligned" if passed else "misaligned"}


_STEP_TIMEOUTS: Mapping[str, float] = {
    "backup": 300.0,
    "rollback-tag": 60.0,
    "build": 2400.0,
    "stop": 180.0,
    "migrate": 600.0,
    f"recreate-{APP_CONTAINER}": 120.0,
    f"recreate-{WORKER_CONTAINER}": 120.0,
}


def execute_plan(
    runner: LabRunner,
    plan: Plan,
    root: Path,
    receipts_path: Path,
    app_health_url: str = DEFAULT_APP_HEALTH_URL,
) -> dict[str, Any]:
    """Execute the plan; every step receipts; a failure stops the run."""
    document: dict[str, Any] = {
        "stamp": ALIGNMENT_STAMP,
        "generated_at": _now(),
        "already_aligned_at_start": plan.already_aligned,
        "observations": plan.observations,
        "steps": [],
    }
    if receipts_path.is_file():
        try:
            previous = json.loads(receipts_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("stamp") == ALIGNMENT_STAMP:
                history = previous.get("runs", [])
                document["runs"] = list(history)
        except json.JSONDecodeError:
            document["runs"] = []
    document.setdefault("runs", [])

    run: dict[str, Any] = {"started_at": _now(), "steps": [], "result": "executing"}
    for step in plan.steps:
        receipt: dict[str, Any] = {
            "receipt_id": new_receipt_id(),
            "step": step.name,
            "description": step.description,
        }
        if step.name == "verify":
            try:
                outcome = execute_verify(runner, root, app_health_url)
            except AlignmentError as exc:
                outcome = {"result": "refused", "reason": str(exc)}
            receipt.update(outcome)
            run["steps"].append(receipt)
            run["result"] = (
                "aligned"
                if outcome.get("result") == "aligned"
                else outcome.get("result", "refused")
            )
            break
        outcomes = []
        failed = False
        for directory in step.prepare_dirs:
            directory.mkdir(parents=True, exist_ok=True)
        for argv in step.argv:
            outcome = _run_step_argv(runner, argv, _STEP_TIMEOUTS.get(step.name, 600.0))
            outcomes.append(outcome)
            if outcome["returncode"] != 0:
                failed = True
                break
        receipt["commands"] = outcomes
        receipt["result"] = "failed" if failed else "ok"
        run["steps"].append(receipt)
        if failed:
            run["result"] = f"stopped-at-{step.name}"
            break
    run["finished_at"] = _now()
    document["runs"].append(run)
    document["result"] = run["result"]
    document["generated_at"] = _now()

    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    receipts_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return document


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/align_lab.py",
        description=(
            "R37-08 (#289): align the real GitLab CE lab onto the repo head — "
            "scripted, idempotent, receipted. Default is --dry-run (plan only)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without executing anything (default behavior)",
    )
    parser.add_argument("--apply", action="store_true", help="EXECUTE the plan (stops consumers)")
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help=(
            "where the pre-alignment pg_dump lands. DEFAULT: the private storage "
            f"base ({PRIVATE_BACKUP_ENV}, else ~/forge-private/backups) plus the "
            "session's date subdirectory — R38-03 (#304): a destination inside "
            "the repository is REFUSED; only a sanitized receipt is publishable"
        ),
    )
    parser.add_argument("--budget-profiles", default=DEFAULT_BUDGET_PROFILES)
    parser.add_argument("--lane-budget-seconds", default=DEFAULT_LANE_BUDGET_SECONDS)
    parser.add_argument("--lane-grace-seconds", default=DEFAULT_LANE_GRACE_SECONDS)
    parser.add_argument("--max-commit-cycles", default=DEFAULT_MAX_COMMIT_CYCLES)
    parser.add_argument(
        "--worker-mount",
        action="append",
        default=[],
        metavar="SRC:DST",
        help=(
            "a host bind the worker must carry (repeatable). R37-08 live-found: "
            "the lab worker ran WITHOUT the shared /app/data volume, so the app "
            "and the worker did not share the checkpoint store — a /retry found "
            "no checkpoint and parked (uncertain). The intended wiring (compose) "
            "shares forge-data with BOTH consumers; this flag restores it."
        ),
    )
    parser.add_argument(
        "--extra-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "additional env pinned on BOTH recreated consumers (repeatable); "
            "a missing or different value re-runs the alignment. R37-08 uses "
            "FORGE_HARNESS_PREFERENCE=claude-sdk-lane — the profile's frozen "
            "exact-resume lane (the batch claude-code lane cannot restore WIP)"
        ),
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--app-health-url", default=DEFAULT_APP_HEALTH_URL)
    args = parser.parse_args(argv)

    if args.dry_run and args.apply:
        print("align_lab: REFUSED: --dry-run and --apply are mutually exclusive", file=sys.stderr)
        return 2
    extra_env: dict[str, str] = {}
    for item in args.extra_env:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            print(f"align_lab: REFUSED: --extra-env {item!r} is not KEY=VALUE", file=sys.stderr)
            return 2
        extra_env[key.strip()] = value
    runner = LabRunner()
    try:
        plan = build_plan(
            runner,
            args.root,
            budget_profiles=args.budget_profiles,
            lane_budget_seconds=args.lane_budget_seconds,
            lane_grace_seconds=args.lane_grace_seconds,
            max_commit_cycles=args.max_commit_cycles,
            extra_env=extra_env,
            worker_mounts=[item for item in args.worker_mount if item.strip()],
            backup_dir=args.backup_dir,
            app_health_url=args.app_health_url,
        )
    except AlignmentError as exc:
        print(f"align_lab: REFUSED: {exc}", file=sys.stderr)
        return 2

    print(plan.render())
    if not args.apply:
        print("align_lab: dry-run — nothing executed")
        return 0

    document = execute_plan(runner, plan, args.root, args.receipts, args.app_health_url)
    print(f"align_lab: receipts → {args.receipts}")
    print(f"align_lab: result: {document.get('result')}")
    return 0 if document.get("result") == "aligned" else 1


if __name__ == "__main__":
    raise SystemExit(main())
