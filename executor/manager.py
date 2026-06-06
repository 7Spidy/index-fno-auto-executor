"""
Manager — state machine driver.  Spec §16 (state machine), §6-§13 (rules).

Each public function handles one phase transition or intra-phase management.
All functions mutate the position dict in place and call state.save_position().

State machine phases:
  IDLE → ENTERING → OPEN_FIXED ↔ LOCKED / RUNNER → EXITING → COOLDOWN → IDLE

Exit priority order (spec §12):
  1. SL-M hit on exchange
  2. Target hit (non-runner)
  3. Runner give-back
  4. Health < 50
  5. VWAP lost ≥ 2 consecutive
  6. Reversal
  7. Theta time-stop
  8. Hard square-off 15:10
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import redis as redis_lib

from executor import config, state, sizing, health, trailing
from executor.gateway.base import OrderGateway
from executor.utils.calendar_nse import now_ist, IST

log = logging.getLogger(__name__)

_OPEN_PHASES = ("ENTERING", "OPEN_FIXED", "LOCKED", "RUNNER", "EXITING")


# ── Entry flow ────────────────────────────────────────────────────────────────

def try_enter(
    intent: dict,
    gateway: OrderGateway,
    r: redis_lib.Redis,
) -> None:
    """
    Called when gate passes.  Places entry MARKET order and transitions to ENTERING.
    Qty is computable from premium_risk (which does not depend on fill price).
    In paper mode the order fills immediately; check_entry_fill() is called next.
    """
    ts = intent.get("tradingsymbol", "")
    log.info("manager: entering %s direction=%s", ts, intent.get("direction"))

    pos = state.fresh_position_from_intent(intent)

    # Compute qty upfront — premium_risk = spot_risk_pts × ATM_DELTA (spec §5)
    premium_risk = intent["spot_risk_pts"] * intent.get("atm_delta", config.ATM_DELTA)
    qty = sizing.compute_qty(r, ts, premium_risk)
    if qty == 0:
        log.warning("manager: sizing returned 0 — aborting entry")
        return

    order_id = gateway.place_order(
        tradingsymbol=ts,
        exchange="NFO",
        transaction_type="BUY",
        quantity=qty,
        order_type="MARKET",
        product="MIS",
        tag="executor_entry",
    )
    pos["entry_order_id"] = order_id
    pos["qty"]            = qty
    state.save_position(r, pos)
    log.info("manager: entry order placed %s qty=%d", order_id, qty)


def check_entry_fill(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    spot_ltp: float,
    option_ltp: float,
) -> None:
    """
    Check if the entry order has filled.
    On fill: compute SL/target levels, size the position, place SL+target orders,
    transition to OPEN_FIXED.
    """
    oid = pos.get("entry_order_id")
    if not oid:
        log.error("manager: ENTERING but no entry_order_id in state")
        return

    result = gateway.get_order_status(oid)
    if result.status != "COMPLETE":
        log.info("manager: entry order %s still %s — waiting", oid, result.status)
        return

    # Entry filled — spec §5 level derivation uses fill LTP
    entry_premium = result.filled_price
    if not entry_premium:
        log.error("manager: entry fill price is None — aborting entry")
        state.delete_position(r)
        return

    intent_stub = {
        "spot_risk_pts": pos["spot_risk_pts"],
        "atm_delta":     pos["atm_delta"],
    }
    premium_risk, sl_premium, target_premium = sizing.compute_levels(intent_stub, entry_premium)

    qty = pos.get("qty")
    if not qty or qty == 0:
        log.warning("manager: qty missing from state — aborting entry, cancelling order")
        gateway.cancel_order(oid)
        state.delete_position(r)
        return

    now_utc = state.now_utc_iso()

    # Place SL-M order (spec §14)
    sl_oid = gateway.place_order(
        tradingsymbol=pos["tradingsymbol"],
        exchange="NFO",
        transaction_type="SELL",
        quantity=qty,
        order_type="SL-M",
        trigger_price=sl_premium,
        product="MIS",
        tag="executor_sl",
    )

    # Place target LIMIT order (spec §14)
    tgt_oid = gateway.place_order(
        tradingsymbol=pos["tradingsymbol"],
        exchange="NFO",
        transaction_type="SELL",
        quantity=qty,
        order_type="LIMIT",
        price=target_premium,
        product="MIS",
        tag="executor_target",
    )

    pos.update({
        "phase":          "OPEN_FIXED",
        "entry_premium":  round(entry_premium, 2),
        "entry_spot":     round(spot_ltp, 2),
        "sl_premium":     sl_premium,
        "target_premium": target_premium,
        "qty":            qty,
        "sl_order_id":    sl_oid,
        "target_order_id": tgt_oid,
        "peak_premium":   entry_premium,
        "entry_ts":       now_utc,
    })
    state.save_position(r, pos)
    log.info(
        "manager: OPEN_FIXED entry=%.2f sl=%.2f target=%.2f qty=%d  sl_oid=%s tgt_oid=%s",
        entry_premium, sl_premium, target_premium, qty, sl_oid, tgt_oid,
    )


# ── Position management (called every 1-min run) ──────────────────────────────

def manage_position(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    option_ltp: float,
    spot_ltp: float,
    candles_5m: pd.DataFrame,    # last 20 NIFTY spot 5-min candles, oldest→newest
) -> None:
    """
    Main per-minute management tick.  Handles all open phases.
    Spec §6–§13.
    """
    phase = pos["phase"]
    entry = pos["entry_premium"]
    current_sl = pos["sl_premium"]
    target = pos.get("target_premium")
    peak = pos.get("peak_premium", entry)

    T = (pos["spot_risk_pts"] * pos["atm_delta"]) * config.TARGET_RR   # total target distance
    progress = option_ltp - entry

    # Update peak (for runner give-back)
    if option_ltp > peak:
        pos["peak_premium"] = round(option_ltp, 2)
        peak = pos["peak_premium"]

    # ── Reconcile fill flags (set by gateway.reconcile earlier in run.py) ──────
    if pos.get("sl_filled"):
        _exit(gateway, pos, r, "sl_hit", pos.get("sl_fill_price", option_ltp))
        return
    if pos.get("target_filled"):
        _exit(gateway, pos, r, "target_hit", pos.get("target_fill_price", option_ltp))
        return
    if pos.get("position_flat_external"):
        _exit(gateway, pos, r, "flat_external", option_ltp)
        return

    # ── Health rescore (5-min clock) ──────────────────────────────────────────
    health_result = None
    if health.should_rescore(pos.get("last_health_ts")):
        health_result = health.score(candles_5m, pos["direction"], pos["vwap_lost_consec"])
        pos["last_health_score"]  = health_result.effective_score
        pos["vwap_lost_consec"]   = health_result.vwap_lost_consec
        pos["last_health_ts"]     = health_result.candle_ts
        log.info("manager: health rescore score=%d vwap_lost=%d",
                 health_result.effective_score, health_result.vwap_lost_consec)

        # VWAP exit (highest priority after SL fill)
        if health_result.exit_vwap:
            _exit(gateway, pos, r, "vwap_lost_twice", option_ltp)
            return
        # Reversal exit
        if health_result.exit_reversal:
            _exit(gateway, pos, r, "reversal", option_ltp)
            return

    eff_health = pos["last_health_score"]

    # ── Health < 50 → faded → exit (any open phase) ───────────────────────────
    if eff_health < config.HEALTH_CAUTION and phase in ("OPEN_FIXED", "LOCKED"):
        _exit(gateway, pos, r, "health_faded", option_ltp)
        return

    # ── Theta time-stop (spec §10) ────────────────────────────────────────────
    if phase in ("OPEN_FIXED", "LOCKED"):
        entry_ts = datetime.fromisoformat(pos["entry_ts"]).replace(tzinfo=timezone.utc)
        elapsed_min = (datetime.now(timezone.utc) - entry_ts).total_seconds() / 60
        if (elapsed_min >= config.THETA_MINUTES
                and progress < config.THETA_MIN_PROGRESS * T):
            pos["theta_warned"] = True
            log.warning("manager: theta time-stop triggered elapsed=%.1f min progress=%.2f",
                        elapsed_min, progress)
            _exit(gateway, pos, r, "theta_stop", option_ltp)
            return

    # ── Phase-specific milestone and trailing logic ───────────────────────────

    if phase == "OPEN_FIXED":
        _handle_open_fixed(gateway, pos, r, option_ltp, T, progress, eff_health, candles_5m)

    elif phase == "LOCKED":
        _handle_locked(gateway, pos, r, option_ltp, T, target)

    elif phase == "RUNNER":
        _handle_runner(gateway, pos, r, option_ltp, T, target, peak, eff_health)


def _handle_open_fixed(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    ltp: float,
    T: float,
    progress: float,
    eff_health: int,
    candles_5m: pd.DataFrame,
) -> None:
    entry = pos["entry_premium"]
    current_sl = pos["sl_premium"]
    breakeven_hit = pos.get("breakeven_hit", False)

    # ── Caution trailing (health 50-74, 5-min clock) ──────────────────────────
    if config.HEALTH_CAUTION <= eff_health < config.HEALTH_HEALTHY:
        new_sl = trailing.caution_sl_from_candles(
            candles_5m, pos["direction"],
            entry, pos["entry_spot"], pos["atm_delta"], current_sl,
        )
        if new_sl != current_sl:
            _modify_sl(gateway, pos, new_sl)

    # ── Breakeven milestone ───────────────────────────────────────────────────
    if not breakeven_hit and progress >= config.BREAKEVEN_AT * T:
        # Use pos["sl_premium"] not the captured current_sl — caution trailing may
        # have already moved it this tick.
        new_sl = trailing.ratchet(pos["sl_premium"], trailing.breakeven_sl(entry))
        _modify_sl(gateway, pos, new_sl)
        pos["breakeven_hit"] = True
        breakeven_hit = True
        log.info("manager: BREAKEVEN milestone hit ltp=%.2f", ltp)

    # Fork: breakeven reached (this tick or earlier) + health currently healthy → RUNNER
    # Handles both same-tick and deferred health improvement cases.
    if breakeven_hit and eff_health >= config.HEALTH_HEALTHY:
        _enter_runner(gateway, pos, r, ltp)
        return

    # ── Lock milestone (non-runner only) ──────────────────────────────────────
    if breakeven_hit and progress >= config.LOCK_AT * T:
        lock = trailing.lock_sl(entry, T)
        new_sl = trailing.ratchet(pos["sl_premium"], lock)
        _modify_sl(gateway, pos, new_sl)
        pos["phase"] = "LOCKED"
        log.info("manager: LOCKED milestone hit ltp=%.2f lock_sl=%.2f", ltp, lock)
        state.save_position(r, pos)
        return

    state.save_position(r, pos)


def _handle_locked(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    ltp: float,
    T: float,
    target: Optional[float],
) -> None:
    # Target hit check (belt-and-suspenders; main fill via reconcile)
    if target is not None and ltp >= target:
        _exit(gateway, pos, r, "target_hit", ltp)
        return
    state.save_position(r, pos)


def _handle_runner(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    ltp: float,
    T: float,
    original_target: Optional[float],
    peak: float,
    eff_health: int,
) -> None:
    # Health < 50 → exit runner
    if eff_health < config.HEALTH_CAUTION:
        _exit(gateway, pos, r, "runner_health_faded", ltp)
        return

    # Compute trail SL
    entry = pos["entry_premium"]
    past_target = (original_target is not None and peak >= original_target)
    trail = trailing.runner_trail_sl(peak, past_target)
    new_sl = trailing.ratchet(pos["sl_premium"], trail)
    if new_sl != pos["sl_premium"]:
        _modify_sl(gateway, pos, new_sl)

    # Give-back check
    if ltp <= pos["sl_premium"]:
        _exit(gateway, pos, r, "runner_giveback", ltp)
        return

    state.save_position(r, pos)


# ── Exit flow ─────────────────────────────────────────────────────────────────

def _enter_runner(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    ltp: float,
) -> None:
    """Cancel target order, switch to RUNNER phase."""
    tgt_oid = pos.get("target_order_id")
    if tgt_oid:
        gateway.cancel_order(tgt_oid)
        pos["target_order_id"] = None
        pos["target_premium"]  = None

    pos["phase"] = "RUNNER"
    state.save_position(r, pos)
    log.info("manager: → RUNNER  ltp=%.2f peak=%.2f", ltp, pos.get("peak_premium", ltp))


def _exit(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    reason: str,
    exit_price: float,
) -> None:
    """
    Transition to EXITING: place market sell (if not already filled by SL/target),
    cancel any outstanding opposing orders.
    """
    log.info("manager: EXIT reason=%s exit_price=%.2f", reason, exit_price)
    phase = pos["phase"]
    pos["phase"]       = "EXITING"
    pos["exit_reason"] = reason
    pos["exit_premium"] = round(exit_price, 2)
    state.save_position(r, pos)

    sl_oid  = pos.get("sl_order_id")
    tgt_oid = pos.get("target_order_id")

    if reason in ("sl_hit",):
        # SL filled by exchange — cancel target if open
        if tgt_oid:
            gateway.cancel_order(tgt_oid)
    elif reason in ("target_hit",):
        # Target filled — cancel SL
        if sl_oid:
            gateway.cancel_order(sl_oid)
    else:
        # Discretionary exit: cancel both bracket orders, then place market sell.
        if sl_oid:
            gateway.cancel_order(sl_oid)
        if tgt_oid:
            gateway.cancel_order(tgt_oid)
        if phase not in ("IDLE", "EXITING"):
            exit_oid = gateway.place_order(
                tradingsymbol=pos["tradingsymbol"],
                exchange="NFO",
                transaction_type="SELL",
                quantity=pos["qty"],
                order_type="MARKET",
                product="MIS",
                tag=f"executor_exit_{reason}",
            )
            pos["exit_order_id"] = exit_oid
            state.save_position(r, pos)


def check_exit_complete(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
) -> None:
    """Check if the position is now flat; transition to COOLDOWN if yes."""
    open_positions = gateway.get_open_positions()
    ts = pos.get("tradingsymbol", "")
    is_flat = not any(p["tradingsymbol"] == ts for p in open_positions)

    if not is_flat:
        log.info("manager: EXITING — still waiting for fill")
        return

    # Resolve actual exit fill price (important for discretionary exits)
    exit_oid = pos.get("exit_order_id")
    if exit_oid:
        result = gateway.get_order_status(exit_oid)
        if result.filled_price:
            pos["exit_premium"] = result.filled_price

    # Compute P&L (paper only; live P&L from Kite statement)
    entry  = pos.get("entry_premium", 0)
    exit_p = pos.get("exit_premium", entry)
    qty    = pos.get("qty", 0)
    from executor.gateway.paper import PaperGateway
    if isinstance(gateway, PaperGateway) and qty > 0:
        pnl = gateway.compute_pnl(entry, exit_p, qty)
        pos["pnl"] = pnl
        log.info("manager: paper P&L = ₹%.2f", pnl)

    now_utc = state.now_utc_iso()
    pos["phase"]             = "COOLDOWN"
    pos["cooldown_start_ts"] = now_utc
    state.save_position(r, pos)
    log.info("manager: → COOLDOWN  exit_reason=%s pnl=%s",
             pos.get("exit_reason"), pos.get("pnl"))


def check_cooldown_elapsed(pos: dict, r: redis_lib.Redis) -> None:
    """Transition COOLDOWN → IDLE (delete position) once 15 min have elapsed."""
    start_ts_str = pos.get("cooldown_start_ts")
    if not start_ts_str:
        state.delete_position(r)
        return
    try:
        start = datetime.fromisoformat(start_ts_str)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    except ValueError:
        state.delete_position(r)
        return

    elapsed_min = (datetime.now(timezone.utc) - start).total_seconds() / 60
    if elapsed_min >= config.COOLDOWN_AFTER_EXIT:
        log.info("manager: COOLDOWN elapsed (%.1f min) → IDLE", elapsed_min)
        state.delete_position(r)
    else:
        log.info("manager: COOLDOWN %.1f / %d min", elapsed_min, config.COOLDOWN_AFTER_EXIT)


def force_squareoff(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    option_ltp: float = 0.0,
) -> None:
    """Hard square-off at 15:10 IST — unconditional.  Spec §12 item 8."""
    log.warning("manager: HARD SQUAREOFF 15:10 IST")
    _exit(gateway, pos, r, "hard_squareoff", option_ltp)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _modify_sl(gateway: OrderGateway, pos: dict, new_sl: float) -> None:
    """Modify existing SL-M order in place (spec §14 — never cancel+replace)."""
    oid = pos.get("sl_order_id")
    if not oid:
        log.error("_modify_sl: no sl_order_id in state")
        return
    old_sl = pos["sl_premium"]
    if new_sl <= old_sl:
        return  # ratchet already enforced upstream; defensive
    gateway.modify_order(order_id=oid, trigger_price=new_sl)
    pos["sl_premium"] = new_sl
    log.info("manager: SL ratcheted %.2f → %.2f", old_sl, new_sl)
