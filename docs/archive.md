# Historical evidence archive

The local branch `archive/evidence-0.42` preserves the historical files
removed from the active checkout. Its exact snapshot is
`7df3c46ddbf98d5967e976adce327403a34aed2a`.

This branch is local only and has not been published. No archive tag or
remote branch was created. A clone from GitHub cannot use this local ref
until the owner separately publishes or transfers it. This archive is a
historical record, not evidence that the current implementation passed a
hosted release or live acceptance check.

The original paths in that snapshot are:

- `qualification/`: lab inventories, qualification records and old fixture policy.
- `evaluation/`: recorded captures, reports and economic measurements.
- `docs/evaluation/`: evaluation narratives, receipts and associated captures.
- `docs/releases/evidence/`: historical release and promotion evidence.
- `docs/reviews/`: review reports and their dispositions.
- `docs/roadmap/2026-09-21-adaptive/`: the superseded adaptive design and examples.
- `demo/`: the old demo video, script and assembler.

All 508 archived tracked files (20,767,016 bytes) were checked against
this exact local ref and recovered through Git before deletion. Ignored
owner logs, private files and external backups are outside this archive;
none were deleted or moved. The current
[simplification program](roadmap/2026-09-29-simplification/ROADMAP.md)
remains in the active tree.

To inspect one original file locally, use
`git show archive/evidence-0.42:demo/SCRIPT.md`. To recover a directory,
use `git archive archive/evidence-0.42 demo/` and save/extract that stream
in a separate destination. These commands read the local archive and do
not publish it.

The six JSON examples still used by model-contract tests now live under
`tests/fixtures/adaptive_contracts/`. The historical budget calibration
measurement used by retained budget tests is
`tests/fixtures/budget-calibration-v1.json`. These are unchanged test
inputs; their presence does not claim new measured outcomes. The current
public-artifact gate owns its unchanged reviewed fixture allowlist at
`scripts/public-artifact-fixtures.json`. Database dump denial remains active.

## Documentation snapshot

Before the documentation diet, all 160 tracked documentation/README/changelog
files (2,575,212 bytes) were compared with their Git blobs and recovered
byte-for-byte from local branch `archive/docs-0.42-20261023-a80706e`, exact
commit `a80706e54674b5abb571f4b8481252a41273c0c7`. This branch is also local
only and has not been published. The [path/blob manifest](archive/documentation-0.42.json)
records every original path; it does not assert that every snapshot file
was removed. Retained runbooks were shortened after their original bytes
were verified. No ignored/private owner material was moved or deleted.

This snapshot preserves `docs/research/`, `docs/specs/`, old qualification,
drill and profile runbooks, old release-profile documentation, and the
pre-diet versions of current guides. Older evidence remains in the separate
snapshot above. The historical root changelog also remains unchanged as
[CHANGELOG-0.x.md](archive/CHANGELOG-0.x.md).

Read an original file with
`git show archive/docs-0.42-20261023-a80706e:docs/research/2026-09-21-codex-app-server.md`.
Recover into a separate destination with `git archive` rather than restoring
old runbooks over current files. A remote clone cannot use these local refs
until the owner separately publishes or transfers them. No hosted archive
URL is fabricated.

## Documentation measurements

The active-reader count includes every Markdown word under `docs/`,
including ADRs and the edge-case register, except the canonical
`docs/roadmap/2026-09-29-simplification/` execution records and the unchanged
`docs/archive/CHANGELOG-0.x.md`. Those explicit exclusions distinguish product
instructions from preserved program/history records. README length is
measured separately. `scripts/metrics.py` remains unchanged and counts all
Markdown under `docs/`, including both exclusions and any untracked plans.
Its all-docs total therefore remains above the 60K target; a smaller active
reader count alone does not close that measured Phase 1 exit criterion.
