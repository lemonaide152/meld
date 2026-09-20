#!/usr/bin/env node

const BASE = process.env.MELD_URL || "https://meld.lemonaide152.workers.dev";
const API_KEY = process.env.MELD_API_KEY;

const [cmd, ...args] = process.argv.slice(2);

async function api(method, path, body, extraHeaders = {}) {
  const headers = { ...extraHeaders };
  if (body) headers["Content-Type"] = "application/json";
  if (API_KEY) headers["Authorization"] = `Bearer ${API_KEY}`;
  const res = await fetch(`${BASE}${path}`, {
    method, headers, body: body ? JSON.stringify(body) : undefined
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(`${res.status}: ${data.detail || "unknown error"}`);
  }
  return data;
}

async function main() {
  try {
    if (cmd === "send") {
      const context = args.join(" ");
      if (!context) { console.error("Usage: meld send <your context>"); process.exit(1); }
      const m = await api("POST", "/api/melds", { context });
      console.log(`\n  meld created: ${m.code}\n`);
      console.log(`  share link:  ${m.url}`);
      if (m.owner_url) {
        console.log(`  owner link:  ${m.owner_url}`);
        console.log(`\n  ⚠ Save the owner link — it's the only way to read the result.\n`);
      }
      console.log(`  Share the link with whoever needs the context.`);
      console.log(`  When they resolve, check with: meld read ${m.code} --token <owner_token>`);
      if (m.owner_token) console.log(`\n  owner_token: ${m.owner_token}\n`);
    } else if (cmd === "view") {
      const [code] = args;
      if (!code) { console.error("Usage: meld view <code>"); process.exit(1); }
      const v = await api("GET", `/api/melds/${code}`);
      console.log(`\n  meld: ${code}\n`);
      console.log(`  status: ${v.resolved ? "resolved" : "waiting for party B"}`);
      console.log(`  context_a: ${v.context_a?.substring(0, 200)}${v.context_a?.length > 200 ? "…" : ""}`);
      if (v.context_b) console.log(`  context_b: ${v.context_b?.substring(0, 200)}`);
      console.log();
    } else if (cmd === "resolve") {
      const [code, ...rest] = args;
      const context = rest.join(" ");
      if (!code || !context) { console.error("Usage: meld resolve <code> <your answer>"); process.exit(1); }
      const r = await api("POST", `/api/melds/${code}/resolve`, { context });
      console.log(`\n  meld resolved: ${code}\n`);
      console.log(`  context_a: ${r.context_a?.substring(0, 200)}`);
      console.log(`  context_b: ${r.context_b?.substring(0, 200)}`);
      console.log(`\n  The meld will dissolve soon.\n`);
    } else if (cmd === "read") {
      const [code, ...rest] = args;
      let token = rest.find(a => !a.startsWith("--"));
      if (!token) { console.error("Usage: meld read <code> <owner_token>"); process.exit(1); }
      const r = await api("GET", `/api/melds/${code}/result`, null);
      // Actually need to use headers
      const res = await fetch(`${BASE}/api/melds/${code}/result`, {
        headers: { "X-Meld-Token": token }
      });
      if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
      const d = await res.json();
      console.log(`\n  meld: ${code}\n`);
      console.log(`  context_a: ${d.context_a?.substring(0, 300)}`);
      console.log(`  context_b: ${d.context_b?.substring(0, 300)}\n`);
    } else {
      console.log(`
  meld — ephemeral context bridge

  Usage:
    meld send <context>              Create a meld
    meld resolve <code> <answer>     Resolve a meld
    meld read <code> <owner_token>   Read the result
    meld view <code>                 View a meld

  Environment:
    MELD_URL       Override the base URL
    MELD_API_KEY   API key for agent tier (higher limits)

  Learn more: https://meld.lemonaide152.workers.dev
`);
    }
  } catch (e) {
    console.error(`
  Error: ${e.message}
`);
    process.exit(1);
  }
}

main();
