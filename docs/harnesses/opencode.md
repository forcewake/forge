# Existing OpenCode batch lane

Template: [opencode.gitlab-ci.yml](../../ci/templates/opencode.gitlab-ci.yml),
legacy driver `opencode`. This recipe differs from the experimental v2
server driver in `forge_lane.contrib`.

The current template configures its OpenAI-compatible provider with
`ZAI_API_KEY` and the dispatched `FORGE_HARNESS_MODEL`. Provision only that
route's masked key; the publisher token never enters the lane. The
renderer owns model substitution, MCP translation and the permission map.
Do not hand-edit a running job's map to bypass a refusal.

The existing batch recipe auto-approves operations outside its deny rules
and explicitly handles otherwise interactive permission defaults. It
therefore depends on the approved isolated CI execution profile; it is
not the new contrib driver's confined file-only policy. Commit and push
remain denied, and the checkout cannot push independently of those flags.

Canonical MCP HTTP entries become remote servers; stdio command/args
become local command arrays. Headers and secret references retain their
configured meaning. Invalid configuration refuses. Missing parseable usage
is `null`, never zero. Check the generated provider/model configuration,
permission map and pinned executable when a job hangs or fails on its first
turn. See [onboarding](onboarding.md) and [the archive](../archive.md) for
historical wire observations.
