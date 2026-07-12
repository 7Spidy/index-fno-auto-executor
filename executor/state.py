"""
Redis state layer — spec §13 (idempotency, startup reconcile, single-position lock).

Keys managed here:
  executor:position       — serialised PositionState dict (TTL: end of day)
  executor:pending_intent — trade intent from signal bot (TTL set by signal bot, 6 min)
  executor:last_signal_ts — ISO ts of last consumed intent (for cooldown gate)

Position dict schema (all phases share the same key; unused fields are None):
  phase              str   IDLE|ENTERING|OPEN_FIXED|LOCKED|RUNNER|EXITING|COOLDOWN
  intent_ts          str   ISO timestamp of the signal that created this trade
  instrument         str   e.g. "NIFTY"
  direction          str   "CE" | "PE"
  tradingsymbol      str   e.g. "NIFTY2560824500CE"
  atm_strike         int
  spot_risk_pts      float
  atm_delta          float
  entry_premium      float  (set on fill)
  entry_spot         float  NIFTY spot at entry fill time
  sl_premium         float  current SL (ratcheted)
  target_premium     float | None  (None in runner mode)
  qty                int
  entry_order_id     str
  sl_order_id        str
  target_order_id    str | None
  breakeven_hit      bool
  peak_premium       float  (for runner give-back calculation)
  last_health_ts     str | None  ISO ts of last health rescore
  last_health_score  int
  vwap_lost_consec   int   consecutive 5-min candles with VWAP condition False
  entry_ts           str   ISO ts of entry fill
  cooldown_start_ts  str | None  ISO ts when COOLDOWN phase began
  exit_reason        str | None
  exit_premium       float | None
  pnl                float | None
  sl_filled          bool  (set by reconcile)
  sl_fill_price      float | None
  target_filled      bool  (set by reconcile)
  target_fill_price  float | None
  position_flat_external  bool  (set by reconcile on live mismatch)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import redis as redis_lib

log = logging.getLogger(__name__)

_KEY_POSITION     = "executor:position"
_KEY_INTENT       = "executor:pending_intent"
_KEY_LAST_SIG_TS  = "executor:last_signal_ts"
_DAY_TTL_SECS     = 8 * 3600   # position key lives at most 8 hours (full trading day)

_KEY_DAILY_PNL_PREFIX   = "executor:daily_pnl:"      # + date_str (YYYY-MM-DD)
_KEY_NO_MORE_PREFIX     = "executor:entries_blocked:" # + date_str
_DAILY_KEY_TTL_SECS     = 86400

_KEY_PAPER_MODE_OVERRIDE = "executor:paper_mode_override"


def _loads_tolerant(raw: Any) -> Any:
    """Decode a Redis JSON value, tolerating a double-encoded payload.

    The signal bot (repo 1) writes the intent via the Upstash REST API and
    json-encodes the body twice, so a single json.loads yields a str rather
    than a dict. Unwrap successive string layers until we reach a container
    (or the value stops being parseable).
    """
    val = json.loads(raw)
    depth = 0
    while isinstance(val, str) and depth < 3:
        try:
            val = json.loads(val)
        except (ValueError, TypeError):
            break
        depth += 1
    return val


# ── Position ──────────────────────────────────────────────────────────────────

def load_position(r: redis_lib.Redis) -> Optional[dict]:
    raw = r.get(_KEY_POSITION)
    if not raw:
        return None
    return json.loads(raw)


def save_position(r: redis_lib.Redis, pos: dict) -> None:
    r.set(_KEY_POSITION, json.dumps(pos), ex=_DAY_TTL_SECS)


def delete_position(r: redis_lib.Redis) -> None:
    r.delete(_KEY_POSITION)
    log.info("state: position cleared")


def committed_premium(r: redis_lib.Redis) -> float:
    """
    Sum of entry_price * qty for all currently-open executor positions.
    Mirrors Repo 1's paper_engine._committed_premium(). Given Repo 2's
    single-instrument, single-position-at-a-time gate (_check_no_open_position),
    this will be 0 whenever try_enter() is reachable — implemented for parity
    and to avoid silent divergence if multi-instrument support is added later.
    """
    pos = load_position(r)
    if not pos:
        return 0.0
    return (pos.get("entry_premium") or 0.0) * (pos.get("qty") or 0)


# ── Pending intent ─────────────────────────────────────────────────────────────

def load_intent(r: redis_lib.Redis) -> Optional[dict]:
    raw = r.get(_KEY_INTENT)
    if not raw:
        return None
    return _loads_tolerant(raw)


def consume_intent(r: redis_lib.Redis) -> Optional[dict]:
    """
    Atomically read + delete the pending intent.
    Records the intent ts as last_signal_ts (for cooldown gate).
    Returns None if no intent present.
    """
    raw = r.get(_KEY_INTENT)
    if not raw:
        return None
    intent = _loads_tolerant(raw)
    r.delete(_KEY_INTENT)
    r.set(_KEY_LAST_SIG_TS, intent.get("ts", ""), ex=_DAY_TTL_SECS)
    log.info("state: intent consumed ts=%s", intent.get("ts"))
    return intent


def discard_intent(r: redis_lib.Redis, reason: str) -> None:
    """Delete intent without consuming (gate failed)."""
    raw = r.get(_KEY_INTENT)
    if not raw:
        return
    intent = _loads_tolerant(raw)
    r.delete(_KEY_INTENT)
    r.set(_KEY_LAST_SIG_TS, intent.get("ts", ""), ex=_DAY_TTL_SECS)
    log.info("state: intent discarded reason=%s ts=%s", reason, intent.get("ts"))


def get_last_signal_ts(r: redis_lib.Redis) -> Optional[str]:
    raw = r.get(_KEY_LAST_SIG_TS)
    return raw.decode() if raw else None


# ── Helpers ────────────────────────────────────────────────────────────────────

def fresh_position_from_intent(intent: dict) -> dict:
    """
    Bootstrap an empty position dict from a signal intent.
    Phase is ENTERING; numeric fields are populated after entry fill.
    """
    return {
        "phase":               "ENTERING",
        "intent_ts":           intent["ts"],
        "instrument":          intent["instrument"],
        "direction":           intent["direction"],
        "tradingsymbol":       intent["tradingsymbol"],
        "atm_strike":          intent["atm_strike"],
        "spot_risk_pts":       intent["spot_risk_pts"],
        "atm_delta":           intent.get("atm_delta", 0.50),
        # filled on entry
        "entry_premium":       None,
        "entry_spot":          None,
        "sl_premium":          None,
        "target_premium":      None,
        "qty":                 None,
        "entry_order_id":      None,
        "sl_order_id":         None,
        "target_order_id":     None,
        # milestone tracking
        "breakeven_hit":       False,
        "peak_premium":        None,
        # health
        "last_health_ts":      None,
        "last_health_score":   0,
        "vwap_lost_consec":    0,
        # timing
        "entry_ts":            None,
        "cooldown_start_ts":   None,
        # exit
        "exit_reason":         None,
        "exit_premium":        None,
        "pnl":                 None,
        # reconcile flags (cleared each run after handling)
        "sl_filled":           False,
        "sl_fill_price":       None,
        "target_filled":       False,
        "target_fill_price":   None,
        "position_flat_external": False,
    }


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Daily-loss circuit breaker ──────────────────────────────────────────────────

def _daily_pnl_key(date_str: str) -> str:
    return f"{_KEY_DAILY_PNL_PREFIX}{date_str}"


def _no_more_key(date_str: str) -> str:
    return f"{_KEY_NO_MORE_PREFIX}{date_str}"


def get_daily_pnl(r: redis_lib.Redis, date_str: str) -> float:
    raw = r.get(_daily_pnl_key(date_str))
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def update_daily_pnl(r: redis_lib.Redis, date_str: str, delta: float) -> float:
    """Add delta to today's cumulative P&L and return the new total."""
    new_val = get_daily_pnl(r, date_str) + delta
    r.set(_daily_pnl_key(date_str), str(round(new_val, 2)), ex=_DAILY_KEY_TTL_SECS)
    return new_val


