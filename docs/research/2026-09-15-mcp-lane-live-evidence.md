# Live evidence: coding-agent harnesses invoke external MCP servers in CI lanes

Date: 2026-09-15
Lab: forcewake/forge-lab (project 68) on GitLab CE 18.9.1, shared docker-executor runner `unraid`.
Method: manual CI job `forge-mcp-smoke` (lab-local; mirrors `ci/templates/claude-code.gitlab-ci.yml` env
posture — `FORGE_HARNESS_MCP` CI variable → `/tmp/forge-mcp.json` → `--mcp-config --strict-mcp-config`,
`IS_SANDBOX=1`, `ANTHROPIC_MODEL=glm-5.3-flash[1m]`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`,
pipefail, `--output-format stream-json`, `HTTPS_PROXY=$FORGE_HARNESS_HTTPS_PROXY` with
`NO_PROXY=$CI_SERVER_HOST,127.0.0.1,localhost`). Prompt forced tool use on both servers.

## claude-code — VERIFIED

- Job: https://gitlab.forcewake.duckdns.org/forcewake/forge-lab/-/jobs/651 (job id 651, pipeline 317, commit 338018f)
- Duration: 61 s wall (08:50:58Z → 08:51:59Z); claude run itself 51.5 s (`duration_ms`), API time 48.0 s
- claude --version: 2.1.272 (Claude Code); model `glm-5.3-flash[1m]` via `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`
- Verdict: **VERIFIED** — 4 MCP `tool_use` events (2 per server), 4 `tool_result` payloads with real data,
  init event reports both servers `connected`.
- Exact tools observed (unique `tool_use` names):
  - `mcp__context7__resolve-library-id`, `mcp__context7__query-docs`
  - `mcp__learn__microsoft_docs_search`, `mcp__learn__microsoft_code_sample_search`
- Gotchas found:
  - The live Context7 server advertises `query-docs` — NOT `get-library-docs` as historically
    documented. The `mcp__context7__*` allow glob absorbed the rename without a job change.
  - The Learn server advertises `microsoft_docs_search`, `microsoft_docs_fetch`,
    `microsoft_code_sample_search` (the brief's guessed name `microsoft_learn_search_docs` does not exist).
  - Pre-existing lab `smoke` job fails a seeded test collection, which skips manual jobs in later
    stages; the smoke job carries `needs: []` to stay playable. Unrelated to the driver.

## Trace excerpt (trimmed, job 651)

```text
08:51:04  $ for attempt in 1 2 3; do …          # npm install @anthropic-ai/claude-code
08:51:04  added 2 packages in 3s
08:51:04  2.1.272 (Claude Code)                 # claude --version
08:51:04  MCP servers configured: context7, learn
08:51:05  INIT {"type":"system","subtype":"init","model":"glm-5.3-flash[1m]",
           "mcp_servers":[{"name":"context7","status":"connected"},{"name":"learn","status":"connected"}],
           "tools":["mcp__context7__query-docs","mcp__context7__resolve-library-id",
                    "mcp__learn__microsoft_code_sample_search","mcp__learn__microsoft_docs_fetch",
                    "mcp__learn__microsoft_docs_search", …]}
08:51:10  tool_use name=mcp__context7__resolve-library-id
           input={"libraryName":"litellm","query":"litellm Python library for calling LLM providers with a unified OpenAI-format API"}
08:51:11  tool_use name=mcp__learn__microsoft_docs_search
           input={"query":"Azure Blob Storage upload blob python"}
08:51:13  tool_result (1959 B) "Available Libraries:\n\n- Title: Litellm\n- Context7-compatible library ID: /berriai/litellm …"
08:51:17  tool_result "{\"results\":[{\"description\":\"…uploads a blob to an Azure Storage container…\",\"codeSnippet\":\"def upload_blob_acc…\", …]}"   # learn
08:51:28  tool_use name=mcp__context7__query-docs
           input={"libraryId":"/berriai/litellm","query":"How to call a model with the completion() function and set the API key and provider environment variables"}
08:51:29  tool_use name=mcp__learn__microsoft_code_sample_search
           input={"language":"python","query":"upload blob to Azure Blob Storage container with BlobServiceClient"}
08:51:34  tool_result (2820 B) "### Import completion and set provider API keys\n\nSource: https://github.com/berriai/litellm/blob/main/cookbook/liteLLM_Streaming_Demo.ipynb …"
08:51:58  RESULT "…CONTEXT7_TOOLS_USED=mcp__context7__resolve-library-id,mcp__context7__query-docs
                  LEARN_TOOLS_USED=mcp__learn__microsoft_docs_search,mcp__learn__microsoft_code_sample_search"
08:51:58  === FORGE-MCP-SMOKE EVIDENCE SUMMARY ===
08:51:58  MCP servers in init event: [{"name":"context7","status":"connected"},{"name":"learn","status":"connected"}]
08:51:58  MCP tools invoked (unique): mcp__context7__resolve-library-id, mcp__learn__microsoft_docs_search, mcp__context7__query-docs, mcp__learn__microsoft_code_sample_search
08:51:58  FINAL: CONTEXT7_TOOLS_USED=mcp__context7__resolve-library-id,mcp__context7__query-docs
08:51:58  FINAL: LEARN_TOOLS_USED=mcp__learn__microsoft_docs_search,mcp__learn__microsoft_code_sample_search
```

(Timestamps UTC from the job trace; long lines truncated for readability.)

## grok (xAI Grok Build CLI) — FAILED (native MCP), servers reached via model fallback

- Job: https://gitlab.forcewake.duckdns.org/forcewake/forge-lab/-/jobs/654 (job id 654, pipeline for efa89c8)
- Duration: 194 s wall (09:07:58Z → 09:11:12Z)
- grok 1.0.30 (04b7ffed98c6), model grok-4.6-build; config per shipped grok template
  (`FORGE_GROK_AUTH` → `~/.grok/auth.json`, `FORGE_HARNESS_MCP` → `~/.grok/settings.json` with
  claude-shaped `{"type":"http","url":...}` entries)
- Verdict: **FAILED** for harness-native MCP invocation. Grok's session did not connect the
  servers from `settings.json` (`mcpServers`), and its `use_tool` bridge rejects
  `server__tool` naming:
  ```text
  {"type":"tool_call","toolName":"use_tool","rawInput":{"tool_name":"context7__resolve-library-id",...}}
  {"type":"tool_call_update","status":"failed",...text:"Tool `context7__resolve-library-id` failed via `use_tool`:
    ... {"error":"tool_execution_failed","message":"Tool not found: context7__resolve-library-id"}"}
  ```
  (6/6 native calls failed this way: context7__resolve-library-id, context7__query-docs,
  learn__microsoft_docs_search — "Tool not found".)
- Grok's own final answer (assembled from `{"type":"text","data":...}` stream chunks) confirms:
  "MCP servers are listed in the environment but not connected… The servers are listed in
  settings but not connected… `use_tool` isn't connected in this session, so I'll invoke the
  MCP tools over HTTP". The model then hand-rolled MCP JSON-RPC clients (tools/list,
  tools/call) in python via `run_terminal_command` and DID get real results from both servers
  (context7 `/berriai/litellm` library list; azure-storage-blob code samples), finally printing
  `CONTEXT7_TOOLS_USED=resolve-library-id,query-docs` / `LEARN_TOOLS_USED=microsoft_docs_search`.
- Bottom line: remote MCP servers are reachable from the lane, but grok 1.0.30's native MCP
  integration (settings.json http entries) is not verified working — the invocation was
  model-authored raw HTTP, not the harness's MCP client. Job exit was still 0 (driver ran fine).

## opencode — VERIFIED

- Job: https://gitlab.forcewake.duckdns.org/forcewake/forge-lab/-/jobs/655 (job id 655, pipeline for efa89c8)
- Duration: 39 s wall (09:07:59Z → 09:08:37Z)
- opencode 1.18.31, model glm-5.3-flash via z.ai OpenAI-compatible endpoint
  (`https://api.z.ai/api/coding/paas/v4`, key from `{env:ZAI_API_KEY}`); MCP config written into
  `~/.config/opencode/opencode.json` `mcp` key as `{"type":"remote","url":...,"enabled":true}`
- Verdict: **VERIFIED** — native MCP tool calls streamed straight into the job trace:
  ```text
  ⚙ learn_microsoft_docs_search {"query":"Azure Blob Storage upload file Python"}
  ⚙ context7_resolve-library-id {"libraryName":"litellm","query":"LiteLLM Python SDK usage for calling LLM providers"}
  ⚙ context7_query-docs {"libraryId":"/berriai/litellm","query":"How to call an LLM with the litellm.completion function in Python"}
  CONTEXT7_TOOLS_USED=resolve-library-id,query-docs
  LEARN_TOOLS_USED=learn_microsoft_docs_search
  ```
- Gotcha: opencode flattens MCP tool names to `<server>_<tool>` (e.g. `context7_query-docs`,
  `learn_microsoft_docs_search`) — not `mcp__<server>__<tool>`; any per-driver allow-listing must
  use that shape. The model reported the learn tool WITH the prefix in its final line; the
  underlying tool name is `microsoft_docs_search`.

## Summary

| driver | verdict | native MCP tools observed | job |
|---|---|---|---|
| claude-code 2.1.272 | VERIFIED | `mcp__context7__resolve-library-id`, `mcp__context7__query-docs`, `mcp__learn__microsoft_docs_search`, `mcp__learn__microsoft_code_sample_search` | 651 |
| opencode 1.18.31 | VERIFIED | `context7_resolve-library-id`, `context7_query-docs`, `learn_microsoft_docs_search` | 655 |
| grok 1.0.30 (grok-4.6-build) | FAILED (native) — servers reached only via model-written raw HTTP JSON-RPC | none native (`use_tool` "Tool not found") | 654 |

Cross-driver gotchas: Context7's docs tool is live-named `query-docs` (not `get-library-docs`);
Learn advertises `microsoft_docs_search` / `microsoft_docs_fetch` / `microsoft_code_sample_search`;
`needs: []` is required for manual jobs in a stage after a failing job (lab `smoke` fails a seeded
test collection, which otherwise skips later-stage manual jobs with "Unplayable Job").
