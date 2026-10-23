# Existing Copilot lane

The batch recipe is
[copilot.gitlab-ci.yml](../../ci/templates/copilot.gitlab-ci.yml), driver
`copilot`. The new lane v1 `copilot_cli` driver is a separate ACP integration.

Provision the explicit `COPILOT_GITHUB_TOKEN` for a Copilot subscription.
Do not use the control-plane publisher token as model authentication.
The selected CLI needs the vendor's Copilot permission/token type; a
classic repository PAT is not an interchangeable substitute.
`COPILOT_MODEL` is the batch recipe's model selection variable.

The rendered recipe owns scoped grants and commit/push denials; deny rules
win over allow rules. Do not replace them with allow-all. Session grants
must not become persistent approvals. MCP servers need explicit configured
grants as well as translated configuration. The checkout push URL is
forbidden independently of model instructions.

Subscription-side usage is opaque to forge: missing receipts are unknown,
never zero cost. Wall-clock and turn limits still apply. Check enterprise
policies, token type, CLI pin and rendered grants when startup or tool calls
refuse. No commits from the coding process is expected; the working-tree
candidate goes to the trusted publisher. See [onboarding](onboarding.md).
