"""
PaperGateway — simulated fills with Repo 1's exact charges model.
Spec §15.  All paper order state lives in Redis under executor:paper_orders,
shared across all 17 concurrently-running instruments (claude_change_spec_repo2.md).

Fill rules:
  MARKET  → fills immediately when set_current_ltp() has been called.
  LIMIT   → SELL fills when LTP ≥ limit_price; BUY fills when LTP ≤ limit_price
            (including instantly, at placement time, for a marketable BUY).
  SL-M    → SELL fills when LTP ≤ trigger_price; BUY fills when LTP ≥ trigger_price.
Spread: buy at LTP + HALF_SPREAD, sell at LTP − HALF_SPREAD.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis as redis_lib

from executor.gateway.base import OrderGateway, OrderResult
from executor import config
from executor.charges import net_pnl

log = logging.getLogger(__name__)

HALF_SPREAD = config.PAPER_SPREAD / 2   # 0.375 ₹


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperGateway(OrderGateway):
    """
    Implements OrderGateway using simulated fills stored in Redis.
    Call set_current_ltp(ltp) once per instrument, per executor run, before
    any order operations for that instrument. Then call process_tick(),
    scoped to that instrument's tradingsymbol, to advance its pending
    LIMIT / SL-M orders against that LTP.
    """

    _ORDERS_KEY = "executor:paper_orders"

    def __init__(self, redis_client: redis_lib.Redis) -> None:
        self._r = redis_client
        self._current_ltp: Optional[float] = None

    # ── LTP setter (called by run.py once per instrument, per tick) ────────────

    def set_current_ltp(self, ltp: float) -> None:
        self._current_ltp = ltp

    # ── Internal order store ───────────────────────────────────────────────────

    def _load(self) -> dict:
        raw = self._r.get(self._ORDERS_KEY)
        return json.loads(raw) if raw else {}

    def _save(self, orders: dict) -> None:
        self._r.set(self._ORDERS_KEY, json.dumps(orders))

    def _new_id(self) -> str:
        return "PAPER_" + uuid.uuid4().hex[:10].upper()

    # ── OrderGateway interface ─────────────────────────────────────────────────

    def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        product: str = "MIS",
        tag: str = "",
    ) -> str:
        oid = self._new_id()
        order = {
            "order_id":        oid,
            "tradingsymbol":   tradingsymbol,
            "exchange":        exchange,
            "transaction_type": transaction_type,
            "quantity":        quantity,
            "order_type":      order_type,
            "price":           price,
            "trigger_price":   trigger_price,
            "product":         product,
            "tag":             tag,
            "status":          "OPEN",
            "filled_price":    None,
            "filled_qty":      0,
            "placed_ts":       _now_utc(),
            "filled_ts":       None,
        }
        orders = self._load()
        orders[oid] = order
        self._save(orders)

        # MARKET orders fill immediately at current LTP.
        if order_type == "MARKET":
            if self._current_ltp is None:
                raise RuntimeError("PaperGateway: set_current_ltp() must be called before placing MARKET orders")
            fill_price = (
                self._current_ltp + HALF_SPREAD if transaction_type == "BUY"
                else self._current_ltp - HALF_SPREAD
            )
            fill_price = round(max(fill_price, 0.05), 2)
            orders = self._load()
            self._fill_order(orders[oid], fill_price)
            self._save(orders)
            log.info("PAPER MARKET fill %s %s %s @ %.2f qty=%d",
                     oid, transaction_type, tradingsymbol, fill_price, quantity)

        # A marketable LIMIT order (or an SL-M whose trigger is already
        # touched) behaves like a market order when its condition is already
        # satisfied at placement time — mirrors real exchange behavior and
        # preserves the same-tick fill assumption _run_idle relies on.
        elif order_type == "LIMIT" and price is not None and self._current_ltp is not None:
            fills_now = (
                (transaction_type == "BUY" and self._current_ltp <= price) or
                (transaction_type == "SELL" and self._current_ltp >= price)
            )
            if fills_now:
                fill_price = (
                    self._current_ltp + HALF_SPREAD if transaction_type == "BUY"
                    else self._current_ltp - HALF_SPREAD
                )
                fill_price = round(max(fill_price, 0.05), 2)
                orders = self._load()
                self._fill_order(orders[oid], fill_price)
                self._save(orders)
                log.info("PAPER LIMIT instant-fill %s %s %s @ %.2f qty=%d",
                          oid, transaction_type, tradingsymbol, fill_price, quantity)
            else:
                log.info("PAPER placed %s %s %s qty=%d type=%s trigger=%s price=%s",
                         oid, transaction_type, tradingsymbol, quantity,
                         order_type, trigger_price, price)

        else:
            log.info("PAPER placed %s %s %s qty=%d type=%s trigger=%s price=%s",
                     oid, transaction_type, tradingsymbol, quantity,
                     order_type, trigger_price, price)
        return oid

    def modify_order(
        self,
        order_id: str,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        quantity: Optional[int] = None,
    ) -> None:
        orders = self._load()
        if order_id not in orders:
            raise KeyError(f"PAPER modify: order {order_id} not found")
        o = orders[order_id]
        if o["status"] != "OPEN":
            raise ValueError(f"PAPER modify: order {order_id} is {o['status']}, not OPEN")
        if price is not None:
            o["price"] = price
        if trigger_price is not None:
            o["trigger_price"] = trigger_price
        if quantity is not None:
            o["quantity"] = quantity
        self._save(orders)
        log.info("PAPER modified %s trigger=%.2f", order_id,
                 trigger_price if trigger_price else (price or 0))

    def cancel_order(self, order_id: str) -> None:
        orders = self._load()
        if order_id not in orders:
            return  # idempotent
        if orders[order_id]["status"] == "OPEN":
            orders[order_id]["status"] = "CANCELLED"
            self._save(orders)
            log.info("PAPER cancelled %s", order_id)

    def get_order_status(self, order_id: str) -> OrderResult:
        orders = self._load()
        if order_id not in orders:
            return OrderResult(order_id=order_id, status="UNKNOWN")
        o = orders[order_id]
        return OrderResult(
            order_id=order_id,
            status=o["status"],
            filled_price=o.get("filled_price"),
            filled_qty=o.get("filled_qty", 0),
        )

    def get_open_positions(self) -> list[dict]:
        """Derive net open positions, one per distinct tradingsymbol, from
        filled order pairs. Must not blend fills across different
        instruments — 17 instruments can have concurrently open positions,
        each with its own filled BUY/SELL orders in the same shared store."""
        orders = self._load()
        by_symbol: dict[str, dict] = {}
        for o in orders.values():
            if o["status"] != "COMPLETE":
                continue
            sym = o["tradingsymbol"]
            entry = by_symbol.setdefault(sym, {"buy_qty": 0, "sell_qty": 0, "buy_notional": 0.0})
            if o["transaction_type"] == "BUY":
                entry["buy_qty"] += o["filled_qty"]
                entry["buy_notional"] += o["filled_price"] * o["filled_qty"]
            elif o["transaction_type"] == "SELL":
                entry["sell_qty"] += o["filled_qty"]

        result = []
        for sym, e in by_symbol.items():
            net_qty = e["buy_qty"] - e["sell_qty"]
            if net_qty <= 0 or e["buy_qty"] == 0:
                continue
            avg_price = e["buy_notional"] / e["buy_qty"]
            result.append({
                "tradingsymbol": sym,
                "product": "MIS",
                "quantity": net_qty,
                "average_price": round(avg_price, 2),
            })
        return result

    def reconcile(self, position_state: dict) -> dict:
        """
        Check if the paper SL order was filled externally (via process_tick
        in a previous run that crashed before updating Redis position state).
        Marks the sl_filled flag if applicable.
        """
        oid = position_state.get("sl_order_id")
        if oid:
            result = self.get_order_status(oid)
            if result.status == "COMPLETE" and not position_state.get("sl_filled"):
                position_state["sl_filled"] = True
                position_state["sl_fill_price"] = result.filled_price
                log.warning("PAPER reconcile: sl_order_id was already filled @ %.2f",
                            result.filled_price or 0)
        return position_state

    # ── Tick processor (called every executor run for pending LIMIT / SL-M orders) ─

    def process_tick(self, tradingsymbol: Optional[str] = None) -> list[str]:
        """
        Advance OPEN non-MARKET paper orders against self._current_ltp.

        `tradingsymbol`, when given, scopes this call to only that
        instrument's orders — required because executor:paper_orders is a
        single store shared across all 17 concurrently-ticking instruments,
        each with its own LTP; without this filter, one instrument's LTP
        would incorrectly evaluate every other instrument's pending orders.

        Returns list of order_ids filled this call.
        """
        if self._current_ltp is None:
            return []
        ltp = self._current_ltp
        orders = self._load()
        filled: list[str] = []

        for oid, o in orders.items():
            if o["status"] != "OPEN":
                continue
            if tradingsymbol is not None and o["tradingsymbol"] != tradingsymbol:
                continue
            ot = o["order_type"]
            tt = o["transaction_type"]

            if ot == "LIMIT":
                if tt == "SELL" and ltp >= o["price"]:
                    self._fill_order(o, round(o["price"] - HALF_SPREAD, 2))
                    filled.append(oid)
                elif tt == "BUY" and ltp <= o["price"]:
                    self._fill_order(o, round(o["price"] + HALF_SPREAD, 2))
                    filled.append(oid)

            elif ot == "SL-M":
                if tt == "SELL" and ltp <= o["trigger_price"]:
                    # Triggered; fill at market (adverse slippage = spread)
                    fill_price = round(ltp - HALF_SPREAD, 2)
                    self._fill_order(o, max(fill_price, 0.05))
                    filled.append(oid)
                elif tt == "BUY" and ltp >= o["trigger_price"]:
                    fill_price = round(ltp + HALF_SPREAD, 2)
                    self._fill_order(o, fill_price)
                    filled.append(oid)

        if filled:
            self._save(orders)
        return filled

    # ── P&L helper ─────────────────────────────────────────────────────────────

    def compute_pnl(self, entry_price: float, exit_price: float, qty: int, direction: str) -> float:
        """Net P&L via Repo 1's exact charges model — see executor/charges.py."""
        return net_pnl(entry_price, exit_price, qty, direction)

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fill_order(order: dict, fill_price: float) -> None:
        order["status"]       = "COMPLETE"
        order["filled_price"] = round(fill_price, 2)
        order["filled_qty"]   = order["quantity"]
        order["filled_ts"]    = _now_utc()
