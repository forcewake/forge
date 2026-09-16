// forge harness log filter — one universal renderer for all four drivers.
//
// Usage: node harness-log-filter.mjs <claude-code|grok-build|opencode|copilot>
// (driver also read from FORGE_LOG_DRIVER). Reads the driver's stdout stream
// on stdin, writes ONLY the unified grammar lines (docs/research/
// harness-log-formats.md §5) to stdout, and appends the F22-lite usage
// receipt (FORGE_USAGE:{json}, ADR-0016 §4) at stream end — skipped when no
// receipts were seen (unknown stays unknown, never zero).
//
// Full-fidelity logs live in the lane's events file (tee BEFORE this filter);
// the job log gets the readable trace. Suppression is counted, never silent.
//
// claude-code : stream-json events (assistant/user/system/result)
// grok-build  : streaming-json (update unwrap, end aggregate wins)
// opencode    : `run --auto --format json` NDJSON parts (step_finish tokens)
// copilot     : no machine events [documented] — passthrough + header/footer

import { createInterface } from "node:readline";
import { appendFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const driver = (process.argv[2] || process.env.FORGE_LOG_DRIVER || "")
  .replace(/^["']|["']$/g, "")
  .trim();
// Collapse platform: "actions" (::group::), "gitlab" (section_start
// [collapsed=true]), "flat" (no native folding — Azure DevOps has no log
// section commands; the pretty lines render as-is).
const platform = (process.env.FORGE_LOG_PLATFORM || "actions").trim().toLowerCase();

const groupOpen = (title, id) => {
  if (platform === "actions") {
    emit(`::group::${title}`);
  } else if (platform === "gitlab") {
    const epoch = Math.floor(Date.now() / 1000);
    process.stdout.write(
      `\x1b[0Ksection_start:${epoch}:${id}[collapsed=true]\r\x1b[0K${title}\n`,
    );
  } else {
    emit(title); // flat: the one-line summary still reads on its own
  }
};
const groupClose = (id) => {
  if (platform === "actions") emit("::endgroup::");
  else if (platform === "gitlab") {
    const epoch = Math.floor(Date.now() / 1000);
    process.stdout.write(`\x1b[0Ksection_end:${epoch}:${id}\r\x1b[0K`);
  }
  // flat: nothing to close
};
const usageFile = process.env.FORGE_USAGE_FILE || ".forge/usage.json";
const eventsFile = process.env.FORGE_EVENTS_FILE || "";

// ── formatting helpers ────────────────────────────────────────────────
const stripAnsi = (s) => String(s ?? "").replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
const oneLine = (s) => stripAnsi(s).replace(/\s+/g, " ").trim();
const cap = (s, n) => {
  const clean = oneLine(s);
  return clean.length > n ? clean.slice(0, n) + "…" : clean;
};
const ts = () => new Date().toISOString().slice(11, 19);
const kfmt = (n) =>
  n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
const emit = (line) => process.stdout.write(line + "\n");
const glyphLine = (glyph, payload) => emit(`${ts()} ${glyph} ${payload}`);

// ── shared counters ───────────────────────────────────────────────────
const stats = {
  tools: 0, toolsOk: 0, toolsErr: 0,
  hidden: 0, hiddenLastReport: 0,
  thinkTokens: 0,
  turns: 0,
  input: 0, cached: 0, output: 0,
  cost: null,
  startedAt: Date.now(),
  sawReceipt: false,
  model: "",
  session: "",
  result: "",
};
const hidden = (n = 1) => {
  stats.hidden += n;
  if (stats.hidden - stats.hiddenLastReport >= 100) {
    glyphLine("…", `${stats.hidden} routine events hidden`);
    stats.hiddenLastReport = stats.hidden;
  }
};
const turnSep = () => {
  const cached = stats.cached ? ` (c ${kfmt(stats.cached)})` : "";
  emit(`── turn ${stats.turns} · ↑${kfmt(stats.input)}${cached} ↓${kfmt(stats.output)} tok ──`);
};
const hintFrom = (raw) => {
  // Pull the most human-meaningful ≤80-char hint out of a tool input object.
  if (raw == null) return "";
  if (typeof raw === "string") return cap(raw, 80);
  if (typeof raw !== "object") return cap(String(raw), 80);
  const keys = ["file_path", "path", "notebook_path", "command", "pattern", "url",
    "query", "skill", "description", "prompt", "file", "name", "id"];
  for (const key of keys) {
    const v = raw[key];
    if (typeof v === "string" && v.trim()) return cap(v, 80);
  }
  const first = Object.values(raw).find((v) => typeof v === "string" && v.trim());
  return first ? cap(first, 80) : cap(JSON.stringify(raw), 80);
};
const resultSize = (content) => {
  try {
    return JSON.stringify(content).length;
  } catch {
    return 0;
  }
};
const firstText = (content) => {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const block = content.find((b) => b && b.type === "text");
    return block?.text ?? "";
  }
  return "";
};
const fmtDuration = (ms) => {
  const s = Math.round(ms / 1000);
  return s >= 60 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s` : `${s}s`;
};
const summaryFooter = (exit) => {
  const cached = stats.cached ? ` (c ${kfmt(stats.cached)})` : "";
  const cost = stats.cost != null ? ` · $${stats.cost.toFixed(4)}` : "";
  const dur = fmtDuration(Date.now() - stats.startedAt);
  emit(`── forge run summary ${"─".repeat(41)}`);
  emit(`driver ${driver} · model ${stats.model || "unknown"} · exit ${exit}`);
  emit(`turns ${stats.turns} · ↑${kfmt(stats.input)}${cached} ↓${kfmt(stats.output)} tok${cost} · ${dur}`);
  emit(`tools ${stats.tools} (✅ ${stats.toolsOk} · ❌ ${stats.toolsErr}) · 💭 ~${kfmt(stats.thinkTokens)} tok · hidden ${stats.hidden}`);
  if (stats.result) emit(`result: ${cap(stats.result, 140)}`);
};
const writeUsageReceipt = () => {
  if (!stats.sawReceipt) return; // unknown stays unknown — never zero
  const receipt = {
    input_tokens: stats.input,
    cached_input_tokens: stats.cached,
    output_tokens: stats.output,
    completeness: "aggregate",
    source: `${driver}:stream`,
  };
  emit(`FORGE_USAGE:${JSON.stringify(receipt)}`);
  try {
    mkdirSync(dirname(usageFile), { recursive: true });
    writeFileSync(usageFile, JSON.stringify(receipt, null, 2) + "\n");
  } catch { /* the receipt line above is the contract; the file is a copy */ }
};

// ── collapsible tool groups ───────────────────────────────────────────
// A group opens at tool_use (title = the 🔧 line) and closes after the
// tool_result lands; the expanded view carries the full formatted output.
let openGroup = null;
const groupFor = (hint) =>
  "tool-" + oneLine(hint).toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 40) || "tool";
const openToolGroup = (hint) => {
  if (openGroup) groupClose(openGroup);
  openGroup = groupFor(hint);
  groupOpen(`${ts()} 🔧 ${cap(hint, 80)}`, openGroup);
};
const closeToolGroup = (outcome, hint, detail) => {
  glyphLine(outcome, `${cap(hint, 80)}${detail ? " · " + detail : ""}`);
  if (openGroup) {
    groupClose(openGroup);
    openGroup = null;
  }
};

// ── driver: claude-code ───────────────────────────────────────────────
const pendingClaude = new Map(); // tool_use_id -> hint
let claudeThoughtThisTurn = false;

async function runClaude(rl) {
  for await (const line of rl) {
    let e;
    try {
      e = JSON.parse(line);
    } catch {
      hidden();
      continue;
    }
    if (e.type === "system") {
      if (e.subtype === "init") {
        stats.session = String(e.session_id || "").slice(0, 8);
        stats.model = String(e.model || "");
        emit(`── claude-code · model ${stats.model || "?"} · session ${stats.session || "?"} ──`);
      } else if (e.subtype === "api_retry") {
        glyphLine("⏳", `API retry ${e.attempt ?? "?"}/${e.max_retries ?? "?"}` +
          (e.error_status ? ` · HTTP ${e.error_status}` : "") +
          (e.retry_delay_ms != null ? ` · wait ${(e.retry_delay_ms / 1000).toFixed(1)}s` : ""));
      } else if (e.subtype === "thinking_tokens") {
        stats.thinkTokens += e.estimated_tokens ?? 0;
      } else {
        hidden();
      }
      continue;
    }
    if (e.type === "assistant") {
      const scope = e.parent_tool_use_id ? "⟲ " : "";
      for (const b of e.message?.content ?? []) {
        if (b.type === "tool_use") {
          stats.tools += 1;
          const hint = hintFrom(b.input);
          pendingClaude.set(b.id, `${b.name} ${hint}`.trim());
          if (!scope) openToolGroup(`${b.name} ${hint}`);
          else glyphLine("🔧", `⟲ ${b.name} ${hint}`);
        } else if (b.type === "text") {
          const text = cap(b.text, 140);
          if (text) glyphLine("💬", text);
        } else if (b.type === "thinking") {
          if (!claudeThoughtThisTurn) glyphLine("💭", `${cap(b.thinking, 110)}…`);
          claudeThoughtThisTurn = true;
        }
      }
      continue;
    }
    if (e.type === "user") {
      for (const b of e.message?.content ?? []) {
        if (b.type !== "tool_result") continue;
        const hint = pendingClaude.get(b.tool_use_id) || "tool";
        pendingClaude.delete(b.tool_use_id);
        if (b.is_error) {
          stats.toolsErr += 1;
          const full = oneLine(firstText(b.content));
          if (full) emit(cap(full, 4000)); // expanded view: the real output
          closeToolGroup("❌", hint, cap(firstText(b.content), 120));
        } else {
          stats.toolsOk += 1;
          const full = oneLine(firstText(b.content));
          if (full) emit(cap(full, 4000)); // expanded view: the real output
          closeToolGroup("✅", hint, `${kfmt(resultSize(b.content))}B`);
        }
      }
      continue;
    }
    if (e.type === "result") {
      stats.turns = e.num_turns ?? stats.turns;
      addUsageClaude(e.usage ?? {});
      if (e.total_cost_usd != null) stats.cost = e.total_cost_usd;
      stats.result = e.result ?? "";
      turnSep();
      claudeThoughtThisTurn = false;
      continue;
    }
    hidden();
  }
}
function addUsageClaude(u) {
  for (const [key, src] of [["input", "input_tokens"], ["output", "output_tokens"]]) {
    const v = u?.[src];
    if (typeof v === "number" && Number.isFinite(v) && v >= 0) stats[key] += v;
  }
  const cached = u?.cache_read_input_tokens;
  if (typeof cached === "number" && Number.isFinite(cached) && cached >= 0) stats.cached += cached;
  stats.sawReceipt = true;
}

// ── driver: grok-build ────────────────────────────────────────────────
const grokCalls = new Map(); // callId -> title
let grokEnd = null;

async function runGrok(rl) {
  for await (const line of rl) {
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      hidden();
      continue;
    }
    const e = msg.update ?? msg.params?.update ?? msg;
    if (e.type === "tool_call") {
      stats.tools += 1;
      const title = cap(e.title || hintFrom(e.rawInput), 80);
      grokCalls.set(e.callId ?? `${e.toolName}`, `${e.toolName ?? "tool"} ${title}`.trim());
      openToolGroup(`${e.toolName ?? "tool"} ${title}`);
      continue;
    }
    if (e.type === "tool_call_update") {
      const hint = grokCalls.get(e.callId) || "tool";
      const failed = e.status && e.status !== "success" && e.status !== "completed";
      const size = resultSize(e.rawOutput);
      if (failed) {
        stats.toolsErr += 1;
        closeToolGroup("❌", hint, cap(oneLine(e.rawOutput), 120));
      } else {
        stats.toolsOk += 1;
        closeToolGroup("✅", hint, `${kfmt(size)}B`);
      }
      continue;
    }
    if (e.type === "text" || e.type === "assistant_text") {
      const text = cap(e.text ?? e.content, 140);
      if (text) glyphLine("💬", text);
      continue;
    }
    if (e.type === "thought" || e.type === "thinking") {
      const text = cap(e.text ?? e.thinking, 110);
      if (text) glyphLine("💭", `${text}…`);
      continue;
    }
    if (e.type === "usage") {
      addUsageGrok(e);
      turnSep();
      continue;
    }
    if (e.type === "end") {
      grokEnd = e;
      if (e.usage) addUsageGrok(e.usage, /*override*/ true);
      stats.turns = e.num_turns ?? stats.turns;
      continue;
    }
    hidden(); // available_commands, plan, …
  }
}
function addUsageGrok(u, override = false) {
  if (override) {
    stats.input = u?.input_tokens ?? stats.input;
    stats.cached = u?.cached_input_tokens ?? stats.cached;
    stats.output = u?.output_tokens ?? stats.output;
  } else {
    stats.input += u?.input_tokens ?? 0;
    stats.cached += u?.cached_input_tokens ?? 0;
    stats.output += u?.output_tokens ?? 0;
  }
  stats.sawReceipt = true;
}

// ── driver: opencode ──────────────────────────────────────────────────
let ocBuffers = { text: null, thought: null };

const ocFlush = () => {
  if (ocBuffers.thought) {
    glyphLine("💭", `${cap(ocBuffers.thought, 110)}…`);
    ocBuffers.thought = null;
  }
  if (ocBuffers.text) {
    glyphLine("💬", cap(ocBuffers.text, 140));
    ocBuffers.text = null;
  }
};

async function runOpencode(rl) {
  for await (const line of rl) {
    let e;
    try {
      e = JSON.parse(line);
    } catch {
      hidden();
      continue;
    }
    const part = e.part ?? e;
    const type = part.type ?? e.type;
    if (type === "text") {
      ocBuffers.text = (ocBuffers.text ? ocBuffers.text + " " : "") + (part.text ?? "");
      continue;
    }
    if (type === "reasoning") {
      ocBuffers.thought = (ocBuffers.thought ? ocBuffers.thought + " " : "") + (part.text ?? "");
      continue;
    }
    if (type === "tool_use") {
      ocFlush();
      stats.tools += 1;
      const state = part.state ?? {};
      const hint = cap(state.title || hintFrom(state.input), 80);
      if (state.status === "error" || state.error) {
        stats.toolsErr += 1;
        glyphLine("❌", `${part.tool ?? "tool"} ${hint} · ${cap(oneLine(state.output), 120)}`);
      } else {
        stats.toolsOk += 1;
        closeToolGroup("✅", `${part.tool ?? "tool"} ${hint}`, `${kfmt(resultSize(state.output))}B`);
      }
      continue;
    }
    if (type === "step_finish") {
      ocFlush();
      const t = part.tokens ?? {};
      stats.input += t.input ?? 0;
      stats.cached += t.cache?.read ?? 0;
      stats.output += (t.output ?? 0) + (t.reasoning ?? 0);
      stats.turns += 1;
      stats.sawReceipt = true;
      if (part.cost) stats.cost = (stats.cost ?? 0) + part.cost;
      turnSep();
      continue;
    }
    if (type === "error") {
      ocFlush();
      glyphLine("❌", cap(part.message ?? JSON.stringify(part), 120));
      continue;
    }
    if (type === "step_start") {
      ocFlush();
      continue;
    }
    hidden();
  }
  ocFlush();
}

// ── driver: copilot (passthrough — no machine events [documented]) ────
async function runCopilot(rl) {
  emit(`── copilot · raw CLI output (no machine events available) ──`);
  for await (const line of rl) {
    emit(line.length > 200 ? line.slice(0, 200) + "…" : line);
  }
}

// ── dispatch ──────────────────────────────────────────────────────────
const runners = {
  "claude-code": runClaude,
  "grok-build": runGrok,
  opencode: runOpencode,
  copilot: runCopilot,
};
const runner = runners[driver];
if (!runner) {
  process.stderr.write(`harness-log-filter: unknown driver ${driver || "(empty)"}\n`);
  process.exit(2);
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
try {
  await runner(rl);
} catch (err) {
  process.stderr.write(`harness-log-filter: stream error: ${err}\n`);
}

// ── footer ────────────────────────────────────────────────────────────
const exit = process.env.FORGE_DRIVER_EXIT || "completed";
summaryFooter(exit);
if (grokEnd?.stopReason) emit(`stop reason: ${grokEnd.stopReason}`);
writeUsageReceipt();
// full-fidelity pointer
if (eventsFile) appendFileSync(eventsFile, "");
