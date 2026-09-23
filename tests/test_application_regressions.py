"""Composed application regressions (review ccab247, NEXT-25; R32-17 wave).

The reviewer's APPLICATION_REGRESSIONS_EN.md specified 12 traces that
must drive the REAL service surfaces — production constructors, HTTP
routes and provider adapters — with bounded typed doubles ONLY at
external-effect boundaries, never a hand-built context where the caller
must construct it. The first wave promoted the five most critical
traces into CI:

- **G01 research-harness production** — research mode through the real
  planning composition (``maybe_run_discovery`` over a
  ``DiscoveryRunContext`` whose harness is built by the PRODUCTION
  ``completion_from_llm_client`` seam): the harness is invoked before
  the final plan, every research completion is charged to the run, and
  a missing model route is a PRECISE configuration refusal (never a
  silent lexical downgrade; ``none`` and ``lexical`` stay supported).
- **G02 tool observations reach the next turn** — a rule that lives in
  a source file (NOT in the issue text or lexical evidence) reaches
  the SECOND research completion's prompt with its provenance after a
  ``read_file``, ``list_paths`` names reach the next turn too, and the
  sensitivity guard proves the assertion reads the ACTUAL prompt bytes
  (a serializer that drops content makes the suite red).
- **G03 attempt credential round trip** — a dispatch-issued
  generation-scoped token works on BOTH lane APIs (controls + the
  checkpoint channel) through the production ``create_app``; after the
  attempt is superseded the SAME token is refused everywhere with the
  actionable 403; the replacement works; the legacy migration window
  and an authority outage behave as specified (503, never legacy).
- **G04 exact resume survives ACK and a newer upload** — checkpoint A
  is approved, its resume command ACKED out of the pending set, a NEWER
  checkpoint B uploaded, and a fresh runner restores EXACTLY A; a
  control-read timeout never selects latest.
- **G05 promotion failure never leaves a usable target** — through the
  real channel (upload → download), a restore whose promotion fails at
  the landing boundary leaves the workspace at its ORIGINAL state, and
  the retry reconstructs cleanly.

NEXT-17's provenance leg rides along: the expected-vs-installed
resource-hash reconciliation ``provenance_report`` now carries.

The R32-17 wave (review 0fca1b7's FORGE_ACCEPTANCE_TRACES) promotes the
NEXT five most critical traces, same doctrine — the REAL service
construction, typed doubles only at external-effect boundaries:

- **R32-A the restored generation IS the execution workspace** (AT-01) —
  the real lane entry (``lane_driver.main``) under a required resume:
  the production restore lands a sibling generation, the process
  chdir'd into it, the vendor's relative-path reads see the RESTORED
  bytes and its writes land in the generation (never the checkout), the
  meta stays anchored at the stable checkout, and the collector
  resolves the generation through the ``.forge/workspace-generation``
  pointer.
- **R32-B sibling recovery assets are never selected** (AT-02) — two
  workspaces under one shared parent; B parked mid-promotion; the lane
  restore for A inventories B's leftovers and never touches them; a
  contentless A inherits nothing of B's; B's own lane restore later
  resolves B's own leftovers.
- **R32-C the fixed compatibility expiry across process replacement**
  (AT-03) — the legacy token authenticates inside the window through
  the production app; with a start recorded 31 days ago a FRESH
  subprocess re-derives the SAME past deadline (never ``now + 30``),
  the app refuses the legacy token on both APIs naming the deadline,
  and the new-generation bearer still works.
- **R32-D capacity is reserved at actual execution** (AT-05) — four
  approved runs under a three-slot policy, their ``/go`` commands
  submitted CONCURRENTLY through the real GitHubRunService: exactly
  three native workflow dispatches leave, the fourth parks
  ``blocked(execution_capacity)`` with zero dispatch I/O, and a
  terminal release frees the slot for the next dispatch.
- **R32-E the dispatch selects the lane's resume mode** (AT-04) — the
  real ``/retry`` re-dispatch carries ``lane_resume_mode=required``;
  the SHIPPED template's own mapping expression (evaluated as written)
  turns it into ``FORGE_LANE_RESUME=1``, which the lane's
  ``resume_mode()`` reads as the required-restore contract; the initial
  dispatch's ``fresh`` maps to no restore requirement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.adaptive.artifact_store import ContentAddressedStore
from forge.adaptive.checkpoint_channel import (
    CheckpointChannel,
    LaneControlAPI,
    format_checkpoint_ref,
    work_scoped_token,
)
from forge.adaptive.checkpointing import capture_wip, restore_wip
from forge.adaptive.discovery_stage import (
    DiscoveryRunContext,
    DiscoveryStageError,
    maybe_run_discovery,
)
from forge.adaptive.drivers.live_registrations import provenance_report
from forge.adaptive.models import ControlCommand
from forge.adaptive.research_planner import (
    FORGE_DISCOVERY_MODE_ENV,
    RESEARCH_BEGIN,
    RESEARCH_HARNESS_MODE,
    ResearchHarness,
    ToolObservation,
    completion_from_llm_client,
)
from forge.config import Settings
from forge.database import reset_engine
from forge.durable import FlowStatus
from forge.durable.models import FlowRun
from forge.factory.planner import PLANNER_TIER
from forge.main import create_app
from forge.models.base import Base

SECRET = "regression-lane-secret"  # noqa: S105 — fake shared secret for tests
RUN_ID = "run-appreg-1"
WORK_ID = "run-appreg-1"
TENANT = "work-tenant"

PLANNER_INPUT = "Refactor the LLMPlanner so plan() cites evidence and start_run stays stable."

#: The rule G02 plants: it lives in a SOURCE FILE only. Its wording is
#: deliberately free of every keyword the lexical probes extract from
#: the planner input (refactor/llmplanner/plan/cites/evidence/
#: start_run/stays/stable), so the only road into the model's context
#: is the tool-observation channel.
CONVENTIONS_RULE = "RULE: submissions carry a ticket reference in the approval header."

FILES = {
    "src/app/planner.py": "class LLMPlanner:\n    def plan(self, issue):\n        return issue\n",
    "src/app/service.py": (
        "from app.planner import LLMPlanner\n"
        "\n"
        "def start_run():\n"
        "    planner = LLMPlanner()\n"
        "    return planner.plan(None)\n"
    ),
    "README.md": "Use LLMPlanner for planning.\n",
    "docs/conventions.md": "# Conventions\n" + CONVENTIONS_RULE + "\n",
}


class FakeReader:
    """A typed repository-read double (get_tree / read_text) — never an
    AsyncMock: NEXT-25's clean-lifecycle doctrine keeps doubles typed and
    closing so no unawaited-coroutine noise leaks into the suite."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> list:
        return [SimpleNamespace(path=p, type="blob") for p in sorted(self._files)]

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        return self._files[file_path]


