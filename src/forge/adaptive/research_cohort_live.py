"""R36-12 — the LIVE-capture driver for the research-quality cohort.

The replay evaluator (:mod:`forge.adaptive.research_cohort`) grades
RECORDED artifacts; this module is how a live cohort's artifacts come
to exist — under a :class:`~forge.adaptive.research_cohort.Preregistration`
that froze the task set, snapshot digests, eligibility rules, budget,
arms, review procedure and promotion criteria BEFORE the first capture.

:meth:`capture_arm` drives the REAL research paths against the task's
frozen snapshot — never a mock of them:

- ``research`` runs the actual
  :func:`~forge.adaptive.research_planner.run_research_pass` loop over
  one frozen :class:`~forge.adaptive.discovery_tools.SnapshotToolbox`
  per authorized repository (the same construction the discovery
  stage's research leg uses), so every observation is what the tool
  machinery actually returned;
- ``lexical`` runs the discovery stage's deterministic probes
  (:func:`~forge.adaptive.discovery_stage.extract_keywords` +
  ``find_symbol``/``find_references`` over the same toolboxes);
- ``none`` constructs the planner input with discovery off — the plan
  rests on the statement alone and its emptiness is graded honestly.

Model calls: when the lab LLM gateway is configured
(:data:`FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV`, the same OpenAI-compatible
litellm endpoint the planner's ``LLMClient`` talks to) the driver
executes for real under HARD budget caps (the preregistration's call
and wall budgets, plus a per-call token cap) and stamps the capture
``live-model`` with the model/route identity.  Otherwise the driver runs
in ``recorded-scripted`` mode — a reactive scripted model over the REAL
ToolObservation loop (the RC-08 recipe) — and stamps
``offline-scripted-model``.  Every artifact records which one it was;
the two are never pooled into one score by the report.

The captured PLAN is synthesized deterministically from what the run
ACTUALLY established: claims cite the findings' anchors with the
asserted content taken from the snapshot's real bytes, the surface is
what was read, and NO reviewer grades are fabricated — claim
importance, assumption severity and correction minutes stay absent
until the blind review lands
(:func:`~forge.adaptive.research_cohort.merge_review_grades`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from forge.adaptive.discovery_stage import extract_keywords
from forge.adaptive.discovery_tools import SnapshotToolbox
from forge.adaptive.research_cohort import (
    MODE_LEXICAL,
    MODE_NONE,
    MODE_RESEARCH,
    PROVENANCE_LIVE_MODEL,
    PROVENANCE_OFFLINE_SCRIPTED,
    REVIEW_PENDING,
    CohortSpecError,
    CohortTask,
    Preregistration,
    Snapshot,
    _canonical,
)
from forge.adaptive.research_planner import (
    CompletionFn,
    ResearchHarness,
    ResearchRepo,
    ToolObservation,
    _execute_call,
    _first_json_object,
    run_research_pass,
)

__all__ = [
    "FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV",
    "FORGE_RESEARCH_LIVE_GATEWAY_TIER_ENV",
    "FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV",
    "REACTIVE_DEEP_READ",
    "SCRIPTED_MODEL_IDENTITY",
    "DeterministicClock",
    "LiveGateway",
    "ScriptedInvestigation",
    "capture_arm",
    "gateway_completion",
    "prompt_policy_digest",
    "resolve_live_gateway",
]

#: The env var that opts a capture into REAL gateway execution.  Set it
#: to the lab's OpenAI-compatible endpoint (the litellm proxy the
#: planner's ``LLMClient`` uses); unset/empty means the driver runs in
#: ``recorded-scripted`` mode instead.  The URL alone is never enough —
#: the MODEL identity must be pinned too, so a live capture can never be
#: mislabelled.
FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV = "FORGE_RESEARCH_LIVE_GATEWAY_URL"

#: The model name the gateway executes (recorded verbatim on the capture).
FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV = "FORGE_RESEARCH_LIVE_GATEWAY_MODEL"

#: The route/tier the live capture rides (default: the planner's tier).
FORGE_RESEARCH_LIVE_GATEWAY_TIER_ENV = "FORGE_RESEARCH_LIVE_GATEWAY_TIER"

#: The identity a scripted offline model carries on its capture block.
SCRIPTED_MODEL_IDENTITY = "scripted:reactive-investigation/v1"

#: The deterministic default clock: a capture with no injected clock
#: replays byte-identically (the shipped artifacts were captured under it).
_CLOCK_START = 1000.0
_CLOCK_STEP = 0.25

#: How many anchors a synthesized plan carries (steps/claims/surface stay
#: bounded and readable, like every other durable record in this area).
_MAX_PLAN_ANCHORS = 4


class DeterministicClock:
    """A stepped monotonic clock — captures replay byte-identically."""

    def __init__(self, start: float = _CLOCK_START, step: float = _CLOCK_STEP) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> float:
        current = self._now
        self._now += self._step
        return current


@dataclass(frozen=True)
class LiveGateway:
    """The resolved lab gateway: endpoint + model + route identity."""

    base_url: str
    model: str
    tier: str

    @property
    def route(self) -> str:
        return f"litellm-gateway:{self.tier}"


def resolve_live_gateway(env: Mapping[str, str] | None = None) -> LiveGateway | None:
    """The live gateway when the lab env pins one, else ``None``.

    A URL without a MODEL identity is a REFUSAL, not a guess — an
    unlabelled live capture is exactly the mislabelling R36-12 forbids.
    """
    source = os.environ if env is None else env
    url = str(source.get(FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV, "") or "").strip()
    if not url:
        return None
    model = str(source.get(FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV, "") or "").strip()
    if not model:
        raise CohortSpecError(
            f"{FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV} is set but "
            f"{FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV} is not — a live capture is never "
            "executed (or labelled) without the model identity"
        )
    tier = str(source.get(FORGE_RESEARCH_LIVE_GATEWAY_TIER_ENV, "") or "").strip() or "planner"
    return LiveGateway(base_url=url.rstrip("/"), model=model, tier=tier)


def gateway_completion(
    gateway: LiveGateway, *, max_tokens: int = 1024, timeout: float | None = None
) -> CompletionFn:
    """The budget-capped live completion seam against the lab gateway.

    One OpenAI-compatible chat completion per call with ``json_mode``;
    usage counters ride back through the ``LLMResult`` shape so the
    research loop's unknown-usage accounting stays intact.  The HARD
    caps are the caller's: the preregistration's call/wall budgets bind
    the loop (:class:`~forge.adaptive.research_planner.ResearchHarness`)
    and *max_tokens* bounds each response.
    """

    async def _complete(system: str, user: str) -> Any:
        import httpx

        payload: dict[str, Any] = {
            "model": gateway.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": int(max_tokens),
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=timeout or 120.0) as client:
            response = await client.post(
                f"{gateway.base_url}/v1/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            body = response.json()
        choice = (body.get("choices") or [{}])[0]
        text = str(((choice.get("message") or {}).get("content")) or "")
        usage = body.get("usage") or {}
        return _Completion(
            text=text,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

    return _complete


@dataclass(frozen=True)
class _Completion:
    """The ``LLMResult`` shape the research loop normalizes."""

    text: str
    input_tokens: int | None
    output_tokens: int | None


#: A scripted turn that REACTS to the previous observations: pages a
#: read window around the line a grep/symbol observation reported for the
#: configured deep target.  A script carrying this placeholder proves the
#: REAL feedback loop — the model's next call is derived from what the
#: tool machinery actually returned, not authored prose.
REACTIVE_DEEP_READ = "reactive-deep-read"


class ScriptedInvestigation:
    """A reactive, RECORDED offline investigation script (RC-08 recipe).

    *turns* is the fixed script: each entry is either a model turn
    (``{"calls": [...]}`` or ``{"done": True, "summary": ..., ...}``) or
    the :data:`REACTIVE_DEEP_READ` placeholder, replaced at runtime by a
    ``read_file`` call computed from the PREVIOUS observations in the
    prompt (the deep target is ``(repo, path, marker)``).  Every proposed
    call executes through the REAL tool machinery; the recorded token
    counts ride each turn (a missing entry means UNKNOWN usage — never
    silently zero).
    """

    def __init__(
        self,
        snapshot: Snapshot,
        *,
        turns: Sequence[Mapping[str, Any] | str],
        deep: tuple[str, str, str] | None = None,
        input_tokens: Sequence[int] = (),
        output_tokens: Sequence[int] = (),
    ) -> None:
        self._snapshot = snapshot
        self._turns = list(turns)
        self._deep = deep
        self._input = iter(input_tokens)
        self._output = iter(output_tokens)
        self._index = 0
        self.proposed: list[dict[str, Any]] = []

    async def __call__(self, system_prompt: str, user_prompt: str) -> Any:
        from types import SimpleNamespace

        if self._index >= len(self._turns):
            turn: Mapping[str, Any] | str = {"done": True, "summary": "", "assumptions": []}
        else:
            turn = self._turns[self._index]
            self._index += 1
        if isinstance(turn, str):
            if turn != REACTIVE_DEEP_READ:  # pragma: no cover - scripted by the cohort author
                raise CohortSpecError(f"unknown scripted placeholder {turn!r}")
            turn = {"calls": [self._reactive_read(user_prompt)]}
        for call in turn.get("calls") or []:  # type: ignore[union-attr]
            self.proposed.append(dict(call))
        return SimpleNamespace(
            text=json.dumps(turn),
            input_tokens=next(self._input, None),
            output_tokens=next(self._output, None),
        )

    def _reactive_read(self, user_prompt: str) -> dict[str, Any]:
        """The read call derived from what the tools ACTUALLY returned."""
        if self._deep is None:
            raise CohortSpecError("REACTIVE_DEEP_READ needs a (repo, path, marker) target")
        repo_key, path, marker = self._deep
        match = re.search(rf"{re.escape(path)}:(\d+):[^\n]*{re.escape(marker)}", user_prompt)
        if match is None:
            raise CohortSpecError(
                f"the {marker!r} observation did not reach the scripted model — the "
                "reactive turn cannot react to an observation the loop never fed back"
            )
        line_no = int(match.group(1))
        content = self._snapshot.files_of(repo_key)[path]
        lines = content.splitlines(keepends=True)
        start = sum(len(text) for text in lines[: max(0, line_no - 3)])
        stop = sum(len(text) for text in lines[: min(len(lines), line_no + 2)])
        return {
            "tool": "read_file",
            "repo": repo_key,
            "args": {"path": path, "offset": start, "length": stop - start},
        }


def prompt_policy_digest() -> str:
    """The digest over the prompts/policies the arms run under.

    The preregistration records this hash; if the system prompt, the
    lexical probe policy or the plan-synthesis policy changes between
    iterations, the digest moves and the cohort MUST be re-registered
    (a new preregistration generation) — that is the optimization-leak
    prevention R36-12 asks for, made mechanical.
    """
    from forge.adaptive import research_planner as planner

    return hashlib.sha256(
        _canonical(
            {
                "system_prompt": planner._SYSTEM_PROMPT,
                "lexical_probes": "extract_keywords+find_symbol+find_references/v1",
                "plan_synthesis": "derive-from-captured-run/v1",
            }
        ).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# The real-path machinery the arms drive
# ---------------------------------------------------------------------------


def _research_repos(snapshot: Snapshot) -> dict[str, ResearchRepo]:
    """One frozen toolbox per authorized repository (the stage's construction)."""
    return {
        key: ResearchRepo(
            repo_key=key,
            repository_id=str(entry.get("repository_id") or ""),
            source_oid=str(entry.get("source_oid") or ""),
            toolbox=SnapshotToolbox(dict(snapshot.files_of(key))),
        )
        for key, entry in snapshot.repos.items()
    }


def _lexically_probe(
    planner_input: str, repos: Mapping[str, ResearchRepo], *, limit: int = 4
) -> list[dict[str, Any]]:
    """The discovery stage's deterministic lexical probes, for real.

    Identifiers come out of the frozen statement
    (:func:`extract_keywords`); each is looked up through every repo's
    toolbox with ``find_symbol`` and ``find_references`` — the same
    bounded read-only probes ``_probe`` runs in
    :func:`~forge.adaptive.discovery_stage.run_discovery_stage`.
    """
    hits: list[dict[str, Any]] = []
    for keyword in extract_keywords(planner_input):
        for repo in repos.values():
            symbols = (repo.toolbox.find_symbol(keyword).get("symbols") or [])[:2]
            references = (repo.toolbox.find_references(keyword).get("references") or [])[:2]
            for symbol in symbols:
                hits.append(
                    {
                        "repo_key": repo.repo_key,
                        "repository_id": repo.repository_id,
                        "source_oid": repo.source_oid,
                        "path": str(symbol.get("path") or ""),
                        "line": int(symbol.get("line_no") or 0),
                        "kind": "symbol",
                        "detail": str(symbol.get("symbol") or keyword),
                    }
                )
            for reference in references:
                hits.append(
                    {
                        "repo_key": repo.repo_key,
                        "repository_id": repo.repository_id,
                        "source_oid": repo.source_oid,
                        "path": str(reference.get("path") or ""),
                        "line": int(reference.get("line_no") or 0),
                        "kind": "reference",
                        "detail": keyword,
                    }
                )
    return hits[:limit]


def _snapshot_line(snapshot: Snapshot, repo_key: str, path: str, line: int) -> str | None:
    """The ACTUAL bytes at ``repo:path:line`` (None when out of range)."""
    content = snapshot.files_of(repo_key).get(path)
    if content is None:
        return None
    lines = content.splitlines()
    if not 1 <= line <= len(lines):
        return None
    return lines[line - 1].strip()


_QUESTION_RE = re.compile(r"[^.?!]*\?")


def _questions_of(summary: str) -> list[dict[str, Any]]:
    """Interrogative sentences lifted from the run's own summary.

    The questions a synthesized plan may carry come from what the RUN
    said, never from the grading ground truth — a scripted or live model
    that asked the question gets it into the plan; one that stayed silent
    does not.  ``specific`` is deliberately NOT set: question specificity
    is a reviewer grade, pending like every other.
    """
    questions: list[dict[str, Any]] = []
    for match in _QUESTION_RE.finditer(summary or ""):
        text = match.group(0).strip()
        if len(text) >= 8:
            questions.append({"question_id": f"q{len(questions) + 1}", "text": text})
    return questions[:3]


def _synthesize_plan(
    task: CohortTask,
    snapshot: Snapshot,
    anchors: Sequence[Mapping[str, Any]],
    *,
    summary: str,
    assumptions: Sequence[str] = (),
    evidence_prefix: str | None = None,
) -> dict[str, Any]:
    """The deterministic plan derived from what the run ACTUALLY established.

    Claims cite the anchors' repo/path/line with the asserted content
    read out of the frozen snapshot's bytes — a synthesized claim can
    never assert bytes the tools did not return.  Reviewer grades are
    NOT invented: no claim importance, no assumption severity, no
    invented-default flags, no correction estimate.  ``evidence_prefix``
    mints the citation ids the mode's own machinery would have minted
    (``ev-`` for research findings, ``lex-`` for lexical probe hits);
    ``None`` mints none (the none arm read nothing).
    """
    claims: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    surface: list[dict[str, Any]] = []
    seen_surface: set[tuple[str, str]] = set()
    for index, anchor in enumerate(anchors[:_MAX_PLAN_ANCHORS], start=1):
        repo_key = str(anchor.get("repo_key") or "")
        path = str(anchor.get("path") or "")
        line = int(anchor.get("line") or 0)
        actual = _snapshot_line(snapshot, repo_key, path, line)
        if actual is None or not repo_key or not path:
            continue
        evidence_ref = f"{evidence_prefix}{index}" if evidence_prefix is not None else ""
        claim: dict[str, Any] = {
            "claim_id": f"c{len(claims) + 1}",
            "text": actual[:400],
            "repo": repo_key,
            "path": path,
            "line": line,
            "asserted_content": actual,
        }
        if evidence_prefix == "ev-":
            # research findings carry the evidence id the research document
            # itself minted, so the citation-integrity check can resolve it
            claim["evidence"] = f"evidence:{evidence_ref}"
        claims.append(claim)
        step: dict[str, Any] = {
            "step_id": f"s{len(steps) + 1}",
            "objective": (
                f"address {repo_key}/{path} ({anchor.get('kind')}: {anchor.get('detail')})"
            ),
        }
        if evidence_ref:
            step["evidence_refs"] = [evidence_ref]
        steps.append(step)
        if (repo_key, path) not in seen_surface:
            seen_surface.add((repo_key, path))
            surface.append(
                {"repo": repo_key, "path": path, "why": str(anchor.get("kind") or "read")}
            )
    return {
        "steps": steps or [{"step_id": "s1", "objective": task.statement[:200]}],
        "surface": surface,
        "claims": claims,
        "questions": _questions_of(summary),
        "assumptions": [{"text": str(text)} for text in assumptions or []],
    }


def _recording_complete(inner: CompletionFn) -> tuple[CompletionFn, list[dict[str, Any]]]:
    """Wrap a completion seam so the proposals it returned are recorded.

    The research loop does not surface the model's raw turns; this
    wrapper records every proposed call so the capture can mint the REAL
    :class:`ToolObservation` records by re-executing the proposals
    through the same frozen executor the loop used (the RC-08 recipe).
    """

    proposals: list[dict[str, Any]] = []

    async def _wrapped(system: str, user: str) -> Any:
        result = await inner(system, user)
        text = str(getattr(result, "text", result))
        parsed = _first_json_object(text)
        if parsed is not None:
            for call in parsed.get("calls") or []:
                if isinstance(call, dict):
                    proposals.append(dict(call))
        return result

    return _wrapped, proposals


def _cost_of(
    document: Mapping[str, Any], calls_proposed: int, calls_executed: int
) -> dict[str, Any]:
    return {
        "calls_proposed": calls_proposed,
        "calls_executed": calls_executed,
        "wall_seconds_used": float(document.get("wall_seconds_used") or 0.0),
        "tokens": dict(document.get("tokens") or {}),
    }


async def capture_arm(
    preregistration: Preregistration | None,
    task: CohortTask,
    mode: str,
    *,
    snapshot: Snapshot,
    env: Mapping[str, str] | None = None,
    scripted_model: CompletionFn | None = None,
    completion: CompletionFn | None = None,
    completion_identity: str = "",
    captured_at: str = "",
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Capture ONE (task, mode) arm under the frozen pre-registration.

    Refuses before anything runs when there is no preregistration, the
    task is not the frozen binding (id/digest/archetype), the mode is
    not one of the pre-registered arms, or the snapshot does not
    re-derive the task's digest.  The returned document is a
    ``forge.research.cohort.run/1`` recording whose ``capture`` block
    carries the provenance label, the model/route identity, the
    preregistration digest + generation and the budget actually spent.
    An injected *completion* (with its *completion_identity*) executes
    for real like a gateway would and is stamped ``live-model``.
    """
    if preregistration is None:
        raise CohortSpecError(
            "capture refuses to start without a pre-registered contract — freeze the "
            "task set, budget, review procedure and promotion criteria FIRST "
            "(forge.research.preregistration/1)"
        )
    if not preregistration.binds(task):
        raise CohortSpecError(
            f"{task.task_id}: not the frozen pre-registered binding "
            f"(id + snapshot digest + archetype must all agree)"
        )
    if mode not in tuple(preregistration.arms):
        raise CohortSpecError(
            f"{mode!r} is not one of the pre-registered arms {list(preregistration.arms)}"
        )
    if snapshot.digest != task.snapshot_digest:
        raise CohortSpecError(
            f"{task.task_id}: snapshot re-derives {snapshot.digest[:12]}… but the task "
            f"authorizes {task.snapshot_digest[:12]}…"
        )
    gateway = resolve_live_gateway(env) if completion is None else None
    clock = now if now is not None else DeterministicClock()
    budget = {
        "max_calls": int(preregistration.budget.get("max_calls") or 0),
        "wall_seconds": float(preregistration.budget.get("wall_seconds") or 0.0),
    }
    repos = _research_repos(snapshot)
    recorded_at = captured_at or preregistration.registered_at

    if mode == MODE_RESEARCH:
        if completion is not None:
            inner = completion
            provenance = PROVENANCE_LIVE_MODEL
            identity = f"live:{completion_identity or 'injected-completion'}"
            route = "injected-live-completion"
            live_provider = True
        elif gateway is not None:
            inner = gateway_completion(gateway)
            provenance = PROVENANCE_LIVE_MODEL
            identity = f"live:{gateway.model}"
            route = gateway.route
            live_provider = True
        else:
            if scripted_model is None:
                raise CohortSpecError(
                    f"{task.task_id}/{mode}: no live gateway configured and no scripted "
                    "model supplied — a recorded-scripted capture needs the reactive "
                    "script that drives the real tool loop"
                )
            inner = scripted_model
            provenance = PROVENANCE_OFFLINE_SCRIPTED
            identity = SCRIPTED_MODEL_IDENTITY
            route = "offline-scripted-over-real-tool-loop"
            live_provider = False
        wrapped, proposals = _recording_complete(inner)
        harness = ResearchHarness(
            complete=wrapped, max_calls=budget["max_calls"], wall_seconds=budget["wall_seconds"]
        )
        outcome = await run_research_pass(
            harness,
            planner_input=task.statement,
            lexical=[],
            repos=repos,
            now=clock,
        )
        # Mint the REAL observations by re-executing the recorded
        # proposals through the same frozen executor the loop used.
        observations: list[ToolObservation] = [_execute_call(call, repos)[1] for call in proposals]
        document = dict(outcome.document)
        anchors = [
            {
                "repo_key": finding.repo_key,
                "repository_id": finding.repository_id,
                "source_oid": finding.source_oid,
                "path": finding.path,
                "line": finding.line,
                "kind": finding.kind,
                "detail": finding.detail,
                "text": finding.text,
            }
            for finding in outcome.findings
        ]
        findings_doc = [
            {
                "evidence_id": f"ev-{index}",
                "repo_key": finding.repo_key,
                "repository_id": finding.repository_id,
                "path": finding.path,
                "line": finding.line,
                "kind": finding.kind,
                "detail": finding.detail,
            }
            for index, finding in enumerate(outcome.findings, start=1)
        ]
        document["findings"] = findings_doc
        stopped = str(document.get("stopped_reason") or "")
        plan = (
            _synthesize_plan(
                task,
                snapshot,
                anchors,
                summary=str(document.get("summary") or ""),
                assumptions=[str(a) for a in document.get("assumptions") or []],
                evidence_prefix="ev-",
            )
            if anchors
            else None  # nothing established: a FAILED attempt, reported with cost
        )
        cost = _cost_of(
            document,
            int(document.get("calls_proposed") or 0),
            int(document.get("calls_executed") or 0),
        )
        harness_name = "forge.adaptive.research_planner.run_research_pass"
    elif mode == MODE_LEXICAL:
        probes = _lexically_probe(task.statement, repos)
        plan = _synthesize_plan(task, snapshot, probes, summary="", evidence_prefix="lex-")
        document = {
            "probes": probes,
            "summary": "",
        }
        stopped = ""
        observations = []
        cost = {
            "calls_proposed": 0,
            "calls_executed": 0,
            "wall_seconds_used": 0.0,
            "tokens": {
                "input": 0,
                "output": 0,
                "input_lower_bound": 0,
                "output_lower_bound": 0,
                "unknown_usage_calls": 0,
            },
        }
        provenance = PROVENANCE_OFFLINE_SCRIPTED
        identity = "deterministic-lexical-probes/v1"
        route = "deterministic (no model calls)"
        live_provider = False
        harness_name = "forge.adaptive.discovery_stage lexical probes"
    elif mode == MODE_NONE:
        plan = _synthesize_plan(task, snapshot, [], summary="")
        document = {}
        stopped = ""
        observations = []
        cost = {
            "calls_proposed": 0,
            "calls_executed": 0,
            "wall_seconds_used": 0.0,
            "tokens": {
                "input": 0,
                "output": 0,
                "input_lower_bound": 0,
                "output_lower_bound": 0,
                "unknown_usage_calls": 0,
            },
        }
        provenance = PROVENANCE_OFFLINE_SCRIPTED
        identity = "planner-input-only/v1"
        route = "deterministic (no model calls)"
        live_provider = False
        harness_name = "planner-input construction with discovery off"
    else:  # pragma: no cover - the arms check above already refused
        raise CohortSpecError(f"unknown arm {mode!r}")

    return {
        "schema": "forge.research.cohort.run/1",
        "task_id": task.task_id,
        "mode": mode,
        "snapshot_digest": task.snapshot_digest,
        "budget": budget,
        "attempts": [
            {
                "attempt": 1,
                "stopped_reason": stopped,
                "cost": cost,
            }
        ],
        **({"research_document": document} if mode == MODE_RESEARCH else {}),
        "observations": [
            {
                "tool": observation.tool,
                "repo_key": observation.repo_key,
                "call": observation.call,
                "content": observation.content,
                "error": observation.error,
                "truncated": observation.truncated,
            }
            for observation in observations
        ],
        "plan": plan,
        "reviewer": {
            "notes": (
                f"captured {provenance} under preregistration "
                f"{preregistration.digest[:12]}… generation {preregistration.generation}; "
                "blind review pending — no reviewer grades recorded yet"
            )
        },
        "capture": {
            "provenance": provenance,
            "model_identity": identity,
            "route": route,
            "live_provider": live_provider,
            "preregistration": {
                "digest": preregistration.digest,
                "generation": preregistration.generation,
            },
            "budget": budget,
            "budget_spent": cost,
            "harness": harness_name,
            "captured_at": recorded_at,
            "review_state": REVIEW_PENDING,
            "issue": "R36-12 / #271",
        },
    }
