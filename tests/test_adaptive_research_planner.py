"""R28-16 — the bounded research-harness discovery pass (review 1ae5290 §8).

These tests pin the third honest mode's contract:

- the mode gate is a real tri-state (``none`` | ``lexical`` |
  ``research-harness``), fails CLOSED on unknown values, and an empty
  value defers to the legacy ``FORGE_DISCOVERY_ENABLED`` flag so
  existing deployments stay byte-for-byte unchanged;
- the loop is BOUNDED from three sides at once: the tool-call budget
  (``FORGE_RESEARCH_MAX_CALLS``), the wall-clock deadline, and the
  gateway's own token guard (a refused reservation is an honest partial
  stop, never a bypass);
- the model only PROPOSES: every call executes through the frozen
  ``SnapshotToolbox``, so paths outside ``allowed_globs`` do not exist,
  unknown repos/tools are refused omissions, and no write surface is
  ever reachable;
- exhaustion is honest — a budget stop returns the partial investigation
  with ``complete: false`` and a ``stopped_reason``, never an
  evidence-backed success claim;
- the findings become ordinary evidence records of the SAME discovery
  record (ids minted by the stage), the summary feeds the planner as a
  third delimited section with a citation map that validates against
  the record, and a replay never re-pays the model.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.discovery_stage import (
    DIGEST_BEGIN,
    DiscoveryRunContext,
    DiscoveryStageError,
    QuestionsOutstanding,
    enforce_citations_against_snapshot,
    maybe_run_discovery,
)
from forge.adaptive.discovery_tools import SnapshotToolbox
from forge.adaptive.research_planner import (
    FORGE_DISCOVERY_MODE_ENV,
    FORGE_RESEARCH_MAX_CALLS_ENV,
    RESEARCH_BEGIN,
    RESEARCH_END,
    ResearchHarness,
    ResearchRepo,
    attach_research,
    completion_from_llm_client,
    discovery_mode,
    render_research_section,
    research_max_calls,
    research_max_wall_seconds,
    run_research_pass,
)
from forge.durable import FlowRun, Outbox
from forge.models.base import Base

FILES = {
    "src/app/planner.py": ("class LLMPlanner:\n    def plan(self, issue):\n        return issue\n"),
    "src/app/service.py": (
        "from app.planner import LLMPlanner\n"
        "\n"
        "def start_run():\n"
        "    planner = LLMPlanner()\n"
        "    return planner.plan(None)\n"
    ),
    "docs/generated.md": "generated tree noise mentioning LLMPlanner\n",
}

PLANNER_INPUT = "Refactor the LLMPlanner so plan() cites evidence and start_run stays stable."


class ScriptedCompletion:
    """The gateway double: canned responses (or exceptions), call journal."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> object:
        self.calls.append((system, user))
        if not self.responses:
            raise AssertionError("the completion seam was called more times than scripted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class UsageResult:
    """The ``LLMResult`` shape: text plus usage counters."""

    def __init__(self, text: str, input_tokens: int, output_tokens: int) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _repo(
    files: dict[str, str] | None = None, *, globs: list[str] | None = None, key: str = "own"
) -> ResearchRepo:
    return ResearchRepo(
        repo_key=key,
        repository_id="example/repo" if key == "own" else f"partner/{key}",
        source_oid="f" * 40 if key == "own" else "a" * 40,
        toolbox=SnapshotToolbox(dict(FILES if files is None else files), allowed_globs=globs),
    )


def _propose(*calls: dict) -> str:
    return json.dumps({"calls": list(calls), "done": False})


def _done(summary: str = "The planner owns citation rendering.") -> str:
    return json.dumps(
        {
            "done": True,
            "summary": summary,
            "assumptions": ["start_run callers keep the current return shape"],
            "contradictions": [],
        }
    )


# ---------------------------------------------------------------------------
# The mode gate — three honest modes, failing closed
# ---------------------------------------------------------------------------


def test_mode_is_a_tri_state_deferring_to_the_legacy_flag_when_empty():
    assert discovery_mode({}) == "none"  # legacy default: OFF
    assert discovery_mode({"FORGE_DISCOVERY_ENABLED": "1"}) == "lexical"
    assert discovery_mode({FORGE_DISCOVERY_MODE_ENV: "none", "FORGE_DISCOVERY_ENABLED": "1"}) == (
        "none"
    )
    assert discovery_mode({FORGE_DISCOVERY_MODE_ENV: "lexical"}) == "lexical"
    assert discovery_mode({FORGE_DISCOVERY_MODE_ENV: "research-harness"}) == "research-harness"
    assert discovery_mode({FORGE_DISCOVERY_MODE_ENV: "Research-Harness"}) == "research-harness"


def test_unknown_mode_fails_closed_to_none():
    assert discovery_mode({FORGE_DISCOVERY_MODE_ENV: "full-agent"}) == "none"
    assert discovery_mode({FORGE_DISCOVERY_MODE_ENV: "research harness"}) == "none"


def test_budget_env_values_parse_with_safe_defaults(monkeypatch):
    monkeypatch.delenv(FORGE_RESEARCH_MAX_CALLS_ENV, raising=False)
    assert research_max_calls({}) == 10
    assert research_max_calls({FORGE_RESEARCH_MAX_CALLS_ENV: "3"}) == 3
    assert research_max_calls({FORGE_RESEARCH_MAX_CALLS_ENV: "0"}) == 1  # clamped, not zeroed
    assert research_max_calls({FORGE_RESEARCH_MAX_CALLS_ENV: "many"}) == 10
    assert research_max_wall_seconds({}) == 90.0
    assert research_max_wall_seconds({"FORGE_RESEARCH_MAX_WALL_SECONDS": "5"}) == 5.0
    assert research_max_wall_seconds({"FORGE_RESEARCH_MAX_WALL_SECONDS": "soon"}) == 90.0


# ---------------------------------------------------------------------------
# The loop — propose → execute → observe, bounded and read-only
# ---------------------------------------------------------------------------


async def test_loop_executes_proposed_calls_and_completes():
    completion = ScriptedCompletion(
        _propose({"tool": "find_symbol", "repo": "own", "args": {"name": "LLMPlanner"}}),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
    )
    assert outcome.complete is True
    assert outcome.stopped_reason == ""
    assert len(outcome.findings) == 1
    finding = outcome.findings[0]
    assert (finding.path, finding.line) == ("src/app/planner.py", 1)
    assert finding.kind == "research_symbol"
    assert finding.repository_id == "example/repo"
    assert finding.source_oid == "f" * 40
    document = outcome.document
    assert document["complete"] is True
    assert document["repos_consulted"] == ["own"]
    assert document["calls_executed"] == 1
    assert document["summary"] == "The planner owns citation rendering."
    assert document["assumptions"] == ["start_run callers keep the current return shape"]
    # Both iterations saw the issue and the authorized repo menu.
    assert len(completion.calls) == 2
    assert all(PLANNER_INPUT[:40] in user for _system, user in completion.calls)
    assert all("example/repo" in user for _system, user in completion.calls)


async def test_call_budget_caps_the_loop_with_an_explicit_partial():
    # One iteration proposes three calls; the budget of 2 must stop the
    # loop mid-proposal — charged per proposal, executed or not.
    completion = ScriptedCompletion(
        _propose(
            {"tool": "find_symbol", "repo": "own", "args": {"name": "LLMPlanner"}},
            {"tool": "grep", "repo": "own", "args": {"pattern": "LLMPlanner"}},
            {"tool": "read_file", "repo": "own", "args": {"path": "src/app/service.py"}},
        ),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=2, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
    )
    assert outcome.complete is False
    assert outcome.stopped_reason == "max_calls"
    assert outcome.document["calls_proposed"] == 2
    assert outcome.document["calls_executed"] == 2
    assert outcome.document["budget"] == {"max_calls": 2, "wall_seconds": 30.0}
    # ONE completion was spent — the budget stop prevented the second.
    assert len(completion.calls) == 1
    # The partial investigation is honest about what it did establish.
    assert outcome.findings, "the calls executed before the stop kept their findings"


async def test_reads_only_within_the_authorized_snapshot():
    completion = ScriptedCompletion(
        _propose(
            # A path the toolbox's allowed_globs never authorized, an
            # unknown repo key, and one authorized read — the loop must
            # survive the first two as recorded omissions.
            {"tool": "read_file", "repo": "own", "args": {"path": "secrets/env.py"}},
            {"tool": "read_file", "repo": "attacker", "args": {"path": "src/app/service.py"}},
            {"tool": "find_symbol", "repo": "own", "args": {"name": "start_run"}},
        ),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo(globs=["src/**"])},
    )
    assert outcome.complete is True
    omissions = outcome.document["omissions"]
    assert any("outside the authorized snapshot" in item for item in omissions)
    assert any("unknown repository key" in item for item in omissions)
    # Only the authorized read produced findings — all inside src/**.
    assert [f.path for f in outcome.findings] == ["src/app/service.py"]


