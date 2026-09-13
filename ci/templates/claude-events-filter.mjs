// Compacts claude code stream-json events into short, human-readable trace
// lines. Used by ci/templates/claude-code.gitlab-ci.yml so the job log shows
// live progress (tool calls, retries, API errors) while the harness runs.
// Full-fidelity events stay in /tmp/claude-events.jsonl inside the job.
import { createInterface } from "node:readline";

const ts = () => new Date().toISOString().slice(11, 19);
const rl = createInterface({ input: process.stdin });

for await (const line of rl) {
  let e;
  try {
    e = JSON.parse(line);
  } catch {
    continue;
  }
  if (e.type === "system" && e.subtype === "init") {
    console.log(`[${ts()}] init: model=${e.model ?? "?"} session=${(e.session_id ?? "").slice(0, 8)}`);
  } else if (e.type === "system" && e.subtype === "api_error") {
    console.log(
      `[${ts()}] API-ERROR retry ${e.retryAttempt ?? "?"}/${e.maxRetries ?? "?"}: ${e.error?.formatted ?? e.error?.message ?? "unknown"}`,
    );
  } else if (e.type === "assistant" && Array.isArray(e.message?.content)) {
    for (const b of e.message.content) {
      if (b.type === "tool_use") {
        const d = b.input ?? {};
        const hint = d.file_path ?? d.command ?? d.pattern ?? d.path ?? "";
        console.log(`[${ts()}] tool: ${b.name} ${String(hint).slice(0, 100)}`);
      } else if (b.type === "text" && b.text?.trim()) {
        console.log(`[${ts()}] say: ${b.text.replace(/\n/g, " ").slice(0, 140)}`);
      } else if (b.type === "thinking" && b.thinking?.trim()) {
        console.log(`[${ts()}] think: ${b.thinking.replace(/\n/g, " ").slice(0, 110)}`);
      }
    }
  } else if (e.type === "result") {
    console.log(
      `[${ts()}] result: ${e.subtype ?? "?"} turns=${e.num_turns ?? "?"} cost_usd=${e.total_cost_usd ?? "?"} out_tokens=${e.usage?.output_tokens ?? "?"}`,
    );
  }
}
