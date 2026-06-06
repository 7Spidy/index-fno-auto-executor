"""
Redis-backed auth helpers — copied from signal bot pattern.
Reads the access token and instrument caches written by morning-login.yml.
"""

from __future__ import annotations

import json
import logging

import redis as redis_lib

log = logging.getLogger(__name__)

# Keys written by morning-login.yml
KEY_ACCESS_TOKEN   = "kite:access_token"
KEY_INSTRUMENTS    = "kite:instruments"        # {tradingsymbol: {token, lot_size, ...}}
KEY_OPTION_TOKENS  = "kite:option_tokens"      # {tradingsymbol: instrument_token}
KEY_NIFTY_TOKEN    = "kite:nifty_spot_token"   # int — NIFTY 50 instrument token


def get_access_token(r: redis_lib.Redis) -> str:
    raw = r.get(KEY_ACCESS_TOKEN)
    if not raw:
        raise RuntimeError(
            "kite:access_token missing from Redis — ensure morning-login.yml ran today"
        )
    return raw.decode() if isinstance(raw, bytes) else raw


def get_instrument_cache(r: redis_lib.Redis) -> dict:
    raw = r.get(KEY_INSTRUMENTS)
    if not raw:
        raise RuntimeError("kite:instruments missing — ensure morning-login.yml ran today")
    return json.loads(raw)


def get_option_token_cache(r: redis_lib.Redis) -> dict:
    raw = r.get(KEY_OPTION_TOKENS)
    if not raw:
        raise RuntimeError("kite:option_tokens missing — ensure morning-login.yml ran today")
    return json.loads(raw)


def get_nifty_spot_token(r: redis_lib.Redis) -> int:
    raw = r.get(KEY_NIFTY_TOKEN)
    if raw:
        return int(raw)
    # Well-known fallback: NIFTY 50 on NSE has token 256265 on Kite.
    # morning-login.yml should write this; we use the fallback defensively.
    log.warning("kite:nifty_spot_token not in Redis — using hardcoded fallback 256265")
    return 256265


def get_lot_size(r: redis_lib.Redis, tradingsymbol: str) -> int:
    """Look up lot size from the instrument cache."""
    cache = get_instrument_cache(r)
    if tradingsymbol in cache:
        return int(cache[tradingsymbol].get("lot_size", 25))
    # Derive from any cached NIFTY option entry — lot size is per underlying.
    for sym, data in cache.items():
        if sym.startswith("NIFTY") and "lot_size" in data:
            return int(data["lot_size"])
    log.warning("lot_size not found for %s — defaulting to 25", tradingsymbol)
    return 25
