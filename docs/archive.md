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