class ScriptedCompletionLLM:
    """A typed LLMClient double at the EXISTING ``complete()`` seam.

    Records every call's identity (tier, role, flow_run_id, prompt) so a
    test can prove WHICH surface consumed the run's budget and WHAT the
    next turn actually saw — the captured prompt bytes are the evidence
    G02 leans on.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        role: str = "research",
        flow_run_id: str | None = None,
        **_kwargs: object,
    ) -> str:
        self.calls.append(
            {
                "tier": tier,
                "role": role,
                "flow_run_id": flow_run_id,
                "system": system,
                "user": user,
            }
        )
        return self._responses.pop(0)


def _research_call(tool: str, **args: object) -> str:
    return json.dumps({"calls": [{"tool": tool, "repo": "own", "args": args}], "done": False})


def _research_done(summary: str) -> str:
    return json.dumps({"done": True, "summary": summary, "assumptions": [], "contradictions": []})


async def _build_research_db(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/regressions.db", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(FlowRun(id=RUN_ID, project_id=1, status="planning"))
        await session.commit()
    return engine


@pytest.fixture()
async def db(tmp_path: Path) -> async_sessionmaker:
    """The research-stage session factory with the engine DISPOSED at
    teardown (R32-18's lifecycle rule for every engine a fixture owns —
    a leaked aiosqlite engine keeps a worker thread alive past the
    event loop)."""
    engine = await _build_research_db(tmp_path)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _harness(llm: ScriptedCompletionLLM) -> ResearchHarness:
    """The PRODUCTION construction: the harness's completion callable is
    ``completion_from_llm_client`` over the LLMClient seam (the same
    binding ``GitHubRunService.start_run`` performs) — never a hand-built
    context a manually supplied callable would hide."""
    return ResearchHarness(
        complete=completion_from_llm_client(llm, tier=PLANNER_TIER, flow_run_id=RUN_ID)
    )


# ---------------------------------------------------------------------------
# G01 — research composition through the real planning surface
# ---------------------------------------------------------------------------


class TestG01ResearchHarnessProduction:
    @pytest.fixture(autouse=True)
    def _mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, RESEARCH_HARNESS_MODE)

    async def test_the_harness_runs_before_the_final_plan_charged_to_the_run(self, db):
        """The composed trace: research-harness mode through the ordinary
        discovery splice. The harness is constructed by the production
        seam, every completion carries the run's identity + the research
        role, and the findings land in the durable record the final plan
        cites."""
        llm = ScriptedCompletionLLM(
            [
                _research_call("find_symbol", name="LLMPlanner"),
                _research_done("The planner class is declared at src/app/planner.py:1."),
            ]
        )
        factory = db
        ctx = DiscoveryRunContext(
            run_id=RUN_ID,
            project_id=1,
            session_factory=factory,
            snapshot_files=dict(FILES),
            repository_id="example/repo",
            source_oid="f" * 40,
            research=_harness(llm),
        )

        augmented = await maybe_run_discovery(ctx, PLANNER_INPUT)

        # Two research completions happened — propose, then done — each
        # charged to THIS run at the planner tier with the research role.
        assert [call["role"] for call in llm.calls] == ["research", "research"]
        assert {call["flow_run_id"] for call in llm.calls} == {RUN_ID}
        assert {call["tier"] for call in llm.calls} == {PLANNER_TIER}
        # The research pass completed and its findings joined the durable
        # record as citable evidence BEFORE the final plan: the augmented
        # planner input carries the research section beside the digest.
        async with factory() as session:
            run = await session.get(FlowRun, RUN_ID)
            record = dict((run.evidence or {}).get("discovery") or {})
        assert record["status"] == "complete"
        research = record.get("research") or {}
        assert research.get("complete") is True
        assert any(
            "planner.py" in str(entry.get("path", "")) for entry in (record.get("evidence") or [])
        )
        assert RESEARCH_BEGIN in augmented
        assert "src/app/planner.py" in augmented

    async def test_no_model_route_is_a_precise_configuration_refusal(self, db):
        """Research mode WITHOUT the configured completion callable
        refuses loudly and precisely — never a silent lexical-only
        downgrade the plan would then cite as if it had researched."""
        factory = db
        ctx = DiscoveryRunContext(
            run_id=RUN_ID,
            project_id=1,
            session_factory=factory,
            snapshot_files=dict(FILES),
            repository_id="example/repo",
            source_oid="f" * 40,
            research=None,  # the wiring forgot the harness
        )

        with pytest.raises(DiscoveryStageError, match="without a configured research"):
            await maybe_run_discovery(ctx, PLANNER_INPUT)

        # The refusal is DURABLE: the record says failed with the reason.
        async with factory() as session:
            run = await session.get(FlowRun, RUN_ID)
            record = dict((run.evidence or {}).get("discovery") or {})
        assert record.get("status") == "failed"
        assert "without a configured research" in str(record.get("block_reason", ""))

    async def test_none_and_lexical_modes_remain_supported(self, db, monkeypatch):
        """The tri-state stays honest: ``none`` returns the input UNTOUCHED
        (nothing persisted, no completion spent); ``lexical`` runs the
        deterministic probes without any research completion."""
        factory = db
        llm = ScriptedCompletionLLM([])
        ctx = DiscoveryRunContext(
            run_id=RUN_ID,
            project_id=1,
            session_factory=factory,
            snapshot_files=dict(FILES),
            repository_id="example/repo",
            source_oid="f" * 40,
            research=_harness(llm),
        )

        monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "none")
        assert await maybe_run_discovery(ctx, PLANNER_INPUT) == PLANNER_INPUT
        assert llm.calls == []

        monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "lexical")
        augmented = await maybe_run_discovery(ctx, PLANNER_INPUT)
        assert augmented != PLANNER_INPUT  # the digest section attached
        assert llm.calls == []  # lexical never touches the model


# ---------------------------------------------------------------------------
# G02 — tool observations reach the next turn
# ---------------------------------------------------------------------------


class TestG02ToolObservationsReachTheNextTurn:
    @pytest.fixture(autouse=True)
    def _mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, RESEARCH_HARNESS_MODE)

    async def _run(self, db, llm: ScriptedCompletionLLM) -> list[str]:
        ctx = DiscoveryRunContext(
            run_id=RUN_ID,
            project_id=1,
            session_factory=db,
            snapshot_files=dict(FILES),
            repository_id="example/repo",
            source_oid="f" * 40,
            research=_harness(llm),
        )
        await maybe_run_discovery(ctx, PLANNER_INPUT)
        return [str(call["user"]) for call in llm.calls]

    async def test_read_file_content_and_provenance_reach_the_next_turn(self, db):
        """A rule present ONLY in a source file (not the issue, not the
        lexical evidence) reaches the SECOND completion's prompt after a
        read_file — with the observation's provenance header, so the
        model knows WHERE the bytes came from."""
        llm = ScriptedCompletionLLM(
            [
                _research_call("read_file", path="docs/conventions.md"),
                _research_done("The conventions file requires file:line citations."),
            ]
        )

        prompts = await self._run(db, llm)

        assert len(prompts) == 2
        # Turn 1 never saw the rule (it is not in the issue or lexical set)...
        assert CONVENTIONS_RULE not in prompts[0]
        # ...turn 2 saw BOTH the content and its provenance.
        assert CONVENTIONS_RULE in prompts[1]
        assert "[tool: read_file own docs/conventions.md" in prompts[1]

    async def test_list_paths_names_reach_the_next_turn(self, db):
        llm = ScriptedCompletionLLM(
            [
                _research_call("list_paths", prefix="src/app/"),
                _research_done("The planner and service modules are the surface."),
            ]
        )

        prompts = await self._run(db, llm)

        assert "src/app/planner.py" in prompts[1]
        assert "src/app/service.py" in prompts[1]
        assert "[tool: list_paths own prefix src/app/]" in prompts[1]

    async def test_a_serializer_that_drops_content_is_detected(self, db, monkeypatch):
        """The sensitivity guard (the spec's 'mutate the observation
        serializer to drop content; the baseline must become red'): with
        ``ToolObservation.render`` reduced to its header, the rule NO
        LONGER reaches turn 2 — proving the assertions above read the
        real prompt bytes and go red on exactly this regression."""
        llm = ScriptedCompletionLLM(
            [
                _research_call("read_file", path="docs/conventions.md"),
                _research_done("done"),
            ]
        )

        def header_only(self: ToolObservation) -> str:
            return f"[tool: {self.tool} {self.repo_key} {self.call}]"

        monkeypatch.setattr(ToolObservation, "render", header_only)
        prompts = await self._run(db, llm)

        assert CONVENTIONS_RULE not in prompts[1]  # the mutation DID drop it
        assert "[tool: read_file own docs/conventions.md" in prompts[1]


# ---------------------------------------------------------------------------
# G03 — the attempt credential round trip over BOTH lane APIs
# ---------------------------------------------------------------------------


def _lane_settings(tmp_path: Path) -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/lane-control.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_LANE_CONTROL_SECRET=SecretStr(SECRET),
    )


class _BrokenAuthority:
    """A session factory whose lookups fail — the authority-outage leg."""

    def __call__(self):
        raise RuntimeError("authority store unavailable")


class TestG03AttemptCredentialRoundTrip:
    @pytest.fixture()
    async def app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        reset_engine()
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", SECRET)  # the channel's env half
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "checkpoint-store"))
        application = create_app(settings=_lane_settings(tmp_path))
        async with application.router.lifespan_context(application):
            async with application.state.session_factory() as session:
                session.add(
                    FlowRun(
                        id=WORK_ID,
                        project_id=1,
                        provider="github",
                        status="planning",
                        cancellation_generation=1,
                    )
                )
                await session.commit()
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app) -> AsyncClient:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @staticmethod
    def _bearer(generation: int) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {work_scoped_token(SECRET, WORK_ID, generation=generation)}"
        }

    async def _bump_generation(self, app, generation: int) -> None:
        async with app.state.session_factory() as session:
            await session.execute(
                update(FlowRun)
                .where(FlowRun.id == WORK_ID)
                .values(cancellation_generation=generation)
            )
            await session.commit()

    async def test_the_dispatch_token_round_trips_both_apis_and_retires_on_supersede(
        self, app, client, tmp_path
    ):
        """The full G03 trace over the production app: the dispatch-issued
        token polls controls AND uploads/downloads a checkpoint; the
        attempt is superseded; the SAME token is refused on BOTH APIs
        with the actionable 403; the replacement works on both."""
        from tests.test_adaptive_checkpoint_channel import _wire_payload  # the wire contract

        tree = tmp_path / "runner-a"
        (tree / "src").mkdir(parents=True)
        (tree / "src" / "app.py").write_bytes(b'print("v2")\n')
        (tree / "README.md").write_bytes(b"# readme\n")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = capture_wip(
            work_id=WORK_ID,
            root=tree,
            store=store,
            tracked_baseline={"src/app.py": hashlib.sha256(b'print("v1")\n').hexdigest()},
            sequence=1,
        )
        bearer = self._bearer(1)

        # The dispatch-issued token works on BOTH APIs.
        controls = await client.get(f"/lane/controls?work_id={WORK_ID}", headers=bearer)
        assert controls.status_code == 200, controls.text
        put = await client.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=_wire_payload(store, receipt.artifact_id),
            headers=bearer,
        )
        assert put.status_code == 200, put.text
        get = await client.get(f"/lane/checkpoints/{WORK_ID}", headers=bearer)
        assert get.status_code == 200

        # The attempt is SUPERSEDED — a new runner generation opens.
        await self._bump_generation(app, 2)

        # The old token: every new effectful operation fails, on BOTH
        # APIs, naming both generations (the retired-lane oracle). The
        # STATUS is the surface's own refusal class — 403 on the control
        # surface, 401 on the channel — the DETAIL is the shared ladder.
        superseded = (
            ("403", await client.get(f"/lane/controls?work_id={WORK_ID}", headers=bearer)),
            ("401", await client.get(f"/lane/checkpoints/{WORK_ID}", headers=bearer)),
            (
                "401",
                await client.put(
                    f"/lane/checkpoints/{WORK_ID}",
                    json=_wire_payload(store, receipt.artifact_id),
                    headers=bearer,
                ),
            ),
        )
        for expected_status, response in superseded:
            assert response.status_code == int(expected_status), response.text
            assert "superseded runner generation" in response.json()["detail"]
            assert "generation 2" in response.json()["detail"]

        # The replacement token works on BOTH APIs.
        replacement = self._bearer(2)
        controls2 = await client.get(f"/lane/controls?work_id={WORK_ID}", headers=replacement)
        checkpoint2 = await client.get(f"/lane/checkpoints/{WORK_ID}", headers=replacement)
        assert controls2.status_code == 200
        assert checkpoint2.status_code == 200

    async def test_the_legacy_migration_window_is_explicit_on_both_apis(
        self, app, client, monkeypatch
    ):
        """Legacy work-scoped tokens authenticate inside the window and are
        refused WITH THE DEADLINE past it — on both APIs (one ladder)."""
        legacy = {"Authorization": f"Bearer {work_scoped_token(SECRET, WORK_ID)}"}

        inside = await client.get(f"/lane/checkpoints/{WORK_ID}", headers=legacy)
        assert inside.status_code == 404  # authenticated; nothing held yet

        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", "2020-01-01T00:00:00+00:00")
        controls_refusal = await client.get(f"/lane/controls?work_id={WORK_ID}", headers=legacy)
        channel_refusal = await client.get(f"/lane/checkpoints/{WORK_ID}", headers=legacy)
        # 403 on the control surface, 401 on the channel — the shared
        # ladder's refusal classes; the DETAIL names the deadline on both.
        assert controls_refusal.status_code == 403, controls_refusal.text
        assert channel_refusal.status_code == 401, channel_refusal.text
        assert "migration deadline" in controls_refusal.json()["detail"]
        assert "migration deadline" in channel_refusal.json()["detail"]

    async def test_an_authority_outage_is_a_refusal_never_legacy(self, app, client):
        """The G03 outage leg: with the durable authority unreadable, both
        APIs answer 503 — an outage is not a migration state, and the
        legacy scheme is never silently accepted instead."""
        healthy_factory = app.state.session_factory
        app.state.session_factory = _BrokenAuthority()
        try:
            legacy = {"Authorization": f"Bearer {work_scoped_token(SECRET, WORK_ID)}"}
            for response in (
                await client.get(f"/lane/controls?work_id={WORK_ID}", headers=legacy),
                await client.get(f"/lane/checkpoints/{WORK_ID}", headers=legacy),
                await client.get(f"/lane/controls?work_id={WORK_ID}", headers=self._bearer(1)),
            ):
                assert response.status_code == 503, response.text
                assert "unavailable" in response.json()["detail"]
        finally:
            app.state.session_factory = healthy_factory


# ---------------------------------------------------------------------------
# G04 — exact resume survives ACK and a newer upload
# ---------------------------------------------------------------------------

CP = "http://cp.test"


def _resume_command_row(seq: int, checkpoint_id: str) -> dict:
    """The durable resume command ROW — acked all the way up, gone from
    the pending set, its payload immutable."""
    command = ControlCommand.model_validate(
        {
            "schema": "forge.proposal.control-command/1",
            "command_id": f"cmd-resume-{seq}",
            "work_id": WORK_ID,
            "sequence": seq,
            "kind": "resume",
            "actor_ref": "human:op",
            "actor_origin": "server_authenticated_human",
            "idempotency_key": f"resume-key-{seq}",
            "status": "checkpointed",
            "payload": {"checkpoint_ref": format_checkpoint_ref(WORK_ID, checkpoint_id)},
        }
    )
    return command.model_dump(mode="json")


class TestG04ExactResumeSurvivesAckAndNewerUpload:
    @staticmethod
    def _active_generation(checkout: Path) -> Path:
        """The workspace generation the restore pointer names (R32-01): the
        lane's restored bytes live in a SIBLING generation, never in the
        checkout the process sits in."""
        pointer = json.loads((checkout / ".forge" / "workspace-generation").read_text())
        return checkout.parent / pointer["generation"]

    @pytest.fixture()
    def captures(self, tmp_path: Path):
        """Checkpoint A (approved resume point, sequence 10) and checkpoint
        B (a NEWER upload, sequence 20) over one runner's tree."""
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        baseline = {"src/app.py": hashlib.sha256(b'print("v1")\n').hexdigest()}

        (tree / "src").mkdir(parents=True)
        (tree / "src" / "app.py").write_bytes(b'print("v2 - approved")\n')
        (tree / "README.md").write_bytes(b"# readme\n")
        checkpoint_a = capture_wip(
            work_id=WORK_ID, root=tree, store=store, tracked_baseline=baseline, sequence=10
        )
        (tree / "src" / "app.py").write_bytes(b'print("v3 - newer")\n')
        checkpoint_b = capture_wip(
            work_id=WORK_ID, root=tree, store=store, tracked_baseline=baseline, sequence=20
        )
        return tree, store, checkpoint_a, checkpoint_b

    @pytest.fixture()
    def lane_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A fresh runner's checkout: the BASE the dispatch cut (v1), not
        either checkpoint's content."""
        cwd = tmp_path / "lane-checkout"
        (cwd / "src").mkdir(parents=True)
        (cwd / "src" / "app.py").write_bytes(b'print("v1")\n')
        (cwd / "README.md").write_bytes(b"# readme\n")
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", CP)
        monkeypatch.setenv("FORGE_LANE_CONTROL_TOKEN", "lane-token-1")
        return cwd

    @staticmethod
    def _serve(httpx_mock, store: ContentAddressedStore, artifact_id: str, sequence: int):
        from tests.test_adaptive_checkpoint_channel import _wire_payload

        document = _wire_payload(store, artifact_id)
        document["checkpoint_id"] = artifact_id
        document["sequence"] = sequence
        httpx_mock.add_response(
            url=f"{CP}/lane/checkpoints/{WORK_ID}?checkpoint_id={artifact_id}", json=document
        )

    def test_the_acked_resume_spec_restores_exactly_a_never_b(self, httpx_mock, lane_cwd, captures):
        """The composed G04: A is approved and its command ACKED (the row
        long left pending); B is uploaded afterwards; a NEW runner boots —
        A is restored BY ITS ResumeSpec, B never substitutes, and the
        workspace carries A's bytes. B's endpoint is deliberately NOT
        served: had the lane fetched it, the unmatched request would fail
        the restore right here."""
        from forge.lane_driver import _maybe_restore_wip

        _tree, store, checkpoint_a, _checkpoint_b = captures
        httpx_mock.add_response(
            url=f"{CP}/lane/controls/resume-spec?work_id={WORK_ID}",
            json={"work_id": WORK_ID, "command": _resume_command_row(5, checkpoint_a.artifact_id)},
        )
        self._serve(httpx_mock, store, checkpoint_a.artifact_id, 10)

        report = _maybe_restore_wip(WORK_ID)

        assert report is not None
        assert report["restored"] is True, report
        assert report["checkpoint_selection"] == "exact"
        assert report["checkpoint_sequence"] == 10
        assert report["checkpoint_ref"] == format_checkpoint_ref(WORK_ID, checkpoint_a.artifact_id)
        # A's bytes — never B's — landed in the ACTIVE workspace GENERATION
        # beside the checkout (R32-01: the lane never rewrites its own cwd;
        # the pointer file resolves the generation for the collector step).
        assert (
            self._active_generation(lane_cwd) / "src" / "app.py"
        ).read_bytes() == b'print("v2 - approved")\n'
        assert (lane_cwd / "src" / "app.py").read_bytes() == b'print("v1")\n'
        # The checkpoint GET NAMED A: B never substituted.
        checkpoint_gets = [
            request
            for request in httpx_mock.get_requests()
            if request.url.path == f"/lane/checkpoints/{WORK_ID}"
        ]
        assert [request.url.params.get("checkpoint_id") for request in checkpoint_gets] == [
            checkpoint_a.artifact_id
        ]

    def test_a_control_read_timeout_must_not_select_latest(
        self, httpx_mock, lane_cwd, captures, monkeypatch
    ):
        """The G04 timeout leg with a REQUIRED resume: the spec lookup
        timing out is an honest refusal — no checkpoint GET ever leaves,
        the newer upload can never ride a timeout."""
        from forge.lane_driver import _maybe_restore_wip

        monkeypatch.setenv("FORGE_LANE_RESUME", "1")
        httpx_mock.add_exception(
            httpx.ConnectError("control plane unreachable"),
            url=f"{CP}/lane/controls/resume-spec?work_id={WORK_ID}",
        )

        report = _maybe_restore_wip(WORK_ID)

        assert report is not None
        assert report["restored"] is False
        assert report["checkpoint_selection"] == "unavailable"
        assert [
            request
            for request in httpx_mock.get_requests()
            if request.url.path.startswith("/lane/checkpoints")
        ] == []

    def test_a_fresh_run_with_no_resume_does_not_require_any_checkpoint(
        self, httpx_mock, lane_cwd, captures, monkeypatch
    ):
        """The G04 fresh-run leg: no resume command exists at all — the
        lane does not FAIL for want of a checkpoint; it takes the active
        one by the recorded (never silent) fallback and proceeds."""
        from forge.lane_driver import _maybe_restore_wip
        from tests.test_adaptive_checkpoint_channel import _wire_payload

        monkeypatch.delenv("FORGE_LANE_RESUME", raising=False)
        _tree, store, checkpoint_a, _checkpoint_b = captures
        httpx_mock.add_response(
            url=f"{CP}/lane/controls/resume-spec?work_id={WORK_ID}",
            json={"work_id": WORK_ID, "command": None},
        )
        # The ACTIVE checkpoint answers WITHOUT a checkpoint_id param (the
        # fallback's own request shape).
        document = _wire_payload(store, checkpoint_a.artifact_id)
        document["checkpoint_id"] = checkpoint_a.artifact_id
        document["sequence"] = 10
        httpx_mock.add_response(url=f"{CP}/lane/checkpoints/{WORK_ID}", json=document)

        report = _maybe_restore_wip(WORK_ID)

        assert report is not None
        assert report["restored"] is True, report
        assert report["checkpoint_selection"] == "latest"
        assert "fallback" in report["selection_note"]
        # The fallback still landed the ACTIVE checkpoint's bytes — in the
        # sibling GENERATION, with the checkout's original bytes intact.
        assert (
            self._active_generation(lane_cwd) / "src" / "app.py"
        ).read_bytes() == b'print("v2 - approved")\n'
        assert (lane_cwd / "src" / "app.py").read_bytes() == b'print("v1")\n'


# ---------------------------------------------------------------------------
# G05 — promotion failure never leaves a usable target
# ---------------------------------------------------------------------------


class TestG05PromotionFailureNeverLeavesAUsableTarget:
    @pytest.fixture()
    def runner_b(self, tmp_path: Path) -> Path:
        target = tmp_path / "runner-b"
        target.mkdir()
        (target / "src").mkdir()
        (target / "src" / "app.py").write_bytes(b'print("original")\n')
        (target / "README.md").write_bytes(b"# original readme\n")
        return target

    def _capture_and_upload(self, tmp_path: Path, server) -> tuple[str, bytes]:
        tree = tmp_path / "runner-a"
        (tree / "src").mkdir(parents=True)
        (tree / "src" / "app.py").write_bytes(b'print("checkpointed")\n')
        (tree / "README.md").write_bytes(b"# readme\n")
        store = ContentAddressedStore(tmp_path / "store-a", tenant=TENANT)
        receipt = capture_wip(
            work_id=WORK_ID,
            root=tree,
            store=store,
            tracked_baseline={
                "src/app.py": hashlib.sha256(b'print("original")\n').hexdigest(),
                "README.md": hashlib.sha256(b"# original readme\n").hexdigest(),
            },
            sequence=3,
        )
        channel = CheckpointChannel(
            LaneControlAPI(base_url="http://testserver", token=_server_secret(), client=server)
        )
        channel.upload_checkpoint(store, WORK_ID)
        return receipt.artifact_id, store

    def test_a_failed_promotion_leaves_the_original_workspace_and_the_retry_reconstructs(
        self, tmp_path: Path, runner_b: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The composed G05 through the REAL channel: upload on runner A,
        download on runner B, promotion fails at the landing boundary —
        the workspace keeps its ORIGINAL bytes (never mixed), the report
        says promotion_failed, and a later retry reconstructs cleanly."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import forge.api_checkpoint_channel as api_channel
        from tests.test_adaptive_checkpoint_channel import _wire_payload

        monkeypatch.setenv(api_channel.LANE_CONTROL_SECRET_ENV, "g5-secret")
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "server-store"))
        application = FastAPI()
        application.include_router(api_channel.checkpoint_channel_router)
        server = TestClient(application)

        artifact_id, store_a = self._capture_and_upload(tmp_path, server)
        payload = _wire_payload(store_a, artifact_id)
        put = server.put(
            f"/lane/checkpoints/{WORK_ID}",
            json=payload,
            headers={"Authorization": f"Bearer {work_scoped_token('g5-secret', WORK_ID)}"},
        )
        assert put.status_code == 200, put.text

        # Runner B: a FRESH store downloads through the channel API.
        store_b = ContentAddressedStore(tmp_path / "store-b", tenant=TENANT)
        download = server.get(
            f"/lane/checkpoints/{WORK_ID}?checkpoint_id={artifact_id}",
            headers={"Authorization": f"Bearer {work_scoped_token('g5-secret', WORK_ID)}"},
        )
        assert download.status_code == 200
        import base64

        manifest = json.loads(base64.b64decode(download.json()["manifest"]))
        for entry in manifest["files"].values():
            store_b.put(base64.b64decode(download.json()["blobs"][str(entry["digest"])]))
        store_b.put(base64.b64decode(download.json()["manifest"]), content_type="application/json")

        # The promotion fails at the LANDING boundary of the generation
        # switch — the reviewer's "fail the second rename". Only the
        # FIRST landing fails: the rollback rename must still be able to
        # bring the original generation back.
        real_replace = os.replace
        landed = {"n": 0}

        def failing_landing(src, dst):
            if dst == runner_b:
                landed["n"] += 1
                if landed["n"] == 1:  # tree->target; the rollback is next
                    raise OSError("injected: the landing rename fails")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", failing_landing)
        failed = restore_wip(
            artifact_id=artifact_id, store=store_b, target=runner_b, principal=TENANT
        )

        assert failed.ok is False
        assert failed.phase == "promotion_failed"
        assert failed.target_invalid is False
        # The ORIGINAL workspace, byte for byte — never a mix.
        assert (runner_b / "src" / "app.py").read_bytes() == b'print("original")\n'
        assert (runner_b / "README.md").read_bytes() == b"# original readme\n"

        # The retry reconstructs from the approved checkpoint.
        monkeypatch.setattr(os, "replace", real_replace)
        retried = restore_wip(
            artifact_id=artifact_id,
            store=ContentAddressedStore(tmp_path / "store-b", tenant=TENANT),
            target=runner_b,
            principal=TENANT,
        )
        assert retried.ok is True, retried.failures
        assert (runner_b / "src" / "app.py").read_bytes() == b'print("checkpointed")\n'


