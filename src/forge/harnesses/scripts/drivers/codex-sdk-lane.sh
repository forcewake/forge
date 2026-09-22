@@NPM_PIN@@
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
codex --version
# OPTIONAL provider-native credential: the ChatGPT-login auth
# blob (OAuth tokens cannot ride an API-key env). Guarded: an
# unauthenticated lane still runs and reports its own failure.
@@CREDENTIAL@@
@@CODEX_MODEL_EXPORT@@export CODEX_CWD="$PWD"
python -m forge.lane_driver --driver codex
