"""Append-only credential-redemption receipts + the canonical usage identity
(Q39-03 #322 / Q39-05 #324, review 6df4020).

Revision ID: 028
Revises: 027
Create Date: 2026-09-25 00:00:00.000000

Two durable-storage contracts, one revision (the files they change are
disjoint from the rest of the slice):

**Q39-03 (#322)** — ``credential_redemptions``: the append-only audit
ledger behind ``GET /lane/credentials/redeem``. The endpoint previously
read the WHOLE ``FlowRun.evidence`` document, appended the receipt and
wrote the whole JSON back (an unversioned full-document overwrite — the
P04 SQLite schedule: a concurrent native-handle/checkpoint write between
read and write was LOST) and kept only the last 50 entries (a bounded
projection used as the ONLY audit history). From 028 the TABLE is the
authority (INSERT-only, complete history, value-free) and the embedded
list becomes a versioned optimistic-CAS projection. No backfill runs in
the migration itself — ``forge.adaptive.credential_audit.
backfill_embedded_redemptions`` is the operator-invoked backfill
(``provenance='legacy-embedded'``, no invented grant ids) so the counts
and skips are REPORTED rather than buried in a chain step; the embedded
evidence stays readable throughout.

**Q39-05 (#324)** — ``usage_receipts`` gets ONE canonical durable
identity that finally matches the in-memory ingestion contract:

- the unique arbiter widens from ``(run_id, attempt_id, receipt_id)`` to
  ``(run_id, attempt_id, receipt_id, source_namespace)`` — the same
  label from two source namespaces stops collapsing into one row, and a
  FINAL receipt can now reconcile a stored PARTIAL instead of dying on
  the old ``ON CONFLICT DO NOTHING`` (the P02 probe: partial
  0.20/final=False stayed in the DB after the 1.20 final arrived);
- ``source_namespace`` backfills from the existing ``source`` column
  (legacy rows keep their R23 lane label as their namespace);
- ``final`` (default TRUE — every pre-028 row is a settled fact) lets
  ``DO UPDATE ... WHERE final IS NOT TRUE`` reconcile partial→final
  without ever letting a late partial downgrade a final;
- the completeness vocabulary widens with ``partial`` (a streamed
  partial is neither unknown nor aggregate — the old CHECK would have
  REJECTED the very rows the streaming contract accepts); SQLite cannot
  DROP a CHECK constraint in place, so the widen runs as a batched table
  rebuild there and as a plain constraint swap on PostgreSQL;
- ``identity_digest`` (stable sha256 over the full four-part identity)
  plus the economics columns (``cost_usd`` / ``cost_basis`` /
  ``rate_card_id`` / ``route_version`` / ``segment`` /
  ``artifact_digest``) become queryable columns beside ``raw``;
- ``usage_ingestion_conflicts`` preserves conflicting finals and
  attribution refusals explicitly — never silently merged, never
  dropped.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None

_WIDENED_COMPLETENESS = "completeness IN ('exact', 'aggregate', 'partial', 'unknown')"
_NARROW_COMPLETENESS = "completeness IN ('exact', 'aggregate', 'unknown')"


def _swap_completeness_constraint(narrow: bool) -> None:
    """Widen (or restore) the completeness CHECK, dialect-aware.

    SQLite has no ``ALTER TABLE DROP CONSTRAINT``: the swap runs as a
    batched table rebuild there (alembic reflects and recreates the
    table), while PostgreSQL takes the plain constraint drop/create.
    """
    expression = _NARROW_COMPLETENESS if narrow else _WIDENED_COMPLETENESS
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("usage_receipts") as batch:
            batch.drop_constraint("ck_usage_receipts_completeness", type_="check")
            batch.create_check_constraint("ck_usage_receipts_completeness", expression)
        return
    op.drop_constraint("ck_usage_receipts_completeness", "usage_receipts", type_="check")
    op.create_check_constraint("ck_usage_receipts_completeness", "usage_receipts", expression)


def upgrade() -> None:
    # -- Q39-03: the append-only redemption ledger ---------------------------
    op.create_table(
        "credential_redemptions",
        sa.Column("receipt_id", sa.String(length=64), primary_key=True),
        sa.Column("work_id", sa.String(length=32), nullable=False),
        sa.Column("grant_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("attempt_generation", sa.Integer(), nullable=True),
        sa.Column("route", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("credential_ref", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("resolver", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("subject", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("provider", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("binding_revision", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False, server_default="redeemed"),
        sa.Column("retry_of", sa.String(length=64), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("broker_receipt_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("resolved_version_kind", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("credential_policy", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("provenance", sa.String(length=30), nullable=False, server_default="live"),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["work_id"], ["flow_runs.id"]),
        sa.CheckConstraint("outcome IN ('redeemed')", name="ck_credential_redemptions_outcome"),
        sa.CheckConstraint(
            "provenance IN ('live', 'legacy-embedded')",
            name="ck_credential_redemptions_provenance",
        ),
    )
    op.create_index("ix_credential_redemptions_work_id", "credential_redemptions", ["work_id"])

    # -- Q39-05: the canonical usage identity --------------------------------
    op.add_column(
        "usage_receipts",
        sa.Column("source_namespace", sa.String(length=100), nullable=False, server_default=""),
    )
    op.add_column(
        "usage_receipts",
        sa.Column("identity_digest", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "usage_receipts",
        sa.Column("final", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("usage_receipts", sa.Column("cost_usd", sa.Float(), nullable=True))
    op.add_column("usage_receipts", sa.Column("cost_basis", sa.String(length=30), nullable=True))
    op.add_column("usage_receipts", sa.Column("rate_card_id", sa.String(length=64), nullable=True))
    op.add_column("usage_receipts", sa.Column("route_version", sa.String(length=64), nullable=True))
    op.add_column("usage_receipts", sa.Column("segment", sa.String(length=80), nullable=True))
    op.add_column(
        "usage_receipts", sa.Column("artifact_digest", sa.String(length=128), nullable=True)
    )
    # Legacy rows keep their R23 lane label as their namespace — the
    # identity of a settled row never changes semantics, it only gains the
    # namespace component it always logically had.
    op.get_bind().execute(
        sa.text("UPDATE usage_receipts SET source_namespace = COALESCE(source, '')")
    )
    # The widened completeness vocabulary: a streamed partial is its own
    # honest state (counters known, more calls may still arrive).
    _swap_completeness_constraint(narrow=False)
    # ONE canonical identity: the 3-column arbiter widens to carry the
    # source namespace. Dropping the old index first is what makes the
    # same-label-two-sources collapse impossible from now on.
    op.drop_index("uq_usage_receipt_identity", table_name="usage_receipts")
    op.create_index(
        "uq_usage_receipt_identity",
        "usage_receipts",
        ["run_id", "attempt_id", "receipt_id", "source_namespace"],
        unique=True,
    )

    # -- Q39-05: the explicit conflict/refusal diagnostic record -------------
    op.create_table(
        "usage_ingestion_conflicts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("receipt_id", sa.String(length=64), nullable=False),
        sa.Column("source_namespace", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("content_digest", sa.String(length=80), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_usage_ingestion_conflicts_run_id", "usage_ingestion_conflicts", ["run_id"])
    op.create_index(
        "uq_usage_ingestion_conflict_identity",
        "usage_ingestion_conflicts",
        [
            "run_id",
            "attempt_id",
            "receipt_id",
            "source_namespace",
            "kind",
            "content_digest",
        ],
        unique=True,
    )


def downgrade() -> None:
    # Honest downgrade (the 027 precedent): refuse while reverting would
    # DESTROY evidence. Collapsing the identity back to three columns
    # merges same-label rows from two source namespaces (silent spend
    # reconciliation loss); dropping ``final`` erases the partial/final
    # distinction the ledger already recorded; dropping the redemption
    # ledger erases append-only audit evidence. All guards run BEFORE any
    # change so a refusal leaves the schema untouched.
    bind = op.get_bind()
    collapsed = bind.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM usage_receipts "
            "GROUP BY run_id, attempt_id, receipt_id HAVING count(*) > 1) shared"
        )
    ).scalar_one()
    if collapsed:
        raise RuntimeError(
            f"usage_receipts holds {collapsed} (run, attempt, receipt) identit(y/ies) "
            "delivered by more than one source namespace — downgrading to the "
            "three-column identity would silently merge them; reconcile the "
            "sources before downgrading"
        )
    unresolved = bind.execute(
        sa.text("SELECT count(*) FROM usage_receipts WHERE final IS NOT TRUE")
    ).scalar_one()
    if unresolved:
        raise RuntimeError(
            f"usage_receipts holds {unresolved} partial receipt(s) — dropping the "
            "final column would erase the partial/final distinction; reconcile "
            "them to final (or accept the loss explicitly) before downgrading"
        )
    recorded = bind.execute(sa.text("SELECT count(*) FROM credential_redemptions")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"credential_redemptions holds {recorded} audit row(s) — the ledger is "
            "append-only evidence and is never dropped by a downgrade; archive "
            "them explicitly (and accept the audit loss) before downgrading"
        )
    op.drop_index("uq_usage_ingestion_conflict_identity", table_name="usage_ingestion_conflicts")
    op.drop_index("ix_usage_ingestion_conflicts_run_id", table_name="usage_ingestion_conflicts")
    op.drop_table("usage_ingestion_conflicts")
    op.drop_index("uq_usage_receipt_identity", table_name="usage_receipts")
    op.create_index(
        "uq_usage_receipt_identity",
        "usage_receipts",
        ["run_id", "attempt_id", "receipt_id"],
        unique=True,
    )
    _swap_completeness_constraint(narrow=True)
    op.drop_column("usage_receipts", "artifact_digest")
    op.drop_column("usage_receipts", "segment")
    op.drop_column("usage_receipts", "route_version")
    op.drop_column("usage_receipts", "rate_card_id")
    op.drop_column("usage_receipts", "cost_basis")
    op.drop_column("usage_receipts", "cost_usd")
    op.drop_column("usage_receipts", "final")
    op.drop_column("usage_receipts", "identity_digest")
    op.drop_column("usage_receipts", "source_namespace")
    op.drop_index("ix_credential_redemptions_work_id", table_name="credential_redemptions")
    op.drop_table("credential_redemptions")
