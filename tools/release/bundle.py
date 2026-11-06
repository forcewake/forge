"""Build and prove a local candidate without publishing or opening a model session."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from tools.release.artifacts import (
    artifact_files,
    lane_requirements,
    locked_requirements,
    release_base,
    sha256,
    stage_assets,
    wheel_identity,
    workflow_pins,
    write_once,
)


def command(arguments: list[str], root: Path, *, quiet: bool = False) -> str:
    result = subprocess.run(
        arguments, cwd=root, check=True, text=True, stdout=subprocess.PIPE if quiet else None
    )
    return result.stdout or ""


def source_identity(root: Path) -> dict:
    commit = command(["git", "rev-parse", "HEAD"], root, quiet=True).strip()
    changed = command(["git", "status", "--porcelain"], root, quiet=True).strip()
    files = [root / "pyproject.toml", root / "uv.lock"]
    for directory in ("src/forge", "packages/forge-lane/src", "packages/forge-protocol/src"):
        files.extend(
            p for p in (root / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts
        )
    files.extend(
        root / name
        for name in (
            "packages/forge-lane/pyproject.toml",
            "packages/forge-protocol/pyproject.toml",
            ".github/workflows/lane.yml",
            "templates/github/forge-lane.yml",
            "ci/lane.gitlab-ci.yml",
            "tools/release/artifacts.py",
            "tools/release/bundle.py",
            "tools/ci/github_junit.py",
        )
    )
    identity = {str(p.relative_to(root)): sha256(p) for p in sorted(set(files))}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return {
        "commit": commit,
        "source_dirty": bool(changed),
        "source_tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "source_files": identity,
    }


def export_lock(
    root: Path, package: str, destination: Path, extras: tuple[str, ...], *, offline: bool
) -> None:
    arguments = [
        "uv",
        "export",
        *(["--offline"] if offline else []),
        "--frozen",
        "--package",
        package,
        "--no-dev",
        "--no-emit-workspace",
        "--no-header",
        "--no-annotate",
        "--output-file",
        str(destination),
    ]
    for extra in extras:
        arguments.extend(["--extra", extra])
    command(arguments, root, quiet=True)


def local_requirements(dist: Path, target: Path, extras: tuple[str, ...]) -> str:
    locked, _ = locked_requirements(dist / "lane-v1-dependencies.txt", lane=True)
    own = []
    for path in sorted(dist.glob("*.whl")):
        identity = wheel_identity(path)
        if identity["name"] not in {"forge-lane", "forge-protocol"}:
            continue
        name = identity["name"] + (
            "[claude]" if identity["name"] == "forge-lane" and extras else ""
        )
        own.append(f"{name} @ {path.resolve().as_uri()} --hash=sha256:{identity['sha256']}")
    write_once(target, locked + "\n" + "\n".join(own) + "\n")
    return sha256(target)


def isolated_install(
    root: Path, output: Path, requirements: Path, extras: tuple[str, ...], *, offline: bool
) -> dict:
    environment = output / "lane-environment"
    command(
        [
            "uv",
            "venv",
            *(["--offline"] if offline else []),
            "--no-python-downloads",
            "--python",
            sys.executable,
            str(environment),
        ],
        root,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command(
        [
            "uv",
            "pip",
            "install",
            *(["--offline"] if offline else []),
            "--python",
            str(python),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--requirement",
            str(requirements),
        ],
        root,
    )
    probe = """import importlib.metadata as m, importlib.util, json
from pathlib import Path
import forge_lane, forge_protocol
assert importlib.util.find_spec("forge") is None, "server installed in lane"
for name in ("fastapi", "sqlalchemy", "alembic", "asyncpg", "aiosqlite"):
    assert importlib.util.find_spec(name) is None, "server-only dependency: " + name
assert "site-packages" in forge_lane.__file__ and "site-packages" in forge_protocol.__file__
proof = {"status":"passed", "server_absent":True, "installed":{d.metadata["Name"]:d.version for d in m.distributions()}}
"""
    if extras:
        probe += """import claude_agent_sdk
binary = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
assert binary.is_file(), "SDK bundled CLI is missing"
proof["sdk_bundled_cli_sha256"] = __import__("hashlib").sha256(binary.read_bytes()).hexdigest()
"""
    probe += "print(json.dumps(proof, sort_keys=True))\n"
    raw = command([str(python), "-I", "-c", probe], output, quiet=True)
    proof = json.loads(raw)
    proof.update(
        requirements_sha256=sha256(requirements),
        model_sessions=0,
        provider_requests=0,
        python=str(python),
    )
    return proof


def build(
    root: Path,
    output: Path,
    repo: str,
    rc: str,
    extras: tuple[str, ...],
    *,
    offline: bool = False,
) -> dict:
    before = source_identity(root)
    base = release_base(repo, rc, before["commit"])
    output.mkdir(parents=True, exist_ok=False)
    dist = output / "dist"
    command(
        [
            "uv",
            "build",
            *(["--offline"] if offline else []),
            "--python",
            sys.executable,
            "--no-python-downloads",
            "--all-packages",
            "--sdist",
            "--wheel",
            "--out-dir",
            str(dist),
        ],
        root,
    )
    export_lock(root, "forge", dist / "runtime-requirements.txt", ("postgres",), offline=offline)
    export_lock(root, "forge-lane", dist / "lane-v1-dependencies.txt", extras, offline=offline)
    lane = lane_requirements(dist, base, extras)
    assets = stage_assets(dist, repo, before["commit"])
    local = output / "lane-local-requirements.txt"
    local_requirements(dist, local, extras)
    proof = isolated_install(root, output, local, extras, offline=offline)
    after = source_identity(root)
    if (
        before["source_tree_sha256"] != after["source_tree_sha256"]
        or before["commit"] != after["commit"]
    ):
        raise ValueError(
            "source changed during build; discard this unsealed output and build again"
        )
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    manifest = {
        "schema": "forge.local-release-bundle/1",
        "status": "local-install-verified",
        "publication": "not-published",
        "image": None,
        "version": version,
        "rc_tag": rc,
        **before,
        **workflow_pins(repo, before["commit"]),
        "lane_extras": list(extras),
        "lane_wheels": lane,
        "lane_requirements_url": base + "/lane-v1-requirements.txt",
        "lane_requirements_sha256": sha256(dist / "lane-v1-requirements.txt"),
        "artifacts": {p.name: sha256(p) for p in artifact_files(dist)},
        "assets": {
            key: {"url": base + "/" + value, "sha256": sha256(dist / value)}
            for key, value in assets.items()
        },
        "install_proof": proof,
        "external_acceptance": {
            "published_source_commit": "pending",
            "published_assets": "pending",
            "image_digest": "pending",
            "native_provider_and_model_runs": "pending",
        },
    }
    write_once(output / "local-bundle.json", json.dumps(manifest, indent=2) + "\n")
    write_once(
        output / "SHA256SUMS",
        "\n".join(value + "  dist/" + name for name, value in manifest["artifacts"].items()) + "\n",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--rc", required=True)
    parser.add_argument("--lane-extras", choices=("", "claude"), default="claude")
    parser.add_argument(
        "--offline", action="store_true", help="require already cached dependencies"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    manifest = build(
        root,
        args.output.resolve(),
        args.repo,
        args.rc,
        tuple(filter(None, args.lane_extras.split(","))),
        offline=args.offline,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "status": manifest["status"],
                "source_dirty": manifest["source_dirty"],
                "publication": "not-published",
            }
        )
    )


if __name__ == "__main__":
    main()
