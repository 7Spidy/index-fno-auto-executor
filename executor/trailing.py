"""
Trailing stop helpers — spec §9 (caution tier) and §13 (ratchet invariant).

All money is in option premium (₹ per unit).
All favourability is on NIFTY spot (passed in as candle data).
"""

from __future__ import annotations

import logging

import pandas as pd

from executor import config
from executor.utils.indicators import compute_atr

log = logging.getLogger(__name__)


# ── Ratchet invariant (spec §13) ──────────────────────────────────────────────

def ratchet(current_sl: float, proposed_sl: float) -> float:
    """
    SL can only ever move toward profit (i.e. upward for a long premium trade).
    Rejects any proposal that loosens the stop.  Spec §13 hard invariant.
    """
    if proposed_sl < current_sl:
        log.debug("ratchet: rejected sl=%.2f (current=%.2f) — stop can only tighten",
                  proposed_sl, current_sl)
        return current_sl
    return round(proposed_sl, 2)


# ── Milestone SL levels (spec §8) ─────────────────────────────────────────────

def breakeven_sl(entry_premium: float) -> float:
    return round(entry_premium, 2)


def lock_sl(entry_premium: float, t: float) -> float:
    """SL at ~70% of the move locked in.  spec §8 LOCK milestone."""
    return round(entry_premium + config.LOCK_FRACTION * t, 2)


def runner_trail_sl(peak_premium: float, past_target: bool) -> float:
    """
    Runner trailing stop — spec §8.
    trail_sl = peak × 0.90 (normal) or peak × 0.95 (past original target).
    """
    factor = (1 - config.RUNNER_GIVEBACK_LATE) if past_target else (1 - config.RUNNER_GIVEBACK)
    return round(peak_premium * factor, 2)


# ── Caution-tier trailing (spec §9) ───────────────────────────────────────────

def caution_sl_from_candles(
    candles: pd.DataFrame,   # last 20 5-min NIFTY spot candles, oldest→newest
    direction: str,          # "CE" (long) | "PE" (short)
    entry_premium: float,
    entry_spot: float,
    atm_delta: float,
    current_sl: float,
) -> float:
    """
    Compute caution-tier trailing SL.
    1. Find swing level = min(low) of last 3 completed candles (CE) or max(high) (PE).
    2. Apply 0.1×ATR buffer away from position.
    3. Convert spot level to option premium via delta.
    4. Apply ratchet invariant.

    Conversion: premium_sl = entry_premium − (entry_spot − spot_sl_level) × delta
    This is approximate (delta changes with spot) but matches spec intent.
    """
    n = config.CAUTION_TRAIL_SWINGS   # 3
    if len(candles) < n:
        log.warning("caution_trail: not enough candles (%d < %d) — keeping current SL",
                    len(candles), n)
        return current_sl

    recent = candles.iloc[-n:]
    atr_series = compute_atr(candles)
    atr = atr_series.iloc[-1]

    if direction == "CE":
        swing_level = recent["low"].min() - config.CAUTION_ATR_BUFFER * atr
    else:
        swing_level = recent["high"].max() + config.CAUTION_ATR_BUFFER * atr

    # Convert spot swing level to premium space
    spot_delta = entry_spot - swing_level   # positive for CE (spot dropped below swing)
    premium_sl = entry_premium - spot_delta * atm_delta
    premium_sl = round(max(premium_sl, 0.05), 2)

    result = ratchet(current_sl, premium_sl)
    log.info(
        "caution_trail: dir=%s swing=%.1f atr=%.1f raw_premium_sl=%.2f → ratcheted=%.2f",
        direction, swing_level, atr, premium_sl, result,
    )
    return result
