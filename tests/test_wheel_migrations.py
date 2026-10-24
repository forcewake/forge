"""A released server wheel must bootstrap without a checkout or Docker assets."""

import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(args, *, cwd, env):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.mark.slow
def test_installed_wheel_bootstraps_and_checks_its_own_schema(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env.update(UV_OFFLINE="1", PYTHONPATH="", NO_PROXY="*")
    dist = tmp_path / "dist"
    run(["uv", "build", "--all-packages", "--offline", "--out-dir", str(dist)], cwd=ROOT, env=env)
    wheel = next(dist.glob("forge-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert (
            archive.read("forge/store/schema/baseline.json")
            == (ROOT / "src/forge/store/schema/baseline.json").read_bytes()
        )
        assert not any(name.startswith("forge/_migrations/") for name in archive.namelist())
        assert not any(name.startswith("forge/adaptive/") for name in archive.namelist())
    from tests.runtime_fixture import configuration

    config, values = configuration(tmp_path)
    probe_config = tmp_path / "probe-config.json"
    probe_config.write_text(
        json.dumps({"config": config.model_dump(mode="json", by_alias=True), "environment": values})
    )
    outside = tmp_path / "outside-checkout"
    outside.mkdir()
    venv = tmp_path / "wheel-env"
    run(["uv", "venv", "--python", sys.executable, str(venv)], cwd=outside, env=env)
    python = venv / "bin" / "python"
    run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(python),
            str(wheel) + "[sqlite]",
            *(str(path) for path in dist.glob("*.whl") if path != wheel),
        ],
        cwd=outside,
        env=env,
    )
    result = run(
        [
            str(python),
            "-I",
            str(ROOT / "tests/fixtures/wheel_bootstrap_probe.py"),
            str(probe_config),
        ],
        cwd=outside,
        env=env,
    )
    proof = json.loads(result.stdout.splitlines()[-1])
    assert proof["version"] == "1.0.0" and proof["retired_runtime_files"] == 0
    assert proof["fresh_bootstrap"] and proof["zero_mutation_check"]
    assert proof["asgi_startup_and_health"] and proof["provider_writes"] == 0
