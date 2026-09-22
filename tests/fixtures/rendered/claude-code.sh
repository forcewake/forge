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
# MCP (ADR-0022): config ONLY from the CI variable — strict mode
# locks out the repo's own .mcp.json (injection surface).
cat > /tmp/forge-mcp.json <<'FORGE_MCP_EOF'
{
  "mcpServers": {}
}
FORGE_MCP_EOF
for attempt in 1 2 3; do
  npm install -g --no-fund --no-audit @anthropic-ai/claude-code@2.1.276 && break
  echo "npm install of claude-code failed (attempt $attempt), retrying..."
  sleep $((attempt * 5))
done
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
claude --version
claude -p 'Implement the approved task in .forge/brief.md. Read it first, then follow it exactly.' --model glm-5.3-flash \
  --allowedTools 'Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git -C * diff:*),Bash(ls:*),Bash(cat:*),Bash(grep:*),Bash(head:*),Bash(tail:*),Bash(wc:*),Bash(which:*),Bash(awk:*),Bash(sed:*),Bash(sort:*),Bash(uniq:*),Bash(cut:*),Bash(tr:*),Bash(find:*),Bash(diff:*),Bash(basename:*),Bash(dirname:*),Bash(realpath:*),Bash(python3:*),Bash(python:*),Bash(.venv/bin/python:*),Bash(./.venv/bin/python:*),Bash(.venv/bin/pytest:*),Bash(.venv/bin/ruff:*),Bash(.venv/bin/mypy:*),Bash(./.venv/bin/pytest:*),Bash(./.venv/bin/ruff:*),Bash(./.venv/bin/mypy:*),Bash(pip install:*),Bash(pip list),Bash(pip show:*),Bash(pip3 install:*),Bash(pytest:*),Bash(ruff:*),Bash(mypy:*),Bash(uv:*),Bash(make:*),Bash(set:*)' \
  --disallowedTools "Bash(git commit:*)" "Bash(git push:*)" \
  --permission-prompts none \
  --permission-mode bypassPermissions \
  --max-turns 200 \
  --mcp-config /tmp/forge-mcp.json --strict-mcp-config \
  --setting-sources '' --output-format stream-json --verbose 2>&1 | tee -a .forge/events.jsonl | $FORGE_FILTER_PIPE