async def test_unknown_tool_is_refused_charged_and_recorded():
    completion = ScriptedCompletion(
        _propose({"tool": "write_file", "repo": "own", "args": {"path": "x.py", "content": "pwn"}}),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
    )
    assert outcome.complete is True
    assert outcome.findings == ()
    assert any("unknown tool" in item for item in outcome.document["omissions"])
    assert outcome.document["calls_executed"] == 0


async def test_gateway_error_stops_the_loop_partial_and_explicit():
    completion = ScriptedCompletion(RuntimeError("budget_exhausted"))
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
    )
    assert outcome.complete is False
    assert outcome.stopped_reason == "gateway_error: RuntimeError"
    assert outcome.document["stopped_reason"] == "gateway_error: RuntimeError"
    assert outcome.findings == ()


# ---------------------------------------------------------------------------
# NEXT-08 — the model sees what its tools ACTUALLY returned
# ---------------------------------------------------------------------------


async def test_read_file_returns_the_actual_code_to_the_next_prompt():
    """THE regression (review ccab247 §3, Finding 2): a business rule on
    line 3 of a read window — nowhere in the issue text — must appear in
    the NEXT completion input as the actual code, not as
    "read N chars from offset 0" metadata. A mutation that drops result
    bodies fails here."""
    files = {
        "src/rates.py": (
            "def rate_limit():\n"
            '    """Ordinary header docstring."""\n'
            "    return RATE_LIMIT_SENTINEL_9f21\n"
            "    # the business rule lives on line 3, not line 1\n"
        ),
    }
    completion = ScriptedCompletion(
        _propose(
            {"tool": "read_file", "repo": "own", "args": {"path": "src/rates.py", "length": 400}}
        ),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input="Where is the per-tenant rate limit defined?",  # sentinel NOT here
        lexical=[],
        repos={"own": _repo(files)},
    )
    assert outcome.complete is True
    finding = outcome.findings[0]
    assert finding.kind == "research_read"
    assert "RATE_LIMIT_SENTINEL_9f21" in finding.content
    assert len(finding.content) > len(finding.text)  # the window, not the first line
    second_prompt = completion.calls[1][1]
    assert "[tool: read_file own src/rates.py" in second_prompt
    assert "RATE_LIMIT_SENTINEL_9f21" in second_prompt


