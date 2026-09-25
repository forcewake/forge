#!/usr/bin/env python
"""R38-06 (#307) — the COLD-INSTALL proof for the frozen supported profile.

The freeze (``scripts/freeze_supported_profile.py``) pins the exact
supported composition; THIS script proves a second engineer can install
it from IMMUTABLE artifacts and see every identity match — without
reading source to repair anything. Three modes, each honest about what
it executed:

- ``--mode fresh`` — a CLEAN temp environment (fresh venv, empty pip
  cache) + a DISPOSABLE GitLab project. Installs the lane from the
  manifest's pinned wheel by sha256 (the R36-07 ladder): the v2 freeze
  pins the WORKING-TREE build (the qualification composition — the bytes
  at the version-named URL, when a URL is pinned, must equally reproduce
  the manifest's wheel sha). A different wheel under the same version
  string is a REFUSAL before anything executes. The target template is
  rendered FROM THE MANIFEST's frozen bytes (the wheel ships no
  ``ci/templates/`` — the manifest is the recipe's immutable carrier,
  never a moving working tree).
  Preflight (doctor --capabilities + the execution-spec composition
  matrix) must be green BEFORE anything paid; the only execution is the
  SMOKE job (the independent, precommitted, oracle-bearing CI job —
  python asserts in a ``python:3.13-slim`` container, ZERO model calls;
  the lane job is gated on ``$FORGE_RUN_ID`` which a cold install never
  sets). A full model task is a different drill's domain (R38-05's live
  trace is already green on this composition).

- ``--mode upgrade`` — a DISPOSABLE Postgres (never the lab DB), seeded
  with data-bearing rows at the declared PREDECESSOR schema (N-1 via
  ``alembic downgrade``), upgraded to head; preservation is proven by
  row counts AND per-table sha256 fingerprints (the canary pattern) and
  the ACTUAL schema transition is reported (``026 -> 027``) — an
  unchanged-head run never implies a migration.

- ``--mode verify`` — READ-ONLY against an installed environment (the
  live lab): every identity vs the manifest — image digests (app AND
  worker), schema head, reported version, budget caps, the shared
  checkpoint mount, the runner, the installed target template. The
  manifest binds TWO image identities honestly (the promoted release
  and the executed lab build); each axis must match ONE of them
  exactly — semver equality is never enough, and each mismatch is
  NAMED precisely (the old-worker / missing-mount / mismatched-template
  refusal arms fire before any model call would be allowed).

Exit codes: 0 = the check ran and every refusal-severity finding is
absent (named divergences are reported, never hidden); 1 = a REFUSAL (a
preflight arm fired, a probe could not observe what it must, or a
preservation check failed); 2 = usage error.

Usage (from the repository root):

    uv run python scripts/cold_install_check.py --mode verify
    uv run python scripts/cold_install_check.py --mode fresh [--skip-gitlab]
    uv run python scripts/cold_install_check.py --mode upgrade
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from freeze_supported_profile import (  # noqa: E402
    MANIFEST_PATH,
    SUPPORTED_PROFILE_STAMP,
    recover_template_bytes as recover_r38_05_section,
    validate_manifest,
)

#: Egress this check needs (the restricted-egress negative test's
#: documented allowlist): the release asset host and PyPI. Everything
#: else (the model gateway) is never contacted by this script.
EGRESS_ALLOWLIST = (
    "github.com",
    "release-assets.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
)

#: The disposable containers this script owns (upgrade mode). NEVER the
#: lab's forge-postgres.
UPGRADE_PG_BASENAME = "forge-cold-upgrade-pg"

PG_IMAGE = "postgres:17-alpine"
LAB_PG_CONTAINER = "forge-postgres"
APP_CONTAINER = "forge-app"
WORKER_CONTAINER = "forge-worker"

#: The checkpoint-authority bind both consumers must share (the
#: alignment receipts' mount; a worker without it holds a PRIVATE,
#: inconsistent authority — the doctor's before-retry refusal).
SHARED_DATA_MOUNT_DESTINATION = "/app/data"

_BUDGET_CAPS = ("FORGE_BUDGET_PROFILES", "FORGE_LANE_BUDGET_SECONDS")


class CheckRefused(RuntimeError):
    """A cold-install check refused — the message names the arm."""


# ---------------------------------------------------------------------------
# Findings: the one vocabulary every mode reports through
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One named observation. ``severity``:

    - ``match`` — the axis matched a manifest identity (which one is in
      the detail — matching the executed-lab bind is a MATCH, with the
      divergence from the promoted release still named);
    - ``divergence`` — an honestly-named difference the manifest itself
      binds (e.g. the lab runs the working-tree build, not the promoted
      digest; the integration project's template is not the frozen
      recipe);
    - ``refusal`` — a preflight arm (old worker image / missing shared
      mount / mismatched template / unknown image identity): the check
      REFUSES and no model call would be allowed past this point.
    """

    axis: str
    severity: str
    detail: str

    def render(self) -> str:
        return f"{self.severity:>10}  {self.axis}: {self.detail}"


def preflight_refusals(findings: Sequence[Finding]) -> tuple[str, ...]:
    """Every refusal-severity arm — the negative test's precise names."""
    return tuple(
        f"preflight REFUSED — {f.axis}: {f.detail}" for f in findings if f.severity == "refusal"
    )


