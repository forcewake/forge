#!/usr/bin/env python3
"""The native template + secret-consumer conformance gate (R38-16 / #317).

The boundary failures this gate exists for (both shipped in green-CI
releases):

- the SDK lanes' packaged shell ended with an unconditional driver-rc
  exit — a SUCCESSFUL driver left a green job without its delivery
  artifact (R38-01 / #302; the regression suite
  ``tests/test_gitlab_sdk_lane_finalization.py`` executes the packaged
  shell, and this gate PROMOTES that execution into the release gate);
- fake native ledgers accepted dispatch inputs the shipped workflow does
  not declare, and the #303 credential delivery landed with no
  consumer-side proof in the gate (R38-02 / #303).

Five checks, every push (the lint job — fast, no database, no provider):

1. ``shipped-recipes`` — every ``ci/templates/*`` recipe carrying the
   finalization/driver block is YAML-parsed, its finalization script
   extracted VERBATIM and executed under REAL Bash in a REAL git checkout
   through #302's own fixtures (the stub ``FORGE_LANE_PYTHON`` driver leg
   + the REAL packaged collector): success / failure / no-op /
   restored-generation. The #302 mutation — an unconditional early exit
   restored before collection — is replayed as a SELF-TEST arm on every
   template and MUST be caught; an uncaught mutation refuses the gate
   itself (the gate proved itself insensitive).
2. ``dispatch-schema`` — the shipped templates' DECLARED dispatch surface
   (GitHub ``on.workflow_dispatch.inputs``, Azure ``parameters``, the
   GitLab recipes' consumed pipeline variables) validated against the
   CAPTURED production dispatch payload shapes
   (``scripts/conformance_dispatch_captures.json`` — fresh captures
   through the production-entry fakes, regenerated with
   ``--regenerate-captures`` and drift-pinned by
   ``tests/test_gate_conformance.py``). An undeclared key the service
   sends, or a declared key never sent (nor conditionally sent from a
   source-verified dispatch site), is a typed finding.
3. ``secret-consumers`` — every recipe carrying a credential-consumption
   block (the #303 env mappings) is rendered with a DELIVERED sentinel
   beside a DIFFERENT ambient sentinel in the parent env and executed:
   the consumed env must carry exactly the sentinel the block selects,
   the same family's stray variables must be scrubbed, an empty carrier
   must fail CLOSED, and the mutation dropping the consumer mapping must
   be CAUGHT despite a correct-looking receipt. Q39-04 (#323) adds the
   ``sentinel_locator`` arm per recipe: the block executed with a
   LOCATOR-shaped ref + carrier (the collision-safe spelling the
   locator registry dispatches) — the delivered sentinel must still be
   the consumed one.
4. ``native-locators`` (Q39-04 / #323) — the collision-safe locator
   dimension: the P05 pair (``vault:kv/team-a`` vs ``vault:kv/team_a``)
   and its slash/dash/underscore/case variants map to DISTINCT locators
   within the provider charset; the per-DRIVER template set validates
   the negotiated route against EACH driver's own recipe (the GitLab
   SDK lanes and batch routes are different files — a driver whose
   template does not implement the route refuses, never a
   claude-code-shaped pass); the INSTALLED-template digest inventory is
   recorded per validated template, with the digest-mismatch and
   unknown-driver refusals replayed as SELF-TEST arms (the legacy lossy
   encoder ``credential_secret_segment`` is replayed as the sensitivity
   proof — it MUST be flagged).
5. ``consumer-contracts`` (Q39-08 / #327) — the NEW contracts (#320 the
   operation grant, #321 the revision rebind) proven through their
   ACTUAL consumers, offline and deterministic, sentinel values only:
   the grant is redeemed through the REAL ASGI endpoint
   (``GET /lane/credentials/redeem`` over ``httpx.ASGITransport`` on the
   REAL ``create_app`` app): the granted route redeems the delivered
   sentinel, the SIBLING route of the same project refuses with typed
   ``grant_route_mismatch`` and ZERO broker calls, and a grant document
   whose every DTO value is intact but parked where the production
   caller cannot load it authorizes NOTHING (the issue's named
   DTO-preserving disconnect trap). The runner-side typed verification
   (#320's CD-9 shape) runs as REAL ``python -m forge.lane_driver``
   subprocesses against a CANNED endpoint: the correct document's
   baseline passes the SAME trace, then a wrong-slot and an expired
   answer each halt the lane with ``credential_redemption_failed`` and
   ZERO calls at the fake model endpoint. The #321 rebind digest rides
   a REAL ``RunService`` dispatch through the fake native server's
   GitLab mode: the persisted ``revision.executor_input_digest`` equals
   the digest recomputed from the RECORDED dispatch variables, and the
   dispatched ``FORGE_BRIEF_ENVELOPE_DIGEST`` verifies over the
   dispatched ``FORGE_PLAN`` bytes — the offline three-way equality
   (the PE trace stays the deeper proof). A comment-only marker spoof
   (the block's every line re-spelled as a comment) must NEVER satisfy
   the consumer-block extraction.

Usage::

    uv run python scripts/gate_conformance.py --report conformance-gate.json

Exit codes: 0 green · 2 prerequisite · 3 shipped-recipe execution ·
4 dispatch-schema findings · 5 secret-consumer sentinel · 6 mutation
escape (the gate is insensitive — the most severe outcome there is) ·
7 native-locator conformance · 8 consumer-contract conformance.

Relationship to the PG gate (``scripts/pg_gate.py``, #267): this gate
runs on EVERY push (lint); the PG profiles run on the integration job
against real PostgreSQL. Both write an executed-ID manifest; neither
ever converts a failure into a skip. The accounting-race contracts
(#322 the CAS projection, #324 the partial→final reconcile) live on
the PG gate's REQUIRED selection — the executed-ID manifest covers
them there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "ci" / "templates"
CAPTURES_PATH = REPO_ROOT / "scripts" / "conformance_dispatch_captures.json"

#: The committed capture-fixture schema (drift-pinned by the capture tests).
CAPTURES_SCHEMA = "forge.conformance-dispatch-captures/1"

GITHUB_TEMPLATE = "forge-harness.github.yml"
AZURE_TEMPLATE = "forge-lane.azure-pipelines.yml"

#: The GitLab recipes that MUST carry a credential-consumption block today
#: (the #303 anthropic-route lanes). Recipes discovered by scan beyond
#: this set activate the sentinel arms automatically; a recipe outside
#: the set without a block is recorded ``absent`` (visible, not failing —
#: the block rollout to the other SDK lanes is #305's scope).
GITLAB_CREDENTIAL_BLOCK_REQUIRED = (
    "claude-code.gitlab-ci.yml",
    "claude-sdk-lane.gitlab-ci.yml",
)

#: Sentinel fixture values (never real credentials): the value the
#: delivery channel carries vs the DIFFERENT ambient value that must
#: never reach the model process.
DELIVERED_SENTINEL = "conf-gate-delivered-91f4c7a2"  # noqa: S105 — a fixture value
AMBIENT_SENTINEL = "conf-gate-ambient-never-3d05be86"  # noqa: S105 — a fixture value

#: The credential slots the sentinel probe observes: the consumed
#: anthropic-route variable plus the same-family strays the block must
#: scrub (the CLI's documented precedence would otherwise let a stray
#: outrank the delivered credential).
PROBE_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

#: Per-provider scrub guarantees: which strays each shipped recipe's
#: block actually unsets. The gitlab lanes and the GitHub template scrub
#: the whole anthropic family; the Azure block currently guarantees only
#: CLAUDE_CODE_OAUTH_TOKEN (its ANTHROPIC_API_KEY posture is observed —
#: the probe still records it — but is not a gate assertion on that
#: sibling-owned surface).
SCRUB_GUARANTEE: dict[str, tuple[str, ...]] = {
    "gitlab-lane": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "github": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "azure": ("CLAUDE_CODE_OAUTH_TOKEN",),
}

#: The dispatch key the credential ref rides under (the #303 spelling).
CREDENTIAL_REF_ENV = "FORGE_CREDENTIAL_REF"
CREDENTIAL_REDEEM_ENV = "FORGE_CREDENTIAL_REDEEM"

#: The P05 collision probe pair (Q39-04 / #323): the two refs that
#: COLLIDED under the legacy lossy segment encoder (both sanitized to
#: ``VAULT_KV_TEAM_A``). Fixture refs, never values.
LOCATOR_COLLISION_PAIR = ("vault:kv/team-a", "vault:kv/team_a")

#: The locator-distinctness probe set: refs differing by slash, dash,
#: underscore, dot and case — plus the canonical env ref — must ALL map
#: to pairwise-distinct locators (the acceptance criterion AC-01).
LOCATOR_PROBE_REFS = (
    *LOCATOR_COLLISION_PAIR,
    "vault:kv-team-a",
    "vault:kv/Team-A",
    "vault:kv/team.a",
    "vault:kv/TEAM_A",
    "env:ANTHROPIC_AUTH_TOKEN",
)

# ---------------------------------------------------------------------------
# Check 5 (Q39-08 / #327) — the consumer-contract fixtures. Sentinel
# values ONLY, never a real credential; the values never enter the
# report (boolean expectations + refs, as ever).
# ---------------------------------------------------------------------------

#: The grant arm's work identity (the P01 world: ONE project, TWO live
#: bindings — the dispatched anthropic route and the sibling openai one).
GRANT_WORK_ID = "conf-grant-work-1"
GRANT_PROJECT_ID = 90327
GRANT_SECRET = "conf-grant-lane-secret-5c1f"  # noqa: S105 — a fixture value
GRANT_GENERATION = 2

#: The two sibling routes' refs and the delivered sentinels their staged
#: broker double carries (refs are spellings, values are sentinels).
GRANT_REF = "env:ANTHROPIC_AUTH_TOKEN"
GRANT_SIBLING_REF = "env:OPENAI_API_KEY"
GRANT_PROVIDER = "anthropic-gateway"
GRANT_SIBLING_PROVIDER = "openai"

#: The rebind arm's revision world (the #321 counterexample's rename
#: shape, staged offline): revision 1 renames the entrypoint; the spec
#: brief still names the old one.
REBIND_OLD_NAME = "check"
REBIND_NEW_NAME = "validate_email"

# ---------------------------------------------------------------------------
# Typed refusals — each is a distinct, non-green outcome (never a skip)
# ---------------------------------------------------------------------------


class ConformanceGateError(Exception):
    """Base class: the gate refuses to qualify."""

    exit_code = 2


class PrerequisiteError(ConformanceGateError):
    """Templates/captures missing or unparseable; bash/git unavailable."""

    exit_code = 2


class RecipeExecutionError(ConformanceGateError):
    """A shipped recipe's executed shell missed its contract."""

    exit_code = 3


class SchemaConformanceError(ConformanceGateError):
    """The declared dispatch surface and the captured payloads disagree."""

    exit_code = 4


class SecretConsumerError(ConformanceGateError):
    """A secret-consumer sentinel arm failed."""

    exit_code = 5


class MutationEscapeError(ConformanceGateError):
    """A mutation the gate exists to catch went UNCAUGHT — the gate is
    insensitive, which is worse than any single recipe failing."""

    exit_code = 6


class LocatorConformanceError(ConformanceGateError):
    """A native-locator conformance arm failed (Q39-04 / #323): the
    collision-safe encoding, the per-driver template route, or the
    installed-template digest dimension."""

    exit_code = 7


class ConsumerContractError(ConformanceGateError):
    """A consumer-contract arm failed (Q39-08 / #327): the operation
    grant through the real ASGI endpoint, the runner-side typed
    verification, or the revision-rebind digest through the recorded
    dispatch."""

    exit_code = 8


# ---------------------------------------------------------------------------
# The #302 test machinery, reused BY IMPORT (never a second copy)
# ---------------------------------------------------------------------------


