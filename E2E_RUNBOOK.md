# x402 preview end-to-end

This branch is for a throwaway Cloudflare preview Worker. Do not deploy Worker `meld`. Do not set secrets on Worker `meld` or D1 `meld-prod`. Do not commit payee addresses, facilitator tokens, or private keys.

Production (`https://meld.mergeinc.workers.dev`) stays on the free pilot. These curls hit the preview URL only.

PR #9's preview workflow (branch `cursor/cf-preview-deploys-1bf2`) is not on this branch. That workflow also strips `X402_*` from the deploy environment, so it cannot settle USDC. Use the commands below.

## What you need

| Name | Where | Notes |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | your shell, not the repo | Workers Scripts Edit and D1 Edit on the account. |
| `CLOUDFLARE_ACCOUNT_ID` | your shell, not the repo | 32 hex characters. |
| `X402_PAY_TO` | preview Worker secret | Base (`eip155:8453`) address that receives USDC. |
| `X402_FACILITATOR_URL` | preview Worker secret | `https://api.cdp.coinbase.com/platform/v2/x402` for Base mainnet. |
| `X402_FACILITATOR_AUTH` | preview Worker secret, if CDP requires it | The full `Authorization` header value, for example `Bearer <credential>`. |
| A wallet that can sign x402 exact USDC | the paying client | Not stored in this repo. Settlement is irreversible. |

Fixed in code: network `eip155:8453`, USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, amount `3330000` (6 decimals, $3.33), EIP-712 name `USD Coin` / version `2`.

Every create, including a paid one, lives 1 hour. Omit `ttl` or send `1hr`. Any other value is HTTP 400 and does not call the facilitator. Mint-next (`prev_code`) starts a new one-hour link (not an extend).

## 1. Create a preview Worker and D1

`deploy/wrangler.toml` names Worker `meld` and D1 `meld-prod`. Do not pass that file to Wrangler. Every command below uses a copy in `$STAGE`.

`deploy/pyproject.toml` asks for Python 3.13 (`uv`). The unit tests in this file do not need it.

```bash
GUID="$(uuidgen | tr 'A-Z' 'a-z')"
NAME="meld-prev-${GUID}"
test "$NAME" != "meld"
STAGE="$(mktemp -d)"
# Copy the worker sources. Do not copy deploy/wrangler.toml.
rsync -a --exclude wrangler.toml --exclude .venv --exclude .venv-workers \
  --exclude python_modules --exclude __pycache__ --exclude .wrangler \
  deploy/ "$STAGE/"
# d1 create does not need a database_id. Deploy does. Create the database
# from this directory only after the preview config exists, and never with
# deploy/wrangler.toml on the command line.
cat > "$STAGE/wrangler.toml" <<EOF
# Throwaway preview. Never deploy this as production.
name = "${NAME}"
main = "worker.py"
compatibility_date = "2026-09-18"
compatibility_flags = ["python_workers"]

workers_dev = true

[[d1_databases]]
binding = "DB"
database_name = "${NAME}"
database_id = "00000000-0000-0000-0000-000000000000"

[vars]
MELD_PREVIEW = "1"
EOF
if grep -qx 'name = "meld"' "$STAGE/wrangler.toml" || grep -q meld-prod "$STAGE/wrangler.toml"; then
  echo "refusing production wrangler config" >&2
  exit 1
fi

cd "$STAGE"
npx --yes wrangler@4.135.0 d1 create "$NAME" --config "$STAGE/wrangler.toml"
# Replace database_id in $STAGE/wrangler.toml with the UUID wrangler printed.
# Confirm the file still does not contain meld-prod or
# a550ad8f-9359-4666-ac10-c440ad461464.

# Fresh database: both ALTER statements in schema.sql succeed once.
# A second run may report duplicate column on resolver_ip and clicker_ip.
npx --yes wrangler@4.135.0 d1 execute "$NAME" --remote --config "$STAGE/wrangler.toml" --file=schema.sql
npx --yes wrangler@4.135.0 d1 execute "$NAME" --remote --config "$STAGE/wrangler.toml" --file=schema_api.sql

uv run --group dev pywrangler sync
rm -rf .venv .venv-workers
npx --yes wrangler@4.135.0 deploy \
  --name "$NAME" \
  --config "$STAGE/wrangler.toml" \
  --no-experimental-provision \
  --no-experimental-auto-create
```

