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

R37-11 (gen-2): under a live provider the PLAN itself is synthesized by
the REAL model in EVERY arm (none/lexical/research) — only the
discovery phase differs between arms, so the comparison is planner-MODE,
not model — and every call rides a :class:`SpendLedger` (the #290-style
hard-dollar accounting: worst-case projection before the call, receipt
from the gateway's reported usage after it, unknown usage charged at the
worst case, never zero).  A plan synthesis that fails, truncates or is
capped is PRESERVED as a failed attempt with its receipts — never
retried into a pool, never zeroed.

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
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
    "PRICE_NOTE",
    "PRICES_USD_PER_MTOK",
    "REACTIVE_DEEP_READ",
    "SCRIPTED_MODEL_IDENTITY",
    "DeterministicClock",
    "LiveGateway",
    "ScriptedInvestigation",
    "SpendCapReached",
    "SpendLedger",
    "capture_arm",
    "capped_completion",
    "gateway_completion",
    "model_plan_document",
    "plan_synthesis_prompt",
    "prompt_policy_digest",
    "render_plan_synthesis_user_prompt",
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
    # The default timeout is generous on purpose: reasoning models can think
    # past two minutes on a plan-synthesis prompt, and a transport timeout
    # there becomes an honest failed attempt rather than a truncated read.
    timeout = timeout or 240.0
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
        async with httpx.AsyncClient(timeout=timeout) as client:
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


# ---------------------------------------------------------------------------
# The hard spend cap (R37-11) — the #290-style dollar accounting
# ---------------------------------------------------------------------------

#: Conservative OVER-estimates used ONLY to enforce the spend cap; the
#: receipts record the usage the gateway actually reported and the capture
#: names the assumption (never presented as vendor pricing).
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "fast": {"input": 2.0, "output": 8.0},
    "openai/glm-5.3-flash": {"input": 2.0, "output": 8.0},
}

PRICE_NOTE = (
    "conservative over-estimate used only to enforce the hard spend cap; "
    "actual vendor pricing is not recorded here — the receipts carry the "
    "usage the gateway reported"
)

#: The hard ceiling a live cohort's TOTAL budget may declare (R37-11: the
#: whole comparison — every arm, every task — runs inside this many USD).
MAX_COHORT_USD = 3.0


class SpendCapReached(Exception):
    """Raised BEFORE a model call when its worst-case projection would
    exceed the hard spend cap — the capture records it as an honest stop
    (``spend_cap``), preserving the attempt and its receipts."""


@dataclass
class SpendLedger:
    """The hard USD bound over the usage receipts the gateway reports.

    One ledger may be SHARED by every arm of a cohort (the total cap);
    each call is checked BEFORE the provider is contacted (a projected
    worst case that would cross the bound refuses to start) and charged
    AFTER it, from the usage the gateway reported.  An unknown-usage
    receipt is charged at a conservative worst case — never zero: the
    cap may over-stop, it may never under-stop.
    """

    model: str = "unknown"
    limit_usd: float = MAX_COHORT_USD
    prices: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: dict(PRICES_USD_PER_MTOK)
    )
    worst_case_input_tokens: int = 20_000
    spent_usd: float = 0.0
    receipts: list[dict[str, Any]] = field(default_factory=list)

    def _price(self) -> tuple[float, float]:
        entry = self.prices.get(self.model) or next(iter(self.prices.values()))
        return float(entry["input"]), float(entry["output"])

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self._price()
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000

    def allows_call(
        self,
        max_tokens: int,
        *,
        allowance_usd: float | None = None,
    ) -> bool:
        """Whether a worst-case call still fits every bound in play.

        *allowance_usd* is an optional per-arm remainder: the call must
        fit inside ``spent_usd + allowance`` as well as the ledger's
        total limit.
        """
        projected = self.estimate(self.worst_case_input_tokens, int(max_tokens))
        if self.spent_usd + projected > self.limit_usd:
            return False
        if allowance_usd is not None and projected > allowance_usd:
            return False
        return True

    def charge(
        self, *, input_tokens: int | None, output_tokens: int | None, max_tokens: int
    ) -> dict[str, Any]:
        known = input_tokens is not None and output_tokens is not None
        charged_in = int(input_tokens or 0) if known else self.worst_case_input_tokens
        charged_out = int(output_tokens or 0) if known else int(max_tokens)
        usd = self.estimate(charged_in, charged_out)
        self.spent_usd = round(self.spent_usd + usd, 6)
        receipt = {
            "input_tokens": int(input_tokens) if input_tokens is not None else None,
            "output_tokens": int(output_tokens) if output_tokens is not None else None,
            "charged_input_tokens": charged_in,
            "charged_output_tokens": charged_out,
            "usage_known": known,
            "usd_estimated": round(usd, 6),
            "running_usd_estimated": self.spent_usd,
        }
        self.receipts.append(receipt)
        return receipt

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd


