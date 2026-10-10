---
name: forge-setup
description: Set up the forge dev environment from a fresh clone and verify it with forge doctor. Use when the repo was just cloned, the venv is missing, or tests/lint must be validated before work.
---

# forge-setup: dev environment + verification

1. Check prerequisites: `python3 --version` (need 3.13+) and `uv --version`.
   If missing, install uv via the official installer; use the system Python
   3.13+ (pyenv/homebrew acceptable).
2. `uv sync` from the repository root.
3. Verify: `set -o pipefail && .venv/bin/python -m pytest -q -m "not slow"` — the
   default tier must pass with zero failures (counts change as the
   simplification program deletes code; `docs/reference/testing.md` explains
   the tiers).
4. Lint: `.venv/bin/ruff format . && .venv/bin/ruff check .`, then the
   guardrails: `uv run lint-imports && uv run python scripts/ratchet.py`.
5. Final gate: `uv run python -m forge.doctor`. With a `.env` it must exit 0
   (`--project <id>` adds target-project checks). Without one, doctor prints a
   single FAIL line for the missing settings (for example `GITLAB_URL`) and
   exits 1; that is expected for a pure-dev clone, where what must pass is the
   default tier, lint and the guardrails.

Report: test counts, lint status, doctor output. Do not commit anything.
