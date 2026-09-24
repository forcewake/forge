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
  THIS CandidateSet's OIDs, image artifacts and baseline identities —
  every member is pinned, and every launched service resolves to a
  recorded exact artifact or is flagged unresolved (never a mutable
  tag) — plus the set's tested-world digest.
- :func:`freeze_verified_world` / :func:`freeze_tested_world` /
  :func:`bound_tested_world_digest` /
  :func:`bound_applicability_digest` — the NXT-22 freeze-time binding:
  the world digests (and the pins/policies they cover) are PERSISTED on
  the candidate set at freeze time, so results bind to the digest that
  was recorded then, never to a call-time recomputation.
  :func:`freeze_tested_world` (R36-19) completes the freeze with the
  bundle/profile digests a whole tested world is composed of, so the
  persisted digest covers members AND images AND the contract/test
  bundles AND the environment profile in one identity.
- :func:`baseline_drift` / :class:`WorldInputDrift` — the R36-19
  ``verification.baseline_drift`` source: the NAMED applicability
  inputs that moved between two frozen worlds, flagging the review's
  exact arm (a rebuilt image under an UNCHANGED source SHA).
- :class:`DependencyIdentity` / :class:`EvidenceRecord` /
  :class:`EvidenceLedger` — the NXT-21 per-dependency applicability
  core: evidence records claim the EXACT member identities (plus test
  bundle, environment pins, policies) they cover, and
  :meth:`EvidenceLedger.invalidated_by` names precisely the records a
  single dependency change breaks — an unrelated member's move, or a
  plan-revision bump, invalidates NOTHING.
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
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from forge.adaptive.models import (
    UNRESOLVED_IMAGE_DIGEST,
    CandidateSet,
    CandidateSetMember,
    validate_image_digest,
)
from forge.adaptive.workpackage import applicability_digest as _applicability_digest_of
from forge.adaptive.workpackage import tested_world_digest as _tested_world_digest_of

