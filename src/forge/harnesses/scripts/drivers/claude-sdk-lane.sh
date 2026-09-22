@@NPM_PIN@@
claude --version
# The bootstrap installs forge WITHOUT extras; the claude lane
# needs the interactive extra's SDK (LIVE-found on the Actions
# runner: sdk_missing with a green bootstrap).
pip install --quiet 'claude-agent-sdk>=0.2.118'
# GitLab docker executors run as root; claude refuses the bypass
# posture for root unless told it is sandboxed (ADR-0002).
export IS_SANDBOX="${IS_SANDBOX:-1}"
# NXT-10 outbound leg: the steering attach dials the control
# plane when the dispatch carried the pair — the work-scoped
# lane token rides the job env (never a control-plane secret);
# exported empty when unset so the lane honestly stays local.
export FORGE_LANE_CONTROL_URL="${FORGE_LANE_CONTROL_URL:-}"
export FORGE_LANE_CONTROL_TOKEN="${FORGE_LANE_CONTROL_TOKEN:-}"
python -m forge.lane_driver --driver claude
