"""
Thin wrapper around kiteconnect.KiteConnect — copied from signal bot pattern.
Only exposes what the executor needs: quotes, historical candles, and order ops
(order ops are delegated to the gateway; this module is read-only market data).
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from kiteconnect import KiteConnect

log = logging.getLogger(__name__)

KEY_MARGINS_CACHE = "kite:margins_cache"      # short-TTL cache — avoid hammering margins() per gate check
MARGINS_CACHE_TTL_SECS = 60


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

    def get_margins(self, r: "redis_lib.Redis | None" = None) -> float:
        """
        Return available equity cash for the live capital-availability gate.
        Mirrors the capital figure semantics of Repo 1's DAILY_CAPITAL constant.

        If a Redis client is passed, the result is cached for
        MARGINS_CACHE_TTL_SECS to avoid calling margins() on every gate
        evaluation within the same tick.
        """
        if r is not None:
            cached = r.get(KEY_MARGINS_CACHE)
            if cached is not None:
                return float(cached)

        margins = self._kite.margins()
        available = float(margins["equity"]["available"]["live_balance"])

        if r is not None:
            r.set(KEY_MARGINS_CACHE, str(available), ex=MARGINS_CACHE_TTL_SECS)

        return available

    # ── Order / position passthrough (used by KiteLiveGateway) ─────────────────

    def place_order(self, **kwargs) -> str:
        return self._kite.place_order(**kwargs)

    def modify_order(self, variety: str, order_id: str, **kwargs) -> str:
        return self._kite.modify_order(variety=variety, order_id=order_id, **kwargs)

    def cancel_order(self, variety: str, order_id: str) -> str:
        return self._kite.cancel_order(variety=variety, order_id=order_id)

    def get_order_history(self, order_id: str) -> list[dict]:
        return self._kite.order_history(order_id)

    def get_positions(self) -> dict:
        return self._kite.positions()
