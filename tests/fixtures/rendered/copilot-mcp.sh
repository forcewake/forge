for attempt in 1 2 3; do
  npm install -g --no-fund --no-audit @github/copilot@1.0.86 && break
  echo "npm install of copilot failed (attempt $attempt), retrying..."
  sleep $((attempt * 5))
done
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
copilot --version
# MCP (ADR-0022): documented Copilot CLI config location.
mkdir -p ~/.copilot
cat > ~/.copilot/mcp-config.json <<'FORGE_MCP_EOF'
{
  "mcpServers": {
    "context7": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
FORGE_MCP_EOF
copilot -p 'Implement the approved task in .forge/brief.md. Read it first, then follow it exactly.' --model glm-5.3-flash \
  --allow-tool 'read,write' \
  --allow-tool 'shell(git:*)' \
  --allow-tool 'shell(uv run pytest:*)' \
  --allow-tool 'shell(pytest:*)' \
  --allow-tool 'shell(uv run ruff:*)' \
  --allow-tool 'shell(uv run mypy:*)' \
  --allow-tool 'shell(uv:*)' --allow-tool 'shell(make:*)' \
  --allow-tool 'shell(set:*)' --allow-tool 'shell(ruff:*)' \
  --allow-tool 'shell(mypy:*)' \
  --allow-tool 'shell(awk:*)' --allow-tool 'shell(sed:*)' \
  --allow-tool 'shell(sort:*)' --allow-tool 'shell(cut:*)' \
 --allow-tool context7  --deny-tool 'shell(git commit)' --deny-tool 'shell(git push)' 2>&1 | tee -a .forge/events.jsonl | $FORGE_FILTER_PIPE