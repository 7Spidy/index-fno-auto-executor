"""
Entry gate — spec §4.  All conditions must pass; if any fails the intent is
discarded (not retried) and the gate returns (False, reason).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import redis as redis_lib

from executor import config
from executor.utils import auth
from executor.utils.calendar_nse import now_ist, ist_hhmm
from executor.utils.kite_client import KiteClient

log = logging.getLogger(__name__)


def check_all(
    intent: dict,
    r: redis_lib.Redis,
    kite: KiteClient,
    exchange: str,
) -> tuple[bool, str]:
    """
    Run all entry gate checks in spec order.
    Returns (True, "") on pass, or (False, reason_string) on first failure.
    """
    from executor.state import entries_blocked, block_reason

    # 0. Daily-loss circuit breaker — checked first, mirrors Repo 1
    date_str = now_ist().strftime("%Y-%m-%d")
    if entries_blocked(r, date_str):
        reason = block_reason(r, date_str) or "entries blocked"
        return False, reason

    # 1. Cooldown — no new entry within COOLDOWN_CANDLES × 5 min of last signal
    #    for THIS instrument (mirrors Repo 1's per-instrument cooldown:{name}:{direction}
    #    key in main.py/stock_main.py — cooldown is not global across instruments).
    ok, reason = _check_cooldown(r, intent.get("instrument", ""))
    if not ok:
        return False, reason

    # 2. No open position for this instrument (caller already checks
    #    executor:position:{instrument} is absent, but we double-check here
    #    for safety)
    ok, reason = _check_no_open_position(r, intent.get("instrument", ""))
    if not ok:
        return False, reason

    # 3. Time window + intent freshness
    ok, reason = _check_time(intent)
    if not ok:
        return False, reason

    # 4. Option tradable
    ok, reason = _check_option_tradable(intent, r, kite, exchange)
    if not ok:
        return False, reason

    return True, ""


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_cooldown(r: redis_lib.Redis, instrument: str) -> tuple[bool, str]:
    from executor.state import get_last_signal_ts
    last_ts_str = get_last_signal_ts(r, instrument)
    if not last_ts_str:
        return True, ""
    try:
        last_ts = datetime.fromisoformat(last_ts_str)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return True, ""
    cooldown_secs = config.COOLDOWN_CANDLES * 5 * 60
    elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
    if elapsed < cooldown_secs:
        remaining = int(cooldown_secs - elapsed)
        return False, f"cooldown: {remaining}s remaining (last signal {int(elapsed)}s ago)"
    log.info("gate cooldown elapsed=%.0fs  OK", elapsed)
    return True, ""


def _check_no_open_position(r: redis_lib.Redis, instrument: str) -> tuple[bool, str]:
    from executor.state import load_position
    pos = load_position(r, instrument)
    if pos and pos.get("phase") not in (None, "COOLDOWN"):
        return False, f"position already open (phase={pos.get('phase')})"
    return True, ""


def _check_time(intent: dict) -> tuple[bool, str]:
    now = now_ist()

    # Current time must be in 09:40–14:45
    window_start = ist_hhmm(config.EVAL_WINDOW_START, now)
    no_entry     = ist_hhmm(config.NO_NEW_ENTRY, now)
    if not (window_start <= now <= no_entry):
        return False, f"current time {now.strftime('%H:%M')} outside 09:40–14:45"

    # Intent must not be older than INTENT_TTL_MIN
    try:
        intent_ts = datetime.fromisoformat(intent["ts"])
        if intent_ts.tzinfo is None:
            intent_ts = intent_ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - intent_ts).total_seconds() / 60
        if age_min > config.INTENT_TTL_MIN:
            return False, f"intent stale: {age_min:.1f} min old (max {config.INTENT_TTL_MIN})"
    except (KeyError, ValueError) as exc:
        return False, f"intent ts parse error: {exc}"

    log.info("gate time OK age=%.1f min", age_min)
    return True, ""


def _check_option_tradable(
    intent: dict,
    r: redis_lib.Redis,
    kite: KiteClient,
    exchange: str,
) -> tuple[bool, str]:
    ts = intent.get("tradingsymbol", "")
    if not ts:
        return False, "tradingsymbol missing from intent"

    # Check instrument exists in the shared token cache
    try:
        token_cache = auth.get_option_cache(r)
    except RuntimeError as exc:
        return False, str(exc)

    if ts not in token_cache:
        return False, f"{ts} not found in option token cache"

    # Check LTP is non-zero
    try:
        ltp_map = kite.get_ltp([f"{exchange}:{ts}"])
        ltp = ltp_map.get(f"{exchange}:{ts}", 0.0)
    except Exception as exc:
        return False, f"LTP fetch for {ts} failed: {exc}"

    if ltp <= 0:
        return False, f"{ts} LTP is zero — not tradable"

    log.info("gate option tradable %s LTP=%.2f  OK", ts, ltp)
    return True, ""
