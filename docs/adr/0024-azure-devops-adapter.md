# ADR-0024: Azure DevOps source and execution adapter

Status: accepted on 2026-09-15; superseded on main by
[ADR-0035](0035-simplification-program.md). The decision remains historical
context for the [0.42 maintenance line](../operations/maintenance-0.42.md).
Its complete original is preserved in the verified
[documentation archive](../archive.md), at this same path.

The original adapter made Azure DevOps a peer of GitHub and GitLab over the
same durable approval, candidate, publication and verification rules.
It used a typed asynchronous REST client, dedicated least-privilege PAT
identity, authenticated HTTPS service hooks and Azure Pipelines. Provider
payload shape alone never authenticated an event. Durable command acceptance
preceded acknowledgement, and bot-authored events never admitted paid work.

Publication used Azure's native expected-parent comparison. Stale heads
refused instead of force-overwriting human work. The CI lane checked out the
frozen source, retained no repository write credential and returned a candidate
artifact. The trusted server alone published it. Draft PR creation never
authorized completion or merge. Verification correlated independent native
builds with the exact candidate source version; Azure Repos branch policies
owned PR build validation.

Connection-scoped credentials, approvers, bot identity and event authentication
prevented authority from leaking between organizations. Typed execution
handles, native recovery and conservative unknown outcomes preserved the
shared lifecycle. Readiness required real provider verification using an
owner-supplied organization; unit tests alone did not establish parity.

Azure is outside the 1.0 runtime. Its clients, routing, configuration and
pipeline compatibility are not installed with the new server. Returning Azure
support requires a new explicit adapter task and qualification against the
current ports, rather than restoring its old service fork.
