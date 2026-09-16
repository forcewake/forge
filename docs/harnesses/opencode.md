# Harness: opencode

Template: `ci/templates/opencode.gitlab-ci.yml` · driver id `opencode`
· interface notes: [research/harness-interfaces.md §4](../research/harness-interfaces.md)

The open-source terminal coding agent (sst/opencode), run headless via
`run --auto` with an OpenAI-compatible provider endpoint. The forge
posture: a permission map injected as config — mechanical denies plus
explicit allows for the two "ask"-by-default keys that would otherwise
hang a headless run.

## CI variables

| Variable | Required | Purpose |
|---|---|---|
| `ZAI_API_KEY` | ✅ | key for the OpenAI-compatible endpoint the lane configures (the template ships a z.ai coding-endpoint provider; edit the generated `opencode.json` for your own) |
| `FORGE_HARNESS_MODEL` | — | the model routed by forge; substituted into the generated config (`__MODEL__`) |
| `FORGE_BOT_READ_TOKEN` | — | optional READ-ONLY repo PAT |

## Headless invocation (what the lane runs)

The lane generates `~/.config/opencode/opencode.json` — provider config +
permission map + MCP — then:

```
opencode run --auto "<task pointer>"
```

The generated permission map (the R5 posture):

```json
{
  "permission": {
    "bash": {"git commit *": "deny", "git push *": "deny", "*": "allow"},
    "external_directory": "allow",
    "doom_loop": "allow"
  },
  "mcp": {"<server>": {"type": "remote", "url": "…", "enabled": true}}
}
```

- **Mechanical deny**: `run --auto` approves what the map does not deny —
  and `git commit *` / `git push *` are denied outright (the lane's push
  URL is FORBIDDEN regardless).
- **Headless-hang sources closed**: `external_directory` and `doom_loop`
  default to **ask** — an unattended run that hits one HANGS forever. The
  map allows both explicitly.
- **Model is config-owned** (not a CLI flag) — the lane substitutes the
  dispatched model into the generated config, same as the GitLab template.

## MCP

The canonical `FORGE_HARNESS_MCP` JSON is schema-translated into the same
generated config: claude `http` → opencode `remote` (Streamable HTTP),
stdio `{command, args}` → `local` with a command array; `headers` map
1:1. `enabled: true` is set explicitly.

## Usage receipts

None parseable on stdout — the candidate meta records `usage: null`
(unknown ≠ zero, F22-lite). The actions lane records the same.

## Live notes / gotchas

- Validated live on GLM via the z.ai coding endpoint: tool loops complete
  in tens of seconds.
- `run --auto` auto-approves everything the map does not explicitly deny —
  the map IS the security boundary for this driver; that is why the deny
  rules are mechanical and the brief is contract, not enforcement.
- Install via the retrying `curl https://opencode.ai/install | bash`
  preamble; `opencode --version` lands in the trace.

## Triage

| Symptom | Look at |
|---|---|
| run hangs forever, no tool loop | an "ask"-default key fired — check the generated config carried the map (external_directory / doom_loop) |
| provider errors at turn 1 | the generated `opencode.json` — the model substitution and `{env:ZAI_API_KEY}` expansion |
| no changes in the candidate | opencode streams progress directly; the trace shows the tool loop |
