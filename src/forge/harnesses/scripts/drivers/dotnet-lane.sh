# Vendor timeout budgets (R5): long tool calls and API turns
# must not die at the client default mid-run.
export API_TIMEOUT_MS=3000000 BASH_DEFAULT_TIMEOUT_MS=300000 BASH_MAX_TIMEOUT_MS=600000
# Ephemeral isolated config (same posture as the claude-code arm): a
# fresh config dir per lane guarantees a cold start; lane auth rides on
# env vars, so nothing stored is lost.
export CLAUDE_CONFIG_DIR="$(mktemp -d /tmp/claude-lane-config.XXXXXX)"
# Repair re-dispatches cap the thinking budget (see the claude-code arm).
if [ -n "$FORGE_REPAIR_CONTEXT" ]; then
  export MAX_THINKING_TOKENS="${FORGE_MAX_THINKING_TOKENS:-8000}"
fi
@@MCP_PROVISION@@
@@NPM_PIN@@
# R15: the resolved CLI version lands in the job log — pin
# drift is visible, never silent.
claude --version
# R28-21: the reproducible .NET lane preamble. The runner must carry the
# pinned .NET SDK (the digest-pinned mcr.microsoft.com/dotnet/sdk image);
# global.json selects the EXACT version and `dotnet --version` fails
# loudly on a mismatch — a drifted environment dies HERE, before the
# first paid call.
if ! command -v dotnet >/dev/null 2>&1; then
  echo "dotnet-lane: the .NET SDK is not on PATH — run on the digest-pinned mcr.microsoft.com/dotnet/sdk image (R28-21)" >&2
  exit 1
fi
if [ ! -f global.json ]; then
  echo "dotnet-lane: global.json is required (pin the .NET SDK version — R28-21 reproducible recipe)" >&2
  exit 1
fi
if ! find . -name "*.lock.json" -not -path "*/bin/*" -not -path "*/obj/*" | grep -q .; then
  echo "dotnet-lane: no nuget.lock.json found — commit it (RestorePackagesWithLockFile, R28-21)" >&2
  exit 1
fi
dotnet --version
mkdir -p .forge
# The agent: the claude-code unattended contract verbatim (same grants,
# same mechanical commit/push deny, same strict MCP). A failed agent
# classifies the lane failed but never aborts the verification tail —
# the audit trail stays (the tail's outcome rides .forge/verify.json).
set +e
claude -p @@QUOTED_PROMPT@@@@MODEL_FLAG@@ \
  --allowedTools @@ALLOWED_TOOLS@@ \
  --disallowedTools "Bash(git commit:*)" "Bash(git push:*)" \
  --permission-prompts none \
  --permission-mode bypassPermissions \
  --max-turns 200 \
  --mcp-config /tmp/forge-mcp.json --strict-mcp-config \
  --setting-sources '' --output-format stream-json --verbose 2>&1 | tee -a @@EVENTS@@ | $FORGE_FILTER_PIPE
_agent_rc=$?
# R28-21 steps 3-5 + NEXT-16: the reproducible verification tail — locked
# restore, locked build, TRX tests with a UNIQUE LogFilePrefix per report
# (multi-project/multi-target runs each write their own forge_*.trx;
# nothing is overwritten, and the aggregator below counts EVERY report).
# Each leg's exit code is RECORDED beside the aggregated TRX counters
# (environment failures separated from test failures); only the AGENT's
# failure classifies the lane itself.
FORGE_RESTORE_EXIT=0 FORGE_BUILD_EXIT=0 FORGE_TEST_EXIT=0
dotnet restore --locked-mode || FORGE_RESTORE_EXIT=$?
dotnet build --no-restore --locked-mode || FORGE_BUILD_EXIT=$?
dotnet test --no-build --logger "trx;LogFilePrefix=${FORGE_DOTNET_TRX_PREFIX:-forge_}" --results-directory .forge/testresults || FORGE_TEST_EXIT=$?
export FORGE_RESTORE_EXIT FORGE_BUILD_EXIT FORGE_TEST_EXIT
python3 - <<'PYEOF'
import hashlib, json, os, xml.etree.ElementTree as ET

trx_dir = ".forge/testresults"
prefix = os.environ.get("FORGE_DOTNET_TRX_PREFIX", "forge_")
legacy_name = os.environ.get("FORGE_DOTNET_TRX", "forge.trx")
max_projects = 25
reports = []
parse_failures = []
legacy_reports = []
if os.path.isdir(trx_dir):
    for root, _dirs, names in sorted(os.walk(trx_dir)):
        for name in sorted(names):
            path = os.path.join(root, name)
            if name == legacy_name:
                # A leftover fixed-name report is stale by construction (it
                # may predate this attempt) — visible, never counted.
                legacy_reports.append(path)
                continue
            if not (name.startswith(prefix) and name.endswith(".trx")):
                continue
            try:
                raw = open(path, "rb").read()
                node = ET.fromstring(raw)
            except (OSError, ET.ParseError) as exc:
                parse_failures.append({"file": path, "error": str(exc)[:200]})
                continue
            ns = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"
            outcome = ""
            counters = {}
            summary = node.find(f"{ns}ResultSummary")
            if summary is not None:
                outcome = summary.get("outcome", "")
                counted = summary.find(f"{ns}Counters")
                if counted is not None:
                    counters = {
                        key: int(value)
                        for key, value in counted.attrib.items()
                        if value is not None and value.isdigit()
                    }
            reports.append({
                "file": path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "outcome": outcome,
                "counters": counters,
            })
verify = {
    "kind": "dotnet-trx/2",
    "test_projects": len(reports),
    "total_passed": sum(r["counters"].get("passed", 0) for r in reports),
    "total_failed": sum(r["counters"].get("failed", 0) for r in reports),
    "projects": [
        {
            "file": r["file"],
            "outcome": r["outcome"],
            "passed": r["counters"].get("passed", 0),
            "failed": r["counters"].get("failed", 0),
            "sha256": r["sha256"],
        }
        for r in reports[:max_projects]
    ],
    "projects_dropped": max(0, len(reports) - max_projects),
    "parse_failures": parse_failures[:10],
    "legacy_reports": legacy_reports[:10],
    "reports_absent": not reports,
    "restore_exit": int(os.environ.get("FORGE_RESTORE_EXIT", "0") or 0),
    "build_exit": int(os.environ.get("FORGE_BUILD_EXIT", "0") or 0),
    "test_exit": int(os.environ.get("FORGE_TEST_EXIT", "0") or 0),
}
with open(".forge/verify.json", "w") as fh:
    json.dump(verify, fh, indent=2, sort_keys=True)
PYEOF
exit "$_agent_rc"
