# Architecture and migration status

The control plane receives authenticated GitHub or GitLab events, records
commands durably, freezes a plan and consumes a digest-bound human gate.
A CI lane executes the approved brief without repository write credentials.
The trusted publisher validates the returned candidate and current head,
journals each external effect, and opens or updates one Draft per run.
The project's independent CI verifies the candidate SHA before closing
review and readiness. Human edits and unresolved effect outcomes fence
publication. Cancellation revokes authority before stopping execution.

The repository now has three workspace distributions:

- `forge`: control plane, durable state, provider clients and publisher.
- `forge-protocol`: versioned, validated wire models without server or lane imports.
- `forge-lane`: CI runner, workspace/checkpoint handling and driver adapters; it never imports the server.

The current GitHub and GitLab services still use the existing lane entry
points. The new `/lane/v1` API and independent runner are being integrated;
their presence does not mean all provider dispatches use them. Lane v1
uses CI OIDC, exact attempt binding, durable replay protection and short
server sessions. Legacy lane credentials remain isolated until their
callers migrate. New model-key minting needs the separately composed
model gateway; an absent provider refuses rather than using ambient keys.

The next architectural step is one lifecycle engine over provider ports
with intent-journaled effects. Read the binding
[design](roadmap/2026-09-29-simplification/DESIGN.md),
[ADRs](adr/0000-record-architecture-decisions.md) and
[current implementation record](roadmap/2026-09-29-simplification/IMPLEMENTATION-1.0.md).
Historical topology and qualification narratives are recoverable through
[the archive](archive.md).
