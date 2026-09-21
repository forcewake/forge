"""NXT-05: the durable discovery stage spliced into the /implement path.

These tests pin the integration slice's behavior:

- disabled by default (the classic planner input, byte for byte, and
  nothing persisted — the lazy snapshot is not even read);
- enabled: dispatch record persisted with durable identity + read-only
  dispatch intent BEFORE completion, evidence artifact written with
  file:line citations, bounded digest attached to the planner input;
- durability across a "process restart": a second engine/sessionmaker
  over the same database file ADOPTS the completed discovery (no probes
  re-paid, ``discovery.replayed`` journaled) and a dispatched-but-never-
  completed record recovers under the SAME discovery id;
- failure is loud: a failed discovery never silently falls back;
- the NXT-06 citation contract: ``evidence:<id>`` citations validated
  against the durable record's evidence ids — invalid citations reject
  the plan, uncited claims stay allowed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forge.adaptive.artifact_store import ContentAddressedStore
from forge.adaptive.discovery import dispatch_target
from forge.adaptive.discovery_stage import (
    DIGEST_BEGIN,
    DIGEST_END,
    FORGE_DISCOVERY_ENABLED_ENV,
    PLANNER_INPUT_CAP_CHARS,
    DiscoveryRunContext,
    DiscoveryStageError,
    InvalidPlanCitation,
    attach_digest,
    discovery_enabled,
    enforce_plan_citations,
    extract_citations,
    extract_keywords,
    evidence_ids_of,
    frozen_input_digest,
    load_snapshot_files,
    maybe_run_discovery,
    render_digest_section,
    run_discovery_stage,
    snapshot_set_digest,
    validate_plan_citations,
)
from forge.durable import FlowRun, Outbox
from forge.factory.planner import PLANNER_MAX_INPUT_CHARS
from forge.models.base import Base

PLANNER_INPUT = "Refactor the LLMPlanner so plan() cites evidence and start_run stays stable."

FILES = {
    "src/app/planner.py": ("class LLMPlanner:\n    def plan(self, issue):\n        return issue\n"),
    "src/app/service.py": (
        "from app.planner import LLMPlanner\n"
        "\n"
        "def start_run():\n"
        "    planner = LLMPlanner()\n"
        "    return planner.plan(None)\n"
    ),
    "README.md": "Use LLMPlanner for planning.\n",
}

RUN_ID = "run-disc-stage-1"


class FakeReader:
    """Duck-typed repository read surface (get_tree / read_text)."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.tree_reads = 0
        self.text_reads: list[str] = []

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> list:
        self.tree_reads += 1
        entries = [SimpleNamespace(path=p, type="blob") for p in sorted(self._files)]
        entries.insert(1, SimpleNamespace(path="docs", type="tree"))
        return entries

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        self.text_reads.append(file_path)
        return self._files[file_path]


