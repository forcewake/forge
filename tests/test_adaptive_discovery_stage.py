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
  the plan, uncited claims stay allowed — and, beyond resolution, each
  citation's AUTHORITY binding: the record's own dispatch
  repository/OID plus the recorded snapshot tree bound every cited
  file:line, so cross-repo entries, stale OIDs, out-of-tree paths and
  out-of-range lines all fail closed;
- the NXT-07 answer gate: persisted clarification questions refuse
  planning with a typed :class:`QuestionsOutstanding` until every
  question id has a durably recorded answer; answers arrive through the
  ``OperatorControlService`` mailbox and unblock planning across a
  second process, with the bounded answers section injected beside the
  evidence digest;
- the NXT-08 multi-repo read leg: a context built from N EXPLICITLY
  authorized readers freezes every repo's tree under per-repo
  namespaces, records the full authorized set + per-repo OIDs in the
  dispatch, and authorizes them under ONE repo-set digest — so a repo
  added or removed is new input, an untouched repo's identical content
  is a replay, evidence from different repos coexists in one record
  (each citation validating against its OWN repo's tree; cross-repo
  path confusion fails closed), the caps apply PER repo, and the
  planner digest carries a per-repo provenance header. Single-repo
  callers stay byte-identical (``from_reader`` delegates to
  ``from_readers`` with one entry).
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from forge.adaptive.artifact_store import ContentAddressedStore
from forge.adaptive.discovery import dispatch_target
from forge.adaptive.discovery_stage import (
    ANSWERS_BEGIN,
    ANSWERS_END,
    DIGEST_BEGIN,
    DIGEST_END,
    FORGE_DISCOVERY_ENABLED_ENV,
    PLANNER_INPUT_CAP_CHARS,
    SNAPSHOT_TREE_SCHEMA,
    SNAPSHOT_TREE_SET_SCHEMA,
    _SNAPSHOT_MAX_FILES,
    CitationAuthority,
    DiscoveryRunContext,
    DiscoveryStageError,
    InvalidPlanCitation,
    QuestionsOutstanding,
    attach_answers,
    attach_digest,
    apply_answer_command,
    apply_answer_commands,
    discovery_enabled,
    discovery_open_questions,
    enforce_citations_against_snapshot,
    enforce_plan_citations,
    extract_citations,
    extract_keywords,
    evidence_ids_of,
    frozen_input_digest,
    load_snapshot_files,
    maybe_run_discovery,
    open_question_ids_of,
    open_questions_of,
    record_answer,
    record_answers,
    render_answers_section,
    render_digest_section,
    repo_set_digest,
    run_discovery_stage,
    snapshot_set_digest,
    snapshot_tree_digest,
    snapshot_tree_of,
    validate_citations_against_snapshot,
    validate_plan_citations,
)
from forge.adaptive.research_planner import FORGE_DISCOVERY_MODE_ENV
from forge.adaptive.wiring import OperatorControlService
from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, Outbox
from forge.factory.planner import PLANNER_MAX_INPUT_CHARS
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.orchestrator.project_config import clear_cache
from forge.runs.github_service import GitHubRunService
from forge.runs.stubs import StubImplementer, StubReviewer
from tests.fixtures.fake_github import FakeGitHub

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

OWN_REPO_ID = "example/repo"
OWN_OID = "f" * 40
NEIGHBOR_REPO_ID = "partner/org/neighbor"
NEIGHBOR_OID = "a" * 40

#: The NEIGHBOR repository (NXT-08): its own symbols for the same issue
#: keywords, one path that ONLY exists there (``libs/neighbor/bridge.py``)
#: and one path that exists in BOTH repos with DISAGREEING content — the
#: own repo's planner.py is 3 lines, the neighbor's declares the class at
#: line 4 of a 6-line file, so a re-bound citation trips the line bounds.
NEIGHBOR_FILES = {
    "src/app/planner.py": (
        "# the neighbor's fork of the planner\n"
        "# deliberately different, and longer,\n"
        "# so line bounds disagree with the own repo's tree.\n"
        "class LLMPlanner:\n"
        "    def plan(self, issue, ctx):\n"
        "        return ctx\n"
    ),
    "libs/neighbor/bridge.py": (
        "from app.planner import LLMPlanner\n"
        "\n"
        "def start_run():\n"
        "    planner = LLMPlanner()\n"
        "    return planner.plan(None, None)\n"
    ),
}


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


