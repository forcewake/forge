# Pre-alignment database backups — sanitized receipts (R38-03 / #304)

This directory previously carried six raw PostgreSQL custom-format dumps
(`pre-r3708-alignment-*.dump`, 200–222 KB each) taken by `scripts/align_lab.py`
before each mutation of the live lab during the R37-08 (#289) qualification.
Committing unclassified operational database bytes to a publishable docs tree
was the confidentiality defect of external review item R38-03: the dumps carry
the lab's real operational state, not an allowlisted test receipt.

As of 2026-09-24 (issue #304) the binaries are REMOVED from the repository.
What remains here is the sanitized receipt per artifact — everything a
reviewer needs to verify the backup existed, was restorable, and what it was
classified to contain — WITHOUT distributing any database row. The bytes
live in the maintainer's access-controlled private storage; see
`qualification/backups/README.md` for the private-storage index (also a
receipt catalog, no bytes).

## Receipts

The machine-readable block below is the receipt catalog. Every entry carries
`sha256` + `classification` + `private_reference` (validated by
`scripts/gate_public_artifacts.py` and `tests/test_gate_public_artifacts.py`).

```json
[
  {
    "filename": "pre-r3708-alignment-20260924T114520Z.dump",
    "created_utc": "2026-09-24T11:45:20Z",
    "size_bytes": 204769,
    "sha256": "b384e96a5b1d7e925ea83740dc1527e23fc95090e51085199871d3662230b0eb",
    "format": "postgresql-custom-dump (PGDMP magic, v1.16)",
    "schema_version": "alembic-026",
    "restore_test": "ok (pg_restore exit 0, disposable database)",
    "classification": "sensitive_content",
    "classification_basis": "credential scan negative; internal operational text present",
    "private_reference": "forge-private-2026-09-24-01",
    "alignment_receipt": "r3708-0c76a2e9ab46 (run stopped-at-recreate-forge-app)"
  },
  {
    "filename": "pre-r3708-alignment-20260924T114800Z.dump",
    "created_utc": "2026-09-24T11:48:00Z",
    "size_bytes": 205305,
    "sha256": "79427305093be85c2dd7ba7f026985769d1b0b3130ba2270db20147474841f00",
    "format": "postgresql-custom-dump (PGDMP magic, v1.16)",
    "schema_version": "alembic-027",
    "restore_test": "ok (pg_restore exit 0, disposable database)",
    "classification": "sensitive_content",
    "classification_basis": "credential scan negative; internal operational text present",
    "private_reference": "forge-private-2026-09-24-02",
    "alignment_receipt": "r3708-b5671fcb3b7f (run refused after backup)"
  },
  {
    "filename": "pre-r3708-alignment-20260924T115921Z.dump",
    "created_utc": "2026-09-24T11:59:21Z",
    "size_bytes": 205305,
    "sha256": "ec013688384d1d3f60227fdfb5190d559bce4447feda2aaeb3a6f85eaa9970a1",
    "format": "postgresql-custom-dump (PGDMP magic, v1.16)",
    "schema_version": "alembic-027",
    "restore_test": "ok (pg_restore exit 0, disposable database)",
    "classification": "sensitive_content",
    "classification_basis": "credential scan negative; internal operational text present",
    "private_reference": "forge-private-2026-09-24-03",
    "alignment_receipt": "r3708-6561cad5149f (run aligned)"
  },
  {
    "filename": "pre-r3708-alignment-20260924T120548Z.dump",
    "created_utc": "2026-09-24T12:05:48Z",
    "size_bytes": 205745,
    "sha256": "89e5afe79b646633658c462e8c599fa84e6c8d86b242bad7d473ab3c3395ae2b",
    "format": "postgresql-custom-dump (PGDMP magic, v1.16)",
    "schema_version": "alembic-027",
    "restore_test": "ok (pg_restore exit 0, disposable database)",
    "classification": "sensitive_content",
    "classification_basis": "credential scan negative; internal operational text present",
    "private_reference": "forge-private-2026-09-24-04",
    "alignment_receipt": "r3708-d30bbc7dc159 (run aligned)"
  },
  {
    "filename": "pre-r3708-alignment-20260924T122429Z.dump",
    "created_utc": "2026-09-24T12:24:29Z",
    "size_bytes": 212540,
    "sha256": "8a5cc200466010098e898e522aae43b490860016453f8506adf673983ba57a96",
    "format": "postgresql-custom-dump (PGDMP magic, v1.16)",
    "schema_version": "alembic-027",
    "restore_test": "ok (pg_restore exit 0, disposable database)",
    "classification": "sensitive_content",
    "classification_basis": "credential scan negative; internal operational text present",
    "private_reference": "forge-private-2026-09-24-05",
    "alignment_receipt": "r3708-98d23543f75d (run aligned)"
  },
  {
    "filename": "pre-r3708-alignment-20260924T125753Z.dump",
    "created_utc": "2026-09-24T12:57:53Z",
    "size_bytes": 221969,
    "sha256": "fbc59541131f1b62e6f368a5d75109d691ab9f40b912338f66f5c6dc3db7c3d1",
    "format": "postgresql-custom-dump (PGDMP magic, v1.16)",
    "schema_version": "alembic-027",
    "restore_test": "ok (pg_restore exit 0, disposable database)",
    "classification": "sensitive_content",
    "classification_basis": "credential scan negative; internal operational text present",
    "private_reference": "forge-private-2026-09-24-06",
    "alignment_receipt": "r3708-8ebbd2497fe3 (run aligned)"
  }
]
```

## How each dump was classified (2026-09-24, the maintainer's lab)

Every dump was restored with `pg_restore` into a DISPOSABLE database on the
lab's Postgres container (never the live `forge` database), inventoried, and
scanned, then the disposable database was dropped. The per-dump inventory:

| Dump (UTC stamp) | Alembic | Restore | Populated tables (non-zero rows) |
| --- | --- | --- | --- |
| 114520Z | 026 | ok | 6 tables: event_inbox=103, control_command_deliveries=10, flow_runs=1, control_commands=1, checkpoint_metadata=1, outbox=4 |
| 114800Z | 027 | ok | same 6 tables as 114520Z |
| 115921Z | 027 | ok | same 6 tables as 114520Z |
| 120548Z | 027 | ok | 7 tables: the 6 above (event_inbox=104) + step_runs=1 |
| 122429Z | 027 | ok | 15 tables: + action_log=3, budget_reservations=1, execution_leases=1, gate_approvals=1, llm_calls=1, run_budgets=1, run_specs=1, step_runs=5 (event_inbox=107, flow_runs=2, outbox=10) |
| 125753Z | 027 | ok | 18 tables: the fullest state — + mr_reservations=1, pause_fences=1, publication_intents=1 (action_log=15, control_commands=2, event_inbox=113, execution_leases=3, flow_runs=4, gate_approvals=3, llm_calls=5, outbox=27, run_budgets=3, run_specs=3, step_runs=13, budget_reservations=5) |

Targeted sensitive-content scan over every text-bearing column of every
populated table (regex classes: API-key shapes `sk-…`, `Bearer …`, JWT
shapes, GitHub/GitLab/Slack PAT shapes, URL-embedded credentials,
keyword=value secret pairs, 32+ hex and 40+ base64 runs, emails):

- **Credentials: NONE found in any of the six dumps.** Zero standalone
  `sk-` keys (26 substring hits per dump were the word "ta**sk-evidence**"),
  zero Bearer/JWT/PAT shapes, zero URL-embedded credentials, zero
  keyword=value secret pairs. Long hex/base64 runs are git SHAs (40) and
  sha256 digests / run ids (32/64) — opaque identifiers, not secrets.
  **No credential rotation is indicated.**
- **Personal identifiers: the maintainer's OWN personal email address**
  (21 occurrences per dump, inside webhook payloads), the maintainer's
  GitHub noreply address (38), and system addresses (`git@`, `noreply@`,
  `support@github.com`). No third-party personal identifiers found.
- **Internal operational text (why the verdict is `sensitive_content`, not
  `clean_of_credentials`):** full webhook event bodies — issue/comment text
  (~1.4 MB in the fullest dump; `run_command` control notes, issue bodies),
  `run_specs.document` with `plan`/`task`/`subject` prompt-shaped text,
  model routes and project config, flow-run evidence bundles. This is
  exactly the operational state R38-03 says must not ship in a public
  evidence surface — hence removal, even though no secret was found.

Verdict semantics: `clean_of_credentials` = scan negative AND no operational
text; `sensitive_content` = scan-negative credentials but internal/personal
content present (the case for all six); `unknown` = not decodable — blocking.
Because `sensitive_content` was confirmed, a DRY-RUN history-cleanup plan
(affected refs listed, nothing rewritten, no command executed) was written
next to the private bytes — see `qualification/backups/README.md`. The dumps
remain in git HISTORY until the maintainer explicitly authorizes the rewrite;
deleting from HEAD does not retract existing clones/forks.

## Where the bytes are

Private, access-controlled storage under the maintainer's
`FORGE_PRIVATE_BACKUP_DIR` base (mode-700 directory tree outside this
repository, date-keyed subdirectory `2026-09-24/`), referenced from here only
by the opaque `private_reference` ids above. The public index of that
storage — still no bytes, no absolute personal paths — is
`qualification/backups/README.md`.

Future alignment runs CANNOT recreate this defect: `scripts/align_lab.py`
now defaults its backup destination to the private base and REFUSES to write
a backup inside the repository, and `scripts/gate_public_artifacts.py`
(wired into CI) fails any tracked database-archive type outside the reviewed
synthetic-fixture allowlist (`qualification/fixtures-allowlist.json`).
