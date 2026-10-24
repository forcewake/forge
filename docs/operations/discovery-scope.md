# Read-many, write-one scope

Each run has one writable native repository. Explicit neighbors authorize
read-only discovery; a discovered name, URL or dependency grants nothing.
[Closed configuration](../../src/forge/runtime/settings.py) names allowed
neighbors and path windows. [Runtime routing](../../src/forge/runtime/routing.py)
resolves each through its configured connection and freezes its exact source
SHA before planning. The planner receives bounded repository reads, not
publication credentials.

[Citations](../../src/forge/domain/plan.py) bind the native provider,
connection, repository ID, source SHA, path and line range. The final frozen
specification includes the complete approved plan and execution policy.
Moving those facts changes the approval digest. Unavailable reads never
produce an invented complete plan.

Multiwriter packages, discovery cohorts and aggregate tested-world digests
are outside 1.0. Source repositories remain read-only references. Only the
validated current candidate can reach the sole publisher.
