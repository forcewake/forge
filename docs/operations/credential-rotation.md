# Operation-grant credential rotation (runbook)

R41-10 (#365). This is the runbook for rotating a BOUND model
credential on the `runner-redemption` delivery route — the flow where
the control plane mints an **operation grant** per attempt, the lane
**redeems** it through the lane-control endpoint, and the
`EnvBroker` resolves the bound ref into exactly one env slot.

The rotation contract in one sentence: **a rotation is a NEW binding
decision (revision N→N+1 at the same locator); every authorization
recorded under the old revision is refused typed, and the next dispatch
minted under the new revision is what recovers the lane.** Nothing
restarts, nothing is silently reused, and every refusal is typed with
zero emitted values.

Live-qualified on the current pairing by
[qualification/records/redemption-2026-09-27.json](../../qualification/records/redemption-2026-09-27.json)
(evidence:
[docs/evaluation/2026-09-27-redemption-pairing/](../evaluation/2026-09-27-redemption-pairing/));
the typed-refusal matrix is additionally covered offline by
`tests/test_operation_grant.py`, `test_credential_consumption.py`,
`test_credential_broker.py` and `test_credential_audit.py`.

## 0. The preflight (BEFORE anything rotates)

**Locator-to-env-slot compatibility** is the first check, and it runs
at TWO moments — both fail closed, both NAME the mismatch:

1. **At bind time** (the operator's earliest moment): the shipped
   registry (`ProjectCredentialRegistry.bind`) refuses an `env:` locator
   naming a slot other than the route's with the typed reason
   `binding_slot_mismatch` (observability
   `preflight.binding_slot_mismatch`), naming both the locator's slot
   and the binding's slot. Under the default `EnvBroker` an env locator
   stages under its OWN name, so the ref's name must BE the route's
   slot — for `anthropic-gateway` that is `env:ANTHROPIC_AUTH_TOKEN`.
   This is the #343 live incident (a ref named
   `env:FORGE_BROKER_MODEL_TOKEN`) moved from "discovered at the
   redemption endpoint after a lane booted" to "refused at the bind
   decision".
2. **At lane boot** (the defense that rides the DISPATCHED envelope):
   the lane package's own preflight
   (`forge.lane_driver.lane_binding_slot_preflight`) re-checks the
   dispatched `FORGE_CREDENTIAL_REF` against this lane's provider-route
   slot BEFORE the redemption HTTP call — so a registry document
   hand-edited past the bind seam (the only remaining route to a
   misbound binding) still fails closed at the lane with
   `preflight.binding_slot_mismatch` in the job trace, zero endpoint
   calls, zero model turns.

Operator check (run on the control-plane host, before a dispatch):

```bash
uv run python - <<'PY'
from forge.adaptive.project_credentials import binding_slot_preflight
# returns None when compatible; a mismatch document when not
print(binding_slot_preflight("anthropic-gateway", "env:ANTHROPIC_AUTH_TOKEN"))
PY
```

## 1. Rotate (the new decision)

Rotate = re-bind the SAME subject + route with the new value staged at
the SAME slot-named locator. The registry bumps the revision, archives
the superseded binding, and nothing restarts:

```bash
uv run python - <<'PY'
from pathlib import Path
from forge.adaptive.project_credentials import ProjectCredentialRegistry

registry = ProjectCredentialRegistry(path=Path("data/credential-bindings.json"))
binding = registry.bind(
    "gitlab/-/<project_id>",          # the run's canonical subject
    "anthropic-gateway",              # the provider route
    "env:ANTHROPIC_AUTH_TOKEN",       # the SLOT-NAMED locator (see §0)
    bound_by="<operator> (rotation <date>)",
    project_id=<project_id>,
)
print("rotation landed:", binding.credential_ref, "revision", binding.revision)
PY
```

The credential VALUE itself lives in the consumers' env slot (rotate it
in the deployment's secret store and recreate the consumers when the
value changes — `scripts/align_lab.py --extra-env
ANTHROPIC_AUTH_TOKEN=<new>` receipts the recreation; the ref and the
slot name do not change).

## 2. What the old revision's lanes see (typed refusals, by design)

- A lane dispatched BEFORE the rotation (its grant recorded revision N)
  redeems AFTER it → the endpoint answers **HTTP 403
  `binding_revision_mismatch`** (zero emitted values, no ledger row)
  and the lane fails CLOSED (`credential_redemption_failed`, zero model
  turns). There is no ambient fallback.
- An operator replaying an old generation's lane token → the same typed
  refusal (plus `superseded` for retired generations and
  `attempt_terminal` for finished attempts).
- The registry is re-read on EVERY dispatch command and EVERY
  redemption request — the rotation binds the NEXT dispatch with no
  consumer restart; an already-minted grant keeps the revision it
  recorded and is refused on mismatch.

## 3. Corrective rebind (if the rotation was WRONG — the misbound shape)

If a binding was created pointing at a locator that is not the slot
(the shape the preflight now refuses, but a hand-edited document or a
pre-preflight registry can still hold it), CORRECT it the same way as a
rotation: `bind()` with the slot-named ref. The corrective rebind is
itself revision N+1, so no refusal under the old revision is forgotten.

## 4. The new grant + lane recovery

Re-enter the blocked run through the native continuation command — a
lane that failed closed holds no checkpoint, so the restart mode is
explicit:

```text
@forge /retry <run_id> restart
```

The new dispatch mints a NEW attempt generation whose grant records the
LIVE revision (N+1); the lane's bootstrap redeems it, the consumer
receipt joins grant→redemption→attempt→consumer, and the model traffic
rides the rotated credential. Expected trail: the old generation's
failed lane (terminal `credential_redemption_failed`), then the new
generation's green redemption. Nothing else recovers the lane — do not
hand-edit grants, do not reseed values, do not restart consumers
expecting the old grant to apply.

## 5. Verify (all refs, zero secret bytes)

```bash
# the ledger: every successful redemption joined, every refusal absent
podman exec forge-postgres psql -U forge -d forge -c \
  "SELECT work_id, attempt_generation, binding_revision, outcome, created_at
   FROM credential_redemptions ORDER BY created_at DESC LIMIT 10;"

# the authority rows: one grant per (work, attempt, route), each with its
# absolute deadline
podman exec forge-postgres psql -U forge -d forge -c \
  "SELECT grant_id, attempt_generation, status, redemption_deadline
   FROM operation_grants ORDER BY created_at DESC LIMIT 10;"
```

Recovery is complete when the newest redemption row for the run joins
the newest authority row at the CURRENT binding revision.

## 6. Expiry and restart semantics (why "wait it out" is not recovery)

- The grant's `redemption_deadline` is ABSOLUTE (a persisted column set
  at authorization): a cold control-plane restart inside the window
  does not move it, an idempotent replay inside the window succeeds
  through the restarted plane, and past it the endpoint answers 403
  `grant_expired` — at the endpoint AND in the real lane.
- A NEW attempt generation retires the previous one's authority: the
  old generation's lane token answers the typed superseded refusal even
  before its window would have closed.
