"""
Redis-backed auth helpers — reads the shared cache written by Repo 1's
morning-login.yml. Repo 2 does not log in independently (see
claude_change_spec_repo2.md Step 4).

Key shapes verified against Repo 1's src/kite_client.py, src/stock_kite_client.py:
  kite:instrument_tokens   {index_name: {"token": int, ...}}         — futures tokens
  kite:option_tokens       {"{name}_{strike}_{CE|PE}": {token, tradingsymbol, lot_size}}
  kite:stock_equity_tokens {equity_symbol: instrument_token}          — plain int values
  kite:stock_option_tokens same shape as kite:option_tokens, for the 14 stocks
"""
from __future__ import annotations

import json
import logging

import redis as redis_lib

log = logging.getLogger(__name__)

KEY_ACCESS_TOKEN        = "kite:access_token"
KEY_INSTRUMENT_TOKENS    = "kite:instrument_tokens"
KEY_OPTION_TOKENS        = "kite:option_tokens"
KEY_STOCK_EQUITY_TOKENS  = "kite:stock_equity_tokens"
KEY_STOCK_OPTION_TOKENS  = "kite:stock_option_tokens"


def get_access_token(r: redis_lib.Redis) -> str:
    raw = r.get(KEY_ACCESS_TOKEN)
    if not raw:
        raise RuntimeError(
            "kite:access_token missing from Redis — ensure Repo 1's "
            "morning-login.yml ran today"
        )
    return raw.decode() if isinstance(raw, bytes) else raw


def _load_json(r: redis_lib.Redis, key: str, required: bool = True) -> dict:
    raw = r.get(key)
    if not raw:
        if required:
            raise RuntimeError(f"{key} missing — ensure Repo 1's morning-login.yml ran today")
        return {}
    return json.loads(raw)


def _by_tradingsymbol(cache: dict) -> dict:
    """Repo 1 keys option caches by '{name}_{strike}_{type}'; rebuild a
    tradingsymbol-indexed view for O(1) lookup."""
    out = {}
    for _, data in cache.items():
        ts = data.get("tradingsymbol")
        if ts:
            out[ts] = data
    return out


def get_option_cache(r: redis_lib.Redis) -> dict:
    """Merged index + stock option cache, keyed by tradingsymbol."""
    indices = _by_tradingsymbol(_load_json(r, KEY_OPTION_TOKENS))
    stocks  = _by_tradingsymbol(_load_json(r, KEY_STOCK_OPTION_TOKENS, required=False))
    return {**indices, **stocks}


def get_lot_size(r: redis_lib.Redis, tradingsymbol: str) -> int:
    cache = get_option_cache(r)
    if tradingsymbol in cache and "lot_size" in cache[tradingsymbol]:
        return int(cache[tradingsymbol]["lot_size"])
    log.error("lot_size not found for %s in shared Repo1 cache", tradingsymbol)
    raise RuntimeError(f"lot_size not found for {tradingsymbol} — check kite:option_tokens / kite:stock_option_tokens")


def get_instrument_token(r: redis_lib.Redis, tradingsymbol: str) -> int:
    cache = get_option_cache(r)
    if tradingsymbol in cache:
        return int(cache[tradingsymbol]["token"])
    raise RuntimeError(f"instrument_token not found for {tradingsymbol}")


def get_underlying_token(r: redis_lib.Redis, instrument_name: str) -> int:
    """Futures/spot token for RSI candle fetch — index futures for indices,
    equity spot for stocks."""
    idx_tokens = _load_json(r, KEY_INSTRUMENT_TOKENS, required=False)
    if instrument_name.upper() in idx_tokens:
        info = idx_tokens[instrument_name.upper()]
        return int(info["token"] if isinstance(info, dict) else info)
    stock_tokens = _load_json(r, KEY_STOCK_EQUITY_TOKENS, required=False)
    if instrument_name.upper() in stock_tokens:
        info = stock_tokens[instrument_name.upper()]
        return int(info["token"] if isinstance(info, dict) else info)
    raise RuntimeError(f"underlying token not found for {instrument_name}")
