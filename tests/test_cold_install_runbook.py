"""Cold installation uses the actual schema/doctor and release artifact gates.

The retired annotated-runbook grader never proved a second operator installed
anything. These checks assert read-only preflight and exact artifact identity;
real PostgreSQL upgrade/canary evidence remains in its existing owning suites.
"""

from types import SimpleNamespace
import pytest

from forge import doctor
from forge.database import check_schema_compatible, dispose_engine
from tests.test_database_lifecycle import _alembic_head, _seed_database, _stamped_revisions
from tools.release.live_smoke import verify_install


async def test_read_only_preflight_does_not_bootstrap_an_empty_database(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    try:
        with pytest.raises(RuntimeError, match="not bootstrapped"):
            await check_schema_compatible(url)
        engine = create_async_engine(url)
        async with engine.connect() as connection:
            assert (
                await connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            ).all() == []
        await engine.dispose()
    finally:
        await dispose_engine()


async def test_existing_schema_at_wrong_revision_refuses_without_upgrade(tmp_path):
    url = await _seed_database(tmp_path / "old.db", "001")
    try:
        with pytest.raises(RuntimeError, match="out of date"):
            await check_schema_compatible(url)
        assert await _stamped_revisions(url) == {"001"}
    finally:
        await dispose_engine()


async def test_existing_schema_at_release_head_passes_without_rewrite(tmp_path):
    head = _alembic_head()
    url = await _seed_database(tmp_path / "current.db", head)
    try:
        await check_schema_compatible(url)
        assert await _stamped_revisions(url) == {head}
    finally:
        await dispose_engine()


def test_lane_artifact_version_or_digest_mismatch_refuses_before_model_work():
    identity = {
        "pin": "wheel",
        "route": "explicit-wheel",
        "version": "1.0.0",
        "qualified": True,
        "actual_sha256": "a" * 64,
        "expected_sha256": "a" * 64,
    }
    verify_install(identity, "1.0.0", "a" * 64)
    for field, value in (("version", "0.42.0"), ("actual_sha256", "b" * 64), ("pin", "git")):
        with pytest.raises(ValueError, match="exact RC"):
            verify_install({**identity, field: value}, "1.0.0", "a" * 64)


async def test_doctor_observes_checkpoint_authority_without_creating_marker(tmp_path, monkeypatch):
    from forge.adaptive.checkpoint_repository import authority_marker_path

    monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("FORGE_CHECKPOINT_DURABILITY", "postgres")
    result = await doctor.check_checkpoint_authority(SimpleNamespace())
    assert next(r for r in result if r.name == "checkpoint.authority_marker").status == "pass"
    assert next(r for r in result if r.name == "checkpoint.blob_topology").status == "warn"
    assert not authority_marker_path(tmp_path).exists()


@pytest.mark.parametrize("option", ["--capabilities", "--support-matrix", "--strict"])
def test_retired_qualification_modes_are_usage_errors(option):
    with pytest.raises(SystemExit) as caught:
        doctor.main([option])
    assert caught.value.code == 2
