"""Persist purpose-bound gateway keys and exact amendment targets."""

import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("eng_gateway_keys") as batch:
        batch.drop_constraint("uq_eng_key_dispatch", type_="unique")
        batch.add_column(
            sa.Column("purpose", sa.String(20), nullable=False, server_default="implementation")
        )
        batch.add_column(sa.Column("reference_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("token_digest", sa.String(64), nullable=True))
        batch.create_unique_constraint(
            "uq_eng_key_dispatch", ("run_id", "attempt", "generation", "purpose")
        )
    with op.batch_alter_table("eng_budget_amendments") as batch:
        batch.add_column(sa.Column("reservation_key", sa.String(300), nullable=True))
        batch.add_column(sa.Column("target_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("eng_gateway_keys", "eng_budget_amendments"):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError("engine budget history exists; downgrade would discard scope")
    with op.batch_alter_table("eng_budget_amendments") as batch:
        batch.drop_column("target_json")
        batch.drop_column("reservation_key")
    with op.batch_alter_table("eng_gateway_keys") as batch:
        batch.drop_constraint("uq_eng_key_dispatch", type_="unique")
        batch.create_unique_constraint("uq_eng_key_dispatch", ("run_id", "attempt", "generation"))
        batch.drop_column("token_digest")
        batch.drop_column("reference_json")
        batch.drop_column("purpose")
