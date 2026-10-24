"""Offline source inspection never converts old lifecycle authority into new runs."""

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection


class CutoverRefused(RuntimeError):
    pass


# These are the maintenance baseline and the explicitly additive development
# revisions. Other versions require their original release's upgrade procedure.
SUPPORTED_REVISIONS = frozenset(("032", "033", "034", "035"))
_QUIESCENT = {
    "flow_runs": "status IS NULL OR status NOT IN ('ready_for_human','blocked','failed','cancelled')",
    "step_runs": "status IS NULL OR status NOT IN ('succeeded','failed','skipped','dead','cancelled')",
    "action_log": "status IS NULL OR status NOT IN ('succeeded','failed')",
    "publication_intents": "status IS NULL OR status NOT IN ('committed','adopted','duplicated','failed')",
    "event_inbox": "status IS NULL OR status NOT IN ('processed','rejected')",
    "outbox": "processed_at IS NULL",
    "mr_reservations": "status IS NULL OR status <> 'confirmed'",
    "execution_leases": "released_at IS NULL",
    "budget_reservations": "released IS NULL OR NOT released",
    "run_budgets": "status IS NULL OR status <> 'closed'",
    "usage_receipts": "final IS NULL OR NOT final OR completeness IS NULL OR completeness NOT IN ('exact','aggregate')",
    "operation_grants": "status IS NULL OR status <> 'revoked'",
    "review_rounds": "status IS NULL OR status NOT IN ('stale','completed','ended')",
}


async def assert_quiescent(connection: AsyncConnection) -> None:
    if connection.dialect.name == "postgresql":
        namespaces = list(
            (
                await connection.execute(
                    text(
                        "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' "
                        "AND nspname <> 'information_schema' ORDER BY nspname"
                    )
                )
            ).scalars()
        )
        if namespaces != ["public"]:
            raise CutoverRefused("unsupported legacy namespaces; source remains untouched")
    names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    if not {"alembic_version", *_QUIESCENT} <= names:
        raise CutoverRefused(
            "unsupported legacy schema; preserve it and use its original upgrade procedure"
        )
    revisions = tuple(
        (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars()
    )
    if len(revisions) != 1 or revisions[0] not in SUPPORTED_REVISIONS:
        raise CutoverRefused("unsupported or branched legacy revision")
    for name, predicate in _QUIESCENT.items():
        if name in names and await connection.scalar(
            text(f'SELECT count(*) FROM "{name}" WHERE {predicate}')
        ):
            raise CutoverRefused("legacy lifecycle, effects, leases or spend remain unresolved")
    for name in names:
        quoted = connection.dialect.identifier_preparer.quote(name)
        if name.startswith("eng_") and await connection.scalar(
            text("SELECT count(*) FROM " + quoted)
        ):
            raise CutoverRefused(
                "development engine history requires explicit operator reconciliation"
            )
