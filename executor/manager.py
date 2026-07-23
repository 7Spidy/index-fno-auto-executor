"""
Manager — state machine driver.  Spec §16 (state machine), §6-§13 (rules).

Each public function handles one phase transition or intra-phase management.
All functions mutate the position dict in place and call state.save_position().

State machine phases:
  IDLE → ENTERING → OPEN → EXITING → COOLDOWN → IDLE

Exit reasons (mirrors Repo 1 exactly — see claude_change_spec_repo2.md):
  1. SL-M hit on exchange       → "sl_hit"
  2. Hard square-off 15:10      → "hard_squareoff"
  3. Position flat externally   → "flat_external"

No target order, no health/VWAP/theta/runner exit engine — Repo 1's ladder SL
(executor/trailing.py) is the only trailing/exit logic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import redis as redis_lib

from executor import charges, config, state, sizing, trailing
from executor.gateway.base import OrderGateway
from executor.utils.calendar_nse import now_ist

log = logging.getLogger(__name__)


# ── Entry flow ────────────────────────────────────────────────────────────────

def try_enter(
    intent: dict,
    gateway: OrderGateway,
    r: redis_lib.Redis,
    kite: "KiteClient",
    entry_ltp: float,
    exchange: str,
    lot_multiplier: int = 1,
) -> None:
    """
    Called when gate passes.  Places entry marketable-LIMIT order and
    transitions to ENTERING. Qty is fixed-lot × lot_multiplier, gated on
    capital availability (see sizing.compute_qty).
    In paper mode the order fills immediately if marketable; check_entry_fill()
    is called next either way.

    `lot_multiplier` — always 1 in paper mode; in live mode it's the
    once-per-day multiplier decided in run.py's main() before the instrument
    loop (see state.get_lot_multiplier).
    """
    ts = intent.get("tradingsymbol", "")
    log.info("manager: entering %s direction=%s", ts, intent.get("direction"))

    pos = state.fresh_position_from_intent(intent)
    pos["exchange"] = exchange

    qty = sizing.compute_qty(
        r, ts,
        entry_ltp=entry_ltp,
        paper_mode=config.PAPER_MODE,
        kite=kite if not config.PAPER_MODE else None,
        lot_multiplier=lot_multiplier,
    )
    if qty == 0:
        log.warning("manager: sizing returned 0 — aborting entry")
        return

    # Marketable LIMIT: 1% above LTP, always BUY (long premium) — caps
    # worst-case slippage while keeping a near-certain fill.
    entry_limit_price = round(entry_ltp * (1 + config.ENTRY_LIMIT_BUFFER_PCT), 2)
    order_id = gateway.place_order(
        tradingsymbol=ts,
        exchange=exchange,
        transaction_type="BUY",
        quantity=qty,
        order_type="LIMIT",
        price=entry_limit_price,
        product="MIS",
        tag="executor_entry",
    )
    pos["entry_order_id"]    = order_id
    pos["entry_limit_price"] = entry_limit_price
    pos["qty"]                = qty
    state.save_position(r, pos["instrument"], pos)
    log.info("manager: entry order placed %s qty=%d limit=%.2f", order_id, qty, entry_limit_price)


def check_entry_fill(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    spot_ltp: float,
    option_ltp: float,
) -> None:
    """
    Check if the entry order has filled.
    On fill: derive initial SL (Repo 1-equivalent delta approximation), size
    the position, place the SL-M order, transition to OPEN.
    """
    oid = pos.get("entry_order_id")
    if not oid:
        log.error("manager: ENTERING but no entry_order_id in state")
        return

    result = gateway.get_order_status(oid)
    if result.status != "COMPLETE":
        log.info("manager: entry order %s still %s — waiting", oid, result.status)
        return

    entry_premium = result.filled_price
    if not entry_premium:
        log.error("manager: entry fill price is None — aborting entry")
        state.delete_position(r, pos["instrument"])
        return

    qty = pos.get("qty")
    if not qty or qty == 0:
        log.warning("manager: qty missing from state — aborting entry, cancelling order")
        gateway.cancel_order(oid)
        state.delete_position(r, pos["instrument"])
        return

    # Derive initial option SL from spot_risk_pts via delta approximation —
    # Repo 1-equivalent of paper_engine.simulate_entry's initial-SL derivation.
    # spot_risk_pts is already the spot-points risk distance (executor_bridge.py
    # computes it the same way Repo 1's target_pts / TARGET_RR does), so no
    # further back-derivation is needed here.
    delta         = pos.get("atm_delta", config.ATM_DELTA)
    spot_risk_pts = pos.get("spot_risk_pts")
    if spot_risk_pts is not None and spot_risk_pts > 0:
        option_risk_pts = spot_risk_pts * delta
        initial_sl = entry_premium - option_risk_pts
        initial_sl = max(initial_sl, 0.05)
        target_t   = option_risk_pts * config.TARGET_RR
    else:
        # Fallback: 70% of entry premium as initial SL buffer
        initial_sl = entry_premium * 0.70
        target_t   = None
        log.warning("manager: spot_risk_pts missing — using 70%% of entry as SL floor")

    now_utc = state.now_utc_iso()
    exchange = pos.get("exchange", "NFO")

    # Place SL-M order (spec §14)
    sl_oid = gateway.place_order(
        tradingsymbol=pos["tradingsymbol"],
        exchange=exchange,
        transaction_type="SELL",
        quantity=qty,
        order_type="SL-M",
        trigger_price=round(initial_sl, 2),
        product="MIS",
        tag="executor_sl",
    )

    pos.update({
        "phase":            "OPEN",
        "entry_premium":    round(entry_premium, 2),
        "entry_spot":       round(spot_ltp, 2),
        "sl_premium":       round(initial_sl, 2),
        "sl_ladder_stage":  round(initial_sl, 2),
        "initial_sl":       round(initial_sl, 2),
        "target_t":         round(target_t, 2) if target_t else None,
        "qty":              qty,
        "sl_order_id":      sl_oid,
        "entry_ts":         now_utc,
    })
    state.save_position(r, pos["instrument"], pos)
    log.info(
        "manager: OPEN entry=%.2f sl=%.2f target_t=%s qty=%d  sl_oid=%s",
        entry_premium, initial_sl, target_t, qty, sl_oid,
    )


# ── Position management (called every 1-min run) ──────────────────────────────

def manage_position(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    option_ltp: float,
    rsi_last3: list | None,
) -> None:
    """Per-minute management tick — ports Repo 1's position_tracker.py
    run_heartbeat Step 2 (trail SL, check SL-hit) exactly. No target order,
    no health/VWAP/theta/runner exits — SL-hit and hard-squareoff only."""
    entry = pos["entry_premium"]
    T = pos.get("target_t")
    sl_stage = pos.get("sl_ladder_stage", pos["initial_sl"])

    # Reconcile: SL filled externally (exchange hit it between ticks)
    if pos.get("sl_filled"):
        _exit(gateway, pos, r, "sl_hit", pos.get("sl_fill_price", option_ltp))
        return
    if pos.get("position_flat_external"):
        _exit(gateway, pos, r, "flat_external", option_ltp)
        return

    if T and T > 0:
        raw_progress = (option_ltp - entry) / T
        market_snapshot = {
            "rsi_last3": rsi_last3, "progress": raw_progress,
            "current_price": option_ltp, "T": T,
        }
        # Repo 1 always uses "CE" here regardless of real direction — see
        # trailing.py docstring. Replicate exactly.
        ladder_sl = trailing.compute_ladder_sl(entry, T, option_ltp, "CE", sl_stage)
        ai_sl     = trailing.compute_ai_adjusted_sl(ladder_sl, "CE", market_snapshot)
        final_sl  = trailing.compute_final_sl(ladder_sl, ai_sl, "CE")
    else:
        final_sl = sl_stage

    pos["sl_ladder_stage"] = round(final_sl, 2)
    if final_sl != pos["sl_premium"]:
        _modify_sl(gateway, pos, final_sl)

    # SL hit this tick (belt-and-suspenders — exchange SL-M is primary floor)
    if option_ltp <= final_sl:
        _exit(gateway, pos, r, "sl_hit", final_sl)
        return

    state.save_position(r, pos["instrument"], pos)


# ── Exit flow ─────────────────────────────────────────────────────────────────

def _exit(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    reason: str,
    exit_price: float,
) -> None:
    """
    Transition to EXITING: place market sell (if not already filled by SL),
    cancel any outstanding SL order.
    """
    log.info("manager: EXIT reason=%s exit_price=%.2f", reason, exit_price)
    phase = pos["phase"]
    pos["phase"]        = "EXITING"
    pos["exit_reason"]  = reason
    pos["exit_premium"] = round(exit_price, 2)
    state.save_position(r, pos["instrument"], pos)

    sl_oid = pos.get("sl_order_id")

    if reason == "sl_hit":
        # SL filled by exchange — nothing else to cancel.
        pass
    else:
        # Discretionary / hard-squareoff / flat-external exit: cancel the SL,
        # then place market sell.
        if sl_oid:
            gateway.cancel_order(sl_oid)
        if phase not in ("IDLE", "EXITING"):
            exit_oid = gateway.place_order(
                tradingsymbol=pos["tradingsymbol"],
                exchange=pos.get("exchange", "NFO"),
                transaction_type="SELL",
                quantity=pos["qty"],
                order_type="MARKET",
                product="MIS",
                tag=f"executor_exit_{reason}",
            )
            pos["exit_order_id"] = exit_oid
            state.save_position(r, pos["instrument"], pos)


def check_exit_complete(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    kite: "KiteClient",
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

    # Net P&L via Repo 1's exact charges model — numeric parity with Repo 1's
    # paper P&L (see executor/charges.py).
    entry  = pos.get("entry_premium", 0)
    exit_p = pos.get("exit_premium", entry)
    qty    = pos.get("qty", 0)
    if qty > 0:
        pnl = charges.net_pnl(entry, exit_p, qty, pos["direction"])
        pos["pnl"] = round(pnl, 2)
        log.info("manager: P&L = ₹%.2f", pnl)

    date_str = now_ist().strftime("%Y-%m-%d")
    new_pnl = state.update_daily_pnl(r, date_str, pos.get("pnl", 0.0) or 0.0)

    loss_limit = sizing.get_daily_loss_limit(
        config.PAPER_MODE, kite if not config.PAPER_MODE else None, r,
    )
    if new_pnl <= loss_limit and not state.entries_blocked(r, date_str):
        state.block_entries(r, date_str, f"daily_loss_breaker: pnl={new_pnl:.2f}")

    now_utc = state.now_utc_iso()
    pos["phase"]             = "COOLDOWN"
    pos["cooldown_start_ts"] = now_utc
    state.save_position(r, pos["instrument"], pos)
    log.info("manager: → COOLDOWN  exit_reason=%s pnl=%s",
             pos.get("exit_reason"), pos.get("pnl"))


def check_cooldown_elapsed(pos: dict, r: redis_lib.Redis) -> None:
    """Transition COOLDOWN → IDLE (delete position) once 15 min have elapsed."""
    instrument = pos["instrument"]
    start_ts_str = pos.get("cooldown_start_ts")
    if not start_ts_str:
        state.delete_position(r, instrument)
        return
    try:
        start = datetime.fromisoformat(start_ts_str)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    except ValueError:
        state.delete_position(r, instrument)
        return

    elapsed_min = (datetime.now(timezone.utc) - start).total_seconds() / 60
    if elapsed_min >= config.COOLDOWN_AFTER_EXIT:
        log.info("manager: COOLDOWN elapsed (%.1f min) → IDLE", elapsed_min)
        state.delete_position(r, instrument)
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
    """Modify existing SL-M order in place (spec §14 — never cancel+replace).

    The ladder functions (trailing.py) already enforce monotonicity internally
    via max()/min() against prior_sl, so this only guards against a redundant
    no-op modify — not a one-directional-only ratchet check.
    """
    oid = pos.get("sl_order_id")
    if not oid:
        log.error("_modify_sl: no sl_order_id in state")
        return
    old_sl = pos["sl_premium"]
    if new_sl == old_sl:
        return  # no-op — ladder already enforced monotonicity upstream
    gateway.modify_order(order_id=oid, trigger_price=new_sl)
    pos["sl_premium"] = new_sl
    log.info("manager: SL ratcheted %.2f → %.2f", old_sl, new_sl)
