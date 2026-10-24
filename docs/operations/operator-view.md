# Read-only operator API

`GET /api/v1/runs?limit=50&offset=0` lists scoped durable runs.
`GET /api/v1/runs/<run-id>` returns that run and its persisted evidence.
These reads perform no lifecycle transitions, model calls or provider writes.

## Scope and authentication

Use an opaque bearer whose environment name and native repository grants are
declared in server configuration. Provider, exact connection and numeric native
repository ID define authority; a display name grants nothing. Foreign and
unknown run IDs return the same 404 response. Operator credentials cannot
authenticate the lane. Unauthenticated requests refuse, and client-supplied
scope, old cursors, unknown query fields and duplicate fields are invalid.

## Bounds and honest coverage

List limits range from 1 to 100; offsets range from 0 to 10000. Detail exposes
bounded persisted candidates, budget pools, amendment states and effect journal
outcomes. Credential-shaped values are redacted on list, detail and native
status replies. Reads preserve source rows.

Approval, execution, checkpoint activation, publication and readiness are
different facts. Unknown spend is held liability. A committed historical
verification does not establish that the current candidate is green. SQL
records alone do not prove native acceptance or current blob availability.
Treat absent evidence as unobserved; refresh and use an explicitly authorized
command to act. A read or advisory hint grants no execution authority.

## Recovery

Use [operator commands](operator-commands.md) for guarded reconciliation,
cancellation, correction and exact checkpoint resume. Uncertain effects require
probing the original journal identity. Budget exhaustion needs an explicit
amendment of its limiting axis. Human commits and unresolved discussions remain
under human control. See [closing budget](closing-budget.md) and
[schema cutover](schema-cutover.md) for their separate boundaries.
