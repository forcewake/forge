"""The final engine owns its metadata without importing retired lifecycle models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