async def test_list_paths_result_is_selectable_on_the_next_turn():
    """A path the model never saw before (not in the issue, not in the
    menu — the menu shows counts only) is disclosed by the listing and
    selectable next turn: the returned list IS the observation."""
    completion = ScriptedCompletion(
        _propose({"tool": "list_paths", "repo": "own", "args": {"prefix": "src/app"}}),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input="Map the layout before editing anything.",
        lexical=[],
        repos={"own": _repo()},
    )
    assert outcome.complete is True
    assert outcome.findings == ()  # a listing scouts — no citable file:line
    assert outcome.document["calls_executed"] == 1
    second_prompt = completion.calls[1][1]
    assert "[tool: list_paths own prefix src/app]" in second_prompt
    assert "src/app/service.py" in second_prompt


async def test_grep_and_symbol_results_carry_their_lines_into_the_prompt():
    completion = ScriptedCompletion(
        _propose(
            {"tool": "find_symbol", "repo": "own", "args": {"name": "start_run"}},
        ),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
    )
    second_prompt = completion.calls[1][1]
    # The observation carries the actual find_symbol answer — every
    # declaration's file:line:kind:symbol, not just a hit count.
    assert "[tool: find_symbol own name start_run]" in second_prompt
    assert "src/app/service.py:3: function start_run" in second_prompt
    assert outcome.findings[0].path == "src/app/service.py"


