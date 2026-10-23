# Native credential locators

Existing native delivery routes map a credential ref to
`FORGE_MODEL_<LOCATOR>`. The locator has a bounded uppercase ASCII prefix
and twelve hex characters derived from SHA-256 of the complete UTF-8 ref.
Punctuation, case and Unicode affect the digest; a readable prefix is
cosmetic. Refs with no representable identifier refuse. Allocation still
checks collisions and names both conflicting refs; a truncated hash is not
a mathematical guarantee that collisions cannot occur.

The persisted locator map records namespace, route, ref, locator/carrier,
allocation time and any legacy carrier/retirement time. Writes lock and
rename atomically. Namespaces bind provider profile and canonical subject;
case-insensitive comparison prevents provider-name aliases. Shared group
or organization namespaces require an explicit operator choice.
Idempotent allocation preserves its original timestamp.

## Migrate an existing carrier

1. Inventory and `adopt_legacy` each ref without renaming a live secret.
2. Resolve every lossy legacy collision explicitly; no first-match choice.
3. Provision the new locator carrier with the intended value while keeping
   the old one available for the controlled transition.
4. Verify the actual installed template consumes the new carrier, using
   its value-free receipt for the exact dispatched attempt.
5. Retire the legacy claim in the map, then remove its provider-side secret
   after consumption is verified. Preserve the mapping's audit history.

A restart keeps mappings, timestamps and unresolved collision refusals.
Without the locator registry, the legacy spelling remains explicit; a
library update never silently renames a CI secret.

Delivery checks the selected driver's template and its installed digest.
Unknown drivers, mismatched template identity or missing route guards
refuse before model execution. A local shipped template is not evidence
about another installed template. The old standalone conformance gate is
removed; current template/credential tests retain runtime behavior.

See [rotation](credential-rotation.md), [consumer receipts](credential-consumption.md)
and the historical original `docs/operations/native-locators.md` in
[the archive](../archive.md).
