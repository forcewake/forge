"""Mandatory live acceptance of one installed RC on one explicit native sandbox.

This command performs real issue/comment and paid-lane work when the release
owner runs it. Local qualification uses labelled owned synthetic peers only.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path

from sqlalchemy.engine import make_url

from forge import __version__
from forge.runtime.assembly import open_runtime
from forge.runtime.settings import load, secret
from tools.release.live_native import Sandbox
from tools.release.live_pins import baseline
from tools.release.live_receipts import scenario_receipt, wait_run
from tools.release.sandbox_config import pinned_image, scope_name


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError("missing explicit release input " + name)
    return value


def artifact_install(manifest):
    import forge

    if (
        "site-packages" not in str(Path(forge.__file__).resolve())
        or __version__ != manifest["version"]
    ):
        raise ValueError("live smoke must run the installed exact server artifact outside checkout")
    if any(
        importlib.util.find_spec(name) is not None
        for name in ("forge_lane", "claude_agent_sdk", "anthropic")
    ):
        raise ValueError("server artifact contains a lane or vendor SDK")
    pinned_image(manifest["image"])


async def exercise(runtime, sandbox, manifest, artifacts, timeout):
    await sandbox.authorize()
    base = await baseline(runtime, sandbox.connection, sandbox.repository, manifest, artifacts)
    scenarios = []
    for scenario in ("happy", "repair"):
        issue = sandbox.issue(manifest["version"], scenario)
        number = issue["number" if sandbox.connection.provider == "github" else "iid"]
        admitted = await sandbox.command(issue, "/implement")
        run_id = admitted["run_id"]
        await wait_run(sandbox, run_id, "awaiting_approval", timeout)
        approved = await sandbox.command(issue, "/go " + run_id)
        if approved["run_id"] != run_id or approved["reason"] != "approval_queued":
            raise ValueError("native approval did not identify the original frozen gate")
        await wait_run(sandbox, run_id, "ready_for_human", timeout)
        scenarios.append(await scenario_receipt(runtime, sandbox, run_id, number, scenario, base))
        if (
            await sandbox.native.repository.head(
                sandbox.native.ref, sandbox.repository.policy.target_branch
            )
            != base
        ):
            raise ValueError("live acceptance changed the protected target branch")
    repaired = scenarios[1]
    return {
        key: manifest[key] for key in ("version", "image", "commit", "lane_requirements_sha256")
    } | {
        "schema": "forge.live-smoke/2",
        "provider": sandbox.connection.provider,
        "status": "passed",
        "candidate_sha": repaired["candidate_sha"],
        "oracle_jobs": repaired["oracle_jobs"],
        "scenarios": scenarios,
        "ingress": "signed-native-api-comment-through-configured-hook",
        "lane_workflow_commit": manifest["workflow_commit"],
    }


async def execute(args):
    if required("FORGE_RELEASE_SANDBOX") != "1":
        raise ValueError("live acceptance requires its dedicated sandbox acknowledgement")
    scope = scope_name(required("FORGE_RELEASE_SCOPE"))
    manifest = json.loads(args.manifest.read_text())
    artifact_install(manifest)
    config = load(args.config)
    (connection,) = config.connections
    (repository,) = connection.repositories
    if connection.provider != args.provider or repository.key != args.repository:
        raise ValueError("live sandbox contains another configured native target")
    url = make_url(secret(config.database_url_env, os.environ).get_secret_value())
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != scope + "-pg"
        or url.database != "forge"
    ):
        raise ValueError("release evidence must use only this isolated job's fresh database")
    async with open_runtime(config, environ=os.environ) as runtime:
        sandbox = Sandbox(
            runtime, connection, repository, required("FORGE_RELEASE_SMOKE_ACTOR_TOKEN"), os.environ
        )
        try:
            return await exercise(runtime, sandbox, manifest, args.artifacts, args.timeout)
        finally:
            await sandbox.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("github", "gitlab"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--config", type=Path, default=Path("/etc/forge/forge.toml"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=2400)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 5400:
        parser.error("live acceptance timeout must be bounded")
    try:
        value = asyncio.run(execute(args))
    except Exception as error:
        args.out.write_text(
            json.dumps(
                {"provider": args.provider, "status": "failed", "error_type": type(error).__name__}
            )
            + "\n"
        )
        raise SystemExit("live acceptance refused: " + type(error).__name__) from None
    args.out.write_text(json.dumps(value, indent=2) + "\n")


if __name__ == "__main__":
    main()
