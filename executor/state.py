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


# ── Pending intent ─────────────────────────────────────────────────────────────

def load_intent(r: redis_lib.Redis) -> Optional[dict]:
    raw = r.get(_KEY_INTENT)
    if not raw:
        return None
    return json.loads(raw)


def consume_intent(r: redis_lib.Redis) -> Optional[dict]:
    """
    Atomically read + delete the pending intent.
    Records the intent ts as last_signal_ts (for cooldown gate).
    Returns None if no intent present.
    """
    raw = r.get(_KEY_INTENT)
    if not raw:
        return None
    intent = json.loads(raw)
    r.delete(_KEY_INTENT)
    r.set(_KEY_LAST_SIG_TS, intent.get("ts", ""), ex=_DAY_TTL_SECS)
    log.info("state: intent consumed ts=%s", intent.get("ts"))
    return intent


def discard_intent(r: redis_lib.Redis, reason: str) -> None:
    """Delete intent without consuming (gate failed)."""
    raw = r.get(_KEY_INTENT)
    if not raw:
        return
    intent = json.loads(raw)
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
