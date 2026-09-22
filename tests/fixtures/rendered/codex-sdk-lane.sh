for attempt in 1 2 3; do
  npm install -g --no-fund --no-audit @openai/codex@0.153.4 && break
  echo "npm install of codex failed (attempt $attempt), retrying..."
  sleep $((attempt * 5))
done
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
codex --version
# OPTIONAL provider-native credential: the ChatGPT-login auth
# blob (OAuth tokens cannot ride an API-key env). Guarded: an
# unauthenticated lane still runs and reports its own failure.
mkdir -p ~/.codex
if [ -n "$FORGE_CODEX_AUTH" ]; then
  printf "%s" "$FORGE_CODEX_AUTH" > ~/.codex/auth.json
  chmod 600 ~/.codex/auth.json
fi
export CODEX_MODEL=glm-5.3-flash
export CODEX_CWD="$PWD"
python -m forge.lane_driver --driver codex