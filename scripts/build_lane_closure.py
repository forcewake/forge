#!/usr/bin/env python
"""R36-10 (#269) — build and verify the lane's hash-locked wheelhouse.

The Forge wheel is hash-pinned (the R36-07 ladder), but pip still
resolves the wheel's RUNTIME dependencies at install time — a
reproducible-FILE guarantee, not a reproducible-ENVIRONMENT one. This
script closes the gap: it builds a wheelhouse in which EVERY artifact
the lane runtime consists of — forge itself plus its whole transitive
dependency set — is present as an exact, sha256-pinned wheel, and
writes ``closure-manifest.json``, the document whose canonical-JSON
digest (``closure_digest``) IS the closure identity:

    forge==0.35.0  +  fastapi==…  +  … (every pinned wheel, hashed)

The dependency set is resolved OFFLINE FROM THE LOCKFILE — never from
a live index: ``uv export --frozen`` turns ``uv.lock`` (the committed,
hash-pinned resolution) into a requirements file whose every pin
carries ``--hash=sha256:…``, and ``pip download --require-hashes``
fetches exactly those artifacts, refusing any file whose bytes do not
reproduce a listed hash. Resolution is therefore lockfile-shaped, not
registry-shaped: the same lock always yields the same closure.

Usage
-----

Build from the PROMOTED release (the production route — the wheel the
latest archived promotion record vouches for):

    uv run python scripts/build_lane_closure.py

Build from the LOCAL TREE (the development route, honestly less
qualified — a moving source ref is never a pinned identity, exactly
like the ladder's dev-source route):

    uv run python scripts/build_lane_closure.py --local

Build from an EXPLICIT pinned wheel URL:

    uv run python scripts/build_lane_closure.py \\
        --wheel-url https://github.com/.../forge-0.35.0-py3-none-any.whl \\
        --wheel-sha256 <64-hex>

Verify a staged wheelhouse against its manifest (no network, no
imports from the directory — the refusal is pre-execution):

    uv run python scripts/build_lane_closure.py --verify dist/lane-closure

A tiny pinned dependency set (tests, dry runs):

    --requirements <file>   # a hash-pinned requirements file

The manifest contract (schema, digest, verify semantics) lives in
``forge.adaptive.qualification`` — the runtime verifies staged
wheelhouses through the LIBRARY, never through this script. Operations
documentation: ``docs/operations/lane-closure.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from forge.adaptive.qualification import (  # noqa: E402
    CLOSURE_MANIFEST_FILENAME,
    ClosureArtifact,
    ClosureVerificationError,
    LaneClosureManifest,
    hash_file_sha256,
    verify_closure_dir,
    write_closure_manifest_file,
)

#: The canonical, path-free resolution command every manifest records.
#: Deterministic by construction (no absolute paths, no timestamps) so
#: two clean builds of the same lock produce the SAME closure_digest.
RESOLUTION_COMMAND = (
    "uv export --frozen --no-dev --no-emit-project --format requirements-txt "
    "-o lane-requirements.txt"
    " && python -m pip download --only-binary :all: --require-hashes "
    "-r lane-requirements.txt"
)

#: The requirements file name inside the builder staging area (also the
#: logical name the canonical resolution command carries).
REQUIREMENTS_FILENAME = "lane-requirements.txt"


class ClosureBuildError(RuntimeError):
    """The closure cannot be built — the wheel source, the lock export
    or the hash-checked download refused. Building never falls back to
    an unpinned shape: a closure that cannot be hashed completely is
    not built at all."""


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a build subprocess, capturing output; the caller turns a
    non-zero exit into a :class:`ClosureBuildError` with the output."""
    return subprocess.run(
        command, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False
    )


# ---------------------------------------------------------------------------
# The forge wheel: local build, pinned URL, or the promoted record
# ---------------------------------------------------------------------------


def _forge_wheel_local(root: Path, staging: Path) -> tuple[Path, str]:
    """Build the wheel from the working tree (``uv build``) — the LOCAL
    development route. Returns ``(wheel_path, source)``."""
    run = _run(["uv", "build", "--wheel", "--out-dir", str(staging)], cwd=root)
    if run.returncode != 0:
        raise ClosureBuildError(f"uv build failed:\n{run.stdout}\n{run.stderr}")
    wheels = sorted(staging.glob("*.whl"))
    if len(wheels) != 1:
        raise ClosureBuildError(
            f"uv build produced {len(wheels)} wheels ({[w.name for w in wheels]}) — "
            "the closure carries exactly one forge wheel"
        )
    return wheels[0], "local-uv-build"


