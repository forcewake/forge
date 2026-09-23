"""Promote release artifacts only from qualified profile evidence (R32-19).

The release manifest (:mod:`forge.release_manifest`) states which evidence
exists per capability; THIS module is the promotion-side contract — the
gate that decides whether the mutable release tags may move onto a digest.
Research basis: ``docs/research/2026-09-23-e2e-qualification/``
``03-evidence-based-capability-qualification.md``:

- Qualification is a QUERY over recorded evidence, not a re-run at promotion
  time. ``evaluate_promotion`` consumes check results and canary outcomes
  that were collected EARLIER (the CI runs, the canary stage of the release
  workflow) — the gate never re-executes a suite.
- Evidence is tagged with stable requirement ids AT COLLECTION TIME. Canary
  stage outcomes carry the manifest capability slug they qualify, so the
  gaps query can join promotions back onto capability claims.
- Failure history is retained, never overwritten: a check that failed and
  then passed on a retry records BOTH attempts and earns ``conditional_pass``
  — it is never silently green.
- Fail-closed: a FAILED required check blocks promotion even when the boot
  canary passed, and a required check with no recorded result (unexecuted,
  cancelled, still running) blocks as well, with the check's provenance named
  in the reason. One blocked row wins the argument regardless of greens.

Archive discipline (the change from "regenerate per release"): every
promotion lands its evidence under ``docs/releases/evidence/v<version>/``
as a COMMITTED, immutable artifact — the manifest snapshot plus the
promotion record (``archive_release_evidence``). ``scripts/
generate_template_pins.py`` renders the README version pins FROM that
archive, so docs can only cite digests that qualified.

CLI::

    python -m forge.release_promotion gate --image-ref ... --digest sha256:... \
        --version 0.34.0 --head-sha ... --ci-run-id ... \
        --checks-from-github owner/repo@SHA --canary-json canary.json
    python -m forge.release_promotion gaps [--version 0.34.0]
    python -m forge.release_promotion archive --version 0.34.0

``gate`` exits non-zero on a blocked promotion (the workflow's tag-attach
job never runs), zero on promote / conditional_promote.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

__all__ = [
    "PROMOTION_STAMP",
    "QUALIFICATION_CLASSES",
    "REQUIRED_CHECKS",
    "CanaryResult",
    "CheckAttempt",
    "CheckSpec",
    "FileIdentity",
    "PromotionIntegrityError",
    "PromotionRecord",
    "PromotionDecision",
    "QualificationGap",
    "RequiredCheck",
    "WheelIdentity",
    "archive_release_evidence",
    "evaluate_promotion",
    "fetch_check_attempts",
    "group_attempts",
    "latest_promotion_record",
    "load_promotion_records",
    "qualification_gaps",
    "render_json",
]

#: Versioned stamp of the promotion-record document. Bump when the shape
#: changes in a way readers must distinguish.
PROMOTION_STAMP: Final[str] = "forge.release.promotion/1"

#: Evidence classes whose claims need live/release qualification evidence —
#: the classes the gaps query treats as "requires qualification".
QUALIFICATION_CLASSES: Final[frozenset[str]] = frozenset({"boot_canary", "real_provider_e2e"})

#: Conclusions that prove a required check PASSED (GitHub check-run
#: vocabulary; "pass" is kept for hand-built records).
_PASSING: Final[frozenset[str | None]] = frozenset({"success", "pass"})
#: Conclusions that prove a required check FAILED — the latest of these
#: blocks promotion even when the canary is green.
_FAILING: Final[frozenset[str | None]] = frozenset(
    {"failure", "fail", "timed_out", "cancelled", "startup_failure"}
)
#: Everything else (None, "skipped", "stale", "neutral", ...) is UNKNOWN —
#: fail-closed: an unexecuted check can never promote a release.


@dataclass(frozen=True)
class CheckSpec:
    """A required check: explicit, provenance-bound, never a wildcard.

    ``name`` is the GitHub check-run name (matrix job checks are distinct
    names, e.g. ``test (3.13)``); ``provenance`` is the workflow file and
    job that MUST produce it — named in block reasons so the operator knows
    where to look, and so a name collision from another workflow can be
    spotted.
    """

    name: str
    provenance: str


#: The qualification profile: every check a release sha must have PASSED
#: before its digest may carry the mutable tags. Explicit names only — a
#: wildcard here would silently qualify whatever happened to run.
REQUIRED_CHECKS: Final[tuple[CheckSpec, ...]] = (
    CheckSpec(name="lint", provenance=".github/workflows/ci.yml#jobs.lint"),
    CheckSpec(name="typecheck", provenance=".github/workflows/ci.yml#jobs.typecheck"),
    CheckSpec(name="test (3.13)", provenance=".github/workflows/ci.yml#jobs.test[3.13]"),
    CheckSpec(name="test (3.14)", provenance=".github/workflows/ci.yml#jobs.test[3.14]"),
    CheckSpec(name="integration", provenance=".github/workflows/ci.yml#jobs.integration"),
)


@dataclass(frozen=True)
class CheckAttempt:
    """One recorded execution of a required check (a check-run row).

    ``conclusion`` uses the GitHub check-run vocabulary; ``run_id`` ties the
    attempt to the workflow run that produced it. A FAILED attempt is kept
    even after a later retry passes — the failure is history, not noise.
    """

    conclusion: str | None
    run_id: str | None = None
    completed_at: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "conclusion": self.conclusion,
            "run_id": self.run_id,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class RequiredCheck:
    """A required check plus its recorded attempts, oldest first.

    Retry semantics live here: attempts are append-only. The verdict (see
    :func:`evaluate_promotion`) derives from ALL of them —
    failed-then-passed is ``conditional_pass``, passed-then-failed is a
    block, and no attempts at all is fail-closed.
    """

    name: str
    provenance: str
    attempts: tuple[CheckAttempt, ...] = ()

    @classmethod
    def from_result(
        cls, name: str, provenance: str, result: str | None, run_id: str | None = None
    ) -> RequiredCheck:
        """Single-attempt constructor — the common recorded shape."""
        return cls(name=name, provenance=provenance, attempts=(CheckAttempt(result, run_id),))

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provenance": self.provenance,
            "attempts": [attempt.to_json() for attempt in self.attempts],
        }


@dataclass(frozen=True)
class CanaryResult:
    """One canary stage outcome, tagged with the capability it qualifies.

    The capability slug is the manifest requirement id, attached AT
    COLLECTION TIME (the canary script writes it), so promotion records can
    be joined back onto capability claims without re-deriving anything at
    promotion time. ``outcome`` is ``pass`` | ``skip`` | ``fail`` — a skip
    is recorded as a skip, never counted as a pass.
    """

    stage: str
    capability: str
    outcome: str
    detail: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "capability": self.capability,
            "outcome": self.outcome,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PromotionDecision:
    """The gate's verdict: promote | conditional_promote | block.

    ``verdict`` is derived (never hand-set) from the recorded evidence:
    any failed/not-executed/unknown required check, or any failed canary
    stage, blocks — regardless of every other green. Retry-passed checks
    and skipped canary stages keep the verdict honest as
    ``conditional_promote``: the gap stays visible, it is never washed into
    a plain promote.
    """

    verdict: Literal["promote", "conditional_promote", "block"]
    reasons: tuple[str, ...] = ()
    check_verdicts: tuple[tuple[str, str, str], ...] = ()  # (name, verdict, reason)

    @property
    def qualified(self) -> bool:
        """True when the tags may move (promote or conditional_promote)."""
        return self.verdict in {"promote", "conditional_promote"}

    def to_json(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "checks": [
                {"name": name, "verdict": verdict, "reason": reason}
                for name, verdict, reason in self.check_verdicts
            ],
        }


@dataclass(frozen=True)
class FileIdentity:
    """A wheel/sdist identity: exact filename plus its sha256."""

    name: str
    sha256: str

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "sha256": self.sha256}


@dataclass(frozen=True)
class WheelIdentity:
    """The Python-artifact half of the promotion (image digest is the other).

    The release pipeline builds images only; when no wheel/sdist exists for
    a release the fields stay ``None`` and ``note`` says so — unknown stays
    unknown, never zero-filled.
    """

    sdist: FileIdentity | None = None
    wheel: FileIdentity | None = None
    note: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "sdist": self.sdist.to_json() if self.sdist else None,
            "wheel": self.wheel.to_json() if self.wheel else None,
            "note": self.note,
        }


@dataclass(frozen=True)
class PromotionRecord:
    """Everything the promotion binds, under a versioned stamp.

    The record names the EXACT artifacts promoted — image digest, wheel
    identity — plus the qualifying CI run and head sha, the per-check
    results with provenance, the canary stage outcomes, the derived
    decision and its timestamp. Different digest or wheel => different
    record; nothing here is inferred at read time.

    Q35-08 lane-artifact identity (additive): ``wheel_sha256`` /
    ``sdist_sha256`` and ``wheel_url`` / ``sdist_url`` name the AUTHORITATIVE
    lane distribution — the published locked wheel set — so target templates
    can pin install defaults straight from the archived record. The fields
    are additive on purpose: records archived before lanes shipped with a
    wheel set (v0.33.0/v0.34.0 were image-only) carry none of them, parse
    to ``None`` and stay honestly "not built" — exactly like the
    image-only note — never zero-filled.
    """

    version: str
    image_ref: str
    image_digest: str
    wheel: WheelIdentity = field(default_factory=WheelIdentity)
    wheel_sha256: str | None = None
    sdist_sha256: str | None = None
    wheel_url: str | None = None
    sdist_url: str | None = None
    ci_run_id: str | None = None
    head_sha: str | None = None
    required_checks: tuple[RequiredCheck, ...] = ()
    canary: tuple[CanaryResult, ...] = ()
    decision: PromotionDecision = field(default_factory=PromotionDecision)
    promoted_at: str = ""
    stamp: str = PROMOTION_STAMP
    note: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "stamp": self.stamp,
            "version": self.version,
            "image_ref": self.image_ref,
            "image_digest": self.image_digest,
            "wheel": self.wheel.to_json(),
            "wheel_sha256": self.wheel_sha256,
            "sdist_sha256": self.sdist_sha256,
            "wheel_url": self.wheel_url,
            "sdist_url": self.sdist_url,
            "ci_run_id": self.ci_run_id,
            "head_sha": self.head_sha,
            "required_checks": [check.to_json() for check in self.required_checks],
            "canary": [result.to_json() for result in self.canary],
            "decision": self.decision.to_json(),
            "promoted_at": self.promoted_at,
            "note": self.note,
        }


@dataclass(frozen=True)
class QualificationGap:
    """One capability whose required qualification evidence is missing."""

    capability: str
    provider: str
    backend: str
    evidence_class: str
    reason: str

    def to_json(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "provider": self.provider,
            "backend": self.backend,
            "evidence_class": self.evidence_class,
            "reason": self.reason,
        }


class PromotionIntegrityError(Exception):
    """The promotion evidence is unusable (bad shape, missing archive, drift)."""


# ---------------------------------------------------------------------------
# The gate: a query over recorded evidence — never a re-run
# ---------------------------------------------------------------------------


def _check_verdict(check: RequiredCheck) -> tuple[str, str]:
    """(verdict, reason) for one required check over its attempts.

    - no attempts                        -> not_executed (fail-closed)
    - unknown conclusion anywhere latest -> unknown        (fail-closed)
    - latest attempt failed              -> fail           (blocks)
    - a failure earlier, latest passed   -> conditional_pass (never silent)
    - all attempts passed                -> pass
    """
    if not check.attempts:
        return (
            "not_executed",
            f"required check {check.name!r} ({check.provenance}) has no recorded result — "
            "unexecuted checks can never promote a release",
        )
    latest = check.attempts[-1]
    if latest.conclusion not in _PASSING:
        if latest.conclusion in _FAILING:
            return (
                "fail",
                f"required check {check.name!r} ({check.provenance}) FAILED in run "
                f"{latest.run_id or 'unknown'} — a red check blocks promotion even when "
                "the canary passed",
            )
        return (
            "unknown",
            f"required check {check.name!r} ({check.provenance}) concluded "
            f"{latest.conclusion!r} in run {latest.run_id or 'unknown'} — not a pass; "
            "fail-closed",
        )
    earlier = [a for a in check.attempts[:-1] if a.conclusion not in _PASSING]
    if earlier:
        runs = ", ".join(
            f"run {a.run_id or '?'} ({a.conclusion or 'no conclusion'})" for a in earlier
        )
        return (
            "conditional_pass",
            f"required check {check.name!r} ({check.provenance}) did not pass earlier — "
            f"{runs} — and passed on retry in run {latest.run_id or 'unknown'}: conditional "
            "pass, the earlier attempt(s) stay on record and are never silently green",
        )
    return "pass", f"required check {check.name!r} ({check.provenance}) passed"


def evaluate_promotion(
    required_checks: tuple[RequiredCheck, ...], canary: tuple[CanaryResult, ...]
) -> PromotionDecision:
    """Derive the promotion decision from recorded evidence.

    Blocking (verdict ``block``), in precedence order:

    - any required check whose latest attempt failed, was never executed,
      or concluded something other than a pass (cancelled / skipped /
      still running — unknown is fail-closed), provenance named in the
      reason — EVEN IF every canary stage passed;
    - any canary stage with outcome ``fail``;
    - no canary results at all (no boot evidence — nothing to promote on).

    A retry-passed check, or a canary stage that self-skipped (recorded as
    ``skip``, never counted as pass), downgrades the verdict to
    ``conditional_promote`` — the gap stays explicit, never silently green.
    """
    blocking: list[str] = []
    conditional: list[str] = []
    check_verdicts: list[tuple[str, str, str]] = []

    if not required_checks:
        blocking.append(
            "no required checks recorded — the qualification profile is empty, which "
            "means nothing was verified; fail-closed"
        )

    for check in required_checks:
        verdict, reason = _check_verdict(check)
        check_verdicts.append((check.name, verdict, reason))
        if verdict in {"fail", "not_executed", "unknown"}:
            blocking.append(reason)
        elif verdict == "conditional_pass":
            conditional.append(reason)

    if not canary:
        blocking.append(
            "no canary results recorded — the release artifact was never exercised; fail-closed"
        )
    for result in canary:
        if result.outcome == "fail":
            blocking.append(
                f"canary stage {result.stage!r} (capability {result.capability!r}) FAILED"
            )
        elif result.outcome == "skip":
            conditional.append(
                f"canary stage {result.stage!r} (capability {result.capability!r}) "
                "self-skipped — recorded as a skip, never counted as a pass"
            )
        elif result.outcome != "pass":
            blocking.append(
                f"canary stage {result.stage!r} (capability {result.capability!r}) has "
                f"unknown outcome {result.outcome!r} — fail-closed"
            )

    if blocking:
        return PromotionDecision(
            verdict="block", reasons=tuple(blocking), check_verdicts=tuple(check_verdicts)
        )
    if conditional:
        return PromotionDecision(
            verdict="conditional_promote",
            reasons=tuple(conditional),
            check_verdicts=tuple(check_verdicts),
        )
    return PromotionDecision(
        verdict="promote",
        reasons=("all required checks passed; canary stages passed",),
        check_verdicts=tuple(check_verdicts),
    )


# ---------------------------------------------------------------------------
# The gaps query: capabilities whose qualification evidence is missing
# ---------------------------------------------------------------------------


def qualification_gaps(
    manifest_entries: tuple[object, ...],
    promotion_records: tuple[PromotionRecord, ...],
    version: str | None = None,
) -> tuple[QualificationGap, ...]:
    """Capabilities claiming qualification evidence with no fresh record.

    In scope: every manifest entry whose ``evidence_class`` is a
    qualification class (:data:`QUALIFICATION_CLASSES` — the live classes a
    contract suite can never stand in for). Fresh evidence for a capability
    means: a promotion record whose decision qualified (promote /
    conditional_promote) AND whose canary results are TAGGED with that
    capability AND whose version is the version being checked. Everything
    else — no records, no tagged stage for the capability, a blocked record,
    or evidence only for an older version — is an explicit gap with the
    reason spelled out.
    """
    target = version
    if target is None and promotion_records:
        target = max(record.version for record in promotion_records)

    tagged: dict[str, PromotionRecord] = {}
    for record in promotion_records:
        if not record.decision.qualified:
            continue
        if target is not None and record.version != target:
            continue
        for result in record.canary:
            current = tagged.get(result.capability)
            if current is None or (record.promoted_at or "") >= (current.promoted_at or ""):
                tagged[result.capability] = record

    gaps: list[QualificationGap] = []
    for entry in manifest_entries:
        evidence_class = getattr(entry, "evidence_class", "")
        if evidence_class not in QUALIFICATION_CLASSES:
            continue
        capability = getattr(entry, "capability", "")
        record = tagged.get(capability)
        if record is not None:
            continue
        if not promotion_records:
            reason = "no promotion records archived — nothing qualifies any live capability"
        elif record is None and any(
            r.decision.qualified and result.capability == capability
            for r in promotion_records
            for result in r.canary
        ):
            older = sorted(
                {
                    r.version
                    for r in promotion_records
                    if r.decision.qualified
                    and any(result.capability == capability for result in r.canary)
                }
            )
            reason = (
                f"qualification evidence exists only for {', '.join(f'v{v}' for v in older)} — "
                f"stale for the version being checked" + (f" (v{target})" if target else "")
            )
        elif any(
            not r.decision.qualified and any(result.capability == capability for result in r.canary)
            for r in promotion_records
        ):
            reason = (
                "the promotion record tagging this capability was BLOCKED — its canary ran, "
                "its evidence does not qualify"
            )
        else:
            reason = "no promotion record tags canary evidence for this capability" + (
                f" at v{target}" if target else ""
            )
        gaps.append(
            QualificationGap(
                capability=capability,
                provider=getattr(entry, "provider", "*"),
                backend=getattr(entry, "backend", "*"),
                evidence_class=evidence_class,
                reason=reason,
            )
        )
    return tuple(gaps)


# ---------------------------------------------------------------------------
# Historical evidence: the committed archive under docs/releases/evidence/
# ---------------------------------------------------------------------------


def _evidence_dir(root: Path, version: str) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+([ab.rc]+\d*)?", version):
        raise PromotionIntegrityError(f"bad release version {version!r}")
    return root / "docs" / "releases" / "evidence" / f"v{version}"


def render_json(document: dict[str, object]) -> str:
    """Deterministic JSON: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def archive_release_evidence(
    version: str,
    root: Path,
    manifest: dict[str, object] | None = None,
    record: PromotionRecord | None = None,
    records: tuple[PromotionRecord, ...] = (),
    replace: bool = False,
) -> dict[str, Path]:
    """Write the committed evidence for *version*; immutable by default.

    Files (deterministic paths — the storage is the path, not a database):

    - ``docs/releases/evidence/v<version>/manifest.json``  — the manifest
      snapshot the release claims against;
    - ``docs/releases/evidence/v<version>/promotion.json``  — the promotion
      record (the qualifying one, or an explicit list under ``promotions``
      when several exist).

    Idempotent: writing byte-identical content is a no-op. Writing
    DIFFERENT content over an existing artifact raises (immutable storage)
    unless ``replace=True`` — a re-tagged release supersedes deliberately.
    """
    target = _evidence_dir(root, version)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    def _write(name: str, content: str) -> None:
        path = target / name
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                written[name] = path
                return
            if not replace:
                raise PromotionIntegrityError(
                    f"{path} already holds DIFFERENT evidence — release evidence is "
                    "immutable; pass replace=True only when superseding a re-tag"
                )
        path.write_text(content, encoding="utf-8")
        written[name] = path

    if manifest is not None:
        _write("manifest.json", render_json(manifest))
    if record is not None or records:
        document: dict[str, object]
        if record is not None:
            document = record.to_json()
        else:
            document = {"promotions": [r.to_json() for r in records]}
        _write("promotion.json", render_json(document))
    return written


