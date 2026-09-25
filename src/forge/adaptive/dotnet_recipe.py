"""R38-08 (#309) — the SDK-pinned .NET service-and-dependency recipe.

R37-15 (#296) proved verification against BUILT Python wheels, sqlite
transitions and a socket fake broker — real mechanics, reference
dependencies. This module is the next step the review named: a
CUSTOMER-SHAPED recipe — two .NET services sharing a contract, built
with a PINNED SDK, tested with a complete TRX inventory, against REAL
dependency images (PostgreSQL, RabbitMQ) — qualified honestly to the
maximum extent the lab allows, never faked green:

- :class:`DotnetRecipeManifest` — the pinned manifest
  (``forge.verification.dotnet-recipe-manifest/1``): the exact dotnet
  SDK + target framework, the pinned NuGet test packages, the
  dependency IMAGES with immutable digests, the required test
  projects/frameworks/report paths and the migration pair. Loaded from
  ``qualification/recipes/dotnet-service-dependency/manifest.json``;
  :attr:`DotnetRecipeManifest.manifest_digest` is the canonical-JSON
  sha256, so a changed pin is a different recipe by construction and a
  verdict frozen against one digest never silently covers another.
- **The TRX arm** — ``dotnet test`` per required project with
  ``--logger trx``, an identity sidecar written beside every report
  (the #226 ``<report>.identity.json`` contract), reconciled through
  the EXISTING qualification machinery (:class:`ExpectedReports` +
  :func:`forge.adaptive.qualification.reconcile_reports`): a MISSING
  report is ``missing_report``, a leftover is ``stale_report``, a
  failing second project can never disappear behind a passing first —
  and the inventory itself is frozen with the work contract via
  :func:`forge.adaptive.qualification.freeze_report_inventory`.
- **The migration arm** — the fixture's N-1 -> N SQL pair over the
  REAL postgres container (psql inside the container, seeded canary
  rows, preservation via count + per-row sha256 fingerprints over the
  preserved columns, and a CONSTRAINT probe: the negative insert must
  succeed on the baseline and FAIL after the upgrade — not just a
  version field). The sqlite dialect is the labeled reference stand-in
  when no container can run.
- **The redelivery arm** — against the REAL RabbitMQ (a minimal
  stdlib AMQP 0-9-1 client: declare a durable queue, publish with a
  deliberate DUPLICATE publish, ``basic.get`` WITHOUT ack, crash the
  channel after the state commit, receive the broker's REDelivery,
  ack only then) with the consumer's exactly-once side effects
  asserted from the database. When the image cannot run, the arm
  records ``image-unavailable`` and the labeled in-process reference
  harness stands in — never a green claim about a broker that never
  ran.
- **The executor binding** — the recipe runs its tool invocations
  under the VerificationExecutor's OWN isolation machinery (composed,
  not duplicated): the same :class:`EnforcementProfile`,
  :func:`scrubbed_environment` allowlist scrub and
  :func:`narrowed_path_entries` PATH policy, with the #308
  five-outcome probe taxonomy carrying the dependency containers'
  network reachability as the positive control
  (:func:`dependency_control_probe`). The run document binds the
  enforcement-profile digest, so its claims are scoped to exactly the
  profile that produced them.

Honesty rules that hold everywhere in this module: a missing dotnet
SDK is recorded (``dotnet-sdk-unavailable``) — never a synthetic
green; an unpullable image is recorded (``image-unavailable``) with
the reference arm labeled; an unpinned/changed image digest is a
typed mismatch, never a silent substitution; every disposable
container is stopped AND removed on every path (including failures);
and the run grants neither merge nor deployment authority — the
document is evidence for a verdict, not a verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from forge.adaptive.qualification import (
    IDENTITY_SIDECAR_SUFFIX,
    ExpectedReport,
    ExpectedReports,
    freeze_report_inventory,
    reconcile_reports,
)
from forge.adaptive.verification_executor import (
    DEFAULT_ENV_ALLOWLIST,
    OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_UNAVAILABLE,
    REFERENCE_COVERAGE,
    CommandRun,
    EnforcementProfile,
    ProbeAttempt,
    narrowed_path_entries,
    scrubbed_environment,
)
from forge.runs.spec import canonical_json_digest

__all__ = [
    "ARM_DIGEST_MISMATCH",
    "ARM_FAILED",
    "ARM_IMAGE_UNAVAILABLE",
    "ARM_NOT_RUN",
    "ARM_REAL",
    "ARM_REFERENCE",
    "ARM_RUNTIME_UNAVAILABLE",
    "ARM_SDK_UNAVAILABLE",
    "DEPENDENCY_COVERAGE_REAL",
    "DOTNET_RECIPE_MANIFEST_SCHEMA",
    "DOTNET_RECIPE_RUN_SCHEMA",
    "DotnetRecipeManifest",
    "DotnetRecipeRun",
    "ManifestError",
    "PinnedImage",
    "PinnedPackage",
    "PinnedTestProject",
    "REAL_COVERAGE_LABEL",
    "dependency_control_probe",
    "environment_digest_of",
    "expected_reports_for",
    "fixture_source_digest",
    "load_pinned_manifest",
    "main",
    "run_recipe",
    "seed_rows_document",
]

#: The pinned manifest's versioned discriminator.
DOTNET_RECIPE_MANIFEST_SCHEMA = "forge.verification.dotnet-recipe-manifest/1"

#: The run document's versioned discriminator.
DOTNET_RECIPE_RUN_SCHEMA = "forge.verification.dotnet-recipe-run/1"

#: Where the pinned manifest lives (repository root anchored).
PINNED_MANIFEST_RELPATH = Path("qualification/recipes/dotnet-service-dependency/manifest.json")

#: The per-arm honest status vocabulary. ``real`` — the arm ran against
#: the real pinned dependency; ``reference`` — the labeled stand-in
#: ran; the rest NAME the blocker instead of pretending: the SDK is
#: absent, the image could not be pulled, no container runtime exists,
#: the resolved digest is not the pin, the attempt errored, or the arm
#: was never attempted (its prerequisites failed first).
ARM_REAL = "real"
ARM_REFERENCE = "reference"
ARM_SDK_UNAVAILABLE = "dotnet-sdk-unavailable"
ARM_IMAGE_UNAVAILABLE = "image-unavailable"
ARM_RUNTIME_UNAVAILABLE = "container-runtime-unavailable"
ARM_DIGEST_MISMATCH = "image-digest-mismatch"
ARM_FAILED = "failed"
ARM_NOT_RUN = "not-run"

#: The coverage labels: ``real-dependency`` claims the REAL pinned
#: image ran; ``reference-coverage`` is the executor's existing label
#: for the deterministic stand-in and claims nothing about production
#: dependencies.
REAL_COVERAGE_LABEL = "real-dependency"
DEPENDENCY_COVERAGE_REAL = REAL_COVERAGE_LABEL

#: Which container runtime to prefer (docker first, podman second —
#: the lab exposes podman; a clean runner may expose either).
CONTAINER_RUNTIMES: tuple[str, ...] = ("docker", "podman")

#: How long to wait for a dependency container to accept its first
#: connection (postgres: pg_isready; rabbitmq: the AMQP handshake).
CONTAINER_READY_TIMEOUT = 90.0

#: The container-local synthetic credential the recipe provisions (a
#: per-run user on the disposable container — never a real secret).
SYNTHETIC_DB_USER = "forge"
SYNTHETIC_DB_PASSWORD = "forge-recipe-local"
SYNTHETIC_BROKER_USER = "forge"
SYNTHETIC_BROKER_PASSWORD = "forge-recipe-local"


class ManifestError(ValueError):
    """The pinned manifest is not a valid recipe — refuse semantics."""


#: A package pin that cannot reproduce a build: a wildcard, a range or
#: the floating "latest". An exact x.y.z(-label) pin is required.
_FLOATING_PIN = re.compile(r"^(\*|latest|\d+\.\*|\*-\*|\[[^\]]+\)|\(|\{)")


# ---------------------------------------------------------------------------
# The pinned manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinnedPackage:
    """One pinned NuGet test package (a floating pin is a modelling error)."""

    name: str
    version: str

    def as_document(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class PinnedImage:
    """One pinned dependency image: the tag for humans, the DIGEST for identity."""

    role: str
    image: str
    digest: str

    def as_document(self) -> dict[str, str]:
        return {"role": self.role, "image": self.image, "digest": self.digest}


@dataclass(frozen=True)
class PinnedTestProject:
    """One required test project: where it lives, its framework, its report."""

    name: str
    framework: str
    project: str
    report: str

    def as_document(self) -> dict[str, str]:
        return {
            "name": self.name,
            "framework": self.framework,
            "project": self.project,
            "report": self.report,
        }


def _require_str(document: Mapping[str, Any], key: str, where: str) -> str:
    value = str(document.get(key) or "").strip()
    if not value:
        raise ManifestError(f"{where}: {key} must be a non-empty string")
    return value


@dataclass(frozen=True)
class DotnetRecipeManifest:
    """The frozen recipe contract (everything the run binds to).

    Constructed via :func:`load_pinned_manifest` (or
    :meth:`from_document`); every pin is validated at construction —
    a floating tag, a malformed digest or a duplicate report path is a
    :class:`ManifestError`, never a warning.
    """

    recipe_id: str
    fixture_root: str
    sdk_version: str
    target_framework: str
    packages: tuple[PinnedPackage, ...]
    images: tuple[PinnedImage, ...]
    test_projects: tuple[PinnedTestProject, ...]
    migration_baseline: str
    migration_upgrade_postgres: str
    migration_upgrade_sqlite: str
    preserved_columns: tuple[str, ...]
    broker_queue_prefix: str
    duplicates_per_message: int
    seed_rows: int
    redelivery_messages: int

    def __post_init__(self) -> None:
        if not self.packages:
            raise ManifestError("the manifest must pin at least one test package")
        for package in self.packages:
            if not package.name or not package.version:
                raise ManifestError(f"package pin {package!r} needs name AND version")
            if _FLOATING_PIN.match(package.version):
                raise ManifestError(
                    f"package {package.name} pinned as {package.version!r} — floating pins"
                    " are not reproducible; pin the exact version"
                )
        if not self.images:
            raise ManifestError("the manifest must pin at least one dependency image")
        for image in self.images:
            digest = image.digest.strip()
            if not digest.startswith("sha256:") or len(digest) != 7 + 64:
                raise ManifestError(
                    f"image {image.image} digest {digest!r} is not a sha256:64-hex manifest"
                    " digest — mutable pins are not verifiable"
                )
            try:
                int(digest[7:], 16)
            except ValueError as error:
                raise ManifestError(f"image {image.image} digest is not hex: {digest}") from error
        if not self.test_projects:
            raise ManifestError("the manifest must require at least one test project")
        names = [project.name for project in self.test_projects]
        reports = [project.report for project in self.test_projects]
        if len(set(names)) != len(names) or len(set(reports)) != len(reports):
            raise ManifestError(
                "one report path and one name per required test project — a passing first"
                " report must never answer for a second project"
            )
        if self.seed_rows <= 0 or self.redelivery_messages <= 0 or self.duplicates_per_message < 2:
            raise ManifestError(
                "seed_rows and redelivery_messages must be positive and the broker must"
                " duplicate at least twice (fewer duplicates prove no idempotency)"
            )
        if not self.preserved_columns:
            raise ManifestError("the migration pin must name the preserved columns")

    @property
    def body_document(self) -> dict[str, Any]:
        """The manifest WITHOUT its digest (what the digest covers)."""
        return {
            "schema": DOTNET_RECIPE_MANIFEST_SCHEMA,
            "recipe_id": self.recipe_id,
            "fixture_root": self.fixture_root,
            "sdk": {"version": self.sdk_version, "target_framework": self.target_framework},
            "packages": [package.as_document() for package in self.packages],
            "dependency_images": [image.as_document() for image in self.images],
            "test_projects": [project.as_document() for project in self.test_projects],
            "migrations": {
                "baseline": self.migration_baseline,
                "upgrade": {
                    "postgres": self.migration_upgrade_postgres,
                    "sqlite": self.migration_upgrade_sqlite,
                },
                "preserved_columns": list(self.preserved_columns),
            },
            "broker": {
                "queue_prefix": self.broker_queue_prefix,
                "duplicates_per_message": self.duplicates_per_message,
            },
            "scenario": {
                "seed_rows": self.seed_rows,
                "redelivery_messages": self.redelivery_messages,
            },
        }

    @property
    def manifest_digest(self) -> str:
        """The canonical-JSON sha256 over the pinned body — the recipe identity."""
        return canonical_json_digest(self.body_document)

    def as_document(self) -> dict[str, Any]:
        return {**self.body_document, "manifest_digest": self.manifest_digest}

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> DotnetRecipeManifest:
        """Read + VALIDATE a manifest document (the pinned file or a copy)."""
        if str(document.get("schema") or "") != DOTNET_RECIPE_MANIFEST_SCHEMA:
            raise ManifestError(
                f"unknown manifest schema {document.get('schema')!r}; expected"
                f" {DOTNET_RECIPE_MANIFEST_SCHEMA}"
            )
        sdk = document.get("sdk") if isinstance(document.get("sdk"), Mapping) else {}
        migrations = (
            document.get("migrations") if isinstance(document.get("migrations"), Mapping) else {}
        )
        upgrade = (
            migrations.get("upgrade") if isinstance(migrations.get("upgrade"), Mapping) else {}
        )
        broker = document.get("broker") if isinstance(document.get("broker"), Mapping) else {}
        scenario = document.get("scenario") if isinstance(document.get("scenario"), Mapping) else {}
        images_raw = document.get("dependency_images")
        packages_raw = document.get("packages")
        projects_raw = document.get("test_projects")
        if not isinstance(images_raw, list) or not isinstance(packages_raw, list):
            raise ManifestError("dependency_images and packages must be lists")
        if not isinstance(projects_raw, list):
            raise ManifestError("test_projects must be a list")
        try:
            return cls(
                recipe_id=_require_str(document, "recipe_id", "manifest"),
                fixture_root=_require_str(document, "fixture_root", "manifest"),
                sdk_version=_require_str(sdk, "version", "sdk"),
                target_framework=_require_str(sdk, "target_framework", "sdk"),
                packages=tuple(
                    PinnedPackage(
                        name=_require_str(entry, "name", "package"),
                        version=_require_str(entry, "version", "package"),
                    )
                    for entry in packages_raw
                    if isinstance(entry, Mapping)
                ),
                images=tuple(
                    PinnedImage(
                        role=_require_str(entry, "role", "image"),
                        image=_require_str(entry, "image", "image"),
                        digest=_require_str(entry, "digest", "image"),
                    )
                    for entry in images_raw
                    if isinstance(entry, Mapping)
                ),
                test_projects=tuple(
                    PinnedTestProject(
                        name=_require_str(entry, "name", "test project"),
                        framework=_require_str(entry, "framework", "test project"),
                        project=_require_str(entry, "project", "test project"),
                        report=_require_str(entry, "report", "test project"),
                    )
                    for entry in projects_raw
                    if isinstance(entry, Mapping)
                ),
                migration_baseline=_require_str(migrations, "baseline", "migrations"),
                migration_upgrade_postgres=_require_str(upgrade, "postgres", "migrations.upgrade"),
                migration_upgrade_sqlite=_require_str(upgrade, "sqlite", "migrations.upgrade"),
                preserved_columns=tuple(
                    str(column) for column in migrations.get("preserved_columns", ())
                ),
                broker_queue_prefix=_require_str(broker, "queue_prefix", "broker"),
                duplicates_per_message=int(broker.get("duplicates_per_message", 2)),
                seed_rows=int(scenario.get("seed_rows", 0)),
                redelivery_messages=int(scenario.get("redelivery_messages", 0)),
            )
        except (TypeError, ValueError) as error:
            if isinstance(error, ManifestError):
                raise
            raise ManifestError(f"malformed manifest: {error}") from error

    def image_for_role(self, role: str) -> PinnedImage | None:
        for image in self.images:
            if image.role == role:
                return image
        return None


def load_pinned_manifest(path: Path | None = None) -> DotnetRecipeManifest:
    """Load + validate the pinned recipe manifest from disk."""
    manifest_path = path or _repo_root() / PINNED_MANIFEST_RELPATH
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(
            f"cannot read the pinned manifest at {manifest_path}: {error}"
        ) from error
    return DotnetRecipeManifest.from_document(document)


def _repo_root() -> Path:
    """The repository root (anchored on this module's location)."""
    return Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Source binding digests
# ---------------------------------------------------------------------------


def fixture_source_digest(fixture_root: Path) -> str:
    """The FROZEN-SOURCE digest of the fixture: sha256 over every
    .cs/.csproj/.sql file's relative path + content digest (bin/obj
    excluded). Editing any fixture file is a different test bundle —
    reports bound to the old digest go stale, exactly as a changed
    wheel does in the executor world."""
    entries: list[str] = []
    for path in sorted(fixture_root.rglob("*")):
        if path.suffix not in (".cs", ".csproj", ".sql") or not path.is_file():
            continue
        relative = path.relative_to(fixture_root).as_posix()
        parts = relative.split("/")
        if "bin" in parts or "obj" in parts:
            continue
        entries.append(f"{relative}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
    payload = "\n".join(entries)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def environment_digest_of(
    *, manifest_digest: str, sdk_version: str, image_digests: Mapping[str, str]
) -> str:
    """The tested environment's digest: the recipe pins + the SDK that
    actually built + the image digests that actually ran. A changed
    baseline image with unchanged source STILL moves this digest — the
    recorded verification no longer applies to the new world."""
    return canonical_json_digest(
        {
            "manifest_digest": manifest_digest,
            "sdk_version": sdk_version,
            "image_digests": dict(sorted(image_digests.items())),
        }
    )


def seed_rows_document(count: int) -> list[tuple[str, str, str]]:
    """The deterministic seeded canary rows (id, total, created_at)."""
    return [
        (f"seed-{index:04d}", str(10 * index), "2026-09-25T10:00:00Z")
        for index in range(1, count + 1)
    ]


# ---------------------------------------------------------------------------
# Command receipts (every external command digested, executor-style)
# ---------------------------------------------------------------------------


def _run_receipted(
    commands: list[CommandRun],
    name: str,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 300.0,
    cwd: Path | None = None,
    stdin_text: str | None = None,
) -> tuple[CommandRun, str]:
    """Run one command; capture, digest and record its log whatever happened."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(part) for part in argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
            input=stdin_text,
        )
        exit_code: int | None = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as expired:
        exit_code = None
        stdout = expired.stdout or ""
        stderr = (expired.stderr or "") + f"\ncommand timed out after {timeout}s"
    duration = int((time.monotonic() - started) * 1000)
    log = (
        f"$ {' '.join(str(part) for part in argv)}\n"
        f"exit={exit_code}\n--- stdout ---\n{stdout}--- stderr ---\n{stderr}"
    )
    command = CommandRun(
        name=name,
        argv=tuple(str(part) for part in argv),
        exit_code=-1 if exit_code is None else exit_code,
        log_sha256=hashlib.sha256(log.encode("utf-8")).hexdigest(),
        duration_ms=duration,
    )
    commands.append(command)
    return command, log


# ---------------------------------------------------------------------------
# The executor isolation binding (composed from verification_executor)
# ---------------------------------------------------------------------------


def container_runtime_command() -> str | None:
    """The available container runtime CLI (docker, else podman), or None."""
    for runtime in CONTAINER_RUNTIMES:
        if shutil.which(runtime):
            return runtime
    return None


def recipe_tool_dirs() -> tuple[str, ...]:
    """The resolved tool directories the recipe's narrowed PATH keeps
    (the dotnet SDK dir and the container runtime dir — the same
    ``extra_dirs`` pattern the executor uses for its venv tooling)."""
    dirs: list[str] = []
    for tool in ("dotnet", container_runtime_command() or ""):
        resolved = shutil.which(tool) if tool else None
        if resolved:
            dirs.append(str(Path(resolved).parent))
    return tuple(sorted(set(dirs)))


def recipe_enforcement_profile(extra_env_keys: Sequence[str] = ()) -> EnforcementProfile:
    """The isolation configuration the recipe runs its tools under —
    composed from the executor's OWN classes so the profile digest in
    the run document is the same shape (and the same scrub) the
    VerificationExecutor would record."""
    return EnforcementProfile(
        extra_env_keys=tuple(sorted(set(extra_env_keys))),
        home_path="",
        path_entries=narrowed_path_entries(extra_dirs=recipe_tool_dirs()),
    )


def recipe_scrubbed_env(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the recipe's tool subprocesses run under: the
    parent's intersected with the executor's allowlist (no model keys,
    no provider tokens) plus the RECORDED extras."""
    return scrubbed_environment(os.environ, DEFAULT_ENV_ALLOWLIST, dict(extra_env or {}))


def dependency_control_probe(
    name: str, host: str, port: int, *, timeout: float = 5.0
) -> ProbeAttempt:
    """The #308 five-outcome probe for ONE dependency container's
    network reachability — the POSITIVE CONTROL the recipe's real
    claims ride on: the dependency must actually answer on its service
    port before any arm claims the real image ran. A refused/unreachable
    dependency is ``unavailable`` (it demonstrated nothing), a timeout
    is ``inconclusive`` — never a green."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ProbeAttempt(
                OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED,
                None,
                f"{name} answered on {host}:{port} — the dependency is alive and reachable",
            )
    except socket.timeout:
        return ProbeAttempt(
            OUTCOME_INCONCLUSIVE,
            None,
            f"{name} did not answer on {host}:{port} within {timeout}s — inconclusive",
        )
    except OSError as error:
        return ProbeAttempt(
            OUTCOME_UNAVAILABLE,
            None,
            f"{name} is unreachable on {host}:{port} ({type(error).__name__}: {error}) —"
            " unreachability is not coverage",
        )


# ---------------------------------------------------------------------------
# The disposable dependency container (always stopped AND removed)
# ---------------------------------------------------------------------------


@dataclass
class DependencyContainer:
    """One disposable dependency container under the recipe's control.

    Started from the PINNED DIGEST (``image@digest``); the resolved
    digest is verified against the pin afterwards — a registry that
    served something else is a typed mismatch, never a silent
    substitution. ``stop()`` runs on EVERY path (the context manager
    guarantees it) and removes the container too.
    """

    runtime: str
    pinned: PinnedImage
    name: str
    port: int = 0
    resolved_digest: str = ""
    pull_status: str = ""
    commands: list[CommandRun] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)

    def __enter__(self) -> DependencyContainer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _run(
        self,
        name: str,
        argv: Sequence[str],
        *,
        timeout: float = 300.0,
        stdin_text: str | None = None,
    ) -> tuple[CommandRun, str]:
        return _run_receipted(self.commands, name, argv, timeout=timeout, stdin_text=stdin_text)

    def start(self, env: Mapping[str, str], ports: Sequence[int]) -> str:
        """Pull the pinned digest, run the container, map the ports.
        Returns '' on success or the honest status token."""
        digest_ref = f"{self.pinned.image}@{self.pinned.digest}"
        pull, _log = self._run(
            f"pull-{self.pinned.role}", [self.runtime, "pull", digest_ref], timeout=600.0
        )
        if pull.exit_code != 0:
            self.pull_status = ARM_IMAGE_UNAVAILABLE
            return ARM_IMAGE_UNAVAILABLE
        run_argv = [
            self.runtime,
            "run",
            "-d",
            "--name",
            self.name,
            *(arg for port in ports for arg in ("-p", f"127.0.0.1::{port}")),
            *(arg for key, value in env.items() for arg in ("-e", f"{key}={value}")),
            digest_ref,
        ]
        run, _log = self._run(f"run-{self.pinned.role}", run_argv, timeout=120.0)
        if run.exit_code != 0:
            return ARM_FAILED
        resolved = self._image_digest()
        if resolved and resolved != self.pinned.digest:
            self.resolved_digest = resolved
            self.stop()
            return ARM_DIGEST_MISMATCH
        self.resolved_digest = resolved or self.pinned.digest
        for port in ports:
            mapped = self.host_port(port)
            if mapped:
                self.port = mapped
                break
        return ""

    def _image_digest(self) -> str:
        inspect, log = self._run(
            f"digest-{self.pinned.role}",
            [self.runtime, "image", "inspect", f"{self.pinned.image}@{self.pinned.digest}"],
        )
        if inspect.exit_code != 0:
            return ""
        try:
            document = json.loads(
                log.split("--- stdout ---\n", 1)[1].rsplit("--- stderr ---", 1)[0]
            )
            for entry in document:
                for candidate in entry.get("RepoDigests", ()):
                    if candidate.endswith(f"@{self.pinned.digest}"):
                        return self.pinned.digest
                digest = str(entry.get("Digest") or "")
                if digest:
                    return digest
        except (json.JSONDecodeError, IndexError, KeyError):
            return ""
        return ""

    def host_port(self, container_port: int) -> int:
        port, _log = self._run(
            f"port-{self.pinned.role}-{container_port}",
            [self.runtime, "port", self.name, str(container_port)],
        )
        if port.exit_code != 0:
            return 0
        line = _log.split("--- stdout ---\n", 1)[1].rsplit("--- stderr ---", 1)[0].strip()
        if not line:
            return 0
        try:
            return int(line.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return 0

    def exec_sql(self, sql: str, *, db: str = "orders", fatal: bool = True) -> tuple[int, str]:
        """Run one SQL statement batch via psql INSIDE the container."""
        command, log = self._run(
            f"psql-{self.pinned.role}",
            [
                self.runtime,
                "exec",
                "-i",
                self.name,
                "psql",
                "-U",
                SYNTHETIC_DB_USER,
                "-d",
                db,
                "-v",
                "ON_ERROR_STOP=1",
                "-t",
                "-A",
                "-F",
                "|",
                *(("-q",) if fatal else ()),
            ],
            stdin_text=sql + "\n",
        )
        stdout = log.split("--- stdout ---\n", 1)[1].rsplit("--- stderr ---", 1)[0]
        return command.exit_code, stdout

    def exec_script(self, sql: str, *, db: str = "orders") -> tuple[int, str]:
        command, log = self._run(
            f"psql-script-{self.pinned.role}",
            [
                self.runtime,
                "exec",
                "-i",
                self.name,
                "psql",
                "-U",
                SYNTHETIC_DB_USER,
                "-d",
                db,
                "-v",
                "ON_ERROR_STOP=1",
                "-q",
            ],
            stdin_text=sql + "\n",
        )
        stdout = log.split("--- stdout ---\n", 1)[1].rsplit("--- stderr ---", 1)[0]
        return command.exit_code, stdout

    def wait_postgres_ready(self, db: str = "orders") -> bool:
        deadline = time.monotonic() + CONTAINER_READY_TIMEOUT
        while time.monotonic() < deadline:
            command, _log = self._run(
                "pg-isready",
                [self.runtime, "exec", self.name, "pg_isready", "-U", SYNTHETIC_DB_USER, "-d", db],
                timeout=30.0,
            )
            if command.exit_code == 0:
                return True
            time.sleep(1.0)
        return False

    def stop(self) -> None:
        """Stop AND remove the disposable container (both best-effort —
        the commands are receipted so a wedged cleanup is visible)."""
        for label, argv in (
            (f"stop-{self.pinned.role}", [self.runtime, "stop", "-t", "1", self.name]),
            (f"rm-{self.pinned.role}", [self.runtime, "rm", "-f", self.name]),
        ):
            try:
                self._run(label, argv, timeout=60.0)
            except OSError as error:  # pragma: no cover — runtime vanished mid-run
                self.log_lines.append(f"{label}: {type(error).__name__}: {error}")


# ---------------------------------------------------------------------------
# The TRX arm: build/test the required projects, reconcile the inventory
# ---------------------------------------------------------------------------


def write_identity_sidecar(
    results_dir: Path, report_name: str, *, candidate_id: str, bundle_digest: str
) -> None:
    """Write the #226 identity sidecar beside one report (the contract
    ``reconcile_reports`` binds a found report to THIS run with)."""
    sidecar = results_dir / (report_name + IDENTITY_SIDECAR_SUFFIX)
    sidecar.write_text(
        json.dumps({"candidate_id": candidate_id, "bundle_digest": bundle_digest}, sort_keys=True),
        encoding="utf-8",
    )


def expected_reports_for(
    manifest: DotnetRecipeManifest, *, candidate_id: str, bundle_digest: str
) -> ExpectedReports:
    """The frozen expected-report inventory rows for the recipe's
    required projects (one row per project, bound to the run identity)."""
    return ExpectedReports(
        reports=tuple(
            ExpectedReport(
                test_project=project.name,
                report_path=project.report,
                candidate_id=candidate_id,
                bundle_digest=bundle_digest,
            )
            for project in manifest.test_projects
        )
    )


def run_test_projects(
    manifest: DotnetRecipeManifest,
    fixture_root: Path,
    results_dir: Path,
    *,
    candidate_id: str,
    bundle_digest: str,
    commands: list[CommandRun],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """``dotnet test`` EVERY required project separately, each with its
    own TRX logger name + identity sidecar, then reconcile the whole
    inventory through the #226 machinery — a passing first project can
    never hide a failing, missing or stale second one, and an absent
    dotnet SDK is recorded, never faked."""
    sdk = _dotnet_sdk_version(commands, env=env)
    projects_document: list[dict[str, Any]] = []
    status = ARM_REAL
    if sdk is None:
        status = ARM_SDK_UNAVAILABLE
    else:
        results_dir.mkdir(parents=True, exist_ok=True)
        for project in manifest.test_projects:
            report_path = results_dir / project.report
            if report_path.exists():
                report_path.unlink()
            command, _log = _run_receipted(
                commands,
                f"dotnet-test-{project.name}",
                [
                    shutil.which("dotnet") or "dotnet",
                    "test",
                    str(fixture_root / project.project),
                    "--framework",
                    project.framework,
                    "--logger",
                    f"trx;LogFileName={project.report}",
                    "--results-directory",
                    str(results_dir),
                ],
                env=env,
                timeout=600.0,
                cwd=str(fixture_root),
            )
            report_exists = report_path.is_file()
            if report_exists:
                write_identity_sidecar(
                    results_dir,
                    project.report,
                    candidate_id=candidate_id,
                    bundle_digest=bundle_digest,
                )
            projects_document.append(
                {
                    "project": project.name,
                    "framework": project.framework,
                    "exit_code": command.exit_code,
                    "report": project.report,
                    "report_produced": report_exists,
                }
            )
    expected = expected_reports_for(
        manifest, candidate_id=candidate_id, bundle_digest=bundle_digest
    )
    reconciliation = reconcile_reports(expected, results_dir)
    return {
        "status": status,
        "sdk_observed": "" if sdk is None else sdk,
        "projects": projects_document,
        "report_reconciliation": reconciliation.to_document(),
        "report_inventory": freeze_report_inventory(
            expected, contract_digest=manifest.manifest_digest
        ),
    }


def _dotnet_sdk_version(
    commands: list[CommandRun], env: Mapping[str, str] | None = None
) -> str | None:
    """The installed dotnet SDK version, or None when no SDK exists."""
    resolved = shutil.which("dotnet")
    if not resolved:
        return None
    command, log = _run_receipted(
        commands, "dotnet-version", [resolved, "--version"], env=env, timeout=60.0
    )
    if command.exit_code != 0:
        return None
    stdout = log.split("--- stdout ---\n", 1)[1].rsplit("--- stderr ---", 1)[0]
    version = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
    return version or None


# ---------------------------------------------------------------------------
# The migration arm (postgres real / sqlite reference)
# ---------------------------------------------------------------------------


def _normalize_cell(value: str) -> str:
    """Canonicalize one fetched cell for the fingerprint (numeric forms
    like 10 / 10.0 / 10.00 collapse to one identity)."""
    text = value.strip()
    try:
        return str(Decimal(text))
    except InvalidOperation:
        return text


def _fingerprint_of(rows: Sequence[Sequence[str]]) -> dict[str, Any]:
    """The preservation fingerprint: row count + the sha256 over the
    sorted, cell-normalized joined rows — an empty table is a count of
    zero and a digest over nothing (never a silent wildcard)."""
    joined = "\n".join("|".join(_normalize_cell(str(cell)) for cell in row) for row in rows)
    return {
        "rows": len(rows),
        "rows_sha256": hashlib.sha256(joined.encode("utf-8")).hexdigest(),
    }


def _migration_arm_document(
    *,
    status: str,
    coverage: str,
    dialect: str,
    baseline: dict[str, Any],
    after: dict[str, Any],
    constraint_pre_upgrade: str,
    constraint_post_upgrade: str,
    region_backfill_ok: bool,
    preserved: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "coverage": coverage,
        "dialect": dialect,
        "baseline_fingerprint": baseline,
        "after_fingerprint": after,
        "preserved": preserved,
        "constraint_probe": {
            "pre_upgrade_negative_insert": constraint_pre_upgrade,
            "post_upgrade_negative_insert": constraint_post_upgrade,
        },
        "region_backfill_complete": region_backfill_ok,
        "detail": detail,
    }


def run_migration_arm_sqlite(
    manifest: DotnetRecipeManifest, fixture_root: Path, db_path: Path
) -> dict[str, Any]:
    """The migration arm over sqlite — the labeled REFERENCE dialect
    (same SQL pair, same fingerprints, no real postgres claim)."""
    baseline_sql = (fixture_root / manifest.migration_baseline).read_text(encoding="utf-8")
    upgrade_sql = (fixture_root / manifest.migration_upgrade_sqlite).read_text(encoding="utf-8")
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(baseline_sql)
        connection.executemany(
            "INSERT INTO orders (id, total, created_at) VALUES (?, ?, ?)",
            seed_rows_document(manifest.seed_rows),
        )
        baseline = _fingerprint_of(
            connection.execute("SELECT id, total, created_at FROM orders ORDER BY id").fetchall()
        )
        # the constraint is ABSENT on the baseline — the negative insert lands
        constraint_pre = "accepted"
        connection.execute(
            "INSERT INTO orders (id, total, created_at) VALUES ('neg-probe', -1, 'x')"
        )
        connection.execute("DELETE FROM orders WHERE id = 'neg-probe'")
        connection.executescript(upgrade_sql)
        after = _fingerprint_of(
            connection.execute("SELECT id, total, created_at FROM orders ORDER BY id").fetchall()
        )
        constraint_post = "rejected"
        try:
            connection.execute(
                "INSERT INTO orders (id, total, created_at, region)"
                " VALUES ('neg-probe-2', -1, 'x', 'u')"
            )
            constraint_post = "accepted"
        except sqlite3.IntegrityError:
            pass
        region_backfill_ok = bool(
            connection.execute("SELECT count(*) FROM orders WHERE region IS NULL").fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    preserved = baseline == after
    ok = preserved and constraint_post == "rejected" and region_backfill_ok
    return _migration_arm_document(
        status=ARM_REFERENCE,
        coverage=REFERENCE_COVERAGE,
        dialect="sqlite",
        baseline=baseline,
        after=after,
        constraint_pre_upgrade=constraint_pre,
        constraint_post_upgrade=constraint_post,
        region_backfill_ok=region_backfill_ok,
        preserved=preserved,
        detail=(
            "the sqlite reference upgrade preserved every seeded row (counts and sha256"
            " fingerprints equal) and enforces the new constraint"
            if ok
            else "the sqlite reference upgrade did NOT satisfy preservation + constraint"
            f" (preserved={preserved}, constraint_post={constraint_post},"
            f" backfill={region_backfill_ok})"
        ),
    )


def run_migration_arm_postgres(
    manifest: DotnetRecipeManifest, fixture_root: Path, container: DependencyContainer
) -> dict[str, Any]:
    """The migration arm over the REAL pinned postgres container: the
    fixture's N-1 baseline, the seeded canary rows, the N-1 -> N
    upgrade, preservation by fingerprint and the constraint probe —
    psql runs INSIDE the container (no driver, no host state)."""
    baseline_sql = (fixture_root / manifest.migration_baseline).read_text(encoding="utf-8")
    upgrade_sql = (fixture_root / manifest.migration_upgrade_postgres).read_text(encoding="utf-8")
    seed = seed_rows_document(manifest.seed_rows)
    values = ", ".join(f"('{row_id}', {total}, '{created}')" for row_id, total, created in seed)
    select = "SELECT id, total, created_at FROM orders ORDER BY id;"

    exit_code, _out = container.exec_script(baseline_sql)
    if exit_code != 0:
        return _migration_arm_document(
            status=ARM_FAILED,
            coverage=REFERENCE_COVERAGE,
            dialect="postgres",
            baseline={},
            after={},
            constraint_pre_upgrade="not-probed",
            constraint_post_upgrade="not-probed",
            region_backfill_ok=False,
            preserved=False,
            detail="the baseline schema could not be applied inside the container",
        )
    exit_code, _out = container.exec_script(f"INSERT INTO orders VALUES {values};")
    if exit_code != 0:
        return _migration_arm_document(
            status=ARM_FAILED,
            coverage=REFERENCE_COVERAGE,
            dialect="postgres",
            baseline={},
            after={},
            constraint_pre_upgrade="not-probed",
            constraint_post_upgrade="not-probed",
            region_backfill_ok=False,
            preserved=False,
            detail="the canary rows could not be seeded",
        )
    _exit, baseline_rows = container.exec_sql(select)
    baseline = _fingerprint_of(
        [tuple(line.split("|")) for line in baseline_rows.strip().splitlines() if line.strip()]
    )
    # the constraint is ABSENT pre-upgrade: the negative insert lands, then is cleaned.
    constraint_pre = "accepted"
    exit_code, _out = container.exec_script(
        "INSERT INTO orders (id, total, created_at) VALUES ('neg-probe', -1, 'x');"
        "DELETE FROM orders WHERE id = 'neg-probe';"
    )
    if exit_code != 0:
        constraint_pre = "rejected"
    exit_code, _out = container.exec_script(upgrade_sql)
    if exit_code != 0:
        return _migration_arm_document(
            status=ARM_FAILED,
            coverage=REFERENCE_COVERAGE,
            dialect="postgres",
            baseline=baseline,
            after={},
            constraint_pre_upgrade=constraint_pre,
            constraint_post_upgrade="not-probed",
            region_backfill_ok=False,
            preserved=False,
            detail="the N-1 -> N upgrade failed inside the container",
        )
    _exit, after_rows = container.exec_sql(select)
    after = _fingerprint_of(
        [tuple(line.split("|")) for line in after_rows.strip().splitlines() if line.strip()]
    )
    exit_code, _out = container.exec_script(
        "INSERT INTO orders (id, total, created_at, region) VALUES ('neg-probe-2', -1, 'x', 'u');"
    )
    constraint_post = "rejected" if exit_code != 0 else "accepted"
    exit_code, region_rows = container.exec_sql("SELECT count(*) FROM orders WHERE region IS NULL;")
    region_backfill_ok = exit_code == 0 and region_rows.strip() == "0"
    preserved = baseline == after
    ok = preserved and constraint_post == "rejected" and region_backfill_ok
    return _migration_arm_document(
        status=ARM_REAL,
        coverage=REAL_COVERAGE_LABEL,
        dialect="postgres",
        baseline=baseline,
        after=after,
        constraint_pre_upgrade=constraint_pre,
        constraint_post_upgrade=constraint_post,
        region_backfill_ok=region_backfill_ok,
        preserved=preserved,
        detail=(
            f"postgres 16 upgraded the seeded baseline in the real container: {baseline['rows']}"
            " rows preserved (counts and sha256 fingerprints equal) and the new CHECK"
            " constraint rejects what the baseline accepted"
            if ok
            else "the real postgres upgrade did NOT satisfy preservation + constraint"
            f" (preserved={preserved}, constraint_post={constraint_post},"
            f" backfill={region_backfill_ok})"
        ),
    )


# ---------------------------------------------------------------------------
# The dedup consumer (exactly-once side effects, asserted from the DB)
# ---------------------------------------------------------------------------


class DedupConsumer:
    """The reference consumer the redelivery harness drives: the
    durable side effect is COMMITTED to sqlite BEFORE the harness
    acks, and a duplicate/redelivery finds the row and applies
    NOTHING — the DB is the truth the arm asserts from."""

    def __init__(self, db_path: Path) -> None:
        self._connection = sqlite3.connect(db_path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS projection_effects ("
            " message_id TEXT PRIMARY KEY,"
            " effect TEXT NOT NULL,"
            " deliveries_seen INTEGER NOT NULL DEFAULT 0)"
        )
        self._connection.commit()

    def apply(self, message: Mapping[str, Any], *, redelivered: bool, acked: bool) -> str:
        """Apply one delivery; ``acked`` False means the harness will
        crash the channel BEFORE the ack (the effect is already
        committed — the invariant the arm attacks)."""
        message_id = str(message["id"])
        effect = (
            f"projection:{message_id}:total={message['total']}"
            f":region={message.get('region') or 'unknown'}"
        )
        inserted = self._connection.execute(
            "INSERT OR IGNORE INTO projection_effects (message_id, effect, deliveries_seen)"
            " VALUES (?, ?, 0)",
            (message_id, effect),
        )
        self._connection.execute(
            "UPDATE projection_effects SET deliveries_seen = deliveries_seen + 1"
            " WHERE message_id = ?",
            (message_id,),
        )
        self._connection.commit()
        return "applied" if inserted.rowcount == 1 else "duplicate"

    def effects(self) -> dict[str, dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT message_id, effect, deliveries_seen FROM projection_effects ORDER BY message_id"
        ).fetchall()
        return {
            str(row[0]): {"effect": str(row[1]), "deliveries_seen": int(row[2])} for row in rows
        }

    def close(self) -> None:
        self._connection.close()


def _exactly_once(effects: Mapping[str, dict[str, Any]], messages: int) -> bool:
    return len(effects) == messages and all(
        entry["deliveries_seen"] >= 2 for entry in effects.values()
    )


# ---------------------------------------------------------------------------
# The redelivery arm: real RabbitMQ via a minimal AMQP 0-9-1 client
# ---------------------------------------------------------------------------


class _AmqpReader:
    """The AMQP 0-9-1 argument reader (big-endian primitives)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u8(self) -> int:
        value = self.data[self.pos]
        self.pos += 1
        return value

    def u16(self) -> int:
        value = struct.unpack_from(">H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def u32(self) -> int:
        value = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def u64(self) -> int:
        value = struct.unpack_from(">Q", self.data, self.pos)[0]
        self.pos += 8
        return value

    def shortstr(self) -> str:
        length = self.u8()
        value = self.data[self.pos : self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return value

    def longstr(self) -> str:
        length = self.u32()
        value = self.data[self.pos : self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return value

    def skip_table(self) -> None:
        """Skip a field table as an opaque length-prefixed blob (server
        properties are never interpreted — the client only writes the
        types it knows)."""
        length = self.u32()
        self.pos += length


class _AmqpWriter:
    """The AMQP 0-9-1 argument writer."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def u8(self, value: int) -> None:
        self.buf += struct.pack(">B", value)

    def u16(self, value: int) -> None:
        self.buf += struct.pack(">H", value)

    def u32(self, value: int) -> None:
        self.buf += struct.pack(">I", value)

    def u64(self, value: int) -> None:
        self.buf += struct.pack(">Q", value)

    def method(self, class_id: int, method_id: int) -> None:
        self.u16(class_id)
        self.u16(method_id)

    def shortstr(self, value: str) -> None:
        raw = value.encode("utf-8")
        self.u8(len(raw))
        self.buf += raw

    def longstr(self, value: str) -> None:
        raw = value.encode("utf-8")
        self.u32(len(raw))
        self.buf += raw

    def table(self, entries: Mapping[str, str | int | bool]) -> None:
        inner = _AmqpWriter()
        for name, value in entries.items():
            inner.shortstr(name)
            if isinstance(value, bool):
                inner.u8(ord("t"))
                inner.u8(1 if value else 0)
            elif isinstance(value, int):
                inner.u8(ord("I"))
                inner.buf += struct.pack(">i", value)
            else:
                inner.u8(ord("S"))
                inner.longstr(str(value))
        self.u32(len(inner.buf))
        self.buf += inner.buf


_FRAME_METHOD = 1
_FRAME_HEADER = 2
_FRAME_BODY = 3
_FRAME_HEARTBEAT = 8


def _frame(frame_type: int, channel: int, payload: bytes) -> bytes:
    return struct.pack(">BHI", frame_type, channel, len(payload)) + payload + b"\xce"


class AmqpClient:
    """A minimal AMQP 0-9-1 client for EXACTLY the redelivery
    scenario: connection handshake (PLAIN), channel open, durable
    queue declare, persistent publish, ``basic.get`` without ack,
    ``basic.ack``, channel/connection close. Deliberately no
    consumer-registration, no confirms, no TLS — the small protocol
    subset the arm needs against the REAL broker version, stdlib
    only."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        user: str = SYNTHETIC_BROKER_USER,
        password: str = SYNTHETIC_BROKER_PASSWORD,
        vhost: str = "/",
    ) -> None:
        self.sock = socket.create_connection((host, port), timeout=10.0)
        self._user = user
        self._password = password
        self._vhost = vhost
        self._buffer = b""
        self.frame_max = 131072

    # -- frame plumbing ---------------------------------------------------

    def _send(self, frame_type: int, channel: int, payload: bytes) -> None:
        self.sock.sendall(_frame(frame_type, channel, payload))

    def _recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("the broker closed the connection")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def _recv_frame(self) -> tuple[int, int, bytes]:
        head = self._recv_exact(7)
        frame_type, channel, size = struct.unpack(">BHI", head)
        payload = self._recv_exact(size + 1)
        if payload[-1:] != b"\xce":
            raise ConnectionError("malformed frame end")
        return frame_type, channel, payload[:-1]

    def _expect_method(
        self, channel: int, *wanted: tuple[int, int]
    ) -> tuple[int, int, _AmqpReader]:
        while True:
            frame_type, frame_channel, payload = self._recv_frame()
            if frame_type == _FRAME_HEARTBEAT:
                continue
            if frame_type != _FRAME_METHOD:
                raise ConnectionError(f"expected a method frame, got type {frame_type}")
            reader = _AmqpReader(payload)
            class_id, method_id = reader.u16(), reader.u16()
            if class_id == 10 and method_id in (50, 51):  # connection.blocked/unblocked
                continue
            if class_id == 10 and method_id == 60:
                close = _AmqpReader(payload)
                close.u16()
                close.u16()
                code = close.u16()
                text = close.shortstr()
                raise ConnectionError(f"the broker closed the connection: {code} {text}")
            if frame_channel == channel and (class_id, method_id) in wanted:
                return class_id, method_id, reader
            raise ConnectionError(f"unexpected method {class_id}/{method_id}")

    # -- connection / channel ----------------------------------------------

    def open(self) -> None:
        self.sock.sendall(b"AMQP\x00\x00\x09\x01")
        _c, _m, start = self._expect_method(0, (10, 10))
        start.u8()
        start.u8()
        start.skip_table()
        start.longstr()
        start.longstr()
        writer = _AmqpWriter()
        writer.method(10, 11)
        writer.table({"product": "forge-dotnet-recipe", "version": "1", "platform": "forge"})
        writer.shortstr("PLAIN")
        writer.longstr(f"\x00{self._user}\x00{self._password}")
        writer.shortstr("en_US")
        self._send(_FRAME_METHOD, 0, bytes(writer.buf))
        _c, _m, tune = self._expect_method(0, (10, 30))
        channel_max, frame_max, heartbeat = tune.u16(), tune.u32(), tune.u16()
        self.frame_max = frame_max or self.frame_max
        writer = _AmqpWriter()
        writer.method(10, 31)
        writer.u16(channel_max)
        writer.u32(self.frame_max)
        writer.u16(heartbeat)
        self._send(_FRAME_METHOD, 0, bytes(writer.buf))
        writer = _AmqpWriter()
        writer.method(10, 40)
        writer.shortstr(self._vhost)
        writer.shortstr("")
        writer.u8(0)
        self._send(_FRAME_METHOD, 0, bytes(writer.buf))
        self._expect_method(0, (10, 41))

    def open_channel(self, channel: int) -> None:
        writer = _AmqpWriter()
        writer.method(20, 10)
        writer.shortstr("")
        self._send(_FRAME_METHOD, channel, bytes(writer.buf))
        self._expect_method(channel, (20, 11))

    def declare_queue(self, channel: int, queue: str) -> int:
        """Declare a durable queue; returns the message count it held
        (a non-zero count on a fresh run-name would mean a collision)."""
        writer = _AmqpWriter()
        writer.method(50, 10)
        writer.u16(0)  # ticket
        writer.shortstr(queue)
        writer.u8(1 << 1)  # durable
        writer.table({})
        self._send(_FRAME_METHOD, channel, bytes(writer.buf))
        _c, _m, reader = self._expect_method(channel, (50, 11))
        reader.shortstr()  # queue name echoed
        message_count = reader.u32()
        reader.u32()  # consumer count
        return message_count

    # -- publish / get / ack -------------------------------------------------

    def publish(self, channel: int, routing_key: str, body: bytes) -> None:
        writer = _AmqpWriter()
        writer.method(60, 40)
        writer.u16(0)  # ticket
        writer.shortstr("")  # default exchange
        writer.shortstr(routing_key)
        writer.u8(0)  # not mandatory, not immediate
        self._send(_FRAME_METHOD, channel, bytes(writer.buf))
        properties = _AmqpWriter()
        properties.u16(0x1000)  # delivery-mode flag (bit 12 — the 4th basic property)
        properties.u8(2)  # persistent
        header = struct.pack(">HHQ", 60, 0, len(body)) + bytes(properties.buf)
        self._send(_FRAME_HEADER, channel, header)
        for offset in range(0, len(body), self.frame_max - 8):
            self._send(_FRAME_BODY, channel, body[offset : offset + self.frame_max - 8])

    def get(
        self, channel: int, queue: str, *, timeout: float = 15.0
    ) -> tuple[int, bool, bytes] | None:
        """``basic.get`` with no-ack=False: returns
        ``(delivery_tag, redelivered, body)`` or None when the queue is
        empty. The caller acks SEPARATELY — the crash simulation
        depends on getting without acking."""
        self.sock.settimeout(timeout)
        writer = _AmqpWriter()
        writer.method(60, 70)
        writer.u16(0)  # ticket
        writer.shortstr(queue)
        writer.u8(0)  # no-ack = False
        self._send(_FRAME_METHOD, channel, bytes(writer.buf))
        while True:
            frame_type, _ch, payload = self._recv_frame()
            if frame_type == _FRAME_HEARTBEAT:
                continue
            if frame_type != _FRAME_METHOD:
                raise ConnectionError(f"expected a method frame, got type {frame_type}")
            reader = _AmqpReader(payload)
            class_id, method_id = reader.u16(), reader.u16()
            if class_id == 10 and method_id in (50, 51):
                continue
            if (class_id, method_id) == (60, 72):  # basic.get-empty
                return None
            if (class_id, method_id) != (60, 71):  # basic.get-ok
                raise ConnectionError(f"unexpected method {class_id}/{method_id} in get")
            delivery_tag = reader.u64()
            redelivered = bool(reader.u8() & 1)
            reader.shortstr()  # exchange
            reader.shortstr()  # routing key
            reader.u32()  # message count
            body = b""
            while True:
                inner_type, _ic, inner_payload = self._recv_frame()
                if inner_type != _FRAME_HEADER:
                    continue
                header = _AmqpReader(inner_payload)
                header.u16()
                header.u16()
                size = header.u64()
                while len(body) < size:
                    body_type, _bc, body_payload = self._recv_frame()
                    if body_type == _FRAME_BODY:
                        body += body_payload
                return delivery_tag, redelivered, body

    def ack(self, channel: int, delivery_tag: int) -> None:
        writer = _AmqpWriter()
        writer.method(60, 80)
        writer.u64(delivery_tag)
        writer.u8(0)
        self._send(_FRAME_METHOD, channel, bytes(writer.buf))

    def close_channel(self, channel: int) -> None:
        """Close the channel WITHOUT acking (the crash simulation) —
        the broker requeues the unacked delivery."""
        writer = _AmqpWriter()
        writer.method(20, 40)
        writer.u16(0)
        writer.shortstr("")
        writer.u16(0)
        writer.u16(0)
        self._send(_FRAME_METHOD, channel, bytes(writer.buf))
        try:
            self._expect_method(channel, (20, 41), (20, 40))
        except (ConnectionError, OSError, TimeoutError):
            pass

    def close(self) -> None:
        writer = _AmqpWriter()
        writer.method(10, 60)
        writer.u16(0)
        writer.shortstr("")
        writer.u16(0)
        writer.u16(0)
        self._send(_FRAME_METHOD, 0, bytes(writer.buf))
        try:
            self._expect_method(0, (10, 61))
        except (ConnectionError, OSError, TimeoutError):
            pass
        self.sock.close()


def run_redelivery_arm_real(
    manifest: DotnetRecipeManifest,
    *,
    host: str,
    port: int,
    db_path: Path,
    candidate_id: str,
    image_digest: str,
) -> dict[str, Any]:
    """The REAL broker redelivery scenario against the pinned
    RabbitMQ: a durable queue (unique per run — a leftover from an
    earlier run can never answer), every message published
    ``duplicates_per_message`` times (the duplicate publish), one
    delivery consumed WITHOUT ack whose channel is crashed (the
    broker's redelivery observed with ``redelivered=true``), the
    consumer's exactly-once side effects asserted from the database."""
    messages = [
        {"id": f"order-{index:04d}", "total": str(10 * index), "region": "emea"}
        for index in range(1, manifest.redelivery_messages + 1)
    ]
    queue = f"{manifest.broker_queue_prefix}.{candidate_id}"
    client = AmqpClient(host, port)
    consumer = DedupConsumer(db_path)
    redeliveries_observed = 0
    deliveries = 0
    first_channel_redelivery: bool | None = None
    detail = ""
    try:
        client.open()
        client.open_channel(1)
        client.declare_queue(1, queue)
        for message in messages:
            body = json.dumps(message, sort_keys=True)
            for _ in range(manifest.duplicates_per_message):
                client.publish(1, queue, body.encode("utf-8"))
        # consume the FIRST message without ack, crash the channel after
        # the state commit (the effect is committed before any ack)
        got = client.get(1, queue)
        if got is None:
            raise ConnectionError("the broker delivered nothing after the publishes")
        tag, _redelivered, body = got
        consumer.apply(json.loads(body), redelivered=False, acked=False)
        client.close_channel(1)
        client.open_channel(2)
        got = client.get(2, queue)
        if got is None:
            raise ConnectionError("the broker did not redeliver the unacked message")
        tag2, redelivered, body2 = got
        first_channel_redelivery = redelivered
        if redelivered:
            redeliveries_observed += 1
        consumer.apply(json.loads(body2), redelivered=redelivered, acked=True)
        client.ack(2, tag2)
        deliveries += 2
        # drain the remaining duplicate-publish deliveries, acking each
        while True:
            got = client.get(2, queue)
            if got is None:
                break
            drain_tag, drain_redelivered, drain_body = got
            deliveries += 1
            if drain_redelivered:
                redeliveries_observed += 1
            consumer.apply(json.loads(drain_body), redelivered=drain_redelivered, acked=True)
            client.ack(2, drain_tag)
        client.close_channel(2)
        client.close()
    except (ConnectionError, OSError, json.JSONDecodeError) as error:
        detail = f"the real-broker redelivery errored: {type(error).__name__}: {error}"
    finally:
        try:
            consumer.close()
        except sqlite3.Error:  # pragma: no cover — already closed
            pass
    effects = {}
    try:
        effects = DedupConsumer(db_path).effects()
        DedupConsumer(db_path).close()
    except sqlite3.Error:  # pragma: no cover
        pass
    exactly_once = _exactly_once(effects, len(messages))
    ok = bool(first_channel_redelivery) and exactly_once and not detail
    return {
        "status": ARM_REAL if ok else ARM_FAILED,
        "coverage": REAL_COVERAGE_LABEL,
        "broker": f"rabbitmq:{image_digest[:19]}",
        "queue": queue,
        "messages": len(messages),
        "duplicate_publishes_per_message": manifest.duplicates_per_message,
        "deliveries_observed": deliveries,
        "redeliveries_observed": redeliveries_observed,
        "crash_before_ack_redelivered": first_channel_redelivery,
        "effects_per_message": effects,
        "exactly_once_all": exactly_once,
        "detail": detail
        or (
            f"{len(messages)} messages, each published {manifest.duplicates_per_message}x on a"
            f" real RabbitMQ queue; the unacked delivery came back redelivered"
            f" ({first_channel_redelivery}) and every message produced exactly one durable"
            f" side effect ({deliveries} deliveries observed)"
            if ok
            else "the real-broker redelivery did NOT observe the agreed effect invariant"
            f" (redelivered={first_channel_redelivery}, exactly_once={exactly_once})"
        ),
    }


def run_redelivery_arm_reference(manifest: DotnetRecipeManifest, db_path: Path) -> dict[str, Any]:
    """The labeled REFERENCE redelivery harness (no broker claim): the
    same messages, the same duplicate-delivery injection and the same
    crash-before-ack replay, driven in-process against the same dedup
    consumer — the deterministic stand-in when no real image ran."""
    messages = [
        {"id": f"order-{index:04d}", "total": str(10 * index), "region": "emea"}
        for index in range(1, manifest.redelivery_messages + 1)
    ]
    consumer = DedupConsumer(db_path)
    deliveries = 0
    redeliveries = 0
    try:
        for message in messages:
            consumer.apply(message, redelivered=False, acked=False)  # commit
            deliveries += 1
            # crash before ack: the broker would redeliver — the harness
            # replays the delivery instead
            consumer.apply(message, redelivered=True, acked=True)
            deliveries += 1
            redeliveries += 1
            for _ in range(manifest.duplicates_per_message - 1):
                consumer.apply(message, redelivered=False, acked=True)
                deliveries += 1
    finally:
        consumer.close()
    effects = DedupConsumer(db_path).effects()
    DedupConsumer(db_path).close()
    exactly_once = _exactly_once(effects, len(messages))
    return {
        "status": ARM_REFERENCE,
        "coverage": REFERENCE_COVERAGE,
        "broker": "reference-in-process",
        "queue": "",
        "messages": len(messages),
        "duplicate_publishes_per_message": manifest.duplicates_per_message,
        "deliveries_observed": deliveries,
        "redeliveries_observed": redeliveries,
        "crash_before_ack_redelivered": True,
        "effects_per_message": effects,
        "exactly_once_all": exactly_once,
        "detail": (
            "the REFERENCE redelivery harness replayed every delivery (duplicates + the"
            " crash-before-ack repeat) and observed exactly one durable side effect per"
            " message — reference coverage only, no real-broker claim"
            if exactly_once
            else "the reference harness failed its own effect invariant"
        ),
    }


# ---------------------------------------------------------------------------
# The recipe runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DotnetRecipeRun:
    """One recipe execution's honest record (never a verdict — the
    document is EVIDENCE a verdict can bind to; it grants neither
    merge nor deployment)."""

    manifest: DotnetRecipeManifest
    candidate_id: str
    workspace: str
    sdk_observed: str
    document: dict[str, Any]

    @property
    def verifies(self) -> bool:
        """Every executed arm's assertions held (real or labeled
        reference) AND the required TRX inventory is green."""
        return bool(self.document.get("verifies"))

    @property
    def real_coverage_complete(self) -> bool:
        """The stronger claim: the build/test arm AND both dependency
        arms ran against the REAL pinned artifacts/images (no reference
        stand-in, no unavailable arm)."""
        return bool(self.document.get("real_coverage_complete"))

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(str(line) for line in self.document.get("problems", ()))

    def to_document(self) -> dict[str, Any]:
        return self.document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> DotnetRecipeRun:
        manifest = DotnetRecipeManifest.from_document(document.get("manifest", {}))
        return cls(
            manifest=manifest,
            candidate_id=str(document.get("candidate_id", "")),
            workspace=str(document.get("workspace", "")),
            sdk_observed=str(document.get("sdk", {}).get("observed", "")),
            document=dict(document),
        )


def _wait_amqp_ready(container: DependencyContainer) -> tuple[ProbeAttempt, bool]:
    """Wait for the rabbitmq container to accept a REAL AMQP handshake
    (a TCP accept is not an application-ready broker). The LAST
    five-outcome reachability probe is returned as the recorded
    positive-control evidence."""
    attempt = dependency_control_probe("orders-bus", "127.0.0.1", container.port)
    deadline = time.monotonic() + CONTAINER_READY_TIMEOUT
    while time.monotonic() < deadline:
        attempt = dependency_control_probe("orders-bus", "127.0.0.1", container.port)
        if attempt.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED:
            try:
                client = AmqpClient("127.0.0.1", container.port)
                client.open()
                client.close()
                return attempt, True
            except (ConnectionError, OSError):
                pass  # TCP answered, the app is still booting — keep waiting
        time.sleep(1.0)
    return attempt, False


def run_recipe(
    manifest: DotnetRecipeManifest | None = None,
    *,
    workspace: Path | None = None,
    root: Path | None = None,
    candidate_id: str = "",
    allow_containers: bool = True,
    enforce_isolation: bool = True,
    extra_env: Mapping[str, str] | None = None,
) -> DotnetRecipeRun:
    """Execute the pinned recipe honestly, arm by arm.

    The order: the SDK probe (a missing SDK is recorded, never faked),
    the pinned dependency containers (disposable — stopped AND removed
    on every path — with the #308 five-outcome reachability probes as
    the positive control), the TRX build/test arm over the frozen
    fixture source (reconciled through the #226 inventory), the
    migration arm (real postgres when the container ran, the labeled
    sqlite reference otherwise) and the redelivery arm (real RabbitMQ
    when the container ran, the labeled in-process reference
    otherwise). The tool invocations run under the executor's own
    isolation profile (allowlist scrub + narrowed PATH + recorded
    extras) and the run document binds its digest.
    """
    manifest = manifest or load_pinned_manifest()
    root = root or _repo_root()
    fixture_root = root / manifest.fixture_root
    random_id = hashlib.sha256(
        f"{time.time_ns()}:{manifest.manifest_digest}".encode("utf-8")
    ).hexdigest()[:12]
    candidate_id = candidate_id or f"dotnet-recipe-{random_id}"
    workspace = workspace or Path(tempfile_default()) / f"forge-dotnet-recipe-{random_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    results_dir = workspace / "results"
    commands: list[CommandRun] = []
    problems: list[str] = []
    degradations: list[str] = []

    # -- the executor isolation binding (composed, same classes) --------
    tool_env = recipe_scrubbed_env(extra_env) if enforce_isolation else dict(os.environ)
    profile = recipe_enforcement_profile(extra_env_keys=(extra_env or {}).keys())
    if enforce_isolation:
        narrowed = narrowed_path_entries(extra_dirs=recipe_tool_dirs())
        if narrowed:
            tool_env["PATH"] = os.pathsep.join(narrowed)

    # -- the SDK probe ------------------------------------------------------
    sdk_observed = _dotnet_sdk_version(commands, env=tool_env if enforce_isolation else None)
    sdk_matches = sdk_observed == manifest.sdk_version
    if sdk_observed is None:
        problems.append(
            "no dotnet SDK on PATH — the build/test arm cannot run (recorded, never faked)"
        )
    elif not sdk_matches:
        problems.append(
            f"the installed SDK {sdk_observed} is not the pinned {manifest.sdk_version} —"
            " the run is evidence only under the observed SDK"
        )

    # -- the dependency containers ------------------------------------------
    runtime = container_runtime_command() if allow_containers else None
    if allow_containers and runtime is None:
        degradations.append(
            "no container runtime (docker/podman) — the dependency arms run the labeled"
            " reference instead"
        )
    containers_document: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    started: list[DependencyContainer] = []
    postgres: DependencyContainer | None = None
    rabbitmq: DependencyContainer | None = None
    image_digests: dict[str, str] = {}
    if runtime is not None:
        for pinned_role, env, ports in (
            (
                "orders-db",
                {
                    "POSTGRES_USER": SYNTHETIC_DB_USER,
                    "POSTGRES_PASSWORD": SYNTHETIC_DB_PASSWORD,
                    "POSTGRES_DB": "orders",
                },
                (5432,),
            ),
            (
                "orders-bus",
                {
                    "RABBITMQ_DEFAULT_USER": SYNTHETIC_BROKER_USER,
                    "RABBITMQ_DEFAULT_PASS": SYNTHETIC_BROKER_PASSWORD,
                },
                (5672,),
            ),
        ):
            pinned = manifest.image_for_role(pinned_role)
            if pinned is None:  # pragma: no cover — the manifest pins both
                continue
            container = DependencyContainer(
                runtime=runtime,
                pinned=pinned,
                name=f"forge-dotnet-recipe-{pinned.role}-{random_id}",
                commands=commands,
            )
            started.append(container)
            status = container.start(env, ports)
            containers_document.append(
                {
                    "role": pinned.role,
                    "image": pinned.image,
                    "pinned_digest": pinned.digest,
                    "resolved_digest": container.resolved_digest,
                    "container_name": container.name,
                    "status": ARM_REAL if status == "" else status,
                }
            )
            if status == ARM_DIGEST_MISMATCH:
                problems.append(
                    f"the registry served {pinned.image} at a digest that is not the pin"
                    f" {pinned.digest} — refusing to claim the pinned image ran"
                )
            elif status == ARM_IMAGE_UNAVAILABLE:
                degradations.append(
                    f"the pinned image {pinned.image}@{pinned.digest} could not be pulled —"
                    " the arm records image-unavailable"
                )
            elif status != "":
                problems.append(f"the {pinned.role} container failed to start ({status})")
            else:
                image_digests[pinned.role] = container.resolved_digest
            if pinned.role == "orders-db" and status == "":
                if not container.wait_postgres_ready():
                    containers_document[-1]["status"] = ARM_FAILED
                    problems.append("the postgres container never became ready")
                else:
                    postgres = container
            if pinned.role == "orders-bus" and status == "":
                attempt, ready = _wait_amqp_ready(container)
                probes.append(attempt.as_document())
                if not ready:
                    containers_document[-1]["status"] = ARM_FAILED
                    problems.append("the rabbitmq container never answered on its AMQP port")
                else:
                    rabbitmq = container
        if postgres is not None:
            probes.append(
                dependency_control_probe("orders-db", "127.0.0.1", postgres.port).as_document()
            )

    # -- the TRX arm ---------------------------------------------------------
    bundle_digest = fixture_source_digest(fixture_root)
    build_env = tool_env if enforce_isolation else None
    trx_arm = run_test_projects(
        manifest,
        fixture_root,
        results_dir,
        candidate_id=candidate_id,
        bundle_digest=bundle_digest,
        commands=commands,
        env=build_env,
    )
    trx_arm["test_bundle_digest"] = bundle_digest
    trx_arm["sdk_pinned"] = manifest.sdk_version
    trx_arm["sdk_matches_pin"] = sdk_matches
    if trx_arm["status"] == ARM_SDK_UNAVAILABLE:
        trx_arm["detail"] = (
            "dotnet-sdk-unavailable — the build/test arm did not run and every required"
            " report is missing_report (never a synthetic green)"
        )
    for problem in trx_arm["report_reconciliation"]["problems"]:
        problems.append(problem)

    # -- the migration arm ----------------------------------------------------
    if postgres is not None:
        migration_arm = run_migration_arm_postgres(manifest, fixture_root, postgres)
    else:
        migration_arm = run_migration_arm_sqlite(
            manifest, fixture_root, workspace / "migration-reference.db"
        )
        pg_status = next(
            (entry["status"] for entry in containers_document if entry["role"] == "orders-db"),
            "no-runtime",
        )
        degradations.append(
            f"the migration arm ran the labeled sqlite REFERENCE dialect (postgres"
            f" arm status: {pg_status})"
        )
    if not (
        migration_arm["preserved"]
        and migration_arm["constraint_probe"]["post_upgrade_negative_insert"] == "rejected"
    ):
        problems.append(f"the migration arm failed: {migration_arm['detail']}")

    # -- the redelivery arm ----------------------------------------------------
    if rabbitmq is not None:
        redelivery_arm = run_redelivery_arm_real(
            manifest,
            host="127.0.0.1",
            port=rabbitmq.port,
            db_path=workspace / "redelivery.db",
            candidate_id=random_id,
            image_digest=rabbitmq.resolved_digest,
        )
        if redelivery_arm["status"] != ARM_REAL:
            problems.append(f"the real redelivery arm failed: {redelivery_arm['detail']}")
    else:
        redelivery_arm = run_redelivery_arm_reference(
            manifest, workspace / "redelivery-reference.db"
        )
        bus_status = next(
            (entry["status"] for entry in containers_document if entry["role"] == "orders-bus"),
            "no-runtime",
        )
        degradations.append(
            f"the redelivery arm ran the labeled REFERENCE harness (rabbitmq arm"
            f" status: {bus_status})"
        )
    if not redelivery_arm["exactly_once_all"]:
        problems.append(f"redelivery did not hold the effect invariant: {redelivery_arm['detail']}")

    # -- cleanup: every disposable container stopped AND removed ------------
    for container in started:
        container.stop()

    reconciliation_green = trx_arm["report_reconciliation"]["aggregate"] == "all_passed"
    verifies = not problems and reconciliation_green
    real_coverage_complete = (
        verifies
        and sdk_matches
        and postgres is not None
        and rabbitmq is not None
        and migration_arm["status"] == ARM_REAL
        and redelivery_arm["status"] == ARM_REAL
    )
    environment_digest = environment_digest_of(
        manifest_digest=manifest.manifest_digest,
        sdk_version=sdk_observed or "",
        image_digests=image_digests,
    )
    document = {
        "schema": DOTNET_RECIPE_RUN_SCHEMA,
        "recipe_id": manifest.recipe_id,
        "candidate_id": candidate_id,
        "workspace": str(workspace),
        "manifest": manifest.as_document(),
        "sdk": {
            "pinned": manifest.sdk_version,
            "observed": sdk_observed or "",
            "matches": sdk_matches,
        },
        "environment_digest": environment_digest,
        "enforcement_profile": profile.as_document(),
        "enforcement_profile_digest": profile.digest(),
        "isolation_enforced": enforce_isolation,
        "containers": containers_document,
        "probes": probes,
        "arms": {
            "build_test": trx_arm,
            "migration": migration_arm,
            "redelivery": redelivery_arm,
        },
        "commands": [command.as_document() for command in commands],
        "verifies": verifies,
        "real_coverage_complete": real_coverage_complete,
        "problems": problems,
        "degradations": degradations,
        "authority": "none — this document grants neither merge nor deployment",
    }
    report_path = workspace / "recipe-run.json"
    report_path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return DotnetRecipeRun(
        manifest=manifest,
        candidate_id=candidate_id,
        workspace=str(workspace),
        sdk_observed=sdk_observed or "",
        document=document,
    )


def tempfile_default() -> str:
    """The default scratch root for recipe workspaces."""
    return os.environ.get("TMPDIR") or "/tmp"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m forge.adaptive.dotnet_recipe",
        description="Execute the pinned .NET service-and-dependency recipe (R38-08).",
    )
    parser.add_argument("--workspace", type=Path, default=None, help="where to run")
    parser.add_argument("--out", type=Path, default=None, help="copy the run document here")
    parser.add_argument(
        "--no-containers",
        action="store_true",
        help="skip the real dependency containers (reference arms only)",
    )
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="run the tools under the inherited environment (not the scrubbed profile)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run = run_recipe(
            workspace=args.workspace,
            allow_containers=not args.no_containers,
            enforce_isolation=not args.no_isolation,
        )
    except (ManifestError, OSError) as error:
        print(f"recipe failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    if args.out is not None:
        args.out.write_text(
            json.dumps(run.to_document(), indent=2, sort_keys=True), encoding="utf-8"
        )
    summary = {
        "verifies": run.verifies,
        "real_coverage_complete": run.real_coverage_complete,
        "problems": run.problems,
    }
    print(json.dumps(summary, indent=2))
    return 0 if run.verifies else 1


if __name__ == "__main__":  # pragma: no cover — the CLI entry
    raise SystemExit(main())
