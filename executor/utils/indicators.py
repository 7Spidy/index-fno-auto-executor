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
    """Wilder-smoothed RSI. Seed = simple average of first `period` changes.

    Manual recursion (not pandas .ewm) to stay byte-parity with Repo 1's
    src/indicators.py::rsi_wilder — ewm's own seeding diverges slightly
    from Wilder's explicit simple-average seed in the first bars after
    min_periods.
    """
    close = closes.values.astype(float)
    n = len(close)
    rsi = np.full(n, np.nan)

    if n < period + 1:
        return pd.Series(rsi, index=closes.index, name="rsi")

    deltas = np.diff(close)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    alpha = 1.0 / period
    for i in range(period, n - 1):
        avg_gain = avg_gain * (1 - alpha) + gains[i] * alpha
        avg_loss = avg_loss * (1 - alpha) + losses[i] * alpha
        if avg_loss == 0.0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return pd.Series(rsi, index=closes.index, name="rsi")


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
