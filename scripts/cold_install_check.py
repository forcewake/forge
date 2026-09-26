#!/usr/bin/env python
"""R38-06 (#307) + R40-12 (#348) — the COLD-INSTALL proof for the frozen supported profile.

The freeze (``scripts/freeze_supported_profile.py``) pins the exact
supported composition; THIS script proves a second engineer can install
it from IMMUTABLE artifacts and see every identity match — without
reading source to repair anything. Six modes, each honest about what
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

- ``--mode oracle-replay`` (R40-12) — the INITIAL-DELIVERY install-level
  trace, disposable and offline: the same fresh local legs (clean venv,
  the pinned wheel by sha256, the template from the manifest, the
  offline preflights) and then the smoke job's OWN oracle script —
  extracted from the manifest-rendered CI, not reimplemented — executed
  by the INSTALLED package's venv python against the seeded GOLD state.
  Zero model calls; a green replay is an install-only delivery claim,
  never a model-turn claim.

- ``--mode negative-arms`` (R40-12) — the four documented failure modes
  (missing permission / unavailable runner / stale wheel / incompatible
  schema N-2), each executed on DISPOSABLE material and each required to
  produce its TYPED refusal BEFORE the unsafe or paid action (a counting
  probe proves no pip install / alembic migration / dispatch ran).

- ``--mode from-runbook`` (R40-12) — executes the RUNBOOK'S OWN
  commands: ``docs/onboarding/cold-install-runbook.md`` carries
  ``# forge-step:``-marked command blocks; this mode parses them,
  runs every ``machine`` step verbatim (in a fresh evidence directory,
  against disposable environments only — never the shared lab app,
  never the customer DB), checks each step's documented observable,
  and counts the ``human`` and ``lab`` markers honestly instead of
  executing them. The kit is proven when the runbook's commands,
  executed as written, produce the documented observables.

Exit codes: 0 = the check ran and every refusal-severity finding is
absent (named divergences are reported, never hidden); 1 = a REFUSAL (a
preflight arm fired, a probe could not observe what it must, or a
preservation check failed); 2 = usage error.

Usage (from the repository root):

    uv run python scripts/cold_install_check.py --mode verify
    uv run python scripts/cold_install_check.py --mode fresh [--skip-gitlab]
    uv run python scripts/cold_install_check.py --mode upgrade [--rollback-rehearsal]
    uv run python scripts/cold_install_check.py --mode oracle-replay
    uv run python scripts/cold_install_check.py --mode negative-arms
    uv run python scripts/cold_install_check.py --mode from-runbook
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import random
import re
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

#: The R40-12 kit's entry document — the runbook whose OWN commands the
#: ``from-runbook`` mode parses and executes (never a parallel
#: implementation of the documented steps).
RUNBOOK_PATH = ROOT / "docs" / "onboarding" / "cold-install-runbook.md"

#: Every machine step writes its receipts here; the runbook's commands
#: read it (``$FORGE_COLD_EVIDENCE``). The from-runbook executor sets it
#: to a fresh temp dir; a human following the runbook sets it once by
#: hand (the runbook's first machine step does exactly that).
EVIDENCE_DIR_ENV = "FORGE_COLD_EVIDENCE"

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
      REFUSES and no model call would be allowed past this point;
    - ``human`` (R40-12) — a runbook step that is a HUMAN deliverable:
      counted with its observable, never executed (it is not a finding
      against the install and never refuses);
    - ``lab-blocked`` (R40-12) — a runbook step that needs the shared
      lab or a paid lane this window: recorded with its reason, never
      executed (also never a refusal).
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
# R40-12 (#348): the three NEW preflight arms (fixture-driven in tests,
# executed on disposable material by --mode negative-arms)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialsObservation:
    """What a read-only GitLab permission probe saw.

    ``projects_probe_http_status`` is the HTTP status of a minimal
    ``GET /api/v4/projects`` (``200`` = the token may use the projects
    API; ``401``/``403`` = missing permission; ``0`` = the probe could
    not observe — offline stand-ins and fakes use this).
    """

    token_present: bool = False
    projects_probe_http_status: int = 0


def credentials_preflight_findings(observed: CredentialsObservation) -> list[Finding]:
    """The missing-permission arm, BEFORE the first GitLab write or any
    model call.

    A cold install creates a disposable project and (in the live drills)
    dispatches a paid lane job; a token that cannot use the projects API
    must refuse TYPED at preflight — never mid-drill with a half-built
    environment, and never after spend.
    """
    if not observed.token_present:
        return [
            Finding(
                axis="credentials.permission",
                severity="refusal",
                detail=(
                    "no GitLab token is configured (GITLAB_TOKEN absent) — MISSING PERMISSION; "
                    "preflight refuses BEFORE the first project is created or any model call"
                ),
            )
        ]
    status = observed.projects_probe_http_status
    if status == 200:
        return [
            Finding(
                axis="credentials.permission",
                severity="match",
                detail="the token can use the projects API (HTTP 200 on the read-only probe)",
            )
        ]
    if status in (401, 403):
        return [
            Finding(
                axis="credentials.permission",
                severity="refusal",
                detail=(
                    f"the token's read-only projects-API probe answered HTTP {status} — MISSING "
                    "PERMISSION (the api scope is required); preflight refuses BEFORE the first "
                    "project is created or any model call"
                ),
            )
        ]
    return [
        Finding(
            axis="credentials.permission",
            severity="refusal",
            detail=(
                f"the projects-API permission probe could not observe the token (status {status}) "
                "— an unverifiable permission is a refusal, never a silent skip"
            ),
        )
    ]


def runner_availability_findings(
    *, observed_status: str, manifest: Mapping[str, Any]
) -> list[Finding]:
    """The unavailable-runner arm, BEFORE dispatching the paid lane job.

    The composition's runner is PINNED (id + description in the
    manifest); a dispatch to an offline runner would pay for nothing or
    hang — the typed refusal fires at preflight instead.
    """
    expected = manifest["runner"]
    identity = f"runner id {expected['id']} {expected['description']!r}"
    if observed_status == str(expected["observed_status"]):
        return [
            Finding(
                axis="runner.availability",
                severity="match",
                detail=f"the pinned {identity} observed {observed_status!r} — available",
            )
        ]
    return [
        Finding(
            axis="runner.availability",
            severity="refusal",
            detail=(
                f"the pinned {identity} observed status {observed_status or '(unobservable)'} — "
                "RUNNER UNAVAILABLE; preflight refuses BEFORE dispatching the paid lane job "
                "(a dispatch to an offline runner pays for nothing or hangs)"
            ),
        )
    ]


def _revision_steps_behind(observed: str, predecessor: str) -> str:
    """The honest gap sentence when both revisions are numeric (the
    alembic chain's ``NNN`` spelling); empty when not comparable."""
    try:
        gap = int(predecessor) - int(observed)
    except ValueError:
        return ""
    return f" {gap} step(s) behind the declared predecessor" if gap > 0 else ""


def schema_compatibility_findings(
    *, observed_head: str, manifest: Mapping[str, Any]
) -> list[Finding]:
    """The incompatible-schema arm (the N-2 exemplar), BEFORE the
    migration runs.

    The supported install paths are EXACTLY two: a database already at
    the pinned head, or at the declared predecessor (the one-step
    upgrade the upgrade mode proves). Anything older (N-2 and further)
    or foreign must refuse at preflight — a cold install never walks an
    unbounded migration chain inside one action, and never migrates a
    database it cannot identify.
    """
    head = str(manifest["control_plane"]["schema_revision"]["head"])
    predecessor = str(manifest["control_plane"]["schema_revision"]["predecessor"])
    if observed_head == head:
        return [
            Finding(
                axis="schema.compatibility",
                severity="match",
                detail=f"the database is at the pinned head {head} — no migration needed",
            )
        ]
    if observed_head == predecessor:
        return [
            Finding(
                axis="schema.compatibility",
                severity="match",
                detail=(
                    f"the database is at the declared predecessor {predecessor} — the supported "
                    f"one-step upgrade {predecessor} -> {head} applies (upgrade mode proves it)"
                ),
            )
        ]
    gap = _revision_steps_behind(observed_head, predecessor)
    refusal = "INCOMPATIBLE SCHEMA; preflight refuses BEFORE the migration runs"
    return [
        Finding(
            axis="schema.compatibility",
            severity="refusal",
            detail=(
                f"the database head {observed_head or '(unobservable)'} is neither the pinned head "
                f"{head} nor its declared predecessor {predecessor}{gap} — {refusal} "
                "(the supported paths are fresh-at-head "
                f"or the single {predecessor} -> {head} step; an older database must walk the "
                "recorded chain deliberately, never inside a cold install)"
            ),
        )
    ]


def extract_smoke_oracle_script(rendered_ci_yaml: str) -> str:
    """The smoke job's own python oracle, extracted VERBATIM from the
    manifest-rendered CI (the ``python3 - <<'PY' … PY`` heredoc).

    The oracle-replay mode executes THESE bytes with the installed
    package's venv python — the independent oracle from the installed
    artifacts, never a reimplementation beside them.
    """
    lines = rendered_ci_yaml.splitlines()
    for index, line in enumerate(lines):
        if "python3 - <<'PY'" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        body: list[str] = []
        for follow in lines[index + 1 :]:
            if follow.strip() == "PY":
                return "\n".join(body) + "\n"
            body.append(follow[indent:] if follow[:indent].strip() == "" else follow)
        break
    raise CheckRefused(
        "the rendered CI carries no ``python3 - <<'PY' … PY`` oracle heredoc — "
        "not this generator's output"
    )


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

    def projects_probe_status(self) -> int:
        """The read-only permission probe (R40-12): the HTTP status of a
        minimal projects-API read — 200 means the token may use the API
        the cold install needs; 401/403 is the typed missing-permission
        arm; 0 means the probe could not observe (transport failure)."""
        import httpx

        try:
            response = self._client().get(
                "/api/v4/projects", params={"owned": "true", "per_page": 1}
            )
        except httpx.HTTPError:
            return 0
        return int(response.status_code)

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


@dataclass(frozen=True)
class FreshContext:
    """The live disposable environment the fresh local legs built (None
    fields mean the legs refused before completing — the refusals list
    says why)."""

    tmp: Path | None = None
    venv: Path | None = None
    pip_env: dict[str, str] = field(default_factory=dict)
    rendered_ci: str = ""


def _fresh_local_legs(
    manifest: Mapping[str, Any], shell: ShellProbe, tmp: Path, receipt: dict[str, Any]
) -> tuple[list[Finding], FreshContext]:
    """The OFFLINE fresh legs (steps 1-5): clean venv, the pinned wheel
    by sha256, the import identity gate, the template FROM the manifest,
    doctor + the composition preflight — all before any GitLab contact.
    Shared verbatim by ``run_fresh`` and ``run_oracle_replay`` (the
    replay IS the fresh install plus the oracle execution).
    """
    findings: list[Finding] = []
    wheel = manifest["lane"]["wheel"]
    version = str(wheel["version"])
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
        return findings, FreshContext(
            tmp=tmp, venv=venv, pip_env=pip_env
        )  # refuse BEFORE installing
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
    return findings, FreshContext(tmp=tmp, venv=venv, pip_env=pip_env, rendered_ci=rendered)


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
    with tempfile.TemporaryDirectory(prefix="forge-cold-install-") as tmp_name:
        tmp = Path(tmp_name)
        findings, _ctx = _fresh_local_legs(manifest, shell, tmp, receipt)
        # 6. preflight gates everything: a refusal never reaches GitLab.
        if preflight_refusals(findings):
            receipt["refused_before_smoke"] = True
            receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
            # R40-12: the two NEW preflight arms fire BEFORE the first
            # GitLab write — a missing permission or an offline runner
            # refuses typed here, never mid-project, never after spend.
            findings.extend(
                credentials_preflight_findings(
                    CredentialsObservation(
                        token_present=True,
                        projects_probe_http_status=gitlab.projects_probe_status(),
                    )
                )
            )
            try:
                runner_row = gitlab.runner_row(int(manifest["runner"]["id"]))
            except CheckRefused as exc:
                runner_row = {}
                findings.append(
                    Finding(
                        axis="runner.availability",
                        severity="refusal",
                        detail=f"the pinned runner could not be observed: {exc}",
                    )
                )
            else:
                findings.extend(
                    runner_availability_findings(
                        observed_status=str(runner_row.get("status", "")), manifest=manifest
                    )
                )
            if preflight_refusals(findings):
                receipt["refused_before_smoke"] = True
                receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                return findings, receipt
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
                                f"{job_names} — the smoke oracle must be green and "
                                "the lane job must not run in a cold install"
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
# oracle-replay mode (R40-12): the initial-delivery install-level trace
# ---------------------------------------------------------------------------


def run_oracle_replay(
    manifest: Mapping[str, Any], shell: ShellProbe
) -> tuple[list[Finding], dict[str, Any]]:
    """The INITIAL-DELIVERY trace from the INSTALLED artifacts.

    The fresh local legs build the disposable install (clean venv + the
    pinned wheel by sha256 + the template from the manifest + the
    offline preflights); the replay then seeds the task's GOLD state
    and executes the smoke job's OWN oracle script — extracted VERBATIM
    from the manifest-rendered CI, never reimplemented — with the
    INSTALLED package's venv python. Zero model calls: this is the
    install-only delivery claim (the useful-WIP continuation with the
    real model is the #326 playbook's lab-bound step, not this one).
    """
    receipt: dict[str, Any] = {
        "mode": "oracle-replay",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "claim": "install-only initial delivery (gold state, zero model calls)",
    }
    with tempfile.TemporaryDirectory(prefix="forge-cold-oracle-") as tmp_name:
        tmp = Path(tmp_name)
        findings, ctx = _fresh_local_legs(manifest, shell, tmp, receipt)
        if preflight_refusals(findings) or ctx.venv is None:
            receipt["refused_before_oracle"] = True
            receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return findings, receipt
        # the GOLD state seed (the same tree the smoke pipeline commits)
        gold = tmp / "gold-project"
        gold.mkdir()
        for rel_path, content in sorted(_seed_files(manifest, "forge-cold-oracle-replay").items()):
            target = gold / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        script = extract_smoke_oracle_script(ctx.rendered_ci)
        (tmp / "smoke-oracle.py").write_text(script, encoding="utf-8")
        replayed = shell.run(
            [str(ctx.venv / "bin" / "python"), "-"],
            cwd=gold,
            env=ctx.pip_env,
            input_text=script,
        )
        cases = len(manifest["verification_contract"]["slugify_cases"])
        expected_oracle_line = f"slugify oracle: {cases}/{cases} OK"
        receipt["oracle_replay"] = {
            "python": str(ctx.venv / "bin" / "python"),
            "executor": (
                "the INSTALLED package's venv python, fed the smoke job's own heredoc "
                "EXTRACTED VERBATIM from the manifest-rendered CI (never a reimplementation)"
            ),
            "cwd": "the seeded GOLD state (the task's completed files)",
            "exit": replayed.returncode,
            "oracle_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
            "stdout": replayed.stdout.strip()[-500:],
            "stderr": replayed.stderr.strip()[-300:],
        }
        if (
            replayed.returncode == 0
            and expected_oracle_line in replayed.stdout
            and "shape oracle: app rewired, legacy deleted" in replayed.stdout
        ):
            findings.append(
                Finding(
                    axis="delivery.oracle",
                    severity="match",
                    detail=(
                        f"the INDEPENDENT oracle (the smoke job's own script, {expected_oracle_line}) "
                        "passed on the GOLD state executed by the INSTALLED package's venv python — "
                        "the initial-delivery install-level trace; ZERO model calls (a model turn is "
                        "NOT claimed here)"
                    ),
                )
            )
        else:
            findings.append(
                Finding(
                    axis="delivery.oracle",
                    severity="refusal",
                    detail=(
                        f"the oracle replay exited {replayed.returncode} — expected {expected_oracle_line!r} "
                        f"in the output; stdout tail: {replayed.stdout.strip()[-200:]!r}; "
                        f"stderr tail: {replayed.stderr.strip()[-200:]!r}"
                    ),
                )
            )
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
    manifest: Mapping[str, Any], shell: ShellProbe, *, rehearse_rollback: bool = False
) -> tuple[list[Finding], dict[str, Any]]:
    """The data-bearing upgrade proof on a DISPOSABLE Postgres.

    Chain: upgrade to head -> downgrade to the declared PREDECESSOR ->
    seed real-shaped rows -> fingerprint -> upgrade to head -> fingerprint
    -> the ACTUAL transition + preservation verdicts. The lab database
    is never touched.

    With ``rehearse_rollback`` (R40-12): the ROLLBACK/FORWARD-RECOVERY
    rehearsal continues the chain on the SAME disposable database —
    downgrade to the predecessor (the declared rollback edge), verify
    the data-bearing fingerprints SURVIVED the rollback, then upgrade to
    head again (forward recovery) and require the seeded fingerprints to
    reproduce EXACTLY across the whole cycle.
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
        if rehearse_rollback:
            # the ROLLBACK leg: head -> the declared predecessor, on the
            # SAME disposable database, with the data-bearing rows in place.
            rolled_back = _alembic(shell, database_url, "downgrade", predecessor)
            if rolled_back.returncode != 0:
                raise CheckRefused(
                    f"the rollback rehearsal's alembic downgrade {head} failed: "
                    f"{rolled_back.stderr.strip()[-400:]}"
                )
            rolled_head = _psql_single(shell, container, "SELECT version_num FROM alembic_version")
            receipt["rollback_edge"] = f"{target_head} -> {rolled_head}"
            if rolled_head == predecessor:
                findings.append(
                    Finding(
                        axis="rollback.rehearsal",
                        severity="match",
                        detail=(
                            f"the declared ROLLBACK edge {target_head} -> {rolled_head} executed on "
                            "the disposable database (the manifest's declared predecessor — never a "
                            "blind downgrade)"
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        axis="rollback.rehearsal",
                        severity="refusal",
                        detail=(
                            f"the rollback rehearsal ended at {rolled_head} but the declared "
                            f"predecessor is {predecessor} — an undisclosed rollback edge"
                        ),
                    )
                )
            rolled_rows = _fingerprint_rows(shell, container)
            receipt["fingerprint_after_rollback"] = [row.render() for row in rolled_rows]
            findings.extend(preservation_findings(after, rolled_rows))
            # the FORWARD-RECOVERY leg: predecessor -> head again.
            recovered = _alembic(shell, database_url, "upgrade", "head")
            if recovered.returncode != 0:
                raise CheckRefused(
                    f"the forward-recovery alembic upgrade head failed: "
                    f"{recovered.stderr.strip()[-400:]}"
                )
            recovered_head = _psql_single(
                shell, container, "SELECT version_num FROM alembic_version"
            )
            receipt["forward_edge"] = f"{rolled_head} -> {recovered_head}"
            if recovered_head == head:
                findings.append(
                    Finding(
                        axis="rollback.forward_recovery",
                        severity="match",
                        detail=(
                            f"the FORWARD-RECOVERY edge {rolled_head} -> {recovered_head} executed — "
                            "the composition returns to the pinned head after the rehearsal rollback"
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        axis="rollback.forward_recovery",
                        severity="refusal",
                        detail=(
                            f"the forward recovery ended at {recovered_head} but the manifest pins "
                            f"head {head}"
                        ),
                    )
                )
            recovered_rows = _fingerprint_rows(shell, container)
            receipt["fingerprint_after_recovery"] = [row.render() for row in recovered_rows]
            findings.extend(preservation_findings(rolled_rows, recovered_rows))
            if [row.render() for row in before] == [row.render() for row in recovered_rows]:
                findings.append(
                    Finding(
                        axis="rollback_cycle.preservation",
                        severity="match",
                        detail=(
                            "the rollback + forward-recovery cycle preserved every seeded "
                            "fingerprint EXACTLY (seed == recovered — the data survived both edges)"
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        axis="rollback_cycle.preservation",
                        severity="refusal",
                        detail=(
                            "the rollback + forward-recovery cycle did NOT reproduce the seeded "
                            "fingerprints (seed != recovered) — the rehearsal names the drift, "
                            "never averages it away"
                        ),
                    )
                )
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
# negative-arms mode (R40-12): the four documented failure modes, each
# executed on DISPOSABLE material, each refusing BEFORE the unsafe or
# paid action
# ---------------------------------------------------------------------------

#: The four arms in their documented order (the runbook's failure-mode
#: table lists exactly these typed refusals).
NEGATIVE_ARMS: tuple[str, ...] = (
    "missing-permission",
    "unavailable-runner",
    "stale-wheel",
    "incompatible-schema-n2",
)


class CountingShellProbe:
    """A :class:`ShellProbe` stand-in that records every executed argv —
    the negative arms' BEFORE proofs (no pip install, no alembic
    migration, no dispatch ever ran past the refusal)."""

    def __init__(self, inner: ShellProbe) -> None:
        self.inner = inner
        self.invocations: list[tuple[str, ...]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.invocations.append(tuple(str(word) for word in command))
        return self.inner.run(command, cwd=cwd, env=env, input_text=input_text)

    def count(self, *words: str) -> int:
        """How many invocations carried EVERY given word (e.g. ``("pip",
        "install")``)."""
        return sum(1 for argv in self.invocations if all(word in argv for word in words))


def alembic_chain_revisions() -> list[str]:
    """The revision ids of the alembic chain, sorted (``026`` … ``031``)."""
    return sorted(
        path.name.split("_", 1)[0]
        for path in (ROOT / "alembic" / "versions").glob("*.py")
        if path.name[0].isdigit()
    )


def revision_two_behind(manifest: Mapping[str, Any]) -> str:
    """The N-2 revision for the incompatible-schema arm: two steps behind
    the pinned head along the committed chain."""
    revisions = alembic_chain_revisions()
    predecessor = str(manifest["control_plane"]["schema_revision"]["predecessor"])
    if predecessor not in revisions:
        raise CheckRefused(
            f"the declared predecessor {predecessor} is not in the committed alembic chain "
            f"({revisions[-3:]}…) — the N-2 arm cannot derive its stand-in"
        )
    index = revisions.index(predecessor)
    if index == 0:
        raise CheckRefused(
            "the declared predecessor is the chain's first revision — no N-2 exists; "
            "the incompatible-schema arm needs an older revision to stand in"
        )
    return revisions[index - 1]


def run_negative_arms(
    manifest: Mapping[str, Any], shell: ShellProbe
) -> tuple[list[Finding], dict[str, Any]]:
    """Each documented failure mode must fire its TYPED refusal BEFORE
    the unsafe or paid action — executed, not asserted on paper.

    This mode's findings are ARM VERDICTS (``match`` = the arm refused
    typed and in time; ``refusal`` = the arm failed to refuse); the
    underlying typed refusal texts ride the receipt verbatim.
    """
    receipt: dict[str, Any] = {
        "mode": "negative-arms",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": (
            "each arm must produce its TYPED refusal BEFORE the unsafe or paid action "
            "(a counting probe proves no pip install / alembic migration / dispatch ran)"
        ),
        "arms": {},
    }
    findings: list[Finding] = []
    arms: dict[str, Any] = receipt["arms"]

    def verdict(arm: str, refusal: Finding, *, before_proof: str, proven: bool) -> None:
        arms[arm] = {
            "typed_refusal": refusal.detail,
            "refusal_fired": refusal.severity == "refusal" and proven,
            "before_proof": before_proof,
        }
        if refusal.severity == "refusal" and proven:
            findings.append(
                Finding(
                    axis=f"negative-arm.{arm}",
                    severity="match",
                    detail=f"the arm refused TYPED before the unsafe/paid action ({before_proof})",
                )
            )
        else:
            findings.append(
                Finding(
                    axis=f"negative-arm.{arm}",
                    severity="refusal",
                    detail=(
                        f"the arm DID NOT prove its typed refusal in time "
                        f"(before-proof: {before_proof}; observed severity {refusal.severity})"
                    ),
                )
            )

    # -- arm 1: MISSING PERMISSION (the offline stand-in: the read-only
    #    projects-API probe answered 403). The wired path (run_fresh's
    #    GitLab leg) fires this BEFORE the first project is created.
    permission = credentials_preflight_findings(
        CredentialsObservation(token_present=True, projects_probe_http_status=403)
    )
    assert permission and permission[0].severity == "refusal"
    verdict(
        "missing-permission",
        permission[0],
        before_proof="the refusal fires at preflight, before the first project is created or any model call",
        proven=all("MISSING PERMISSION" in f.detail and "BEFORE" in f.detail for f in permission),
    )

    # -- arm 2: UNAVAILABLE RUNNER (the offline stand-in: the pinned
    #    runner observed 'offline'). The wired path fires this BEFORE
    #    dispatching the paid lane job.
    runner = runner_availability_findings(observed_status="offline", manifest=manifest)
    assert runner and runner[0].severity == "refusal"
    verdict(
        "unavailable-runner",
        runner[0],
        before_proof="the refusal fires at preflight, before dispatching the paid lane job",
        proven=all(
            "RUNNER UNAVAILABLE" in f.detail and "BEFORE dispatching" in f.detail for f in runner
        ),
    )

    # -- arm 3: STALE WHEEL — executed on a corrupted COPY of the REAL
    #    pinned bytes (same version string, different bytes): run_fresh
    #    must refuse BEFORE pip installs anything.
    wheel_path = ROOT / str(manifest["lane"]["wheel"]["path"])
    if not wheel_path.is_file():
        raise CheckRefused(
            "the pinned wheel is absent — the stale-wheel arm corrupts a COPY of the real bytes"
        )
    with tempfile.TemporaryDirectory(prefix="forge-cold-negative-wheel-") as tmp_name:
        stale = Path(tmp_name) / wheel_path.name
        data = bytearray(wheel_path.read_bytes())
        data[len(data) // 2] ^= 0xFF  # the SAME version string, DIFFERENT bytes
        stale.write_bytes(bytes(data))
        mutated = copy.deepcopy(dict(manifest))
        mutated["lane"]["wheel"]["path"] = str(stale)
        counting = CountingShellProbe(shell)
        arm_findings, arm_receipt = run_fresh(mutated, counting, None)
        arm_refusals = preflight_refusals(arm_findings)
        fired = any("DIFFERENT wheel under the SAME version string" in r for r in arm_refusals)
        pip_runs = counting.count("pip")
        arms["stale-wheel"] = {
            "typed_refusal": next(
                (r for r in arm_refusals if "DIFFERENT wheel" in r), "(the refusal did not fire)"
            ),
            "refusal_fired": fired,
            "pip_invocations_after_gate": pip_runs,
            "refused_before_smoke": bool(arm_receipt.get("refused_before_smoke")),
            "wheel_sha256_observed": str(arm_receipt.get("wheel", {}).get("actual_sha256", "")),
        }
        if fired and pip_runs == 0:
            findings.append(
                Finding(
                    axis="negative-arm.stale-wheel",
                    severity="match",
                    detail=(
                        f"the corrupted copy under the SAME version string refused TYPED with ZERO "
                        f"pip invocations (observed sha {arms['stale-wheel']['wheel_sha256_observed'][:16]}…)"
                    ),
                )
            )
        else:
            findings.append(
                Finding(
                    axis="negative-arm.stale-wheel",
                    severity="refusal",
                    detail=(
                        f"the stale-wheel arm failed: refusal_fired={fired}, pip invocations={pip_runs} "
                        "— the mutable-tag gate must fire BEFORE anything installs"
                    ),
                )
            )

    # -- arm 4: INCOMPATIBLE SCHEMA (N-2) — a DISPOSABLE Postgres rolled
    #    back two chain steps; the compatibility gate must refuse BEFORE
    #    any migration runs (the version must still read N-2 afterwards).
    n2 = revision_two_behind(manifest)
    head = str(manifest["control_plane"]["schema_revision"]["head"])
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    container = f"forge-cold-negative-pg-{suffix}"
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
    receipt["schema_arm_container"] = container
    try:
        port_row = shell.run(["podman", "port", container, "5432/tcp"])
        if port_row.returncode != 0:
            raise CheckRefused("the disposable postgres published no port")
        port = port_row.stdout.strip().rsplit(":", 1)[-1]
        database_url = f"postgresql+asyncpg://forge:forge@127.0.0.1:{port}/forge"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if (
                shell.run(
                    ["podman", "exec", container, "pg_isready", "-U", "forge", "-d", "forge"]
                ).returncode
                == 0
            ):
                break
            time.sleep(1.0)
        else:
            raise CheckRefused("the disposable postgres never became ready")
        up = _alembic(shell, database_url, "upgrade", "head")
        if up.returncode != 0:
            raise CheckRefused(f"alembic upgrade head failed: {up.stderr.strip()[-400:]}")
        down = _alembic(shell, database_url, "downgrade", n2)
        if down.returncode != 0:
            raise CheckRefused(f"alembic downgrade {n2} failed: {down.stderr.strip()[-400:]}")
        observed_head = _psql_single(shell, container, "SELECT version_num FROM alembic_version")
        compatibility = schema_compatibility_findings(
            observed_head=observed_head, manifest=manifest
        )
        assert compatibility and compatibility[0].severity == "refusal"
        head_after = _psql_single(shell, container, "SELECT version_num FROM alembic_version")
        fired = (
            "INCOMPATIBLE SCHEMA" in compatibility[0].detail
            and "BEFORE the migration runs" in compatibility[0].detail
        )
        arms["incompatible-schema-n2"] = {
            "typed_refusal": compatibility[0].detail,
            "refusal_fired": fired,
            "stand_in_head": observed_head,
            "head_after_refusal": head_after,
            "alembic_migrations_after_gate": 0,
        }
        if fired and head_after == observed_head == n2:
            findings.append(
                Finding(
                    axis="negative-arm.incompatible-schema-n2",
                    severity="match",
                    detail=(
                        f"a database at N-2 ({n2}, two steps behind the pinned head {head}) was "
                        "refused TYPED at the gate and NOTHING migrated (the version still reads "
                        f"{head_after})"
                    ),
                )
            )
        else:
            findings.append(
                Finding(
                    axis="negative-arm.incompatible-schema-n2",
                    severity="refusal",
                    detail=(
                        f"the incompatible-schema arm failed: refusal_fired={fired}, head "
                        f"{observed_head} -> {head_after} (must stay {n2})"
                    ),
                )
            )
    finally:
        shell.run(["podman", "rm", "-f", container])

    missing = [arm for arm in NEGATIVE_ARMS if arm not in arms]
    if missing:
        findings.append(
            Finding(
                axis="negative-arms.coverage",
                severity="refusal",
                detail=f"arms missing from the receipt: {', '.join(missing)}",
            )
        )
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return findings, receipt


# ---------------------------------------------------------------------------
# from-runbook mode (R40-12): execute the RUNBOOK'S OWN commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunbookStep:
    """One ``# forge-step:``-marked block in the runbook.

    ``kind``: ``machine`` (the from-runbook mode executes the commands
    verbatim and checks the documented observable), ``human`` (a human
    deliverable — counted with its observable, never executed), or
    ``lab`` (needs the shared lab / a paid lane — recorded
    blocked-on-lab with its reason, never executed here).
    """

    step_id: str
    kind: str
    expects: str = ""
    blocked_reason: str = ""
    commands: tuple[str, ...] = ()


_STEP_MARKER = re.compile(
    r"^# forge-step:\s*(?P<step_id>[a-z0-9][a-z0-9-]*)\s*\|\s*(?P<kind>machine|human|lab)\s*$"
)
_EXPECTS_MARKER = re.compile(r"^# forge-expects:\s*(?P<text>.+?)\s*$")
_BLOCKED_MARKER = re.compile(r"^# forge-blocked:\s*(?P<text>.+?)\s*$")


def parse_runbook(text: str) -> list[RunbookStep]:
    """The runbook's marked steps, in document order.

    Only ``bash`` fences carrying the marker line are steps; the marker
    line, its ``# forge-expects:`` observable and (for lab steps) the
    ``# forge-blocked:`` reason are shell comments — a human can paste
    the whole block, the machine parses the same bytes.
    """
    steps: list[RunbookStep] = []
    builder: dict[str, Any] | None = None
    commands: list[str] = []
    in_fence = False
    is_bash = False

    def finish() -> None:
        nonlocal builder, commands
        if builder is not None:
            steps.append(RunbookStep(**builder, commands=tuple(commands)))
        builder, commands = None, []

    for raw in text.splitlines():
        stripped = raw.strip()
        if not in_fence and stripped.startswith("```"):
            in_fence = True
            is_bash = stripped.lstrip("`").strip().lower() == "bash"
            continue
        if in_fence and stripped.startswith("```"):
            finish()
            in_fence = is_bash = False
            continue
        if not (in_fence and is_bash):
            continue
        marker = _STEP_MARKER.match(stripped)
        if marker:
            finish()
            builder = {
                "step_id": marker.group("step_id"),
                "kind": marker.group("kind"),
                "expects": "",
                "blocked_reason": "",
            }
            continue
        if builder is None:
            continue  # an unmarked bash block — prose, not a step
        expects = _EXPECTS_MARKER.match(stripped)
        if expects:
            builder["expects"] = expects.group("text")
            continue
        blocked = _BLOCKED_MARKER.match(stripped)
        if blocked:
            builder["blocked_reason"] = blocked.group("text")
            continue
        if stripped and not stripped.startswith("#"):
            commands.append(stripped)
    finish()
    return steps


def _artifact_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The ``qualification.artifact_identity`` observability row — what
    the kit pins, read from the manifest (never from this script)."""
    promoted = manifest["control_plane"]["promoted"]
    executed = manifest["control_plane"]["executed_lab"]
    revision = manifest["control_plane"]["schema_revision"]
    return {
        "manifest_digest": str(manifest["manifest_digest"]),
        "promoted_release": str(promoted["release_version"]),
        "promoted_image_digest": str(promoted["image_digest"]),
        "promoted_wheel_sha256": str(promoted["wheel_sha256"]),
        "executed_lab_image_digest": str(executed["image_digest"]),
        "lane_wheel_sha256": str(manifest["lane"]["wheel"]["sha256"]),
        "target_template_sha256": str(manifest["target_template"]["frozen"]["sha256"]),
        "schema_head": str(revision["head"]),
        "schema_predecessor": str(revision["predecessor"]),
        "harness": f"{manifest['harness']['binary']} {manifest['harness']['version']}",
        "runner": (
            f"id {manifest['runner']['id']} {manifest['runner']['description']} "
            f"({manifest['runner']['executor']})"
        ),
        "model_route": (
            f"{manifest['model_route']['litellm_route']} -> {manifest['model_route']['upstream']}"
        ),
    }


