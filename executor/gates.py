"""
Entry gate — spec §4.  All conditions must pass; if any fails the intent is
discarded (not retried) and the gate returns (False, reason).

Manual kill switch (executor:kill_switch, Redis string "true"/"false", no
TTL) — checked first in check_all() as a cheap short-circuit before any Kite
API calls. Set/clear manually via redis-cli or an ad-hoc script:
  redis-cli -u $REDIS_URL SET executor:kill_switch true
  redis-cli -u $REDIS_URL SET executor:kill_switch false
Blocks NEW ENTRIES ONLY — positions already OPEN continue through their
normal SL/target/trailing/exit lifecycle untouched; this does not force a
squareoff. See state.get_kill_switch / state.set_kill_switch.
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
    from executor.state import entries_blocked, block_reason, get_kill_switch

    # 0a. Manual kill switch — checked first of all, cheapest short-circuit
    # (avoids unnecessary Kite API calls for cooldown/time/tradability checks).
    if get_kill_switch(r):
        return False, "kill_switch_active"

    # 0b. Daily-loss circuit breaker — mirrors Repo 1
    date_str = now_ist().strftime("%Y-%m-%d")
    if entries_blocked(r, date_str):
        reason = block_reason(r, date_str) or "entries blocked"
        return False, reason

    # 0c. Dynamic direction-restriction safety net (defense in depth). Repo 2
    # doesn't recompute signals — it should not blindly trust a single
    # upstream field without a cross-check. Should never actually fire in
    # practice (Repo 1 only ever generates the matching direction for a
    # dynamic pick).
    if intent.get("is_dynamic"):
        restriction = intent.get("direction_restriction")
        direction   = intent.get("direction")
        if (restriction == "CE_ONLY" and direction != "CE") or \
           (restriction == "PE_ONLY" and direction != "PE"):
            return False, "dynamic direction_restriction violated"

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

    # Static-cache membership check is skipped for dynamic intents — a
    # dynamic stock's tradingsymbol legitimately never appears in the shared
    # static cache (kite:stock_option_tokens is only ever populated for the
    # static 14 stocks). The live LTP check below remains mandatory and
    # unchanged for both static and dynamic instruments.
    if not intent.get("is_dynamic"):
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
