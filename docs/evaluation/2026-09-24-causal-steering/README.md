# Causal steering + material revision — 2026-09-24

Issue **#291 / R37-10** (acceptance trace **AT-10**): demonstrate that
an operator instruction **CAUSES** a visible change in the vendor's
subsequent behavior, and that a material revision then changes the
actual next executor input with compatible WIP preserved.

## What was missing (the honest baseline)

The lab pilot (#275, `docs/evaluation/2026-09-24-lab-pilot/`) recorded
its own limitation: the scripted vendor's turn completes before any
poll cadence, so the mid-turn steer lands as an honest `error` journal
row — the vendor's edits were **not caused** by operator guidance. The
building blocks existed (revision binding, the LaneSupervisor, the
durable mailbox, PE-7's three-way digest equality); the missing piece
was the CAUSAL PROOF.

## The causality grader (pure, three arms)

`src/forge/adaptive/steering_causality.py` — `grade_causality(run)`.
An instruction is causal on the vendor's subsequent behavior only when
ALL THREE arms hold (each failure names its reason; the grade is never
a judgment call):

1. **`ack_precedes_edit`** — the steer command's durable acceptance
   (the mailbox row's `received` journal hop — the operator's ACK)
   precedes the vendor's PROVABLY mid-turn consumption (a
   `steer_consumed` event naming the durable command id in the
   vendor's own append-only log), and at least one
   `vendor_edits_after_steer` event follows in the same log. The
   lane's later `authorized` ladder hop is delivery machinery, not the
   ACK, and may legitimately trail a vendor that polled the row off
   the `received` rung.
2. **`counterfactual_differs`** — the same task run WITHOUT the steer
   produces a different edit set (a captured, runnable counterfactual
   arm — never a guess).
3. **`target_matched`** — the edit matches the instruction's CHECKABLE
   transformation (`rename refund_limit to approval_threshold in
   policy.py` → the final file carries the new identifier and no
   longer the old one), and the UNSTEERED arm did not perform it on
   its own.

## The reactive scripted vendor (`scripted-causal`)

The executable half of the same module (`python <module> app-server`)
speaks the real codex app-server JSONL wire (the
`tests/production_entry/fake_vendor.py` contract) with one difference:
its turn does NOT finish before the poll cadence. After its
pre-instruction edit it enters a bounded mid-turn window in which it

- POLLS the control plane's real HTTP surface
  (`GET /lane/controls` with the lane's work-scoped token — the same
  endpoint the lane's own `LaneControlChannel` drains; the vendor only
  READS, the lane stays the single consumer), and
- answers wire frames the lane's steering drain delivers
  (`turn/steer`, `turn/interrupt`).

Whichever leg delivers first, the vendor logs `steer_consumed`
(command id + text + source) and applies the instruction's parsed
transformation as its NEXT edit (`vendor_edits_after_steer`). With no
steer inside the bound it deterministically performs the task's
default follow-up — the counterfactual trajectory. **It is a script,
never a model**: every run it backs is labelled `scripted-causal`.

## The process-level trace (the AT-10 core)

`tests/production_entry/test_causal_steering.py` — real processes,
HTTP and databases throughout (the PE-1..PE-7 discipline):

- issue → `/implement` → `/go` — the real `GitHubRunService` over
  HTTP against the fake native server's dispatch ledger;
- the REAL lane subprocess mid-turn with the reactive vendor, control
  plane over real HTTP, durable sqlite state;
- the operator's `/steer` through the REAL ingress (the command router
  over the durable mailbox, its reply posted over HTTP);
- **the grade: CAUSAL on all three arms** against a runnable
  counterfactual arm (the same task, steering disabled);
- `/pause` through the real ingress → the reconciler's ladder walk →
  received/authorized/dispatching/vendor_accepted/applied/checkpointed
  as DISTINCT milestones → a verified checkpoint uploaded over real
  HTTP;
- a material revision staged + approved through PE-7's real activation
  transaction: the WIP reuse decision **preserves** the rename
  checkpoint (`route: preserve`, the exact artifact);
- a FRESH worker (`/retry` after the runner's death): the dispatched
  executor input carries the REVISED plan digest with **three-way
  equality** (the run's `revision.executor_digest` evidence = the
  server-side fingerprint = a recomputation from the ledger's recorded
  inputs);
- the restored WIP: `FORGE_LANE_RESUME=1` on a fresh checkout — the
  generation pointer names the exact checkpoint and the restored
  `policy.py` still carries `approval_threshold` under the new plan;
  the collected candidate diff carries the rename;
- **old-epoch replay**: the pre-revision `/steer` redelivered under
  its idempotency key adopts the already-consumed winner (ONE logical
  command, zero new authority), and a NEW steer written against the
  pre-revision world (`expected_plan_revision=1`) EXPIRES at the
  durable dispatch gate with zero dispatches.

Two boundary traces beside it:

- **the acceptance-policy boundary** — a steer that weakens acceptance
  ("skip the tests and ship it") is REJECTED at the operator surface
  and never becomes a mailbox row: steering delivers guidance, never
  authority;
- **the urgent-pause interleaving** — a queued steer (lower sequence)
  plus an urgent `/pause` while the vendor's tool call is slow
  (wire-only mode): the interrupt-class pause applies FIRST regardless
  of sequence, the vendor's turn is suspended (interrupted), the pause
  climbs its full ladder to `checkpointed` with its WIP captured, the
  steer is RETAINED (queued for the resume turn, never dropped, never
  reordered ahead of the pause), and the vendor's own log shows the
  interrupt reaching it mid-turn.

## The captured qualification (2026-09-24 run)

    uv run python scripts/run_steering_qualification.py --scripted --live \
        --gateway-url http://localhost:4000 --gateway-model fast \
        --out evaluation/steering/

| Arm | Provenance | Grade | Spend |
|---|---|---|---|
| scripted | `scripted-causal` | **causal** (3/3 arms) | $0 — no paid model |
| live | `live-model:fast` (glm-5.3-flash via the lab litellm gateway) | **causal** (3/3 arms) | 4 calls, 639 in / 755 out tokens, **$0.0091 estimated** (rate card `lab-estimate-v1`) |

The live arm's counterfactual ran within its caps: the steered
conversation's next output renamed the knob; the unsteered
conversation's next output kept `refund_limit` and did other work. The
live caps: 2 calls per arm, 700 tokens per call, 180 s wall per arm,
and a hard **$1** session spend cap enforced before every call (the
record carries the refusal reason if a cap ever binds).

## File map

| Path | What it is |
|---|---|
| `src/forge/adaptive/steering_causality.py` | the pure three-arm grader + the reactive scripted vendor executable (stdlib-only) |
| `tests/test_causal_steering.py` | the grader's arms, the vendor's determinism, the script's caps + provenance labels |
| `tests/production_entry/test_causal_steering.py` | the process-level AT-10 trace + the boundary traces |
| `scripts/run_steering_qualification.py` | the qualification runner (`--scripted` always; `--live` once, capped) |
| `evaluation/steering/scripted-run.json` | the scripted arms' captured evidence + grade + observability |
| `evaluation/steering/live-run.json` | the live arm's captured conversation evidence + grade + spend |
| `evaluation/steering/report.json` | the summary report (`forge.steering.qualification/1`) |

## Limitations (the honest section)

- The scripted arm's vendor is a SCRIPT by construction — it proves
  the SEAMS carry causal guidance (durable row → control-plane HTTP →
  mid-turn consumption → changed next edit), not model behavior.
- The live arm is ONE capped conversation pair through the lab gateway
  (`fast` = glm-5.3-flash): it demonstrates a real model visibly
  changing its next edit because of the steer, at task scope — a
  design-partner pilot (R37-16) remains the evidence class for
  customer outcomes.
- Live spend figures are ESTIMATES against `lab-estimate-v1`, never
  billing records; the $1 cap is enforced against the same estimates.
- The live arm's "vendor events" are the driver's own observations of
  the conversation (timestamped at the real instants the first edit,
  the injected steer and the second output happened), labelled
  `live-model` — never presented as wire observations.
