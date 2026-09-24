# Discovery scope and authority (read-many/write-one)

How forge keeps a multi-repository discovery HONEST: it may READ several
authorized repositories, and it may WRITE exactly one — the run's own.
The declarative side is the
[`SystemContextProfile`](../../src/forge/adaptive/system_context.py)
(parsed from the project's `forge.yml` `neighbors:` section — explicit
authorization only); the LIVE side is
[`discovery_authority`](../../src/forge/adaptive/discovery_authority.py)
(R36-11 / issue #270). This runbook describes what operators see and
must act on.

## Who may be read, and how readers are built

- The `neighbors:` section of `forge.yml` authorizes each neighbor:
  `provider` (`gitlab` / `github` / `azure`), `repository_id`, an
  immutable `ref` (pin a commit sha — `HEAD` floats), and optional
  `allowed_globs` (empty = the whole repository). Unknown fields or
  providers refuse at parse time; nothing imported or discovered
  contributes authorization.
- Neighbor readers are resolved from **connection identities** — the
  provider family + base URL + numeric connection id + the repository's
  numeric id on that connection (`resolve_readers`). A display name, a
  filename, or a URL an agent supplies is never identity. Two
  same-named repositories on different hosts/connections stay distinct
  bindings; a name that matches SEVERAL connections refuses as
  `ambiguous` (pin one in the connection catalog) instead of guessing.

## The refusals an operator must answer

| Signal | Meaning | Required action |
|--------|---------|-----------------|
| `plan.blocking_questions` (reader `unavailable` / `ambiguous`) | An authorized neighbor has no reader — the planning input carries the question; the run does NOT produce a confident complete plan over the unread source | Fix the connection catalog entry or remove the neighbor from `forge.yml` |
| `DiscoveryAuthorizationRefusal` (`outside_authorized_set`) | A tool/model asked for a repository outside the approved set — the typed refusal fires before any read; that repository contributes no content and receives no requests | None (the boundary held); widen `neighbors:` only through an explicit config change |
| `write_scope.expansion_requests` | A proposal wanted to WRITE to a read-only neighbor — refused and SURFACED as a material scope-change request; `publication_targets` still names ONLY the approved target | Approve or reject the expansion explicitly; it is never applied implicitly |

## Observability keys

- `discovery.authorized_repo_set_digest` — sha256 over the frozen
  authorization (own repo identity + path scope, every neighbor's
  identity + pinned ref + globs). Recorded by
  `AuthorizedRepoSet.as_document()` before anything is read; it moves
  when the authorization moves, not when reads do.
- `discovery.truncation` — the explicit truncation marker
  (`truncation_document`): which repositories and windows were cut by
  per-repository read limits (`BoundedNeighborReader`), rendered into
  the planning input as a bounded section naming the partial windows.
- `plan.blocking_questions` / `write_scope.expansion_requests` — see the
  table above.

## When evidence goes stale

Discovery evidence binds to the immutable OIDs it read. If a referenced
neighbor snapshot moves (a different OID, a re-pointed repository, a
neighbor added or removed), `evaluate_snapshot_invalidation` returns an
explicit `identity_changed` decision naming the moved neighbors: the
recorded discovery/plan evidence is invalidated and must be re-derived
through a new discovery run — it is never silently reused. The check is
`identity_changed`-style over `neighbor_set_digest` and reads the
per-repository OIDs a real discovery record already persists in
`dispatch.repositories`.

## The offline capture seed

`evaluation/research_cohort/recorded/RC-08-offline-neighbor-deadline-policy/`
is a REAL captured research run (actual `ToolObservation` records from
`run_research_pass` over `SnapshotToolbox` repos built from
`snapshots/snap-offline-neighbor.json`) where the decisive dependency
lives only in the neighbor repository, at a non-initial file window. It
is labeled `capture.provenance = offline-scripted-model`
(`live_provider: false`) and seeds the live cohort for issue #271; the
capture is deterministic and re-verified by
`tests/test_discovery_authority.py`.
