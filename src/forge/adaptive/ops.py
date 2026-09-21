"""Operator-facing projections: plan quality, lineage, budget, status, policy.

The OPS epic's through-line: a lane that reports on itself must report
HONESTLY and from ONE place. Model selection is a cost/quality trade
that only means something against a plan-quality record computed BEFORE
any model comparison happens (OPS-01); token spend is only governable
when every stage's usage is itemized end-to-end instead of conflated
into one number (OPS-02); an operator staring at a dashboard needs ONE
coherent projection whose summary names the ONE next action, not three
widgets disagreeing (OPS-03); evidence deletion follows a policy that
refuses to act on what it does not understand (OPS-04); and the service
protects itself with explicit admission caps and failure-oriented
objectives — success rate measured against an SLO, not happy-path
averages (OPS-05).

Pure functions over plain dicts throughout: these are derived views,
cheap to recompute and never a second source of truth.
"""

from __future__ import annotations

__all__ = [
    "PLAN_QUALITY_SCHEMA",
    "STAGES",
    "admission_check",
    "budget_report",
    "error_budget",
    "plan_quality",
    "retention_decision",
    "status_projection",
    "usage_lineage",
]

#: Discriminator tag for plan-quality records. Quality is a measurement
#: with a versioned meaning: if the scoring rules ever change in a way
#: that breaks comparability, the tag bumps and old records stay
#: interpretable under the rules they were computed with.
PLAN_QUALITY_SCHEMA = "forge.plan.quality/1"

#: The end-to-end stage vocabulary a run's usage lineage itemizes. Every
#: stage always appears in a lineage — an unused stage reports zeros,
#: not absence, so "no spend" and "never measured" stay distinguishable.
STAGES: tuple[str, ...] = (
    "discovery",
    "planning",
    "implementation",
    "verification",
    "review",
)

#: Discovery shapes that count as evidence a repository was researched.
#: A discovery dict with no non-empty value under one of these keys is
#: an empty handshake, not research — ``repository_researched`` stays
#: False no matter what the plan asserts about itself.
_DISCOVERY_EVIDENCE_KEYS: tuple[str, ...] = ("evidence", "findings", "repositories")

#: The residencies a retention policy may declare. Anything else is a
#: policy this code does not understand, and an unknown policy routes to
#: human review — never to a silent delete.
_KNOWN_RESIDENCIES: tuple[str, ...] = ("eu", "us", "any")


def plan_quality(plan: dict, discovery: dict | None) -> dict:
    """Score a plan's quality — measured BEFORE model selection comparisons.

    OPS-01: which model to use is a cost/quality trade, and that trade
    is only honest when quality is a number computed FIRST, from the
    plan's own shape, with no model's self-assessment in the loop. The
    record (schema ``forge.plan.quality/1``):

    - ``citation_coverage`` — the fraction of steps carrying non-empty
      ``evidence_refs`` (or ``citations`` — both spellings accepted, the
      check is the same: a step cites or it does not). A plan with NO
      steps is fully covered; there was nothing to leave unevidenced.
    - ``open_questions`` / ``assumptions`` — plan-level counts of the
      unresolved questions and stated assumptions: the human reviewer's
      preview of what the plan itself is unsure about.
    - ``repository_researched`` — True ONLY when *discovery* is a dict
      with evidence present (a non-empty ``evidence`` / ``findings`` /
      ``repositories`` entry). ``None`` or an empty handshake means no
      research happened.
    - ``fast_path`` — an honesty marker, not a judgment: a plan built
      without repository research IS the fast path, and deriving the
      flag (instead of accepting it) is what keeps it from being
      flattered into True.
    """
    steps = plan.get("steps") or []
    cited = sum(1 for step in steps if step.get("evidence_refs") or step.get("citations"))
    coverage = cited / len(steps) if steps else 1.0
    researched = discovery is not None and any(
        discovery.get(key) for key in _DISCOVERY_EVIDENCE_KEYS
    )
    return {
        "schema": PLAN_QUALITY_SCHEMA,
        "citation_coverage": coverage,
        "open_questions": len(plan.get("questions") or []),
        "assumptions": len(plan.get("assumptions") or []),
        "repository_researched": researched,
        "fast_path": not researched,
    }


def usage_lineage(events: list[dict]) -> dict:
    """Sum model usage per stage, end-to-end; stages never conflated (OPS-02).

    Each event is ``{"stage": <one of :data:`STAGES`>, "call_id": str,
    "input_tokens": int, "output_tokens": int, "cached_tokens": int}``
    (``cached_tokens`` defaults to 0). The lineage carries one row per
    stage — all five, zeroed when unused — plus a ``total`` row that is
    the SUM of the stage rows, never a second counter that can drift
    from them. Discovery spend is discovery spend: nothing migrates to
    the nearest bucket. An event naming an unknown stage raises
    ``ValueError`` — a mislabelled spend must fail visibly, not land
    somewhere plausible.
    """
    lineage: dict[str, dict[str, int]] = {
        stage: {"calls": 0, "input": 0, "output": 0, "cached": 0} for stage in STAGES
    }
    for event in events:
        stage = event["stage"]
        if stage not in lineage:
            raise ValueError(f"unknown usage stage: {stage!r}")
        row = lineage[stage]
        row["calls"] += 1
        row["input"] += event["input_tokens"]
        row["output"] += event["output_tokens"]
        row["cached"] += event.get("cached_tokens", 0)

    total = {"calls": 0, "input": 0, "output": 0, "cached": 0}
    for row in lineage.values():
        for key in total:
            total[key] += row[key]
    lineage["total"] = total
    return lineage


