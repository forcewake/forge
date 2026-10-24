# Dogfooding the current factory

The intended loop is issue → `/implement` → exact plan → authorized `/go`
→ CI lane → collected candidate → Draft PR/MR → independent verification
and read-only review → human merge decision. This describes the product
workflow, not a claim that hosted dogfooding has been enabled for this tree.

Onboard through the current [GitHub guide](../getting-started/github.md) or
[GitLab guide](../getting-started/gitlab.md). A target repository installs the
[dispatch wrapper](../../templates/github/forge-lane.yml), which calls the
[reusable lane](../../.github/workflows/lane.yml) at an explicitly reviewed
immutable release reference. GitLab uses the
[current OIDC job](../../ci/lane.gitlab-ci.yml). The lane receives no native
repository write credential. There is no old harness workflow or automatic
runtime fallback.

Configure native actors, required independent jobs, budgets and gateway
artifact identity explicitly. Keep the complete task in the issue; the
human approval binds its exact frozen implementation plan and policy.
Changing source or scope needs a new authorized decision. Bot notes are
never commands. Reactive human-push review remains read-only; the bot never
merges. Historical debug, security-triage and extra-driver routes do not
activate in 1.0.
