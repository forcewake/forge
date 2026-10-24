# Connect a GitLab project

Follow the [cold install procedure](../onboarding/cold-install-runbook.md).
Use a disposable project for the first workflow. Obtain the owner's authority
before changing tokens, hooks, runners or CI.

## 1. Approve the execution profile

Use an ephemeral unprivileged runner with no Docker socket or production
credentials. Protect the target branch; humans merge and the bot cannot bypass
protection. The coding lane holds no repository write credential.

## 2. Configure identity and webhook

Declare an explicit GitLab connection with numeric project, project-bot and
approver IDs. Keep separate read and write project credentials on the server.
Use a strong configured webhook secret and the HTTPS endpoint
`/hooks/<connection-key>`. Enable the Notes, Issues, Merge request, Pipeline and
Job events consumed by the native adapter. Transport authentication does not
grant the sender approval authority. An unconfigured connection refuses events.

## 3. Install the matching lane

Install the pinned [lane v1](../harnesses/lane-v1.md) CI configuration. Bind the
configured native issuer and JWKS URL, immutable lane requirements and selected
driver. Declare actual required independent job names. Successful unrelated
jobs, another pipeline, missing jobs and stale candidate SHAs cannot establish
verification. Configure the reviewed prospective budget guard before planning.
The lane redeems only its current authenticated grant; old variable carriers
and ambient duplicate credentials are unsupported.

## 4. Verify and operate

Run `forge --config forge.toml doctor` and
`forge --config forge.toml onboard <repository-key>`. Start with `/implement`,
inspect the posted plan and approve `/go <run-id>`. The publisher validates
candidate bytes before native Git publication. Independent CI and closing
review precede readiness; the bot never merges.

[Operator commands](../operations/operator-commands.md) describe corrections,
exact checkpoint resume, cancellation and budget amendments. Preserve an
existing installation with the [offline schema procedure](../operations/schema-cutover.md)
before adopting this local source candidate.
