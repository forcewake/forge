# Current lane credential consumption

The existing provider services retain their negotiated native or
runner-redemption routes until migration. The new lane v1 uses OIDC and
exact attempt gateway grants instead; there is no shared-secret fallback
between those protocols.

A value-free consumer receipt joins work/attempt generation, consumer job,
env-slot name, binding revision, policy and redemption/broker identities.
It appears in candidate metadata and the steering journal after staging,
including failure paths. `consumed` requires a completed driven turn;
otherwise the status remains `staged-unresolved`. Staging alone proves no
model usage. Never emit the secret value.

A presence stamp such as `env:NAME:present` is not a secret-version
fingerprint. Receipts distinguish `presence`, `fixture`, `binding-revision`
and actual `secret-version`. Rotation after dispatch never mutates the
launched attempt's staged snapshot; the next authorized dispatch resolves
the current revision. Rotation before dispatch must not silently substitute
a different authorized binding.

The JSON registry uses exclusive locks, reload-under-lock and atomic rename
for writes. Cross-worker reads observe changes within the configured stat
TTL; this prototype is not a cross-node audit-grade authority.
`FORGE_CREDENTIAL_POLICY` accepts `compat` or `strict-broker`; invalid values
refuse. With bindings present, strict mode refuses unbound/unknown routes;
compat labels ambient legacy fallback. An empty registry remains explicitly
ambient legacy even in strict mode; it does not prove project binding.
Missing connection or route never claims bound authority.

Use [rotation](credential-rotation.md) and [native locators](native-locators.md).
The original narrative remains in the [archive](../archive.md) at
`docs/operations/credential-consumption.md`.
