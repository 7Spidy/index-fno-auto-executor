"""
PaperGateway — simulated fills with honest cost model.
Spec §15.  All paper order state lives in Redis under executor:paper_orders.

Fill rules:
  MARKET  → fills immediately when set_current_ltp() has been called.
  LIMIT   → SELL fills when LTP ≥ limit_price; BUY fills when LTP ≤ limit_price.
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

log = logging.getLogger(__name__)

HALF_SPREAD = config.PAPER_SPREAD / 2   # 0.375 ₹


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paper_costs(sell_price: float, qty: int, lot_size: int) -> float:
    """
    Per-trade sell-side costs (spec §15):
      brokerage ₹20, STT 0.025% of sell premium × qty,
      exchange ~₹0.50/lot, GST 18% on brokerage + exchange.
    """
    lots = qty / lot_size
    brokerage = 20.0
    stt = 0.00025 * sell_price * qty
    exchange = 0.50 * lots
    gst = 0.18 * (brokerage + exchange)
    return round(brokerage + stt + exchange + gst, 2)


class PaperGateway(OrderGateway):
    """
    Implements OrderGateway using simulated fills stored in Redis.
    Call set_current_ltp(ltp) once per executor run before any order operations.
    Then call process_tick() to advance all pending orders against that LTP.
    """

    _ORDERS_KEY = "executor:paper_orders"

    def __init__(self, redis_client: redis_lib.Redis, lot_size: int = 25) -> None:
        self._r = redis_client
        self._lot_size = lot_size
        self._current_ltp: Optional[float] = None

    # ── LTP setter (called by run.py at start of each tick) ────────────────────

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
        """Derive net open positions from filled order pairs."""
        orders = self._load()
        buy_fills  = [o for o in orders.values()
                      if o["transaction_type"] == "BUY"  and o["status"] == "COMPLETE"]
        sell_fills = [o for o in orders.values()
                      if o["transaction_type"] == "SELL" and o["status"] == "COMPLETE"]
        buy_qty  = sum(o["filled_qty"] for o in buy_fills)
        sell_qty = sum(o["filled_qty"] for o in sell_fills)
        net_qty = buy_qty - sell_qty
        if net_qty <= 0 or not buy_fills:
            return []
        avg_price = sum(o["filled_price"] * o["filled_qty"] for o in buy_fills) / buy_qty
        return [{
            "tradingsymbol": buy_fills[-1]["tradingsymbol"],
            "product": "MIS",
            "quantity": net_qty,
            "average_price": round(avg_price, 2),
        }]

    def reconcile(self, position_state: dict) -> dict:
        """
        Check if paper SL or target orders were filled externally (via process_tick
        in a previous run that crashed before updating Redis position state).
        Marks sl_filled / target_filled flags if applicable.
        """
        for key, fill_key in [("sl_order_id", "sl_filled"),
                               ("target_order_id", "target_filled")]:
            oid = position_state.get(key)
            if not oid:
                continue
            result = self.get_order_status(oid)
            if result.status == "COMPLETE" and not position_state.get(fill_key):
                position_state[fill_key] = True
                price_key = "sl_fill_price" if key == "sl_order_id" else "target_fill_price"
                position_state[price_key] = result.filled_price
                log.warning("PAPER reconcile: %s was already filled @ %.2f",
                            key, result.filled_price or 0)
        return position_state

    # ── Tick processor (called every executor run for pending LIMIT / SL-M orders) ─

    def process_tick(self) -> list[str]:
        """
        Advance all OPEN non-MARKET paper orders against self._current_ltp.
        Returns list of order_ids filled this tick.
        """
        if self._current_ltp is None:
            return []
        ltp = self._current_ltp
        orders = self._load()
        filled: list[str] = []

        for oid, o in orders.items():
            if o["status"] != "OPEN":
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

    def compute_pnl(self, entry_price: float, exit_price: float, qty: int) -> float:
        """Net P&L after costs (one-way sell-side costs per trade)."""
        gross = (exit_price - entry_price) * qty
        costs = _paper_costs(exit_price, qty, self._lot_size)
        return round(gross - costs, 2)

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fill_order(order: dict, fill_price: float) -> None:
        order["status"]       = "COMPLETE"
        order["filled_price"] = round(fill_price, 2)
        order["filled_qty"]   = order["quantity"]
        order["filled_ts"]    = _now_utc()