def _load_seams() -> dict[str, Any]:
    """Import the #302 / #260 test fixtures the gate executes through.

    The seam functions (block extraction, lane env, the stub interpreter,
    the checkout/generation builders) are the SAME ones the regression
    suite uses — the gate can never drift from the tests it promotes.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from tests.test_candidate_collector import (  # noqa: PLC0415
            CHECKPOINT_ID,
            WORK_ID,
            make_checkout,
            make_generation,
        )
        from tests.test_gitlab_sdk_lane_finalization import (  # noqa: PLC0415
            SDK_LANE_TEMPLATES,
            finalization_block,
            lane_env,
            load_meta,
            outcome as lane_outcome,
            write_stub_lane_python,
        )
        from tests.test_templates import HARNESS_TEMPLATES  # noqa: PLC0415

        from forge.adaptive.credential_broker import (  # noqa: PLC0415
            DELIVERY_MODE_AZURE_GROUP,
            DELIVERY_MODE_GITHUB_NATIVE,
            DELIVERY_MODE_GITLAB_PROTECTED,
            CredentialDeliveryPlan,
            credential_secret_name,
            credential_secret_segment,
            delivery_template_conformance,
            native_locator,
            native_route_structural_gaps,
            template_identity_digest,
            DRIVER_TEMPLATE_FILES,
        )
    except Exception as exc:  # noqa: BLE001 — any import break is the same refusal
        raise PrerequisiteError(
            f"the #302/#260 test seams could not be imported (run via "
            f"`uv run python scripts/gate_conformance.py` from the repo root): {exc}"
        ) from exc
    return {
        "CHECKPOINT_ID": CHECKPOINT_ID,
        "WORK_ID": WORK_ID,
        "make_checkout": make_checkout,
        "make_generation": make_generation,
        "SDK_LANE_TEMPLATES": tuple(SDK_LANE_TEMPLATES),
        "finalization_block": finalization_block,
        "lane_env": lane_env,
        "load_meta": load_meta,
        "lane_outcome": lane_outcome,
        "write_stub_lane_python": write_stub_lane_python,
        "HARNESS_TEMPLATES": tuple(HARNESS_TEMPLATES),
        "credential_secret_segment": credential_secret_segment,
        "credential_secret_name": credential_secret_name,
        "native_locator": native_locator,
        "native_route_structural_gaps": native_route_structural_gaps,
        "template_identity_digest": template_identity_digest,
        "driver_template_files": dict(DRIVER_TEMPLATE_FILES),
        "delivery_plan_template": CredentialDeliveryPlan,
        "delivery_template_conformance": delivery_template_conformance,
        "gitlab_protected_mode": DELIVERY_MODE_GITLAB_PROTECTED,
        "github_native_mode": DELIVERY_MODE_GITHUB_NATIVE,
        "azure_group_mode": DELIVERY_MODE_AZURE_GROUP,
    }


def _require_tools() -> None:
    missing = [
        tool
        for tool, probe in (("bash", ["bash", "-c", ":"]), ("git", ["git", "--version"]))
        if subprocess.run(probe, capture_output=True, timeout=30, check=False).returncode != 0
    ]
    if missing:
        raise PrerequisiteError(f"required tools unavailable: {missing}")
    if not TEMPLATES_DIR.is_dir():
        raise PrerequisiteError(f"the templates directory is missing: {TEMPLATES_DIR}")


# ---------------------------------------------------------------------------
# Template parsing (pure — unit-tested in tests/test_gate_conformance.py)
# ---------------------------------------------------------------------------

_FORGE_VARIABLE_RE = re.compile(r"\bFORGE_[A-Z0-9_]+")

#: The credential-consumption guard: the ``if`` line that opens the #303
#: block in every shipped recipe (gitlab lanes, the GitHub driver step,
#: the Azure driver step).
_CREDENTIAL_GUARD_RE = re.compile(
    r'^\s*if \[ -n "\$\{FORGE_CREDENTIAL_REF:-\}" \] && '
    r'\[ "\$\{FORGE_CREDENTIAL_REDEEM:-\}" != "1" \]; then'
)

#: The consumer mapping inside the block: the export that moves the
#: delivered credential into the provider env slot the model process
#: reads (``export ANTHROPIC_AUTH_TOKEN="$_CRED_VALUE"`` on the gitlab
#: lanes, ``="$FORGE_MODEL_CREDENTIAL"`` on GitHub).
_CONSUMER_EXPORT_RE = re.compile(r'^(?P<indent>\s*)export\s+(?P<var>[A-Z0-9_]+)="\$\w+"\s*$')

#: Where the driver phase ends in the SDK finalization block — the seam
#: the #302 mutation is restored after (an unconditional exit here is
#: exactly the shipped defect: the job died before the meta floor, the
#: collection and the markers).
_DRIVER_PHASE_END_RE = re.compile(r'echo "forge lane: driver_exit=')


def yaml_doc(path: Path) -> dict[str, Any]:
    """YAML-parse a shipped template (the only parse the gate trusts)."""
    import yaml  # noqa: PLC0415 — already a test dependency of the seams

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise PrerequisiteError(f"{path.name} did not parse into a YAML mapping")
    return doc


def github_declared_inputs(doc: dict[str, Any]) -> set[str]:
    """The declared workflow_dispatch input names (pyyaml reads bare
    ``on:`` as boolean True — normalize both spellings)."""
    trigger = doc.get("on", doc.get(True))
    if not isinstance(trigger, dict) or "workflow_dispatch" not in trigger:
        raise PrerequisiteError("the GitHub template declares no workflow_dispatch trigger")
    inputs = trigger["workflow_dispatch"].get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise PrerequisiteError("the GitHub template declares no workflow_dispatch inputs")
    return set(inputs)


def azure_declared_parameters(doc: dict[str, Any]) -> set[str]:
    """The declared queue-time parameter names."""
    parameters = doc.get("parameters")
    if not isinstance(parameters, list) or not parameters:
        raise PrerequisiteError("the Azure template declares no parameters")
    return {str(entry["name"]) for entry in parameters}


def referenced_forge_variables(text: str) -> set[str]:
    """Every FORGE_* pipeline variable the recipe references (the
    conservative consumed-surface: scripts, rules and comments that name
    the contract)."""
    return set(_FORGE_VARIABLE_RE.findall(text))


def credential_block(text: str) -> str | None:
    """The credential-consumption ``if..fi`` span, VERBATIM.

    A structural scan (the guard line to the matching ``fi`` at the same
    indentation) — not bash parsing; the shipped blocks are flat
    if/then/fi fragments, and the extraction failing loudly on a
    restructured block is the desired behavior.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _CREDENTIAL_GUARD_RE.match(line):
            continue
        indent = len(line) - len(line.lstrip())
        for end in range(index + 1, len(lines)):
            candidate = lines[end]
            if (
                re.fullmatch(r"\s*fi\s*", candidate)
                and (len(candidate) - len(candidate.lstrip())) == indent
            ):
                return "\n".join(lines[index : end + 1]) + "\n"
        raise PrerequisiteError(
            "the credential-consumption guard has no closing 'fi' at its own "
            f"indentation (line {index + 1}) — the block was restructured"
        )
    return None


def consumer_export_lines(block: str) -> list[str]:
    """The consumer-mapping lines inside the block (the export that
    delivers the credential into the provider env slot)."""
    return [line for line in block.splitlines() if _CONSUMER_EXPORT_RE.match(line)]


_LINE_COMMENT_RE = re.compile(r"^\s*#")


def strip_line_comments(text: str) -> str:
    """Drop full-line shell/YAML comments — the spoof detector's lens: a
    marker that survives ONLY as a comment disappears under it."""
    return "\n".join(line for line in text.splitlines() if not _LINE_COMMENT_RE.match(line))


def comment_only_spoof(text: str, block: str) -> str:
    """The Q39-08 spoof mutation: every line of the extracted block
    re-spelled as a comment — all the expected STRINGS still present in
    the file (a marker-grep would pass), the executable consumer gone."""
    block_lines = block.splitlines()
    lines = text.splitlines()
    span = len(block_lines)
    for index in range(len(lines) - span + 1):
        if lines[index : index + span] == block_lines:
            return (
                "\n".join(
                    f"# {line}" if index <= i < index + span else line
                    for i, line in enumerate(lines)
                )
                + "\n"
            )
    raise PrerequisiteError(
        "the extracted block was not found verbatim in its own template — "
        "the comment-only spoof cannot be replayed"
    )


def drop_consumer_mapping(block: str) -> tuple[str, list[str]]:
    """The #303 acceptance mutation: remove the consumer mapping lines.

    Returns (mutated block, dropped lines). The block still validates the
    ref and still fails closed on an empty carrier — the receipt looks
    correct — but the delivered sentinel never reaches the provider env
    slot, so the AMBIENT value survives in it.
    """
    dropped: list[str] = []
    kept: list[str] = []
    for line in block.splitlines():
        if _CONSUMER_EXPORT_RE.match(line):
            dropped.append(line)
        else:
            kept.append(line)
    if not dropped:
        raise PrerequisiteError("no consumer mapping line found to drop — the block moved")
    return "\n".join(kept) + "\n", dropped


def restore_unconditional_exit(block: str) -> str:
    """The #302 defect, restored verbatim: an unconditional exit with the
    driver's rc BEFORE the meta floor, the collection and the markers."""
    lines = block.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if _DRIVER_PHASE_END_RE.search(line):
            return (
                "".join(lines[: index + 1]) + 'exit "$_driver_rc"\n' + "".join(lines[index + 1 :])
            )
    raise PrerequisiteError(
        "the driver-phase end seam is missing from the finalization block — "
        "the #302 mutation cannot be replayed"
    )


# ---------------------------------------------------------------------------
# The dispatch-schema decision logic (pure — unit-tested)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaFinding:
    """One typed disagreement between declared and captured surfaces."""

    type: str  # undeclared_sent | declared_never_sent | sent_never_consumed | stale_conditional_annotation
    key: str
    detail: str


#: Declared inputs whose sending is CONDITIONAL on a dispatch state the
#: captures do not exercise (an activated plan revision; a repair
#: re-dispatch). Each annotation carries the dispatch-construction site
#: whose source must still contain the key — a stale annotation is a
#: finding, so the escape hatch cannot rot into a hole.
CONDITIONAL_INPUTS: dict[str, dict[str, tuple[str, str]]] = {
    "github": {
        "plan_digest": (
            "src/forge/runs/github_service.py",
            '"plan_digest": plan_binding.plan_digest',
        ),
        "repair_context": (
            "src/forge/runs/github_service.py",
            '"repair_context": repair_context',
        ),
    },
    "azure": {
        "repair_context": (
            "src/forge/runs/azure_service.py",
            '"repair_context": repair_context',
        ),
    },
}


def verify_conditional_annotations(
    provider: str, annotations: dict[str, tuple[str, str]]
) -> list[SchemaFinding]:
    """Every conditional annotation's needle must still exist at its
    recorded source site — else the annotation is stale."""
    findings: list[SchemaFinding] = []
    for key, (relative, needle) in sorted(annotations.items()):
        source = REPO_ROOT / relative
        text = source.read_text(encoding="utf-8") if source.is_file() else ""
        if needle not in text:
            findings.append(
                SchemaFinding(
                    type="stale_conditional_annotation",
                    key=key,
                    detail=f"{relative} no longer contains {needle!r} — the "
                    "conditional-input annotation is stale (recapture or drop it)",
                )
            )
    del provider
    return findings


def declared_vs_captured(
    provider: str,
    declared: set[str],
    captured_shapes: dict[str, list[str]],
    annotations: dict[str, tuple[str, str]] | None = None,
) -> list[SchemaFinding]:
    """The bidirectional GitHub/Azure conformance rule.

    - ``undeclared_sent``: a captured shape sends a key the shipped
      template does not declare — the real provider answers a dispatch-wide
      422 while a permissive fake stays green (the recorded failure mode);
    - ``declared_never_sent``: a declared key no captured shape sends and
      no source-verified conditional annotation covers — a dead input or
      a template drift.
    """
    findings: list[SchemaFinding] = []
    for shape in sorted(captured_shapes):
        for key in sorted(set(captured_shapes[shape]) - declared):
            findings.append(
                SchemaFinding(
                    type="undeclared_sent",
                    key=key,
                    detail=f"the captured {provider} shape {shape!r} sends {key!r} "
                    f"but {provider} template does not declare it — the real "
                    "provider refuses the whole dispatch",
                )
            )
    sent_union: set[str] = set()
    for keys in captured_shapes.values():
        sent_union |= set(keys)
    conditional = dict(annotations or {})
    for key, (relative, _needle) in sorted(conditional.items()):
        if key not in declared:
            findings.append(
                SchemaFinding(
                    type="stale_conditional_annotation",
                    key=key,
                    detail=f"the conditional annotation for {key!r} names a key "
                    f"{provider} template does not declare ({relative})",
                )
            )
    never_sent = declared - sent_union - set(conditional)
    for key in sorted(never_sent):
        findings.append(
            SchemaFinding(
                type="declared_never_sent",
                key=key,
                detail=f"the {provider} template declares {key!r} but no captured "
                "dispatch shape sends it and no conditional annotation covers it",
            )
        )
    findings.extend(verify_conditional_annotations(provider, conditional))
    return findings


def sent_vs_consumed(
    captured_shapes: dict[str, list[str]], consumed: set[str]
) -> list[SchemaFinding]:
    """The GitLab rule: every pipeline variable the production dispatch
    sends must be consumed by at least one shipped recipe — a variable no
    recipe reads is dispatch drift (the ledger accepted it silently)."""
    findings: list[SchemaFinding] = []
    for shape in sorted(captured_shapes):
        for key in sorted(set(captured_shapes[shape]) - consumed):
            findings.append(
                SchemaFinding(
                    type="sent_never_consumed",
                    key=key,
                    detail=f"the captured gitlab shape {shape!r} sends {key!r} but "
                    "no shipped gitlab recipe references it",
                )
            )
    return findings


