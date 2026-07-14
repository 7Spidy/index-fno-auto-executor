"""
Redis state layer — spec §13 (idempotency, startup reconcile, single-position lock).

Keys managed here (all namespaced per instrument — see claude_change_spec_repo2.md
Step 8; this repo runs all 17 instruments concurrently, not a single implicit
position):
  executor:position:{instrument}        — serialised PositionState dict (TTL: end of day)
  executor:pending_intent:{instrument}  — trade intent from signal bot (TTL set by signal bot, 6 min)
  executor:open_instruments             — JSON list of instruments with an open position
  executor:last_signal_ts:{instrument}  — ISO ts of last consumed intent (per-instrument
                                           cooldown gate — mirrors Repo 1's
                                           cooldown:{name}:{direction} key in main.py/
                                           stock_main.py, which is per-instrument too)

Position dict schema (all phases share the same key; unused fields are None):
  phase              str   IDLE|ENTERING|OPEN|EXITING|COOLDOWN
  intent_ts          str   ISO timestamp of the signal that created this trade
  instrument         str   e.g. "NIFTY"
  direction          str   "CE" | "PE"
  tradingsymbol      str   e.g. "NIFTY2560824500CE"
  atm_strike         int
  spot_risk_pts      float
  atm_delta          float
  entry_premium      float  (set on fill)
  entry_spot         float  underlying spot at entry fill time
  entry_limit_price  float  marketable-LIMIT price the entry order was placed at
  sl_premium         float  current SL (ratcheted)
  sl_ladder_stage    float  ladder's running SL state (mirrors Repo 1 field name)
  target_t           float | None  option-premium-space target distance (ladder's T
                                    denominator only — never a take-profit order)
  initial_sl         float  SL derived at entry fill, before any ladder trailing
  qty                int
  entry_order_id     str
  sl_order_id        str
  breakeven_hit       bool
  last_health_ts      str | None  (unused — health engine removed; kept for schema stability)
  entry_ts            str   ISO ts of entry fill
  cooldown_start_ts   str | None  ISO ts when COOLDOWN phase began
  exit_reason         str | None
  exit_premium        float | None
  pnl                 float | None
  sl_filled            bool  (set by reconcile)
  sl_fill_price        float | None
  position_flat_external  bool  (set by reconcile on live mismatch)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import redis as redis_lib

log = logging.getLogger(__name__)

_KEY_POSITION_PREFIX     = "executor:position:"        # + instrument name
_KEY_INTENT_PREFIX       = "executor:pending_intent:"  # + instrument name
_KEY_OPEN_INDEX          = "executor:open_instruments"  # json list, mirrors Repo1's paper index
_KEY_LAST_SIG_TS_PREFIX  = "executor:last_signal_ts:"   # + instrument name
_DAY_TTL_SECS     = 8 * 3600   # position key lives at most 8 hours (full trading day)

_KEY_DAILY_PNL_PREFIX   = "executor:daily_pnl:"      # + date_str (YYYY-MM-DD)
_KEY_NO_MORE_PREFIX     = "executor:entries_blocked:" # + date_str
_DAILY_KEY_TTL_SECS     = 86400

_KEY_CLOSED_TODAY_PREFIX = "executor:closed_today:"    # + date_str (YYYY-MM-DD), Redis list
_KEY_DISCORD_MSG_ID_PREFIX = "executor:discord_msg_id:" # + date_str (YYYY-MM-DD)

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

def _position_key(instrument: str) -> str:
    return f"{_KEY_POSITION_PREFIX}{instrument.upper()}"


def load_position(r: redis_lib.Redis, instrument: str) -> Optional[dict]:
    raw = r.get(_position_key(instrument))
    if not raw:
        return None
    return json.loads(raw)


def save_position(r: redis_lib.Redis, instrument: str, pos: dict) -> None:
    r.set(_position_key(instrument), json.dumps(pos), ex=_DAY_TTL_SECS)
    _add_to_open_index(r, instrument)


def delete_position(r: redis_lib.Redis, instrument: str) -> None:
    r.delete(_position_key(instrument))
    _remove_from_open_index(r, instrument)
    log.info("state: position cleared for %s", instrument)


# ── Open-instrument index ────────────────────────────────────────────────────

def list_open_instruments(r: redis_lib.Redis) -> list[str]:
    raw = r.get(_KEY_OPEN_INDEX)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_open_index(r: redis_lib.Redis, instruments: list[str]) -> None:
    r.set(_KEY_OPEN_INDEX, json.dumps(instruments), ex=_DAY_TTL_SECS)


def _add_to_open_index(r: redis_lib.Redis, instrument: str) -> None:
    instrument = instrument.upper()
    idx = list_open_instruments(r)
    if instrument not in idx:
        idx.append(instrument)
        _save_open_index(r, idx)


def _remove_from_open_index(r: redis_lib.Redis, instrument: str) -> None:
    instrument = instrument.upper()
    idx = list_open_instruments(r)
    if instrument in idx:
        idx.remove(instrument)
        _save_open_index(r, idx)


def committed_premium(r: redis_lib.Redis) -> float:
    """
    Sum of entry_price * qty for all currently-open executor positions,
    across all 17 instruments. Mirrors Repo 1's paper_engine._committed_premium().
    """
    total = 0.0
    for instrument in list_open_instruments(r):
        pos = load_position(r, instrument)
        if pos:
            total += (pos.get("entry_premium") or 0.0) * (pos.get("qty") or 0)
    return total


# ── Pending intent ─────────────────────────────────────────────────────────────

def _intent_key(instrument: str) -> str:
    return f"{_KEY_INTENT_PREFIX}{instrument.upper()}"


def load_intent(r: redis_lib.Redis, instrument: str) -> Optional[dict]:
    raw = r.get(_intent_key(instrument))
    if not raw:
        return None
    return _loads_tolerant(raw)


def consume_intent(r: redis_lib.Redis, instrument: str) -> Optional[dict]:
    """
    Atomically read + delete the pending intent for this instrument.
    Records the intent ts as this instrument's last_signal_ts (for cooldown gate).
    Returns None if no intent present.
    """
    raw = r.get(_intent_key(instrument))
    if not raw:
        return None
    intent = _loads_tolerant(raw)
    r.delete(_intent_key(instrument))
    r.set(_last_signal_ts_key(instrument), intent.get("ts", ""), ex=_DAY_TTL_SECS)
    log.info("state: intent consumed instrument=%s ts=%s", instrument, intent.get("ts"))
    return intent


def discard_intent(r: redis_lib.Redis, instrument: str, reason: str) -> None:
    """Delete intent without consuming (gate failed)."""
    raw = r.get(_intent_key(instrument))
    if not raw:
        return
    intent = _loads_tolerant(raw)
    r.delete(_intent_key(instrument))
    r.set(_last_signal_ts_key(instrument), intent.get("ts", ""), ex=_DAY_TTL_SECS)
    log.info("state: intent discarded instrument=%s reason=%s ts=%s",
              instrument, reason, intent.get("ts"))


def _last_signal_ts_key(instrument: str) -> str:
    return f"{_KEY_LAST_SIG_TS_PREFIX}{instrument.upper()}"


def get_last_signal_ts(r: redis_lib.Redis, instrument: str) -> Optional[str]:
    raw = r.get(_last_signal_ts_key(instrument))
    return raw.decode() if isinstance(raw, bytes) else raw


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
        "entry_limit_price":   None,
        "sl_premium":          None,
        "sl_ladder_stage":     None,
        "target_t":            None,
        "initial_sl":          None,
        "qty":                 None,
        "entry_order_id":      None,
        "sl_order_id":         None,
        # milestone tracking
        "breakeven_hit":       False,
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
    return raw.decode() if isinstance(raw, bytes) else raw


# ── Closed-today list + Discord consolidated tracker message-ID ────────────────

def _closed_today_key(date_str: str) -> str:
    return f"{_KEY_CLOSED_TODAY_PREFIX}{date_str}"


def append_closed_today(r: redis_lib.Redis, date_str: str, record: dict) -> None:
    """Append one closed-trade record to today's list. TTL refreshed on every
    write so the list survives until end of day regardless of write order."""
    key = _closed_today_key(date_str)
    r.rpush(key, json.dumps(record))
    r.expire(key, _DAILY_KEY_TTL_SECS)


def get_closed_today(r: redis_lib.Redis, date_str: str) -> list[dict]:
    raw_list = r.lrange(_closed_today_key(date_str), 0, -1)
    out: list[dict] = []
    for raw in raw_list or []:
        try:
            out.append(_loads_tolerant(raw))
        except Exception:
            continue
    return out


def _discord_msg_id_key(date_str: str) -> str:
    return f"{_KEY_DISCORD_MSG_ID_PREFIX}{date_str}"


def get_discord_msg_id(r: redis_lib.Redis, date_str: str) -> Optional[str]:
    raw = r.get(_discord_msg_id_key(date_str))
    return raw.decode() if isinstance(raw, bytes) else raw


def set_discord_msg_id(r: redis_lib.Redis, date_str: str, msg_id: str) -> None:
    r.set(_discord_msg_id_key(date_str), msg_id, ex=_DAILY_KEY_TTL_SECS)


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
