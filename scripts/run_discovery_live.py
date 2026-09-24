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

Usage::

    uv run python scripts/run_discovery_live.py --init-manifest --out evaluation/discovery_live/
    uv run python scripts/run_discovery_live.py --scripted --out evaluation/discovery_live/
    uv run python scripts/run_discovery_live.py --live --out evaluation/discovery_live/ \\
        [--gateway-url http://localhost:4000 --gateway-model fast]
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
