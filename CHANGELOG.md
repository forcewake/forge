# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.40.0] - 2026-09-26

### Added — the b521e1a review: 12/18 items implemented (#337-#354)

The closed-human-loop cycle: review is no longer a dead end after
readiness, budget decisions are executable, and credential authority is
serialized. Suite 8593 → 8846 (+253). The 6 remaining items closed at
their honest human/external boundaries (#345 partner task, #346 blind
review, #347 customer discovery, #350 .NET customer profile, #352
two-writer, #354 the 1.0 declaration) — packages, kits and contracts
ready, nothing weakened to force a close.

**The two P1:**
- **#337 R40-01**: the /fix//ask production path — flag-gated ingress
  (FORGE_REVIEW_FEEDBACK_ENABLED, default OFF = zero routing), two-layer
  dedup (delivery-UUID SET-NX at ingress + the logical
  connection/project/MR/note identity), the installed reconciler's
  correction pass (worker-restart semantics proven), typed 2xx refusals
  (the GitLab 4-failure hook auto-disable protection), and
  registration-revert mutation arms.
- **#339 R40-03**: finality-based exposure — settled / accrued-unsettled /
  retained-liability separated; the P01 counterexample (a partial with a
  cost no longer releases its envelope) dead; typed unbounded-exposure,
  incoherent-bound and invalid-number findings; monotonicity, order and
  replay invariance property-tested; SQL-reload parity.

**The loop (the review's named main product result):**
- **#338 R40-02**: bounded review rounds after ready_for_human — linked
  child work units (migration 031, partial-unique race arbiter: two
  racing authorized /fix notes collapse onto one round), the classic-run
  adapter deriving the active plan from the frozen spec, the head-fence
  eligibility ladder (human commits preserved), one outstanding round
  per MR, the original delivery immutable with old evidence historical.

**Executable budgets:**
- **#340 R40-04**: typed budget amendments keyed by the ORIGINATING
  NATIVE COMMAND identity (migration 030; two identical commands are two
  decisions, a redelivery applies once), atomic application to the
  enforcing RunBudget (count axes raise limits and re-open limit-reached
  exhaustion; usd raises the closing gate's effective cap), the
  closing-partition/1 reserve frozen BEFORE coding (the implementer sees
  limit − share, the reviewer the full limits), and AT-04 with the REAL
  LLMClient + BudgetGuard — an evidence-only amendment cannot buy a
  reviewer call.

**Serialized authority:**
- **#341 R40-05**: the operation_grants keyed table (migration 029) —
  the unique index is the creation lock (concurrent creators converge on
  one grant_id and one absolute deadline); the evidence projection is a
  targeted CAS merge re-reading the authority each round; concurrent
  checkpoint/native-handle fields survive (mutation-proven).
- **#342 R40-06**: complete grant identity validation pre-broker
  (schema/operation/delivery-mode/work/subject/generation), the
  binding-revision comparison with the explicit grandfather adapter, the
  issuance/cancellation linearization contract, post-await fence
  re-reads, and runner verification of the operation word + revision +
  deadline.

**Qualification and gates:**
- **#343 R40-07**: operation-grant redemption LIVE-qualified — the
  native dispatch minted the grant (never seeded), the real lane
  bootstrap redeemed, the sentinel proof (the broker-selected key
  presented, the ambient never), generation retirement, cold restart
  preserving the deadline, seven typed negative classes with zero
  successful retrievals. <$0.01 spend.
- **#344 R40-08**: the required composed-trace mutation gates (every
  seeded defect proven to fail its trace), prerequisite-missing honesty
  in the PG gate (a required profile without its fixture reports
  UNQUALIFIED, exit 2 — never an invisible skip), and the aiosqlite
  teardown fixed at the allocation origin.
- **#351 R40-15**: the measured operating envelope under the real
  workflow shape (partition occupancy, degradation parking, the
  workflow-rows restore drill covering rounds+amendments+grants, five
  alerts with threshold bases); the honestly-blocked live leg (the lab's
  expired broker credential) recorded as the failing drill row.
- **#348 R40-12 (machine part)**: the cold-install kit — the runbook
  whose machine steps execute verbatim with verified observables, four
  human steps counted honestly, the four negative arms firing typed
  refusals before any unsafe or paid action.
- **#349 R40-13**: the five-fact operator projection (execution / review
  round / candidate / verification / acceptance as separate but linked
  facts), action versioning with typed stale refusals, one-snapshot
  consistency across API/comment/native lines.
- **#353 R40-17**: ADR-0034 (the ownership map with verified production
  callers), the BudgetAmendment unification (GitHub off the legacy
  evidence ledger onto the one table, guarded by an architecture
  allow-set), fixture isolation pinned.
- **#354 R40-18 (the contract drafted)**:
  docs/product/supported-contract.md — the 1.0 candidate centered on
  reviewed delivery + bounded follow-up, with the six-point declaration
  gate (four machine-proven and linked, two honestly human).

## [0.39.0] - 2026-09-25

### Added — the 6df4020 review: 12/17 items implemented (#320-#336)

The credential-authority and execution-boundary cycle. Suite 8283 → 8593
(+310). The 5 remaining items closed at their honest human/external
boundaries (#328 blind review, #329 partner pilot, #331 real-neighbor
discovery, #333 .NET customer profile, #335 two-writer) — packages,
ladders and substrates ready, nothing weakened to force a close.

**The two P1:**
- **#320 Q39-01**: operation-scoped credential grants — redemption
  authority comes from a grant PERSISTED at dispatch (subject, work,
  attempt generation, route, exact ref+revision, operation, ABSOLUTE
  deadline), never from the request; the refusal matrix
  (grant_route_mismatch / grant_ref_mismatch / grant_absent_native_only /
  grant_absent_legacy / grant_expired / attempt_terminal), the await-fence
  re-validating authority after broker resolution, and typed runner
  verification (work/route/ref/slot/generation/expiry equality) before
  any vendor client exists. `grant_id` rides the versioned execution
  spec; migration 028 persists grants.
- **#321 Q39-02**: the executor brief renders from the ACTIVE revision
  (`ApprovedInput`, `forge.revision.approved-input/1`), not the stale
  `spec.plan_summary` — the live counterexample (a resumed model
  reverting to the pre-revision approach) is gone; three-way digest
  equality (evidence == server == consumed) pins the exact text the
  executor consumed; `approved_input_digest` rides the execution spec.

**Authority and accounting:**
- **#322 Q39-03**: the credential audit is append-only — INSERT-only
  redemption rows committed BEFORE any bytes release, a bounded CAS
  projection for run summaries, retry counts on the same logical row,
  retention holds and backfill; concurrent writes are never lost.
- **#323 Q39-04**: collision-safe native secret locators
  (`TEAMA_<sha256-12-of-full-ref>`, `_` separator because GitHub's API
  refuses hyphens) + a concurrent registry with legacy migration — never
  a silent rename.
- **#324 Q39-05**: usage receipts reconcile partial→final through the
  canonical 4-part identity (run, attempt, receipt, source namespace)
  with a conditional `DO UPDATE ... WHERE final IS NOT TRUE` — a late
  final never vanishes from budgets; caller attribution is authoritative
  over payload; conflicting finals are explicit records.
- **#325 Q39-06**: the closing budget separates known spend / unknown
  lower bound / reserved liability (the P03 counterexample no longer
  reserves an honest-unknown and refuses); the hard cap is known +
  reserved + projection; `closing_reserve` yields the coder ceiling;
  review-only continuation (zero coder dispatches) inside the reserve;
  `review_shortcut_stale` on a moved head.
- **#330 Q39-11**: the accepted-task economics ledger
  (`forge.delivery.accepted-ledger/1`) — run → attempt → receipt →
  native job → candidate → verification → human decision joined by
  stable ids, unjoinable links surfacing as `identity_gaps`; three cost
  columns (provider-reported / price-card / billing-reconciliation)
  never blended, empty renders null not 0.0; two accepted measures;
  seven separate time measures; `pending` is a real human-decision
  state. Executed over the real captures: $0.815235 exact (coverage
  1.0) on the primary population, $0.803749 lower bound (coverage 0.8)
  on the SDK-receipt population — byte-deterministic on rerun.

**Qualification and consolidation:**
- **#326 Q39-07**: the supported composition v2 — the profile re-frozen
  onto the exact composition that completed the trace (the working-tree
  build + its uv wheel, receipts committed; the v0.37 composition
  archived verbatim, never rewritten), cold-install proven in all three
  modes (fresh: preflight on the INSTALLED package; upgrade: the real
  027→028 transition with five seeded tables preserved; verify), and
  the LIVE reviewed-ready trace: adaptive ladder measured on the empty
  mode, material revision approved NATIVELY mid-run, the resumed
  candidate itself carrying the revision-2 brief (zero rescue steers),
  the closing review within reserve → run terminal `ready_for_human`.
  $0.4032 lane spend. Two drill-side defects found and fixed live (a
  sequence tie hiding the useful checkpoint; the reuse guard comparing
  the wrong artifact's name).
- **#327 Q39-08**: the conformance gate v2 — five checks (shipped
  recipes as real Bash, dispatch-schema vs captures, secret-consumer
  sentinels, native locators, consumer contracts) plus four mutation
  arms (unconditional-exit, consumer-mapping-dropped,
  DTO-preserved-caller-disconnected, comment-only-marker-spoof), each
  proven to FAIL the gate on its seeded defect.
- **#334 Q39-15**: operating limits — one authorized operator surface
  exposing the six state quantities (current attempt, native occupancy,
  exact checkpoint, unresolved effects, required checks, accounting
  coverage); the four `ops.*` measures as structurally separate records
  (unknown windows counted, never zero-filled); capped admission whose
  verdict DERIVES from occupancy only — no parameter accepts an intake
  count; eight negative drills (slow runner, vendor 429, control-plane
  restart, unknown native start, concurrent commands, retention with a
  paused checkpoint + investigation hold, out-of-scope bundle, admission
  at cap); the support agreement with measured limits, response
  ownership and excluded failure domains — the day's real ~55-minute
  home-network outage (graceful degradation, zero paid dispatches,
  recovery by operator action) recorded as the worked example.
- **#336 Q39-17**: ADR-0033 (the six-owner execution map), the closure
  matrix (capability × six evidence levels — 25/36 proven, gaps stay
  pending as data) and three boundary rules with trap tests; the
  execution spec carries the two new authority members.

**Post-merge feedback:**
- **#332 Q39-13**: `/fix` and `/ask` classify as clarification /
  in-scope / material (fail-closed on ambiguity), note-id idempotency,
  the head-binding fence (feedback binds the exact head it addressed),
  and the required-discussion readiness gate before merge-side
  progression.

## [0.38.0] - 2026-09-25

### Added — the 59ba869 review: 16/18 items implemented (#302-#319)

All 3 P1 + 13 P2 landed with LIVE proofs. Suite 7621 → 8283 (+662).
The 2 remaining items (#311 blind review, #315 partner pilot) closed at
their honest human-gated boundary — the packages/profiles are ready.

**M0 — the three P1:**
- **#302 R38-01**: the shipped GitLab SDK finalizer — the unconditional
  exit that killed candidate collection (the live run's manual patch is
  now the shipped recipe in all four SDK lanes) + the packaged
  generation-aware collector; the regression executes the actual
  shipped shell. Qualified live as a by-product (MR, zero patches).
- **#303 R38-02**: the provider-safe credential delivery contract —
  CredentialDeliveryPlan, the raw staged_env spreads removed from all
  three dispatch legs, the redemption endpoint + the lane client, and
  the consumer proof (the fake model endpoint received the
  broker-selected sentinel, never the ambient key). Research basis:
  6 docs with the official provider docs read in full.
- **#304 R38-03**: the six operational DB dumps classified for real
  (credentials NONE; sensitive operational text) → private storage,
  sanitized receipts, the public-artifact gate; the history-cleanup
  dry-run plan awaits maintainer authorization.

**M1**: #305 (consumption/rotation at the runner boundary — the
consumer receipt, the concurrent registry, the policy matrix); **#306
THE useful-WIP cross-runner resume LIVE GREEN** (three file shapes
through the exact checkpoint; oracle green on the candidate; $0.82);
#307 (the supported profile frozen + cold-install fresh/upgrade
proven); #308 (isolation claims match containment — the five-outcome
taxonomy + the positive control); #317 (the conformance release gate —
both mutation arms caught on every recipe).

**M2/M3/M4**: #310 (real lane economics — coverage 0.8, three
measures); #312 (the customer-scale discovery machinery — the
observation cache with cross-scope isolation, 16/16 arms); **#313 the
combined steering trace causal=true on all five arms** (mid-turn steer,
the real activation CAS, the counterfactual); #314 (two-writer content
safety — same-file window typed, blob-verified adoption); #316 (the
operator recovery surface — empty-diff = FAILED); #318 (the versioned
ExecutionSpec + the composition matrix + the forge CLI entry-points);
#319 (deployment ops bound to the frozen profile — 9/9, the
review-named arms measured); #309 (the .NET recipe executed REAL —
pinned images, TRX reconciliation, migration preservation, RabbitMQ
exactly-once).

**Human-gated, closed honestly**: #311 (the blind package ready, 15
live arms; the grading awaits real reviewers), #315 (the ladder +
frozen profile + proven capability await the named partner).

## [0.37.0] - 2026-09-24

### Added — the 4af6b33 review COMPLETE: all 20 R37 items (#282-#301)

All 20 items (1 P1 + 19 P2) closed in one campaign. Suite 7015 → 7578 (+563).
For the first time the campaign includes LIVE proofs: the lab was aligned
for real and the real model ran through the real provider.

**M0 — correctness:**
- **#282 R37-01 (P1)**: continuation reuse bound to the exact recovery
  event and source attempt — identity-keyed matching; equal booleans
  never reuse another event's decision (AT-01/AT-02 green on real PG).
- **#283/#284**: canonical operator subjects (v2 grants; legacy
  name-only resolves-or-fails-closed) + a read model that never
  relabels history (activation matched to the command's checkpoint;
  historical passes stay historical; source-version fence).
- **#285/#286**: strict report-inventory identities (per-row matching,
  unbound subject typed, numeric attempt ordering, exit-code
  combination) + typed applicability across verdicts and views.
