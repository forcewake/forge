"""Q39-04 (#323): the collision-safe native credential locators, the
``native_locator_map`` registry, and the legacy migration contract.

The P05 defect this module pins: ``credential_secret_segment`` maps
punctuation to ``_`` and uppercases, so ``vault:kv/team-a`` and
``vault:kv/team_a`` shared ONE native carrier name in a shared
namespace (rotation between them was a silent alias — and the legacy
spelling of every case/dash/dot variant collapsed onto it too).

Covered here:

- the ENCODING (:func:`native_locator`): a bounded ASCII-readable
  prefix + the first 12 hex of sha256 over the FULL ref — distinct refs
  (slash/dash/underscore/dot/case variants, the P05 pair) can never
  share a locator; the charset stays inside ``[A-Z0-9_]`` (the
  GitHub/GitLab/Azure carrier-name intersection); overlength and
  Unicode refs get the same bounded form; the truly unrepresentable
  refuse typed;
- the REGISTRY (:class:`NativeLocatorRegistry`): idempotent allocation
  within one provider namespace + route, case-insensitive namespaces,
  independent namespaces independent, the registry defending its own
  invariant against a hand-collided map, persistence across a control
  plane restart (partial rollout: old mappings intact, nothing
  renamed);
- the LEGACY MIGRATION: the pre-locator ``FORGE_MODEL_<SEGMENT>``
  mappings adopted into an inventory, ambiguous groups refusing typed
  with EVERY candidate named (never first-match), the explicit operator
  resolution, and the create-new → retire-old runbook halves;
- the DISPATCH SEAM (:func:`delivery_plan` with a locator registry):
  the native modes dispatch under the collision-safe carrier — the P05
  pair planned through the registry yields two DISTINCT transports —
  while the no-registry compat path keeps the legacy spelling (never a
  silent rename), and the Azure carrier stays the env-slot-named group
  secret (fixed per route, nothing ref-shaped in it to collide).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from forge.adaptive.credential_broker import (
    CREDENTIAL_SECRET_PREFIX,
    DELIVERY_MODE_AZURE_GROUP,
    DELIVERY_MODE_GITHUB_NATIVE,
    DELIVERY_MODE_GITLAB_PROTECTED,
    DELIVERY_ROUTE_ENV,
    DELIVERY_TEMPLATE_DIR_ENV,
    NATIVE_LOCATOR_DIGEST_CHARS,
    NATIVE_LOCATOR_PREFIX_MAX,
    NativeLocatorRegistry,
    credential_secret_name,
    credential_secret_segment,
    delivery_plan,
    locator_namespace,
    native_locator,
    native_locator_carrier,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import CredentialRefusal, ProjectCredentialRegistry
from forge.adaptive.credential_broker import StagedBroker

SUBJECT = CanonicalSubject(provider_family="gitlab", connection="gitlab.example", native_id="90210")
NAMESPACE = locator_namespace(SUBJECT, "gitlab")
#: A SECOND, independent project namespace — the same locator may live
#: in both (no global uniqueness is required or wanted).
OTHER_NAMESPACE = locator_namespace(
    CanonicalSubject(provider_family="gitlab", connection="gitlab.example", native_id="77777"),
    "gitlab",
)
#: A shared GROUP namespace (the same-route-in-shared-group-secrets
#: negative): carriers there collision-check TOGETHER even though the
#: subjects differ.
GROUP_NAMESPACE = "gitlab-group/777"

REF_A = "vault:kv/team-a"  # the P05 pair — identical legacy segment …
REF_B = "vault:kv/team_a"  # … FORGE_MODEL_VAULT_KV_TEAM_A
LEGACY_CARRIER = credential_secret_name(REF_A)

TEMPLATES_DIR = Path(__file__).parents[1] / "ci" / "templates"

#: The refs whose locators must be pairwise distinct (AC-01): slash,
#: dash, underscore, dot, case — plus the canonical env ref.
DISTINCTNESS_REFS = (
    REF_A,
    REF_B,
    "vault:kv-team-a",
    "vault:kv/Team-A",
    "vault:kv/team.a",
    "vault:kv/TEAM_A",
    "env:ANTHROPIC_AUTH_TOKEN",
)


def _delivery_env(*routes: str) -> dict[str, str]:
    return {
        DELIVERY_ROUTE_ENV: ",".join(routes),
        DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR),
    }


def _bound_registry(ref: str = "env:ANTHROPIC_AUTH_TOKEN") -> ProjectCredentialRegistry:
    registry = ProjectCredentialRegistry()
    registry.bind(SUBJECT, "anthropic-gateway", ref, bound_by="ops@a")
    return registry


# ----------------------------------------------------------------------
# The encoding
# ----------------------------------------------------------------------


class TestNativeLocatorEncoding:
    @pytest.mark.parametrize("ref", DISTINCTNESS_REFS)
    def test_the_locator_stays_inside_the_provider_charset(self, ref: str):
        """The carrier must be provisionable on every provider's native
        secret facility: [A-Z0-9_] is the intersection (GitHub secret
        names refuse hyphens at the API; GitLab variables and Azure
        group names follow the env-var shape)."""
        assert re.fullmatch(r"[A-Z0-9_]+", native_locator(ref))
        assert re.fullmatch(r"[A-Z0-9_]+", native_locator_carrier(ref))
        assert native_locator_carrier(ref).startswith(CREDENTIAL_SECRET_PREFIX)

    def test_the_p05_collision_pair_gets_distinct_locators(self):
        """THE defect (probe P05): the two refs whose LEGACY segments
        collide get two distinct carriers — rotation between them can
        never be a silent alias again."""
        assert credential_secret_segment(REF_A) == credential_secret_segment(REF_B)
        assert native_locator(REF_A) != native_locator(REF_B)
        assert native_locator_carrier(REF_A) != native_locator_carrier(REF_B)

    def test_every_distinctness_variant_maps_to_a_unique_locator(self):
        locators = {ref: native_locator(ref) for ref in DISTINCTNESS_REFS}
        assert len(set(locators.values())) == len(locators)

    def test_the_locator_is_deterministic(self):
        assert native_locator(REF_A) == native_locator(REF_A)
        # the digest is over the EXACT ref — nothing environmental
        assert native_locator("env:ANTHROPIC_AUTH_TOKEN").endswith(
            native_locator("env:ANTHROPIC_AUTH_TOKEN")[-NATIVE_LOCATOR_DIGEST_CHARS:]
        )

    def test_the_shape_is_readable_prefix_plus_digest(self):
        locator = native_locator(REF_A)
        prefix, separator, digest = locator.rpartition("_")
        assert separator == "_"
        assert prefix == "TEAMA"  # team-a → the readable leaf, punctuation dropped
        assert re.fullmatch(r"[0-9A-F]{12}", digest)
        assert native_locator_carrier(REF_A) == f"FORGE_MODEL_{locator}"

    def test_an_overlength_ref_gets_the_same_bounded_form(self):
        long_ref = f"vault:kv/{'a' * 200}"
        locator = native_locator(long_ref)
        assert len(locator) <= NATIVE_LOCATOR_PREFIX_MAX + 1 + NATIVE_LOCATOR_DIGEST_CHARS
        # the bounded form still disambiguates: two long refs differ
        assert native_locator(long_ref) != native_locator(f"vault:kv/{'a' * 199}")

    def test_a_unicode_ref_with_ascii_characters_gets_a_bounded_locator(self):
        locator = native_locator("vault:kv/café-key")
        assert re.fullmatch(r"[A-Z0-9_]+", locator)
        assert locator.startswith("CAFKEY_")
        # the unicode detail rides the digest — two spellings differ
        assert locator != native_locator("vault:kv/cafe-key")

    def test_a_ref_without_any_ascii_identifier_refuses_typed(self):
        with pytest.raises(CredentialRefusal, match="native_locator_unrepresentable") as caught:
            native_locator("ключ:значение")
        instruction = str(caught.value.detail["instruction"])
        assert "re-bind" in instruction  # actionable, not a bare refusal

    def test_an_empty_ref_refuses_typed(self):
        with pytest.raises(CredentialRefusal, match="native_locator_unrepresentable"):
            native_locator("")


# ----------------------------------------------------------------------
# The registry: allocation, namespaces, persistence
# ----------------------------------------------------------------------


class TestLocatorRegistryAllocation:
    def test_allocation_is_idempotent_with_a_frozen_timestamp(self):
        registry = NativeLocatorRegistry()
        first = registry.allocate(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        second = registry.allocate(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        assert first == second  # same allocation, timestamp frozen at first

    def test_two_refs_in_one_namespace_allocate_their_own_distinct_carriers(self):
        registry = NativeLocatorRegistry()
        a = registry.allocate(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        b = registry.allocate(REF_B, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        assert a.locator != b.locator
        assert a.carrier_name != b.carrier_name

    def test_the_same_locator_in_independent_namespaces_is_fine(self):
        """Independent project namespaces need not be globally unique:
        the same ref may hold the same locator in both."""
        registry = NativeLocatorRegistry()
        here = registry.allocate(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        there = registry.allocate(
            REF_A, namespace=OTHER_NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        assert here.locator == there.locator
        assert here.namespace != there.namespace

    def test_namespaces_compare_case_insensitively(self):
        """GitHub secret names are case-INSENSITIVELY unique, so two
        namespace spellings differing only by case are ONE namespace:
        the same ref there returns the same allocation (no second,
        case-variant allocation sneaks past the collision check)."""
        registry = NativeLocatorRegistry()
        first = registry.allocate(
            REF_A, namespace=NAMESPACE.swapcase(), route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        again = registry.allocate(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        assert first == again

    def test_a_hand_collided_map_is_defended_typed(self, tmp_path: Path):
        """The registry defends its own invariant even against a
        hand-edited map: two different refs holding one locator within
        a namespace+route refuse ``native_locator_collision`` with BOTH
        candidates named (the digest allocator makes this
        near-impossible; the JSON map is operator-editable)."""
        document = {
            "schema": "forge.credential.native-locator-map/1",
            "allocations": [
                {
                    # a hand-edited row: REF_A's allocation was rewritten
                    # to hold REF_B's REAL locator (the digest allocator
                    # would never produce this — an operator's edit did)
                    "credential_ref": REF_A,
                    "locator": native_locator(REF_B),
                    "carrier_name": native_locator_carrier(REF_B),
                    "namespace": NAMESPACE,
                    "route": DELIVERY_MODE_GITLAB_PROTECTED,
                    "allocated_at": "2026-09-24T00:00:00+00:00",
                },
                {
                    "credential_ref": REF_B,
                    "locator": native_locator(REF_B),
                    "carrier_name": native_locator_carrier(REF_B),
                    "namespace": NAMESPACE,
                    "route": DELIVERY_MODE_GITLAB_PROTECTED,
                    "allocated_at": "2026-09-24T00:00:00+00:00",
                },
            ],
        }
        path = tmp_path / "native_locator_map.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        registry = NativeLocatorRegistry(path=path)
        with pytest.raises(CredentialRefusal, match="native_locator_collision") as caught:
            registry.allocate(REF_B, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        assert caught.value.detail["observability"] == "credential.native_locator_collision"
        assert sorted(caught.value.detail["candidates"]) == sorted([REF_A, REF_B])

    def test_an_unreadable_map_refuses_typed(self, tmp_path: Path):
        path = tmp_path / "native_locator_map.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(CredentialRefusal, match="native_locator_map_invalid"):
            NativeLocatorRegistry(path=path)

    def test_a_map_with_a_wrong_schema_refuses_typed(self, tmp_path: Path):
        path = tmp_path / "native_locator_map.json"
        path.write_text(
            json.dumps({"schema": "something.else/1", "allocations": []}), encoding="utf-8"
        )
        with pytest.raises(CredentialRefusal, match="native_locator_map_invalid"):
            NativeLocatorRegistry(path=path)

    def test_partial_rollout_survives_a_control_plane_restart(self, tmp_path: Path):
        """The negative rollout scenario: a map holding OLD (legacy)
        mappings, a restarted control plane — the inventory is intact,
        the allocation is the SAME (timestamp frozen), and NOTHING was
        renamed underneath the live carriers."""
        path = tmp_path / "native_locator_map.json"
        before = NativeLocatorRegistry(path=path)
        adopted = before.adopt_legacy(
            REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        allocated = before.allocate(
            "env:ANTHROPIC_AUTH_TOKEN",
            namespace=NAMESPACE,
            route=DELIVERY_MODE_GITLAB_PROTECTED,
        )
        # the restarted control plane
        after = NativeLocatorRegistry(path=path)
        assert (
            after.allocation_for(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
            == adopted
        )
        assert (
            after.allocation_for(
                "env:ANTHROPIC_AUTH_TOKEN",
                namespace=NAMESPACE,
                route=DELIVERY_MODE_GITLAB_PROTECTED,
            )
            == allocated
        )
        # the legacy carrier is still the live mapping — never renamed
        assert (
            after.allocation_for(
                REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
            ).legacy_carrier
            == LEGACY_CARRIER
        )

    def test_the_persisted_document_is_value_free(self, tmp_path: Path):
        path = tmp_path / "native_locator_map.json"
        registry = NativeLocatorRegistry(path=path)
        registry.allocate(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        text = path.read_text(encoding="utf-8")
        assert "value" not in text.lower().replace("allocated_at", "")
        document = json.loads(text)
        assert document["schema"] == "forge.credential.native-locator-map/1"


# ----------------------------------------------------------------------
# The legacy migration: inventory, collision, resolution, retirement
# ----------------------------------------------------------------------


class TestLegacyMigration:
    def test_adoption_records_the_live_legacy_carrier_beside_the_locator(self):
        registry = NativeLocatorRegistry()
        adopted = registry.adopt_legacy(
            REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        assert adopted.legacy_carrier == LEGACY_CARRIER
        # the migration TARGET is computed and recorded — nothing renamed
        assert adopted.carrier_name == native_locator_carrier(REF_A)
        assert adopted.carrier_name != adopted.legacy_carrier

    def test_the_inventory_lists_colliding_legacy_refs_and_renames_nothing(self):
        registry = NativeLocatorRegistry()
        a = registry.adopt_legacy(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        b = registry.adopt_legacy(REF_B, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        groups = registry.legacy_collision_groups()
        assert len(groups) == 1
        assert groups[0].legacy_carrier == LEGACY_CARRIER
        assert groups[0].candidates == (REF_A, REF_B)
        # NONE renamed: both keep the live legacy carrier as adopted
        assert a.legacy_carrier == b.legacy_carrier == LEGACY_CARRIER

    def test_a_case_insensitive_namespace_forms_one_legacy_group(self):
        registry = NativeLocatorRegistry()
        registry.adopt_legacy(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        registry.adopt_legacy(
            REF_B, namespace=NAMESPACE.swapcase(), route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        groups = registry.legacy_collision_groups()
        assert len(groups) == 1
        assert set(groups[0].candidates) == {REF_A, REF_B}

    def test_the_same_route_in_a_shared_group_namespace_collides(self):
        """Shared group secrets: two projects' refs whose carriers live
        in ONE group namespace collision-check together (the group is
        the namespace — the subjects being different changes nothing)."""
        registry = NativeLocatorRegistry()
        registry.adopt_legacy(
            REF_A, namespace=GROUP_NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        registry.adopt_legacy(
            REF_B, namespace=GROUP_NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        assert [group.candidates for group in registry.legacy_collision_groups()] == [
            (REF_A, REF_B)
        ]

    def test_resolving_an_ambiguous_legacy_carrier_refuses_every_candidate_named(self):
        registry = NativeLocatorRegistry()
        registry.adopt_legacy(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        registry.adopt_legacy(REF_B, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        with pytest.raises(CredentialRefusal, match="legacy_locator_collision") as caught:
            registry.resolve_legacy(
                LEGACY_CARRIER, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
            )
        assert sorted(caught.value.detail["candidates"]) == sorted([REF_A, REF_B])
        assert "first match" in str(caught.value.detail["instruction"])

    def test_resolving_an_absent_legacy_carrier_refuses_typed(self):
        registry = NativeLocatorRegistry()
        with pytest.raises(CredentialRefusal, match="legacy_locator_absent"):
            registry.resolve_legacy(
                LEGACY_CARRIER, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
            )

    def test_resolving_an_unambiguous_legacy_carrier_returns_the_owner(self):
        registry = NativeLocatorRegistry()
        registry.adopt_legacy(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        owner = registry.resolve_legacy(
            LEGACY_CARRIER, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        assert owner.credential_ref == REF_A

    def test_rotation_between_colliding_legacy_refs_refuses_until_resolved(self):
        """AC-03: a rotation onto the colliding ref cannot allocate
        while the ambiguity stands — the explicit operator resolution is
        the only path through."""
        registry = NativeLocatorRegistry()
        registry.adopt_legacy(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        with pytest.raises(CredentialRefusal, match="native_locator_collision"):
            registry.allocate(REF_B, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)

    def test_the_operator_resolution_keeps_one_legacy_and_frees_the_rest(self):
        registry = NativeLocatorRegistry()
        registry.adopt_legacy(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        registry.adopt_legacy(REF_B, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        resolved = registry.resolve_legacy_collision(
            namespace=NAMESPACE,
            route=DELIVERY_MODE_GITLAB_PROTECTED,
            legacy_carrier=LEGACY_CARRIER,
            keep_ref=REF_A,
        )
        by_ref = {allocation.credential_ref: allocation for allocation in resolved}
        assert by_ref[REF_A].legacy_carrier == LEGACY_CARRIER  # the keeper
        assert by_ref[REF_B].legacy_carrier == ""  # the loser dropped its claim
        assert by_ref[REF_B].carrier_name == native_locator_carrier(REF_B)
        # the group is GONE and the carrier now resolves uniquely
        assert registry.legacy_collision_groups() == []
        owner = registry.resolve_legacy(
            LEGACY_CARRIER, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        assert owner.credential_ref == REF_A
        # the loser can now allocate (the rotation proceeds)
        assert (
            registry.allocate(REF_B, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
            == by_ref[REF_B]
        )

    def test_the_resolution_refuses_a_keeper_outside_the_group(self):
        registry = NativeLocatorRegistry()
        registry.adopt_legacy(REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        registry.adopt_legacy(REF_B, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED)
        with pytest.raises(CredentialRefusal, match="legacy_locator_collision"):
            registry.resolve_legacy_collision(
                namespace=NAMESPACE,
                route=DELIVERY_MODE_GITLAB_PROTECTED,
                legacy_carrier=LEGACY_CARRIER,
                keep_ref="env:ANTHROPIC_AUTH_TOKEN",
            )

    def test_retiring_a_legacy_carrier_keeps_the_locator_and_the_audit_trail(self):
        registry = NativeLocatorRegistry()
        adopted = registry.adopt_legacy(
            REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        retired = registry.retire_legacy(
            REF_A, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
        )
        assert retired is not None
        assert retired.legacy_retired_at  # timestamped, never deleted
        assert retired.locator == adopted.locator
        with pytest.raises(CredentialRefusal, match="legacy_locator_absent"):
            registry.resolve_legacy(
                LEGACY_CARRIER, namespace=NAMESPACE, route=DELIVERY_MODE_GITLAB_PROTECTED
            )


# ----------------------------------------------------------------------
# The dispatch seam: the locator registry through delivery_plan
# ----------------------------------------------------------------------


class TestLocatorDeliveryPlanIntegration:
    async def test_github_native_dispatches_under_the_locator_carrier(self):
        locator_registry = NativeLocatorRegistry()
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
            locator_registry=locator_registry,
        )
        assert plan is not None
        allocation = locator_registry.allocation_for(
            "env:ANTHROPIC_AUTH_TOKEN",
            namespace=locator_namespace(SUBJECT, "github"),
            route=DELIVERY_MODE_GITHUB_NATIVE,
        )
        assert allocation is not None
        assert plan.transport_ref == allocation.carrier_name
        assert plan.dispatch_ref == allocation.locator
        assert plan.native_locator == allocation.locator
        assert plan.locator_namespace == locator_namespace(SUBJECT, "github")
        document = plan.as_document()
        assert document["native_locator"] == allocation.locator  # the evidence carries it
        assert "value" not in json.dumps(document)

    async def test_gitlab_native_dispatches_under_the_locator_carrier(self):
        locator_registry = NativeLocatorRegistry()
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="gitlab",
            environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
            locator_registry=locator_registry,
        )
        assert plan is not None
        assert plan.transport_ref == native_locator_carrier("env:ANTHROPIC_AUTH_TOKEN")
        assert plan.transport_ref != credential_secret_name("env:ANTHROPIC_AUTH_TOKEN")

    async def test_the_p05_pair_planned_through_the_registry_yields_two_transports(self):
        """THE seam-level proof of the fix: two colliding-legacy refs
        planned through the registry dispatch under two DISTINCT
        carriers — the silent alias is structurally gone."""
        locator_registry = NativeLocatorRegistry()
        plans = []
        for ref in (REF_A, REF_B):
            plan = await delivery_plan(
                _bound_registry(ref),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="gitlab",
                environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
                locator_registry=locator_registry,
            )
            assert plan is not None
            plans.append(plan)
        assert plans[0].transport_ref != plans[1].transport_ref
        assert plans[0].dispatch_ref != plans[1].dispatch_ref

    async def test_azure_keeps_the_env_slot_carrier_and_locates_the_dispatch_ref(self):
        """The Azure carrier is named after the provider's ENV SLOT
        (fixed per route — macro references cannot be composed at
        runtime), so there is no ref spelling in it to collide; the
        locator is still the dispatch identity the guard interpolates."""
        locator_registry = NativeLocatorRegistry()
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="azure",
            environ=_delivery_env(DELIVERY_MODE_AZURE_GROUP),
            locator_registry=locator_registry,
        )
        assert plan is not None
        assert plan.transport_ref == "forge-lane-credentials/ANTHROPIC_AUTH_TOKEN"
        assert plan.dispatch_ref == native_locator("env:ANTHROPIC_AUTH_TOKEN")
        assert plan.native_locator == native_locator("env:ANTHROPIC_AUTH_TOKEN")

    async def test_without_a_registry_the_legacy_spelling_stays_compatible(self):
        """NEVER a silent rename: without a locator registry the plan
        keeps the legacy ``FORGE_MODEL_<SEGMENT>`` spelling — the
        migration runbook owns the transition."""
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="gitlab",
            environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
        )
        assert plan is not None
        assert plan.transport_ref == credential_secret_name("env:ANTHROPIC_AUTH_TOKEN")
        assert plan.native_locator == ""

    async def test_the_locator_allocation_persists_across_the_plan(self, tmp_path: Path):
        path = tmp_path / "native_locator_map.json"
        locator_registry = NativeLocatorRegistry(path=path)
        await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="gitlab",
            environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
            locator_registry=locator_registry,
        )
        reloaded = NativeLocatorRegistry(path=path)
        assert (
            reloaded.allocation_for(
                "env:ANTHROPIC_AUTH_TOKEN",
                namespace=locator_namespace(SUBJECT, "gitlab"),
                route=DELIVERY_MODE_GITLAB_PROTECTED,
            )
            is not None
        )
