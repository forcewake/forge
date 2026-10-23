# Existing GitLab .NET recipe

[dotnet-lane.gitlab-ci.yml](../../ci/templates/dotnet-lane.gitlab-ci.yml)
is the existing Linux GitLab recipe selected by `dotnet-lane`. It combines
the Claude agent with a reproducible .NET verification tail; it is not a
first-class new lane v1 driver or a GitHub recipe.

Before execution, commit an exact SDK `global.json` with
`rollForward: disable`, a `nuget.lock.json` beside every project, and tests
for the system's real migration, duplicate/out-of-order message and
commit/ack crash behavior. The template's image digest and SDK must agree.
Refresh them deliberately together. Use disposable database/broker test
services, never production credentials or deployment permission.

The lane checks the resolved SDK, runs the bounded Claude turn, then locked
restore, no-restore Release build and no-build tests with a TRX result.
Agent failure does not erase the verification tail or its audit evidence.
The lane is proposal-only: no repository write token or commit/push.
It uses the Claude gateway credential pair and optional explicitly
configured MCP servers; see [Claude](claude-code.md).

Candidate metadata retains `dotnet-trx/1` verification with separate
restore/build/test exits, raw TRX SHA-256, outcome and observed counters.
Unknown counters are omitted, not zero-filled. Restore/build failure is
infrastructure, not evidence of a failed code test. The parsed digest-bound
block travels inside candidate metadata; the raw TRX is not an additional
candidate contract file. The existing recipe selects one `forge.trx` and
does not promise aggregation of bespoke per-project report names.

The database/broker topology and meaningful assertions are the target
repository's responsibility. A fresh-empty-database pass does not prove a
legacy migration, and the recipe does not invent those tests. Independent
provider CI must still verify the published candidate SHA.
Historical examples and image observations remain at original path
`docs/harnesses/dotnet-lane.md` in the [archive](../archive.md).
