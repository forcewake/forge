@@NPM_PIN@@
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
opencode --version
# The mechanical deny rides in via the documented
# config-injection env (merges over global/project config).
export OPENCODE_CONFIG_CONTENT=@@OPENCODE_CONFIG@@
opencode run --auto --format json @@QUOTED_PROMPT@@ 2>&1 | tee -a @@EVENTS@@ | $FORGE_FILTER_PIPE
