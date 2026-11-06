"""Run only the selected release digest against its private fresh sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from tools.release.sandbox_config import SCHEMA, pinned_image, prepare, scope_name, strings


def launch(
    image: str, scope: str, config_path: Path, role: str, *, runtime: str = "docker"
) -> None:
    pinned_image(image)
    scope_name(scope)
    config = json.loads(config_path.read_text())
    if config["schema"] != SCHEMA or config["scope"] != scope or config["image"] != image:
        raise ValueError("release sandbox identity changed")
    if role not in {"install", "app", "worker"} or os.getuid() == 0:
        raise ValueError("release role requires a nonroot sandbox operator")
    environment = strings(config["environment"])
    environment["FORGE_RELEASE_SCOPE"] = scope
    root = config_path.resolve().parent
    if hashlib.sha256((root / "forge.toml").read_bytes()).hexdigest() != config["config_sha256"]:
        raise ValueError("private runtime configuration changed after preparation")
    command = [
        runtime,
        "run",
        "--network",
        scope,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,mode=1777",
    ]
    for key in sorted(environment):
        command.extend(("--env", key))
    command.extend(
        (
            "--mount",
            f"type=bind,src={root},dst=/etc/forge,readonly",
            "--mount",
            f"type=bind,src={root}/blobs,dst=/data/blobs",
        )
    )
    if role == "install":
        command.append("--rm")
    else:
        command.extend(("-d", "--name", f"{scope}-{role}"))
    if role == "app":
        command.extend(("-p", "127.0.0.1:8420:8420"))
    if role == "worker":
        command.append("--no-healthcheck")
    command.extend(
        (image, "forge", "--config", "/etc/forge/forge.toml", "serve" if role == "app" else role)
    )
    if role == "app":
        command.extend(("--host", "0.0.0.0", "--port", "8420"))
    subprocess.run(command, env={**os.environ, **environment}, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("prepare")
    for name in ("bundle", "manifest", "output"):
        init.add_argument("--" + name, type=Path, required=True)
    init.add_argument("--provider", choices=("github", "gitlab"), required=True)
    init.add_argument("--repository", required=True)
    init.add_argument("--scope", required=True)
    run = sub.add_parser("launch")
    run.add_argument("image")
    run.add_argument("scope")
    run.add_argument("config", type=Path)
    run.add_argument("role", choices=("install", "app", "worker"))
    run.add_argument("--runtime", default="docker")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.bundle, args.manifest, args.output, args.provider, args.repository, args.scope)
    else:
        launch(args.image, args.scope, args.config, args.role, runtime=args.runtime)


if __name__ == "__main__":
    main()
