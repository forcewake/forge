"""The bounded research-harness discovery pass (R28-16, review 1ae5290 §8).

The review's finding: the durable discovery stage (NXT-05) gathers real
repository evidence, but its probes are deterministic keyword/symbol
lookups — useful, not yet a bounded tool-using investigation. This module
is the third honest mode: alongside ``none`` (discovery off) and
``lexical`` (the existing deterministic probes) a run may opt into
``research-harness``, where ONE bounded research loop drives additional
read-only tool calls over the SAME authorized snapshot the lexical pass
used, and the resulting research summary feeds the planner BESIDE the
lexical evidence — lexical discovery is never discarded.

The loop is deliberately NOT a general agent:

- **One completion per iteration** through the EXISTING litellm gateway
  (the planner's ``LLMClient`` — its ``BudgetGuard`` reserves calls and
  tokens before the provider is contacted, so the token budget is the
  run's own budget system, not a new one). The model proposes which
  tools to call next; it never executes anything itself.
- **Every proposed call executes through the frozen
  :class:`~forge.adaptive.discovery_tools.SnapshotToolbox`** — the same
  read-only, scope-filtered, output-budgeted surface the lexical probes
  use. No repository write credentials exist on this path; a path
  outside ``allowed_globs`` does not exist for the toolbox, so it cannot
  exist for the model either.
- **The model SEES what its tools returned (NEXT-08)**: every executed
  call produces a bounded :class:`ToolObservation` whose content — the
  real read window, the actual path listing, the matched lines — rides
  the NEXT completion's prompt (``[tool: read_file path offset N]`` +
  the actual code below it). A tool's output is never silently
  discarded; truncated or dropped (too-old) observations are flagged and
  counted so invisible content can never pose as reviewed content.
- **The budget bounds the loop from three sides at once**: a maximum
  number of tool calls (:data:`FORGE_RESEARCH_MAX_CALLS_ENV`, default
  :data:`RESEARCH_MAX_CALLS_DEFAULT`), a wall-clock deadline
  (:data:`FORGE_RESEARCH_MAX_WALL_SECONDS_ENV`, enforced as a BOUNDED
  await on each completion — a slow final call is cut off inside the
  remaining window, never after it), and the token budget the gateway's
  own guard enforces (a refused reservation stops the loop like any
  other exhaustion).
- **Exhaustion is honest**: a loop that stops on any budget returns the
  partial investigation with ``complete: false`` and a ``stopped_reason``
  — never an evidence-backed success claim. The research document
  records consulted repositories, omissions (refused or failed calls)
  and the model's declared assumptions and contradictions, so the
  planner sees what the pass did NOT establish too.

The findings become ordinary evidence records in the SAME discovery
record (ids minted by the stage, bound to each finding's repository/OID/
path/line), so a plan citing them validates against the record's
authorized snapshot binding exactly like a lexical citation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from forge.adaptive.discovery_tools import SnapshotToolbox

logger = logging.getLogger(__name__)

__all__ = [
    "FORGE_DISCOVERY_MODE_ENV",
    "FORGE_RESEARCH_MAX_CALLS_ENV",
    "FORGE_RESEARCH_MAX_WALL_SECONDS_ENV",
    "LEXICAL_MODE",
    "NONE_MODE",
    "RESEARCH_BEGIN",
    "RESEARCH_END",
    "RESEARCH_HARNESS_MODE",
    "RESEARCH_MAX_CALLS_DEFAULT",
    "RESEARCH_MAX_WALL_SECONDS_DEFAULT",
    "RESEARCH_SUMMARY_MAX_CHARS",
    "RESEARCH_SCHEMA",
    "OBSERVATION_CONTENT_MAX_CHARS",
    "OBSERVATION_BLOCK_MAX_CHARS",
    "CompletionFn",
    "ResearchFinding",
    "ResearchHarness",
    "ResearchOutcome",
    "ResearchRepo",
    "ToolObservation",
    "attach_research",
    "completion_from_llm_client",
    "discovery_mode",
    "render_research_section",
    "research_max_calls",
    "research_max_wall_seconds",
    "run_research_pass",
]

#: The env var that selects the discovery mode (R28-16's tri-state).
FORGE_DISCOVERY_MODE_ENV = "FORGE_DISCOVERY_MODE"

#: The three honest modes. ``none`` and ``lexical`` are the legacy
#: ``FORGE_DISCOVERY_ENABLED`` outcomes; ``research-harness`` adds the
#: bounded research loop of this module on top of the lexical probes.
NONE_MODE = "none"
LEXICAL_MODE = "lexical"
RESEARCH_HARNESS_MODE = "research-harness"

#: The env var bounding the research loop's tool calls.
FORGE_RESEARCH_MAX_CALLS_ENV = "FORGE_RESEARCH_MAX_CALLS"

#: The env var bounding the research loop's wall clock (seconds).
FORGE_RESEARCH_MAX_WALL_SECONDS_ENV = "FORGE_RESEARCH_MAX_WALL_SECONDS"

RESEARCH_MAX_CALLS_DEFAULT = 10
RESEARCH_MAX_WALL_SECONDS_DEFAULT = 90.0

#: Delimiters around the planner-facing research summary — the same
#: survival property as the digest and answers sections, a third
#: namespace: research summaries never masquerade as either.
RESEARCH_BEGIN = "<<<FORGE_DISCOVERY_RESEARCH"
RESEARCH_END = "FORGE_DISCOVERY_RESEARCH>>>"

#: The research-summary budget (chars). The section rides beside the
#: evidence digest and answers inside the planner's input cap.
RESEARCH_SUMMARY_MAX_CHARS = 2000

#: The schema stamp of the research document inside the discovery record.
RESEARCH_SCHEMA = "forge.discovery.research/1"

#: How many tool calls one model iteration may propose (a proposal is
#: bounded too — one turn cannot ask for the whole repository).
_MAX_CALLS_PER_ITERATION = 3

#: How many findings one tool call may contribute (the toolbox answers
#: are already output-budgeted; this keeps the durable record bounded).
_MAX_FINDINGS_PER_CALL = 3

#: How many omissions are recorded verbatim (the count is always exact;
#: the list is capped for the record's size).
_MAX_OMISSIONS_RECORDED = 12

#: The per-observation content cap (chars) — NEXT-08: the ACTUAL tool
#: output (a full read window, a real path listing, the matched lines)
#: the next model iteration must see, bounded so one call cannot flood
#: the prompt or the durable record.
OBSERVATION_CONTENT_MAX_CHARS = 2000

#: The total budget of the prompt's tool-observation block (chars). Older
#: observations drop out FIRST with an explicit omitted count — the model
#: always knows what it can no longer see, and can re-read the exact
#: window with ``read_file`` (never treat invisible content as reviewed).
OBSERVATION_BLOCK_MAX_CHARS = 6000

#: The closed tool vocabulary the model may propose. Everything executes
#: through :class:`SnapshotToolbox` — read-only by construction.
_TOOL_NAMES = frozenset({"read_file", "list_paths", "grep", "find_symbol", "find_references"})

#: async (system, user) -> the completion's text (or an object exposing
#: ``.text`` plus optional ``.input_tokens``/``.output_tokens`` — the
#: ``LLMResult`` shape, so usage accounting survives the seam).
CompletionFn = Callable[[str, str], Awaitable[Any]]


def discovery_mode(env: Mapping[str, str] | None = None) -> str:
    """The resolved discovery mode: ``none`` | ``lexical`` | ``research-harness``.

    An explicit :data:`FORGE_DISCOVERY_MODE_ENV` value wins: ``none``
    disables discovery outright, ``lexical`` and ``research-harness``
    select their profiles. An EMPTY value defers to the legacy
    :data:`~forge.adaptive.discovery_stage.FORGE_DISCOVERY_ENABLED_ENV`
    flag (on → ``lexical``, off → ``none``) so existing deployments are
    byte-for-byte unchanged. An UNKNOWN value fails CLOSED to ``none``
    with a warning — an operator typo must narrow scope, never widen it.
    """
    source = os.environ if env is None else env
    raw = str(source.get(FORGE_DISCOVERY_MODE_ENV, "")).strip().lower()
    if raw in ("", NONE_MODE, LEXICAL_MODE, RESEARCH_HARNESS_MODE):
        if raw == NONE_MODE:
            return NONE_MODE
        if raw == LEXICAL_MODE:
            return LEXICAL_MODE
        if raw == RESEARCH_HARNESS_MODE:
            return RESEARCH_HARNESS_MODE
        from forge.adaptive.discovery_stage import discovery_enabled

        return LEXICAL_MODE if discovery_enabled(env) else NONE_MODE
    logger.warning(
        "unknown %s value %r — failing closed to %s (expected none|lexical|research-harness)",
        FORGE_DISCOVERY_MODE_ENV,
        raw,
        NONE_MODE,
    )
    return NONE_MODE


def research_max_calls(env: Mapping[str, str] | None = None) -> int:
    """The research loop's tool-call budget (>= 1, default 10)."""
    source = os.environ if env is None else env
    raw = str(source.get(FORGE_RESEARCH_MAX_CALLS_ENV, "")).strip()
    try:
        value = int(raw) if raw else RESEARCH_MAX_CALLS_DEFAULT
    except ValueError:
        logger.warning(
            "invalid %s=%r — using the default %d",
            FORGE_RESEARCH_MAX_CALLS_ENV,
            raw,
            RESEARCH_MAX_CALLS_DEFAULT,
        )
        return RESEARCH_MAX_CALLS_DEFAULT
    return max(1, value)


