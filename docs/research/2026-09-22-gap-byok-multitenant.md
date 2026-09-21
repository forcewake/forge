# Multi-tenant BYOK / enterprise patterns — gap research (2026-09-22)

> Gap-analysis research for forge. Sources: BYOK architecture write-ups
> (osFoundry/DVARA, ModelPlane, tianpan.co), CMEK/envelope-encryption
> guides, enterprise procurement frameworks for agentic dev tools (2026);
> fetched 2026-09-22. Confidence marks: **[documented]** / **[observed]** /
> **[inference]**.

## Why it matters for forge

forge is self-hosted and effectively **single-tenant per installation**
today: one bot identity per provider (PAT / GitHub App / service account),
one LiteLLM, model keys as environment configuration. That is a feature
for the "own infrastructure" pitch — but two forge-adjacent motions make
multi-tenancy and BYOK the next real architecture questions:

1. **A shared forge instance serving several groups/projects** (the
   realistic GitLab CE deployment: one forge, many projects, model spend
   that must be attributed and fenced per project).
2. **Enterprise buyers of agentic dev tools**, who evaluate the *control
   plane*, not the agent — and whose first questions are about key
   custody, audit export, and compliance artifacts.

forge already has the right primitives to build on: credentials are
referenced by broker-owned names (`CredentialBinding.credential_ref` is a
name, never a value), lane harness keys are CI-variable-scoped, the
DriverMatrix is fail-closed on credential modes, and LiteLLM is native.
[inference, from repo]

## Findings

### 1. BYOK API-key architectures: the three shapes

