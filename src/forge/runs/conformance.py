"""Provider capability records + the verified-ready registration rule (R21).

One frozen, HONEST record per supported provider describing what its
adapter actually does today — the facts a conformance kit (and a future
adapter author) needs to tell a genuine capability difference from a
lifecycle violation:

- **How the write enforces compare-and-swap.** GitHub's
  ``createCommitOnBranch`` pins ``expectedHeadOid`` and Azure DevOps' push
  pins ``oldObjectId`` — the PROVIDER refuses a stale head. GitLab's
  Commits API has no compare-and-swap: forge's own
  :class:`~forge.repository.writer.ChangesetWriter` checks the branch head
  before dispatch and raises :class:`~forge.repository.writer.BranchDriftError`
  (with the R11 intent probe behind lost responses). Both orders REJECT a
  stale head with zero writes — the invariant is identical, the origin is
  a record fact, not a test-time guess.
- **What the merge surface is.** GitHub and Azure DevOps give forge a
  native pull-request object; GitLab does not anchor the review to provider
  merge machinery, so forge falls back to a synthetic Draft MR it maintains
  over the run-owned branch (CI tests the branch head itself). On native-PR
  lanes the provider may test its own merge ref (GitHub Actions
  ``pull_request`` runs a synthetic merge revision — a verification-reader
  concern owned by :mod:`forge.integrations.github_flow`, never conflated
  with the candidate head).
- **How harness-lane candidates travel.** On every provider the candidate
  rides CI artifacts (``candidate.diff`` + ``candidate.meta.json``) — the
  harness never holds a write token (ADR-0015/0016).
- **What the readonly review is bound to** — the per-provider surface
  identity the reviewer reads the candidate diff through, and **whether the
  lane may record a verified ready verdict at all**.

The registration rule: :func:`verified_ready_capable` returns True only
when the provider's record claims the capability AND its conformance
scenarios have passed in this process (:func:`attest_conformance` — the
hook ``tests/test_conformance.py`` calls after a lane clears every
scenario). A new adapter cannot ship a ``verified_ready_capable=True``
record while failing the kit: the kit derives its lanes from
:data:`PROVIDER_CAPABILITIES`, so a record without a lane fails the suite,
and a lane that violates a scenario never attests.

A14 attestation bookkeeping: the attestation is SCENARIO-ACCOUNTED. Every
mandatory scenario (:func:`mandatory_scenarios` — the kit's forbidden
battery plus the composed service-leg scenarios of
:data:`COMPOSED_SCENARIOS`) must have been recorded ``passed`` via
:func:`record_scenario` before :func:`attest_conformance` accepts a lane;
a scenario that failed, was skipped, or never ran blocks the attestation
with an explicit :class:`AttestationBlockedError`. A helper-level green
test can no longer smuggle a capability past a mandatory path that was
never driven, and :func:`conformance_manifest` publishes the
machine-readable record (per-provider scenario outcomes, skips,
attestation) CI can attach to a release.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ARTIFACT_TRANSPORT_CI",
    "AttestationBlockedError",
    "BATTERY_SCENARIOS",
    "CAS_FORGE_WRITER_GUARD",
    "CAS_GRAPHQL_EXPECTED_HEAD_OID",
    "CAS_PUSH_OLD_OBJECT_ID",
    "COMPOSED_SCENARIOS",
    "PROVIDER_CAPABILITIES",
    "ProviderCapability",
    "SCENARIO_FAILED",
    "SCENARIO_NOT_RUN",
    "SCENARIO_PASSED",
    "SCENARIO_SKIPPED",
    "UnknownProviderError",
    "attest_conformance",
    "capability_of",
    "conformance_attested",
    "conformance_manifest",
    "mandatory_scenarios",
    "record_scenario",
    "registration_allowed",
    "reset_conformance_attestations",
    "scenario_outcomes",
    "verified_ready_capable",
]

#: The candidate transport for harness lanes on EVERY provider: the diff and
#: its metadata ride CI artifacts — never a harness-held write token.
ARTIFACT_TRANSPORT_CI = "ci_artifacts"

#: ``cas_mechanism`` values — the honest origin of each lane's stale-head
#: refusal. ``CAS_FORGE_WRITER_GUARD`` means the provider has no CAS and
#: forge's writer enforces the pin application-side.
CAS_GRAPHQL_EXPECTED_HEAD_OID = "graphql_createCommitOnBranch.expectedHeadOid"
CAS_PUSH_OLD_OBJECT_ID = "git_push.oldObjectId"
CAS_FORGE_WRITER_GUARD = "forge_writer_head_check"

#: The provider identities the readonly reviewer binds to (``readonly_identity``).
READONLY_IDENTITY_MERGE_REQUEST_IID = "merge_request_iid"
READONLY_IDENTITY_PULL_REQUEST_NUMBER = "pull_request_number"
READONLY_IDENTITY_PULL_REQUEST_ID = "pull_request_id"


@dataclass(frozen=True)
class ProviderCapability:
    """The frozen, honest capability record of one provider lane.

    Constructed only here, at import time — the table below is the single
    declaration; the conformance kit reads it instead of hard-coding
    provider facts, and a divergence between record and behavior fails the
    kit.
    """

    #: The provider id used on ``flow_runs.provider`` and service dispatch.
    provider: str
    #: The commit API the lane writes with (``commits_api``, ``graphql``,
    #: ``git_push_api``).
    commit_transport: str
    #: True when the PROVIDER enforces the compare-and-swap on the write;
    #: False when forge's writer enforces the pin application-side.
    native_cas: bool
    #: The mechanism the stale-head refusal comes from (a ``CAS_*`` constant
    #: or an equally specific description).
    cas_mechanism: str
    #: True when the review/merge surface is forge-synthesized — a Draft MR
    #: forge maintains over the run-owned branch (the fallback where the
    #: provider offers no native pull-request flow to pin) — rather than the
    #: provider's native pull-request object.
    synthetic_merge_fallback: bool
    #: How harness-lane candidates travel (an ``ARTIFACT_TRANSPORT_*``
    #: constant). Never a write token.
    artifact_transport: str
    #: The surface identity the readonly review binds to (a
    #: ``READONLY_IDENTITY_*`` constant).
    readonly_identity: str
    #: Whether the lane is allowed to record a VERIFIED ready verdict at all
    #: (it has a verification producer wired). Claiming this without passing
    #: the conformance kit never registers — see :func:`verified_ready_capable`.
    verified_ready_capable: bool


#: The honest capability table for the supported providers (R21). One entry
#: per lane the conformance kit drives; a new provider adds its record here
#: AND a lane to ``tests/test_conformance.py`` — the kit fails while either
#: half is missing.
PROVIDER_CAPABILITIES: Mapping[str, ProviderCapability] = {
    "gitlab": ProviderCapability(
        provider="gitlab",
        commit_transport="commits_api",
        native_cas=False,
        cas_mechanism=CAS_FORGE_WRITER_GUARD,
        synthetic_merge_fallback=True,
        artifact_transport=ARTIFACT_TRANSPORT_CI,
        readonly_identity=READONLY_IDENTITY_MERGE_REQUEST_IID,
        verified_ready_capable=True,
    ),
    "github": ProviderCapability(
        provider="github",
        commit_transport="graphql",
        native_cas=True,
        cas_mechanism=CAS_GRAPHQL_EXPECTED_HEAD_OID,
        synthetic_merge_fallback=False,
        artifact_transport=ARTIFACT_TRANSPORT_CI,
        readonly_identity=READONLY_IDENTITY_PULL_REQUEST_NUMBER,
        verified_ready_capable=True,
    ),
    "azure_devops": ProviderCapability(
        provider="azure_devops",
        commit_transport="git_push_api",
        native_cas=True,
        cas_mechanism=CAS_PUSH_OLD_OBJECT_ID,
        synthetic_merge_fallback=False,
        artifact_transport=ARTIFACT_TRANSPORT_CI,
        readonly_identity=READONLY_IDENTITY_PULL_REQUEST_ID,
        verified_ready_capable=True,
    ),
}


class UnknownProviderError(LookupError):
    """No capability record exists for the requested provider id.

    A lane with a run row but no record cannot be driven, attested or
    registered — spelled out instead of a silent ``None``.
    """


#: The providers whose conformance scenarios passed IN THIS PROCESS (the
#: test-time hook). Empty until a conformance suite attests a lane.
_CONFORMANCE_ATTESTATIONS: set[str] = set()


# ----------------------------------------------------------------------
# A14 attestation bookkeeping: scenario-accounted attestation
# ----------------------------------------------------------------------

#: Scenario-outcome values recorded in the ledger.
SCENARIO_PASSED = "passed"
SCENARIO_FAILED = "failed"
SCENARIO_SKIPPED = "skipped"
SCENARIO_NOT_RUN = "not_run"

#: The conformance kit's forbidden battery — one entry per invariant every
#: provider must refuse (the scenario functions in ``tests/test_conformance.py``).
BATTERY_SCENARIOS: tuple[str, ...] = (
    "positive_control_publishes_exactly_the_validated_manifest",
    "bad_candidate_refused_with_zero_writes",
    "out_of_scope_candidate_refused",
    "stale_head_drifts_with_zero_writes",
    "missing_spec_refused",
    "cancelled_generation_never_publishes",
    "failed_ci_never_verified_ready",
    "unverified_run_honestly_labeled",
)

#: A14 composed invariant-failure scenarios (review d16f523): COMBINED cases
#: over the PRODUCTION entry points that the per-invariant battery cannot
#: express. Each entry names the driver the conformance suite must run (and
#: record) before the provider may attest; a driver that fails, is skipped,
#: or is never invoked blocks the attestation. A provider without composed
#: scenarios yet carries an empty tuple — the battery alone attests it.
COMPOSED_SCENARIOS: Mapping[str, tuple[str, ...]] = {
    # A05 composed: the durable claim loop over the REAL command-execution
    # leg — a batch whose leases die mid-handler with two workers, each
    # handler entering exactly once under its CURRENT owner.
    "gitlab": ("a05_leased_batch_expiry_two_workers",),
    "github": (
        # A06 composed: the durable run mirror over a real streamable-HTTP
        # MCP mount — a scoped token reading a run the REAL GitHub service
        # leg produced, whose subject the token's allowlist matches/denies.
        "a06_run_mirror_scoped_token_http",
        # A01 composed: full evaluate_waiting_ci_one — the required check
        # ABSENT while an optional workflow is green must stay waiting
        # (never verified), and the late required success must verify.
        "a01_required_absent_green_optional",
        # A12 composed: the delayed-apply stub — an accepted-but-unapplied
        # publication write meets the recovery paths; exactly one logical
        # candidate survives (CAS refuses the duplicate / scanner adopts).
        "a12_pending_remote_application",
        # A03 composed: full dispatch → lane render — the plan comment with
        # the SAME id but an edited approved byte is refused by the lane's
        # envelope re-verification ("re-approval required").
        "a03_comment_same_id_edited_body",
    ),
    "azure_devops": (),
}


class AttestationBlockedError(RuntimeError):
    """A lane may not attest: a mandatory scenario failed/skipped/never ran.

    The attestation is only as strong as the scenarios actually driven;
    this error names every mandatory path that is not proven ``passed``
    for the provider.
    """


#: (provider, scenario) → outcome, plus the free-form note the recorder left.
_SCENARIO_LEDGER: dict[tuple[str, str], str] = {}
_SCENARIO_NOTES: dict[tuple[str, str], str] = {}


def mandatory_scenarios(provider: str) -> tuple[str, ...]:
    """Every scenario *provider* must clear before it may attest.

    The forbidden battery plus the provider's composed service-leg
    scenarios (:data:`COMPOSED_SCENARIOS`). Raises
    :class:`UnknownProviderError` for an unrecorded provider.
    """
    capability_of(provider)  # an unknown provider has no mandatory set
    return (*BATTERY_SCENARIOS, *COMPOSED_SCENARIOS.get(provider, ()))


def record_scenario(
    provider: str,
    scenario: str,
    *,
    outcome: str = SCENARIO_PASSED,
    note: str = "",
) -> None:
    """Record one conformance-scenario outcome for *provider* (A14).

    The hook the conformance suite calls as each scenario finishes:
    ``passed`` only after the scenario's assertions all held, ``failed``
    when one did not, ``skipped`` when the path was not exercised. The
    ledger is the attestation's evidence — :func:`attest_conformance`
    refuses a provider with any mandatory scenario not recorded ``passed``.
    """
    capability_of(provider)  # an unknown provider never records
    if outcome not in (SCENARIO_PASSED, SCENARIO_FAILED, SCENARIO_SKIPPED):
        raise ValueError(f"unknown scenario outcome {outcome!r}")
    key = (provider, scenario)
    _SCENARIO_LEDGER[key] = outcome
    if note:
        _SCENARIO_NOTES[key] = note
    else:
        _SCENARIO_NOTES.pop(key, None)


def scenario_outcomes(provider: str) -> dict[str, str]:
    """The recorded outcome for each of *provider*'s mandatory scenarios.

    Names without a recorded outcome read :data:`SCENARIO_NOT_RUN` — a
    scenario the process never drove. Raises
    :class:`UnknownProviderError` for an unrecorded provider.
    """
    return {
        scenario: _SCENARIO_LEDGER.get((provider, scenario), SCENARIO_NOT_RUN)
        for scenario in mandatory_scenarios(provider)
    }


def capability_of(provider: str) -> ProviderCapability:
    """The frozen capability record for *provider*.

    Raises :class:`UnknownProviderError` for an id outside
    :data:`PROVIDER_CAPABILITIES`.
    """
    try:
        return PROVIDER_CAPABILITIES[provider]
    except KeyError:
        raise UnknownProviderError(
            f"no capability record for provider {provider!r} — "
            "add one to forge.runs.conformance.PROVIDER_CAPABILITIES "
            "and a lane to the conformance kit"
        ) from None


def attest_conformance(provider: str) -> None:
    """Record that *provider*'s conformance scenarios passed in this process.

    The hook the conformance suite calls once a lane has cleared every
    scenario; only providers WITH a capability record can attest. A14: the
    attestation is scenario-accounted — every mandatory scenario
    (:func:`mandatory_scenarios`) must have been recorded ``passed`` via
    :func:`record_scenario` first. A battery that was skipped, a composed
    service-leg driver that failed, or a mandatory path that never ran
    raises :class:`AttestationBlockedError` naming each unproven scenario
    — a capability contract cannot go attested with a hole in its evidence.
    """
    capability_of(provider)  # an unknown provider never attests
    unproven = {
        scenario: outcome
        for scenario, outcome in scenario_outcomes(provider).items()
        if outcome != SCENARIO_PASSED
    }
    if unproven:
        detail = ", ".join(f"{scenario}={outcome}" for scenario, outcome in unproven.items())
        raise AttestationBlockedError(
            f"conformance attestation for {provider!r} blocked — mandatory scenarios "
            f"not proven passed: {detail}"
        )
    _CONFORMANCE_ATTESTATIONS.add(provider)


def conformance_attested(provider: str) -> bool:
    """Whether *provider*'s conformance scenarios passed in this process."""
    return provider in _CONFORMANCE_ATTESTATIONS


