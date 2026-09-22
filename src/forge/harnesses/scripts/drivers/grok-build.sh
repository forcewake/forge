@@NPM_PIN@@
# The platform binary follows the PINNED wrapper: GROK_VER is
# read back from the binary just installed (a pin on the
# wrapper alone would leave the optionalDependency floating).
GROK_VER="$(grok --version | awk '{print $2}')"
npm install -g --no-fund --no-audit "@xai-official/grok-linux-x64@${GROK_VER}" \
  || npm install -g --no-fund --no-audit @xai-official/grok-linux-x64
test -d /usr/local/lib/node_modules/@xai-official/grok-linux-x64
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
grok --version
# R15 minimal lane credentials: the ONLY grok credential is the
# provider-native subscription auth blob (FORGE_GROK_AUTH — the
# full ~/.grok/auth.json contents, the GitLab template
# contract). An API key is a different capability and is never
# requested here (BYOK is per capability/credential pair, not
# interchangeable).
@@CREDENTIAL@@
@@MCP_PROVISION@@
grok --no-auto-update --always-approve --no-alt-screen \
  --trust --max-turns 200 \
  --allow 'Bash(uv run pytest:*)' --allow 'Bash(pytest:*)' \
  --allow 'Bash(uv run ruff:*)' --allow 'Bash(uv run mypy:*)' \
  --allow 'Bash(python3:*)' --allow 'Bash(python:*)' \
  --allow 'Bash(pip install:*)' \
  --allow 'Bash(uv:*)' --allow 'Bash(make:*)' --allow 'Bash(set:*)' \
  --allow 'Bash(ruff:*)' --allow 'Bash(mypy:*)' \
  --allow 'Bash(.venv/bin/python:*)' --allow 'Bash(.venv/bin/ruff:*)' \
  --allow 'Bash(awk:*)' --allow 'Bash(sed:*)' --allow 'Bash(sort:*)' \
  --allow 'Bash(cut:*)' --allow 'Bash(tr:*)' --allow 'Bash(find:*)' \
  --deny 'Bash(git commit:*)' --deny 'Bash(git push:*)' \
  --output-format streaming-json \
  --debug-file @@DEBUG_LOG@@ \
  -p @@QUOTED_PROMPT@@ 2>&1 | tee -a @@EVENTS@@ | $FORGE_FILTER_PIPE
