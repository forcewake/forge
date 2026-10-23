# Bounded review rounds

On the current GitLab feedback surface, an authorized `/fix` on a ready
Draft MR can admit a new correction round. GitHub parity remains migration
work; do not infer it from the common command parser. Enable the capability
explicitly and follow [operator commands](operator-commands.md).

The original `ready_for_human` delivery stays terminal and immutable.
A round creates an independent child run with its own base, generation,
admission and budget. `review_rounds` links it to the lineage root; every
delivery shares the persisted collaboration target's repository, source
branch, target branch and one MR. Display prefixes never derive authority.
Earlier candidates and green checks become historical, not current proof.

## Admission and refusals

Admission checks MR state, one outstanding round, exact current head and
the configured round limit. Closed/merged MR returns `mr_closed`; an open
round `conflicting_correction`; moved head `stale_head`; exhausted bound
`round_limit`. Every refusal preserves human edits and creates no commit.
`FORGE_MAX_REVIEW_ROUNDS` defaults to one correction and is bounded at ten;
zero disables post-readiness rounds. Concurrent requests contend on the
unique open-round constraint, not a process-local flag.

One transaction records the round/child, digest-identical frozen parent
spec, child reservation on the recorded target, own budget, accepted request
and active-plan seed, and outbox event. All persisted provider/repository/
branch/MR handles must agree first; `collaboration.target_mismatch` refuses
rather than picking one. A refused legacy topology is never guessed.
The correction seed combines the verified approved input and accepted
request, preserving provenance; a durable active revision chains from that
revision instead. No paid re-plan is implied by round admission.

After commit, the request records `round_admitted`, the MR gets an
idempotent reply, and ordinary head-fenced dispatch continues. The current
head becomes the approved base; a later move refuses publication. The
child's closing reserve is partitioned from its frozen ceilings; prior
amendments do not silently fund another round. Its originating discussion
must be human-resolved before readiness.

## Restart and operator actions

The reconciler scans open rounds in bounded passes. An interrupted admitted
round re-drives idempotently, adopting journaled effects before creating
another. A terminal child closes its round (`completed` for readiness,
`ended` otherwise). One note identity can admit at most one round;
redelivery never opens the next one. Another independent note after the
round settles can request the next bounded correction.

Use `/status round N of <root-short>` for read-only display. Mutating
commands should use the full child run ID printed in the reply; ambiguous
prefixes refuse. `/cancel <full-child-id>` stops that child without
reopening the original delivery or replacing the MR. After a stale head,
raise a new correction against the actual current head. After a closed MR,
start a new `/implement` issue. Raising the round bound is an explicit
operator policy decision, not an automatic retry.

The bot never merges or resolves discussions. Candidate role, verification
binding, human acceptance and command progress remain distinct in
[operator diagnostics](operator-view.md). Historical narrative remains at
`docs/operations/review-rounds.md` in the [archive](../archive.md).