def research_max_wall_seconds(env: Mapping[str, str] | None = None) -> float:
    """The research loop's wall-clock budget in seconds (>= 1, default 90)."""
    source = os.environ if env is None else env
    raw = str(source.get(FORGE_RESEARCH_MAX_WALL_SECONDS_ENV, "")).strip()
    try:
        value = float(raw) if raw else RESEARCH_MAX_WALL_SECONDS_DEFAULT
    except ValueError:
        logger.warning(
            "invalid %s=%r — using the default %s",
            FORGE_RESEARCH_MAX_WALL_SECONDS_ENV,
            raw,
            RESEARCH_MAX_WALL_SECONDS_DEFAULT,
        )
        return RESEARCH_MAX_WALL_SECONDS_DEFAULT
    return max(1.0, value)


def completion_from_llm_client(
    client: Any,
    *,
    tier: str,
    flow_run_id: str | None = None,
    max_tokens: int = 1024,
    role: str = "research",
) -> CompletionFn:
    """Adapt an :class:`~forge.factory.llm.LLMClient` to :data:`CompletionFn`.

    The client's ``complete`` goes through the litellm gateway with its
    ``BudgetGuard`` attached, so every research completion reserves from
    the RUN's budget — the token ceiling is the budget system's, and a
    refused reservation surfaces here as an error the loop records as an
    honest partial stop (``gateway_error``), never a bypass.
    """

    async def _complete(system: str, user: str) -> Any:
        return await client.complete(
            tier=tier,
            system=system,
            user=user,
            role=role,
            flow_run_id=flow_run_id,
            json_mode=True,
            max_tokens=max_tokens,
        )

    return _complete


