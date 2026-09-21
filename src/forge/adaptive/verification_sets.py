"""Verification execution as a separate, trusted lane (VER epic core).

The coding agent's environment is privileged: it holds write scopes,
credentials and the model's own tooling. Verification that runs THERE
verifies nothing — an implementation grading its own homework is a
claim, not a result. Every helper in this module exists to keep
verification EXECUTION separate and its evidence honest:

- :class:`VerificationLane` / :func:`select_lane` — WHERE checks run
  (a separate trusted executor, never the coding agent) and how heavy
  that executor must be (real DB/broker probes vs pure contract shape).
- :func:`focused_recipe` — baselines plus ONLY the impacted changed
  set; unrelated changed repositories are explicitly skipped, never
  silently run or silently dropped.
- :func:`contract_checks` — HTTP and message contract descriptors;
  an entry without a machine-checkable expectation is flagged
  ``must_verify`` instead of passing on shape alone.
- :func:`db_upgrade_plan` — upgrades verify from a data-bearing
  baseline (an empty schema proves nothing).
- :func:`async_failure_scenarios` — the fixed crash/redelivery/order
  catalog asynchronous semantics must survive.
- :func:`environment_compose` — the integration environment binds to
  THIS CandidateSet's OIDs and digests, not to whatever is checked out.
- :class:`VerificationSelector` / :func:`selector_matches` /
  :func:`freshness` — checks are selected by IDENTITY (workflow, job,
  event, ref), never display name, and only recent enough evidence
  counts.
- :func:`evidence_aware_review` — a claim is supported only when EVERY
  piece of evidence behind it verified; implementation claims never
  count as their own proof.

Pure stdlib and frozen throughout: verification recipes are pinned into
run records and must never mutate under a running run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from forge.adaptive.models import CandidateSet

__all__ = [
    "RECIPE_SCHEMA",
    "VerificationLane",
    "VerificationSelector",
    "async_failure_scenarios",
    "contract_checks",
    "db_upgrade_plan",
    "environment_compose",
    "evidence_aware_review",
    "focused_recipe",
    "freshness",
    "select_lane",
    "selector_matches",
]

#: The schema discriminator every focused recipe carries (versioned: a
#: breaking change to the recipe's meaning bumps the tag).
RECIPE_SCHEMA = "forge.verify.recipe/1"

#: The executor profiles. The integration profile runs repository code
#: against real services; the contract-only profile executes no
#: repository code at all and must not be granted the right to.
INTEGRATION_PROFILE = "trusted-integration-v1"
CONTRACT_ONLY_PROFILE = "contract-only-v1"


def _short_id(payload: object) -> str:
    """A short deterministic id for a check/contract payload.

    Results bind to the exact contract content, not to a display name
    (names are edited and duplicated; content digests are not).
    """

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class VerificationLane:
    """WHERE a set of checks executes.

    Verification runs in a SEPARATE trusted executor — never inside
    the coding agent's privileged environment. The agent that wrote
    the code cannot grade it: anything executed with the same process,
    credentials and incentives that produced the change is an
    implementation claim, not a verification result.

    ``profile`` names the trusted executor profile; ``runs_code``
    records whether the lane executes repository code at all (a
    contract-only lane does not, and must not be asked to).
    """

    lane_id: str
    profile: str = INTEGRATION_PROFILE
    runs_code: bool = True


def select_lane(check_ids: list[str], *, has_db: bool, has_broker: bool) -> VerificationLane:
    """Pick the executor lane for *check_ids*.

    Probes against real infrastructure (database, broker) select the
    heavier ``trusted-integration-v1`` profile — they need the actual
    services running. Pure contract checks select
    ``contract-only-v1`` with ``runs_code=False``: shape validation
    executes no repository code, so it must not be granted an executor
    that can. The ``lane_id`` derives from the check ids alone, so the
    same checks always select the same lane — a stable identity to pin
    into run records.
    """

    lane_id = f"lane-{_short_id(sorted(check_ids))}"
    if has_db or has_broker:
        return VerificationLane(lane_id=lane_id, profile=INTEGRATION_PROFILE, runs_code=True)
    return VerificationLane(lane_id=lane_id, profile=CONTRACT_ONLY_PROFILE, runs_code=False)


def focused_recipe(candidate_set: CandidateSet, impacted: list[str]) -> dict:
    """Baseline + focused dependency recipe for one CandidateSet.

    Focused dependency tests run ONLY the impacted changed set. Every
    repository whose role is ``changed`` but which the impact analysis
    did NOT name is listed under ``skipped_unrelated`` — an explicit
    skip, never a silent run (wasted minutes) or a silent drop (an
    unexplained hole in the recipe). Baselines run in full: they are
    the comparison substrate, not the suspect.
    """

    changed = [m.repository_id for m in candidate_set.members if m.role == "changed"]
    baselines = [m.repository_id for m in candidate_set.members if m.role == "baseline"]
    return {
        "schema": RECIPE_SCHEMA,
        "changed": changed,
        "baselines": baselines,
        "focused": sorted(set(impacted) & set(changed)),
        "skipped_unrelated": sorted(set(changed) - set(impacted)),
    }


def _check_descriptor(kind: str, entry: dict) -> dict:
    descriptor: dict = {"kind": kind, "id": _short_id({"kind": kind, **entry})}
    descriptor.update(entry)
    if "expected_status" not in entry:
        # No machine-checkable expectation → the check cannot pass on
        # its own; it demands consumer/provider verification.
        descriptor["must_verify"] = True
    return descriptor


def contract_checks(spec: dict) -> list[dict]:
    """Turn an HTTP/message contract spec into check descriptors.

    Each descriptor carries its ``kind``, a short deterministic ``id``
    over the entry's content, and the original fields. An entry
    without ``expected_status`` has no machine-checkable expectation,
    so it is flagged ``must_verify``: message contracts (which carry
    only a ``payload_schema``) need consumer/provider verification,
    not just a shape check — and so does an HTTP entry whose expected
    status was never stated.
    """

    checks: list[dict] = []
    for entry in spec.get("http", []):
        checks.append(_check_descriptor("http", entry))
    for entry in spec.get("messages", []):
        checks.append(_check_descriptor("message", entry))
    return checks


def db_upgrade_plan(baseline_schema: str, target_schema: str, synthetic_data: bool) -> dict:
    """Plan a REAL upgrade test between two schema versions.

    An empty schema proves nothing: migrations that only "succeed" on
    a blank database say nothing about the rows already in tables.
    The plan therefore verifies from a data-bearing baseline —
    snapshot it, apply the migrations, then verify the data that
    should have survived (synthetic seed data when real data cannot
    cross environments).
    """

    verification_step = "verify_synthetic_data" if synthetic_data else "verify_empty"
    return {
        "steps": ["snapshot_baseline", "apply_migrations", verification_step],
        "baseline": baseline_schema,
        "target": target_schema,
    }


def async_failure_scenarios() -> list[dict]:
    """The fixed asynchronous-failure catalog a change must survive.

    Exactly-once in-order delivery is a lie networks tell. The three
    ways reality deviates — the writer crashes BETWEEN committing and
    acknowledging (so the consumer sees a retry), a redelivery
    produces a duplicate, and events arrive out of order — are the
    scenarios integration verification replays, not hopes away.
    """

    return [
        {"name": "crash_between_commit_and_ack"},
        {"name": "redelivery_duplicate"},
        {"name": "out_of_order_events"},
    ]


def environment_compose(candidate_set: CandidateSet, services: list[str]) -> dict:
    """Compose the integration environment for THIS CandidateSet.

    The environment binds to the set's member OIDs and its environment
    profile digest — not to whatever happens to be checked out on the
    runner. A verification result is only meaningful for the exact set
    it ran against; loose binding is how "passed" stops meaning
    anything.
    """

    return {
        "members": {
            member.repository_id: member.candidate_oid
            for member in candidate_set.members
            if member.role == "changed"
        },
        "environment_profile_digest": candidate_set.environment_profile_digest,
        "services": services,
    }


#: The identity fields a selector can pin. Empty means wildcard; a
#: filled field is a demand for an EQUAL observed counterpart.
_SELECTOR_FIELDS = (
    "check_id",
    "workflow_identity",
    "job_identity",
    "event",
    "ref",
    "tested_revision",
)


@dataclass(frozen=True)
class VerificationSelector:
    """IDENTIFIES a check by stable identity, never its display name.

    Display names are edited, localized and duplicated; identities are
    not. A selector pins as much identity as it knows — an empty field
    is a wildcard, a filled field is a demand (the C-series lesson:
    act on identity, and refuse when identity cannot be established).
    """

    check_id: str
    workflow_identity: str = ""
    job_identity: str = ""
    event: str = ""
    ref: str = ""
    tested_revision: str = ""


def selector_matches(selector: VerificationSelector, observed: dict) -> bool:
    """Whether *observed* satisfies every filled field of *selector*.

    Empty selector fields are wildcards. A filled field must find an
    EQUAL counterpart in ``observed``: a missing observed key is NO
    match (absence of identity is not a match on it), and a differing
    value is a mismatch no plausible display name can repair.
    """

    for name in _SELECTOR_FIELDS:
        expected = getattr(selector, name)
        if not expected:
            continue
        if name not in observed or observed[name] != expected:
            return False
    return True


def _parse_timestamp(value: str) -> datetime:
    # ``Z`` is ISO 8601 but not accepted by fromisoformat before 3.11
    # semantics on every input we see; normalize it away.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def freshness(observed_at: str, now: str, max_age_s: int = 3600) -> str:
    """Classify evidence age: ``fresh``, ``stale`` or ``unknown``.

    Verification results decay — a suite that passed last week has
    said nothing about today's code. Timestamps that cannot be parsed
    or compared (garbage input, mixed aware/naive, or a result
    "observed" in the future, which means clock skew) return
    ``unknown`` rather than raising: an unjudgeable age must never
    crash the verifier, and must never silently count as fresh.
    """

    try:
        observed = _parse_timestamp(observed_at)
        current = _parse_timestamp(now)
        age_s = (current - observed).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return "unknown"
    if age_s < 0:
        return "unknown"
    return "fresh" if age_s <= max_age_s else "stale"


def evidence_aware_review(claims: list[dict], evidence: list[dict]) -> dict:
    """Split claims by whether their EVIDENCE actually verified.

    A claim lands in ``supported`` only when EVERY evidence id behind
    it verified. One failed piece — or a referenced id nobody recorded,
    which is an unverified claim in disguise — moves it to
    ``unsupported``. Claims with no evidence ids at all are
    ``unbacked``: implementation claims never count as their own
    proof, so the review never reports them as supported.
    """

    verified = {item["evidence_id"]: bool(item.get("verified")) for item in evidence}
    supported: list[str] = []
    unsupported: list[str] = []
    unbacked: list[str] = []
    for claim in claims:
        evidence_ids = claim.get("evidence_ids") or []
        if not evidence_ids:
            unbacked.append(claim["claim_id"])
        elif all(verified.get(evidence_id) for evidence_id in evidence_ids):
            supported.append(claim["claim_id"])
        else:
            unsupported.append(claim["claim_id"])
    return {"supported": supported, "unsupported": unsupported, "unbacked": unbacked}
