#!/usr/bin/env python3
"""R37-09 (issue #290) — prove authorized read-many/write-one discovery LIVE.

The authority surface (``forge.adaptive.discovery_authority``, #270) and
the capture driver (``forge.adaptive.research_cohort_live``, #271)
established the boundary machinery and the capture path.  What is
missing is a REAL planner run over a real multi-repo boundary where the
decisive constraint lives only in a NEIGHBOR beyond the first file
window — this script is that run, graded mechanically.

The scenario (frozen in ``evaluation/discovery_live/``):

- three fixture service repositories — the writable target ``orders-api``
  (the task's issue lives here), the authorized decisive neighbor
  ``billing-policy`` (the refund-approval threshold — finance revision
  F-2024-11 — lives ONLY there, at line 200+ of
  ``src/policy/refunds.py``), and the authorized-but-irrelevant
  ``docs-site`` decoy — plus a fourth repository ``payments-core`` that
  is CONFIGURED in the catalog but NOT authorized: every read of it
  must refuse with the typed ``DiscoveryAuthorizationRefusal`` (zero
  content);
- the runner builds the ``AuthorizedRepoSet`` from the manifest, runs
  the lexical orientation pass and the REAL research harness
  (``run_research_pass`` over ``SnapshotToolbox`` per authorized
  repository), then synthesizes the plan — deterministically from what
  the run established (``--scripted``) or BY THE REAL MODEL through the
  lab gateway (``--live``, reusing ``research_cohort_live``'s gateway
  resolution and hard caps: max calls, wall clock, per-call tokens and
  a HARD spend cap of $1);
- the mechanical grader (:func:`grade_discovery_run`) decides: was the
  neighbor's decisive byte window cited (repo/OID/line-range match)?
  was the decoy dragged in?  did the ambiguous part become a question
  rather than an invented default?  does the write scope stay confined
  to the authorized target?  did the unauthorized read refuse, typed,
  with zero content?

Two capture modes, never pooled: ``--scripted`` (the reactive-script
fallback, provenance ``offline-scripted-model``) and ``--live`` (the
gateway, provenance ``live-model``).  A live attempt with an
unreachable/unconfigured gateway is recorded as a REFUSAL with its
reason — live provenance is never fabricated.

The CUSTOMER-SCALE PLANNING PROFILE (R38-11 / #312): ``--profile`` runs
the deterministic offline qualification over ``manifest-customer-v1.json``
— a NINE-repository fixture graph (the writable target, three decisive
neighbors at different depths, one LARGE irrelevant docs repository,
four noise repositories) exercising the machinery of
``forge.adaptive.discovery_profile`` (observation cache with
policy-scope isolation, exhaustion taxonomies with bounded recoveries,
budget-suited synthesis validation, the carry-forward into the
implementation brief).  The live-model run over the same graph is the
partner-gated remainder and is NOT claimed here.

Usage::

    uv run python scripts/run_discovery_live.py --init-manifest --out evaluation/discovery_live/
    uv run python scripts/run_discovery_live.py --scripted --out evaluation/discovery_live/
    uv run python scripts/run_discovery_live.py --live --out evaluation/discovery_live/ \\
        [--gateway-url http://localhost:4000 --gateway-model fast]
    uv run python scripts/run_discovery_live.py --init-customer-manifest --out evaluation/discovery_live/
    uv run python scripts/run_discovery_live.py --profile \\
        --manifest evaluation/discovery_live/manifest-customer-v1.json \\
        --out evaluation/discovery_live/customer-profile-run/
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - direct execution
    sys.path.insert(0, str(REPO_ROOT))

from forge.adaptive.discovery_authority import (  # noqa: E402
    AuthorizedReadSurface,
    AuthorizedRepoSet,
    CatalogRepository,
    ConnectionIdentity,
    DiscoveryAuthorizationRefusal,
    neighbor_set_digest,
    resolve_readers,
    review_write_proposals,
)
from forge.adaptive.discovery_profile import (  # noqa: E402
    ConflictSide,
    InvestigationOutcome,
    ObservationCache,
    PolicyConflict,
    SYNTHESIS_BUDGET_PROFILES,
    attach_observation_cache,
    brief_envelope_seam,
    budget_profile_for,
    cache_from_record,
    carry_forward,
    classify_investigation,
    conflict_question,
    planning_scope,
    render_carry_forward_section,
    review_scope,
    validate_plan_synthesis,
    verify_carry_forward,
)
from forge.adaptive.discovery_stage import extract_keywords  # noqa: E402
from forge.adaptive.discovery_tools import SnapshotToolbox  # noqa: E402
from forge.adaptive.research_cohort import (  # noqa: E402
    PROVENANCE_LIVE_MODEL,
    PROVENANCE_OFFLINE_SCRIPTED,
    CohortSpecError,
    Snapshot,
)
from forge.adaptive.research_cohort_live import (  # noqa: E402
    FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV,
    FORGE_RESEARCH_LIVE_GATEWAY_TIER_ENV,
    FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV,
    REACTIVE_DEEP_READ,
    DeterministicClock,
    LiveGateway,
    ScriptedInvestigation,
    gateway_completion,
    resolve_live_gateway,
)
from forge.adaptive.research_planner import (  # noqa: E402
    OBSERVATION_CONTENT_MAX_CHARS,
    CompletionFn,
    ResearchHarness,
    ResearchRepo,
    ToolObservation,
    _execute_call,
    _first_json_object,
    run_research_pass,
)
from forge.adaptive.system_context import (  # noqa: E402
    NeighborRepository,
    SystemContextProfile,
    WritableTarget,
)

# ---------------------------------------------------------------------------
# The frozen scenario — the single source of truth the manifest records
# ---------------------------------------------------------------------------

MANIFEST_SCHEMA = "forge.discovery.live.manifest/1"
RUN_SCHEMA = "forge.discovery.live.run/1"
REPORT_SCHEMA = "forge.discovery.live.report/1"
OID_SCHEME = "sha1-canonical-fileset/1"

REGISTERED_AT = "2026-09-24T00:00:00+00:00"
TASK_ID = "DL-01-refund-approval-neighbor"

OWN_KEY = "gitlab:orders-api"
BILLING_KEY = "gitlab:billing-policy"
DOCS_KEY = "gitlab:docs-site"
PAYMENTS_KEY = "gitlab:payments-core"

DECISIVE_PATH = "src/policy/refunds.py"
DECISIVE_MARKER = "REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS"
#: the decisive constraint must live BEYOND the first file window.
MIN_DECISIVE_LINE = 200

TASK_STATEMENT = (
    "Support refund requests in Orders' checkout for orders that expired less "
    "than 30 days ago, wiring the request through to Billing's policy client. "
    "Finance said large refunds need an extra approval step before capture, "
    "but the issue does not say from which amount, and it does not say who "
    "performs that approval for orders-api."
)

AMBIGUITY = {
    "note": (
        "the issue leaves the approval step undecided: neither the routing nor "
        "the approver is stated anywhere the planner can read — the expected "
        "behavior is a question, and an assumed default (auto-approval, "
        "approval-skipping) is a failure"
    ),
    "question_terms": (
        "who approves",
        "who performs",
        "approver",
        "approval owner",
        "approvals queue",
        "approval routing",
    ),
    "invented_default_terms": (
        "auto-approve",
        "approve automatically",
        "automatically approved",
        "no approval required",
        "approval is not required",
        "skip the approval",
        "without approval",
    ),
}

#: provider, base_url, connection_id, numeric_id, repository_id per repo.
REPOSITORIES: tuple[tuple[str, str, int, int, str, str], ...] = (
    ("writable", OWN_KEY, "https://gitlab-orders.internal.example", 1, 101, "orders-api"),
    ("neighbor", BILLING_KEY, "https://gitlab-billing.internal.example", 2, 202, "billing-policy"),
    ("decoy", DOCS_KEY, "https://gitlab-docs.internal.example", 3, 303, "docs-site"),
    (
        "unauthorized",
        PAYMENTS_KEY,
        "https://gitlab-payments.internal.example",
        4,
        404,
        "payments-core",
    ),
)

#: the hard caps both capture modes run under (the live spend cap is $1;
#: the per-call token caps carry the reasoning model's thinking budget too
#: — attempts 1 and 2 of the live capture showed 3000/6000 synthesis
#: tokens truncating the JSON mid-emission, so the caps were re-registered
#: at 8000 with a leaner plan-synthesis prompt).
CAPS: dict[str, Any] = {
    "max_calls": 12,
    "wall_seconds": 300.0,
    "max_tokens_per_call": 3000,
    "plan_max_tokens": 8000,
    "max_usd": 1.0,
}

#: Conservative OVER-estimates used ONLY to enforce the spend cap; the
#: receipts record actual usage and the run names the assumption.
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "fast": {"input": 2.0, "output": 8.0},
    "openai/glm-5.3-flash": {"input": 2.0, "output": 8.0},
}
PRICE_NOTE = (
    "conservative over-estimate used only to enforce the hard spend cap; "
    "actual vendor pricing is not recorded here — the receipts carry the "
    "usage the gateway reported"
)

ROLE_WRITABLE = "writable"
ROLE_NEIGHBOR = "neighbor"
ROLE_DECOY = "decoy"
ROLE_UNAUTHORIZED = "unauthorized"

#: The reactive offline investigation (the RC-08 recipe): a lexical-style
#: orientation turn over BOTH the target and the neighbor, a REACTIVE deep
#: read that pages around the line the grep observation actually reported,
#: then an honest done turn whose summary carries the question.
SCRIPTED_TURNS: tuple[Mapping[str, Any] | str, ...] = (
    {
        "calls": [
            {"tool": "grep", "repo": OWN_KEY, "args": {"pattern": "request_refund"}},
            {"tool": "grep", "repo": BILLING_KEY, "args": {"pattern": "MANUAL_APPROVAL"}},
        ]
    },
    REACTIVE_DEEP_READ,
    {
        "done": True,
        "summary": (
            "Orders' checkout gates refund requests by age only (src/checkout.py) "
            "and routes money decisions to Billing's policy client; the approval "
            "rules are NOT decided in Orders. The CURRENT approval policy is "
            "Billing's revision F-2024-11 in src/policy/refunds.py: refunds at or "
            "above 5000 cents ($50.00) REQUIRE a manual approval step before "
            "capture and automated approval at or above the threshold is "
            "forbidden; the F-2023 and F-2024-02 thresholds earlier in the file "
            "are superseded history. The issue still leaves undecided who "
            "performs the approval step for orders-api — which approvals queue "
            "or owner handles refunds above the threshold?"
        ),
        "assumptions": [],
        "contradictions": [],
    },
)
SCRIPTED_DEEP = (BILLING_KEY, DECISIVE_PATH, DECISIVE_MARKER)
SCRIPTED_INPUT_TOKENS = (2100, 2600, 1500)
SCRIPTED_OUTPUT_TOKENS = (180, 60, 320)

_MODE_SCRIPTED = "scripted"
_MODE_LIVE = "live"


# ---------------------------------------------------------------------------
# Canonical JSON / digests
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _oid(files: Mapping[str, str]) -> str:
    """The frozen content identity of one repository's file set."""
    return hashlib.sha1(_canonical(dict(files)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The manifest — the frozen system boundary the run authorizes against
# ---------------------------------------------------------------------------


def _load_fixture_files(fixtures_root: Path, fixture: str) -> dict[str, str]:
    root = fixtures_root / fixture
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return files


def _decisive_line(files: Mapping[str, str]) -> int:
    lines = (files.get(DECISIVE_PATH) or "").splitlines()
    for number, text in enumerate(lines, start=1):
        if DECISIVE_MARKER in text and "=" in text:
            return number
    raise SystemExit(f"the decisive marker {DECISIVE_MARKER!r} is not in {DECISIVE_PATH}")


def build_manifest_document(fixtures_root: Path) -> dict[str, Any]:
    """Derive the manifest from the scenario constants + fixture bytes."""
    repos: list[dict[str, Any]] = []
    for role, key, base_url, connection_id, numeric_id, repository_id in REPOSITORIES:
        fixture = f"fixtures/{repository_id}"
        files = _load_fixture_files(fixtures_root, fixture)
        if not files:
            raise SystemExit(f"fixture {fixtures_root / fixture} is empty")
        repos.append(
            {
                "key": key,
                "role": role,
                "connection": {
                    "provider": "gitlab",
                    "base_url": base_url,
                    "connection_id": connection_id,
                },
                "numeric_id": numeric_id,
                "repository_id": repository_id,
                "source_oid": _oid(files),
                "fixture": fixture,
                "allowed_globs": ["**"],
            }
        )
    billing = next(entry for entry in repos if entry["key"] == BILLING_KEY)
    billing_files = _load_fixture_files(fixtures_root, billing["fixture"])
    marker_line = _decisive_line(billing_files)
    if marker_line < MIN_DECISIVE_LINE:
        raise SystemExit(
            f"the decisive marker sits at line {marker_line} — the scenario requires a "
            f"non-initial window (>= line {MIN_DECISIVE_LINE})"
        )
    document = {
        "schema": MANIFEST_SCHEMA,
        "registered_at": REGISTERED_AT,
        "issue": "R37-09 / #290",
        "task": {
            "task_id": TASK_ID,
            "statement": TASK_STATEMENT,
            "decisive": {
                "repo_key": BILLING_KEY,
                "path": DECISIVE_PATH,
                "marker": DECISIVE_MARKER,
                "min_line": MIN_DECISIVE_LINE,
                "marker_line": marker_line,
                "window": {"start": marker_line - 8, "end": marker_line + 14},
                "note": (
                    "the refund manual-approval threshold (finance revision "
                    "F-2024-11) exists ONLY in the authorized neighbor, beyond "
                    "the first file window; the file's earlier F-2023/F-2024-02 "
                    "values are superseded decoys-within-the-file"
                ),
            },
            "ambiguity": {
                **AMBIGUITY,
                "question_terms": list(AMBIGUITY["question_terms"]),
                "invented_default_terms": list(AMBIGUITY["invented_default_terms"]),
            },
        },
        "repos": repos,
        "caps": dict(CAPS),
        "prices_usd_per_mtok": {k: dict(v) for k, v in PRICES_USD_PER_MTOK.items()},
        "price_note": PRICE_NOTE,
        "oid_scheme": OID_SCHEME,
    }
    validate_manifest_document(document, fixtures_root)
    return document


def manifest_digest(document: Mapping[str, Any]) -> str:
    return _sha256(_canonical(dict(document)))


def validate_manifest_document(document: Mapping[str, Any], fixtures_root: Path) -> None:
    """The manifest contract — refuses any drift from the frozen scenario."""
    if document.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"manifest schema {document.get('schema')!r} is not {MANIFEST_SCHEMA!r}")
    repos = document.get("repos")
    if not isinstance(repos, list) or not repos:
        raise ValueError("the manifest needs a non-empty repos list")
    roles = [str(entry.get("role") or "") for entry in repos]
    if roles.count(ROLE_WRITABLE) != 1:
        raise ValueError("exactly one repository may be the writable target")
    if ROLE_NEIGHBOR not in roles:
        raise ValueError("no authorized decisive neighbor is declared")
    if ROLE_DECOY not in roles:
        raise ValueError("no authorized-but-irrelevant decoy is declared")
    if ROLE_UNAUTHORIZED not in roles:
        raise ValueError("no configured-but-unauthorized repository is declared (refusal arm)")
    keys = [str(entry.get("key") or "") for entry in repos]
    if len(set(keys)) != len(keys):
        raise ValueError("repository keys must be unique")

    task = document.get("task") if isinstance(document.get("task"), Mapping) else {}
    statement = str(task.get("statement") or "")
    if len(statement) < 40:
        raise ValueError("the task statement is missing")
    decisive = task.get("decisive") if isinstance(task.get("decisive"), Mapping) else {}
    ambiguity = task.get("ambiguity") if isinstance(task.get("ambiguity"), Mapping) else {}
    if not ambiguity.get("question_terms") or not ambiguity.get("invented_default_terms"):
        raise ValueError("the ambiguity needs both question and invented-default terms")
    if str(decisive.get("marker") or "") in statement:
        raise ValueError("the task statement must NOT leak the decisive marker")

    oid_scheme = str(document.get("oid_scheme") or "")
    decisive_repo: Mapping[str, Any] | None = None
    for entry in repos:
        key = str(entry.get("key") or "")
        files = _load_fixture_files(fixtures_root, str(entry.get("fixture") or ""))
        connection = entry.get("connection") if isinstance(entry.get("connection"), Mapping) else {}
        ConnectionIdentity(
            provider=str(connection.get("provider") or ""),
            base_url=str(connection.get("base_url") or ""),
            connection_id=int(connection.get("connection_id") or 0),
        )
        if oid_scheme == OID_SCHEME:
            derived = _oid(files)
            recorded = str(entry.get("source_oid") or "")
            if derived != recorded:
                raise ValueError(
                    f"{key}: the fixture re-derives OID {derived[:12]}… but the manifest "
                    f"recorded {recorded[:12]}… — the frozen snapshot drifted; re-register "
                    "the manifest (--init-manifest --force) as a NEW record"
                )
        if str(entry.get("role")) != ROLE_UNAUTHORIZED and key == str(decisive.get("repo_key")):
            decisive_repo = entry
    if decisive_repo is None:
        raise ValueError(
            f"the decisive repo key {decisive.get('repo_key')!r} is not an authorized repository"
        )
    decisive_files = _load_fixture_files(fixtures_root, str(decisive_repo.get("fixture") or ""))
    lines = (decisive_files.get(str(decisive.get("path") or "")) or "").splitlines()
    marker = str(decisive.get("marker") or "")
    hits = [number for number, text in enumerate(lines, start=1) if marker in text]
    if not hits:
        raise ValueError("the decisive marker is absent from the decisive repository")
    marker_line = next(number for number in hits if "=" in lines[number - 1])
    if marker_line < int(decisive.get("min_line") or 0):
        raise ValueError(
            f"the decisive marker sits at line {marker_line}, before the required "
            f"non-initial window (line >= {decisive.get('min_line')})"
        )
    # The decisive business rule lives ONLY in the authorized neighbor.
    for entry in repos:
        if entry is decisive_repo:
            continue
        other = _load_fixture_files(fixtures_root, str(entry.get("fixture") or ""))
        if any(marker in text for text in other.values()):
            raise ValueError(
                f"the decisive marker also appears in {entry.get('key')} — the "
                "constraint must live ONLY in the authorized neighbor"
            )
    caps = document.get("caps") if isinstance(document.get("caps"), Mapping) else {}
    if int(caps.get("max_calls") or 0) < 1 or float(caps.get("wall_seconds") or 0) < 1.0:
        raise ValueError("the caps must bound calls and wall clock")
    if not 0 < float(caps.get("max_usd") or 0) <= 1.0:
        raise ValueError("the spend cap must be in (0, 1.0] USD")


def load_manifest(out_dir: Path) -> dict[str, Any]:
    document = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    validate_manifest_document(document, out_dir)
    return document


# ---------------------------------------------------------------------------
# The authority construction — profile, catalog, authorized read set
# ---------------------------------------------------------------------------


class FixtureReader:
    """The async duck-typed reader over one fixture repository."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self._files = dict(files)
        self.reads = 0

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> dict[str, Any]:
        return {"paths": sorted(self._files), "truncated": False, "complete": True}

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        self.reads += 1
        return self._files[file_path]


@dataclass(frozen=True)
class DiscoveryBoundary:
    """The frozen authority the run reads through."""

    profile: SystemContextProfile
    authorized: AuthorizedRepoSet
    files: Mapping[str, Mapping[str, str]]
    repos_document: tuple[dict[str, Any], ...]
    catalog_document: tuple[dict[str, str], ...]
    resolved_identities: tuple[dict[str, str], ...]


def build_boundary(document: Mapping[str, Any], fixtures_root: Path) -> DiscoveryBoundary:
    """Build the read-many/write-one boundary from the manifest.

    The profile authorizes the writable target plus the neighbors
    (decisive + decoy); the CONFIGURED-but-unauthorized repository is in
    the catalog yet absent from the profile, so no reader can be built
    for it and every read refuses with the typed refusal.
    """
    repos = document["repos"]
    files = {
        str(entry["key"]): _load_fixture_files(fixtures_root, str(entry["fixture"]))
        for entry in repos
    }
    writable_entry = next(entry for entry in repos if entry["role"] == ROLE_WRITABLE)
    neighbor_entries = [entry for entry in repos if entry["role"] in (ROLE_NEIGHBOR, ROLE_DECOY)]
    profile = SystemContextProfile(
        writable=WritableTarget(
            provider="gitlab",
            repository_id=str(writable_entry["repository_id"]),
            ref=str(writable_entry["source_oid"]),
            allowed_globs=("**",),
        ),
        neighbors=tuple(
            NeighborRepository(
                provider="gitlab",
                repository_id=str(entry["repository_id"]),
                ref=str(entry["source_oid"]),
                allowed_globs=("**",),
            )
            for entry in neighbor_entries
        ),
    )
    catalog = tuple(
        CatalogRepository(
            connection=ConnectionIdentity(
                provider=str(entry["connection"]["provider"]),
                base_url=str(entry["connection"]["base_url"]),
                connection_id=int(entry["connection"]["connection_id"]),
            ),
            numeric_id=int(entry["numeric_id"]),
            display_name=str(entry["repository_id"]),
            default_ref=str(entry["source_oid"]),
        )
        for entry in repos
    )
    resolution = resolve_readers(
        profile, repositories=catalog, reader_factory=lambda repo: FixtureReader({})
    )
    if not resolution.ok:
        raise ValueError(
            f"reader resolution refused: {[r.as_blocking_question() for r in resolution.refusals]}"
        )
    unauthorized = {str(entry["key"]) for entry in repos if entry["role"] == ROLE_UNAUTHORIZED}
    leaking = unauthorized & set(resolution.readers)
    if leaking:
        raise ValueError(f"readers were built for UNAUTHORIZED repositories {sorted(leaking)}")
    return DiscoveryBoundary(
        profile=profile,
        authorized=AuthorizedRepoSet(profile),
        files=files,
        repos_document=tuple(dict(entry) for entry in repos),
        catalog_document=tuple(
            {"key": str(e["key"]), "identity": c.identity} for e, c in zip(repos, catalog)
        ),
        resolved_identities=tuple(
            {"neighbor_key": binding.neighbor_key, "identity": binding.identity}
            for binding in resolution.bindings
        ),
    )


# ---------------------------------------------------------------------------
# The spend cap — a HARD dollar bound over the usage receipts
# ---------------------------------------------------------------------------


class SpendCapReached(Exception):
    """Raised BEFORE a model call when its worst-case projection would
    exceed the hard spend cap — the loop records it as an honest stop."""


@dataclass
class SpendCap:
    """The hard USD bound over the receipts the gateway reports.

    An unknown usage receipt is charged at a conservative worst case —
    never zero: the cap may over-stop, it may never under-stop.
    """

    model: str
    limit_usd: float
    prices: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    worst_case_input_tokens: int = 20_000
    spent_usd: float = 0.0
    receipts: list[dict[str, Any]] = field(default_factory=list)

    def _price(self) -> tuple[float, float]:
        entry = self.prices.get(self.model) or next(iter(self.prices.values()))
        return float(entry["input"]), float(entry["output"])

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self._price()
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000

    def allows_call(self, max_tokens: int) -> bool:
        projected = self.estimate(self.worst_case_input_tokens, int(max_tokens))
        return self.spent_usd + projected <= self.limit_usd

    def charge(
        self, *, input_tokens: int | None, output_tokens: int | None, max_tokens: int
    ) -> dict[str, Any]:
        known = input_tokens is not None and output_tokens is not None
        charged_in = int(input_tokens or 0) if known else self.worst_case_input_tokens
        charged_out = int(output_tokens or 0) if known else int(max_tokens)
        usd = self.estimate(charged_in, charged_out)
        self.spent_usd = round(self.spent_usd + usd, 6)
        receipt = {
            "input_tokens": int(input_tokens) if input_tokens is not None else None,
            "output_tokens": int(output_tokens) if output_tokens is not None else None,
            "charged_input_tokens": charged_in,
            "charged_output_tokens": charged_out,
            "usage_known": known,
            "usd_estimated": round(usd, 6),
            "running_usd_estimated": self.spent_usd,
        }
        self.receipts.append(receipt)
        return receipt

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd


def capped_completion(
    inner: CompletionFn, cap: SpendCap, *, max_tokens: int, purpose: str
) -> CompletionFn:
    """Wrap a completion seam with the HARD spend cap.

    The check runs BEFORE the provider is contacted (a projected
    worst-case call that would cross the cap refuses to start) and the
    charge lands AFTER, from the usage the gateway reported — unknown
    usage is charged at the worst case, never zero.
    """

    async def _complete(system: str, user: str) -> Any:
        if not cap.allows_call(max_tokens):
            raise SpendCapReached(
                f"the {purpose} call's worst-case projection would exceed the hard "
                f"spend cap (${cap.limit_usd:.2f}, already spent ${cap.spent_usd:.4f})"
            )
        result = await inner(system, user)
        in_tok = getattr(result, "input_tokens", None)
        out_tok = getattr(result, "output_tokens", None)
        cap.charge(input_tokens=in_tok, output_tokens=out_tok, max_tokens=max_tokens)
        return result

    return _complete


# ---------------------------------------------------------------------------
# The capture — the real research loop over the authorized snapshot
# ---------------------------------------------------------------------------


def _research_repos(boundary: DiscoveryBoundary) -> dict[str, ResearchRepo]:
    return {
        key: ResearchRepo(
            repo_key=key,
            repository_id=str(
                next(e["repository_id"] for e in boundary.repos_document if e["key"] == key)
            ),
            source_oid=str(
                next(e["source_oid"] for e in boundary.repos_document if e["key"] == key)
            ),
            toolbox=SnapshotToolbox(dict(files)),
        )
        for key, files in boundary.files.items()
        if _role_of(boundary, key) != ROLE_UNAUTHORIZED
    }


def _role_of(boundary: DiscoveryBoundary, key: str) -> str:
    return str(next(entry["role"] for entry in boundary.repos_document if entry["key"] == key))


def _lexical_orientation(statement: str, repos: Mapping[str, ResearchRepo]) -> list[dict[str, Any]]:
    """The deterministic lexical probes over the authorized repos."""
    hits: list[dict[str, Any]] = []
    for keyword in extract_keywords(statement):
        for repo in repos.values():
            for symbol in (repo.toolbox.find_symbol(keyword).get("symbols") or [])[:2]:
                hits.append(
                    {
                        "repo_key": repo.repo_key,
                        "path": str(symbol.get("path") or ""),
                        "line": int(symbol.get("line_no") or 0),
                        "kind": "symbol",
                        "detail": str(symbol.get("symbol") or keyword),
                    }
                )
            for reference in (repo.toolbox.find_references(keyword).get("references") or [])[:2]:
                hits.append(
                    {
                        "repo_key": repo.repo_key,
                        "path": str(reference.get("path") or ""),
                        "line": int(reference.get("line_no") or 0),
                        "kind": "reference",
                        "detail": keyword,
                    }
                )
    return hits[:8]


def _recording_complete(inner: CompletionFn) -> tuple[CompletionFn, list[dict[str, Any]]]:
    """Record every tool call a completion proposed (the RC-08 recipe)."""
    proposals: list[dict[str, Any]] = []

    async def _wrapped(system: str, user: str) -> Any:
        result = await inner(system, user)
        parsed = _first_json_object(str(getattr(result, "text", result)))
        if parsed is not None:
            for call in parsed.get("calls") or []:
                if isinstance(call, dict):
                    proposals.append(dict(call))
        return result

    return _wrapped, proposals


def _execute_gated(
    call: Mapping[str, Any],
    repos: Mapping[str, ResearchRepo],
    authorized: AuthorizedRepoSet,
    refusals: list[dict[str, Any]],
) -> ToolObservation:
    """Re-execute one proposed call THROUGH the authority gate.

    An authorized key executes over the frozen toolbox; a key outside
    the approved set NEVER reaches a reader — the typed
    ``DiscoveryAuthorizationRefusal`` becomes the observation (zero
    content) and rides the refusal record.  The writable target passes
    the gate under its ``own`` namespace (the discovery-stage
    convention :data:`~forge.adaptive.system_context.OWN_REPO_KEY`).
    """
    repo_key = str(call.get("repo") or call.get("repository") or "")
    gate_key = "own" if repo_key == authorized.profile.writable.key else repo_key
    tool = str(call.get("tool") or "")
    args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
    header = json.dumps(dict(args))[:160]
    try:
        authorized.authorize_read(gate_key)
    except DiscoveryAuthorizationRefusal as refusal:
        refusals.append(
            {
                "requested": refusal.requested,
                "code": refusal.code,
                "authorized": list(refusal.authorized),
                "content_bytes": 0,
                "source": "model_proposal",
            }
        )
        return ToolObservation(
            tool=tool or "?",
            repo_key=repo_key,
            call=header,
            content="",
            error=f"authority refusal ({refusal.code}): the repository is outside the authorized set",
        )
    return _execute_call(call, repos)[1]


def _surface_key(boundary: DiscoveryBoundary, repo_key: str) -> str:
    """The read-surface key: the writable target occupies ``own``."""
    writable_key = boundary.profile.writable.key
    return "own" if repo_key == writable_key else repo_key


async def _authority_probe(boundary: DiscoveryBoundary) -> dict[str, Any]:
    """The authored refusal assertion over the REAL guarded read surface.

    One read naming the configured-but-UNAUTHORIZED repository must
    raise the typed refusal BEFORE any underlying reader runs — the
    per-reader counters prove the negative (zero reads).
    """
    readers = {
        _surface_key(boundary, key): FixtureReader(files)
        for key, files in boundary.files.items()
        if key in boundary.authorized.read_keys or key == boundary.profile.writable.key
    }
    surface = AuthorizedReadSurface(boundary.profile, readers)
    decisive = next(entry for entry in boundary.repos_document if entry["role"] == ROLE_NEIGHBOR)
    unauthorized_key = next(
        str(entry["key"]) for entry in boundary.repos_document if entry["role"] == ROLE_UNAUTHORIZED
    )
    probe: dict[str, Any] = {
        "requested": unauthorized_key,
        "path": "src/gateway/charges.py",
        "expected": "typed DiscoveryAuthorizationRefusal, zero content",
    }
    try:
        await surface.read_text(unauthorized_key, str(probe["path"]))
    except DiscoveryAuthorizationRefusal as refusal:
        probe.update(
            {
                "refused": True,
                "code": refusal.code,
                "content_bytes": 0,
                "readers_touched": {key: reader.reads for key, reader in readers.items()},
            }
        )
    else:  # pragma: no cover - the boundary must refuse
        probe.update({"refused": False, "code": "", "content_bytes": None})
    # the authorized control: the SAME surface reads the decisive neighbor
    authorized_read = await surface.read_text(str(decisive["key"]), "src/policy/refunds.py")
    probe["authorized_control"] = {
        "requested": str(decisive["key"]),
        "read_bytes": len(authorized_read),
        "refused": False,
    }
    return {
        "authorized_repo_set_digest": boundary.authorized.digest,
        "read_keys": list(boundary.authorized.read_keys),
        "requests": list(surface.requests),
        "probe": probe,
    }


# ---------------------------------------------------------------------------
# Plan synthesis — deterministic (scripted) and model-driven (live)
# ---------------------------------------------------------------------------


def _questions_of(summary: str) -> list[str]:
    """Interrogative sentences lifted from the run's own summary."""
    questions: list[str] = []
    for match in re.finditer(r"[^.?!\n]*\?", summary or ""):
        text = " ".join(match.group(0).split())
        if len(text) >= 12:
            questions.append(text)
    return questions[:4]


def _window_text(
    files: Mapping[str, Mapping[str, str]], repo: str, path: str, start: int, end: int
) -> str:
    lines = (files.get(repo, {}).get(path) or "").splitlines()
    return "\n".join(lines[max(0, start - 1) : max(0, end)])


def synthesize_scripted_plan(
    document: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    files: Mapping[str, Mapping[str, str]],
    *,
    writable_key: str,
    decisive: Mapping[str, Any],
) -> dict[str, Any]:
    """The deterministic plan derived from what the run ACTUALLY read.

    Claims cite the findings' anchors with the asserted content read out
    of the frozen bytes — a synthesized claim can never assert bytes the
    tools did not return; questions come from the run's own summary.
    """
    claims: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        repo = str(finding.get("repo_key") or "")
        path = str(finding.get("path") or "")
        line = int(finding.get("line") or 0)
        if not repo or not path or line < 1 or (repo, path) in seen:
            continue
        decisive_hit = repo == str(decisive.get("repo_key")) and path == str(decisive.get("path"))
        start, end = (line - 2, line + 2) if not decisive_hit else (line - 3, line + 8)
        content = _window_text(files, repo, path, start, end)
        if not content:
            continue
        seen.add((repo, path))
        claims.append(
            {
                "claim_id": f"c{len(claims) + 1}",
                "text": content.strip().splitlines()[0][:240],
                "repo": repo,
                "path": path,
                "line_start": max(1, start),
                "line_end": max(1, start) + len(content.splitlines()) - 1,
                "asserted_content": content,
                "decisive": decisive_hit,
            }
        )
        steps.append(
            {
                "step_id": f"s{len(steps) + 1}",
                "objective": f"address {repo}/{path}",
                "repo": repo,
                "evidence_ids": [f"c{len(claims)}"],
            }
        )
    return {
        "steps": steps,
        "claims": claims,
        "questions": _questions_of(str(document.get("summary") or "")),
        "assumptions": [str(text) for text in document.get("assumptions") or []],
        "write_targets": [writable_key],
        "synthesis": "deterministic-from-captured-run/v1",
    }


_PLAN_SYSTEM_PROMPT = (
    "You are the planning stage of a bounded change pipeline over FROZEN "
    "repository snapshots. Keep the plan MINIMAL (at most 3 steps, at most 3 "
    "claims) and spend no effort on prose. Respond with ONLY a JSON object of "
    'shape {"steps": [{"step_id": str, "objective": str, "repo": str}], '
    '"claims": [{"claim_id": str, "text": str, "repo": str, "path": str, '
    '"line_start": int, "line_end": int, "asserted_content": str}], '
    '"questions": [str], "assumptions": [str], "write_targets": [str]}. '
    "Rules: cite only bytes the tool observations actually returned (the "
    "exact repo key, path and the LINE NUMBERS shown beside the quoted "
    "bytes; asserted_content must quote those bytes verbatim); a rule you "
    "did not observe must become a question, never a claim or an assumed "
    "default; write_targets may name ONLY the writable target — a needed "
    "change in a neighbor is a question or assumption, never a step or "
    "write target."
)


def _numbered_observation_block(
    document: Mapping[str, Any],
    observations: Sequence[Any],
    files: Mapping[str, Mapping[str, str]],
) -> str:
    """The observations rendered with LINE NUMBERS beside the bytes.

    The research loop shows byte offsets; the planner must cite LINE
    windows.  This render annotates each read observation with the line
    numbers its byte offset corresponds to (re-derived from the same
    frozen snapshot) so a citation can quote exactly what was seen.
    """
    blocks: list[str] = []
    for observation in observations[-6:]:
        repo = str(getattr(observation, "repo_key", "") or "")
        call = str(getattr(observation, "call", "") or "")
        content = str(getattr(observation, "content", "") or "")
        if getattr(observation, "error", ""):
            continue
        match = re.match(r"(?P<path>\S+) offset (?P<offset>\d+)(?: length (?P<length>\d+))?", call)
        lines: list[str] = []
        header = f"[{repo} {call}]"
        if match and content:
            path = match.group("path")
            offset = int(match.group("offset"))
            before = (files.get(repo, {}).get(path) or "")[:offset]
            start_line = before.count("\n") + 1
            for index, text in enumerate(content.splitlines()):
                lines.append(f"{start_line + index:5d}| {text}")
            header = f"[{repo} {path} lines {start_line}..{start_line + max(0, len(lines) - 1)}]"
        elif content:
            lines = [text for text in content.splitlines()][:12]
        body = "\n".join(lines)[:4000]
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks) if blocks else "(no observations)"