@dataclass(frozen=True)
class ResearchRepo:
    """One authorized repository the research loop may read.

    ``toolbox`` is the SAME frozen, scope-filtered
    :class:`SnapshotToolbox` the lexical probes ran over — the loop
    inherits the authorization (and its blindness: paths outside
    ``allowed_globs`` raise ``KeyError`` exactly like unknown ones).
    """

    repo_key: str
    repository_id: str
    source_oid: str
    toolbox: SnapshotToolbox


@dataclass(frozen=True)
class ResearchFinding:
    """One citable research result, pre-id: the stage mints the evidence id."""

    repo_key: str
    repository_id: str
    source_oid: str
    path: str
    line: int
    kind: str
    detail: str
    text: str
    #: The ACTUAL bounded tool output behind the finding (NEXT-08) — the
    #: full read window or the matched line, not just metadata: what the
    #: research model observed and what the cited bytes actually said.
    content: str = ""


@dataclass(frozen=True)
class ToolObservation:
    """What one proposed call ACTUALLY returned (NEXT-08, review ccab247 §3).

    The observation is the research model's FEEDBACK channel: the loop's
    next completion sees the tool's real bounded output — or its real
    error — never a silently discarded result. It is deliberately a
    SEPARATE representation from :class:`ResearchFinding` (the citation
    anchor bound to repository/OID/path/line): an observation says what a
    tool returned on the last turn; a finding says what a plan may cite.
    """

    tool: str
    repo_key: str
    #: The compact call identity rendered into the prompt header (e.g.
    #: ``"src/app/service.py offset 0 length 40"``).
    call: str
    #: The bounded ACTUAL output ("" when the call errored).
    content: str
    #: "" on success; the omission reason on failure.
    error: str = ""
    #: True when the content was cut (toolbox budget or the char cap) —
    #: the model must know the output continues beyond what it sees.
    truncated: bool = False

    def render(self) -> str:
        """The prompt block: ``[tool: name repo args]`` + the actual output."""
        header = f"[tool: {self.tool} {self.repo_key} {self.call}]".rstrip()
        if self.error:
            return f"{header}\nerror: {self.error}"
        suffix = (
            "\n(output truncated — re-read with offset/length for the rest)"
            if self.truncated
            else ""
        )
        return f"{header}\n{self.content}{suffix}"


