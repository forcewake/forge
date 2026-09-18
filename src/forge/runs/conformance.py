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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ARTIFACT_TRANSPORT_CI",
    "CAS_FORGE_WRITER_GUARD",
    "CAS_GRAPHQL_EXPECTED_HEAD_OID",
    "CAS_PUSH_OLD_OBJECT_ID",
    "PROVIDER_CAPABILITIES",
    "ProviderCapability",
    "UnknownProviderError",
    "attest_conformance",
    "capability_of",
    "conformance_attested",
    "registration_allowed",
    "reset_conformance_attestations",
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
    scenario; only providers WITH a capability record can attest.
    """
    capability_of(provider)  # an unknown provider never attests
    _CONFORMANCE_ATTESTATIONS.add(provider)


def conformance_attested(provider: str) -> bool:
    """Whether *provider*'s conformance scenarios passed in this process."""
    return provider in _CONFORMANCE_ATTESTATIONS


def reset_conformance_attestations() -> None:
    """Drop every attestation (suite teardown / a fresh CI registration)."""
    _CONFORMANCE_ATTESTATIONS.clear()


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
