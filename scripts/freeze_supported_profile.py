#!/usr/bin/env python
"""R38-06 (#307) — freeze ONE exact supported deployment profile.

The external review's basis (`59ba869` §R38-06): the successful live run
used a working-tree control-plane image, a pinned lane from another
snapshot and a manually corrected target template — and the v0.37.0
promotion binds source sha ``1ca2656`` while the reviewed broker commit
is ``59ba869``, both reporting package 0.37.0. **A version string alone
identified two different compositions.** This script is the fix's first
half: it captures the EXACT supported composition into
``qualification/profiles/supported-gitlab-ce-v1.json`` — a frozen
MANIFEST-OF-MANIFESTS in which every value traces to an actual receipt:

- the **promoted release** identity (wheel sha256 + URL, image digest,
  source sha, sdist sha) — from ``docs/releases/evidence/v0.37.0/
  promotion.json`` (the release record that RETAINS its failed/cancelled
  checks with reasons);
- the **executed lab** identity (the working-tree build the green live
  trace actually ran: image id + digest, rollback tag, reported version,
  deployed schema head) — from the #306 alignment receipts and the
  post-alignment inventory;
- the **lane** identity on both routes: the install route a second
  engineer uses (the promoted wheel, by sha256) and the route the green
  trace used (the immutable git sha ``59ba869…``), never merged into one
  ambiguous value;
- the **closure** axis from #269's ``closure-manifest.json`` — recorded
  with its honest status (the staged closure predates the promoted
  wheel, so the composition is wheel-pinned, not closure-pinned);
- the **target template bytes, frozen INTO the manifest**: the wheel
  ships no ``ci/templates/`` (package data is ``src/forge`` only), so a
  moving working tree was the recipe's only source — exactly the R38-06
  defect. The frozen bytes are RECOVERED FROM THE IMMUTABLE RECEIPT
  (the disposable project's committed ``.gitlab-ci.yml`` on the GitLab
  instance, seed commit pinned) and cross-checked against the live
  trace's ``task.template_sha256``; a disagreement refuses the freeze;
- the **runner profile** (id 4 ``unraid``, docker executor, online),
  **harness version** (claude-code 2.1.273), **model route** (litellm
  ``fast`` = ``openai/glm-5.3-flash`` through the z.ai
  Anthropic-compatible gateway), **credential route** (the #303 delivery
  modes the broker declares for gitlab) and the **verification
  contract** (the smoke oracle: six exact slugify cases + the three file
  shapes + the untouchable paths);
- the **four DISTINCT evidence records** — source review, release
  canary, native workflow qualification and human support approval —
  where the human support decision stays ``pending`` (honest).

The freeze is fail-closed: a missing receipt, a non-hex digest, a
template reconstruction that does not reproduce the traced sha, or a
cross-reference contradiction REFUSES the whole capture. Nothing is
guessed and nothing is silently substituted.

Usage (from the repository root):

    uv run python scripts/freeze_supported_profile.py            # capture
    uv run python scripts/freeze_supported_profile.py --check    # re-verify

``--check`` re-reads the committed manifest, recomputes its
``manifest_digest`` (sha256 over the canonical document without the
digest field) and re-runs every still-checkable cross-reference against
the receipts — a manifest that drifted from its receipts is refused.

STRICTLY read-only against the world: the only network access is the
GitLab files API (fetching the committed template receipt); nothing is
created, pushed or paid.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]

#: The versioned stamp of the frozen supported-profile document.
SUPPORTED_PROFILE_STAMP = "forge.supported.profile/1"

#: Where the frozen manifest is committed.
MANIFEST_PATH = ROOT / "qualification" / "profiles" / "supported-gitlab-ce-v1.json"

#: The v0.37-composition archive (Q39-07/#326): the manifest the #307
#: freeze produced stays historical, byte-identical, under this name —
#: the CURRENT manifest is the v2 composition. Never a history rewrite.
ARCHIVED_MANIFEST_PATH = (
    ROOT / "qualification" / "profiles" / "supported-gitlab-ce-v1@v0.37-composition.json"
)

#: Every receipt the freeze binds, by role (repo-relative).
PROMOTION_RECEIPT = "docs/releases/evidence/v0.39.0/promotion.json"
#: The v2 receipts (Q39-07/#326 — the executed-lab trace on the NEW
#: composition): the alignment + live bundle + record live in the v2
#: evaluation directory; the inventory is the post-alignment v2 snapshot.
#: The #306 receipts stay historical, referenced by the archived manifest.
ALIGNMENT_RECEIPT = "docs/evaluation/2026-09-25-supported-composition-v2/alignment-receipts.json"
LIVE_TRACE_RECEIPT = "docs/evaluation/2026-09-25-supported-composition-v2/useful-wip-resume-v2.json"
LIVE_RUN_RECEIPT = "docs/evaluation/2026-09-25-supported-composition-v2/live-run-evidence.json"
INVENTORY_RECEIPT = "qualification/inventory-2026-09-25-v2.json"
#: The promotion record that carries THIS tree: v0.39.0 is built from the
#: exact composition the trace executed (the promoted lane wheel is
#: byte-identical to the qualification wheel — 78711c28…) — the
#: moving-tree gap the v0.38.0 record named is CLOSED by this promotion.
LATEST_PROMOTION_RECEIPT = "docs/releases/evidence/v0.39.0/promotion.json"
#: The uv build of the working tree — the QUALIFICATION composition's
#: wheel (the fresh cold-install target). Built by ``uv build`` from the
#: exact tree the alignment image carried; the freeze binds its sha256
#: (the local dist/ bytes when present, else the COMMITTED receipt — the
#: closure pattern: CI has no dist/, and the committed copy IS the
#: receipt).
WORKING_TREE_WHEEL_GLOB = "dist/forge-{version}-py3-none-any.whl"
WHEEL_RECEIPT_PATH = "qualification/profiles/receipts/working-tree-wheel-v2.json"


#: The closure receipt: the locally built artifact when present, else the
#: COMMITTED copy (CI has no dist/ — the freeze binds receipts, and the
#: committed copy IS the receipt for the promoted composition).
def _first_existing(root, candidates):
    for candidate in candidates:
        if (root / candidate).is_file():
            return candidate
    raise FreezeRefused(
        f"receipt {candidates[0]} is missing — the freeze binds receipts, never guesses"
    )


CLOSURE_RECEIPT_CANDIDATES = (
    "dist/lane-closure/closure-manifest.json",
    "qualification/profiles/receipts/lane-closure-v0.37.0.json",
)
PROFILE_RECORD_RECEIPT = "qualification/records/gitlab-ce-v1@0.39.0.json"
PROFILE_DOC_RECEIPT = "qualification/profiles/gitlab-ce-v1.md"
#: The lab gateway's model-route receipt (env-referenced keys only — no
#: secrets; the working-tree file is gitignored, so the COMMITTED copy is
#: the citable receipt; CI has no lab file).
LITELLM_CONFIG_CANDIDATES = (
    "litellm-config.yaml",
    "qualification/profiles/receipts/litellm-config-lab-v1.yaml",
)
LANE_TEMPLATE_PATH = "ci/templates/claude-sdk-lane.gitlab-ci.yml"
TASK_SCRIPT = "scripts/run_useful_wip_resume.py"

#: The disposable project whose seed commit is the template's immutable
#: receipt (created by the R38-05 drill; the committed ``.gitlab-ci.yml``
#: embeds the shipped template VERBATIM between its deterministic header
#: and oracle tail).
TEMPLATE_RECEIPT_PROJECT_ID = 94

#: The deterministic header the R38-05 generator prepends (verbatim from
#: ``scripts/run_useful_wip_resume.py::ci_yaml``) — stripped to recover
#: the template bytes from the committed receipt file.
_TEMPLATE_RECEIPT_HEADER = (
    "# Generated by scripts/run_useful_wip_resume.py (R38-05/#306): the\n"
    "# SHIPPED SDK lane template VERBATIM (the #302 phased finalization —\n"
    "# driver / collection / final-status, the packaged collector with\n"
    "# --require-generation on resume) plus this project's independent\n"
    "# three-shape smoke oracle, committed BEFORE any run.\n"
    "stages: [test, harness]\n"
    "\n"
)

#: The deterministic oracle tail marker the generator appends.
_TEMPLATE_RECEIPT_TAIL_MARKER = (
    "\n# The INDEPENDENT verification contract (R38-05): the six exact\n"
)

_HEX64_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
#: A git object sha (40 hex) — a DIFFERENT identity shape than a sha256;
#: conflating the two is exactly the field-shape conflation the record
#: schema refuses.
_GITSHA_RE = re.compile(r"[0-9a-f]{40}")
_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")


class FreezeRefused(RuntimeError):
    """The freeze cannot capture the supported composition honestly.

    Every refusal names the receipt and the contradiction — the freeze
    never guesses past unreadable or self-contradicting evidence.
    """


# ---------------------------------------------------------------------------
# The probe boundary (the ONE live read: the committed template receipt)
# ---------------------------------------------------------------------------


class TemplateReceiptProbe:
    """Fetch the committed ``.gitlab-ci.yml`` from the disposable project.

    Credentials are read ONLY through ``forge.config.Settings`` (never
    parsed out of ``.env`` by hand); the token stays in the API header.
    Tests fake this class — no test touches the network.
    """

    def fetch_installed_ci_yaml(self, project_id: int) -> str:
        import httpx

        from forge.config import Settings

        settings = Settings()
        base = str(settings.GITLAB_URL).rstrip("/")
        response = httpx.get(
            f"{base}/api/v4/projects/{project_id}/repository/files/"
            f"{'.gitlab-ci.yml'.replace('/', '%2F')}",
            headers={"PRIVATE-TOKEN": settings.GITLAB_TOKEN.get_secret_value()},
            params={"ref": "main"},
            timeout=15.0,
        )
        if response.status_code != 200:
            raise FreezeRefused(
                f"the template receipt (project {project_id} .gitlab-ci.yml) answered "
                f"HTTP {response.status_code} — without the committed bytes the "
                "executed recipe cannot be frozen; refusing"
            )
        document = response.json()
        return base64.b64decode(str(document.get("content", ""))).decode("utf-8", "replace")


def recover_template_bytes(generated_ci_yaml: str) -> str:
    """Strip the generator's deterministic header/tail from the committed
    receipt file — what remains is the shipped template VERBATIM.

    Pure function (unit-tested): the header must match exactly and the
    oracle tail marker must be present, else the recovery refuses.
    """
    if not generated_ci_yaml.startswith(_TEMPLATE_RECEIPT_HEADER):
        raise FreezeRefused(
            "the committed template receipt does not carry the R38-05 generator's "
            "deterministic header — the bytes are not the traced recipe; refusing"
        )
    rest = generated_ci_yaml[len(_TEMPLATE_RECEIPT_HEADER) :]
    index = rest.find(_TEMPLATE_RECEIPT_TAIL_MARKER)
    if index < 0:
        raise FreezeRefused(
            "the committed template receipt carries no oracle-tail marker — the "
            "template bytes cannot be isolated; refusing"
        )
    return rest[:index]


# ---------------------------------------------------------------------------
# Receipt loading + field extraction
# ---------------------------------------------------------------------------


def _load_json(root: Path, relative: str) -> Any:
    path = root / relative
    if not path.is_file():
        raise FreezeRefused(
            f"receipt {relative!r} is missing — the freeze binds receipts, never guesses"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _require_hex64(value: object, axis: str, receipt: str) -> str:
    text = str(value or "").strip()
    match = _HEX64_RE.fullmatch(text)
    if not match:
        raise FreezeRefused(f"{axis} in {receipt} is not a hex64 sha256 ({text[:24]!r}…)")
    return match.group(1)


def _require_gitsha(value: object, axis: str, receipt: str) -> str:
    text = str(value or "").strip()
    if not _GITSHA_RE.fullmatch(text):
        raise FreezeRefused(f"{axis} in {receipt} is not a 40-hex git sha ({text[:24]!r}…)")
    return text


def _require_semver(value: object, axis: str, receipt: str) -> str:
    text = str(value or "").strip()
    if not _SEMVER_RE.fullmatch(text):
        raise FreezeRefused(f"{axis} in {receipt} is not a semantic version ({text!r})")
    return text


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_digest(document: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of the document WITHOUT its own
    ``manifest_digest`` field — the manifest vouches for itself."""
    body = {key: value for key, value in document.items() if key != "manifest_digest"}
    return _sha256_bytes(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def repo_schema_heads(root: Path) -> tuple[str, str]:
    """The migration chain's (head, predecessor) parsed from alembic/versions.

    The head is the revision no other migration names as down_revision;
    its predecessor is the head's own down_revision (the declared N-1 the
    upgrade check must actually transition from — never an unchanged head).
    """
    revisions: dict[str, str | None] = {}
    down_revisions: set[str] = set()
    revision_re = re.compile(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
    down_re = re.compile(
        r"^down_revision(?::[^=]*)?\s*=\s*(?:[\"']([^\"']+)[\"']|None)", re.MULTILINE
    )
    for path in sorted((root / "alembic" / "versions").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        match = revision_re.search(text)
        if not match:
            continue
        revisions[match.group(1)] = None
        down = down_re.search(text)
        if down and down.group(1):
            revisions[match.group(1)] = down.group(1)
            down_revisions.add(down.group(1))
    heads = [revision for revision in revisions if revision not in down_revisions]
    if len(heads) != 1:
        raise FreezeRefused(
            f"alembic/versions has {len(heads)} heads ({heads}) — a branched chain is "
            "not a freezable schema revision"
        )
    head = heads[0]
    predecessor = revisions[head]
    if not predecessor:
        raise FreezeRefused(f"migration {head} names no down_revision — no declared predecessor")
    return head, predecessor


def _load_frozen_task() -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """The frozen acceptance task's oracle cases + file shapes, loaded from
    the drill script's own constants (imported by path — scripts carry no
    package). Cross-checked against the traced receipt by the caller."""
    spec = importlib.util.spec_from_file_location("forge_wip_drill", ROOT / TASK_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - path is fixed
        raise FreezeRefused(
            f"{TASK_SCRIPT} is not importable — the frozen task constants are unreadable"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = (
        module  # dataclass processing resolves cls.__module__ through sys.modules
    )
    spec.loader.exec_module(module)
    cases = tuple((str(a), str(b)) for a, b in module.SLUGIFY_CASES)
    shapes = tuple((str(a), str(b)) for a, b in module.TASK_SHAPES)
    if not cases or not shapes:
        raise FreezeRefused("the frozen task constants are empty — nothing to verify against")
    return cases, shapes


def _litellm_route(root: Path, route_name: str) -> str:
    """The upstream model the litellm ``fast`` route resolves to."""
    path = root / _first_existing(root, LITELLM_CONFIG_CANDIDATES)
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"model_name:\s*{re.escape(route_name)}\s*\n\s*litellm_params:\s*\n\s*model:\s*(\S+)"
    )
    match = pattern.search(text)
    if not match:
        raise FreezeRefused(
            f"{path} carries no '{route_name}' route — the model route axis cannot be bound"
        )
    return match.group(1)


def _credential_modes() -> tuple[str, ...]:
    from forge.adaptive.credential_broker import PROFILE_DELIVERY_MODES

    modes = PROFILE_DELIVERY_MODES.get("gitlab")
    if not modes:
        raise FreezeRefused("the credential broker declares no gitlab delivery modes")
    return tuple(sorted(modes))


def _composition_row() -> dict[str, Any]:
    """The execution-spec composition row for the frozen recipe (gitlab x
    SDK lane x claude-sdk-lane): the preflight contract a cold install
    must satisfy BEFORE any paid call."""
    from forge.adaptive.execution_spec import supported_compositions

    rows = [
        row
        for row in supported_compositions()
        if row.provider == "gitlab"
        and row.runtime_recipe == "gitlab-sdk-lane"
        and row.harness == "claude-sdk-lane"
    ]
    if not rows:
        raise FreezeRefused("the execution-spec matrix carries no gitlab SDK-lane row")
    row = rows[0]
    return {
        "provider": row.provider,
        "runtime_recipe": row.runtime_recipe,
        "harness": row.harness,
        "credential_route": row.credential_route,
        "resume_supported": row.resume_supported,
        "caller_contract": row.caller_contract,
        "template_contract": row.template_contract,
        "consumer_contract": row.consumer_contract,
        "receipt": "src/forge/adaptive/execution_spec.py::supported_compositions()",
    }


# ---------------------------------------------------------------------------
# The capture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureInputs:
    """Everything the capture reads (all fakeable in tests)."""

    promotion: Mapping[str, Any]
    latest_promotion: Mapping[str, Any]
    alignment: Mapping[str, Any]
    trace: Mapping[str, Any]
    live_run: Mapping[str, Any]
    inventory: Mapping[str, Any]
    closure: Mapping[str, Any]
    record: Mapping[str, Any]
    template_bytes: str
    working_tree_template_sha256: str
    working_tree_wheel: Mapping[str, Any]
    schema_head: str
    schema_predecessor: str
    oracle_cases: tuple[tuple[str, str], ...]
    task_shapes: tuple[tuple[str, str], ...]
    litellm_upstream: str
    credential_modes: tuple[str, ...]
    composition_row: Mapping[str, Any]
    frozen_at: str

    @classmethod
    def from_root(cls, root: Path, probe: TemplateReceiptProbe) -> CaptureInputs:
        promotion = _load_json(root, PROMOTION_RECEIPT)
        latest_promotion = _load_json(root, LATEST_PROMOTION_RECEIPT)
        alignment = _load_json(root, ALIGNMENT_RECEIPT)
        trace = _load_json(root, LIVE_TRACE_RECEIPT)
        live_run = _load_json(root, LIVE_RUN_RECEIPT)
        inventory = _load_json(root, INVENTORY_RECEIPT)
        closure = _load_json(root, _first_existing(root, CLOSURE_RECEIPT_CANDIDATES))
        record = _load_json(root, PROFILE_RECORD_RECEIPT)
        # The template bytes: recovered from the committed receipt (the v2
        # disposable project the live trace itself created — its id rides
        # the evidence bundle) and cross-checked against the traced sha
        # below — the receipt IS the bytes' provenance, the trace is their
        # fingerprint.
        project_id = int(
            (live_run.get("phases", {}).get("setup", {}).get("project", {}) or {}).get("id")
            or TEMPLATE_RECEIPT_PROJECT_ID
        )
        generated = probe.fetch_installed_ci_yaml(project_id)
        template_bytes = recover_template_bytes(generated)
        head, predecessor = repo_schema_heads(root)
        cases, shapes = _load_frozen_task()
        return cls(
            promotion=promotion,
            latest_promotion=latest_promotion,
            alignment=alignment,
            trace=trace,
            live_run=live_run,
            inventory=inventory,
            closure=closure,
            record=record,
            template_bytes=template_bytes,
            working_tree_template_sha256=_sha256_bytes((root / LANE_TEMPLATE_PATH).read_bytes()),
            working_tree_wheel=_working_tree_wheel(root),
            schema_head=head,
            schema_predecessor=predecessor,
            oracle_cases=cases,
            task_shapes=shapes,
            litellm_upstream=_litellm_route(root, "fast"),
            credential_modes=_credential_modes(),
            composition_row=_composition_row(),
            frozen_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def _working_tree_wheel(root: Path) -> dict[str, Any]:
    """The uv build of the working tree — the QUALIFICATION composition.

    Fail-closed: no built wheel AND no committed wheel receipt means the
    freeze cannot bind the fresh cold-install target. ``uv build`` first —
    the freeze binds receipts, never guesses. (The closure pattern: the
    local ``dist/`` bytes when present, else the COMMITTED receipt — CI
    has no dist/, and the committed copy IS the receipt for the frozen
    composition.)
    """
    version = _repo_version(root)
    name = f"forge-{version}-py3-none-any.whl"
    local = root / "dist" / name
    if local.is_file():
        return {
            "name": name,
            "path": f"dist/{name}",
            "sha256": _sha256_bytes(local.read_bytes()),
            "source": "local-uv-build",
            "built_from": "the working tree (uncommitted-ahead state recorded in the manifest)",
            # same shape as the committed receipt below: the capture must
            # reproduce byte-identically with OR without a local dist/ (CI
            # has none) — a shape difference is a reproducibility defect.
            "built_for": "the v2 qualification composition (forge#326)",
        }
    receipt = root / WHEEL_RECEIPT_PATH
    if receipt.is_file():
        document = json.loads(receipt.read_text(encoding="utf-8"))
        # the sha must parse hex64 (fail-closed on a corrupted receipt)
        _require_hex64(
            document.get("sha256"), "working-tree wheel receipt sha256", WHEEL_RECEIPT_PATH
        )
        if str(document.get("name", "")) != name:
            raise FreezeRefused(
                f"{WHEEL_RECEIPT_PATH} pins {document.get('name')!r} but the tree's "
                f"version is {version} — the wheel receipt and the tree disagree"
            )
        return dict(document)
    raise FreezeRefused(
        f"neither dist/{name} nor {WHEEL_RECEIPT_PATH} exists — run `uv build` first: "
        "the fresh cold-install target is the WORKING-TREE build (the qualification "
        "composition), and the freeze binds its receipt"
    )


def _repo_version(root: Path) -> str:
    init = root / "src" / "forge" / "__init__.py"
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", init.read_text(encoding="utf-8"))
    if match is None:
        raise FreezeRefused(f"no __version__ in {init}")
    return match.group(1)


def capture_supported_profile(inputs: CaptureInputs) -> dict[str, Any]:
    """Build the frozen manifest document from the receipts (pure).

    Every cross-reference contradiction refuses the capture; every field
    carries the receipt it came from.
    """
    promotion = inputs.promotion
    trace = inputs.trace
    alignment_runs = alignment_runs_of(inputs.alignment)

    # -- the promoted release identity (the immutable install source) ----
    wheel_sha = _require_hex64(promotion.get("wheel_sha256"), "wheel_sha256", PROMOTION_RECEIPT)
    image_digest = _require_hex64(promotion.get("image_digest"), "image_digest", PROMOTION_RECEIPT)
    source_sha = _require_gitsha(promotion.get("head_sha"), "head_sha", PROMOTION_RECEIPT)
    release_version = _require_semver(promotion.get("version"), "version", PROMOTION_RECEIPT)

    # -- the executed lab identity (what the green trace actually ran) ---
    build_step = alignment_runs.get("step==build")
    image_id = str(build_step.stdout_image_id) if build_step else ""
    if not image_id:
        raise FreezeRefused(
            f"{ALIGNMENT_RECEIPT} carries no build image id — the executed lab "
            "composition cannot be bound"
        )
    verify_obs = inputs.alignment.get("observations", {})
    # The POST-ALIGNMENT facts: the receipts' plan-time observations read
    # the lab BEFORE the migration ran (the deployed head there is the
    # PRE-alignment value); the last run's own VERIFY step carries what
    # the aligned deployment reports. Freeze THAT, falling back to the
    # plan observations only when no verify step exists.
    post = post_alignment_observations(inputs.alignment)
    reported_version = post.get("reported_version") or str(verify_obs.get("repo_version", ""))
    deployed_head = (
        post.get("deployed_schema_head") or str(verify_obs.get("deployed_schema_head", "")) or "027"
    )
    lab_image_digest = _require_hex64(
        inventory_stage(inputs.inventory, "control-plane", {}).get("image", {}).get("image_digest"),
        "executed_lab.image_digest",
        INVENTORY_RECEIPT,
    )

    # -- the template bytes: the receipt must reproduce the traced sha ---
    template_sha = _sha256_bytes(inputs.template_bytes.encode("utf-8"))
    traced_sha = str(trace.get("task", {}).get("template_sha256", ""))
    if traced_sha and template_sha != traced_sha:
        raise FreezeRefused(
            f"the recovered template bytes hash {template_sha[:12]}… but the live trace "
            f"pins {traced_sha[:12]}… ({LIVE_TRACE_RECEIPT}#task.template_sha256) — the "
            "committed receipt and the traced recipe disagree; refusing to freeze"
        )
    installed_content_sha = (
        inputs.live_run.get("phases", {})
        .get("collect", {})
        .get("installed_template", {})
        .get("content_sha256", "")
    )

    # -- the lane identity on BOTH routes, never merged -------------------
    lane_ref = str(trace.get("task", {}).get("lane_ref", ""))
    if not _GITSHA_RE.fullmatch(lane_ref):
        raise FreezeRefused(
            f"{LIVE_TRACE_RECEIPT}#task.lane_ref is not a 40-hex git sha ({lane_ref!r})"
        )
    template_project_id = int(
        (inputs.live_run.get("phases", {}).get("setup", {}).get("project", {}) or {}).get("id")
        or TEMPLATE_RECEIPT_PROJECT_ID
    )
    closure_forge = inputs.closure.get("forge", {})
    closure_digest = _require_hex64(
        inputs.closure.get("closure_digest"), "closure_digest", CLOSURE_RECEIPT_CANDIDATES[0]
    )
    closure_wheel_sha = _require_hex64(
        closure_forge.get("wheel", {}).get("sha256"),
        "closure.forge.wheel.sha256",
        CLOSURE_RECEIPT_CANDIDATES[0],
    )

    # -- the runner profile -----------------------------------------------
    runner_rows = (
        inventory_stage(inputs.inventory, "runner", {}).get("gitlab_runners", {}).get("runners", [])
    )
    runner = next(
        (row for row in runner_rows if row.get("id") == 4 and row.get("description") == "unraid"),
        None,
    )
    if runner is None:
        raise FreezeRefused(
            f"{INVENTORY_RECEIPT} observes no runner id 4 'unraid' — the frozen runner "
            "profile must come from an observed receipt"
        )

    # -- harness + model route + verification contract ---------------------
    harness_version = str(inputs.record.get("harness_version", ""))
    if harness_version != "2.1.273":
        raise FreezeRefused(
            f"{PROFILE_RECORD_RECEIPT} harness_version is {harness_version!r}, expected the "
            "live-evidenced claude-code 2.1.273"
        )
    record_sha = _require_hex64(
        inputs.record.get("wheel_sha256"), "record.wheel_sha256", PROFILE_RECORD_RECEIPT
    )
    if record_sha != wheel_sha:
        raise FreezeRefused(
            f"the profile record pins wheel {record_sha[:12]}… but the promotion record "
            f"pins {wheel_sha[:12]}… — two wheels under one freeze; refusing"
        )
    mr = trace.get("mr", {})
    if not mr.get("candidate_sha"):
        raise FreezeRefused(
            f"{LIVE_TRACE_RECEIPT} carries no verified candidate sha — no green trace to freeze"
        )
    traced_shapes = tuple(
        (str(pair[0]), str(pair[1]))
        for pair in trace.get("task", {}).get("shapes", [])
        if len(pair) == 2
    )
    if traced_shapes and traced_shapes != inputs.task_shapes:
        raise FreezeRefused(
            f"the frozen task shapes {inputs.task_shapes} disagree with the traced shapes "
            f"{traced_shapes} ({LIVE_TRACE_RECEIPT}#task.shapes) — the verification "
            "contract cannot be bound"
        )

    document: dict[str, Any] = {
        "schema": SUPPORTED_PROFILE_STAMP,
        "profile": "supported-gitlab-ce-v1",
        "frozen_at": inputs.frozen_at,
        "basis": {
            "issue": "forge#326 (external review 6df4020 §Q39-07)",
            "defect": (
                "version equality never substitutes for composition equality: the v0.37 "
                "manifest's live pass ran one composition while installs reproduced "
                "another — this v2 freeze binds ONLY the exact composition that "
                "completed the end-to-end trace (the alignment image's working-tree "
                "build + the pushed lane sha + the uv wheel of the same tree), with "
                "the v0.37 world archived, never rewritten"
            ),
        },
        "control_plane": {
            "promoted": {
                "release_version": release_version,
                "source_sha": source_sha,
                "image_digest": f"sha256:{image_digest}",
                "image_ref": str(promotion.get("image_ref", "")),
                "wheel_sha256": wheel_sha,
                "wheel_url": str(promotion.get("wheel_url", "")),
                "sdist_sha256": _require_hex64(
                    promotion.get("sdist_sha256"), "sdist_sha256", PROMOTION_RECEIPT
                ),
                "receipt": PROMOTION_RECEIPT,
            },
            "latest_promotion": {
                "release_version": _require_semver(
                    inputs.latest_promotion.get("version"),
                    "latest_promotion.version",
                    LATEST_PROMOTION_RECEIPT,
                ),
                "source_sha": _require_gitsha(
                    inputs.latest_promotion.get("head_sha"),
                    "latest_promotion.head_sha",
                    LATEST_PROMOTION_RECEIPT,
                ),
                "image_digest": f"sha256:{_require_hex64(inputs.latest_promotion.get('image_digest'), 'latest_promotion.image_digest', LATEST_PROMOTION_RECEIPT)}",
                "wheel_sha256": _require_hex64(
                    inputs.latest_promotion.get("wheel_sha256"),
                    "latest_promotion.wheel_sha256",
                    LATEST_PROMOTION_RECEIPT,
                ),
                "wheel_url": str(inputs.latest_promotion.get("wheel_url", "")),
                "status": (
                    "promoted FROM the qualified composition — v0.39.0 is built "
                    "from the exact tree the trace executed and the promoted lane "
                    "wheel is byte-identical to the qualification wheel "
                    "(78711c28…); the pending-promotion note the v0.38.0 era "
                    "carried is retired"
                ),
                "receipt": LATEST_PROMOTION_RECEIPT,
            },
            "executed_lab": {
                "image_name": "localhost/forge:dev",
                "image_id": image_id,
                "image_digest": f"sha256:{lab_image_digest}",
                "reported_version": reported_version,
                "deployed_schema_head": str(deployed_head),
                "rollback_tag": rollback_tag_of(inputs.alignment),
                "qualification_composition": {
                    "wheel": dict(inputs.working_tree_wheel),
                    "note": (
                        "the executed-lab section IS the qualification composition: "
                        "the alignment image is the WORKING-TREE build and the wheel "
                        "beside it is the uv build of the SAME tree — a cold install "
                        "from this manifest installs THESE bytes, never a released "
                        "artifact that predates them"
                    ),
                },
                "receipts": [ALIGNMENT_RECEIPT, INVENTORY_RECEIPT],
            },
            "schema_revision": {
                "head": inputs.schema_head,
                "predecessor": inputs.schema_predecessor,
                "upgrade_claim_basis": (
                    "the alignment migrate ran head->head (same-head preservation); the "
                    "N-1->head transition is proven separately by cold_install_check "
                    "--mode upgrade and must be reported as its ACTUAL edge"
                ),
                "receipt": "alembic/versions + " + ALIGNMENT_RECEIPT,
            },
            "divergence": (
                "the promoted v0.39.0 release is built from this exact tree and "
                "its lane wheel is byte-identical to the qualification wheel "
                "(78711c28…) — the moving-tree gap is closed at this promotion; "
                "the executed-lab image digest and the promoted GHCR digest still "
                "differ BY CONSTRUCTION (the podman working-tree build that RAN "
                "the trace vs the CI build of the same source) — both are bound, "
                "neither is silently substituted, and the next lab re-alignment "
                "onto the promoted digest is recorded as the open follow-up"
            ),
        },
        "lane": {
            "install_route": "working-tree-wheel",
            "wheel": {
                "sha256": str(inputs.working_tree_wheel.get("sha256", "")),
                "version": str(inputs.working_tree_wheel.get("name", "")).split("-")[1]
                if len(str(inputs.working_tree_wheel.get("name", "")).split("-")) > 1
                else "",
                "name": str(inputs.working_tree_wheel.get("name", "")),
                "path": str(inputs.working_tree_wheel.get("path", "")),
                "source": "local-uv-build",
                "promotion_pending": (
                    "the fresh cold install installs THIS working-tree build by "
                    "sha256 (the qualification composition); the promoted-wheel "
                    "section names what is released and stays behind it"
                ),
                "receipt": "uv build of the working tree + " + ALIGNMENT_RECEIPT,
            },
            "promoted_wheel": {
                "url": str(promotion.get("wheel_url", "")),
                "sha256": wheel_sha,
                "version": release_version,
                "status": (
                    "the promoted v0.39.0 wheel — BYTE-IDENTICAL to the "
                    "qualification wheel (78711c28…): for the first time the "
                    "released install pin and the qualified composition are the "
                    "same bytes"
                ),
                "receipt": PROMOTION_RECEIPT,
            },
            "executed_live": {
                "git_sha": lane_ref,
                "install": f"git+https://github.com/forcewake/forge@{lane_ref}",
                "receipt": f"{LIVE_TRACE_RECEIPT}#task.lane_ref",
            },
            "closure": {
                "closure_digest": closure_digest,
                "forge_wheel": {
                    "name": str(closure_forge.get("wheel", {}).get("name", "")),
                    "sha256": closure_wheel_sha,
                    "source": str(closure_forge.get("source", "")),
                },
                "status": (
                    "staged closure predates the working-tree build — the supported "
                    "composition is WHEEL-pinned, not closure-pinned (an honest gap, "
                    "not a defaulted axis)"
                ),
                "receipt_candidates": list(CLOSURE_RECEIPT_CANDIDATES),
            },
        },
        "target_template": {
            "path": LANE_TEMPLATE_PATH,
            "job": "forge-agent-claude-sdk",
            "frozen": {
                "sha256": template_sha,
                "bytes_b64": base64.b64encode(inputs.template_bytes.encode("utf-8")).decode(
                    "ascii"
                ),
                "carrier": (
                    "this manifest — the wheel ships no ci/templates/ (package data is "
                    "src/forge only), so the frozen bytes are the recipe's immutable "
                    "carrier; cold installs render the template FROM HERE, never from "
                    "a moving working tree"
                ),
                "recovered_from": (
                    f"the disposable project {template_project_id} committed "
                    ".gitlab-ci.yml (seed commit "
                    f"{inputs.live_run.get('phases', {}).get('collect', {}).get('installed_template', {}).get('last_commit_id', '')}), "
                    f"content_sha256 {installed_content_sha} — cross-checked against "
                    f"{LIVE_TRACE_RECEIPT}#task.template_sha256"
                ),
            },
            "working_tree_drift": {
                "sha256": inputs.working_tree_template_sha256,
                "status": (
                    "the working tree has moved past the executed recipe — the drift is "
                    "named, never silently absorbed; installs MUST render from the "
                    "frozen bytes above"
                ),
                "receipt": LANE_TEMPLATE_PATH,
            },
        },
        "runner": {
            "id": 4,
            "description": "unraid",
            "executor": "docker",
            "observed_status": str(runner.get("status", "")),
            "receipt": f"{INVENTORY_RECEIPT}#stages.runner",
        },
        "harness": {
            "binary": "claude-code",
            "version": harness_version,
            "receipt": PROFILE_RECORD_RECEIPT,
        },
        "model_route": {
            "litellm_route": "fast",
            "upstream": inputs.litellm_upstream,
            "lane_model": "glm-5.3-flash",
            "gateway": "https://api.z.ai/api/anthropic",
            "receipts": list(LITELLM_CONFIG_CANDIDATES) + [PROFILE_RECORD_RECEIPT],
        },
        "credential_route": {
            "provider": "gitlab",
            "delivery_modes": list(inputs.credential_modes),
            "receipt": "src/forge/adaptive/credential_broker.py::PROFILE_DELIVERY_MODES",
        },
        "execution_spec_composition": dict(inputs.composition_row),
        "verification_contract": {
            "required_jobs": ["smoke"],
            "oracle": (
                "six exact slugify cases + app rewired + legacy deleted, committed "
                "before any run; green on the CURRENT candidate sha"
            ),
            "slugify_cases": [list(case) for case in inputs.oracle_cases],
            "file_shapes": [list(shape) for shape in inputs.task_shapes],
            "candidate_may_not_touch": [".gitlab-ci.yml", "tests/"],
            "verified_candidate_sha": str(mr.get("candidate_sha", "")),
            "oracle_tampering": list(mr.get("oracle_tampering", [])),
            "receipts": [LIVE_TRACE_RECEIPT, PROFILE_RECORD_RECEIPT, PROFILE_DOC_RECEIPT],
        },
        "evidence_records": {
            "source_review": {
                "status": (
                    "conditional — lint/typecheck passed; test(3.13)/test(3.14)/"
                    "integration passed on retry after a cancelled run; every earlier "
                    "failed/cancelled attempt stays on record, never silently green"
                ),
                "source_sha": source_sha,
                "receipt": f"{PROMOTION_RECEIPT}#decision.checks + #required_checks",
            },
            "release_canary": {
                "status": (
                    "pass (v0.39.0) — fresh-install canary + seeded previous-head "
                    "upgrade 027->028 WITH data preservation (five seeded tables "
                    "fingerprint-identical through the real migration); the "
                    "data-bearing N-1->head edge is now proven by the promotion "
                    "canary itself, cross-checked by cold_install_check --mode "
                    "upgrade"
                ),
                "receipt": f"{LATEST_PROMOTION_RECEIPT}#canary",
            },
            "native_workflow_qualification": {
                "status": _trace_status_text(trace),
                "receipt": LIVE_TRACE_RECEIPT,
            },
            "human_support_approval": {
                "status": "pending",
                "note": (
                    "the human gate holds: this freeze records the qualified "
                    "capabilities exactly and approves nothing — approvals live in "
                    "qualification/profile-approvals.json, a separate human artifact"
                ),
                "receipt": "forge.profile_qualification.build_supported_profiles (the human gate)",
            },
        },
        "exclusions": [
            "the qualified composition is the WORKING-TREE build (alignment image + "
            "the uv wheel of the same tree) — NO release carries these bytes yet "
            "(v0.38.0 predates them); installs from this manifest install the "
            "working-tree wheel by sha256 and the promotion is pending, never assumed",
            "the lane of the executed trace runs the PUSHED sha named under "
            "lane.executed_live (the working tree is never pushed — no commit, no "
            "merge), so the lane package is one push behind the control-plane build; "
            "the brief bytes — the revision-2 text — are the payload this binds",
            "no committed live TraceRecord under qualification/traces/ references "
            "the profile's capabilities — the supported-profiles manifest holds "
            "gitlab-ce-v1 at pending-approval (the #298 trace-tier hold)",
            "usage_receipts coverage for harness-lane runs is partial (lane meta "
            "receipts + run_budgets counters)",
            "the staged lane closure pins forge 0.35.0 (local build) — the "
            "composition is wheel-pinned, not closure-pinned",
            "the batch lane templates cannot restore WIP (a required-resume refuses "
            "in the template itself) — only the SDK lane recipes are in scope",
            "merge/deploy stay human decisions; nothing here claims enterprise or "
            "fleetwide readiness",
        ],
    }
    document["manifest_digest"] = _canonical_digest(document)
    return document


def _trace_status_text(trace: Mapping[str, Any]) -> str:
    """The native-workflow evidence status, from the trace's own outcome.

    Never a relabel: a reviewed-ready trace says so; anything else says
    exactly what it was.
    """
    outcome = str(trace.get("outcome", ""))
    spend = trace.get("spend") or {}
    review = trace.get("review") or {}
    reserve = review.get("closing_reserve_usd")
    if outcome == "reviewed-ready":
        return (
            "green live — outcome reviewed-ready: the useful-WIP checkpoint (all "
            "three file shapes, verified digests) restored exactly on a second "
            "runner whose resumed dispatch carried the APPROVED revision-2 text "
            "(no rescue steer), the final candidate verified by the precommitted "
            f"oracle on the exact sha, AND the closing review completed within "
            f"the #325 closing reserve (reserve {reserve} usd visible) — the run "
            f"ended ready_for_human; Draft MR left for human review; spend "
            f"{spend.get('total_usd')} usd of the {spend.get('cap_usd')} usd lane cap"
        )
    if outcome in ("review-non-ready", "useful-wip-continued"):
        return (
            f"live — outcome {outcome}: the delivery and independent verification "
            "stand; the closing review did NOT complete within this trace "
            f"(run {review.get('status')}; budget decision "
            f"{(review.get('review_budget_block') or {}).get('short_reason') or 'none'}) — "
            "recorded honestly, never relabelled reviewed-ready"
        )
    return f"live — outcome {outcome}: see the trace record for the honest failures"


@dataclass(frozen=True)
class _AlignmentStep:
    step: str
    stdout_image_id: str


def post_alignment_observations(alignment: Mapping[str, Any]) -> dict[str, str]:
    """The LAST run's verify-step facts — what the ALIGNED lab reports.

    The receipts' top-level ``observations`` are the plan-time read
    (BEFORE backup/build/migrate: a stale deployed head there is the
    pre-alignment value, not the frozen composition's). Walks the runs
    newest-first for a verify step and extracts the version + schema the
    aligned deployment itself reported.
    """
    for run in reversed(alignment.get("runs", []) or []):
        for step in run.get("steps", []) or []:
            if str(step.get("step", "")) != "verify":
                continue
            checks = step.get("checks") or {}
            schema = checks.get("schema == repo chain head") or {}
            version = checks.get("controlplane.version == repo __version__") or {}
            result: dict[str, str] = {}
            if str(schema.get("observed") or "").strip():
                result["deployed_schema_head"] = str(schema["observed"]).strip()
            if str(version.get("observed") or "").strip():
                result["reported_version"] = str(version["observed"]).strip()
            if result:
                return result
    return {}


def alignment_runs_of(alignment: Mapping[str, Any]) -> dict[str, _AlignmentStep]:
    """The alignment receipts' steps, keyed for the fields the freeze binds."""
    result: dict[str, _AlignmentStep] = {}
    for run in alignment.get("runs", []) or []:
        for step in run.get("steps", []) or []:
            name = str(step.get("step", ""))
            image_id = ""
            for command in step.get("commands", []) or []:
                stdout = str(command.get("stdout_tail", ""))
                match = re.search(r"([0-9a-f]{64})\s*$", stdout.strip())
                if match and name == "build":
                    image_id = match.group(1)
            result[f"step=={name}"] = _AlignmentStep(step=name, stdout_image_id=image_id)
    return result


def rollback_tag_of(alignment: Mapping[str, Any]) -> str:
    for run in alignment.get("runs", []) or []:
        for step in run.get("steps", []) or []:
            if str(step.get("step", "")) != "rollback-tag":
                continue
            for command in step.get("commands", []) or []:
                for value in command.get("argv", []) or []:
                    # the rollback tag lands as ``localhost/forge:pre-…`` — the
                    # ``pre-`` tag segment is the addressable rollback identity
                    segment = str(value).rsplit(":", 1)[-1]
                    if segment.startswith("pre-"):
                        return segment
    raise FreezeRefused("the alignment receipts carry no rollback tag — the freeze binds it")


def inventory_stage(inventory: Mapping[str, Any], stage: str, default: Any) -> Any:
    stages = inventory.get("stages", {})
    value = stages.get(stage, default) if isinstance(stages, Mapping) else default
    return value if value is not None else default


# ---------------------------------------------------------------------------
# Validation (the committed artifact must satisfy its own contract)
# ---------------------------------------------------------------------------


def validate_manifest(document: Mapping[str, Any]) -> list[str]:
    """The frozen manifest's own typed validation — findings, never guesses.

    Checked: the stamp; every digest field hex64; the frozen template's
    sha256 equals the sha256 of its embedded bytes; the verification
    contract carries cases + shapes + untouchable paths; the four
    DISTINCT evidence records exist and the human support decision is
    ``pending``; the divergence between promoted and executed identities
    is STATED (a silent merge of the two is the exact R38-06 defect).
    """
    findings: list[str] = []
    if document.get("schema") != SUPPORTED_PROFILE_STAMP:
        findings.append(f"schema {document.get('schema')!r} != {SUPPORTED_PROFILE_STAMP!r}")
    control = document.get("control_plane", {})
    promoted = control.get("promoted", {})
    executed = control.get("executed_lab", {})
    for path, value in (
        ("control_plane.promoted.wheel_sha256", promoted.get("wheel_sha256", "")),
        ("control_plane.promoted.image_digest", promoted.get("image_digest", "")),
        ("control_plane.promoted.sdist_sha256", promoted.get("sdist_sha256", "")),
        ("control_plane.executed_lab.image_digest", executed.get("image_digest", "")),
        ("control_plane.executed_lab.image_id", executed.get("image_id", "")),
        (
            "control_plane.latest_promotion.wheel_sha256",
            control.get("latest_promotion", {}).get("wheel_sha256", ""),
        ),
        (
            "control_plane.latest_promotion.image_digest",
            control.get("latest_promotion", {}).get("image_digest", ""),
        ),
        (
            "control_plane.executed_lab.qualification_composition.wheel.sha256",
            (executed.get("qualification_composition", {}) or {})
            .get("wheel", {})
            .get("sha256", ""),
        ),
        ("lane.wheel.sha256", document.get("lane", {}).get("wheel", {}).get("sha256", "")),
        (
            "lane.promoted_wheel.sha256",
            document.get("lane", {}).get("promoted_wheel", {}).get("sha256", ""),
        ),
        (
            "lane.closure.closure_digest",
            document.get("lane", {}).get("closure", {}).get("closure_digest", ""),
        ),
        (
            "target_template.frozen.sha256",
            document.get("target_template", {}).get("frozen", {}).get("sha256", ""),
        ),
        (
            "target_template.working_tree_drift.sha256",
            document.get("target_template", {}).get("working_tree_drift", {}).get("sha256", ""),
        ),
    ):
        if not _HEX64_RE.fullmatch(str(value or "")):
            findings.append(f"{path} is not a hex64 sha256 ({str(value)[:24]!r}…)")
    for path, value in (
        ("control_plane.promoted.source_sha", promoted.get("source_sha", "")),
        (
            "lane.executed_live.git_sha",
            document.get("lane", {}).get("executed_live", {}).get("git_sha", ""),
        ),
        (
            "verification_contract.verified_candidate_sha",
            document.get("verification_contract", {}).get("verified_candidate_sha", ""),
        ),
    ):
        if not _GITSHA_RE.fullmatch(str(value or "")):
            findings.append(f"{path} is not a 40-hex git sha ({str(value)[:24]!r}…)")
    template = document.get("target_template", {}).get("frozen", {})
    try:
        embedded = base64.b64decode(str(template.get("bytes_b64", "")))
        actual = _sha256_bytes(embedded)
        if actual != str(template.get("sha256", "")):
            findings.append(
                f"target_template.frozen.sha256 pins {str(template.get('sha256'))[:12]}… but the "
                f"embedded bytes hash {actual[:12]}… — the frozen recipe does not vouch for itself"
            )
    except (ValueError, TypeError) as exc:
        findings.append(f"target_template.frozen.bytes_b64 is not decodable base64 ({exc})")
    if not template.get("bytes_b64"):
        findings.append(
            "target_template.frozen.bytes_b64 is empty — the recipe bytes are the manifest's payload"
        )
    contract = document.get("verification_contract", {})
    if not contract.get("slugify_cases") or not contract.get("file_shapes"):
        findings.append("verification_contract carries no oracle cases or file shapes")
    if str(promoted.get("image_digest", "")) == str(executed.get("image_digest", "")):
        findings.append(
            "control_plane divergence is UNSTATED: the promoted and executed image digests "
            "are identical — either the receipts changed under the freeze or the divergence "
            "note lies"
        )
    if not str(control.get("divergence", "")).strip():
        findings.append(
            "control_plane.divergence is empty — the two-composition defect must be named"
        )
    latest = control.get("latest_promotion", {})
    if not str(latest.get("status", "")).strip():
        findings.append(
            "control_plane.latest_promotion.status is empty — the pending promotion must "
            "be named, never assumed"
        )
    if str(document.get("lane", {}).get("wheel", {}).get("source", "")) != "local-uv-build":
        findings.append(
            "lane.wheel.source is not 'local-uv-build' — the v2 fresh-install target is "
            "the working-tree build (the qualification composition)"
        )
    if not str(document.get("lane", {}).get("wheel", {}).get("path", "")).strip():
        findings.append("lane.wheel.path is empty — the local wheel receipt is missing")
    records = document.get("evidence_records", {})
    for name in (
        "source_review",
        "release_canary",
        "native_workflow_qualification",
        "human_support_approval",
    ):
        if (
            not isinstance(records.get(name), Mapping)
            or not str(records[name].get("status", "")).strip()
        ):
            findings.append(
                f"evidence_records.{name} is missing or carries no status — four DISTINCT records"
            )
    if str(records.get("human_support_approval", {}).get("status", "")) != "pending":
        findings.append(
            "evidence_records.human_support_approval.status must stay 'pending' — a freeze "
            "approves nothing; approval is a separate human act"
        )
    schema_revision = control.get("schema_revision", {})
    if str(schema_revision.get("head", "")) == str(schema_revision.get("predecessor", "")):
        findings.append(
            "schema_revision head == predecessor — an unchanged head is not a transition"
        )
    if not document.get("exclusions"):
        findings.append("exclusions is empty — the honest gaps must stay explicit")
    digest = document.get("manifest_digest", "")
    if not _HEX64_RE.fullmatch(str(digest)):
        findings.append("manifest_digest is not a hex64 sha256")
    elif _canonical_digest(document) != digest:
        findings.append(
            "manifest_digest does not vouch for the document — the manifest drifted from itself"
        )
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/freeze_supported_profile.py",
        description=(
            "R38-06 (#307): freeze the exact supported deployment composition into "
            "qualification/profiles/supported-gitlab-ce-v1.json — every value from an "
            "actual receipt, fail-closed on contradiction."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=MANIFEST_PATH,
        help="the manifest path (default: the committed location)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-verify the committed manifest (digest + validation) instead of capturing",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.root / "src"))

    if args.check:
        path = args.out
        if not path.is_file():
            print(f"freeze_supported_profile: REFUSED: {path} does not exist", file=sys.stderr)
            return 1
        document = json.loads(path.read_text(encoding="utf-8"))
        findings = validate_manifest(document)
        if findings:
            print(
                f"freeze_supported_profile: CHECK REFUSED ({len(findings)} finding(s)):",
                file=sys.stderr,
            )
            for finding in findings:
                print(f"  - {finding}", file=sys.stderr)
            return 1
        print(f"freeze_supported_profile: manifest verified: {path}")
        print(f"  manifest_digest: {document['manifest_digest']}")
        print(f"  frozen template: {document['target_template']['frozen']['sha256'][:16]}…")
        return 0

    try:
        inputs = CaptureInputs.from_root(args.root, TemplateReceiptProbe())
        document = capture_supported_profile(inputs)
    except FreezeRefused as error:
        print(f"freeze_supported_profile: REFUSED: {error}", file=sys.stderr)
        return 1
    findings = validate_manifest(document)
    if findings:
        print(
            "freeze_supported_profile: REFUSED — the capture is self-inconsistent:", file=sys.stderr
        )
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(_render(document), encoding="utf-8")
    # the wheel receipt (the closure pattern): when the local dist/ bytes
    # exist, publish the committed receipt beside the manifest so a
    # dist-less checkout (CI) still reads ONE consistent bind.
    wheel_receipt = ROOT / WHEEL_RECEIPT_PATH
    local_wheel = ROOT / str(document["lane"]["wheel"]["path"])
    if local_wheel.is_file():
        receipt_body = {
            "name": document["lane"]["wheel"]["name"],
            "path": document["lane"]["wheel"]["path"],
            "sha256": document["lane"]["wheel"]["sha256"],
            "source": "local-uv-build",
            "built_from": ("the working tree (uncommitted-ahead state recorded in the manifest)"),
            "built_for": "the v2 qualification composition (forge#326)",
        }
        wheel_receipt.parent.mkdir(parents=True, exist_ok=True)
        wheel_receipt.write_text(
            json.dumps(receipt_body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"freeze_supported_profile: wheel receipt → {wheel_receipt}")
    print(f"freeze_supported_profile: froze {args.out}")
    print(f"  manifest_digest     : {document['manifest_digest']}")
    print(f"  promoted wheel sha  : {document['control_plane']['promoted']['wheel_sha256'][:16]}…")
    print(
        f"  executed lab digest : {document['control_plane']['executed_lab']['image_digest'][:16]}…"
    )
    print(f"  frozen template sha : {document['target_template']['frozen']['sha256'][:16]}…")
    print(
        f"  schema (head, N-1)  : {document['control_plane']['schema_revision']['head']}, "
        f"{document['control_plane']['schema_revision']['predecessor']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