@dataclass(frozen=True)
class ResearchHarness:
    """The research-loop configuration carried on the run context.

    ``complete`` is the gateway seam (:data:`CompletionFn`); ``max_calls``
    and ``wall_seconds`` default to their env values when ``None``.
    """

    complete: CompletionFn
    max_calls: int | None = None
    wall_seconds: float | None = None

    def resolve(self, env: Mapping[str, str] | None = None) -> tuple[int, float]:
        """The effective ``(max_calls, wall_seconds)`` budget."""
        calls = self.max_calls if self.max_calls is not None else research_max_calls(env)
        wall = (
            self.wall_seconds if self.wall_seconds is not None else research_max_wall_seconds(env)
        )
        return max(1, int(calls)), max(1.0, float(wall))


@dataclass(frozen=True)
class ResearchOutcome:
    """What one research pass established — findings plus the honest document.

    ``document`` is the durable research record (``complete`` is true
    ONLY when the model declared the investigation done; every budget
    stop carries a ``stopped_reason``). ``findings`` are handed to the
    stage, which mints evidence ids and binds them into the record.
    """

    findings: tuple[ResearchFinding, ...]
    document: dict[str, Any]
    iterations: int = 0
    complete: bool = False
    stopped_reason: str = ""


_SYSTEM_PROMPT = (
    "You are a bounded repository research assistant inside a planning "
    "pipeline. You propose read-only tool calls over a FROZEN repository "
    "snapshot to investigate the issue; a harness executes them and shows "
    "you the bounded results. You have a SMALL budget of tool calls — spend "
    "it on the few reads that resolve the issue's real dependencies and "
    "contracts, not on exhaustive listing.\n"
    "Respond with ONLY a JSON object:\n"
    '{"calls": [{"tool": "<name>", "repo": "<key>", "args": {...}}], '
    '"done": false}\n'
    "or, when the investigation is sufficient:\n"
    '{"done": true, "summary": "<what you established, citing file paths>", '
    '"assumptions": ["..."], "contradictions": ["..."]}\n'
    "Tools: read_file{path, offset, length}, list_paths{prefix}, "
    "grep{pattern, is_regex}, find_symbol{name}, find_references{name}. "
    "Every call must name the repo key it targets. Paths outside the "
    "authorized snapshot do not exist. Never invent file contents."
)


def _first_json_object(text: str) -> dict[str, Any] | None:
    """The first balanced ``{...}`` in *text* (string/escape aware), or None.

    Local on purpose: :mod:`forge.adaptive.research_planner` stays
    import-light (no httpx/sqlalchemy chain) so the discovery substrate
    can use it freely.
    """
    cleaned = text.strip()
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(cleaned[start : index + 1])
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _normalize_completion(result: Any) -> tuple[str, int | None, int | None]:
    """``(text, input_tokens, output_tokens)`` from a completion result."""
    text = getattr(result, "text", None)
    if text is None:
        return str(result), None, None
    return (
        str(text),
        getattr(result, "input_tokens", None),
        getattr(result, "output_tokens", None),
    )


def _bounded_content(text: str, *, cap: int = OBSERVATION_CONTENT_MAX_CHARS) -> tuple[str, bool]:
    """``(content, truncated)`` — the actual output capped at *cap* chars."""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _render_match_lines(matches: list[Mapping[str, Any]]) -> str:
    """``path:line: text`` per match — grep / find_references output."""
    return "\n".join(
        f"{match.get('path')}:{match.get('line_no')}: {match.get('text')}" for match in matches
    )


def _render_symbol_lines(symbols: list[Mapping[str, Any]]) -> str:
    """``path:line: kind symbol`` per declaration — find_symbol output."""
    return "\n".join(
        f"{symbol.get('path')}:{symbol.get('line_no')}: {symbol.get('kind')} {symbol.get('symbol')}"
        for symbol in symbols
    )


def _render_observations(observations: list[ToolObservation]) -> str:
    """The recent observations, oldest-first, within the block budget.

    Older observations drop FIRST and the block says exactly how many are
    gone — the model can never treat invisible content as reviewed, and
    ``read_file`` re-reads any exact window still needed (NEXT-08's
    "retained provenance and a way to re-read exact content").
    """
    if not observations:
        return "(none yet — this is your first iteration)"
    kept: list[str] = []
    budget = OBSERVATION_BLOCK_MAX_CHARS
    omitted = 0
    for index in range(len(observations) - 1, -1, -1):
        rendered = observations[index].render()
        if kept and len(rendered) + 1 > budget:
            omitted = index + 1
            break
        kept.append(rendered)
        budget -= len(rendered) + 1
    kept.reverse()
    block = "\n\n".join(kept)
    if omitted:
        block += (
            f"\n\n[older tool results omitted: {omitted} — re-read exact windows "
            "with read_file if still needed]"
        )
    return block