[documented] ([BYOK multi-tenant AI](https://app-lab.ai/blog/byok-multi-tenant-ai),
[tianpan.co BYOK re-architecture](https://tianpan.co/blog/2026-05-14-byok-ai-features-sales-driven-rearchitecture))

- **Gateway (proxy-with-vault)**: the platform holds the customer key in
  its KMS, scopes it per tenant at the gateway, calls the provider on the
  tenant's behalf. Cleanest for product features (routing, caching,
  budgets, judge calibration); requires defending the gateway as a
  credential vault in security review.
- **Pass-through**: the customer's key is presented directly to the
  provider on every call; the customer sees every request in their own
  provider dashboard. Cleanest for trust; worst for platform observability
  (no provider-side token counts in platform traces).
- **Hybrid / embedded SDK**: client holds the key, server never sees the
  payload.
- A published 4-step **credential resolution chain**: tenant credential →
  platform default → vault (HashiCorp/AWS SM/Azure KV) → env var
  (dev-only). [documented, DVARA]
- Storage pattern: per-tenant envelope encryption — AES-256-GCM payloads,
  DEK wrapped by KEK in KMS; ModelPlane derives **per-user encryption keys
  via HKDF(user-id-salted root)** so a gateway can only ever decrypt the
  current tenant's credentials (`cred:{userId}:{credId}`).
  [documented](https://modelplane.dev/blog/byok-credential-security)
- [documented] Market pull: a 2025 Gartner snapshot (via osFoundry) put
  **64 % of enterprise GenAI buyers already holding direct provider
  contracts** — re-billing them through the vendor's account is friction.

### 2. The operational bill nobody costed

[documented] ([tianpan.co](https://tianpan.co/blog/2026-05-14-byok-ai-features-sales-driven-rearchitecture))
BYOK silently moves: cost attribution (onto the customer's dashboard —
platform traces lose provider-side accounting → "bill tripled, was it us
or them?" support tickets), incident response (customer-side key
compromise is their incident, but your debugging), observability (per-key
token/cost telemetry must be rebuilt), rate limiting (per-customer
provider limits you don't control), model-version pinning (customer's
provider account, their deprecations), and on-call accountability.
[inference] forge's `usage_receipts`/`llm_calls` ledger is exactly the
asset that avoids the "debugging blindness" failure mode — but only if it
keeps recording *complete* per-call usage even when the key (and bill) is
the customer's.

### 3. CMEK (customer-managed encryption keys) — the compliance grade of BYOK

[documented] ([BYOK security](https://app-lab.ai/blog/byok-ai-security),
[CMEK guide](https://vibeweek.ai/grow/customer-managed-encryption-keys-byok-chat),
[contentwave per-customer keys](https://contentwave.net/article/implementing-percustomer-encryption-keys-byok-in-multitenant-saas))
Envelope encryption with the **KEK in the customer's own KMS** (AWS/Azure/
GCP/Vault): the platform stores only wrapped DEKs; when the customer
revokes, the platform holds ciphertext it cannot unwrap — the posture that
supports HIPAA's encryption-safe-harbor and survives breach notification.
Custody ladder: true BYOK (customer material) → CMK (platform-created,
customer controls rotation/disable) → platform-managed. Enterprise-shipped
default in 2026: platform-managed at base tier, customer-KMS at enterprise
tier; CMEK cited as unlocking otherwise-lost deals and 20–40 % seat
premiums. [documented claims from vendor literature — treat magnitudes as
marketing-adjacent]
[inference] For forge the objects this must cover are not chat logs but
**checkpoints, snapshots and candidate artifacts** — they contain customer
source code, which is precisely the asset an enterprise security review
worries about.

### 4. What enterprise buyers of agentic dev tools actually demand

[documented] ([Enterprise AI coding agents: evaluation framework](https://vibecoding.app/blog/enterprise-ai-coding-agents),
[agentic.ai procurement checklist](https://agentic.ai/best/enterprise-coding-agents),
[enterprise IDE security checklist](https://aisecurityinpractice.com/foundations/enterprise-ide-security-checklist))

> "Enterprise buyers are not shopping for a better agent. They are
> shopping for a **control plane**" — what the organisation can see,
> scope, revoke and prove.

The five controls: **identity** (SSO + SCIM deprovisioning), **data
handling** (retention numbers, no-training in the contract), **audit**
(*an export into the buyer's own log store* — not a dashboard), **scope**
(per-team/per-repo agent access respecting source-control permissions),
**merge gate** (agent cannot land code on main without a human).
The six procurement questions, in order: (1) SOC 2 **Type II**, current,
readable under NDA; (2) what's in the report's *scope section*; (3) exact
retention number; (4) is no-training contractual; (5) can audit events
export to SIEM, at what granularity; (6) subprocessor list + change
notification.
[documented] Baseline certifications buyers expect: SOC 2 Type II (US),
ISO 27001 (international), plus domain-specific (HIPAA BAA, FedRAMP for
federal). Scope-specific caveats apply — a company-level badge may not
cover the product. Cursor's Jan 2026 SOC 2 Type II, Devin's ISO 27001,
Windsurf's FedRAMP High are the reference points.
[inference] forge's structural answers are unusually good — bot never
merges (ADR-0003), proposal-only lanes, evidence per commit — but they are
currently *documentation-shaped*, not *artifact-shaped*: there is no
SIEM-ready audit export, no retention-policy enforcement surface, no
SOC-2-style artifact pack, and the MCP scoped-token surface is the only
identity story.

### 5. Audit-trail standardization to build toward

[documented] The IETF **Agent Audit Trail** draft
([datatracker](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/01))
defines JSON records (agent identity, action type incl. `tool_call` /
`decision` / `delegation`, outcome, trust level), hash-chained for
tamper-evidence, mapped to EU AI Act / SOC 2 / ISO 42001 logging
requirements — a concrete target schema for forge's `action_log` +
`llm_calls` + MCP audit lines when enterprise export arrives.

## Concrete recommendations (ranked by effort/impact)

1. **Per-project BYOK model keys via LiteLLM virtual keys (medium
   effort, high impact).** Map `CredentialBinding.credential_ref` → a
   LiteLLM virtual key minted per project (or per run) with the project's
   own provider credential, model allowlist and USD `max_budget`; forge
   stays on the gateway side (the pattern LiteLLM already documents) and
   keeps its strict reservation ledger as the inner fence. The DriverMatrix
  's fail-closed credential modes extend naturally: a project without a
   bound key fails onboarding, never falls back to forge's key.
   [inference]
2. **Audit export (medium effort, high impact — it is procurement
   question #5).** A documented, streaming export of `action_log` +
   `llm_calls` + control-command ladder + MCP audit lines as JSONL/syslog
   (webhook or file sink), field-aligned with AAT vocabulary; retention
   policy as configuration with evidence in `forge doctor`. This turns
   forge's existing journals into the SIEM artifact buyers ask for.
   [inference]
3. **Per-tenant encryption of the artifact/checkpoint store (medium–high
   effort, compliance unlock).** Envelope-encrypt snapshots, checkpoints
   and candidate artifacts with per-project DEKs; a customer-KMS KEK
   option is the later enterprise tier. Pairs with OPS-04 (retention/
   residency) already in the backlog. [inference]
4. **Compliance artifact pack (low effort, high leverage for the
   enterprise motion).** A trust-center-style pack that forge-the-project
   can hand a buyer: subprocessor list (model providers, registry), the
   data-flow diagram already implied by the runbook §6, the no-training
   statement per provider route, the guarantee matrix as evidence index.
   Much of this exists as docs — it needs packaging and per-deployment
   answers. [inference]
5. **Identity beyond bot PATs (higher effort, later).** SSO/SCIM for
   forge operators and MCP scoped tokens is the current ceiling; the
   enterprise tier eventually wants human-actor identity federation into
   the gate decisions (who approved `/go`), which the control-command
   actor fields already record — federating that actor is the seam.
   [inference]

Relationship to existing plans: OPS-04 (evidence retention/residency/
deletion) and FND-04 (versioned capability and credential profiles)
already point this direction; this research adds the BYOK routing pattern
(LiteLLM virtual keys), the CMEK tiering, and the buyer-side checklist
that defines "done" for enterprise readiness.
