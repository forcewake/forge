"""Release bytes stay fixed across RC testing and final promotion."""

from __future__ import annotations

import copy
import json

import pytest

from tools.release.artifacts import prepare, report, verify
from tools.release.live_smoke import Sandbox, verify_install


@pytest.fixture
def candidate(tmp_path):
    (tmp_path / "forge-1.0.0-py3-none-any.whl").write_bytes(b"server")
    (tmp_path / "forge_protocol-0.1.0-py3-none-any.whl").write_bytes(b"protocol")
    (tmp_path / "forge_lane-0.1.0-py3-none-any.whl").write_bytes(b"lane")
    (tmp_path / "forge-1.0.0.tar.gz").write_bytes(b"source")
    (tmp_path / "lane-dependencies.txt").write_text("httpx==0.28.1 --hash=sha256:" + "e" * 64)
    (tmp_path / "lane-v1-dependencies.txt").write_text("httpx==0.28.1 --hash=sha256:" + "e" * 64)
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
    requirements = (directory / "lane-requirements.txt").read_text()
    assert requirements.count("/releases/download/v1.0.0-rc.123.1/") == 3
    verify(manifest, directory)
    (directory / "forge_protocol-0.1.0-py3-none-any.whl").write_bytes(b"rebuilt")
    with pytest.raises(ValueError, match="artifact changed"):
        verify(manifest, directory)


def release_proofs(tmp_path, manifest):
    junit = tmp_path / "invariants.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase name="real-invariant"/></testsuite></testsuites>'
    )
    canary = {
        "image": manifest["image"],
        "stages": [{"stage": "fresh", "outcome": "pass"}, {"stage": "migrate", "outcome": "skip"}],
    }
    smokes = [
        {
            "provider": p,
            "version": manifest["version"],
            "image": manifest["image"],
            "wheel_sha256": manifest["wheel_sha256"],
            "requirements_sha256": manifest["requirements_sha256"],
            "status": "passed",
            "candidate_sha": "c" * 40,
            "oracle_jobs": [123],
        }
        for p in ("github", "gitlab")
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
        smokes[1]["wheel_sha256"] = "d" * 64
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


@pytest.mark.parametrize(
    "field,value",
    [
        ("pin", "git"),
        ("route", "default-wheel"),
        ("version", "0.42.0"),
        ("qualified", False),
        ("actual_sha256", "d" * 64),
        ("expected_sha256", "d" * 64),
    ],
)
def test_native_lane_install_requires_exact_rc_bytes(field, value):
    identity = {
        "pin": "wheel",
        "route": "explicit-wheel",
        "version": "1.0.0",
        "qualified": True,
        "actual_sha256": "e" * 64,
        "expected_sha256": "e" * 64,
    }
    verify_install(identity, "1.0.0", "e" * 64)
    identity[field] = value
    with pytest.raises(ValueError, match="exact RC"):
        verify_install(identity, "1.0.0", "e" * 64)


def test_existing_gitlab_variable_metadata_is_preserved(monkeypatch):
    import httpx

    calls = []
    old = {
        "key": "FORGE_LANE_WHEEL",
        "value": "old-wheel",
        "raw": False,
        "protected": True,
        "masked": False,
    }

    def transport(request):
        calls.append((request.method, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json=copy.deepcopy(old))

    sandbox = Sandbox.__new__(Sandbox)
    sandbox.provider = "gitlab"
    sandbox.root = "projects/42"
    sandbox.client = httpx.Client(
        base_url="https://gitlab.example/api/v4/", transport=httpx.MockTransport(transport)
    )
    assert sandbox.variable("FORGE_LANE_WHEEL", "new-wheel") == old
    assert calls[-1] == ("PUT", {"value": "new-wheel"})
    sandbox.client.close()


def test_release_container_keeps_multiline_secrets_out_of_arguments(tmp_path, monkeypatch):
    from tools.release.containers import launch

    config = tmp_path / "config.json"
    pem = "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n"
    config.write_text(json.dumps({"FORGE_GITHUB_PRIVATE_KEY": pem, "DATABASE_URL": "existing-lab"}))
    calls = []
    monkeypatch.setattr(
        "tools.release.containers.subprocess.run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    launch("ghcr.io/acme/forge@sha256:" + "f" * 64, "forge-release-owned", config, "app")
    args, options = calls[0]
    assert pem not in args
    assert options["env"]["FORGE_GITHUB_PRIVATE_KEY"] == pem
    assert options["env"]["DATABASE_URL"] == "postgresql+asyncpg://forge:forge@postgres/forge"
    assert args[args.index("--name") + 1] == "forge-release-owned-app"


def test_artifact_redirect_does_not_forward_gitlab_credentials(monkeypatch):
    import httpx

    sandbox = Sandbox.__new__(Sandbox)
    sandbox.client = httpx.Client(
        base_url="https://gitlab.example/",
        headers={"PRIVATE-TOKEN": "private-sentinel"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "https://storage.example/artifact"}
            )
        ),
    )
    received = []

    def download(url, **kwargs):
        received.append(kwargs)
        return httpx.Response(200, request=httpx.Request("GET", url), content=b"zip-bytes")

    monkeypatch.setattr(httpx, "get", download)
    assert sandbox.download("projects/42/jobs/1/artifacts").content == b"zip-bytes"
    assert "headers" not in received[0]
    sandbox.client.close()


def test_native_lane_requires_the_complete_rc_requirement_set():
    identity = {
        "pin": "wheel",
        "route": "explicit-wheel",
        "version": "1.0.0",
        "qualified": True,
        "expected_sha256": "e" * 64,
        "actual_sha256": "e" * 64,
        "requirements_sha256": "f" * 64,
    }
    verify_install(identity, "1.0.0", "e" * 64, "f" * 64)
    with pytest.raises(ValueError, match="exact RC"):
        verify_install(identity, "1.0.0", "e" * 64, "d" * 64)


def test_release_refuses_editable_source_dependencies(candidate):
    directory, manifest = candidate
    (directory / "lane-dependencies.txt").write_text("-e .\n")
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