def _server_secret() -> str:
    return "g5-secret"


# ---------------------------------------------------------------------------
# NEXT-17 — expected-vs-installed resource hashes in the provenance report
# ---------------------------------------------------------------------------


class TestProvenanceResourceHashes:
    """The templates record the EXPECTED sha256 (the wheel pin) beside the
    ACTUAL hash the runner computed (``.forge/lane_install.json``); the
    provenance report reconciles the two halves exactly as it reconciles
    pin/version/registration."""

    HASH = "a" * 64

    def test_matching_hashes_carry_no_warning(self):
        report = provenance_report(
            "claude-code",
            installed_cli_version="claude 2.1.273 (Claude Code)",
            declared_pin="2.1.273",  # the registered pin: no unrelated drift noise
            expected_resource_sha256=self.HASH,
            installed_resource_sha256=self.HASH,
        )
        assert report["expected_resource_sha256"] == self.HASH
        assert report["installed_resource_sha256"] == self.HASH
        assert report["resource_hash_matches"] is True
        assert report["warnings"] == []

    def test_a_mismatch_is_said_loudly_with_both_hashes(self):
        report = provenance_report(
            "claude-code",
            installed_cli_version="claude 2.1.273 (Claude Code)",
            declared_pin="2.1.273",
            expected_resource_sha256=self.HASH,
            installed_resource_sha256="b" * 64,
        )
        assert report["resource_hash_matches"] is False
        warnings = " ".join(str(w) for w in report["warnings"])
        assert "sha256:" + self.HASH in warnings and "b" * 64 in warnings
        assert "NOT the bytes" in warnings

    def test_expected_without_actual_is_an_unknown_not_a_pass(self):
        report = provenance_report(
            "claude-code",
            installed_cli_version="claude 2.1.273 (Claude Code)",
            expected_resource_sha256=self.HASH,
        )
        assert report["resource_hash_matches"] is None
        assert any("never reported" in str(w) for w in report["warnings"])

    def test_no_hash_claims_at_all_stay_silent_and_json_shaped(self):
        report = provenance_report(
            "claude-code", installed_cli_version="claude 2.1.273 (Claude Code)"
        )
        assert report["resource_hash_matches"] is None
        assert report["expected_resource_sha256"] == ""
        assert json.loads(json.dumps(report)) == report


