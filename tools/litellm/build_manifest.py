"""Bind the packaged guard, policy, bootstrap and native configuration at build time."""

import hashlib
import json
from pathlib import Path
import argparse

FILES = (
    "Dockerfile",
    "build_manifest.py",
    "guard_policy.py",
    "guard_ledger.py",
    "forge_budget_guard.py",
    "bootstrap.py",
    "forge-budget-policy.json",
    "config.yaml",
)


def manifest(root: Path, *, policy: Path | None = None, config: Path | None = None) -> bytes:
    paths = {name: root / name for name in FILES}
    if policy is not None:
        paths["forge-budget-policy.json"] = policy
    if config is not None:
        paths["config.yaml"] = config
    return json.dumps(
        {
            "schema": "forge.gateway-artifact/1",
            "files": {
                name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parent
    output = arguments.output or root / "artifact.json"
    output.write_bytes(manifest(root, policy=arguments.policy, config=arguments.config))
