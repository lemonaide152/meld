"""x402 (HTTP 402) exact-scheme payments: USDC on Base mainnet.

Protocol: x402 v2 (PAYMENT-REQUIRED / PAYMENT-SIGNATURE / PAYMENT-RESPONSE).
https://github.com/coinbase/x402/blob/main/specs/x402-specification-v2.md
https://github.com/coinbase/x402/blob/main/specs/transports-v2/http.md

The payee address and any facilitator credential come from Worker env at
request time. This module has no default wallet. Unlock happens only after
the facilitator verifies the authorization and settle returns a transaction
hash that matches the amount, payTo, asset, and network we required.
A client body flag is not a payment.

On-chain settlement is irreversible.
"""
import base64
import json
import re
import time

# Base mainnet (CAIP-2). Not Base Sepolia.
NETWORK = "eip155:8453"
# Circle USDC on Base. Public token contract, not a payee.
# EIP-712 domain on this contract is name "USD Coin", version "2", 6 decimals.
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_NAME = "USD Coin"
USDC_VERSION = "2"
# $3.33 with 6 decimal places. Same price as MELD_PRICE_CENTS (333).
AMOUNT_ATOMIC = "3330000"
AMOUNT_CENTS = 333
MAX_TIMEOUT_SECONDS = 300
SCHEME = "exact"

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SIG = re.compile(r"^0x[0-9a-fA-F]{130}$")
_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")

# Replaced by the worker with the Workers fetch wrapper. Tests replace it too.
async def http_post(url: str, headers: dict, body: str):
    raise RuntimeError("x402 http_post is not configured")


class PaymentRejected(Exception):
    """Payment missing or not acceptable. body is a PaymentRequired object."""

    def __init__(self, status: int, body: dict, headers: dict):
        self.status = status
        self.body = body
        self.headers = headers
        super().__init__(body.get("error") if isinstance(body, dict) else "payment required")


def config_from_env(env) -> dict:
    """Read payee and facilitator settings. Invalid payTo is treated as unset."""
    pay_to = (getattr(env, "X402_PAY_TO", None) or "").strip() if env is not None else ""
    if not _ADDRESS.match(pay_to):
        pay_to = ""
    facilitator = (getattr(env, "X402_FACILITATOR_URL", None) or "").strip() if env is not None else ""
    facilitator = facilitator.rstrip("/")
    if facilitator and not facilitator.startswith("https://"):
        facilitator = ""
    auth = (getattr(env, "X402_FACILITATOR_AUTH", None) or "").strip() if env is not None else ""
    return {
        "pay_to": pay_to,
        "facilitator_url": facilitator,
        "facilitator_auth": auth,
    }


