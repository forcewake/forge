# Verified executor against built artifacts (2026-09-24)

R37-15 (#296): execute the independent system verification against
REAL candidate artifacts and dependencies, in a SEPARATE trusted
executor whose isolation is proved from the launched process.
R38-07 (#308) later tightened the isolation claims to match what is
actually observable — the isolation contract below is the R38-07 one,
and the wording that preceded it is explicitly downgraded (see "The
claims downgrade"). The harness is
`src/forge/adaptive/verification_executor.py`; the pins are
`tests/test_verification_executor.py` (62 tests) and the
customer-level traces in
`tests/production_entry/test_system_verification_executor.py`
(PE-8a..PE-8e, 6 tests). The in-repo twin
(`src/forge/adaptive/system_verification.py`, #278) STAYS, explicitly
labeled the DETERMINISTIC REFERENCE (`TWIN_REFERENCE_LABEL`) — it pins
the mechanism's semantics cheaply and reproducibly; the executor
proves them against real artifacts and processes.

## The three gaps this closes (against the #278 twin)

| Gap in the twin | What the executor does |
|---|---|
| synthetic services, behavior declared not built | the two services are REAL INSTALLABLE WHEELS the executor installs and runs |
| in-process delivery | a socket-based fake broker SUBPROCESS; duplicate delivery injected at the socket |
| environment document checks declared fields | a scrubbed, clean-HOME, narrowed-PATH LAUNCH plus safe CONTROLLED PROBES from inside the launched process |

## The executor and its isolation contract (R38-07)

`VerificationExecutor.run(bundle, out)` launches the shipped module as
a REAL subprocess
(`python -m forge.adaptive.verification_executor --candidate-set <doc>
--out <report> --enforcement-profile <doc>`) under a scrubbed
environment: the child env is the parent's intersected with an explicit
allowlist (`DEFAULT_ENV_ALLOWLIST` = PATH, HOME, LANG, LC_ALL, TMPDIR,
UV cache locations). No model keys, no provider tokens, no publication
credentials, no proxy configuration — anything not listed is dropped,
and the launch receipt records the exact keys passed and any explicit
additions. Two enforcement overrides ride on top of the scrub:

- **Clean HOME** — the child does NOT inherit the parent HOME. The
  launcher provisions an isolated EMPTY home (recreated on every
  launch) and the launched process reads its OWN home for the
  credential file shapes (`~/.netrc`, `~/.git-credentials`,
  `~/.config/git/credentials`, `~/.aws/credentials`,
  `~/.docker/config.json`, `~/.kube/config`): a planted credential
  file under the parent home is UNREADABLE from the verifier, and a
  readable shape under the verifier's own home is a hygiene violation
  that fails the run closed.
- **Narrowed PATH** — the child's executable search path is the
  system minimum (`/usr/local/bin`, `/usr/bin`, `/bin`, `/usr/sbin`,
  `/sbin`, existence-filtered) plus the resolved venv tooling; the
  parent's full PATH (with any planted bin directory) never reaches
  the child.

The launcher records this whole configuration — env allowlist and its
version, HOME/PATH policy and exact entries, the network policy class
— as the enforcement profile, digested in the report as
`verification.enforcement_profile_digest`. Every isolation claim is
scoped to exactly this profile, and the launched process VALIDATES the
declaration against what it actually observes (HOME value, PATH
entries, env-key membership); drift is a hygiene violation, not a
silent pass.

### The controlled probes and the five-outcome taxonomy

Inside the launched process, each probe class (sentinel-egress,
provider-api) runs as a controlled pair against a real local HTTP
server with TWO kinds of routes: the gated deny route and a
positive-control route that answers 200 WITHOUT credentials. The
control dials first; the deny probe runs ONLY while the control is
green. Every attempt lands in exactly one of five outcomes:

| Outcome | Meaning |
|---|---|
| `authorized_control_succeeded` | the positive control answered 200 — the endpoint is alive, the class's denial test is meaningful |
| `expected_denial_observed` | 401/403/407 to the probe credential, with the control green — the only outcome that counts as a denial OBSERVED |
| `unavailable` | transport failure, DNS failure, or any 5xx — a dead or unhealthy endpoint demonstrates NOTHING |
| `inconclusive` | a timeout (or a non-auth-shaped answer) — neither service nor denial |
| `violation` | a 2xx answer to credential material — the isolation did not hold |

The probes present an explicit SYNTHETIC qualification credential
(`SYNTHETIC_PROBE_CREDENTIAL`), never an ambient key: the
first-surviving-key presentation heuristic is gone, and the
environment scan is now purely the ABSENCE assertion — any
credential-shaped env survivor is a hygiene violation that fails the
run closed. The probes keep their teeth both ways: an endpoint that
answers 200 to the synthetic credential does not gate (`violation`),
and a deliberately leaked token riding the launch is named by the
absence scan from the launched process (PE-8d).

### The verdict: three separate fields, never one boolean

The authority receipt reports the isolation as THREE DISTINCT fields
plus a tri-state:

- `environment_hygiene` — `passed`/`failed`: env allowlist respected,
  zero credential-shaped survivors, clean HOME (no readable shapes),
  PATH within the declared profile;
- `credential_non_disclosure` — `passed`/`violated`/`unproven`: no
  credential material worked from the verification process;
- `network_enforcement` — `demonstrated`/`unproven`: every probe class
  had a live positive control AND observed its expected denial;
- `isolation` — `proven` / `unproven` / `violated`.

A `proven` verdict REQUIRES all three: the positive controls alive,
one `expected_denial_observed` per probe class, zero violations,
clean HOME/PATH. Anything less is `unproven` with the NAMED blocker
per class — a 503, DNS failure or timeout at the sentinel can NEVER
alone yield an isolation pass (the run exits
`5 (EXIT_ISOLATION_UNPROVEN)` and nothing verifies). A `violated`
verdict (leak, readable home shape, ungated endpoint) exits
`3 (EXIT_ISOLATION_VIOLATED)` and nothing verifies. A
security-prerequisite failure BLOCKS verification — it is never
misclassified as a code defect on some member (`require_isolated`
raises `IsolationViolated`/`IsolationUnproven`, and `executor_readiness`
fails every edge in both cases).

## The claims downgrade (what the old wording overstated)

Before R38-07 the receipt collapsed isolation into one
`isolated: true` boolean, and the deny probes classified ANY transport
failure as `denied-refused` and any 500/503 as `denied-other-status` —
both counted as successful denials, so a DEAD endpoint "proved"
containment (external review 59ba869, probe P04 reproduced it), and
the subprocess inherited the parent HOME and full PATH. Those claims
are RETRACTED as of this note:

- The old `denied-*` outcomes no longer exist; historical reports that
  carry them did NOT prove network enforcement — only that the probes
  failed to reach anything. They remain valid evidence of
  environment-scrub absence only.
- The old `isolated: true` was, at best, environment-hygiene evidence.
  Environment hygiene, credential non-disclosure and actual network
  enforcement are now reported as separate fields, and enforcement
  claims additionally require the positive control to have been alive.
- Isolation claims are scoped to the recorded enforcement profile
  (`verification.enforcement_profile_digest`); a launch without that
  record made no scoped claim at all.

What is still NOT proven (unchanged in honesty): that no credential
exists outside the environment+HOME axes (agent tooling, other mounts)
— those remain lane policy; and that process separation is an
adversarial sandbox.

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

## Real dependency scenarios — reference coverage, labeled

- **Seeded schema upgrade** (`orders-db`, labeled
  `reference-coverage` in the environment profile and the report) —
  the sqlite ladder runs INSIDE the executor as the INSTALLED wheel's
  own code (`orders_projection.schema`): v1 baseline built and seeded
  with 25 synthetic rows, fingerprinted (counts + sha256 over ordered
  row identities), upgraded to v2 (adds `region`), fingerprinted again
  — preservation means both equal; the schema must advance and accept
  new-shaped writes. Never from an empty schema. This is reference
  coverage of the upgrade mechanics, NOT production PostgreSQL proof.
- **Redelivery over real sockets** (`orders-bus`, labeled
  `reference-coverage`) — the fake broker is its own subprocess (an
  asyncio TCP server, `--serve-broker`): the executor dials it as
  controller and publishes the producer-emitted batch over TCP; the
  INSTALLED consumer (its own subprocess from the venv) subscribes,
  receives each message TWICE (the duplicate injected at the socket),
  applies its idempotent handler (effect + dedup key in one sqlite
  transaction, the ack a separate later write) and acks each delivery.
  The outcome (`dependency.redelivery_outcome`) is cross-checked from
  BOTH sides: the broker's journal (deliveries sent, acks seen on the
  wire) and the consumer's own durable rows. 8 messages → 16
  deliveries, 16 acks, exactly one business effect per message. No
  blanket exactly-once claim: with the old-dialect consumer the same
  arm reports the ACTUAL rejections (`exactly_once_all: false`) and
  blocks readiness. This is reference coverage of the redelivery
  mechanics over real loopback sockets, NOT production RabbitMQ proof.

## The report

Per-edge results (the twin's three edges, member-for-member) carrying
the executor receipt — every command's argv, exit code and log
sha256; the installed wheels' dist/version/file+installed digests; the
authority receipt (env keys seen by the launched process, allowlist,
credential-shaped survivors, observed HOME + home shapes checked and
found, observed PATH entries, per-class control and deny outcomes with
blockers, the three verdict fields and the tri-state isolation) — the
enforcement profile and its digest, the `dependency_coverage`
reference-coverage labels, `verification.report_coverage`, evidence
records bound to the frozen world (the SAME `EvidenceLedger` the twin
and the two-writer readiness consume), and the readiness document.
`verification_ready`, `merge_permitted` and `deploy_permitted` remain
THREE DISTINCT booleans: a green verification grants neither merge nor
deploy; explicit human grants are recorded verbatim by the parent's
`executor_readiness` query and never derived. Exit codes: 0 clean,
2 usage, 3 isolation violated, 4 verification failed, 5 isolation
unproven (a probe prerequisite could not be demonstrated); the report
file is ALWAYS written.

## Honest evidence classes (what this is, and is not)

- The two services are FIXTURE WHEELS built from
  `evaluation/tested_world/` — real installable packages with their
  own contract modules and tests, but not customer images. A customer
  rollout binds the customer's built artifacts the same way (digest →
  frozen member identity → installed bytes).
- The broker is a LOCAL FAKE with REAL sockets (loopback TCP, real
  frames, real duplicate injection) — reference coverage, not the
  customer's broker; the sqlite ladder is likewise reference coverage,
  not production PostgreSQL. Both are LABELED as such in every report
  (`dependency_coverage`, plus per-check `coverage` fields) — the
  label is a note, not a removal.
- The sentinel and provider endpoints are local gated HTTP servers
  with an explicit positive-control route — they stand in for
  "reachable only with egress/provider credentials"; the probe
  mechanism (five-outcome classification, control gating, synthetic
  credential, 2xx = caught) is the part that generalizes.
- Isolation is VERIFIED-BY-PROBE under the recorded enforcement
  profile: the launch scrub and clean-HOME/narrowed-PATH provisioning
  are policy, the controlled probes are the behavioral check, and the
  receipt is the evidence. What is NOT proven: that no credential
  exists outside the environment+HOME axes (agent tooling, other
  mounts) — those remain lane policy.
- The ledger-baseline member is a pinned IMAGE identity without a
  wheel (the pin discipline, same as the twin); the baseline leg runs
  the installed consumer's projection code against the pin's served
  API shape.

## What stays out of scope

No arbitrary distributed-system proofs; no production traffic replay;
no general deployment platform; no cross-process crash injection in the
socket arm (the crash-between-commit-and-ack window stays pinned in the
deterministic twin, whose harness remains the reference semantics for
it); no blanket security proof from two HTTP requests; no assertion
that process separation alone is an adversarial sandbox.