def capped_completion(
    inner: CompletionFn,
    ledger: SpendLedger,
    *,
    max_tokens: int,
    purpose: str,
    allowance_usd: float | Callable[[], float | None] | None = None,
) -> CompletionFn:
    """Wrap a completion seam with the HARD spend cap.

    The check runs BEFORE the provider is contacted (a projected
    worst-case call that would cross the cap refuses to start) and the
    charge lands AFTER, from the usage the gateway reported — unknown
    usage is charged at the worst case, never zero.  *allowance_usd* is
    the per-arm remainder (a constant or a zero-arg callable evaluated
    at call time, so sequential calls see the spend that already
    landed).
    """

    def _allowance() -> float | None:
        if allowance_usd is None:
            return None
        if callable(allowance_usd):
            return allowance_usd()
        return float(allowance_usd)

    async def _complete(system: str, user: str) -> Any:
        remaining = _allowance()
        if not ledger.allows_call(max_tokens, allowance_usd=remaining):
            raise SpendCapReached(
                f"the {purpose} call's worst-case projection would exceed a hard "
                f"spend bound (cap ${ledger.limit_usd:.2f}, spent ${ledger.spent_usd:.4f}, "
                f"arm allowance {f'${remaining:.4f}' if remaining is not None else 'unbounded'})"
            )
        result = await inner(system, user)
        in_tok = getattr(result, "input_tokens", None)
        out_tok = getattr(result, "output_tokens", None)
        ledger.charge(input_tokens=in_tok, output_tokens=out_tok, max_tokens=max_tokens)
        return result

    return _complete


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


