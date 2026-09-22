@@NPM_PIN@@
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
opencode --version
export OPENCODE_CONFIG_CONTENT=@@OPENCODE_CONFIG@@
@@OPENCODE_MODEL_EXPORTS@@export OPENCODE_SERVE_CWD="$PWD"
export OPENCODE_PERMISSION_RESPONSE="${OPENCODE_PERMISSION_RESPONSE:-once}"
python -m forge.lane_driver --driver opencode
