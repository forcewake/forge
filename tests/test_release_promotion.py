"""Promotion keeps exact artifact, native-provider and migration evidence."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

from tools.release.artifacts import report
from tests.test_release_flow import candidate as candidate, release_proofs

ROOT = Path(__file__).resolve().parents[1]


def test_a_failed_required_check_blocks_despite_a_green_canary(candidate):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    smokes[1]["status"] = "failed"
    with pytest.raises(ValueError, match="live smoke did not pass"):
        report(manifest, canary, smokes, junit)


@pytest.mark.parametrize("status", [None, "unknown", "skipped", "cancelled"])
def test_an_unexecuted_check_blocks_fail_closed(candidate, status):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    smokes[0]["status"] = status
    with pytest.raises(ValueError):
        report(manifest, canary, smokes, junit)


def test_an_empty_profile_blocks_fail_closed(candidate):
    directory, manifest = candidate
    canary, _, junit = release_proofs(directory, manifest)
    with pytest.raises(ValueError, match="both provider smokes"):
        report(manifest, canary, [], junit)


@pytest.mark.parametrize("tag", ["failure", "error", "skipped"])
def test_a_nonpassing_invariant_is_not_hidden_by_a_green_canary(candidate, tag):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    junit.write_text(
        f"<testsuites><testsuite><testcase><{tag}/></testcase></testsuite></testsuites>"
    )
    with pytest.raises(ValueError, match="zero-skip"):
        report(manifest, canary, smokes, junit)


def test_no_canary_evidence_blocks_fail_closed(candidate):
    directory, manifest = candidate
    _, smokes, junit = release_proofs(directory, manifest)
    with pytest.raises(ValueError, match="fresh-database boot"):
        report(manifest, {"image": manifest["image"], "stages": []}, smokes, junit)


def test_a_failed_then_successful_canary_preserves_the_failure(candidate):
    directory, manifest = candidate
    canary, smokes, junit = release_proofs(directory, manifest)
    canary["stages"].insert(0, {"stage": "fresh", "outcome": "fail"})
    original = copy.deepcopy(canary)
    with pytest.raises(ValueError, match="stage failed"):
        report(manifest, canary, smokes, junit)
    assert canary == original


def test_required_quality_and_both_smokes_precede_promotion():
    jobs = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    assert jobs["candidate"]["needs"] == "quality"
    assert jobs["quality"]["uses"] == "./.github/workflows/ci.yml"
    assert set(jobs["live-smoke"]["strategy"]["matrix"]["provider"]) == {"github", "gitlab"}
    assert set(jobs["promote"]["needs"]) == {"candidate", "live-smoke"}
    assert all("if" not in jobs[name] for name in ("candidate", "live-smoke", "promote"))


PG_TEST_URL = os.environ.get("FORGE_PG_TEST_URL", "")

_pg_reason = (
    "FORGE_PG_TEST_URL not set — the seeded-upgrade canary runs only against a "
    "disposable real Postgres (its schema is dropped)"
)


def _load_canary_smoke():
    spec = importlib.util.spec_from_file_location(
        "canary_smoke", ROOT / "tools" / "canary_smoke.py"
    )
    canary_smoke = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(canary_smoke)
    return canary_smoke


async def _fingerprint(engine, canary_smoke) -> dict[str, str]:
    """The canary's per-table preservation fingerprint, via SQLAlchemy."""
    from sqlalchemy import text

    fingerprint: dict[str, str] = {}
    async with engine.begin() as conn:
        for table, expression in canary_smoke._SEED_FINGERPRINTS:
            count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            digest = (
                await conn.execute(
                    text(
                        "SELECT encode(sha256(convert_to("
                        "coalesce(string_agg(x, E'|' ORDER BY x), ''), 'UTF8')), 'hex') "
                        f"FROM (SELECT {expression} AS x FROM {table}) s"
                    )
                )
            ).scalar_one()
            fingerprint[table] = f"{count} {digest}"
    return fingerprint


