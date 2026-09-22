@@NPM_PIN@@
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
copilot --version
# Auth rides the ambient env (COPILOT_GITHUB_TOKEN — a fine-grained
# PAT with the "Copilot Requests" permission; classic ghp_ tokens
# fail silently). No file landing: the ACP child reads the env
# itself and reports its own auth failure.
export COPILOT_CWD="$PWD"
python -m forge.lane_driver --driver copilot
