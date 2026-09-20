
import { create, view, resolve, result } from "./index.js";

const BASE = process.env.MELD_URL || "http://127.0.0.1:8080";

async function main() {
  const m = await create("SDK test: what is 2+2?");
  console.log("create OK:", m.code);
  const v = await view(m.code);
  console.log("view OK:", v.context_a === "SDK test: what is 2+2?");
  const r = await resolve(m.code, "4");
  console.log("resolve OK:", r.resolved);
  const res = await result(m.code, m.owner_token);
  console.log("result OK:", res.context_b === "4");
  console.log("\nAll SDK tests passed.");
}
main();