# ---------------------------------------------------------------------------
# R32-17 wave — the 0fca1b7 review's next five most-critical acceptance
# traces (AT-01/02/03/05/04), same doctrine: the REAL service
# construction, typed doubles only at external-effect boundaries.
# ---------------------------------------------------------------------------


def _generation_of(checkout: Path) -> Path:
    """The workspace generation the checkout's pointer names — how every
    step AFTER the lane resolves the restored workspace (never an
    inherited directory inode)."""
    pointer = json.loads((checkout / ".forge" / "workspace-generation").read_text())
    return checkout.parent / pointer["generation"]


def _work_resume_row(work_id: str, seq: int, checkpoint_id: str) -> dict:
    """The durable resume command ROW for *work_id* (G04's shape, scoped)."""
    command = ControlCommand.model_validate(
        {
            "schema": "forge.proposal.control-command/1",
            "command_id": f"cmd-resume-{work_id}-{seq}",
            "work_id": work_id,
            "sequence": seq,
            "kind": "resume",
            "actor_ref": "human:op",
            "actor_origin": "server_authenticated_human",
            "idempotency_key": f"resume-key-{work_id}-{seq}",
            "status": "checkpointed",
            "payload": {"checkpoint_ref": format_checkpoint_ref(work_id, checkpoint_id)},
        }
    )
    return command.model_dump(mode="json")


