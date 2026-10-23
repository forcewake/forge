# Coding lanes

Current provider services use the existing templates under `ci/templates/`
and the server-package lane entry points. Keep server, template and wheel
identity compatible; the lane never receives repository write credentials.
Start with [onboarding](onboarding.md).

| Existing recipe | Guide |
| --- | --- |
| Claude Code / Claude SDK lane | [Claude](claude-code.md) |
| Copilot batch lane | [Copilot](copilot-cli.md) |
| Grok batch lane | [Grok](grok-build.md) |
| OpenCode batch lane | [OpenCode](opencode.md) |
| GitLab .NET recipe | [dotnet-lane](dotnet-lane.md) |

The new independent [lane v1](lane-v1.md) has a separate typed driver
contract. Claude Code and Copilot CLI are first-class targets; contrib
integrations are experimental. Names and capability flags from the old
batch/SDK registry do not imply that a v1 driver is active.

Selection is bounded by trusted configuration and installed drivers.
Project configuration can tighten authority, never expand it. Missing
credentials, required resume or onboarding cannot trigger an implicit
alternative backend. Unknown usage stays unknown. Required independent
verification belongs to the target's CI, not a vendor completion event.
