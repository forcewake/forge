# Verified executor against built artifacts (2026-09-24)

R37-15 (#296): execute the independent system verification against
REAL candidate artifacts and dependencies, in a SEPARATE trusted
executor whose isolation is proved from the launched process. The
harness is `src/forge/adaptive/verification_executor.py`; the pins are
`tests/test_verification_executor.py` (38 tests) and the
customer-level traces in
`tests/production_entry/test_system_verification_executor.py`
(PE-8a..PE-8d, 5 tests). The in-repo twin
(`src/forge/adaptive/system_verification.py`, #278) STAYS, explicitly
labeled the DETERMINISTIC REFERENCE (`TWIN_REFERENCE_LABEL`) — it pins
the mechanism's semantics cheaply and reproducibly; the executor
proves them against real artifacts and processes.

## The three gaps this closes (against the #278 twin)

| Gap in the twin | What the executor does |
|---|---|
| synthetic services, behavior declared not built | the two services are REAL INSTALLABLE WHEELS the executor installs and runs |
| in-process delivery | a socket-based fake broker SUBPROCESS; duplicate delivery injected at the socket |
| environment document checks declared fields | a scrubbed LAUNCH plus safe DENY PROBES from inside the launched process |

## The executor and its isolation contract

`VerificationExecutor.run(bundle, out)` launches the shipped module as
a REAL subprocess
(`python -m forge.adaptive.verification_executor --candidate-set <doc>
--out <report>`) under a scrubbed environment: the child env is the
parent's intersected with an explicit allowlist
(`DEFAULT_ENV_ALLOWLIST` = PATH, HOME, LANG, LC_ALL, TMPDIR, UV cache
locations). No model keys, no provider tokens, no publication
credentials, no proxy configuration — anything not listed is dropped,
and the launch receipt records the exact keys passed and any explicit
additions.

Isolation is then PROVED BY PROBE, not by DTO fields. Inside the
launched process, two safe deny probes run:

- **sentinel-egress** — an HTTP GET to a sentinel endpoint reachable
  only with egress/proxy credentials. The probe presents the first
  credential-shaped env value it can find (name matching
  TOKEN/KEY/SECRET/PASSWORD/CREDENTIAL/PROXY). 401/403/407 →
  `denied-unauthorized`; connection refused → `denied-refused`; a 2xx
  → `violated`.
- **provider-api** — a provider-API-shaped call whose token must be
  absent (same presentation rule, `Authorization: Bearer …`).

Any `violated` outcome — or any credential-shaped key surviving the
scrub — writes the report with `isolation_violated: true`, exits
`3 (EXIT_ISOLATION_VIOLATED)`, and NOTHING verifies. The probes have
teeth: PE-8d launches with `EGRESS_TOKEN` deliberately riding the
explicit additions, the credential actually reaches the sentinel, the
sentinel answers 200, and the run dies closed. Pinned both ways: a
dev environment full of `GITLAB_TOKEN`/`OPENAI_API_KEY`/`HTTP_PROXY`
(PE-8b) verifies green because the scrub drops them; the env-clean CI
shape (gate 2) passes because nothing depended on them existing.

## Built artifacts, not models

The tested services are the two fixture projects under
`evaluation/tested_world/` — `orders-api` (producer) and
`orders-projection` (consumer), each with a pyproject, a contract
module and its own selftest suite. The tests BUILD them with
`uv build` (session-cached) and the executor:

1. verifies each wheel FILE's sha256 against the digest frozen in the
   candidate set (`member.image_digest` IS `sha256:<wheel sha256>`);
2. installs those exact bytes into its OWN venv (uv, stdlib fallback)
   and re-digests the installed source via the dist's
   `direct_url.json`;
3. runs the wheels' OWN selftests in that venv (the unit pipelines are
   the artifact's, not forge's);
4. reads each installed wheel's declared contract document and checks
   it against the frozen referee dialect;
5. drives the integration scenario by importing the INSTALLED modules
   (`orders_api.produce`, `orders_projection.schema`,
   `orders_projection.project`, `orders_projection.consumer`) — never
   the in-repo source.

A wheel file whose digest is not the frozen one is REFUSED (never
installed): the edges covering that member fail naming it, the others
still run — selective by member. Pinned: a stale producer fails
contract+environment while the baseline edge still passes; a stale
consumer fails every edge (all cover it).

Determinism note (honest): hatchling builds are byte-deterministic —
same source, same wheel digest — so an unchanged-source rebuild
correctly invalidates NOTHING. The "rebuilt artifact under an
unchanged source SHA" arm is therefore carried where it is real: the
ledger-baseline PIN (whose image is not a deterministically-built
artifact) — switching only its image digest under the same oid
invalidates baseline+environment evidence and retains the contract
edge, with `source_sha_unchanged: true` on the drift row (PE-8c).

## Real dependency scenarios

- **Seeded schema upgrade** — the sqlite ladder runs INSIDE the
  executor as the INSTALLED wheel's own code
  (`orders_projection.schema`): v1 baseline built and seeded with 25
  synthetic rows, fingerprinted (counts + sha256 over ordered row
  identities), upgraded to v2 (adds `region`), fingerprinted again —
  preservation means both equal; the schema must advance and accept
  new-shaped writes. Never from an empty schema.
- **Redelivery over real sockets** — the fake broker is its own
  subprocess (an asyncio TCP server, `--serve-broker`): the executor
  dials it as controller and publishes the producer-emitted batch over
  TCP; the INSTALLED consumer (its own subprocess from the venv)
  subscribes, receives each message TWICE (the duplicate injected at
  the socket), applies its idempotent handler (effect + dedup key in
  one sqlite transaction, the ack a separate later write) and acks
  each delivery. The outcome (`dependency.redelivery_outcome`) is
  cross-checked from BOTH sides: the broker's journal (deliveries
  sent, acks seen on the wire) and the consumer's own durable rows.
  8 messages → 16 deliveries, 16 acks, exactly one business effect per
  message. No blanket exactly-once claim: with the old-dialect
  consumer the same arm reports the ACTUAL rejections
  (`exactly_once_all: false`) and blocks readiness.