`pywrangler sync` vendors FastAPI into `python_modules/`. The deploy config must be named `wrangler.toml` inside `$STAGE`, and that path must not be `deploy/wrangler.toml`.

Preview URL:

```text
https://meld-prev-<guid>.<account-subdomain>.workers.dev
```

Set `BASE` to that origin. If the hostname's first label is `meld`, stop.

## 2. Secrets on the preview Worker only

Wrangler reads `name` from `--config`. Point it at the preview file so the secret is not written to Worker `meld`.

```bash
cd "$STAGE"
printf '%s' "$X402_PAY_TO" | npx --yes wrangler@4.135.0 secret put X402_PAY_TO \
  --name "$NAME" --config "$STAGE/wrangler.toml"
printf '%s' "$X402_FACILITATOR_URL" | npx --yes wrangler@4.135.0 secret put X402_FACILITATOR_URL \
  --name "$NAME" --config "$STAGE/wrangler.toml"
# Omit this when the facilitator does not require Authorization.
printf '%s' "$X402_FACILITATOR_AUTH" | npx --yes wrangler@4.135.0 secret put X402_FACILITATOR_AUTH \
  --name "$NAME" --config "$STAGE/wrangler.toml"
```

D1 already has `x402_payments` from `schema.sql`. The worker also runs `CREATE TABLE IF NOT EXISTS` on the first successful claim. There is no `payTo` column.

## 3. Unpaid probe

```bash
curl -sS -D - -o /tmp/x402-challenge.json \
  -H 'accept: application/json' \
  "$BASE/api/x402"
```

Expect HTTP 402. The JSON body and the `PAYMENT-REQUIRED` header (base64 JSON) both contain:

- `x402Version`: `2`
- `accepts[0].scheme`: `exact`
- `accepts[0].network`: `eip155:8453`
- `accepts[0].amount`: `3330000`
- `accepts[0].asset`: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- `accepts[0].payTo`: the preview's `X402_PAY_TO`
- `resource.url`: `$BASE/api/x402`

Before `X402_PAY_TO` is set, the same GET returns HTTP 503 and does not invent an address.

An agent `POST $BASE/api/melds` with a valid `ttl` and no payment returns the same 402 shape after 3 creates from that IP in the hour. A body with a missing or invalid `ttl` is HTTP 400 and is not a challenge.

## 4. Paid create

Sign an x402 v2 exact payment for the challenge above (amount, asset, `payTo`, network, resource URL `$BASE/api/x402`). Put the base64 payload in `PAYMENT-SIGNATURE`. The signing key stays on the client.

```bash
curl -sS -D - -o /tmp/x402-create.json \
  -X POST "$BASE/api/x402" \
  -H 'content-type: application/json' \
  -H "PAYMENT-SIGNATURE: $PAYMENT_SIGNATURE" \
  -d '{"context":"preview paid handoff","ttl":"1hr"}'
```

Expect HTTP 200, a `code`, `ttl` of `1hr`, and header `PAYMENT-RESPONSE` whose base64 JSON has `success: true` and a `transaction`. `expires_at` is about one hour out. Omit `ttl` for the same result. Any other `ttl` is HTTP 400 and does not settle.

Empty context is HTTP 400 and does not settle.

## 5. Resolve

```bash
CODE="$(python3 -c 'import json; print(json.load(open("/tmp/x402-create.json"))["code"])')"
curl -sS -X POST "$BASE/api/melds/$CODE/resolve" \
  -H 'content-type: application/json' \
  -d '{"context":"preview answer"}'
curl -sS "$BASE/api/melds/$CODE"
```

Resolve returns the original context and `resolved: true`. GET returns both sides while the bridge is live. The TTL chosen at create is unchanged by resolve.

## 6. Delete the preview

```bash
npx --yes wrangler@4.135.0 delete --name "$NAME" --config "$STAGE/wrangler.toml" --force
npx --yes wrangler@4.135.0 d1 delete "$NAME" --skip-confirmation
rm -rf "$STAGE"
```

If delete would target `meld` or `meld-prod`, stop.

## Local tests (no network, facilitator stubbed)

```bash
python3 test_x402.py
python3 test_pay_per_meld.py
python3 test_freelimit_002.py
python3 test_no_subscriptions.py
python3 test_pilot_bridges.py
```
