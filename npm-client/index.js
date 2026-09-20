const BASE = process.env.MELD_URL || "http://127.0.0.1:8080";

export async function create(context, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.apiKey) headers["Authorization"] = `Bearer ${opts.apiKey}`;
  const res = await fetch(`${BASE}/api/melds`, {
    method: "POST", headers,
    body: JSON.stringify({ context })
  });
  if (!res.ok) throw new Error(`create failed: ${res.status}`);
  return res.json();
}

export async function view(code) {
  const res = await fetch(`${BASE}/api/melds/${code}`, {
    headers: { "Accept": "application/json" }
  });
  if (!res.ok) throw new Error(`view failed: ${res.status}`);
  return res.json();
}

export async function resolve(code, context, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.apiKey) headers["Authorization"] = `Bearer ${opts.apiKey}`;
  const res = await fetch(`${BASE}/api/melds/${code}/resolve`, {
    method: "POST", headers,
    body: JSON.stringify({ context })
  });
  if (!res.ok) throw new Error(`resolve failed: ${res.status}`);
  return res.json();
}

export async function result(code, ownerToken) {
  const res = await fetch(`${BASE}/api/melds/${code}/result`, {
    headers: { "X-Meld-Token": ownerToken }
  });
  if (!res.ok) throw new Error(`result failed: ${res.status}`);
  return res.json();
}

export { BASE };