def _render_plan_prompt(
    task: Mapping[str, Any],
    document: Mapping[str, Any],
    repos: Mapping[str, ResearchRepo],
    observations: Sequence[Mapping[str, Any]],
    files: Mapping[str, Mapping[str, str]],
) -> str:
    menu = json.dumps(
        {
            key: {"repository_id": repo.repository_id, "source_oid": repo.source_oid}
            for key, repo in sorted(repos.items())
        },
        sort_keys=True,
    )
    no_summary = "(the budget stopped the investigation before a summary — the observations are what was read)"
    return (
        f"ISSUE:\n{task['statement']}\n\n"
        f"REPOSITORIES (cite claims with these repo keys):\n{menu}\n\n"
        f"RESEARCH SUMMARY:\n{document.get('summary') or no_summary}\n\n"
        f"TOOL OBSERVATIONS (the actual bytes read, with line numbers):\n"
        f"{_numbered_observation_block(document, observations, files)}\n\n"
        "Emit the plan JSON now."
    )


def _validated_model_plan(parsed: Mapping[str, Any]) -> dict[str, Any] | None:
    """The minimal structural gate a model plan must pass to be graded."""
    for key in ("steps", "claims", "questions", "assumptions", "write_targets"):
        if not isinstance(parsed.get(key), list):
            return None
    for claim in parsed["claims"]:
        if not isinstance(claim, Mapping):
            return None
        if not claim.get("repo") or not claim.get("path"):
            return None
        try:
            int(claim.get("line_start"))
            int(claim.get("line_end"))
        except (TypeError, ValueError):
            return None
    if not all(isinstance(entry, str) for entry in parsed["write_targets"]):
        return None
    return {
        "steps": [
            dict(step) if isinstance(step, Mapping) else {"objective": str(step)}
            for step in parsed["steps"]
        ],
        "claims": [dict(claim) for claim in parsed["claims"]],
        "questions": [str(q) for q in parsed["questions"]],
        "assumptions": [str(a) for a in parsed["assumptions"]],
        "write_targets": [str(t) for t in parsed["write_targets"]],
        "synthesis": "live-model/v1",
    }


