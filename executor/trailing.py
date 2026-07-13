"""
Ladder SL — verbatim port of Repo 1's src/position_tracker.py
compute_ladder_sl / compute_ai_adjusted_sl / compute_final_sl.
Do not let this drift from the source; premium space only.
"""
from __future__ import annotations

import math
import logging

log = logging.getLogger(__name__)


def compute_ladder_sl(entry: float, T: float, current_price: float,
                       direction: str, prior_sl: float) -> float:
    """Monotonic trailing SL via a mechanical progress ladder.

    direction must be "CE" or "PE" (case-insensitive). Anything else raises
    ValueError — this is a programming bug, not bad market data.

    T must be > 0. If T <= 0 or current_price is None, returns prior_sl
    unchanged and logs a warning.

    Ladder (sl_fraction applied when progress reaches each threshold):
      progress >= 0.5      → sl_fraction = 0.25
      progress >= 0.9      → sl_fraction = 0.60
      progress >= 1.0      → sl_fraction = 0.90
      progress >= 1.0+0.1n → sl_fraction = 0.90 + 0.10*n  (n>=1, each +0.1T step)

    sl_price = entry + sl_fraction * T  (CE)
             = entry - sl_fraction * T  (PE)

    Final return is monotonically non-decreasing (CE) / non-increasing (PE)
    relative to prior_sl.
    """
    direction = direction.upper()
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")
    if T is None or T <= 0:
        log.warning("compute_ladder_sl: T=%r invalid — returning prior_sl", T)
        return prior_sl
    if current_price is None:
        log.warning("compute_ladder_sl: current_price is None — returning prior_sl")
        return prior_sl

    if direction == "CE":
        progress = (current_price - entry) / T
    else:
        progress = (entry - current_price) / T

    if progress < 0.5:
        return prior_sl
    if progress < 0.9:
        sl_fraction = 0.25
    elif progress < 1.0:
        sl_fraction = 0.60
    else:
        n = math.floor(round((progress - 1.0) / 0.1, 9))
        sl_fraction = 0.9 + 0.1 * n

    if direction == "CE":
        sl_price = entry + sl_fraction * T
        return max(sl_price, prior_sl)
    else:
        sl_price = entry - sl_fraction * T
        return min(sl_price, prior_sl)


def _rsi_reversing(direction: str, rsi_values: list) -> bool:
    """Return True if the RSI 3-point staircase is reversing against the trade."""
    r0, r1, r2 = rsi_values[0], rsi_values[1], rsi_values[2]
    if direction.upper() == "CE":
        return r1 < r0 and r2 < r1
    return r1 > r0 and r2 > r1


def compute_ai_adjusted_sl(ladder_sl: float, direction: str, market_snapshot: dict) -> float:
    """Rule-based heuristic that may ONLY tighten the SL vs the ladder.

    Deterministic, no LLM call. If RSI shows a 3-point reversal against the
    position's direction AND progress >= 0.7T, tighten SL to
    current_price ∓ 0.05*T.
    """
    direction = direction.upper()
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")

    rsi_values    = market_snapshot.get("rsi_last3")
    progress      = market_snapshot.get("progress", 0.0)
    current_price = market_snapshot.get("current_price")
    T             = market_snapshot.get("T")

    if (rsi_values is None or len(rsi_values) < 3 or progress < 0.7
            or current_price is None or T is None or T <= 0):
        return ladder_sl

    if not _rsi_reversing(direction, rsi_values):
        return ladder_sl

    if direction == "CE":
        tightened = current_price - 0.05 * T
        return max(tightened, ladder_sl)
    else:
        tightened = current_price + 0.05 * T
        return min(tightened, ladder_sl)


def compute_final_sl(ladder_sl: float, ai_sl: float, direction: str) -> float:
    """Combine ladder SL and AI-adjusted SL, always taking the tighter side."""
    direction = direction.upper()
    if direction == "CE":
        return max(ladder_sl, ai_sl)
    return min(ladder_sl, ai_sl)
