#!/usr/bin/env node
import { create, view, resolve, result } from "./index.js";

const [cmd, ...args] = process.argv.slice(2);
const apiKey = process.env.MELD_API_KEY;

async function main() {
  try {
    if (cmd === "send") {
      const context = args.join(" ");
      if (!context) { console.error("Usage: meld send <context>"); process.exit(1); }
      const m = await create(context, { apiKey });
      console.log(`\nmeld created: ${m.url}`);
      console.log(`owner link (save this): ${m.owner_url || m.url + " (no owner_url)"}`);
      console.log(`code: ${m.code}\n`);
      console.log("Share the link. When the other party resolves, read the result with:");
      console.log(`  meld read ${m.code} --token ${m.owner_token}\n`);
    } else if (cmd === "read") {
      const [code, token] = args;
      if (!code || !token) { console.error("Usage: meld read <code> --token <owner_token>"); process.exit(1); }
      const r = await result(code, token);
      console.log(JSON.stringify(r, null, 2));
    } else if (cmd === "resolve") {
      const [code, ...rest] = args;
      const context = rest.join(" ");
      if (!code || !context) { console.error("Usage: meld resolve <code> <answer>"); process.exit(1); }
      const r = await resolve(code, context, { apiKey });
      console.log(`\nmeld resolved: ${code}\n`);
      console.log(JSON.stringify(r, null, 2));
    } else if (cmd === "view") {
      const [code] = args;
      const v = await view(code);
      console.log(JSON.stringify(v, null, 2));
    } else {
      console.log(`meld — ephemeral context bridge\n`);
      console.log(`  meld send <context>            Create a meld`);
      console.log(`  meld resolve <code> <answer>   Resolve a meld`);
      console.log(`  meld read <code> --token <t>   Read the result`);
      console.log(`  meld view <code>               View a meld`);
    }
  } catch (e) {
    console.error("Error:", e.message);
    process.exit(1);
  }
}
main();
