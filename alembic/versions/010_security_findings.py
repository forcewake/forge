"""Security findings store: forge-owned triage state (v0.7, ADR-0021 §2).

Revision ID: 010
Revises: 009
Create Date: 2026-09-14 00:00:00.000000

- ``security_findings``: one deduplicated finding per
  (``source``, ``scope``, ``fingerprint``) — the unique index IS the
  dedupe key. GitLab fingerprints are forge-computed sha256 over
  ``category`` + primary ``identifiers[].value`` + location hash
  (research §4.1: CE gets no stable scanner id and no Ultimate
  fingerprint column); GitHub fingerprints are the alert number per repo.
  Triage status lives here (CE has no vulnerabilities API; GitHub state
  is mirrored, never authoritative for forge's verdicts).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "security_findings",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=32),
            sa.ForeignKey("flow_runs.id"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("sha", sa.String(length=40), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False, server_default="info"),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("identifiers", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("triage_note", sa.Text(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'triaged', 'fixed', 'false_positive', 'dismissed')",
            name="ck_security_findings_status",
        ),
    )
    op.create_index(
        "uq_finding_per_source_scope_fingerprint",
        "security_findings",
        ["source", "scope", "fingerprint"],
        unique=True,
    )
    op.create_index(
        "ix_security_findings_scope_status",
        "security_findings",
        ["scope", "status"],
    )
    op.create_index("ix_security_findings_provider", "security_findings", ["provider"])
    op.create_index("ix_security_findings_run_id", "security_findings", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_security_findings_run_id", table_name="security_findings")
    op.drop_index("ix_security_findings_provider", table_name="security_findings")
    op.drop_index("ix_security_findings_scope_status", table_name="security_findings")
    op.drop_index("uq_finding_per_source_scope_fingerprint", table_name="security_findings")
    op.drop_table("security_findings")
