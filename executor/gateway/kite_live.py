"""
KiteLiveGateway — real Kite Connect orders.
Guards: PAPER_MODE must be False before this is instantiated (checked in run.py).
"""

from __future__ import annotations

import logging
from typing import Optional

from kiteconnect import KiteConnect

from executor.gateway.base import OrderGateway, OrderResult
from executor.utils.kite_client import KiteClient

log = logging.getLogger(__name__)

_VARIETY = "regular"   # MIS intraday options use regular variety on Zerodha


class KiteLiveGateway(OrderGateway):
    """Wraps KiteClient; delegates all order ops to Kite Connect REST API."""

    def __init__(self, kite_client: KiteClient) -> None:
        self._kite = kite_client

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
        kwargs: dict = dict(
            variety=_VARIETY,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            order_type=order_type,
            tag=tag,
        )
        if price is not None:
            kwargs["price"] = price
        if trigger_price is not None:
            kwargs["trigger_price"] = trigger_price

        order_id = self._kite.place_order(**kwargs)
        log.info("LIVE placed %s %s %s qty=%d type=%s trigger=%s",
                 order_id, transaction_type, tradingsymbol, quantity,
                 order_type, trigger_price)
        return order_id

    def modify_order(
        self,
        order_id: str,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        quantity: Optional[int] = None,
    ) -> None:
        kwargs: dict = {}
        if price is not None:
            kwargs["price"] = price
        if trigger_price is not None:
            kwargs["trigger_price"] = trigger_price
        if quantity is not None:
            kwargs["quantity"] = quantity
        self._kite.modify_order(variety=_VARIETY, order_id=order_id, **kwargs)
        log.info("LIVE modified %s %s", order_id, kwargs)

    def cancel_order(self, order_id: str) -> None:
        try:
            self._kite.cancel_order(variety=_VARIETY, order_id=order_id)
            log.info("LIVE cancelled %s", order_id)
        except Exception as exc:
            # Already cancelled / filled — idempotent
            log.warning("LIVE cancel %s: %s", order_id, exc)

    def get_order_status(self, order_id: str) -> OrderResult:
        try:
            history = self._kite.get_order_history(order_id)
        except Exception as exc:
            log.error("get_order_history %s failed: %s", order_id, exc)
            return OrderResult(order_id=order_id, status="UNKNOWN", message=str(exc))
        if not history:
            return OrderResult(order_id=order_id, status="UNKNOWN")
        last = history[-1]
        filled_price = last.get("average_price") or last.get("price")
        return OrderResult(
            order_id=order_id,
            status=last.get("status", "UNKNOWN"),
            filled_price=filled_price if filled_price else None,
            filled_qty=last.get("filled_quantity", 0),
        )

    def get_open_positions(self) -> list[dict]:
        data = self._kite.get_positions()
        day_positions = data.get("day", [])
        return [
            p for p in day_positions
            if p.get("product") == "MIS" and p.get("quantity", 0) != 0
        ]

    def reconcile(self, position_state: dict) -> dict:
        """
        Query Kite for live order statuses; mark sl_filled if the SL order
        has been filled outside our executor's knowledge.
        """
        oid = position_state.get("sl_order_id")
        if oid:
            result = self.get_order_status(oid)
            if result.status == "COMPLETE" and not position_state.get("sl_filled"):
                position_state["sl_filled"] = True
                position_state["sl_fill_price"] = result.filled_price
                log.warning("LIVE reconcile: sl_order_id (%s) already filled @ %s",
                            oid, result.filled_price)

        # Also check if the net position is flat (safety catch-all)
        positions = self.get_open_positions()
        ts = position_state.get("tradingsymbol", "")
        net_qty = sum(p["quantity"] for p in positions if p["tradingsymbol"] == ts)
        if net_qty == 0 and position_state.get("phase") == "OPEN":
            position_state["position_flat_external"] = True
            log.error("LIVE reconcile: position is flat externally for %s", ts)

        return position_state
