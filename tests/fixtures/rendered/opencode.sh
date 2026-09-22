for attempt in 1 2 3; do
  npm install -g --no-fund --no-audit opencode-ai@1.18.31 && break
  echo "npm install of opencode failed (attempt $attempt), retrying..."
  sleep $((attempt * 5))
done
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
opencode --version
# The mechanical deny rides in via the documented
# config-injection env (merges over global/project config).
export OPENCODE_CONFIG_CONTENT='{"permission": {"bash": {"git commit *": "deny", "git push *": "deny", "*": "allow"}, "external_directory": "allow", "doom_loop": "allow"}}'
opencode run --auto --format json 'Implement the approved task in .forge/brief.md. Read it first, then follow it exactly.' 2>&1 | tee -a .forge/events.jsonl | $FORGE_FILTER_PIPE