def entries_blocked(r: redis_lib.Redis, date_str: str) -> bool:
    """True if the daily-loss breaker has tripped today."""
    return r.exists(_no_more_key(date_str)) == 1


def block_entries(r: redis_lib.Redis, date_str: str, reason: str) -> None:
    r.set(_no_more_key(date_str), reason, ex=_DAILY_KEY_TTL_SECS)
    log.info("entries BLOCKED for %s: %s", date_str, reason)


def block_reason(r: redis_lib.Redis, date_str: str) -> Optional[str]:
    raw = r.get(_no_more_key(date_str))
    return raw.decode() if raw else None


# ── PAPER_MODE runtime override ─────────────────────────────────────────────────

def get_paper_mode_override(r: redis_lib.Redis) -> Optional[bool]:
    """
    Redis-backed runtime toggle for PAPER_MODE.  Returns None if no override is
    set (caller should fall back to the PAPER_MODE env var / config default).
    No TTL — persists until explicitly set or cleared.
    """
    raw = r.get(_KEY_PAPER_MODE_OVERRIDE)
    if raw is None:
        return None
    val = raw.decode() if isinstance(raw, bytes) else raw
    return val.strip().lower() == "true"


def set_paper_mode_override(r: redis_lib.Redis, value: bool) -> None:
    r.set(_KEY_PAPER_MODE_OVERRIDE, "true" if value else "false")
    log.info("state: PAPER_MODE override set to %s", "PAPER" if value else "LIVE")


def clear_paper_mode_override(r: redis_lib.Redis) -> None:
    r.delete(_KEY_PAPER_MODE_OVERRIDE)
    log.info("state: PAPER_MODE override cleared — falling back to env/config default")
