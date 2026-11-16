"""A deleted planning key can be minted again under the same guard alias."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tools.litellm.guard_ledger import NativeLedger, deleted_aliases


class Memory:
    """Prisma-shaped store that rejects a second live row for one alias."""

    def __init__(self) -> None:
        self.native: dict[str, dict[str, object]] = {}
        self.accounts: dict[str, dict[str, object]] = {}
        self.by_alias: dict[str, str] = {}
        self.holds: dict[str, dict[str, object]] = {}

    def tx(self) -> "Memory":
        return self

    async def __aenter__(self) -> "Memory":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute_raw(self, sql: str, *args: object) -> None:
        self._apply(sql, args)

    async def query_raw(self, sql: str, *args: object) -> list[dict[str, object]]:
        return self._apply(sql, args)

    def _apply(self, sql: str, args: tuple[object, ...]) -> list[dict[str, object]]:
        if sql.startswith("CREATE TABLE"):
            return []
        if "LiteLLM_VerificationToken" in sql:
            token, ceiling = str(args[0]), Decimal(str(args[1]))
            row = self.native.get(token)
            if row is None or row["blocked"] or Decimal(str(row["max_budget"])) < ceiling:
                return []
            return [{"key_alias": row["key_alias"]}]
        if sql.startswith("SELECT token_digest FROM forge_budget_guard_accounts WHERE alias"):
            token = self.by_alias.get(str(args[0]))
            return [{"token_digest": token}] if token is not None else []
        if sql.startswith("UPDATE forge_budget_guard_accounts SET blocked=TRUE"):
            token, renamed = str(args[0]), str(args[1])
            row = self.accounts[token]
            del self.by_alias[str(row["alias"])]
            row["alias"] = renamed
            row["blocked"] = True
            self.by_alias[renamed] = token
            return []
        if sql.startswith("INSERT INTO forge_budget_guard_accounts"):
            token, alias, ceiling = str(args[0]), str(args[1]), Decimal(str(args[2]))
            if token in self.accounts:
                return []
            if alias in self.by_alias:
                raise RuntimeError("RawQueryError: duplicate key value violates unique constraint")
            self.accounts[token] = {
                "token_digest": token,
                "alias": alias,
                "ceiling": ceiling,
                "held": Decimal(0),
                "settled": Decimal(0),
                "blocked": False,
            }
            self.by_alias[alias] = token
            return []
        if sql.startswith("SELECT * FROM forge_budget_guard_accounts WHERE token_digest"):
            row = self.accounts.get(str(args[0]))
            return [row] if row is not None else []
        if sql.startswith("SELECT * FROM forge_budget_guard_holds WHERE request_id"):
            row = self.holds.get(str(args[0]))
            return [row] if row is not None else []
        if "held=held+" in sql:
            token, ceiling, amount = str(args[0]), Decimal(str(args[1])), Decimal(str(args[2]))
            row = self.accounts[token]
            if row["blocked"] or row["settled"] + row["held"] + amount > ceiling:
                return []
            row["ceiling"] = ceiling
            row["held"] = row["held"] + amount
            return [{"token_digest": token}]
        if sql.startswith("INSERT INTO forge_budget_guard_holds"):
            request, token, amount = str(args[0]), str(args[1]), Decimal(str(args[2]))
            self.holds[request] = {
                "request_id": request,
                "token_digest": token,
                "reserved": amount,
                "state": "held",
            }
            return []
        raise AssertionError(sql)


async def test_delete_then_remint_keeps_one_live_alias():
    from pathlib import Path

    guard = Path("tools/litellm/forge_budget_guard.py").read_text()
    assert "/key/delete" in guard and "retire_alias" in guard
    aliases = deleted_aliases({"key_aliases": ["forge:run:1:1:planning"]})
    assert aliases == ("forge:run:1:1:planning",)
    memory = Memory()
    ledger = NativeLedger(memory)
    alias = aliases[0]
    old, new = "a" * 64, "b" * 64
    expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    memory.native[old] = {"key_alias": alias, "blocked": False, "expires": expiry, "max_budget": 10}
    await ledger.reserve(old, alias, "c" * 32, Decimal("10"), Decimal("1"))
    memory.native[old]["blocked"] = True
    memory.native[new] = {"key_alias": alias, "blocked": False, "expires": expiry, "max_budget": 10}
    await ledger.retire_alias(alias)
    await ledger.reserve(new, alias, "d" * 32, Decimal("10"), Decimal("1"))
    assert memory.accounts[new]["alias"] == alias
    assert memory.accounts[old]["blocked"] is True
    assert memory.accounts[old]["alias"] == f"{alias}:retired:{old}"
    assert memory.holds["c" * 32]["token_digest"] == old


async def test_remint_replaces_the_alias_when_delete_did_not_run():
    memory = Memory()
    ledger = NativeLedger(memory)
    alias = "forge:run:1:1:planning"
    old, new = "1" * 64, "2" * 64
    expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    memory.native[old] = {"key_alias": alias, "blocked": False, "expires": expiry, "max_budget": 10}
    memory.native[new] = {"key_alias": alias, "blocked": False, "expires": expiry, "max_budget": 10}
    await ledger.reserve(old, alias, "e" * 32, Decimal("10"), Decimal("1"))
    memory.native[old]["blocked"] = True
    await ledger.reserve(new, alias, "f" * 32, Decimal("10"), Decimal("1"))
    assert memory.by_alias[alias] == new
    assert memory.accounts[old]["alias"] == f"{alias}:retired:{old}"