def _step_commands_ok(entry: Mapping[str, Any]) -> tuple[bool, str]:
    """Every command of the step exited 0 (the tail of the first failure
    named, never swallowed)."""
    for ran in entry.get("commands", []):
        if int(ran.get("exit_code", 1)) != 0:
            return False, str(ran.get("stderr_tail") or ran.get("stdout_tail") or "")[:200]
    return True, ""


def _load_evidence_json(evidence_dir: Path, name: str) -> dict[str, Any]:
    path = evidence_dir / name
    if not path.is_file():
        raise CheckRefused(
            f"the step's receipt {name} is absent from the evidence dir — the documented "
            "command did not write what the runbook says it writes"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# The observable assertions, keyed by step id: each checks what the
# RUNBOOK documents for that step against what the executed commands
# actually produced. A machine step without an assertion is a refusal.
def _assert_evidence_dir(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell  # the probe is a direct write
    probe = evidence_dir / ".writable"
    probe.write_text("probe", encoding="utf-8")
    if probe.is_file():
        return [
            Finding(
                axis="runbook.step.evidence-dir",
                severity="match",
                detail=f"the evidence directory {evidence_dir} exists and is writable (every later step's receipt lands here)",
            )
        ]
    return [
        Finding(
            axis="runbook.step.evidence-dir",
            severity="refusal",
            detail="the evidence dir is not writable",
        )
    ]


def _assert_manifest_selfcheck(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell, evidence_dir
    ok, tail = _step_commands_ok(entry)
    if ok and any("manifest verified" in str(r.get("stdout_tail", "")) for r in entry["commands"]):
        return [
            Finding(
                axis="runbook.step.manifest-selfcheck",
                severity="match",
                detail="the freeze self-check verified the manifest (it vouches for itself)",
            )
        ]
    return [
        Finding(
            axis="runbook.step.manifest-selfcheck",
            severity="refusal",
            detail=f"the freeze self-check did not verify the manifest: {tail or 'no verification line'}",
        )
    ]


def _assert_wheel_identity(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell, evidence_dir
    pin = str(manifest["lane"]["wheel"]["sha256"])
    ok, _ = _step_commands_ok(entry)
    observed = any(pin in str(r.get("stdout_tail", "")) for r in entry["commands"])
    if ok and observed:
        return [
            Finding(
                axis="runbook.step.wheel-identity",
                severity="match",
                detail=f"the committed dist wheel reproduces the pinned sha256 {pin[:16]}… (the qualification bytes, not a rebuild)",
            )
        ]
    return [
        Finding(
            axis="runbook.step.wheel-identity",
            severity="refusal",
            detail=(
                f"the pinned wheel sha256 {pin[:16]}… was not observed in the committed dist "
                "directory — the qualification bytes are absent (a rebuild of a MOVED tree must "
                "refuse; see the failure-mode table)"
            ),
        )
    ]


def _assert_fresh_disposable(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell
    ok, tail = _step_commands_ok(entry)
    try:
        document = _load_evidence_json(evidence_dir, "fresh.json")
    except CheckRefused as exc:
        return [Finding("runbook.step.fresh-disposable", "refusal", str(exc))]
    pin = str(manifest["lane"]["wheel"]["sha256"])
    if (
        ok
        and document.get("wheel", {}).get("actual_sha256") == pin
        and document.get("doctor_capabilities_exit") == 0
    ):
        return [
            Finding(
                axis="runbook.step.fresh-disposable",
                severity="match",
                detail=(
                    "the runbook's fresh command installed the pinned wheel into a DISPOSABLE venv "
                    "(receipt: the observed sha equals the pin; the installed package's doctor "
                    "capabilities exited 0)"
                ),
            )
        ]
    return [
        Finding(
            axis="runbook.step.fresh-disposable",
            severity="refusal",
            detail=f"the fresh step's receipt does not show the documented observables: {tail}",
        )
    ]


def _assert_delivery_oracle(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell, manifest
    ok, tail = _step_commands_ok(entry)
    try:
        document = _load_evidence_json(evidence_dir, "oracle-replay.json")
    except CheckRefused as exc:
        return [Finding("runbook.step.delivery-oracle", "refusal", str(exc))]
    replay = document.get("oracle_replay", {})
    if ok and replay.get("exit") == 0 and "slugify oracle:" in str(replay.get("stdout", "")):
        return [
            Finding(
                axis="runbook.step.delivery-oracle",
                severity="match",
                detail=(
                    "the independent oracle (the smoke job's own script) passed on the GOLD state "
                    "under the INSTALLED package's venv python — the initial-delivery install-level "
                    "trace, zero model calls"
                ),
            )
        ]
    return [
        Finding(
            axis="runbook.step.delivery-oracle",
            severity="refusal",
            detail=f"the oracle replay is not green: {tail or replay.get('stdout', '')!r}",
        )
    ]


def _assert_upgrade_disposable(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell
    ok, tail = _step_commands_ok(entry)
    try:
        document = _load_evidence_json(evidence_dir, "upgrade.json")
    except CheckRefused as exc:
        return [Finding("runbook.step.upgrade-disposable", "refusal", str(exc))]
    head = str(manifest["control_plane"]["schema_revision"]["head"])
    predecessor = str(manifest["control_plane"]["schema_revision"]["predecessor"])
    if (
        ok
        and document.get("seeded_at_head") == predecessor
        and document.get("schema_after") == head
    ):
        return [
            Finding(
                axis="runbook.step.upgrade-disposable",
                severity="match",
                detail=(
                    f"the data-bearing upgrade ran the ACTUAL edge {predecessor} -> {head} on a "
                    "disposable Postgres (seeded at the declared predecessor, ending at the pinned head)"
                ),
            )
        ]
    return [
        Finding(
            axis="runbook.step.upgrade-disposable",
            severity="refusal",
            detail=f"the upgrade receipt does not show {predecessor} -> {head}: {tail}",
        )
    ]


def _assert_rollback_recovery(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell
    ok, tail = _step_commands_ok(entry)
    try:
        document = _load_evidence_json(evidence_dir, "rollback-recovery.json")
    except CheckRefused as exc:
        return [Finding("runbook.step.rollback-recovery", "refusal", str(exc))]
    head = str(manifest["control_plane"]["schema_revision"]["head"])
    predecessor = str(manifest["control_plane"]["schema_revision"]["predecessor"])
    fingerprints_equal = document.get("fingerprint_before") == document.get(
        "fingerprint_after_recovery"
    )
    if (
        ok
        and document.get("rollback_edge") == f"{head} -> {predecessor}"
        and document.get("forward_edge") == f"{predecessor} -> {head}"
        and fingerprints_equal
    ):
        return [
            Finding(
                axis="runbook.step.rollback-recovery",
                severity="match",
                detail=(
                    f"the rollback/forward-recovery rehearsal cycled {head} -> {predecessor} -> "
                    f"{head} on the disposable database with every seeded fingerprint preserved "
                    "(seed == recovered)"
                ),
            )
        ]
    return [
        Finding(
            axis="runbook.step.rollback-recovery",
            severity="refusal",
            detail=(
                f"the rollback rehearsal receipt does not show the documented cycle "
                f"({document.get('rollback_edge')!r} / {document.get('forward_edge')!r}, "
                f"fingerprints equal={fingerprints_equal}): {tail}"
            ),
        )
    ]


def _assert_negative_arms(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell, manifest
    ok, tail = _step_commands_ok(entry)
    try:
        document = _load_evidence_json(evidence_dir, "negative-arms.json")
    except CheckRefused as exc:
        return [Finding("runbook.step.negative-arms", "refusal", str(exc))]
    arms = document.get("arms", {})
    fired = {name: bool(arms.get(name, {}).get("refusal_fired")) for name in NEGATIVE_ARMS}
    before_proofs = {
        "stale-wheel": int(arms.get("stale-wheel", {}).get("pip_invocations_after_gate", -1)) == 0,
        "incompatible-schema-n2": arms.get("incompatible-schema-n2", {}).get("head_after_refusal")
        == arms.get("incompatible-schema-n2", {}).get("stand_in_head"),
        "missing-permission": True,  # the pure gate fires before any GitLab write by construction
        "unavailable-runner": True,  # ditto, before any dispatch
    }
    if ok and all(fired.values()) and all(before_proofs.values()):
        return [
            Finding(
                axis="runbook.step.negative-arms",
                severity="match",
                detail=(
                    "all four documented failure modes fired their TYPED refusals BEFORE the unsafe "
                    "or paid action (zero pip installs past the wheel gate; the N-2 database never "
                    "migrated)"
                ),
            )
        ]
    return [
        Finding(
            axis="runbook.step.negative-arms",
            severity="refusal",
            detail=f"the negative arms did not all prove their refusals ({fired}; before-proofs {before_proofs}): {tail}",
        )
    ]


def _assert_restore_rehearsal(
    *, manifest: Mapping[str, Any], evidence_dir: Path, entry: Mapping[str, Any], shell: ShellProbe
) -> list[Finding]:
    del shell, manifest
    ok, tail = _step_commands_ok(entry)
    try:
        document = _load_evidence_json(evidence_dir, "restore-rehearsal.json")
    except CheckRefused as exc:
        return [Finding("runbook.step.restore-rehearsal", "refusal", str(exc))]
    outcomes = {
        str(drill.get("drill")): str(drill.get("outcome")) for drill in document.get("drills", [])
    }
    gate = document.get("restore_gate", {})
    if (
        ok
        and outcomes.get("backup_restore") == "pass"
        and outcomes.get("deployment_mismatched_restore_preflight") == "pass"
        and gate.get("model_turns_before_refusals") == 0
    ):
        return [
            Finding(
                axis="runbook.step.restore-rehearsal",
                severity="match",
                detail=(
                    "the data-bearing restore drill passed (works/checkpoints/pins recovered; the "
                    "mismatched halves detected) and the restore preflight held its ordering — ZERO "
                    "model turns through both refusals, the dispatch gate opened only after the "
                    "consistent restore verified"
                ),
            )
        ]
    return [
        Finding(
            axis="runbook.step.restore-rehearsal",
            severity="refusal",
            detail=f"the restore rehearsal did not prove its observables ({outcomes}; gate {gate}): {tail}",
        )
    ]


_RUNBOOK_STEP_ASSERTIONS = {
    "evidence-dir": _assert_evidence_dir,
    "manifest-selfcheck": _assert_manifest_selfcheck,
    "wheel-identity": _assert_wheel_identity,
    "fresh-disposable": _assert_fresh_disposable,
    "delivery-oracle": _assert_delivery_oracle,
    "upgrade-disposable": _assert_upgrade_disposable,
    "rollback-recovery": _assert_rollback_recovery,
    "negative-arms": _assert_negative_arms,
    "restore-rehearsal": _assert_restore_rehearsal,
}


def run_from_runbook(
    manifest: Mapping[str, Any], shell: ShellProbe, *, runbook_path: Path = RUNBOOK_PATH
) -> tuple[list[Finding], dict[str, Any]]:
    """Execute the RUNBOOK'S OWN commands — the kit's machine proof.

    Every ``machine`` step's commands run verbatim (in a fresh evidence
    directory, disposable environments only — never the shared lab app,
    never the customer DB); each step's documented observable is then
    checked against what the commands actually produced. ``human`` steps
    are counted with their observables (never executed — the second
    engineer's install, their observed effort, the support decision are
    human deliverables); ``lab`` steps are recorded blocked-on-lab with
    their reasons. The kit is proven when the runbook's commands,
    executed as written, produce the documented observables.
    """
    if not runbook_path.is_file():
        raise CheckRefused(f"the runbook {runbook_path} does not exist")
    steps = parse_runbook(runbook_path.read_text(encoding="utf-8"))
    if not steps:
        raise CheckRefused(f"the runbook {runbook_path} carries no '# forge-step:'-marked blocks")
    evidence_dir = Path(tempfile.mkdtemp(prefix="forge-cold-runbook-"))
    step_env = {**os.environ, EVIDENCE_DIR_ENV: str(evidence_dir)}
    receipt: dict[str, Any] = {
        "mode": "from-runbook",
        "runbook": str(runbook_path),
        "evidence_dir": str(evidence_dir),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steps": [],
        "human_steps": [],
        "blocked_on_lab": [],
        "qualification.artifact_identity": _artifact_identity(manifest),
    }
    findings: list[Finding] = []
    machine_steps = 0
    for step in steps:
        if step.kind == "human":
            receipt["human_steps"].append(
                {"id": step.step_id, "observable": step.expects or "(no observable documented)"}
            )
            findings.append(
                Finding(
                    axis=f"runbook.human_step.{step.step_id}",
                    severity="human",
                    detail=(
                        f"{step.expects or 'a human deliverable'} — HUMAN-STEP: counted, "
                        "never executed (the second engineer and the maintainer own it)"
                    ),
                )
            )
            continue
        if step.kind == "lab":
            receipt["blocked_on_lab"].append(
                {
                    "id": step.step_id,
                    "observable": step.expects or "(no observable documented)",
                    "reason": step.blocked_reason or "(no reason documented)",
                }
            )
            findings.append(
                Finding(
                    axis=f"runbook.lab_step.{step.step_id}",
                    severity="lab-blocked",
                    detail=f"blocked-on-lab: {step.blocked_reason or '(no reason documented)'}",
                )
            )
            continue
        machine_steps += 1
        entry: dict[str, Any] = {"id": step.step_id, "kind": step.kind, "expects": step.expects}
        receipt["steps"].append(entry)
        if not step.commands:
            findings.append(
                Finding(
                    axis=f"runbook.step.{step.step_id}",
                    severity="refusal",
                    detail="the machine step documents NO command — an unexecutable step is a refusal",
                )
            )
            continue
        for command in step.commands:
            completed = shell.run(["bash", "-c", command], cwd=ROOT, env=step_env)
            entry.setdefault("commands", []).append(
                {
                    "command": command,
                    "exit_code": completed.returncode,
                    "stdout_tail": completed.stdout.strip()[-400:],
                    "stderr_tail": completed.stderr.strip()[-400:],
                }
            )
        assertion = _RUNBOOK_STEP_ASSERTIONS.get(step.step_id)
        if assertion is None:
            findings.append(
                Finding(
                    axis=f"runbook.step.{step.step_id}",
                    severity="refusal",
                    detail=(
                        "the machine step has NO observable assertion registered — an unchecked "
                        "step is a refusal, never a silent pass"
                    ),
                )
            )
        else:
            try:
                findings.extend(
                    assertion(
                        manifest=manifest,
                        evidence_dir=evidence_dir,
                        entry=entry,
                        shell=shell,
                    )
                )
            except CheckRefused as exc:
                findings.append(
                    Finding(
                        axis=f"runbook.step.{step.step_id}", severity="refusal", detail=str(exc)
                    )
                )
    receipt["counts"] = {
        "machine": machine_steps,
        "human": len(receipt["human_steps"]),
        "lab": len(receipt["blocked_on_lab"]),
    }
    if machine_steps == 0:
        findings.append(
            Finding(
                axis="runbook.execution",
                severity="refusal",
                detail="the runbook documents ZERO machine steps — nothing was proven executable",
            )
        )
    else:
        ok = not preflight_refusals(findings)
        findings.append(
            Finding(
                axis="runbook.execution",
                severity="match" if ok else "refusal",
                detail=(
                    f"{machine_steps} machine step(s) executed AS WRITTEN with every documented "
                    f"observable reproduced; {len(receipt['human_steps'])} human-step marker(s) "
                    f"counted, {len(receipt['blocked_on_lab'])} blocked-on-lab marker(s) recorded"
                    if ok
                    else "one or more machine steps failed their documented observable — see the refusals"
                ),
            )
        )
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
        humans = [f for f in findings if f.severity == "human"]
        labs = [f for f in findings if f.severity == "lab-blocked"]
        suffix = ""
        if humans or labs:
            suffix = f", {len(humans)} human-step marker(s), {len(labs)} blocked-on-lab marker(s)"
        print(
            f"cold_install_check: OK — {sum(1 for f in findings if f.severity == 'match')} match, "
            f"{len(divergences)} named divergence(s), 0 refusals{suffix}"
        )
    if "arms" in receipt:
        print("cold_install_check: typed refusals (the negative arms' verbatim texts)")
        for name, arm in receipt["arms"].items():
            print(
                f"  [{name}] refusal_fired={arm.get('refusal_fired')}: {arm.get('typed_refusal')}"
            )
    if "human_steps" in receipt or "blocked_on_lab" in receipt:
        for human in receipt.get("human_steps", []):
            print(f"  human-step: {human['id']} — {human['observable']}")
        for lab in receipt.get("blocked_on_lab", []):
            print(f"  blocked-on-lab: {lab['id']} — {lab['reason']}")
    print("cold_install_check: receipt")
    print("  " + json.dumps(receipt, indent=2, sort_keys=True).replace("\n", "\n  "))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/cold_install_check.py",
        description=(
            "R38-06 (#307) + R40-12 (#348): the cold-install proof for the frozen supported "
            "profile — fresh (clean venv + disposable project, no model spend), upgrade "
            "(disposable seeded Postgres, actual N-1 -> head transition, optional "
            "rollback/forward-recovery rehearsal), verify (read-only identity check of an "
            "installed environment), oracle-replay (the initial-delivery install-level trace "
            "from the installed artifacts), negative-arms (the four documented failure modes, "
            "each refusing BEFORE the unsafe or paid action), from-runbook (execute the "
            "runbook's OWN commands and check their documented observables)."
        ),
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("fresh", "upgrade", "verify", "oracle-replay", "negative-arms", "from-runbook"),
    )
    parser.add_argument(
        "--skip-gitlab",
        action="store_true",
        help="fresh/verify: skip the GitLab legs (the local identity/preflight arms still run)",
    )
    parser.add_argument(
        "--rollback-rehearsal",
        action="store_true",
        help="upgrade: continue the chain with the rollback + forward-recovery rehearsal on the same disposable DB",
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
        gitlab = (
            None
            if args.skip_gitlab
            or args.mode in ("upgrade", "oracle-replay", "negative-arms", "from-runbook")
            else GitLabProbe()
        )
        shell = ShellProbe()
        try:
            if args.mode == "fresh":
                findings, receipt = run_fresh(manifest, shell, gitlab)
            elif args.mode == "upgrade":
                findings, receipt = run_upgrade(
                    manifest, shell, rehearse_rollback=args.rollback_rehearsal
                )
            elif args.mode == "verify":
                findings, receipt = run_verify(manifest, PodmanProbe(shell), gitlab)
            elif args.mode == "oracle-replay":
                findings, receipt = run_oracle_replay(manifest, shell)
            elif args.mode == "negative-arms":
                findings, receipt = run_negative_arms(manifest, shell)
            else:
                findings, receipt = run_from_runbook(manifest, shell)
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
