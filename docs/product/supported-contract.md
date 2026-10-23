# Product scope and support status

This source tree is unreleased. It does not declare a supported 1.0
composition, fleet SLA or external customer acceptance. The historical
0.x candidate contract remains at original path
`docs/product/supported-contract.md` in the [archive](../archive.md).

The current target is GitHub and GitLab: an authorized issue, a digest-bound
human approval, bounded CI implementation, one validated publisher and a
Draft whose independent verification names the current candidate SHA.
Merge stays human. Claude Code and Copilot CLI are the first-class targets;
additional drivers are experimental. Azure belongs to the maintenance line.
Deferred scope and binding safety rules are in
[ADR-0035](../adr/0035-simplification-program.md).

A release decision needs actual verification of its exact artifacts and
both provider workflows. A support or acceptance decision additionally
needs the named humans and deployment facts; a local pass, archived trace
or generated grade cannot substitute. See the
[support boundary](../operations/support-agreement.md),
[release flow](../operations/release.md) and
[implementation record](../roadmap/2026-09-29-simplification/IMPLEMENTATION-1.0.md).