async def test_tool_errors_are_observed_not_swallowed():
    """The model SEES its failed call's real error (and the omission is
    still recorded) — an unauthorized read is indistinguishable from an
    unknown one BY DESIGN, and the observation says exactly that."""
    completion = ScriptedCompletion(
        _propose({"tool": "read_file", "repo": "own", "args": {"path": "secrets/env.py"}}),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo(globs=["src/**"])},
    )
    second_prompt = completion.calls[1][1]
    assert "[tool: read_file own secrets/env.py" in second_prompt
    assert "outside the authorized snapshot" in second_prompt
    assert outcome.document["observations"] == {"count": 1, "truncated": 0, "errors": 1}
    assert any("outside the authorized snapshot" in item for item in outcome.document["omissions"])


async def test_observation_truncation_is_explicit_never_silent():
    files = {"src/big.py": "x = 1\n" * 2000}  # 12k chars — past the content cap
    completion = ScriptedCompletion(
        _propose({"tool": "read_file", "repo": "own", "args": {"path": "src/big.py"}}),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo(files)},
    )
    assert len(outcome.findings[0].content) == 2000  # capped content
    second_prompt = completion.calls[1][1]
    assert "output truncated" in second_prompt
    assert outcome.document["observations"]["truncated"] == 1


async def test_older_observations_drop_with_an_explicit_count():
    """Beyond the observation-block budget the OLDEST results drop first,
    with an explicit omitted count and a way to re-read them — the model
    can never treat invisible content as reviewed."""
    files = {f"src/f{i}.py": f"# file {i}\n" + "y = 2\n" * 290 for i in range(4)}
    completion = ScriptedCompletion(
        *(
            _propose({"tool": "read_file", "repo": "own", "args": {"path": f"src/f{i}.py"}})
            for i in range(4)
        ),
        _done(),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=10, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo(files)},
    )
    assert outcome.complete is True
    last_prompt = completion.calls[4][1]  # after all four reads
    assert "older tool results omitted: 1" in last_prompt
    # The MOST RECENT observation stays visible in full.
    assert "[tool: read_file own src/f3.py" in last_prompt
    assert "older tool results omitted: 2" not in last_prompt


async def test_malformed_model_response_stops_the_loop():
    completion = ScriptedCompletion("the planner seems fine, trust me")
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
    )
    assert outcome.complete is False
    assert outcome.stopped_reason == "malformed_response"


async def test_wall_clock_deadline_stops_the_loop():
    completion = ScriptedCompletion(
        _propose({"tool": "list_paths", "repo": "own", "args": {"prefix": "src/"}}),
        _propose({"tool": "list_paths", "repo": "own", "args": {"prefix": "docs/"}}),
        _propose({"tool": "list_paths", "repo": "own", "args": {"prefix": "x/"}}),
    )
    ticks = iter([0.0, 0.0, 10.0, 10.0, 100.0])  # deadline crossed mid-loop

    def now() -> float:
        return next(ticks, 100.0)

    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=9, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
        now=now,
    )
    assert outcome.complete is False
    assert outcome.stopped_reason == "wall_time"
    assert outcome.document["wall_seconds_used"] >= 0.0


