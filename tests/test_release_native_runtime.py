"""Current release gates on owned processes/native HTTP; no live-provider acceptance."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
from types import SimpleNamespace as NS

import httpx
import pytest
from sqlalchemy import select

from forge.runtime.assembly import open_runtime
from forge.runtime.settings import RuntimeSettings
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineEffect
from forge.store.models_runs import EngineRun
from forge.verification.checks import verdict
from tests.ci_failure_fixture import NativeCI
from tests.fakes.ports import FixedClock
from tests.native_clients_fixture import (
    app_key as app_key,
    blob_case as blob_case,
    github as github,
    gitlab as gitlab,
)
from tests.runtime_fixture import certificate, configuration
from tests.test_budget_engine import NativeAdmin
from tools.release import live_oracle
from tools.release.containers import launch
from tools.release.live_native import Sandbox
from tools.release.live_pins import baseline, IMPLEMENTATION, ORACLE, ORACLE_PATH
from tools.release.live_receipts import verification, wait_run
from tools.release.sandbox_config import prepare

IMAGE = "ghcr.io/owned/forge@sha256:" + "a" * 64
SCOPE = "forge-release-owned-proof"
ROOT = Path(__file__).resolve().parents[1]


def private_bundle(tmp_path, *, database_env="OWNED_DB"):
    raw = f'''schema = "forge.runtime/1"
database_url_env = "{database_env}"
secret_key_env = "SIGNING"
blob_directory = "/data/blobs"
lane_requirements_url = "https://before.invalid/lane.txt"
lane_requirements_sha256 = "{"0" * 64}"
[gateway]
url = "https://guard.invalid"
admin_key_env = "ADMIN"
policy_path = "/etc/forge/policy.json"
[[operators]]
token_env = "OPERATOR"
[[connections]]
provider = "gitlab"
webhook_secret_env = "HOOK"
tls_ca_path = "/etc/forge/native.pem"
[[connections.repositories]]
key = "sandbox"
read_token_env = "READ"
write_token_env = "WRITE"
verification_failure_jobs = ["release-smoke-oracle"]
[connections.repositories.policy.write_policy]
allowed_globs = ["forge_release_smoke.py"]
[connections.repositories.policy.verification]
required_checks = ["release-smoke-oracle"]
'''
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(
            dict(
                schema="forge.release-sandbox/1",
                runtime_toml=raw,
                policy_json="{}",
                certificates={"native.pem": "synthetic PEM"},
                environment={
                    key: "synthetic-" + key
                    for key in ("SIGNING", "ADMIN", "HOOK", "OPERATOR", "READ", "WRITE")
                },
            )
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            dict(
                image=IMAGE,
                lane_requirements_url="https://release.invalid/lane.txt",
                lane_requirements_sha256="1" * 64,
            )
        )
    )
    return bundle, manifest, tmp_path / "private"


def test_private_bundle_pins_owned_database_and_lane_route_without_secret_arguments(
    tmp_path, monkeypatch
):
    bundle, manifest, private = private_bundle(tmp_path)
    control = prepare(bundle, manifest, private, "gitlab", "sandbox", SCOPE)
    document = json.loads(control.read_text())
    assert (
        document["environment"]["OWNED_DB"]
        == f"postgresql+asyncpg://forge:forge@{SCOPE}-pg:5432/forge"
    )
    assert (private / "forge.toml").read_text().count("https://release.invalid/lane.txt") == 1
    assert private.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in private.iterdir() if path.is_file())
    calls = []
    monkeypatch.setattr(
        "tools.release.containers.subprocess.run", lambda command, **kw: calls.append((command, kw))
    )
    monkeypatch.setattr("tools.release.containers.os.getuid", lambda: 501)
    launch(IMAGE, SCOPE, control, "install")
    command, options = calls[0]
    assert command[-4:] == ["forge", "--config", "/etc/forge/forge.toml", "install"]
    assert "--rm" in command and "--read-only" in command
    assert all("synthetic-" not in value for value in command)
    assert options["env"]["READ"] == "synthetic-READ"
    (private / "forge.toml").write_text("changed")
    with pytest.raises(ValueError, match="changed after preparation"):
        launch(IMAGE, SCOPE, control, "app")
    assert len(calls) == 1


@pytest.mark.parametrize("database_env", ["PATH", "SIGNING", "FORGE_RELEASE_SCOPE"])
def test_private_bundle_cannot_replace_process_or_signing_authority(tmp_path, database_env):
    bundle, manifest, private = private_bundle(tmp_path, database_env=database_env)
    with pytest.raises(ValueError, match="database environment"):
        prepare(bundle, manifest, private, "gitlab", "sandbox", SCOPE)
    assert not private.exists()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def process_document(tmp_path, provider, database):
    key, cert = certificate(tmp_path)
    native_url = f"https://127.0.0.1:{free_port()}"
    config, values = configuration(tmp_path, native_url=native_url, ca=cert)
    values["SYNTHETIC_DATABASE_URL"] = database
    document = config.model_dump(mode="json", by_alias=True)
    document["public_url"] = f"https://127.0.0.1:{free_port()}"
    if provider == "github":
        connection = document["connections"][0]
        connection.update(
            provider="github", app_issuer="owned-app", app_private_key_env="SYNTHETIC_APP_KEY"
        )
        connection["repositories"][0].update(
            bot_login="forge[bot]", installation_id=900, read_token_env=None, write_token_env=None
        )
        values["SYNTHETIC_APP_KEY"] = key.read_text()
    config = RuntimeSettings.model_validate_json(json.dumps(document))
    path = tmp_path / "process.json"
    path.write_text(
        json.dumps(
            dict(
                config=document,
                environment=values,
                cert=str(cert),
                key=str(key),
                receipt=str(tmp_path / "process-receipt.json"),
            )
        )
    )
    path.chmod(0o600)
    return config, values, path, cert


async def wait_application(process, tmp_path, config, cert):
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=cert), trust_env=False
    ) as http:
        for _ in range(200):
            try:
                response = await http.get(config.public_url + "/healthz", timeout=0.2)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                pytest.fail((tmp_path / "app.log").read_text())
            await asyncio.sleep(0.05)
    pytest.fail("owned application process did not start")


@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_real_process_tls_native_comment_hook_and_cold_schema_command_receipt(
    tmp_path, provider
):
    database = "sqlite+aiosqlite:///" + str(tmp_path / "runtime.db")
    config, values, document, cert = process_document(tmp_path, provider, database)
    subprocess.run(
        [sys.executable, "-m", "forge.store.schema", "init", "--database-url", database],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=True,
        capture_output=True,
        timeout=20,
    )
    with (tmp_path / "app.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.release_runtime_peer", str(document)],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        try:
            await wait_application(process, tmp_path, config, cert)
            clock = FixedClock(datetime.now(timezone.utc))
            gateway = NativeAdmin(clock)
            async with open_runtime(
                config, environ=values, gateway_transport=httpx.MockTransport(gateway), clock=clock
            ) as runtime:
                connection, repository = (
                    config.connections[0],
                    config.connections[0].repositories[0],
                )
                sandbox = Sandbox(runtime, connection, repository, "synthetic-actor", values)
                try:
                    await sandbox.authorize()
                    issue = sandbox.issue("1.0.0", "happy")
                    result = await sandbox.command(issue, "/implement")
                    assert result["reason"] == "implementation_queued"
                    receipt = await wait_run(sandbox, result["run_id"], "queued", 5)
                    assert receipt["subject_id"] == "7"
                    async with runtime.factory() as session:
                        runs = list(await session.scalars(select(EngineRun)))
                        commands = list(await session.scalars(select(EngineCommand)))
                        effects = list(await session.scalars(select(EngineEffect)))
                    assert len(runs) == len(commands) == len(effects) == 1
                    assert (
                        commands[0].state == "completed"
                        and json.loads(commands[0].document_json)["source"] == "webhook"
                    )
                    assert effects[0].kind == "post_comment" and effects[0].state == "committed"
                    assert effects[0].command_id == commands[0].id
                    notes = sandbox.call(
                        "GET",
                        sandbox.root
                        + "/issues/7/"
                        + ("comments" if provider == "github" else "notes"),
                    )
                    assert len(notes) == 2
                    (tmp_path / "process-receipt.json").write_text(
                        json.dumps(
                            dict(
                                scope="owned-synthetic-native-and-guard",
                                provider=provider,
                                run_id=result["run_id"],
                                state=receipt["state"],
                                notes=len(notes),
                                runs=len(runs),
                                commands=len(commands),
                                effects=len(effects),
                            )
                        )
                    )
                finally:
                    await sandbox.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
    proof = json.loads((tmp_path / "process-receipt.json").read_text())
    assert proof["scope"] == "owned-synthetic-native-and-guard"
    assert proof["runs"] == 1 and proof["notes"] == 2 and proof["state"] == "queued"


async def test_release_receipt_uses_concrete_native_code_proof_and_refuses_corruption(
    blob_case, app_key
):
    clock = FixedClock(datetime.now(timezone.utc))
    native = NativeCI(blob_case.repo, "a" * 40, clock, app_key)
    native.job["name"] = native.check["name"] = "release-smoke-oracle"
    native.ci.failure_jobs = frozenset({"release-smoke-oracle"})
    try:
        checks = await native.ci.checks_for(blob_case.repo, "a" * 40)
        saved = verdict(
            checks,
            "a" * 40,
            ("release-smoke-oracle",),
            now=clock.now(),
            max_age_seconds=3600,
            missing_is_final=True,
        )
        row = NS(
            published_sha="a" * 40,
            validation_json=json.dumps(dict(verification=saved.model_dump(mode="json"))),
        )
        runtime = NS(budget=NS(clock=clock))
        sandbox = NS(native=NS(ci=native.ci, ref=blob_case.repo))
        frozen = NS(spec=NS(verification=NS(required_checks=("release-smoke-oracle",))))
        result = await verification(runtime, sandbox, row, frozen, failed=True)
        assert result[0]["native_id"] == "901" and result[0]["failure_kind"] == "code"
        if blob_case.repo.identity.provider == "github":
            native.check["output"] = {"text": "unproven failed status"}
        else:
            native.summary["test_suites"][0]["build_ids"] = [999]
        with pytest.raises(ValueError, match="code-defect"):
            await verification(runtime, sandbox, row, frozen, failed=True)
    finally:
        await native.close()


def baseline_files(tmp_path):
    manifest = dict(
        commit="a" * 40,
        workflow_commit="a" * 40,
        workflow_repository="owned/forge",
        github_workflow_ref="owned/forge/.github/workflows/lane.yml@" + "a" * 40,
        lane_requirements_url="https://release.invalid/lane.txt",
        lane_requirements_sha256="1" * 64,
        artifacts={},
    )
    assets = {
        "github_wrapper": b"uses: " + manifest["github_workflow_ref"].encode(),
        "gitlab_include": (
            "include:\n  - remote: "
            + json.dumps(
                "https://raw.githubusercontent.com/owned/forge/"
                + "a" * 40
                + "/ci/lane.gitlab-ci.yml"
            )
            + "\n"
        ).encode(),
        "verification_reporter": b"exact reporter",
    }
    manifest["gitlab_template_url"] = (
        "https://raw.githubusercontent.com/owned/forge/" + "a" * 40 + "/ci/lane.gitlab-ci.yml"
    )
    for field, body in assets.items():
        name = field + ".txt"
        (tmp_path / name).write_bytes(body)
        manifest[field + "_url"] = "https://release.invalid/" + name
        manifest[field + "_sha256"] = manifest["artifacts"][name] = hashlib.sha256(body).hexdigest()
    files = {
        ORACLE_PATH: ORACLE,
        IMPLEMENTATION: None,
        "lane.yml": assets["github_wrapper"],
        "forge-lane.gitlab-include.yml": assets["gitlab_include"],
        live_oracle.GITHUB_PATH: live_oracle.github(manifest),
        ".gitlab-ci.yml": json.dumps(live_oracle.gitlab()).encode(),
    }
    return manifest, files


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("corruption", [None, "oracle", "workflow", "reporter"])
async def test_baseline_requires_exact_independent_oracle_and_immutable_lane_assets(
    tmp_path, provider, corruption
):
    manifest, files = baseline_files(tmp_path)
    if corruption == "oracle":
        files[ORACLE_PATH] = b"assert True"
    if corruption == "workflow":
        path = live_oracle.GITHUB_PATH if provider == "github" else ".gitlab-ci.yml"
        files[path] = b"{}"
    if corruption == "reporter":
        if provider == "github":
            (tmp_path / "verification_reporter.txt").write_bytes(b"changed")
        else:
            files["forge-lane.gitlab-include.yml"] = b"changed include"

    class Repo:
        async def head(self, *args):
            return "a" * 40

        async def read_blob(self, ref, sha, path):
            return files[path]

    repository = NS(
        key="sandbox",
        lane_workflow="lane.yml",
        ci_configuration=".gitlab-ci.yml",
        verification_workflows=(live_oracle.GITHUB_PATH,),
        verification_failure_jobs=(live_oracle.JOB,),
        policy=NS(
            target_branch="main",
            write_policy=NS(allowed_globs=(IMPLEMENTATION,)),
            verification=NS(required_checks=(live_oracle.JOB,)),
        ),
    )
    runtime = NS(
        config=NS(
            lane_requirements_url=manifest["lane_requirements_url"],
            lane_requirements_sha256="1" * 64,
        ),
        connections=NS(registry=NS(named=lambda key: NS(repository=Repo(), ref=None))),
    )
    if corruption:
        with pytest.raises(ValueError):
            await baseline(runtime, NS(provider=provider), repository, manifest, tmp_path)
    else:
        assert (
            await baseline(runtime, NS(provider=provider), repository, manifest, tmp_path)
            == "a" * 40
        )


def test_release_workflow_keeps_both_provider_acceptance_mandatory():
    import yaml

    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    jobs = workflow["jobs"]
    assert jobs["live-smoke"]["strategy"]["matrix"]["provider"] == ["github", "gitlab"]
    assert "if" not in jobs["live-smoke"] and jobs["promote"]["needs"] == [
        "candidate",
        "live-smoke",
    ]
    assert "continue-on-error" not in jobs["live-smoke"]
    assert any(
        "quality" in step.get("name", "").lower() or "pytest" in step.get("run", "")
        for step in jobs["candidate"]["steps"]
    )
