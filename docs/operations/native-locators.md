# Native credential locators: collision-safe carriers, the locator map, and the legacy migration (Q39-04)

How a bound credential ref becomes the NAME of a provider-native secret,
why the pre-#323 spelling could collide two refs onto one carrier, and
the operator runbook for adopting the collision-safe locators
([#323 / Q39-04](https://github.com/forcewake/forge/issues/323); the
delivery design is
[#303 / R38-02](https://github.com/forcewake/forge/issues/303), the
consumption half is
[#305 / R38-04](https://github.com/forcewake/forge/issues/305)).

## The defect (probe P05)

`credential_secret_segment()` builds the native carrier name by mapping
every punctuation character to `_` and upper-casing. Two distinct refs —
`vault:kv/team-a` and `vault:kv/team_a` (and every dash/dot/case
variant) — sanitized to the SAME segment `VAULT_KV_TEAM_A`, so the
GitHub secret / GitLab protected+masked variable / Azure group secret
`FORGE_MODEL_VAULT_KV_TEAM_A` was ONE carrier shared by two credentials:
rotation between them was a silent alias, and in a shared group
namespace two projects' credentials overwrote each other.

## The locator

`native_locator(ref)` = a bounded ASCII-readable PREFIX + `_` + the
first 12 hex of sha256 over the FULL ref, canonical uppercase:

```
vault:kv/team-a  →  TEAMA_505D2A14EE03
vault:kv/team_a  →  TEAM_A_FFF203E029E0
carrier name     →  FORGE_MODEL_<locator>
```

- **Distinct refs can never share a locator** — the digest is over the
  exact ref (UTF-8), so slash/dash/underscore/dot/case and Unicode
  differences all land in the digest; the readable prefix overlapping
  (`team-a` and `team_a` both read `TEAM…`) is cosmetic.
- **The charset is `[A-Z0-9_]`** — the intersection GitHub secret names,
  GitLab CI/CD variable names and Azure variable-group names accept.
  A `-` separator (the issue's illustrative spelling) is NOT
  provisionable: GitHub's secret API refuses a hyphenated name with a
  422, so the landed separator is `_`. Canonical uppercase also cannot
  near-miss under GitHub's case-insensitive secret-name uniqueness.
- **The prefix** is the ref's leaf identifier (the most
  human-identifying segment — `team-a` of `vault:kv/team-a`),
  upper-cased, non-`[A-Z0-9_]` characters dropped, trimmed, truncated to
  24 characters. Overlength and Unicode refs get the same bounded form
  (`vault:kv/café-key` → `CAFKEY_…` — the digest disambiguates).
- **The truly unrepresentable refuse typed** — a ref with no ASCII
  identifier character anywhere (`native_locator_unrepresentable`, with
  the re-bind instruction). An unreadable prefix helps no operator
  reconcile a carrier by hand.

Azure is the documented exception: its carrier is named after the
provider's ENV SLOT inside the authorized variable group
(`forge-lane-credentials/ANTHROPIC_AUTH_TOKEN`) because Azure macro
references cannot be composed from runtime parameters — there is no ref
spelling in that name to collide. The locator is still the dispatch
identity (`FORGE_CREDENTIAL_REF`) the lane guard interpolates.

## The locator map (the persisted registry)

`NativeLocatorRegistry` persists the `native_locator_map` JSON document
(schema `forge.credential.native-locator-map/1`, exclusive-lock +
atomic-rename writes, the same multi-writer contract as the binding
registry): one row per (namespace, route, ref) — the ref, its locator
and carrier, the allocation timestamp (frozen at first allocation), and
for migrated refs the LEGACY carrier plus its retirement timestamp.

- **Namespaces** are `<profile>/<family>/<connection>/<native_id>` —
  the canonical subject IS the provider project/repo identity, so two
  independent projects' namespaces are independent (the same locator
  may live in both; no global uniqueness is required or wanted).
  Namespaces compare CASE-INSENSITIVELY (GitHub secret names are
  case-insensitively unique — `GitLab/Example/1` and `gitlab/example/1`
  are one namespace). A SHARED namespace (a GitLab group, a GitHub
  org/environment) is named explicitly by the operator through the
  registry API (`adopt_legacy`/`allocate` with `namespace=...`) — never
  guessed from a subject it does not cover.
- **Allocation** (`allocate`) is idempotent per namespace+route+ref and
  refuses `native_locator_collision` (typed, BOTH refs named,
  observability `credential.native_locator_collision`) when the locator
  is already held by a different ref in the same namespace+route — the
  digest makes that near-impossible, but the JSON map is
  operator-editable and the registry defends its own invariant.
- `delivery_plan(..., locator_registry=...)` allocates through the map:
  the native GitHub/GitLab transports become `FORGE_MODEL_<LOCATOR>` and
  `dispatch_ref` the locator. WITHOUT a registry the legacy
  `FORGE_MODEL_<SEGMENT>` spelling stays — **never a silent rename**
  (see the migration below).

## The legacy migration runbook

Existing `FORGE_MODEL_<SEGMENT>` mappings are NEVER renamed silently —
a live secret's name is changed only by the operator, in this order:

1. **Inventory.** Adopt every pre-upgrade ref into the map:

   ```python
   registry = NativeLocatorRegistry(path="native_locator_map.json")
   registry.adopt_legacy("vault:kv/team-a", namespace=ns, route=mode)
   ```

   `adopt_legacy` records the LIVE legacy carrier beside the computed
   locator — nothing is renamed. `legacy_collision_groups()` lists every
   ambiguity the lossy spelling created (the P05 pairs, the
   case-insensitive namespaces, the shared group secrets).

2. **Resolve each collision group explicitly.**
   `resolve_legacy(segment)` refuses `legacy_locator_collision` with
   EVERY candidate named — never a first match. The operator decides
   which ref keeps the live legacy carrier:

   ```python
   registry.resolve_legacy_collision(
       namespace=ns, route=mode,
       legacy_carrier="FORGE_MODEL_VAULT_KV_TEAM_A",
       keep_ref="vault:kv/team-a",
   )
   ```

   The keeper keeps the legacy carrier; every other candidate drops its
   legacy claim and owns its collision-safe locator.

3. **Create-new.** Provision each migrated ref's locator carrier on the
   provider (`gh secret set FORGE_MODEL_TEAMA_505D…`, the GitLab
   protected+masked variable, the group secret) with the same value the
   legacy carrier holds. The old carrier STAYS.

4. **Verify sentinel consumption.** Dispatch through the locator
   registry and confirm the consumer receipt / lane bootstrap consumed
   the NEW carrier — the conformance gate's `sentinel_locator` arms
   prove the shipped templates consume a locator-shaped ref; the
   run-level proof is the lane's `credential_consumption` record naming
   the attempt.

5. **Retire-old.** After the locator carrier is verified live,
   `retire_legacy(ref, ...)` stamps the legacy carrier retired (never
   deleted — the audit trail keeps the mapping), and the operator
   deletes the provider-side secret at their leisure.

A restarted control plane finds the map intact (partial rollout): old
mappings keep their live names, allocations keep their frozen
timestamps, and unresolved legacy groups still refuse typed.

## Conformance by driver and installed template

`delivery_template_conformance` gained two dimensions (#323):

- **The driver dimension.** On the gitlab profile every driver ships
  its OWN recipe — `delivery_plan(..., driver="claude-sdk-lane")`
  validates the SDK lane's template, not `claude-code.gitlab-ci.yml`.
  A driver whose recipe does not structurally implement the negotiated
  route (the guard, the consumer mapping of the provider env slot, the
  fail-closed marker — structural checks over substring presence)
  refuses `delivery_template_mismatch`; an unknown driver refuses
  `consumer_route_unknown`.
- **The installed-template digest.** `template_identity_digest(text)`
  (first 16 hex of sha256) is the value onboarding records for the
  INSTALLED target template. `delivery_plan(...,
  installed_template_digest=...)` refuses
  `profile_template_digest_mismatch` BEFORE any marker check: the LOCAL
  shipped template passing is not evidence about a different installed
  text — an old target template refuses before model execution even
  when the newer local template passes.

The release gate (`scripts/gate_conformance.py`, check
`native-locators`) pins both on every push: the encoding distinctness
over the P05 pair and variants, the per-driver template route matrix
(the digest inventory is recorded per validated template), and the
self-tests (the legacy lossy encoder MUST be flagged; a wrong driver's
template, a mismatched digest and an unknown driver MUST refuse).

## Observability spellings

| Spelling | Raised by |
|---|---|
| `credential.native_locator_collision` | `native_locator_collision`, `legacy_locator_collision` (both name every candidate) |
| `profile.template_digest_mismatch` | `profile_template_digest_mismatch` |
| `credential.consumer_route_unknown` | `consumer_route_unknown` |
