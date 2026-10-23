"""Start only the release sandbox's containers, with JSON-safe credentials."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def launch(image: str, scope: str, config_path: Path, role: str) -> None:
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in config.items()
    ):
        raise ValueError("release sandbox configuration must be a JSON object of strings")
    config.update(
        DATABASE_URL="postgresql+asyncpg://forge:forge@postgres/forge",
        REDIS_URL="redis://redis:6379/0",
    )
    command = ["docker", "run", "--network", scope]
    # Docker reads values from the subprocess environment, including PEM
    # newlines. Tokens never enter the command arguments or output.
    for key in sorted(config):
        command.extend(["--env", key])
    if role == "migrate":
        command.append("--rm")
    else:
        command.extend(["-d", "--name", f"{scope}-{role}"])
    if role == "app":
        command.extend(["-p", "127.0.0.1:8420:8420"])
    command.append(image)
    if role == "migrate":
        command.extend(["python", "-m", "forge.migrate"])
    elif role == "worker":
        command.extend(["python", "-m", "forge.worker"])
    subprocess.run(command, env={**os.environ, **config}, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("scope")
    parser.add_argument("config", type=Path)
    parser.add_argument("role", choices=("app", "worker", "migrate"))
    args = parser.parse_args()
    launch(args.image, args.scope, args.config, args.role)