def _serve_checkpoint(
    httpx_mock, base_url: str, work_id: str, store, artifact_id: str, sequence: int
):
    """Serve one checkpoint's download document at the channel's shape."""
    from tests.test_adaptive_checkpoint_channel import _wire_payload

    document = _wire_payload(store, artifact_id)
    document["checkpoint_id"] = artifact_id
    document["sequence"] = sequence
    httpx_mock.add_response(
        url=f"{base_url}/lane/checkpoints/{work_id}?checkpoint_id={artifact_id}", json=document
    )


# ---------------------------------------------------------------------------
# R32-A (AT-01) — the restored generation IS the execution workspace
# ---------------------------------------------------------------------------


class TestR32ARestoredGenerationIsTheExecutionWorkspace:
    """restore → chdir → the agent reads/writes IN the generation → the
    collector finds the artifacts. Driven through the REAL lane entry
    (``lane_driver.main``) with a required resume: the production restore
    implementation, the production generation switch, and the production
    artifact contract — a stubbed restore report is not permitted."""

    CP = "http://cp-r32a.test"

    @pytest.fixture()
    def checkout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A fresh runner's checkout: the BASE the dispatch cut (v1), the
        brief beside it, and the dispatch env a required-resume lane gets."""
        cwd = tmp_path / "lane-checkout"
        (cwd / "src").mkdir(parents=True)
        (cwd / "src" / "app.py").write_bytes(b'print("v1")\n')
        (cwd / "README.md").write_bytes(b"# readme\n")
        (cwd / ".forge").mkdir()
        (cwd / ".forge" / "brief.md").write_text("PLAN: modernize the orders service")
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", self.CP)
        monkeypatch.setenv("FORGE_LANE_CONTROL_TOKEN", "lane-token-r32a")
        monkeypatch.setenv("FORGE_WORK_ID", WORK_ID)
        monkeypatch.setenv("FORGE_RUN_ID", RUN_ID)
        monkeypatch.setenv("FORGE_ISSUE_IID", "42")
        monkeypatch.setenv("FORGE_ATTEMPT_BASE", "1" * 40)
        monkeypatch.setenv("FORGE_CLAUDE_MODEL", "glm-5.3-flash[1m]")
        monkeypatch.setenv("FORGE_LANE_POLL_SECONDS", "0.01")
        # The dispatch's WIP-continuity contract (R32-04/R32-E): REQUIRED.
        monkeypatch.setenv("FORGE_LANE_RESUME", "1")
        monkeypatch.setenv("FORGE_STEERING_ENABLED", "1")
        return cwd

    def _checkpoint(self, tmp_path: Path) -> tuple[ContentAddressedStore, object]:
        """Checkpoint A: the operator-approved WIP (v2) over the v1 base."""
        tree = tmp_path / "runner-a"
        store = ContentAddressedStore(tmp_path / "store-r32a", tenant=TENANT)
        baseline = {"src/app.py": hashlib.sha256(b'print("v1")\n').hexdigest()}
        (tree / "src").mkdir(parents=True)
        (tree / "src" / "app.py").write_bytes(b'print("v2 - approved")\n')
        (tree / "README.md").write_bytes(b"# readme\n")
        receipt = capture_wip(
            work_id=WORK_ID, root=tree, store=store, tracked_baseline=baseline, sequence=10
        )
        return store, receipt

    def test_the_vendor_reads_and_writes_in_the_restored_generation(
        self, httpx_mock, checkout, tmp_path
    ):
        """The composed AT-01: the lane restores, ENTERS the generation, the
        vendor's relative-path read sees the RESTORED bytes (which exist
        ONLY in the generation), its write lands in the generation, the
        checkout keeps its original bytes, and the collector resolves the
        generation through the pointer and finds the artifacts."""
        from tests.test_lane_driver import FakeLaneClient, FakeSDK

        class GenerationWorkingVendor(FakeLaneClient):
            """The vendor double: its file effects land wherever the lane
            process sits — the assertion reads them back to prove WHERE."""

            async def query(self, prompt, session_id="default"):
                if not self.connected:
                    raise RuntimeError("Not connected. Call connect() first.")
                self.queries.append(prompt)
                self.vendor_read = Path("src/app.py").read_bytes()  # RELATIVE path
                Path("src/generated.py").write_bytes(b"# vendor output\n")
                await self._inbox.put(_fake_user_message(prompt))
                await self._inbox.put(self._result_message())

        store, receipt = self._checkpoint(tmp_path)
        httpx_mock.add_response(
            url=f"{self.CP}/lane/controls/resume-spec?work_id={WORK_ID}",
            json={"work_id": WORK_ID, "command": _work_resume_row(WORK_ID, 5, receipt.artifact_id)},
        )
        _serve_checkpoint(httpx_mock, self.CP, WORK_ID, store, receipt.artifact_id, 10)
        # The steering drain polls the control plane — nothing pending (the
        # cursor rides the query string, so the matcher is a pattern).
        httpx_mock.add_response(
            url=re.compile(rf"^{re.escape(self.CP)}/lane/controls(?:\?.*)?$"),
            json={"commands": []},
            is_reusable=True,
        )

        registry = FakeSDK(GenerationWorkingVendor)
        from forge.lane_driver import main as lane_main

        assert lane_main(sdk=registry.module) == 0

        vendor = registry.sole_client
        generation = _generation_of(checkout)
        # The VENDOR read the RESTORED bytes — v2 exists ONLY in the
        # generation, so the vendor's cwd was the generation, not the
        # checkout it was handed.
        assert vendor.vendor_read == b'print("v2 - approved")\n'
        # Its write landed in the GENERATION; the checkout is pristine.
        assert (generation / "src" / "generated.py").read_bytes() == b"# vendor output\n"
        assert not (checkout / "src" / "generated.py").exists()
        assert (checkout / "src" / "app.py").read_bytes() == b'print("v1")\n'
        assert (generation / "src" / "app.py").read_bytes() == b'print("v2 - approved")\n'
        # The artifact contract stays anchored at the STABLE checkout: the
        # meta names the active generation; the sidecar records the restore.
        meta = json.loads((checkout / ".forge" / "candidate.meta.json").read_text())
        assert meta["exit"] == "completed"
        assert meta["workspace_generation"] == str(generation)
        assert (checkout / ".forge" / "usage.json").is_file()
        sidecar = json.loads((checkout / ".forge" / "steering.json").read_text())
        assert sidecar["wip_restore"]["restored"] is True
        assert sidecar["wip_restore"]["checkpoint_selection"] == "exact"
        # The COLLECTOR step: resolve the pointer, find the artifacts.
        assert (generation / "src" / "app.py").is_file()
        assert (checkout / ".forge" / "candidate.meta.json").is_file()

    def test_a_required_resume_that_cannot_land_halts_before_any_vendor_session(
        self, httpx_mock, checkout, tmp_path
    ):
        """AT-01's refusal leg: the required resume's checkpoint DOWNLOAD
        fails after a valid spec — ZERO vendor sessions exist, no partial
        target is treated as valid, and the failure rides the sidecar the
        CI collects."""
        store, receipt = self._checkpoint(tmp_path)
        httpx_mock.add_response(
            url=f"{self.CP}/lane/controls/resume-spec?work_id={WORK_ID}",
            json={"work_id": WORK_ID, "command": _work_resume_row(WORK_ID, 5, receipt.artifact_id)},
        )
        httpx_mock.add_exception(
            httpx.ConnectError("checkpoint store unreachable"),
            url=re.compile(rf"^{re.escape(self.CP)}/lane/checkpoints/{WORK_ID}(?:\?.*)?$"),
        )

        from forge.lane_driver import main as lane_main
        from tests.test_lane_driver import FakeSDK

        registry = FakeSDK()  # any vendor construction would prove the bug
        assert lane_main(sdk=registry.module) == 1
        assert registry.clients == []  # no vendor session ever existed
        sidecar = json.loads((checkout / ".forge" / "steering.json").read_text())
        assert sidecar["wip_restore"]["restored"] is False
        assert any("download failed" in failure for failure in sidecar["wip_restore"]["failures"])
        # No generation pointer was written for a restore that never landed.
        assert not (checkout / ".forge" / "workspace-generation").exists()
        meta = json.loads((checkout / ".forge" / "candidate.meta.json").read_text())
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "wip_restore_failed"


def _fake_user_message(prompt: str):
    from tests.test_lane_driver import FakeUserMessage

    return FakeUserMessage(content=prompt)


# ---------------------------------------------------------------------------
# R32-B (AT-02) — sibling recovery assets are never selected or collected
# ---------------------------------------------------------------------------


class TestR32BSiblingRecoveryOwnership:
    """Two workspaces A and B under ONE shared parent. B paused mid-restore
    (its staging and parked backup exist); the lane restore for A — through
    the REAL ``_maybe_restore_wip`` — must leave B's files, leftovers and
    lock untouched; an EMPTY A must not inherit B's content; B's own lane
    restore remains possible afterwards."""

    CP = "http://cp-r32b.test"
    WORK_A = "run-r32b-a"
    WORK_B = "run-r32b-b"

    A_WIP = b"A checkpointed\n"
    B_WIP = b"B checkpointed\n"

    @staticmethod
    def _workspaces(tmp_path: Path) -> tuple[Path, Path]:
        parent = tmp_path / "shared-parent"
        ws_a = parent / "workspace-a"
        ws_b = parent / "workspace-b"
        ws_a.mkdir(parents=True)
        ws_b.mkdir()
        (ws_a / "a.txt").write_bytes(b"A original\n")
        (ws_b / "b.txt").write_bytes(b"B original\n")
        return ws_a, ws_b

    @staticmethod
    def _checkpoint_for(
        tmp_path: Path, work_id: str, files: dict[str, bytes], base: dict[str, bytes]
    ):
        tree = tmp_path / f"runner-{work_id}"
        store = ContentAddressedStore(tmp_path / f"store-{work_id}", tenant=TENANT)
        baseline = {name: hashlib.sha256(content).hexdigest() for name, content in base.items()}
        for name, content in files.items():
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_bytes(content)
        receipt = capture_wip(
            work_id=work_id, root=tree, store=store, tracked_baseline=baseline, sequence=10
        )
        return store, receipt

    def _lane_restore(
        self, httpx_mock, monkeypatch, checkout: Path, work_id: str, store, receipt
    ) -> dict:
        """Run the lane's production restore for *work_id* from *checkout*."""
        from forge.lane_driver import _maybe_restore_wip

        monkeypatch.chdir(checkout)
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", self.CP)
        monkeypatch.setenv("FORGE_LANE_CONTROL_TOKEN", f"lane-token-{work_id}")
        httpx_mock.add_response(
            url=f"{self.CP}/lane/controls/resume-spec?work_id={work_id}",
            json={"work_id": work_id, "command": _work_resume_row(work_id, 5, receipt.artifact_id)},
        )
        _serve_checkpoint(httpx_mock, self.CP, work_id, store, receipt.artifact_id, 10)
        report = _maybe_restore_wip(work_id)
        assert report is not None
        return report

    def _park_b(self, ws_b: Path, checkpoint_id: str) -> tuple[Path, Path]:
        """B paused mid-restore: its original parked under B's OWNED backup
        name (bound to *checkpoint_id*'s promotion), its staging tree left."""
        fragment = checkpoint_id[:8]
        b_backup = ws_b.parent / f".forge-restore-backup-{self.WORK_B}-{fragment}-4242-{'c' * 8}"
        os.replace(ws_b, b_backup)
        b_staging = ws_b.parent / f".forge-restore-{self.WORK_B}-9kmqx7t"
        b_staging.mkdir()
        (b_staging / "tree").mkdir()
        return b_backup, b_staging

    def test_recovery_for_a_never_touches_bs_assets(self, httpx_mock, tmp_path, monkeypatch):
        """The composed AT-02: A's lane restore succeeds, B's parked backup
        stays byte-identical, both B leftovers are only INVENTORIED (they
        ride A's report), and B's workspace is not resurrected."""
        ws_a, ws_b = self._workspaces(tmp_path)
        store_a, receipt_a = self._checkpoint_for(
            tmp_path, self.WORK_A, {"a.txt": self.A_WIP}, {"a.txt": b"A original\n"}
        )
        b_backup, b_staging = self._park_b(ws_b, "bbbb1111")  # a DIFFERENT checkpoint's promotion
        assert not ws_b.exists()

        report = self._lane_restore(httpx_mock, monkeypatch, ws_a, self.WORK_A, store_a, receipt_a)

        assert report["restored"] is True, report
        # R32-01: the lane restore lands the WIP in the SIBLING GENERATION —
        # the checkout keeps its own bytes and only gains the pointer.
        assert (_generation_of(ws_a) / "a.txt").read_bytes() == self.A_WIP
        assert (ws_a / "a.txt").read_bytes() == b"A original\n"
        # B's assets: untouched, and REPORTED as not A's to resolve.
        assert (b_backup / "b.txt").read_bytes() == b"B original\n"
        assert b_backup.is_dir() and b_staging.is_dir()
        assert any(entry.startswith(b_backup.name) for entry in report["recovery_unrecognized"])
        assert any(entry.startswith(b_staging.name) for entry in report["recovery_unrecognized"])
        assert not ws_b.exists()  # A never resurrected B

    def test_an_empty_a_inherits_nothing_of_bs_content(self, httpx_mock, tmp_path, monkeypatch):
        """A absent (its runner came back to an EMPTY checkout): A's restore
        rebuilds from A's OWN checkpoint — B's parked original is never
        rolled into A's tree, and no b.txt exists anywhere under A."""
        ws_a, ws_b = self._workspaces(tmp_path)
        store_a, receipt_a = self._checkpoint_for(
            tmp_path, self.WORK_A, {"a.txt": self.A_WIP}, {"a.txt": b"A original\n"}
        )
        b_backup, b_staging = self._park_b(ws_b, "bbbb1111")  # B's own, OTHER-checkpoint backup
        # The fresh A checkout: nothing in it (the dispatch base re-cloned).
        for child in ws_a.iterdir():
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()

        report = self._lane_restore(httpx_mock, monkeypatch, ws_a, self.WORK_A, store_a, receipt_a)

        assert report["restored"] is True, report
        # A's WIP comes from A's OWN checkpoint — into A's generation.
        assert (_generation_of(ws_a) / "a.txt").read_bytes() == self.A_WIP
        # The fresh checkout gained only the pointer; B's content never
        # crossed over — not into the checkout, not into the generation.
        assert sorted(p.name for p in ws_a.iterdir()) == [".forge"]
        assert not (ws_a / "b.txt").exists()
        assert not _generation_of(ws_a).joinpath("b.txt").exists()

    def test_bs_own_lane_restore_remains_possible_after_as_recovery(
        self, httpx_mock, tmp_path, monkeypatch
    ):
        """Then resume B: B's own lane restore resolves B's OWN leftovers
        (its staging collected under B's token) and lands B's WIP from B's
        own checkpoint."""
        ws_a, ws_b = self._workspaces(tmp_path)
        store_a, receipt_a = self._checkpoint_for(
            tmp_path, self.WORK_A, {"a.txt": self.A_WIP}, {"a.txt": b"A original\n"}
        )
        store_b, receipt_b = self._checkpoint_for(
            tmp_path, self.WORK_B, {"b.txt": self.B_WIP}, {"b.txt": b"B original\n"}
        )
        b_backup, b_staging = self._park_b(ws_b, receipt_b.artifact_id)  # B's OWN checkpoint
        report_a = self._lane_restore(
            httpx_mock, monkeypatch, ws_a, self.WORK_A, store_a, receipt_a
        )
        assert report_a["restored"] is True

        # B's runner returns to a fresh checkout of ITS base.
        ws_b.mkdir()
        report_b = self._lane_restore(
            httpx_mock, monkeypatch, ws_b, self.WORK_B, store_b, receipt_b
        )

        assert report_b["restored"] is True, report_b
        assert (_generation_of(ws_b) / "b.txt").read_bytes() == self.B_WIP
        # B's OWNED staging was collected by B's recovery (not A's).
        assert not b_staging.exists()
        # B's OWN parked backup was resolved by B's recovery — with the
        # fresh target standing, it is the retired original and is gone.
        assert not b_backup.exists()


