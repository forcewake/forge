"""Real isolated lane installation and immutable release artifact refusals."""

import copy
import json
import os
from pathlib import Path
import shutil
import tomllib

import pytest

from tools.release.artifacts import (
    locked_requirements,
    prepare,
    sha256,
    verify,
    workflow_pins,
)
from tools.release.bundle import build


@pytest.mark.slow
def test_actual_lane_bundle_installs_without_server_and_refuses_changed_authority(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = Path(os.environ.get("FORGE_LOCAL_BUNDLE_PROOF_OUT", tmp_path / "candidate"))
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    manifest = build(
        root, output, "forcewake/forge", f"v{version}-rc.local.20261106", ("claude",), offline=True
    )
    proof = manifest["install_proof"]
    assert proof["status"] == "passed" and proof["server_absent"]
    assert proof["model_sessions"] == proof["provider_requests"] == 0
    installed = {name.lower().replace("_", "-") for name in proof["installed"]}
    assert {"forge-lane", "forge-protocol", "claude-agent-sdk"} <= installed
    assert not installed & {"forge", "fastapi", "sqlalchemy", "alembic", "asyncpg", "aiosqlite"}
    assert proof["sdk_bundled_cli_sha256"]
    assert manifest["publication"] == "not-published" and manifest["image"] is None
    requirements = (output / "dist/lane-v1-requirements.txt").read_text()
    assert "forge-lane[claude] @ https://" in requirements
    assert "forge-protocol @ https://" in requirements
    assert f"forge-{version}-" not in requirements
    assert "@v1" not in (output / "dist/forge-lane.github.yml").read_text()
    assert manifest["workflow_commit"] in (output / "dist/forge-lane.github.yml").read_text()
    include = (output / "dist/forge-lane.gitlab-include.yml").read_text()
    assert manifest["gitlab_template_url"] in include and "project:" not in include
    dist = tmp_path / "manifest-check"
    shutil.copytree(output / "dist", dist)
    release = prepare(
        dist,
        "forcewake/forge",
        "v" + version,
        manifest["rc_tag"],
        manifest["commit"],
        "ghcr.io/forcewake/forge@sha256:" + "a" * 64,
    )
    before = {name: sha256(dist / name) for name in release["artifacts"]}
    verify(release, dist)
    assert before == {name: sha256(dist / name) for name in release["artifacts"]}
    changed = copy.deepcopy(release)
    changed["workflow_commit"] = "v1"
    with pytest.raises(ValueError, match="exact full source commit"):
        verify(changed, dist)
    with pytest.raises(ValueError, match="immutable source commit"):
        workflow_pins("forcewake/forge", "main")
    locked = tmp_path / "injected-lock.txt"
    locked.write_text("sqlalchemy==2.0.48 --hash=sha256:" + "b" * 64 + "\n")
    with pytest.raises(ValueError, match="server"):
        locked_requirements(locked, lane=True)
    wheel = dist / release["lane_wheels"]["forge-protocol"]["file"]
    wheel.write_bytes(wheel.read_bytes() + b"changed after staging")
    with pytest.raises(ValueError, match="artifact changed"):
        verify(release, dist)
    assert (
        json.loads((output / "local-bundle.json").read_text())["artifacts"] == manifest["artifacts"]
    )