def _record_from_json(document: dict[str, object]) -> PromotionRecord:
    stamp = document.get("stamp")
    if stamp != PROMOTION_STAMP:
        raise PromotionIntegrityError(f"promotion record stamp {stamp!r} != {PROMOTION_STAMP!r}")
    wheel_doc = document.get("wheel") or {}
    try:
        wheel = WheelIdentity(
            sdist=(
                FileIdentity(**wheel_doc["sdist"])
                if isinstance(wheel_doc.get("sdist"), dict)
                else None
            ),
            wheel=(
                FileIdentity(**wheel_doc["wheel"])
                if isinstance(wheel_doc.get("wheel"), dict)
                else None
            ),
            note=str(wheel_doc.get("note", "")),
        )
        checks = tuple(
            RequiredCheck(
                name=str(check["name"]),
                provenance=str(check["provenance"]),
                attempts=tuple(
                    CheckAttempt(
                        conclusion=attempt.get("conclusion"),
                        run_id=attempt.get("run_id"),
                        completed_at=str(attempt.get("completed_at", "")),
                    )
                    for attempt in check.get("attempts", ())
                ),
            )
            for check in document.get("required_checks", ())
        )
        canary = tuple(
            CanaryResult(
                stage=str(result["stage"]),
                capability=str(result["capability"]),
                outcome=str(result["outcome"]),
                detail=str(result.get("detail", "")),
            )
            for result in document.get("canary", ())
        )
        decision_doc = document.get("decision") or {}
        decision = PromotionDecision(
            verdict=decision_doc.get("verdict", "block"),  # type: ignore[arg-type]
            reasons=tuple(decision_doc.get("reasons", ())),
            check_verdicts=tuple(
                (str(c["name"]), str(c["verdict"]), str(c["reason"]))
                for c in decision_doc.get("checks", ())
            ),
        )
        return PromotionRecord(
            version=str(document["version"]),
            image_ref=str(document["image_ref"]),
            image_digest=str(document["image_digest"]),
            wheel=wheel,
            # Q35-08 additive lane-artifact identity: absent on pre-wheel
            # records (v0.33.0/v0.34.0) -> None = honestly not built.
            wheel_sha256=(str(document["wheel_sha256"]) if document.get("wheel_sha256") else None),
            sdist_sha256=(str(document["sdist_sha256"]) if document.get("sdist_sha256") else None),
            wheel_url=str(document["wheel_url"]) if document.get("wheel_url") else None,
            sdist_url=str(document["sdist_url"]) if document.get("sdist_url") else None,
            ci_run_id=document.get("ci_run_id"),
            head_sha=document.get("head_sha"),
            required_checks=checks,
            canary=canary,
            decision=decision,
            promoted_at=str(document.get("promoted_at", "")),
            note=str(document.get("note", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PromotionIntegrityError(f"malformed promotion record: {exc}") from exc


def load_promotion_records(root: Path) -> tuple[PromotionRecord, ...]:
    """Every archived promotion record, oldest version first.

    Reads ``docs/releases/evidence/v*/promotion.json``; each file holds one
    record (or a ``promotions`` list — retrospective multi-record files).
    A missing archive dir is simply no records, not an error.
    """
    base = root / "docs" / "releases" / "evidence"
    if not base.is_dir():
        return ()
    records: list[PromotionRecord] = []
    for path in sorted(base.glob("v*/promotion.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if "promotions" in document:
            records.extend(_record_from_json(doc) for doc in document["promotions"])
        else:
            records.append(_record_from_json(document))
    return tuple(records)


def latest_promotion_record(root: Path) -> PromotionRecord | None:
    """The archived record of the highest version (None when no archive)."""
    records = load_promotion_records(root)
    if not records:
        return None
    return max(records, key=lambda r: tuple(int(p) for p in r.version.split(".")[:3]))


# ---------------------------------------------------------------------------
# Collecting required-check attempts from the GitHub check-runs API
# ---------------------------------------------------------------------------

_CHECK_RUNS_URL = "https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100"


def group_attempts(payload: dict, names: tuple[str, ...]) -> dict[str, tuple[CheckAttempt, ...]]:
    """Group a check-runs API payload into per-name attempts, oldest first.

    Re-runs of a check appear as separate check-runs; they are grouped per
    name and ordered by completion (``completed_at``, then id) — so a
    failed-then-passed retry keeps BOTH attempts in order. Names outside
    *names* are ignored (the profile is explicit, never "whatever ran").
    """
    grouped: dict[str, list[tuple[str, int, CheckAttempt]]] = {name: [] for name in names}
    for run in payload.get("check_runs", ()):
        name = run.get("name")
        if name not in grouped:
            continue
        details_url = run.get("details_url") or ""
        match = re.search(r"/runs/(\d+)/", details_url)
        grouped[name].append(
            (
                str(run.get("completed_at") or ""),
                int(run.get("id") or 0),
                CheckAttempt(
                    conclusion=run.get("conclusion"),
                    run_id=match.group(1) if match else None,
                    completed_at=str(run.get("completed_at") or ""),
                ),
            )
        )
    return {
        name: tuple(attempt for _, _, attempt in sorted(rows, key=lambda row: (row[0], row[1])))
        for name, rows in grouped.items()
    }


def fetch_check_attempts(
    repo: str, sha: str, names: tuple[str, ...] = tuple(c.name for c in REQUIRED_CHECKS)
) -> dict[str, tuple[CheckAttempt, ...]]:
    """Required-check attempts for *sha* from the GitHub check-runs API.

    Uses ``GH_TOKEN``/``GITHUB_TOKEN`` when present. A name in the profile
    with no check-run on the sha simply maps to zero attempts — which the
    gate then treats fail-closed (not executed).
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    request = urllib.request.Request(  # noqa: S310 — fixed https:// api.github.com target
        _CHECK_RUNS_URL.format(repo=repo, sha=sha),
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError) as exc:
        raise PromotionIntegrityError(f"cannot read check-runs for {repo}@{sha}: {exc}") from exc
    return group_attempts(payload, names)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cmd_gate(args: argparse.Namespace) -> int:
    checks: tuple[RequiredCheck, ...]
    if args.checks_from_github:
        repo, _, sha = args.checks_from_github.partition("@")
        attempts_by_name = fetch_check_attempts(repo, sha)
        checks = tuple(
            RequiredCheck(
                name=spec.name,
                provenance=spec.provenance,
                attempts=attempts_by_name.get(spec.name, ()),
            )
            for spec in REQUIRED_CHECKS
        )
    else:
        raw = json.loads(Path(args.checks_json).read_text(encoding="utf-8"))
        checks = tuple(
            RequiredCheck(
                name=str(item["name"]),
                provenance=str(item.get("provenance", "<no provenance recorded>")),
                attempts=tuple(
                    CheckAttempt(
                        conclusion=attempt.get("conclusion"),
                        run_id=attempt.get("run_id"),
                        completed_at=str(attempt.get("completed_at", "")),
                    )
                    for attempt in item.get("attempts", ())
                ),
            )
            for item in raw
        )

    canary: tuple[CanaryResult, ...] = ()
    if args.canary_json:
        document = json.loads(Path(args.canary_json).read_text(encoding="utf-8"))
        canary = tuple(
            CanaryResult(
                stage=str(result["stage"]),
                capability=str(result["capability"]),
                outcome=str(result["outcome"]),
                detail=str(result.get("detail", "")),
            )
            for result in document.get("stages", ())
        )

    decision = evaluate_promotion(checks, canary)
    if args.sdist or args.wheel:
        wheel_note = args.wheel_note
    else:
        wheel_note = args.wheel_note or (
            "no wheel/sdist built by the release pipeline (image-only release)"
        )
    record = PromotionRecord(
        version=args.version,
        image_ref=args.image_ref,
        image_digest=args.digest,
        wheel=WheelIdentity(
            sdist=FileIdentity(args.sdist, args.sdist_sha256) if args.sdist else None,
            wheel=FileIdentity(args.wheel, args.wheel_sha256) if args.wheel else None,
            note=wheel_note,
        ),
        # Q35-08: the lane-artifact identity — the published wheel set the
        # target templates pin their install defaults from. URLs are given
        # explicitly by the release workflow (never derived at read time).
        wheel_sha256=args.wheel_sha256,
        sdist_sha256=args.sdist_sha256,
        wheel_url=args.wheel_url,
        sdist_url=args.sdist_url,
        ci_run_id=args.ci_run_id,
        head_sha=args.head_sha,
        required_checks=checks,
        canary=canary,
        decision=decision,
        promoted_at=_utc_now(),
        note=args.note,
    )

    rendered = render_json(record.to_json())
    sys.stdout.write(rendered)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
    if args.archive:
        archive_release_evidence(
            args.version,
            Path(args.archive_root),
            record=record,
            replace=args.replace,
        )

    if not decision.qualified:
        print(
            f"promotion-gate: BLOCKED ({len(decision.reasons)} reason(s)) — mutable tags "
            "must NOT move onto this digest",
            file=sys.stderr,
        )
        return 1
    print(
        f"promotion-gate: {decision.verdict.upper()} — mutable tags may move onto {args.digest}",
        file=sys.stderr,
    )
    return 0


def _cmd_gaps(args: argparse.Namespace) -> int:
    from .release_manifest import ENTRIES

    root = Path(args.root)
    records = load_promotion_records(root)
    gaps = qualification_gaps(ENTRIES, records, version=args.version)
    document = {
        "stamp": "forge.release.qualification-gaps/1",
        "version": args.version,
        "gaps": [gap.to_json() for gap in gaps],
    }
    sys.stdout.write(render_json(document))
    if gaps:
        print(
            f"qualification-gaps: {len(gaps)} capability(ies) without fresh qualification "
            "evidence — see the gaps above",
            file=sys.stderr,
        )
        return 1 if args.fail_on_gap else 0
    print(
        "qualification-gaps: every qualification-class capability has fresh evidence",
        file=sys.stderr,
    )
    return 0


def _cmd_archive(args: argparse.Namespace) -> int:
    root = Path(args.root)
    from .release_manifest import build_manifest, render_manifest

    manifest = build_manifest(root)
    record = latest_promotion_record(root)
    if record is None or record.version != args.version:
        raise PromotionIntegrityError(
            f"no archived promotion record for v{args.version} — run the gate first"
        )
    manifest_doc = json.loads(render_manifest(manifest))
    written = archive_release_evidence(
        args.version, root, manifest=manifest_doc, record=record, replace=args.replace
    )
    for name, path in sorted(written.items()):
        print(f"archive: wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m forge.release_promotion",
        description="Promote release artifacts only from qualified profile evidence (R32-19).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gate = sub.add_parser("gate", help="evaluate a promotion from recorded evidence")
    gate.add_argument("--version", required=True)
    gate.add_argument("--image-ref", required=True, help="e.g. ghcr.io/forcewake/forge")
    gate.add_argument("--digest", required=True, help="the exact image digest sha256:...")
    gate.add_argument("--ci-run-id", default=None, help="the qualifying workflow run id")
    gate.add_argument("--head-sha", default=None)
    gate.add_argument(
        "--checks-from-github",
        default=None,
        metavar="OWNER/REPO@SHA",
        help="collect required-check attempts from the GitHub check-runs API",
    )
    gate.add_argument(
        "--checks-json",
        default=None,
        help="JSON file: [{name, provenance, attempts: [{conclusion, run_id}]}]",
    )
    gate.add_argument(
        "--canary-json",
        default=None,
        help="canary results JSON written by scripts/canary_smoke.py --results-json",
    )
    gate.add_argument("--sdist", default=None, help="sdist filename")
    gate.add_argument("--sdist-sha256", default=None)
    gate.add_argument("--wheel", default=None, help="wheel filename")
    gate.add_argument("--wheel-sha256", default=None)
    gate.add_argument(
        "--wheel-url",
        default=None,
        help="published URL of the wheel asset (the lane install's default route, Q35-08)",
    )
    gate.add_argument(
        "--sdist-url",
        default=None,
        help="published URL of the sdist asset (recorded identity, Q35-08)",
    )
    gate.add_argument("--wheel-note", default="")
    gate.add_argument("--out", default=None, help="also write the record JSON here")
    gate.add_argument(
        "--archive",
        action="store_true",
        help="write the record to docs/releases/evidence/v<version>/promotion.json",
    )
    gate.add_argument("--archive-root", default=".", type=Path)
    gate.add_argument("--replace", action="store_true", help="supersede an existing record")
    gate.add_argument("--note", default="")
    gate.set_defaults(func=_cmd_gate)

    gaps = sub.add_parser("gaps", help="the qualification gaps query (doctor-style export)")
    gaps.add_argument("--root", default=".", type=Path)
    gaps.add_argument("--version", default=None, help="version to check freshness against")
    gaps.add_argument("--fail-on-gap", action="store_true")
    gaps.set_defaults(func=_cmd_gaps)

    archive = sub.add_parser("archive", help="commit the release evidence snapshot")
    archive.add_argument("--version", required=True)
    archive.add_argument("--root", default=".", type=Path)
    archive.add_argument("--replace", action="store_true")
    archive.set_defaults(func=_cmd_archive)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PromotionIntegrityError as exc:
        print(f"release-promotion: REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