def _render_user_prompt(
    planner_input: str,
    lexical: list[dict[str, Any]],
    repos: Mapping[str, ResearchRepo],
    findings: list[ResearchFinding],
    observations: list[ToolObservation],
    remaining_calls: int,
) -> str:
    """The iteration prompt: issue, lexical evidence, repo menu, findings,
    and — NEXT-08 — what the previous calls' tools ACTUALLY returned (the
    real bounded output, not path/kind/metadata alone)."""
    menu = json.dumps(
        {
            key: {
                "repository_id": repo.repository_id,
                "paths_visible": repo.toolbox.path_count,
            }
            for key, repo in sorted(repos.items())
        },
        sort_keys=True,
    )
    gathered = json.dumps(
        [
            {
                "repo": finding.repo_key,
                "path": finding.path,
                "line": finding.line,
                "kind": finding.kind,
                "detail": finding.detail,
            }
            for finding in findings[-_MAX_FINDINGS_PER_CALL * 4 :]
        ],
        sort_keys=True,
    )
    return (
        f"ISSUE:\n{planner_input[:4000]}\n\n"
        f"LEXICAL EVIDENCE ALREADY GATHERED:\n{json.dumps(lexical[:36], sort_keys=True)}\n\n"
        f"AUTHORIZED REPOSITORIES (call tools with these repo keys):\n{menu}\n\n"
        f"FINDINGS SO FAR (citable file:line anchors):\n{gathered}\n\n"
        f"LAST TOOL RESULTS (what your previous calls actually returned):\n"
        f"{_render_observations(observations)}\n\n"
        f"TOOL CALLS REMAINING: {remaining_calls}\n"
    )


def _finding_from_symbol(repo: ResearchRepo, symbol: Mapping[str, Any]) -> ResearchFinding | None:
    path = str(symbol.get("path") or "")
    line_no = int(symbol.get("line_no") or 0)
    if not path or line_no < 1:
        return None
    window = repo.toolbox.read_file(path, offset=line_no - 1, length=1)
    line_text = str(window.get("content") or "")
    return ResearchFinding(
        repo_key=repo.repo_key,
        repository_id=repo.repository_id,
        source_oid=repo.source_oid,
        path=path,
        line=line_no,
        kind="research_symbol",
        detail=str(symbol.get("symbol") or ""),
        text=line_text,
        content=line_text,
    )


def _finding_from_line(
    repo: ResearchRepo, match: Mapping[str, Any], kind: str, detail: str
) -> ResearchFinding | None:
    path = str(match.get("path") or "")
    line_no = int(match.get("line_no") or 0)
    if not path or line_no < 1:
        return None
    line_text = str(match.get("text") or "")
    return ResearchFinding(
        repo_key=repo.repo_key,
        repository_id=repo.repository_id,
        source_oid=repo.source_oid,
        path=path,
        line=line_no,
        kind=kind,
        detail=detail[:160],
        text=line_text[:400],
        content=line_text,
    )


def _call_args_doc(tool: str, args: Mapping[str, Any]) -> str:
    """The compact call identity rendered into the observation header."""
    if tool == "read_file":
        length = args.get("length")
        span = f"offset {args.get('offset') or 0}"
        if length is not None:
            span += f" length {length}"
        return f"{args.get('path') or ''} {span}".strip()
    if tool == "list_paths":
        return f"prefix {args.get('prefix') or ''}".strip()
    if tool == "grep":
        return f"pattern {args.get('pattern') or ''}".strip()
    return f"name {args.get('name') or ''}".strip()  # find_symbol / find_references


