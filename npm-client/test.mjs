import { create, view, resolve, result } from "./index.js";
import assert from "node:assert";

const BASE = process.env.MELD_URL || "http://127.0.0.1:8080";

async function main() {
  console.log("Testing meld SDK against", BASE);

  // 1. Create
  const m = await create("SDK test: the auth flow uses OAuth2+PKCE");
  assert(m.code && m.code.length === 12, "code should be 12 chars");
  assert(m.owner_token && m.owner_token.length === 64, "owner_token should be 64 chars");
  assert(m.owner_url && m.owner_url.includes("#t="), "owner_url should have fragment");
  console.log("  ✅ create:", m.code);

  // 2. View
  const v = await view(m.code);
  assert(v.context_a === "SDK test: the auth flow uses OAuth2+PKCE");
  assert(!v.resolved, "should not be resolved yet");
  console.log("  ✅ view");

  // 3. Resolve
  const r = await resolve(m.code, "Looks good, add rate limiting");
  assert(r.resolved === true);
  console.log("  ✅ resolve");

  // 4. Result with token
  const res = await result(m.code, m.owner_token);
  assert(res.context_b === "Looks good, add rate limiting");
  assert(res.owner_token !== m.owner_token, "token should rotate");
  console.log("  ✅ result + rotation");

  // 5. Old token rejected
  let rejected = false;
  try {
    await result(m.code, m.owner_token);
  } catch (e) {
    rejected = e.message.includes("403") || e.message.includes("Invalid");
  }
  assert(rejected, "old token should be rejected");
  console.log("  ✅ old token rejected");

  console.log("\n  All 5 SDK tests passed.\n");
}

main().catch(e => { console.error(e); process.exit(1); });
