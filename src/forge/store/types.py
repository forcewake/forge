"""Database money never travels through SQLite's floating point conversion."""

from decimal import Decimal
from typing import cast

from sqlalchemy import BigInteger, Numeric, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

from forge.domain.budget import Resources


def exact_money(value: Decimal | str) -> Decimal:
    if not isinstance(value, (Decimal, str)):
        raise ValueError("money must be decimal text or Decimal")
    parsed = Decimal(value)
    return Resources(usd=parsed).usd


class ExactMoney(TypeDecorator[Decimal]):
    impl = Numeric(38, 18)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[object]:
        return dialect.type_descriptor(
            cast(TypeEngine[object], Text() if dialect.name == "sqlite" else Numeric(38, 18))
        )

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Decimal | str | None:
        if value is None:
            return None
        parsed = exact_money(value)
        return format(parsed, "f") if dialect.name == "sqlite" else parsed

    def process_result_value(self, value: Decimal | str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else exact_money(value)


class ExactCount(TypeDecorator[int]):
    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: int | None, dialect: Dialect) -> int | None:
        if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
            raise ValueError("resource count must be an exact nonnegative signed-64-bit integer")
        return value

    def process_result_value(self, value: int | None, dialect: Dialect) -> int | None:
        return self.process_bind_param(value, dialect)