- **#299**: the fixture-lifecycle leaks closed at the root
  (undisposed engines 3/8 → 0/8) + the resource-lifecycle meta-gate.

**M1 — the lab aligned + GitLab parity + records:**
- **#287**: the read-only lab inventory executed (honest MISALIGNED
  record) + the strict record schema with typed validation.
- **#288**: GitLab dispatch parity — the full continuation envelope on
  every pipeline; retry/revival select decisions; callbacks bound to
  the current attempt.
- **#289**: THE LIVE QUALIFICATION — the lab aligned for real
  (0.36.0/schema 027/caps, receipts + rollback tags), then Arm 1 GREEN
  (real issue → claude-sdk-lane → real model → Draft MR, oracle green,
  $0.22) and the exact-resume envelope LIVE (mechanics green; the
  empty-diff failure honestly recorded; a real template exit bug
  found).
- **#298**: records promoted from executed evidence only — trace
  tiers, the supported-profile manifest with the human gate,
  upgrade-claim honesty, the immutable store.

**M2/M3 — live proofs:**
- **#290**: read-many/write-one LIVE ($0.39) — the real model cited
  the neighbor-only line-218 window, excluded the decoy, wrote only
  the target.
- **#291**: causal steering PROVEN — scripted AND live-model 3/3
  ($0.0091); three-way digest equality; old-epoch expiry.
- **#292**: the genuinely live planning comparison — 15/15 live-model
  arms ($0.77), HOLD pending human review.
- **#293**: the staged partner-pilot ladder (fail-closed on the named
  customer + observed baseline); stage-1 pending-lab honestly.
- **#294**: economics joined to real receipts (unknown never zero;
  synthetic counters excluded from throughput).
- **#297**: the bounded operator experience (typed blocked reasons
  with evidence links; bundle caps; query observables).

**M4 + separation + ops:**
- **#295**: the two-writer saga through REAL GitLab native effects
  (kill matrix live; no duplicates; human edits preserved; zero
  merges).
- **#296**: the trusted verification executor — isolation proven by
  deny probes; BUILT fixture wheels installed by digest; socket
  redelivery.
- **#300**: contracts separated from reference scenarios (ADR-0031 +
  the reference package + AST rules).
- **#301**: deployment ops EXECUTED on the aligned lab (5/5 drills;
  a real stale-row finding; rotation generations correct).

## [0.36.0] - 2026-09-24

### Added — the 16339c2 review COMPLETE: all 22 R36 items (#260-#281)

All 4 P1 + 18 P2 closed in one campaign. Suite 5918 → 7015 (+1097).

**M0 — the four P1 correctness boundaries:**
- **#260 R36-01**: collector ownership hardened — non-following lstat
  checks, physical containment, checkpoint/attempt binding from the
  trusted dispatch, TOCTOU stamp rechecks; the P01
  symlink-to-foreign-repo refused before cleanup.
- **#261 R36-02**: one typed continuation decision — reason-coded
  refusals (explicit discard lifts ONLY continuity), `restart` parsed
  in argument position, vendor-start certainty from the persisted
  native-start intent, never from discovery absence.
- **#262 R36-03**: every retry/revival lookup through the configured
  async authority — typed exact/absent/unavailable/corrupt/unauthorized;
  the legacy filesystem/HTTP chain confined behind an opt-in flag;
  AT-04 proven on real PostgreSQL.
- **#263 R36-04**: CAS reference acquisition serialized with deletion —
  one volume-wide lock + advisory twin; every unlink through the locked
  sweep entries; SWEEP=off operator mode; the P04 post-final-scan
  schedule fixed red-first.

**M1 — composition and gates:**
- **#264**: cutover fence at the standard composition root (mutations
  recheck the authority marker; generation-bound verify reports).
- **#265**: envelope v2 — execution_attempt_id from durable counters,
  authority epoch inside the digest, publication-identity fail-closed.
- **#266**: explicit installer routes (dev/wheel/tag/default ladder;
  conflicts refuse before download; the dev-route regression fixed).
- **#267**: the required PostgreSQL qualification gate in CI
  (alembic-provisioned profiles, manifest mutation detector, typed
  refusals; AT-09 negative arms proven).

**M2 — qualification:**
- **#268**: GitLab CE entry traces at PE rigor + the staged
  qualification driver; live preflight honestly REFUSED (control plane
  0.28.0 vs pinned wheel; no budget caps; the GitLab lane-resume gap
  recorded) — install-check GREEN on the promoted wheel.
- **#269**: hash-locked lane closures (88-artifact manifest, tamper
  arms proven), supply-chain binding, credential isolation receipts.
- **#270**: discovery authority — connection-identity readers, decoy
  rejection, authorized-set enforcement, neighbor-write refusal; a real
  captured neighbor-dependency run (RC-08).
- **#272**: revision proof through the next executor input (PE-7
  three-way digest equality; fail-closed WIP reuse decisions).
- **#273**: verification bound to the exact candidate (subject identity,
  freshness, frozen report inventory, infra-vs-defect repair budgets).

**M3 — operator, measurement, pilot, records:**
- **#274**: the operator projection wired to live authorized reads
  (read-only API, current-candidate binding; closes #213's remainder).
- **#275**: the pilot EXECUTED — 12/12 lab tasks through the
  production-entry seams; verdict 2-of-3 → expand at lab scope.
- **#276**: connected delivery measurement (identity joins, honest
  unknowns, the decode-label guard).
- **#280**: six ops drills green incl. the PG variant (capacity under
  lost responses, upload budgets, responsiveness, degraded faults,
  backup/restore with mismatch detection, override audit).
- **#281**: profile qualification records — the evidence-class lattice
  with DERIVED verdicts; upgrade-claim honesty; the profile gate step
  in release.yml.

**M4 — two-writer and boundaries:**
- **#277**: the durable saga over native-shaped remotes — the REAL
  coordinator in the kill matrix on real PostgreSQL; no marker dedup;
  lost-response adoption via native correlation.
- **#278**: complete tested-world verification — full digest over
  images/bundles/profile; the twin verifier with exactly-once
  double-delivery; three-distinct-permissions readiness.
- **#279**: ADR-0030 + the boundary registry + 30 AST enforcement
  tests — six owning boundaries mechanically confined; the audit found
  ZERO production violations.

**And #271**: the live research cohort machinery (preregistration,
provenance-labeled capture, blind review) — the shipped cohort reports
honestly HOLD pending human review.

## [0.35.0] - 2026-09-23

### Fixed — the c7ae8db review M0: the four P1 execution gaps

