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

Three checks, every push (the lint job — fast, no database, no provider):

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
   be CAUGHT despite a correct-looking receipt.

Usage::

    uv run python scripts/gate_conformance.py --report conformance-gate.json

Exit codes: 0 green · 2 prerequisite · 3 shipped-recipe execution ·
4 dispatch-schema findings · 5 secret-consumer sentinel · 6 mutation
escape (the gate is insensitive — the most severe outcome there is).

Relationship to the PG gate (``scripts/pg_gate.py``, #267): this gate
runs on EVERY push (lint); the PG profiles run on the integration job
against real PostgreSQL. Both write an executed-ID manifest; neither
ever converts a failure into a skip.
"""

from __future__ import annotations

import argparse
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
            credential_secret_segment,
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
                failures=failures,
                escapes=escapes,
            )
        )

    return {
        "status": "fail" if failures or escapes else "pass",
        "recipes": recipes_report,
        "failures": failures,
        "mutation_escapes": escapes,
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
    failures: list[str],
    escapes: list[str],
) -> dict[str, Any]:
    """sentinel / fail-closed / dropped-mapping arms for ONE recipe."""

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

    recipe_failures = [record for record in arms if record.status == "fail"]
    return {
        "template": template,
        "status": "fail" if recipe_failures else "pass",
        "arms": [record.as_document() for record in arms],
    }


# ---------------------------------------------------------------------------
# Check 2 — the dispatch-schema conformance (declared vs captured)
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
    report: dict[str, Any] = {
        "gate": {
            "script": "scripts/gate_conformance.py",
            "issue": "#317 (R38-16)",
            "started_utc": started_iso,
            "duration_seconds": round(duration, 3),
            "source_identity": source_identity(),
        },
        "checks": {
            "shipped_recipes": recipes,
            "dispatch_schema": schema,
            "secret_consumers": sentinels,
        },
        # The #267 pattern: WHAT actually executed, per arm — never an
        # aggregate green alone.
        "executed_ids": executed_ids,
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
    recipes = schema = sentinels = None
    try:
        _require_tools()
        seams = _load_seams()
        captures = load_captures()
        with tempfile.TemporaryDirectory(prefix="forge-conformance-gate-") as tmp:
            workroot = Path(tmp)
            recipes = run_shipped_recipes(seams, workroot)
            schema = run_dispatch_schema(seams, captures)
            sentinels = run_secret_consumers(seams)
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

    duration = time.monotonic() - started
    report = build_report(recipes, schema, sentinels, started_iso, duration, refusals)
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
        f"conformance-gate: GREEN — {executed} executed arm ids across 3 checks; "
        "both mutation self-tests caught; 0 schema findings"
    )
    if args.report is not None:
        print(f"conformance-gate: report written to {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
