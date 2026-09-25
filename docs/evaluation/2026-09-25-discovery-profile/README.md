# Discovery planning profile — customer-scale qualification (R38-11 / #312)

Registered 2026-09-25. Issue: **R38-11 — Turn read-many/write-one discovery
into a customer-scale planning profile** (external review `59ba869`).
Previous: #290 (the live fixture scenario, `evaluation/discovery_live/`),
#292. This task is the MACHINERY slice — the customer-scale planning
profile qualified OFFLINE on a scaled multi-repo fixture graph. The
live-model run over the same graph is the partner-gated remainder
(R38-14) and is NOT claimed here.

## What was built

| Piece | Where |
| --- | --- |
| The profile machinery (observation cache, exhaustion taxonomies, budget-suited synthesis validation, carry-forward) | `src/forge/adaptive/discovery_profile.py` |
| The scaled fixture graph (9 repositories, frozen snapshots with OIDs) | `evaluation/discovery_live/fixtures-customer-v1/` |
| The frozen manifest (connection identities, decisive declarations, caps) | `evaluation/discovery_live/manifest-customer-v1.json` |
| The `--profile` driver and the mechanical grader | `scripts/run_discovery_live.py` |
| The checks (cache, taxonomy arms, budgets, validation routing, carry-forward, manifest, grader arms) | `tests/test_discovery_profile.py` |

## The scaled fixture graph

Nine repositories — beyond the four of the #290 scenario:

- `orders-api` — the ONE writable target (the task's issue lives here);
- three decisive neighbors at DIFFERENT depths:
  - `billing-policy` — the manual-approval threshold at **line 200+**
    of `src/policy/refunds.py` (with superseded thresholds inside the
    same file as decoys);
  - `audit-events` — the emission window at **char 3473** of
    `src/audit/pipeline.py`, beyond the first 2000-char observation
    page: a first-page read cannot see it, only a continuation read
    reaches it;
  - `shipping-policy` — a **conflicting current-vs-obsolete pair**
    (`S-2026-03` "customer-and-fulfillment" at line 39 vs the
    superseded `S-2024-08` "warehouse-only" at line 78 of
    `src/shipping/confirmation.py`, kept visible for a regulatory
    window);
- `platform-docs` — the LARGE irrelevant docs repository (121 files,
  ~185 KB), the budget-sink arm;
- four noise repositories (`marketing-site`, `infra-terraform`,
  `mobile-checkout`, `search-index`).

The manifest is validated by the existing machinery: connection
identities (`ConnectionIdentity`), frozen OIDs re-derived from the
fixture bytes, plus the scale contract (≥8 repos, exactly one writer,
all three depth kinds distinct, a genuinely large sink, decisive
markers living ONLY in their own repositories, markers not leaked into
the task statement, the second-page fact beyond the first page, the
conflict sides at different lines, bounded caps).

## The machinery, as landed

**Observation cache** (`ObservationCache`) — keyed by
`(repository, OID, path, policy-scope)`, exactly the issue's identity
(not text similarity):

- a repeated read of the same immutable source under one scope REUSES
  the verified observation; the underlying reader is not re-paid —
  windows of a cached source are slices, also reuse;
- a different policy scope is a MISS with its own fresh verified read —
  `assert_no_cross_scope_leak()` walks the complete serve log and
  raises on any cross-scope service (an injected leak is caught in the
  tests);
- the cache rides the discovery record (`attach_observation_cache` /
  `cache_from_record`) — a restart restores the observations and
  resumes without re-reading them.

**The exhaustion taxonomies** (`classify_investigation`) — five classes,
each with its OWN bounded recovery:

| class | recovery | bound |
| --- | --- | --- |
| `completed` | carry forward | — |
| `exhausted_budget` | surface + ask (retained findings visible) | 0 extra reads |
| `truncated_output` | continuation read of the cut window | exactly 1 read |
| `invalid_synthesis` | re-synthesis under schema validation | exactly 1 synthesis |
| `confused_policy` | explicit question with BOTH citations | a question, never a pick |

No classification ever licenses a "complete system understanding"
claim — `completed` means the bounded investigation finished, not that
the system is understood. Precedence is deterministic: a policy
conflict dominates everything; a failed synthesis dominates exhaustion;
a recorded stop dominates a pending truncation.

**Budget-suited synthesis** — a per-mode output-budget profile; the
reasoning-heavy route reserves the larger share (4000 of 12000 plan
tokens; the standard route 800 of 8000) — the #290 live attempts'
mid-emission JSON truncations priced this. Unknown modes refuse
(fail closed). `validate_plan_synthesis` is a verdict, never a repair:
a `True` line number is an ERROR, not a coerced `1`; the failed
document rides back unmodified and routes to the `invalid_synthesis`
recovery.

**The carry-forward** (`carry_forward` / `verify_carry_forward` /
`brief_envelope_seam`) — the plan's facts/assumptions/questions with
their citations render into a delimited evidence section that rides the
plan text, and the seam is proven through the lane's OWN path: the A03
brief envelope freezes the plan bytes, the lane's
`extract_approved_sections` + `verify_brief_envelope` walk them back,
and the extracted plan bytes still carry the section — implementation
consumes the findings through the approved brief instead of re-paying
discovery.

## The offline qualification

```
env -u GITLAB_URL -u GITLAB_TOKEN -u GITLAB_WEBHOOK_SECRET \
  uv run python scripts/run_discovery_live.py --profile \
    --manifest evaluation/discovery_live/manifest-customer-v1.json \
    --out evaluation/discovery_live/customer-profile-run/
```

Deterministic (offline-scripted-model provenance, zero vendor spend),
byte-reproducible. The scripted investigation greps the target and the
decisive neighbors, pages the audit file (first page truncated → the
continuation read), deep-reads the billing window, deliberately
REPEATS the billing read (the cache hit), and declares done with an
honest summary naming the conflict. A separate sub-investigation spends
its whole 4-call budget paging `platform-docs`. Per-arm verdicts
(written to `profile-run.json`, printed by the command):

| arm | verdict |
| --- | --- |
| plan_present | PASS |
| decisive_line_depth_found (billing, line ≥ 200) | PASS |
| decisive_second_page_found (audit; NOT in page one) | PASS |
| second_page_continuation_used (offset ≥ 2000 read) | PASS |
| conflict_became_question (both lines + both values cited) | PASS |
| no_conflict_silent_pick (no shipping claim) | PASS |
| budget_sink_signalled (max_calls → exhausted_budget → surface_and_ask, findings retained, no completeness claim) | PASS |
| cache_reuse_on_repeat (hits ≥ 1, reader not re-paid) | PASS |
| no_cross_scope_leak (planning vs review scopes isolated) | PASS |
| restart_resumption (record-restored cache serves the hit) | PASS |
| budget_profile_reserved (reasoning-heavy 4000 > standard 800) | PASS |
| synthesis_validated (the emitted plan passes schema validation) | PASS |
| synthesis_invalid_routes_to_recovery (corrupted probe fails, routes to re-synthesis, raw kept unmodified) | PASS |
| carry_forward_populated (3 facts, 2 questions, verified in plan) | PASS |
| carry_forward_seam_verified (A03 envelope round-trip carries the section) | PASS |
| write_scope_single_target | PASS |

The run's terminal verdict is honestly `confused_policy` → the explicit
shipping question (the conflict is unresolved by design); the budget
sink's is `exhausted_budget` → surface and ask; the truncated first
page's ledger entry records `truncated_output` → a SPENT continuation.

## What is NOT claimed

- No live-model run over the scaled graph (partner-gated, R38-14); the
  scripted arm proves the machinery and the capture path, not model
  performance.
- The repositories are fixtures standing in for an authorized customer
  service graph — one task, no sample statistics.
- The grader is mechanical; a human code owner's usefulness evaluation
  (R38-11 acceptance 7) happens against the pre-agreed task, outside
  this artifact.