# ---------------------------------------------------------------------------
# R32-C (AT-03) — the fixed compatibility expiry across process replacement
# ---------------------------------------------------------------------------


class TestR32CLegacyDeadlineAcrossProcesses:
    """A legacy work-scoped token authenticates inside the window; with the
    migration start recorded 31 days ago, a FRESH subprocess re-derives the
    SAME past deadline (start + 30 days — never a recomputed ``now + 30``),
    the production app refuses the legacy token on BOTH APIs naming the
    deadline, and the new-generation bearer still works."""

    @pytest.fixture()
    async def app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        reset_engine()
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", SECRET)
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "checkpoint-store"))
        application = create_app(settings=_lane_settings(tmp_path))
        async with application.router.lifespan_context(application):
            async with application.state.session_factory() as session:
                session.add(
                    FlowRun(
                        id=WORK_ID,
                        project_id=1,
                        provider="github",
                        status="planning",
                        cancellation_generation=1,
                    )
                )
                await session.commit()
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app) -> AsyncClient:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_a_fresh_process_rederives_the_same_past_deadline_and_the_app_agrees(
        self, app, client, monkeypatch
    ):
        from forge.api_lane_control import LANE_LEGACY_TOKEN_START_ENV

        legacy = {"Authorization": f"Bearer {work_scoped_token(SECRET, WORK_ID)}"}
        bearer = {"Authorization": f"Bearer {work_scoped_token(SECRET, WORK_ID, generation=1)}"}

        # Before the deadline is recorded, the legacy window is open: the
        # token authenticates (404 = authenticated, nothing held).
        inside = await client.get(f"/lane/checkpoints/{WORK_ID}", headers=legacy)
        assert inside.status_code == 404

        # The operator records the migration start 31 days ago. A FRESH
        # subprocess (the "restarted" control plane — no process state
        # exists to extend the window) re-derives the deadline from the
        # recorded value ALONE.
        start = datetime.now(timezone.utc) - timedelta(days=31)
        recorded = start.isoformat()
        fresh = subprocess.run(
            [
                sys.executable,
                "-c",
                "from forge.api_lane_control import legacy_token_deadline;"
                "print(legacy_token_deadline().isoformat())",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, LANE_LEGACY_TOKEN_START_ENV: recorded},
        )
        restarted = datetime.fromisoformat(fresh.stdout.strip())
        assert restarted == start + timedelta(days=30)  # start + window, exactly
        assert restarted < datetime.now(timezone.utc)  # and it stays in the past

        # The app in THIS process refuses the legacy token on BOTH APIs,
        # naming the migration deadline — the same instant the subprocess
        # derived, never a new thirty-day window.
        monkeypatch.setenv(LANE_LEGACY_TOKEN_START_ENV, recorded)
        controls_refusal = await client.get(f"/lane/controls?work_id={WORK_ID}", headers=legacy)
        channel_refusal = await client.get(f"/lane/checkpoints/{WORK_ID}", headers=legacy)
        assert controls_refusal.status_code == 403, controls_refusal.text
        assert channel_refusal.status_code == 401, channel_refusal.text
        assert "migration deadline" in controls_refusal.json()["detail"]
        assert "migration deadline" in channel_refusal.json()["detail"]

        # The new-generation bearer still works — the window closed on the
        # LEGACY derivation only.
        authorized = await client.get(f"/lane/controls?work_id={WORK_ID}", headers=bearer)
        assert authorized.status_code == 200, authorized.text