__all__ = [
    "RECIPE_SCHEMA",
    "DependencyChange",
    "DependencyIdentity",
    "EnvironmentPinChange",
    "EvidenceLedger",
    "EvidenceRecord",
    "MemberChange",
    "TestBundleChange",
    "VerificationLane",
    "VerificationSelector",
    "WorldInputDrift",
    "async_failure_scenarios",
    "baseline_drift",
    "bound_applicability_digest",
    "bound_tested_world_digest",
    "contract_checks",
    "db_upgrade_plan",
    "describe_change",
    "environment_compose",
    "evidence_aware_review",
    "focused_recipe",
    "freeze_tested_world",
    "freeze_verified_world",
    "freshness",
    "member_identity",
    "record_evidence",
    "select_lane",
    "selector_matches",
    "world_identities",
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


def environment_compose(
    candidate_set: CandidateSet,
    services: list[str],
    *,
    service_pins: Mapping[str, str] | None = None,
    policy_refs: Iterable[str] | None = None,
) -> dict:
    """Compose the integration environment for THIS CandidateSet (NXT-25).

    The environment binds to the set's member identities — OIDs AND
    exact image artifacts, for changed AND baseline members (a drifted
    baseline is a different system under test, so baselines are pinned
    like anyone else) — not to whatever happens to be checked out on
    the runner. A verification result is only meaningful for the exact
    world it ran against; loose binding is how "passed" stops meaning
    anything.

    Every name in *services* must resolve to a recorded EXACT artifact:
    a member repository resolves to that member's ``image_digest``; an
    external dependency (postgres, kafka, …) resolves only through an
    explicit *service_pins* entry. A service that resolves to neither
    is listed with ``"unresolved": True`` and no digest — visible, not
    silently run as whatever a mutable tag happens to point at today
    (display names and mutable tags are out of scope as verification
    identity by decision; a pin VALUE that is a tag or bare name is
    refused outright, and a member still carrying the
    ``unresolved`` sentinel composes flagged, never faked). A pin that
    CONTRADICTS a member's artifact is refused: two different claimed
    artifacts for one launched thing is a caller error, not a compose
    choice.

    When the set carries a PERSISTED world (see
    :func:`freeze_verified_world`), the compose binds to it: the
    persisted pins/policies are the world, and composing against
    DIFFERENT ones is refused — the recorded digest would otherwise
    describe a world this compose did not launch. An unfrozen set
    keeps the call-time behaviour and computes
    :func:`~forge.adaptive.workpackage.tested_world_digest` over the
    pins/policies passed here.
    """

    member_artifacts = {member.repository_id: member for member in candidate_set.members}
    pins = dict(service_pins or {})
    for name, digest in sorted(pins.items()):
        # A pin VALUE must itself be an exact artifact: "latest" pinned
        # explicitly is still a mutable tag, still not an identity.
        validate_image_digest(digest, allow_unresolved=False)

    frozen = candidate_set.tested_world_digest is not None
    if frozen:
        persisted_pins = dict(candidate_set.environment_pins)
        if pins and pins != persisted_pins:
            raise ValueError(
                f"the set is frozen to different environment pins ({sorted(persisted_pins)}):"
                " one frozen set records one world"
            )
        pins = persisted_pins
        if policy_refs is not None and set(policy_refs) != set(candidate_set.policy_refs):
            raise ValueError(
                f"the set is frozen to different policy refs ({list(candidate_set.policy_refs)}):"
                " one frozen set records one world"
            )
        world_digest_value: str | None = candidate_set.tested_world_digest
    else:
        world_digest_value = None

    for name, digest in pins.items():
        member = member_artifacts.get(name)
        if member is not None and digest != member.image_digest:
            raise ValueError(
                f"service {name} is pinned to {digest} but member {name}'s exact artifact"
                f" is {member.image_digest}: one launched thing, one recorded artifact"
            )

    composed_services: list[dict] = []
    for name in services:
        member = member_artifacts.get(name)
        if member is not None and member.image_digest != UNRESOLVED_IMAGE_DIGEST:
            composed_services.append(
                {"service": name, "artifact_digest": member.image_digest, "source": "member"}
            )
        elif member is not None:
            # The member rides along without a recorded exact artifact
            # yet: flagged like any unresolved service, never faked.
            composed_services.append(
                {"service": name, "artifact_digest": None, "source": "member", "unresolved": True}
            )
        elif name in pins:
            composed_services.append(
                {"service": name, "artifact_digest": pins[name], "source": "pin"}
            )
        else:
            # No recorded exact artifact → the service cannot be part of a
            # verified world yet. Flag it; never invent or trust a tag.
            composed_services.append({"service": name, "artifact_digest": None, "unresolved": True})

    return {
        "members": {
            member.repository_id: {
                "candidate_oid": member.candidate_oid,
                "image_digest": member.image_digest,
                "role": member.role,
            }
            for member in sorted(candidate_set.members, key=lambda member: member.repository_id)
        },
        "environment_profile_digest": candidate_set.environment_profile_digest,
        "services": composed_services,
        "tested_world_digest": world_digest_value
        if world_digest_value is not None
        else _tested_world_digest_of(candidate_set, environment=pins, policy_refs=policy_refs),
    }


# ---------------------------------------------------------------------------
# Freeze-time world binding (NXT-22): results bind to the digest AT
# PERSISTENCE, not to whatever a later call would compute.
# ---------------------------------------------------------------------------


def freeze_verified_world(
    candidate_set: CandidateSet,
    *,
    environment_pins: Mapping[str, str] | None = None,
    policy_refs: Iterable[str] | None = None,
) -> CandidateSet:
    """Persist the world binding ON the set, AT FREEZE TIME (NXT-22).

    Computes the tested-world and applicability digests over the exact
    *environment_pins*/*policy_refs* and stores them — together with
    the pins and refs themselves — on the returned copy. A verification
    result then binds to :func:`bound_tested_world_digest`: the digest
    PERSISTED here, never a call-time recomputation against whatever
    the world looks like later (a naive later call without the same
    pins yields a DIFFERENT digest — that difference is the point).

    Re-freezing with DIFFERENT pins or refs is refused: one frozen set
    records one world, and a changed world is a NEW set, not an edit of
    the old one. Re-freezing with the same inputs is idempotent. Pin
    values must be exact ``algorithm:hex`` artifacts — a mutable tag
    pinned explicitly is still a mutable tag.
    """
    persisted_pins = dict(candidate_set.environment_pins)
    pins = dict(environment_pins or {}) or persisted_pins
    frozen_refs = set(candidate_set.policy_refs)
    refs = sorted(set(policy_refs or ()) or frozen_refs)
    if candidate_set.tested_world_digest is not None and (
        pins != persisted_pins or set(refs) != frozen_refs
    ):
        raise ValueError(
            "the set is already frozen to a different world"
            f" (pins {sorted(persisted_pins)}, policy refs {sorted(frozen_refs)}):"
            " one frozen set records one world"
        )

    world = _tested_world_digest_of(candidate_set, environment=pins, policy_refs=refs)
    applicability = _applicability_digest_of(candidate_set, environment=pins, policy_refs=refs)
    return CandidateSet.model_validate(
        {
            **candidate_set.model_dump(),
            "environment_pins": sorted(pins.items()),
            "policy_refs": refs,
            "tested_world_digest": world,
            "applicability_digest": applicability,
        }
    )


def bound_tested_world_digest(candidate_set: CandidateSet) -> str:
    """The tested-world digest PERSISTED at freeze time (NXT-22).

    Refuses an unfrozen set instead of recomputing: the whole point of
    persistence is that a result binds to the digest recorded when the
    world was frozen. A caller who wants the current-world digest of an
    unfrozen set may compute it — but must do so EXPLICITLY, and know
    it is a call-time value, not a binding.
    """
    if candidate_set.tested_world_digest is None:
        raise ValueError(
            "candidate set carries no persisted tested_world_digest:"
            " freeze_verified_world first — a binding must be recorded, not recomputed"
        )
    return candidate_set.tested_world_digest


def bound_applicability_digest(candidate_set: CandidateSet) -> str:
    """The applicability digest PERSISTED at freeze time (NXT-22)."""
    if candidate_set.applicability_digest is None:
        raise ValueError(
            "candidate set carries no persisted applicability_digest:"
            " freeze_verified_world first — a binding must be recorded, not recomputed"
        )
    return candidate_set.applicability_digest


# ---------------------------------------------------------------------------
# The COMPLETE tested-world freeze (R36-19): members AND their images AND
# the contract/test bundles AND the environment profile, one identity.
# ---------------------------------------------------------------------------

#: Bundle/profile digests are verification identity: a value must be a
#: lowercase 64-hex sha256 to enter the world digest (the same shape
#: :func:`forge.adaptive.workpackage.tested_world_digest` covers).
_HEX64 = re.compile(r"[0-9a-f]{64}")

#: The spelled absence of an optional world input (bundle digests, pins,
#: policy refs may legitimately be absent — drift reports say so instead
#: of comparing against an ambiguous empty string).
_ABSENT = "<absent>"
_UNSET = "<none>"


def _require_bundle_digest(name: str, value: str) -> str:
    if not _HEX64.fullmatch(value):
        raise ValueError(
            f"{name} must be a lowercase 64-hex sha256 to enter the tested-world digest: {value!r}"
        )
    return value


def freeze_tested_world(
    candidate_set: CandidateSet,
    *,
    contract_bundle_digest: str | None = None,
    test_bundle_digest: str | None = None,
    environment_profile_digest: str | None = None,
    environment_pins: Mapping[str, str] | None = None,
    policy_refs: Iterable[str] | None = None,
) -> CandidateSet:
    """Freeze the COMPLETE tested world (R36-19) and persist its digests.

    The world a system verification binds to is composed of EVERY
    applicability input: changed AND unchanged repository revisions, the
    EXACT image artifact of each (a rebuilt image under the same source
    SHA is a different world), the contract bundle, the test bundle, the
    environment profile, the external service pins and the policy refs.
    :func:`freeze_verified_world` persists pins/refs and the digests over
    whatever the set already carries; this entry COMPLETES that set with
    any bundle/profile digests the caller freezes at the same moment, so
    the persisted tested-world and applicability digests cover the full
    composition in one identity.

    Supplied digests fill MISSING fields only: replacing a digest the
    set already records is refused — one frozen set records one world,
    and a changed bundle is a NEW set, not an edit of the old one.
    """
    updates: dict[str, str] = {}
    for name, supplied in (
        ("contract_bundle_digest", contract_bundle_digest),
        ("test_bundle_digest", test_bundle_digest),
        ("environment_profile_digest", environment_profile_digest),
    ):
        held = getattr(candidate_set, name)
        if supplied is None or supplied == held:
            continue
        if held is not None:
            raise ValueError(
                f"the set already records {name}={held}:"
                " one frozen set records one world — a changed bundle is a new set"
            )
        updates[name] = _require_bundle_digest(name, supplied)
    completed = (
        CandidateSet.model_validate({**candidate_set.model_dump(), **updates})
        if updates
        else candidate_set
    )
    return freeze_verified_world(
        completed, environment_pins=environment_pins, policy_refs=policy_refs
    )


@dataclass(frozen=True)
class WorldInputDrift:
    """ONE named applicability input that moved between two worlds (R36-19).

    ``input_path`` names the input structurally —
    ``member/<repository>/image_digest``, ``member/<repository>/candidate_oid``,
    ``member/<repository>`` (appearance/removal), ``test_bundle_digest``,
    ``environment_profile_digest``, ``environment_pin/<service>``,
    ``policy_refs`` — so drift is REPORTED BY NAME, never as an opaque
    digest inequality. ``source_sha_unchanged`` is the review's exact
    arm: the image was rebuilt while the source SHA (candidate oid)
    stayed put, so a source-SHA match alone would have hidden the drift.
    """

    input_path: str
    previous: str
    current: str
    source_sha_unchanged: bool = False

    def as_document(self) -> dict[str, str | bool]:
        """The ``verification.baseline_drift`` fragment's one row."""
        return {
            "input": self.input_path,
            "previous": self.previous,
            "current": self.current,
            "source_sha_unchanged": self.source_sha_unchanged,
        }


def _describe_member(member: CandidateSetMember) -> str:
    return f"{member.candidate_oid}@{member.image_digest}"


def baseline_drift(previous: CandidateSet, current: CandidateSet) -> tuple[WorldInputDrift, ...]:
    """Name EVERY applicability input that moved between two candidate sets.

    The R36-19 selective-invalidation source: replaying previously
    passed evidence is judged against the inputs the evidence actually
    claimed, and what moved is reported by NAME (the drifted baseline
    dependency, the rebuilt image, the changed test bundle, the moved
    pin, the edited policy) — never as a bare "digest differs". The
    comparison covers exactly the applicability surface (members at
    their candidate oids and image artifacts, the test bundle, the
    environment profile, the pins, the policy refs); ``work_id``,
    ``plan_revision`` and the work-contract/contract-bundle digests are
    deliberately invisible (provenance and this-run obligations, not
    dependencies — the same split :func:`applicability_digest` makes).
    """
    drifts: list[WorldInputDrift] = []
    previous_members = {member.repository_id: member for member in previous.members}
    current_members = {member.repository_id: member for member in current.members}
    for repository_id in sorted(set(previous_members) | set(current_members)):
        before = previous_members.get(repository_id)
        after = current_members.get(repository_id)
        if before is None:
            drifts.append(
                WorldInputDrift(f"member/{repository_id}", _ABSENT, _describe_member(after))
            )
            continue
        if after is None:
            drifts.append(
                WorldInputDrift(f"member/{repository_id}", _describe_member(before), _ABSENT)
            )
            continue
        if before.candidate_oid != after.candidate_oid:
            drifts.append(
                WorldInputDrift(
                    f"member/{repository_id}/candidate_oid",
                    before.candidate_oid,
                    after.candidate_oid,
                )
            )
        if before.image_digest != after.image_digest:
            drifts.append(
                WorldInputDrift(
                    f"member/{repository_id}/image_digest",
                    before.image_digest,
                    after.image_digest,
                    source_sha_unchanged=before.candidate_oid == after.candidate_oid,
                )
            )
        if before.role != after.role:
            drifts.append(WorldInputDrift(f"member/{repository_id}/role", before.role, after.role))
    for field in ("test_bundle_digest", "environment_profile_digest"):
        before, after = getattr(previous, field), getattr(current, field)
        if before != after:
            drifts.append(WorldInputDrift(field, before or _UNSET, after or _UNSET))
    previous_pins = dict(previous.environment_pins)
    current_pins = dict(current.environment_pins)
    for service in sorted(set(previous_pins) | set(current_pins)):
        before, after = previous_pins.get(service), current_pins.get(service)
        if before != after:
            drifts.append(
                WorldInputDrift(f"environment_pin/{service}", before or _UNSET, after or _UNSET)
            )
    before_refs = sorted(set(previous.policy_refs))
    after_refs = sorted(set(current.policy_refs))
    if before_refs != after_refs:
        drifts.append(
            WorldInputDrift(
                "policy_refs", ",".join(before_refs) or _UNSET, ",".join(after_refs) or _UNSET
            )
        )
    return tuple(drifts)


# ---------------------------------------------------------------------------
# Per-dependency applicability (NXT-21): invalidate evidence by the
# RELEVANT dependency identity, never by whole-plan numbering.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyIdentity:
    """The identity of ONE dependency: what a piece of evidence covered.

    Exactly the per-member slice of the tested world — repository,
    candidate oid, exact image artifact and role. Deliberately EXCLUDES
    plan revision, work id and every other whole-plan number: those are
    historical provenance (WHICH plan asked, under which numbering),
    not parts of any dependency. Folding them into the comparison is
    how a renumbered plan used to invalidate evidence for steps that
    never moved — the NXT-21 defect this type exists to prevent.
    """

    repository_id: str
    candidate_oid: str
    image_digest: str
    role: str


def member_identity(member: CandidateSetMember) -> DependencyIdentity:
    """The dependency identity ONE member currently holds."""
    return DependencyIdentity(
        repository_id=member.repository_id,
        candidate_oid=member.candidate_oid,
        image_digest=member.image_digest,
        role=member.role,
    )


def world_identities(candidate_set: CandidateSet) -> dict[str, DependencyIdentity]:
    """Every member's current dependency identity, keyed by repository."""
    return {member.repository_id: member_identity(member) for member in candidate_set.members}


@dataclass(frozen=True)
class MemberChange:
    """ONE member's dependency identity moved (the NXT-21 headline case).

    ``previous``/``current`` are the identities around the move;
    ``None`` means the member was not in the world (appearance) or left
    it (removal).
    """

    repository_id: str
    previous: DependencyIdentity | None
    current: DependencyIdentity | None


@dataclass(frozen=True)
class TestBundleChange:
    """The test bundle the world is judged under changed.

    ``None`` is a real recorded value — "judged under no test bundle" —
    so a bundle APPEARING invalidates records that claimed none, the
    same way a bundle edit invalidates records judged under the old
    one. No indefinite reuse by SHA: what judges the code is part of
    what the evidence vouches for.
    """

    # pytest must NOT collect this despite the ``Test`` prefix — the
    # name is the domain's ("test bundle"), not the test suite's.
    __test__ = False

    previous: str | None
    current: str | None


@dataclass(frozen=True)
class EnvironmentPinChange:
    """ONE external service pin moved (NXT-21 precision per service).

    Only records that EXPLICITLY pinned this service at *previous*
    participate: a record that never pinned the service made no claim
    about it, so an unrelated pin move must not touch it.
    """

    service: str
    previous: str | None
    current: str | None


#: The change vocabulary :meth:`EvidenceLedger.invalidated_by` accepts.
DependencyChange = MemberChange | TestBundleChange | EnvironmentPinChange


def describe_change(change: DependencyChange) -> str:
    """The durable one-line reason a change invalidates what it does."""
    if isinstance(change, MemberChange):
        return f"member {change.repository_id} identity moved (candidate_oid/image_digest/role)"
    if isinstance(change, TestBundleChange):
        return "test bundle changed"
    return f"environment pin for {change.service} changed"


@dataclass(frozen=True)
class EvidenceRecord:
    """One evidence item's CLAIMED applicability (NXT-21).

    The record does not merely say "verified" — it names the EXACT
    dependencies it vouches for: the member identities it covered, the
    test bundle it was judged under, the environment profile/pins and
    the policy refs it ran against. ``superseded`` (with its recorded
    reason) marks the moment its authority was withdrawn; a superseded
    record stays inspectable forever but can never masquerade as
    current again except through an EXPLICIT
    :meth:`EvidenceLedger.reactivate` applicability check.
    """

    evidence_id: str
    dependencies: frozenset[DependencyIdentity] = frozenset()
    test_bundle_digest: str | None = None
    environment_profile_digest: str | None = None
    environment_pins: tuple[tuple[str, str], ...] = ()
    policy_refs: frozenset[str] = frozenset()
    superseded: bool = False
    superseded_reason: str = ""

    def covers(self, repository_id: str) -> bool:
        """Whether this record claims *repository_id* at all."""
        return any(dep.repository_id == repository_id for dep in self.dependencies)

    def claimed_identity(self, repository_id: str) -> DependencyIdentity | None:
        """The identity this record claims for *repository_id* (or None)."""
        for dep in self.dependencies:
            if dep.repository_id == repository_id:
                return dep
        return None

    def pinned_digest(self, service: str) -> str | None:
        """The exact artifact this record pinned *service* to (or None)."""
        return dict(self.environment_pins).get(service)


def record_evidence(
    evidence_id: str,
    candidate_set: CandidateSet,
    covers: Iterable[str],
) -> EvidenceRecord:
    """Bind a new evidence item to what it covers, AS THE SET RECORDS IT.

    The record claims the CURRENT identity of every repository named
    in *covers* — an unknown name is refused: a claim over a member
    that does not exist is not precise, it is wrong. It also carries
    the set's test bundle and environment profile and, when the set
    was frozen with them, its PERSISTED environment pins and policy
    refs — so the claim binds at PERSISTENCE time (NXT-22), not to
    whatever the caller remembers later.
    """
    members = {member.repository_id: member for member in candidate_set.members}
    claimed = set(covers)
    unknown = sorted(claimed - set(members))
    if unknown:
        raise ValueError(f"evidence {evidence_id} claims repositories outside the set: {unknown}")
    return EvidenceRecord(
        evidence_id=evidence_id,
        dependencies=frozenset(
            member_identity(members[repository_id]) for repository_id in claimed
        ),
        test_bundle_digest=candidate_set.test_bundle_digest,
        environment_profile_digest=candidate_set.environment_profile_digest,
        environment_pins=tuple(sorted(candidate_set.environment_pins)),
        policy_refs=frozenset(candidate_set.policy_refs),
    )


@dataclass(frozen=True)
class EvidenceLedger:
    """The applicability ledger: who claims what, and who survived.

    NXT-21's core demand, made structural. :meth:`invalidated_by`
    answers "which records' CLAIMS did this one change break" — never
    "which records existed when a plan number moved". A change in ONE
    member's candidate oid or image artifact touches exactly the
    records covering THAT member; everything else stays applicable,
    stays current, and does not rerun.
    """

    records: tuple[EvidenceRecord, ...] = ()

    def record(self, *records: EvidenceRecord) -> EvidenceLedger:
        """Add records; a duplicate evidence id refuses (an ambiguous id)."""
        incoming = [record.evidence_id for record in records]
        known = {existing.evidence_id for existing in self.records}
        if len(incoming) != len(set(incoming)) or set(incoming) & known:
            raise ValueError(f"duplicate evidence id in ledger: {incoming}")
        return replace(self, records=self.records + records)

    def _by_id(self, evidence_id: str) -> EvidenceRecord:
        for existing in self.records:
            if existing.evidence_id == evidence_id:
                return existing
        raise ValueError(f"no evidence record {evidence_id!r} in the ledger")

    def invalidated_by(self, change: DependencyChange) -> set[str]:
        """The evidence ids whose CLAIMS *change* breaks — only those.

        The uniform rule: a move from ``previous`` to ``current``
        invalidates exactly the records that claimed ``previous``, and
        only when the two differ. Records that never covered the moved
        dependency are untouched — an unrelated member's change must
        not force every expensive integration test to rerun. Records
        already claiming ``current`` (recorded against the new world,
        e.g. after a reversion) are untouched BY THIS EVENT; whether a
        record applies to a world at all is the separate, state-based
        :meth:`applicable_to` question. Provenance fields (plan
        revision, work id) are invisible here by construction — there
        is no change type that carries them.
        """
        if isinstance(change, MemberChange):
            if change.previous == change.current:
                return set()
            return {
                record.evidence_id
                for record in self.records
                if record.covers(change.repository_id)
                and record.claimed_identity(change.repository_id) == change.previous
            }
        if isinstance(change, TestBundleChange):
            if change.previous == change.current:
                return set()
            return {
                record.evidence_id
                for record in self.records
                if record.test_bundle_digest == change.previous
            }
        if isinstance(change, EnvironmentPinChange):
            if change.previous == change.current:
                return set()
            return {
                record.evidence_id
                for record in self.records
                if record.pinned_digest(change.service) is not None
                and record.pinned_digest(change.service) == change.previous
            }
        raise TypeError(f"unknown dependency change: {change!r}")

    def supersede(self, evidence_ids: Iterable[str], reason: str) -> EvidenceLedger:
        """Withdraw authority from the named records — WITHOUT deleting them.

        Superseded records stay in the ledger, inspectable forever, with
        the recorded *reason*; only their authority is gone.
        Superseding an already-superseded record keeps its FIRST reason
        (history is not rewritten); superseding an unknown id refuses
        (a typo must not silently no-op). This is the "supersede, never
        delete" half of NXT-21.
        """
        wanted = set(evidence_ids)
        unknown = sorted(wanted - {record.evidence_id for record in self.records})
        if unknown:
            raise ValueError(f"no evidence record(s) {unknown} in the ledger")
        updated = tuple(
            record
            if record.evidence_id not in wanted or record.superseded
            else replace(record, superseded=True, superseded_reason=reason)
            for record in self.records
        )
        return replace(self, records=updated)

    def apply(self, change: DependencyChange) -> EvidenceLedger:
        """Supersede exactly what *change* invalidates, reason recorded."""
        return self.supersede(self.invalidated_by(change), describe_change(change))

    def applicable_to(self, candidate_set: CandidateSet) -> set[str]:
        """The records whose COMPLETE applicability fingerprint matches this world.

        The state-based question (vs the event-based
        :meth:`invalidated_by`): a record applies iff it is not
        superseded AND every dependency it claims sits in
        *candidate_set* at the EXACT claimed identity AND it was judged
        under this set's test bundle, environment profile, persisted
        environment pins and policy refs. Provenance — plan revision,
        work id, member ORDER — is deliberately invisible here: a
        renumbered or reordered plan preserves applicable verification.
        """
        world = world_identities(candidate_set)
        world_pins = dict(candidate_set.environment_pins)
        world_refs = set(candidate_set.policy_refs)
        applicable: set[str] = set()
        for record in self.records:
            if record.superseded:
                continue
            if any(world.get(dep.repository_id) != dep for dep in record.dependencies):
                continue
            if record.test_bundle_digest != candidate_set.test_bundle_digest:
                continue
            if record.environment_profile_digest != candidate_set.environment_profile_digest:
                continue
            if dict(record.environment_pins) != world_pins:
                continue
            if set(record.policy_refs) != world_refs:
                continue
            applicable.add(record.evidence_id)
        return applicable

    def reactivate(self, evidence_id: str, candidate_set: CandidateSet) -> EvidenceLedger:
        """EXPLICIT reactivation after a reversion — never blind reuse.

        A world that moved A -> B -> A does NOT resurrect its old A
        evidence by itself: those records were superseded when A -> B
        landed and stay inspectable-but-inert. Bringing one back is an
        explicit decision, and it passes through the FULL applicability
        check first — the record must match the current world
        completely (same rule as :meth:`applicable_to`), or the
        reactivation refuses.
        """
        target = self._by_id(evidence_id)
        if not target.superseded:
            raise ValueError(f"evidence {evidence_id} is not superseded: nothing to reactivate")
        # Probe the record's FINGERPRINT, not its authority state: the
        # ledger-wide applicable_to would skip it for being superseded,
        # which is exactly the state a reactivation re-examines.
        probe = replace(target, superseded=False, superseded_reason="")
        if evidence_id not in EvidenceLedger(records=(probe,)).applicable_to(candidate_set):
            raise ValueError(
                f"evidence {evidence_id} does not match the current world:"
                " reactivation requires an explicit applicability check"
            )
        updated = tuple(
            replace(record, superseded=False, superseded_reason="")
            if record.evidence_id == evidence_id
            else record
            for record in self.records
        )
        return replace(self, records=updated)


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