def budget_report(lineage: dict, budget: dict) -> dict:
    """Report call spend against a ``{"max_calls"}`` budget (OPS-02).

    ``total_calls`` is re-derived from the per-stage rows rather than
    trusted from a stored ``total`` key — the report must describe the
    lineage it was handed. ``headroom`` is signed
    (``budget_calls - total_calls``): a negative headroom IS the
    over-budget signal, so ``within_budget`` compares the two without
    clamping either. Spending exactly the budget is within it.
    """
    per_stage = {stage: row for stage, row in lineage.items() if stage != "total"}
    total_calls = sum(row["calls"] for row in per_stage.values())
    budget_calls = budget["max_calls"]
    return {
        "per_stage": per_stage,
        "total_calls": total_calls,
        "budget_calls": budget_calls,
        "within_budget": total_calls <= budget_calls,
        "headroom": budget_calls - total_calls,
    }


def status_projection(run: dict, questions: list[dict], saga: dict | None) -> dict:
    """ONE coherent operator projection whose summary names ONE action (OPS-03).

    Widgets disagree when state, blockage and wait-reason are projected
    separately; here they are one record. ``waiting_on`` is ordered: an
    unresolved question outranks a partially published saga, because a
    human answer unblocks more than republishing does — and a question
    without a resolution marker counts as unresolved (fail toward
    operator attention, never toward silence). ``summary`` follows the
    SAME order, so the human line can never contradict the
    machine-readable fields: it names the one next action — answer the
    open question, finish the publication, clear the blockage, or let
    the run proceed.
    """
    unresolved = [question for question in questions if not question.get("resolved")]
    if unresolved:
        waiting_on = "question"
    elif saga is not None and saga.get("state") == "partially_published":
        waiting_on = "saga"
    else:
        waiting_on = None

    state = run["status"]
    blocked_reason = run.get("blocked_reason", "")
    if waiting_on == "question":
        plural = "" if len(unresolved) == 1 else "s"
        summary = f"Answer {len(unresolved)} open question{plural} to unblock the run."
    elif waiting_on == "saga":
        summary = "Finish publishing the partially published saga to complete the run."
    elif blocked_reason:
        summary = f"Clear the blockage to proceed: {blocked_reason}"
    else:
        summary = f"Run is {state}; let it proceed — no operator action pending."

    return {
        "state": state,
        "blocked_reason": blocked_reason,
        "waiting_on": waiting_on,
        "summary": summary,
    }


def retention_decision(artifact_age_days: int, policy: dict) -> str:
    """Decide ``keep`` / ``delete`` / ``review`` under a retention policy (OPS-04).

    The policy is ``{"min_days": int, "max_days": int, "residency":
    "eu"|"us"|"any"}``. Under ``min_days`` → keep (still fresh
    evidence); over ``max_days`` → delete; between (inclusive) →
    review. An unknown residency — a value outside the vocabulary, or
    a policy that declares none at all — → ``review`` ALWAYS, even
    when the age alone would say delete: never silently destroy
    evidence under a policy you do not understand.
    """
    if policy.get("residency") not in _KNOWN_RESIDENCIES:
        return "review"
    if artifact_age_days < policy["min_days"]:
        return "keep"
    if artifact_age_days > policy["max_days"]:
        return "delete"
    return "review"


def admission_check(
    active_work: int, queue_depth: int, *, max_active: int = 4, max_queue: int = 32
) -> tuple[bool, str]:
    """Gate new work on explicit capacity caps (OPS-05).

    Admitted only when strictly under BOTH caps — at the cap is full,
    not room for one more. Active work is checked before queue depth so
    the reason names the BINDING constraint (``"capacity: active"`` vs
    ``"capacity: queue"``): an operator throttling intake needs to know
    which wall was hit, not merely that one of them was.
    """
    if active_work >= max_active:
        return False, "capacity: active"
    if queue_depth >= max_queue:
        return False, "capacity: queue"
    return True, "admitted"


def error_budget(failures: int, window_requests: int, slo: float) -> dict:
    """Failure-oriented service objective: observed success rate vs SLO (OPS-05).

    The budget is expressed from the FAILURE side on purpose — what an
    operator needs to know is how much failure room is LEFT.
    ``observed_success_rate`` is ``(requests - failures) / requests``
    over the window (an empty window observes a perfect rate: nothing
    failed because nothing ran). ``budget_remaining`` is the signed
    distance to the SLO, and ``exhausted`` is strictly-below: meeting
    the objective exactly still meets it.
    """
    if window_requests <= 0:
        observed = 1.0
    else:
        observed = (window_requests - failures) / window_requests
    return {
        "slo": slo,
        "observed_success_rate": observed,
        "budget_remaining": observed - slo,
        "exhausted": observed < slo,
    }
