"""The wheel carries exactly one fresh baseline, independent of old migrations."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def config(database_url: str | None = None) -> Config:
    result = Config()
    result.set_main_option("script_location", str(Path(__file__).resolve().parent / "alembic"))
    if database_url is not None:
        result.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return result


def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(config())
