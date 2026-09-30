#!/usr/bin/env node
/**
 * meld MCP server — Model Context Protocol server for meld.
 *
 * Lets any MCP client (Claude Desktop, Claude Code, Cursor, ...) create and
 * resolve melds as tools. Zero dependencies: speaks JSON-RPC over stdio.
 *
 * Usage:
 *   MELD_BASE=https://meld.mergeinc.workers.dev node meld-mcp.js
 *
 * Tools:
 *   meld_create   — create a meld with your context, get a share link
 *   meld_resolve  — resolve a meld (answer it) with your context
 *   meld_read     — owner: read the result (needs owner token)
 */
import { createInterface } from "node:readline";

const BASE = process.env.MELD_BASE || "https://meld.mergeinc.workers.dev";

// ---- JSON-RPC over stdio ----
const rl = createInterface({ input: process.stdin });
const send = (msg) => process.stdout.write(JSON.stringify(msg) + "\n");

function reply(id, result) { send({ jsonrpc: "2.0", id, result }); }
function error(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

// ---- meld API ----
async function api(method, path, body, headers = {}) {
  const res = await fetch(BASE + path, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

const TOOLS = [
  {
    name: "meld_create",
    description:
      "Create a timed bridge. Returns a capability URL for the other party. ttl is required: 3m, 1hr, or 1d. There is no default. Pilot bridges are free. The host can read the exchange while it is live; anyone with the link can too. Not for secrets. Dissolves when that TTL ends.",
    inputSchema: {
      type: "object",
      properties: {
        context: { type: "string", description: "Working context the other party should read. Not for secrets, credentials, or regulated data." },
        ttl: {
          type: "string",
          enum: ["3m", "1hr", "1d"],
          description: "Required bridge time. 3m = 3 minutes, 1hr = 1 hour, 1d = 1 day. The server enforces this TTL. There is no default.",
        },
        pin: { type: "string", description: "Optional PIN the other party must supply to answer." },
      },
      required: ["context", "ttl"],
    },
  },
  {
    name: "meld_resolve",
    description:
      "Resolve a meld you received a link for. Submit your context/answer. Returns the other party's context. The meld dissolves shortly after.",
    inputSchema: {
      type: "object",
      properties: {
        code: { type: "string", description: "The meld code from the link (the part after /m/)." },
        context: { type: "string", description: "Your answer/context." },
        pin: { type: "string", description: "PIN if the meld has one." },
      },
      required: ["code", "context"],
    },
  },
  {
    name: "meld_read",
    description:
      "Owner: read the resolved result of your meld. The owner token rotates on every read — use the newest one.",
    inputSchema: {
      type: "object",
      properties: {
        code: { type: "string", description: "The meld code." },
        owner_token: { type: "string", description: "Your owner token (from meld_create or the previous read)." },
      },
      required: ["code", "owner_token"],
    },
  },
];

async function callTool(name, args) {
  if (name === "meld_create") {
    const allowed = new Set(["3m", "1hr", "1d"]);
    if (!allowed.has(args.ttl)) {
      throw new Error("Bridge time is required. Choose 3m, 1hr, or 1d. There is no default.");
    }
    const body = { context: args.context, ttl: args.ttl };
    if (args.pin) body.pin = args.pin;
    const d = await api("POST", "/api/melds", body);
    return {
      code: d.code,
      share_link: d.url,
      owner_link: d.owner_url,
      ttl: d.ttl,
      expires_at: d.expires_at,
      note: "Send the share link. It is the capability URL. The bridge dissolves at expires_at. Not for secrets.",
    };
  }
  if (name === "meld_resolve") {
    const body = { context: args.context };
    if (args.pin) body.pin = args.pin;
    const d = await api("POST", `/api/melds/${encodeURIComponent(args.code)}/resolve`, body);
    return { resolved: true, their_context: d.context_a, your_context: d.context_b };
  }
  if (name === "meld_read") {
    const d = await api("GET", `/api/melds/${encodeURIComponent(args.code)}/result`, null,
      { "X-Meld-Token": args.owner_token });
    return {
      resolved: true,
      their_context: d.context_b,
      your_context: d.context_a,
      new_owner_token: d.owner_token,
      note: "Use new_owner_token for any future read — the old token is now invalid.",
    };
  }
  throw new Error(`Unknown tool: ${name}`);
}

rl.on("line", async (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  const { id, method, params } = msg;
  if (!method) return;

  if (method === "initialize") {
    reply(id, {
      protocolVersion: "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "meld", version: "1.0.0" },
    });
  } else if (method === "tools/list") {
    reply(id, { tools: TOOLS });
  } else if (method === "tools/call") {
    try {
      const result = await callTool(params.name, params.arguments || {});
      reply(id, { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] });
    } catch (e) {
      reply(id, { content: [{ type: "text", text: `Error: ${e.message}` }], isError: true });
    }
  } else if (method === "notifications/initialized") {
    // no response needed
  } else if (id !== undefined) {
    error(id, -32601, `Method not found: ${method}`);
  }
});
