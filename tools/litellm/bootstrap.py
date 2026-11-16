"""The image starts its own immutable callback/configuration; CLI overrides are unavailable."""

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent


def verify_artifact(root: Path = ROOT) -> str:
    raw = (root / "artifact.json").read_bytes()
    body = json.loads(raw)
    expected = {
        "Dockerfile",
        "build_manifest.py",
        "guard_policy.py",
        "guard_ledger.py",
        "forge_budget_guard.py",
        "bootstrap.py",
        "forge-budget-policy.json",
        "config.yaml",
    }
    if body.get("schema") != "forge.gateway-artifact/1" or set(body.get("files", {})) != expected:
        raise ValueError("unsupported gateway artifact")
    for name, digest in body["files"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ValueError("gateway artifact changed after build")
    if version("litellm") != "1.106.2":
        raise ValueError("gateway requires the pinned native version")
    return hashlib.sha256(raw).hexdigest()


def provider_credential_env(environ: dict[str, str] | None = None) -> str:
    """Which environment variable carries the upstream model credential."""
    source = os.environ if environ is None else environ
    name = source.get("FORGE_GATEWAY_PROVIDER_KEY_ENV", "ANTHROPIC_API_KEY")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
        raise ValueError("provider credential environment name is invalid")
    return name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4000)
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        raise ValueError("invalid listening port")
    verify_artifact()
    provider_env = provider_credential_env()
    # No inherited provider/config/Python variables can add a second paid route.
    names = (
        "PATH",
        "HOME",
        "LANG",
        "DATABASE_URL",
        "LITELLM_MASTER_KEY",
        provider_env,
        "PRISMA_BINARY_CACHE_DIR",
        "PRISMA_CLI_PATH",
        "PRISMA_CLI_QUERY_ENGINE_TYPE",
        "PRISMA_OFFLINE_MODE",
    )
    environment = {name: os.environ[name] for name in names if name in os.environ}
    if not all(
        environment.get(name) for name in ("DATABASE_URL", "LITELLM_MASTER_KEY", provider_env)
    ):
        raise ValueError("explicit database, administrator and provider credentials are required")
    environment.update(
        {
            "PYTHONPATH": str(ROOT),
            "LITELLM_TELEMETRY": "false",
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        }
    )
    executable = shutil.which("litellm")
    if executable is None:
        raise ValueError("native gateway executable is missing")
    os.execve(
        executable,
        [
            executable,
            "--config",
            str(ROOT / "config.yaml"),
            "--host",
            args.host,
            "--port",
            str(args.port),
        ],
        environment,
    )


if __name__ == "__main__":
    main()
