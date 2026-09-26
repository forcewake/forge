# Review rounds: the bounded correction after `ready_for_human` (R40-02, #338)

How a reviewer's AFTER-readiness `/fix` becomes a new candidate on the
same Draft MR — without reopening the terminal delivery, without
force-pushing human edits, and without an unbounded correction ladder.
Module: `forge.runs.service` (the admission + the reconciler pass),
`forge.adaptive.revisions` (the classic-run adapter and the round's
active-plan seed), storage: the `review_rounds` table (migration `031`);
tests: `tests/test_review_rounds.py` (the AT-02 spine),
`tests/test_review_feedback.py`, `tests/test_adaptive_revisions.py`.

## The model

```
Delivery 1 — immutable, ready.
    ↓ human requests a change (/fix on the MR, an authorized approver)
Review round 2: current MR head + request + scope + budget + approval
    ↓
New candidate, new checks, new review.
```

`ready_for_human` stays terminal (ADR-0004 — no outgoing edge, the
human decision is final). The round is a **linked work unit**, not a
reopening: a NEW child `flow_runs` row with its own admission, its own
numerical budget, its own base head and its own execution generation,
related to the ready delivery through `review_rounds`
(`parent_run_id` → `child_run_id`, `root_run_id` naming delivery 1 for
the whole lineage). Supersession is a relationship between deliveries —
the earlier verdict, verification and candidate list are never edited;
they stay visible as historical evidence.

Branch continuity is by construction: the child run id SHARES the
parent's 8-hex prefix, so every branch-deriving leg (implementer,
publisher, CI polling, drift checks) lands on the SAME factory branch
and the SAME Draft MR — the MR stays the collaboration surface. Within
a lineage only one run is ever non-terminal at a time; command short
forms (`/go`, `/cancel`) that resolve by 8-hex prefix are therefore
ambiguous across the lineage — use the full run id (rounds are
machine-driven and never sit at the `/go` gate).

## The eligibility ladder (every rung refuses with a reply and ZERO commits)

| rung | refusal (request status) | meaning |
| --- | --- | --- |
| MR state (read first) | `mr_closed` | a merged/closed MR ended the human decision — the collaboration surface is gone; raise a new `/implement` instead |
| one outstanding round (checked before the head) | `conflicting_correction` | the lineage already holds an open round; the partial unique index `uq_review_round_open_per_root` is the arbiter two racing /fix notes collapse onto |
| head fence | `stale_head` | the MR head moved between the note and the admission — a human edit the authorization never saw; preserved, never force-pushed; re-raise against the new head |
| round bound | `round_limit` | `FORGE_MAX_REVIEW_ROUNDS` exhausted for the lineage |

The round bound is an operator policy (`FORGE_MAX_REVIEW_ROUNDS`,
default `1` — delivery 1 plus one correction round; `0` restores the
pre-#338 behavior where every post-readiness `/fix` answered
`correction_window_closed`; values clamp into `[0, 10]`, a typo narrows
never widens). A round's budget is its OWN admission: the child's
`run_budgets` row opens from the SAME frozen spec ceilings (closing
share partitioned up front, `closing-partition/1`) — an amendment never
silently funds a round.

## The admission (one transaction, or nothing)

1. the round row (`admitted`) and the child run, walked over the legal
   graph to `proposing` (no paid call — the round's plan is derived, not
   re-planned);
2. the parent's frozen RunSpec copied VERBATIM (same document, same
   digest — the child's digest checks recompute over the same bytes);
3. a confirmed `mr_reservations` row for the child on the SAME branch;
4. the child's own budget opened from that spec;
5. the child's evidence seeded with the round's ACTIVE-plan document and
   its own copy of the request (so the child's readiness gate holds the
   candidate until the reviewer resolves the originating discussion);
6. the `review_round.admitted` outbox row.

The plan derivation is the **verified classic-run adapter**
(`classic_spec_revision`): for an ordinary run that never staged a
`PlanRevision` (no `active_plan` pointer), the initial approved input is
derived from EXACTLY two durable sources — the digest-verified frozen
spec and the accepted request — one step carrying the frozen plan
objective, the correction folded in through the existing
`review_correction_revision` (steps byte-identical, the correction
riding the summary with its provenance). A parent that DID stage a
revision chains from its durable content instead (revision N → N+1,
`revised_from_digest` recording the supersession). The seed document
(`round_active_plan_seed`) is what `resolve_approved_input` reads at
every dispatch entry — the #321 join, so the executor is briefed from
the correction + the CURRENT head, never an obsolete source version.

After the commit: the parent's request is marked `round_admitted`
(durable-first), the MR gets one operator reply naming the round, and
the dispatch leg runs — the same advance the repair edge uses, reading
and materializing at the APPROVED current MR head (a human-added
nonconflicting file is in the snapshot) and pinning `expected_head` to
it (a later move is the existing `branch_drift` refusal; the publication
intent's CAS adopts or refuses, never replays against the old base).

## The reconciler pass (`evaluate_review_rounds`)

One bounded scan per tick over the OPEN round rows:

- a round whose child run reached a terminal status is CLOSED
  (`completed` for `ready_for_human`, `ended` otherwise) — freeing the
  lineage's one outstanding-correction slot so the next independent
  correction can be requested;
- a round still `admitted` (or whose child sits mid-advance after a
  crash) is re-driven — head-fenced first, then the advance leg, which
  is mid-leg idempotent. At most ONE child round and ONE publication
  effect intent ever survive a restart: the round row is the
  admission's idempotency and the advance adopts any journaled effect
  before creating a new one.

## Replay and chain discipline

- one reviewer comment is one request: the note id is the idempotency
  key (`uq_review_round_request` on `(parent_run_id, note_id)`), so a
  redelivered webhook records nothing new and replays round two's note
  can never create round three;
- after round two completes (its child `ready_for_human`), a SECOND,
  independent note opens round three linked to round two's delivery —
  the lineage root stays delivery 1, and the bound counts ROUNDS, not
  deliveries;
- forge never merges and never resolves the discussion: the reviewer's
  resolve is the human decision that gates the new candidate's
  readiness, and the final summary names the resolved and still-open
  threads with the tested candidate.

## Observability

- outbox: `review_round.admitted`, `review_round.status` (payload:
  run/parent/root ids, round number, note, decision, base head,
  requester, status, reason);
- the child's evidence carries `review_round` (the linkage mirror) and
  `round_invalidation` (which of the parent's evidence items the round
  supersedes — the old green checks stay readable as history, only the
  new candidate's checks can call it verified);
- the parent's request record carries the linkage (`round_admitted`,
  the decision id, the invalidation partition).

## Operator surface

| situation | what to do |
| --- | --- |
| reviewer asks for a fix after readiness | `/fix <edit> \`<path>\`` on the Draft MR (an approver; scope must be provable — otherwise it records as a material proposal) |
| head moved between the request and the round | nothing to clean: the branch keeps the human commit; re-raise the `/fix` against the new head |
| round must be stopped | `@forge /cancel <full child run id>` — delivery 1 and the MR stay untouched; the pass closes the round row as `ended` |
| more rounds needed than the policy allows | raise `FORGE_MAX_REVIEW_ROUNDS` explicitly (bounded at 10); the exhaustion reply names the current bound |
| MR merged/closed with feedback outstanding | expected: `mr_closed`, zero commits — the audit keeps the request; raise a new `/implement` for follow-up work |
