# Existing Grok batch lane

Template: [grok.gitlab-ci.yml](../../ci/templates/grok.gitlab-ci.yml),
legacy driver `grok-build`. Its permission posture is broader than the new
confined contrib driver; it is not a first-class lane v1 guarantee.

`FORGE_GROK_AUTH` is the full subscription OAuth JSON bundle, stored as a
masked CI secret. Refresh tokens rotate; local login/refresh may require
updating the CI bundle. Never commit it or include it in a candidate.
Optional `FORGE_HARNESS_HTTPS_PROXY` addresses deployment-specific slow
streams. The lane gets no publisher token.

Preserve the template's explicit platform-binary installation matched to
the pinned npm wrapper. Optional dependency installation can otherwise
silently omit the binary and hang startup. Keep the version check and
bounded retry/turn/job limits. The current batch recipe uses unattended
approval and project trust; its mechanical deny rules and isolated CI
profile remain essential. This is not an instruction to enable those flags
in the new contrib integration.

The streaming event filter keeps final aggregate usage distinct from
per-response totals to avoid double-counting. Missing usage stays unknown.
Inspect auth rotation, binary availability, vendor MCP compatibility and
network reachability when a job stalls. Historical vendor observations
remain in the [archive](../archive.md); they do not qualify current binaries.
See [onboarding](onboarding.md).
