@@NPM_PIN@@
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
copilot --version
@@MCP_PROVISION@@
copilot -p @@QUOTED_PROMPT@@@@MODEL_FLAG@@ \
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
@@MCP_GRANTS@@  --deny-tool 'shell(git commit)' --deny-tool 'shell(git push)' 2>&1 | tee -a @@EVENTS@@ | $FORGE_FILTER_PIPE