def prompt_policy_digest(*, plan_synthesis: str = "derive-from-captured-run/v1") -> str:
    """The digest over the prompts/policies the arms run under.

    The preregistration records this hash; if the system prompt, the
    lexical probe policy or the plan-synthesis policy changes between
    iterations, the digest moves and the cohort MUST be re-registered
    (a new preregistration generation) — that is the optimization-leak
    prevention R36-12 asks for, made mechanical.  The default keeps the
    gen-1 policy hash byte-identical (the shipped generation-1 contract
    still loads); a gen-2 live cohort passes the plan-synthesis policy
    it actually runs under (``live-model-plan-synthesis/v1``).
    """
    from forge.adaptive import research_planner as planner

    return hashlib.sha256(
        _canonical(
            {
                "system_prompt": planner._SYSTEM_PROMPT,
                "lexical_probes": "extract_keywords+find_symbol+find_references/v1",
                "plan_synthesis": plan_synthesis,
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


# ---------------------------------------------------------------------------
# The LIVE plan synthesis (R37-11) — the REAL model writes every arm's plan
# ---------------------------------------------------------------------------

#: The plan-synthesis policy identity a gen-2 preregistration hashes into
#: its prompt/policy digest (only the discovery phase differs between
#: arms — the comparison is planner-mode, not model).
PLAN_SYNTHESIS_POLICY = "live-model-plan-synthesis/v1"


def plan_synthesis_prompt(*, with_evidence_refs: bool) -> str:
    """The system prompt for the one live plan-synthesis call.

    The contract is the cohort plan shape: bounded steps, claims that
    CITE the frozen bytes (repo key + path + line + the quoted bytes), a
    missing decision becoming a QUESTION, never an invented default.
    """
    citations = (
        "Steps MAY cite the listed evidence ids via evidence_refs (e.g. "
        '["ev-1"]); a citation of an id NOT in the list invalidates the plan.'
        if with_evidence_refs
        else "There are no citable evidence ids in this pass — do not invent any."
    )
    return (
        "You are the planning stage of a bounded change pipeline over FROZEN "
        "repository snapshots. Keep the plan MINIMAL (at most 4 steps, at most "
        "4 claims) and spend no effort on prose. Respond with ONLY a JSON "
        'object of shape {"steps": [{"step_id": str, "objective": str}], '
        '"claims": [{"claim_id": str, "text": str, "repo": str, "path": str, '
        '"line": int, "asserted_content": str}], "questions": [str], '
        '"assumptions": [str]}. Rules: cite only bytes the evidence block '
        "actually shows (the exact repo key, path and the LINE NUMBER shown "
        "beside the quoted bytes; asserted_content must quote those bytes "
        "verbatim); a rule you did not observe must become a question, never "
        "a claim or an assumed default. " + citations
    )


def _numbered_window(
    snapshot: Snapshot, repo_key: str, path: str, line: int, *, span: int = 2
) -> str:
    """One evidence window rendered with line numbers beside the bytes."""
    lines = snapshot.files_of(repo_key).get(path, "").splitlines()
    start = max(1, line - span)
    stop = min(len(lines), line + span)
    if stop < start:
        return ""
    body = "\n".join(f"{number:5d}| {lines[number - 1]}" for number in range(start, stop + 1))
    return f"[{repo_key} {path} lines {start}..{stop}]\n{body}"


def _numbered_observation_block(
    snapshot: Snapshot, observations: Sequence[ToolObservation], *, limit: int = 6
) -> str:
    """The read observations rendered with LINE NUMBERS beside the bytes.

    The research loop shows byte offsets; the planner must cite line
    numbers.  The render re-derives each read's line range from the same
    frozen snapshot, so a citation can quote exactly what was seen.
    """
    blocks: list[str] = []
    for observation in observations[-limit:]:
        if observation.error:
            continue
        call = str(observation.call)
        match = re.match(r"(?P<path>\S+) offset (?P<offset>\d+)", call)
        if not match or not observation.content:
            continue
        path = match.group("path")
        offset = int(match.group("offset"))
        content = snapshot.files_of(observation.repo_key).get(path, "")
        start_line = content[:offset].count("\n") + 1
        rendered = [
            f"{start_line + index:5d}| {text}"
            for index, text in enumerate(observation.content.splitlines())
        ]
        if not rendered:
            continue
        blocks.append(
            f"[{observation.repo_key} {path} lines "
            f"{start_line}..{start_line + len(rendered) - 1}]\n" + "\n".join(rendered)[:4000]
        )
    return "\n\n".join(blocks) if blocks else "(no readable observations)"


def render_plan_synthesis_user_prompt(
    task: CohortTask,
    repos: Mapping[str, ResearchRepo],
    *,
    evidence_block: str,
    evidence_note: str,
) -> str:
    """The one plan-synthesis user prompt (the evidence block differs per
    arm — that difference is the planner-mode comparison)."""
    menu = json.dumps(
        {
            key: {"repository_id": repo.repository_id, "source_oid": repo.source_oid}
            for key, repo in sorted(repos.items())
        },
        sort_keys=True,
    )
    return (
        f"ISSUE:\n{task.statement}\n\n"
        f"REPOSITORIES (cite claims with these repo keys):\n{menu}\n\n"
        f"{evidence_note}:\n{evidence_block}\n\n"
        "Emit the plan JSON now."
    )


def research_evidence_block(
    document: Mapping[str, Any],
    findings_doc: Sequence[Mapping[str, Any]],
    snapshot: Snapshot,
    observations: Sequence[ToolObservation],
) -> str:
    """The research arm's evidence: the run's summary, its citable
    findings (with the evidence ids the citation integrity check will
    resolve) and the bytes its tools actually read."""
    listed = (
        "\n".join(
            f"- {finding.get('evidence_id')} {finding.get('repo_key')}:{finding.get('path')}:"
            f"{finding.get('line')} ({finding.get('kind')}: {finding.get('detail')})"
            for finding in findings_doc
        )
        or "(the investigation established no findings)"
    )
    summary = str(document.get("summary") or "") or (
        "(the budget stopped the investigation before a summary — the "
        "observations are what was read)"
    )
    return (
        f"RESEARCH SUMMARY:\n{summary}\n\n"
        f"FINDINGS (the ONLY citable evidence ids):\n{listed}\n\n"
        f"TOOL OBSERVATIONS (the actual bytes read, with line numbers):\n"
        f"{_numbered_observation_block(snapshot, observations)}"
    )


def lexical_evidence_block(snapshot: Snapshot, probes: Sequence[Mapping[str, Any]]) -> str:
    """The lexical arm's evidence: the deterministic probe hits with the
    frozen bytes beside each hit."""
    if not probes:
        return "(the lexical probes matched nothing)"
    blocks = [
        _numbered_window(
            snapshot,
            str(probe.get("repo_key") or ""),
            str(probe.get("path") or ""),
            int(probe.get("line") or 0),
        )
        for probe in probes
    ]
    return "\n\n".join(block for block in blocks if block)


def model_plan_document(
    parsed: Mapping[str, Any],
    known_evidence_ids: Mapping[str, Any] | set[str],
) -> dict[str, Any] | None:
    """Normalize + structurally gate one model-emitted plan.

    Fail-closed (returns ``None``, the attempt stays a preserved
    failure): wrong shape, a claim without repo/path/int line, or a step
    citing an evidence id outside the run's own findings.  A claim whose
    bytes do not match the snapshot is NOT rejected here — that is the
    GRADER's verdict (``invalid`` / ``syntactic_only``), reported
    honestly rather than laundered at parse time.  The ``surface`` is
    derived mechanically from the claims the model actually made.
    """
    known = set(known_evidence_ids)
    steps_raw = parsed.get("steps")
    claims_raw = parsed.get("claims")
    questions_raw = parsed.get("questions")
    assumptions_raw = parsed.get("assumptions")
    for entry in (steps_raw, claims_raw, questions_raw, assumptions_raw):
        if not isinstance(entry, list):
            return None
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps_raw[: _MAX_PLAN_ANCHORS + 2], start=1):
        if not isinstance(step, Mapping):
            return None
        entry: dict[str, Any] = {
            "step_id": str(step.get("step_id") or f"s{index}"),
            "objective": str(step.get("objective") or "")[:300],
        }
        refs = step.get("evidence_refs")
        if isinstance(refs, list) and refs:
            clean: list[str] = []
            for ref in refs:
                ref_id = str(ref)
                if ref_id.startswith("evidence:"):
                    ref_id = ref_id[len("evidence:") :]
                if ref_id not in known:
                    return None
                clean.append(ref_id)
            entry["evidence_refs"] = clean
        steps.append(entry)
    if not steps:
        return None
    claims: list[dict[str, Any]] = []
    for index, claim in enumerate(claims_raw[:_MAX_PLAN_ANCHORS], start=1):
        if not isinstance(claim, Mapping):
            return None
        repo = str(claim.get("repo") or "")
        path = str(claim.get("path") or "")
        if not repo or not path:
            return None
        try:
            line = int(claim.get("line"))
        except (TypeError, ValueError):
            return None
        claims.append(
            {
                "claim_id": str(claim.get("claim_id") or f"c{index}"),
                "text": " ".join(str(claim.get("text") or "").split())[:400],
                "repo": repo,
                "path": path,
                "line": line,
                "asserted_content": str(claim.get("asserted_content") or ""),
            }
        )
    questions = [
        {"question_id": f"q{index}", "text": " ".join(str(entry).split())[:300]}
        for index, entry in enumerate(questions_raw[:3], start=1)
        if str(entry).strip()
    ]
    assumptions = [
        {"text": str(entry)[:300]} for entry in assumptions_raw[:6] if str(entry).strip()
    ]
    surface: list[dict[str, Any]] = []
    seen_surface: set[tuple[str, str]] = set()
    for claim in claims:
        pair = (claim["repo"], claim["path"])
        if pair in seen_surface:
            continue
        seen_surface.add(pair)
        surface.append({"repo": pair[0], "path": pair[1], "why": "claim"})
    return {
        "steps": steps,
        "surface": surface,
        "claims": claims,
        "questions": questions,
        "assumptions": assumptions,
        "synthesis": "live-model/v1",
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


def _live_cost(
    base: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    *,
    plan_receipt_usage: tuple[int | None, int | None] | None,
    document_tokens: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The live arm's cost: the research document's tool accounting PLUS
    the plan-synthesis call's usage, and the USD the receipts recorded.

    A missing receipt stays visible (``usd_coverage``) — never silently
    zero; unknown usage keeps the totals lower bounds.
    """
    tokens = dict(document_tokens or {})
    input_exact = tokens.get("input") is not None
    output_exact = tokens.get("output") is not None
    input_known = int(tokens.get("input_lower_bound") or 0)
    output_known = int(tokens.get("output_lower_bound") or 0)
    unknown_calls = int(tokens.get("unknown_usage_calls") or 0)
    if plan_receipt_usage is not None:
        pin, pout = plan_receipt_usage
        if pin is None or pout is None:
            input_exact = output_exact = False
            unknown_calls += 1
        else:
            input_known += pin
            output_known += pout
    return {
        "calls_proposed": int(base.get("calls_proposed") or 0),
        "calls_executed": int(base.get("calls_executed") or 0),
        "wall_seconds_used": float(base.get("wall_seconds_used") or 0.0),
        "tokens": {
            "input": input_known if input_exact else None,
            "output": output_known if output_exact else None,
            "input_lower_bound": input_known,
            "output_lower_bound": output_known,
            "unknown_usage_calls": unknown_calls,
        },
        "usd_estimated": round(
            sum(float(entry.get("usd_estimated") or 0) for entry in receipts), 6
        ),
        "usd_coverage": "receipted" if receipts else "none-recorded",
        "receipts": [dict(entry) for entry in receipts],
    }


async def _live_plan_synthesis(
    capped: Callable[[str, int], CompletionFn],
    task: CohortTask,
    repos: Mapping[str, ResearchRepo],
    *,
    system: str,
    evidence_block: str,
    evidence_note: str,
    known_evidence_ids: set[str],
    plan_max_tokens: int,
) -> dict[str, Any]:
    """Run the ONE live plan-synthesis call and gate its output.

    Returns the model's plan document (with ``_plan_receipt_usage``
    riding along for the cost block), or a marker document carrying the
    honest ``plan_failure`` reason — a failed, truncated or capped
    synthesis is preserved, never retried into a pool.
    """
    seam = capped("plan-synthesis", plan_max_tokens)
    user = render_plan_synthesis_user_prompt(
        task, repos, evidence_block=evidence_block, evidence_note=evidence_note
    )
    try:
        result = await seam(system, user)
    except SpendCapReached as exc:
        return {
            "plan_failure": f"spend_cap: {exc}",
            "_failed": True,
            "_plan_receipt_usage": None,
        }
    except Exception as exc:  # noqa: BLE001 — preserve the failure, never crash the cohort
        # A transport/gateway failure during synthesis is an honest FAILED
        # attempt (the research loop treats its own gateway errors the same
        # way): recorded with the reason and any spend already landed,
        # never retried into a pool.
        return {
            "plan_failure": f"gateway_error: {type(exc).__name__}",
            "_failed": True,
            "_plan_receipt_usage": None,
        }
    text = str(getattr(result, "text", result))
    usage = (
        getattr(result, "input_tokens", None),
        getattr(result, "output_tokens", None),
    )
    parsed = _first_json_object(text)
    if parsed is None:
        return {
            "plan_failure": "malformed_or_invalid_plan_json",
            "_failed": True,
            "_plan_receipt_usage": usage,
        }
    plan = model_plan_document(parsed, known_evidence_ids)
    if plan is None:
        return {
            "plan_failure": "invalid_plan_shape_or_unresolved_citations",
            "_failed": True,
            "_plan_receipt_usage": usage,
        }
    plan["_plan_receipt_usage"] = usage
    return plan


def _strip_plan_markers(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(plan, Mapping):
        return None
    cleaned = {key: value for key, value in plan.items() if not key.startswith("_")}
    return cleaned or None


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
    spend: SpendLedger | None = None,
    issue: str = "R36-12 / #271",
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

    R37-11 (gen-2): under a live provider (gateway env or injected
    *completion*) the PLAN is synthesized by the REAL model in EVERY
    arm — the none/lexical arms differ from research only in what their
    discovery phase established, so the comparison is planner-MODE, not
    model — and every model call rides the :class:`SpendLedger` (a
    shared *spend* enforces the cohort-wide hard dollar cap; the
    preregistration budget's ``max_usd_per_arm`` bounds each arm's
    slice).  A plan synthesis that fails, truncates or hits a cap is
    preserved as a failed attempt with its receipts.
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
    # The run records the FULL pre-registered budget it executed under (the
    # replay compares it against the spec's budget verbatim); the loop's
    # call/wall caps are the two keys every generation must carry.
    budget = dict(preregistration.budget)
    max_calls = int(budget.get("max_calls") or 0)
    wall_seconds = float(budget.get("wall_seconds") or 0.0)
    repos = _research_repos(snapshot)
    recorded_at = captured_at or preregistration.registered_at

    # -- resolve the live source (injected completion > gateway env) ------
    live_inner: CompletionFn | None = None
    live_identity = ""
    live_route = ""
    if completion is not None:
        live_inner = completion
        live_identity = completion_identity or "injected-completion"
        live_route = "injected-live-completion"
    elif gateway is not None:
        live_identity = gateway.model
        live_route = gateway.route

    # -- the live bookkeeping: one ledger (shared = the hard total cap) ---
    live_mode = live_inner is not None or gateway is not None
    ledger: SpendLedger | None = None
    arm_receipt_start = 0
    per_arm_usd = float(preregistration.budget.get("max_usd_per_arm") or 0.0) or None
    if live_mode:
        if spend is not None:
            ledger = spend
        else:
            ledger = SpendLedger(
                model=live_identity,
                limit_usd=float(preregistration.budget.get("max_usd_total") or MAX_COHORT_USD),
            )
        arm_receipt_start = len(ledger.receipts)
        arm_start_usd = ledger.spent_usd
        if now is None:
            clock = time.monotonic  # live captures ride the real wall clock

        def _allowance() -> float | None:
            if per_arm_usd is None or ledger is None:
                return None
            return round(per_arm_usd - (ledger.spent_usd - arm_start_usd), 6)

        def _capped(purpose: str, max_tokens: int) -> CompletionFn:
            # The gateway seam is built PER PURPOSE: its max_tokens rides the
            # HTTP call itself, so a plan synthesis really gets the plan cap
            # (a shared 1024-default seam would truncate every synthesis).
            assert ledger is not None
            inner = (
                gateway_completion(gateway, max_tokens=max_tokens)
                if gateway is not None
                else live_inner
            )
            assert inner is not None
            return capped_completion(
                inner,
                ledger,
                max_tokens=max_tokens,
                purpose=purpose,
                allowance_usd=_allowance,
            )

        plan_max_tokens = int(preregistration.budget.get("plan_max_tokens") or 8000)
    else:
        _capped = None  # type: ignore[assignment]

    plan_failure = ""
    observations: list[ToolObservation] = []
    document: dict[str, Any] = {}

    if mode == MODE_RESEARCH:
        if live_mode:
            assert _capped is not None and ledger is not None
            inner: CompletionFn = _capped(
                "research", int(preregistration.budget.get("max_tokens_per_call") or 3000)
            )
            provenance = PROVENANCE_LIVE_MODEL
            identity = f"live:{live_identity}"
            route = live_route
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
        harness = ResearchHarness(complete=wrapped, max_calls=max_calls, wall_seconds=wall_seconds)
        outcome = await run_research_pass(
            harness,
            planner_input=task.statement,
            lexical=[],
            repos=repos,
            now=clock,
        )
        # Mint the REAL observations by re-executing the recorded
        # proposals through the same frozen executor the loop used.
        observations = [_execute_call(call, repos)[1] for call in proposals]
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
        if stopped == "gateway_error: SpendCapReached":
            stopped = "spend_cap"
        if live_mode:
            # R37-11: the REAL model synthesizes the plan from what THIS run
            # established.  No findings → nothing to plan from: a FAILED
            # attempt, preserved with its cost (never a hallucinated plan).
            if findings_doc:
                plan_doc = await _live_plan_synthesis(
                    _capped,
                    task,
                    repos,
                    system=plan_synthesis_prompt(with_evidence_refs=True),
                    evidence_block=research_evidence_block(
                        document, findings_doc, snapshot, observations
                    ),
                    evidence_note="RESEARCH EVIDENCE (what the investigation established)",
                    known_evidence_ids={str(entry["evidence_id"]) for entry in findings_doc},
                    plan_max_tokens=plan_max_tokens,
                )
            else:
                plan_doc = {
                    "plan_failure": stopped or "research_produced_no_findings",
                    "_failed": True,
                    "_plan_receipt_usage": None,
                }
            plan_failure = str(plan_doc.get("plan_failure") or "")
            plan: dict[str, Any] | None = None if plan_failure else _strip_plan_markers(plan_doc)
            base_cost = {
                "calls_proposed": int(document.get("calls_proposed") or 0),
                "calls_executed": int(document.get("calls_executed") or 0),
                "wall_seconds_used": float(document.get("wall_seconds_used") or 0.0),
            }
            cost = _live_cost(
                base_cost,
                ledger.receipts[arm_receipt_start:],
                plan_receipt_usage=plan_doc.get("_plan_receipt_usage"),
                document_tokens=dict(document.get("tokens") or {}),
            )
            harness_name = "forge.adaptive.research_planner.run_research_pass + live plan synthesis"
        else:
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
        if live_mode:
            assert _capped is not None and ledger is not None
            plan_doc = await _live_plan_synthesis(
                _capped,
                task,
                repos,
                system=plan_synthesis_prompt(with_evidence_refs=False),
                evidence_block=lexical_evidence_block(snapshot, probes),
                evidence_note="LEXICAL PROBE EVIDENCE (deterministic identifier probes)",
                known_evidence_ids=set(),
                plan_max_tokens=plan_max_tokens,
            )
            plan_failure = str(plan_doc.get("plan_failure") or "")
            plan = None if plan_failure else _strip_plan_markers(plan_doc)
            cost = _live_cost(
                {"calls_proposed": 0, "calls_executed": 0, "wall_seconds_used": 0.0},
                ledger.receipts[arm_receipt_start:],
                plan_receipt_usage=plan_doc.get("_plan_receipt_usage"),
                document_tokens=None,
            )
            provenance = PROVENANCE_LIVE_MODEL
            identity = f"live:{live_identity}"
            route = live_route
            live_provider = True
            harness_name = "forge.adaptive.discovery_stage lexical probes + live plan synthesis"
        else:
            plan = _synthesize_plan(task, snapshot, probes, summary="", evidence_prefix="lex-")
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
        document = {
            "probes": probes,
            "summary": "",
        }
        stopped = ""
    elif mode == MODE_NONE:
        if live_mode:
            assert _capped is not None and ledger is not None
            plan_doc = await _live_plan_synthesis(
                _capped,
                task,
                repos,
                system=plan_synthesis_prompt(with_evidence_refs=False),
                evidence_block=(
                    "(no discovery ran — the issue statement is everything the plan may rest on)"
                ),
                evidence_note="DISCOVERY EVIDENCE",
                known_evidence_ids=set(),
                plan_max_tokens=plan_max_tokens,
            )
            plan_failure = str(plan_doc.get("plan_failure") or "")
            plan = None if plan_failure else _strip_plan_markers(plan_doc)
            cost = _live_cost(
                {"calls_proposed": 0, "calls_executed": 0, "wall_seconds_used": 0.0},
                ledger.receipts[arm_receipt_start:],
                plan_receipt_usage=plan_doc.get("_plan_receipt_usage"),
                document_tokens=None,
            )
            provenance = PROVENANCE_LIVE_MODEL
            identity = f"live:{live_identity}"
            route = live_route
            live_provider = True
            harness_name = "planner-input construction + live plan synthesis"
        else:
            plan = _synthesize_plan(task, snapshot, [], summary="")
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
        document = {}
        stopped = ""
    else:  # pragma: no cover - the arms check above already refused
        raise CohortSpecError(f"unknown arm {mode!r}")

    if plan_failure:
        # The attempt did not produce a usable plan: the failure IS the
        # attempt's honest stop — preserved with its spend, never retried.
        stopped = stopped or f"plan_synthesis_failed: {plan_failure}"

    attempt: dict[str, Any] = {
        "attempt": 1,
        "stopped_reason": stopped,
        "cost": cost,
    }
    if plan_failure:
        attempt["plan_failure"] = plan_failure

    capture_block: dict[str, Any] = {
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
        "issue": issue,
    }
    if ledger is not None:
        arm_receipts = ledger.receipts[arm_receipt_start:]
        capture_block["plan_synthesis"] = PLAN_SYNTHESIS_POLICY
        capture_block["spend"] = {
            "usd_estimated": round(
                sum(float(entry.get("usd_estimated") or 0) for entry in arm_receipts), 6
            ),
            "cap_usd_total": ledger.limit_usd,
            "cap_usd_per_arm": per_arm_usd,
            "cap_exhausted": ledger.exhausted,
            "receipts_count": len(arm_receipts),
            "price_note": PRICE_NOTE,
        }
        if plan_failure:
            capture_block["plan_failure"] = plan_failure

    return {
        "schema": "forge.research.cohort.run/1",
        "task_id": task.task_id,
        "mode": mode,
        "snapshot_digest": task.snapshot_digest,
        "budget": budget,
        "attempts": [attempt],
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
        "capture": capture_block,
    }
