"""Bind approval consumption to its atomic parent step and close prepared gates."""

import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("eng_gates") as batch:
        batch.alter_column("state", existing_type=sa.String(20), server_default="prepared")
        batch.add_column(sa.Column("consumed_step_id", sa.String(32), nullable=True))
        batch.create_foreign_key(
            "fk_eng_gate_consuming_step", "eng_steps", ["consumed_step_id"], ["id"]
        )
        batch.drop_constraint("ck_eng_gate_state", type_="check")
        batch.create_check_constraint(
            "ck_eng_gate_state", "state IN ('prepared','open','consumed','expired','refused')"
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM eng_gates WHERE state = 'prepared'")):
        raise RuntimeError("prepared gates must be resolved before downgrade")
    with op.batch_alter_table("eng_gates") as batch:
        batch.drop_constraint("fk_eng_gate_consuming_step", type_="foreignkey")
        batch.drop_column("consumed_step_id")
        batch.alter_column("state", existing_type=sa.String(20), server_default="open")
        batch.drop_constraint("ck_eng_gate_state", type_="check")
        batch.create_check_constraint(
            "ck_eng_gate_state", "state IN ('open','consumed','expired','refused')"
        )
