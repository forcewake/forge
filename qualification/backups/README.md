# Private backup storage — index (R38-03 / #304)

R38-03 (issue #304) removed the operational database backups from the public
evidence surface. The bytes live in the maintainer's access-controlled
PRIVATE storage; this file is the committed index of what that storage
contains — a receipt catalog. It carries NO bytes, NO credentials, and NO
absolute personal paths.

## Storage class

- Location: the `FORGE_PRIVATE_BACKUP_DIR` base (default
  `~/forge-private/backups` relative to the maintainer's home), date-keyed
  subdirectories per alignment session, directory mode 700 / files 600,
  OUTSIDE any git repository. `scripts/align_lab.py` writes there by default
  and refuses to write a backup inside the repository.
- Access: the maintainer only. Nothing in this storage is ever pushed,
  packaged, exported or attached to a review bundle.
- Retention: kept until the corresponding lab state is superseded by a
  verified newer backup AND the alignment qualification it supports is
  closed; then deletable on the maintainer's explicit decision.
- Restore verification: each artifact below was `pg_restore`d into a
  disposable database on 2026-09-24 as part of classification (outcome in
  the table); any future artifact must pass the same restore test before it
  is relied on for rollback.

## Artifact index (2026-09-24 alignment session)

Reference ids are opaque; the public sanitized receipts
(`docs/evaluation/2026-09-24-live-single-writer/backups/README.md`) cite
them. sha256 is the storage-integrity digest.

| Private reference | Filename (date-keyed dir `2026-09-24/`) | sha256 | Classification | Restore test |
| --- | --- | --- | --- | --- |
| forge-private-2026-09-24-01 | pre-r3708-alignment-20260924T114520Z.dump | b384e96a5b1d7e925ea83740dc1527e23fc95090e51085199871d3662230b0eb | sensitive_content (credential scan negative) | ok |
| forge-private-2026-09-24-02 | pre-r3708-alignment-20260924T114800Z.dump | 79427305093be85c2dd7ba7f026985769d1b0b3130ba2270db20147474841f00 | sensitive_content (credential scan negative) | ok |
| forge-private-2026-09-24-03 | pre-r3708-alignment-20260924T115921Z.dump | ec013688384d1d3f60227fdfb5190d559bce4447feda2aaeb3a6f85eaa9970a1 | sensitive_content (credential scan negative) | ok |
| forge-private-2026-09-24-04 | pre-r3708-alignment-20260924T120548Z.dump | 89e5afe79b646633658c462e8c599fa84e6c8d86b242bad7d473ab3c3395ae2b | sensitive_content (credential scan negative) | ok |
| forge-private-2026-09-24-05 | pre-r3708-alignment-20260924T122429Z.dump | 8a5cc200466010098e898e522aae43b490860016453f8506adf673983ba57a96 | sensitive_content (credential scan negative) | ok |
| forge-private-2026-09-24-06 | pre-r3708-alignment-20260924T125753Z.dump | fbc59541131f1b62e6f368a5d75109d691ab9f40b912338f66f5c6dc3db7c3d1 | sensitive_content (credential scan negative) | ok |

Classification owner: the maintainer (Pavel Nasovich / forcewake), 2026-09-24.
Classification basis per artifact: full inventory + targeted sensitive-content
scan of every text-bearing column after `pg_restore` into a disposable
database (see the public receipts README for the method and the per-dump
table inventory). No credential-shaped value was found in any of the six;
the `sensitive_content` verdict reflects internal operational text (webhook
issue/comment bodies, `run_specs` plan/task text) and the maintainer's own
personal email address.

## History-cleanup plan (DRY RUN — nothing executed)

Because the classification confirmed `sensitive_content`, a dry-run history
cleanup plan was written next to the private bytes (`HISTORY-CLEANUP-PLAN.md`
in the same private date-keyed directory): the affected refs and paths, the
`git filter-repo` command that WOULD remove the dumps from history, and the
retraction caveat (rewriting does not retract existing clones/forks). It is
NOT executed — execution requires the maintainer's explicit authorization.
The public summary line: six dump paths remain in git history on the default
branch; credential rotation is NOT indicated (no token-shaped value found in
any dump); the pending decision is limited to whether the internal-text
exposure justifies the rewrite.

## Gate

`scripts/gate_public_artifacts.py` (CI-wired) refuses any tracked
database-archive type outside the reviewed synthetic-fixture allowlist
(`qualification/fixtures-allowlist.json`) — a regression that re-commits a
real dump to a publishable path fails the build.
