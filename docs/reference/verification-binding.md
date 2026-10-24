# Verification binds the current candidate

A lane report is execution evidence. Readiness requires separate native
verification of the exact published candidate. The
[check reducer](../../src/forge/verification/checks.py) accepts complete
native workflow/pipeline, run, job, attempt, SHA and observation-time facts.
Configured required check names must all pass. Optional green jobs, a lane's
own success, missing checks, stale results and unknown outcomes do not pass.

The [verification handler](../../src/forge/engine/verify.py) checks the
current owned native head and bounded deadline before and after provider
reads. Duplicate names from different native sources are ambiguous. A later
attempt supersedes an earlier attempt only within the same trusted source.
Only a positively classified code failure may spend a bounded repair
attempt; infrastructure, cancellation, timeout and unknown failure do not.

The [readiness transaction](../../src/forge/store/review_proof.py) rechecks
stored candidate, immutable approval, exact verification subject and native
posted review. A moved human head or a forged/stale record cannot become
`ready_for_human`. Historical evidence remains recorded without granting
fresh execution or publication authority. Multi-repository tested-world and
applicability machinery is retired.