def _download_pinned(url: str, sha256: str, destination: Path) -> Path:
    """Fetch a wheel URL and REFUSE the bytes unless they reproduce the
    pinned sha256 (the same pre-execution check the lane install
    fragment performs)."""
    if not url.endswith(".whl"):
        raise ClosureBuildError(f"the pinned wheel URL must name a .whl, got {url!r}")
    with urllib.request.urlopen(url) as response:  # noqa: S310 - the URL is the operator's pin
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != sha256:
        raise ClosureBuildError(
            f"the pinned wheel {url} hashes {actual[:12]}, the pin says {sha256[:12]} — "
            "the bytes are not the pinned ones; refusing before anything installs"
        )
    target = destination / url.rsplit("/", 1)[-1]
    target.write_bytes(payload)
    return target


def _forge_wheel_from_record(root: Path, staging: Path) -> tuple[Path, str]:
    """Resolve the PROMOTED wheel from the latest archived promotion
    record — the production route. An image-only record (no wheel
    built) refuses honestly: nothing is fabricated."""
    from forge.release_promotion import latest_promotion_record

    record = latest_promotion_record(root)
    if record is None:
        raise ClosureBuildError(
            "no archived promotion record under docs/releases/evidence/ — there is "
            "no promoted wheel to close over; use --local or --wheel-url"
        )
    if not record.wheel_url or not record.wheel_sha256:
        raise ClosureBuildError(
            f"the promotion record for v{record.version} built no wheel (image-only "
            "release) — it cannot anchor a wheel closure; use --local or --wheel-url"
        )
    wheel = _download_pinned(record.wheel_url, record.wheel_sha256, staging)
    return wheel, "promotion-record"


# ---------------------------------------------------------------------------
# The dependency set: the frozen lock, exported and hash-downloaded
# ---------------------------------------------------------------------------


def _export_requirements(root: Path, staging: Path) -> Path:
    """Export the frozen runtime pins (+hashes) from ``uv.lock`` —
    ``uv export --frozen`` never resolves against an index; it renders
    the committed lock, markers and all, as a hash-pinned requirements
    file."""
    target = staging / REQUIREMENTS_FILENAME
    run = _run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "-o",
            str(target),
        ],
        cwd=root,
    )
    if run.returncode != 0:
        raise ClosureBuildError(f"uv export --frozen failed:\n{run.stdout}\n{run.stderr}")
    if not target.is_file():
        raise ClosureBuildError(f"uv export wrote no requirements file at {target}")
    return target


def _seed_builder_venv(staging: Path) -> Path:
    """A throwaway interpreter with pip (``uv venv --seed``) — the
    builder's pip, kept OUT of the closure: only wheels land in the
    wheelhouse, never the builder's tooling."""
    venv_dir = staging / "builder-venv"
    run = _run(["uv", "venv", "--seed", str(venv_dir)])
    if run.returncode != 0:
        raise ClosureBuildError(f"uv venv --seed failed:\n{run.stdout}\n{run.stderr}")
    python = venv_dir / "bin" / "python"
    if not python.is_file():
        raise ClosureBuildError(f"the seeded builder venv has no python at {python}")
    return python


def _pip_download(python: Path, requirements: Path, closure_dir: Path) -> None:
    """``pip download`` the pinned set: ``--require-hashes`` refuses any
    artifact whose bytes do not reproduce a lock hash;
    ``--only-binary :all:`` keeps the wheelhouse wheels-only."""
    run = _run(
        [
            str(python),
            "-m",
            "pip",
            "download",
            "--only-binary",
            ":all:",
            "--require-hashes",
            "--no-deps",
            "-r",
            str(requirements),
            "-d",
            str(closure_dir),
        ]
    )
    if run.returncode != 0:
        raise ClosureBuildError(f"pip download refused the pinned set:\n{run.stdout}\n{run.stderr}")


# ---------------------------------------------------------------------------
# Build and verify
# ---------------------------------------------------------------------------


