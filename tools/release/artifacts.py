"""Bind release files, the installed-wheel tests and live smokes to one image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(dist: Path, repo: str, tag: str, rc: str, commit: str, image: str) -> dict:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("release tag must be a stable semantic version")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release requires its complete source commit")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("release image must be pinned by registry digest")
    wheels = sorted(dist.glob("*.whl"))
    if not wheels or not list(dist.glob("*.tar.gz")):
        raise ValueError("release requires wheels and source distributions")
    server = [p for p in wheels if p.name.startswith(f"forge-{tag[1:]}-")]
    if len(server) != 1:
        raise ValueError("release requires exactly one server wheel matching its tag")
    base = f"https://github.com/{repo}/releases/download/{rc}"
    requirements = dist / "lane-requirements.txt"
    locked = (dist / "lane-dependencies.txt").read_text()
    if any(line.startswith("-e ") or " @ file:" in line for line in locked.splitlines()):
        raise ValueError("release requirements cannot contain source checkouts")
    requirements.write_text(
        locked
        + "\n"
        + "\n".join(f"{base}/{p.name} --hash=sha256:{sha256(p)}" for p in wheels)
        + "\n"
    )
    lane_wheels = [p for p in wheels if p.name.startswith(("forge_lane-", "forge_protocol-"))]
    if len(lane_wheels) != 2:
        raise ValueError("release requires exactly one lane and protocol wheel")
    lane_requirements = dist / "lane-v1-requirements.txt"
    lane_locked = (dist / "lane-v1-dependencies.txt").read_text()
    if any(line.startswith("-e ") or " @ file:" in line for line in lane_locked.splitlines()):
        raise ValueError("lane requirements cannot contain source checkouts")
    lane_requirements.write_text(
        lane_locked
        + "\n"
        + "\n".join(f"{base}/{p.name} --hash=sha256:{sha256(p)}" for p in lane_wheels)
        + "\n"
    )
    artifacts = {
        p.name: sha256(p)
        for p in sorted(dist.iterdir())
        if p.is_file() and p.name != "release-manifest.json"
    }
    manifest = {
        "version": tag[1:],
        "commit": commit,
        "image": image,
        "rc_tag": rc,
        "artifacts": artifacts,
        "wheel_url": f"{base}/{server[0].name}",
        "wheel_sha256": sha256(server[0]),
        "requirements_url": f"{base}/{requirements.name}",
        "requirements_sha256": sha256(requirements),
        "lane_requirements_url": f"{base}/{lane_requirements.name}",
        "lane_requirements_sha256": sha256(lane_requirements),
    }
    (dist / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify(manifest: dict, directory: Path) -> None:
    for name, expected in manifest["artifacts"].items():
        if Path(name).name != name or not DIGEST.fullmatch(expected):
            raise ValueError("invalid release artifact identity")
        if sha256(directory / name) != expected:
            raise ValueError(f"release artifact changed: {name}")


def report(manifest: dict, canary: dict, smokes: list[dict], junit: Path) -> dict:
    if canary.get("image") != manifest["image"]:
        raise ValueError("canary tested another image")
    stages = canary.get("stages", [])
    if not any(r.get("stage") == "fresh" and r.get("outcome") == "pass" for r in stages):
        raise ValueError("fresh-database boot did not pass")
    if any(r.get("outcome") not in {"pass", "skip"} for r in stages):
        raise ValueError("a boot or migration stage failed")
    tests = list(ET.parse(junit).getroot().iter("testcase"))
    if not tests or any(
        any(child.tag in {"failure", "error", "skipped"} for child in case) for case in tests
    ):
        raise ValueError("installed-wheel Invariant Suite requires a zero-skip pass")
    if len(smokes) != 2 or {r.get("provider") for r in smokes} != {"github", "gitlab"}:
        raise ValueError("both provider smokes are mandatory")
    for smoke in smokes:
        if (
            any(
                smoke.get(key) != manifest[key]
                for key in ("version", "image", "wheel_sha256", "requirements_sha256")
            )
            or smoke.get("status") != "passed"
        ):
            raise ValueError("live smoke did not pass on these release bytes")
        if not smoke.get("candidate_sha") or not smoke.get("oracle_jobs"):
            raise ValueError("live smoke lacks native candidate CI evidence")
    return {
        "schema": "forge.release/1",
        **manifest,
        "invariants": {"passed": len(tests), "skipped": 0},
        "canary": canary,
        "live_smokes": smokes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify", "report"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--repo")
    parser.add_argument("--tag")
    parser.add_argument("--rc")
    parser.add_argument("--commit")
    parser.add_argument("--image")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.dist, args.repo, args.tag, args.rc, args.commit, args.image)
        return
    manifest = json.loads((args.dist / "release-manifest.json").read_text())
    verify(manifest, args.dist)
    if args.command == "report":
        value = report(
            manifest,
            json.loads(Path("canary-results.json").read_text()),
            [json.loads(Path(f"{p}-smoke.json").read_text()) for p in ("github", "gitlab")],
            Path("invariants.xml"),
        )
        value["sbom_sha256"] = sha256(Path("sbom.spdx.json"))
        Path("release-report.json").write_text(json.dumps(value, indent=2) + "\n")


if __name__ == "__main__":
    main()
