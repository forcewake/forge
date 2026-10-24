"""Fresh installed-image canary with owned PG and explicitly synthetic native TLS peers.

Offline backup/cutover and live release acceptance remain separate gates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from canary_probes import CERTIFICATE, PROBE


class Canary:
    def __init__(self, args):
        self.args = args
        self.name = "forge-canary-" + uuid4().hex[:12]
        self.containers = []

    def run(self, *command, input=None, timeout=120):
        result = subprocess.run(
            [self.args.runtime, *command],
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(
                f"canary exit {result.returncode}: {result.stdout[-2000:]} {result.stderr[-2000:]}"
            )
        return result.stdout

    def image(self):
        try:
            image = json.loads(self.run("image", "inspect", self.args.image))[0]
        except RuntimeError:
            self.run("pull", self.args.image, timeout=600)
            image = json.loads(self.run("image", "inspect", self.args.image))[0]
        if re.fullmatch(r".+@sha256:[0-9a-f]{64}", self.args.image):
            assert self.args.image in image.get("RepoDigests", []), "image digest differs"
        return image["Id"]

    def execute(self, image, *command, network="none", volumes=(), env=None, input=None):
        name = self.name + "-" + uuid4().hex[:6]
        self.containers.append(name)
        args = ["run", "--rm", "-i", "--name", name, "--network", network, "--workdir", "/tmp"]
        for volume in volumes:
            args.extend(("-v", volume))
        for key, value in (env or {}).items():
            args.extend(("-e", key + "=" + value))
        return self.run(*args, image, *command, input=input, timeout=180)

    def postgres(self):
        self.run("network", "create", "--internal", self.name)
        name = self.name + "-pg"
        self.containers.append(name)
        self.run(
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            self.name,
            "-e",
            "POSTGRES_USER=forge",
            "-e",
            "POSTGRES_PASSWORD=synthetic-only",
            "-e",
            "POSTGRES_DB=forge",
            self.args.postgres_image,
        )
        for _ in range(60):
            try:
                self.run("exec", name, "pg_isready", "-U", "forge", "-d", "forge", timeout=5)
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("owned PostgreSQL readiness timeout")
        self.run(
            "exec",
            name,
            "psql",
            "-U",
            "forge",
            "-d",
            "forge",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE DATABASE canary_legacy",
        )
        return name

    def fresh(self, image):
        volume = self.name + "-fixture"
        self.run("volume", "create", volume)
        cert = json.loads(
            self.execute(
                image, "python", "-I", "-c", CERTIFICATE, volumes=(volume + ":/data:rw,z",)
            )
        )
        assert re.fullmatch(r"/opt/forge/lib/python3\.13/site-packages/certifi", cert["trust"])
        pg = self.postgres()
        env = {
            "CANARY_DATABASE": f"postgresql+asyncpg://forge:synthetic-only@{pg}:5432/forge",
            "CANARY_LEGACY_DATABASE": f"postgresql+asyncpg://forge:synthetic-only@{pg}:5432/canary_legacy",
        }
        volumes = (volume + ":/canary:ro,z", volume + ":" + cert["trust"] + ":ro,z")
        output = self.execute(
            image, "python", "-I", "-", network=self.name, volumes=volumes, env=env, input=PROBE
        )
        return json.loads(output.strip().splitlines()[-1])

    def close(self):
        for name in reversed(self.containers):
            subprocess.run([self.args.runtime, "rm", "-f", name], capture_output=True, timeout=30)
        for kind, name in (("network", self.name), ("volume", self.name + "-fixture")):
            subprocess.run([self.args.runtime, kind, "rm", name], capture_output=True, timeout=30)


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--runtime", default=shutil.which("podman") or shutil.which("docker"))
    parser.add_argument("--expected-version")
    parser.add_argument("--postgres-image", default="postgres:17.8")
    parser.add_argument("--allow-unpinned", action="store_true")
    parser.add_argument("--stages", choices=["fresh"], default="fresh")
    parser.add_argument("--results-json", type=Path)
    args = parser.parse_args(argv)
    if not args.runtime or (
        not re.fullmatch(r".+@sha256:[0-9a-f]{64}", args.image) and not args.allow_unpinned
    ):
        parser.error("select a runtime and digest-pinned image; --allow-unpinned is local only")
    return args


def main(argv=None):
    args = arguments(argv)
    canary = Canary(args)
    result = {"image": args.image, "stages": []}
    row = {"stage": "fresh", "capability": "release-artifact-canary", "outcome": "fail"}
    try:
        image = canary.image()
        result["resolved_image_id"] = image
        receipt = canary.fresh(image)
        assert not args.expected_version or receipt["version"] == args.expected_version
        assert canary.image() == image, "image reference changed during canary"
        result["receipt"] = receipt
        row.update(
            outcome="pass",
            detail="installed image, explicit schema, refusal, health; synthetic TLS peers",
        )
        print(json.dumps(result | {"stages": [row]}))
        return 0
    except Exception as error:
        row["detail"] = str(error)[:400]
        print(str(error), file=sys.stderr)
        return 1
    finally:
        result["stages"].append(row)
        if args.results_json:
            args.results_json.write_text(json.dumps(result, indent=2) + "\n")
        canary.close()


if __name__ == "__main__":
    sys.exit(main())
