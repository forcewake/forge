# v0.9 brief: task-aware harness selection (ADR-0023 implementation)

Status: in progress (2026-09-15) · Implements [ADR-0023](../adr/0023-dynamic-harness-selection.md) · Research: [research/harness-selection.md](../research/harness-selection.md)

Scope = ADR-0023 Decisions 1–5 minus v1.0+ items: plan-time selection frozen
into the RunSpec, gate-visible "Implementation" block, driver-filtered
GitLab templates, and (opt-in, off by default) dispatch-time fallback.
NOT in scope: repair-leg switching between cycles, telemetry ranking
(doctor suggestion surface is a follow-up), Actions-lane dispatch changes
beyond what exists (`FORGE_DRIVER` input already parameterizes the driver).

## 1. Config surface

- `ForgeConfig` (`src/forge/config.py` ForgeConfig block): `implement.harnesses` — ordered list of driver ids (["claude-code", …]); an empty/absent value means "the existing backend as a one-element list" (byte-compatible). Validation: ids ∈ the shipped driver set; entries must be ≥ the current backend (tighten-only is enforced at config-validation time the same way as today's backend tightening).
- Settings: `FORGE_HARNESS_PREFERENCE: str = ""` (comma-separated driver ids; the env form of the list for lab/CI usage), `FORGE_HARNESS_FALLBACK: bool = False` (dispatch-time fallback switch, OFF by default).

## 2. Compiler — `src/forge/runs/harness_selection.py` (pure, unit-tested)

```python
@dataclass(frozen=True)
class HarnessSelection:
    harness: str                      # selected driver
    fallbacks: tuple[str, ...]        # frozen ordered tail (subset of allowed)
    budget_class: str                 # trivial | standard | heavy
    reason: str                       # one line; planner's or "default"

def compile_harness_selection(
    preference: list[str],            # project's ordered list (may be empty)
    current_backend: str,             # today's configured backend driver
    available_lanes: set[str],        # lanes the project onboarded (creds present)
    planner_proposal: dict | None,    # {"harness": str, "budget_class": str, "reason": str} | None
    default_budget_class: str = "standard",
) -> HarnessSelection
```

Rules (each a separate test):
1. `available_lanes` always caps the result — a proposal can reorder, never extend.
2. Empty preference ⇒ list = [current_backend] (byte-compatible; fallbacks empty unless FALLBACK enabled and the list has >1).
3. Preference entries not in `available_lanes` are dropped (compiler input = onboarded lanes only; doctor feeds this).
4. Planner proposal honored iff its harness ∈ preference∩available; `budget_class` ∈ {trivial, standard, heavy} else default; reason defaults to "planner selection".
5. Fallbacks frozen = preference tail after the selected harness (∩ available), ALWAYS recorded in the spec even when the fallback switch is off (the spec describes the chain; the runtime switch is a separate policy).
6. Deterministic: same inputs → identical output (no clocks, no randomness).

## 3. RunSpec + policy digest

- `_build_run_spec_document` (runs/service.py) and the GitHub twin (runs/github_service.py): add `backend_config = {"harness", "harness_fallbacks", "budget_class", "selection_reason"}` (the existing `backend_config` keys stay — extend, don't break: keep old keys for the digest continuity note in tests).
- `RUN_SPEC_SCHEMA_VERSION` 1 → 2 (durable/models.py + the constant's comment).
- `_policy_digest` (both services) binds the new fields — changing preference invalidates pending gates (test: digest changes when the chain changes).
- `run.evidence["backend"]["harness"]` records the selection at run start (joins the existing backend evidence).

## 4. Plan comment — "Implementation" block

`_plan_comment` (GitLab) and the GitHub plan comment gain, after the plan
body and BEFORE the command footer:

```
## Implementation
- Harness: **claude-code** · model glm-5.3-flash[1m]
- Fallbacks: grok-build, opencode
- Budget class: standard (trivial|standard|heavy)
- Commit cycles: 3
- Selection reason: planner selection
```

(The block is 5 fixed lines; "Fallbacks:" line says `none` when empty.)
Tested: footer order, gate visibility, `/go` flow unchanged.

## 5. Planner proposal (optional, structured)

- `LLMPlanner` output schema gains OPTIONAL `harness`, `budget_class`, `selection_reason` keys (prompt documents the allowed set = the project's preference list, passed into the prompt as context). Parsing stays lenient: absent/invalid → None (compiler defaults). No new agent, no extra call.
- The provider-agnostic plan service passes `planner_proposal` into the compiler before freezing the spec.

## 6. Dispatch + fallback (opt-in)

- The `/go` publish leg dispatches the lane for `selection.harness` — GitLab: sets the new `FORGE_HARNESS_DRIVER` pipeline variable (§7); GitHub/Actions: passes `driver` input (already supported). The Actions lane's `harness_entry` ignores nothing new.
- Fallback advance (both services share one helper in `harness_selection.py`): when `FORGE_HARNESS_FALLBACK` is ON and a lane leg fails with `harness_infrastructure` classification BEFORE any candidate exists → journal an `action_log` entry ({"event":"harness_fallback","from":…,"to":…,"reason":…}), re-reserve the F22 budget for the next leg, re-dispatch from the frozen chain's next entry. Chain exhausted (or OFF, or code/quality failure) → existing blocked/failed semantics, untouched. Candidate exists → never switch (ADR-0016 single producer). Tests: fake-lane flows for both providers + the OFF-by-default invariant.

## 7. GitLab templates — driver filter (multi-driver repos)

Each `ci/templates/*.gitlab-ci.yml` job `rules` gains a driver selector so
a repo including MULTIPLE forge templates runs exactly one lane:

```yaml
rules:
  - if: '$FORGE_RUN_ID && ($FORGE_HARNESS_DRIVER == "" || $FORGE_HARNESS_DRIVER == "claude-code")'
```

(with each template's own id; GitLab CE supports `==` on variables in
rules). Single-driver repos (FORGE_HARNESS_DRIVER unset) behave exactly
as today — contract tests updated for the new rule lines. The Actions
workflow is untouched (its `driver` input already selects).

## 8. Doctor

`forge doctor --project` gains per-driver lane checks (variable NAMES
only): for each driver in the project's preference, report its required
harness variables present/absent; the compilable chain = preference ∩
lanes-with-creds. JSON output stays additive.

## 9. Test plan (exit bar)

- `tests/test_harness_selection.py`: compiler rules 1–6 (pure).
- Plan-comment/RunSpec tests: both services freeze the chain; digest
  sensitivity; "Implementation" block rendering (GitLab + GitHub).
- Fallback flow tests (off-by-default, infra-only, pre-candidate-only,
  journal + re-reserve, chain exhaustion) over the fake lanes.
- Template contract: every shipped GitLab template carries its driver
  filter; single-driver back-compat.
- Full suite + mypy green; no network tests.
