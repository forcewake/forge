# ADR-0032: One versioned execution contract

Status: accepted on 2026-09-24; amended by
[ADR-0035](0035-simplification-program.md). The complete original, including
its historical compatibility window and console-entry decisions, is verified
at this path in the [documentation archive](../archive.md).

The execution envelope owns the identity, driver, model, approved policy,
checkpoint and artifact contract consumed by a lane. Construct it from durable
resolved inputs once. Repeated delivery must reconstruct identical authority.
Templates and vendor environment variables cannot re-derive or override a
pin. Unknown versions, unknown fields, malformed digests, unsupported
compositions and missing required authority refuse before provider dispatch.
A capability matrix must describe actual support without fabricated parity.

This closes the original defect where batch and SDK templates selected ambient
models that differed from the model the gate approved. One visible release
version must also identify one concrete composition, with working installed
entry points and all required package assets. Import success from a checkout
does not prove an installed wheel works.

Under ADR-0035 the concrete 1.0 contracts are `forge.run-spec/2` and
`forge.lane/1`, shared through `forge-protocol`. Brief requires complete
canonical specification bytes. SHA-256, plan, base, revision and the six-field
ApprovedInput must agree. Absent specifications, old numeric schemas and
empty model pins are refused; there is no vendor-default or old-template
fallback. The server checks current run, attempt, generation and cancellation
authority before dispatch and credential delivery.

OIDC binds the native repository, configured workflow or pipeline, first
native job and one-use nonce/JTI receipts. Scoped gateway grants replace the
old credential-redemption carrier. Neither a grant reference nor a restored
checkpoint can supply missing approval. A required resume names one exact
checkpoint; corruption or unavailability cannot select the newest one.

The installed `forge` command directly owns install, doctor, onboard, serve
and worker. `forge-schema` owns explicit schema initialization, read-only
checks and offline history export/cutover. Retired dispatcher aliases and the
old migration chain are excluded from the server wheel. Unknown schema
versions require an explicit migration procedure, never a runtime interpreter
that manufactures compatibility or converts old lifecycle authority.
