# forge — demo script («Your factory. Your keys. Your merge button.»)

Sales demo, **~2:45**, 1080p30, English voiceover (FluidAudio/Kokoro, voice
`af_heart`). Every frame is a **real screen recording of the live GitLab
UI** — nothing is staged, nothing is cut; long waits are sped up. Regenerate
end-to-end with the `forge-demo` skill (`.claude/skills/forge-demo/SKILL.md`)
and `demo/assemble_full_ui.py`.

| # | Beat | On screen (recording) | Voiceover |
|---|------|------------------------|-----------|
| S01 | Hook | Title card | You run your own GitLab. Full control, zero vendor lock-in. But there is no Premium tier, no Duo, no AI-assisted anything. Your factory floor is missing its robots. |
| S02 | Idea | Repo scroll | This is forge. An open source agentic software factory sidecar for self-hosted GitLab Community Edition. An authorized issue becomes a branch with code, a green pipeline, and a Draft merge request. And the iron principle: forge never merges. You do. |
| S03/S04 | Spark → plan | Issue #23: typing `/implement` → post → the forge plan live-appends on camera | It starts with a single comment on an issue. Watch. / Seconds later, forge answers with a concrete plan — posted right on the issue, with a digest that binds it to this exact run. Nothing implicit. Nothing hidden. |
| S05 | Gate | Issue #23: typing `@forge /go <run-id>` → gate consumed | And then forge does what most automation never does: it stops and asks. A human presses go. The gate is single-use, bound to the plan digest and the base commit. Approval here is a signature, not a rubber stamp. |
| S06 | Work | Job #612 from birth: docker prep → npm → the agent streaming `[grok:say]/[grok:think]/[grok:tool]` → FORGE_RESULT → Job succeeded (3× speed) | Now the factory floor. The coding agent runs inside your own CI — an ephemeral container with no secrets and no access to forge. Every tool call streams live into the job trace. You can watch it think. |
| S08 | Fail-safe | Pipeline #292 red (seeded bug) → repair job log (cycle 2) | Failure is a first class citizen here. Cancel a job, and forge blames the infrastructure, not the code. Kill the worker mid-run, and the run survives — a new worker picks it up. Red pipeline? forge enters a bounded repair cycle, carrying the failing logs with it. |
| S07 | Proof | Draft MR !15: description (plan digest, candidate, cycle), review verdict, evidence comment, green pipeline widget | When the agent claims it is done, forge refuses to take its word for it. The branch head is verified independently, a Draft merge request is opened, the pipeline must go green, and a separate reviewer reads the diff and files its verdict — evidence bound to the exact commit. |
| S09 | The deal | Back to the top of the MR — the merge button | Which brings us to the deal. The merge button stays yours. forge assembles the evidence — the tests, the review, the exact commit — and a human makes the call. Always. |
| S10 | Outro | End card | forge is open source, and it is AI-ready: point your coding agent at the repository and it sets itself up — doctor verifies every step. forge. Your factory. Your keys. Your merge button. |

## Production layout

- `demo/production/video/*.webm` — raw IAB recordings (A, B, C, C1, C2, D, E)
- `demo/production/video/out/*.mp4` — per-scene renders (timed to VO)
- `demo/production/audio/S*.wav` — per-scene voiceover
- `demo/assemble_full_ui.py` — the assembler (speed-ups, VO placement, concat)
- Final cut: `demo/forge-demo.mp4`

Regenerate: `.claude/skills/forge-demo/SKILL.md`.