def load_captures(path: Path | None = None) -> dict[str, Any]:
    """The committed capture fixture (the recorded production truth)."""
    captures_path = path if path is not None else CAPTURES_PATH
    if not captures_path.is_file():
        raise PrerequisiteError(
            f"the dispatch-capture fixture is missing: {captures_path} — regenerate with "
            "`uv run python scripts/gate_conformance.py --regenerate-captures`"
        )
    try:
        captures = json.loads(captures_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PrerequisiteError(f"the dispatch-capture fixture is unparseable: {exc}") from exc
    if captures.get("schema") != CAPTURES_SCHEMA:
        raise PrerequisiteError(
            f"the dispatch-capture fixture carries schema {captures.get('schema')!r}, "
            f"expected {CAPTURES_SCHEMA!r}"
        )
    return captures


# ---------------------------------------------------------------------------
# Check 1 — the shipped recipes, executed
# ---------------------------------------------------------------------------


@dataclass
class ArmRecord:
    """One executed arm (the report's atomic unit)."""

    arm: str
    status: str  # pass | fail | caught | absent
    detail: str = ""
    job_rc: int | None = None
    candidate_state: str | None = None
    expectations: dict[str, bool] = field(default_factory=dict)

    def as_document(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "status": self.status,
            "job_rc": self.job_rc,
            "candidate_state": self.candidate_state,
            "expectations": self.expectations,
            "detail": self.detail,
        }


def _run_block(block: str, checkout: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Execute one extracted/mutated script block as the job's one shell."""
    return subprocess.run(
        ["bash", "-c", block],
        cwd=str(checkout),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _tail(text: str, limit: int = 1200) -> str:
    return text[-limit:].strip()


def _success_expectations(
    result: subprocess.CompletedProcess[str], checkout: Path, base_oid: str, edit: str
) -> dict[str, bool]:
    """The SUCCESS-arm contract (the assertions the #302 regression pins)."""
    expectations = {
        "job_rc_zero": result.returncode == 0,
        "candidate_marker": f'FORGE_CANDIDATE:{{"attempt_base": "{base_oid}"' in result.stdout,
        "outcome_marker": "FORGE_LANE_OUTCOME:" in result.stdout,
        "diff_carries_the_edit": False,
        "outcome_fields": False,
    }
    diff = checkout / ".forge" / "candidate.diff"
    if diff.is_file():
        expectations["diff_carries_the_edit"] = edit in diff.read_text(encoding="utf-8")
    for line in result.stdout.splitlines():
        if line.startswith("FORGE_LANE_OUTCOME:"):
            try:
                payload = json.loads(line[len("FORGE_LANE_OUTCOME:") :])
            except json.JSONDecodeError:
                break
            expectations["outcome_fields"] = payload == {
                "driver_exit": "completed",
                "collector_exit": 0,
                "candidate_state": "candidate",
            }
            break
    return expectations


def _guarded_arm(arm: str, runner) -> ArmRecord:
    """Run one arm body; ANY break (a missing artifact the arm tried to
    read, a dead bash, a broken extraction) is a FAILED arm carrying the
    exception — a broken recipe is a recipe failure (exit 3), never a
    gate crash."""
    try:
        return runner()
    except Exception as exc:  # noqa: BLE001 — the arm's own failure detail
        return ArmRecord(arm=arm, status="fail", detail=f"{type(exc).__name__}: {exc}")


def run_shipped_recipes(
    seams: dict[str, Any], workroot: Path, templates: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """Every SDK-lane recipe, VERBATIM, under real bash — four contract
    arms plus the #302 mutation self-test per template.

    *templates* narrows the selection (the unit tests' seam); the gate
    itself always runs the full shipped set.
    """
    stub = seams["write_stub_lane_python"](workroot / "stub-venv")
    templates_report: list[dict[str, Any]] = []
    failures: list[str] = []
    escapes: list[str] = []

    for name in templates or tuple(seams["SDK_LANE_TEMPLATES"]):
        template = TEMPLATES_DIR / name
        if not template.is_file():
            raise PrerequisiteError(f"the shipped recipe is missing: {template}")
        block = seams["finalization_block"](template)
        arms: list[ArmRecord] = []
        with tempfile.TemporaryDirectory(prefix=f"conf-{template.stem}-", dir=workroot) as tmp:
            cases = Path(tmp)

            edit = "print('conformance gate edit')"

            def _arm_success() -> ArmRecord:
                # (a) success: rc=0 + an edit → candidate advertised.
                checkout, base_oid = seams["make_checkout"](cases / "success")
                result = _run_block(
                    block,
                    checkout,
                    seams["lane_env"](
                        stub,
                        base_oid,
                        FORGE_STUB_DRIVER_RC="0",
                        FORGE_STUB_DRIVER_META=json.dumps(
                            {"attempt_base": base_oid, "driver": "stub", "exit": "completed"}
                        ),
                        FORGE_STUB_DRIVER_EDIT="src/app.py",
                        FORGE_STUB_DRIVER_CONTENT=edit,
                    ),
                )
                expectations = _success_expectations(result, checkout, base_oid, edit)
                return ArmRecord(
                    arm="success",
                    status="pass" if all(expectations.values()) else "fail",
                    job_rc=result.returncode,
                    candidate_state=_outcome_state(result.stdout),
                    expectations=expectations,
                    detail=_tail(result.stdout + result.stderr)
                    if not all(expectations.values())
                    else "",
                )

            def _arm_failure() -> ArmRecord:
                # (b) failure: rc=7 → the job fails with EXACTLY rc=7, the
                # meta floor is honest, the marker is never a VALID candidate.
                checkout, base_oid = seams["make_checkout"](cases / "failure")
                result = _run_block(
                    block,
                    checkout,
                    seams["lane_env"](
                        stub,
                        base_oid,
                        FORGE_STUB_DRIVER_RC="7",
                        FORGE_STUB_DRIVER_EDIT="src/app.py",
                    ),
                )
                meta = seams["load_meta"](checkout)
                expectations = {
                    "job_rc_is_the_driver_rc": result.returncode == 7,
                    "meta_floor_honest": meta.get("exit") == "failed"
                    and meta.get("terminal_reason") == "lane_driver_no_meta",
                    "marker_never_valid": '"exit": "failed"' in result.stdout,
                    "outcome_driver_failed": _outcome_state(result.stdout) == "driver_failed",
                }
                return ArmRecord(
                    arm="failure",
                    status="pass" if all(expectations.values()) else "fail",
                    job_rc=result.returncode,
                    candidate_state=_outcome_state(result.stdout),
                    expectations=expectations,
                    detail=_tail(result.stdout + result.stderr)
                    if not all(expectations.values())
                    else "",
                )

            def _arm_noop() -> ArmRecord:
                # (c) no-op: rc=0 with no edit → green job, distinct state.
                checkout, base_oid = seams["make_checkout"](cases / "noop")
                result = _run_block(
                    block, checkout, seams["lane_env"](stub, base_oid, FORGE_STUB_DRIVER_RC="0")
                )
                diff = checkout / ".forge" / "candidate.diff"
                expectations = {
                    "job_rc_zero": result.returncode == 0,
                    "zero_change_state": _outcome_state(result.stdout) == "zero_change",
                    "honest_empty_diff": diff.is_file() and diff.read_text(encoding="utf-8") == "",
                }
                return ArmRecord(
                    arm="noop",
                    status="pass" if all(expectations.values()) else "fail",
                    job_rc=result.returncode,
                    candidate_state=_outcome_state(result.stdout),
                    expectations=expectations,
                    detail=_tail(result.stdout + result.stderr)
                    if not all(expectations.values())
                    else "",
                )

            def _arm_restored_generation() -> ArmRecord:
                # (d) restored generation: the resume dispatch ships the
                # GENERATION's diff (modified + new + deleted), the
                # checkout is never collected accidentally.
                checkout, base_oid = seams["make_checkout"](cases / "resume")
                generation = seams["make_generation"](checkout)
                (generation / "src" / "app.py").write_text(
                    "print('restored wip')\n", encoding="utf-8"
                )
                (generation / "notes").mkdir()
                (generation / "notes" / "new-file.md").write_text("agent edit\n", encoding="utf-8")
                (generation / "run.sh").unlink()
                result = _run_block(
                    block,
                    checkout,
                    seams["lane_env"](
                        stub,
                        base_oid,
                        FORGE_LANE_RESUME="1",
                        FORGE_RESUME_CHECKPOINT=seams["CHECKPOINT_ID"],
                        FORGE_STUB_DRIVER_RC="0",
                    ),
                )
                diff_text = (checkout / ".forge" / "candidate.diff").read_text(encoding="utf-8")
                expectations = {
                    "job_rc_zero": result.returncode == 0,
                    "generation_diff": "+print('restored wip')" in diff_text
                    and "notes/new-file.md" in diff_text
                    and "-#!/bin/sh" in diff_text,
                    "collector_picked_the_generation": '"source": "generation"' in result.stdout,
                    "checkout_untouched": (checkout / "src" / "app.py").read_text(encoding="utf-8")
                    == "print('base')\n"
                    and (checkout / "run.sh").exists(),
                }
                return ArmRecord(
                    arm="restored_generation",
                    status="pass" if all(expectations.values()) else "fail",
                    job_rc=result.returncode,
                    candidate_state=_outcome_state(result.stdout),
                    expectations=expectations,
                    detail=_tail(result.stdout + result.stderr)
                    if not all(expectations.values())
                    else "",
                )

            def _arm_mutation() -> ArmRecord:
                # (e) the #302 mutation self-test: the unconditional exit,
                # restored before collection, on a SUCCESSFUL driver. The
                # success-arm contract MUST fail under it — a green job
                # with no collection is the shipped defect this gate
                # exists for.
                mutated = restore_unconditional_exit(block)
                checkout, base_oid = seams["make_checkout"](cases / "mutation")
                result = _run_block(
                    mutated,
                    checkout,
                    seams["lane_env"](
                        stub,
                        base_oid,
                        FORGE_STUB_DRIVER_RC="0",
                        FORGE_STUB_DRIVER_EDIT="src/app.py",
                        FORGE_STUB_DRIVER_CONTENT=edit,
                    ),
                )
                expectations = _success_expectations(result, checkout, base_oid, edit)
                caught = not all(expectations.values())
                collection_killed = (
                    not expectations["outcome_marker"] and not expectations["diff_carries_the_edit"]
                )
                return ArmRecord(
                    arm="mutation:unconditional_exit",
                    status="caught" if caught and collection_killed else "escape",
                    job_rc=result.returncode,
                    candidate_state=_outcome_state(result.stdout),
                    expectations=expectations,
                    detail=(
                        "the restored unconditional exit still reached collection — "
                        "the gate cannot detect the #302 defect"
                        if not caught
                        else "caught: the mutated job exits green before the meta "
                        "floor, the collection and the markers"
                        if collection_killed
                        else "caught on a non-collection expectation — inspect the arm"
                    ),
                )

            arms.append(_guarded_arm("success", _arm_success))
            arms.append(_guarded_arm("failure", _arm_failure))
            arms.append(_guarded_arm("noop", _arm_noop))
            arms.append(_guarded_arm("restored_generation", _arm_restored_generation))
            arms.append(_guarded_arm("mutation:unconditional_exit", _arm_mutation))

        for record in arms:
            if record.status == "fail":
                failures.append(f"{name}:{record.arm}")
            if record.status == "escape":
                escapes.append(f"{name}:{record.arm}")
        templates_report.append({"template": name, "arms": [a.as_document() for a in arms]})

    return {
        "status": "fail" if failures or escapes else "pass",
        "templates": templates_report,
        "failures": failures,
        "mutation_escapes": escapes,
    }


def _outcome_state(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("FORGE_LANE_OUTCOME:"):
            try:
                return str(json.loads(line[len("FORGE_LANE_OUTCOME:") :]).get("candidate_state"))
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# Check 3 — the secret-consumer sentinels
# ---------------------------------------------------------------------------

_SENTINEL_PROBE = "".join(
    f"printf 'FORGE_SENTINEL_PROBE:{var}=%s\\n' \"${{{var}:-}}\"\n" for var in PROBE_VARS
)


def run_sentinel_arm(
    block: str,
    *,
    carrier_env: dict[str, str],
    ref: str,
    redeem: str = "",
    scrubbed_vars: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    parent_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, bool], subprocess.CompletedProcess[str]]:
    """Execute one credential-consumption block + probe; return the
    sentinel expectations.

    The parent env carries the AMBIENT sentinel in every credential slot
    (the value that must never reach the model process); the carrier env
    carries the DELIVERED sentinel exactly where the recipe's mapping
    puts it. The probe snapshots the credential slots the block leaves
    behind. *parent_overrides* mutates the parent env (the Azure arm's
    dropped-mapping mutation: an unmapped secret variable is ABSENT from
    the step env, not ambient).
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        CREDENTIAL_REF_ENV: ref,
        CREDENTIAL_REDEEM_ENV: redeem,
        # the ambient parent values — must be replaced or refused, never kept
        "ANTHROPIC_AUTH_TOKEN": AMBIENT_SENTINEL,
        "ANTHROPIC_API_KEY": AMBIENT_SENTINEL,
        "CLAUDE_CODE_OAUTH_TOKEN": AMBIENT_SENTINEL,
        # the delivered credential (the mapping's own carrier — applied last,
        # exactly like a runner applying the step's env mapping)
        **carrier_env,
        **(parent_overrides or {}),
    }
    result = subprocess.run(
        ["bash", "-c", block + "\n" + _SENTINEL_PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    observed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("FORGE_SENTINEL_PROBE:"):
            var, _, value = line[len("FORGE_SENTINEL_PROBE:") :].partition("=")
            observed[var] = value
    expectations = {
        "exit_zero": result.returncode == 0,
        "consumed_is_the_delivered_sentinel": observed.get("ANTHROPIC_AUTH_TOKEN")
        == DELIVERED_SENTINEL,
        # the ambient sentinel must never reach the CONSUMED slot (the
        # value the model process presents); strays outside this
        # recipe's scrub guarantee stay OBSERVED, recorded in the arm
        "ambient_never_consumed": AMBIENT_SENTINEL not in observed.get("ANTHROPIC_AUTH_TOKEN", ""),
        "strays_scrubbed": all(observed.get(var, "") == "" for var in scrubbed_vars),
    }
    return expectations, result


def _fail_closed_arm(block: str, *, carrier_env: dict[str, str], ref: str) -> ArmRecord:
    """An empty carrier refuses the lane CLOSED — never an ambient
    fallback (zero model calls on a missing onboarding step)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        CREDENTIAL_REF_ENV: ref,
        CREDENTIAL_REDEEM_ENV: "",
        "ANTHROPIC_AUTH_TOKEN": AMBIENT_SENTINEL,  # the fallback that must NOT happen
        "ANTHROPIC_API_KEY": AMBIENT_SENTINEL,
        **{key: "" for key in carrier_env},
    }
    result = subprocess.run(
        ["bash", "-c", block], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    expectations = {
        "refused": result.returncode != 0,
        "bootstrap_marker": "FORGE_BOOTSTRAP_FAILED" in result.stdout + result.stderr,
        "no_ambient_fallback": AMBIENT_SENTINEL not in result.stdout + result.stderr,
    }
    return ArmRecord(
        arm="fail_closed",
        status="pass" if all(expectations.values()) else "fail",
        job_rc=result.returncode,
        expectations=expectations,
        detail=_tail(result.stdout + result.stderr) if not all(expectations.values()) else "",
    )


def _carrier_env(provider: str, segment: str) -> dict[str, str]:
    """The delivered sentinel in the recipe's own carrier slot."""
    if provider == "gitlab-lane":
        return {f"FORGE_MODEL_{segment}": DELIVERED_SENTINEL}
    if provider == "github":
        return {"FORGE_MODEL_CREDENTIAL": DELIVERED_SENTINEL}
    if provider == "azure":
        # the template's own env mapping renders the variable-group secret
        # into the step env (`ANTHROPIC_AUTH_TOKEN: $(LANE_ANTHROPIC_AUTH_TOKEN)`)
        return {"ANTHROPIC_AUTH_TOKEN": DELIVERED_SENTINEL}
    raise PrerequisiteError(f"unknown credential provider {provider!r}")


def run_secret_consumers(seams: dict[str, Any]) -> dict[str, Any]:
    """The sentinel proof per recipe carrying a credential-consumption
    block, plus the dropped-mapping mutation self-test."""
    segment = str(seams["credential_secret_segment"]("env:ANTHROPIC_AUTH_TOKEN"))
    # Q39-04 (#323): the collision-safe locator spelling of the P05
    # probe ref — the sentinel arms prove the shipped blocks consume a
    # LOCATOR-shaped ref + carrier exactly as they consume the legacy
    # segment spelling.
    locator_ref = str(seams["native_locator"](LOCATOR_COLLISION_PAIR[0]))
    recipes_report: list[dict[str, Any]] = []
    failures: list[str] = []
    escapes: list[str] = []

    # (i) the GitLab recipes: scan EVERY shipped harness template; a
    # block discovered beyond the pinned set activates the arms by itself.
    gitlab_recipes: list[tuple[str, str]] = []
    for name in seams["HARNESS_TEMPLATES"]:
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        gitlab_recipes.append((name, "present" if credential_block(text) else "absent"))
    # (ii) the GitHub + Azure templates (their blocks are required).
    native_specs: list[tuple[str, str]] = [
        (GITHUB_TEMPLATE, "github"),
        (AZURE_TEMPLATE, "azure"),
    ]

    for name, presence in gitlab_recipes:
        if presence == "absent":
            if name in GITLAB_CREDENTIAL_BLOCK_REQUIRED:
                failures.append(f"{name}:credential_block_missing")
                recipes_report.append(
                    {
                        "template": name,
                        "status": "fail",
                        "detail": "the required credential-consumption block is absent",
                        "arms": [ArmRecord(arm="credential_block", status="fail").as_document()],
                    }
                )
            else:
                recipes_report.append(
                    {
                        "template": name,
                        "status": "absent",
                        "detail": "no credential-consumption block shipped (recorded; "
                        "the sentinel arms activate when one lands)",
                        "arms": [],
                    }
                )
            continue
        recipes_report.append(
            _sentinel_recipe_arms(
                template=name,
                provider="gitlab-lane",
                block=credential_block((TEMPLATES_DIR / name).read_text(encoding="utf-8")),
                segment=segment,
                locator_ref=locator_ref,
                failures=failures,
                escapes=escapes,
            )
        )

    for name, provider in native_specs:
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        block = credential_block(text)
        if block is None:
            failures.append(f"{name}:credential_block_missing")
            recipes_report.append(
                {
                    "template": name,
                    "status": "fail",
                    "detail": "the required credential-consumption block is absent",
                    "arms": [ArmRecord(arm="credential_block", status="fail").as_document()],
                }
            )
            continue
        recipes_report.append(
            _sentinel_recipe_arms(
                template=name,
                provider=provider,
                block=block,
                segment=segment,
                locator_ref=locator_ref,
                failures=failures,
                escapes=escapes,
            )
        )

    return {
        "status": "fail" if failures or escapes else "pass",
        "recipes": recipes_report,
        "failures": failures,
        "mutation_escapes": escapes,
        "locator_probe_ref": LOCATOR_COLLISION_PAIR[0],
        "sentinel_policy": (
            "the delivered and ambient sentinel VALUES never enter the report — "
            "arms record boolean expectations only"
        ),
    }


def _sentinel_recipe_arms(
    *,
    template: str,
    provider: str,
    block: str,
    segment: str,
    locator_ref: str,
    failures: list[str],
    escapes: list[str],
) -> dict[str, Any]:
    """sentinel / fail-closed / dropped-mapping / locator arms for ONE
    recipe (the locator arm: the block executed with the collision-safe
    LOCATOR spelling of the P05 probe ref — Q39-04)."""

    def fail(arm: str) -> str:
        failures.append(f"{template}:{arm}")
        return "fail"

    # (1) the sentinel arm: the consumed slot carries EXACTLY the
    # delivered sentinel; the ambient value never leaks; the provider's
    # guaranteed strays are scrubbed. Slots outside the provider's
    # guarantee stay OBSERVED (recorded in the arm) — they are
    # sibling-owned template surfaces, never silently ignored.
    guarantee = SCRUB_GUARANTEE.get(provider, ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"))
    expectations, result = run_sentinel_arm(
        block,
        carrier_env=_carrier_env(provider, segment),
        ref=segment,
        scrubbed_vars=guarantee,
    )
    observed_gap = [
        var
        for var in PROBE_VARS
        if var not in guarantee
        and var != "ANTHROPIC_AUTH_TOKEN"
        and f"FORGE_SENTINEL_PROBE:{var}=" in result.stdout
        and not result.stdout.split(f"FORGE_SENTINEL_PROBE:{var}=", 1)[1].splitlines()[0] == ""
    ]
    detail = (
        _tail(result.stdout + result.stderr)
        if not all(expectations.values())
        else (
            f"observed (not asserted, outside this recipe's scrub guarantee): "
            f"{', '.join(observed_gap)}"
            if observed_gap
            else ""
        )
    )
    arms = [
        ArmRecord(
            arm="sentinel",
            status="pass" if all(expectations.values()) else fail("sentinel"),
            job_rc=result.returncode,
            expectations=expectations,
            detail=detail,
        )
    ]

    # (2) the fail-closed arm: an empty carrier refuses before any model
    # call; the ambient value is never the fallback.
    record = _fail_closed_arm(block, carrier_env=_carrier_env(provider, segment), ref=segment)
    if record.status != "pass":
        record.status = fail("fail_closed")
    arms.append(record)

    # (3) the acceptance mutation: the consumer mapping DROPPED while the
    # receipt stays correct-looking (the ref validated, the carrier
    # delivered — exactly what the dispatch ledger would record). The
    # sentinel arm's expectations MUST fail under it — the proof that a
    # correct-looking receipt is not evidence of consumption.
    #
    # Provider shape of the dropped mapping: the gitlab lanes and the
    # GitHub template map inside the block (the ``export`` line); the
    # Azure template's mapping is the DRIVER STEP'S env mapping
    # (``ANTHROPIC_AUTH_TOKEN: $(LANE_ANTHROPIC_AUTH_TOKEN)``) — dropping
    # it leaves the step env without the credential (an unmapped Azure
    # SECRET variable is absent from the step env by design).
    if provider == "azure":
        mutated_block = block
        mutated_carrier: dict[str, str] = {}
        mutated_parents = {"ANTHROPIC_AUTH_TOKEN": ""}
        dropped_count = 1
    else:
        try:
            mutated_block, dropped_lines = drop_consumer_mapping(block)
            dropped_count = len(dropped_lines)
        except PrerequisiteError:
            # the SHIPPED block itself carries no consumer mapping — the
            # defect this mutation replays is already the template's
            # reality (the sentinel arm above has FAILED on it); the
            # mutation arm records that state rather than crashing.
            mutated_block = block
            dropped_count = 0
        mutated_carrier = _carrier_env(provider, segment)
        mutated_parents = None
    mutated_expectations, mutated_result = run_sentinel_arm(
        mutated_block,
        carrier_env=mutated_carrier,
        ref=segment,
        scrubbed_vars=guarantee,
        parent_overrides=mutated_parents,
    )
    caught = not all(mutated_expectations.values())
    wrong_value_reached = (
        mutated_expectations.get("ambient_never_consumed") is False
        or mutated_expectations.get("consumed_is_the_delivered_sentinel") is False
    )
    if caught and wrong_value_reached:
        status = "caught"
        detail = (
            f"caught despite the correct-looking receipt ({dropped_count} consumer "
            "mapping(s) dropped): the sentinel never reached the consumed slot"
        )
    elif caught:
        status = "caught"
        detail = "caught on a non-sentinel expectation — inspect the arm"
    else:
        status = "escape"
        detail = (
            "the dropped consumer mapping still satisfied every sentinel "
            "expectation — the sentinel check cannot prove consumption"
        )
        escapes.append(f"{template}:mutation:consumer_mapping_dropped")
    arms.append(
        ArmRecord(
            arm="mutation:consumer_mapping_dropped",
            status=status,
            job_rc=mutated_result.returncode,
            expectations=mutated_expectations,
            detail=detail,
        )
    )

    # (4) the locator arm (Q39-04 / #323): the same sentinel + fail-closed
    # contract executed with the collision-safe LOCATOR spelling of the
    # P05 probe ref — the carrier a locator-registry dispatch names. The
    # block derives the carrier from the dispatched ref at runtime
    # (FORGE_MODEL_<ref>), so this arm proves the locator spelling flows
    # end-to-end: delivered sentinel consumed, ambient never, strays
    # scrubbed, an empty locator carrier failing CLOSED.
    locator_expectations, locator_result = run_sentinel_arm(
        block,
        carrier_env=_carrier_env(provider, locator_ref),
        ref=locator_ref,
        scrubbed_vars=guarantee,
    )
    locator_record = ArmRecord(
        arm="sentinel_locator",
        status="pass" if all(locator_expectations.values()) else fail("sentinel_locator"),
        job_rc=locator_result.returncode,
        expectations=locator_expectations,
        detail=_tail(locator_result.stdout + locator_result.stderr)
        if not all(locator_expectations.values())
        else "",
    )
    arms.append(locator_record)
    locator_closed = _fail_closed_arm(
        block, carrier_env=_carrier_env(provider, locator_ref), ref=locator_ref
    )
    if locator_closed.status != "pass":
        locator_closed.status = fail("fail_closed_locator")
    else:
        locator_closed.arm = "fail_closed_locator"
    arms.append(locator_closed)

    # (5) the comment-only marker spoof (Q39-08 / #327): every line of
    # the block re-spelled as a comment — all the expected STRINGS still
    # present, the executable consumer GONE. The extraction must refuse
    # the spoof (an UNCAUGHT spoof is an escape: the gate would accept a
    # comment-only marker), and the SHIPPED text must survive the same
    # comment-stripped lens (the shipped block is not itself a spoof).
    spoof_refused = True
    spoof_error = ""
    try:
        shipped_text = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
        spoofed = comment_only_spoof(shipped_text, block)
        if credential_block(spoofed) is not None:
            spoof_refused = False
        shipped_survives = credential_block(strip_line_comments(shipped_text)) is not None
    except PrerequisiteError as exc:
        spoof_error = str(exc)[:200]
        spoof_refused = False
        shipped_survives = False
    expectations = {
        "strings_only_in_comments_refuse": spoof_refused,
        "the_shipped_block_survives_the_stripped_lens": shipped_survives,
    }
    caught = all(expectations.values())
    if not caught:
        escapes.append(f"{template}:mutation:comment_only_marker_spoof")
    arms.append(
        ArmRecord(
            arm="mutation:comment_only_marker_spoof",
            status="caught" if caught else "escape",
            expectations=expectations,
            detail=(
                spoof_error
                if spoof_error
                else (
                    "a comment-only marker still satisfied the consumer-block "
                    "extraction — the gate cannot tell a comment from a consumer"
                )
                if not caught
                else (
                    "caught: the block's strings exist only as comments — the "
                    "extraction refuses; the shipped block survives the same lens"
                )
            ),
        )
    )

    recipe_failures = [record for record in arms if record.status == "fail"]
    return {
        "template": template,
        "status": "fail" if recipe_failures else "pass",
        "arms": [record.as_document() for record in arms],
    }


# ---------------------------------------------------------------------------
# Check 4 — the native-locator conformance (Q39-04 / #323)
# ---------------------------------------------------------------------------

_LOCATOR_CHARSET_RE = re.compile(r"^[A-Z0-9_]+$")


def _locator_encoding_findings(encode: Any, refs: tuple[str, ...]) -> list[str]:
    """The pure encoding findings for one encoder over *refs*: a shared
    locator between two distinct refs, or a locator outside the provider
    charset intersection ``[A-Z0-9_]`` (GitHub secret names, GitLab
    CI/CD variables, Azure variable-group names — a hyphen is refused by
    GitHub's secret API at provisioning)."""
    locators: dict[str, str] = {}
    findings: list[str] = []
    for ref in refs:
        locator = str(encode(ref))
        locators[ref] = locator
        if not _LOCATOR_CHARSET_RE.match(locator):
            findings.append(f"charset:{ref}->{locator}")
    by_locator: dict[str, str] = {}
    for ref in sorted(locators):
        holder = by_locator.get(locators[ref])
        if holder is not None:
            findings.append(f"collision:{holder}~{ref}->{locators[ref]}")
        else:
            by_locator[locators[ref]] = ref
    return findings


def _locator_probe_plan(seams: dict[str, Any], profile: str = "gitlab") -> Any:
    """A native-mode delivery plan for the driver/template conformance
    arms (refs only — the plan shape the broker validates), in the right
    mode per *profile*."""
    plan = seams["delivery_plan_template"]
    if profile == "github":
        mode, transport_ref = seams["github_native_mode"], "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN"
    elif profile == "azure":
        mode, transport_ref = (
            seams["azure_group_mode"],
            "forge-lane-credentials/ANTHROPIC_AUTH_TOKEN",
        )
    else:
        mode, transport_ref = (
            seams["gitlab_protected_mode"],
            "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN",
        )
    return plan(
        subject="gitlab/conformance-gate/0",
        provider="anthropic-gateway",
        profile=profile,
        credential_ref="env:ANTHROPIC_AUTH_TOKEN",
        env_var="ANTHROPIC_AUTH_TOKEN",
        binding_revision=1,
        mode=mode,
        transport_ref=transport_ref,
        dispatch_ref="ENV_ANTHROPIC_AUTH_TOKEN",
        redemption=False,
    )


def run_locator_conformance(seams: dict[str, Any]) -> dict[str, Any]:
    """The native-locator dimension (Q39-04 / #323): the collision-safe
    encoding, the per-DRIVER template route, and the installed-template
    digest inventory — each with a self-test arm proving the check is
    sensitive (the legacy lossy encoder, a wrong driver's template, a
    mismatched digest, an unknown driver — all must be CAUGHT)."""
    failures: list[str] = []
    escapes: list[str] = []

    # (1) the encoding: pairwise-distinct, charset-clean locators for the
    # P05 pair and its variants.
    encoding_findings = _locator_encoding_findings(seams["native_locator"], LOCATOR_PROBE_REFS)
    if encoding_findings:
        failures.extend(f"encoding:{finding}" for finding in encoding_findings)

    # (2) the per-driver template dimension: every gitlab driver's OWN
    # recipe, structurally. Drivers whose recipe implements the native
    # route must PASS the conformance named with THEIR driver; the
    # rollout surface (recipes without a block yet — #305's scope) is
    # RECORDED, with the anthropic-route set still REQUIRED.
    driver_report: list[dict[str, Any]] = []
    digest_inventory: dict[str, str] = {}
    required_native_drivers = {"claude-code", "claude-sdk-lane"}
    for driver, filename in sorted(seams["driver_template_files"].items()):
        path = TEMPLATES_DIR / filename
        if not path.is_file():
            failures.append(f"driver_template_missing:{driver}:{filename}")
            continue
        text = path.read_text(encoding="utf-8")
        gaps = list(seams["native_route_structural_gaps"](text, "ANTHROPIC_AUTH_TOKEN"))
        if gaps:
            if driver in required_native_drivers:
                failures.append(f"driver_route_missing:{driver}:{','.join(gaps)}")
                status = "fail"
            else:
                status = "route_absent"
            driver_report.append(
                {
                    "driver": driver,
                    "template": filename,
                    "status": status,
                    "structural_gaps": gaps,
                    "digest": seams["template_identity_digest"](text),
                }
            )
            continue
        plan = _locator_probe_plan(seams)
        try:
            seams["delivery_template_conformance"](plan, text, driver=driver)
        except Exception as exc:  # noqa: BLE001 — a refused conformance is an arm failure
            failures.append(f"driver_conformance_refused:{driver}:{exc}")
            driver_report.append(
                {
                    "driver": driver,
                    "template": filename,
                    "status": "fail",
                    "detail": str(exc)[:200],
                    "digest": seams["template_identity_digest"](text),
                }
            )
            continue
        digest_inventory[filename] = seams["template_identity_digest"](text)
        driver_report.append(
            {
                "driver": driver,
                "template": filename,
                "status": "pass",
                "digest": digest_inventory[filename],
            }
        )
    # the parameterized single templates (github/azure) must pass the
    # same structural floor with a named driver.
    for provider_template, profile in ((GITHUB_TEMPLATE, "github"), (AZURE_TEMPLATE, "azure")):
        text = (TEMPLATES_DIR / provider_template).read_text(encoding="utf-8")
        try:
            seams["delivery_template_conformance"](
                _locator_probe_plan(seams, profile), text, driver="claude-code"
            )
            digest_inventory[provider_template] = seams["template_identity_digest"](text)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"profile_template_refused:{provider_template}:{exc}")

    # (3) the self-tests — each replays a defect the dimension exists to
    # catch; an UNCAUGHT one is an ESCAPE (the gate is insensitive).
    self_tests: dict[str, dict[str, Any]] = {}

    def _must_refuse(arm: str, runner: Any) -> None:
        try:
            runner()
        except Exception as exc:  # noqa: BLE001 — the expected typed refusal
            self_tests[arm] = {"status": "caught", "refusal": str(exc).split(":", 1)[0]}
            return
        self_tests[arm] = {"status": "escape"}
        escapes.append(arm)

    # (3a) the legacy lossy encoder MUST be flagged (P05 sensitivity).
    legacy_findings = _locator_encoding_findings(
        seams["credential_secret_segment"], LOCATOR_PROBE_REFS
    )
    self_tests["legacy_encoder_flagged"] = {
        "status": "caught" if legacy_findings else "escape",
        "findings": legacy_findings[:8],
    }
    if not legacy_findings:
        escapes.append("legacy_encoder_flagged")

    # (3b) a wrong driver's template (one without the route) must refuse
    # when THAT driver is dispatched — never a claude-code-shaped pass.
    absent = next(
        ((entry["driver"], entry) for entry in driver_report if entry["status"] == "route_absent"),
        None,
    )
    if absent is not None:
        driver, entry = absent
        text = (TEMPLATES_DIR / entry["template"]).read_text(encoding="utf-8")
        _must_refuse(
            "wrong_driver_template_refused",
            lambda: seams["delivery_template_conformance"](
                _locator_probe_plan(seams), text, driver=driver
            ),
        )
    else:  # pragma: no cover — every shipped recipe implemented the route
        self_tests["wrong_driver_template_refused"] = {"status": "skipped_no_absent_route"}

    # (3c) an installed-template digest mismatch must refuse BEFORE the
    # marker checks (the local template passing proves nothing).
    shipped = (TEMPLATES_DIR / "claude-code.gitlab-ci.yml").read_text(encoding="utf-8")
    _must_refuse(
        "digest_mismatch_refused",
        lambda: seams["delivery_template_conformance"](
            _locator_probe_plan(seams),
            shipped,
            driver="claude-code",
            expected_template_digest="0" * 16,
        ),
    )

    # (3d) an unknown driver must refuse consumer_route_unknown.
    _must_refuse(
        "unknown_driver_refused",
        lambda: seams["delivery_template_conformance"](
            _locator_probe_plan(seams), shipped, driver="forge-not-a-driver"
        ),
    )

    return {
        "status": "fail" if failures or escapes else "pass",
        "encoding": {
            "probe_refs": list(LOCATOR_PROBE_REFS),
            "locators": {ref: str(seams["native_locator"](ref)) for ref in LOCATOR_PROBE_REFS},
            "findings": encoding_findings,
            "charset": "^[A-Z0-9_]+$ (the GitHub/GitLab/Azure carrier-name intersection)",
        },
        "driver_templates": driver_report,
        "digest_inventory": digest_inventory,
        "digest_policy": (
            "the recorded digests are the onboarding values for the INSTALLED "
            "target templates — a compatibility-sensitive template change "
            "re-verifies the installed digest against this inventory"
        ),
        "self_tests": self_tests,
        "failures": failures,
        "mutation_escapes": escapes,
    }


# ---------------------------------------------------------------------------
# Check 5 — the consumer contracts (Q39-08 / #327): the grant through the
# real ASGI endpoint, the runner's typed verification, the rebind digest
# through the recorded dispatch — offline, deterministic, sentinels only.
# ---------------------------------------------------------------------------


def _consumer_seams() -> dict[str, Any]:
    """The production consumers + the PE runner-boundary helpers the
    consumer arms drive (reused BY IMPORT — the same doctrine as the
    #302 seams: the gate can never drift from the fixtures it promotes)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from tests.production_entry import conftest as pe  # noqa: PLC0415
        from tests.production_entry.test_credential_dispatch import (  # noqa: PLC0415
            AMBIENT_VALUE as RUNNER_AMBIENT,
            CannedRedemptionEndpoint,
            FakeModelEndpoint,
            LANE_WORK_ID,
            REF_V1,
            SECRET_V2 as RUNNER_DELIVERED,
            _FAKE_SDK_STUB,
            _lane_job_env,
            _lying_document,
            _run_lane_job,
        )
        from tests.test_gate_conformance import _env as scoped_env  # noqa: PLC0415

        from forge.adaptive.credential_broker import (  # noqa: PLC0415
            DELIVERY_ROUTE_ENV,
            DELIVERY_TEMPLATE_DIR_ENV,
            CredentialOperationGrant,
            EVIDENCE_OPERATION_GRANTS_KEY,
            StagedBroker,
        )
        from forge.adaptive.operator_snapshot import CanonicalSubject  # noqa: PLC0415
        from forge.adaptive.project_credentials import ProjectCredentialRegistry  # noqa: PLC0415
        from forge.api_lane_control import (  # noqa: PLC0415
            LANE_CREDENTIAL_REDEEM_ROUTE,
            LEGACY_CREDENTIAL_ANCHOR_FILE_ENV,
            lane_control_token,
            persist_operation_grant,
        )
        from forge.config import Settings  # noqa: PLC0415
        from forge.database import reset_engine  # noqa: PLC0415
        from forge.main import create_app  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — any import break is the same refusal
        raise PrerequisiteError(
            f"the #327 consumer seams could not be imported (run via "
            f"`uv run python scripts/gate_conformance.py` from the repo root): {exc}"
        ) from exc
    return {
        "pe": pe,
        "runner_ambient": RUNNER_AMBIENT,
        "runner_delivered": RUNNER_DELIVERED,
        "CannedRedemptionEndpoint": CannedRedemptionEndpoint,
        "FakeModelEndpoint": FakeModelEndpoint,
        "LANE_WORK_ID": LANE_WORK_ID,
        "REF_V1": REF_V1,
        "fake_sdk_stub": _FAKE_SDK_STUB,
        "lane_job_env": _lane_job_env,
        "lying_document": _lying_document,
        "run_lane_job": _run_lane_job,
        "scoped_env": scoped_env,
        "delivery_route_env": DELIVERY_ROUTE_ENV,
        "delivery_template_dir_env": DELIVERY_TEMPLATE_DIR_ENV,
        "grant_template": CredentialOperationGrant,
        "grants_key": EVIDENCE_OPERATION_GRANTS_KEY,
        "staged_broker": StagedBroker,
        "canonical_subject": CanonicalSubject,
        "registry_cls": ProjectCredentialRegistry,
        "redeem_route": LANE_CREDENTIAL_REDEEM_ROUTE,
        "anchor_env": LEGACY_CREDENTIAL_ANCHOR_FILE_ENV,
        "lane_control_token": lane_control_token,
        "persist_operation_grant": persist_operation_grant,
        "settings_cls": Settings,
        "reset_engine": reset_engine,
        "create_app": create_app,
    }


def _grant_world(seams: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    """The P01 fixture world: one project, TWO live sibling bindings, a
    staged broker double whose call list is the zero-broker-I/O proof,
    and the attempt's grant persisted through the production seam."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    subject = seams["canonical_subject"](
        provider_family="gitlab", connection="-", native_id=str(GRANT_PROJECT_ID)
    )
    registry = seams["registry_cls"]()
    registry.bind(subject, GRANT_PROVIDER, GRANT_REF, bound_by="conf-gate")
    registry.bind(subject, GRANT_SIBLING_PROVIDER, GRANT_SIBLING_REF, bound_by="conf-gate")
    broker = seams["staged_broker"]()
    broker.stage(GRANT_REF, DELIVERED_SENTINEL, env_var="ANTHROPIC_AUTH_TOKEN", version="g1")
    broker.stage(GRANT_SIBLING_REF, AMBIENT_SENTINEL, env_var="OPENAI_API_KEY", version="s1")
    now = _datetime.now(_UTC)
    grant = seams["grant_template"](
        grant_id="conf-grant-g1",
        work_id=GRANT_WORK_ID,
        subject=subject.subject_id(),
        provider=GRANT_PROVIDER,
        credential_ref=GRANT_REF,
        binding_revision=1,
        attempt_generation=GRANT_GENERATION,
        delivery_mode="runner-redemption",
        redemption_deadline=now + _timedelta(hours=1),
        created_at=now,
    )
    return subject, registry, broker, grant


async def _drive_grant_asgi(seams: dict[str, Any], workroot: Path) -> list[ArmRecord]:
    """The grant through the REAL ASGI endpoint: the REAL ``create_app``
    app over a disposable sqlite file, lifespan up, the redemption route
    driven through ``httpx.ASGITransport`` — sentinel values only.

    Arm order is the baseline-first discipline: the granted route redeems
    FIRST (the baseline leg of the SAME trace every later mutation leg
    replays), then the sibling refusal, the value-free audit and the
    DTO-preserving disconnect each compare against it.
    """
    from pydantic import SecretStr  # noqa: PLC0415

    settings = seams["settings_cls"](
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-conf-gate"),
        GITLAB_WEBHOOK_SECRET=SecretStr("conf-gate-whsec"),
        DATABASE_URL=f"sqlite+aiosqlite:///{workroot / 'grant-consumer.db'}",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_LANE_CONTROL_SECRET=SecretStr(GRANT_SECRET),
    )
    saved_anchor = os.environ.get(seams["anchor_env"])
    os.environ[seams["anchor_env"]] = str(workroot / "grant-legacy-anchor")
    seams["reset_engine"]()
    application = seams["create_app"](settings=settings)
    arms: list[ArmRecord] = []

    async def _redeem(client: Any, *, ref: str, provider: str) -> Any:
        token = seams["lane_control_token"](
            GRANT_SECRET, GRANT_WORK_ID, generation=GRANT_GENERATION
        )
        return await client.get(
            seams["redeem_route"],
            params={"work_id": GRANT_WORK_ID, "credential_ref": ref, "provider": provider},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _audit_rows(app: Any) -> list[dict[str, Any]]:
        from forge.durable.models import FlowRun as FlowRunRow  # noqa: PLC0415

        async with app.state.session_factory() as session:
            row = await session.get(FlowRunRow, GRANT_WORK_ID)
        return list((row.evidence or {}).get("credential_redemptions") or [])

    try:
        async with application.router.lifespan_context(application):
            from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

            from forge.durable.models import FlowRun as FlowRunRow  # noqa: PLC0415

            _subject, registry, broker, grant = _grant_world(seams)
            application.state.credential_registry = registry
            application.state.credential_broker = broker
            async with application.state.session_factory() as session:
                session.add(
                    FlowRunRow(
                        id=GRANT_WORK_ID,
                        project_id=GRANT_PROJECT_ID,
                        provider="gitlab",
                        cancellation_generation=GRANT_GENERATION,
                        status="waiting_harness",
                    )
                )
                await session.commit()
            effective = await seams["persist_operation_grant"](
                application.state.session_factory, grant=grant
            )
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://conf-gate") as client:
                # (1) the BASELINE: the granted route redeems the DELIVERED
                # sentinel through the real endpoint, joined on the grant id.
                response = await _redeem(client, ref=GRANT_REF, provider=GRANT_PROVIDER)
                document = response.json() if response.status_code == 200 else {}
                expectations = {
                    "http_200": response.status_code == 200,
                    "delivered_sentinel_redeemed": document.get("value") == DELIVERED_SENTINEL,
                    "grant_id_join": document.get("grant_id") == effective.grant_id,
                    "broker_resolved_the_granted_ref": broker.resolve_calls == [GRANT_REF],
                }
                arms.append(
                    ArmRecord(
                        arm="grant:redeem_granted_route",
                        status="pass" if all(expectations.values()) else "fail",
                        expectations=expectations,
                        detail=(
                            f"HTTP {response.status_code}: "
                            f"{str(document.get('detail') or '')[:160]}"
                            if not all(expectations.values())
                            else ""
                        ),
                    )
                )

                # (2) the SIBLING route of the same project refuses typed
                # with ZERO broker calls — project membership is not
                # operation authorization (the P01 shape).
                sibling = await _redeem(
                    client, ref=GRANT_SIBLING_REF, provider=GRANT_SIBLING_PROVIDER
                )
                sibling_detail = sibling.text[:200] if sibling.status_code != 200 else ""
                expectations = {
                    "refused_403": sibling.status_code == 403,
                    "typed_route_mismatch": "grant_route_mismatch" in sibling_detail,
                    "zero_broker_calls": broker.resolve_calls == [GRANT_REF],
                }
                arms.append(
                    ArmRecord(
                        arm="grant:sibling_route_refused_zero_broker",
                        status="pass" if all(expectations.values()) else "fail",
                        expectations=expectations,
                        detail=sibling_detail if not all(expectations.values()) else "",
                    )
                )

                # (3) the durable audit the redemption wrote BEFORE the
                # value left: refs/metadata only, joined on the grant.
                rows = await _audit_rows(application)
                expectations = {
                    "audit_row_persists_before_the_value_leaves": bool(rows),
                    "audit_is_value_free": DELIVERED_SENTINEL not in json.dumps(rows)
                    and AMBIENT_SENTINEL not in json.dumps(rows),
                    "audit_names_the_granted_ref_only": bool(rows)
                    and all(row.get("credential_ref") == GRANT_REF for row in rows),
                    "audit_joins_the_grant": bool(rows)
                    and all(row.get("grant_id") == effective.grant_id for row in rows),
                }
                arms.append(
                    ArmRecord(
                        arm="grant:audit_value_free",
                        status="pass" if all(expectations.values()) else "fail",
                        expectations=expectations,
                    )
                )

                # (4) the issue's named trap, as a mutation arm on the SAME
                # trace: the grant document's every DTO value intact,
                # parked under a key the production caller never loads — a
                # correct-looking DTO authorizes NOTHING (typed refusal,
                # zero broker calls). The baseline leg (1) proved the same
                # trace redeems when the caller is wired; this leg proves
                # the endpoint keys on the CALLER's document, never on any
                # correct-looking grant DTO.
                async with application.state.session_factory() as session:
                    row = await session.get(FlowRunRow, GRANT_WORK_ID)
                    evidence = dict(row.evidence or {})
                    grants = dict(evidence.get(seams["grants_key"]) or {})
                    evidence["credential_operation_grants_orphaned"] = grants
                    evidence.pop(seams["grants_key"], None)
                    row.evidence = evidence
                    await session.commit()
                # R40-06 (#342): the grant's ONE authoritative home is the
                # KEYED row; orphaning only the (derived) evidence
                # projection disconnects nothing anymore. The disconnect
                # takes the authority row too — every DTO value stays
                # intact, parked where the production caller cannot load it.
                from sqlalchemy import delete  # noqa: PLC0415

                from forge.durable.models import OperationGrant  # noqa: PLC0415

                async with application.state.session_factory() as session:
                    await session.execute(
                        delete(OperationGrant).where(OperationGrant.work_id == GRANT_WORK_ID)
                    )
                    await session.commit()
                try:
                    orphan = await _redeem(client, ref=GRANT_REF, provider=GRANT_PROVIDER)
                    orphan_detail = orphan.text[:200] if orphan.status_code != 200 else ""
                    expectations = {
                        "refused_403": orphan.status_code == 403,
                        "typed_grant_absent": "grant_absent" in orphan_detail,
                        "zero_broker_calls": broker.resolve_calls == [GRANT_REF],
                        "no_new_audit_row": len(await _audit_rows(application)) == len(rows),
                    }
                finally:
                    async with application.state.session_factory() as session:
                        row = await session.get(FlowRunRow, GRANT_WORK_ID)
                        evidence = dict(row.evidence or {})
                        evidence[seams["grants_key"]] = evidence.pop(
                            "credential_operation_grants_orphaned", {}
                        )
                        row.evidence = evidence
                        await session.commit()
                    # Restore the authority row EXACTLY: the same grant id
                    # and deadline re-persist (the replay keeps the first
                    # window — nothing downstream observes the mutation).
                    await seams["persist_operation_grant"](
                        application.state.session_factory, grant=effective
                    )
                caught = all(expectations.values())
                arms.append(
                    ArmRecord(
                        arm="grant:mutation:dto_preserved_caller_disconnected",
                        status="caught" if caught else "escape",
                        expectations=expectations,
                        detail=(
                            "the disconnected grant DTO still authorized a "
                            "redemption — the endpoint does not key on the "
                            "production caller's document"
                            if not caught
                            else "caught: every DTO value intact yet parked where the "
                            "production caller cannot load it — typed refusal, "
                            "zero broker calls"
                        ),
                    )
                )
    finally:
        seams["reset_engine"]()
        if saved_anchor is None:
            os.environ.pop(seams["anchor_env"], None)
        else:
            os.environ[seams["anchor_env"]] = saved_anchor
    return arms


def _drive_runner_verification(seams: dict[str, Any], workroot: Path) -> list[ArmRecord]:
    """The runner-side typed verification (the #320 CD-9 shape), as REAL
    ``python -m forge.lane_driver`` subprocesses against a CANNED
    redemption endpoint: the BASELINE (the correct document) passes the
    same trace first — the fake model endpoint receives the delivered
    sentinel — then a wrong-slot and an expired answer each halt the lane
    with ``credential_redemption_failed`` and ZERO calls at the endpoint
    (the vendor client is never constructed; the ambient never
    substitutes)."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    arms: list[ArmRecord] = []

    def _lane_job(
        document: dict[str, Any], endpoint: Any, case: str
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        canned = seams["CannedRedemptionEndpoint"](document)
        try:
            sdk_dir = workroot / f"sdk-{case}"
            sdk_dir.mkdir(parents=True, exist_ok=True)
            (sdk_dir / "claude_agent_sdk.py").write_text(seams["fake_sdk_stub"])
            job_dir = workroot / f"lane-job-{case}"
            env = seams["lane_job_env"](
                canned.base_url, "any-token-works-here", endpoint_url=endpoint.url, sdk_dir=sdk_dir
            )
            env["FORGE_ATTEMPT_GENERATION"] = "0"
            result = seams["run_lane_job"](job_dir, env)
            meta_path = job_dir / ".forge" / "candidate.meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            return result, meta
        finally:
            canned.stop()

    def _endpoint_scoped(runner: Any) -> ArmRecord:
        # each leg gets its OWN endpoint: the zero-calls assertion is
        # about THAT leg's vendor client, never a leftover from another.
        endpoint = seams["FakeModelEndpoint"]()
        try:
            return runner(endpoint)
        finally:
            endpoint.close()

    def _baseline_arm(endpoint: Any) -> ArmRecord:
        # (5) the BASELINE of the SAME canned-endpoint trace: the correct
        # document applies the delivered sentinel and the driven turn
        # presents it — the harness is wired, so the refusal legs below
        # can never pass vacuously.
        result, meta = _lane_job(seams["lying_document"](), endpoint, "baseline")
        bearers = endpoint.bearers()
        expectations = {
            "job_completed": result.returncode == 0,
            "meta_completed": meta.get("exit") == "completed",
            "endpoint_received_the_delivered_sentinel": bearers
            == [f"Bearer {seams['runner_delivered']}"],
            "ambient_never_presented": all(
                seams["runner_ambient"] not in bearer for bearer in bearers
            ),
        }
        return ArmRecord(
            arm="runner-verification:baseline_applies_delivered",
            status="pass" if all(expectations.values()) else "fail",
            job_rc=result.returncode,
            expectations=expectations,
            detail=_tail(result.stdout + result.stderr) if not all(expectations.values()) else "",
        )

    def _refusal_arm(endpoint: Any, *, axis: str, document: dict[str, Any], case: str) -> ArmRecord:
        result, meta = _lane_job(document, endpoint, case)
        expectations = {
            "lane_halted": result.returncode == 1,
            "typed_redemption_failure": meta.get("terminal_reason")
            == "credential_redemption_failed",
            f"the_{axis}_axis_named": axis in str(meta.get("error") or ""),
            "zero_model_calls": endpoint.bearers() == [],
            "ambient_never_substituted": seams["runner_ambient"] not in result.stdout
            and seams["runner_ambient"] not in result.stderr,
        }
        return ArmRecord(
            arm=f"runner-verification:{axis}_answer_halts_zero_calls",
            status="pass" if all(expectations.values()) else "fail",
            job_rc=result.returncode,
            expectations=expectations,
            detail=_tail(result.stdout + result.stderr) if not all(expectations.values()) else "",
        )

    arms.append(
        _guarded_arm("runner-verification:baseline", lambda: _endpoint_scoped(_baseline_arm))
    )
    wrong_slot = seams["lying_document"](env_var="OPENAI_API_KEY")
    arms.append(
        _guarded_arm(
            "runner-verification:wrong_slot",
            lambda: _endpoint_scoped(
                lambda endpoint: _refusal_arm(
                    endpoint, axis="env_var", document=wrong_slot, case="wrong-slot"
                )
            ),
        )
    )
    stale = (_datetime.now(_UTC) - _timedelta(seconds=1)).isoformat()
    expired = seams["lying_document"](expires_at=stale)
    arms.append(
        _guarded_arm(
            "runner-verification:expired",
            lambda: _endpoint_scoped(
                lambda endpoint: _refusal_arm(
                    endpoint, axis="expires_at", document=expired, case="expired"
                )
            ),
        )
    )
    return arms


#: The rebind identity variables a revision-bound dispatch carries (the
#: executor-input identity #321 digests — non-secret digests only).
REBIND_IDENTITY_VARIABLES = (
    "FORGE_RUN_ID",
    "FORGE_PLAN_DIGEST",
    "FORGE_BRIEF_ENVELOPE_DIGEST",
    "FORGE_SPEC_DIGEST",
    "FORGE_LANE_RESUME_MODE",
)


async def _drive_rebind_digest(seams: dict[str, Any], workroot: Path) -> dict[str, Any]:
    """The #321 rebind digest through the consumer, offline: the REAL
    ``RunService`` dispatch through the fake native server's GitLab mode,
    with an ACTIVE revision seeded before the /go (the revision-bound
    branch of the same ``resolve_approved_input`` every dispatch entry
    runs). The recorded dispatch variables and the persisted evidence
    must agree — the three-way equality, offline shape (the PE trace
    with the real approval/retry flow stays the deeper proof)."""
    import tempfile as _tempfile

    from forge.adaptive.models import PlanRevision, PlanStep  # noqa: PLC0415
    from forge.adaptive.revisions import (  # noqa: PLC0415
        ACTIVE_PLAN_KEY,
        APPROVED_INPUT_KEY,
        REVISION_CONTENT_KEY,
        REVISION_EXECUTOR_DIGEST_KEY,
        executor_input_digest,
        plan_digest,
    )
    from forge.harnesses.brief_envelope import verify_brief_envelope  # noqa: PLC0415

    pe = seams["pe"]
    from tests.test_gate_conformance import _fake_native  # noqa: PLC0415

    class _RecordingGitLabClient:
        """The real client wrapper that records FULL key→value dicts (the
        rebind arm needs the dispatched VALUES — digests and digested
        bytes only; no secret ever rides this dispatch)."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.captured: list[dict[str, str]] = []

        async def create_pipeline(self, project_id, ref, variables=None):
            self.captured.append(
                {str(entry["key"]): str(entry["value"]) for entry in (variables or [])}
            )
            return await self._inner.create_pipeline(project_id, ref, variables=variables)

        async def read_blob(self, project_id, file_path, ref="HEAD"):
            return await self._inner.read_blob(project_id, file_path, ref=ref)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    revision = PlanRevision(
        plan_id="plan-conf-327",
        work_id="wp-conf-327",
        revision=1,
        parent_revision=None,
        work_contract_digest="3" * 64,
        snapshot_set_digest="4" * 64,
        summary=f"Standing direction: the entrypoint is {REBIND_NEW_NAME}, not {REBIND_OLD_NAME}.",
        steps=[
            PlanStep(
                step_id="s1",
                objective=f"Rename the entrypoint to {REBIND_NEW_NAME}.",
                write_repository_id="repo",
            )
        ],
    )
    revision_digest = plan_digest(revision)

    root = Path(_tempfile.mkdtemp(prefix="conf-rebind-", dir=workroot))
    arms: list[ArmRecord] = []
    with _fake_native(root, gitlab=True) as native:
        native.seed_issue(pe.GL_ISSUE_IID, pe.GL_ISSUE_TITLE, pe.GL_ISSUE_DESC)
        database = pe.PEDatabase(f"sqlite+aiosqlite:///{root / 'rebind.db'}")
        await database.create_schema()
        try:
            from forge.config import ForgeConfig  # noqa: PLC0415
            from forge.gitlab.client import GitLabClient  # noqa: PLC0415
            from forge.runs.service import RunService  # noqa: PLC0415
            from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer  # noqa: PLC0415

            client = _RecordingGitLabClient(
                GitLabClient(base_url=native.base_url, token="conf-gate-glpat")
            )
            service = RunService(
                database.worker_factory(),
                gitlab=client,
                settings=pe.gl_settings(),
                config=ForgeConfig(),
                planner=StubPlanner(),
                implementer=StubImplementer(),
                reviewer=StubReviewer(),
            )
            run_id = await service.start_run(
                pe.GL_PROJECT_ID,
                pe.GL_ISSUE_IID,
                pe.GL_ISSUE_TITLE,
                pe.GL_ISSUE_DESC,
                "alice",
            )
            # The ACTIVE revision, staged the app's own durable way (the
            # #313 disclosure: the planner does not emit revisions; the
            # evidence shape is production's).
            factory = database.worker_factory()
            from forge.durable import FlowRun as FlowRunRow  # noqa: PLC0415

            async with factory() as session:
                row = await session.get(FlowRunRow, run_id)
                evidence = dict(row.evidence or {})
                evidence[ACTIVE_PLAN_KEY] = {
                    "schema": "forge.revision.active-plan/1",
                    "work_id": revision.work_id,
                    "plan_id": revision.plan_id,
                    "active_revision": 1,
                    "work_contract_digest": revision.work_contract_digest,
                    "authorization_epoch": 4,
                    "publication_epoch": 1,
                    "plan_digest": revision_digest,
                    "revised_from_digest": "",
                    "activated_by_decision": "",
                    REVISION_CONTENT_KEY: revision.model_dump(),
                }
                row.evidence = evidence
                await session.commit()
            with seams["scoped_env"](
                **{
                    seams["delivery_route_env"]: "",
                    seams["delivery_template_dir_env"]: str(TEMPLATES_DIR),
                }
            ):
                await service.handle_command_note(
                    pe.GL_PROJECT_ID,
                    f"@forge /go {run_id}",
                    "alice",
                    pe.GL_ISSUE_IID,
                    author_user_id=11,
                )
            if not client.captured:
                raise AssertionError("the rebind drive never dispatched")
            variables = client.captured[-1]
            async with factory() as session:
                row = await session.get(FlowRunRow, run_id)
                evidence = dict(row.evidence or {})
            await client.close()

            # (6) the dispatched envelope: the revision-bound variables a
            # consumer re-verifies the brief against.
            expectations = {
                f"carries_{name.lower()}": name in variables for name in REBIND_IDENTITY_VARIABLES
            }
            expectations["briefs_the_revision_not_the_spec"] = (
                REBIND_NEW_NAME in variables.get("FORGE_PLAN", "")
                and evidence.get(APPROVED_INPUT_KEY, {}).get("source") == "revision"
            )
            arms.append(
                ArmRecord(
                    arm="rebind:dispatch_carries_the_envelope",
                    status="pass" if all(expectations.values()) else "fail",
                    expectations=expectations,
                    detail="the revision-bound dispatch variables are incomplete"
                    if not all(expectations.values())
                    else "",
                )
            )

            # (7) THE three-way equality, offline shape: the persisted
            # executor-input digest == the digest recomputed from the
            # RECORDED dispatch variables, and the dispatched envelope
            # digest verifies over the dispatched FORGE_PLAN bytes (the
            # bytes the CI job env exports and the lane consumes).
            document = dict(evidence.get(REVISION_EXECUTOR_DIGEST_KEY) or {})
            identity = {
                "run_id": variables.get("FORGE_RUN_ID", ""),
                "plan_digest": variables.get("FORGE_PLAN_DIGEST", ""),
                "envelope_digest": variables.get("FORGE_BRIEF_ENVELOPE_DIGEST", ""),
                "spec_digest": variables.get("FORGE_SPEC_DIGEST", ""),
                "lane_resume_mode": variables.get("FORGE_LANE_RESUME_MODE", ""),
            }
            envelope_error = ""
            try:
                verify_brief_envelope(
                    identity["envelope_digest"],
                    run_id=run_id,
                    task_title=pe.GL_ISSUE_TITLE,
                    task_description=pe.GL_ISSUE_DESC,
                    plan_text=variables.get("FORGE_PLAN", ""),
                    spec_digest=identity["spec_digest"],
                )
            except Exception as exc:  # noqa: BLE001 — the verification's refusal detail
                envelope_error = f"{type(exc).__name__}: {exc}"[:200]
            expectations = {
                "evidence_persisted_the_executor_digest": bool(document)
                and bool(document.get("executor_input_digest")),
                "evidence_equals_the_recorded_dispatch": bool(document)
                and document.get("executor_input_digest") == executor_input_digest(identity),
                "evidence_envelope_digest_equals_the_dispatched_one": bool(document)
                and document.get("envelope_digest") == identity["envelope_digest"],
                "envelope_verifies_over_the_dispatched_plan_bytes": not envelope_error,
            }
            arms.append(
                ArmRecord(
                    arm="rebind:three_way_digest_equality",
                    status="pass" if all(expectations.values()) else "fail",
                    expectations=expectations,
                    detail=envelope_error
                    or (
                        "the persisted executor digest and the recorded dispatch disagree"
                        if not all(expectations.values())
                        else ""
                    ),
                )
            )

            # (8) the rendered-template leg: the recipes that consume the
            # digested bytes — every SDK lane recipe references FORGE_PLAN
            # (GitLab exports pipeline variables into the job env; the
            # recipe's own script is the consumption).
            consuming = sorted(
                name
                for name in seams["harness_templates"]
                if "FORGE_PLAN" in (TEMPLATES_DIR / name).read_text(encoding="utf-8")
            )
            expectations = {
                "sdk_lane_recipes_consume_the_plan": "claude-sdk-lane.gitlab-ci.yml" in consuming,
                "batch_lane_recipes_consume_the_plan": "claude-code.gitlab-ci.yml" in consuming,
            }
            arms.append(
                ArmRecord(
                    arm="rebind:templates_consume_the_digested_bytes",
                    status="pass" if all(expectations.values()) else "fail",
                    expectations=expectations,
                    detail=f"consuming recipes: {consuming}"
                    if not all(expectations.values())
                    else "",
                )
            )

            # (9) the mutation legs — SAME recorded trace, each defect the
            # equality exists to catch, each MUST be caught (an escape is
            # the gate proving itself insensitive). The plan-binding
            # removal replays the #321 counterexample exactly: the lane
            # briefed from the SPEC's frozen summary while the envelope
            # claims the active revision.
            spec_brief = str((evidence.get("plan") or {}).get("summary") or "")
            superseded_verified = False
            try:
                verify_brief_envelope(
                    identity["envelope_digest"],
                    run_id=run_id,
                    task_title=pe.GL_ISSUE_TITLE,
                    task_description=pe.GL_ISSUE_DESC,
                    plan_text=spec_brief or "not-the-approved-plan",
                    spec_digest=identity["spec_digest"],
                )
                superseded_verified = True  # the superseded brief VERIFIED — an escape
            except Exception:  # noqa: BLE001 — the expected typed refusal
                pass
            expectations = {
                "the_spec_brief_is_not_the_dispatched_bytes": bool(spec_brief)
                and spec_brief != variables.get("FORGE_PLAN", ""),
                "envelope_refuses_the_superseded_brief": not superseded_verified,
            }
            caught = all(expectations.values())
            arms.append(
                ArmRecord(
                    arm="rebind:mutation:plan_binding_removed",
                    status="caught" if caught else "escape",
                    expectations=expectations,
                    detail=(
                        "the superseded spec brief still verified against the "
                        "dispatched envelope digest — the #321 counterexample "
                        "would pass"
                        if not caught
                        else "caught: the envelope only verifies over the ACTIVE "
                        "revision's bytes, never the spec brief it replaced"
                    ),
                )
            )

            # The source-identity swap: the same identity with the SPEC's
            # digest standing in for the ACTIVE revision's plan digest
            # (a dispatch that briefed under one source while stamping the
            # other) must NOT satisfy the equality.
            swapped = dict(identity, plan_digest=identity["spec_digest"])
            swapped_matches = executor_input_digest(swapped) == document.get(
                "executor_input_digest"
            )
            expectations = {
                "a_swapped_source_identity_changes_the_digest": not swapped_matches,
                "the_active_revision_digest_is_what_was_dispatched": document.get("plan_digest")
                == revision_digest,
            }
            caught = all(expectations.values())
            arms.append(
                ArmRecord(
                    arm="rebind:mutation:source_identity_swapped",
                    status="caught" if caught else "escape",
                    expectations=expectations,
                    detail=(
                        "a swapped source identity still satisfied the equality — "
                        "the digest does not bind the dispatched plan"
                        if not caught
                        else "caught: the identity digest binds the ACTIVE "
                        "revision's plan digest, not the spec's"
                    ),
                )
            )
            return {
                "arms": arms,
                "recorded_variables": sorted(variables),
                "run_id": run_id,
                "active_revision_digest": revision_digest,
            }
        finally:
            await database.dispose()


def run_consumer_contracts(seams: dict[str, Any], workroot: Path) -> dict[str, Any]:
    """Check 5's entry: the grant ASGI arms, the runner-verification
    subprocess arms and the rebind digest arms — failures and mutation
    escapes aggregated exactly like the other checks."""
    consumer_seams = _consumer_seams()
    consumer_seams["harness_templates"] = seams["HARNESS_TEMPLATES"]
    failures: list[str] = []
    escapes: list[str] = []
    rebind_facts: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="conf-consumers-", dir=workroot) as tmp:
        root = Path(tmp)
        grant_arms = _guarded_arms(
            "grant", failures, escapes, lambda: asyncio.run(_drive_grant_asgi(consumer_seams, root))
        )
        runner_arms = _guarded_arms(
            "runner-verification",
            failures,
            escapes,
            lambda: _drive_runner_verification(consumer_seams, root),
        )
        rebind_arms = _guarded_arms(
            "rebind",
            failures,
            escapes,
            lambda: asyncio.run(_drive_rebind_digest(consumer_seams, root)),
            facts_out=rebind_facts,
        )
    all_arms = [*(grant_arms or []), *(runner_arms or []), *rebind_arms]
    return {
        "status": "fail" if failures or escapes else "pass",
        "arms": [arm.as_document() for arm in all_arms],
        "failures": failures,
        "mutation_escapes": escapes,
        "rebind": rebind_facts,
        "sentinel_policy": (
            "sentinel values only, never a real credential; the values never "
            "enter the report — boolean expectations + refs"
        ),
        "offline_scope": (
            "all arms offline/deterministic: an in-process ASGI transport, "
            "loopback canned endpoints and the fake native server — no "
            "provider, no paid model, no real secret"
        ),
    }


def _guarded_arms(
    group: str,
    failures: list[str],
    escapes: list[str],
    runner: Any,
    facts_out: dict[str, Any] | None = None,
) -> list[ArmRecord]:
    """Run one arm group; a group-level break is a FAILED arm carrying
    the exception (exit 8), never a gate crash."""
    try:
        outcome = runner()
    except Exception as exc:  # noqa: BLE001 — the group's own failure detail
        failures.append(f"{group}:group_failure:{type(exc).__name__}")
        return [ArmRecord(arm=f"{group}:group_failure", status="fail", detail=f"{exc}")]
    if facts_out is not None and isinstance(outcome, dict):
        arms = outcome.pop("arms")
        facts_out.update({key: value for key, value in outcome.items() if key != "arms"})
    else:
        arms = outcome
    for record in arms:
        if record.status == "fail":
            failures.append(f"{group}:{record.arm}")
        if record.status == "escape":
            escapes.append(f"{group}:{record.arm}")
    return arms


# ---------------------------------------------------------------------------
# Check 5 — end
# ---------------------------------------------------------------------------


def run_dispatch_schema(seams: dict[str, Any], captures: dict[str, Any]) -> dict[str, Any]:
    providers = captures.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise PrerequisiteError("the capture fixture carries no providers")
    providers_report: dict[str, Any] = {}
    findings: list[str] = []

    github_shapes = _shapes(providers, "github")
    declared = github_declared_inputs(yaml_doc(TEMPLATES_DIR / GITHUB_TEMPLATE))
    github_findings = declared_vs_captured(
        "github", declared, github_shapes, CONDITIONAL_INPUTS.get("github")
    )
    providers_report["github"] = {
        "declared": sorted(declared),
        "captured_shapes": {shape: sorted(keys) for shape, keys in sorted(github_shapes.items())},
        "findings": [finding.__dict__ for finding in github_findings],
    }

    azure_shapes = _shapes(providers, "azure")
    declared = azure_declared_parameters(yaml_doc(TEMPLATES_DIR / AZURE_TEMPLATE))
    azure_findings = declared_vs_captured(
        "azure", declared, azure_shapes, CONDITIONAL_INPUTS.get("azure")
    )
    providers_report["azure"] = {
        "declared": sorted(declared),
        "captured_shapes": {shape: sorted(keys) for shape, keys in sorted(azure_shapes.items())},
        "findings": [finding.__dict__ for finding in azure_findings],
    }

    gitlab_shapes = _shapes(providers, "gitlab")
    consumed: set[str] = set()
    for name in seams["HARNESS_TEMPLATES"]:
        consumed |= referenced_forge_variables((TEMPLATES_DIR / name).read_text(encoding="utf-8"))
    gitlab_findings = sent_vs_consumed(gitlab_shapes, consumed)
    providers_report["gitlab"] = {
        "consumed_by_shipped_recipes": sorted(consumed),
        "captured_shapes": {shape: sorted(keys) for shape, keys in sorted(gitlab_shapes.items())},
        "findings": [finding.__dict__ for finding in gitlab_findings],
    }

    for provider, report in providers_report.items():
        for finding in report["findings"]:
            findings.append(f"{provider}:{finding['type']}:{finding['key']}")
    return {
        "status": "fail" if findings else "pass",
        "providers": providers_report,
        "findings": findings,
        # Q39-04 (#323): the locator dimension of the captured dispatch
        # surface. The captures record KEY SETS (never values), and the
        # shipped recipes compose the carrier name at RUNTIME from the
        # dispatched ref (FORGE_MODEL_<ref>), so the boundary accepts
        # both the legacy segment spelling and the collision-safe
        # locator spelling of the credential_ref value — the EXECUTED
        # proof lives in the secret-consumers sentinel_locator arms and
        # the native-locators encoding arms.
        "locator_dimension": {
            "credential_ref_key": {
                "github": "credential_ref",
                "azure": "credential_ref",
                "gitlab": CREDENTIAL_REF_ENV,
            },
            "carrier_derivation": "runtime FORGE_MODEL_<ref> composition (spelling-agnostic)",
            "executed_proof": [
                "secret-consumers sentinel_locator arms",
                "native-locators encoding arms",
            ],
        },
    }


def _shapes(providers: dict[str, Any], provider: str) -> dict[str, list[str]]:
    entry = providers.get(provider)
    if not isinstance(entry, dict) or not isinstance(entry.get("shapes"), dict):
        raise PrerequisiteError(
            f"the capture fixture carries no {provider} shapes — regenerate it with "
            "`--regenerate-captures`"
        )
    shapes = entry["shapes"]
    if not shapes:
        raise PrerequisiteError(f"the capture fixture carries an EMPTY {provider} shape set")
    return {str(shape): [str(key) for key in keys] for shape, keys in shapes.items()}


# ---------------------------------------------------------------------------
# Report + entry point
# ---------------------------------------------------------------------------


def source_identity() -> dict[str, Any]:
    commit, dirty = None, False
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
            ).stdout.strip()
            or None
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout
        dirty = bool(status.strip())
    except OSError:
        pass
    return {"git_commit": commit, "tree_dirty": dirty}


def build_report(
    recipes: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    sentinels: dict[str, Any] | None,
    locators: dict[str, Any] | None,
    consumers: dict[str, Any] | None,
    started_iso: str,
    duration: float,
    refusals: list[ConformanceGateError],
) -> dict[str, Any]:
    executed_ids: list[str] = []
    if recipes:
        for template in recipes["templates"]:
            for arm in template["arms"]:
                if arm["status"] != "absent":
                    executed_ids.append(f"shipped-recipes/{template['template']}/{arm['arm']}")
    if schema:
        executed_ids.append("dispatch-schema/github")
        executed_ids.append("dispatch-schema/azure")
        executed_ids.append("dispatch-schema/gitlab")
    if sentinels:
        for recipe in sentinels["recipes"]:
            for arm in recipe["arms"]:
                executed_ids.append(f"secret-consumers/{recipe['template']}/{arm['arm']}")
    if locators:
        executed_ids.append("native-locators/encoding")
        for entry in locators.get("driver_templates", []):
            if entry["status"] in {"pass", "fail"}:
                executed_ids.append(f"native-locators/driver/{entry['driver']}")
        for arm in locators.get("self_tests", {}):
            executed_ids.append(f"native-locators/self-test/{arm}")
    if consumers:
        for arm in consumers["arms"]:
            executed_ids.append(f"consumer-contracts/{arm['arm']}")
    # Q39-08 (#327): the required consumer cases — the arms the gate
    # cannot qualify without. Anything REQUIRED that did not execute
    # (a check refused before reaching it) is MISSING, never silently
    # absent (the observable `conformance.required_case_missing`).
    required_consumer_ids = [
        f"consumer-contracts/{arm}"
        for arm in (
            "grant:redeem_granted_route",
            "grant:sibling_route_refused_zero_broker",
            "grant:audit_value_free",
            "grant:mutation:dto_preserved_caller_disconnected",
            "runner-verification:baseline_applies_delivered",
            "runner-verification:env_var_answer_halts_zero_calls",
            "runner-verification:expires_at_answer_halts_zero_calls",
            "rebind:dispatch_carries_the_envelope",
            "rebind:three_way_digest_equality",
            "rebind:templates_consume_the_digested_bytes",
            "rebind:mutation:plan_binding_removed",
            "rebind:mutation:source_identity_swapped",
        )
    ]
    required_case_missing = [case for case in required_consumer_ids if case not in executed_ids]
    report: dict[str, Any] = {
        "gate": {
            "script": "scripts/gate_conformance.py",
            "issue": "#317 (R38-16); native-locators #323 (Q39-04); consumer-contracts #327 (Q39-08)",
            "started_utc": started_iso,
            "duration_seconds": round(duration, 3),
            "source_identity": source_identity(),
        },
        "checks": {
            "shipped_recipes": recipes,
            "dispatch_schema": schema,
            "secret_consumers": sentinels,
            "native_locators": locators,
            "consumer_contracts": consumers,
        },
        # Q39-08 (#327): the exact source/template/runtime identity per
        # check and per arm class — offline, native and paid arms are
        # DISTINGUISHABLE in the report (every arm this gate runs is
        # offline; the native/paid dimensions live on the qualification
        # job and the PG gate, named here so the report never lets an
        # offline green pose as a live one).
        "identities": case_identities(locators, consumers),
        # The #267 pattern: WHAT actually executed, per arm — never an
        # aggregate green alone.
        "executed_ids": executed_ids,
        "observability": {
            "conformance.executed_case_count": len(executed_ids),
            "conformance.required_case_missing": required_case_missing,
            "ci.critical_path_seconds": round(duration, 3),
        },
        "qualification": {
            "result": "refused" if refusals else "green",
            "refusals": [
                {
                    "type": type(refusal).__name__,
                    "detail": str(refusal),
                    "exit_code": refusal.exit_code,
                }
                for refusal in refusals
            ],
        },
        "ci": {
            "runner": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
            "lane": "lint (every push); the PG profiles live on the integration job",
        },
    }
    return report


#: The per-arm execution classes the identity block names (offline vs
#: native vs paid — the Q39-08 report requirement).
ARM_EXECUTION_CLASSES: dict[str, str] = {
    "grant:": "offline-asgi (real route, real app, in-process transport)",
    "runner-verification:": "offline-subprocess (real `python -m forge.lane_driver`)",
    "rebind:": "offline-drive (real RunService over the fake native server)",
}


def case_identities(
    locators: dict[str, Any] | None, consumers: dict[str, Any] | None
) -> dict[str, Any]:
    """The identity stamping: what source, what templates (by identity
    digest), what runtime, and which execution class each arm ran under."""
    import platform  # noqa: PLC0415

    digest_inventory: dict[str, str] = dict((locators or {}).get("digest_inventory") or {})
    try:
        captures_ref = str(CAPTURES_PATH.relative_to(REPO_ROOT))
    except ValueError:  # a test redirected the fixture outside the repo
        captures_ref = str(CAPTURES_PATH)
    arm_identities = {
        ARM_EXECUTION_CLASSES[prefix]: [
            arm["arm"]
            for arm in ((consumers or {}).get("arms") or [])
            if arm["arm"].startswith(prefix)
        ]
        for prefix in ARM_EXECUTION_CLASSES
    }
    return {
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "runner": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
        },
        "checks": {
            "shipped_recipes": {
                "execution": "offline (real bash + real git checkout)",
                "source": "scripts/gate_conformance.py:run_shipped_recipes",
                "template_digests": digest_inventory,
            },
            "dispatch_schema": {
                "execution": "offline (declared surface vs committed captures)",
                "source": "scripts/gate_conformance.py:run_dispatch_schema",
                "captures": captures_ref,
            },
            "secret_consumers": {
                "execution": "offline (real bash, sentinel env)",
                "source": "scripts/gate_conformance.py:run_secret_consumers",
            },
            "native_locators": {
                "execution": "offline (structural + digests)",
                "source": "scripts/gate_conformance.py:run_locator_conformance",
                "digest_inventory": digest_inventory,
            },
            "consumer_contracts": {
                "execution": "offline (per-arm classes below)",
                "source": "scripts/gate_conformance.py:run_consumer_contracts",
                "consumers": (
                    "forge.api_lane_control:redeem_lane_credential (real ASGI), "
                    "forge.lane_driver (real subprocess), "
                    "forge.runs.service.RunService (real dispatch)"
                ),
                "arm_classes": arm_identities,
            },
        },
        "execution_classes": {
            "offline": "this gate — every arm, every push (lint)",
            "native": "the PG gate's required selection (integration job, real PostgreSQL)",
            "paid": "the triggered live-qualification job (scripts/run_live_qualification.py)",
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="conformance-gate", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write the gate report JSON here (the CI artifact)",
    )
    parser.add_argument(
        "--regenerate-captures",
        action="store_true",
        help="drive the REAL services through the production-entry fakes and rewrite "
        "scripts/conformance_dispatch_captures.json (the recorded production truth)",
    )
    parser.add_argument(
        "--skip-executed", action="store_true", help="run only the capture-fixture checks"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    del env  # the gate reads no environment secrets
    started_iso = datetime.now(UTC).isoformat(timespec="seconds")
    started = time.monotonic()

    if args.regenerate_captures:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from tests.test_gate_conformance import capture_dispatch_payloads  # noqa: PLC0415

        document = capture_dispatch_payloads()
        CAPTURES_PATH.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"conformance-gate: captures regenerated at {CAPTURES_PATH}")
        return 0

    refusals: list[ConformanceGateError] = []
    recipes = schema = sentinels = locators = consumers = None
    try:
        _require_tools()
        seams = _load_seams()
        captures = load_captures()
        with tempfile.TemporaryDirectory(prefix="forge-conformance-gate-") as tmp:
            workroot = Path(tmp)
            recipes = run_shipped_recipes(seams, workroot)
            schema = run_dispatch_schema(seams, captures)
            sentinels = run_secret_consumers(seams)
            locators = run_locator_conformance(seams)
            consumers = run_consumer_contracts(seams, workroot)
    except ConformanceGateError as exc:
        refusals.append(exc)
        print(f"conformance-gate: REFUSED ({type(exc).__name__}) — {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — an unplanned break is a prerequisite-class refusal
        refusal = PrerequisiteError(f"unexpected gate failure: {type(exc).__name__}: {exc}")
        refusals.append(refusal)
        print(f"conformance-gate: REFUSED — {refusal}", file=sys.stderr)

    # Collected failures become typed refusals AFTER every check ran, so
    # the report always carries the full picture.
    if recipes and recipes["mutation_escapes"]:
        refusals.append(
            MutationEscapeError(
                "the restored unconditional-exit mutation went uncaught on: "
                + ", ".join(recipes["mutation_escapes"])
            )
        )
    if sentinels and sentinels["mutation_escapes"]:
        refusals.append(
            MutationEscapeError(
                "the dropped consumer-mapping mutation went uncaught on: "
                + ", ".join(sentinels["mutation_escapes"])
            )
        )
    if locators and locators["mutation_escapes"]:
        refusals.append(
            MutationEscapeError(
                "the native-locator self-tests went uncaught on: "
                + ", ".join(locators["mutation_escapes"])
            )
        )
    if recipes and recipes["failures"]:
        refusals.append(
            RecipeExecutionError(
                "shipped-recipe execution failures: " + ", ".join(recipes["failures"])
            )
        )
    if schema and schema["findings"]:
        refusals.append(
            SchemaConformanceError("dispatch-schema findings: " + ", ".join(schema["findings"]))
        )
    if sentinels and sentinels["failures"]:
        refusals.append(
            SecretConsumerError(
                "secret-consumer sentinel failures: " + ", ".join(sentinels["failures"])
            )
        )
    if locators and (locators["failures"] or locators["mutation_escapes"]):
        refusals.append(
            LocatorConformanceError(
                "native-locator conformance failures: " + ", ".join(locators["failures"])
            )
        )
    if consumers:
        if consumers["mutation_escapes"]:
            refusals.append(
                MutationEscapeError(
                    "the consumer-contract mutations went uncaught on: "
                    + ", ".join(consumers["mutation_escapes"])
                )
            )
        if consumers["failures"]:
            refusals.append(
                ConsumerContractError(
                    "consumer-contract failures: " + ", ".join(consumers["failures"])
                )
            )

    duration = time.monotonic() - started
    report = build_report(
        recipes, schema, sentinels, locators, consumers, started_iso, duration, refusals
    )
    if args.report is not None:
        args.report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    if refusals:
        for refusal in refusals:
            print(f"conformance-gate: {type(refusal).__name__}: {refusal}", file=sys.stderr)
        return refusals[0].exit_code

    executed = len(report["executed_ids"])
    print(
        f"conformance-gate: GREEN — {executed} executed arm ids across 5 checks; "
        "all mutation self-tests caught; 0 schema findings"
    )
    if args.report is not None:
        print(f"conformance-gate: report written to {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
