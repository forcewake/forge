"""Release bytes stay fixed across RC testing and final promotion."""

from __future__ import annotations

import hashlib
import json
from zipfile import ZipFile

import pytest

from tools.release.artifacts import prepare, report, verify

HASH = "e" * 64


def _wheel(path, name, version, package):
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{package}/__init__.py", "")
        archive.writestr(f"{package}-{version}.dist-info/METADATA", metadata)
        archive.writestr(
            f"{package}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )


@pytest.fixture
def candidate(tmp_path):
    _wheel(tmp_path / "forge-1.0.0-py3-none-any.whl", "forge", "1.0.0", "forge")
    _wheel(
        tmp_path / "forge_protocol-0.1.0-py3-none-any.whl",
        "forge-protocol",
        "0.1.0",
        "forge_protocol",
    )
    _wheel(tmp_path / "forge_lane-0.1.0-py3-none-any.whl", "forge-lane", "0.1.0", "forge_lane")
    (tmp_path / "forge-1.0.0.tar.gz").write_bytes(b"source")
    lock = f"httpx==0.28.1 --hash=sha256:{HASH}\nclaude-agent-sdk==0.2.157 --hash=sha256:{HASH}\n"
    (tmp_path / "lane-v1-dependencies.txt").write_text(lock)
    (tmp_path / "runtime-requirements.txt").write_text(f"httpx==0.28.1 --hash=sha256:{HASH}\n")
    value = prepare(
        tmp_path,
        "acme/forge",
        "v1.0.0",
        "v1.0.0-rc.123.1",
        "a" * 40,
        "ghcr.io/acme/forge@sha256:" + "b" * 64,
    )
    return tmp_path, value


def test_release_requirement_set_binds_every_built_wheel(candidate):
    directory, manifest = candidate
    requirements = (directory / "lane-v1-requirements.txt").read_text()
    assert requirements.count("/releases/download/v1.0.0-rc.123.1/") == 2
    assert "forge-protocol @" in requirements and "forge-lane[claude] @" in requirements
    verify(manifest, directory)
    (directory / "forge_protocol-0.1.0-py3-none-any.whl").write_bytes(b"rebuilt")
    with pytest.raises(ValueError, match="artifact changed"):
        verify(manifest, directory)


def _scenario(name, attempt, final):
    first = "d" * 40
    candidates = (
        [{"sha": final, "base_sha": "b" * 40}]
        if attempt == 1
        else [
            {"sha": first, "base_sha": "b" * 40},
            {"sha": final, "base_sha": first},
        ]
    )
    value = {
        "scenario": name,
        "attempt": attempt,
        "generation": attempt,
        "candidates": candidates,
        "oracle_jobs": [123],
        "lane_dispatches": [1],
        "draft": {"number": 8},
        "candidate_sha": final,
        "implementation_return": 42,
    }
    if name == "repair":
        value["initial_failure"] = {
            "candidate_sha": first,
            "implementation_return": 41,
            "native_jobs": [
                {
                    "status": "failed",
                    "failure_kind": "code",
                    "native_id": "job-1",
                    "native_run_id": "run-1",
                    "workflow_path": ".github/workflows/ci.yml",
                }
            ],
        }
    return value


def release_proofs(tmp_path, manifest):
    junit = tmp_path / "invariants.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase name="real-invariant"/></testsuite></testsuites>'
    )
    canary = {
        "image": manifest["image"],
        "stages": [{"stage": "fresh", "outcome": "pass"}],
    }
    final = "c" * 40
    smokes = [
        {
            "provider": provider,
            "version": manifest["version"],
            "image": manifest["image"],
            "commit": manifest["commit"],
            "wheel_sha256": manifest["wheel_sha256"],
            "requirements_sha256": manifest["requirements_sha256"],
            "lane_requirements_sha256": manifest["lane_requirements_sha256"],
            "status": "passed",
            "candidate_sha": final,
            "oracle_jobs": [123],
            "scenarios": [_scenario("happy", 1, "a" * 40), _scenario("repair", 2, final)],
        }
        for provider in ("github", "gitlab")
    ]
    return canary, smokes, junit


def test_report_accepts_only_complete_bound_proofs(candidate):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    value = report(manifest, canary, smokes, junit)
    assert value["invariants"] == {"passed": 1, "skipped": 0}
    assert value["artifacts"] == manifest["artifacts"]


@pytest.mark.parametrize(
    "failure",
    [
        "wrong-image",
        "missing-provider",
        "wrong-wheel",
        "no-oracle",
        "failed-boot",
        "skipped-invariant",
    ],
)
def test_report_refuses_missing_or_foreign_proofs(candidate, failure):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    if failure == "wrong-image":
        canary["image"] += "wrong"
    elif failure == "missing-provider":
        smokes.pop()
    elif failure == "wrong-wheel":
        smokes[1]["lane_requirements_sha256"] = "d" * 64
    elif failure == "no-oracle":
        smokes[0]["oracle_jobs"] = []
    elif failure == "failed-boot":
        canary["stages"].append({"stage": "migrate", "outcome": "fail"})
    else:
        junit.write_text(
            "<testsuites><testsuite><testcase><skipped/></testcase></testsuite></testsuites>"
        )
    with pytest.raises(ValueError):
        report(manifest, canary, smokes, junit)


def test_release_container_keeps_multiline_secrets_out_of_arguments(tmp_path, monkeypatch):
    from tools.release.containers import launch
    from tools.release.sandbox_config import SCHEMA

    image = "ghcr.io/acme/forge@sha256:" + "f" * 64
    scope = "forge-release-owned"
    pem = "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n"
    (tmp_path / "forge.toml").write_text('schema = "forge.runtime/1"\n')
    (tmp_path / "blobs").mkdir()
    config = {
        "schema": SCHEMA,
        "scope": scope,
        "image": image,
        "environment": {"FORGE_GITHUB_PRIVATE_KEY": pem},
        "config_sha256": hashlib.sha256((tmp_path / "forge.toml").read_bytes()).hexdigest(),
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    calls = []
    monkeypatch.setattr(
        "tools.release.containers.subprocess.run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    launch(image, scope, path, "app")
    args, options = calls[0]
    rendered = " ".join(args)
    assert pem not in rendered and "secret" not in rendered
    assert options["env"]["FORGE_GITHUB_PRIVATE_KEY"] == pem
    assert args[args.index("--name") + 1] == scope + "-app"


def test_release_refuses_editable_source_dependencies(candidate):
    directory, manifest = candidate
    (directory / "lane-v1-dependencies.txt").write_text("-e .\n")
    with pytest.raises(ValueError, match="source checkouts"):
        prepare(
            directory,
            "acme/forge",
            "v1.0.0",
            manifest["rc_tag"],
            manifest["commit"],
            manifest["image"],
        )


def test_v1_requirements_contain_only_lane_and_protocol_wheels(candidate):
    directory, manifest = candidate
    requirements = (directory / "lane-v1-requirements.txt").read_text()
    assert "forge_lane-0.1.0-" in requirements
    assert "forge_protocol-0.1.0-" in requirements
    assert "forge-1.0.0-" not in requirements
    assert requirements.count("/releases/download/") == 2
    assert manifest["lane_requirements_sha256"]
    verify(manifest, directory)
