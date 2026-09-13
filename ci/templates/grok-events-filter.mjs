// Compactor for grok's `--output-format streaming-json` NDJSON stream
// (ACP session updates): turns the raw stream into short, readable trace
// lines for the GitLab job trace. Text/thinking deltas are concatenated
// and flushed as sentences; tool calls print their title; usage prints a
// one-line token summary. Mirrors claude-events-filter.mjs.
import { createInterface } from "node:readline";

const rl = createInterface({ input: process.stdin });

let textBuf = "";
let thinkBuf = "";

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
});