All 4 P1 closed (#238-#241). Suite 6224 (+306).

- **#238 Q35-01**: the shipped GitHub Emit step collected the ORIGINAL
  checkout after a resume — restored work existed on disk but never
  reached the candidate (0-byte uploads). Now one packaged collector
  (`candidate_collector.py` + `harness_entry --collect-candidate`) with
  an ownership-validated generation pointer, `git -C` on the validated
  generation, and typed errors (`|| true` gone). Negative-arm test
  proves the old sequence missed generation edits.
- **#239 Q35-02**: `/retry` unconditionally demanded a checkpoint — an
  early bootstrap death made the run unretryable. Continuation is now
  decided from persisted evidence (committed-baseline / exact-WIP /
  explicit-restart / uncertain); uncertain parks with an operator note
  and zero dispatches; the strict required-restore guard is untouched.
- **#240 Q35-03**: upload and resume read different checkpoint
  authorities in postgres mode (the resume producer used the filesystem
  JSON index). ONE configured async checkpoint repository now serves
  upload, resume, health and retention; half-configurations refuse;
  DB outage is typed unavailable — never a filesystem fallback, never a
  JSON mirror. PG-gated cross-process proofs.
- **#241 Q35-04**: execution slots were reservation guarantees, not
  occupancy guarantees (`record_native_handle` had zero production
  callers; releases defaulted `native_completed=True`). Migration 027:
  a native-start intent persisted BEFORE the provider call; derived
  occupancy states (never_dispatched / dispatched_unknown /
  native_running / draining / observed_terminal); release requires
  evidence — a lost start response or failed cancellation HOLDS the
  slot until the native job is observed terminal. All three providers
  wired; reconcile probes per provider. Verified on real PostgreSQL.

### Fixed/Added — the M1 wave

- **#242 Q35-05**: reference-safe checkpoint GC — mark/recheck/sweep
  with the recheck INSIDE the deletion transaction (the scan→unlink
  window closed); ResumeSpec pins; first-upload advisory lock; quotas
  count referenced bytes.
- **#243 Q35-06**: the legacy credential window is restart-stable —
  write-once persisted anchor (explicit deadline > recorded start >
  anchor file > fail-closed refusal); doctor check; rotation runbook.
- **#244 Q35-07**: the ADR-0029 composition types adopted in the GitHub
  production dispatch path — envelope constructed before any effectful
  call, axis separation guarded, redispatch digest-verified. The new
  boundary immediately caught two dispatch fixtures the old code let
  run with an empty attempt base.
- **#245 Q35-08**: lane installation reproducible from the promoted
  release — wheel+sdist per release (sha256 in the promotion record),
  template defaults GENERATED from the record, the v0.27.0 fallback
  deleted, installed-identity gate before model calls.
- **#246 Q35-09**: the mandatory production-entry trace suite
  (`tests/production_entry/`) — real processes, a fake native server
  whose state survives worker death, a fake vendor on the REAL codex
  App-Server wire, the real GitHubClient over HTTP. AT-01..AT-06 with
  negative arms; PG-gated traces confirmed on real PostgreSQL.
- **#258 Q35-21**: checkpoint metadata migration as commands —
  inventory → idempotent import → verify → fenced cutover → gated
  rollback; doctor preflight; runbook.

## [0.34.0] - 2026-09-23

### Added — the 0fca1b7 review P2 final wave: E2E qualification substrate (7 items)

All 19/19 P2 from the 0fca1b7 review are now closed. Suite 5917 (+455).
Deep-research basis: `docs/research/2026-09-23-e2e-qualification/` (7 docs).

- R32-13 (#226): `adaptive/qualification.py` — QualificationProfile binds
  one (recipe, harness) combination into a single digest; installed
  fingerprints refuse on mismatch; expected-report binding (missing report
  is never zero failures); egress probe pair detecting
  policy-declared-but-not-enforced; qualification layer vocabulary
- R32-14 (#227): `adaptive/research_cohort.py` — versioned cohort spec
  (4 task archetypes), offline-replayable none/lexical/research
  comparison over recorded artifacts, semantic grading (content, not
  filenames), PASS/HOLD/ROLLBACK promotion verdicts
- R32-19 (#232): `release_promotion.py` — PromotionRecord bound to the
  exact digest + qualifying CI run; fail-closed promotion gate in
  release.yml (a failed required check blocks tags even when the canary
  passed); generated template pins; --seed-real-data upgrade canary;
  per-release evidence archive. The retrospective v0.33.0 record
  honestly evaluates `blocked` (red typecheck it shipped with) — and
  that defect is fixed in this release
- R32-21 (#234): `adaptive/pilot.py` — the design-partner pilot kit:
  one-page learning contract (2-of-3 criteria by a named date, frozen
  baseline, staged ladder), 12–20 task plans with 9 scenario tags,
  tracker on the five holding-up metrics, stop conditions with
  preserved diagnostics
- R32-22 (#235): `adaptive/two_writer_qualification.py` — two-writer
  producer/consumer scenario frozen through freeze_verified_world;
  kill-at-step-k matrix (12 cells) + post-pivot forward-only matrix;
  lost-response adoption; can-i-deploy readiness query; credential
  scope checks
- R32-23 (#236): `adaptive/operator_view.py` + `support_bundle.py` —
  versioned projection with CAS delayed-replay protection, 14-state
  vocabulary incl. wedged, action validity matrix with audit
  four-facts; support bundle with explicit missing/unknown coverage
- R32-24 (#237): ADR-0029 + `runs/composition.py` —
  RepositoryContext/AttemptStartSpec/ResumeSpec formal types (no
  permissive defaults), execution-authority owner map, CompositionMatrix
  can-i-deploy edges, compat fixtures from real git history

### Fixed

- The v0.33.0 red typecheck (`runs/github_service.py:791` — evidence
  `dict | None` guard); blocking mypy core is clean again

## [0.33.0] - 2026-09-23

### Added — the 0fca1b7 review P2: production wiring, supervisor health, lease draining, composed invariants (10 items)

All 5 P1 + 12 of 19 P2 from the 0fca1b7 review are now closed. Suite
5462 (+87).

**Production wiring:**
- R32-10: SystemContextProfile in the GitHub planning path — neighbors
  from ProjectConfig feed multi-repo discovery; byte-identical without
- R32-11: dispatch_plan_binding — /approve-revision → /go dispatches
  with the ACTIVE digest; stale /go refused, zero dispatches
- R32-15: validate_profile_coherence — recipe/harness/credential/egress
  checked as ONE coherent set at lane startup

**Execution reliability:**
- R32-09: control-consumer health — mid-turn drain death degrades the
  outcome (control_degraded); strict mode suspends the turn
- R32-07: lease draining — local terminal ≠ native completion; the
  slot holds until the native job is observed terminal
- R32-16: checkpoint durability — best_effort (filesystem) or postgres
  (DB index, migration 026); fail-closed on misconfig

**Quality infrastructure:**
- R32-17: 5 new composed invariant traces (generation restore, ownership
  recovery, legacy deadline subprocess, concurrent lease under 4
  approvals, resume mode through the shipped template)
- R32-18: zero RuntimeWarnings for coroutine-never-awaited; engine
  dispose in fixtures; autouse warning gate
- R32-12: support matrix dimensions — (driver, source_platform,
  runtime_recipe); tested requires registration+platform+recipe
- R32-20: per-attempt latency breakdown; sticky-unknown missing
  attempts as placeholder rows

Suite 5462; ruff clean.

## [0.32.0] - 2026-09-23

### Fixed — the 0fca1b7 review P1: workspace generations, ownership-scoped recovery, fixed credential deadline, resume dispatch, lease parity (7 items)

All 5 P1 execution defects closed (each with the reviewer's reproduction
regression-pinned), plus 2 P2 reliability items:

**R32-01 — Workspace generation promotion:** restore promotes to a
STABLE SIBLING generation path (`.forge-workspace-gen-<id>/`), never
replaces cwd; the lane chdirs into the generation BEFORE the vendor
client; `.forge/workspace-generation` pointer records the active
generation. Subprocess test proves `os.getcwd()` and relative writes
work after restore — the reviewer's exact FileNotFoundError scenario
is impossible.

**R32-02 — Ownership-scoped recovery:** backup/staging names carry
the work_id; recovery processes only OWNED assets; unknown directories
inventoried as `unrecognized` (never claimed). Two-sibling regression:
B's backup stays byte-identical when A recovers.

**R32-03 — Fixed legacy credential deadline:** anchored to a
PERSISTED migration start (env or module-import default), not
recomputed per check. Day-31 subprocess restart proof: legacy tokens
refused after the window regardless of restarts.

**R32-04 — Resume mode through dispatch:** `lane_resume_mode` workflow
input (fresh/required/restart) emitted by every dispatch path; the
template maps it to FORGE_LANE_RESUME env. Retry/revival = required;
initial /go = fresh; restart is the explicit discard path.

**R32-05 — Lease parity:** execution leases wired in GitHub + GitLab
(every dispatch entry reserves; terminal releases). 5-issues/
4-approvals/limit-3 test proves exactly 3 native starts.

**R32-06 — One-per-run:** partial unique index on run_id WHERE
released_at IS NULL; run-conflict adopts the existing winner
(idempotent, bounded). Migration 024.

**R32-08 — pip bootstrap:** the `#sha256=` fragment on a local wheel
path is an INVALID pip requirement (real-pip repro); now installs the
verified local path directly; strict exactly-one-wheel guard; AzDO
template fixed too.

Suite 5375 (+48).

## [0.31.0] - 2026-09-23

### Added — the ccab247 review P2: execution leases, supervisor, recipe decomposition, support matrix, system context (16 items)

All P1 + 19 of 23 P2 items from the ccab247 review are now closed.
Suite 5327 (+125 from v0.30.0).

**Reliability:**
- NEXT-05: Content-Length + bounded stream read BEFORE JSON parse (512 MiB
  cap) — a hostile payload can't exhaust API memory before validation
- NEXT-06: flock with holder identity; retention decisions recorded in
  the index; multi-process concurrent writer safety
- NEXT-09: bounded research await + sticky-unknown token aggregation

**Execution governance:**
- NEXT-11/12: execution_leases table (migration 023) — CAS slot
  reservation at EVERY dispatch entry; queue admission vs execution
  lease are distinct checks; terminal transitions release the slot
- NEXT-14: LaneSupervisor — ONE owner of the turn, the drain, and the
  terminal classification; urgent interrupt fires in the same
  scheduling slice (not after a queued steer); exactly-once write

**Product:**
- NEXT-13: copilot-acp observed capability profile (turn, interrupt,
  next-turn only — the complement is honestly unobserved)
- NEXT-15: RuntimeRecipe × HarnessProfile decomposition; dotnet-lane is
  a compatibility alias for (dotnet-9, claude-code); any harness on
  any recipe validates
- NEXT-16: TRX LogFilePrefix + multi-project aggregation (bounded
  breakdown, parse failures recorded, zero-reports is explicit policy)
- NEXT-21: SystemContextProfile — N authorized read-only neighbors +
  1 writable target; feeds multi-repo discovery; authorize_write
  returns typed verdicts
- NEXT-20: the dispatch reads the ACTIVE plan post-revision; a stale
  /go with the old digest is refused

**Observability + release:**
- NEXT-23: per-attempt identity-matched cost receipts; conflicting
  receipts surface as conflicts, never averaged
- NEXT-26: support_matrix() — tested/supported/declared_only per
  (driver, provider, recipe); doctor --support-matrix
- NEXT-18: runtime network egress probe (verified / declared_only /
  not_enforced)
- Bug #185: AzDO /go short-id prefix resolution

Suite 5327; ruff clean; all 3 providers live-verified in v0.30.0.

## [0.30.0] - 2026-09-23

### Fixed + Added — the ccab247 review P1: execution identity chain, research harness wiring, tool observations

The reviewer's verdict: "Forge has moved from mechanisms to a genuinely
working adaptive chain." This release closes the connections between
already-written components (all 5 P1 + 3 high-impact P2):

**P1 — execution identity (NEXT-01/02/03):**
- ONE generation-scoped credential through dispatch + lane-control +
  checkpoint APIs; legacy tokens accepted only within a migration
  deadline; DB-outage = 503 refusal, never silent legacy acceptance
- The pause fence is CONDITIONAL: a cleared fence refuses a candidate
  whose grant generation is below the resumed epoch — an old attempt
  never regains write authorization after resume; clear is one CAS
- ResumeSpec: the producer embeds the exact checkpoint reference in
  the resume command's payload; the consumer reads a durable
  /resume-spec endpoint; three distinct modes (fresh / required /
  restart), never a silent fallback to latest

**P1 — research harness (NEXT-07/08/09):**
- The production \`/implement\` path now CONSTRUCTS the ResearchHarness
  from the run's budget-guarded LLM client (the module existed but the
  caller never built it — the composition root was the missing link)
- ToolObservation: \`list_paths\` returns the actual list; \`read_file\`
  carries the full window; the next research prompt includes the
  actual code the tools returned (the model can now see what it asked
  for — the reviewer's sentinel-line test proves it)
- The completion await is bounded (\`asyncio.wait_for\`, not
  check-then-await); token aggregation is sticky-unknown

**P2 (NEXT-04/17/25):** restore promotion is transactional (staged
build + one atomic rename, rollback on failure); wheel pinned by
SHA-256; 17 composed application regressions from the review's traces.

Suite 5202 (+74). Live E2E verified on all three providers.

## [0.29.0] - 2026-09-23

### Added — the 1ae5290 review COMPLETE + the copilot-sdk-lane

All 30 review items closed (#155-#184, minus evaluation-scoped items).
Suite 5128 (+320 from v0.28.0).

**P2 wave 2 (M3+M4):**
- R28-21: .NET 9 lane (digest-pinned SDK, TRX verification, the claude-
  code agent riding the forge gateway on .NET)
- R28-23: bounded admission (per-project/issue/user limits, typed
  refusals, wired into start_run)
- R28-25: project credential bindings (broker-ref, never values) +
  exportable audit trail (credential-redacted JSON)

**The copilot-sdk-lane** (from the research doc): a REAL interactive
Copilot CLI harness over the `copilot --acp` JSON-RPC 2.0 protocol —
session/new, streaming prompt, session/cancel with the #4561 cancel-
ledger workaround (end_turn+ledger→interrupted), no mid-turn steer
(protocol absence, next-turn input only), npm @github/copilot@1.0.86.

**Live E2E on all three providers** (batch claude-code harness):
- GitLab: MR !22 created, pipeline green
- GitHub: PR #95 created, Actions green
- Azure DevOps: pipeline 73 succeeded (live-found: AzDO /go short-id
  prefix bug — filed)

## [0.28.0] - 2026-09-22

### Fixed — the 1ae5290 review M0+M1: checkpoint input security, exact resume, pause fence (14 items)

The reviewer's P0: **checkpoint upload accepted path-shaped blob keys**
— extra entries not referenced by the manifest became filesystem paths
without hex64 validation, writing OUTSIDE the CAS root (reproduced by
the reviewer). All 14 P0+P1 items from the review are closed:

**Checkpoint security (R28-01/02/03/04):**
- Every blob key is hex64-validated before ANY filesystem write; exact
  closure (extra/missing refused); entry-count and aggregate-size caps;
  `_cas_path` itself validates (defense in depth)
- Restore is transactional: full verification → staging dir → atomic
  promotion; symlink ancestors refused; `.git/` and credential
  patterns are reserved namespaces; partial restore is impossible
- A required checkpoint restore that fails HALTS the lane (zero model
  turns, nonzero exit); fresh runs proceed
- The baseline stores raw-content sha256 (one `git cat-file --batch`);
  the walk excludes `.forge/`, `__pycache__`, `.pytest_cache`,
  `*.egg-info`, `node_modules`

**Exact resume (R28-05/06/07):**
- Resume binds to the EXACT checkpoint reference from the resume
  command (`work@id`); fallback to latest is recorded explicitly
- Latest-selection by `max(sequence, checkpoint_id)` — never
  `entries[-1]`; concurrent writes under per-work `fcntl.flock`
- Attempt-scoped credentials: `HMAC(secret, work:generation)`;
  superseded generations get 403; backward-compat works

**Durable pause fence (R28-08):**
- Migration 022 `pause_fences`; /pause raises the durable fence (epoch
  bump), /resume clears under a new epoch; the classic publisher
  checks it at the final native-effect boundary; composed test: pause
  → restart → delayed publish REFUSES

**Control semantics (R28-09/10):**
- Steering dedup by native event identity (delivery ID, not UUID)
- Checkpointed-ack replay is idempotent (200, no state change);
  stale-generation acks get 403

**Quality gates (R28-13/15/24):**
- Shellcheck gate fixed (shell pinned, severity, both streams)
- Discovery and attempt-base freeze on ONE resolved SHA; the discovery
  call is INSIDE the try block
- Execution profile v2 runtime validation (egress allowlist, read-only
  root FS, credential staging) — non-compliant fails closed at lane
  startup

Suite 4808 (+91). The reviewer's exact path-escape repro is pinned by
a test that proves ZERO filesystem writes.

## [0.27.1] - 2026-09-22

### Refactored — the script-rendering architecture (research doc phases 0-3)

The driver scripts that were 324 LOC of Python string concatenation
are now real `.sh` files shipped in the wheel, rendered via stdlib
`string.Template` — the recommendation from
`docs/research/2026-09-22-script-rendering-architecture.md`:

- **Phase 0**: 14 golden fixtures (7 drivers × MCP on/off), byte-for-byte
- **Phase 1**: `src/forge/harnesses/scripts/` in the wheel;
  `harness-log-filter.mjs` rides the same pin (deletes the unpinned
  network fetch); harness_entry 1708→1234 lines. Zero byte drift
- **Phase 2**: `bash -n` over all rendered combinations;
  placeholder-absence test enforced
- **Phase 3**: `<PINNED_REF>` eliminated — real released-tag defaults
  with a documented `FORGE_LANE_REF` override

Suite 4717 (+25).

## [0.27.0] - 2026-09-22

### Added — the FULL review chain LIVE: pause → checkpoint → kill → retry → WIP restore on a second runner → resume

The reviewer's demanded acceptance chain is CLOSED end-to-end on a
real GitHub Actions runner (evidence:
docs/evaluation/2026-09-21-drivers/claude-sdk-lane-runner-README.md):

- **The resume consumer**: a retried lane (same run-id) downloads the
  verified checkpoint from the control-plane API, restores 12 WIP
  files digest-verified into the checkout, and the agent continues
  from the previous runner's work — `wip_restore: {restored: true,
  files_restored: 12, failures: []}` in the candidate meta.
- **/retry accepts a checkpoint**: a paused run with NO candidate but
  a stored checkpoint is retryable (the checkpoint IS the work to
  continue from); the gate crosses the process boundary via the
  checkpoint API.
- **The capture excludes .git internals** (read-only pack files
  Permission-denied on restore — runner infrastructure, never WIP).
- **wip_restore rides the .forge/steering.json sidecar** (emit-meta
  passes it through; writing the meta directly never reached the
  uploaded artifact).

Live-found and fixed this wave (four): DownloadedCheckpoint field
name; .git in the checkpoint; sidecar vs direct write; the /retry
gate's cross-process checkpoint check. Suite 4692.

## [0.26.0] - 2026-09-22

### Added — the adaptive waves LIVE: /steer, /pause with verified checkpoint, /resume

The review's demanded chain — operator command → durable mailbox →
running agent → effect → checkpoint — now runs END-TO-END on a real
GitHub Actions runner (forge-lab-gh, evidence in
docs/evaluation/2026-09-21-drivers/):

- **Wave B (/steer)**: issue comment → ingress → ControlCommandRouter
  → PostgresMailbox → lane outbound poll → Claude agent effect → the
  FULL ack ladder home: received → authorized → dispatching →
  vendor_accepted → applied → checkpointed. steering_journal +
  episode (211s turn) in the candidate meta.
- **Wave C (/pause)**: the pause drain runs the REAL checkpoint
  transaction — interrupt ack 3ms → cooperative capture (git baseline
  + content-addressed store) → upload via /lane/checkpoints →
  pause_status=paused, receipt verified=true. 124 digest-verified
  blobs on the control plane.
- **Wave D (/resume)**: the cross-process pause-state boundary found
  and fixed live — the resume gate now checks the DURABLE checkpoint
  store (the APP process can never hold the LANE's pause).
- **The dispatch infrastructure**: the per-work HMAC lane token
  computed at dispatch (the shared secret NEVER enters a lane job);
  the checkpoint channel API (upload/download, digest-verified both
  sides, retention keeps latest); the lane remote-control poller (the
  same MailboxSurface, steering over HTTP).

**Live-found and fixed** (each one a real runner caught): SecretStr
str() is the MASK (get_secret_value is the value); FORGE_RUN_ID needed
on the driver step; emit-meta dropped lane keys (sidecar pass-through);
the upload channel must be a Protocol object; work-token satisfies the
channel config; resume must check the durable store. Plus: GitHub /go
prefix resolution; the claude-sdk-lane harness_entry arm; #154
eternal-preflight.

**Capability matrix honestly updated**: adaptive-commands,
steering-bridge, pause-resume-checkpoint are now
real_provider_scenario (live evidence artifacts recorded). Suite 4692.

## [0.25.0] - 2026-09-22

### Added — the edf938c backlog completed: every issue closed

The full 31-item review backlog (#123–#153) is closed. This release
lands the remaining 11 slices (+~300 tests; suite ~4700):

- **NXT-10**: `/pause /resume /steer /answer` routed through the real
  authenticated ingress on all three providers
  (FORGE_ADAPTIVE_COMMANDS_ENABLED, default OFF; the /go approver gate;
  short-id resolution; journaled replies).
- **NXT-13+24**: work-wide broadcast commands with per-lane
  acknowledgements (migration 021) + the recoverable publication saga —
  write-ahead intents, adopt-by-marker, HUMAN-moved heads park, and the
  provider protocol has no merge/force-push by construction.
- **NXT-27+28+29**: driver capabilities as versioned observed behavior
  (exact-version answers only; CLI version pins in the templates),
  episode timing in every lane meta, and LANE_PROFILE_V2 (staged
  credentials, data boundaries, cap-drop-all, deny-by-default egress).
- **NXT-21+22**: evidence invalidation by RELEVANT dependency identity
  (revision bumps invalidate nothing — a change type cannot carry a
  revision) + OID/digest validators, frozen members, persisted
  tested-world digests at freeze time.
- **NXT-07+23**: persisted clarification questions gate planning;
  /answer folds mailbox commands; WorkPackage coordination persists —
  idempotent child intents close both crash windows, advance requires
  PROVEN outcomes.
- **NXT-04+06**: 31 real-construction-surface tests (NO fakes — found
  and fixed real drift: GitLabClient lacks read_text); citations bind
  to repository+OID+path+line-range against the authorized snapshot
  tree (cross-repo/stale-OID/out-of-range fail closed).
- **NXT-26**: independent candidate checks — contract suite + DB
  cross-checks, evidence-only (no verdict surface by construction).
- **NXT-14** (second half), **NXT-03** (evidence-mapped closure).

Live findings en route: migration 021 json/jsonb incoherence (the
e53ffd2 lesson, caught by the lab chain run — fixed); a GitHub
preflight//go deadlock surfaced by the live discovery journey (filed,
#154).

## [0.24.0] - 2026-09-22

### Fixed + Added — the edf938c review campaign: assembly over new modules

The review's verdict — components ahead of the finished user process —
answered with 12 delivered slices (issues #123-#153; 13 closed, tracked
in GitHub). Suite 4243 (+197 over 0.23.0).

**M0 — the branch is reliable again**
- NXT-01: the blocking typecheck gate restored — mypy 15→0 via typed
  `_ResolvedGo/_RefusedGo/_AdvanceGo` resolution (zero `type: ignore`);
  the AsyncMock warning flood replaced by typed sync-identity doubles.
- NXT-31: the GitHub harness reconciler is scoped to the bound
  repository (entry guard BEFORE any blocking transition or provider
  call; proven by the OUTER production reconciler with two repos — all
  tests fail against the unfixed source).
- NXT-02: a reachability-based capability manifest — `forge doctor
  --capabilities` exits 1 on drift; 17 rows, tiers from
  domain_contract to real_provider_scenario, unfinished paths stay
  disabled and say why.

**M1 — the customer's first objection**
- NXT-05: the durable discovery stage is SPLICED into the production
  GitHub /implement path (FORGE_DISCOVERY_ENABLED, default OFF):
  replay/recovery semantics, content-addressed evidence with file:line
  citations, `evidence:<id>` plan-citation validation (fail-closed),
  bounded digest inside the planner cap.
- NXT-09: the control mailbox is durable — migration 020, work-scoped
  dedup BEFORE epoch mutation, the 7-rung CAS ladder; FI-proven on a
  real disposable PostgreSQL (full chain 001→020).

**M2 — intervention without losing work**
- NXT-11+12: steering attached to the real lane lifecycle (every lane,
  FORGE_STEERING_ENABLED default OFF) with effect-intent rungs —
  dispatching → vendor_accepted → application_observed;
  outcome_unknown is honest and never silently retried; pause-class
  commands preempt queued guidance.
- NXT-15..18: pause is a real publication fence + checkpoint
  transaction — the fabricated `artifact:wip:` checkpoint is gone;
  content-addressed WIP capture with digest-verified read-back,
  retention-pinned references, honest partial/failed states, restore on
  a fresh instance, resume under a fresh epoch with re-checked
  authorization.

**M2/M3 — authority and evidence**
- NXT-19+20: material activation binds work+proposal-digest+parent+
  contract+epoch in ONE conditional transaction (the cross-work
  activation characterization now refuses); tactical policy is
  fail-closed — unknown materiality routes to a decision, never
  tactical_internal; the old plan never re-authorizes.
- NXT-25: the COMPLETE tested world is fingerprinted
  (tested_world_digest: all members + image digests + bundles + pins +
  policy refs; provenance ≠ applicability, domain-separated digests);
  environment_compose pins baselines to exact artifacts.

**Live-found in the lane plumbing (post-review)**
- A SHARED `forge-agent` job name meant GitLab's last-include-wins
  silently replaced sibling lanes — jobs are `forge-agent-<driver>` now
  and the worker matches by prefix.
- A nonzero driver exit now FAILS the lane job (batch pipefail parity);
  artifacts still upload via `when:always`.
- codex no longer inherits `FORGE_HARNESS_MODEL` (gateway-specific
  model names made turns complete empty in seconds).
- `/go` accepts the short id prefix the plan actually shows.

**Known-honest**: opencode-sdk-lane is blocked upstream (v2.0.10 yanked
from every distribution; the installer's latest is a different major
with a different API); codex-sdk-lane's runner cycle is pending auth
verification (parked). Both are stated in the capability matrix.

## [0.23.0] - 2026-09-21

### Fixed + Added — the real-usage (e2e) pass over the interactive drivers

PONG proved the wire; this pass proved the lane. The new `--e2e` mode
of `scripts/driver_live_smoke.py` hands each driver a scratch repo
with a FAILING test and the task *implement `add` so the tests pass,
run pytest to verify* — the judge is pytest run by the smoke itself,
never the agent's reply. All three drivers completed the task green
(claude 25 s, codex 19 s, opencode 13 s; evidence:
`docs/evaluation/2026-09-21-drivers/*-e2e.json`).

The opencode lane reached green only after two LIVE-found driver fixes
— exactly the defect class the wire smokes cannot see:

- **The SSE reader was killed by its own wait machinery**: the
  subscription awaited the reader through a cancellable observer
  wrapper, and cancelling that observer raced a `CancelledError` into
  the reader — the stream silently died after its first frame and
  every turn limped home on transcript reconciliation. The reader task
  now enters `asyncio.wait` directly (wait never cancels its
  arguments); the observer helper is gone.
- **`finish: "tool-calls"` is not turn completion**: intermediate
  assistant messages of the agent loop carry non-terminal finish
  values; reconciliation now treats only `stop`/`error` as terminal
  (regression-pinned by a test that fails on the old behavior).
- The complete live-observed v2 event vocabulary landed
  (`session.reasoning.*`, `session.text.*`, `session.tool.*`,
  `session.usage.updated`, `shell.*`, `session.inbox.*`, …) — the
  unknown-vocabulary warning spam is gone.

Docs: FAQ gains the interactive-drivers Q&A (what "live-verified"
means, why steering cannot grant permissions, where the drivers run);
the harnesses README now distinguishes the batch CI harnesses from
the interactive drivers; the claude-sdk research doc carries its LIVE
CONFIRMATION note; the evidence README records the e2e table and the
two new defects.

Suite 3855; ruff check + format clean.

## [0.22.0] - 2026-09-21

### Added — live verification of the real drivers + the evidence-backed DriverMatrix seed

All three interactive drivers ran against the real vendor binaries
(`scripts/driver_live_smoke.py`, evidence in
`docs/evaluation/2026-09-21-drivers/`) — and live testing found what
contract tests could not:

- **claude-sdk: 4/4 green** (session start, completed turn, steering
  follow-up, interrupt) over the z.ai Anthropic gateway with
  claude-agent-sdk 0.2.157.
- **codex-app: 5/5 green** (thread, completed turn, mid-flight steer
  via `expectedTurnId`, interrupt → `interrupted`) against codex-cli
  0.153.4. LIVE-found and fixed: the sandbox variant enums are
  ASYMMETRIC (`thread/start` wants kebab-case `workspace-write`,
  `turn/start` `sandboxPolicy.type` wants camelCase `workspaceWrite`);
  `_wire_sandbox` normalizes either spelling and emits per-surface.
  Recorded: `turn/start` responds at turn acceptance, completion is
  the `turn/completed` notification.
- **opencode-server: 4/4 green after a full wire-layer rewrite** —
  v2.0.10 is a different API than researched: routes under `/api`,
  `prompt_async` gone (the prompt route is non-blocking; completion is
  SSE `session.execution.succeeded|failed`), MANDATORY model
  selection (`POST /api/session/{id}/model` with
  `{model: {providerID, id}}`), flat `type`+`data` event frames,
  abort became `interrupt`. The research doc carries the full LIVE
  CORRECTION.
- **`OpenCodeServer` spawner** — lane-local `opencode serve`
  lifecycle: probe-socket port pick, `/doc` readiness, a
  spawner-generated server password (v2.0.10 always enforces one and
  prints a random one to stdout a DEVNULL lane loses), SIGTERM→SIGKILL
  teardown.
- **`live_registrations.seed_live_matrix()`** — the EXE-07 seed: three
  live-verified (sdk × provider_route × credential_mode) combos, each
  citing its evidence JSON; seeding REFUSES an entry whose evidence is
  missing or whose recorded run failed. The runbook documents the
  table and the re-verification command.
- `opencode_client_from_env` now raises `TypeError` on an env dict
  passed positionally (LIVE-found: it silently dialed the default
  port).

Suite 3854 (+26); ruff check + format clean.

## [0.21.0] - 2026-09-21

### Added — REAL interactive-driver clients (claude-sdk, codex-app, opencode-server)

The adaptive adapters always took injected Protocol-shaped clients;
until now only test fakes existed. This release adds the real clients
in `forge.adaptive.drivers`, one per vendor surface, each written
against multi-step web research that was verified against the vendor's
actual artifacts (`docs/research/`):

- **claude_sdk.py** — the `claude-agent-sdk` Python package (new
  `interactive` optional-dependencies group, `>=0.2.118` for
  `terminal_reason` on aborted turns). Interactive `ClaudeSDKClient`
  with bounded interrupt (the #1094 never-ack hang), drain-to-
  ResultMessage discipline before re-querying, and the harness hacks
  ported as first-class behavior: ephemeral per-session
  `CLAUDE_CONFIG_DIR`, `setting_sources=[]` isolation,
  `bypassPermissions` default with a `can_use_tool` policy hook,
  unremovable mechanical deny on `git commit`/`git push`, one-rule-
  per-literal allowlists (A09), and the `ANTHROPIC_BASE_URL` gateway
  (LiteLLM/BYOK) with proxy passthrough. The research was verified
  against the actual 0.2.157 wheel source — where the docs site and
  the package disagreed, the package won.
- **codex_app.py** — no vendor package: pure asyncio stdlib driving
  `codex app-server` over JSON-RPC 2.0 JSONL stdio. initialize →
  thread/start (approvalPolicy `never`, workspaceWrite sandbox) →
  turn/start; steering via `turn/steer` bound to the tracked
  `expectedTurnId` (stale id surfaces the error, never queues);
  interrupt keys completion off `turn/completed(interrupted)` rather
  than the method response; server-initiated approvals are answered
  `decline` (the unattended lane); `-32001` overload retried with
  backoff+jitter; injectable Transport seam.
- **opencode.py** — httpx against `opencode serve`: async-first
  prompting (`prompt_async` + `session.idle` over SSE) with the
  blocking route as fallback and status-polling reconciliation for
  the SSE no-replay gap; defensive EventV2 parsing; `permission.asked`
  answered `reject` by default; Basic auth + BYOK `PUT /auth/:id`
  (key material dropped after send); `probe_spec()` reads the served
  OpenAPI spec and warns on unknown event vocabulary.
- Package wiring pins (`tests/test_adaptive_drivers_package.py`)
  freeze the join between the frozen adapter Protocols and the real
  client classes; the drivers package imports clean WITHOUT any vendor
  package installed.

Suite 3828 (+71); ruff check + format clean.

## [0.20.0] - 2026-09-21

### Added — the adaptive runbook (OPS-08: recipes, control commands, recovery, credentials, decision record)

The supported recipes and architecture/runbook handoff
(`docs/operations/adaptive-runbook.md`):

- The three permission tiers (read scope / write coordination /
  production deployment — the last is always human).
- The supported runtime profiles (claude-sdk-lane, codex-app-lane,
  opencode-server-lane, checkpoint-restart) with their capability
  differences; the DriverMatrix is fail-closed on unregistered
  combinations.
- The safe control-command table (/pause /resume /steer /answer
  /amend /approve-revision) with what each does and does NOT do.
- The lifecycle diagrams (DiscoveryRun, PlanRevision, WorkPackage).
- Recovery procedures: cancelled-with-in-flight-effects (superseded,
  never pretend-undone), failed WorkPackage children (saga recovery by
  intents), expired artifacts (explicit recoverable state).
- The credentials/data-flow chain (broker-owned refs; the coding agent
  never receives forge credentials).
- **The decision record for retaining the Postgres controller**: the
  current controller already provides the primitives (CAS, durable
  steps, fenced claims, publication intents, revival); a second
  authoritative engine creates more migration work than benefit for the
  first customer slice. Criteria for later Temporal evaluation:
  sustained >50 concurrent active runs with lock contention, a
  cross-process saga requirement, or a Temporal-native provider
  integration.
- Doctor additions: CapabilityMatrix/DriverMatrix/privileged_ok are
  read-only and fail-closed.

## [0.19.0] - 2026-09-21

### Added — executed-evidence claims + the bounded design-partner pilot

- **OPS-06 — executed-evidence release claims**: per-check evidence
  classes (executed / skipped / unsupported / not_run / unknown) with
  run ids, URLs, and head SHAs from the Actions checks of the pinned
  commit. A boot canary is explicitly NOT SDLC e2e evidence;
  ``may_claim`` requires release-specific EXECUTED evidence for any
  promoted customer capability; README version/image/test-count
  generated from one source.
- **OPS-07 — the bounded pilot**: ONE fully-instrumented task through
  the live lab: task → plan → approved spec → GitLab CI harness lane
  (claude-code/glm-5.3-flash) → candidate SHA → native verification
  (positive proof, tested_oid == candidate) → readonly review (ok) →
  ready_for_human → human acceptance (merge). Wall clock 3m25s; agent
  spend \$0.25; 9 turns. Evidence:
  `docs/evaluation/2026-09-21-adaptive-pilot/`.
- **Two live-found defects fixed during the pilot**: the GitLab
  template's brief was at /tmp (outside the claude sandbox — DENIED on
  Read); the lab image didn't COPY alembic/ (migration 019 invisible,
  MR creation died with UndefinedTableError).

## [0.18.0] - 2026-09-21

### Added — the adaptive service wiring (the substrate meets the production seams)

- **DiscoveryService**: creates a durable DiscoveryRun and dispatches it
  through the caller's harness-start leg — the SAME CI execution profile
  the classic implement path uses (DSC-01's rule: never inside the
  privileged API process). A waiting_question discovery redispatches
  after the answer; a completed discovery's evidence bundle resumes
  WITHOUT repaying the probes; unresolved critical questions block
  planning rather than inventing defaults.
- **OperatorControlService**: the mailbox-backed operator command surface
  — pause fences the publication epoch BEFORE the interrupt is sent
  (CTL-05's ordering is the guarantee); resume requires a confirmed
  checkpoint; steer delivers bounded guidance and REJECTS
  acceptance-policy changes (routed to the revision gate with a
  rejection reason); answers route through idempotency-keyed commands
  (a redelivered answer never duplicates).
- **WorkPackageCoordination**: a parent WorkPackage launching child runs
  phase-by-phase through a caller-supplied child-run factory (one
  writable repository per child lane); dependency phasing from
  compile_dependencies; a failed child HOLDS the later phases — no
  partial cascade; completion only after the last phase lands.

## [0.17.0] - 2026-09-21

### Added — the full adaptive backlog substrate (all 62 remaining roadmap stories, eight epics)

Ten parallel agents over disjoint scopes landed the complete typed
substrate under `src/forge/adaptive/` — 20 modules, ~600 assertions:

- **Foundation (FND-03..08)**: fail-closed typed read guards (a 403/
  429/5xx/truncated read NEVER becomes create permission); capability
  and credential profiles (discovery cannot select a write-enabled
  profile; value-looking credential refs rejected; absent vs
  explicitly-empty manifests distinguished); the content-addressed
  artifact store (tenant-scoped, immutable, archive traversal/symlink
  checks before extraction, WIP manifests with source OIDs);
  DB-enforced dedup (insert-or-read with first-result-wins) and
  independent publication/checkpoint/control epochs; compatibility
  flags with safe drain and the no-invention migration rules; the
  production-path invariant suite registry with performed/skipped
  honesty.
- **Discovery (DSC-01..08)**: the durable DiscoveryRun stage
  (waiting_question releases the runner, resume keeps the evidence,
  fast-path plans VISIBLY un-researched); read-only snapshot
  workspaces with evidence-ref binding and credential stripping; the
  bounded toolbox (read/list/grep/find_symbol/find_references —
  unauthorized paths never leak even a NAME, truncation explicit and
  pageable, completeness flags survive budget exhaustion); the project
  map with vendor exclusion and commands-as-unvalidated-metadata; the
  SystemManifest (declared/observed/inferred edges, cycles are legal
  service graphs, Backstage owner is routing metadata never
  authorization); bounded impact slices with stored omissions; bounded
  probe requests with baseline-vs-environment failure classification.
- **Planning (PLN-01..08)**: the revision classifier (tactical vs
  material scope/contract/migration) with tactical application and
  immutable revision events; CAS revision decisions (a stale epoch
  refuses, a double-decide refuses); durable Questions with actor
  scope, expiry, and parent routing; precise evidence invalidation
  (superseded is MARKED, never deleted); stale-callback guards;
  decision records that reject secret-looking values; fresh-session
  briefs reconstructed from durable artifacts.
- **Human control (CTL-01..08)**: the durable command mailbox
  (idempotency-key dedup, strict sequences, the
  received→authorized→applied→checkpointed ladder, CAS expiry to
  never a wrong-state apply); pause as revoke-then-interrupt with
  cooperative drain or the honest last-recoverable checkpoint on
  timeout; resume from a confirmed checkpoint with a fresh epoch
  (native session only for pinned interactive profiles, else durable
  reconstruction); bounded steering that NEVER grants authority
  (acceptance-policy changes rejected and routed to the revision
  gate); the final cancel contract — cancellation generations across
  every stage, accepted effects correlated-superseded, any restart
  requires a NEW work command.
- **Execution (EXE-01..08)**: the role-aware HarnessRuntime protocol
  with a capability matrix (checkpoint-only never advertised
  interactive); ClaudeSDK / Codex App / OpenCode adapters over
  injected clients (steering bound to the active turn on Codex; BYOK
  profiles gated by the DriverMatrix — unregistered combos fail at
  onboarding); the outbound control channel (broker-owned token refs,
  monotonic sequences); portable workspace checkpoints with
  traversal-safe rehydration; the checkpoint-restart batch contract
  (control applies at checkpoint boundaries, never mid-turn); tool/
  egress isolation (the docker socket belongs to the trusted test
  executor alone).
- **Multi-repo work (MRP-01..05)**: WorkPackage with ONE writable
  repository per child lane and read-only sibling context;
  dependency phasing (a cycle raises 'phase the CHANGE, not the
  architecture'); bounded parallelism; the explicit publication saga
  (partially_published, recovery by intents, NO pretend rollback);
  CandidateSet freezing (one candidate change invalidates the
  integration-result binding).
- **Verification (VER-01..08)**: lane separation (the trusted
  executor, never the coding agent's environment); focused dependency
  recipes; HTTP/message contract checks; DB upgrade plans from
  data-bearing baselines; the async failure catalog (crash between
  commit and ack, redelivery, ordering); environments bound to set
  digests; identity-based verification selectors with
  never-throw freshness; evidence-aware review (implementation claims
  are never their own proof); the ten-service seeded-hazard benchmark
  (hazards visible, 10-read/1-write acceptance shape).
- **Operations (OPS-01..05)**: plan quality measured before model
  selection; end-to-end per-stage usage lineage (never conflated);
  budget reports; ONE coherent status projection naming the single
  next action; retention decisions (an unknown policy routes to
  review); admission and error-budget capacity controls.

Suite 3731; FI 8/8 on disposable Postgres; mypy at baseline.

## [0.16.0] - 2026-09-21

### Fixed — adaptive foundations (sixth review 05868e9 + customer plan: first slice)

The customer plan's two headline gaps (planner without repository
evidence; plan that cannot evolve under human control) become a 64-story
roadmap — committed under `docs/roadmap/2026-09-21-adaptive/` — and this
release lands its first slice: the two authority remainders and the
typed contracts substrate everything else builds on.

- **FND-01 — the public RepositoryIdentity contract**: adapters implement
  ``identity()``; the authority cache keys on it (tenant + provider +
  native repo + ref + path), never private-attribute probing. The real
  ``AzureRepositoryReader`` (project/repo) previously fell into the
  project-id fallback — two repositories of ONE Azure project could share
  one policy entry. GitHub identity = API host + owner/repo; two hosts
  with identical names never share. Regressions run the REAL reader
  constructors over stub transports.
- **FND-02 — the publication fence at the final native-effect boundary**:
  ``publish_validated`` re-evaluates a pre-dispatch guard immediately
  BEFORE the commit-API call — after the bridge's awaited reads and
  branch setup — so a cancel landing during the publisher's own
  operations yields zero native writes.
- **The adaptive contracts substrate** (pydantic, extra-forbid, digest-
  and vocabulary-guarded): WorkContract / PlanRevision / ChangeProposal /
  ControlCommand / SnapshotSet / CandidateSet / Checkpoint — the review
  package's own examples parse verbatim; corruption tests pin every
  guard (write-inside-read, dependency resolution, closed states,
  unique repositories, sha-shaped digests).

## [0.15.0] - 2026-09-21

### Fixed — authority boundary coherence (fifth external review 44cdae: all 12 findings closed — evidence in `python -m forge.release_manifest`)

- **D01 — config cache identity**: the project-config cache keys on the
  canonical authority identity (provider class, repository, requested ref,
  path) — two repositories of one project (or two refs of one repository)
  no longer cross policies through the cache; cached absence of one repo
  never masks another's restrictions.
- **D02 — strict policy schema**: `implement.paths: "src/**"` (a string)
  used to fall through to `[]` == whole repository, widening a typo into
  UNRESTRICTED scope; `forge: []` raised AttributeError through the typed
  reader. Both are typed invalid now — malformed never means "allow more".
- **D03 — the frozen scope reaches the commit boundary**: both GitHub
  publish sites pass the spec's allowed_paths; an out-of-scope candidate
  results in ZERO commits and ZERO PRs (service-bound regression,
  mutation-verified).
- **D04 — cancel-before-dispatch forbids the write**: the publication
  grant is re-checked immediately before the native dispatch — a cancel
  landing during the paid propose refuses the commit (the branch CAS
  checks the expected head, not the run's right to publish).
- **D05 — factory branches from the attempt OID**: a moved main no longer
  shifts the execution context past the approval (target_branch stays the
  MR destination).
- **D06 — a declared manifest, even empty, is the boundary**: unset
  manifests keep the legacy default; an explicit {'drivers': []} allows
  no driver (callers widen only None).
- **D07 — the cohort rate is a ratio of sums** over the whole joined
  population (run+attempt identity survives the flattening) — the old
  form divided the LAST attempt's numbers (order-dependent; crashed on
  an empty cohort).
- **D08 — receipts provenance**: workspace-file command receipts are
  stamped `self_reported` — telemetry, never wrapper-observed proof.
- **D09/D11** — agree-collision semantics documented (D09 development
  note); `_publish_candidate_run_aware` is the run-aware publication
  entry — the only way the service publishes, loading approved policy by
  run identity with the grant re-check inline.
- **D10 — mutation guards run baseline-then-mutant** on identical traces.
- **D12 — README version/test-count/image tracked by tests** (found stale
  at 0.11.0 AND 0.13.0 in consecutive reviews; now CI-enforced).

## [0.14.0] - 2026-09-21

### Fixed — contract hand-off coherence (fourth external review 7f0139e: all 12 findings closed — evidence in `python -m forge.release_manifest`)

- **C01 — identity-preserving CI verdicts**: a display name claimed by
  several workflow identities with DISAGREEING conclusions is
  ambiguous_check_identity (never verified; agreeing collisions are fine);
  has_pending is decided after the authoritative collapse over the PROOF
  set — a stale/optional pending run no longer holds completed required
  checks hostage.
- **C02 — one budget truth**: the spec builders use the selection's
  RESOLVED ceilings (pinned to the opened RunBudget when the planner moved
  the class) — RunBudget == RunSpec == selection; the class name is never
  re-resolved at freeze.
- **C03 — the credential registry is the contract**: the AzDO recipe maps
  grok-build→FORGE_GROK_AUTH (not XAI, not 'grok'), opencode→ZAI_API_KEY,
  copilot→its token alone, claude-code→the Anthropic surface; both shipped
  recipes AND the GitHub workflow are cross-checked against
  DRIVER_CREDENTIAL_VARS by tests. An enforced dispatch missing the read
  token or any envelope input FAILS CLOSED — a stale pre-provisioned brief
  never executes.
- **C04 — Azure recovery is repository-scoped** (B08's twin): the config/
  attempt recovery scans select only the bound repo; the durable subject
  column is populated at AzDO start (the scans could never match before).
- **C05 — frozen-spec dispatch on GitLab**: BackendStartSpec — the harness
  backend executes the approved model/target (legacy callers fall back
  loudly); the Draft MR target comes from the frozen spec too. Settings
  drift after approval cannot move a dispatch or an MR.
- **C06 — a declared capability manifest is a strict upper bound**: an
  empty/disjoint intersection raises at compile time instead of silently
  degrading to the default driver.
- **C07 — identity-joined effective output rate**: the cohort rate divides
  tokens by the SAME calls' durations (attempt-id join); count equality
  proves nothing and yields None; the name is honest (full-request
  duration ≠ decode speed).
- **C08 — bounded MR I/O under the reservation lock**
  (FORGE_MR_IO_TIMEOUT_SECONDS): a hung list fails CLOSED, a hung create
  records unknown_outcome.
- **C09 — honest 019 downgrade**: genuine multi-observation history refuses
  ('forward-only from here') instead of failing mid-index or deleting audit
  rows; proven on real Postgres both ways.
- **C10 — ObservedExecution command receipts**: the trusted wrapper's
  (argv, exit, report) rows from .forge/commands.tsv ride the candidate
  meta — 'allowed' stays declared vocabulary, these are the proof it ran.
- **C11 — mutation guards**: reverting C01's ambiguity map or B01's epoch
  makes the guards FAIL (the regression tests keep their bite).
- **C12 — README/quick-start synced** to the current release (was
  0.11.0/2601 tests — a new user could install a release predating every
  fix the docs describe).

## [0.13.0] - 2026-09-21

### Fixed — recovery/evidence/recipe coherence (third external review e53ffd2: all 15 findings closed — evidence in `python -m forge.release_manifest`)

- **B01 — immutable verification deadlines**: the wait anchors to a persisted
  verification_epoch {candidate_sha, started_at} (GitHub+Azure), never to
  FlowRun.updated_at — every observation merges evidence and slid the deadline
  forever (probe: 480 observations / 7200 simulated seconds, no timeout).
- **B02 — authoritative CI evidence**: the newest RUN (run_number, id) is the
  authoritative occurrence (run_attempt orders attempts WITHIN a run and must
  never order across runs — an old rerun's success masked a newer failure);
  observations group by workflow identity before the display-name merge.
- **B03 — MR intent vs immutable attempts** (migration 019): mr_reservations
  — the ONE-MR-per-run+branch logical intent, committed BEFORE provider I/O,
  FOR UPDATE-serialized — separate from the action journal; the lost-response
  window adopts via its OWN observation row (no InvalidActionTransition
  deadlock); a failed MR-list read fails CLOSED (no create on an unknown
  surface).
- **B04 — the AzDO lane renders the brief ENFORCED**: the plan leg freezes the
  BriefEnvelope, embeds the approved sections in the plan comment and
  dispatches plan_note_id/envelope_digest/spec_digest (service, handle and
  crash-re-dispatch); an edited comment/work item fails the lane closed — no
  stale-brief fallback on the enforced path.
- **B05 — the shipped AzDO recipe is dispatch-only for real**: explicit
  `trigger: none` (its absence left the IMPLIED CI trigger queueing the lane
  on every push); repair_context flows parameter→variable→env (the
  $(repair_context) macro referenced an undefined variable); harness
  credentials scope to the selected driver via conditional LANE_* variables.
- **B06 — verification waivers freeze into the spec** at approval: a
  post-approval global waiver flip cannot loosen an approved run (empty
  frozen set = NO waivers — no settings fallback on v3+).
- **B07 — required-mode verification**: a non-empty frozen required list with
  no observed checks is a missing MANDATORY GATE (wait → deadline block),
  never an unverified READY; an unreadable PR head surfaces
  freshness_unknown in the evidence.
- **B08 — repository-scoped recovery**: every repo-bound recovery scan
  (revivals, attempt recovery, config blocks) selects only the bound
  repository's runs — a provider-wide scan drove repo B's runs through repo
  A's reader/client.
- **B09 — honest cohort economics**: all-unknown durations/repairs are None
  (never or-0); a receipt-less attempt makes the unit's exact cost unknown
  with a priced LOWER BOUND (cost_exact=false); rates build only over
  proven-matched populations.
- **B10 — pass-1 evidence backfill**: the committed ledger/report carry the
  known execution profile; usage stays honestly unknown — no zero-cost
  claims.
- **B11 — current-plan selection**: the task-aware harness selection compiles
  against the CURRENT plan (the pre-plan compile only reserves the planning
  budget) — a reused planner object can never leak a previous run's plan.
- **B12 — ObservedExecution**: the candidate meta carries what the lane
  ACTUALLY did (driver, exit, usage completeness, candidate-changed) — the
  observed twin of the declared profile; allowed-but-unexecuted is never
  executed.
- **B13 — state-specific status comments**: the post-publish note says
  "candidate published — verification pending" (not a premature ready);
  the verified/unverified ready note follows verification + review.
- **B14 — machine-validated finding coverage**: B01-B15 closures in the
  release manifest with grep-verifiable markers; the suite runs
  warning-clean (the aiosqlite worker-thread teardown artifact is
  documented and narrowly ignored).
- **B15 — recovery-scan boundary pinned by test**: provider services never
  re-implement their own blocked-run scans; the shared dispatcher helpers
  own them.

## [0.12.1] - 2026-09-20

### Fixed — live leg checks (2026-09-20: GitLab + GitHub + Azure DevOps driven end-to-end against real instances)

- **GitHub — launch correlation**: the runs-list call sent the filter as the undocumented
  `head_branch=` param (the API wants `branch=` and silently drops unknown names), so
  concurrent dispatches sharing one re-frozen attempt base latched each other's workflow runs
  — whole batches died `harness_artifact_missing` in the grace window. Client sends `branch=`;
  the executor re-verifies branch equality client-side (e7b308a).
- **Cohort seed CI**: the seed shipped forge's own ci.yml into fixture repos (no
  pyproject.toml → permanently red baseline, repair loop could never converge). The seed now
  renders a per-unit checks workflow FROM the predeclared acceptance checks (927021b).
- **Publication intents**: completing a terminal intent with the SAME effect id is now an
  idempotent no-op instead of a raise that aborted the publish step mid-transaction (the
  aborted journaling then degraded the review to "(diff unavailable)" — fixed separately by
  falling back to the base..candidate compare when the PR ref is missing) (22e855a).
- **Cohort ledger**: attempt rows record once per drive; the cancel procedure targets the
  in-flight row instead of a stale historical one (82901e9).
- **Azure lane packaging**: the lane published all of `.forge/` including forensic logs; the
  archive allowlist is CLOSED (exactly candidate.diff + candidate.meta.json) — now staged
  into a clean `forge-output/` (5b2add4).
- **Azure dispatch/revival**: the dispatch identity is journaled BEFORE the Runs-API call
  (A12); a revival that still lacks identity re-parks the run blocked instead of stranding a
  proposing zombie (69e9799).
- **Azure self-trigger**: every forge comment carries a hidden `forge:authored` marker and
  the gateway skips marker-bearing comments BEFORE the author check — on single-PAT
  deployments forge posts under the operator's identity and the author check alone both
  missed forge's own notes AND would drop the operator's real commands (0acda79).
- **Azure candidate diff**: the emit step scrubs `__pycache__`/`.pyc` and emits a text-only
  diff — a staged .pyc failed every candidate `binary_not_supported` (2cbdf4e).
- **Azure verification read**: the builds list requires `repositoryType=TfsGit` next to
  `repositoryId` AND a repository GUID (a name 400s) — without these the verification pass
  read NO builds and every run died `verification_timeout` (06b5554, b36d31f).

## [0.12.0] - 2026-09-19

### Changed — guarantee parity (second external review d16f523: all 18 findings closed — evidence in `python -m forge.release_manifest`)

- **A01/A02 — same spec, same verification everywhere**: GitHub and Azure freeze and consume
  the same executable RunSpec v3 as GitLab; verification is positive proof that the REQUIRED
  checks from the frozen contract ran (skipped/neutral/unknown need a waiver; cancel/timeout
  are infrastructure, not code repair; the actual head is re-read before verified-ready).
- **A03 — the lane executes the approved bytes**: BriefEnvelope digests bind task/plan;
  any post-approval edit fails the lane closed (re-approval required).
- **A04/A05 — ownership through the whole step**: guarded CAS is the only write path;
  the publisher validates claim ownership before the native call; the sequential worker
  claims one step at a time; heartbeat loss fails the handler closed.
- **A06/A07 — object-level authorization**: MCP run tools honor repository allowlists;
  full-ID operator commands carry the same subject scope as every other form.
- **A08/A09 — shipped-recipe fixes**: non-hidden artifact staging; tokenized permission
  rules (the glued-string bug that defeated the allowlist expansion).
- **A10/A11 — durable concurrency**: concurrent budget creation via ON CONFLICT arbiter;
  retry/auto-revive as durable single transitions with idempotency by delivery id.
- **A12 — effect certainty**: negative probes open a bounded settle window (never treated
  as proof of absence); GitLab parks unknown, GitHub/Azure lean on native CAS.
- **A13 — config scope**: read failures park the run; only confirmed absence earns the
  default profile; provenance frozen in the spec.
- **A14/A16 — conformance and evidence**: composed scenarios on real legs; a verifiable
  release-evidence manifest (28 entries, honest levels, fail-closed guards) as the source
  of truth for capability claims.
- **A15 — ADR-0027 slice 2**: ObserveVerification extracted as one provider-neutral use case.
- **A17/A18 — measurement and profiles**: a 14-task delivery evaluation cohort harness and
  a versioned execution profile (target-contract lanes, bootstrap classification).

## [0.11.0] - 2026-09-18

### Added — Phase B/C/D complete (external review e8bf381: every finding closed — evidence-scoped)

"Closed" above means the fix and its landed test evidence are in this tree
(per-finding evidence levels: `python -m forge.release_manifest`); it does
NOT mean every capability is exercised at every runtime depth — nightly OS
failure injection and the real-provider dogfood loop stay on their own
evidence classes and gates.

- **A16** release-evidence manifest: capability/status claims are generated
  from a verifiable registry (`python -m forge.release_manifest`), not
  hand-written absolutes — per capability, provider/backend, level
  (implemented / contract_tested / live_canary_tested / not_run) with the
  evidence pointer, CI job and gate; boot canary, subprocess FI, coroutine FI
  and real-provider e2e stay separate evidence classes; unknown stays
  explicit (`not_run`), never converted to pass; pyproject/`__version__`
  consistency is asserted at manifest build time (the R30 discipline, now
  failing in tests, not only in the release workflow).
- **A02/A13** verified in-tree and recorded as the first d16f523-review
  closures: GitHub and Azure freeze the same executable spec v3
  (`EXECUTABLE_SPEC_SCHEMA_VERSION`), and project-config reads are typed —
  only a confirmed absence earns the default profile. A01, A04-A12 remain
  unclaimed by the manifest until their fixes verify in-tree.

- **R04** ExecutableRunSpec v3: the gate approves bytes that execute — task/plan/model/
  policy/budgets frozen content-addressed, digest-verified on every read.
- **R07** bounded steps with replay: every non-deterministic step checkpoints; a crash
  never re-calls the model or re-derives published work.
- **R10** ExecutionClaim + guarded transitions + publication grants (cancel fences claims).
- **R11** PublicationIntent persisted before HTTP on all three providers; probe-first
  reconciliation adopts lost pushes (the live branch_drift case now adopts).
- **R13** numeric budget profiles enforced on the standard path (planning reserves).
- **R14** BlobReadResult: only a confirmed 404 proves absence.
- **R24** honest delivery metrics (accepted/rejected/rework, time decomposition).
- **R29** operator commands: /status, /why-blocked, /reconcile (all providers).
- **R31** capability-aware harness selection (manifest + policy-bound planner proposal +
  numeric budget binding).

### Fixed
- Provider-namespaced unique index; Alembic live-head startup gate replaces the
  stale marker; MCP classic-tool scope bypass; worker reaper parks exhausted
  steps; late-callback supersede widened to all terminal states.

### Added — terminal-failure revival (Tier 1 auto-revive + Tier 2 `/retry`)

- **Tier 1 — automatic revival of transient deaths**: every `failed`
  terminalization is classified (`forge.runs.revival`) before the run parks.
  A *transient* cause (dispatch/CI 5xx, network, timeout, rate limits, runner
  startup, an empty harness-start error) parks `blocked` with a revival stamp
  in its evidence; the provider reconciler re-dispatches the SAME branch after
  bounded backoff (60s doubling, capped), at most
  `FORGE_RUN_AUTO_REVIVE_LIMIT` (default 2) times, journaled as `auto_revive`
  actions. No issue comment, no operator. `FORGE_RUN_REVIVE_BACKOFF_SECONDS`
  sets the ladder base; `0` disables auto-revive.
- **Tier 2 — `@forge /retry [run-id]`** on GitLab, GitHub and Azure DevOps
  (bare = the issue's latest `failed`/`blocked` run): approver-authorized like
  `/go`, it walks the run back to `proposing` through the explicit revival
  graph edge, grants ONE operator cycle (may exceed `FORGE_MAX_COMMIT_CYCLES`)
  and re-dispatches the same branch with the terminal reason and the last
  verification evidence as the repair context. Cancelled runs, and runs that
  never committed a candidate, are rejected with an actionable note pointing
  at `/implement`.
- **Fatal failures park `blocked`, not `failed`**: config errors (4xx input
  mismatches, missing workflow), driver quality signals and exhausted cycles
  carry a precise, actionable `status_reason` — nobody watches a run flap.
  ADR-0004 amended with the revival edge (`Controller.revive_transition`,
  audited via `authorized_by` in the outbox payload).

## [0.10.0] - 2026-09-18

### Added — Phase-A correctness alignment (external review e8bf381: all Phase-A P1s closed)

- **R01 — one publication boundary (ADR-0026)**: `publish_validated_candidate`
  owns validate → intent → native adapter; `ValidatedCandidate` is the capability
  to publish (raw ChangeSets refused by type). The GitHub builtin path now crosses
  strict materialization + policy validation before any commit-API call.
  28 negative conformance tests: 8 deny scenarios × 3 publish paths, zero
  commit-API calls asserted.
- **R02 — verification parity**: Azure publication parks at `waiting_ci` with a
  reconciler pass correlating Builds by candidate sha (lane excluded);
  red → repair/blocked, grace → honest unverified. GitLab empty profiles label
  ready as unverified. Unified `VerificationResult` evidence on all providers.
- **R03 — provider-namespaced identity**: migration 012 rebuilds the active-run
  unique index over (provider, project_id, issue_iid); every lifecycle scan and
  guard filters provider — a GitHub repo-id 5 no longer collides with a GitLab
  project 5.
- **R05 — brief binding**: the lane renders the brief from the EXACT journaled
  plan-comment id (dispatch input → addressed fetch → fail-closed on
  missing/wrong-author/wrong-run); the 100-comment heuristic is a loud legacy
  fallback.
- **R08/R09 — patch engine**: discriminated representations
  (Create/Delete/FullReplacement/UnifiedPatch) with `base_blob_digest` +
  `intended_digest` verification; POSIX zero-context placement, `\n`-only
  splitting (CRLF/U+2028 safe), typed rejections (mode/rename/binary/
  corrupt/stale); 31-case differential suite against `git apply` as the oracle.
- **R16 — artifact recipe**: clean non-hidden output dir, meta schema v2
  (attempt id, manifest digest, usage receipt), control-plane ZIP caps/
  allowlist/validation with one bounded retry.
- **R17 — liveness**: deadline/cancel evaluated BEFORE provider I/O on all
  three providers; bounded dispatch discovery; the reaper parks exhausted and
  deadline-exceeded steps as dead; late callbacks to terminal runs are
  superseded evidence, never READY.
- **R19 — MCP authorization**: default-deny wrapper on every tool, repo-target
  allowlist (`FORGE_MCP_TOKEN_REPOS`), denial auditing.
- **Lifecycle parity (#38/#29)**: `issue_edited` auto-replan and `unlabeled`
  gate-cancel on all three providers; the parity test whitelist is empty.
- **Operator tooling**: `/retry` (same-branch revival, operator-granted cycle),
  Tier-1 auto-revive of transient deaths, superseded-PR janitor.
- **Lane hardening**: `bypassPermissions` + mechanical deny (the allowlist
  whack-a-mole is retired), ephemeral `CLAUDE_CONFIG_DIR` (no cross-run memory
  bleed), `uv sync --frozen` from the target lock (the agent runs the gates CI
  runs), pinned toolchain, lane job ceiling 120m.
- **Research base**: docs/research/{patch-application, remote-effect-reconciliation,
  actions-artifacts-usage, schema-upgrade-gates}.md.

## [0.9.0] - 2026-09-16

### Added — task-aware harness selection (ADR-0023) + Azure DevOps adapter beta (ADR-0024)

- **Task-aware harness selection**: the implementer harness is no longer a
  single pin. `.forge.yml` / `FORGE_HARNESS_PREFERENCE` carry an ordered
  preference list; a pure compiler freezes `backend_config{harness,
  harness_fallbacks, budget_class, selection_reason}` into the immutable
  RunSpec (schema v2, bound by the policy digest — changing the chain
  invalidates pending gates). The plan comment gains an "Implementation"
  block, so `/go` authorizes the execution shape, not just the plan.
  Dispatch-time fallback is opt-in, off by default, infrastructure-only,
  journaled, and bounded to the frozen chain. All four GitLab templates
  gained driver-filter rules so multi-driver repos run exactly one lane;
  `forge doctor` reports the compilable chain. Research:
  docs/research/2026-09-15-harness-selection.md.
- **Azure DevOps adapter (beta)** — the third provider, same iron
  contract: `/implement` on a work item or PR comment → plan as a
  work-item comment → `/go` → harness lane in Azure Pipelines
  (dispatch-only proposal-only lane, candidate artifact) → trusted
  publisher via the native CAS (`oldObjectId`) → Draft PR → branch-policy
  CI → reactive review via PR iterations (sticky marker thread, inline
  findings) → `ready_for_human`; build failures → durable debug lane.
  Fail-closed service-hooks ingress (Basic credentials, constant-time;
  no HMAC exists), connection-scoped approvers, identity-enforced
  no-merge. `forge doctor` AzDO checks; docs/azure-setup.md runbook with
  the live-verification checklist. Live verification pending a user PAT
  (tests: 190+ new across client/ingress/service/executor/lanes/joins).
  Research: docs/research/2026-09-15-azure-devops.md (12 payload fixtures; three
  documented-API contradictions found and corrected before implementation).

### Fixed

- Pre-existing mypy errors in gateway/stores; stale uv.lock; three
  ruff-format violations in the MCP modules.

## [0.8.0] - 2026-09-15

### Added — MCP modernization + delivery metrics + fourth harness (v0.8, ADR-0021)

- **GitHub Copilot CLI as the fourth harness driver** (R6 research,
  `docs/research/2026-09-15-harness-config-best-practices.md` §8): proposal-only
  `copilot` lane (`ci/templates/copilot.gitlab-ci.yml`) + Actions-lane
  driver in `forge.harness_entry` — headless `copilot -p`, scoped grants
  (`read,write` + `shell(git:*)`) with deny-wins `--deny-tool` on
  commit/push, env-token auth (fine-grained PAT with "Copilot Requests";
  classic `ghp_` unsupported), optional `COPILOT_MODEL` (RunSpec model
  routes do not map onto Copilot model names), usage stays unknown.
- **Scoped MCP run surface** (ADR-0021 §4 / R3 §8.1):
  `FORGE_MCP_SCOPED_TOKENS` defines per-token principals over a closed
  scope set (`forge:read`, `forge:runs:write`, `forge:approvals:write`,
  `forge:admin`); `run_list` / `run_get` / `plan_get` / `run_evidence_get`
  read durable state directly — the run surface never acts with forge's
  provider tokens (platform-token passthrough killed for reads).
  Per-call scope enforcement with model-actionable denials + an audit log
  (`forge.mcp_server.audit`). Malformed config or unknown scopes fail
  startup.
- **Delivery ladder metrics (F34)**:
  `forge_delivery_ladder{stage=started|planned|gate_approved|candidate_published|ci_passed|ready_for_human}`
  gauges in `/metrics.prometheus` and JSON `/metrics` — where work
  packages stand on the acceptance ladder (the merged rung lives
  provider-side; the bot never merges).

### Fixed

- **The mounted MCP endpoint was broken in production twice over**: the
  FastMCP session manager never ran under the FastAPI mount (every /mcp
  request failed with "Task group is not initialized"), and the SDK's
  DNS-rebinding Host check answered 421 to proxied requests.
  `FORGE_MCP_ALLOWED_HOSTS` lists the public host behind a proxy.
- Harness templates: repaired the YAML a NO_PROXY insert broke (dropped
  list dash), restored the "Do NOT commit and do NOT push" contract
  phrase; opencode config allows `external_directory`/`doom_loop`
  ("ask"-by-default headless hang sources).
- GitHub webhook payloads now captured under `FORGE_CAPTURE_DIR` (the
  GitLab route captured; the GitHub one never did) — live routing gaps
  are diagnosable from `data/captured/`.

### Changed — R5 harness config hardening (research top-5, all lanes)

- Mechanical commit/push deny in every driver (not just the brief):
  Claude `--disallowedTools`, Grok `--deny` (survives
  `--always-approve`), opencode permission-map denies, Copilot
  `--deny-tool` — the contract holds even if the model disobeys.
- Claude: `--permission-prompts none` (explicit no-prompt guarantee),
  `--max-turns 200`, vendor timeout budgets (`API_TIMEOUT_MS`,
  `BASH_*_TIMEOUT_MS`), retrying npm preamble with
  `FORGE_CLAUDE_VERSION` pin.
- Grok: `--trust` (project rules load headlessly) + `--max-turns 200`.
- `NO_PROXY` declared unconditionally in every lane.
- The Actions lane (`forge.harness_entry`) mirrors the full posture;
  contract tests (`TestMechanicalDeny`) now REQUIRE the deny constructs.

## [0.7.0] - 2026-09-14

### Added — reactive parity + complex workloads (v0.7, ADR-0021)

- **GitHub reactive review engine**: pull_request opened/synchronize on
  tracked repos → readonly LLM review posted as a native GitHub review
  (inline severity comments, REQUEST_CHANGES for critical); incremental
  via synchronize before/after SHAs; sticky progress comments never
  duplicate; forge-authored PRs skipped (recursion guard).
- **Actions failure debugging**: failed Actions jobs on PR heads →
  durable debug step → pipeline-debugger agent → sticky root-cause
  comment (fork-safe PR correlation; forge's own harness failures
  excluded — they have their own triage).
- **GitLab durable pipeline-debug**: failed pipelines on non-forge
  branches → root-cause MR notes (the repair loop keeps its own log
  ingestion).
- **Security findings ingestion + triage** (research:
  ci-security-surface.md): GitLab CE gl-sast/secret-detection artifacts +
  GitHub code/secret scanning + Dependabot alerts → forge-owned triage
  state keyed by forge-computed fingerprints (CE has no vulnerabilities
  API); `/security` command → bounded security-triage agent → grouped
  comment; remote dismissal opt-in
  (`FORGE_SECURITY_REMOTE_DISMISS`, default off).
- **Monorepo path-scoped packages**: `.forge.yml` implement.paths globs
  frozen into the RunSpec and enforced at validation and the publisher;
  the plan prompt carries the scope; unscoped projects byte-identical.

41 new tests; 1328 passed / 2 skipped.

## [0.6.0] - 2026-09-14

### Added — trust surface (v0.6, ADR-0021)

- **Budget reservation** (F22, ADR-0018 §5): run budgets opened at RunSpec
  freeze; every model call reserves before dispatch (refusal =
  `budget_exhausted`, provider never touched); harness usage receipts
  reconcile actuals; unknown completeness stays unknown — never zero.
  Migration 009.
- **Evidence policy** (F23): deny-pattern redaction + char caps on repair
  contexts and harness evidence; canary tests prove secrets cannot reach
  prompts or evidence comments.
- **Connection-scoped approvers**: FORGE_GITHUB_APPROVERS vs GitLab
  approvers — provider logins are separate identities (the @demo
  cross-provider leak, found live, closed systematically).

### Fixed — CI hardening (F29)

- Typecheck is blocking on six core packages (110 errors fixed, including
  a real PAT-mode crash: GitHubStaticCredentials field/method collision).
- New required `integration` CI job: the failure-injection exit-bar runs
  on real Postgres service containers.

## [0.9.0] - 2026-09-16

### Added — task-aware harness selection (ADR-0023) + Azure DevOps adapter beta (ADR-0024)

- **Task-aware harness selection**: the implementer harness is no longer a
  single pin. `.forge.yml` / `FORGE_HARNESS_PREFERENCE` carry an ordered
  preference list; a pure compiler freezes `backend_config{harness,
  harness_fallbacks, budget_class, selection_reason}` into the immutable
  RunSpec (schema v2, bound by the policy digest — changing the chain
  invalidates pending gates). The plan comment gains an "Implementation"
  block, so `/go` authorizes the execution shape, not just the plan.
  Dispatch-time fallback is opt-in, off by default, infrastructure-only,
  journaled, and bounded to the frozen chain. All four GitLab templates
  gained driver-filter rules so multi-driver repos run exactly one lane;
  `forge doctor` reports the compilable chain. Research:
  docs/research/2026-09-15-harness-selection.md.
- **Azure DevOps adapter (beta)** — the third provider, same iron
  contract: `/implement` on a work item or PR comment → plan as a
  work-item comment → `/go` → harness lane in Azure Pipelines
  (dispatch-only proposal-only lane, candidate artifact) → trusted
  publisher via the native CAS (`oldObjectId`) → Draft PR → branch-policy
  CI → reactive review via PR iterations (sticky marker thread, inline
  findings) → `ready_for_human`; build failures → durable debug lane.
  Fail-closed service-hooks ingress (Basic credentials, constant-time;
  no HMAC exists), connection-scoped approvers, identity-enforced
  no-merge. `forge doctor` AzDO checks; docs/azure-setup.md runbook with
  the live-verification checklist. Live verification pending a user PAT
  (tests: 190+ new across client/ingress/service/executor/lanes/joins).
  Research: docs/research/2026-09-15-azure-devops.md (12 payload fixtures; three
  documented-API contradictions found and corrected before implementation).

### Fixed

- Pre-existing mypy errors in gateway/stores; stale uv.lock; three
  ruff-format violations in the MCP modules.

## [0.8.0] - 2026-09-15

### Added — MCP modernization + delivery metrics + fourth harness (v0.8, ADR-0021)

- **GitHub Copilot CLI as the fourth harness driver** (R6 research,
  `docs/research/2026-09-15-harness-config-best-practices.md` §8): proposal-only
  `copilot` lane (`ci/templates/copilot.gitlab-ci.yml`) + Actions-lane
  driver in `forge.harness_entry` — headless `copilot -p`, scoped grants
  (`read,write` + `shell(git:*)`) with deny-wins `--deny-tool` on
  commit/push, env-token auth (fine-grained PAT with "Copilot Requests";
  classic `ghp_` unsupported), optional `COPILOT_MODEL` (RunSpec model
  routes do not map onto Copilot model names), usage stays unknown.
- **Scoped MCP run surface** (ADR-0021 §4 / R3 §8.1):
  `FORGE_MCP_SCOPED_TOKENS` defines per-token principals over a closed
  scope set (`forge:read`, `forge:runs:write`, `forge:approvals:write`,
  `forge:admin`); `run_list` / `run_get` / `plan_get` / `run_evidence_get`
  read durable state directly — the run surface never acts with forge's
  provider tokens (platform-token passthrough killed for reads).
  Per-call scope enforcement with model-actionable denials + an audit log
  (`forge.mcp_server.audit`). Malformed config or unknown scopes fail
  startup.
- **Delivery ladder metrics (F34)**:
  `forge_delivery_ladder{stage=started|planned|gate_approved|candidate_published|ci_passed|ready_for_human}`
  gauges in `/metrics.prometheus` and JSON `/metrics` — where work
  packages stand on the acceptance ladder (the merged rung lives
  provider-side; the bot never merges).

### Fixed

- **The mounted MCP endpoint was broken in production twice over**: the
  FastMCP session manager never ran under the FastAPI mount (every /mcp
  request failed with "Task group is not initialized"), and the SDK's
  DNS-rebinding Host check answered 421 to proxied requests.
  `FORGE_MCP_ALLOWED_HOSTS` lists the public host behind a proxy.
- Harness templates: repaired the YAML a NO_PROXY insert broke (dropped
  list dash), restored the "Do NOT commit and do NOT push" contract
  phrase; opencode config allows `external_directory`/`doom_loop`
  ("ask"-by-default headless hang sources).
- GitHub webhook payloads now captured under `FORGE_CAPTURE_DIR` (the
  GitLab route captured; the GitHub one never did) — live routing gaps
  are diagnosable from `data/captured/`.

### Changed — R5 harness config hardening (research top-5, all lanes)

- Mechanical commit/push deny in every driver (not just the brief):
  Claude `--disallowedTools`, Grok `--deny` (survives
  `--always-approve`), opencode permission-map denies, Copilot
  `--deny-tool` — the contract holds even if the model disobeys.
- Claude: `--permission-prompts none` (explicit no-prompt guarantee),
  `--max-turns 200`, vendor timeout budgets (`API_TIMEOUT_MS`,
  `BASH_*_TIMEOUT_MS`), retrying npm preamble with
  `FORGE_CLAUDE_VERSION` pin.
- Grok: `--trust` (project rules load headlessly) + `--max-turns 200`.
- `NO_PROXY` declared unconditionally in every lane.
- The Actions lane (`forge.harness_entry`) mirrors the full posture;
  contract tests (`TestMechanicalDeny`) now REQUIRE the deny constructs.

## [0.7.0] - 2026-09-14

### Added — reactive parity + complex workloads (v0.7, ADR-0021)

- **GitHub reactive review engine**: pull_request opened/synchronize on
  tracked repos → readonly LLM review posted as a native GitHub review
  (inline severity comments, REQUEST_CHANGES for critical); incremental
  via synchronize before/after SHAs; sticky progress comments never
  duplicate; forge-authored PRs skipped (recursion guard).
- **Actions failure debugging**: failed Actions jobs on PR heads →
  durable debug step → pipeline-debugger agent → sticky root-cause
  comment (fork-safe PR correlation; forge's own harness failures
  excluded — they have their own triage).
- **GitLab durable pipeline-debug**: failed pipelines on non-forge
  branches → root-cause MR notes (the repair loop keeps its own log
  ingestion).
- **Security findings ingestion + triage** (research:
  ci-security-surface.md): GitLab CE gl-sast/secret-detection artifacts +
  GitHub code/secret scanning + Dependabot alerts → forge-owned triage
  state keyed by forge-computed fingerprints (CE has no vulnerabilities
  API); `/security` command → bounded security-triage agent → grouped
  comment; remote dismissal opt-in
  (`FORGE_SECURITY_REMOTE_DISMISS`, default off).
- **Monorepo path-scoped packages**: `.forge.yml` implement.paths globs
  frozen into the RunSpec and enforced at validation and the publisher;
  the plan prompt carries the scope; unscoped projects byte-identical.

41 new tests; 1328 passed / 2 skipped.

## [0.6.0] - 2026-09-14

### Added — trust surface (v0.6, ADR-0021)

- **Budget reservation** (F22, ADR-0018 §5): run budgets opened at RunSpec
  freeze; every model call reserves before dispatch (refusal =
  `budget_exhausted`, provider never touched); harness usage receipts
  reconcile actuals; unknown completeness stays unknown — never zero.
  Migration 009.
- **Evidence policy** (F23): deny-pattern redaction + char caps on repair
  contexts and harness evidence; canary tests prove secrets cannot reach
  prompts or evidence comments.
- **Connection-scoped approvers**: FORGE_GITHUB_APPROVERS vs GitLab
  approvers — provider logins are separate identities.

### Fixed — CI hardening (F29)

- Typecheck is blocking on six core packages (110 errors fixed, including
  a real PAT-mode crash: GitHubStaticCredentials field/method collision).
- New required `integration` CI job: the failure-injection exit-bar runs
  on real Postgres service containers.

## [0.5.0] - 2026-09-14

### Added — GitHub path at parity (ADR-0019/0020, F32 completion)

- **Plan + human gate on GitHub**: /implement → plan comment (digest +
  approve instruction) → immutable RunSpec + pending decision with TTL →
  `/go <run-id>` consumes it → publish → Draft PR → sha-bound readonly
  review → ready_for_human. `/cancel` = cancel-as-revoke. One active run
  per (repo, issue); uuid FlowRuns with provider identity (migration 008).
- **GitHub Actions execution adapter** ([ADR-0020](docs/adr/0020-github-actions-executor.md)):
  `/go` can dispatch the real coding-agent harness (Claude Code / Grok
  Build / opencode) into the target repo's Actions runner — proposal-only
  lane (no write credentials, push disabled), candidate artifacts
  (diff + meta + usage) downloaded and applied by the trusted publisher.
- **Shared quality-prompt builder**: one brief structure (role / task /
  constraints / quality bar / lane-specific output contract) rendered for
  all drivers; skills via AGENTS.md / CLAUDE.md conventions.
- **Label trigger**: `issues.labeled` with FORGE_TRIGGER_LABEL (default
  `forge`) starts runs like /implement.

### Fixed — found live during GitHub verification

- Wake-task identity mismatch double-executed run commands (two runs/PRs
  per comment); known-identity-without-step no longer falls back to
  direct execution.
- Candidate meta-key drift (attempt_base_oid vs attempt_base) rejected
  every Actions candidate.
- GitHub-subject runs are no longer polled by the GitLab CI reconciler.
- claude/opencode drivers install themselves in the Actions lane
  (retrying preambles); claude gateway env (AUTH_TOKEN/BASE_URL)
  passthrough.
- Approver-list hygiene: FORGE_APPROVERS are provider logins — never share
  the list across GitLab and GitHub identities.

### Docs

- README rewritten for the two-provider reality; docs/github-setup.md
  (App registration → harness → FAQ); docs/faq.md.

## [0.4.0] - 2026-09-13

### Added — GitHub App adapter, same-repository slice (ADR-0019, F32)

- **GitHub App identity**: RS256 JWT → installation tokens (cached, re-mint
  on 401), least-privilege permissions, private key held as a credential
  reference. Per-installation rate limits with `Retry-After` handling —
  no hot loops.
- **Fail-closed webhook ingress** (`/webhook/github`): `X-Hub-Signature-256`
  over raw bytes (constant-time; official test vector covered by tests),
  503 when disabled, ping, redelivery dedup via the durable inbox.
  `issue_comment` commands route into the same durable step path as GitLab;
  PR comments are distinguishable; installation-deleted disables the
  connection.
- **Publishing**: factory branch cut from the expected head,
  `createCommitOnBranch` with `expectedHeadOid` CAS (STALE_DATA → drift,
  never retried), Draft PR created find-by-head-first (never duplicated).
  Same trusted-publisher/validation semantics as GitLab; human gate is the
  documented next step (this slice is the adapter + flow foundation).
- 57 tests over a CAS-faithful fake, recorded webhook payloads, and JWT
  shape assertions.

### Added — operations read model and API correctness (F26–F28, F30)

- `GET /runs` + `GET /runs/{id}`: durable run read model (steps, evidence
  summary) with optional bearer auth; the legacy `/flows/{id}` route is
  removed. Prometheus exposition at `/metrics.prometheus` (F30).
- DB lifecycle (F26): engines keyed per database URL, async dispose on
  shutdown, `schema_version` compatibility gate (migration 007b) — an
  incompatible existing database refuses to start with upgrade pointers.
- GitLab API correctness (F27/F28): raw-diff endpoint with legacy
  fallback, head checks via single branch GET (no history pagination),
  pagination-cap warning so incomplete evidence is honest. Two raw-diff
  contract xfails closed.

## [0.3.0] - 2026-09-13

### Added — proposal-only harnesses and the trusted publisher (ADR-0016)

- **No write credentials in the execution lane**: harness jobs check out the
  frozen attempt base (detached), run the driver with push disabled by
  construction (`git remote set-url --push origin FORBIDDEN`), and upload
  their result as CI artifacts. `FORGE_BOT_READ_TOKEN` (read-only) replaces
  the write token in the lane; `forge doctor` fails when a write token is
  exposed to it (F04/F21).
- **CandidateBundle**: the runner produces a `git diff --binary` artifact
  plus meta (attempt base, driver, model, exit classification, usage); the
  backend parses it with a strict no-fuzz unified-diff applier against
  authoritative base blobs — binaries, renames and oversize files are
  rejected explicitly (F20).
- **Trusted publisher** (src/forge/runs/publisher.py): grant check (cancel/
  spec digest/fence) → policy validation → single journaled write with
  expected_head. Builtin proposals route through the same validation.
  Nonzero driver exits are never adopted; no-op repairs block as
  `repair_no_effect` (F20).
- **Usage receipts** (F22): harness usage lands in `llm_calls` with
  driver/model/completeness (exact | aggregate | unknown — never
  fabricated); migration 007.

### Changed

- All three harness templates (Claude Code, opencode, Grok) reworked to the
  candidate contract; live-verified end-to-end on the lab: red smoke on the
  seeded bug → bounded repair → review → ready_for_human, with usage
  receipts recorded per attempt.

## [0.2.0] - 2026-09-13

### Added — durable step runtime is now the execution path (ADR-0017)

- **Transactional ingress**: `/implement` and `/go` are answered 202 only
  after the webhook identity and the first scheduled step are committed in
  one Postgres transaction. Redis is a wake-up accelerator, not the
  authority; a crash between receive and execution can no longer lose a
  command (F08).
- **Atomic step ownership**: due steps are claimed with `FOR UPDATE SKIP
  LOCKED` + conditional-UPDATE ownership (portable across PG/SQLite),
  holding a per-step lease (owner, 120s expiry, monotonic fence token)
  renewed by heartbeat. Zombie workers lose the fenced CAS; completion by
  a stale owner is rejected (F09/F10).
- **Full-state recovery**: crash at any transition or after any external
  effect converges — journaled commits and Draft MRs are adopted on resume,
  stuck proposing legs are re-driven, missing READY evidence notes are
  re-posted once (F11). Proven by the new failure-injection suite: two
  real worker processes on live Postgres, hard-kill at six checkpoints,
  cancel-vs-publish race, and 10-way concurrent `/implement` — 8/8
  scenarios, exact effect counting (tests/test_failure_injection.py).
- **DB invariants**: one nonterminal run per (project, issue) via partial
  unique index; one gate per (run, generation) (F12).

### Added — approvals and policy (ADR-0018)

- **Immutable RunSpec** frozen at plan acceptance (subject, source base,
  plan/task digests, extended policy digest, backend config, budgets) — a
  settings change mid-run cannot silently alter an approved execution;
  `/go` validates the spec digest (F14).
- **Pending decision with deadline**: the gate is created at plan
  publication (`FORGE_DECISION_TTL_SECONDS`, default 7 days) and consumed
  at `/go`; expired or spec-drifted decisions are invalid (F15).
- **Admission before spend**: `/implement` from a non-approver is blocked
  before any LLM call; bot-in-approvers is a config contradiction (F16).
- **Cancel-as-revoke**: cancel withdraws scheduled steps, stands down an
  in-flight proposal, and marks late harness results superseded (F13).
- **Verification profile**: empty required-jobs is an explicit warning
  (never a silent pass); branch head re-checked after review — drift
  blocks `candidate_drift_after_review` (F19).

### Migration

- Alembic 005 (step runtime columns + invariants) and 006 (run_specs,
  gate digests, cancel_requested). Migrate before starting the new app/
  worker (see docs/operations/upgrade.md).

## [0.1.1] - 2026-09-13

### Fixed — Stage A safety hotfix (external review, docs/reviews/2026-09-13-v0.1.0/)

- **F01 (P0):** ChangeSet materialization no longer operates on truncated
  blobs — updates to large files cannot silently drop their tail;
  oversized files refuse instead of truncating.
- **F02:** repair cycles build on the last verified candidate
  (`attempt_base`), not the original approved base.
- **F03 (P0):** factory branches are cut from the frozen attempt base with
  an expected-head check (`BranchDriftError`) before committing.
- **F06 (P0):** pipeline / merge-request / note POSTs no longer auto-retry
  (non-idempotent: lost responses are reconciled, not repeated).
- **F07:** unknown commit outcomes resolve by operation marker + parent
  OID; a previous cycle's commit can never be misattributed.
- **F17:** the durable CI deadline fires even for a permanently running
  pipeline or a failing API.
- **F18:** empty / canceled-only CI evidence classifies as `unknown` and
  never triggers an LLM repair.
- **F24:** `ForgeConfig` deep-copies nested defaults (no cross-instance
  mutation).
- **F05 (P0):** the MCP server is fail-closed — not mounted without
  `FORGE_MCP_KEY`; explicit `FORGE_MCP_ENABLED` opt-out.

## [0.1.0] - 2026-09-13

First tagged milestone: the durable factory loop, live-accepted end-to-end
against a real GitLab CE, plus the reactive bot core inherited from
[Codeward](https://github.com/Relrin/codeward) (imported at `fd63ec8`, see
[UPSTREAM.md](UPSTREAM.md)). **Pre-production** — the API surface will move.

### Added — durable run loop (the factory)

- `/implement` → planner (LLM) → **human `/go` gate** (single-use, bound to
  plan digest + base SHA + policy, expiring) → implementer → atomic commit →
  **Draft MR before CI** → reconciler-watched pipeline → readonly LLM review
  → `ready_for_human` with evidence bound to the exact candidate SHA.
  The bot never merges ([ADR-0003](docs/adr/0003-no-merge-is-enforceable.md)).
- Durable execution core ([ADR-0004](docs/adr/0004-controller-owns-lifecycle-implementer-proposes.md),
  [ADR-0005](docs/adr/0005-durable-execution-and-unknown-outcome.md)):
  Postgres-backed lifecycle with a strict transition graph, journaled
  external writes (intent → outcome), unknown-outcome blocking, consume-once
  gates, idempotent webhook inbox, fencing leases, reconciler-polled
  worker-free waits (`waiting_ci`, `waiting_harness`).
- `@forge /cancel [run-id]` (approver-gated, short-id prefixes accepted) and
  a one-active-run-per-issue guard.
- ChangeSet model with exact-match replacement and deterministic
  materialization; Commits API writer with exact-SHA correlation
  ([ADR-0001](docs/adr/0001-commits-api-write-backend-changeset-contract.md)).
- Quality contract
  ([ADR-0008](docs/adr/0008-quality-contract-instead-of-pipeline-status.md)):
  pipeline success + every required job succeeded; failures classified
  code / infrastructure / config — only *code* failures trigger the bounded
  repair loop (`FORGE_MAX_COMMIT_CYCLES`); unknown causes never blame the code.
- Usage ledger (`llm_calls`) for every model call including failures.
- Harness backends
  ([ADR-0015](docs/adr/0015-pluggable-implementer-backends.md)):
  `builtin` (forge-side LLM ChangeSets) and `ci_harness` — Claude Code,
  opencode, and Grok Build CLI running **in the target project's CI**
  (ephemeral containers, credentials only as project CI variables), with
  independent branch-head verification of the claimed result,
  harness-backed repair, streaming job traces, and an optional
  `FORGE_HARNESS_HTTPS_PROXY` for throttled runner networks.

### Added — reactive bot core (from Codeward)

- Code review (inline comments, severity, incremental reviews with thread
  resolution), pipeline debugging, security triage, `@mention` chat, MCP
  server + client, model routing through a LiteLLM proxy
  ([ADR-0014](docs/adr/0014-llm-http-client-over-agno.md): thin HTTP client
  for the factory agents; Agno stays reactive-only).

### Added — operations

- `forge doctor` — read-only environment verification (tokens, Redis,
  database incl. schema presence, LiteLLM, and per-project onboarding:
  webhook, CI variable names, active runner). Human and `--json` output;
  exit code 0 = ready. Never prints secret values.
- AI-ready onboarding: [AGENTS.md](AGENTS.md),
  [onboarding prompt](docs/reference/onboarding-prompt.md), agent skills
  (`.claude/skills/`), and operational runbooks under
  [docs/operations/](docs/operations/).
- Lab-validated failure drills: provider outage, worker crash mid-run,
  harness-job cancellation, duplicate commands, `/go` burst.

### Notes

- 815+ tests (unit/contract), CI on Python 3.13 + 3.14, ruff clean.
- Not yet: budget enforcement beyond the commit-cycle cap, redaction at
  every agent boundary, drift policies beyond block, production hardening.
  See the [README](README.md#status) for the honest gap list.
