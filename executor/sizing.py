"""
Position sizing — spec §5.
Inherits risk parameters from signal intent; does not recompute them.
"""

from __future__ import annotations

import logging
import math

import redis as redis_lib

from executor import config
from executor.utils.auth import get_lot_size

log = logging.getLogger(__name__)


def compute_levels(intent: dict, entry_premium: float) -> tuple[float, float, float]:
    """
    Derive SL and target premiums from the entry fill price.
    Returns (premium_risk, sl_premium, target_premium).
    Spec §5 formulas:
      premium_risk   = spot_risk_pts × ATM_DELTA
      sl_premium     = entry_premium − premium_risk
      target_premium = entry_premium + premium_risk × TARGET_RR
    """
    premium_risk   = intent["spot_risk_pts"] * intent.get("atm_delta", config.ATM_DELTA)
    sl_premium     = round(entry_premium - premium_risk, 2)
    target_premium = round(entry_premium + premium_risk * config.TARGET_RR, 2)
    # SL floor: option premium can't be negative
    sl_premium = max(sl_premium, 0.05)
    return premium_risk, sl_premium, target_premium


def compute_qty(
    r: redis_lib.Redis,
    tradingsymbol: str,
    premium_risk: float,
) -> int:
    """
    Compute quantity (in units = lots × lot_size).
    Spec §5:
      max_lots = floor(CAPITAL_RS × RISK_PCT / (premium_risk × lot_size))
      qty      = max_lots × lot_size   (minimum 1 lot)
    Returns 0 if even 1 lot exceeds the risk cap.
    """
    lot_size  = get_lot_size(r, tradingsymbol)
    max_risk  = config.CAPITAL_RS * config.RISK_PCT          # e.g. ₹2,000
    max_lots  = math.floor(max_risk / (premium_risk * lot_size))

    if max_lots < 1:
        log.warning(
            "sizing: even 1 lot (lot_size=%d) @ premium_risk=%.2f exceeds risk cap ₹%.0f — skip",
            lot_size, premium_risk, max_risk,
        )
        return 0

    qty = max_lots * lot_size
    log.info("sizing: max_lots=%d lot_size=%d qty=%d premium_risk=%.2f max_risk=₹%.0f",
             max_lots, lot_size, qty, premium_risk, max_risk)
    return qty
