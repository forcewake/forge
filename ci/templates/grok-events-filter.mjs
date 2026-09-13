// Compactor for grok's `--output-format streaming-json` NDJSON stream
// (ACP session updates): turns the raw stream into short, readable trace
// lines for the GitLab job trace. Text/thinking deltas are concatenated
// and flushed as sentences; tool calls print their title; usage prints a
// one-line token summary. Mirrors claude-events-filter.mjs.
//
// F22 lite (ADR-0016 §4): [grok:usage] events are aggregated and written
// to .forge/usage.json (path overridable via FORGE_USAGE_FILE) plus a final
// FORGE_USAGE:{json} trace line, so the job can embed the receipt into
// candidate.meta.json. Counts are sums of per-turn receipts ->
// completeness "aggregate"; with no usage events nothing is written and
// the receipt stays unknown (never zero). Cached tokens are kept as their
// own field, never folded into the input count.
import { createInterface } from "node:readline";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const rl = createInterface({ input: process.stdin });

let textBuf = "";
let thinkBuf = "";
const usage = { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, turns: 0 };

function addUsage(u) {
  for (const key of ["input_tokens", "cached_input_tokens", "output_tokens"]) {
    const value = u?.[key];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
      usage[key] += value;
    }
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
    source: "grok:usage",
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

function wrap(prefix, s, cap) {
  const words = s.split(/\s+/);
  const lines = [];
  let cur = prefix;
  for (const w of words) {
    if ((cur + " " + w).trim().length > 96) {
      lines.push(cur.trimEnd());
      cur = " ".repeat(prefix.length) + w;
    } else {
      cur += (cur.length ? " " : "") + w;
    }
    if (lines.length >= cap) return lines.concat(" ".repeat(prefix.length) + "…");
  }
  if (cur.trim()) lines.push(cur.trimEnd());
  return lines;
}

function flushText() {
  const s = textBuf.trim();
  textBuf = "";
  if (s) console.log(wrap("[grok:say]   ", s, 6).join("\n"));
}

function flushThink() {
  const s = thinkBuf.trim();
  thinkBuf = "";
  if (s) console.log(wrap("[grok:think] ", s, 3).join("\n"));
}

rl.on("line", (line) => {
  const raw = line.trim();
  if (!raw) return;
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    return; // non-JSON noise on the stream — skip
  }
  const update = msg.update ?? msg.params?.update ?? msg;
  const kind =
    update?.sessionUpdate ?? msg.sessionUpdate ?? msg.method ?? msg.type ?? "event";

  if (kind === "text" || kind.includes("message_chunk")) {
    flushThink();
    textBuf += update.data ?? update.text ?? compactText(update.content) ?? "";
    return;
  }
  if (kind === "thought" || kind.includes("thought_chunk")) {
    thinkBuf += update.data ?? update.text ?? compactText(update.content) ?? "";
    return;
  }
  flushText();
  flushThink();
  if (kind === "tool_call" || kind === "tool_call_update") {
    const title = update.title ?? update.toolCall?.title ?? update.kind ?? "";
    if (title) console.log(`[grok:tool]  ${title}`);
  } else if (kind === "usage") {
    const u = update.usage ?? update;
    addUsage(u);
    console.log(
      `[grok:usage] in=${u.input_tokens} out=${u.output_tokens} cache=${u.cache_read_input_tokens ?? 0}`,
    );
  } else if (msg.error || kind === "error") {
    console.log(`[grok:error] ${JSON.stringify(msg.error ?? update).slice(0, 180)}`);
  } else if (kind === "end" || kind === "stop") {
    console.log(`[grok:end]   stop=${update.stopReason ?? kind}`);
  }
  // available_commands and unknown kinds stay silent — noise.
});

function compactText(content) {
  const blocks = Array.isArray(content) ? content : [content];
  return blocks
    .map((block) => (block && typeof block === "object" ? block.text : block) ?? "")
    .join("");
}

rl.on("close", () => {
  flushText();
  flushThink();
  writeUsageReceipt();
});
