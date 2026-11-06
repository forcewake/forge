"""Bind independently installed server and lane artifacts to one release identity."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile, ZipFile

DIGEST = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SERVER_ONLY = frozenset({"forge", "fastapi", "sqlalchemy", "alembic", "asyncpg", "aiosqlite"})
WORKSPACE = frozenset({"forge", "forge-lane", "forge-protocol"})
WORKFLOW_PIN = "FORGE_WORKFLOW_REPOSITORY/.github/workflows/lane.yml@FORGE_WORKFLOW_COMMIT"


def sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("release artifact must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_identity(path: Path) -> dict[str, str]:
    try:
        with ZipFile(path) as archive:
            metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ValueError("wheel requires one metadata record")
            value = BytesParser().parsebytes(archive.read(metadata[0]))
            package = normalized(value.get("Name", "")).replace("-", "_")
            if package in {"forge", "forge_lane", "forge_protocol"}:
                for name in archive.namelist():
                    if (
                        name.startswith("/")
                        or ".." in Path(name).parts
                        or not (name.startswith(package + "/") or name.startswith(package + "-"))
                    ):
                        raise ValueError("wheel contains a foreign package or unsafe path")
    except (BadZipFile, KeyError):
        raise ValueError("release wheel is invalid") from None
    name, version = value.get("Name", ""), value.get("Version", "")
    if not name or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[a-z0-9.+-]*)?", version):
        raise ValueError("wheel name and version are required")
    return {"name": normalized(name), "version": version, "file": path.name, "sha256": sha256(path)}


def locked_requirements(path: Path, *, lane: bool) -> tuple[str, set[str]]:
    text = path.read_text()
    entries, current = [], ""
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        current += " " + raw.removesuffix("\\").strip()
        if raw.endswith("\\"):
            continue
        entries.append(current.strip())
        current = ""
    if current or not entries:
        raise ValueError("dependency lock is incomplete")
    names = set()
    for entry in entries:
        hashes = re.findall(r" --hash=sha256:([0-9a-f]{64})(?=\s|$)", entry)
        requirement = re.sub(r" --hash=sha256:[0-9a-f]{64}(?=\s|$)", "", entry)
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9_.-]*)==[^\s;]+(?:\s*;\s*[^\r\n]+)?", requirement
        )
        if not hashes or match is None or " @ " in entry or "file:" in entry:
            raise ValueError(
                "dependencies require exact versions and SHA256 hashes, without source checkouts"
            )
        names.add(normalized(match[1]))
    if names & WORKSPACE or lane and names & SERVER_ONLY:
        raise ValueError("lane dependencies cannot contain server or workspace packages")
    return "\n".join(entries) + "\n", names


def release_base(repo: str, rc: str, commit: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
        raise ValueError("release requires an explicit owner/repository")
    if not re.fullmatch(r"v\d+\.\d+\.\d+-rc\.[A-Za-z0-9.-]+", rc) or not COMMIT.fullmatch(commit):
        raise ValueError("release requires a unique RC and full immutable source commit")
    return f"https://github.com/{repo}/releases/download/{rc}"


def write_once(path: Path, content: str) -> None:
    raw = content.encode()
    if path.is_symlink():
        raise ValueError("release artifact cannot be a symlink: " + path.name)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("refusing to replace previously bound artifact bytes: " + path.name)
    else:
        path.write_bytes(raw)


def lane_requirements(dist: Path, base: str, extras: tuple[str, ...]) -> dict[str, dict[str, str]]:
    if extras not in ((), ("claude",)):
        raise ValueError("only the explicit Claude SDK extra is supported")
    locked, names = locked_requirements(dist / "lane-v1-dependencies.txt", lane=True)
    if ("claude-agent-sdk" in names) != ("claude" in extras):
        raise ValueError("selected SDK extras differ from the lane dependency lock")
    identities = [wheel_identity(path) for path in sorted(dist.glob("*.whl"))]
    lane = {
        item["name"]: item
        for item in identities
        if item["name"] in {"forge-lane", "forge-protocol"}
    }
    if len(lane) != 2 or len(identities) != 3 or {item["name"] for item in identities} != WORKSPACE:
        raise ValueError("release requires exactly the server, protocol and lane wheels")
    wheel_lines = []
    for name in ("forge-protocol", "forge-lane"):
        item = lane[name]
        selection = name + ("[claude]" if name == "forge-lane" and extras else "")
        wheel_lines.append(f"{selection} @ {base}/{item['file']} --hash=sha256:{item['sha256']}")
    write_once(dist / "lane-v1-requirements.txt", locked + "\n" + "\n".join(wheel_lines) + "\n")
    return lane


def workflow_pins(repo: str, commit: str) -> dict[str, str]:
    release_base(repo, "v1.0.0-rc.validation", commit)
    return {
        "workflow_repository": repo,
        "workflow_commit": commit,
        "github_workflow_ref": f"{repo}/.github/workflows/lane.yml@{commit}",
        "gitlab_include_file": "/ci/lane.gitlab-ci.yml",
        "gitlab_template_url": f"https://raw.githubusercontent.com/{repo}/{commit}/ci/lane.gitlab-ci.yml",
    }


def stage_assets(dist: Path, repo: str, commit: str) -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    template = (root / "templates/github/forge-lane.yml").read_text()
    if template.count(WORKFLOW_PIN) != 1:
        raise ValueError("GitHub wrapper template requires one explicit immutable pin marker")
    write_once(
        dist / "forge-lane.github.yml",
        template.replace(WORKFLOW_PIN, workflow_pins(repo, commit)["github_workflow_ref"]),
    )
    write_once(
        dist / "forge-lane.gitlab-include.yml",
        (
            "include:\n  - remote: "
            + json.dumps(workflow_pins(repo, commit)["gitlab_template_url"])
            + "\n"
        ),
    )
    reporter = root / "tools/ci/github_junit.py"
    write_once(dist / "github-junit-reporter.py", reporter.read_text())
    return {
        "github_wrapper": "forge-lane.github.yml",
        "gitlab_include": "forge-lane.gitlab-include.yml",
        "verification_reporter": "github-junit-reporter.py",
    }


def artifact_files(directory: Path) -> list[Path]:
    return [
        p
        for p in sorted(directory.iterdir())
        if p.name not in (".gitignore", "release-manifest.json")
    ]


def prepare(
    dist: Path,
    repo: str,
    tag: str,
    rc: str,
    commit: str,
    image: str,
    lane_extras: tuple[str, ...] = ("claude",),
) -> dict:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("release tag must be a stable semantic version")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("release image must be pinned by registry digest")
    base = release_base(repo, rc, commit)
    if not rc.startswith(tag + "-rc."):
        raise ValueError("RC must belong to the selected stable release")
    if not list(dist.glob("*.tar.gz")):
        raise ValueError("release requires source distributions")
    lane = lane_requirements(dist, base, lane_extras)
    assets = stage_assets(dist, repo, commit)
    server = [wheel_identity(p) for p in dist.glob("*.whl") if p.name.startswith("forge-")]
    if len(server) != 1 or server[0]["version"] != tag[1:]:
        raise ValueError("release requires exactly one server wheel matching its tag")
    locked_requirements(dist / "runtime-requirements.txt", lane=False)
    files = {p.name: sha256(p) for p in artifact_files(dist)}
    manifest = {
        "schema": "forge.release-candidate/2",
        "version": tag[1:],
        "commit": commit,
        "image": image,
        "rc_tag": rc,
        "artifacts": files,
        "lane_extras": list(lane_extras),
        "wheel_url": f"{base}/{server[0]['file']}",
        "wheel_sha256": server[0]["sha256"],
        "requirements_url": f"{base}/runtime-requirements.txt",
        "requirements_sha256": files["runtime-requirements.txt"],
        "lane_requirements_url": f"{base}/lane-v1-requirements.txt",
        "lane_requirements_sha256": files["lane-v1-requirements.txt"],
        "lane_wheels": lane,
        **workflow_pins(repo, commit),
        **{
            key + suffix: (base + "/" + filename if suffix == "_url" else files[filename])
            for key, filename in assets.items()
            for suffix in ("_url", "_sha256")
        },
    }
    write_once(dist / "release-manifest.json", json.dumps(manifest, indent=2) + "\n")
    verify(manifest, dist)
    return manifest


def verify(manifest: dict, directory: Path) -> None:
    if manifest.get("schema") != "forge.release-candidate/2":
        raise ValueError("unsupported release candidate manifest")
    pins = workflow_pins(manifest["workflow_repository"], manifest["commit"])
    if any(manifest.get(key) != value for key, value in pins.items()):
        raise ValueError("workflow must use the exact full source commit, never a tag or branch")
    base = release_base(manifest["workflow_repository"], manifest["rc_tag"], manifest["commit"])
    actual = {p.name for p in artifact_files(directory)}
    if actual != set(manifest["artifacts"]):
        raise ValueError("release artifact inventory changed")
    for name, expected in manifest["artifacts"].items():
        if Path(name).name != name or not DIGEST.fullmatch(expected):
            raise ValueError("invalid release artifact identity")
        if sha256(directory / name) != expected:
            raise ValueError(f"release artifact changed: {name}")
    lane = lane_requirements(directory, base, tuple(manifest["lane_extras"]))
    if lane != manifest["lane_wheels"]:
        raise ValueError("lane wheel identity changed")
    for prefix, filename in (
        ("lane_", "lane-v1-requirements.txt"),
        ("", "runtime-requirements.txt"),
    ):
        if manifest[prefix + "requirements_url"] != base + "/" + filename or (
            manifest[prefix + "requirements_sha256"] != manifest["artifacts"][filename]
        ):
            raise ValueError("requirement identity differs from its bound release artifact")
    for key, filename in (
        ("github_wrapper", "forge-lane.github.yml"),
        ("gitlab_include", "forge-lane.gitlab-include.yml"),
        ("verification_reporter", "github-junit-reporter.py"),
    ):
        if manifest[key + "_url"] != base + "/" + filename or (
            manifest[key + "_sha256"] != manifest["artifacts"][filename]
        ):
            raise ValueError("workflow/reporter identity differs from its release artifact")
    expected_wrapper = f"    uses: {manifest['github_workflow_ref']}\n"
    if expected_wrapper not in (directory / "forge-lane.github.yml").read_text():
        raise ValueError("wrapper does not use its exact immutable workflow commit")
    server = next(wheel_identity(p) for p in directory.glob("forge-*.whl"))
    if server["version"] != manifest["version"] or not manifest["rc_tag"].startswith(
        "v" + manifest["version"] + "-rc."
    ):
        raise ValueError("release version differs from its wheel or RC")
    if (
        manifest["wheel_url"] != base + "/" + server["file"]
        or manifest["wheel_sha256"] != server["sha256"]
    ):
        raise ValueError("server wheel identity changed")


def verify_scenarios(smoke: dict) -> None:
    scenarios = smoke.get("scenarios", [])
    if len(scenarios) != 2 or {item.get("scenario") for item in scenarios} != {"happy", "repair"}:
        raise ValueError("both happy and native CI repair scenarios are mandatory")
    selected = {item["scenario"]: item for item in scenarios}
    for name, expected_attempt in (("happy", 1), ("repair", 2)):
        item = selected[name]
        candidates = item.get("candidates", [])
        if (
            item.get("attempt") != expected_attempt
            or item.get("generation") != expected_attempt
            or len(candidates) != expected_attempt
            or not item.get("oracle_jobs")
            or not item.get("lane_dispatches")
            or not item.get("draft")
            or candidates[-1].get("sha") != item.get("candidate_sha")
        ):
            raise ValueError("scenario lacks exact native execution/candidate evidence")
    repair = selected["repair"]
    first, last = repair["candidates"]
    failure = repair.get("initial_failure", {})
    jobs = failure.get("native_jobs", [])
    if (
        first.get("sha") == last.get("sha")
        or last.get("base_sha") != first.get("sha")
        or failure.get("candidate_sha") != first.get("sha")
        or failure.get("implementation_return") != 41
        or repair.get("implementation_return") != 42
        or not jobs
        or any(
            job.get("status") != "failed"
            or job.get("failure_kind") != "code"
            or not all(job.get(key) for key in ("native_id", "native_run_id", "workflow_path"))
            for job in jobs
        )
        or smoke["candidate_sha"] != last.get("sha")
    ):
        raise ValueError(
            "repair must prove native code failure and a distinct correctly parented candidate"
        )


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
                for key in ("version", "image", "commit", "lane_requirements_sha256")
            )
            or smoke.get("status") != "passed"
        ):
            raise ValueError("live smoke did not pass on these exact lane release bytes")
        if not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", smoke.get("candidate_sha", "")
        ) or not smoke.get("oracle_jobs"):
            raise ValueError("live smoke lacks native candidate CI evidence")
        verify_scenarios(smoke)
    return {
        **manifest,
        "schema": "forge.release/2",
        "invariants": {"passed": len(tests), "skipped": 0},
        "canary": canary,
        "live_smokes": smokes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify", "report"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    for name in ("repo", "tag", "rc", "commit", "image"):
        parser.add_argument("--" + name)
    parser.add_argument("--lane-extras", default="claude", choices=("", "claude"))
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(
            args.dist,
            args.repo,
            args.tag,
            args.rc,
            args.commit,
            args.image,
            tuple(filter(None, args.lane_extras.split(","))),
        )
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
        write_once(Path("release-report.json"), json.dumps(value, indent=2) + "\n")


if __name__ == "__main__":
    main()