# ---------------------------------------------------------------------------
# The mechanical grader — pure over (run, manifest, frozen bytes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryGrade:
    """The mechanical verdict over one discovery run."""

    plan_present: bool
    decisive_constraint_found: bool
    decisive_evidence: tuple[Mapping[str, Any], ...] = ()
    citations_reproduce: bool = True
    citation_mismatches: tuple[str, ...] = ()
    decoy_excluded: bool = True
    decoy_references: tuple[Mapping[str, Any], ...] = ()
    question_raised: bool = False
    invented_default: bool = False
    invented_default_evidence: tuple[str, ...] = ()
    write_scope_untouched: bool = True
    publication_targets: tuple[str, ...] = ()
    expansion_requests: tuple[Mapping[str, Any], ...] = ()
    authority_refusal_typed: bool = False
    authority_refusal_code: str = ""

    @property
    def passed(self) -> bool:
        return (
            self.plan_present
            and self.decisive_constraint_found
            and self.citations_reproduce
            and self.decoy_excluded
            and self.question_raised
            and not self.invented_default
            and self.write_scope_untouched
            and self.authority_refusal_typed
        )

    @property
    def failed_checks(self) -> tuple[str, ...]:
        checks = {
            "plan_present": self.plan_present,
            "decisive_constraint_found": self.decisive_constraint_found,
            "citations_reproduce": self.citations_reproduce,
            "decoy_excluded": self.decoy_excluded,
            "question_raised": self.question_raised,
            "no_invented_default": not self.invented_default,
            "write_scope_untouched": self.write_scope_untouched,
            "authority_refusal_typed": self.authority_refusal_typed,
        }
        return tuple(name for name, ok in checks.items() if not ok)

    def as_document(self) -> dict[str, Any]:
        return {
            "plan_present": self.plan_present,
            "decisive_constraint_found": self.decisive_constraint_found,
            "decisive_evidence": [dict(entry) for entry in self.decisive_evidence],
            "citations_reproduce": self.citations_reproduce,
            "citation_mismatches": list(self.citation_mismatches),
            "decoy_excluded": self.decoy_excluded,
            "decoy_references": [dict(entry) for entry in self.decoy_references],
            "question_raised": self.question_raised,
            "invented_default": self.invented_default,
            "invented_default_evidence": list(self.invented_default_evidence),
            "write_scope_untouched": self.write_scope_untouched,
            "publication_targets": list(self.publication_targets),
            "expansion_requests": [dict(entry) for entry in self.expansion_requests],
            "authority_refusal_typed": self.authority_refusal_typed,
            "authority_refusal_code": self.authority_refusal_code,
            "passed": self.passed,
            "failed_checks": list(self.failed_checks),
        }


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(str(term).lower() in lowered for term in terms)


def grade_discovery_run(
    run: Mapping[str, Any],
    manifest: Mapping[str, Any],
    files: Mapping[str, Mapping[str, str]],
) -> DiscoveryGrade:
    """Grade one captured run MECHANICALLY against the frozen manifest.

    *files* is the frozen snapshot bytes (``repo_key -> path -> content``)
    the run authorized — the grader is pure over its three inputs.  The
    arms: decisive-constraint found (the neighbor's decisive byte window
    cited with its marker bytes), citations reproduce (repo/OID binding
    plus exact byte-range match, non-initial windows included), decoy
    excluded, ambiguity → question (not an invented default), write
    scope confined to the authorized target, and the unauthorized read
    refused typed with zero content.
    """
    task = manifest["task"]
    decisive = task["decisive"]
    ambiguity = task["ambiguity"]
    repos = {str(entry["key"]): entry for entry in manifest["repos"]}
    run_repos = {str(entry["key"]): entry for entry in run.get("repos") or []}
    decoy_keys = {key for key, entry in repos.items() if entry["role"] == ROLE_DECOY}
    writable_key = next(key for key, entry in repos.items() if entry["role"] == ROLE_WRITABLE)

    # OID binding: every authorized repo the run carried must reproduce the
    # manifest's frozen source OID (a moved snapshot invalidates the citations).
    mismatches: list[str] = []
    for key, entry in run_repos.items():
        recorded = repos.get(key)
        if recorded is None:
            mismatches.append(f"{key}: repo outside the manifest")
        elif str(entry.get("source_oid")) != str(recorded["source_oid"]):
            mismatches.append(
                f"{key}: run bound OID {entry.get('source_oid')} but the manifest froze {recorded['source_oid']}"
            )

    plan = run.get("plan") if isinstance(run.get("plan"), Mapping) else None
    claims = [c for c in (plan or {}).get("claims") or [] if isinstance(c, Mapping)]
    steps = [s for s in (plan or {}).get("steps") or [] if isinstance(s, Mapping)]

    decisive_hits: list[dict[str, Any]] = []
    decoy_refs: list[dict[str, Any]] = []
    for index, claim in enumerate(claims, start=1):
        repo = str(claim.get("repo") or "")
        path = str(claim.get("path") or "")
        try:
            start = int(claim.get("line_start"))
            end = int(claim.get("line_end"))
        except (TypeError, ValueError):
            mismatches.append(f"claim {index}: non-integer line window")
            continue
        if repo in decoy_keys:
            decoy_refs.append({"where": "claim", "repo": repo, "path": path})
        repo_files = files.get(repo)
        if repo_files is None:
            mismatches.append(f"claim {index}: repo {repo!r} is outside the authorized snapshot")
            continue
        content_lines = (repo_files.get(path) or "").splitlines()
        if not content_lines:
            mismatches.append(f"claim {index}: path {path!r} is not in {repo}'s frozen snapshot")
            continue
        if not (1 <= start <= end <= len(content_lines)):
            mismatches.append(
                f"claim {index}: window {start}..{end} is outside {repo}/{path} "
                f"(1..{len(content_lines)})"
            )
            continue
        actual = "\n".join(content_lines[start - 1 : end])
        asserted = str(claim.get("asserted_content") or "")
        if asserted.strip():
            # Byte reproduction: the asserted content must be verbatim INSIDE
            # the cited window — equal to it, or a CONTIGUOUS SUB-SPAN of it
            # (quoting the operative lines of the window).  Bytes outside the
            # window can never appear: an invented value fails the substring.
            if asserted.strip() not in actual and asserted.strip() != actual.strip():
                mismatches.append(
                    f"claim {index}: asserted bytes differ from {repo}/{path}:{start}..{end}"
                )
        if (
            repo == str(decisive["repo_key"])
            and path == str(decisive["path"])
            and start <= int(decisive["window"]["end"])
            and end >= int(decisive["window"]["start"])
        ):
            window_bytes = actual if not asserted.strip() else asserted
            if str(decisive["marker"]) in window_bytes:
                decisive_hits.append(
                    {
                        "claim_id": claim.get("claim_id") or f"c{index}",
                        "repo": repo,
                        "source_oid": str((run_repos.get(repo) or {}).get("source_oid") or ""),
                        "path": path,
                        "line_start": start,
                        "line_end": end,
                        "non_initial_window": start > 1,
                        "cited_the_marker": True,
                    }
                )
    for index, step in enumerate(steps, start=1):
        repo = str(step.get("repo") or "")
        if repo in decoy_keys:
            decoy_refs.append(
                {"where": "step", "repo": repo, "path": str(step.get("objective") or "")[:80]}
            )
    for target in (plan or {}).get("write_targets") or []:
        if str(target) in decoy_keys:
            decoy_refs.append({"where": "write_target", "repo": str(target), "path": ""})

    questions = [str(q) for q in (plan or {}).get("questions") or []]
    question_raised = any(_contains_any(q, ambiguity["question_terms"]) for q in questions)
    invented_evidence: list[str] = []
    for assumption in (plan or {}).get("assumptions") or []:
        if _contains_any(str(assumption), ambiguity["invented_default_terms"]):
            invented_evidence.append(f"assumption: {assumption}")
    for claim in claims:
        text = str(claim.get("text") or "") + " " + str(claim.get("asserted_content") or "")
        if _contains_any(text, ambiguity["invented_default_terms"]):
            invented_evidence.append(
                f"claim {claim.get('claim_id')}: {str(claim.get('text'))[:120]}"
            )

    write_scope = run.get("write_scope") if isinstance(run.get("write_scope"), Mapping) else {}
    targets = tuple(str(t) for t in write_scope.get("publication_targets") or [])
    authority = run.get("authority") if isinstance(run.get("authority"), Mapping) else {}
    probe = authority.get("probe") if isinstance(authority.get("probe"), Mapping) else {}
    refused = bool(probe.get("refused")) and str(probe.get("code")) == "outside_authorized_set"
    zero_content = probe.get("content_bytes") == 0

    return DiscoveryGrade(
        plan_present=plan is not None,
        decisive_constraint_found=bool(decisive_hits),
        decisive_evidence=tuple(decisive_hits),
        citations_reproduce=not mismatches,
        citation_mismatches=tuple(mismatches),
        decoy_excluded=not decoy_refs,
        decoy_references=tuple(decoy_refs),
        question_raised=question_raised,
        invented_default=bool(invented_evidence),
        invented_default_evidence=tuple(invented_evidence),
        write_scope_untouched=targets == (writable_key,),
        publication_targets=targets,
        expansion_requests=tuple(
            dict(entry) for entry in write_scope.get("write_scope.expansion_requests") or []
        ),
        authority_refusal_typed=refused and zero_content,
        authority_refusal_code=str(probe.get("code") or ""),
    )