async def _new_process(db_path: Path) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    """A fresh engine + session factory over the SAME file = a restart."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def _seed_run(factory: async_sessionmaker[AsyncSession], run_id: str = RUN_ID) -> None:
    async with factory() as session:
        session.add(FlowRun(id=run_id, project_id=1, status="planning"))
        await session.commit()


async def _discovery_record(
    factory: async_sessionmaker[AsyncSession], run_id: str = RUN_ID
) -> dict:
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        return dict((run.evidence or {}).get("discovery") or {})


async def _outbox_counts(factory: async_sessionmaker[AsyncSession], run_id: str = RUN_ID) -> dict:
    async with factory() as session:
        rows = (
            (await session.execute(select(Outbox).where(Outbox.flow_run_id == run_id)))
            .scalars()
            .all()
        )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.event_type] = counts.get(row.event_type, 0) + 1
        return counts


def _ctx(
    factory: async_sessionmaker[AsyncSession],
    *,
    files: dict[str, str] | None = None,
    store: ContentAddressedStore | None = None,
    max_digest_chars: int | None = None,
) -> DiscoveryRunContext:
    kwargs: dict = {}
    if max_digest_chars is not None:
        kwargs["max_digest_chars"] = max_digest_chars
    return DiscoveryRunContext(
        run_id=RUN_ID,
        project_id=1,
        session_factory=factory,
        snapshot_files=dict(FILES if files is None else files),
        repository_id="example/repo",
        source_oid="f" * 40,
        store=store,
        **kwargs,
    )


def _digest_of(augmented: str) -> dict:
    """Parse the delimited digest section out of an augmented input."""
    body = augmented[augmented.index(DIGEST_BEGIN) + len(DIGEST_BEGIN) :]
    body = body[: body.index(DIGEST_END)]
    return json.loads(body.strip())


def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FORGE_DISCOVERY_ENABLED_ENV, "1")


class TestGating:
    def test_off_by_default(self):
        assert discovery_enabled({}) is False
        assert discovery_enabled({FORGE_DISCOVERY_ENABLED_ENV: ""}) is False
        assert discovery_enabled({FORGE_DISCOVERY_ENABLED_ENV: "0"}) is False

    def test_truthy_spellings(self):
        for value in ("1", "true", "TRUE", "Yes", "on"):
            assert discovery_enabled({FORGE_DISCOVERY_ENABLED_ENV: value}) is True

    async def test_disabled_returns_input_unchanged_and_persists_nothing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv(FORGE_DISCOVERY_ENABLED_ENV, raising=False)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        reader = FakeReader(FILES)
        ctx = DiscoveryRunContext.from_reader(
            run_id=RUN_ID,
            project_id=1,
            session_factory=factory,
            reader=reader,
            ref="main",
        )
        try:
            assert await maybe_run_discovery(ctx, PLANNER_INPUT) == PLANNER_INPUT
            assert await _discovery_record(factory) == {}
            assert await _outbox_counts(factory) == {}
            assert reader.tree_reads == 0  # the lazy snapshot was never even read
        finally:
            await engine.dispose()


class TestTheStage:
    async def test_enabled_augments_planner_input_with_bounded_digest(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            augmented = await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            assert augmented.startswith(PLANNER_INPUT)
            assert DIGEST_BEGIN in augmented and DIGEST_END in augmented
            digest = _digest_of(augmented)
            assert digest["schema"] == "forge.discovery.digest/1"
            assert digest["repository_researched"] is True
            assert digest["truncated"] is False
            entries = digest["evidence"]
            assert entries, "the digest must carry citable file:line entries"
            first = entries[0]
            assert first["path"] in FILES and first["line"] >= 1
            assert first["id"].startswith("ev-")
        finally:
            await engine.dispose()

    async def test_record_has_durable_identity_and_read_only_dispatch_intent(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            record = await _discovery_record(factory)
            assert record["schema"] == "forge.discovery.stage-record/1"
            assert record["discovery_id"].startswith("disc-")
            assert record["run_id"] == RUN_ID
            assert record["status"] == "complete"
            assert record["repository_researched"] is True
            dispatch = record["dispatch"]
            assert dispatch["target"] == dispatch_target() == "ci_execution_profile"
            assert dispatch["intent"] == "read_only_research"
            assert dispatch["executor"] == "snapshot_toolbox"
            assert len(dispatch["frozen_input_digest"]) == 64
            assert dispatch["snapshot_set_digest"] == snapshot_set_digest(FILES)
            counts = await _outbox_counts(factory)
            assert counts.get("discovery.started") == 1
            assert counts.get("plan.research_mode") == 1
        finally:
            await engine.dispose()

    async def test_evidence_artifact_written_with_file_line_citations(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        store = ContentAddressedStore(tmp_path / "artifacts", tenant="tenant-a")
        try:
            outcome = await run_discovery_stage(_ctx(factory, store=store), PLANNER_INPUT)
            assert outcome.artifact_digest
            record = await _discovery_record(factory)
            assert record["evidence_artifact_digest"] == outcome.artifact_digest
            assert store.resolve(outcome.artifact_digest)
            bundle = json.loads(store.get(outcome.artifact_digest))
            assert bundle["schema"] == "forge.discovery.evidence-bundle/1"
            assert bundle["discovery_id"] == outcome.discovery_id
            records = bundle["records"]
            assert records
            for rec in in_both(records, record["evidence"]):
                assert rec["path"] in FILES
                assert rec["line"] >= 1
                assert len(rec["content_digest"]) == 64
                assert rec["repository_id"] == "example/repo"
                assert rec["source_oid"] == "f" * 40
            assert all("text" in rec for rec in records)  # full bundle keeps cited lines
        finally:
            await engine.dispose()

    async def test_without_a_store_the_compact_records_ride_the_evidence_blob(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            outcome = await run_discovery_stage(_ctx(factory), PLANNER_INPUT)
            assert outcome.artifact_digest == ""
            record = await _discovery_record(factory)
            assert [e["id"] for e in record["evidence"]] == list(outcome.evidence_ids)
            assert all("text" not in e for e in record["evidence"])  # compact, bounded
        finally:
            await engine.dispose()


def in_both(records: list, compact: list) -> list:
    """The artifact records, cross-checked against the compact record ids."""
    compact_ids = {entry["id"] for entry in compact}
    assert {rec["id"] for rec in records} == compact_ids
    return records


class TestDurability:
    async def test_restart_adopts_the_completed_discovery_without_repaying(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        db = tmp_path / "runs.db"
        factory_a, engine_a = await _new_process(db)
        await _seed_run(factory_a)
        try:
            first = await maybe_run_discovery(_ctx(factory_a), PLANNER_INPUT)
            record_a = await _discovery_record(factory_a)
        finally:
            await engine_a.dispose()

        # "Process restart": a brand-new engine over the same database file.
        factory_b, engine_b = await _new_process(db)
        try:
            second = await maybe_run_discovery(_ctx(factory_b), PLANNER_INPUT)
            record_b = await _discovery_record(factory_b)
            assert second == first  # same augmented planner input, byte for byte
            assert record_b["discovery_id"] == record_a["discovery_id"]
            assert record_b["status"] == "complete"
            assert record_b["replay_count"] == 1
            counts = await _outbox_counts(factory_b)
            assert counts.get("discovery.started") == 1  # the probes were paid ONCE
            assert counts.get("discovery.replayed") == 1
            assert counts.get("plan.research_mode") == 2  # one per planning pass
        finally:
            await engine_b.dispose()

    async def test_dispatched_record_recovers_under_the_same_identity(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            # Simulate a crash between the dispatch write and completion:
            # only the dispatched record (with its frozen input digest) exists.
            async with factory() as session:
                run = await session.get(FlowRun, RUN_ID)
                run.evidence = {
                    **(run.evidence or {}),
                    "discovery": {
                        "schema": "forge.discovery.stage-record/1",
                        "discovery_id": "disc-crashed",
                        "run_id": RUN_ID,
                        "status": "dispatched",
                        "dispatch": {
                            "target": dispatch_target(),
                            "intent": "read_only_research",
                            "frozen_input_digest": frozen_input_digest(
                                PLANNER_INPUT, snapshot_set_digest(FILES)
                            ),
                        },
                        "evidence": [],
                        "replay_count": 0,
                    },
                }
                await session.commit()

            outcome = await run_discovery_stage(_ctx(factory), PLANNER_INPUT)
            assert outcome.discovery_id == "disc-crashed"  # identity adopted, not re-minted
            assert outcome.replayed is False
            record = await _discovery_record(factory)
            assert record["status"] == "complete"
            counts = await _outbox_counts(factory)
            assert counts.get("discovery.started") == 1
            assert "discovery.replayed" not in counts
        finally:
            await engine.dispose()

    async def test_failed_discovery_never_silently_falls_back(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            async with factory() as session:
                run = await session.get(FlowRun, RUN_ID)
                run.evidence = {
                    **(run.evidence or {}),
                    "discovery": {
                        "schema": "forge.discovery.stage-record/1",
                        "discovery_id": "disc-doomed",
                        "run_id": RUN_ID,
                        "status": "failed",
                        "dispatch": {"intent": "read_only_research"},
                        "block_reason": "probe executor exploded",
                        "evidence": [],
                        "replay_count": 0,
                    },
                }
                await session.commit()

            with pytest.raises(DiscoveryStageError, match="disc-doomed"):
                await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            record = await _discovery_record(factory)
            assert record["status"] == "failed"  # not overwritten into a fake success
        finally:
            await engine.dispose()

    async def test_changed_issue_input_is_new_evidence_not_a_replay(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            first = await _discovery_record(factory)
            changed = PLANNER_INPUT + " Also rename start_run to launch."
            await maybe_run_discovery(_ctx(factory), changed)
            second = await _discovery_record(factory)
            assert second["discovery_id"] != first["discovery_id"]
            assert second["supersedes"] == first["discovery_id"]
            counts = await _outbox_counts(factory)
            assert counts.get("discovery.started") == 2
            assert "discovery.replayed" not in counts
        finally:
            await engine.dispose()

    async def test_missing_run_is_a_loud_error_not_a_no_op(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        try:
            with pytest.raises(DiscoveryStageError, match="not found"):
                await run_discovery_stage(_ctx(factory), PLANNER_INPUT)
        finally:
            await engine.dispose()


class TestDigestBoundedness:
    async def test_digest_section_respects_its_budget_and_marks_truncation(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            augmented = await maybe_run_discovery(
                _ctx(factory, max_digest_chars=600), PLANNER_INPUT
            )
            section = augmented[augmented.rindex(DIGEST_BEGIN) :]
            assert len(section) <= 600
            digest = _digest_of(augmented)
            assert digest["truncated"] is True
            assert digest["dropped"] > 0
            # every surviving id is still a real recorded id
            record = await _discovery_record(factory)
            known = set(evidence_ids_of(record))
            assert {entry["id"] for entry in digest["evidence"]} <= known
        finally:
            await engine.dispose()

    def test_attach_digest_keeps_the_planner_cap(self):
        section = render_digest_section(
            {
                "discovery_id": "disc-x",
                "repository_researched": True,
                "evidence": [
                    {
                        "id": f"ev-{i}",
                        "path": "src/a.py",
                        "line": i,
                        "kind": "symbol",
                        "detail": "s",
                    }
                    for i in range(1, 20)
                ],
            }
        )
        combined = attach_digest("i" * 11_990, section)
        assert len(combined) <= PLANNER_INPUT_CAP_CHARS
        assert combined.endswith(section)  # the digest is never the thing cut
        assert "issue text truncated to" in combined

    def test_attach_digest_short_input_has_no_marker(self):
        section = f"{DIGEST_BEGIN}\n{{}}\n{DIGEST_END}"
        combined = attach_digest("short issue", section)
        assert combined == f"short issue\n\n{section}"

    def test_planner_cap_constant_tracks_the_planner(self):
        assert PLANNER_INPUT_CAP_CHARS == PLANNER_MAX_INPUT_CHARS


class TestCitations:
    def test_extract_citations_is_ordered_and_deduped(self):
        text = "see evidence:ev-2 then evidence:ev-1 and again evidence:ev-2"
        assert extract_citations(text) == ("ev-2", "ev-1")

    def test_invalid_citation_rejects_the_plan(self):
        plan = {"steps": ["Refactor per evidence:ev-99", "plain step"]}
        assert validate_plan_citations(plan, ("ev-1", "ev-2")) == [
            "step 1: unknown evidence id 'ev-99'"
        ]
        with pytest.raises(InvalidPlanCitation, match="ev-99"):
            enforce_plan_citations(plan, ("ev-1", "ev-2"))

    def test_uncited_claims_stay_allowed(self):
        plan = {"steps": ["a plain uncited step"], "summary": "no citations anywhere"}
        assert validate_plan_citations(plan, ()) == []
        enforce_plan_citations(plan, ())  # does not raise

    def test_valid_citations_and_dict_shaped_steps_pass(self):
        plan = {
            "steps": [
                "touch the planner per evidence:ev-1",
                {
                    "step_id": "s2",
                    "objective": "update service per evidence:ev-2",
                    "evidence_refs": ["ev-2", "evidence:ev-1"],
                },
            ]
        }
        assert validate_plan_citations(plan, ("ev-1", "ev-2")) == []
        enforce_plan_citations(plan, ("ev-1", "ev-2"))

    def test_dict_step_refs_are_validated_too(self):
        plan = {"steps": [{"step_id": "s1", "objective": "", "evidence_refs": ["ev-404"]}]}
        assert validate_plan_citations(plan, ("ev-1",)) == ["step s1: unknown evidence id 'ev-404'"]

    async def test_citations_validate_against_the_durable_record(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            outcome = await run_discovery_stage(_ctx(factory), PLANNER_INPUT)
            record = await _discovery_record(factory)
            known = evidence_ids_of(record)
            assert set(known) == set(outcome.evidence_ids)
            good = {"steps": [f"work per evidence:{outcome.evidence_ids[0]}"]}
            enforce_plan_citations(good, known)  # the recorded id is citable
            bad = {"steps": [f"work per evidence:{outcome.evidence_ids[-1]}-x"]}
            with pytest.raises(InvalidPlanCitation):
                enforce_plan_citations(bad, known)
        finally:
            await engine.dispose()


class TestHelpers:
    def test_extract_keywords_filters_stopwords_and_caps(self):
        text = "Refactor the LLMPlanner with start_run, that should import cleanly"
        keywords = extract_keywords(text, limit=3)
        assert keywords == ["Refactor", "LLMPlanner", "start_run"]

    def test_snapshot_digest_is_content_addressed_and_order_stable(self):
        assert snapshot_set_digest(FILES) == snapshot_set_digest(
            dict(reversed(list(FILES.items())))
        )
        changed = dict(FILES)
        changed["README.md"] = FILES["README.md"] + "more\n"
        assert snapshot_set_digest(changed) != snapshot_set_digest(FILES)

    def test_frozen_input_digest_binds_issue_and_snapshot(self):
        digest = frozen_input_digest("issue", "snap")
        assert digest != frozen_input_digest("issue!", "snap")
        assert digest != frozen_input_digest("issue", "snap2")

    async def test_load_snapshot_files_filters_scope_and_non_blobs(self):
        reader = FakeReader(FILES)
        files = await load_snapshot_files(reader, 1, "main", allowed_globs=["src/**"])
        assert sorted(files) == ["src/app/planner.py", "src/app/service.py"]
        assert reader.text_reads == ["src/app/planner.py", "src/app/service.py"]

    async def test_load_snapshot_files_honors_the_file_cap(self):
        reader = FakeReader(FILES)
        files = await load_snapshot_files(reader, 1, "main", max_files=1)
        assert list(files) == ["README.md"]  # sorted selection, capped

    async def test_from_reader_context_runs_the_whole_stage(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        reader = FakeReader(FILES)
        try:
            ctx = DiscoveryRunContext.from_reader(
                run_id=RUN_ID,
                project_id=1,
                session_factory=factory,
                reader=reader,
                ref="main",
                repository_id="example/repo",
            )
            augmented = await maybe_run_discovery(ctx, PLANNER_INPUT)
            assert DIGEST_BEGIN in augmented
            assert reader.tree_reads == 1
            assert await _discovery_record(factory) != {}
        finally:
            await engine.dispose()
