for attempt in 1 2 3; do
  npm install -g --no-fund --no-audit opencode-ai@2.0.10 && break
  echo "npm install of opencode failed (attempt $attempt), retrying..."
  sleep $((attempt * 5))
done
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
opencode --version
export OPENCODE_CONFIG_CONTENT='{"permission": {"bash": {"git commit *": "deny", "git push *": "deny", "*": "allow"}, "external_directory": "allow", "doom_loop": "allow"}}'
export OPENCODE_MODEL_ID=glm-5.3-flash
export OPENCODE_SERVE_CWD="$PWD"
export OPENCODE_PERMISSION_RESPONSE="${OPENCODE_PERMISSION_RESPONSE:-once}"
python -m forge.lane_driver --driver opencode