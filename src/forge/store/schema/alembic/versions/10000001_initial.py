"""Install the final single-engine schema into an explicitly empty database."""

from alembic import op
from sqlalchemy import inspect

from forge.store.schema import baseline

revision = baseline.REVISION
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if set(inspect(connection).get_table_names()) - {"alembic_version"}:
        raise RuntimeError("baseline installation requires an empty database")
    baseline.apply(connection)


def downgrade() -> None:
    raise RuntimeError(
        "restore the verified pre-cutover backup; destructive downgrade is unavailable"
    )
