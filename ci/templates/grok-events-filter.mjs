// Compactor for grok's `--output-format streaming-json` NDJSON stream
// (ACP session updates): turns a raw stream into short trace lines so the
// GitLab job trace shows live harness progress without drowning in
// multi-kilobyte tool payloads. Mirrors claude-events-filter.mjs.
//
// The exact wire shape is not publicly documented, so this filter is
// defensive: it recognizes the common envelope variants and falls back to
// printing a truncated JSON body for anything it does not understand.
import { createInterface } from "node:readline";

const rl = createInterface({ input: process.stdin });

function compactText(content) {
  const blocks = Array.isArray(content) ? content : [content];
  return blocks
    .map((block) => (block && typeof block === "object" ? block.text : block) ?? "")
    .join("");
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

  let detail = "";
  if (kind === "tool_call" || kind === "tool_call_update") {
    detail = update.title ?? update.toolCall?.title ?? update.kind ?? "";
    if (!detail) detail = JSON.stringify(update).slice(0, 160);
  } else if (kind.includes("chunk") || kind === "agent_message") {
    detail = compactText(update.content ?? update.delta ?? update);
  } else if (msg.error || kind === "error") {
    detail = JSON.stringify(msg.error ?? update);
  } else {
    detail = JSON.stringify(update ?? msg).slice(0, 160);
  }

  detail = String(detail).replace(/\s+/g, " ").trim().slice(0, 200);
  if (detail) console.log(`[grok:${kind}] ${detail}`);
});
