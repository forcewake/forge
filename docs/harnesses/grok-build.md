# Harness: Grok Build

Template: `ci/templates/grok.gitlab-ci.yml` · driver id `grok-build`
· interface notes: [research/harness-interfaces.md §3](../research/harness-interfaces.md)

xAI's coding CLI, run headless with subscription auth. The forge posture:
always-approve (the documented CI mode) tamed by deny rules that BEAT it,
plus the hard-won npm provisioning that prevents a silent startup hang.

## CI variables

| Variable | Required | Purpose |
|---|---|---|
| `FORGE_GROK_AUTH` | ✅ | the full `~/.grok/auth.json` bundle from a local `grok login` (subscription auth — no per-run API key). **Refresh tokens rotate on every refresh**: after local CLI use, re-copy the bundle into the CI variable before runs |
| `FORGE_HARNESS_HTTPS_PROXY` | — | strongly recommended on runner networks that throttle long AI streams (lab: 226 s direct vs 2 s via proxy) |
| `FORGE_BOT_READ_TOKEN` | — | optional READ-ONLY repo PAT |

## Headless invocation (what the lane runs)

```bash
# Hardened npm preamble (REQUIRED — see gotchas):
for attempt in 1 2 3; do
  npm install -g --no-fund --no-audit @xai-official/grok && break
  sleep $((attempt * 5))
done
GROK_VER="$(grok --version | awk '{print $2}')"
npm install -g --no-fund --no-audit "@xai-official/grok-linux-x64@${GROK_VER}" \
  || npm install -g --no-fund --no-audit @xai-official/grok-linux-x64
test -d /usr/local/lib/node_modules/@xai-official/grok-linux-x64

grok --no-auto-update --always-approve --no-alt-screen \
  --trust --max-turns 200 \
  --deny 'Bash(git commit:*)' --deny 'Bash(git push:*)' \
  --output-format streaming-json \
  --debug-file .forge/grok-debug.log \
  -p "<task pointer>"
```

- **Mechanical deny**: `--deny` rules survive `--always-approve`
  (documented precedence: deny > allow > always-approve) — commit/push are
  blocked no matter what the model does.
- **`--always-approve` is mandatory**: without it headless grok HANGS on
  the first approval prompt (the ephemeral container is the execution
  profile, ADR-0002).
- **`--trust`**: project rules (AGENTS.md conventions) load headlessly —
  without it the brief is the only convention source.
- **`--max-turns 200`**: the only loop guard grok has; the run deadline
  (ADR-0015) is the outer bound.

## MCP

`mcpServers` written to `~/.grok/settings.json` from the canonical
`FORGE_HARNESS_MCP` JSON (claude-shaped `http` entries pass through — grok
follows the Gemini-CLI conventions). **Known vendor gap (grok 1.0.30)**:
HTTP servers listed in settings are "listed but not connected" in headless
runs — the provisioning stays so the lane upgrades cleanly when fixed
upstream; the lane network itself reaches MCP servers fine.

## Usage receipts

The best receipts of the four drivers: per-response `usage` events, with
the final `end` event carrying the run aggregate — it wins over the sum
(summing both would double-count). Aggregated into `.forge/usage.json`.

## Live notes / gotchas

- **The platform-binary hang**: the `@xai-official/grok` wrapper declares
  its Linux binary as an optionalDependency — a flaky registry silently
  skips it and grok futex-hangs forever at startup. The preamble installs
  BOTH packages explicitly, pins the binary to the wrapper's version, and
  `test -d`s the result. Keep it.
- **Streaming from throttled networks**: point
  `FORGE_HARNESS_HTTPS_PROXY` at a fast-path proxy (lab: one-word prompt
  226 s direct vs 2 s proxied).
- Default `-p` output is silent until completion — the streaming-json +
  filter makes the trace useful; `--debug-file` is the post-mortem.
- Model: grok-4.6-build (vendor default at time of writing).

## Triage

| Symptom | Look at |
|---|---|
| hang at startup, no output | the platform binary missing — check the preamble's `test -d` line |
| `Tool not found: server__tool` | the vendor MCP gap above |
| auth failures mid-run | the rotated refresh token — refresh `FORGE_GROK_AUTH` |
| stalled stream, long silences | the proxy variable |