# ---------------------------------------------------------------------------
# The manifest + the pure check functions (fixture-driven in tests)
# ---------------------------------------------------------------------------


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise CheckRefused(
            f"the frozen manifest {path} does not exist — run scripts/freeze_supported_profile.py first"
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != SUPPORTED_PROFILE_STAMP:
        raise CheckRefused(
            f"manifest schema {document.get('schema')!r} != {SUPPORTED_PROFILE_STAMP!r}"
        )
    findings = validate_manifest(document)
    if findings:
        raise CheckRefused("the frozen manifest is self-inconsistent: " + "; ".join(findings))
    return document


def check_wheel_identity(
    *, expected_sha256: str, actual_sha256: str, filename: str, version: str
) -> Finding:
    """The R36-07 identity gate: the bytes at the version-named URL must
    reproduce the pinned sha256 — a DIFFERENT wheel under the SAME
    version string is the mutable-tag refusal, never an install."""
    if actual_sha256 == expected_sha256:
        return Finding(
            axis="lane.wheel",
            severity="match",
            detail=f"{filename} reproduces the pinned sha256 {expected_sha256[:16]}… (immutable identity, not a tag)",
        )
    return Finding(
        axis="lane.wheel",
        severity="refusal",
        detail=(
            f"the wheel at the v{version} URL hashes {actual_sha256[:16]}… but the manifest pins "
            f"{expected_sha256[:16]}… — a DIFFERENT wheel under the SAME version string (the mutable-tag "
            "defect); refusing BEFORE anything installs or executes"
        ),
    )


def frozen_template(manifest: Mapping[str, Any]) -> str:
    """The recipe bytes the manifest carries — verified against their own
    pinned sha256 before any caller renders them."""
    frozen = manifest["target_template"]["frozen"]
    template = base64.b64decode(str(frozen["bytes_b64"])).decode("utf-8")
    actual = hashlib.sha256(template.encode("utf-8")).hexdigest()
    if actual != str(frozen["sha256"]):
        raise CheckRefused(
            f"the manifest's embedded template bytes hash {actual[:16]}… but pin "
            f"{str(frozen['sha256'])[:16]}… — the frozen recipe does not vouch for itself"
        )
    return template


def render_target_template(manifest: Mapping[str, Any]) -> str:
    """The disposable TARGET project's CI: the FROZEN template VERBATIM +
    the manifest's own verification-contract oracle job.

    Rendered FROM THE MANIFEST (never the working tree): the header
    documents the carrier, the template section must reproduce the
    frozen bytes exactly, and the smoke job re-asserts the manifest's
    oracle cases + file shapes. ``recover_template_bytes`` inverts this
    rendering — a round-trip the fresh mode asserts.
    """
    template = frozen_template(manifest)
    contract = manifest["verification_contract"]
    cases = "\n".join(
        f'    ("{text}", "{expected}"),' for text, expected in contract["slugify_cases"]
    )
    header = (
        "# Generated by scripts/cold_install_check.py (R38-06/#307) from the FROZEN\n"
        "# supported-profile manifest qualification/profiles/supported-gitlab-ce-v1.json\n"
        "# (the manifest is the recipe's immutable carrier — never a moving working\n"
        "# tree). Template section = the frozen bytes VERBATIM; the smoke job is the\n"
        "# manifest's own verification contract, committed BEFORE any run.\n"
        "stages: [test, harness]\n\n"
    )
    tail = (
        "\n# The INDEPENDENT verification contract, from the manifest: six exact\n"
        "# slugify cases AND the three file shapes, committed BEFORE any run.\n"
        "smoke:\n"
        "  stage: test\n"
        "  image: python:3.13-slim\n"
        "  rules:\n"
        "    - if: '$FORGE_RUN_ID'   # a dispatch pipeline carries no candidate yet\n"
        "      when: never\n"
        "    - when: on_success\n"
        "  script:\n"
        "    - |\n"
        "      python3 - <<'PY'\n"
        "      import sys\n"
        "      from pathlib import Path\n"
        "      sys.path.insert(0, 'src')\n"
        "      from utils.text import slugify\n"
        "      CASES = [\n" + "\n".join(f"      {line}" for line in cases.splitlines()) + "\n"
        "      ]\n"
        "      for text, expected in CASES:\n"
        "          got = slugify(text)\n"
        "          assert got == expected, (text, got, expected)\n"
        "      print('slugify oracle: %d/%d OK' % (len(CASES), len(CASES)))\n"
        "      app = Path('src/app.py').read_text(encoding='utf-8')\n"
        "      assert 'legacy' not in app, 'src/app.py still references legacy'\n"
        "      assert 'slugify' in app, 'src/app.py does not use slugify'\n"
        "      assert not Path('src/utils/legacy.py').exists(), 'legacy.py still exists'\n"
        "      print('shape oracle: app rewired, legacy deleted')\n"
        "      PY\n"
    )
    return header + template + tail


_TEMPLATE_TAIL_MARKER = "\n# The INDEPENDENT verification contract, from the manifest: six exact\n"


def recover_frozen_section(rendered_ci_yaml: str) -> str:
    """The inverse of :func:`render_target_template` (round-trip proof).

    Strips the generator's header (everything through the ``stages``
    line) and the oracle tail — what remains must be the frozen bytes.
    """
    marker = "stages: [test, harness]\n\n"
    index = rendered_ci_yaml.find(marker)
    if index < 0:
        raise CheckRefused("the rendered CI carries no stages header — not this generator's output")
    rest = rendered_ci_yaml[index + len(marker) :]
    tail = rest.find(_TEMPLATE_TAIL_MARKER)
    if tail < 0:
        raise CheckRefused("the rendered CI carries no oracle-tail marker")
    return rest[:tail]


def extract_template_section(ci_yaml: str) -> str:
    """The template section of an installed/generated target CI file.

    The known generators both embed the frozen template VERBATIM between
    a deterministic header and an oracle tail (the R38-05 drill's markers
    and this check's own); either is recognized. Unrecognizable bytes
    raise — an installed CI that carries no recognizable frozen section
    is a mismatched template, never a pass.
    """
    try:
        return recover_r38_05_section(ci_yaml)
    except Exception:  # noqa: BLE001 — the alternate marker set decides
        return recover_frozen_section(ci_yaml)


def template_preflight_findings(
    *, rendered_ci_yaml: str, manifest: Mapping[str, Any], template_source: str
) -> list[Finding]:
    """The mismatched-template arm, BEFORE any model call.

    ``template_source`` is the template section the install ACTUALLY
    used (the frozen bytes for a cold install; a working-tree copy for
    the negative arm). Its sha256 must equal the manifest's frozen
    sha256 — anything else is a named refusal, never a silent
    substitution of a moving tree.
    """
    findings: list[Finding] = []
    frozen_sha = str(manifest["target_template"]["frozen"]["sha256"])
    actual_sha = hashlib.sha256(template_source.encode("utf-8")).hexdigest()
    if actual_sha == frozen_sha:
        findings.append(
            Finding(
                axis="target_template",
                severity="match",
                detail=f"the installed template reproduces the frozen sha256 {frozen_sha[:16]}… (rendered from the manifest, not the working tree)",
            )
        )
    else:
        findings.append(
            Finding(
                axis="target_template",
                severity="refusal",
                detail=(
                    f"a MISMATCHED template: the bytes in play hash {actual_sha[:16]}… but the manifest "
                    f"freezes {frozen_sha[:16]}… — a moving working tree or a foreign recipe is never "
                    "silently substituted; preflight refuses before a model call"
                ),
            )
        )
    if template_source != frozen_template(manifest):
        findings.append(
            Finding(
                axis="target_template.bytes",
                severity="refusal",
                detail="the template bytes in play are not the manifest's frozen bytes VERBATIM",
            )
        )
    return findings


def schema_transition_findings(
    *, source_head: str, target_head: str, declared_head: str, declared_predecessor: str
) -> list[Finding]:
    """The honest schema-transition report (upgrade mode's verdict).

    An unchanged head (``X -> X``) is a SAME-HEAD preservation and is
    refused as an upgrade claim; the transition must actually move from
    the declared predecessor to the declared head.
    """
    findings: list[Finding] = []
    if source_head == target_head:
        findings.append(
            Finding(
                axis="upgrade.schema_transition",
                severity="refusal",
                detail=(
                    f"the migration ran {source_head} -> {target_head} (an UNCHANGED head) — a same-head "
                    "preservation run never implies a schema upgrade; the data-bearing upgrade must "
                    f"transition from the declared predecessor {declared_predecessor}"
                ),
            )
        )
        return findings
    if source_head != declared_predecessor:
        findings.append(
            Finding(
                axis="upgrade.schema_transition.source",
                severity="refusal",
                detail=(
                    f"the migration started at {source_head} but the manifest declares the predecessor "
                    f"{declared_predecessor} — an undisclosed starting schema"
                ),
            )
        )
    if target_head != declared_head:
        findings.append(
            Finding(
                axis="upgrade.schema_transition.target",
                severity="refusal",
                detail=(
                    f"the migration ended at {target_head} but the manifest declares head {declared_head}"
                ),
            )
        )
    if not findings:
        findings.append(
            Finding(
                axis="upgrade.schema_transition",
                severity="match",
                detail=(
                    f"an ACTUAL schema transition {source_head} -> {target_head} (the declared "
                    "predecessor to the declared head — never an unchanged-head implication)"
                ),
            )
        )
    return findings


@dataclass(frozen=True)
class FingerprintRow:
    """One table's preservation fingerprint (the canary pattern)."""

    table: str
    count: int
    digest: str

    def render(self) -> str:
        return f"{self.table} {self.count} {self.digest}"


def preservation_findings(
    before: Sequence[FingerprintRow], after: Sequence[FingerprintRow]
) -> list[Finding]:
    """Data-bearing preservation: counts AND sha256 digests, per table —
    each changed table is named, never averaged away."""
    before_map = {row.table: row for row in before}
    after_map = {row.table: row for row in after}
    findings: list[Finding] = []
    for table in sorted(set(before_map) | set(after_map)):
        was, now = before_map.get(table), after_map.get(table)
        if was is None:
            findings.append(
                Finding(
                    axis=f"upgrade.preservation.{table}",
                    severity="divergence",
                    detail=f"the table appeared after the upgrade ({now.render() if now else '?'})",
                )
            )
        elif now is None:
            findings.append(
                Finding(
                    axis=f"upgrade.preservation.{table}",
                    severity="refusal",
                    detail=f"the table VANISHED across the upgrade (was {was.render()})",
                )
            )
        elif (was.count, was.digest) != (now.count, now.digest):
            findings.append(
                Finding(
                    axis=f"upgrade.preservation.{table}",
                    severity="refusal",
                    detail=(
                        f"the upgrade did NOT preserve the seeded rows — was [{was.render()}], "
                        f"now [{now.render()}] (row counts and/or sha256 fingerprints differ)"
                    ),
                )
            )
    if not findings:
        findings.append(
            Finding(
                axis="upgrade.preservation",
                severity="match",
                detail=f"every seeded table preserved EXACTLY ({len(before)} tables — row counts and sha256 fingerprints equal)",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# verify mode: the installed-environment identity checks (fixture-driven)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstalledObservation:
    """What a read-only probe saw on an installed environment."""

    app_image_digest: str = ""
    worker_image_digest: str = ""
    app_reported_version: str = ""
    schema_head: str = ""
    app_caps_numerical: bool = False
    worker_caps_numerical: bool = False
    app_data_mount: bool = False
    worker_data_mount: bool = False
    runner: Mapping[str, Any] = field(default_factory=dict)
    target_project_template: str = (
        ""  # the installed .gitlab-ci.yml of a project CLAIMING the profile
    )


def verify_installed(manifest: Mapping[str, Any], observed: InstalledObservation) -> list[Finding]:
    """Every identity axis vs the manifest — each mismatch named precisely.

    The manifest binds two image identities honestly (the promoted
    release and the executed lab build); an image axis matches when it
    reproduces EITHER exactly. Semver equality is never consulted.
    """
    findings: list[Finding] = []
    promoted = manifest["control_plane"]["promoted"]
    executed = manifest["control_plane"]["executed_lab"]
    binds = {
        "promoted": str(promoted["image_digest"]),
        "executed_lab": str(executed["image_digest"]),
    }

    def image_axis(axis: str, observed_digest: str) -> Finding:
        observed_norm = (
            observed_digest
            if observed_digest.startswith("sha256:")
            else f"sha256:{observed_digest}"
        )
        matched = [name for name, bound in binds.items() if observed_norm == bound]
        if matched:
            names = ", ".join(sorted(matched))
            divergence = (
                ""
                if set(matched) == {"promoted"}
                else (
                    f" — matches the {names} bind; the divergence from the promoted digest "
                    f"{binds['promoted'][:28]}… is named, never merged"
                    if "promoted" not in matched
                    else " — matches every bind"
                )
            )
            return Finding(axis, "match", f"image digest {observed_norm[:28]}…{divergence}")
        return Finding(
            axis,
            "refusal",
            detail=(
                f"image digest {observed_norm[:28]}… is NEITHER the promoted digest "
                f"{binds['promoted'][:28]}… NOR the executed-lab digest {binds['executed_lab'][:28]}… — "
                "an UNKNOWN composition under this version string; preflight refuses before a model call"
            ),
        )

    if not observed.app_image_digest:
        findings.append(
            Finding(
                "control_plane.app_image",
                "refusal",
                "the app's image digest is unobservable — an unverifiable identity is a refusal",
            )
        )
    else:
        findings.append(image_axis("control_plane.app_image", observed.app_image_digest))
    if not observed.worker_image_digest:
        findings.append(
            Finding(
                "control_plane.worker_image",
                "refusal",
                "the worker's image digest is unobservable — an unverifiable identity is a refusal",
            )
        )
    else:
        findings.append(image_axis("control_plane.worker_image", observed.worker_image_digest))
        # the old-worker arm: the consumers must be the SAME build (the
        # alignment recreates both from one image; a worker left on an
        # older build is the named arm even when each digest is known).
        app_norm = (
            observed.app_image_digest
            if observed.app_image_digest.startswith("sha256:")
            else f"sha256:{observed.app_image_digest}"
        )
        worker_norm = (
            observed.worker_image_digest
            if observed.worker_image_digest.startswith("sha256:")
            else f"sha256:{observed.worker_image_digest}"
        )
        if app_norm != worker_norm:
            findings.append(
                Finding(
                    axis="control_plane.worker_app_parity",
                    severity="refusal",
                    detail=(
                        f"the worker runs {worker_norm[:28]}… while the app runs {app_norm[:28]}… — an "
                        "OLD worker image beside a newer control plane (the consumers must be one "
                        "build); preflight refuses before a model call"
                    ),
                )
            )

    version = str(observed.app_reported_version)
    version_binds = {
        str(promoted["release_version"]),
        str(executed.get("reported_version") or ""),
    } - {""}
    if version in version_binds:
        binds_named = (
            "the promoted"
            if version == str(promoted["release_version"])
            else ("the executed-lab (the qualification composition)" if version else "")
        )
        findings.append(
            Finding(
                "control_plane.version",
                "match",
                f"/health reports {version} — matches {binds_named} bind ({', '.join(sorted(version_binds))} are the manifest's bound version identities)",
            )
        )
    else:
        findings.append(
            Finding(
                axis="control_plane.version",
                severity="refusal",
                detail=f"/health reports {version or '(unreachable)'} but the manifest pins {', '.join(sorted(version_binds))} — version TEXT is never sufficient, but a mismatched text is always a refusal",
            )
        )
    head = str(observed.schema_head)
    if head == str(manifest["control_plane"]["schema_revision"]["head"]):
        findings.append(Finding("control_plane.schema", "match", f"alembic head {head}"))
    else:
        findings.append(
            Finding(
                axis="control_plane.schema",
                severity="refusal",
                detail=f"the deployed alembic head is {head or '(unobservable)'} but the manifest pins {manifest['control_plane']['schema_revision']['head']}",
            )
        )
    caps_ok = observed.app_caps_numerical and observed.worker_caps_numerical
    findings.append(
        Finding(
            axis="budget_caps",
            severity="match" if caps_ok else "refusal",
            detail=(
                "numerical budget caps on BOTH consumers"
                if caps_ok
                else "budget caps absent or non-numerical on at least one consumer "
                f"(app={observed.app_caps_numerical}, worker={observed.worker_caps_numerical}) — a qualification flow refuses to start without caps"
            ),
        )
    )
    # the shared checkpoint-authority mount: BOTH consumers must carry it;
    # missing from the WORKER ONLY is the named negative arm (the worker
    # would hold a private, inconsistent authority).
    if observed.app_data_mount and observed.worker_data_mount:
        findings.append(
            Finding(
                axis="shared_checkpoint_mount",
                severity="match",
                detail=f"both consumers bind the shared {SHARED_DATA_MOUNT_DESTINATION} authority",
            )
        )
    elif observed.worker_data_mount and not observed.app_data_mount:
        findings.append(
            Finding(
                axis="shared_checkpoint_mount",
                severity="refusal",
                detail=f"the APP lost the shared {SHARED_DATA_MOUNT_DESTINATION} bind while the worker keeps it — inconsistent checkpoint authority; the doctor refuses a retry until the authority is consistent",
            )
        )
    else:
        findings.append(
            Finding(
                axis="shared_checkpoint_mount",
                severity="refusal",
                detail=(
                    f"the WORKER's shared checkpoint mount ({SHARED_DATA_MOUNT_DESTINATION}) is absent "
                    f"(app={observed.app_data_mount}, worker={observed.worker_data_mount}) — the worker "
                    "would hold a private, inconsistent authority; the doctor detects this BEFORE a "
                    "retry is accepted, and preflight refuses before a model call"
                ),
            )
        )
    expected_runner = manifest["runner"]
    runner = observed.runner or {}
    if (
        runner.get("id") == expected_runner["id"]
        and runner.get("description") == expected_runner["description"]
    ):
        status = str(runner.get("status", ""))
        findings.append(
            Finding(
                axis="runner",
                severity="match" if status == expected_runner["observed_status"] else "divergence",
                detail=(
                    f"runner id {runner.get('id')} {runner.get('description')!r} ({status or 'status unobservable'})"
                    if status != expected_runner["observed_status"]
                    else f"runner id {runner.get('id')} {runner.get('description')!r} online (docker executor)"
                ),
            )
        )
    else:
        findings.append(
            Finding(
                axis="runner",
                severity="refusal",
                detail=(
                    f"the observed runner {runner.get('id')} {runner.get('description')!r} is not the "
                    f"manifest's runner id {expected_runner['id']} {expected_runner['description']!r}"
                ),
            )
        )
    if observed.target_project_template:
        try:
            installed_section = extract_template_section(observed.target_project_template)
        except CheckRefused as exc:
            findings.append(
                Finding(
                    axis="target_template",
                    severity="refusal",
                    detail=(
                        f"the installed target CI carries no recognizable frozen section ({exc}) — "
                        "a mismatched template; preflight refuses before a model call"
                    ),
                )
            )
        else:
            findings.extend(
                template_preflight_findings(
                    rendered_ci_yaml=observed.target_project_template,
                    manifest=manifest,
                    template_source=installed_section,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# The execution probes (faked wholesale in tests)
# ---------------------------------------------------------------------------


class ShellProbe:
    """Real subprocess execution with captured output."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )

    def download(self, url: str, destination: Path) -> None:
        request = urllib.request.Request(url)  # noqa: S310 — the URL is the manifest's pin
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            destination.write_bytes(response.read())


class PodmanProbe:
    """Read-only podman inspection (verify) + the disposable upgrade DB."""

    def __init__(self, shell: ShellProbe) -> None:
        self.shell = shell

    def image_digest(self, container: str) -> str:
        completed = self.shell.run(["podman", "inspect", container, "--format", "{{.ImageDigest}}"])
        if completed.returncode != 0:
            raise CheckRefused(
                f"podman inspect {container} failed: {completed.stderr.strip()[:200]}"
            )
        return completed.stdout.strip()

    def env_caps_numerical(self, container: str) -> bool:
        completed = self.shell.run(
            ["podman", "inspect", container, "--format", "{{json .Config.Env}}"]
        )
        if completed.returncode != 0:
            raise CheckRefused(
                f"podman inspect {container} failed: {completed.stderr.strip()[:200]}"
            )
        entries = json.loads(completed.stdout)
        env = {str(entry).partition("=")[0]: str(entry).partition("=")[2] for entry in entries}
        for name in _BUDGET_CAPS:
            value = env.get(name, "")
            if name == "FORGE_BUDGET_PROFILES":
                try:
                    profiles = json.loads(value) if value else {}
                except json.JSONDecodeError:
                    profiles = {}
                if not isinstance(profiles, dict) or not profiles:
                    return False
                for entry in profiles.values():
                    if not isinstance(entry, dict):
                        return False
                    if not any(
                        isinstance(v, int) and not isinstance(v, bool) and v > 0
                        for v in entry.values()
                    ):
                        return False
            elif not value.strip().isdigit():
                return False
        return True

    def data_mount(self, container: str) -> bool:
        completed = self.shell.run(["podman", "inspect", container, "--format", "{{json .Mounts}}"])
        if completed.returncode != 0:
            raise CheckRefused(
                f"podman inspect {container} failed: {completed.stderr.strip()[:200]}"
            )
        mounts = json.loads(completed.stdout)
        return any(
            str(mount.get("Destination")) == SHARED_DATA_MOUNT_DESTINATION for mount in mounts
        )

    def lab_schema_head(self) -> str:
        completed = self.shell.run(
            [
                "podman",
                "exec",
                LAB_PG_CONTAINER,
                "psql",
                "-U",
                "forge",
                "-d",
                "forge",
                "-t",
                "-A",
                "-c",
                "SELECT version_num FROM alembic_version",
            ]
        )
        if completed.returncode != 0:
            raise CheckRefused(f"the lab schema probe failed: {completed.stderr.strip()[:200]}")
        return completed.stdout.strip().splitlines()[0].strip() if completed.stdout.strip() else ""

    def health_version(self, url: str = "http://127.0.0.1:8420/health") -> str:
        request = urllib.request.Request(url)  # noqa: S310 — fixed localhost target
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return str(json.loads(response.read().decode()).get("version", ""))
        except (OSError, ValueError) as exc:
            raise CheckRefused(f"the app health probe failed: {exc}") from exc


class GitLabProbe:
    """The disposable-project legs (fresh mode) + read-only template reads."""

    def __init__(self) -> None:
        from forge.config import Settings

        settings = Settings()
        self.base = str(settings.GITLAB_URL).rstrip("/")
        self.token = settings.GITLAB_TOKEN.get_secret_value()
        self._session: Any = None

    def _client(self) -> Any:
        if self._session is None:
            import httpx

            self._session = httpx.Client(
                base_url=self.base,
                headers={"PRIVATE-TOKEN": self.token},
                timeout=30.0,
            )
        return self._session

    def _get(self, path: str, **params: Any) -> Any:
        response = self._client().get(path, params=params)
        if response.status_code != 200:
            raise CheckRefused(
                f"GET {path} answered HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    def create_project(self, name: str) -> int:
        response = self._client().post(
            "/api/v4/projects",
            json={"name": name, "visibility": "private", "initialize_with_readme": False},
        )
        if response.status_code not in (200, 201):
            raise CheckRefused(
                f"project creation failed: {response.status_code} {response.text[:200]}"
            )
        return int(response.json()["id"])

    def delete_project(self, project_id: int) -> str:
        """Schedule the disposable project's deletion; return the honest
        disposition (GitLab deletes asynchronously — ``accepted``)."""
        response = self._client().delete(f"/api/v4/projects/{project_id}")
        if response.status_code not in (200, 202, 204):
            raise CheckRefused(
                f"the disposable project {project_id} could not be scheduled for deletion "
                f"(HTTP {response.status_code}) — clean it up by hand"
            )
        return "scheduled" if response.status_code == 202 else "deleted"

    def seed_commit(self, project_id: int, files: Mapping[str, str]) -> str:
        response = self._client().post(
            f"/api/v4/projects/{project_id}/repository/commits",
            json={
                "branch": "main",
                "commit_message": "cold-install seed: frozen template + oracle (R38-06/#307)",
                "actions": [
                    {"action": "create", "file_path": path, "content": content}
                    for path, content in sorted(files.items())
                ],
            },
        )
        if response.status_code not in (200, 201):
            raise CheckRefused(f"seed commit failed: {response.status_code} {response.text[:300]}")
        return str(response.json().get("id", ""))

    def trigger_pipeline(self, project_id: int, ref: str = "main") -> int:
        response = self._client().post(f"/api/v4/projects/{project_id}/pipeline", json={"ref": ref})
        if response.status_code not in (200, 201):
            raise CheckRefused(
                f"pipeline trigger failed: {response.status_code} {response.text[:200]}"
            )
        return int(response.json()["id"])

    def wait_pipeline(
        self, project_id: int, pipeline_id: int, timeout_s: float = 600.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._get(f"/api/v4/projects/{project_id}/pipelines/{pipeline_id}")
            status = str(last.get("status", ""))
            if status in ("success", "failed", "canceled", "skipped"):
                return last
            time.sleep(5.0)
        raise CheckRefused(
            f"pipeline {pipeline_id} did not finish within {timeout_s:.0f}s (last: {last.get('status')})"
        )

    def pipeline_jobs(self, project_id: int, pipeline_id: int) -> list[dict[str, Any]]:
        return list(self._get(f"/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs"))

    def runner_row(self, runner_id: int) -> dict[str, Any]:
        return dict(self._get(f"/api/v4/runners/{runner_id}"))

    def project_file(self, project_id: int, path: str, ref: str = "main") -> str:
        encoded = path.replace("/", "%2F")
        document = self._get(f"/api/v4/projects/{project_id}/repository/files/{encoded}", ref=ref)
        return base64.b64decode(str(document.get("content", ""))).decode("utf-8", "replace")

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


# ---------------------------------------------------------------------------
# fresh mode
# ---------------------------------------------------------------------------


def _seed_files(manifest: Mapping[str, Any], project_name: str) -> dict[str, str]:
    """The disposable target project's seed tree — the GOLD state.

    The cold install runs NO model turn (that is a different drill's
    domain), so the seed ships the task's completed state — the
    ``slugify`` module, the rewired app, the deleted legacy helper —
    which is exactly what the oracle asserts. The smoke pipeline
    therefore proves the composition's DELIVERY surface (the frozen
    template renders valid CI, the runner executes it, the independent
    oracle is green on this composition) without a single model call;
    the receipt says so — a green smoke here is NOT a model-turn claim.
    """
    cases = manifest["verification_contract"]["slugify_cases"]
    test_cases = "\n".join(f'    ("{text}", "{expected}"),' for text, expected in cases)
    return {
        ".gitlab-ci.yml": render_target_template(manifest),
        "README.md": (
            f"# {project_name}\n\nA DISPOSABLE cold-install target (R38-06/#307): the\n"
            "frozen supported-profile template + the independent smoke oracle over\n"
            "the task's GOLD (already-completed) state — zero model calls; the lane\n"
            "job is gated on $FORGE_RUN_ID and never runs. Deleted after the check.\n"
        ),
        "src/app.py": (
            '"""The app entry — rewired onto slugify (the gold state)."""\n'
            "\n"
            "from utils.text import slugify\n"
            "\n"
            "\n"
            "def greet(name: str) -> str:\n"
            '    return slugify(f"hello {name}")\n'
        ),
        "src/utils/__init__.py": "",
        "src/utils/text.py": (
            '"""The slugify helper (the gold state the oracle asserts)."""\n'
            "import re\n"
            "\n"
            "\n"
            "def slugify(text: str) -> str:\n"
            '    """Lowercase; non-alphanumeric runs collapse to one "-"; no edges."""\n'
            '    slugged = re.sub(r"[^a-z0-9]+", "-", text.lower())\n'
            '    return slugged.strip("-")\n'
        ),
        "tests/test_text_utils.py": (
            '"""The independent oracle, mirrored as a test file."""\n'
            "import sys\n"
            "from pathlib import Path\n\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n\n"
            "from utils.text import slugify\n\n"
            "CASES = [\n" + test_cases + "\n]\n\n\n"
            "def test_slugify_oracle() -> None:\n"
            "    for text, expected in CASES:\n"
            "        assert slugify(text) == expected, (text, slugify(text), expected)\n"
        ),
    }


def run_fresh(
    manifest: Mapping[str, Any],
    shell: ShellProbe,
    gitlab: GitLabProbe | None,
    *,
    keep_env: bool = False,
) -> tuple[list[Finding], dict[str, Any]]:
    """The cold-install proof on a CLEAN temp environment.

    No model spend: the only execution is the repo's OWN smoke job
    (python asserts); the lane job never runs (gated on ``$FORGE_RUN_ID``).
    """
    receipt: dict[str, Any] = {
        "mode": "fresh",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "egress_allowlist": list(EGRESS_ALLOWLIST),
    }
    findings: list[Finding] = []
    wheel = manifest["lane"]["wheel"]
    version = str(wheel["version"])

    with tempfile.TemporaryDirectory(prefix="forge-cold-install-") as tmp_name:
        tmp = Path(tmp_name)
        venv = tmp / "venv"
        # 1. a CLEAN environment: fresh venv (--seed: the builder's pip,
        #    kept OUT of the identity under test), empty pip cache.
        created = shell.run(["uv", "venv", "--seed", "--python", "3.13", str(venv)])
        if created.returncode != 0:
            raise CheckRefused(f"uv venv failed: {created.stderr.strip()[:300]}")
        pip_env = {"PIP_NO_CACHE_DIR": "1", "PATH": str(venv / "bin")}
        # 2. the INSTALL TARGET — the manifest's lane wheel. The v2 freeze
        #    pins the WORKING-TREE build (the qualification composition):
        #    the bytes must exist locally and reproduce the pinned sha256
        #    BEFORE anything executes (a drifted tree under the same
        #    version string is the same mutable-tag refusal). A URL-pinned
        #    wheel downloads first; the bytes gate is identical.
        wheel_url = str(wheel["url"]) if wheel.get("url") else ""
        if wheel_url:
            wheel_path = tmp / Path(wheel_url).name
            shell.download(wheel_url, wheel_path)
        else:
            wheel_path = ROOT / str(wheel["path"])
            if not wheel_path.is_file():
                raise CheckRefused(
                    f"the manifest pins the working-tree wheel {wheel['path']} "
                    f"(sha256 {str(wheel['sha256'])[:16]}…) but the file is absent — "
                    "run `uv build` (the qualification composition's bytes must exist "
                    "before a cold install proves them)"
                )
        actual_sha = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        identity = check_wheel_identity(
            expected_sha256=str(wheel["sha256"]),
            actual_sha256=actual_sha,
            filename=wheel_path.name,
            version=version,
        )
        findings.append(identity)
        receipt["wheel"] = {
            "source": str(wheel.get("source", "")),
            "path_or_url": wheel_url or str(wheel["path"]),
            "actual_sha256": actual_sha,
        }
        if identity.severity == "refusal":
            return findings, receipt  # refuse BEFORE installing
        # 3. install + the import identity gate (R36-07).
        installed = shell.run(
            [
                str(venv / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                str(wheel_path),
            ],
            env=pip_env,
        )
        if installed.returncode != 0:
            raise CheckRefused(f"pip install failed: {installed.stderr.strip()[-500:]}")
        probe = shell.run(
            [str(venv / "bin" / "python"), "-c", "import forge; print(forge.__version__)"],
            env=pip_env,
        )
        imported_version = probe.stdout.strip()
        if imported_version == version:
            findings.append(
                Finding(
                    axis="lane.installed_identity",
                    severity="match",
                    detail=f"the installed package imports forge {imported_version} == the wheel's version (the R36-07 identity gate, before any model call)",
                )
            )
        else:
            findings.append(
                Finding(
                    axis="lane.installed_identity",
                    severity="refusal",
                    detail=f"the installed package imports forge {imported_version!r} but the pinned wheel is {version} — identity mismatch; refusing",
                )
            )
        # 4. the target template FROM THE MANIFEST (never the working tree)
        #    + the round-trip proof + the mismatched-template arm.
        rendered = render_target_template(manifest)
        (tmp / "target.gitlab-ci.yml").write_text(rendered, encoding="utf-8")
        recovered = recover_frozen_section(rendered)
        findings.extend(
            template_preflight_findings(
                rendered_ci_yaml=rendered, manifest=manifest, template_source=recovered
            )
        )
        drift_sha = str(manifest["target_template"]["working_tree_drift"]["sha256"])
        tree_sha = hashlib.sha256(
            (ROOT / str(manifest["target_template"]["path"])).read_bytes()
        ).hexdigest()
        findings.append(
            Finding(
                axis="target_template.working_tree",
                severity="divergence",
                detail=(
                    f"the working tree hashes {tree_sha[:16]}… (manifest drift record: {drift_sha[:16]}…) — "
                    "the install rendered from the MANIFEST's frozen bytes, never the tree"
                ),
            )
        )
        # 5. preflight, offline: doctor --capabilities (from the INSTALLED
        #    package) + the execution-spec composition matrix.
        doctor = shell.run(
            [str(venv / "bin" / "python"), "-m", "forge.doctor", "--capabilities"], env=pip_env
        )
        receipt["doctor_capabilities_exit"] = doctor.returncode
        if doctor.returncode == 0:
            findings.append(
                Finding(
                    axis="preflight.doctor_capabilities",
                    severity="match",
                    detail="the installed package's offline capability matrix is green (17 rows, no environment contacts)",
                )
            )
        else:
            findings.append(
                Finding(
                    axis="preflight.doctor_capabilities",
                    severity="refusal",
                    detail=f"forge-doctor --capabilities exited {doctor.returncode} on the installed package: {doctor.stdout.strip()[:200]}",
                )
            )
        composition = manifest["execution_spec_composition"]
        preflight_src = (
            "from forge.adaptive.execution_spec import CompositionRequest, preflight_composition\n"
            "row = preflight_composition(None, CompositionRequest(\n"
            f"    provider={composition['provider']!r},\n"
            f"    runtime_recipe={composition['runtime_recipe']!r},\n"
            f"    harness={composition['harness']!r},\n"
            f"    credential_route={composition['credential_route']!r},\n"
            "    resume_mode='required',\n"
            "))\n"
            "print('composition:', row.combination_key(), '| resume:', row.resume_supported)\n"
        )
        composed = shell.run([str(venv / "bin" / "python"), "-c", preflight_src], env=pip_env)
        if composed.returncode == 0:
            findings.append(
                Finding(
                    axis="preflight.composition",
                    severity="match",
                    detail=(
                        f"the execution-spec composition preflight composes on the INSTALLED package "
                        f"({composition['provider']} x {composition['runtime_recipe']} x "
                        f"{composition['harness']} x {composition['credential_route']}, resume required) "
                        "— green BEFORE any paid call"
                    ),
                )
            )
        elif "ModuleNotFoundError" in composed.stderr:
            # the promoted wheel predates the execution-spec matrix (#318) —
            # run the SAME preflight from the tree the manifest's composition
            # row came from, and NAME the seam honestly.
            local = shell.run(["uv", "run", "python", "-c", preflight_src], cwd=ROOT)
            if local.returncode == 0:
                findings.append(
                    Finding(
                        axis="preflight.composition",
                        severity="divergence",
                        detail=(
                            "the INSTALLED (promoted) wheel predates forge.adaptive.execution_spec "
                            "(#318) — the composition preflight ran against the WORKING TREE's matrix "
                            f"({composition['provider']} x {composition['runtime_recipe']} x "
                            f"{composition['harness']} x {composition['credential_route']}) and is "
                            "green; the seam is named, never silently merged"
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        axis="preflight.composition",
                        severity="refusal",
                        detail=f"the composition preflight refused everywhere: {local.stderr.strip()[:200]}",
                    )
                )
        else:
            findings.append(
                Finding(
                    axis="preflight.composition",
                    severity="refusal",
                    detail=f"the composition preflight refused on the installed package: {composed.stderr.strip()[:200]}",
                )
            )
        # 6. preflight gates everything: a refusal never reaches GitLab.
        if preflight_refusals(findings):
            receipt["refused_before_smoke"] = True
            return findings, receipt
        # 7. the SMOKE: the disposable project's own pipeline (NO model
        #    calls — the lane job is gated on $FORGE_RUN_ID).
        if gitlab is None:
            findings.append(
                Finding(
                    axis="smoke.gitlab",
                    severity="divergence",
                    detail="skipped (--skip-gitlab): the local install identity, template and preflight arms all ran; the smoke job did not",
                )
            )
        else:
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            name = f"forge-cold-install-{datetime.now(timezone.utc):%Y-%m-%d}-{suffix}"
            project_id = gitlab.create_project(name)
            receipt["project"] = {"id": project_id, "name": name}
            try:
                files = _seed_files(manifest, name)
                seed_sha = gitlab.seed_commit(project_id, files)
                receipt["seed_commit"] = seed_sha
                pipeline_id = gitlab.trigger_pipeline(project_id)
                receipt["pipeline_id"] = pipeline_id
                pipeline = gitlab.wait_pipeline(project_id, pipeline_id)
                jobs = gitlab.pipeline_jobs(project_id, pipeline_id)
                job_names = sorted(str(job.get("name")) for job in jobs)
                receipt["jobs"] = [
                    {
                        "name": str(job.get("name")),
                        "status": str(job.get("status")),
                        "web_url": str(job.get("web_url")),
                    }
                    for job in jobs
                ]
                receipt["pipeline_status"] = str(pipeline.get("status"))
                lane_jobs = [name for name in job_names if name != "smoke"]
                if pipeline.get("status") == "success" and not lane_jobs:
                    findings.append(
                        Finding(
                            axis="smoke.gitlab",
                            severity="match",
                            detail=(
                                f"the smoke pipeline {pipeline_id} is GREEN on jobs {job_names} — the "
                                "independent oracle passed over the task's GOLD state on the frozen "
                                "composition with ZERO model calls (the lane job is gated on "
                                "$FORGE_RUN_ID and never ran; a model turn is NOT claimed here)"
                            ),
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            axis="smoke.gitlab",
                            severity="refusal",
                            detail=(
                                f"the smoke pipeline {pipeline_id} ended {pipeline.get('status')} on jobs "
                                f"{job_names} — the smoke oracle must be green and the lane job must "
                                "not run in a cold install"
                            ),
                        )
                    )
            finally:
                receipt["project_disposition"] = gitlab.delete_project(project_id)
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if keep_env:
            receipt["env_dir"] = str(tmp)
    return findings, receipt


# ---------------------------------------------------------------------------
# upgrade mode
# ---------------------------------------------------------------------------


def _fingerprint_rows(shell: ShellProbe, container: str) -> list[FingerprintRow]:
    """The per-table preservation fingerprint via read-only SQL."""
    from canary_smoke import _SEED_FINGERPRINTS  # noqa: N813 — the canary pattern, reused verbatim

    rows: list[FingerprintRow] = []
    for table, expression in _SEED_FINGERPRINTS:
        count = shell.run(
            [
                "podman",
                "exec",
                container,
                "psql",
                "-U",
                "forge",
                "-d",
                "forge",
                "-t",
                "-A",
                "-c",
                f"SELECT count(*) FROM {table};",
            ]
        )
        digest = shell.run(
            [
                "podman",
                "exec",
                container,
                "psql",
                "-U",
                "forge",
                "-d",
                "forge",
                "-t",
                "-A",
                "-c",
                "SELECT encode(sha256(convert_to(coalesce(string_agg(x, E'|' ORDER BY x), ''), 'UTF8')), 'hex') "
                f"FROM (SELECT {expression} AS x FROM {table}) s;",
            ]
        )
        if count.returncode != 0 or digest.returncode != 0:
            raise CheckRefused(
                f"the preservation fingerprint for {table} could not be read: "
                f"{(count.stderr or digest.stderr).strip()[:200]}"
            )
        rows.append(
            FingerprintRow(
                table=table, count=int(count.stdout.strip()), digest=digest.stdout.strip()
            )
        )
    return rows


def _alembic(shell: ShellProbe, database_url: str, *words: str) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    return shell.run(["uv", "run", "alembic", *words], cwd=ROOT, env=env)


def run_upgrade(
    manifest: Mapping[str, Any], shell: ShellProbe
) -> tuple[list[Finding], dict[str, Any]]:
    """The data-bearing upgrade proof on a DISPOSABLE Postgres.

    Chain: upgrade to head -> downgrade to the declared PREDECESSOR ->
    seed real-shaped rows -> fingerprint -> upgrade to head -> fingerprint
    -> the ACTUAL transition + preservation verdicts. The lab database
    is never touched.
    """
    from canary_smoke import _SEED_SQL

    schema_revision = manifest["control_plane"]["schema_revision"]
    head = str(schema_revision["head"])
    predecessor = str(schema_revision["predecessor"])
    receipt: dict[str, Any] = {
        "mode": "upgrade",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    findings: list[Finding] = []
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    container = f"{UPGRADE_PG_BASENAME}-{suffix}"
    started = shell.run(
        [
            "podman",
            "run",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_USER=forge",
            "-e",
            "POSTGRES_PASSWORD=forge",
            "-e",
            "POSTGRES_DB=forge",
            "-p",
            "127.0.0.1::5432",
            PG_IMAGE,
        ]
    )
    if started.returncode != 0:
        raise CheckRefused(f"the disposable postgres did not start: {started.stderr.strip()[:300]}")
    receipt["container"] = container
    try:
        port_row = shell.run(["podman", "port", container, "5432/tcp"])
        if port_row.returncode != 0:
            raise CheckRefused(
                f"the disposable postgres published no port: {port_row.stderr.strip()[:200]}"
            )
        port = port_row.stdout.strip().rsplit(":", 1)[-1]
        database_url = f"postgresql+asyncpg://forge:forge@127.0.0.1:{port}/forge"
        receipt["database_url_host_port"] = f"127.0.0.1:{port}"
        # readiness
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = shell.run(
                ["podman", "exec", container, "pg_isready", "-U", "forge", "-d", "forge"]
            )
            if ready.returncode == 0:
                break
            time.sleep(1.0)
        else:
            raise CheckRefused("the disposable postgres never became ready")
        # establish head, then step BACK to the declared predecessor
        up0 = _alembic(shell, database_url, "upgrade", "head")
        if up0.returncode != 0:
            raise CheckRefused(
                f"alembic upgrade head failed on the disposable DB: {up0.stderr.strip()[-400:]}"
            )
        down = _alembic(shell, database_url, "downgrade", predecessor)
        if down.returncode != 0:
            raise CheckRefused(
                f"alembic downgrade {predecessor} failed: {down.stderr.strip()[-400:]}"
            )
        source_head = _psql_single(shell, container, "SELECT version_num FROM alembic_version")
        # seed the data-bearing rows AT the predecessor schema
        seeded = shell.run(
            [
                "podman",
                "exec",
                "-i",
                container,
                "psql",
                "-U",
                "forge",
                "-d",
                "forge",
                "-v",
                "ON_ERROR_STOP=1",
                "-tA",
            ],
            input_text=_SEED_SQL,
        )
        if seeded.returncode != 0:
            raise CheckRefused(f"seeding at {predecessor} failed: {seeded.stderr.strip()[-400:]}")
        before = _fingerprint_rows(shell, container)
        receipt["seeded_at_head"] = source_head
        receipt["fingerprint_before"] = [row.render() for row in before]
        # the migration under test: predecessor -> head
        up1 = _alembic(shell, database_url, "upgrade", "head")
        if up1.returncode != 0:
            raise CheckRefused(
                f"alembic upgrade head (the tested migration) failed: {up1.stderr.strip()[-400:]}"
            )
        target_head = _psql_single(shell, container, "SELECT version_num FROM alembic_version")
        receipt["schema_after"] = target_head
        after = _fingerprint_rows(shell, container)
        receipt["fingerprint_after"] = [row.render() for row in after]
        findings.extend(
            schema_transition_findings(
                source_head=source_head,
                target_head=target_head,
                declared_head=head,
                declared_predecessor=predecessor,
            )
        )
        findings.extend(preservation_findings(before, after))
    finally:
        shell.run(["podman", "rm", "-f", container])
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return findings, receipt


def _psql_single(shell: ShellProbe, container: str, sql: str) -> str:
    completed = shell.run(
        ["podman", "exec", container, "psql", "-U", "forge", "-d", "forge", "-t", "-A", "-c", sql]
    )
    if completed.returncode != 0:
        raise CheckRefused(f"psql refused: {completed.stderr.strip()[:200]}")
    return completed.stdout.strip().splitlines()[0].strip() if completed.stdout.strip() else ""


# ---------------------------------------------------------------------------
# verify mode
# ---------------------------------------------------------------------------


#: The lab's integration project (68) vs the trace's disposable target
#: project (94): the verify mode reads BOTH — the target project must
#: reproduce the frozen bytes; the integration project's older template
#: is reported as the named divergence it is.
TARGET_PROJECT_ID = 94
INTEGRATION_PROJECT_ID = 68


def _manifest_target_project_id(manifest: Mapping[str, Any]) -> int:
    """The disposable target project the manifest's template was recovered
    from (parsed from ``recovered_from`` — the id sits in the leading
    sentence; the v2 manifest names the trace's own project, which the
    teardown deletes after capture)."""
    import re as _re

    text = str(manifest["target_template"]["frozen"].get("recovered_from", ""))
    match = _re.search(r"project (\d+)", text)
    return int(match.group(1)) if match else TARGET_PROJECT_ID


def _traced_template_sha(manifest: Mapping[str, Any]) -> str:
    """The template sha the live trace pinned (its record is the manifest's
    first verification-contract receipt)."""
    for receipt in manifest.get("verification_contract", {}).get("receipts", []):
        path = ROOT / str(receipt).split("#")[0]
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            sha = str((document.get("task") or {}).get("template_sha256") or "")
            if sha:
                return sha
    return ""


def run_verify(
    manifest: Mapping[str, Any], podman: PodmanProbe, gitlab: GitLabProbe | None
) -> tuple[list[Finding], dict[str, Any]]:
    """READ-ONLY identity verification of the live lab against the manifest."""
    receipt: dict[str, Any] = {
        "mode": "verify",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "read_only": True,
    }
    runner_row: dict[str, Any] = {}
    target_template = ""
    target_template_refusal = ""
    target_project_id = _manifest_target_project_id(manifest)
    receipt["target_project_id"] = target_project_id
    if gitlab is not None:
        try:
            runner_row = gitlab.runner_row(int(manifest["runner"]["id"]))
        except CheckRefused as exc:
            receipt["runner_probe"] = str(exc)
        try:
            target_template = gitlab.project_file(target_project_id, ".gitlab-ci.yml")
        except CheckRefused:
            # The trace's disposable project was DELETED after capture (the
            # teardown's documented disposition — the #289 precedent). The
            # installed-template axis is then verified against the TRACE
            # RECEIPT the freeze cross-checked: the manifest's frozen bytes
            # must reproduce the traced template sha. Named honestly, never
            # a silent pass or a fake read of a dead project.
            traced = _traced_template_sha(manifest)
            frozen = frozen_template(manifest)
            if traced and hashlib.sha256(frozen.encode("utf-8")).hexdigest() == traced:
                target_template_refusal = ""  # replaced by the finding below
                receipt["target_project_disposition"] = "deleted-after-capture"
            else:
                target_template_refusal = (
                    f"the trace's disposable project {target_project_id} is gone AND the "
                    "frozen bytes do not reproduce the traced template sha — the recipe "
                    "identity is unverifiable; refusing"
                )
    observed = InstalledObservation(
        app_image_digest=podman.image_digest(APP_CONTAINER),
        worker_image_digest=podman.image_digest(WORKER_CONTAINER),
        app_reported_version=podman.health_version(),
        schema_head=podman.lab_schema_head(),
        app_caps_numerical=podman.env_caps_numerical(APP_CONTAINER),
        worker_caps_numerical=podman.env_caps_numerical(WORKER_CONTAINER),
        app_data_mount=podman.data_mount(APP_CONTAINER),
        worker_data_mount=podman.data_mount(WORKER_CONTAINER),
        runner=runner_row,
        target_project_template=target_template,
    )
    findings = verify_installed(manifest, observed)
    if target_template_refusal:
        findings.append(
            Finding(axis="target_template", severity="refusal", detail=target_template_refusal)
        )
    elif receipt.get("target_project_disposition") == "deleted-after-capture":
        findings.append(
            Finding(
                axis="target_template",
                severity="match",
                detail=(
                    f"the trace's disposable project {target_project_id} is deleted after "
                    "capture (the teardown's documented disposition); the manifest's frozen "
                    "bytes were cross-checked against the TRACE receipt's pinned template "
                    "sha and reproduce it — the manifest is the recipe's carrier"
                ),
            )
        )
    if gitlab is not None:
        # the integration project's template: honestly NOT the frozen
        # recipe (it rode the disposable target project) — named, never merged.
        try:
            integration_template = gitlab.project_file(INTEGRATION_PROJECT_ID, ".gitlab-ci.yml")
            frozen_sha = str(manifest["target_template"]["frozen"]["sha256"])
            integration_sha = hashlib.sha256(integration_template.encode("utf-8")).hexdigest()
            findings.append(
                Finding(
                    axis="integration_project_template",
                    severity="divergence",
                    detail=(
                        f"the lab integration project {INTEGRATION_PROJECT_ID} runs its own template "
                        f"({integration_sha[:16]}…; the manifest freezes {frozen_sha[:16]}…) — expected: "
                        "the frozen recipe is the TARGET project's CI, not the integration project's"
                    ),
                )
            )
        except CheckRefused as exc:
            findings.append(
                Finding(
                    axis="integration_project_template",
                    severity="divergence",
                    detail=f"the lab integration project's template is unreadable ({exc})",
                )
            )
    receipt["observed"] = {
        "app_image_digest": observed.app_image_digest,
        "worker_image_digest": observed.worker_image_digest,
        "app_reported_version": observed.app_reported_version,
        "schema_head": observed.schema_head,
        "app_caps_numerical": observed.app_caps_numerical,
        "worker_caps_numerical": observed.worker_caps_numerical,
        "app_data_mount": observed.app_data_mount,
        "worker_data_mount": observed.worker_data_mount,
        "runner": runner_row,
    }
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return findings, receipt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(findings: Sequence[Finding], receipt: Mapping[str, Any]) -> None:
    print("cold_install_check: findings")
    for finding in findings:
        print(f"  {finding.render()}")
    refusals = preflight_refusals(findings)
    if refusals:
        print("cold_install_check: REFUSED")
        for refusal in refusals:
            print(f"  - {refusal}")
    else:
        divergences = [f for f in findings if f.severity == "divergence"]
        print(
            f"cold_install_check: OK — {sum(1 for f in findings if f.severity == 'match')} match, "
            f"{len(divergences)} named divergence(s), 0 refusals"
        )
    print("cold_install_check: receipt")
    print("  " + json.dumps(receipt, indent=2, sort_keys=True).replace("\n", "\n  "))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/cold_install_check.py",
        description=(
            "R38-06 (#307): the cold-install proof for the frozen supported profile — "
            "fresh (clean venv + disposable project, no model spend), upgrade (disposable "
            "seeded Postgres, actual N-1 -> head transition), verify (read-only identity "
            "check of an installed environment)."
        ),
    )
    parser.add_argument("--mode", required=True, choices=("fresh", "upgrade", "verify"))
    parser.add_argument(
        "--skip-gitlab",
        action="store_true",
        help="fresh/verify: skip the GitLab legs (the local identity/preflight arms still run)",
    )
    parser.add_argument(
        "--receipt-out",
        type=Path,
        default=None,
        help="persist the receipt JSON to this path (default: print only)",
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest()
        gitlab = None if args.skip_gitlab else GitLabProbe()
        shell = ShellProbe()
        try:
            if args.mode == "fresh":
                findings, receipt = run_fresh(manifest, shell, gitlab)
            elif args.mode == "upgrade":
                findings, receipt = run_upgrade(manifest, shell)
            else:
                findings, receipt = run_verify(manifest, PodmanProbe(shell), gitlab)
        finally:
            if gitlab is not None:
                gitlab.close()
    except CheckRefused as error:
        print(f"cold_install_check: REFUSED: {error}", file=sys.stderr)
        return 1
    _print_report(findings, receipt)
    if args.receipt_out is not None:
        args.receipt_out.parent.mkdir(parents=True, exist_ok=True)
        args.receipt_out.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"cold_install_check: receipt written to {args.receipt_out}")
    return 1 if preflight_refusals(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
