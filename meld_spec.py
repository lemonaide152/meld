"""Constants from SPEC.md. scripts/check_spec.py fails the build if these drift.

Every value here must equal the `meld-spec` block in SPEC.md.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

VERSION = "2.0.0"

OPEN_HOURS = 36          # from creation until the first reply
IDLE_HOURS = 24          # set by the first reply, reset by each later reply
MAX_CHARS = 100_000      # per note and per reply
REPLY_CAP = 50           # the next reply after this is the uniform 404
CODE_BYTES = 24          # 192 bits from secrets.token_urlsafe
MIN_CODE_BITS = 128
SWEEP_MINUTES = 5

NOT_FOUND_STATUS = 404
NOT_FOUND_BODY = {"detail": "Meld not found"}

CREATE_FIELDS = ("note", "context")
LEGACY_FIELDS = ("for", "not_for")
REJECTED_FIELDS = ("ttl", "email", "pin", "prev_code")
REPLY_FIELD = "context"
MCP_TOOLS = ("meld_create", "meld_resolve", "meld_read")

# Codes longer than this are not looked up. They are the uniform 404.
MAX_CODE_LEN = 64

# Tests may shorten these in-process. There is no env override.
OPEN_SECONDS = OPEN_HOURS * 3600
IDLE_SECONDS = IDLE_HOURS * 3600

# Timestamps in JSON and in storage: UTC, ISO 8601, milliseconds, Z.
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime) -> str:
    """One timestamp format everywhere: 2026-10-09T16:48:51.313Z."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def open_expiry(now: datetime) -> str:
    return ts(now + timedelta(seconds=OPEN_SECONDS))


def idle_expiry(now: datetime) -> str:
    return ts(now + timedelta(seconds=IDLE_SECONDS))


def new_code() -> str:
    """192 random bits, URL-safe. Never sequential."""
    return secrets.token_urlsafe(CODE_BYTES)


def code_bits() -> int:
    return CODE_BYTES * 8
