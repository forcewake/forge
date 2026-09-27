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

Branch continuity is the PERSISTED collaboration target (R41-04,
#359): the `collaboration_targets` row a lineage's root admission
mints (repository, the immutable SOURCE branch, the target branch, the
lineage's ONE MR identity) and every delivery of the lineage links
(`flow_runs.target_id` — root and round children the SAME row). The
child run id is INDEPENDENT of the parent's; every branch-consuming
leg (implementer rebind, publisher, CI polling, drift checks, harness
dispatch, operator surface) resolves through the target row — never a
re-derivation from the run id (the pre-#359 prefix reuse coupled work
identity, collaboration identity and display shorthand, and made short
command ids ambiguous within a lineage; the architecture boundary test
catches any new `factory_branch` shortcut). Two repositories sharing an
MR number and a branch label never cross: the target keys on
(provider, repository, source branch).

### Round references (display, never authority)

`round N of <root-short>` — e.g. `round 2 of a1b2c3d4` — is the
concise human reference for one round (round 1 is the original
delivery; N ≥ 2 the recorded round row's child). `/status` accepts it:

- `@forge /status round 2 of a1b2c3d4` — the read-only snapshot of
  that round's child. The reference resolves the LINEAGE by the root
  prefix (every prefix match must agree on one lineage root — legacy
  lineages whose children share the root's 8-hex prefix collapse to
  the same root, two distinct lineages colliding on a prefix refuse)
  and then the recorded round row.
- The reference is NOT an authorization token: `/cancel` and `/retry`
  still require the full 32-hex run id, exactly as forge prints it in
  the round-admitted reply (`/cancel <full child id>`). A display
  label or a round number can never redirect publication — publication
  keys on the target's recorded source branch and the run ids, never
  on a label.
- Commands printed by forge resolve to the intended round: the
  round-admitted reply carries the reference for reads and the FULL
  child id for the destructive command. With legacy shared 8-hex
  prefixes a bare `/status <prefix>` / `/cancel <prefix>` deterministically
  refuses (matches more than one run — nothing is adopted); with
  modern independent ids the 8-char prefixes are unique again and
  resolve directly.

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

1. the round row (`admitted`) and the child run — an INDEPENDENT run id
   linked to the lineage's `collaboration_targets` row — walked over
   the legal graph to `proposing` (no paid call — the round's plan is
   derived, not re-planned). Before any write, the pre-write handle
   guard compares every recorded handle (run MR, round MR, the provider
   MR document's source branch, the repository identity) against the
   target row; a disagreement is the `collaboration.target_mismatch`
   outbox event and a refusal with ZERO writes;
2. the parent's frozen RunSpec copied VERBATIM (same document, same
   digest — the child's digest checks recompute over the same bytes);
3. a confirmed `mr_reservations` row for the child on the TARGET's
   recorded source branch;
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
| follow a round's progress | `@forge /status round N of <root-short>` (the reference printed in the round-admitted reply; read-only, never an authorization token) |
| pre-#359 lineage: the 8-char prefix matches several runs | use the full run id, or the round reference for reads — a shared prefix deterministically refuses, it never adopts one match |
| recorded branch/MR handles disagree (target mismatch) | the round is refused with zero writes and a `collaboration.target_mismatch` outbox event; an operator reconciles the recorded topology — forge never picks one handle |
| legacy topology unresolvable | the adapter records a REFUSED target row with the typed reason (never an inferred branch); resolve the recorded evidence, then re-raise |
| more rounds needed than the policy allows | raise `FORGE_MAX_REVIEW_ROUNDS` explicitly (bounded at 10); the exhaustion reply names the current bound |
| MR merged/closed with feedback outstanding | expected: `mr_closed`, zero commits — the audit keeps the request; raise a new `/implement` for follow-up work |
