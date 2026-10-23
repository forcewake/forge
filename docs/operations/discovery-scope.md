# Read-many/write-one scope

A run has exactly one writable repository. Explicit neighbor declarations
may authorize reads; discovering a name, URL or dependency never grants
access. Project configuration can tighten authority, not expand it.

The retained [system context contract](../../src/forge/adaptive/system_context.py)
validates the exact neighbor set, provider/repository identities, immutable
refs and allowed path windows. Duplicate or unknown entries refuse.
Reader bindings must cover that declared set exactly; unavailable or
ambiguous readers require a question, never a fabricated complete plan.
Neighbor source remains read-only. Wider write scope needs a separate
explicit approval; it is never inferred from a multi-repository plan.

The removed discovery-authority/report helpers and full-system epic claims
do not certify a current multi-repository execution route. The current
product scope is still one validated publication target. Preserve source
OIDs and label bounded/truncated context honestly wherever discovery is
composed. Historical observability names and offline cohort narratives
remain at `docs/operations/discovery-scope.md` in the [archive](../archive.md).
