"""OrderGateway ABC — spec §17.  Both paper and live gateways implement this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderResult:
    order_id: str
    status: str                      # OPEN | COMPLETE | CANCELLED | REJECTED | UNKNOWN
    filled_price: Optional[float] = None
    filled_qty: int = 0
    message: str = ""


class OrderGateway(ABC):
    """
    Minimal interface for order management.
    Implementations: PaperGateway (paper.py) and KiteLiveGateway (kite_live.py).
    All premium prices are in ₹ per unit (not per lot).
    """

    @abstractmethod
    def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,       # "BUY" | "SELL"
        quantity: int,               # total units (lots × lot_size)
        order_type: str,             # "MARKET" | "LIMIT" | "SL-M"
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        product: str = "MIS",
        tag: str = "",
    ) -> str:
        """Place an order; return broker-assigned order_id."""
        ...

    @abstractmethod
    def modify_order(
        self,
        order_id: str,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        quantity: Optional[int] = None,
    ) -> None:
        """Modify an open order in-place (never cancel+replace — spec §14)."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order.  Idempotent — no-op if already cancelled/filled."""
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult:
        """Return the current status of an order by ID."""
        ...

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        """
        Return list of open intraday positions.
        Each dict must contain at least: tradingsymbol, quantity, average_price.
        """
        ...

    @abstractmethod
    def reconcile(self, position_state: dict) -> dict:
        """
        Compare broker state to the Redis position dict.
        Mark sl_filled / target_filled if the respective orders have been filled
        externally.  Returns the (possibly mutated) position_state dict.
        """
        ...
