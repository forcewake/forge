# forge — system description

This document describes forge in Simplified Technical English (ASD-STE100).
It gives one fact per sentence, with the file in this repository that
shows the fact.

## 1. Purpose

forge is a code factory for GitHub, GitLab, and Azure DevOps. You write an
issue. forge writes a plan. You approve the plan. forge sends the plan to a
coding agent. The agent works in a separate build job with no write access.
The job returns a file with code changes. forge checks the file, makes one
commit, and opens a draft pull request. A person merges it. forge never
merges.
Example: `.github/workflows/forge-harness.yml`.

## 2. The change loop (feedback-loop orchestration)

You write `/implement` in an issue. forge writes a plan and freezes it as
a record with a checksum. You write `/go`. forge sends the job. The job
returns the file. forge checks the file, makes the commit, and opens the
pull request. Then the CI system of your project runs on that commit.
forge reads the CI result.

If a CI test fails, forge sends the same branch back to the agent, with
the error text in the job instruction file. The agent corrects its own
work. If the process dies, forge does not start again from zero. Each
step records its result before the next step runs. After a crash, the
next worker reads the recorded results and continues. If a push reached
the server but the record did not, forge asks the server, finds the
commit by its marker, and accepts it.
Examples: `src/forge/runs/checkpoints.py`, `src/forge/durable/intents.py`,
`src/forge/runs/revival.py`.

## 3. Agent checks (local verification)

The job installs the tools from the lock file of your project. The agent
runs the same test, lint, and type commands that your CI runs. The
instruction file says: do not finish while a check fails.
Example: the `uv sync --frozen` step in
`.github/workflows/forge-harness.yml`.

## 4. Checks before a write (validation)

One module owns all writes to the server. Before a write, it checks:
paths in deny lists, path profiles, the allowed path scope of the run,
the file count, and the file size. Then it applies the patch in memory
and compares the result with a recorded checksum. If a check fails, forge
makes no write call.
Examples: `src/forge/runs/publisher.py`,
`tests/test_publication_boundary.py` (8 bad cases × 3 providers; each
case asserts zero write calls), `docs/adr/0026-publication-boundary.md`.

## 5. Tests for the factory itself (acceptance)

Two failure suites check forge with failures on purpose. The first suite
stops workers inside one Python process. The second suite starts real
worker processes and kills them with SIGKILL. Fake servers give wrong or
missing answers on demand. The suites show: a killed worker loses no
work; a lost server answer is resolved by a question to the server, not
by a second write. A third suite runs the same checks on all three
providers.
Examples: `tests/test_failure_injection.py`,
`tests/test_failure_injection_os.py`, `tests/test_conformance.py`.

## 6. Records you can read (observability)

Each run records: a status, a reason for each stop, tokens and calls with
a unique receipt id, and a comment on the issue with links to the branch,
the job, and the pull request. Metrics export a stage counter per run.
Examples: `src/forge/runs/verification.py`, `src/forge/durable/budgets.py`.

## 7. Permissions and limits (security and guardrails)

The job has no write credentials. Push is off in the job. Only one module
can write, and it checks the policy first. The MCP interface gives each
token a set of scopes and a list of permitted repositories. A read token
can not write. Write commands are denied by a mechanical rule, not by a
request to the agent. Human approval is bound to the plan checksum. After
approval, a change to the issue text does not change the approved plan.
Examples: `src/forge/mcp_server/tools.py`, `src/forge/runs/spec.py`,
`src/forge/config.py` (`FORGE_WRITE_PROFILES`).

## 8. Knowledge

Project rules live in `AGENTS.md` in your repository. Job instructions
live in one brief file per run. Design records live in `docs/adr/`.
There is no hidden memory between runs.

## 9. Cost limits (resource governance)

Each run has a budget: a maximum call count, a maximum token count, and a
maximum wall time. The numbers are part of the approved plan record.
Before each model call, forge checks the budget. The job also has a wall
clock. forge records usage with a receipt id. The same usage data twice
counts once. An unknown usage count stays unknown; forge does not record
it as zero.
Examples: `src/forge/durable/budgets.py`,
`src/forge/runs/candidate.py` (`usage_receipt_id`).

## 10. Evidence

The test suite has 2601 tests. It has two failure-injection suites, one
differential suite that compares the patch engine with `git apply`, and
one conformance suite that runs the same checks on three providers.

## 11. How to try

Repository: https://github.com/forcewake/forge
Install: https://github.com/forcewake/forge/blob/main/docs/getting-started/github.md
Commands: `/implement` (make a plan), `/go` (approve), `/retry`
(continue a stopped run), `/status` (show the run record).