class FakeGitLabShapedReader:
    """The GitLab client's read shape: ``get_tree`` + base64 ``get_file``
    records and NO ``read_text`` — the fallback leg of the snapshot seam."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.file_reads: list[str] = []

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> list:
        return [SimpleNamespace(path=p, type="blob") for p in sorted(self._files)]

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD"):
        self.file_reads.append(file_path)
        import base64

        return SimpleNamespace(
            content=base64.b64encode(self._files[file_path].encode("utf-8")).decode("ascii"),
            encoding="base64",
        )


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
    questions=None,
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
        question_source=questions,
        **kwargs,
    )


def _digest_of(augmented: str) -> dict:
    """Parse the delimited digest section out of an augmented input."""
    body = augmented[augmented.index(DIGEST_BEGIN) + len(DIGEST_BEGIN) :]
    body = body[: body.index(DIGEST_END)]
    return json.loads(body.strip())


def _answers_of(augmented: str) -> dict:
    """Parse the delimited answers section out of an augmented input."""
    body = augmented[augmented.index(ANSWERS_BEGIN) + len(ANSWERS_BEGIN) :]
    body = body[: body.index(ANSWERS_END)]
    return json.loads(body.strip())


def _question_source(*specs: dict):
    """A deterministic question source; ``lambda:``-free for readability.

    Each spec is ``{"text", "criticality", "citations", "options"}``;
    ``citations="first-evidence"`` is replaced by the first recorded
    evidence id at emission time so tests cite REAL ids.
    """

    def _source(planner_input: str, evidence: list[dict]) -> list[dict]:
        first = evidence[0]["id"] if evidence else "ev-1"
        out = []
        for spec in specs:
            resolved = dict(spec)
            if resolved.get("citations") == "first-evidence":
                resolved["citations"] = [first]
            out.append(resolved)
        return out

    return _source


def _two_q_source():
    return _question_source(
        {
            "text": "Which database version should the migration target?",
            "criticality": "critical",
            "citations": "first-evidence",
            "options": ["postgres:15", "postgres:16"],
        },
        {
            "text": "Should start_run stay batch-driven or move to the SDK lane?",
            "criticality": "critical",
            "citations": [],
        },
    )


def _answer_command(question_id: str, text: str, *, key: str = "", run_scope: str = "") -> dict:
    """A mailbox answer command in its plain dict shape."""
    payload: dict = {"question_id": question_id, "text": text}
    if run_scope:
        payload["run_id"] = run_scope
    return {
        "command_id": f"cmd-{question_id}-{key or 'base'}",
        "kind": "answer",
        "actor_ref": "operator",
        "idempotency_key": key or f"answer:{RUN_ID}:{question_id}",
        "payload": payload,
    }


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


class TestCitationAuthorityBinding:
    """NXT-06's remaining scope: provenance VERIFIED against the record's
    OWN authorized snapshot binding, not just id existence."""

    def _bound_record(
        self,
        files: dict[str, str],
        entries: list[dict],
        *,
        repository_id: str = "example/repo",
        source_oid: str = "f" * 40,
    ) -> dict:
        """A record whose dispatch and recorded tree are mutually consistent."""
        return {
            "discovery_id": "disc-bound",
            "run_id": RUN_ID,
            "status": "complete",
            "dispatch": {
                "repository_id": repository_id,
                "source_oid": source_oid,
                "snapshot_set_digest": snapshot_set_digest(files),
            },
            "snapshot_tree": {"schema": SNAPSHOT_TREE_SCHEMA, "files": snapshot_tree_of(files)},
            "evidence": entries,
        }

    def _entry(self, evidence_id: str, path: str, line: int, **overrides) -> dict:
        entry = {
            "id": evidence_id,
            "path": path,
            "line": line,
            "kind": "symbol",
            "detail": "d",
            "repository_id": "example/repo",
            "source_oid": "f" * 40,
            "content_digest": "0" * 64,
        }
        entry.update(overrides)
        return entry

    def test_cross_repo_citation_is_invalid(self):
        files = {"src/a.py": "one\ntwo\n"}
        record = self._bound_record(files, [self._entry("ev-1", "src/a.py", 1)])
        record["evidence"][0]["repository_id"] = "other/org:other/repo"
        plan = {"steps": ["work per evidence:ev-1"]}
        violations = validate_citations_against_snapshot(plan, record)
        assert len(violations) == 1
        assert "cross-repository" in violations[0]
        assert "other/org:other/repo" in violations[0]
        with pytest.raises(InvalidPlanCitation, match="cross-repository"):
            enforce_citations_against_snapshot(plan, record)

    def test_stale_oid_citation_is_invalid(self):
        files = {"src/a.py": "one\ntwo\n"}
        record = self._bound_record(files, [self._entry("ev-1", "src/a.py", 1)])
        record["evidence"][0]["source_oid"] = "0" * 40  # a different commit
        plan = {"steps": [{"step_id": "s1", "objective": "work per evidence:ev-1"}]}
        violations = validate_citations_against_snapshot(plan, record)
        assert len(violations) == 1
        assert "stale OID" in violations[0]
        with pytest.raises(InvalidPlanCitation, match="stale OID"):
            enforce_citations_against_snapshot(plan, record)

    def test_in_range_citations_are_valid(self):
        files = {"src/a.py": "one\ntwo\nthree\n", "src/b.py": "alpha\nbeta\n"}
        record = self._bound_record(
            files,
            [
                self._entry("ev-1", "src/a.py", 1),
                self._entry("ev-2", "src/a.py", 3),  # the file's last line
                self._entry("ev-3", "src/b.py", 2),
            ],
        )
        plan = {
            "steps": [
                "first per evidence:ev-1 and evidence:ev-3",
                {
                    "step_id": "s2",
                    "objective": "last line per evidence:ev-2",
                    "evidence_refs": ["ev-2"],
                },
            ]
        }
        assert validate_citations_against_snapshot(plan, record) == []
        enforce_citations_against_snapshot(plan, record)  # does not raise

    def test_boundary_lines_are_exact(self):
        # 3 lines WITHOUT a trailing newline: splitlines counts 3.
        files = {"src/tiny.py": "alpha\nbeta\ngamma"}
        record = self._bound_record(
            files,
            [
                self._entry("ev-first", "src/tiny.py", 1),
                self._entry("ev-last", "src/tiny.py", 3),
                self._entry("ev-over", "src/tiny.py", 4),
                self._entry("ev-zero", "src/tiny.py", 0),
            ],
        )
        ok = {"steps": ["per evidence:ev-first then evidence:ev-last"]}
        assert validate_citations_against_snapshot(ok, record) == []

        over = {"steps": ["per evidence:ev-over"]}
        violations = validate_citations_against_snapshot(over, record)
        assert len(violations) == 1
        assert "line 4 is outside 'src/tiny.py''s recorded range 1..3" in violations[0]

        zero = {"steps": ["per evidence:ev-zero"]}
        violations = validate_citations_against_snapshot(zero, record)
        assert len(violations) == 1
        assert "line 0 is outside" in violations[0]

    def test_path_outside_the_recorded_tree_is_invalid(self):
        files = {"src/a.py": "one\n"}
        record = self._bound_record(files, [self._entry("ev-1", "elsewhere/x.py", 1)])
        plan = {"steps": ["work per evidence:ev-1"]}
        violations = validate_citations_against_snapshot(plan, record)
        assert len(violations) == 1
        assert "not inside the snapshot's recorded tree" in violations[0]
        with pytest.raises(InvalidPlanCitation):
            enforce_citations_against_snapshot(plan, record)

    def test_a_tree_that_does_not_hash_to_the_authorization_refuses_citations(self):
        files = {"src/a.py": "one\ntwo\n"}
        record = self._bound_record(files, [self._entry("ev-1", "src/a.py", 1)])
        record["snapshot_tree"]["files"]["src/a.py"]["digest"] = "f" * 64  # not sha256("one\n")
        assert CitationAuthority.of_record(record).consistent is False
        plan = {"steps": ["work per evidence:ev-1"]}
        violations = validate_citations_against_snapshot(plan, record)
        assert len(violations) == 1
        assert "does not hash to the dispatch's authorized snapshot_set_digest" in violations[0]
        with pytest.raises(InvalidPlanCitation):
            enforce_citations_against_snapshot(plan, record)

    def test_a_record_without_a_binding_fails_closed(self):
        record = {"evidence": [self._entry("ev-1", "src/a.py", 1)]}  # no dispatch, no tree
        plan = {"steps": ["work per evidence:ev-1"]}
        violations = validate_citations_against_snapshot(plan, record)
        assert violations == [
            "step 1: evidence 'ev-1' the record carries no dispatch repository_id to bind "
            "citations to"
        ]
        with pytest.raises(InvalidPlanCitation):
            enforce_citations_against_snapshot(plan, record)

    def test_unknown_ids_are_still_invalid_under_the_binding_validator(self):
        files = {"src/a.py": "one\n"}
        record = self._bound_record(files, [self._entry("ev-1", "src/a.py", 1)])
        plan = {"steps": ["work per evidence:ev-404"]}
        assert validate_citations_against_snapshot(plan, record) == [
            "step 1: unknown evidence id 'ev-404'"
        ]

    async def test_the_real_stage_records_a_tree_that_hashes_to_the_authorization(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            outcome = await run_discovery_stage(_ctx(factory), PLANNER_INPUT)
            record = await _discovery_record(factory)
            tree = record["snapshot_tree"]
            assert tree["schema"] == SNAPSHOT_TREE_SCHEMA
            assert set(tree["files"]) == set(FILES)
            for path, content in FILES.items():
                assert tree["files"][path]["lines"] == len(content.splitlines())
            assert snapshot_tree_digest(tree["files"]) == record["dispatch"]["snapshot_set_digest"]
            assert CitationAuthority.of_record(record).consistent is True

            # every recorded citation is valid against the record's OWN binding
            plan = {"steps": [f"work per evidence:{eid}" for eid in outcome.evidence_ids]}
            assert validate_citations_against_snapshot(plan, record) == []
            enforce_citations_against_snapshot(plan, record)  # does not raise
        finally:
            await engine.dispose()

    async def test_a_waiting_record_carries_the_binding_too(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            with pytest.raises(QuestionsOutstanding):
                await maybe_run_discovery(
                    _ctx(
                        factory,
                        questions=_question_source(
                            {"text": "which way?", "criticality": "critical", "citations": []}
                        ),
                    ),
                    PLANNER_INPUT,
                )
            record = await _discovery_record(factory)
            assert record["snapshot_tree"]["schema"] == SNAPSHOT_TREE_SCHEMA
            assert CitationAuthority.of_record(record).consistent is True
        finally:
            await engine.dispose()

    async def test_gitlab_shaped_reader_satisfies_the_snapshot_seam(self):
        reader = FakeGitLabShapedReader(FILES)
        files = await load_snapshot_files(reader, 1, "main")
        assert files == FILES  # decoded through the get_file base64 shape
        assert sorted(reader.file_reads) == sorted(FILES)


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


class TestQuestionEmission:
    """NXT-07: the stage emits bounded, citation-validated questions."""

    async def test_open_questions_refuse_planning_with_the_typed_error(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            with pytest.raises(QuestionsOutstanding) as excinfo:
                await maybe_run_discovery(_ctx(factory, questions=_two_q_source()), PLANNER_INPUT)
            error = excinfo.value
            assert isinstance(error, DiscoveryStageError)  # loud for old callers too
            assert len(error.questions) == 2
            assert all(q["question_id"].startswith("q-") for q in error.questions)
            assert error.questions[0]["criticality"] == "critical"
            assert error.questions[0]["citations"]  # the question cites its evidence
            assert error.summaries()[0].startswith("[critical] q-")
            record = await _discovery_record(factory)
            assert record["status"] == "waiting_question"
            assert [q["status"] for q in record["questions"]] == ["open", "open"]
            assert record["repository_researched"] is True  # the evidence is durable
            counts = await _outbox_counts(factory)
            assert counts.get("discovery.questions_raised") == 1
        finally:
            await engine.dispose()

    async def test_a_question_citing_unknown_evidence_is_dropped(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            source = _question_source(
                {"text": "leans on nothing", "criticality": "critical", "citations": ["ev-404"]},
                {"text": "which timeout?", "criticality": "advisory", "citations": []},
            )
            await run_discovery_stage(_ctx(factory, questions=source), PLANNER_INPUT)
            record = await _discovery_record(factory)
            texts = [q["text"] for q in record["questions"]]
            assert texts == ["which timeout?"]  # the bad-citation question never persisted
        finally:
            await engine.dispose()

    async def test_emission_is_bounded_and_deduped(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            same = {"text": "duplicated proposal", "criticality": "critical", "citations": []}
            source = _question_source(
                same,
                {"text": "second", "criticality": "critical", "citations": []},
                same,  # identical content: ONE durable identity, not two
                {"text": "third", "criticality": "advisory", "citations": []},
                {"text": "fourth — over the bound", "criticality": "critical", "citations": []},
            )
            await run_discovery_stage(_ctx(factory, questions=source), PLANNER_INPUT)
            record = await _discovery_record(factory)
            texts = [q["text"] for q in record["questions"]]
            assert texts == ["duplicated proposal", "second", "third"]
            ids = [q["question_id"] for q in record["questions"]]
            assert len(set(ids)) == 3  # durable identities are unique
        finally:
            await engine.dispose()

    async def test_a_replayed_waiting_record_does_not_re_emit_or_re_probe(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            with pytest.raises(QuestionsOutstanding):
                await maybe_run_discovery(_ctx(factory, questions=_two_q_source()), PLANNER_INPUT)
            # the second pass adopts the waiting record — even with the
            # question source absent, the durable questions persist.
            with pytest.raises(QuestionsOutstanding) as excinfo:
                await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            assert len(excinfo.value.questions) == 2
            counts = await _outbox_counts(factory)
            assert counts.get("discovery.started") == 1  # probes paid once
            assert counts.get("discovery.questions_raised") == 1  # questions emitted once
            assert counts.get("discovery.replayed") == 1
            assert counts.get("discovery.waiting") == 1
        finally:
            await engine.dispose()


class TestAnswerGate:
    """NXT-07: answers unblock planning, durably; refusals stay typed."""

    async def test_answering_q2_does_not_close_q1(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            with pytest.raises(QuestionsOutstanding) as excinfo:
                await maybe_run_discovery(_ctx(factory, questions=_two_q_source()), PLANNER_INPUT)
            q1, q2 = [q["question_id"] for q in excinfo.value.questions]

            service = OperatorControlService()
            assert await service.answer(RUN_ID, "pavel", q2, "Move it to the SDK lane.") is True
            drain = await record_answers(factory, RUN_ID, await service.pending(RUN_ID))
            assert drain.applied_count == 1

            with pytest.raises(QuestionsOutstanding) as again:
                await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            assert [q["question_id"] for q in again.value.questions] == [q1]
            record = await _discovery_record(factory)
            assert record["status"] == "waiting_question"  # not resolved by one answer
            counts = await _outbox_counts(factory)
            assert "discovery.questions_resolved" not in counts
        finally:
            await engine.dispose()

    async def test_answers_unblock_planning_durably_across_a_second_session(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        db = tmp_path / "runs.db"
        factory_a, engine_a = await _new_process(db)
        await _seed_run(factory_a)
        try:
            with pytest.raises(QuestionsOutstanding) as excinfo:
                await maybe_run_discovery(_ctx(factory_a, questions=_two_q_source()), PLANNER_INPUT)
            q1, q2 = [q["question_id"] for q in excinfo.value.questions]
        finally:
            await engine_a.dispose()

        # A brand-new process: the answers land on the DURABLE record.
        factory_b, engine_b = await _new_process(db)
        try:
            service = OperatorControlService()
            await service.answer(RUN_ID, "pavel", q2, "Move it to the SDK lane.")
            await service.answer(RUN_ID, "pavel", q1, "Target postgres:16.")
            drain = await record_answers(factory_b, RUN_ID, await service.pending(RUN_ID))
            assert drain.applied_count == 2
            assert drain.open_question_ids == ()

            record = await _discovery_record(factory_b)
            assert record["status"] == "complete"  # the flip rode the last answer's commit
            answers = {q["question_id"]: q["answer"] for q in record["questions"]}
            assert answers[q1]["text"] == "Target postgres:16."
            assert answers[q1]["actor"] == "pavel"
            counts = await _outbox_counts(factory_b)
            assert counts.get("discovery.answer_recorded") == 2
            assert counts.get("discovery.questions_resolved") == 1
            assert counts.get("discovery.started") == 1  # still one discovery, one probe pass

            # Planning resumes: digest AND answers injected, bounded.
            augmented = await maybe_run_discovery(_ctx(factory_b), PLANNER_INPUT)
            assert augmented.startswith(PLANNER_INPUT)
            assert DIGEST_BEGIN in augmented
            parsed = _answers_of(augmented)
            assert parsed["schema"] == "forge.discovery.answers/1"
            assert {a["question_id"] for a in parsed["answers"]} == {q1, q2}
            by_id = {a["question_id"]: a for a in parsed["answers"]}
            assert by_id[q1]["answer"] == "Target postgres:16."
            assert by_id[q1]["citations"]  # answers carry their question's citations
            assert len(augmented) <= PLANNER_INPUT_CAP_CHARS
            counts = await _outbox_counts(factory_b)
            assert counts.get("plan.research_mode") >= 1  # announced only once plannable
        finally:
            await engine_b.dispose()

    async def test_unanswered_questions_keep_the_typed_refusal(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            with pytest.raises(QuestionsOutstanding):
                await maybe_run_discovery(_ctx(factory, questions=_two_q_source()), PLANNER_INPUT)
            with pytest.raises(QuestionsOutstanding):
                await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)  # nothing answered
        finally:
            await engine.dispose()

    async def test_duplicate_answer_is_an_idempotent_no_op(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            with pytest.raises(QuestionsOutstanding) as excinfo:
                await maybe_run_discovery(_ctx(factory, questions=_two_q_source()), PLANNER_INPUT)
            q1 = excinfo.value.questions[0]["question_id"]

            service = OperatorControlService()
            await service.answer(RUN_ID, "pavel", q1, "Target postgres:16.")
            commands = await service.pending(RUN_ID)
            first = await record_answers(factory, RUN_ID, commands)
            assert first.applied_count == 1
            # the SAME command redelivered: duplicate, no second effect
            again = await record_answers(factory, RUN_ID, commands)
            assert again.applied_count == 0
            assert again.applications[0].status == "duplicate"
            counts = await _outbox_counts(factory)
            assert counts.get("discovery.answer_recorded") == 1
        finally:
            await engine.dispose()

    async def test_a_second_different_answer_is_rejected_first_stands(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            with pytest.raises(QuestionsOutstanding) as excinfo:
                await maybe_run_discovery(_ctx(factory, questions=_two_q_source()), PLANNER_INPUT)
            q1 = excinfo.value.questions[0]["question_id"]

            await record_answer(factory, RUN_ID, _answer_command(q1, "postgres:16"))
            raced = await record_answer(
                factory, RUN_ID, _answer_command(q1, "postgres:15", key="answer:other:key")
            )
            assert raced.status == "rejected"
            assert "first recorded answer stands" in raced.reason
            record = await _discovery_record(factory)
            assert record["questions"][0]["answer"]["text"] == "postgres:16"
        finally:
            await engine.dispose()

    async def test_foreign_unknown_and_empty_answers_have_no_effect(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            with pytest.raises(QuestionsOutstanding) as excinfo:
                await maybe_run_discovery(_ctx(factory, questions=_two_q_source()), PLANNER_INPUT)
            q1 = excinfo.value.questions[0]["question_id"]

            foreign = await record_answer(
                factory,
                RUN_ID,
                _answer_command(q1, "irrelevant", run_scope="run-someone-else"),
            )
            assert foreign.status == "rejected" and "scoped to run" in foreign.reason
            unknown = await record_answer(factory, RUN_ID, _answer_command("q-ghost", "hello"))
            assert unknown.status == "rejected" and "unknown question id" in unknown.reason
            empty = await record_answer(factory, RUN_ID, _answer_command(q1, "   "))
            assert empty.status == "rejected"
            record = await _discovery_record(factory)
            assert [q["status"] for q in record["questions"]] == ["open", "open"]
            counts = await _outbox_counts(factory)
            assert "discovery.answer_recorded" not in counts
        finally:
            await engine.dispose()

    async def test_answering_a_run_without_discovery_is_rejected_not_fatal(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            application = await record_answer(factory, RUN_ID, _answer_command("q-any", "any"))
            assert application.status == "rejected"
            assert "no persisted discovery record" in application.reason
        finally:
            await engine.dispose()


class TestAnswerHelpers:
    """The pure answer fold — the seam the /answer surface consumes."""

    def _record(self) -> dict:
        return {
            "run_id": RUN_ID,
            "discovery_id": "disc-q",
            "evidence": [{"id": "ev-1"}, {"id": "ev-2"}],
            "questions": [
                {"question_id": "q-1", "text": "Q1?", "status": "open", "answer": None},
                {"question_id": "q-2", "text": "Q2?", "status": "open", "answer": None},
            ],
        }

    def test_one_command_resolves_exactly_its_question(self):
        drain = apply_answer_command(self._record(), _answer_command("q-2", "B"))
        assert drain.status == "applied"
        assert open_question_ids_of(drain.record) == ("q-1",)  # Q1 untouched
        answered = drain.record["questions"][1]
        assert answered["status"] == "answered"
        assert answered["answer"]["actor"] == "operator"

    def test_a_non_answer_command_is_rejected(self):
        application = apply_answer_command(
            self._record(), {"kind": "pause", "payload": {}, "idempotency_key": "k"}
        )
        assert application.status == "rejected"

    def test_fold_applies_in_sequence_order(self):
        commands = [_answer_command("q-1", "A"), _answer_command("q-2", "B")]
        # a redelivery of the first command arriving last changes nothing
        result = apply_answer_commands(self._record(), [commands[1], commands[0], commands[0]])
        assert result.applied_count == 2
        assert result.open_question_ids == ()
        assert [a.status for a in result.applications] == ["applied", "applied", "duplicate"]

    def test_open_questions_read_from_the_record_shape(self):
        assert open_question_ids_of(self._record()) == ("q-1", "q-2")
        assert open_questions_of({}) == ()
        assert (
            open_questions_of({"questions": [{"question_id": "q-x", "status": "answered"}]}) == ()
        )


class TestAnswersSection:
    """The bounded, citation-validated answers injection."""

    def _answered_record(self, count: int = 3, *, long_text: bool = False) -> dict:
        return {
            "discovery_id": "disc-a",
            "evidence": [{"id": f"ev-{i}"} for i in range(1, count + 1)],
            "questions": [
                {
                    "question_id": f"q-{i}",
                    "text": f"Question {i}?" + (" very long " * 60 if long_text else ""),
                    "criticality": "critical",
                    "citations": [f"ev-{i}"],
                    "status": "answered",
                    "answer": {"text": f"Answer {i}." + (" very long " * 60 if long_text else "")},
                }
                for i in range(1, count + 1)
            ],
        }

    def test_nothing_answered_renders_nothing(self):
        assert render_answers_section({"questions": []}) == ""
        assert (
            render_answers_section(
                {"questions": [{"question_id": "q-1", "status": "open", "answer": None}]}
            )
            == ""
        )

    def test_the_section_is_delimited_deterministic_and_citation_validated(self):
        section = render_answers_section(self._answered_record())
        assert section.startswith(ANSWERS_BEGIN) and section.endswith(ANSWERS_END)
        parsed = json.loads(section[len(ANSWERS_BEGIN) + 1 : -len(ANSWERS_END) - 1])
        assert parsed["schema"] == "forge.discovery.answers/1"
        assert [a["question_id"] for a in parsed["answers"]] == ["q-1", "q-2", "q-3"]
        assert parsed["answers"][0]["citations"] == ["ev-1"]

        # a question whose citations no longer resolve is dropped, not rendered
        stale = self._answered_record()
        stale["evidence"] = [{"id": "ev-9"}]
        dropped = json.loads(
            render_answers_section(stale)[len(ANSWERS_BEGIN) + 1 : -len(ANSWERS_END) - 1]
        )
        assert dropped["answers"] == []
        assert dropped["dropped"] == 3

    def test_the_section_respects_its_budget_and_marks_truncation(self):
        section = render_answers_section(self._answered_record(long_text=True), max_chars=600)
        assert len(section) <= 600
        parsed = json.loads(section[len(ANSWERS_BEGIN) + 1 : -len(ANSWERS_END) - 1])
        assert parsed["truncated"] is True
        assert parsed["dropped"] > 0
        assert len(parsed["answers"]) < 3

    def test_attach_answers_keeps_the_planner_cap(self):
        section = f"{ANSWERS_BEGIN}\n" + ("x" * 3000) + f"\n{ANSWERS_END}"
        base = "issue text " * 900  # long enough to crowd the section out
        combined = attach_answers(base, section)
        assert len(combined) <= PLANNER_INPUT_CAP_CHARS
        assert combined.endswith(section)  # the section is never the thing cut
        assert "input truncated to" in combined

    def test_attach_answers_short_base_has_no_marker(self):
        section = f"{ANSWERS_BEGIN}\n{{}}\n{ANSWERS_END}"
        assert attach_answers("short issue", section) == f"short issue\n\n{section}"


def _multi_ctx(
    factory: async_sessionmaker[AsyncSession],
    *,
    neighbor_files: dict[str, str] | None = None,
    neighbor_globs: list[str] | None = None,
) -> DiscoveryRunContext:
    """A context over TWO explicitly authorized repos: own + neighbor."""
    return DiscoveryRunContext.from_readers(
        {"own": FakeReader(FILES), "neighbor": FakeReader(neighbor_files or NEIGHBOR_FILES)},
        {"own": OWN_REPO_ID, "neighbor": NEIGHBOR_REPO_ID},
        run_id=RUN_ID,
        project_id=1,
        session_factory=factory,
        refs={"own": OWN_OID, "neighbor": NEIGHBOR_OID},
        allowed_globs={"neighbor": neighbor_globs} if neighbor_globs is not None else None,
    )


def _own_only_ctx(factory: async_sessionmaker[AsyncSession]) -> DiscoveryRunContext:
    """The same own repo, ALONE — from_readers with one entry."""
    return DiscoveryRunContext.from_readers(
        {"own": FakeReader(FILES)},
        {"own": OWN_REPO_ID},
        run_id=RUN_ID,
        project_id=1,
        session_factory=factory,
        refs={"own": OWN_OID},
    )


class TestMultiRepoDiscovery:
    """NXT-08: discovery reads NEIGHBOR repositories — N authorized
    read-only repos, one durable record, per-repo citation authority."""

    async def test_two_repos_evidence_coexist_in_one_record_with_provenance(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            augmented = await maybe_run_discovery(_multi_ctx(factory), PLANNER_INPUT)
            record = await _discovery_record(factory)
            assert record["status"] == "complete"

            # BOTH repos' evidence in ONE record, unique ids, each entry
            # bound to the repo it was found in.
            entries = record["evidence"]
            by_repo = {e["repository_id"] for e in entries}
            assert by_repo == {OWN_REPO_ID, NEIGHBOR_REPO_ID}
            ids = [e["id"] for e in entries]
            assert len(set(ids)) == len(ids)
            for entry in entries:
                if entry["repository_id"] == NEIGHBOR_REPO_ID:
                    assert entry["source_oid"] == NEIGHBOR_OID
                    assert entry["path"] in NEIGHBOR_FILES
                else:
                    assert entry["source_oid"] == OWN_OID
                    assert entry["path"] in FILES

            # the dispatch carries the FULL authorized set + per-repo
            # OIDs/digests, under one repo-set authorization digest.
            dispatch = record["dispatch"]
            assert dispatch["repository_id"] == OWN_REPO_ID  # the own repo stays primary
            assert dispatch["snapshot_set_digest"] == repo_set_digest(
                {"own": FILES, "neighbor": NEIGHBOR_FILES}
            )
            assert dispatch["repositories"]["neighbor"] == {
                "repository_id": NEIGHBOR_REPO_ID,
                "source_oid": NEIGHBOR_OID,
                "snapshot_set_digest": snapshot_set_digest(NEIGHBOR_FILES),
            }
            assert dispatch["repositories"]["own"]["snapshot_set_digest"] == snapshot_set_digest(
                FILES
            )

            # every repo's tree is recorded under its own namespace and
            # hashes back to its OWN dispatch digest.
            tree = record["snapshot_tree"]
            assert tree["schema"] == SNAPSHOT_TREE_SET_SCHEMA
            assert set(tree["repos"]) == {"own", "neighbor"}
            for key, files in (("own", FILES), ("neighbor", NEIGHBOR_FILES)):
                assert set(tree["repos"][key]["files"]) == set(files)
                assert (
                    snapshot_tree_digest(tree["repos"][key]["files"])
                    == dispatch["repositories"][key]["snapshot_set_digest"]
                )
            assert CitationAuthority.of_record(record).consistent is True

            # a plan citing evidence from BOTH repos validates in one pass
            plan = {"steps": [f"work per evidence:{eid}" for eid in ids]}
            assert validate_citations_against_snapshot(plan, record) == []
            enforce_citations_against_snapshot(plan, record)  # does not raise

            # the planner digest says WHICH repo each citation came from
            digest = _digest_of(augmented)
            assert digest["repositories"] == {
                "own": {"repository_id": OWN_REPO_ID, "source_oid": OWN_OID},
                "neighbor": {"repository_id": NEIGHBOR_REPO_ID, "source_oid": NEIGHBOR_OID},
            }
            assert {entry["repository_id"] for entry in digest["evidence"]} == by_repo
        finally:
            await engine.dispose()

    async def test_a_citation_resolves_against_its_own_repos_tree_only(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            await run_discovery_stage(_multi_ctx(factory), PLANNER_INPUT)
            record = await _discovery_record(factory)

            def _forged() -> dict:
                return json.loads(json.dumps(record))

            # a path that ONLY exists in the neighbor, re-bound to the own
            # repo (id AND oid, to isolate path confusion): fails closed.
            bridge_id = next(
                e["id"] for e in record["evidence"] if e["path"] == "libs/neighbor/bridge.py"
            )
            path_forged = _forged()
            entry = next(e for e in path_forged["evidence"] if e["id"] == bridge_id)
            entry.update(repository_id=OWN_REPO_ID, source_oid=OWN_OID)
            plan = {"steps": [f"work per evidence:{bridge_id}"]}
            violations = validate_citations_against_snapshot(plan, path_forged)
            assert len(violations) == 1
            assert "not inside the snapshot's recorded tree" in violations[0]
            with pytest.raises(InvalidPlanCitation):
                enforce_citations_against_snapshot(plan, path_forged)

            # the SAME relative path in both repos: the neighbor's planner
            # hit (line 4) re-bound to the own repo's 3-line tree trips
            # the line bounds — the trees are not interchangeable.
            neighbor_planner = next(
                e
                for e in record["evidence"]
                if e["path"] == "src/app/planner.py" and e["repository_id"] == NEIGHBOR_REPO_ID
            )
            assert neighbor_planner["line"] == 4  # the neighbor's fork declares at line 4
            line_forged = _forged()
            entry = next(e for e in line_forged["evidence"] if e["id"] == neighbor_planner["id"])
            entry.update(repository_id=OWN_REPO_ID, source_oid=OWN_OID)
            plan = {"steps": [f"work per evidence:{neighbor_planner['id']}"]}
            violations = validate_citations_against_snapshot(plan, line_forged)
            assert len(violations) == 1
            assert "line 4 is outside 'src/app/planner.py''s recorded range 1..3" in violations[0]

            # a repository nobody authorized: refused by name.
            ghost_forged = _forged()
            entry = next(e for e in ghost_forged["evidence"] if e["id"] == bridge_id)
            entry["repository_id"] = "ghost/org/repo"
            plan = {"steps": [f"work per evidence:{bridge_id}"]}
            violations = validate_citations_against_snapshot(plan, ghost_forged)
            assert len(violations) == 1
            assert "not one of this discovery's authorized repositories" in violations[0]
            assert NEIGHBOR_REPO_ID in violations[0]

            # the UNTOUCHED record still validates both repos together.
            plan = {"steps": [f"work per evidence:{bridge_id}"]}
            assert validate_citations_against_snapshot(plan, record) == []
        finally:
            await engine.dispose()

    async def test_a_tampered_per_repo_tree_refuses_every_citation(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            await run_discovery_stage(_multi_ctx(factory), PLANNER_INPUT)
            record = await _discovery_record(factory)
            record["snapshot_tree"]["repos"]["neighbor"]["files"]["libs/neighbor/bridge.py"][
                "digest"
            ] = "0" * 64
            assert CitationAuthority.of_record(record).consistent is False
            any_id = record["evidence"][0]["id"]
            plan = {"steps": [f"work per evidence:{any_id}"]}
            violations = validate_citations_against_snapshot(plan, record)
            assert len(violations) == 1
            assert "authorized repository-set digest" in violations[0]
            with pytest.raises(InvalidPlanCitation):
                enforce_citations_against_snapshot(plan, record)
        finally:
            await engine.dispose()

    def test_repo_set_digest_covers_the_set_and_each_repos_content(self):
        own = {"src/a.py": "one\n"}
        neighbor = {"src/b.py": "two\n"}
        both = {"own": own, "neighbor": neighbor}
        assert repo_set_digest(both) == repo_set_digest(
            {"neighbor": neighbor, "own": own}
        )  # order-stable
        assert repo_set_digest(both) != repo_set_digest({"own": own})  # a repo REMOVED moves it
        assert repo_set_digest(both) != repo_set_digest(
            {"own": own, "renamed": neighbor}
        )  # the namespace is part of the authorization
        changed = dict(neighbor)
        changed["src/b.py"] = "two!\n"
        assert repo_set_digest(both) != repo_set_digest(
            {"own": own, "neighbor": changed}
        )  # a content change in ANY repo moves it
        assert repo_set_digest(both) == repo_set_digest(
            {"own": dict(own), "neighbor": dict(neighbor)}
        )  # identical content, identical digest

    async def test_identity_moves_with_the_repo_set_not_with_identical_content(
        self, tmp_path, monkeypatch
    ):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            first = await maybe_run_discovery(_multi_ctx(factory), PLANNER_INPUT)
            record_a = await _discovery_record(factory)

            # SAME authorized set, fresh readers, identical content: a
            # REPLAY — the untouched repos keep the digest stable.
            second = await maybe_run_discovery(_multi_ctx(factory), PLANNER_INPUT)
            record_b = await _discovery_record(factory)
            assert second == first  # byte for byte, same discovery id inside
            assert record_b["discovery_id"] == record_a["discovery_id"]
            assert record_b["replay_count"] == 1

            # a repo REMOVED from the authorized set: the own repo's
            # content is untouched and identical, yet the authorization
            # digest moved — new input, a NEW discovery that supersedes.
            await maybe_run_discovery(_own_only_ctx(factory), PLANNER_INPUT)
            record_c = await _discovery_record(factory)
            assert record_c["discovery_id"] != record_a["discovery_id"]
            assert record_c["supersedes"] == record_a["discovery_id"]
            counts = await _outbox_counts(factory)
            assert counts.get("discovery.started") == 2  # probes re-paid for the new set
            assert counts.get("discovery.replayed") == 1
        finally:
            await engine.dispose()

    async def test_snapshot_caps_apply_per_repo(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            big = {f"big/src/mod_{i:03d}.py": f"# module {i}\n" for i in range(250)}
            await maybe_run_discovery(_multi_ctx(factory, neighbor_files=big), PLANNER_INPUT)
            record = await _discovery_record(factory)
            trees = record["snapshot_tree"]["repos"]
            # the oversized neighbor is capped at its OWN file cap...
            assert len(trees["neighbor"]["files"]) == _SNAPSHOT_MAX_FILES
            # ...and the own repo is NOT crowded out by it: its full
            # tree froze under the same caps, independently.
            assert set(trees["own"]["files"]) == set(FILES)
        finally:
            await engine.dispose()

    async def test_per_repo_allowed_globs_scope_their_own_repo(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            await maybe_run_discovery(
                _multi_ctx(factory, neighbor_globs=["libs/**"]), PLANNER_INPUT
            )
            record = await _discovery_record(factory)
            trees = record["snapshot_tree"]["repos"]
            assert set(trees["neighbor"]["files"]) == {"libs/neighbor/bridge.py"}
            assert set(trees["own"]["files"]) == set(FILES)  # the own repo keeps its scope
            neighbor_paths = {
                e["path"] for e in record["evidence"] if e["repository_id"] == NEIGHBOR_REPO_ID
            }
            assert neighbor_paths == {"libs/neighbor/bridge.py"}  # evidence respects it too
        finally:
            await engine.dispose()

    async def test_single_repo_callers_stay_byte_identical(self, tmp_path, monkeypatch):
        _enabled(monkeypatch)
        factory, engine = await _new_process(tmp_path / "runs.db")
        await _seed_run(factory)
        try:
            # 1. the legacy direct construction (pre-NXT-08 shapes).
            legacy = await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            record = await _discovery_record(factory)
            assert "repositories" not in record["dispatch"]
            assert record["snapshot_tree"]["schema"] == SNAPSHOT_TREE_SCHEMA
            assert "repos" not in record["snapshot_tree"]
            digest = _digest_of(legacy)
            assert "repositories" not in digest
            assert all("repository_id" not in e for e in digest["evidence"])
            assert CitationAuthority.of_record(record).repos == {}

            # 2. from_reader — which now DELEGATES to from_readers with
            #    one entry — replays the SAME discovery, byte for byte.
            via_from_reader = await maybe_run_discovery(
                DiscoveryRunContext.from_reader(
                    run_id=RUN_ID,
                    project_id=1,
                    session_factory=factory,
                    reader=FakeReader(FILES),
                    ref=OWN_OID,
                    repository_id=OWN_REPO_ID,
                ),
                PLANNER_INPUT,
            )
            assert via_from_reader == legacy

            # 3. from_readers with ONE entry: identical again — a single
            #    namespace is not a multi-repo record.
            via_from_readers = await maybe_run_discovery(_own_only_ctx(factory), PLANNER_INPUT)
            assert via_from_readers == legacy

            record = await _discovery_record(factory)
            assert record["replay_count"] == 2  # both replays adopted the legacy record
            counts = await _outbox_counts(factory)
            assert counts.get("discovery.started") == 1
            assert counts.get("discovery.replayed") == 2
        finally:
            await engine.dispose()

    def test_from_readers_validates_the_authorized_set(self):
        kwargs: dict = {"run_id": RUN_ID, "project_id": 1, "session_factory": None}
        with pytest.raises(ValueError, match="at least one reader"):
            DiscoveryRunContext.from_readers({}, {}, **kwargs)
        with pytest.raises(ValueError, match="missing entries"):
            DiscoveryRunContext.from_readers({"own": object()}, {}, **kwargs)
        with pytest.raises(ValueError, match="with no reader"):
            DiscoveryRunContext.from_readers(
                {"own": object()}, {"own": "a/b", "ghost": "x/y"}, **kwargs
            )


# ---------------------------------------------------------------------------
# R28-16: the mode gate — three honest modes through the planning seam
# ---------------------------------------------------------------------------


class TestDiscoveryModeGate:
    """``FORGE_DISCOVERY_MODE`` resolves none | lexical | research-harness.

    ``none`` is byte-identical to the disabled stage even when the legacy
    flag is ON (an explicit OFF wins); an empty value defers to the legacy
    flag so existing deployments are unchanged; unknown values fail CLOSED.
    """

    async def test_explicit_none_mode_wins_over_the_legacy_flag(self, tmp_path):
        factory, _engine = await _new_process(tmp_path / "mode-none.db")
        await _seed_run(factory)
        with patch.dict(os.environ, {FORGE_DISCOVERY_MODE_ENV: "none"}, clear=False):
            os.environ[FORGE_DISCOVERY_ENABLED_ENV] = "1"
            try:
                augmented = await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            finally:
                os.environ.pop(FORGE_DISCOVERY_ENABLED_ENV, None)
        assert augmented == PLANNER_INPUT  # untouched, byte for byte
        assert await _discovery_record(factory) == {}  # nothing persisted

    async def test_empty_mode_defers_to_the_legacy_flag(self, tmp_path):
        factory, _engine = await _new_process(tmp_path / "mode-empty.db")
        await _seed_run(factory)
        with patch.dict(os.environ, {FORGE_DISCOVERY_MODE_ENV: ""}, clear=False):
            os.environ[FORGE_DISCOVERY_ENABLED_ENV] = "1"
            try:
                augmented = await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
            finally:
                os.environ.pop(FORGE_DISCOVERY_ENABLED_ENV, None)
        assert DIGEST_BEGIN in augmented
        assert (await _discovery_record(factory)).get("status") == "complete"

    async def test_unknown_mode_fails_closed_to_no_discovery(self, tmp_path):
        factory, _engine = await _new_process(tmp_path / "mode-unknown.db")
        await _seed_run(factory)
        with patch.dict(os.environ, {FORGE_DISCOVERY_MODE_ENV: "agentic"}, clear=False):
            augmented = await maybe_run_discovery(_ctx(factory), PLANNER_INPUT)
        assert augmented == PLANNER_INPUT
        assert await _discovery_record(factory) == {}


# ---------------------------------------------------------------------------
# R28-17: the ingress-side open-question reader
# ---------------------------------------------------------------------------


class TestDiscoveryOpenQuestions:
    async def test_reader_reports_the_durable_open_ids(self, tmp_path):
        factory, _engine = await _new_process(tmp_path / "open-q.db")
        await _seed_run(factory)

        def questions(planner_input, evidence_docs):
            return [
                {"text": "First question?", "criticality": "critical", "citations": []},
                {"text": "Second question?", "criticality": "advisory", "citations": []},
            ]

        with patch.dict(os.environ, {FORGE_DISCOVERY_MODE_ENV: "lexical"}):
            with pytest.raises(QuestionsOutstanding):
                await maybe_run_discovery(_ctx(factory, questions=questions), PLANNER_INPUT)
        record = await _discovery_record(factory)
        q1, q2 = open_question_ids_of(record)
        assert await discovery_open_questions(factory, RUN_ID) == (q1, q2)
        # Answering exactly Q1 leaves Q2 open — the reader reports it.
        await record_answers(
            factory,
            RUN_ID,
            [
                {
                    "kind": "answer",
                    "payload": {"question_id": q1, "text": "yes"},
                    "idempotency_key": "k1",
                }
            ],
        )
        assert await discovery_open_questions(factory, RUN_ID) == (q2,)
        await record_answers(
            factory,
            RUN_ID,
            [
                {
                    "kind": "answer",
                    "payload": {"question_id": q2, "text": "no"},
                    "idempotency_key": "k2",
                }
            ],
        )
        assert await discovery_open_questions(factory, RUN_ID) == ()

    async def test_reader_is_empty_without_a_record_and_loud_without_a_run(self, tmp_path):
        factory, _engine = await _new_process(tmp_path / "open-q-none.db")
        await _seed_run(factory)
        assert await discovery_open_questions(factory, RUN_ID) == ()
        with pytest.raises(DiscoveryStageError):
            await discovery_open_questions(factory, "run-that-never-was")


# ---------------------------------------------------------------------------
# R28-17: question-to-plan resumption through the ACTUAL GitHub ingress
# ---------------------------------------------------------------------------


GH_REPO = "acme/acme-widget"
GH_PROJECT_ID = 70010
GH_ISSUE = 42
GH_ISSUE_TITLE = "Refactor the planner"
GH_ISSUE_DESC = "Refactor the LLMPlanner so plan() cites evidence and start_run stays stable."
GH_BASE_HEAD = "1" * 40


class CapturingPlanner:
    """Records every description the planner received; returns a plan."""

    def __init__(self) -> None:
        self.descriptions: list[str] = []

    async def plan(self, issue_title, issue_description, *, flow_run_id=None, path_scope=None):
        self.descriptions.append(issue_description)
        return "## Implementation plan\n\n- Inspect, then implement."


def _two_questions(planner_input, evidence_docs):
    """A question source proposing TWO questions citing real evidence."""
    first = evidence_docs[0]["id"] if evidence_docs else ""
    return [
        {"text": "Keep the current return shape?", "criticality": "critical", "citations": [first]},
        {"text": "Is the docs tree authoritative?", "criticality": "advisory", "citations": []},
    ]


class TestQuestionResumptionIngress:
    """The full question-to-plan loop through ``GitHubRunService``:

    plan → ``QuestionsOutstanding`` → the run parks ``blocked(waiting_question)``
    (a WAIT, visibly asked) → ``/answer`` records answers in the control
    mailbox → the recovery pass drains them into the durable discovery
    record → the run re-enters planning through the fenced plan-restart
    edge and reaches ``waiting_approval`` with the ANSWERS section
    injected — without repeating the probes.
    """

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        clear_cache()
        yield
        clear_cache()

    @pytest.fixture()
    async def db(self, tmp_path):
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path}/q-resume.db",
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()

    @pytest.fixture()
    def fake(self) -> FakeGitHub:
        github = FakeGitHub()
        github.seed_repo(GH_REPO, dict(FILES))
        github.heads[GH_REPO]["main"] = GH_BASE_HEAD
        github.seed_issue(GH_REPO, GH_ISSUE, GH_ISSUE_TITLE, GH_ISSUE_DESC)
        return github

    @staticmethod
    def _service(db, fake, *, planner, control) -> GitHubRunService:
        flow = GitHubPublishFlow(fake, proposer=StubImplementer(), base_branch="main")
        stack = GitHubAgents(
            client=fake,
            reader=fake,
            planner=planner,
            implementer=StubImplementer(),
            reviewer=StubReviewer(),
            flow=flow,
        )
        settings_values = dict(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("whsec"),
            FORGE_APPROVERS="alice",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            FORGE_GITHUB_HARNESS_WORKFLOW="",
        )
        return GitHubRunService(
            db,
            Settings(**settings_values),
            ForgeConfig(),
            stack=stack,
            repo_full_name=GH_REPO,
            control=control,
        )

    async def test_park_answer_resume_through_the_real_ingress(self, db, fake, monkeypatch):
        from forge.durable.controller import FlowStatus

        monkeypatch.setenv(FORGE_DISCOVERY_ENABLED_ENV, "1")
        planner = CapturingPlanner()
        control = OperatorControlService()
        question_calls = []

        def questions(planner_input, evidence_docs):
            question_calls.append(planner_input)
            return _two_questions(planner_input, evidence_docs)

        # Patch the planning path's context construction to carry the
        # question source (the production splice wires it the same way).
        from forge.runs import github_service as service_module

        original_from_reader = service_module.DiscoveryRunContext.from_reader

        def from_reader(*args, **kwargs):
            kwargs["question_source"] = None  # replaced below via ctx wrapper
            return original_from_reader(*args, **kwargs)

        real_maybe = service_module.maybe_run_discovery

        async def maybe_with_questions(run_ctx, planner_input):
            # Same seam the production splice uses, plus the question source.
            ctx = replace(run_ctx, question_source=questions)
            return await real_maybe(ctx, planner_input)

        service_module.maybe_run_discovery = maybe_with_questions
        try:
            service = self._service(db, fake, planner=planner, control=control)
            run_id = await service.start_run(
                project_id=GH_PROJECT_ID,
                issue_number=GH_ISSUE,
                issue_title=GH_ISSUE_TITLE,
                issue_description=GH_ISSUE_DESC,
                author_username="alice",
            )
        finally:
            service_module.maybe_run_discovery = real_maybe

        # 1. The WAIT: parked, visibly asked, planner never called.
        run = await get_run_row(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("waiting_question:")
        assert planner.descriptions == []
        bodies = [call[1][3] for call in fake.calls_of("create_issue_comment")]
        assert any("waiting for clarification" in body for body in bodies)
        record = dict((run.evidence or {}).get("discovery") or {})
        q1, q2 = open_question_ids_of(record)

        # 2. Answering Q1 only does NOT close Q2 — the run stays parked.
        await control.answer(run_id, "alice", q1, "Yes, keep it.", run_id=run_id)
        resumed = await service.evaluate_question_recovery()
        assert resumed == 0
        run = await get_run_row(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        record = dict((run.evidence or {}).get("discovery") or {})
        assert dict((record.get("questions") or [])[1]).get("status") == "open"

        # 3. Answering Q2 resumes: replan with the ANSWERS section injected.
        tree_reads_before = len(fake.calls_of("get_tree"))
        await control.answer(run_id, "alice", q2, "No, generated docs are noise.", run_id=run_id)
        resumed = await service.evaluate_question_recovery()
        assert resumed == 1
        run = await get_run_row(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert planner.descriptions, "planning re-entered"
        assert ANSWERS_BEGIN in planner.descriptions[0]
        assert "Yes, keep it." in planner.descriptions[0]
        assert DIGEST_BEGIN in planner.descriptions[0]
        record = dict((run.evidence or {}).get("discovery") or {})
        assert record["status"] == "complete"
        assert record["replay_count"] == 1  # adopted, not re-dispatched
        assert len(question_calls) == 1  # the probes/questions were not re-paid
        assert len(fake.calls_of("get_tree")) == tree_reads_before + 1  # one snapshot re-load only
        bodies = [call[1][3] for call in fake.calls_of("create_issue_comment")]
        assert any("**resumed**" in body for body in bodies)

    async def test_recovery_pass_leaves_unanswered_runs_parked(self, db, fake, monkeypatch):
        from forge.durable.controller import FlowStatus

        monkeypatch.setenv(FORGE_DISCOVERY_ENABLED_ENV, "1")
        planner = CapturingPlanner()
        control = OperatorControlService()

        def questions(planner_input, evidence_docs):
            return _two_questions(planner_input, evidence_docs)

        from forge.runs import github_service as service_module

        real_maybe = service_module.maybe_run_discovery

        async def maybe_with_questions(run_ctx, planner_input):
            return await real_maybe(replace(run_ctx, question_source=questions), planner_input)

        service_module.maybe_run_discovery = maybe_with_questions
        try:
            service = self._service(db, fake, planner=planner, control=control)
            run_id = await service.start_run(
                project_id=GH_PROJECT_ID,
                issue_number=GH_ISSUE,
                issue_title=GH_ISSUE_TITLE,
                issue_description=GH_ISSUE_DESC,
                author_username="alice",
            )
        finally:
            service_module.maybe_run_discovery = real_maybe

        # No answers at all: the pass drains nothing, resumes nothing.
        assert await service.evaluate_question_recovery() == 0
        run = await get_run_row(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("waiting_question:")
        assert planner.descriptions == []


async def get_run_row(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


# ---------------------------------------------------------------------------
# NEXT-07 — the research harness bound in the ACTUAL planning composition
# root (review ccab247 §3, Finding 1)
# ---------------------------------------------------------------------------


class BudgetedFakeLLM:
    """The ``LLMClient`` surface for the composition root.

    Enforces the SAME guard contract the real client does — reserve before
    answering (a refusal raises ``LLMError("budget_exhausted")`` with no
    response spent), reconcile the actual usage after — and answers from
    per-role scripted queues, so the test sees exactly which role consumed
    which completion of the run's budget.
    """

    def __init__(self, *, research: list[str], planner: list[str]) -> None:
        self._queues: dict[str, list[str]] = {"research": list(research), "planner": list(planner)}
        self._budget = None
        self.calls: list[dict] = []
        self.refusals = 0

    def set_budget(self, budget) -> None:
        self._budget = budget

    async def complete(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        role: str,
        flow_run_id: str | None,
        json_mode: bool = False,
        max_tokens: int = 4096,
    ):
        from forge.factory.llm import LLMError

        self.calls.append(
            {
                "tier": tier,
                "role": role,
                "user": user,
                "flow_run_id": flow_run_id,
                "max_tokens": max_tokens,
            }
        )
        reservation = None
        if self._budget is not None:
            reservation = await self._budget.reserve(calls=1, tokens=max_tokens)
            if reservation is None:
                self.refusals += 1
                raise LLMError("budget_exhausted")
        text = self._queues[role].pop(0)
        if reservation is not None:
            await self._budget.reconcile(reservation, actual_calls=1, actual_tokens=200)
        return SimpleNamespace(text=text, input_tokens=120, output_tokens=80)


def _research_proposal(tool: str, **args: str) -> str:
    return json.dumps({"calls": [{"tool": tool, "repo": "own", "args": args}], "done": False})


def _research_done() -> str:
    return json.dumps(
        {
            "done": True,
            "summary": "The planner class is declared at src/app/planner.py:1.",
            "assumptions": [],
            "contradictions": [],
        }
    )


def _plan_json() -> str:
    return json.dumps(
        {
            "summary": "Refactor the planner to cite evidence.",
            "steps": ["Read src/app/planner.py.", "Add citation rendering."],
            "risks": [],
            "files_hint": ["src/app/planner.py"],
        }
    )


class TestResearchCompositionRoot:
    """NEXT-07: ``FORGE_DISCOVERY_MODE=research-harness`` reaches a REAL
    bounded research loop through the production planning composition —
    ``GitHubRunService.start_run`` constructs the harness from the run's
    budget-guarded planner client (never a hand-built DiscoveryRunContext),
    every research completion charges the SAME run budget as planning, and
    the plan's evidence carries the research findings."""

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        clear_cache()
        yield
        clear_cache()

    @pytest.fixture()
    async def db(self):
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()

    @pytest.fixture()
    def fake(self) -> FakeGitHub:
        github = FakeGitHub()
        github.seed_repo(GH_REPO, dict(FILES))
        github.heads[GH_REPO]["main"] = GH_BASE_HEAD
        github.seed_issue(GH_REPO, GH_ISSUE, GH_ISSUE_TITLE, GH_ISSUE_DESC)
        return github

    @staticmethod
    def _service(db, fake, *, planner, profiles: str) -> GitHubRunService:
        flow = GitHubPublishFlow(fake, proposer=StubImplementer(), base_branch="main")
        stack = GitHubAgents(
            client=fake,
            reader=fake,
            planner=planner,
            implementer=StubImplementer(),
            reviewer=StubReviewer(),
            flow=flow,
        )
        settings_values = dict(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("whsec"),
            FORGE_APPROVERS="alice",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            FORGE_GITHUB_HARNESS_WORKFLOW="",
            FORGE_BUDGET_PROFILES=profiles,
        )
        return GitHubRunService(
            db,
            Settings(**settings_values),
            ForgeConfig(),
            stack=stack,
            repo_full_name=GH_REPO,
        )

    async def test_research_harness_bound_through_the_real_planning_leg(
        self, db, fake, monkeypatch
    ):
        from forge.durable.controller import FlowStatus
        from forge.factory.planner import LLMPlanner, PLANNER_TIER

        monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "research-harness")
        llm = BudgetedFakeLLM(
            research=[
                _research_proposal("find_symbol", name="LLMPlanner"),
                _research_done(),
            ],
            planner=[_plan_json()],
        )
        service = self._service(
            db,
            fake,
            planner=LLMPlanner(llm),
            profiles='{"standard": {"max_calls": 40, "max_tokens": 500000, "wallclock_s": 3600}}',
        )

        run_id = await service.start_run(
            project_id=GH_PROJECT_ID,
            issue_number=GH_ISSUE,
            issue_title=GH_ISSUE_TITLE,
            issue_description=GH_ISSUE_DESC,
            author_username="alice",
        )

        # The run planned and reached the human gate.
        run = await get_run_row(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value

        # 1. The harness was composed from the planner's OWN client and
        #    drove the loop: research-role completions on the planning
        #    tier, charged to THIS run.
        research_calls = [call for call in llm.calls if call["role"] == "research"]
        assert len(research_calls) == 2
        assert all(call["tier"] == PLANNER_TIER for call in research_calls)
        assert all(call["flow_run_id"] == run_id for call in research_calls)

        # 2. Every completion — research AND planning — charged the SAME
        #    run budget (2 research + 1 planning, reconciled to actuals).
        from forge.durable.budgets import budget_for_run

        async with db() as session:
            budget = await budget_for_run(session, run_id)
        assert budget is not None
        assert budget.consumed_calls == 3
        assert budget.unresolved_calls == 0
        assert budget.consumed_tokens == 600
        assert llm.refusals == 0

        # 3. The durable record carries the research leg and its findings
        #    joined the evidence as ordinary, citable records.
        record = dict((run.evidence or {}).get("discovery") or {})
        assert record["status"] == "complete"
        research = dict(record["research"])
        assert research["complete"] is True
        assert research["calls_executed"] == 1
        assert research["observations"] == {"count": 1, "truncated": 0, "errors": 0}
        research_entry = next(e for e in record["evidence"] if e["kind"] == "research_symbol")
        assert research_entry["path"] == "src/app/planner.py"
        assert research["findings"][0]["evidence_id"] == research_entry["id"]

        # 4. The planner planned OVER the research: its input carried the
        #    research section beside the evidence digest.
        planner_call = next(call for call in llm.calls if call["role"] == "planner")
        from forge.adaptive.research_planner import RESEARCH_BEGIN

        assert RESEARCH_BEGIN in planner_call["user"]
        assert DIGEST_BEGIN in planner_call["user"]

    async def test_a_refused_reservation_is_an_honest_partial_stop(self, db, fake, monkeypatch):
        """The harness reuses the run's BudgetGuard: when the budget is
        spent, the research loop stops with an explicit gateway_error
        partial (findings kept), and planning itself then fails honestly —
        never a silent bypass or an unbounded loop."""
        from forge.durable.controller import FlowStatus
        from forge.factory.planner import LLMPlanner

        monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "research-harness")
        llm = BudgetedFakeLLM(
            research=[
                _research_proposal("find_symbol", name="LLMPlanner"),
                _research_proposal("find_symbol", name="start_run"),
                _research_proposal("find_symbol", name="never_reached"),
            ],
            planner=[_plan_json()],
        )
        service = self._service(
            db,
            fake,
            planner=LLMPlanner(llm),
            profiles='{"standard": {"max_calls": 2, "max_tokens": 500000, "wallclock_s": 3600}}',
        )

        from forge.factory.llm import LLMError

        with pytest.raises(LLMError, match="budget_exhausted"):
            await service.start_run(
                project_id=GH_PROJECT_ID,
                issue_number=GH_ISSUE,
                issue_title=GH_ISSUE_TITLE,
                issue_description=GH_ISSUE_DESC,
                author_username="alice",
            )

        # Two completions fit the budget; the third reserve (and planning's
        # own) was refused — the loop stopped honestly on the first refusal.
        assert llm.refusals == 2
        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
        research = dict((run.evidence or {}).get("discovery") or {})
        research_doc = dict(research.get("research") or {})
        assert research_doc["complete"] is False
        assert research_doc["stopped_reason"] == "gateway_error: LLMError"
        assert research_doc["calls_executed"] == 2  # the paid findings were kept

        from forge.durable.budgets import budget_for_run

        async with db() as session:
            budget = await budget_for_run(session, run.id)
        assert budget.consumed_calls == 2

        assert run.status == FlowStatus.BLOCKED.value  # fatal: parked, never silent
        assert "budget_exhausted" in (run.status_reason or "")

    async def test_research_mode_without_an_llm_client_refuses_loudly(self, db, fake, monkeypatch):
        """A stack whose planner carries no LLM client (the stub) cannot
        compose the harness — the stage's configuration refusal parks the
        run visibly; the mode never silently downgrades to lexical-only."""
        from forge.durable.controller import FlowStatus
        from forge.runs.stubs import StubPlanner

        monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "research-harness")
        service = self._service(
            db,
            fake,
            planner=StubPlanner(),
            profiles='{"standard": {"max_calls": 40, "max_tokens": 500000, "wallclock_s": 3600}}',
        )

        with pytest.raises(DiscoveryStageError, match="without a configured research"):
            await service.start_run(
                project_id=GH_PROJECT_ID,
                issue_number=GH_ISSUE,
                issue_title=GH_ISSUE_TITLE,
                issue_description=GH_ISSUE_DESC,
                author_username="alice",
            )

        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
        assert run.status == FlowStatus.BLOCKED.value  # fatal: parked, never silent
        assert "planning_failed" in (run.status_reason or "")
        record = dict((run.evidence or {}).get("discovery") or {})
        assert record["status"] == "failed"
        assert "research" in (record.get("block_reason") or "")
