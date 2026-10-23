# Connect a GitLab project

Current provider services still use the existing GitLab CI lane. The new
[lane v1](../harnesses/lane-v1.md) is a separately composed migration route.
Use a disposable target for the first workflow and obtain the project
owner's authority before changing tokens, hooks or CI.

## 1. Approve the execution profile

Use an ephemeral unprivileged Docker executor, never a shell runner,
Docker socket, production secrets or broad internal-network access.
Permit only required GitLab, package-registry and model endpoints.
Protect the target branch: humans may merge, the bot may neither merge nor
push there. Forge writes only its reserved factory branch.

## 2. Configure identity and webhook

Keep the bot's project-scoped `FORGE_BOT_TOKEN` in the control plane only;
configure the required GitLab API permissions and trusted `FORGE_APPROVERS`.
An optional lane `FORGE_BOT_READ_TOKEN` is read-only. Never stage the bot's
write token or forge's admin/database credentials in project CI.

Enable GitLab in forge, set its base URL and `GITLAB_WEBHOOK_SECRET`, and
configure the project's webhook to the deployment's `/webhook` endpoint.
Use HTTPS outside a deliberately isolated local environment. Enable Notes,
Issues, Merge request, Pipeline and Job events as required by the ingress;
[GitLab documents their event semantics](https://docs.gitlab.com/user/project/integrations/webhook_events/).
LAN delivery also depends on the administrator's allowed-local-request
policy. A webhook secret authenticates transport; it never grants actor
approval authority.

## 3. Install the matching lane

Include or copy the reviewed [GitLab template](../../ci/templates/) for the
selected driver. Pin a reviewed immutable published ref or exact template;
a moving `main` include is not a reproducible deployment. The compiled
configuration must contain the selected lane job. Normal pipelines must
skip implementation when no forge dispatch identity is present.

Provision only that driver's masked model/subscription credential, optional
read-only clone token, and explicitly configured MCP secrets. A protected
variable invisible on factory refs fails preflight; do not work around it
with an ambient duplicate. Use the [credential runbooks](../operations/credential-rotation.md).
`FORGE_REQUIRED_JOBS` names actual independent jobs that must exist and
succeed on the candidate SHA. A green pipeline without those jobs is not
verification. Admission refuses missing CI before paid planning.

## 4. Verify and operate

Run `forge doctor --project <id>`. Then comment `/implement`, review the
posted plan and approve `/go <run-id>`. The lane returns a candidate to the
trusted publisher; independent CI and readonly review precede readiness.
Inspect the exact Draft/head/check evidence and leave merging to a human.
[Operator commands](../operations/operator-commands.md) cover cancellation,
reconciliation, retries and GitLab review feedback.

Required resume needs an SDK lane and the exact verified checkpoint before
any model turn; batch templates refuse it. Steering needs explicit control
configuration and cannot widen approval. See [harness onboarding](../harnesses/onboarding.md)
and [runtime recovery](../operations/adaptive-runbook.md).
