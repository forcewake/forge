"""Provider-namespaced run identity (R03): rebuild the one-active-run index.

Revision ID: 012
Revises: 011
Create Date: 2026-09-17 00:00:00.000000

Numeric subject ids are only unique WITHIN a provider: a GitLab
``project_id=5`` and a GitHub repository internal id ``5`` are unrelated
subjects, yet the old partial unique index ``uq_active_run_per_issue`` over
``(project_id, issue_iid)`` treated them as one — a GitLab run could occupy
a GitHub repo's issue slot (and vice versa) and a bulk scan had no provider
guard. This migration:

- backfills ``flow_runs.provider`` for rows whose subject identity and
  provider column disagree (see the derivation rules below), then
- rebuilds ``uq_active_run_per_issue`` over ``(provider, project_id,
  issue_iid)`` — same name, same partial ``WHERE`` over the non-terminal
  statuses — so one active run per subject is enforced PER PROVIDER.

``provider`` itself is already ``NOT NULL`` with ``server_default='gitlab'``
(migration 008): legacy GitLab rows never carried NULL, so no NOT NULL/ALTER
work is needed here — only the targeted backfill and the index rebuild.

Provider derivation rules (safest-first, applied only where they change
something):

1. ``provider='azure_devops'`` rows are NEVER touched. The Azure service
   stamps the column at insert time (ADR-0024); note an AzDO row MAY carry
   ``github_repo_full_name`` as its legacy repo-identity fallback, so "has a
   repo full name" alone must not imply GitHub.
2. ``provider='gitlab'`` (the 008 default) AND ``github_repo_full_name IS
   NOT NULL`` → ``'github'``: the GitHub subject identity only exists on the
   GitHub lane (E3a), so a row carrying one while still holding the default
   lane value is a GitHub run whose provider column was missed. In a healthy
   chain-ordered database this matches zero rows — the app has stamped
   ``provider='github'`` since 008 — the update is defense-in-depth.
3. Every remaining row keeps ``'gitlab'`` (forge started GitLab-only; rows
   with no GitHub/Azure identity are GitLab runs, including pre-008 legacy).

The drop+create of the index runs inside the migration's single transaction
(alembic default on Postgres), so the constraint is never absent for readers
that would have violated it mid-migration: rows that coexist under the new
key but collided under the old one exist only because the old index was
wrong. The downgrade restores the pre-012 index shape; provider values are
left as upgraded (they are data, not schema).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None

#: Terminal statuses as a literal list in the predicate — unchanged by this
#: migration, kept byte-identical to the model's ``_TERMINAL_STATUS_PREDICATE``
#: (forge.durable.models) so create_all and the migration chain agree.
_TERMINAL_STATUS_PREDICATE = sa.text(
    "status NOT IN ('ready_for_human', 'blocked', 'failed', 'cancelled')"
)


def upgrade() -> None:
    # Backfill (rules 1-3 above): rule 1 is a no-op by construction — the
    # WHERE clause below never matches provider='azure_devops' rows.
    op.execute(
        "UPDATE flow_runs SET provider = 'github' "
        "WHERE provider = 'gitlab' AND github_repo_full_name IS NOT NULL"
    )

    # Rebuild the partial unique index over the namespaced key. Same name as
    # the model's Index — one definition across the two schema factories.
    op.drop_index("uq_active_run_per_issue", table_name="flow_runs")
    op.create_index(
        "uq_active_run_per_issue",
        "flow_runs",
        ["provider", "project_id", "issue_iid"],
        unique=True,
        postgresql_where=_TERMINAL_STATUS_PREDICATE,
        sqlite_where=_TERMINAL_STATUS_PREDICATE,
    )


def downgrade() -> None:
    # Restore the pre-012 (provider-blind) index shape. Provider VALUES stay
    # as upgraded — derivation is not reversible (a 'gitlab' row could have
    # been GitLab or a missed GitHub row), and the app has stamped providers
    # since 008 anyway.
    op.drop_index("uq_active_run_per_issue", table_name="flow_runs")
    op.create_index(
        "uq_active_run_per_issue",
        "flow_runs",
        ["project_id", "issue_iid"],
        unique=True,
        postgresql_where=_TERMINAL_STATUS_PREDICATE,
    )