# ---------------------------------------------------------------------------
# R32-D (AT-05) — capacity is reserved at actual execution, concurrently
# ---------------------------------------------------------------------------

GH_REPO = "acme/r32d-widget"
GH_PROJECT_ID = 70032
GH_BASE_ISSUE = 142
GH_WORKFLOW = "forge-harness.github.yml"


class TestR32DConcurrentLeaseUnderFourApprovals:
    """Four waiting approvals under a three-slot policy, their /go commands
    submitted CONCURRENTLY through the REAL GitHubRunService construction
    (the Actions lane — the native job-start request is the recorded
    workflow_dispatch): exactly three may start, the fourth parks
    blocked(execution_capacity) with zero dispatch I/O, and a terminal
    release frees the slot for the next dispatch."""

    @pytest.fixture()
    async def db(self, tmp_path: Path):
        """A FILE-backed database — concurrent dispatch legs need genuinely
        independent connections, not the shared in-memory one (which is a
        transaction, not a race). The engine is disposed at teardown."""
        from sqlalchemy.pool import AsyncAdaptedQueuePool

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path}/r32d-leases.db",
            connect_args={"check_same_thread": False, "timeout": 10},
            poolclass=AsyncAdaptedQueuePool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()

    @pytest.fixture()
    def fake(self):
        from tests.fixtures.fake_github import FakeGitHub

        github = FakeGitHub()
        github.seed_repo(GH_REPO, {"src/app.py": "print('hi')\n"})
        github.heads[GH_REPO]["main"] = "1" * 40
        return github

    def _service(self, db, fake):
        from tests.test_github_runs import make_service, make_settings

        return make_service(
            db,
            fake,
            settings=make_settings(FORGE_GITHUB_HARNESS_WORKFLOW=GH_WORKFLOW),
        )

    async def _start(self, service, fake, issue: int) -> str:
        fake.seed_issue(GH_REPO, issue, f"task {issue}", f"body {issue}")
        return await service.start_run(
            project_id=GH_PROJECT_ID,
            issue_number=issue,
            issue_title=f"task {issue}",
            issue_description=f"body {issue}",
            author_username="alice",
        )

    async def _go(self, service, issue: int, run_id: str) -> None:
        await service.handle_go(
            project_id=GH_PROJECT_ID,
            issue_number=issue,
            note_text=f"/go {run_id}",
            author_username="alice",
        )

    async def test_four_concurrent_approvals_start_exactly_three_native_jobs(
        self, db, fake, monkeypatch
    ):
        from forge.durable.models import FlowRun as GhFlowRun

        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "3")
        service = self._service(db, fake)
        issues = [GH_BASE_ISSUE + 10 * n for n in range(5)]
        runs = {issue: await self._start(service, fake, issue) for issue in issues}

        # The four go commands arrive TOGETHER — the lease is the only
        # arbiter (a helper-only check_admission result would not count).
        await asyncio.gather(*(self._go(service, issue, runs[issue]) for issue in issues[:4]))

        statuses = {}
        async with db() as session:
            for issue in issues[:4]:
                run = await session.get(GhFlowRun, runs[issue])
                statuses[issue] = run.status
        waiting = [issue for issue, status in statuses.items() if status == "waiting_harness"]
        parked = [issue for issue, status in statuses.items() if status == "blocked"]
        assert len(waiting) == 3  # exactly three may start
        assert len(parked) == 1  # the fourth parks honestly
        # EXACTLY three native job-start requests left the provider.
        assert len(fake.calls_of("dispatch_workflow")) == 3
        parked_run = await _gh_run(db, runs[parked[0]])
        lease = dict(parked_run.evidence or {})["execution_lease"]
        assert lease["acquired"] is False
        assert lease["capacity"]["held"] == 3
        assert lease["capacity"]["limit"] == 3

        # A terminal release frees its slot: the NEXT dispatch acquires it.
        # Cancel one of the LEASE-HOLDING runs — which of the four raced to
        # the park is not deterministic, and issues[0] may be the parked one:
        # ``blocked`` is terminal, so /cancel on it is correctly a no-op and
        # no slot frees (the parked run holds none). That assumption was a
        # ~20% flake under load; picking from ``waiting`` makes the scenario
        # deterministic whichever run lost the lease race.
        cancel_issue = waiting[0]
        await service.handle_cancel(
            project_id=GH_PROJECT_ID,
            issue_number=cancel_issue,
            note_text="/cancel",
            author_username="alice",
        )
        await self._go(service, issues[4], runs[issues[4]])
        fifth = await _gh_run(db, runs[issues[4]])
        assert fifth.status == "waiting_harness"
        assert len(fake.calls_of("dispatch_workflow")) == 4  # one more, not more


