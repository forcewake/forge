# Proposed contract fixtures

These JSON objects and schemas describe the recommended design, not existing Forge configuration or public APIs. The numbers in the example budget are **illustrative test values**, not sizing recommendations or measured costs.

Six example documents have been validated against `adaptive-contracts.schema.json`; example digest references and write/read-set inclusion were also checked. Runtime invariants (authorization, revision compare-and-swap, effect ordering, path canonicalization, semantic impact and artifact provenance) must be enforced by application code, not inferred from JSON Schema validation.

ControlCommand is a **server-authenticated stored envelope**, not a request that allows a caller to set `actor_ref`. Runner credentials must not access human decision endpoints. The open Q1 in the example PlanRevision is a blocking question; this proposed plan must not become executable merely because its JSON is valid.

CandidateSet includes unchanged baseline members deliberately. Its existence is not a passed verification result and not permission to merge or deploy.