def payment_header(request) -> str:
    """PAYMENT-SIGNATURE only. Body fields are never a payment."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get("payment-signature")
    except Exception:
        value = None
    if value:
        return str(value).strip()
    try:
        items = list(headers.items())
    except Exception:
        return ""
    for key, value in items:
        if str(key).lower() == "payment-signature" and value:
            return str(value).strip()
    return ""


def _b64_json(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()


def _decode_b64_json(value: str):
    raw = (value or "").strip()
    if not raw:
        return None
    pad = "=" * ((4 - len(raw) % 4) % 4)
    data = None
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            data = decoder(raw + pad)
            break
        except Exception:
            continue
    if data is None:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def requirements(pay_to: str) -> dict:
    return {
        "scheme": SCHEME,
        "network": NETWORK,
        "amount": AMOUNT_ATOMIC,
        "asset": USDC_BASE,
        "payTo": pay_to,
        "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
        "extra": {
            "name": USDC_NAME,
            "version": USDC_VERSION,
            "assetTransferMethod": "eip3009",
        },
    }


def challenge(resource_url: str, pay_to: str, error: str = "PAYMENT-SIGNATURE header is required") -> dict:
    """x402 v2 PaymentRequired. pay_to must already be a configured address."""
    return {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": resource_url,
            "description": (
                "One meld: ephemeral context bridge. Host-readable while live; "
                "anyone with the link can read it. Not for secrets, credentials, "
                "or regulated data."
            ),
            "mimeType": "application/json",
        },
        "accepts": [requirements(pay_to)],
        "extensions": {},
    }


def challenge_headers(body: dict, pricing_header: str) -> dict:
    return {
        "PAYMENT-REQUIRED": _b64_json(body),
        "X-Meld-Pricing": pricing_header,
        "Access-Control-Expose-Headers": "PAYMENT-REQUIRED, PAYMENT-RESPONSE",
        "Cache-Control": "no-store",
    }


def settlement_header(settlement: dict) -> str:
    body = {
        "success": True,
        "transaction": settlement["transaction"],
        "network": NETWORK,
        "payer": settlement.get("payer") or "",
    }
    return _b64_json(body)


def _atomic(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _addr_eq(left, right) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


def validate_payload(payload, cfg: dict, resource_url: str) -> str:
    """Return an error string, or '' when the payload matches our terms.

    Does not check the EIP-712 signature. The facilitator does that, then
    settle must return a transaction. This only rejects payloads that could
    not be a payment to us for the pinned amount.
    """
    if not isinstance(payload, dict):
        return "malformed PAYMENT-SIGNATURE"
    if payload.get("x402Version") != 2:
        return "unsupported x402 version"
    accepted = payload.get("accepted")
    inner = payload.get("payload")
    if not isinstance(accepted, dict) or not isinstance(inner, dict):
        return "malformed PAYMENT-SIGNATURE"
    if accepted.get("scheme") != SCHEME:
        return "unsupported scheme"
    if accepted.get("network") != NETWORK:
        return "unsupported network"
    if not _addr_eq(accepted.get("asset"), USDC_BASE):
        return "unsupported asset"
    if not _addr_eq(accepted.get("payTo"), cfg["pay_to"]):
        return "payTo mismatch"
    if _atomic(accepted.get("amount")) != int(AMOUNT_ATOMIC):
        return "amount mismatch"
    resource = payload.get("resource")
    if resource is not None:
        if not isinstance(resource, dict) or resource.get("url") != resource_url:
            return "resource mismatch"
    auth = inner.get("authorization")
    if not isinstance(auth, dict):
        return "malformed authorization"
    signature = inner.get("signature")
    if not isinstance(signature, str) or not _SIG.match(signature):
        return "malformed signature"
    if not _addr_eq(auth.get("to"), cfg["pay_to"]):
        return "authorization payTo mismatch"
    if not _ADDRESS.match(auth.get("from") or ""):
        return "malformed payer"
    if _atomic(auth.get("value")) != int(AMOUNT_ATOMIC):
        return "authorization amount mismatch"
    nonce = auth.get("nonce")
    if not isinstance(nonce, str) or not _BYTES32.match(nonce):
        return "malformed nonce"
    try:
        valid_after = int(str(auth.get("validAfter")))
        valid_before = int(str(auth.get("validBefore")))
    except (TypeError, ValueError):
        return "malformed validity window"
    now = int(time.time())
    # validAfter is often 0. Bound how far the authorization still reaches,
    # not the distance back to a zero validAfter.
    if valid_before <= now or valid_after > now:
        return "authorization expired or not yet valid"
    if valid_before - now > MAX_TIMEOUT_SECONDS:
        return "authorization window exceeds maxTimeoutSeconds"
    return ""


def _reject(status: int, resource_url: str, pay_to: str, error: str, pricing_header: str) -> PaymentRejected:
    body = challenge(resource_url, pay_to, error=error)
    return PaymentRejected(status, body, challenge_headers(body, pricing_header))


async def _post_json(url: str, auth: str, body: dict):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    status, text = await http_post(url, headers, json.dumps(body))
    try:
        parsed = json.loads(text) if text else None
    except Exception:
        parsed = None
    return status, parsed


async def settle_payment(cfg: dict, header_value: str, resource_url: str, pricing_header: str) -> dict:
    """Verify then settle. Raise PaymentRejected unless settle yields a tx.

    Returned dict is safe to persist: transaction, nonce, payer, network,
    amount_atomic. It does not include payTo.
    """
    pay_to = cfg["pay_to"]
    decoded = _decode_b64_json(header_value)
    if not isinstance(decoded, dict):
        raise _reject(400, resource_url, pay_to, "malformed PAYMENT-SIGNATURE", pricing_header)
    err = validate_payload(decoded, cfg, resource_url)
    if err:
        status = 400 if err == "malformed PAYMENT-SIGNATURE" else 402
        raise _reject(status, resource_url, pay_to, err, pricing_header)
    if not cfg["facilitator_url"]:
        raise _reject(
            402, resource_url, pay_to,
            "x402 facilitator is not configured", pricing_header)
    reqs = requirements(pay_to)
    envelope = {
        "x402Version": 2,
        "paymentPayload": decoded,
        "paymentRequirements": reqs,
    }
    base = cfg["facilitator_url"]
    v_status, verified = await _post_json(base + "/verify", cfg["facilitator_auth"], envelope)
    if v_status != 200 or not isinstance(verified, dict) or verified.get("isValid") is not True:
        reason = "payment verification failed"
        if isinstance(verified, dict) and verified.get("invalidReason"):
            reason = str(verified["invalidReason"])[:200]
        raise _reject(402, resource_url, pay_to, reason, pricing_header)
    auth_from = decoded["payload"]["authorization"]["from"]
    v_payer = verified.get("payer") or ""
    if v_payer and not _addr_eq(v_payer, auth_from):
        raise _reject(402, resource_url, pay_to, "verification payer mismatch", pricing_header)
    s_status, settled = await _post_json(base + "/settle", cfg["facilitator_auth"], envelope)
    if s_status != 200 or not isinstance(settled, dict) or settled.get("success") is not True:
        reason = "payment settlement failed"
        if isinstance(settled, dict) and settled.get("errorReason"):
            reason = str(settled["errorReason"])[:200]
        raise _reject(402, resource_url, pay_to, reason, pricing_header)
    tx = settled.get("transaction") or ""
    if not isinstance(tx, str) or not _TX.match(tx):
        raise _reject(402, resource_url, pay_to, "settlement missing transaction", pricing_header)
    if settled.get("network") and settled.get("network") != NETWORK:
        raise _reject(402, resource_url, pay_to, "settlement network mismatch", pricing_header)
    s_payer = settled.get("payer") or ""
    if s_payer and not _addr_eq(s_payer, auth_from):
        raise _reject(402, resource_url, pay_to, "settlement payer mismatch", pricing_header)
    if settled.get("amount") is not None and _atomic(settled.get("amount")) != int(AMOUNT_ATOMIC):
        raise _reject(402, resource_url, pay_to, "settlement amount mismatch", pricing_header)
    return {
        "transaction": tx.lower(),
        "nonce": decoded["payload"]["authorization"]["nonce"].lower(),
        "payer": s_payer or auth_from,
        "network": NETWORK,
        "amount_atomic": AMOUNT_ATOMIC,
    }


def d1_changes(result) -> int:
    try:
        return int(result["meta"]["changes"])
    except Exception:
        try:
            return int(result.meta.changes)
        except Exception:
            return -1


CREATE_X402_PAYMENTS = (
    "CREATE TABLE IF NOT EXISTS x402_payments ("
    " tx_hash TEXT PRIMARY KEY,"
    " nonce TEXT NOT NULL UNIQUE,"
    " meld_code TEXT NOT NULL,"
    " amount_atomic TEXT NOT NULL,"
    " payer TEXT,"
    " network TEXT NOT NULL,"
    " paid_at TEXT NOT NULL)"
)


async def ensure_table(conn) -> None:
    await conn.prepare(CREATE_X402_PAYMENTS).run()


async def claim_payment(conn, settlement: dict, meld_code: str, paid_at: str) -> bool:
    """Insert the settled tx. False if this tx or nonce was already redeemed.

    The row does not store payTo.
    """
    await ensure_table(conn)
    inserted = await conn.prepare(
        "INSERT INTO x402_payments (tx_hash, nonce, meld_code, amount_atomic,"
        " payer, network, paid_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT DO NOTHING"
    ).bind(
        settlement["transaction"],
        settlement["nonce"],
        meld_code,
        settlement["amount_atomic"],
        settlement.get("payer") or "",
        settlement["network"],
        paid_at,
    ).run()
    return d1_changes(inserted) == 1


async def release_claim(conn, tx_hash: str) -> None:
    try:
        await conn.prepare("DELETE FROM x402_payments WHERE tx_hash = ?").bind(tx_hash).run()
    except Exception:
        pass