@pytest.mark.skipif(not PG_TEST_URL, reason=_pg_reason)
async def test_seeded_real_data_survives_the_n_minus_1_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canary's seed SQL is schema-accurate at the PREVIOUS head, and the
    delta upgrade preserves every seeded row: counts and the sha256
    fingerprints over ordered row identity — the exact scenario
    ``scripts/canary_smoke.py --seed-real-data`` drives in CI."""
    import asyncio

    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from forge.migrate import build_alembic_config

    canary_smoke = _load_canary_smoke()
    script = ScriptDirectory.from_config(build_alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, heads
    head = heads[0]
    previous = script.get_revision(head).down_revision
    assert previous is not None, "the chain must have an N-1 revision to upgrade from"

    def _upgrade(revision: str) -> None:
        # alembic's env.py drives asyncio.run() internally — off the pytest
        # loop — and resolves the URL from DATABASE_URL (the forge.migrate
        # contract); pin it to the DISPOSABLE test DB so a developer's .env
        # can never leak a live database in here.
        monkeypatch.setenv("DATABASE_URL", PG_TEST_URL)
        command.upgrade(build_alembic_config(PG_TEST_URL), revision)

    async def upgrade(revision: str) -> None:
        await asyncio.to_thread(_upgrade, revision)

    engine = create_async_engine(PG_TEST_URL)
    try:
        # A disposable database: drop everything (the PostgresLab pattern).
        async with engine.begin() as conn:
            names = (
                (
                    await conn.execute(
                        text("select tablename from pg_tables where schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
            for name in names:
                await conn.execute(text(f'drop table if exists "{name}" cascade'))
        await engine.dispose()

        # 1. The PREVIOUS release's chain (schema at the N-1 head), then the
        #    canary's raw seed SQL against exactly that schema.
        await upgrade(previous)
        async with engine.begin() as conn:
            for statement in canary_smoke._SEED_SQL.split(";\n\n"):
                if statement.strip():
                    await conn.execute(text(statement))
        fingerprint_before = await _fingerprint(engine, canary_smoke)
        assert fingerprint_before, "the seed SQL must have inserted rows"
        await engine.dispose()

        # 2. This release's delta upgrade (never create_all).
        await upgrade(head)

        # 3. Every seeded row survived: identical per-table count + sha256.
        fingerprint_after = await _fingerprint(engine, canary_smoke)
        assert fingerprint_after == fingerprint_before
    finally:
        await engine.dispose()


@pytest.mark.skipif(not PG_TEST_URL, reason=_pg_reason)
async def test_the_seeded_tables_exist_at_the_previous_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every table the canary seeds must already exist one revision back —
    seeding a table the previous release never had would be a fiction."""
    import asyncio

    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from forge.migrate import build_alembic_config

    canary_smoke = _load_canary_smoke()
    script = ScriptDirectory.from_config(build_alembic_config())
    head = script.get_heads()[0]
    previous = script.get_revision(head).down_revision

    engine = create_async_engine(PG_TEST_URL)
    try:
        async with engine.begin() as conn:
            names = (
                (
                    await conn.execute(
                        text("select tablename from pg_tables where schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
            for name in names:
                await conn.execute(text(f'drop table if exists "{name}" cascade'))
        await engine.dispose()
        monkeypatch.setenv("DATABASE_URL", PG_TEST_URL)
        await asyncio.to_thread(command.upgrade, build_alembic_config(PG_TEST_URL), previous)
        async with engine.begin() as conn:
            tables = set(
                (
                    await conn.execute(
                        text("select tablename from pg_tables where schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
        seeded = {table for table, _ in canary_smoke._SEED_FINGERPRINTS}
        assert seeded <= tables, (
            f"the canary seeds tables that do not exist at the previous head {previous}: "
            f"{sorted(seeded - tables)}"
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# canary_smoke argument discipline (no containers needed)
# ---------------------------------------------------------------------------


def test_seed_real_data_requires_the_migrate_stage(capsys: pytest.CaptureFixture) -> None:
    canary_smoke = _load_canary_smoke()
    code = canary_smoke.main(
        [
            "localhost/forge:canary",
            "--allow-unpinned",
            "--stages",
            "fresh",
            "--seed-real-data",
        ]
    )
    assert code == 2
    assert "--seed-real-data needs the migrate stage" in capsys.readouterr().err


def test_stage_migrate_records_the_skip_when_the_previous_image_is_not_pullable() -> None:
    canary_smoke = _load_canary_smoke()

    class Unpullable:
        def try_pull(self, image: str) -> bool:
            return False

    recorder = canary_smoke.Recorder("img", None)
    shipped = canary_smoke.stage_migrate(
        Unpullable(),
        "img",
        "ghcr.io/forcewake/forge:latest",
        seed_real_data=True,
        recorder=recorder,
    )
    assert shipped is False
    outcomes = {(row["stage"], row["outcome"]) for row in recorder.stages}
    assert ("migrate", "skip") in outcomes
    assert ("seed-real-data", "skip") in outcomes


def test_the_recorder_writes_fail_rows_for_a_failed_stage(tmp_path: Path) -> None:
    canary_smoke = _load_canary_smoke()
    path = tmp_path / "results.json"
    recorder = canary_smoke.Recorder("img", str(path))
    recorder.add("fresh", "pass")
    recorder.note_failure("migrate", "boom")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["image"] == "img"
    assert [row["outcome"] for row in document["stages"]] == ["pass", "fail"]
    assert document["stages"][1]["capability"] == "release-canary/previous-release-upgrade"
    # a failure recorded by the stage itself is not duplicated:
    recorder.note_failure("migrate", "boom again")
    assert len(recorder.stages) == 2


def test_psql_exec_passes_stdin_through_docker_i() -> None:
    """``docker exec`` drops piped stdin without ``-i``: psql then reads an
    EMPTY script, runs nothing, exits 0 — the count parses as ``''`` (the
    v0.34.0 release-canary bite). The exec argv must carry ``-i``."""
    canary_smoke = _load_canary_smoke()
    canary = canary_smoke.Canary(runtime="podman")
    argv: list[str] = []

    def fake_run(*args: str, input: str | None = None, **_kwargs: object) -> str:
        argv.extend(args)
        assert input is not None, "the SQL must ride stdin, not -c"
        return "42\n"

    canary.run = fake_run  # type: ignore[method-assign]
    assert canary_smoke._psql(canary, "SELECT 1;") == "42\n"
    exec_at = argv.index("exec")
    assert argv[exec_at + 1] == "-i", f"exec must pass -i before the container: {argv}"
    assert canary_smoke._psql_count(canary, "t") == 42
