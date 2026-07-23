"""
Position sizing — spec §5.
Inherits risk parameters from signal intent; does not recompute them.
Fixed-lot, capital-availability model — mirrors Repo 1's paper_engine.py,
except the capital figure diverges by mode (paper: fixed CAPITAL_RS,
live: real-time available margins via KiteClient.get_margins()).
"""

from __future__ import annotations

import logging

import redis as redis_lib

from executor import config, state
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
    entry_ltp: float,
    paper_mode: bool,
    kite: "KiteClient | None" = None,
    lot_multiplier: int = 1,
) -> int:
    """
    Fixed-lot sizing, mirroring Repo 1's paper_engine.py exactly.
    Base size is 1 lot (lot_size from shared instrument cache — auto-syncs
    with Repo 1's INDEX_LOT_SIZES / stock_config lot tables since both write
    to the same Redis instrument cache); final qty = lot_size * lot_multiplier.

    Capital-availability gate:
      - paper_mode=True  -> use fixed CAPITAL_RS (== Repo 1's DAILY_CAPITAL)
      - paper_mode=False -> use live available capital from kite.get_margins()

    `lot_multiplier` — always 1 in paper mode (callers must pass it
    explicitly, do not rely on the default silently). In live mode it is the
    once-per-day multiplier decided in run.py's main() (see
    state.get_lot_multiplier / set_lot_multiplier_if_absent).

    Returns 0 (skip entry) if entry_cost > remaining capital.
    """
    lot_size = get_lot_size(r, tradingsymbol)
    qty = lot_size * lot_multiplier

    if paper_mode:
        capital = config.CAPITAL_RS
    else:
        if kite is None:
            log.error("sizing: live mode requires a KiteClient for margins() — skip")
            return 0
        capital = kite.get_margins(r)

    committed = state.committed_premium(r)          # mirrors Repo 1 _committed_premium()
    remaining = capital - committed
    entry_cost = entry_ltp * qty

    if entry_cost > remaining:
        log.warning(
            "sizing: capital exhausted (need ₹%.0f, have ₹%.0f) — skip",
            entry_cost, remaining,
        )
        return 0

    log.info("sizing: lot_size=%d multiplier=%d qty=%d entry_cost=₹%.0f remaining=₹%.0f",
              lot_size, lot_multiplier, qty, entry_cost, remaining)
    return qty


def decide_lot_multiplier(capital: float) -> int:
    """
    Once-per-day lot-multiplier decision (live mode only — see run.py's
    main()). capital < threshold -> 1, capital >= threshold -> 2.
    """
    if capital >= config.LOT_MULTIPLIER_CAPITAL_THRESHOLD:
        return 2
    return 1


def get_daily_loss_limit(
    paper_mode: bool,
    kite: "KiteClient | None" = None,
    r: redis_lib.Redis | None = None,
) -> float:
    """
    -15% of capital. Paper mode: fixed CAPITAL_RS (mirrors Repo 1).
    Live mode: -15% of live available margins (kite.get_margins()).
    `r` (optional) enables the short-TTL Redis cache on the margins() call.
    """
    if paper_mode:
        capital = config.CAPITAL_RS
    else:
        if kite is None:
            log.error("get_daily_loss_limit: live mode requires a KiteClient — falling back to CAPITAL_RS")
            capital = config.CAPITAL_RS
        else:
            capital = kite.get_margins(r)
    return -(capital * config.DAILY_LOSS_PCT)
