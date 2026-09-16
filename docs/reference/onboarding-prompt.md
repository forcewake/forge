# Onboarding prompt

Paste this to your coding agent (Claude Code, opencode, Grok Build, Codex,
...) in a fresh clone of the repository. It is safe to run: every step is
local, and the verification gate is `forge doctor`.

---

You are setting up the **forge** repository for development. Work from the
repository root; `AGENTS.md` is your authoritative guide — read it fully
before doing anything. Then:

1. **Environment.** Ensure Python 3.13+ and [uv](https://docs.astral.sh/uv/)
   are available. Run `uv sync` to install dependencies.
2. **Verify the unit suite.** `set -o pipefail && .venv/bin/python -m pytest -q`
   must pass with zero failures (xfails are expected and pinned; do not
   "fix" them). If anything fails, stop and report — do not patch tests to
   make them pass.
3. **Lint.** `.venv/bin/ruff format . && .venv/bin/ruff check src tests`
   must be clean.
4. **Optional — integration lab.** Only if the user asked for the full lab
   (podman + a reachable GitLab CE instance), follow
   `.claude/skills/forge-lab/SKILL.md`. Secrets (a GitLab token and a model
   API key) must come from the user — never invent, guess, or commit them.
   With the lab up, run the harness smoke as described in the skill.
5. **Report.** Finish with: versions installed, test counts, lint status,
   and the output of `uv run python -m forge.doctor` (exit code 0 expected
   for steps 1–3; lab checks only if step 4 was performed). List anything
   you could not complete and why.

Rules: never commit unless asked; never touch `.env` contents in ways that
persist secrets outside the machine; the bot never merges, and you never
push to protected branches of target projects.