# ---------------------------------------------------------------------------
# The capture driver
# ---------------------------------------------------------------------------


async def capture_discovery_run(
    manifest: Mapping[str, Any],
    out_dir: Path,
    mode: str,
    *,
    env: Mapping[str, str] | None = None,
    gateway: LiveGateway | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Capture ONE discovery run in *mode* (``scripted`` or ``live``)."""
    if mode not in (_MODE_SCRIPTED, _MODE_LIVE):
        raise ValueError(f"unknown capture mode {mode!r}")
    boundary = build_boundary(manifest, out_dir)
    caps = manifest["caps"]
    task = manifest["task"]
    clock = now if now is not None else DeterministicClock()
    repos = _research_repos(boundary)
    refusals: list[dict[str, Any]] = []

    scripted = mode == _MODE_SCRIPTED
    cap = SpendCap(
        model=(gateway.model if gateway else "scripted"),
        limit_usd=float(caps["max_usd"]),
        prices=manifest["prices_usd_per_mtok"],
    )
    if scripted:
        snapshot = Snapshot(
            repos={
                key: {
                    "repository_id": repo.repository_id,
                    "source_oid": repo.source_oid,
                    "files": dict(boundary.files[key]),
                }
                for key, repo in repos.items()
            }
        )
        script = ScriptedInvestigation(
            snapshot,
            turns=SCRIPTED_TURNS,
            deep=SCRIPTED_DEEP,
            input_tokens=SCRIPTED_INPUT_TOKENS,
            output_tokens=SCRIPTED_OUTPUT_TOKENS,
        )
        # A scripted turn spends no vendor money — it never touches the cap;
        # its recorded token counts ride the cost document as receipts.
        complete: CompletionFn = script
        provenance = PROVENANCE_OFFLINE_SCRIPTED
        identity = "scripted:reactive-investigation/v1"
        route = "offline-scripted-over-real-tool-loop"
    else:
        if gateway is None:
            raise ValueError("a live capture needs the resolved gateway")
        complete = capped_completion(
            gateway_completion(gateway, max_tokens=int(caps["max_tokens_per_call"])),
            cap,
            max_tokens=int(caps["max_tokens_per_call"]),
            purpose="research",
        )
        provenance = PROVENANCE_LIVE_MODEL
        identity = f"live:{gateway.model}"
        route = gateway.route
        clock = now if now is not None else time.monotonic

    lexical = _lexical_orientation(str(task["statement"]), repos)
    wrapped, proposals = _recording_complete(complete)
    harness = ResearchHarness(
        complete=wrapped,
        max_calls=int(caps["max_calls"]),
        wall_seconds=float(caps["wall_seconds"]),
    )
    outcome = await run_research_pass(
        harness, planner_input=str(task["statement"]), lexical=lexical, repos=repos, now=clock
    )
    stopped = str(outcome.document.get("stopped_reason") or "")
    if "SpendCapReached" in stopped:
        stopped = "spend_cap"

    # Mint the observations by re-executing the recorded proposals through
    # the AUTHORITY gate (typed refusal, zero content, for any key outside
    # the approved set — authorized keys ride the frozen toolboxes).
    observations = [
        _execute_gated(call, repos, boundary.authorized, refusals) for call in proposals
    ]
    document = dict(outcome.document)
    document["findings"] = [
        {
            "evidence_id": f"ev-{index}",
            "repo_key": finding.repo_key,
            "repository_id": finding.repository_id,
            "source_oid": finding.source_oid,
            "path": finding.path,
            "line": finding.line,
            "kind": finding.kind,
            "detail": finding.detail,
        }
        for index, finding in enumerate(outcome.findings, start=1)
    ]
    document["lexical_orientation"] = lexical

    plan_failure = ""
    if scripted:
        plan: dict[str, Any] | None = synthesize_scripted_plan(
            document,
            document["findings"],
            boundary.files,
            writable_key=boundary.profile.writable.key,
            decisive=task["decisive"],
        )
    else:
        synthesis = capped_completion(
            gateway_completion(gateway, max_tokens=int(caps["plan_max_tokens"])),
            cap,
            max_tokens=int(caps["plan_max_tokens"]),
            purpose="plan-synthesis",
        )
        try:
            result = await synthesis(
                _PLAN_SYSTEM_PROMPT,
                _render_plan_prompt(task, document, repos, observations, boundary.files),
            )
        except SpendCapReached as exc:
            plan, plan_failure = None, f"spend_cap: {exc}"
        else:
            parsed = _first_json_object(str(getattr(result, "text", result)))
            plan = _validated_model_plan(parsed) if parsed is not None else None
            if plan is None:
                plan_failure = "malformed_or_invalid_plan_json"

    # The write boundary: review the plan's write proposals against the
    # profile — a neighbor write is refused and SURFACED as a material
    # expansion request; publication keeps naming only the writable target.
    write_pairs = sorted(
        {
            (str(t).split(":", 1)[0], str(t).split(":", 1)[1])
            for t in (plan or {}).get("write_targets") or []
            if ":" in str(t)
        }
    )
    review = review_write_proposals(boundary.profile, write_pairs)
    authority = await _authority_probe(boundary)

    run_document = {
        "schema": RUN_SCHEMA,
        "run_id": f"{task['task_id']}-{mode}",
        "task_id": task["task_id"],
        "mode": mode,
        "capture": {
            "provenance": provenance,
            "model_identity": identity,
            "route": route,
            "live_provider": not scripted,
            "captured_at": manifest["registered_at"],
            "harness": "run_research_pass + plan synthesis",
            "issue": manifest["issue"],
        },
        "manifest": {
            "digest": manifest_digest(manifest),
            "authorized_repo_set_digest": boundary.authorized.digest,
            "neighbor_set_digest": neighbor_set_digest(
                {
                    key: {"repository_id": repo.repository_id, "source_oid": repo.source_oid}
                    for key, repo in repos.items()
                    if key != boundary.profile.writable.key
                }
            ),
        },
        "repos": [dict(entry) for entry in boundary.repos_document],
        "catalog": [dict(entry) for entry in boundary.catalog_document],
        "resolved_readers": [dict(entry) for entry in boundary.resolved_identities],
        "research_document": document,
        "observations": [
            {
                "tool": observation.tool,
                "repo_key": observation.repo_key,
                "call": observation.call,
                "content": observation.content,
                "error": observation.error,
                "truncated": observation.truncated,
            }
            for observation in observations
        ],
        "authority": {**authority, "model_refusals": refusals},
        "plan": plan,
        "plan_failure": plan_failure,
        "write_scope": review.as_document(),
        "stopped_reason": stopped,
    }
    run_document["grade"] = grade_discovery_run(
        run_document, manifest, boundary.files
    ).as_document()
    run_document["cost"] = _cost_document(manifest, cap, scripted)
    return run_document


def _cost_document(manifest: Mapping[str, Any], cap: SpendCap, scripted: bool) -> dict[str, Any]:
    if scripted:
        receipts = [
            {
                "input_tokens": int(a),
                "output_tokens": int(b),
                "usage_known": True,
                "usd_estimated": 0.0,
                "note": "scripted turn — recorded token counts, zero vendor spend",
            }
            for a, b in zip(SCRIPTED_INPUT_TOKENS, SCRIPTED_OUTPUT_TOKENS)
        ]
        usd = 0.0
        usage_note = "offline scripted model: token counts are the recorded script values"
    else:
        receipts = list(cap.receipts)
        usd = round(cap.spent_usd, 6)
        usage_note = "usage from the gateway receipts; unknown usage charged at the worst case"
    return {
        "receipts": receipts,
        "usd_estimated": usd,
        "cap_usd": float(manifest["caps"]["max_usd"]),
        "cap_exhausted": cap.exhausted if not scripted else False,
        "prices_usd_per_mtok": manifest["prices_usd_per_mtok"],
        "price_note": manifest["price_note"],
        "usage_note": usage_note,
    }


async def _check_gateway_reachable(gateway: LiveGateway) -> dict[str, Any]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{gateway.base_url}/v1/models")
        return {
            "reachable": response.status_code == 200,
            "status_code": response.status_code,
            "endpoint": f"{gateway.base_url}/v1/models",
        }
    except Exception as exc:  # noqa: BLE001 — record the refusal reason verbatim
        return {
            "reachable": False,
            "status_code": None,
            "endpoint": f"{gateway.base_url}/v1/models",
            "error": f"{type(exc).__name__}: {exc}",
        }


def refused_live_run(
    manifest: Mapping[str, Any], reason: str, detail: dict[str, Any]
) -> dict[str, Any]:
    """The honest record of a live attempt that never ran."""
    return {
        "schema": RUN_SCHEMA,
        "run_id": f"{manifest['task']['task_id']}-live",
        "task_id": manifest["task"]["task_id"],
        "mode": _MODE_LIVE,
        "outcome": "refused",
        "refusal_reason": reason,
        "refusal_detail": detail,
        "capture": {
            "provenance": "not-captured",
            "live_provider": False,
            "captured_at": manifest["registered_at"],
            "issue": manifest["issue"],
        },
        "manifest": {"digest": manifest_digest(manifest)},
        "plan": None,
        "grade": DiscoveryGrade(
            plan_present=False,
            decisive_constraint_found=False,
            citations_reproduce=False,
            decoy_excluded=False,
            question_raised=False,
            write_scope_untouched=False,
            authority_refusal_typed=False,
        ).as_document(),
        "cost": {"usd_estimated": 0.0, "usage_note": "no model call was made"},
    }


# ---------------------------------------------------------------------------
# The report — one entry per mode, never pooled
# ---------------------------------------------------------------------------

ACCEPTANCE_MAPPING: tuple[tuple[str, str], ...] = (
    (">=1 real model run identifies the neighbor-only constraint", "decisive_constraint_found"),
    ("the irrelevant repository is not dragged in", "decoy_excluded"),
    (
        "a disallowed reader cannot be introduced (typed refusal, zero content)",
        "authority_refusal_typed",
    ),
    (
        "citations reproduce the exact repository/OID/byte range incl. noninitial windows",
        "citations_reproduce",
    ),
    ("the published candidate changes only the authorized target", "write_scope_untouched"),
    ("unknowns become actionable questions, not invented defaults", "question_raised"),
)


def build_report(
    manifest: Mapping[str, Any],
    runs: Mapping[str, Mapping[str, Any]],
    files: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """One entry per mode — grades RE-DERIVED mechanically from the recorded
    runs (the grader is pure), with each run's capture-time grade kept
    beside as provenance.  The two modes are never pooled."""
    entries: dict[str, Any] = {}
    for mode, run in sorted(runs.items()):
        entry: dict[str, Any] = {
            "mode": mode,
            "run_id": run.get("run_id"),
            "provenance": (run.get("capture") or {}).get("provenance"),
            "cost": run.get("cost"),
            "artifact": f"runs/{mode}.json",
        }
        if run.get("outcome") == "refused":
            entry["outcome"] = "refused"
            entry["refusal_reason"] = run.get("refusal_reason")
            entry["grade"] = run.get("grade")
        else:
            entry["grade"] = grade_discovery_run(run, manifest, files).as_document()
            entry["grade_at_capture"] = run.get("grade")
        entries[mode] = entry
    return {
        "schema": REPORT_SCHEMA,
        "issue": manifest["issue"],
        "task": {
            "task_id": manifest["task"]["task_id"],
            "statement": manifest["task"]["statement"],
            "decisive": manifest["task"]["decisive"],
        },
        "manifest_digest": manifest_digest(manifest),
        "runs": entries,
        "acceptance_mapping": [
            {"criterion": criterion, "grade_field": field}
            for criterion, field in ACCEPTANCE_MAPPING
        ],
        "never_pooled": True,
        "limitations": [
            "the repositories are FIXTURE service repos, not customer repositories",
            "one task (a single neighbor-dependency scenario); no sample statistics",
            "the grader is mechanical — semantic plan quality is NOT judged here",
            "the scripted arm proves the capture path, not model performance; the "
            "live arm is the only performance-bearing run and is reported separately",
        ],
    }


# ---------------------------------------------------------------------------
# R38-11 (#312) — the CUSTOMER-SCALE planning profile: the scaled fixture
# graph, the frozen manifest, the deterministic qualification run, the grader
# ---------------------------------------------------------------------------

CUSTOMER_MANIFEST_SCHEMA = "forge.discovery.customer.manifest/1"
CUSTOMER_RUN_SCHEMA = "forge.discovery.profile.run/1"
CUSTOMER_REPORT_SCHEMA = "forge.discovery.profile.report/1"

CUSTOMER_REGISTERED_AT = "2026-09-25T00:00:00+00:00"
CUSTOMER_TASK_ID = "DP-01-customer-refund-graph"
CUSTOMER_MANIFEST_FILENAME = "manifest-customer-v1.json"
CUSTOMER_FIXTURES = "fixtures-customer-v1"

ROLE_DECISIVE = "decisive_neighbor"
ROLE_BUDGET_SINK = "budget_sink"
ROLE_NOISE = "noise"

DEPTH_LINE = "line-200+"
DEPTH_SECOND_PAGE = "second-page-window"
DEPTH_CONFLICT = "conflicting-current-vs-obsolete"
DEPTH_KINDS = (DEPTH_LINE, DEPTH_SECOND_PAGE, DEPTH_CONFLICT)

#: the observation page width the second-page decisive fact sits beyond
#: (the research loop's per-observation content cap).
SECOND_PAGE_CHARS = 2000

#: the LARGE docs repository must be genuinely large — big enough that
#: paging it CAN eat a whole investigation budget.
BUDGET_SINK_MIN_FILES = 40
BUDGET_SINK_MIN_BYTES = 100_000

CUST_OWN_KEY = "gitlab:orders-api"
CUST_BILLING_KEY = "gitlab:billing-policy"
CUST_AUDIT_KEY = "gitlab:audit-events"
CUST_SHIPPING_KEY = "gitlab:shipping-policy"
CUST_DOCS_KEY = "gitlab:platform-docs"

CUST_TASK_STATEMENT = (
    "Support refund requests in Orders' checkout for orders that expired less "
    "than 30 days ago: wire the request through to Billing's policy client, "
    "emit the audit event the audit service requires for recent expiries, and "
    "send the shipment confirmation Shipping owns. Finance said large refunds "
    "need an extra approval step before capture, but the issue does not say "
    "from which amount, who performs that approval for orders-api, or which "
    "recipient the refund confirmation must reach."
)

CUST_AMBIGUITY = {
    "question_terms": (
        "who approves",
        "who performs",
        "approver",
        "approval owner",
        "which recipient",
        "which policy is current",
    ),
    "invented_default_terms": (
        "auto-approve",
        "approve automatically",
        "no approval required",
        "without approval",
        "warehouse-only is current",
        "customer-and-fulfillment is current",
    ),
}

#: role, key, base_url, connection_id, numeric_id, repository_id
CUSTOMER_REPOSITORIES: tuple[tuple[str, str, str, int, int, str], ...] = (
    ("writable", CUST_OWN_KEY, "https://gitlab-orders.internal.example", 21, 2101, "orders-api"),
    (
        "decisive",
        CUST_BILLING_KEY,
        "https://gitlab-billing.internal.example",
        22,
        2102,
        "billing-policy",
    ),
    ("decisive", CUST_AUDIT_KEY, "https://gitlab-audit.internal.example", 23, 2103, "audit-events"),
    (
        "decisive",
        CUST_SHIPPING_KEY,
        "https://gitlab-shipping.internal.example",
        24,
        2104,
        "shipping-policy",
    ),
    ("sink", CUST_DOCS_KEY, "https://gitlab-docs.internal.example", 25, 2105, "platform-docs"),
    (
        "noise",
        "gitlab:marketing-site",
        "https://gitlab-marketing.internal.example",
        26,
        2106,
        "marketing-site",
    ),
    (
        "noise",
        "gitlab:infra-terraform",
        "https://gitlab-infra.internal.example",
        27,
        2107,
        "infra-terraform",
    ),
    (
        "noise",
        "gitlab:mobile-checkout",
        "https://gitlab-mobile.internal.example",
        28,
        2108,
        "mobile-checkout",
    ),
    (
        "noise",
        "gitlab:search-index",
        "https://gitlab-search.internal.example",
        29,
        2109,
        "search-index",
    ),
)

CUSTOMER_DECISIVE: dict[str, dict[str, Any]] = {
    CUST_BILLING_KEY: {
        "path": "src/policy/refunds.py",
        "marker": "REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS",
        "min_line": 200,
        "depth": DEPTH_LINE,
        "note": "the manual-approval threshold lives at line 200+ of the neighbor",
    },
    CUST_AUDIT_KEY: {
        "path": "src/audit/pipeline.py",
        "marker": "AUDIT_EMISSION_REQUIRED_AFTER_DAYS",
        "page_chars": SECOND_PAGE_CHARS,
        "depth": DEPTH_SECOND_PAGE,
        "note": "the emission window lives beyond the first observation page",
    },
    CUST_SHIPPING_KEY: {
        "path": "src/shipping/confirmation.py",
        "grep": "SHIP_CONFIRMATION_RECIPIENT",
        "current_marker": 'SHIP_CONFIRMATION_RECIPIENT_CURRENT = "customer-and-fulfillment"',
        "obsolete_marker": 'SHIP_CONFIRMATION_RECIPIENT_OBSOLETE = "warehouse-only"',
        "revision_current": "S-2026-03",
        "revision_obsolete": "S-2024-08",
        "depth": DEPTH_CONFLICT,
        "note": "current and obsolete recipient policies coexist; the plan must ask",
    },
}

CUSTOMER_CAPS: dict[str, Any] = {
    "max_calls": 12,
    "wall_seconds": 300.0,
    "sink_max_calls": 4,
    "budget_mode": "reasoning-heavy",
    "max_usd": 1.0,
}

CUST_SCRIPTED_INPUT_TOKENS = (2400, 2200, 2600, 1800, 1500)
CUST_SCRIPTED_OUTPUT_TOKENS = (200, 160, 220, 140, 420)
CUST_SINK_INPUT_TOKENS = (2100, 2300)
CUST_SINK_OUTPUT_TOKENS = (150, 150)


def _customer_fixture_root(manifest_path: Path) -> Path:
    """The fixtures root for a customer manifest: its OWN directory.

    The manifest lives in ``evaluation/discovery_live/`` and references
    ``fixtures-customer-v1/<repo>`` beside itself, so the scaled graph
    and its frozen manifest stay one unit regardless of where the run
    directory is pointed.
    """
    return manifest_path.parent


def _marker_line_of(files: Mapping[str, str], path: str, marker: str) -> int:
    lines = (files.get(path) or "").splitlines()
    hits = [number for number, text in enumerate(lines, start=1) if marker in text]
    if not hits:
        raise ValueError(f"the marker {marker!r} is absent from {path}")
    return hits[0]


def _marker_char_of(files: Mapping[str, str], path: str, marker: str) -> int:
    content = files.get(path) or ""
    offset = content.find(marker)
    if offset < 0:
        raise ValueError(f"the marker {marker!r} is absent from {path}")
    return offset


def build_customer_manifest_document(fixtures_root: Path) -> dict[str, Any]:
    """Derive the customer-scale manifest from the fixtures' frozen bytes."""
    repos: list[dict[str, Any]] = []
    for role, key, base_url, connection_id, numeric_id, repository_id in CUSTOMER_REPOSITORIES:
        fixture = f"{CUSTOMER_FIXTURES}/{repository_id}"
        files = _load_fixture_files(fixtures_root, fixture)
        if not files:
            raise SystemExit(f"fixture {fixtures_root / fixture} is empty")
        entry: dict[str, Any] = {
            "key": key,
            "role": {
                "writable": ROLE_WRITABLE,
                "decisive": ROLE_DECISIVE,
                "sink": ROLE_BUDGET_SINK,
                "noise": ROLE_NOISE,
            }[role],
            "connection": {
                "provider": "gitlab",
                "base_url": base_url,
                "connection_id": connection_id,
            },
            "numeric_id": numeric_id,
            "repository_id": repository_id,
            "source_oid": _oid(files),
            "fixture": fixture,
            "allowed_globs": ["**"],
        }
        if key in CUSTOMER_DECISIVE:
            decisive = dict(CUSTOMER_DECISIVE[key])
            repo_files = files
            if decisive["depth"] == DEPTH_LINE:
                decisive["marker_line"] = _marker_line_of(
                    repo_files, decisive["path"], decisive["marker"]
                )
            if decisive["depth"] == DEPTH_SECOND_PAGE:
                decisive["marker_char"] = _marker_char_of(
                    repo_files, decisive["path"], decisive["marker"]
                )
                decisive["marker_line"] = _marker_line_of(
                    repo_files, decisive["path"], decisive["marker"]
                )
            if decisive["depth"] == DEPTH_CONFLICT:
                decisive["current_line"] = _marker_line_of(
                    repo_files, decisive["path"], decisive["current_marker"]
                )
                decisive["obsolete_line"] = _marker_line_of(
                    repo_files, decisive["path"], decisive["obsolete_marker"]
                )
            entry["decisive"] = decisive
        repos.append(entry)
    document = {
        "schema": CUSTOMER_MANIFEST_SCHEMA,
        "registered_at": CUSTOMER_REGISTERED_AT,
        "issue": "R38-11 / #312",
        "task": {
            "task_id": CUSTOMER_TASK_ID,
            "statement": CUST_TASK_STATEMENT,
            "ambiguity": {
                **CUST_AMBIGUITY,
                "question_terms": list(CUST_AMBIGUITY["question_terms"]),
                "invented_default_terms": list(CUST_AMBIGUITY["invented_default_terms"]),
            },
        },
        "repos": repos,
        "caps": dict(CUSTOMER_CAPS),
        "oid_scheme": OID_SCHEME,
        "note": (
            "the customer-scale planning profile graph: one writable target,"
            " three decisive neighbors at different depths, one LARGE"
            " irrelevant docs repository (the budget-sink arm) and four noise"
            " repositories; the live-model run over this graph is the"
            " partner-gated remainder (R38-14) and is not claimed here"
        ),
    }
    validate_customer_manifest(document, fixtures_root)
    return document


def validate_customer_manifest(document: Mapping[str, Any], fixtures_root: Path) -> None:
    """The scaled-graph contract — refuses any drift from the frozen scenario.

    Validates through the EXISTING authority machinery (connection
    identities, frozen OIDs re-derived from the fixture bytes) plus the
    scale requirements: at least eight repositories, exactly one
    writable, all three decisive DEPTH kinds present and distinct, a
    genuinely LARGE budget-sink repository, decisive markers living
    ONLY in their own repositories, the second-page fact beyond the
    first observation page, and both sides of the policy conflict
    present in the neighbor at different lines.
    """
    if document.get("schema") != CUSTOMER_MANIFEST_SCHEMA:
        raise ValueError(
            f"manifest schema {document.get('schema')!r} is not {CUSTOMER_MANIFEST_SCHEMA!r}"
        )
    repos = document.get("repos")
    if not isinstance(repos, list) or len(repos) < 8:
        raise ValueError("the customer graph needs at least eight repositories")
    roles = [str(entry.get("role") or "") for entry in repos]
    if roles.count(ROLE_WRITABLE) != 1:
        raise ValueError("exactly one repository may be the writable target")
    if roles.count(ROLE_BUDGET_SINK) != 1:
        raise ValueError("exactly one LARGE budget-sink repository is declared")
    if roles.count(ROLE_NOISE) < 1:
        raise ValueError("the graph needs at least one noise repository")
    keys = [str(entry.get("key") or "") for entry in repos]
    if len(set(keys)) != len(keys):
        raise ValueError("repository keys must be unique")
    depths = [
        str((entry.get("decisive") or {}).get("depth") or "")
        for entry in repos
        if entry.get("role") == ROLE_DECISIVE
    ]
    if sorted(depths) != sorted(DEPTH_KINDS):
        raise ValueError(
            f"the decisive neighbors must carry all three depth kinds {DEPTH_KINDS}, got {depths}"
        )
    task = document.get("task") if isinstance(document.get("task"), Mapping) else {}
    statement = str(task.get("statement") or "")
    if len(statement) < 40:
        raise ValueError("the task statement is missing")

    oid_scheme = str(document.get("oid_scheme") or "")
    all_markers: list[str] = []
    for entry in repos:
        key = str(entry.get("key") or "")
        files = _load_fixture_files(fixtures_root, str(entry.get("fixture") or ""))
        connection = entry.get("connection") if isinstance(entry.get("connection"), Mapping) else {}
        ConnectionIdentity(
            provider=str(connection.get("provider") or ""),
            base_url=str(connection.get("base_url") or ""),
            connection_id=int(connection.get("connection_id") or 0),
        )
        if oid_scheme == OID_SCHEME:
            derived = _oid(files)
            recorded = str(entry.get("source_oid") or "")
            if derived != recorded:
                raise ValueError(
                    f"{key}: the fixture re-derives OID {derived[:12]}… but the manifest"
                    f" recorded {recorded[:12]}… — the frozen snapshot drifted"
                )
        decisive = entry.get("decisive") if isinstance(entry.get("decisive"), Mapping) else None
        if decisive is None:
            continue
        depth = str(decisive.get("depth") or "")
        if depth == DEPTH_LINE:
            marker = str(decisive["marker"])
            line = _marker_line_of(files, str(decisive["path"]), marker)
            if line < int(decisive.get("min_line") or 0):
                raise ValueError(
                    f"{key}: the decisive marker sits at line {line}, before the"
                    f" required depth (line >= {decisive.get('min_line')})"
                )
            all_markers.append(marker)
        elif depth == DEPTH_SECOND_PAGE:
            marker = str(decisive["marker"])
            page = int(decisive.get("page_chars") or SECOND_PAGE_CHARS)
            char = _marker_char_of(files, str(decisive["path"]), marker)
            if char < page:
                raise ValueError(
                    f"{key}: the decisive marker sits at char {char}, INSIDE the first"
                    f" {page}-char observation page — the depth requirement failed"
                )
            all_markers.append(marker)
        elif depth == DEPTH_CONFLICT:
            current_line = _marker_line_of(
                files, str(decisive["path"]), str(decisive["current_marker"])
            )
            obsolete_line = _marker_line_of(
                files, str(decisive["path"]), str(decisive["obsolete_marker"])
            )
            if current_line == obsolete_line:
                raise ValueError(f"{key}: the conflicting policies must sit at different lines")
            all_markers.append(str(decisive["current_marker"]))
            all_markers.append(str(decisive["obsolete_marker"]))
        else:
            raise ValueError(f"{key}: unknown decisive depth {depth!r}")
    for marker in all_markers:
        if marker in statement:
            raise ValueError("the task statement must NOT leak a decisive marker")
    # every decisive marker lives ONLY in its own repository
    marker_owner: dict[str, str] = {}
    for entry in repos:
        decisive = entry.get("decisive") if isinstance(entry.get("decisive"), Mapping) else None
        if decisive is None:
            continue
        for marker_field in ("marker", "current_marker", "obsolete_marker"):
            if str(decisive.get(marker_field) or ""):
                marker_owner[str(decisive[marker_field])] = str(entry.get("key") or "")
    for entry in repos:
        key = str(entry.get("key") or "")
        repo_files = _load_fixture_files(fixtures_root, str(entry.get("fixture") or ""))
        for marker, owner in marker_owner.items():
            if owner == key:
                continue
            if any(marker in text for text in repo_files.values()):
                raise ValueError(
                    f"the decisive marker also appears in {key} — the facts must live"
                    " ONLY in their own repositories"
                )
    sink = next(entry for entry in repos if entry.get("role") == ROLE_BUDGET_SINK)
    sink_files = _load_fixture_files(fixtures_root, str(sink.get("fixture") or ""))
    if (
        len(sink_files) < BUDGET_SINK_MIN_FILES
        or sum(len(t) for t in sink_files.values()) < BUDGET_SINK_MIN_BYTES
    ):
        raise ValueError(
            "the budget-sink repository is not LARGE "
            f"({len(sink_files)} files, needs >= {BUDGET_SINK_MIN_FILES})"
        )
    caps = document.get("caps") if isinstance(document.get("caps"), Mapping) else {}
    if int(caps.get("max_calls") or 0) < 1 or int(caps.get("sink_max_calls") or 0) < 1:
        raise ValueError("the caps must bound both the main and the sink budgets")
    if int(caps.get("sink_max_calls") or 0) >= int(caps.get("max_calls") or 0):
        raise ValueError("the sink budget must be SMALLER than the main budget")
    if str(caps.get("budget_mode") or "") not in SYNTHESIS_BUDGET_PROFILES:
        raise ValueError("the caps must name a known synthesis budget mode")
    if not 0 < float(caps.get("max_usd") or 0) <= 1.0:
        raise ValueError("the spend cap must be in (0, 1.0] USD")


def load_customer_manifest(manifest_path: Path) -> dict[str, Any]:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_customer_manifest(document, _customer_fixture_root(manifest_path))
    return document


def customer_manifest_digest(document: Mapping[str, Any]) -> str:
    return _sha256(_canonical(dict(document)))


def customer_files(manifest: Mapping[str, Any], fixtures_root: Path) -> dict[str, dict[str, str]]:
    """The frozen snapshot bytes of every manifest repository."""
    return {
        str(entry["key"]): _load_fixture_files(fixtures_root, str(entry["fixture"]))
        for entry in manifest["repos"]
    }


# ---------------------------------------------------------------------------
# The scripted qualification — deterministic over the frozen graph
# ---------------------------------------------------------------------------


def _char_span(
    files: Mapping[str, str], path: str, line: int, before: int, after: int
) -> tuple[int, int]:
    """The (offset, length) char span of a line window, from the frozen bytes."""
    lines = (files.get(path) or "").splitlines(keepends=True)
    start_line = max(1, line - before)
    end_line = min(len(lines), line + after)
    offset = sum(len(text) for text in lines[: start_line - 1])
    stop = sum(len(text) for text in lines[:end_line])
    return offset, stop - offset


def _customer_turns(
    manifest: Mapping[str, Any], files: Mapping[str, Mapping[str, str]]
) -> list[dict[str, Any]]:
    """The deterministic investigation script, derived from the frozen bytes.

    Orientation greps over the writable target and the three decisive
    neighbors, a first-page read of the audit file (whose decisive fact
    is beyond it), the decisive deep reads (the billing line window, the
    audit CONTINUATION window past the first page) and one deliberate
    REPEAT of the billing window — the read the observation cache must
    serve without re-paying. The final turn declares done with an honest
    summary that names the conflict instead of resolving it.
    """
    billing = next(e for e in manifest["repos"] if e["key"] == CUST_BILLING_KEY)
    audit = next(e for e in manifest["repos"] if e["key"] == CUST_AUDIT_KEY)
    shipping = next(e for e in manifest["repos"] if e["key"] == CUST_SHIPPING_KEY)
    billing_dec, audit_dec, shipping_dec = (
        billing["decisive"],
        audit["decisive"],
        shipping["decisive"],
    )

    billing_offset, billing_length = _char_span(
        files[CUST_BILLING_KEY], billing_dec["path"], int(billing_dec["marker_line"]), 4, 12
    )
    page = int(audit_dec.get("page_chars") or SECOND_PAGE_CHARS)
    return [
        {
            "calls": [
                {"tool": "grep", "repo": CUST_OWN_KEY, "args": {"pattern": "request_refund"}},
                {"tool": "grep", "repo": CUST_BILLING_KEY, "args": {"pattern": "MANUAL_APPROVAL"}},
                {"tool": "grep", "repo": CUST_AUDIT_KEY, "args": {"pattern": "AUDIT_EMISSION"}},
            ]
        },
        {
            "calls": [
                {
                    "tool": "grep",
                    "repo": CUST_SHIPPING_KEY,
                    "args": {"pattern": shipping_dec["grep"]},
                },
                # the first observation page of the audit file — the
                # decisive fact is deliberately NOT in it
                {"tool": "read_file", "repo": CUST_AUDIT_KEY, "args": {"path": audit_dec["path"]}},
            ]
        },
        {
            "calls": [
                {
                    "tool": "read_file",
                    "repo": CUST_BILLING_KEY,
                    "args": {
                        "path": billing_dec["path"],
                        "offset": billing_offset,
                        "length": billing_length,
                    },
                },
                # the continuation read past the first page — this is
                # where the audit fact becomes visible
                {
                    "tool": "read_file",
                    "repo": CUST_AUDIT_KEY,
                    "args": {"path": audit_dec["path"], "offset": page, "length": page},
                },
            ]
        },
        {
            "calls": [
                # the deliberate REPEAT of the billing window: the read
                # the cache must serve as a verified reuse
                {
                    "tool": "read_file",
                    "repo": CUST_BILLING_KEY,
                    "args": {
                        "path": billing_dec["path"],
                        "offset": billing_offset,
                        "length": billing_length,
                    },
                }
            ]
        },
        {
            "done": True,
            "summary": (
                "Orders' checkout gates refund requests by age only and routes money"
                " decisions to Billing's policy client; the approval rules are NOT"
                " decided in Orders. The CURRENT approval policy is Billing's"
                " revision F-2024-11 in src/policy/refunds.py: refunds at or above"
                " 5000 cents require a manual approval step before capture. The"
                " audit service's CURRENT rule A-2026-02 in src/audit/pipeline.py"
                " requires a refund event while the request is within 21 days of"
                " the ORDER's expiry. Shipping carries CONFLICTING confirmation"
                " recipients for refund shipments — the S-2026-03 value and the"
                " superseded S-2024-08 value both sit in"
                " src/shipping/confirmation.py, so which recipient is current is a"
                " question, not a pick. The issue also leaves undecided who"
                " performs the approval step for orders-api — which approvals"
                " queue or owner handles refunds above the threshold?"
            ),
            "assumptions": [],
            "contradictions": [
                "shipping confirmation recipient: current S-2026-03 vs superseded"
                " S-2024-08 — unresolved"
            ],
        },
    ]


def _sink_turns() -> list[dict[str, Any]]:
    """The budget-sink script: the whole small budget spent paging docs.

    The sink investigation never reaches a done turn — the call budget
    ends first BY CONSTRUCTION, so the arm measures that the docs
    repository cannot silently consume a budget without the exhaustion
    signal surfacing.
    """
    return [
        {
            "calls": [
                {"tool": "list_paths", "repo": CUST_DOCS_KEY, "args": {"prefix": "handbook/"}},
                {
                    "tool": "read_file",
                    "repo": CUST_DOCS_KEY,
                    "args": {"path": "handbook/ch-001.md"},
                },
                {
                    "tool": "read_file",
                    "repo": CUST_DOCS_KEY,
                    "args": {"path": "handbook/ch-002.md"},
                },
            ]
        },
        {
            "calls": [
                {
                    "tool": "read_file",
                    "repo": CUST_DOCS_KEY,
                    "args": {"path": "handbook/ch-003.md"},
                },
                {
                    "tool": "read_file",
                    "repo": CUST_DOCS_KEY,
                    "args": {"path": "handbook/ch-004.md"},
                },
                {
                    "tool": "read_file",
                    "repo": CUST_DOCS_KEY,
                    "args": {"path": "handbook/ch-005.md"},
                },
            ]
        },
    ]


def _customer_research_repos(
    files: Mapping[str, Mapping[str, str]], manifest: Mapping[str, Any]
) -> dict[str, ResearchRepo]:
    entries = {str(entry["key"]): entry for entry in manifest["repos"]}
    return {
        key: ResearchRepo(
            repo_key=key,
            repository_id=str(entries[key]["repository_id"]),
            source_oid=str(entries[key]["source_oid"]),
            toolbox=SnapshotToolbox(dict(repo_files)),
        )
        for key, repo_files in files.items()
    }


def _mint_through_cache(
    call: Mapping[str, Any],
    *,
    repos: Mapping[str, ResearchRepo],
    files: Mapping[str, Mapping[str, str]],
    authorized: AuthorizedRepoSet,
    cache: ObservationCache,
    policy_scope: str,
    refusals: list[dict[str, Any]],
) -> ToolObservation:
    """Re-execute one proposed call through the AUTHORITY gate and, for
    file reads, the OBSERVATION CACHE of the planning scope.

    A read of an immutable source the scope already verified is served
    from the cache (the reader is not re-paid); anything else executes
    exactly like the base harness mint. Unauthorized keys refuse typed
    with zero content, unchanged.
    """
    repo_key = str(call.get("repo") or call.get("repository") or "")
    gate_key = "own" if repo_key == authorized.profile.writable.key else repo_key
    tool = str(call.get("tool") or "")
    args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
    try:
        authorized.authorize_read(gate_key)
    except DiscoveryAuthorizationRefusal as refusal:
        refusals.append(
            {
                "requested": refusal.requested,
                "code": refusal.code,
                "authorized": list(refusal.authorized),
                "content_bytes": 0,
                "source": "model_proposal",
            }
        )
        return ToolObservation(
            tool=tool or "?",
            repo_key=repo_key,
            call=json.dumps(dict(args))[:160],
            content="",
            error=f"authority refusal ({refusal.code}): the repository is outside the authorized set",
        )
    if tool == "read_file":
        path = str(args.get("path") or "")
        offset = max(0, int(args.get("offset") or 0))
        length = args.get("length")
        repo = repos[repo_key]
        observation = cache.observe(
            lambda p: files[repo_key][p],
            repository=repo_key,
            source_oid=repo.source_oid,
            path=path,
            policy_scope=policy_scope,
        )
        content = observation.window(offset, int(length) if length is not None else None)
        truncated = len(content) > OBSERVATION_CONTENT_MAX_CHARS
        if truncated:
            content = content[:OBSERVATION_CONTENT_MAX_CHARS]
        span = f"offset {offset}"
        if length is not None:
            span += f" length {length}"
        return ToolObservation(
            tool="read_file",
            repo_key=repo_key,
            call=f"{path} {span}".strip(),
            content=content,
            truncated=truncated,
        )
    return _execute_call(call, repos)[1]


def _observation_call_parts(observation: Mapping[str, Any]) -> tuple[str, int, int | None]:
    """(path, offset, length) parsed out of a minted read observation."""
    call = str(observation.get("call") or "")
    match = re.match(r"(?P<path>\S+) offset (?P<offset>\d+)(?: length (?P<length>\d+))?", call)
    if match is None:
        return "", 0, None
    length = match.group("length")
    return match.group("path"), int(match.group("offset")), int(length) if length else None


def _conflict_from_observations(
    manifest: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> PolicyConflict | None:
    """The shipping conflict, derived from what the run ACTUALLY observed.

    Both grep-matched lines must be present in a shipping observation —
    the conflict is only real if the investigation saw both sides.
    """
    shipping = next(e for e in manifest["repos"] if e["key"] == CUST_SHIPPING_KEY)
    decisive = shipping["decisive"]
    source_oid = str(shipping["source_oid"])
    for observation in observations:
        if str(observation.get("repo_key")) != CUST_SHIPPING_KEY:
            continue
        content = str(observation.get("content") or "")
        current_line = obsolete_line = 0
        for line in content.splitlines():
            match = re.match(r"(?P<path>\S+):(?P<line>\d+):\s*(?P<text>.*)", line)
            if match is None:
                continue
            if decisive["current_marker"] in match.group("text") and not current_line:
                current_line = int(match.group("line"))
            if decisive["obsolete_marker"] in match.group("text") and not obsolete_line:
                obsolete_line = int(match.group("line"))
        if current_line and obsolete_line:
            return PolicyConflict(
                repository=CUST_SHIPPING_KEY,
                source_oid=source_oid,
                path=str(decisive["path"]),
                current=ConflictSide(
                    revision=str(decisive["revision_current"]),
                    line=current_line,
                    content=str(decisive["current_marker"]),
                ),
                obsolete=ConflictSide(
                    revision=str(decisive["revision_obsolete"]),
                    line=obsolete_line,
                    content=str(decisive["obsolete_marker"]),
                ),
            )
    return None


def _line_window_claim(
    *,
    claim_id: str,
    repo: str,
    path: str,
    line: int,
    before: int,
    after: int,
    files: Mapping[str, Mapping[str, str]],
    source_oid: str,
) -> dict[str, Any]:
    lines = (files.get(repo, {}).get(path) or "").splitlines()
    start = max(1, line - before)
    end = min(len(lines), line + after)
    content = "\n".join(lines[start - 1 : end])
    return {
        "claim_id": claim_id,
        "text": content.strip().splitlines()[0][:240] if content.strip() else "",
        "repo": repo,
        "path": path,
        "line_start": start,
        "line_end": end,
        "asserted_content": content,
        "source_oid": source_oid,
    }


def synthesize_customer_plan(
    manifest: Mapping[str, Any],
    files: Mapping[str, Mapping[str, str]],
    observations: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    conflict: PolicyConflict | None,
    summary: str,
) -> dict[str, Any]:
    """The deterministic customer-graph plan derived from what was READ.

    Claims are minted only for decisive facts whose marker bytes appear
    in a minted observation (the own-repo claim comes from the run's own
    finding); the CONFLICT repository contributes NO claim — its fact
    rides as the explicit conflict question, never a silent pick.
    """
    entries = {str(entry["key"]): entry for entry in manifest["repos"]}
    writable = next(entry for entry in manifest["repos"] if entry["role"] == ROLE_WRITABLE)
    writable_key = str(writable["key"])
    claims: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []

    def _add_claim(claim: dict[str, Any]) -> None:
        claims.append(claim)
        steps.append(
            {
                "step_id": f"s{len(steps) + 1}",
                "objective": f"address {claim['repo']}/{claim['path']}",
                "repo": claim["repo"],
                "evidence_ids": [claim["claim_id"]],
            }
        )

    # the writable target's own anchor (the first finding on the target)
    for finding in findings:
        if str(finding.get("repo_key")) == writable_key and int(finding.get("line") or 0) >= 1:
            _add_claim(
                _line_window_claim(
                    claim_id=f"c{len(claims) + 1}",
                    repo=writable_key,
                    path=str(finding.get("path") or ""),
                    line=int(finding.get("line") or 1),
                    before=2,
                    after=2,
                    files=files,
                    source_oid=str(writable["source_oid"]),
                )
            )
            break
    # the decisive facts — only when a minted observation carried the marker
    for key, before, after in (
        (CUST_BILLING_KEY, 4, 10),
        (CUST_AUDIT_KEY, 6, 10),
    ):
        decisive = entries[key]["decisive"]
        seen = any(
            str(observation.get("repo_key")) == key
            and str(decisive["marker"]) in str(observation.get("content") or "")
            for observation in observations
        )
        if not seen:
            continue
        _add_claim(
            _line_window_claim(
                claim_id=f"c{len(claims) + 1}",
                repo=key,
                path=str(decisive["path"]),
                line=int(decisive["marker_line"]),
                before=before,
                after=after,
                files=files,
                source_oid=str(entries[key]["source_oid"]),
            )
        )

    questions: list[str] = []
    if conflict is not None:
        questions.append(conflict_question(conflict))
    summary_questions = [
        question for question in _questions_of(summary) if question not in questions
    ]
    questions.extend(summary_questions[:2])
    return {
        "steps": steps,
        "claims": claims,
        "questions": questions,
        "assumptions": [],
        "write_targets": [writable_key],
        "synthesis": "deterministic-customer-profile/v1",
    }


# ---------------------------------------------------------------------------
# The mechanical customer-profile grader — pure over (run, manifest, bytes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerProfileGrade:
    """The per-arm verdict over one customer-profile qualification run."""

    plan_present: bool = False
    decisive_line_depth_found: bool = False
    decisive_line_depth_evidence: tuple[Mapping[str, Any], ...] = ()
    decisive_second_page_found: bool = False
    decisive_second_page_evidence: tuple[Mapping[str, Any], ...] = ()
    second_page_continuation_used: bool = False
    conflict_became_question: bool = False
    conflict_silent_pick: bool = False
    conflict_question: str = ""
    budget_sink_signalled: bool = False
    budget_sink_evidence: Mapping[str, Any] | None = None
    cache_reuse_on_repeat: bool = False
    no_cross_scope_leak: bool = False
    restart_resumption: bool = False
    budget_profile_reserved: bool = False
    synthesis_validated: bool = False
    synthesis_invalid_routes_to_recovery: bool = False
    carry_forward_populated: bool = False
    carry_forward_seam_verified: bool = False
    write_scope_single_target: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.plan_present
            and self.decisive_line_depth_found
            and self.decisive_second_page_found
            and self.second_page_continuation_used
            and self.conflict_became_question
            and not self.conflict_silent_pick
            and self.budget_sink_signalled
            and self.cache_reuse_on_repeat
            and self.no_cross_scope_leak
            and self.restart_resumption
            and self.budget_profile_reserved
            and self.synthesis_validated
            and self.synthesis_invalid_routes_to_recovery
            and self.carry_forward_populated
            and self.carry_forward_seam_verified
            and self.write_scope_single_target
        )

    @property
    def failed_checks(self) -> tuple[str, ...]:
        checks = {
            "plan_present": self.plan_present,
            "decisive_line_depth_found": self.decisive_line_depth_found,
            "decisive_second_page_found": self.decisive_second_page_found,
            "second_page_continuation_used": self.second_page_continuation_used,
            "conflict_became_question": self.conflict_became_question,
            "no_conflict_silent_pick": not self.conflict_silent_pick,
            "budget_sink_signalled": self.budget_sink_signalled,
            "cache_reuse_on_repeat": self.cache_reuse_on_repeat,
            "no_cross_scope_leak": self.no_cross_scope_leak,
            "restart_resumption": self.restart_resumption,
            "budget_profile_reserved": self.budget_profile_reserved,
            "synthesis_validated": self.synthesis_validated,
            "synthesis_invalid_routes_to_recovery": self.synthesis_invalid_routes_to_recovery,
            "carry_forward_populated": self.carry_forward_populated,
            "carry_forward_seam_verified": self.carry_forward_seam_verified,
            "write_scope_single_target": self.write_scope_single_target,
        }
        return tuple(name for name, ok in checks.items() if not ok)

    def as_document(self) -> dict[str, Any]:
        return {
            "plan_present": self.plan_present,
            "decisive_line_depth_found": self.decisive_line_depth_found,
            "decisive_line_depth_evidence": [
                dict(entry) for entry in self.decisive_line_depth_evidence
            ],
            "decisive_second_page_found": self.decisive_second_page_found,
            "decisive_second_page_evidence": [
                dict(entry) for entry in self.decisive_second_page_evidence
            ],
            "second_page_continuation_used": self.second_page_continuation_used,
            "conflict_became_question": self.conflict_became_question,
            "conflict_silent_pick": self.conflict_silent_pick,
            "conflict_question": self.conflict_question,
            "budget_sink_signalled": self.budget_sink_signalled,
            "budget_sink_evidence": dict(self.budget_sink_evidence or {}),
            "cache_reuse_on_repeat": self.cache_reuse_on_repeat,
            "no_cross_scope_leak": self.no_cross_scope_leak,
            "restart_resumption": self.restart_resumption,
            "budget_profile_reserved": self.budget_profile_reserved,
            "synthesis_validated": self.synthesis_validated,
            "synthesis_invalid_routes_to_recovery": self.synthesis_invalid_routes_to_recovery,
            "carry_forward_populated": self.carry_forward_populated,
            "carry_forward_seam_verified": self.carry_forward_seam_verified,
            "write_scope_single_target": self.write_scope_single_target,
            "passed": self.passed,
            "failed_checks": list(self.failed_checks),
        }


def grade_customer_profile(
    run: Mapping[str, Any],
    manifest: Mapping[str, Any],
    files: Mapping[str, Mapping[str, str]],
) -> CustomerProfileGrade:
    """Grade the scripted qualification MECHANICALLY, arm by arm.

    Pure over its three inputs; every arm re-derives from the frozen
    bytes and the recorded run — the decisive facts at their depths
    (including that the second-page fact was NOT in the first page and
    WAS reached by a continuation read), the conflict as a question with
    both citations and no silent pick, the budget sink's exhaustion
    signal, the cache economy (repeat reuse, scope isolation, restart
    resumption), the budget reserve, the synthesis validation routing,
    the carry-forward and its brief-envelope seam, and the single
    writable target.
    """
    entries = {str(entry["key"]): entry for entry in manifest["repos"]}
    writable = next(entry for entry in manifest["repos"] if entry["role"] == ROLE_WRITABLE)
    writable_key = str(writable["key"])
    plan = run.get("plan") if isinstance(run.get("plan"), Mapping) else None
    claims = [c for c in (plan or {}).get("claims") or [] if isinstance(c, Mapping)]
    questions = [str(q) for q in (plan or {}).get("questions") or []]
    observations = [
        observation
        for observation in run.get("observations") or []
        if isinstance(observation, Mapping)
    ]

    # -- decisive at line depth (billing) -----------------------------------
    billing_dec = entries[CUST_BILLING_KEY]["decisive"]
    line_evidence: list[dict[str, Any]] = []
    for claim in claims:
        if str(claim.get("repo")) != CUST_BILLING_KEY:
            continue
        try:
            start, end = int(claim.get("line_start")), int(claim.get("line_end"))
        except (TypeError, ValueError):
            continue
        lines = (files[CUST_BILLING_KEY].get(billing_dec["path"]) or "").splitlines()
        if not (1 <= start <= end <= len(lines)):
            continue
        window = "\n".join(lines[start - 1 : end])
        if str(billing_dec["marker"]) in window and start >= int(billing_dec["min_line"]):
            line_evidence.append(
                {
                    "claim_id": claim.get("claim_id"),
                    "line_start": start,
                    "line_end": end,
                    "non_initial_window": start > 1,
                    "min_line": int(billing_dec["min_line"]),
                }
            )

    # -- decisive in the second-page window (audit) --------------------------
    audit_dec = entries[CUST_AUDIT_KEY]["decisive"]
    page = int(audit_dec.get("page_chars") or SECOND_PAGE_CHARS)
    marker = str(audit_dec["marker"])
    first_page_had_marker = False
    continuation_used = False
    continuation_evidence: list[dict[str, Any]] = []
    for observation in observations:
        if str(observation.get("repo_key")) != CUST_AUDIT_KEY:
            continue
        path, offset, _length = _observation_call_parts(observation)
        if path != str(audit_dec["path"]):
            continue
        content = str(observation.get("content") or "")
        if offset == 0 and marker in content:
            first_page_had_marker = True
        if offset >= page and marker in content:
            continuation_used = True
            continuation_evidence.append(
                {
                    "observation_offset": offset,
                    "page_chars": page,
                    "marker_line": int(audit_dec["marker_line"]),
                    "cited_the_marker": True,
                }
            )
    page_evidence: list[dict[str, Any]] = []
    for claim in claims:
        if str(claim.get("repo")) != CUST_AUDIT_KEY:
            continue
        try:
            start, end = int(claim.get("line_start")), int(claim.get("line_end"))
        except (TypeError, ValueError):
            continue
        lines = (files[CUST_AUDIT_KEY].get(audit_dec["path"]) or "").splitlines()
        if not (1 <= start <= end <= len(lines)):
            continue
        window = "\n".join(lines[start - 1 : end])
        if marker in window:
            page_evidence.append(
                {
                    "claim_id": claim.get("claim_id"),
                    "line_start": start,
                    "line_end": end,
                    "marker_char": int(audit_dec["marker_char"]),
                }
            )

    # -- the conflict: a question with BOTH citations, no silent pick --------
    shipping_dec = entries[CUST_SHIPPING_KEY]["decisive"]
    current_value = str(shipping_dec["current_marker"]).split("=", 1)[1].strip().strip('"')
    obsolete_value = str(shipping_dec["obsolete_marker"]).split("=", 1)[1].strip().strip('"')
    conflict_questions = [
        question
        for question in questions
        if str(shipping_dec["path"]) in question
        and current_value in question
        and obsolete_value in question
        and f"line {shipping_dec['current_line']}" in question
        and f"line {shipping_dec['obsolete_line']}" in question
    ]
    conflict_silent_pick = any(
        str(claim.get("repo")) == CUST_SHIPPING_KEY
        and (
            current_value in str(claim.get("asserted_content"))
            or obsolete_value in str(claim.get("asserted_content"))
        )
        for claim in claims
    )

    # -- the budget sink -----------------------------------------------------
    sink = run.get("budget_sink") if isinstance(run.get("budget_sink"), Mapping) else {}
    sink_observations = [
        observation
        for observation in sink.get("observations") or []
        if isinstance(observation, Mapping) and str(observation.get("repo_key")) == CUST_DOCS_KEY
    ]
    sink_verdict = sink.get("verdict") if isinstance(sink.get("verdict"), Mapping) else {}
    sink_recovery = (
        sink_verdict.get("recovery") if isinstance(sink_verdict.get("recovery"), Mapping) else {}
    )
    sink_signalled = bool(
        sink
        and str(sink.get("stopped_reason")) == "max_calls"
        and sink_observations
        and int(sink.get("calls_proposed") or 0) >= int(sink.get("budget") or 0)
        and str(sink_verdict.get("classification")) == "exhausted_budget"
        and str(sink_recovery.get("kind")) == "surface_and_ask"
        and bool(sink_verdict.get("retained_findings_visible"))
        and not bool(sink_verdict.get("complete_understanding_claimed"))
        and int(sink.get("retained_findings") or 0) >= 1
    )

    # -- the cache economy ----------------------------------------------------
    cache = (
        run.get("observation_cache") if isinstance(run.get("observation_cache"), Mapping) else {}
    )
    cache_stats = cache.get("stats") if isinstance(cache.get("stats"), Mapping) else {}
    cache_reuse = (
        int(cache_stats.get("hits") or 0) >= 1
        and bool(cache.get("repeat_hit"))
        and int(cache_stats.get("underlying_reads") or 0)
        < int(cache_stats.get("hits") or 0) + int(cache_stats.get("misses") or 0)
    )
    scope_probe = cache.get("scope_probe") if isinstance(cache.get("scope_probe"), Mapping) else {}
    scopes_of_source = [str(scope) for scope in scope_probe.get("scopes_of_source") or []]
    no_leak = bool(
        cache.get("cross_scope_isolated")
        and scope_probe.get("isolated_scope_missed")
        and len(set(scopes_of_source)) == 2
        and len(scopes_of_source) == 2
    )
    restart = bool(cache.get("restart_resumed")) and int(cache.get("restart_hits") or 0) >= 1

    # -- the budget reserve ---------------------------------------------------
    budget = run.get("budget_profile") if isinstance(run.get("budget_profile"), Mapping) else {}
    standard = SYNTHESIS_BUDGET_PROFILES["standard"]
    budget_reserved = bool(
        str(budget.get("mode")) == str(manifest["caps"].get("budget_mode"))
        and int(budget.get("reasoning_reserve_tokens") or 0) > standard.reasoning_reserve_tokens
        and int(budget.get("plan_content_tokens") or 0) > 0
    )

    # -- synthesis validation routing -----------------------------------------
    synthesis = run.get("synthesis") if isinstance(run.get("synthesis"), Mapping) else {}
    probe = run.get("synthesis_probe") if isinstance(run.get("synthesis_probe"), Mapping) else {}
    synthesis_validated = bool(synthesis.get("ok"))
    invalid_routes = bool(
        probe.get("invalid_ok") is False
        and str(probe.get("invalid_recovery_kind")) == "re_synthesis"
        and probe.get("raw_unchanged")
    )

    # -- the carry-forward ------------------------------------------------------
    carry = run.get("carry_forward") if isinstance(run.get("carry_forward"), Mapping) else {}
    counts = carry.get("counts") if isinstance(carry.get("counts"), Mapping) else {}
    seam = (
        run.get("carry_forward_seam") if isinstance(run.get("carry_forward_seam"), Mapping) else {}
    )
    carry_populated = bool(
        int(counts.get("facts") or 0) >= 2
        and int(counts.get("questions") or 0) >= 1
        and bool(carry.get("verified_in_plan"))
    )

    return CustomerProfileGrade(
        plan_present=plan is not None,
        decisive_line_depth_found=bool(line_evidence),
        decisive_line_depth_evidence=tuple(line_evidence),
        decisive_second_page_found=bool(page_evidence)
        and not first_page_had_marker
        and bool(continuation_evidence),
        decisive_second_page_evidence=tuple(continuation_evidence or page_evidence),
        second_page_continuation_used=continuation_used and not first_page_had_marker,
        conflict_became_question=bool(conflict_questions),
        conflict_silent_pick=conflict_silent_pick,
        conflict_question=conflict_questions[0] if conflict_questions else "",
        budget_sink_signalled=sink_signalled,
        budget_sink_evidence={
            "stopped_reason": sink.get("stopped_reason"),
            "docs_observations": len(sink_observations),
            "classification": sink_verdict.get("classification"),
            "recovery": sink_recovery.get("kind"),
            "retained_findings": sink.get("retained_findings"),
            "complete_understanding_claimed": sink_verdict.get("complete_understanding_claimed"),
        },
        cache_reuse_on_repeat=cache_reuse,
        no_cross_scope_leak=no_leak,
        restart_resumption=restart,
        budget_profile_reserved=budget_reserved,
        synthesis_validated=synthesis_validated,
        synthesis_invalid_routes_to_recovery=invalid_routes,
        carry_forward_populated=carry_populated,
        carry_forward_seam_verified=bool(seam.get("verified")),
        write_scope_single_target=(plan or {}).get("write_targets") == [writable_key],
    )


# ---------------------------------------------------------------------------
# The qualification driver
# ---------------------------------------------------------------------------


async def capture_customer_profile(
    manifest: Mapping[str, Any], fixtures_root: Path
) -> dict[str, Any]:
    """Capture ONE deterministic customer-profile qualification run.

    Phases (all offline, all recorded): the scripted investigation over
    the nine-repo graph with reads minted through the planning-scope
    observation cache; the conflict lifted from what the run observed;
    the deterministic plan synthesis under schema validation; the
    budget-sink sub-investigation whose whole small budget the docs
    repository consumes; the cross-scope isolation probe; the restart
    resumption check; the budget-suited synthesis profile; the negative
    synthesis-validation probe; and the carry-forward with its
    brief-envelope seam.
    """
    files = customer_files(manifest, fixtures_root)
    repos = _customer_research_repos(files, manifest)
    caps = manifest["caps"]
    writable = next(entry for entry in manifest["repos"] if entry["role"] == ROLE_WRITABLE)
    profile = SystemContextProfile(
        writable=WritableTarget(
            provider="gitlab",
            repository_id=str(writable["repository_id"]),
            ref=str(writable["source_oid"]),
            allowed_globs=("**",),
        ),
        neighbors=tuple(
            NeighborRepository(
                provider="gitlab",
                repository_id=str(entry["repository_id"]),
                ref=str(entry["source_oid"]),
                allowed_globs=("**",),
            )
            for entry in manifest["repos"]
            if entry["role"] != ROLE_WRITABLE
        ),
    )
    authorized = AuthorizedRepoSet(profile)
    scope = planning_scope(authorized.digest)
    refusals: list[dict[str, Any]] = []
    cache = ObservationCache()

    # -- the main scripted investigation --------------------------------------
    snapshot = Snapshot(
        repos={
            key: {
                "repository_id": repo.repository_id,
                "source_oid": repo.source_oid,
                "files": dict(files[key]),
            }
            for key, repo in repos.items()
        }
    )
    turns = _customer_turns(manifest, files)
    script = ScriptedInvestigation(
        snapshot,
        turns=turns,
        input_tokens=CUST_SCRIPTED_INPUT_TOKENS,
        output_tokens=CUST_SCRIPTED_OUTPUT_TOKENS,
    )
    wrapped, proposals = _recording_complete(script)
    harness = ResearchHarness(
        complete=wrapped, max_calls=int(caps["max_calls"]), wall_seconds=float(caps["wall_seconds"])
    )
    outcome = await run_research_pass(
        harness,
        planner_input=str(manifest["task"]["statement"]),
        lexical=[],
        repos=repos,
        now=DeterministicClock(),
    )
    document = dict(outcome.document)
    document["findings"] = [
        {
            "evidence_id": f"ev-{index}",
            "repo_key": finding.repo_key,
            "path": finding.path,
            "line": finding.line,
            "kind": finding.kind,
            "detail": finding.detail,
        }
        for index, finding in enumerate(outcome.findings, start=1)
    ]

    # -- mint the observations through the authority gate + the cache ---------
    observations = [
        _mint_through_cache(
            call,
            repos=repos,
            files=files,
            authorized=authorized,
            cache=cache,
            policy_scope=scope,
            refusals=refusals,
        )
        for call in proposals
    ]
    observation_documents = [
        {
            "tool": observation.tool,
            "repo_key": observation.repo_key,
            "call": observation.call,
            "content": observation.content,
            "error": observation.error,
            "truncated": observation.truncated,
        }
        for observation in observations
    ]

    # -- the recovery ledger: the truncated page and its spent continuation ---
    ledger: list[dict[str, Any]] = []
    audit_dec = next(e["decisive"] for e in manifest["repos"] if e["key"] == CUST_AUDIT_KEY)
    page = int(audit_dec.get("page_chars") or SECOND_PAGE_CHARS)
    truncated_windows = [
        observation
        for observation in observation_documents
        if observation["truncated"] and observation["repo_key"] == CUST_AUDIT_KEY
    ]
    continuation_read = any(
        observation["repo_key"] == CUST_AUDIT_KEY
        and _observation_call_parts(observation)[1] >= page
        for observation in observation_documents
    )
    if truncated_windows:
        verdict = classify_investigation(
            InvestigationOutcome(declared_done=False, truncated_observations=len(truncated_windows))
        )
        ledger.append(
            {
                "phase": "after the truncated first-page read",
                "verdict": verdict.as_document(),
                "spent": continuation_read,
            }
        )

    # -- the conflict, from what the run observed ------------------------------
    conflict = _conflict_from_observations(manifest, observation_documents)

    # -- the plan synthesis, under schema validation ---------------------------
    plan = synthesize_customer_plan(
        manifest,
        files,
        observation_documents,
        document["findings"],
        conflict,
        summary=str(document.get("summary") or ""),
    )
    validation = validate_plan_synthesis(plan)
    # the negative probe: a corrupted copy must FAIL and route to the
    # bounded recovery, with the failed document kept unmodified
    corrupted = json.loads(json.dumps(plan))
    corrupted["claims"][0].pop("line_start")
    invalid = validate_plan_synthesis(corrupted)
    synthesis_probe = {
        "invalid_ok": invalid.ok,
        "invalid_errors": list(invalid.errors),
        "invalid_recovery_kind": invalid.recovery.kind,
        "raw_unchanged": "line_start" not in (invalid.raw or {}).get("claims", [{}])[0],
    }

    # -- the terminal taxonomy verdict over the main pass -----------------------
    final_outcome = InvestigationOutcome(
        declared_done=outcome.complete,
        stopped_reason=outcome.stopped_reason,
        synthesis=validation,
        conflicts=(conflict,) if conflict is not None else (),
    )
    final_verdict = classify_investigation(final_outcome)

    # -- the budget sink: the docs repo vs a deliberately small budget ----------
    sink_script = ScriptedInvestigation(
        snapshot,
        turns=_sink_turns(),
        input_tokens=CUST_SINK_INPUT_TOKENS,
        output_tokens=CUST_SINK_OUTPUT_TOKENS,
    )
    sink_wrapped, sink_proposals = _recording_complete(sink_script)
    sink_harness = ResearchHarness(
        complete=sink_wrapped,
        max_calls=int(caps["sink_max_calls"]),
        wall_seconds=float(caps["wall_seconds"]),
    )
    sink_outcome = await run_research_pass(
        sink_harness,
        planner_input=str(manifest["task"]["statement"]),
        lexical=[],
        repos={CUST_DOCS_KEY: repos[CUST_DOCS_KEY]},
        now=DeterministicClock(),
    )
    sink_observations = [
        _execute_gated(call, repos, authorized, refusals) for call in sink_proposals
    ]
    sink_verdict = classify_investigation(
        InvestigationOutcome(
            declared_done=sink_outcome.complete,
            stopped_reason=sink_outcome.stopped_reason,
        )
    )
    budget_sink = {
        "budget": int(caps["sink_max_calls"]),
        "calls_proposed": sink_outcome.document.get("calls_proposed"),
        "stopped_reason": sink_outcome.stopped_reason,
        "complete": sink_outcome.complete,
        "retained_findings": len(sink_outcome.findings),
        "repos_consulted": list(sink_outcome.document.get("repos_consulted") or []),
        "verdict": sink_verdict.as_document(),
        "observations": [
            {
                "tool": observation.tool,
                "repo_key": observation.repo_key,
                "call": observation.call,
                "content": observation.content[:400],
                "error": observation.error,
                "truncated": observation.truncated,
            }
            for observation in sink_observations
        ],
    }

    # -- the cross-scope isolation probe ----------------------------------------
    billing_oid = str(
        next(e["source_oid"] for e in manifest["repos"] if e["key"] == CUST_BILLING_KEY)
    )
    billing_path = str(
        next(e["decisive"]["path"] for e in manifest["repos"] if e["key"] == CUST_BILLING_KEY)
    )
    reads_before_probe = cache.underlying_reads
    cache.observe(
        lambda p: files[CUST_BILLING_KEY][p],
        repository=CUST_BILLING_KEY,
        source_oid=billing_oid,
        path=billing_path,
        policy_scope=review_scope(authorized.digest),
    )
    isolated_scope_missed = cache.underlying_reads == reads_before_probe + 1
    try:
        cache.assert_no_cross_scope_leak()
        cross_scope_isolated = True
    except AssertionError:
        cross_scope_isolated = False
    scope_probe = {
        "source": f"{CUST_BILLING_KEY}:{billing_path}",
        "planning_entry": cache.get(CUST_BILLING_KEY, billing_oid, billing_path, scope) is not None,
        "review_entry_served_fresh": isolated_scope_missed,
        "isolated_scope_missed": isolated_scope_missed,
        "scopes_of_source": list(cache.scopes_of(CUST_BILLING_KEY, billing_oid, billing_path)),
    }

    # -- the restart resumption: the cache rides the record ----------------------
    record: dict[str, Any] = {"discovery_id": f"{manifest['task']['task_id']}-scripted"}
    attach_observation_cache(record, cache)
    restored = cache_from_record(record)
    restart_hits = 0
    if restored is not None:
        reads_before_restart = restored.underlying_reads
        repeat = restored.observe(
            lambda p: files[CUST_BILLING_KEY][p],
            repository=CUST_BILLING_KEY,
            source_oid=billing_oid,
            path=billing_path,
            policy_scope=scope,
        )
        restart_hits = restored.hits
        restart_resumed = (
            repeat.policy_scope == scope and restored.underlying_reads == reads_before_restart
        )
    else:  # pragma: no cover - the cache was attached above
        restart_resumed = False

    # -- the budget-suited synthesis profile -------------------------------------
    budget = budget_profile_for(str(caps["budget_mode"]))

    # -- the carry-forward and the brief-envelope seam ----------------------------
    carry = carry_forward(
        plan, discovery_id=str(record["discovery_id"]), snapshot_digest=authorized.digest
    )
    section = render_carry_forward_section(carry)
    plan_text = json.dumps(plan, indent=2, sort_keys=True) + "\n\n" + section
    seam = brief_envelope_seam(
        carry,
        plan_text,
        run_id=str(record["discovery_id"]),
        task_title=str(manifest["task"]["task_id"]),
        task_description=str(manifest["task"]["statement"]),
        spec_digest=customer_manifest_digest(manifest),
    )

    cache_document = cache.as_document()
    cache_document["repeat_hit"] = cache.hits >= 1
    cache_document["cross_scope_isolated"] = cross_scope_isolated
    cache_document["scope_probe"] = scope_probe
    cache_document["restart_resumed"] = restart_resumed
    cache_document["restart_hits"] = restart_hits

    run_document = {
        "schema": CUSTOMER_RUN_SCHEMA,
        "run_id": f"{manifest['task']['task_id']}-scripted",
        "task_id": manifest["task"]["task_id"],
        "mode": _MODE_SCRIPTED,
        "capture": {
            "provenance": PROVENANCE_OFFLINE_SCRIPTED,
            "model_identity": "scripted:customer-profile-investigation/v1",
            "route": "offline-scripted-over-real-tool-loop",
            "live_provider": False,
            "captured_at": manifest["registered_at"],
            "harness": "run_research_pass + cached observation mint + plan synthesis",
            "issue": manifest["issue"],
        },
        "manifest": {
            "digest": customer_manifest_digest(manifest),
            "authorized_repo_set_digest": authorized.digest,
            "repos": len(manifest["repos"]),
        },
        "repos": [dict(entry) for entry in manifest["repos"]],
        "research_document": document,
        "observations": observation_documents,
        "authority": {
            "authorized_repo_set_digest": authorized.digest,
            "read_keys": list(authorized.read_keys),
            "model_refusals": refusals,
        },
        "recovery_ledger": ledger,
        "conflict": conflict.as_document() if conflict is not None else None,
        "plan": plan,
        "synthesis": validation.as_document(),
        "synthesis_probe": synthesis_probe,
        "final_verdict": final_verdict.as_document(),
        "budget_sink": budget_sink,
        "observation_cache": cache_document,
        "budget_profile": budget.as_document(),
        "carry_forward": {
            **carry.as_document(),
            "verified_in_plan": verify_carry_forward(carry, plan_text),
        },
        "carry_forward_seam": seam,
        "write_scope": review_write_proposals(
            profile, [("gitlab", str(writable["repository_id"]))]
        ).as_document(),
        "stopped_reason": outcome.stopped_reason,
    }
    run_document["grade"] = grade_customer_profile(run_document, manifest, files).as_document()
    return run_document


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _existing_runs(out_dir: Path) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for mode in (_MODE_SCRIPTED, _MODE_LIVE):
        path = out_dir / "runs" / f"{mode}.json"
        if path.exists():
            runs[mode] = json.loads(path.read_text(encoding="utf-8"))
    return runs


def _manifest_files(manifest: Mapping[str, Any], fixtures_root: Path) -> dict[str, dict[str, str]]:
    """The frozen snapshot bytes of every manifest repository."""
    return {
        str(entry["key"]): _load_fixture_files(fixtures_root, str(entry["fixture"]))
        for entry in manifest["repos"]
    }


async def _run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    if args.init_manifest:
        target = out_dir / "manifest.json"
        if target.exists() and not args.force:
            raise SystemExit(
                f"{target} already exists — pass --force to re-register (a NEW record)"
            )
        _write_json(target, build_manifest_document(out_dir))
        print(f"manifest written to {target}")
        return 0

    if args.init_customer_manifest:
        target = out_dir / CUSTOMER_MANIFEST_FILENAME
        if target.exists() and not args.force:
            raise SystemExit(
                f"{target} already exists — pass --force to re-register (a NEW record)"
            )
        _write_json(target, build_customer_manifest_document(out_dir))
        print(f"customer manifest written to {target}")
        return 0

    if args.profile:
        manifest_path = (
            Path(args.manifest).resolve() if args.manifest else out_dir / CUSTOMER_MANIFEST_FILENAME
        )
        manifest = load_customer_manifest(manifest_path)
        document = await capture_customer_profile(manifest, _customer_fixture_root(manifest_path))
        _write_json(out_dir / "profile-run.json", document)
        grade = document["grade"]
        print(f"customer profile run written to {out_dir / 'profile-run.json'}")
        # negative arms (a False verdict is the PASS) print inverted
        negative_arms = {"conflict_silent_pick"}
        for arm, verdict in grade.items():
            if isinstance(verdict, bool):
                ok = (not verdict) if arm in negative_arms else verdict
                print(f"  {'PASS' if ok else 'FAIL'}  {arm}")
        print(f"customer profile: passed={grade['passed']} failed={grade['failed_checks']}")
        return 0 if grade["passed"] else 1

    manifest = load_manifest(out_dir)
    if args.scripted:
        document = await capture_discovery_run(manifest, out_dir, _MODE_SCRIPTED)
        _write_json(out_dir / "runs" / "scripted.json", document)
        print(
            f"scripted capture: grade passed={document['grade']['passed']} "
            f"failed={document['grade']['failed_checks']}"
        )
    if args.live:
        live_path = out_dir / "runs" / "live.json"
        if live_path.exists() and not args.force_live:
            raise SystemExit(
                f"{live_path} already exists — a live attempt is made ONCE; pass "
                "--force-live to supersede it with a new recorded attempt"
            )
        env = dict(os.environ)
        if args.gateway_url:
            env[FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV] = args.gateway_url
        if args.gateway_model:
            env[FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV] = args.gateway_model
        if args.gateway_tier:
            env[FORGE_RESEARCH_LIVE_GATEWAY_TIER_ENV] = args.gateway_tier
        try:
            gateway = resolve_live_gateway(env)
        except CohortSpecError as exc:
            document = refused_live_run(
                manifest,
                str(exc),
                {
                    "env_checked": [
                        FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV,
                        FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV,
                    ]
                },
            )
        else:
            if gateway is None:
                document = refused_live_run(
                    manifest,
                    f"no live gateway configured ({FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV} unset) — "
                    "the capture honestly stays offline-scripted",
                    {"env_checked": [FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV]},
                )
            else:
                reachability = await _check_gateway_reachable(gateway)
                if not reachability["reachable"]:
                    document = refused_live_run(
                        manifest,
                        "the lab gateway is unreachable — refusing to fabricate live provenance",
                        {
                            "gateway": {"base_url": gateway.base_url, "model": gateway.model},
                            **reachability,
                        },
                    )
                else:
                    document = await capture_discovery_run(
                        manifest, out_dir, _MODE_LIVE, env=env, gateway=gateway
                    )
        _write_json(live_path, document)
        if document.get("outcome") == "refused":
            print(f"live capture REFUSED: {document['refusal_reason']}")
        else:
            print(
                f"live capture: grade passed={document['grade']['passed']} "
                f"failed={document['grade']['failed_checks']} "
                f"spend=${document['cost']['usd_estimated']}"
            )
    if args.scripted or args.live or args.report_only:
        report = build_report(manifest, _existing_runs(out_dir), _manifest_files(manifest, out_dir))
        _write_json(out_dir / "report.json", report)
        print(f"report written to {out_dir / 'report.json'}")
        return 0
    print("nothing to do — pass --scripted and/or --live (or --init-manifest)")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", default="evaluation/discovery_live", help="the evaluation directory"
    )
    parser.add_argument("--init-manifest", action="store_true", help="register the frozen manifest")
    parser.add_argument(
        "--init-customer-manifest",
        action="store_true",
        help="register the customer-scale profile manifest (R38-11)",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="run the deterministic customer-scale planning-profile qualification",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="the customer manifest path for --profile (default: <out>/manifest-customer-v1.json)",
    )
    parser.add_argument("--force", action="store_true", help="allow re-registering the manifest")
    parser.add_argument("--scripted", action="store_true", help="capture the offline-scripted arm")
    parser.add_argument("--live", action="store_true", help="attempt ONE live gateway capture")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="rebuild the report from the recorded runs (grades re-derived)",
    )
    parser.add_argument("--force-live", action="store_true", help="supersede an existing live run")
    parser.add_argument("--gateway-url", default="", help="override the gateway URL env var")
    parser.add_argument("--gateway-model", default="", help="override the gateway model env var")
    parser.add_argument("--gateway-tier", default="", help="override the gateway tier env var")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