async def _gh_run(db, run_id: str):
    from forge.durable.models import FlowRun as GhFlowRun

    async with db() as session:
        return await session.get(GhFlowRun, run_id)


# ---------------------------------------------------------------------------
# R32-E (AT-04) — the dispatch selects the lane's resume mode
# ---------------------------------------------------------------------------


class TestR32EDispatchSelectsTheRequiredResumeMode:
    """The retry's re-dispatch carries ``lane_resume_mode=required``; the
    SHIPPED template's own mapping (evaluated as written, so a template
    mutation fails here) turns it into the lane's ``FORGE_LANE_RESUME=1``,
    which ``resume_mode()`` reads as the required-restore contract; the
    initial dispatch's ``fresh`` demands no restore."""

    TEMPLATE = Path(__file__).resolve().parents[1] / "ci" / "templates" / "forge-harness.github.yml"

    @pytest.fixture()
    async def db(self):
        from sqlalchemy.pool import StaticPool

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
    def fake(self):
        from tests.fixtures.fake_github import FakeGitHub

        github = FakeGitHub()
        github.seed_repo(GH_REPO, {"src/app.py": "print('hi')\n"})
        github.heads[GH_REPO]["main"] = "1" * 40
        github.seed_issue(GH_REPO, GH_BASE_ISSUE, "Add password reset", "Users cannot reset.")
        return github

    def _service(self, db, fake):
        from tests.test_github_runs import make_service, make_settings

        return make_service(
            db,
            fake,
            settings=make_settings(
                FORGE_GITHUB_HARNESS_WORKFLOW=GH_WORKFLOW,
                FORGE_HARNESS_MODEL="glm-5.3-flash[1m]",
            ),
        )

    def _template_resume_env(self, mode: str) -> str:
        """Evaluate the SHIPPED template's ``lane_resume_mode`` →
        ``FORGE_LANE_RESUME`` mapping exactly as written: the line's
        ``${{ ... }}`` expression over GitHub's ``&&``/``||`` string
        grammar, applied to the dispatch input the REAL service sent. A
        template that drops the required mapping makes this fail."""
        for line in self.TEMPLATE.read_text().splitlines():
            if "FORGE_LANE_RESUME:" in line and "${{" in line:
                expression = line.split("${{", 1)[1].rsplit("}}", 1)[0]
                pythonized = (
                    expression.replace("inputs.lane_resume_mode", repr(mode))
                    .replace("&&", " and ")
                    .replace("||", " or ")
                )
                return str(eval(pythonized, {"__builtins__": {}}, {}))  # noqa: S307 — closed literal grammar
        raise AssertionError("the shipped GitHub template lost its FORGE_LANE_RESUME mapping")

    async def _drive_to_failed_with_candidate(self, db, fake, service) -> str:
        """The retryable shape: dispatched once, then terminal with a
        candidate on record (the /retry precondition)."""
        from forge.durable.models import FlowRun as GhFlowRun

        run_id = await service.start_run(
            project_id=GH_PROJECT_ID,
            issue_number=GH_BASE_ISSUE,
            issue_title="Add password reset",
            issue_description="Users cannot reset.",
            author_username="alice",
        )
        await service.handle_go(
            project_id=GH_PROJECT_ID,
            issue_number=GH_BASE_ISSUE,
            note_text=f"/go {run_id}",
            author_username="alice",
        )
        async with db() as session:
            run = await session.get(GhFlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = "harness_start_failed: boom"
            run.candidate_shas = ["c1"]
            await session.commit()
        return run_id

    async def test_the_retry_dispatch_selects_the_mode_the_lane_executes(self, db, fake):
        from forge.lane_driver import resume_mode, resume_requested

        service = self._service(db, fake)

        # The INITIAL dispatch selects fresh: a first run restores nothing.
        run_id = await service.start_run(
            project_id=GH_PROJECT_ID,
            issue_number=GH_BASE_ISSUE,
            issue_title="Add password reset",
            issue_description="Users cannot reset.",
            author_username="alice",
        )
        await service.handle_go(
            project_id=GH_PROJECT_ID,
            issue_number=GH_BASE_ISSUE,
            note_text=f"/go {run_id}",
            author_username="alice",
        )
        (initial,) = fake.dispatch_inputs
        assert initial["inputs"]["lane_resume_mode"] == "fresh"
        fresh_env = self._template_resume_env("fresh")
        assert resume_mode({"FORGE_LANE_RESUME": fresh_env}) == "fresh"
        assert resume_requested({"FORGE_LANE_RESUME": fresh_env}) is False

        # The RETRY continues the work in place — its dispatch carries the
        # REQUIRED mode, and the shipped mapping turns that into the env
        # spelling the lane's required-restore gate reads.
        failed = await self._drive_to_failed_with_candidate(db, fake, service)
        fake.dispatch_inputs.clear()
        await service.handle_retry(
            project_id=GH_PROJECT_ID,
            issue_number=GH_BASE_ISSUE,
            note_text=f"/retry {failed}",
            author_username="alice",
            delivery_id="r32e-delivery-1",
        )

        (retry,) = fake.dispatch_inputs
        assert retry["inputs"]["lane_resume_mode"] == "required"
        required_env = self._template_resume_env("required")
        assert required_env == "1"  # the mapping the template ships
        assert resume_mode({"FORGE_LANE_RESUME": required_env}) == "required"
        assert resume_requested({"FORGE_LANE_RESUME": required_env}) is True

        # The mapping stays closed over the third mode too: an explicit
        # restart maps onto the lane's restart spelling, nothing else does.
        assert self._template_resume_env("restart") == "restart"
        assert resume_mode({"FORGE_LANE_RESUME": "restart"}) == "restart"
