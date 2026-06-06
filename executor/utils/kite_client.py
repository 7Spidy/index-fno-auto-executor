"""
Thin wrapper around kiteconnect.KiteConnect — copied from signal bot pattern.
Only exposes what the executor needs: quotes, historical candles, and order ops
(order ops are delegated to the gateway; this module is read-only market data).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd
from kiteconnect import KiteConnect

log = logging.getLogger(__name__)

# Exchange constants (avoids importing kiteconnect in every module)
NFO   = "NFO"
NSE   = "NSE"


class KiteClient:
    def __init__(self, api_key: str, access_token: str) -> None:
        self._kite = KiteConnect(api_key=api_key)
        self._kite.set_access_token(access_token)

    # ── Market data ────────────────────────────────────────────────────────────

    def get_ltp(self, instruments: list[str]) -> dict[str, float]:
        """
        instruments: ["NSE:INDIA VIX", "NFO:NIFTY24JUN24500CE", ...]
        Returns {instrument: last_price}.
        """
        data = self._kite.ltp(instruments)
        return {k: v["last_price"] for k, v in data.items()}

    def get_quote(self, instruments: list[str]) -> dict[str, Any]:
        return self._kite.quote(instruments)

    def get_historical_candles(
        self,
        instrument_token: int,
        from_dt: datetime,
        to_dt: datetime,
        interval: str = "5minute",
        continuous: bool = False,
        oi: bool = False,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles and return a DataFrame with columns:
          date, open, high, low, close, volume
        sorted oldest-to-newest.
        """
        raw = self._kite.historical_data(
            instrument_token, from_dt, to_dt, interval,
            continuous=continuous, oi=oi,
        )
        if not raw:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"])
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    # ── Order / position passthrough (used by KiteLiveGateway) ─────────────────

    def place_order(self, **kwargs) -> str:
        return self._kite.place_order(**kwargs)

    def modify_order(self, variety: str, order_id: str, **kwargs) -> str:
        return self._kite.modify_order(variety=variety, order_id=order_id, **kwargs)

    def cancel_order(self, variety: str, order_id: str) -> str:
        return self._kite.cancel_order(variety=variety, order_id=order_id)

    def get_orders(self) -> list[dict]:
        return self._kite.orders()

    def get_order_history(self, order_id: str) -> list[dict]:
        return self._kite.order_history(order_id)

    def get_positions(self) -> dict:
        return self._kite.positions()
