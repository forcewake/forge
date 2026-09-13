# forge

**forge** is an open-source agentic software factory sidecar for self-hosted
[GitLab Community Edition](https://about.gitlab.com/install/ce-installations/).

forge brings AI-assisted code review, pipeline debugging, security triage, and
a chat assistant to GitLab CE — without requiring GitLab Premium/Ultimate or
the Duo Agent Platform — and is growing into a software factory: an authorized
issue becomes a branch with code, a green pipeline, and a Draft merge request
ready for human review. You bring the model (any provider via
[LiteLLM](https://docs.litellm.ai/)); forge runs on your own infrastructure.

forge is based on the open-source project
[Codeward](https://github.com/Relrin/codeward). See [UPSTREAM.md](UPSTREAM.md)
for provenance and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for
license notices.

## The iron principle: the bot never merges

forge proposes changes, drives the pipeline, and assembles evidence bound to a
specific commit. It never merges, never pushes to a protected target branch,
and never changes permissions. Review and merge are always performed by a
human in GitLab. This is enforced by the executor's capabilities and GitLab
permissions — not by prompt instructions. See
[ADR-0003](docs/adr/0003-no-merge-is-enforceable.md).

## Status

forge is at the **M2 baseline**: the M0 reactive bot core (imported from
Codeward, pinned at commit `fd63ec8`) plus a live-accepted durable run loop
whose planner/implementer/reviewer are now real LLM agents behind the same
seams — still pre-production, honestly listed below.

Reactive bot core (as imported and rebranded):

- **Code review** — inline comments, severity ratings, incremental reviews on
  push with automatic resolution of addressed threads
- **Pipeline debugging** — root-cause analysis of CI failures
- **Security triage** — triage of SAST/DAST/dependency/secret scan findings
- **@mention chat** — ask the bot about merge requests, code, and issues
- **MCP** — an MCP server mounted at `/mcp`, plus support for calling tools
  from external MCP servers
- **Model routing** — route to any provider through the LiteLLM proxy

**Implemented: the durable run loop.** An authorized issue runs
`@forge /implement` → plan → human `/go` gate → LLM implementation → atomic
commit via the Commits API → Draft MR → CI watched by a reconciler → readonly
LLM review → `ready_for_human` with evidence bound to the exact candidate SHA
([ADR-0004](docs/adr/0004-controller-owns-lifecycle-implementer-proposes.md),
[ADR-0007](docs/adr/0007-draft-mr-before-required-ci.md)). The loop is
durable (crash-safe transitions, journaled external writes, unknown-outcome
blocking per [ADR-0005](docs/adr/0005-durable-execution-and-unknown-outcome.md)),
enforces the ADR-0008 quality contract — pipeline success plus every required
job succeeded, failures classified so only *code* failures trigger the
bounded repair loop (`FORGE_MAX_COMMIT_CYCLES`) — and records every model
call in the usage ledger ([ADR-0013](docs/adr/0013-budgets-and-usage-ledger-in-core.md)).
The factory agents call the model through a thin LiteLLM HTTP client
([ADR-0014](docs/adr/0014-llm-http-client-over-agno.md)); Agno stays on the
reactive path only.

**Still pre-production.** Not yet done: per-project quality-contract
onboarding/`doctor` verification, budget enforcement beyond the commit-cycle
cap (reserve/reconcile), redaction at every agent boundary, drift policies
beyond block, and production hardening of the live-accepted slice. The
[architecture decision records](docs/adr/0000-record-architecture-decisions.md)
record what is decided; the gap between ADRs and running code is where work
remains.

The upstream multi-agent YAML flows (including the old `/implement`,
issue → MR) are **not** part of forge's working functionality: the upstream
flow engine has known defects and has been replaced by the typed, durable
controller above ([ADR-0004](docs/adr/0004-controller-owns-lifecycle-implementer-proposes.md)).

## Quick start

forge is designed to run as a local Python service while its infrastructure
dependencies (Redis, LiteLLM, and the Postgres that backs LiteLLM) run in
Docker.

Prerequisites:

- Python (supported version pinned in `pyproject.toml`;
  [uv](https://docs.astral.sh/uv/) provisions the interpreter)
- [uv](https://docs.astral.sh/uv/)
- Docker + Docker Compose (Redis, LiteLLM, LiteLLM's Postgres)
- PostgreSQL for forge's own data — run a **separate** instance from
  LiteLLM's database (SQLite works for a quick trial)
- A GitLab CE instance and a bot Personal Access Token (`api` scope)

```bash
# 1. Clone
git clone <this repository> && cd forge

# 2. Configure
cp .env.example .env
cp litellm-config.example.yaml litellm-config.yaml
# Edit .env -- set GITLAB_URL, GITLAB_TOKEN, GITLAB_WEBHOOK_SECRET,
#              at least one model API key (e.g. OPENROUTER_API_KEY),
#              LITELLM_URL=http://localhost:4000,
#              REDIS_URL=redis://localhost:6379/0,
#              DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/forge

# 3. Start infrastructure (Redis + LiteLLM + its Postgres)
docker compose up -d

# 4. Install dependencies & run migrations
uv sync
make migrate

# 5. Start the service (and, in another terminal, the worker)
make run        # http://localhost:8420 with hot-reload
make worker     # Redis-backed worker

# 6. Register the GitLab webhook for a project
make setup

# 7. Verify
curl http://localhost:8420/health
```

### Registering the GitLab webhook

GitLab must be able to reach the forge service over HTTP.

| Scenario | Solution |
|----------|----------|
| Same machine / LAN | `http://192.168.x.x:8420/webhook` |
| Remote / cloud | Use a tunnel, e.g. `ngrok http 8420` |
| Everything in Docker | Same Docker network |

### Running everything in Docker (optional)

The repository ships a full Docker profile that additionally starts the forge
app and worker containers:

```bash
docker compose --profile full up --build -d
```

This is useful for production-style deployments. For day-to-day development,
the local `uv`/`make run` workflow is faster and gives you hot-reload.

## Configuration

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITLAB_URL` | Yes | -- | GitLab instance URL |
| `GITLAB_TOKEN` | Yes | -- | Bot Personal Access Token (`api` scope) |
| `GITLAB_WEBHOOK_SECRET` | Yes | -- | Webhook validation secret |
| `FORGE_BOT_USERNAME` | No | `forge-bot` | Bot's GitLab username |
| `FORGE_MENTION_PATTERN` | No | `@forge` | Mention trigger pattern |
| `FORGE_AGENTS_DIR` | No | `agents` | Directory with agent YAML definitions |
| `FORGE_MCP_KEY` | No | -- | Bearer token required to call the `/mcp` mount |
| `DATABASE_URL` | No | SQLite default | forge's own database (Postgres recommended) |
| `LITELLM_URL` | No | `http://litellm:4000` | LiteLLM proxy URL; use `http://localhost:4000` when running locally |
| `REDIS_URL` | No | -- | Redis URL; required for the worker |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `AGNO_TELEMETRY` | No | `false` | Agno framework telemetry (disabled by default) |

### Service configuration (`forge.yml`)

Service-level settings (model tiers, defaults, rate limits, token budgets,
redaction, labels) live in `forge.yml`. See `forge.example.yml` for the full
schema.

### Project configuration (`.forge.yml`)

Per-project overrides are placed in the repository root of each connected
project:

```yaml
forge:
  review_rules:
    - "Follow the project style guide"
  skip_paths:
    - "*.lock"
    - "vendor/**"
  disabled_agents:
    - security-triage
```

Project configuration can only tighten limits and pick from allowed options;
it can never grant additional authority. See
[ADR-0011](docs/adr/0011-config-never-delegates-security-downward.md).

## Usage

### @mention

```
@forge What does this function do?
@forge Can you explain the changes in this MR?
```

### Slash commands

```
@forge /review     - Request a code review
@forge /debug      - Diagnose a pipeline failure
@forge /security   - Run security triage
@forge /explain    - Explain the current diff
@forge /help       - Show available commands
```

Code reviews and pipeline debugging also run automatically when merge requests
are opened/updated or pipelines fail.

## Development

| Target | Purpose |
|--------|---------|
| `make run` | Start the forge web app with hot-reload |
| `make worker` | Start the Redis-backed worker |
| `make migrate` | Apply database migrations |
| `make setup` | Register the GitLab webhook for a project |
| `make test` | Run the test suite |
| `make lint` | Lint with ruff |
| `make fmt` | Format with ruff |
| `make docker-up` / `make docker-down` | Start/stop the infrastructure stack |

## Documentation

- [Architecture decision records](docs/adr/0000-record-architecture-decisions.md)
  — the design of the factory controller: write backend and ChangeSet
  contract, execution profiles, no-merge enforcement, the durable lifecycle,
  reconciliation, snapshot isolation, Draft MR and CI ordering, quality
  contracts, human gates, CE-compatible labels, layered configuration,
  context/redaction boundaries, budgets and the usage ledger
- [Threat model](docs/security/threat-model.md) — trust boundaries, key risks,
  and their mitigations
- [Operations runbooks](docs/operations/README.md) — planned
- [UPSTREAM.md](UPSTREAM.md) — provenance and upstream tracking policy

## License

forge is published under the BSD 3-Clause license. It is based on
[Codeward](https://github.com/Relrin/codeward) and contains substantial
modifications. See [LICENSE](LICENSE),
[UPSTREAM.md](UPSTREAM.md), and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
