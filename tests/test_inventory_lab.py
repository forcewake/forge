"""R37-06 (#287) — the read-only lab inventory machinery.

``scripts/inventory_lab.py`` is driven end to end against FAKES: an HTTP
+ podman stub (no network, no container runtime) and a fake repo tree
(promotion record + alembic chain). What is held here:

- every stage records its observation — or its REFUSAL with the reason,
  never a guess: an unreachable app, an unanswerable psql probe, missing
  GitLab credentials and an absent lane venv each produce refused-with-
  reason content;
- the compatibility verdict is DERIVED from observed fact: match only
  when the observation equals the pinned intent, mismatch when it does
  not, unverified when the stage refused — and the overall verdict is
  aligned only when every check matched;
- the caps stage distinguishes present-with-NUMERICAL-value from present-
  but-garbage and absent (FORGE_BUDGET_PROFILES parsed per profile);
- the inventory is read-only by construction: the probe boundary is the
  only thing that touches the lab, and every podman call the fakes see
  is an inspect/exec-SELECT shape.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.inventory_lab import (
    APP_CONTAINER,
    INVENTORY_STAMP,
    ProbeError,
    _parse_budget_profiles,
    _template_include_refs,
    derive_compatibility,
    latest_promotion_record,
    load_intended_profile,
    repo_schema_head,
    run_inventory,
    stage_caps,
    stage_control_plane,
    stage_lane,
    stage_runner,
    stage_schema,
)

PROMOTED_DIGEST = "sha256:" + "d" * 64
DEPLOYED_DIGEST = "sha256:" + "a" * 64
WHEEL_SHA = "4" * 64


# ---------------------------------------------------------------------------
# The fakes: HTTP + podman + a repo tree
# ---------------------------------------------------------------------------


class FakeProbe:
    """The probe boundary stub — records every podman call it receives."""

    def __init__(
        self,
        *,
        health: dict[str, Any] | None = None,
        health_error: str | None = None,
        template_content: str | None = None,
        template_error: str | None = None,
        runners: list[dict[str, Any]] | None = None,
        podman_outputs: dict[tuple[str, ...], str] | None = None,
        app_health_url: str = "http://localhost:8420/health",
    ) -> None:
        self.health = health
        self.health_error = health_error
        self.template_content = template_content
        self.template_error = template_error
        self.runners = runners or []
        self.podman_outputs = podman_outputs or {}
        self.app_health_url = app_health_url
        self.podman_calls: list[tuple[str, ...]] = []
        self.http_calls: list[str] = []

    def http_get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        self.http_calls.append(url)
        if "/health" in url:
            if self.health_error:
                raise ProbeError(self.health_error)
            assert self.health is not None
            return self.health
        if "/repository/files/" in url:
            if self.template_error:
                raise ProbeError(self.template_error)
            assert self.template_content is not None
            return {
                "file_path": ".gitlab-ci.yml",
                "content_sha256": "0" * 64,
                "last_commit_id": "48cd85c0fd9cf208ceb472a1ca8288c9fbc48bf7",
                "content": base64.b64encode(self.template_content.encode()).decode(),
            }
        if "/runners" in url:
            return self.runners
        raise ProbeError(f"unexpected URL {url}")

    def podman(self, *args: str) -> str:
        self.podman_calls.append(args)
        for prefix, output in self.podman_outputs.items():
            if args[: len(prefix)] == prefix:
                return output
        raise ProbeError(f"podman {' '.join(args)} failed (1): not stubbed")


def _podman_stub(
    deployed_version_head: str = "026",
    app_env: list[str] | None = None,
    worker_env: list[str] | None = None,
) -> dict[tuple[str, ...], str]:
    """The podman answers a deployed-but-misaligned lab gives."""
    return {
        ("inspect", APP_CONTAINER, "--format", "{{.ImageName}} {{.ImageDigest}}"): (
            f"localhost/forge:dev {DEPLOYED_DIGEST}"
        ),
        ("inspect", "forge-worker", "--format", "{{.ImageName}} {{.ImageDigest}}"): (
            f"localhost/forge:dev {DEPLOYED_DIGEST}"
        ),
        ("inspect", APP_CONTAINER, "--format", "{{json .Config.Env}}"): json.dumps(
            app_env if app_env is not None else ["FORGE_REQUIRED_JOBS=smoke"]
        ),
        ("inspect", "forge-worker", "--format", "{{json .Config.Env}}"): json.dumps(
            worker_env if worker_env is not None else ["FORGE_REQUIRED_JOBS=smoke"]
        ),
        ("exec", "forge-postgres", "psql"): f" {deployed_version_head}\n",
    }


def _repo_tree(tmp_path: Path) -> Path:
    """A fake repo root: one promotion record + a two-step alembic chain."""
    promotion = {
        "version": "0.36.0",
        "image_digest": PROMOTED_DIGEST,
        "wheel_sha256": WHEEL_SHA,
        "wheel_url": "https://github.com/forcewake/forge/releases/download/v0.36.0/"
        "forge-0.36.0-py3-none-any.whl",
    }
    evidence = tmp_path / "docs" / "releases" / "evidence" / "v0.36.0"
    evidence.mkdir(parents=True)
    (evidence / "promotion.json").write_text(json.dumps(promotion), encoding="utf-8")
    older = tmp_path / "docs" / "releases" / "evidence" / "v0.35.0"
    older.mkdir(parents=True)
    (older / "promotion.json").write_text(json.dumps({**promotion, "version": "0.35.0"}))
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "001_root.py").write_text(
        'revision = "026"\ndown_revision = "025"\n', encoding="utf-8"
    )
    (versions / "002_head.py").write_text(
        'revision = "027"\ndown_revision = "026"\n', encoding="utf-8"
    )
    return tmp_path


def _aligned_lab_env() -> list[str]:
    return [
        "FORGE_REQUIRED_JOBS=smoke",
        "FORGE_BUDGET_PROFILES="
        + json.dumps({"standard": {"max_calls": 40, "max_tokens": 200000, "wallclock_s": 3600}}),
        "FORGE_LANE_BUDGET_SECONDS=1800",
    ]


TEMPLATE_AT_PROMOTED_REF = "\n".join(
    f"include:\n  - remote: 'https://raw.githubusercontent.com/forcewake/forge/"
    f"v0.36.0/ci/templates/{name}.gitlab-ci.yml'"
    for name in ("claude-code", "claude-sdk-lane")
)

TEMPLATE_AT_OLD_REF = TEMPLATE_AT_PROMOTED_REF.replace("v0.36.0", "2fbc321")


# ---------------------------------------------------------------------------
# The intended profile, from the repo's own evidence
# ---------------------------------------------------------------------------


def test_the_intended_profile_comes_from_the_latest_promotion_record(
    tmp_path: Path,
) -> None:
    root = _repo_tree(tmp_path)
    assert latest_promotion_record(root) == (root / "docs/releases/evidence/v0.36.0/promotion.json")
    intended = load_intended_profile(root)
    assert intended.release_version == "0.36.0"
    assert intended.image_digest == PROMOTED_DIGEST
    assert intended.wheel_sha256 == WHEEL_SHA
    assert intended.schema_head == "027"
    assert intended.required_caps == ("FORGE_BUDGET_PROFILES", "FORGE_LANE_BUDGET_SECONDS")


def test_a_branched_alembic_chain_refuses_instead_of_guessing(tmp_path: Path) -> None:
    root = _repo_tree(tmp_path)
    (root / "alembic/versions/003_branch.py").write_text(
        'revision = "028"\ndown_revision = "026"\n', encoding="utf-8"
    )
    with pytest.raises(ProbeError, match="heads"):
        repo_schema_head(root)


# ---------------------------------------------------------------------------
# Stages: observed fact or refused-with-reason
# ---------------------------------------------------------------------------


def test_control_plane_records_the_reported_version_and_image() -> None:
    probe = FakeProbe(
        health={"status": "ok", "version": "0.28.0", "database": "ok"},
        podman_outputs=_podman_stub(),
    )
    observation = stage_control_plane(probe)  # type: ignore[arg-type]
    assert observation["status"] == "observed"
    assert observation["reported_version"] == "0.28.0"
    assert observation["image"]["image_digest"] == DEPLOYED_DIGEST
    assert observation["image"]["image_name"] == "localhost/forge:dev"
    # read-only shapes only: inspect, never start/stop/restart
    assert all(call[0] in {"inspect", "exec"} for call in probe.podman_calls)


def test_an_unreachable_app_refuses_the_stage_with_the_reason() -> None:
    probe = FakeProbe(health_error="GET http://localhost:8420/health unreachable: boom")
    observation = stage_control_plane(probe)  # type: ignore[arg-type]
    assert observation["status"] == "refused"
    assert "unreachable" in observation["reason"]


def test_schema_compares_the_deployed_head_to_the_repo_chain(tmp_path: Path) -> None:
    root = _repo_tree(tmp_path)
    probe = FakeProbe(podman_outputs=_podman_stub(deployed_version_head="026"))
    observation = stage_schema(probe, root)  # type: ignore[arg-type]
    assert observation["status"] == "observed"
    assert observation["deployed_head"] == "026"
    assert observation["repo_chain_head"] == "027"
    assert "read-only SELECT" in observation["method"]


def test_a_dead_postgres_refuses_the_schema_stage(tmp_path: Path) -> None:
    root = _repo_tree(tmp_path)
    probe = FakeProbe(podman_outputs={})
    observation = stage_schema(probe, root)  # type: ignore[arg-type]
    assert observation["status"] == "refused"
    assert "podman exec forge-postgres" in observation["reason"]


def test_lane_records_pinned_wheel_template_and_venv_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo_tree(tmp_path)
    monkeypatch.setattr(
        "scripts.inventory_lab._gitlab_settings",
        lambda: ("https://gitlab.example.com", "token", "forge"),
    )
    probe = FakeProbe(template_content=TEMPLATE_AT_OLD_REF, podman_outputs=_podman_stub())
    observation = stage_lane(
        probe,  # type: ignore[arg-type]
        root,
        load_intended_profile(root),
        lane_venv=tmp_path / "no-such-venv",
    )
    assert observation["pinned_wheel"]["sha256"] == WHEEL_SHA
    template = observation["installed_template"]
    assert template["status"] == "observed"
    assert template["include_refs"] == ["2fbc321"]
    # the lane venv is on the runner — an honest refusal, not a guess:
    assert observation["installed_lane_wheel"]["status"] == "refused"
    assert "absent on this host" in observation["installed_lane_wheel"]["reason"]


def test_lane_observes_an_installed_venv_wheel(tmp_path: Path) -> None:
    root = _repo_tree(tmp_path)
    venv = tmp_path / "venv" / "lib" / "python3.13" / "site-packages"
    venv.mkdir(parents=True)
    (venv / "forge-0.36.0.dist-info").mkdir()
    probe = FakeProbe(template_content=TEMPLATE_AT_PROMOTED_REF)
    observation = stage_lane(
        probe,  # type: ignore[arg-type]
        root,
        load_intended_profile(root),
        lane_venv=tmp_path / "venv",
    )
    assert observation["installed_lane_wheel"] == {
        "status": "observed",
        "dist_info": "forge-0.36.0.dist-info",
        "version": "0.36.0",
    }


def test_missing_credentials_refuse_the_template_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo_tree(tmp_path)

    def _no_credentials() -> tuple[str, str, str]:
        raise RuntimeError("Settings: GITLAB_URL not set")

    monkeypatch.setattr("scripts.inventory_lab._gitlab_settings", _no_credentials)
    probe = FakeProbe(template_error="unreachable")
    observation = stage_lane(
        probe,  # type: ignore[arg-type]
        root,
        load_intended_profile(root),
        lane_venv=tmp_path / "no-such-venv",
    )
    assert observation["installed_template"]["status"] == "refused"
    assert "GITLAB_URL" in observation["installed_template"]["reason"]


def test_runner_records_worker_digests_and_runner_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.inventory_lab._gitlab_settings",
        lambda: ("https://gitlab.example.com", "token", "forge"),
    )
    probe = FakeProbe(
        runners=[{"id": 3, "description": "forge-lab runner", "status": "stale"}],
        podman_outputs=_podman_stub(),
    )
    observation = stage_runner(probe)  # type: ignore[arg-type]
    assert observation["containers"][0]["image_digest"] == DEPLOYED_DIGEST
    assert observation["gitlab_runners"]["runners"][0]["status"] == "stale"


def test_runner_tolerates_an_unreachable_gitlab(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreachable() -> tuple[str, str, str]:
        raise ProbeError("no .env")

    monkeypatch.setattr("scripts.inventory_lab._gitlab_settings", _unreachable)
    probe = FakeProbe(podman_outputs=_podman_stub())
    observation = stage_runner(probe)  # type: ignore[arg-type]
    assert observation["status"] == "observed"
    assert observation["gitlab_runners"]["status"] == "refused"


# ---------------------------------------------------------------------------
# Caps: present-with-numerical-value vs present-but-garbage vs absent
# ---------------------------------------------------------------------------


def test_absent_caps_are_recorded_absent_and_non_numerical() -> None:
    probe = FakeProbe(podman_outputs=_podman_stub(app_env=[]))
    observation = stage_caps(probe)  # type: ignore[arg-type]
    app = observation["containers"][APP_CONTAINER]
    assert app["caps"]["FORGE_BUDGET_PROFILES"] == {"present": False}
    assert app["caps"]["FORGE_LANE_BUDGET_SECONDS"] == {"present": False}
    assert observation["caps_present_and_numerical"] is False


def test_numerical_caps_make_the_stage_numerical() -> None:
    probe = FakeProbe(podman_outputs=_podman_stub(app_env=_aligned_lab_env()))
    observation = stage_caps(probe)  # type: ignore[arg-type]
    app = observation["containers"][APP_CONTAINER]
    assert app["caps"]["FORGE_BUDGET_PROFILES"]["numerical"] is True
    assert app["caps"]["FORGE_LANE_BUDGET_SECONDS"] == {
        "present": True,
        "numerical": True,
    }
    assert observation["caps_present_and_numerical"] is True


def test_present_but_non_numerical_caps_stay_a_mismatch() -> None:
    probe = FakeProbe(podman_outputs=_podman_stub(app_env=["FORGE_BUDGET_PROFILES=not json"]))
    observation = stage_caps(probe)  # type: ignore[arg-type]
    app = observation["containers"][APP_CONTAINER]
    assert app["caps"]["FORGE_BUDGET_PROFILES"]["present"] is True
    assert app["caps"]["FORGE_BUDGET_PROFILES"]["numerical"] is False
    assert observation["caps_present_and_numerical"] is False


def test_budget_profiles_parsing_covers_the_limit_axes() -> None:
    numerical = _parse_budget_profiles(
        json.dumps({"standard": {"max_calls": 10, "wallclock_s": 60}})
    )
    assert numerical == {
        "present": True,
        "numerical": True,
        "profiles": {"standard": {"axes": {"max_calls": 10, "wallclock_s": 60}, "numerical": True}},
    }
    assert _parse_budget_profiles("") == {"present": False}
    assert _parse_budget_profiles("{oops")["numerical"] is False
    assert _parse_budget_profiles("{}")["numerical"] is False
    # a profile with NO numerical axis disarms the cap — not numerical:
    assert _parse_budget_profiles(json.dumps({"standard": {}}))["numerical"] is False


def test_the_caps_stage_never_copies_env_values() -> None:
    secret_env = ["FORGE_MCP_KEY=super-secret-value", "FORGE_BUDGET_PROFILES={}"]
    probe = FakeProbe(podman_outputs=_podman_stub(app_env=secret_env))
    observation = stage_caps(probe)  # type: ignore[arg-type]
    assert "super-secret-value" not in json.dumps(observation)


# ---------------------------------------------------------------------------
# The derived compatibility verdict
# ---------------------------------------------------------------------------


def _stages(probe: FakeProbe, root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(
        "scripts.inventory_lab._gitlab_settings",
        lambda: ("https://gitlab.example.com", "token", "forge"),
    )
    intended = load_intended_profile(root)
    return {
        "control-plane": stage_control_plane(probe),  # type: ignore[arg-type]
        "schema": stage_schema(probe, root),  # type: ignore[arg-type]
        "lane": stage_lane(probe, root, intended, lane_venv=root / "no-venv"),  # type: ignore[arg-type]
        "runner": stage_runner(probe),  # type: ignore[arg-type]
        "caps": stage_caps(probe),  # type: ignore[arg-type]
    }


def test_the_misaligned_lab_derives_misaligned_with_named_resolutions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo_tree(tmp_path)
    probe = FakeProbe(
        health={"status": "ok", "version": "0.28.0"},
        template_content=TEMPLATE_AT_OLD_REF,
        podman_outputs=_podman_stub(),
    )
    verdict = derive_compatibility(load_intended_profile(root), _stages(probe, root, monkeypatch))
    assert verdict["verdict"] == "misaligned"
    by_check = {check["check"]: check for check in verdict["checks"]}
    assert by_check["control-plane.version == pinned release"]["result"] == "mismatch"
    assert by_check["control-plane.version == pinned release"]["observed"] == "0.28.0"
    assert by_check["control-plane.image_digest == promoted image digest"]["result"] == "mismatch"
    assert by_check["schema at repo chain head"]["result"] == "mismatch"
    assert by_check["schema at repo chain head"]["observed"] == "026"
    assert by_check["lane template pinned to the promoted release"]["result"] == "mismatch"
    assert by_check["lane template pinned to the promoted release"]["observed"] == "2fbc321"
    assert by_check["numerical budget caps configured"]["result"] == "mismatch"
    assert by_check["installed lane wheel observable"]["result"] == "unverified"
    # every non-match names its resolution:
    for check in verdict["checks"]:
        if check["result"] != "match":
            assert check["resolution"].startswith("runbook §"), check


def test_a_fully_matched_lab_derives_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo_tree(tmp_path)
    venv = root / "venv" / "lib" / "python3.13" / "site-packages"
    venv.mkdir(parents=True)
    (venv / "forge-0.36.0.dist-info").mkdir()
    probe = FakeProbe(
        health={"status": "ok", "version": "0.36.0"},
        template_content=TEMPLATE_AT_PROMOTED_REF,
        podman_outputs={
            **_podman_stub(deployed_version_head="027", app_env=_aligned_lab_env()),
            ("inspect", APP_CONTAINER, "--format", "{{.ImageName}} {{.ImageDigest}}"): (
                f"ghcr.io/forcewake/forge {PROMOTED_DIGEST}"
            ),
        },
    )
    monkeypatch.setattr(
        "scripts.inventory_lab._gitlab_settings",
        lambda: ("https://gitlab.example.com", "token", "forge"),
    )
    intended = load_intended_profile(root)
    stages = {
        "control-plane": stage_control_plane(probe),  # type: ignore[arg-type]
        "schema": stage_schema(probe, root),  # type: ignore[arg-type]
        "lane": stage_lane(probe, root, intended, lane_venv=root / "venv"),  # type: ignore[arg-type]
        "runner": stage_runner(probe),  # type: ignore[arg-type]
        "caps": stage_caps(probe),  # type: ignore[arg-type]
    }
    verdict = derive_compatibility(intended, stages)
    assert verdict["verdict"] == "aligned"
    assert all(check["result"] == "match" for check in verdict["checks"])


def test_a_refused_image_probe_is_unverified_not_silently_skipped(tmp_path: Path) -> None:
    """An unobservable image identity must BLOCK alignment — a dropped
    check would let a half-observed lab derive 'aligned'."""
    root = _repo_tree(tmp_path)
    verdict = derive_compatibility(
        load_intended_profile(root),
        {
            "control-plane": {
                "status": "observed",
                "reported_version": "0.36.0",
                "image": {"status": "refused", "reason": "podman inspect failed (125)"},
            },
        },
    )
    by_check = {check["check"]: check for check in verdict["checks"]}
    image_check = by_check["control-plane.image_digest == promoted image digest"]
    assert image_check["result"] == "unverified"
    assert "podman inspect failed" in image_check["observed"]
    assert verdict["verdict"] == "misaligned"


def test_a_refused_stage_is_unverified_and_blocks_alignment(
    tmp_path: Path,
) -> None:
    root = _repo_tree(tmp_path)
    probe = FakeProbe(
        health={"status": "ok", "version": "0.36.0"},
        podman_outputs={
            ("inspect", APP_CONTAINER, "--format", "{{.ImageName}} {{.ImageDigest}}"): (
                f"ghcr.io/forcewake/forge {PROMOTED_DIGEST}"
            ),
        },
    )

    inner = probe.podman

    def _failing_psql(*args: str) -> str:
        if args[:2] == ("exec", "forge-postgres"):
            raise ProbeError("podman exec forge-postgres psql failed (125)")
        return inner(*args)

    probe.podman = _failing_psql  # type: ignore[method-assign]
    verdict = derive_compatibility(
        load_intended_profile(root),
        {
            "control-plane": stage_control_plane(probe),  # type: ignore[arg-type]
            "schema": stage_schema(probe, root),  # type: ignore[arg-type]
        },
    )
    by_check = {check["check"]: check for check in verdict["checks"]}
    assert by_check["schema at repo chain head"]["result"] == "unverified"
    assert "failed" in by_check["schema at repo chain head"]["observed"]
    assert verdict["verdict"] == "misaligned"


# ---------------------------------------------------------------------------
# The whole document
# ---------------------------------------------------------------------------


def test_run_inventory_emits_the_stamped_read_only_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo_tree(tmp_path)
    probe = FakeProbe(
        health={"status": "ok", "version": "0.28.0"},
        template_content=TEMPLATE_AT_OLD_REF,
        podman_outputs=_podman_stub(),
    )
    monkeypatch.setattr(
        "scripts.inventory_lab._gitlab_settings",
        lambda: ("https://gitlab.example.com", "token", "forge"),
    )
    document = run_inventory(probe, root, lane_venv=root / "no-venv")  # type: ignore[arg-type]
    assert document["stamp"] == INVENTORY_STAMP
    assert document["read_only"] is True
    assert document["intended_profile"]["release_version"] == "0.36.0"
    assert set(document["stages"]) == {
        "control-plane",
        "schema",
        "lane",
        "runner",
        "caps",
    }
    assert document["compatibility_verdict"]["verdict"] == "misaligned"
    assert "generated_at" in document
    # the GitLab token never reaches the document:
    assert "token" not in json.dumps(document)


def test_run_inventory_refuses_an_unknown_stage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        run_inventory(FakeProbe(), tmp_path, stages=["deploy"])  # type: ignore[arg-type]


def test_the_cli_writes_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.inventory_lab as inventory_lab

    root = _repo_tree(tmp_path)
    monkeypatch.setattr(
        "scripts.inventory_lab.run_inventory",
        lambda *args, **kwargs: {
            "stamp": INVENTORY_STAMP,
            "compatibility_verdict": {"verdict": "misaligned", "checks": []},
        },
    )
    out = tmp_path / "qualification" / "inventory-test.json"
    assert inventory_lab.main(["--root", str(root), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["stamp"] == INVENTORY_STAMP


def test_template_include_refs_extracts_the_pinned_git_refs() -> None:
    assert _template_include_refs(TEMPLATE_AT_OLD_REF) == ["2fbc321"]
    assert _template_include_refs("no includes here") == []
    # two different refs are both recorded, sorted and deduplicated:
    mixed = TEMPLATE_AT_PROMOTED_REF.replace(
        "https://raw.githubusercontent.com/forcewake/forge/v0.36.0/ci/templates/"
        "claude-code.gitlab-ci.yml",
        "https://raw.githubusercontent.com/forcewake/forge/v0.35.0/ci/templates/"
        "claude-code.gitlab-ci.yml",
    )
    assert _template_include_refs(mixed) == ["v0.35.0", "v0.36.0"]
