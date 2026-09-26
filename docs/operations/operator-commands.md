# Operator commands (provider-neutral)

Every forge command is a **comment on the subject** — an issue (GitLab), an
issue (GitHub), or a work item (Azure DevOps) — parsed by the same
mention/first-token parser on all three providers. `@forge /go <run-id>`
and a bare `/go <run-id>` are equally valid; hyphenated commands parse as
one token. Forge's own comments contain command lines, so a bot-authored
note is never a trigger or an approval.

Commands live on the run's subject. `/security` is the one exception: it is
MR/PR-neutral by contract and also accepted on merge-request and pull-
request notes (triage targets MRs, issues and PRs). The review-feedback
verbs (`/fix`, `/ask`) are the second exception, in the other direction:
they are **MR-discussion-bound** — a reviewer comments on the Draft MR the
candidate opened — and currently **GitLab-only** (see
[Review feedback on the Draft MR](#review-feedback-on-the-draft-mr-gitlab)).

## The command table

| Command | Effect | Authorization | Provider parity |
|---|---|---|---|
| `/implement` | Starts a durable run: task snapshot → plan → plan comment with the Implementation block → pending gate decision; run parks `waiting_approval` | Admission gate before the first paid call: the actor must be on the connection's approver list, or the run parks `blocked(admission_denied)` — the planner is never constructed | GitLab issue comment · GitHub issue comment **or** the `forge` label · Azure DevOps work-item comment |
| `/go <run-id>` | Consumes the gate decision exactly once and advances: frozen RunSpec verified by digest → harness/builtin lane → candidate → Draft MR/PR | Approver list only; requires a pending, unconsumed decision that is still inside its TTL and whose plan/base/spec digests still match — expired or drifted decisions block the run with an explanatory comment, never a silent ignore | All three (Azure: work-item comment) |
| `/cancel [run-id]` | Cancel-as-revoke (F13): the publication grant is revoked FIRST, scheduled steps are withdrawn, then the run is cancelled durably. Bare = the latest active run on the subject; terminal runs are never touched | Approver list only | All three |
| `/retry [run-id]` | Tier-2 operator revival of a dead run (see [below](#revival-and-retry-tier-1-auto-tier-2-operator)) | Approver list only; rejection table below | All three |
| `/status [run-id]` | **Read-only** snapshot from durable state: status, reason, commit cycle, candidates, budget headroom, verification evidence, publication-intent states, revive/retry counters. Bare = the issue's latest run, any state | None — any authenticated webhook actor; the only write is the journaled reply note | All three |
| `/why-blocked [run-id]` | **Read-only** explanation of a terminal/blocked cause: the parked reason, its transient/fatal classification, and the honest revive/retry verdict — quoting the SAME rejection table `/retry` enforces | None — read-only, as `/status` | All three |
| `/reconcile <run-id>` | Drives the publication-intent recovery pass explicitly: probe → adopt / report duplicate / report unknown with manual instruction. Refuses runs with no publication intent — revival is `/retry`'s job, not this command's | Approver list only (it is the one mutating R29 command) | All three |
| `/security` | Runs the durable triage pass over security findings for the subject; the AI verdict is a SUGGESTION (`suggested_verdict`) | Confirming/rejecting a suggestion requires a triager (`FORGE_SECURITY_TRIAGERS`); `FORGE_SECURITY_AUTO_ACCEPT=true` is the explicit opt-out of that human confirm — default OFF | Issue/MR/PR-neutral on all three |
| `/fix <bounded description>` | Review feedback on the Draft MR (GitLab only, behind `FORGE_REVIEW_FEEDBACK_ENABLED`): classifies against the frozen write scope — every explicitly claimed path (backticked) inside it stages a bounded input revision through the EXISTING human gate; a human `/approve-revision` activates it and the reconciler's registered correction pass re-drives the run through the repair edge with the reviewer's text in the executor brief | Approver list only (the same authority as `/go`); unauthorized, deleted-discussion, conflicting, stale-head and closed-window cases are typed refusals with an operator-visible MR reply | GitLab MR note only |
| `/ask <question>` | Review feedback, clarification class: a durable clarification record + a reply routed to the approvers — **no coding attempt, no coder reservation, no dispatch** (reviewer-only recovery consumes no implementation budget) | Approver list only, like `/fix` | GitLab MR note only |

Approval authority is **connection-scoped** (ADR-0018 §3): GitHub reads
`FORGE_GITHUB_APPROVERS` and Azure DevOps `FORGE_AZDO_APPROVERS` (empty →
the shared `FORGE_APPROVERS` fallback). The lists never merge — a GitLab
username can never approve (or spend on) a GitHub run, and vice versa.

## Read-only vs mutating

- **Read-only:** `/status`, `/why-blocked`, and `/ask` (a clarification record
  plus its reply — no transitions, no model calls, no coder reservation, no
  dispatch). The reply note itself is the only journaled write.
- **Mutating:** everything else. `/implement` spends model budget (hence
  the pre-spend admission gate, ADR-0018 §3); `/go` consumes a one-shot
  gate; `/cancel` revokes a publication grant; `/retry` and `/reconcile`
  re-drive durable legs; `/fix` stages an input revision behind the human
  approval gate. Every external write stays intent-first in
  `action_log` (ADR-0005).

## The `forge` label (GitHub)

`FORGE_TRIGGER_LABEL` (default `forge`, case-insensitive) is the
comment-free equivalent of `/implement` on GitHub:

- **Label on** (`issues.labeled`) → the same run command as `/implement`,
  with the labeler as the actor — admission applies to whoever applied the
  label, not to an impersonal event.
- **Label off** (`issues.unlabeled`) → **cancel-at-gate**: only runs parked
  in `waiting_approval` (a regenerable plan) are cancelled durably. A run
  past the human gate is never yanked by a label removal.

On Azure DevOps the same semantics exist for the work-item **tag**, with an
honest detection limit (see [gaps](#honest-gaps)); there is no tag-on →
`/implement` path — use a work-item comment.

## Revival and retry (Tier 1 auto, Tier 2 operator)

A run that dies terminal is classified first, in ONE table shared by all
three lanes (`forge.runs.revival`) so the same reason text classifies the
same way everywhere:

- **Transient** (dispatch 5xx / network / timeout, rate limits, runner
  startup, an empty harness-start error) — Tier 1: the run parks `blocked`
  with a revival stamp and the reconciler re-dispatches the **same branch**
  after a bounded backoff ladder (`FORGE_RUN_REVIVE_BACKOFF_SECONDS`, 60s →
  120s → 240s … capped at 900s), at most `FORGE_RUN_AUTO_REVIVE_LIMIT`
  times (default 2; `0` disables). Journaled as `auto_revive`. No operator.
- **Fatal** (driver exit failed — a real quality signal, config errors such
  as 4xx input mismatches or a missing workflow, exhausted cycles) — parks
  `blocked` with the precise reason. No auto-retry, by design: a
  mis-classified fatal costs two bounded retries, a mis-classified
  transient hides a quality signal.

**`/retry`** is the operator override for everything Tier 1 correctly
refuses to touch. It walks `failed`/`blocked` → `proposing` through the
explicit revival graph edge, grants **one operator cycle** (the commit
cycle may exceed `FORGE_MAX_COMMIT_CYCLES`), and re-dispatches the **same
branch** with a repair context built from the terminal reason and the last
verification evidence — no new run id, no re-planning.

The rejection table (`/why-blocked` quotes the same one):

| Refusal when… | Because |
|---|---|
| another run is active on the subject | forge keeps one active run per subject — let it finish or `/cancel` it |
| the run is not `failed`/`blocked` | nothing to retry; cancelled runs and fresh work need `/implement` |
| an operator cancelled it | retrying a revoked publication grant is not allowed |
| it never committed a candidate | there is no work to continue in place — `/implement` is the honest path |

## Event-driven operator busywork

- **Auto-replan on issue edit** (all three providers): an edit to the
  issue/work-item text is compared against the task snapshot frozen at plan
  time. Still `waiting_approval` with an unconsumed gate → the stale run is
  cancelled durably and a fresh run replans from the new text (a note says
  the plan was regenerated). Beyond the gate → the agent executes the
  APPROVED snapshot; one informational note says the edit is *not* in the
  plan — never a mid-flight yank. Text identical to the snapshot → nothing
  happens (redeliveries and no-op churn filter out). Only an actor who
  could have started a run can trigger a replan (ADR-0009).
- **Superseded-PR janitor** (GitHub): when a successor run reaches the
  harness lane or publishes its Draft PR, OPEN Draft PRs on
  `failed`/`blocked`/`cancelled` predecessors' factory branches are closed
  with a note naming the successor. A `ready_for_human` predecessor's PR is
  the live deliverable and is never touched. Best-effort: one stale PR that
  cannot be closed is logged, never breaks the successor.

## Review feedback on the Draft MR (GitLab)

A reviewer reads the candidate and names an edit in a Draft-MR discussion. The
surface is wired end to end (R40-01 / #337) behind ONE capability flag:

- **`FORGE_REVIEW_FEEDBACK_ENABLED`** (default **OFF**). With the flag off the
  GitLab ingress does not parse `/fix` or `/ask` at all — zero routing, the
  classic workflow byte for byte (the `FORGE_ADAPTIVE_COMMANDS_ENABLED`
  pattern). GitHub and Azure DevOps never parse the verbs; the capability
  manifest (`forge doctor --capabilities`, row `operator-commands/review-feedback`)
  reports the surface as wired for the GitLab MR-note profile only.
- **The ingress**: a token-authenticated GitLab MR note with `/fix …` or
  `/ask …` travels the SAME durable run-command path as every note command —
  inbox row + scheduled step committed in ONE transaction, the 202 answered
  only after the commit (ADR-0017). The logical request identity is the NOTE
  (`(connection, project.id, mr.iid, note.id)` — a GitLab note id is unique
  inside a project); dedup runs at TWO layers: the per-delivery
  `X-Gitlab-Event-UUID` at the ingress (exact network replays) and the inbox
  unique index over the note identity (manual redeliveries, worker-restart
  replays). One logical note ⇒ one request, one journaled reply, one revision
  decision id, at most one authorized correction start.
- **The refusal posture**: a malformed verb (`/fix` with no description) is
  answered **2xx with a typed payload** (`feedback: refused`,
  `refusal_reason: malformed_feedback_command`) — never a 4xx, because GitLab
  auto-disables a webhook after 4 consecutive failures (24 h backoff,
  project-wide). Unauthorized reviewer, foreign-repository MR and the typed
  lifecycle refusals all leave the run untouched: no revision activation, no
  pipeline dispatch.
- **The correction cycle**: an in-scope `/fix` stages through the EXISTING
  approval route; `/approve-revision` (the adaptive verb) activates it; the
  INSTALLED periodic reconciler's registered correction pass re-drives the run
  through the repair edge — head-fenced (`stale_head` preserves human edits),
  scope-fenced (out-of-scope is a material proposal, never a permission
  expansion). The pass is also the recovery path when a delivery is lost:
  discovery of requests lives in the ingress/step path, authorization to start
  an attempt lives with the human approval.
- **The bot never merges and never resolves the discussion** — the reviewer's
  resolve is the human decision that gates readiness.

Full trace: `tests/production_entry/test_feedback_ingress.py`
(ASGI ingress → durable inbox → restarted worker → installed reconciler).
Design doc: `docs/evaluation/2026-09-25-review-feedback/README.md`.

## Honest gaps

- **Azure `unlabeled` detection is not exact.** The documented
  `workitem.updated` payload carries `resource.fields` as the work item's
  CURRENT values — no `oldValue`/`newValue` pairs — so a tag REMOVAL cannot
  be proven from one delivery; an update of any kind can look like one.
  The blast radius is bounded by construction (cancel-at-gate only, for an
  admitted actor), and the ambiguity disappears entirely when the Azure
  subscription is created with the `changedFields: System.Tags` filter —
  deliveries then fire only on tag changes. **Create the subscription with
  that filter** (the Azure onboarding guide's checklist includes it).
- GitHub's commit CAS check (`expectedHeadOid`) happens at publish time,
  inside forge — platform-side enforcement is a known gap tracked in the
  [guarantee matrix](../../README.md#guarantee-levels).

## Configuration

The knobs behind this surface (defaults shown are the shipped defaults):
`FORGE_APPROVERS` / `FORGE_GITHUB_APPROVERS` / `FORGE_AZDO_APPROVERS`,
`FORGE_TRIGGER_LABEL`, `FORGE_DECISION_TTL_SECONDS`,
`FORGE_RUN_AUTO_REVIVE_LIMIT`, `FORGE_RUN_REVIVE_BACKOFF_SECONDS`,
`FORGE_SECURITY_TRIAGERS`, `FORGE_SECURITY_AUTO_ACCEPT` — see
[`.env.example`](../../.env.example) (annotated) and
`src/forge/config.py` (authoritative).