async def test_usage_counters_accumulate_across_iterations():
    completion = ScriptedCompletion(
        UsageResult(
            _propose({"tool": "grep", "repo": "own", "args": {"pattern": "LLMPlanner"}}), 100, 7
        ),
        UsageResult(_done(), 200, 9),
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
    )
    # Full usage coverage: the totals are exact AND the lower bounds agree.
    assert outcome.document["tokens"] == {
        "input": 300,
        "output": 16,
        "input_lower_bound": 300,
        "output_lower_bound": 16,
        "unknown_usage_calls": 0,
    }


async def test_unknown_usage_makes_the_subtotal_a_lower_bound_not_a_total():
    """NEXT-09: ``[None, 100]`` must NOT re-total as ``100`` — once any
    call reports unknown usage the subtotal is sticky-unknown: the exact
    totals are None, the KNOWN parts ride as explicit lower bounds, and
    the unknown-usage calls are counted."""
    completion = ScriptedCompletion(
        UsageResult(
            _propose({"tool": "grep", "repo": "own", "args": {"pattern": "LLMPlanner"}}), 100, 7
        ),
        _done(),  # a bare string result: no usage counters on this call
    )
    outcome = await run_research_pass(
        ResearchHarness(complete=completion, max_calls=5, wall_seconds=30.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
    )
    assert outcome.complete is True
    tokens = outcome.document["tokens"]
    assert tokens["input"] is None
    assert tokens["output"] is None
    assert tokens["input_lower_bound"] == 100
    assert tokens["output_lower_bound"] == 7
    assert tokens["unknown_usage_calls"] == 1


async def test_a_slow_completion_is_cut_off_inside_the_remaining_window():
    """NEXT-09: the wall budget is a BOUNDED await, not check-then-await —
    a completion slower than the remaining pass window is cancelled inside
    it, and (the provider may already have accepted the request) its usage
    stays unknown: a lower bound, never silently zero."""
    import asyncio

    ticks = iter([0.0, 0.0])  # started; the first loop check

    def now() -> float:
        return next(ticks, 1.0 - 1e-6)  # then the wall nearly runs out

    class SlowCompletion:
        async def __call__(self, system: str, user: str) -> object:
            await asyncio.sleep(0.05)
            raise AssertionError("the slow completion must be cut off before answering")

    outcome = await run_research_pass(
        ResearchHarness(complete=SlowCompletion(), max_calls=5, wall_seconds=1.0),
        planner_input=PLANNER_INPUT,
        lexical=[],
        repos={"own": _repo()},
        now=now,
    )
    assert outcome.complete is False
    assert outcome.stopped_reason == "wall_time"
    assert outcome.iterations == 0
    tokens = outcome.document["tokens"]
    assert tokens["input"] is None
    assert tokens["unknown_usage_calls"] == 1


async def test_completion_adapter_targets_the_gateway_with_json_mode():
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return UsageResult(_done(), 10, 2)

    client = FakeClient()
    complete = completion_from_llm_client(client, tier="fast", flow_run_id="run-1")
    await complete("system", "user")
    (call,) = client.calls
    assert call["tier"] == "fast"
    assert call["flow_run_id"] == "run-1"
    assert call["json_mode"] is True


# ---------------------------------------------------------------------------
# The planner-facing section
# ---------------------------------------------------------------------------


def test_render_research_section_validates_citations_against_the_record():
    record = {
        "discovery_id": "disc-1",
        "evidence": [{"id": "ev-1"}, {"id": "ev-2"}],
        "research": {
            "complete": True,
            "stopped_reason": "",
            "repos_consulted": ["own"],
            "omissions": [],
            "summary": "The planner owns citation rendering.",
            "assumptions": ["callers keep the shape"],
            "contradictions": [],
            "findings": [
                {
                    "evidence_id": "ev-1",
                    "path": "src/app/planner.py",
                    "line": 1,
                    "kind": "research_symbol",
                },
                # ev-77 was never recorded — the citation map must drop it.
                {"evidence_id": "ev-77", "path": "gone.py", "line": 9, "kind": "research_read"},
            ],
        },
    }
    section = render_research_section(record)
    assert section.startswith(RESEARCH_BEGIN) and section.endswith(RESEARCH_END)
    body = json.loads(section[len(RESEARCH_BEGIN) + 1 : section.index(RESEARCH_END)])
    assert [c["evidence"] for c in body["citations"]] == ["ev-1"]
    assert body["summary"] == "The planner owns citation rendering."
    assert render_research_section({"discovery_id": "disc-1"}) == ""


def test_render_research_section_is_bounded():
    record = {
        "discovery_id": "disc-1",
        "evidence": [{"id": f"ev-{n}"} for n in range(1, 30)],
        "research": {
            "complete": False,
            "stopped_reason": "max_calls",
            "repos_consulted": ["own"],
            "omissions": [f"omission number {n} with some words" for n in range(40)],
            "summary": "s" * 400,
            "assumptions": [f"assumption {n}" for n in range(10)],
            "contradictions": [],
            "findings": [
                {
                    "evidence_id": f"ev-{n}",
                    "path": f"src/f{n}.py",
                    "line": 1,
                    "kind": "research_read",
                }
                for n in range(1, 30)
            ],
        },
    }
    section = render_research_section(record, max_chars=1200)
    assert len(section) <= 1300  # bounded, with the truncation marker accounted
    body = json.loads(section[len(RESEARCH_BEGIN) + 1 : section.index(RESEARCH_END)])
    assert body["truncated"] is True
    assert body["dropped"] > 0


def test_attach_research_keeps_the_section_and_cuts_the_head_under_cap():
    section = f'{RESEARCH_BEGIN}\n{{"k": 1}}\n{RESEARCH_END}'
    attached = attach_research("issue text", section, cap=10_000)
    assert attached.endswith(section)
    tight = attach_research("i" * 5000, section, cap=len(section) + 200)
    assert tight.endswith(section)
    assert "truncated to" in tight


# ---------------------------------------------------------------------------
# The stage integration — research-harness mode through the durable stage
# ---------------------------------------------------------------------------


@pytest.fixture()
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_run(factory, run_id: str = "run-research-1") -> None:
    async with factory() as session:
        session.add(FlowRun(id=run_id, project_id=1, status="planning"))
        await session.commit()


async def _record_of(factory, run_id: str = "run-research-1") -> dict:
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        return dict((run.evidence or {}).get("discovery") or {})


def _ctx(factory, harness: ResearchHarness | None) -> DiscoveryRunContext:
    return DiscoveryRunContext(
        run_id="run-research-1",
        project_id=1,
        session_factory=factory,
        snapshot_files=dict(FILES),
        repository_id="example/repo",
        source_oid="f" * 40,
        research=harness,
    )


async def test_research_harness_mode_feeds_the_planner_with_cited_evidence(
    session_factory, monkeypatch
):
    monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "research-harness")
    await _seed_run(session_factory)
    completion = ScriptedCompletion(
        _propose({"tool": "find_symbol", "repo": "own", "args": {"name": "LLMPlanner"}}),
        _done(),
    )
    augmented = await maybe_run_discovery(
        _ctx(session_factory, ResearchHarness(complete=completion)), PLANNER_INPUT
    )
    # All three sections ride the planner input: digest + research (+ no answers).
    assert DIGEST_BEGIN in augmented
    assert RESEARCH_BEGIN in augmented
    assert augmented.index(DIGEST_BEGIN) < augmented.index(RESEARCH_BEGIN)
    record = await _record_of(session_factory)
    research = record["research"]
    assert research["complete"] is True
    # The finding joined the record as ORDINARY evidence, citable and bound.
    research_entry = next(e for e in record["evidence"] if e["kind"] == "research_symbol")
    assert (research_entry["path"], research_entry["line"]) == ("src/app/planner.py", 1)
    assert research_entry["repository_id"] == "example/repo"
    # The summary's citation map resolves to recorded ids only.
    body = json.loads(
        augmented[
            augmented.index(RESEARCH_BEGIN) + len(RESEARCH_BEGIN) + 1 : augmented.index(
                RESEARCH_END
            )
        ]
    )
    assert body["citations"][0]["evidence"] == research_entry["id"]
    # A plan citing the research evidence validates against the snapshot binding.
    plan = {
        "steps": [
            {
                "step_id": "S1",
                "objective": f"Refactor the declaration cited as evidence:{research_entry['id']}",
            }
        ]
    }
    enforce_citations_against_snapshot(plan, record)  # no raise
    # The stage journaled the research-mode announcement.
    async with session_factory() as session:
        events = [
            row.event_type for row in ((await session.execute(select(Outbox))).scalars().all())
        ]
    assert "plan.research_mode" in events