def _execute_call(
    call: Mapping[str, Any], repos: Mapping[str, ResearchRepo]
) -> tuple[list[ResearchFinding], ToolObservation]:
    """Execute ONE proposed call through the frozen toolboxes.

    Returns ``(findings, observation)``: the findings are the citable
    anchors (the stage mints their evidence ids); the observation is the
    call's ACTUAL bounded output — or its real error — so the next model
    iteration sees what its tool really returned (NEXT-08: ``list_paths``
    results are no longer discarded, ``read_file`` no longer keeps only
    the window's first line). The observation's ``error`` replaces the
    old omission note: the caller records it AND the model sees it.
    """
    tool = str(call.get("tool") or "")
    repo_key = str(call.get("repo") or call.get("repository") or "")
    args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
    header = _call_args_doc(tool, args) if tool in _TOOL_NAMES else json.dumps(dict(args))[:160]

    def _observed(content: str, *, error: str = "", truncated: bool = False) -> ToolObservation:
        return ToolObservation(
            tool=tool or "?",
            repo_key=repo_key,
            call=header,
            content=content,
            error=error,
            truncated=truncated,
        )

    if tool not in _TOOL_NAMES:
        return [], _observed("", error=f"unknown tool {tool!r}")
    repo = repos.get(repo_key)
    if repo is None:
        known = ", ".join(sorted(repos))
        return [], _observed("", error=f"unknown repository key {repo_key!r} (authorized: {known})")
    try:
        if tool == "list_paths":
            # A listing contributes no citable file:line finding — but its
            # RESULT is the observation: every returned path is visible to
            # the next turn, selectable without prior disclosure.
            result = repo.toolbox.list_paths(str(args.get("prefix") or ""))
            paths = [str(path) for path in (result.get("paths") or [])]
            content, capped = _bounded_content("\n".join(paths))
            return [], _observed(content, truncated=bool(result.get("truncated")) or capped)
        if tool == "read_file":
            path = str(args.get("path") or "")
            offset = max(0, int(args.get("offset") or 0))
            length = args.get("length")
            window = repo.toolbox.read_file(
                path, offset=offset, length=int(length) if length is not None else None
            )
            content = str(window.get("content") or "")
            bounded, capped = _bounded_content(content)
            lines = content.splitlines()
            cited = offset + 1
            text = lines[0] if lines else ""
            return [
                ResearchFinding(
                    repo_key=repo.repo_key,
                    repository_id=repo.repository_id,
                    source_oid=repo.source_oid,
                    path=path,
                    line=cited,
                    kind="research_read",
                    detail=f"read {len(content)} chars from offset {offset}",
                    text=text[:400],
                    content=bounded,
                )
            ], _observed(bounded, truncated=bool(window.get("truncated")) or capped)
        if tool == "grep":
            pattern = str(args.get("pattern") or "")
            result = repo.toolbox.grep(pattern, is_regex=bool(args.get("is_regex")))
            matches = [
                match for match in (result.get("matches") or []) if isinstance(match, Mapping)
            ]
            findings = [
                finding
                for match in matches[:_MAX_FINDINGS_PER_CALL]
                if (finding := _finding_from_line(repo, match, "research_match", pattern))
                is not None
            ]
            content, capped = _bounded_content(_render_match_lines(matches))
            return findings, _observed(
                content, truncated=not bool(result.get("complete")) or capped
            )
        if tool == "find_symbol":
            name = str(args.get("name") or "")
            result = repo.toolbox.find_symbol(name)
            symbols = [
                symbol for symbol in (result.get("symbols") or []) if isinstance(symbol, Mapping)
            ]
            findings = [
                finding
                for symbol in symbols[:_MAX_FINDINGS_PER_CALL]
                if (finding := _finding_from_symbol(repo, symbol)) is not None
            ]
            content, capped = _bounded_content(_render_symbol_lines(symbols))
            return findings, _observed(
                content, truncated=not bool(result.get("complete")) or capped
            )
        result = repo.toolbox.find_references(str(args.get("name") or ""))
        references = [
            reference
            for reference in (result.get("references") or [])
            if isinstance(reference, Mapping)
        ]
        findings = [
            finding
            for match in references[:_MAX_FINDINGS_PER_CALL]
            if (
                finding := _finding_from_line(
                    repo, match, "research_reference", str(args.get("name") or "")
                )
            )
            is not None
        ]
        content, capped = _bounded_content(_render_match_lines(references))
        return findings, _observed(content, truncated=not bool(result.get("complete")) or capped)
    except KeyError as exc:
        # Unauthorized and unknown paths are indistinguishable BY DESIGN —
        # the omission says nothing about whether the path exists.
        return [], _observed("", error=f"path {exc.args[0]!r} is outside the authorized snapshot")
    except (ValueError, TypeError) as exc:
        return [], _observed("", error=f"invalid arguments: {exc}")


