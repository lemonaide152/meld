const DEFAULT_BASE = "https://meld.lemonaide152.workers.dev";

function getBase(opts) {
  return (opts && opts.baseUrl) || process.env.MELD_URL || DEFAULT_BASE;
}

export async function create(context, opts = {}) {
  const base = getBase(opts);
  const headers = { "Content-Type": "application/json" };
  if (opts.apiKey) headers["Authorization"] = `Bearer ${opts.apiKey}`;
  const res = await fetch(`${base}/api/melds`, {
    method: "POST", headers,
    body: JSON.stringify({ context })
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`meld create failed (${res.status}): ${err.detail || "unknown error"}`);
  }
  return res.json();
}

export async function view(code, opts = {}) {
  const base = getBase(opts);
  const headers = { "Accept": "application/json" };
  if (opts.apiKey) headers["Authorization"] = `Bearer ${opts.apiKey}`;
  const res = await fetch(`${base}/api/melds/${code}`, { headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`meld view failed (${res.status}): ${err.detail || "not found"}`);
  }
  return res.json();
}

export async function resolve(code, context, opts = {}) {
  const base = getBase(opts);
  const headers = { "Content-Type": "application/json" };
  if (opts.apiKey) headers["Authorization"] = `Bearer ${opts.apiKey}`;
  const res = await fetch(`${base}/api/melds/${code}/resolve`, {
    method: "POST", headers,
    body: JSON.stringify({ context })
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`meld resolve failed (${res.status}): ${err.detail || "unknown error"}`);
  }
  return res.json();
}

export async function result(code, ownerToken, opts = {}) {
  const base = getBase(opts);
  const res = await fetch(`${base}/api/melds/${code}/result`, {
    headers: { "X-Meld-Token": ownerToken }
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`meld result failed (${res.status}): ${err.detail || "invalid token"}`);
  }
  return res.json();
}
