// Graph-MIND for OpenCode, written by graph-mind-install (rewritten on every install).
//
// 1. Every user message and every finished assistant reply is appended, as said, to a transcript
//    the capture service reads like Claude Code's: masked, stored, searchable by meaning, shared
//    with the user's other agents. Nothing else: no tool calls, no reasoning.
// 2. Before the model answers a message, the memory that bears on it is added to the system
//    prompt (python -m graph_mind.inject), so every model gets the past, not only the ones that
//    think to call brain_context.
// Any failure is silent: memory must never stand in the way of a message.
import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { execFile } from "node:child_process";

const PYTHON = __PYTHON__;
const TRANSCRIPTS = __TRANSCRIPTS__;
const INJECT_TIMEOUT_MS = 15000;

function write(sessionID, record) {
  try {
    mkdirSync(TRANSCRIPTS, { recursive: true });
    appendFileSync(join(TRANSCRIPTS, sessionID + ".jsonl"), JSON.stringify(record) + "\n");
  } catch {}
}

function recall(text) {
  return new Promise((resolve) => {
    try {
      const child = execFile(PYTHON, ["-m", "graph_mind.inject"],
        { timeout: INJECT_TIMEOUT_MS, windowsHide: true, maxBuffer: 1 << 20,
          env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" } },
        (error, stdout) => resolve(error ? "" : String(stdout || "")));
      child.stdin.end(text);
    } catch {
      resolve("");
    }
  });
}

export const GraphMindPlugin = async ({ directory }) => {
  const pending = new Map();     // sessionID -> memory for the message being answered
  const roles = new Map();       // messageID -> "user" | "assistant"
  const texts = new Map();       // messageID -> Map(partID -> text)
  const written = new Set();     // assistant messageIDs already appended
  return {
    "chat.message": async (input, output) => {
      const text = (output.parts || []).filter((p) => p.type === "text" && !p.synthetic)
        .map((p) => p.text || "").join("\n").trim();
      if (!text) return;
      write(input.sessionID, { type: "user", uuid: output.message?.id || input.messageID,
        sessionId: input.sessionID, cwd: directory, timestamp: new Date().toISOString(),
        message: { content: text } });
      const memory = await recall(text);
      if (memory.trim()) pending.set(input.sessionID, memory);
    },
    "experimental.chat.system.transform": async (input, output) => {
      const memory = input.sessionID && pending.get(input.sessionID);
      if (memory) {
        output.system.push(memory);
        pending.delete(input.sessionID);
      }
    },
    event: async ({ event }) => {
      try {
        const props = event.properties || {};
        if (event.type === "message.updated" && props.info) {
          const info = props.info;
          roles.set(info.id, info.role);
          if (info.role === "assistant" && info.time?.completed && !written.has(info.id)) {
            const text = [...(texts.get(info.id)?.values() || [])].join("\n").trim();
            if (text) {
              written.add(info.id);
              write(info.sessionID, { type: "assistant", uuid: info.id, sessionId: info.sessionID,
                cwd: directory, timestamp: new Date(info.time.completed).toISOString(),
                message: { content: text } });
            }
            texts.delete(info.id);
          }
        } else if (event.type === "message.part.updated" && props.part?.type === "text") {
          const part = props.part;
          if (part.synthetic || roles.get(part.messageID) === "user") return;
          if (!texts.has(part.messageID)) texts.set(part.messageID, new Map());
          texts.get(part.messageID).set(part.id, part.text || "");
        }
      } catch {}
    },
  };
};

export default { id: "graph-mind", server: GraphMindPlugin };
