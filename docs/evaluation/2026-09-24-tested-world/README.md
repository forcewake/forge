# Tested-world system verification (2026-09-24)

R36-19 (#278): verify a complete two-service CandidateSet with real
dependency behavior. The harness is
`src/forge/adaptive/system_verification.py` (plus the additive
`freeze_tested_world`/`baseline_drift` composition in
`src/forge/adaptive/verification_sets.py`); the pins are
`tests/test_system_verification.py` (60 tests) and the eleven new
cases in `tests/test_adaptive_verification_sets.py`.

**Two separately green PRs ≠ one compatible system change.** The
two-writer qualification (#269) drove the models and recorded evidence
in-harness; the durable saga (#277) made publication itself durable;
the verdict binding (#273) binds a verdict to the exact candidate.
This slice is the missing leg: an INDEPENDENT EXECUTION against the
FROZEN TESTED WORLD. It composes with those siblings — it reuses
`EvidenceLedger`/`record_evidence` (the twin's records are literally
consumable by the two-writer `readiness` query), `db_upgrade_plan`,
`contract_checks`, `async_failure_scenarios`, `environment_compose`
and the freeze machinery — and duplicates none of them.

## The complete tested-world identity

`freeze_tested_world` (in `verification_sets.py`) completes the
existing freeze with the bundle/profile digests a whole world is
composed of, then persists both digests through `freeze_verified_world`.
The identity covers: changed AND unchanged repository revisions, the
EXACT image artifact of each member (a rebuilt image under an
UNCHANGED source SHA is a different world), the contract bundle, the
test bundle, the environment profile, the external service pins and
the policy refs. Every one of those inputs flipping moves the digest —
pinned exhaustively by test (including the image-rebuild-with-same-SHA
arm, the contract-bundle arm, and the determinism arm).

`baseline_drift` names the moved inputs structurally
(`member/<repo>/image_digest`, `test_bundle_digest`,
`environment_pin/<service>`, …) and flags
`source_sha_unchanged` on the rebuilt-image arm — the
`verification.baseline_drift` observability fragment. Provenance
(plan revision, work id) and this-run obligations (work-contract,
contract-bundle digests) are deliberately invisible to drift and to
the applicability digest, exactly as before.

## The verifier and its (lack of) authority

`run_system_verification(candidate_set, environment)` fail-closes, in
order: the environment document holds NO model credentials and NO
publication authority (`VerifierAuthorityError`, before anything
executes); the set carries a persisted world binding (unfrozen
refuses); the twin's services ARE the frozen membership and every
composed service resolves to a recorded exact artifact (unresolved
refuses). The report's `environment` fragment PROVES both absences —
absence as recorded fact, not as claim.

## The PYTHON-shaped twin

The first scenario proves the MECHANISM in-process (.NET/TRX scenarios
stay in the ExpectedReports machinery of `verification_binding.py`):

| Member | Role | Build behavior |
|---|---|---|
| `orders-api` | changed (producer) | emits the order event at dialect v2 (`id,total,region`) |
| `orders-projection` | changed (consumer) | requires dialect v2; integrates with the ledger baseline at `api/v3` |
| `ledger-baseline` | baseline (PINNED) | serves `api/v3` at its pinned digest; its branch head has moved PAST the pin |
| `orders-db` / `orders-bus` | external pins | the synthetic dependencies below |

The synthetic dependencies are real machinery, not mocks:

- **Schema upgrade** (`execute_schema_upgrade`, driven by
  `db_upgrade_plan`): a real sqlite file database at the pinned v1
  schema, 25 seeded synthetic rows, the migration ladder to v2 — and
  preservation under the release canary's fingerprint discipline:
  per-table row count plus sha256 over the ordered row-identity
  strings, compared across the upgrade. The schema must genuinely
  advance and accept new-shaped writes; a destructive
  DROP-and-recreate migration is CAUGHT by the fingerprint while the
  version ladder still advances (the discriminating arm — only the
  data fingerprint can catch it).
- **Idempotency** (`DoubleDeliveryHarness`, REDIS-less): three sqlite
  tables (idempotency key, projected effect, acknowledgement) so the
  crash window is a REAL transaction boundary — the effect and the
  idempotency key commit together, the ack is a separate later write
  the injected death skips. The full `async_failure_scenarios` catalog
  replays: crash between commit and ack (redelivered post-commit),
  pure duplicate redelivery, out-of-order arrival. Exactly-once is
  asserted per message as ONE projected effect regardless of delivery
  count — a JSON-schema match is not idempotency proof.

## Per-edge results

Three edges, each named by its members:

- `contract:orders-api->orders-projection` — 12 synthetic messages
  replayed from the producer build's dialect against the consumer
  build's requirement; the shared bundle's dialect is the referee that
  decides WHICH member a mismatch names;
- `baseline:orders-projection->ledger-baseline` — 15 synthetic rows
  served by the PIN (never the branch head) in its API's shape,
  projected by the consumer through a real sqlite round trip;
- `environment:integration` — the schema upgrade + the delivery legs;
  its evidence covers EVERY member (the compose binds them all).

A failed edge NAMES the failing member (`failed_member`) and blocks
`system_ready`; a failed edge records NO evidence. The old/new
producer-consumer combinations are pinned both ways: the old producer
build fails the edge naming the PRODUCER, the old consumer build names
the CONSUMER, and a stale baseline pin names the PINNED member.
`verification.report_coverage` records expected/verified/failed/missing
edges plus executed checks.

## Selective invalidation

`replay_against_changed_inputs(ledger, previous, current)` replays a
previously-passed record against changed dependency inputs: the drift
is named, translated into the per-dependency change vocabulary
(`MemberChange` — image rebuilds under an unchanged source SHA
included — `TestBundleChange`, `EnvironmentPinChange`) and applied.
ONLY the records whose CLAIMED inputs moved are superseded; the
records stay inspectable forever with the drift as their reason. The
matrix, as landed: a rebuilt baseline image invalidates the
baseline+environment edges and RETAINS the producer-consumer edge; a
changed test bundle invalidates every record judged under the old
bundle; a changed db pin invalidates every record from the frozen set
(records bind pins at persistence time — the same world is never
judged under two different databases); an unchanged world invalidates
nothing.

## Readiness: a query, three distinct booleans

`system_readiness(ledger, candidate_set, edges)` is a pure lookup over
executed evidence + applicability — it cannot re-run anything (it is
not handed anything executable). Blocked shapes are named per edge:
`missing` (no covering evidence), `stale` (superseded), `invalid`
(recorded against different frozen identities — stale evidence is
never silently reused), `failed` (the last executed report failed the
edge). One deliberate signature extension: a `CandidateSet` carries no
dependency topology, so the edges come from the scenario — the same
shape the two-writer `readiness` uses.

`verification_ready`, `merge_permitted` and `deploy_permitted` are
THREE DISTINCT booleans in the document: the query only answers the
first; the latter two record explicit human grants unchanged, and a
passed environment test authorizes no production migration (pinned: a
green upgrade+idempotency leg with a failed contract edge reports
`verification_ready=False` and both permissions `False`).

## What stays out of scope

No blanket proof of distributed-system properties; no production
traffic in the qualification fixture (issue's own boundary). The twin
is stdlib-sqlite-in-a-tempdir: real DDL/DML/transaction boundaries,
deterministic by construction — a provider-wired twin (real Postgres,
real broker) is future work on top of the same edges.
