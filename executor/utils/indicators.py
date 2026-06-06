"""
Technical indicator helpers — copied from signal bot pattern.
All functions operate on a pandas DataFrame with columns:
  date, open, high, low, close, volume
sorted oldest-to-newest (index 0 = oldest).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ── VWAP ──────────────────────────────────────────────────────────────────────

def compute_vwap(candles: pd.DataFrame) -> pd.Series:
    """
    Intraday VWAP = cumsum(typical_price × volume) / cumsum(volume).
    Falls back to TWAP (time-weighted) when all volume values are zero
    (Kite sometimes returns zero volume for index historical data).
    """
    tp = (candles["high"] + candles["low"] + candles["close"]) / 3
    vol = candles["volume"]
    if vol.sum() == 0:
        return tp.expanding().mean()
    cum_tpv = (tp * vol).cumsum()
    cum_vol = vol.cumsum().replace(0, np.nan)
    return cum_tpv / cum_vol


# ── RSI ───────────────────────────────────────────────────────────────────────

def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's smoothed RSI."""
    delta = closes.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ── ATR ───────────────────────────────────────────────────────────────────────

def compute_atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    h, l, c = candles["high"], candles["low"], candles["close"]
    prev_c = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


# ── DMI (+DI / -DI) ───────────────────────────────────────────────────────────

def compute_dmi(candles: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series]:
    """
    Returns (+DI, -DI) as a tuple of Series.
    Uses Wilder's exponential smoothing to match the signal bot.
    """
    h = candles["high"]
    l = candles["low"]
    up   = h - h.shift(1)
    down = l.shift(1) - l

    pos_dm = up.where((up > down) & (up > 0), 0.0)
    neg_dm = down.where((down > up) & (down > 0), 0.0)

    atr = compute_atr(candles, period)

    smooth_p = pos_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    smooth_n = neg_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    pdi = (100 * smooth_p / atr.replace(0, np.nan)).fillna(0)
    ndi = (100 * smooth_n / atr.replace(0, np.nan)).fillna(0)
    return pdi, ndi
