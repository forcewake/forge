# R40-07 (#343) — operation-grant redemption, qualified LIVE

The 0.39.0 live trace ([gitlab-ce-v1@0.39.0 record](../../../qualification/records/gitlab-ce-v1@0.39.0.json))
reached `ready_for_human` on the native/protected-variable credential route and named
operation-grant LIVE redemption as the remaining open proof class. This evaluation
closes it: a REAL native dispatch under the **runner-redemption** delivery mode
MINTED the operation grant through the native path (never seeded), the actual lane
bootstrap REDEEMED it through the lane-control endpoint, and the actual model
consumer presented the **BROKER-SELECTED** sentinel — never the competing AMBIENT
one.

## The composition (every axis receipted)

| Axis | Value | Receipt |
| --- | --- | --- |
| Control plane | the working-tree alignment build (image `sha256:ddcb9137…`, schema head 031 — migration 029 `operation_grants` deployed by THIS alignment) | `alignment-receipts.json` (4 runs) |
| Delivery mode | `FORGE_CREDENTIAL_DELIVERY=runner-redemption` pinned on BOTH consumers (explicit selection, never defaulted) | `alignment-receipts.json` |
| Lane package | `git+https://github.com/forcewake/forge@b521e1a…` (the PROMOTED v0.39.0 tree, pushed sha; the working tree is never pushed) | `live-run-evidence.json#phases.setup.variables` |
| Template | the SHIPPED `ci/templates/claude-sdk-lane.gitlab-ci.yml` VERBATIM (`sha256 3d74be37…`, byte-identical to the frozen profile's template) | seed commit `d3351d0c` |
| Runner | unraid docker-executor (GitLab runner id 4) — the same frozen profile | profile manifest |
| Registry | `data/credential-bindings.json` (gitignored; REFS only): subject `gitlab/-/132` → `anthropic-gateway` → `env:ANTHROPIC_AUTH_TOKEN` | `live-run-evidence.json#phases.setup.binding` |
| Model endpoint | the local recorder (`recorder_server.py`, podman-published on the lab host LAN IP) — ZERO real model calls; the reach probe ran first from a scratch runner job (job 985 green) | `live-run-evidence.json#phases.reach` |

## The two sentinels

- **AMBIENT** — the disposable project's ordinary `ANTHROPIC_AUTH_TOKEN` CI variable
  (exactly the credential an ambient-fallback lane would have presented). Value in
  the maintainer-private state under `data/`; sha256 `7d6359fb…` recorded.
- **BROKER-SELECTED** — the value behind the bound ref, delivered ONLY through
  `/lane/credentials/redeem`. sha256 `6e3098b5…` recorded.

## The trace (run `3360ff64…`, issue #1, disposable project 132)

1. `@forge /implement` → the evidence-backed plan (claude-sdk-lane, glm-5.3-flash)
   — run created; **no grant exists yet** (asserted against the authority table).
2. `@forge /go` → the REAL dispatch mints grant `c7faeaa3…` (attempt 0) — under the
   OPERATOR-MISBOUND ref `env:FORGE_BROKER_MODEL_TOKEN`. The lane's redemption was
   refused typed **`staged_slot_mismatch`** (the EnvBroker staged under the ref's own
   env name, not the binding's slot — the endpoint's slot guard refused with ZERO
   emitted values; the lane failed CLOSED, the recorder stayed silent). **This is a
   live-found configuration lesson, not a code defect** — the offline suites bind
   `env:ANTHROPIC_AUTH_TOKEN` exactly because the EnvBroker stages under the ref's
   name. The runbook's redemption section now documents the invariant.
3. Corrective rebind (registry revision 2, ref `env:ANTHROPIC_AUTH_TOKEN`) +
   re-alignment pinning the slot-named broker env → `@forge /retry <run> restart` →
   generation 1's grant `cb80a3ca…` minted natively → the lane bootstrap REDEEMED
   (audit row 1) → the consumer receipt in the lane's own artifacts joins the
   authority row (`grant_id` `cb80a3ca…`, `redemption_id` `6b665cf4…`,
   `broker_receipt_id` `e9d25820…`, `binding_revision` 2) → the vendor client
   (claude-cli, agent-sdk) POSTed `/v1/messages?beta=true` with **the broker
   sentinel** (the recorder's single capture digest-matches it; the ambient digest
   appears NOWHERE) → the recorder's 400 fails the turn (`api_error`) with ZERO
   model spend — the identity proof stands on the capture, not on a completed turn.

## The arms (all live, all typed)

| Arm | Outcome |
| --- | --- |
| Generation retirement | `/retry` → gen-2 grant `9abf7f9e…`; the gen-2 lane followed its OWN grant (receipt join verified); gen-1's token refused **typed** (superseded generation) |
| Cold restart (mid-window) | `podman restart forge-app` WHILE the gen-3 lane was in flight; the grant's absolute deadline did NOT move; the operator replay redeemed through the RESTARTED plane (HTTP 200, idempotent — `grant 7421e65c…`) and the in-flight lane's own redemption also landed (audit rows 2→4); replaying a FINISHED attempt is refused typed `attempt_terminal` (recorded) |
| Wrong ref (live attempt) | **typed `grant_ref_mismatch`**, zero values |
| Wrong route (live attempt) | **typed `grant_route_mismatch`** (the confused-deputy guard) |
| Binding rotation | the registry rotated (same ref, revision 2→3) BETWEEN the gen-4 grant mint and the lane's redemption: the endpoint refused **typed `binding_revision_mismatch`** (app log 14:06:22Z), the lane failed CLOSED with zero model calls, the recorder silent; a grant minted AFTER the rotation redeems (the registry is re-read on EVERY dispatch command AND every redemption — live-found, recorded) |
| Expired grant | consumers re-aligned with a 20 s grant window (receipted): the second issue's grant expired at its absolute deadline — the endpoint refused **typed `grant_expired`** AND the real lane failed closed on the expired window (zero model turns) |

The append-only `credential_redemptions` ledger holds **6 rows, every one joined to
its grant**; the seven refused redemptions (misbound ref, superseded tokens ×2,
terminal replays ×2, rotation, expiry) left **zero rows** — refusals emit nothing.

## Honesty ledger

- The identity proof used a RECORDING model endpoint, not a real provider pass —
  issue #343 scope 3 permits exactly this ("a controlled model endpoint; a separate
  bounded real-model pass only if authorized"). Zero lane model spend; the planner
  legs cost 1 call / 769 tokens on run 1 (glm-5.3-flash; issue 2's planner call has
  no separate run-budget row). Total model spend well under $0.01.
- The control plane is the WORKING-TREE alignment build (the R40 cycle's #341/#342
  machinery is uncommitted-ahead of v0.39.0); the LANE ran the PROMOTED v0.39.0
  package (`b521e1a…`) whose lane-side redemption consumer predates R40-06 — the
  endpoint (the authority) enforced the full #342 identity validation; the lane's
  own response-verification is the #303/#320 vintage, which accepts the response
  shape (extra R40-06 fields are additive). The two identities are bound separately,
  never merged.
- The first alignment's misbound ref (`env:FORGE_BROKER_MODEL_TOKEN`) was an
  OPERATOR error, caught live by the typed slot guard — recorded here and in the
  bundle, never papered over.
- The disposable project (132) was deleted after capture (receipt in
  `live-run-evidence.json#phases.teardown`); the recorder was stopped and removed.
- Values (sentinels, tokens, secrets) appear NOWHERE in this tree — the bundle
  carries sha256 digests; the private state lives under gitignored `data/`.

## Files

- `live-run-evidence.json` — the resumable evidence bundle (refs/digests only).
- `alignment-receipts.json` — the four `align_lab.py` runs (delivery-mode selection,
  corrective rebind alignment, short-window alignment, canonical restore).
- `recorder_server.py` — the recording model endpoint (the committed instrument).
- Driver: `scripts/run_redemption_qualification.py` (repo `scripts/`).
