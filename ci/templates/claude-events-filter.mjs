// Compacts claude code stream-json events into short, human-readable trace
// lines. Used by ci/templates/claude-code.gitlab-ci.yml so the job log shows
// live progress (tool calls, retries, API errors) while the harness runs.
// Full-fidelity events stay in /tmp/claude-events.jsonl inside the job.
//
// F22 lite (ADR-0016 §4): terminal `result` usage receipts are aggregated
// and written to .forge/usage.json (path overridable via FORGE_USAGE_FILE)
// plus a final FORGE_USAGE:{json} trace line, so the job can embed the
// receipt into candidate.meta.json. Counts are sums of per-turn receipts ->
// completeness "aggregate"; with no result events nothing is written and
// the receipt stays unknown (never zero). cache_read_input_tokens is kept
// as its own cached field, never folded into the input count;
// cache_creation_input_tokens are not claimed as cached reads and stay
// out of the receipt rather than being invented.
import { createInterface } from "node:readline";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const ts = () => new Date().toISOString().slice(11, 19);
const rl = createInterface({ input: process.stdin });

const usage = { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, turns: 0 };

function addUsage(u) {
  for (const [key, source] of [
    ["input_tokens", "input_tokens"],
    ["output_tokens", "output_tokens"],
  ]) {
    const value = u?.[source];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
      usage[key] += value;
    }
  }
  const cached = u?.cache_read_input_tokens;
  if (typeof cached === "number" && Number.isFinite(cached) && cached >= 0) {
    usage.cached_input_tokens += cached;
  }
  usage.turns += 1;
}

function writeUsageReceipt() {
  if (usage.turns === 0) return; // unknown stays unknown — never zero
  const receipt = {
    input_tokens: usage.input_tokens,
    cached_input_tokens: usage.cached_input_tokens,
    output_tokens: usage.output_tokens,
    completeness: "aggregate",
    source: "claude:result",
  };
  const line = `FORGE_USAGE:${JSON.stringify(receipt)}`;
  console.log(line);
  try {
    const file = process.env.FORGE_USAGE_FILE || ".forge/usage.json";
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, JSON.stringify(receipt));
  } catch {
    // The receipt is best-effort; never break the trace pipeline on a
    // filesystem error.
  }
}

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
    addUsage(e.usage);
    console.log(
      `[${ts()}] result: ${e.subtype ?? "?"} turns=${e.num_turns ?? "?"} cost_usd=${e.total_cost_usd ?? "?"} out_tokens=${e.usage?.output_tokens ?? "?"}`,
    );
  }
}
writeUsageReceipt();