def build_closure(
    root: Path,
    closure_dir: Path,
    *,
    local: bool = False,
    wheel_url: str = "",
    wheel_sha256: str = "",
    requirements: Path | None = None,
) -> LaneClosureManifest:
    """Build the hash-locked wheelhouse at *closure_dir* and write its
    manifest. The directory must not exist or must be EMPTY — a build
    over a populated directory could silently absorb foreign files into
    what then claims to be a closure."""
    closure_dir = Path(closure_dir)
    if closure_dir.exists() and any(closure_dir.iterdir()):
        raise ClosureBuildError(
            f"{closure_dir} is not empty — a closure is built into a clean "
            "directory; refusing to absorb foreign files"
        )
    closure_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="forge-lane-closure-") as tmp:
        staging = Path(tmp)
        if local:
            wheel_path, source = _forge_wheel_local(root, staging)
        elif wheel_url:
            if not wheel_sha256:
                raise ClosureBuildError(
                    "an explicit --wheel-url requires --wheel-sha256 — an unpinned "
                    "URL is exactly what the closure exists to refuse"
                )
            wheel_path = _download_pinned(wheel_url, wheel_sha256, staging)
            source = "pinned-url"
        else:
            wheel_path, source = _forge_wheel_from_record(root, staging)
        reqs = Path(requirements) if requirements else _export_requirements(root, staging)
        _pip_download(_seed_builder_venv(staging), reqs, closure_dir)
        shutil.copy2(wheel_path, closure_dir / wheel_path.name)
    artifacts = sorted(closure_dir.glob("*.whl"))
    if not artifacts:
        raise ClosureBuildError("the wheelhouse carries no wheels — nothing to close over")
    manifest = LaneClosureManifest(
        forge_wheel=ClosureArtifact(
            name=wheel_path.name, sha256=hash_file_sha256(closure_dir / wheel_path.name)
        ),
        forge_version=wheel_path.name.split("-")[1],
        forge_source=source,
        resolution_command=RESOLUTION_COMMAND,
        artifacts=tuple(
            ClosureArtifact(name=wheel.name, sha256=hash_file_sha256(wheel)) for wheel in artifacts
        ),
    )
    digest = write_closure_manifest_file(closure_dir / CLOSURE_MANIFEST_FILENAME, manifest)
    print(f"closure built: {closure_dir}")
    print(f"  forge wheel : {manifest.forge_wheel.name} ({source})")
    print(f"  artifacts   : {len(manifest.artifacts)} wheels, hash-pinned")
    print(f"  closure sha256: {digest}")
    return manifest


def verify_closure(closure_dir: Path) -> LaneClosureManifest:
    """Verify a staged wheelhouse (see
    :func:`forge.adaptive.qualification.verify_closure_dir` — every
    artifact present and hash-matching, no undeclared files, the
    manifest vouches for itself)."""
    manifest = verify_closure_dir(closure_dir)
    print(f"closure verified: {closure_dir}")
    print(f"  closure sha256: {manifest.closure_digest}")
    print(f"  forge wheel : {manifest.forge_wheel.name} ({manifest.forge_source})")
    print(f"  artifacts   : {len(manifest.artifacts)} wheels, all hash-matching")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_lane_closure.py",
        description=(
            "Build/verify the lane's hash-locked wheelhouse (R36-10): forge + its "
            "frozen-lock dependency set, every wheel sha256-pinned in one manifest."
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--local",
        action="store_true",
        help=(
            "build the forge wheel from the working tree (uv build) — the "
            "development route, honestly less qualified than a promoted release"
        ),
    )
    source.add_argument("--wheel-url", default="", help="an explicitly pinned wheel URL")
    parser.add_argument(
        "--wheel-sha256", default="", help="the pinned wheel's sha256 (required with --wheel-url)"
    )
    parser.add_argument(
        "--closure-dir",
        default=str(ROOT / "dist" / "lane-closure"),
        help="the wheelhouse directory to build into (default: dist/lane-closure)",
    )
    parser.add_argument(
        "--requirements",
        default=None,
        help=(
            "a hash-pinned requirements file overriding the frozen-lock export "
            "(tests / tiny closures); the download stays --require-hashes"
        ),
    )
    parser.add_argument(
        "--verify",
        metavar="CLOSURE_DIR",
        default=None,
        help="verify a staged wheelhouse against its closure-manifest.json (no network)",
    )
    args = parser.parse_args(argv)

    if args.verify:
        try:
            verify_closure(Path(args.verify))
        except ClosureVerificationError as error:
            print(
                f"CLOSURE VERIFICATION REFUSED ({len(error.problems)} problem(s)):", file=sys.stderr
            )
            for problem in error.problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        return 0

    try:
        build_closure(
            ROOT,
            Path(args.closure_dir),
            local=args.local,
            wheel_url=args.wheel_url,
            wheel_sha256=args.wheel_sha256,
            requirements=Path(args.requirements) if args.requirements else None,
        )
    except (ClosureBuildError, ClosureVerificationError) as error:
        print(f"CLOSURE BUILD REFUSED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
