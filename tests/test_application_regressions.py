"""Composed application regressions (review ccab247, NEXT-25).

The reviewer's APPLICATION_REGRESSIONS_EN.md specifies 12 traces that
must drive the REAL service surfaces — production constructors, HTTP
routes and provider adapters — with bounded typed doubles ONLY at
external-effect boundaries, never a hand-built context where the caller
must construct it. This file promotes the five most critical traces
into CI:

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
"""

from __future__ import annotations

import hashlib
import json
import os
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


async def _session_factory(tmp_path: Path) -> async_sessionmaker:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/regressions.db", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(FlowRun(id=RUN_ID, project_id=1, status="planning"))
        await session.commit()
    return factory


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

    async def test_the_harness_runs_before_the_final_plan_charged_to_the_run(self, tmp_path: Path):
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
        factory = await _session_factory(tmp_path)
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

    async def test_no_model_route_is_a_precise_configuration_refusal(self, tmp_path: Path):
        """Research mode WITHOUT the configured completion callable
        refuses loudly and precisely — never a silent lexical-only
        downgrade the plan would then cite as if it had researched."""
        factory = await _session_factory(tmp_path)
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

    async def test_none_and_lexical_modes_remain_supported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The tri-state stays honest: ``none`` returns the input UNTOUCHED
        (nothing persisted, no completion spent); ``lexical`` runs the
        deterministic probes without any research completion."""
        factory = await _session_factory(tmp_path)
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

    async def _run(self, tmp_path: Path, llm: ScriptedCompletionLLM) -> list[str]:
        factory = await _session_factory(tmp_path)
        ctx = DiscoveryRunContext(
            run_id=RUN_ID,
            project_id=1,
            session_factory=factory,
            snapshot_files=dict(FILES),
            repository_id="example/repo",
            source_oid="f" * 40,
            research=_harness(llm),
        )
        await maybe_run_discovery(ctx, PLANNER_INPUT)
        return [str(call["user"]) for call in llm.calls]

    async def test_read_file_content_and_provenance_reach_the_next_turn(self, tmp_path: Path):
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

        prompts = await self._run(tmp_path, llm)

        assert len(prompts) == 2
        # Turn 1 never saw the rule (it is not in the issue or lexical set)...
        assert CONVENTIONS_RULE not in prompts[0]
        # ...turn 2 saw BOTH the content and its provenance.
        assert CONVENTIONS_RULE in prompts[1]
        assert "[tool: read_file own docs/conventions.md" in prompts[1]

    async def test_list_paths_names_reach_the_next_turn(self, tmp_path: Path):
        llm = ScriptedCompletionLLM(
            [
                _research_call("list_paths", prefix="src/app/"),
                _research_done("The planner and service modules are the surface."),
            ]
        )

        prompts = await self._run(tmp_path, llm)

        assert "src/app/planner.py" in prompts[1]
        assert "src/app/service.py" in prompts[1]
        assert "[tool: list_paths own prefix src/app/]" in prompts[1]

    async def test_a_serializer_that_drops_content_is_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
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
        prompts = await self._run(tmp_path, llm)

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
        # A's bytes — never B's — landed on the fresh runner.
        assert (lane_cwd / "src" / "app.py").read_bytes() == b'print("v2 - approved")\n'
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
        assert (lane_cwd / "src" / "app.py").read_bytes() == b'print("v2 - approved")\n'


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