async def test_research_mode_without_a_harness_fails_loud(session_factory, monkeypatch):
    monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "research-harness")
    await _seed_run(session_factory)
    with pytest.raises(DiscoveryStageError, match="without a configured research"):
        await maybe_run_discovery(_ctx(session_factory, None), PLANNER_INPUT)
    record = await _record_of(session_factory)
    assert record["status"] == "failed"
    assert "research" in (record.get("block_reason") or "")


async def test_replay_never_re_pays_the_model(session_factory, monkeypatch):
    monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "research-harness")
    await _seed_run(session_factory)
    completion = ScriptedCompletion(
        _propose({"tool": "find_symbol", "repo": "own", "args": {"name": "LLMPlanner"}}),
        _done(),
    )
    first = await maybe_run_discovery(
        _ctx(session_factory, ResearchHarness(complete=completion)), PLANNER_INPUT
    )
    spent = len(completion.calls)
    assert spent == 2
    # A "restart" replays the completed record — the model is not called again.
    second = await maybe_run_discovery(
        _ctx(session_factory, ResearchHarness(complete=completion)), PLANNER_INPUT
    )
    assert len(completion.calls) == spent
    assert second == first


async def test_lexical_mode_runs_no_research_leg(session_factory, monkeypatch):
    """The lexical mode is unchanged: no research document, no section,
    and a harness on the context is simply unused (the mode, not the
    wiring, selects the profile)."""
    monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "lexical")
    await _seed_run(session_factory)
    completion = ScriptedCompletion(_done())
    augmented = await maybe_run_discovery(
        _ctx(session_factory, ResearchHarness(complete=completion)), PLANNER_INPUT
    )
    assert len(completion.calls) == 0  # the model was never consulted
    assert DIGEST_BEGIN in augmented
    assert RESEARCH_BEGIN not in augmented
    record = await _record_of(session_factory)
    assert "research" not in record
    assert record["status"] == "complete"


