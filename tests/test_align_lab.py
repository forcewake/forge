"""R37-08 (#289) — the authorized lab-alignment machinery.

``scripts/align_lab.py`` is driven end to end against FAKES: a podman/HTTP
stub (no container runtime, no network) and a fake repo tree (version +
alembic chain). What is held here:

- the PLAN is derived from OBSERVED state only: a misaligned lab yields
  backup → rollback-tag → build → stop → migrate → recreate(app+worker,
  observed config + caps) → verify, while an already-aligned lab yields a
  verify-only plan (idempotency);
- the recreate argv preserves the observed env (minus image defaults and
  runtime-injected names), mounts, ports, network and command, and appends
  the numerical budget caps;
- dry-run executes NOTHING: the only podman traffic is read-only probing;
- every executed step receipts an id, redacted argv, timings and outcome,
  and a failing step stops the run honestly (never continues past it);
- the verify step derives aligned/misaligned from the probes, never from
  the plan's expectations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.align_lab import (
    APP_CONTAINER,
    IMAGE_TAG,
    WORKER_CONTAINER,
    AlignmentError,
    build_plan,
    execute_plan,
    main,
    redact,
)

REPO_VERSION = "0.36.0"
REPO_HEAD = "027"

APP_ENV = [
    "GITLAB_URL=https://gitlab.example.test",
    "GITLAB_TOKEN=glpat-secret-app",
    "DATABASE_URL=postgresql+asyncpg://forge:forge@host.containers.internal:5433/forge",
    "FORGE_IMPLEMENTER_BACKEND=ci_harness",
    "PYTHONUNBUFFERED=1",
    "HOSTNAME=a613bfe02ff2",
]
WORKER_ENV = [
    "GITLAB_URL=https://gitlab.example.test",
    "DATABASE_URL=postgresql+asyncpg://forge:forge@host.containers.internal:5433/forge",
    "FORGE_IMPLEMENTER_BACKEND=ci_harness:claude-code",
    "FORGE_LANE_CONTROL_URL=http://host.containers.internal:8420",
    "PYTHONUNBUFFERED=1",
    "HOSTNAME=325a9c0ce759",
]
IMAGE_ENV = ["PYTHONUNBUFFERED=1", "PATH=/app/.venv/bin:/usr/local/bin"]
CAPS_ENV = [
    'FORGE_BUDGET_PROFILES={"trivial":{"max_calls":8,"max_tokens":40000,"wallclock_s":900}}',
    "FORGE_LANE_BUDGET_SECONDS=1800",
    "FORGE_LANE_GRACE_SECONDS=60",
    "FORGE_MAX_COMMIT_CYCLES=3",
]


class FakeLabRunner:
    """The execution-boundary stub — records every podman call it sees."""

    def __init__(
        self,
        *,
        health_version: str = "0.28.0",
        schema: str = "026",
        update_health_on_recreate: bool = True,
    ) -> None:
        self.health = {"status": "ok", "version": health_version, "database": "ok"}
        self.schema = schema
        self.update_health_on_recreate = update_health_on_recreate
        self.envs: dict[str, list[str]] = {
            APP_CONTAINER: list(APP_ENV),
            WORKER_CONTAINER: list(WORKER_ENV),
        }
        self.recreated: dict[str, bool] = {APP_CONTAINER: False, WORKER_CONTAINER: False}
        self.fail_prefixes: list[tuple[str, ...]] = []
        self.calls: list[tuple[str, ...]] = []

    # -- helpers ---------------------------------------------------------

    def _container_doc(self, container: str) -> dict[str, Any]:
        env = list(self.envs[container])
        if self.recreated[container]:
            present = {entry.partition("=")[0] for entry in env}
            env += [entry for entry in CAPS_ENV if entry.partition("=")[0] not in present]
        binds = (
            ["/repo/data:/app/data:rprivate,rbind", "/repo/.secrets:/app/.secrets:rprivate,rbind"]
            if container == APP_CONTAINER
            else ["/repo/.secrets:/app/.secrets:rprivate,rbind"]
        )
        ports = (
            {"8420/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8420"}]}
            if container == APP_CONTAINER
            else {}
        )
        cmd = (
            ["uvicorn", "forge.main:app", "--host", "0.0.0.0", "--port", "8420"]
            if container == APP_CONTAINER
            else ["python", "-m", "forge.worker"]
        )
        return {
            "Config": {
                "Env": env,
                "Cmd": cmd,
                "ImageName": IMAGE_TAG,
            },
            "ImageName": IMAGE_TAG,
            "HostConfig": {"Binds": binds, "PortBindings": ports},
            "NetworkSettings": {"Networks": {"podman": {}}},
        }

    def _fail_if_requested(self, args: tuple[str, ...]) -> None:
        for prefix in self.fail_prefixes:
            if args[: len(prefix)] == prefix:
                raise AlignmentError(f"podman {' '.join(args)} failed (1): stubbed failure")

    # -- the boundary ------------------------------------------------------

    def podman(self, *args: str, timeout: float = 600.0) -> str:
        self.calls.append(args)
        self._fail_if_requested(args)
        if args[0] == "inspect" and len(args) > 1 and args[1] in self.envs:
            return json.dumps([self._container_doc(args[1])])
        if args[:2] == ("image", "inspect"):
            return json.dumps(IMAGE_ENV)
        if args[0] == "exec" and any("alembic_version" in part for part in args):
            return self.schema + "\n"
        if args[0] == "exec":  # the pg_dump backup inside the lab postgres
            return ""
        if args[0] == "tag":
            return ""
        if args[0] == "rm":
            return ""
        if args[0] == "cp":
            return ""
        if args[0] == "build":
            return "sha256:" + "b" * 64 + "\n"
        if args[0] == "stop":
            return ""
        if args[0] == "run":
            if "--rm" in args:  # the migrate step: the new image carries the chain
                self.schema = REPO_HEAD
                return ""
            name = args[args.index("--name") + 1]
            self.recreated[name] = True
            if name == APP_CONTAINER and self.update_health_on_recreate:
                self.health = {"status": "ok", "version": REPO_VERSION, "database": "ok"}
            return "container-id\n"
        raise AlignmentError(f"podman {' '.join(args)} failed (1): not stubbed")

    def http_get_json(self, url: str, timeout: float = 10.0) -> Any:
        return dict(self.health)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "forge").mkdir(parents=True)
    (root / "src" / "forge" / "__init__.py").write_text(
        f'__version__ = "{REPO_VERSION}"\n', encoding="utf-8"
    )
    versions = root / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "026_previous.py").write_text(
        'revision = "026"\ndown_revision = "025"\n', encoding="utf-8"
    )
    (versions / "027_head.py").write_text(
        'revision = "027"\ndown_revision = "026"\n', encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_plan_orders_steps_and_preserves_observed_config(repo_root: Path) -> None:
    runner = FakeLabRunner(health_version="0.28.0", schema="026")
    plan = build_plan(runner, repo_root)
    assert [step.name for step in plan.steps] == [
        "backup",
        "rollback-tag",
        "build",
        "stop",
        "migrate",
        f"recreate-{APP_CONTAINER}",
        f"recreate-{WORKER_CONTAINER}",
        "verify",
    ]
    app_step = next(s for s in plan.steps if s.name == f"recreate-{APP_CONTAINER}")
    assert list(app_step.argv[0]) == ["rm", APP_CONTAINER]  # the stopped consumer goes first
    app_argv = app_step.argv[1]
    text = " ".join(app_argv)
    # observed config preserved: env, mounts, ports, network, command
    assert "-e GITLAB_URL=https://gitlab.example.test" in text
    assert "-v /repo/data:/app/data" in text
    assert "-p 0.0.0.0:8420:8420" in text
    assert "--network podman" in text
    assert "uvicorn forge.main:app --host 0.0.0.0 --port 8420" in text
    # the caps block appended
    assert "-e FORGE_LANE_BUDGET_SECONDS=1800" in text
    assert "-e FORGE_BUDGET_PROFILES=" in text
    worker_argv = next(s for s in plan.steps if s.name == f"recreate-{WORKER_CONTAINER}").argv[-1]
    worker_text = " ".join(worker_argv)
    assert "python -m forge.worker" in worker_text
    assert "-e FORGE_LANE_CONTROL_URL=http://host.containers.internal:8420" in worker_text
    assert "-e FORGE_LANE_BUDGET_SECONDS=1800" in worker_text
    # the migrate step runs on the NEW image before the consumers start
    migrate = next(s for s in plan.steps if s.name == "migrate").argv[0]
    assert "python -m forge.migrate" in " ".join(migrate)
    stop = next(s for s in plan.steps if s.name == "stop").argv[0]
    assert list(stop) == ["stop", WORKER_CONTAINER, APP_CONTAINER]


def test_plan_drops_image_defaults_and_runtime_env(repo_root: Path) -> None:
    runner = FakeLabRunner()
    plan = build_plan(runner, repo_root)
    app_step = next(s for s in plan.steps if s.name == f"recreate-{APP_CONTAINER}")
    assert list(app_step.argv[0]) == ["rm", APP_CONTAINER]  # the stopped consumer goes first
    app_argv = app_step.argv[1]
    text = " ".join(app_argv)
    assert "HOSTNAME=" not in text  # runtime-injected, never explicit
    # PYTHONUNBUFFERED equals the image default — it was not runtime-set
    assert text.count("-e PYTHONUNBUFFERED=1") == 0


def test_plan_refuses_without_database_url(repo_root: Path) -> None:
    runner = FakeLabRunner()
    runner.envs[APP_CONTAINER] = [
        entry for entry in APP_ENV if not entry.startswith("DATABASE_URL")
    ]
    with pytest.raises(AlignmentError, match="DATABASE_URL"):
        build_plan(runner, repo_root)


def test_aligned_lab_yields_verify_only_plan(repo_root: Path) -> None:
    runner = FakeLabRunner(health_version=REPO_VERSION, schema=REPO_HEAD)
    for container in runner.envs:
        runner.envs[container] = runner.envs[container] + CAPS_ENV
        runner.recreated[container] = True
    plan = build_plan(runner, repo_root)
    assert plan.already_aligned is True
    assert [step.name for step in plan.steps] == ["verify"]


def test_garbage_caps_keep_the_lab_misaligned(repo_root: Path) -> None:
    runner = FakeLabRunner(health_version=REPO_VERSION, schema=REPO_HEAD)
    for container in runner.envs:
        runner.envs[container] = runner.envs[container] + [
            "FORGE_BUDGET_PROFILES=not-json",
            "FORGE_LANE_BUDGET_SECONDS=1800",
        ]
        runner.recreated[container] = True
    plan = build_plan(runner, repo_root)
    assert plan.already_aligned is False
    assert f"recreate-{APP_CONTAINER}" in [step.name for step in plan.steps]


# ---------------------------------------------------------------------------
# dry-run executes nothing
# ---------------------------------------------------------------------------


def test_dry_run_probes_read_only_and_executes_nothing(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeLabRunner()
    monkeypatch.setattr("scripts.align_lab.LabRunner", lambda: runner)
    code = main(["--root", str(repo_root)])
    assert code == 0
    mutating = {"build", "stop", "run", "tag", "cp", "rm"}
    assert all(call[0] not in mutating for call in runner.calls)
    printed = capsys.readouterr().out
    assert "alignment plan:" in printed
    assert "dry-run — nothing executed" in printed


# ---------------------------------------------------------------------------
# execution + receipts
# ---------------------------------------------------------------------------


def test_execute_receipts_every_step_in_order(repo_root: Path, tmp_path: Path) -> None:
    runner = FakeLabRunner()
    plan = build_plan(runner, repo_root, backup_dir=tmp_path / "backups")
    receipts = tmp_path / "receipts.json"
    document = execute_plan(runner, plan, repo_root, receipts)
    assert document["result"] == "aligned"
    run = document["runs"][-1]
    assert [step["step"] for step in run["steps"]] == [
        "backup",
        "rollback-tag",
        "build",
        "stop",
        "migrate",
        f"recreate-{APP_CONTAINER}",
        f"recreate-{WORKER_CONTAINER}",
        "verify",
    ]
    for step in run["steps"]:
        assert step["receipt_id"].startswith("r3708-")
    verify = run["steps"][-1]
    assert verify["result"] == "aligned"
    assert verify["checks"]["controlplane.version == repo __version__"]["observed"] == REPO_VERSION
    assert verify["checks"]["schema == repo chain head"]["observed"] == REPO_HEAD
    # ordering actually executed: stop before migrate, migrate before recreates
    names = []
    for call in runner.calls:
        if call[0] == "run":
            names.append("migrate" if "--rm" in call else "recreate")
        else:
            names.append(call[0])
    assert names.index("stop") < names.index("migrate")
    assert names.index("migrate") < names.index("recreate")
    # secrets never land in the receipts
    receipts_text = receipts.read_text(encoding="utf-8")
    assert "glpat-secret-app" not in receipts_text
    assert "postgresql+asyncpg" not in receipts_text
    assert "FORGE_LANE_BUDGET_SECONDS" in receipts_text


def test_execute_stops_at_first_failure(repo_root: Path, tmp_path: Path) -> None:
    runner = FakeLabRunner()
    runner.fail_prefixes.append(("build",))
    plan = build_plan(runner, repo_root, backup_dir=tmp_path / "backups")
    receipts = tmp_path / "receipts.json"
    document = execute_plan(runner, plan, repo_root, receipts)
    assert document["result"] == "stopped-at-build"
    run = document["runs"][-1]
    build = next(step for step in run["steps"] if step["step"] == "build")
    assert build["result"] == "failed"
    # nothing after the failure executed
    assert ("stop", WORKER_CONTAINER, APP_CONTAINER) not in runner.calls


def test_execute_reports_honest_mismatch(repo_root: Path, tmp_path: Path) -> None:
    # the recreate lands an OLD build (health never catches up) — the
    # verify probes must see through the plan and report the mismatch
    runner = FakeLabRunner(update_health_on_recreate=False)
    plan = build_plan(runner, repo_root, backup_dir=tmp_path / "backups")
    receipts = tmp_path / "receipts.json"
    document = execute_plan(runner, plan, repo_root, receipts)
    assert document["result"] == "misaligned"
    verify = document["runs"][-1]["steps"][-1]
    assert verify["checks"]["controlplane.version == repo __version__"]["result"] == "mismatch"
    assert verify["checks"]["controlplane.version == repo __version__"]["observed"] == "0.28.0"


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_redact_masks_env_values_except_caps() -> None:
    redacted = redact(
        [
            "run",
            "-e",
            "DATABASE_URL=postgresql+asyncpg://forge:forge@h:5433/forge",
            "-e",
            "FORGE_LANE_BUDGET_SECONDS=1800",
            "image",
        ]
    )
    assert any("DATABASE_URL=***" in item for item in redacted)
    assert any("FORGE_LANE_BUDGET_SECONDS=1800" in item for item in redacted)
    assert not any("forge:forge@" in item for item in redacted)


def test_stopped_control_plane_is_plannable(repo_root: Path) -> None:
    # a run interrupted mid-alignment (consumers stopped) must still plan:
    # the recreate steps are exactly what brings the lab back
    runner = FakeLabRunner()

    def unreachable(url: str, timeout: float = 10.0) -> Any:
        raise AlignmentError("GET http://localhost:8420/health unreachable")

    runner.http_get_json = unreachable  # type: ignore[method-assign]
    plan = build_plan(runner, repo_root, backup_dir=repo_root / "backups")
    assert plan.already_aligned is False
    assert f"recreate-{APP_CONTAINER}" in [step.name for step in plan.steps]
    assert plan.observations["controlplane_unreachable"]
