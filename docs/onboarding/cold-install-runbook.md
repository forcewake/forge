# Install and verify a source checkout

This is the development installation route for the unreleased source tree.
It replaces the historical frozen-profile kit; that kit remains at original
path `docs/onboarding/cold-install-runbook.md` in the
[documentation archive](../archive.md). No second-operator install or
current live-provider acceptance is claimed.

Use Python 3.13+ and uv. From a clean checkout, run
`uv sync --all-packages --extra postgres` to install the server, protocol,
lane and development dependencies. PostgreSQL support is needed when
`DATABASE_URL` selects PostgreSQL. A local SQLite configuration is suitable
for development, not evidence of a PostgreSQL deployment.

Copy `.env.example` to a private `.env`, supply only the services and
provider credentials you own, and choose the relevant
[GitHub](../getting-started/github.md) or [GitLab](../getting-started/gitlab.md)
onboarding guide. Keep the publisher credential on the control plane;
the CI lane receives no repository write token. The builtin implementer
is unavailable, so the target's existing lane must be configured before
admission. The new lane v1 route is documented separately and is not an
automatic migration switch.

Run `uv run python -m forge.doctor --json`; exit zero means its configured
environment checks passed. Missing PostgreSQL, Redis or model-gateway
reachability is a real setup gap; never replace it with a claimed success.
For PostgreSQL, migrate with `uv run python -m forge.migrate` before starting
app and worker from the same pinned source or image. Do not migrate an
existing lab as an installation experiment.

Run the [default tests and guardrails](../reference/testing.md) for local
source validation. A successful install or fake test is distinct from a
live issue → approved plan → candidate → independent CI → Draft workflow.
Use a disposable authorized project for that first workflow and leave its
Draft for human review. [Upgrade](../operations/upgrade.md) and
[backup/restore](../operations/backup-restore.md) govern existing deployments.