async def test_questions_can_cite_research_evidence(session_factory, monkeypatch):
    """The research findings join the record BEFORE question emission, so
    a question may cite them — and the wait still refuses planning."""
    monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "research-harness")
    await _seed_run(session_factory)
    completion = ScriptedCompletion(
        _propose({"tool": "find_symbol", "repo": "own", "args": {"name": "LLMPlanner"}}),
        _done(),
    )

    def questions(planner_input, evidence_docs):
        research_ids = [d["id"] for d in evidence_docs if d["kind"].startswith("research_")]
        assert research_ids, "research evidence must be visible to the question source"
        return [
            {
                "text": "Keep the current return shape?",
                "criticality": "critical",
                "citations": [research_ids[0]],
            }
        ]

    ctx = DiscoveryRunContext(
        run_id="run-research-1",
        project_id=1,
        session_factory=session_factory,
        snapshot_files=dict(FILES),
        repository_id="example/repo",
        source_oid="f" * 40,
        research=ResearchHarness(complete=completion),
        question_source=questions,
    )
    with pytest.raises(QuestionsOutstanding):
        await maybe_run_discovery(ctx, PLANNER_INPUT)
    record = await _record_of(session_factory)
    assert record["status"] == "waiting_question"
    assert record["research"]["complete"] is True