async def run_research_pass(
    harness: ResearchHarness,
    *,
    planner_input: str,
    lexical: list[dict[str, Any]],
    repos: Mapping[str, ResearchRepo],
    env: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> ResearchOutcome:
    """Drive the bounded research loop over the authorized snapshot.

    Loop shape: propose (one gateway completion) → execute (bounded
    read-only calls through the frozen toolboxes) → OBSERVE (the next
    prompt carries the tools' actual bounded output, NEXT-08) → repeat,
    until the model declares ``done`` or a budget side exhausts. Every
    proposed call — executed or refused — is charged to the call budget,
    so the iteration count is bounded by the call budget by
    construction. The outcome is honest about how it ended:
    ``complete`` only on the model's own ``done``; otherwise
    ``stopped_reason`` names the exhaustion (``max_calls``,
    ``wall_time``, ``gateway_error: …``, ``malformed_response``,
    ``no_calls_proposed``).
    """
    max_calls, wall_seconds = harness.resolve(env)
    started = now()
    deadline = started + wall_seconds
    remaining_calls = max_calls
    findings: list[ResearchFinding] = []
    observations: list[ToolObservation] = []
    omissions: list[str] = []
    consulted: set[str] = set()
    calls_proposed = 0
    calls_executed = 0
    iterations = 0
    # NEXT-09: token totals are exact ONLY under full usage coverage. A
    # None anywhere in the sequence makes the subtotal sticky-unknown: the
    # document then carries the KNOWN parts as an explicit lower bound and
    # counts the unknown-usage calls — never a None silently re-totalled.
    input_exact = True
    output_exact = True
    known_input = 0
    known_output = 0
    unknown_usage_calls = 0
    stopped = ""
    complete = False
    summary = ""
    assumptions: list[str] = []
    contradictions: list[str] = []

    while True:
        if remaining_calls <= 0:
            stopped = "max_calls"
            break
        if now() >= deadline:
            stopped = "wall_time"
            break
        prompt = _render_user_prompt(
            planner_input, lexical, repos, findings, observations, remaining_calls
        )
        # NEXT-09: the completion itself is bounded by the REMAINING wall
        # budget — a bounded await, not check-then-await — so a slow final
        # completion cannot return ``done`` after the configured pass
        # window closed. And because the provider may already have
        # accepted the request, a cut-off call's usage stays UNKNOWN (a
        # lower bound, never silently zero).
        remaining_wall = deadline - now()
        try:
            result = await asyncio.wait_for(
                harness.complete(_SYSTEM_PROMPT, prompt),
                timeout=remaining_wall,
            )
        except asyncio.TimeoutError:
            stopped = "wall_time"
            unknown_usage_calls += 1
            input_exact = False
            output_exact = False
            logger.warning(
                "research pass completion exceeded the remaining wall budget "
                "(%.3fs) — cutting it off",
                remaining_wall,
            )
            break
        except Exception as exc:  # noqa: BLE001 — the loop must record, not crash
            stopped = f"gateway_error: {type(exc).__name__}"
            logger.warning("research pass completion failed: %s", exc)
            break
        text, in_tok, out_tok = _normalize_completion(result)
        if in_tok is None:
            input_exact = False
        else:
            known_input += in_tok
        if out_tok is None:
            output_exact = False
        else:
            known_output += out_tok
        if in_tok is None or out_tok is None:
            unknown_usage_calls += 1
        iterations += 1
        parsed = _first_json_object(text)
        if parsed is None:
            stopped = "malformed_response"
            break
        if bool(parsed.get("done")):
            summary = str(parsed.get("summary") or "")[:1000]
            assumptions = [str(a)[:300] for a in (parsed.get("assumptions") or [])][:6]
            contradictions = [str(c)[:300] for c in (parsed.get("contradictions") or [])][:6]
            complete = True
            break
        calls = parsed.get("calls")
        calls = (
            [call for call in calls if isinstance(call, Mapping)][:_MAX_CALLS_PER_ITERATION]
            if isinstance(calls, list)
            else []
        )
        if not calls:
            stopped = "no_calls_proposed"
            break
        for call in calls:
            if remaining_calls <= 0:
                stopped = "max_calls"
                break
            # Charged whether it executes or is refused: a proposal is a
            # spent budget slot, so the loop cannot iterate for free.
            remaining_calls -= 1
            calls_proposed += 1
            call_findings, observation = _execute_call(call, repos)
            observations.append(observation)
            if observation.error:
                omissions.append(f"call {calls_proposed}: {observation.error}")
                continue
            calls_executed += 1
            consulted.add(observation.repo_key)
            findings.extend(call_findings)
        if stopped:
            break

    document = {
        "schema": RESEARCH_SCHEMA,
        "mode": RESEARCH_HARNESS_MODE,
        "complete": complete,
        "stopped_reason": stopped,
        "iterations": iterations,
        "calls_proposed": calls_proposed,
        "calls_executed": calls_executed,
        # NEXT-08: explicit observation counts — truncated and failed
        # observations are visible in the durable record, so invisible
        # content can never be mistaken for reviewed content.
        "observations": {
            "count": len(observations),
            "truncated": sum(1 for observation in observations if observation.truncated),
            "errors": sum(1 for observation in observations if observation.error),
        },
        "budget": {"max_calls": max_calls, "wall_seconds": wall_seconds},
        "wall_seconds_used": round(now() - started, 3),
        "tokens": {
            "input": known_input if input_exact else None,
            "output": known_output if output_exact else None,
            "input_lower_bound": known_input,
            "output_lower_bound": known_output,
            "unknown_usage_calls": unknown_usage_calls,
        },
        "repos_consulted": sorted(consulted),
        "repos_authorized": sorted(repos),
        "findings": [],  # the stage fills each finding's evidence_id in
        "omissions": omissions[:_MAX_OMISSIONS_RECORDED],
        "omissions_dropped": max(0, len(omissions) - _MAX_OMISSIONS_RECORDED),
        "summary": summary,
        "assumptions": assumptions,
        "contradictions": contradictions,
        "rules": (
            "Bounded research pass over the authorized snapshot. complete=false "
            "means a budget stopped the investigation early — the findings are "
            "partial, not exhaustive. Findings cite evidence:<id> recorded by "
            "this discovery. tokens.input/output are the EXACT totals only "
            "when unknown_usage_calls is 0; otherwise the *_lower_bound "
            "fields are what was actually observed."
        ),
    }
    return ResearchOutcome(
        findings=tuple(findings),
        document=document,
        iterations=iterations,
        complete=complete,
        stopped_reason=stopped,
    )


def _research_document_of(record: Mapping[str, Any]) -> dict[str, Any] | None:
    entry = record.get("research") if isinstance(record, Mapping) else None
    return dict(entry) if isinstance(entry, Mapping) else None


def render_research_section(
    record: Mapping[str, Any], *, max_chars: int = RESEARCH_SUMMARY_MAX_CHARS
) -> str:
    """Render the delimited research summary, bounded to *max_chars*.

    Empty string when the record carries no research pass. The summary's
    claims ride beside the citation map (each research finding's
    ``evidence:<id>``), validated against the record's OWN evidence ids —
    a finding whose id no longer resolves is dropped from the map, never
    rendered leaning on evidence that is not there. Under budget the
    assumptions/contradictions lists drop from the END with
    ``truncated``/``dropped`` saying so.
    """
    document = _research_document_of(record)
    if document is None:
        return ""
    known = {
        str(entry.get("id"))
        for entry in (record.get("evidence") or [])
        if isinstance(entry, Mapping) and entry.get("id")
    }
    citations = [
        {
            "evidence": str(finding.get("evidence_id") or ""),
            "path": str(finding.get("path") or ""),
            "line": int(finding.get("line") or 0),
            "kind": str(finding.get("kind") or ""),
        }
        for finding in (document.get("findings") or [])
        if isinstance(finding, Mapping) and str(finding.get("evidence_id") or "") in known
    ]
    doc: dict[str, Any] = {
        "schema": RESEARCH_SCHEMA,
        "discovery_id": str(record.get("discovery_id") or ""),
        "complete": bool(document.get("complete")),
        "stopped_reason": str(document.get("stopped_reason") or ""),
        "repos_consulted": [str(key) for key in (document.get("repos_consulted") or [])],
        "omissions": [str(item) for item in (document.get("omissions") or [])],
        "summary": str(document.get("summary") or ""),
        "assumptions": [str(item) for item in (document.get("assumptions") or [])],
        "contradictions": [str(item) for item in (document.get("contradictions") or [])],
        "citations": citations,
    }

    def _render() -> str:
        return f"{RESEARCH_BEGIN}\n{json.dumps(doc, sort_keys=True, separators=(',', ':'))}\n{RESEARCH_END}"

    section = _render()
    dropped = 0
    while len(section) > max_chars and (
        doc["contradictions"] or doc["assumptions"] or doc["omissions"] or doc["citations"]
    ):
        dropped += 1
        if doc["contradictions"]:
            doc["contradictions"].pop()
        elif doc["assumptions"]:
            doc["assumptions"].pop()
        elif doc["omissions"]:
            doc["omissions"].pop()
        elif doc["citations"]:
            doc["citations"].pop()
        section = _render()
    if dropped:
        doc["truncated"] = True
        doc["dropped"] = dropped
        section = _render()
    return section


def attach_research(planner_input: str, research_section: str, *, cap: int = 12000) -> str:
    """Return the input with the research section appended, within the cap.

    The same bargain as the digest/answers attach: the combined prompt
    stays within the planner's input cap by cutting the input's HEAD —
    the sections at the end survive — never the section itself (it is
    already bounded by its renderer).
    """
    section_len = len(research_section)
    if len(planner_input) + 2 + section_len <= cap:
        return f"{planner_input}\n\n{research_section}"
    kept = planner_input[: max(0, cap - section_len - 2 - 96)]
    while kept and len(kept) + 2 + section_len + 96 > cap:
        kept = kept[:-1]
    marker = (
        f"(input truncated to {len(kept)} chars to fit the discovery research "
        f"within the planner input cap)\n\n"
    )
    return f"{kept}\n\n{marker}{research_section}"
