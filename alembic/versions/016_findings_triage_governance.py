"""Findings triage governance + scan completeness (R25/R26).

Revision ID: 016
Revises: 015
Create Date: 2026-09-17 00:00:00.000000

Two governance holes close with this migration:

R25 — the AI triage verdict is a SUGGESTION, never a status write:

- ``security_findings.suggested_verdict`` / ``suggested_at`` /
  ``suggested_by`` record what the security-triage agent proposed; the
  authoritative ``status`` moves only on an authorized confirmation (or
  the explicit ``FORGE_SECURITY_AUTO_ACCEPT`` opt-in, default OFF).
- ``security_findings.version`` (``INTEGER NOT NULL DEFAULT 0``) is the
  optimistic-concurrency counter: verdict write-back pins the version
  observed at triage start and applies as compare-and-set, so a manual
  status change during the LLM call can never be overwritten.
- ``security_findings.absent_in_last_scan`` feeds the reappearance rule:
  a suppressed finding that reappears after being absent gets a fresh
  evidence assessment instead of a silently restored suppression.
- ``security_finding_actions`` journals every suggestion, confirmation,
  rejection, reopen and remote dismissal (intent first, outcome second —
  the ADR-0005 shape for the findings surface).
- the dedupe key gains ``connection_id`` as its leading column
  (``uq_finding_per_connection_source_scope_fingerprint``): two
  connections carrying the same project id never mix findings (R26),
  mirroring ``uq_active_run_per_issue``'s provider-led key.

R26 — a partial scan must never look clean:

- ``security_scan_executions`` records every ingestion pass per surface
  with its ``completeness`` — a 403 surface, an expired artifact or a
  pipeline without report jobs lands as ``incomplete``, visible forever.

Conservative shape, mirroring 012–015:

- columns added on ``security_findings`` are ``NOT NULL`` with server
  defaults matching the model (so ``create_all`` and the chain agree) or
  plain NULLable — pre-016 rows keep working unchanged: every existing
  finding joins the DEFAULT connection (``''`` = the deployment's single
  GitLab connection, exactly what the pre-connection key meant) at
  version 0 with no pending suggestion;
- the old three-column unique index is replaced by the connection-led
  one — the swap is safe because (connection, source, scope, fingerprint)
  is a superset identity of the old key (no data can collide that did not
  collide before), and no backfill decision exists to make;
- no CHECK on the ALTERed table (Postgres-only ``ADD CONSTRAINT``; the
  closed sets are enforced by the model for ``create_all`` databases and
  by the application layer everywhere);
- the two new tables carry their CHECKs at creation like every other
  durable table; the downgrade drops them and restores the old key.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "security_findings",
        sa.Column("connection_id", sa.String(length=100), nullable=False, server_default=""),
    )
    op.add_column(
        "security_findings",
        sa.Column("suggested_verdict", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "security_findings",
        sa.Column("suggested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "security_findings",
        sa.Column("suggested_by", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "security_findings",
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "security_findings",
        sa.Column("absent_in_last_scan", sa.Boolean(), nullable=False, server_default="false"),
    )

    # The dedupe key becomes connection-led (R26). The swap cannot create
    # a collision that the narrower key did not already prevent.
    op.drop_index("uq_finding_per_source_scope_fingerprint", table_name="security_findings")
    op.create_index(
        "uq_finding_per_connection_source_scope_fingerprint",
        "security_findings",
        ["connection_id", "source", "scope", "fingerprint"],
        unique=True,
    )
    op.create_index(
        "ix_security_findings_connection_scope",
        "security_findings",
        ["connection_id", "scope"],
    )

    op.create_table(
        "security_scan_executions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("connection_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=80), nullable=True),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("sha", sa.String(length=40), nullable=True),
        sa.Column("scanner_version", sa.String(length=80), nullable=True),
        sa.Column("ingest_version", sa.String(length=10), nullable=False, server_default="1"),
        sa.Column("completeness", sa.String(length=20), nullable=False, server_default="complete"),
        sa.Column("parsed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=32),
            sa.ForeignKey("flow_runs.id"),
            nullable=True,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "completeness IN ('complete', 'incomplete', 'partial')",
            name="ck_scan_executions_completeness",
        ),
    )
    op.create_index(
        "ix_scan_executions_scope_observed",
        "security_scan_executions",
        ["connection_id", "scope", "observed_at"],
    )
    op.create_index(
        op.f("ix_security_scan_executions_run_id"),
        "security_scan_executions",
        ["run_id"],
    )

    op.create_table(
        "security_finding_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "finding_id",
            sa.String(length=32),
            sa.ForeignKey("security_findings.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("before_status", sa.String(length=20), nullable=True),
        sa.Column("after_status", sa.String(length=20), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False, server_default="applied"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('suggest_verdict', 'confirm_verdict', 'reject_verdict', "
            "'reopen', 'remote_dismiss')",
            name="ck_finding_actions_action",
        ),
        sa.CheckConstraint(
            "outcome IN ('applied', 'requested', 'succeeded', 'failed', 'skipped')",
            name="ck_finding_actions_outcome",
        ),
    )
    op.create_index(
        "ix_finding_actions_finding",
        "security_finding_actions",
        ["finding_id", "created_at"],
    )


def downgrade() -> None:
    # The journal and the scan ledger are audit/observability state —
    # dropping them loses no finding row (status stays authoritative).
    op.drop_index("ix_finding_actions_finding", table_name="security_finding_actions")
    op.drop_table("security_finding_actions")

    op.drop_index(op.f("ix_security_scan_executions_run_id"), table_name="security_scan_executions")
    op.drop_index("ix_scan_executions_scope_observed", table_name="security_scan_executions")
    op.drop_table("security_scan_executions")

    op.drop_index("ix_security_findings_connection_scope", table_name="security_findings")
    op.drop_index(
        "uq_finding_per_connection_source_scope_fingerprint", table_name="security_findings"
    )
    op.create_index(
        "uq_finding_per_source_scope_fingerprint",
        "security_findings",
        ["source", "scope", "fingerprint"],
        unique=True,
    )

    op.drop_column("security_findings", "absent_in_last_scan")
    op.drop_column("security_findings", "version")
    op.drop_column("security_findings", "suggested_by")
    op.drop_column("security_findings", "suggested_at")
    op.drop_column("security_findings", "suggested_verdict")
    op.drop_column("security_findings", "connection_id")
