# Vendor timeout budgets (R5): long tool calls and API turns
# must not die at the client default mid-run.
export API_TIMEOUT_MS=3000000 BASH_DEFAULT_TIMEOUT_MS=300000 BASH_MAX_TIMEOUT_MS=600000
# Ephemeral isolated config: claude's auto-memory is pointless in
# a proposal-only lane (the brief IS this run's memory) and
# HAZARDOUS on reused runners — the MEMORY.md index auto-loads into
# context, so a stale index from ANOTHER run on the same VM would
# poison this run (LIVE-found: the agent wrote memory/MEMORY.md).
# A fresh config dir per lane guarantees a cold start; lane auth
# rides on env vars, so nothing stored is lost.
export CLAUDE_CONFIG_DIR="$(mktemp -d /tmp/claude-lane-config.XXXXXX)"
# Repair re-dispatches are GUIDED fixes (bounded failure context
# rides in the brief) — deep per-turn thinking is the lane's
# dominant wall-time cost (LIVE: 17% of turns >40s ≈ half the
# run), so repair cycles cap the thinking budget. First cycles
# think freely.
if [ -n "$FORGE_REPAIR_CONTEXT" ]; then
  export MAX_THINKING_TOKENS="${FORGE_MAX_THINKING_TOKENS:-8000}"
fi
@@MCP_PROVISION@@
@@NPM_PIN@@
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
claude --version
claude -p @@QUOTED_PROMPT@@@@MODEL_FLAG@@ \
  --allowedTools @@ALLOWED_TOOLS@@ \
  --disallowedTools "Bash(git commit:*)" "Bash(git push:*)" \
  --permission-prompts none \
  --permission-mode bypassPermissions \
  --max-turns 200 \
  --mcp-config /tmp/forge-mcp.json --strict-mcp-config \
  --setting-sources '' --output-format stream-json --verbose 2>&1 | tee -a @@EVENTS@@ | $FORGE_FILTER_PIPE