## The report

Per-edge results (the twin's three edges, member-for-member) carrying
the executor receipt — every command's argv, exit code and log
sha256; the installed wheels' dist/version/file+installed digests; the
authority receipt (env keys seen by the launched process, allowlist,
deny-probe outcomes, verdict) — plus `verification.report_coverage`,
evidence records bound to the frozen world (the SAME `EvidenceLedger`
the twin and the two-writer readiness consume), and the readiness
document. `verification_ready`, `merge_permitted` and
`deploy_permitted` remain THREE DISTINCT booleans: a green verification
grants neither merge nor deploy; explicit human grants are recorded
verbatim by the parent's `executor_readiness` query and never derived.
Exit codes: 0 clean, 2 usage, 3 isolation violated, 4 verification
failed; the report file is ALWAYS written.

## Honest evidence classes (what this is, and is not)

- The two services are FIXTURE WHEELS built from
  `evaluation/tested_world/` — real installable packages with their
  own contract modules and tests, but not customer images. A customer
  rollout binds the customer's built artifacts the same way (digest →
  frozen member identity → installed bytes).
- The broker is a LOCAL FAKE with REAL sockets (loopback TCP, real
  frames, real duplicate injection) — not the customer's broker.
- The sentinel and provider endpoints are local gated HTTP servers —
  they stand in for "reachable only with egress/provider credentials";
  the probe mechanism (present what survived the scrub; 2xx = caught)
  is the part that generalizes.
- Isolation is VERIFIED-BY-PROBE: the launch scrub is policy, the
  probes are the behavioral check, and the receipt is the evidence.
  What is NOT proven: that no credential exists outside the
  environment axis (filesystem, agent tooling) — those remain lane
  policy.
- The ledger-baseline member is a pinned IMAGE identity without a
  wheel (the pin discipline, same as the twin); the baseline leg runs
  the installed consumer's projection code against the pin's served
  API shape.

## What stays out of scope

No arbitrary distributed-system proofs; no production traffic replay;
no general deployment platform; no cross-process crash injection in
the socket arm (the crash-between-commit-and-ack window stays pinned
in the deterministic twin, whose harness remains the reference
semantics for it).