def reset_conformance_attestations() -> None:
    """Drop every attestation and scenario record (suite teardown / a fresh
    CI registration)."""
    _CONFORMANCE_ATTESTATIONS.clear()
    _SCENARIO_LEDGER.clear()
    _SCENARIO_NOTES.clear()


def conformance_manifest() -> dict[str, object]:
    """The machine-readable conformance record (A14 acceptance).

    One entry per recorded provider: its mandatory scenarios with their
    outcomes (``not_run`` for anything this process never drove), the
    recorded notes, and whether the lane is attested under the
    registration rule. CI attaches this document to a release so a green
    ``verified_ready`` can be traced to the exact scenarios — and skips —
    that earned it.
    """
    providers: dict[str, object] = {}
    for provider in PROVIDER_CAPABILITIES:
        key_provider = provider
        scenarios = scenario_outcomes(provider)
        providers[key_provider] = {
            "attested": conformance_attested(provider),
            "scenarios": scenarios,
            "skipped": sorted(
                name for name, outcome in scenarios.items() if outcome == SCENARIO_SKIPPED
            ),
            "notes": {
                scenario: _SCENARIO_NOTES[(provider, scenario)]
                for scenario in sorted(scenarios)
                if (provider, scenario) in _SCENARIO_NOTES
            },
        }
    return {"schema_version": 1, "providers": providers}


def registration_allowed(record: ProviderCapability, *, conformance_passed: bool) -> bool:
    """The pure registration rule: the record's claim AND a passed kit.

    Either half alone is never enough — a ``verified_ready_capable=True``
    record without a passing conformance suite does not register, and a
    passing suite cannot register a lane whose record withholds the claim.
    """
    return record.verified_ready_capable and conformance_passed


def verified_ready_capable(provider: str) -> bool:
    """The registration rule (R21): may *provider* record a verified ready?

    True only when BOTH hold:

    1. the provider's capability record claims the capability (it has a
       verification producer wired), AND
    2. its conformance scenarios passed in this process — the test-time
       hook :func:`attest_conformance`. A record without a passing kit
       never registers, so a new adapter cannot be green in its own tests
       while violating the core promise.
    """
    return registration_allowed(
        capability_of(provider), conformance_passed=conformance_attested(provider)
    )
