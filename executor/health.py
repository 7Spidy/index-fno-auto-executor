"""
Health scorer — spec §7.
Scores 0-100 from 4 weighted conditions on NIFTY spot 5-min candles.
Two extra signals override the score: VWAP veto and reversal detection.

Clock: only rescores when a new 5-min candle has closed (stateless — stores
last_health_ts in the position dict so each cron run can compare).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from executor import config
from executor.utils.calendar_nse import last_completed_5min_open, now_ist, IST
from executor.utils.indicators import compute_vwap, compute_rsi, compute_dmi

log = logging.getLogger(__name__)


@dataclass
class HealthResult:
    raw_score: int           # 0-100 before VWAP veto adjustment
    effective_score: int     # score after tier-drop (vwap_lost_consec == 1)
    vwap_lost_consec: int    # updated consecutive count (0 if VWAP OK this candle)
    exit_vwap: bool          # True if vwap_lost_consec >= VWAP_LOST_EXIT → immediate exit
    exit_reversal: bool      # True if opposite C1+C2 fired → immediate exit
    candle_ts: str           # ISO ts of the candle that triggered this rescore


def should_rescore(last_health_ts_iso: Optional[str]) -> bool:
    """
    True when a new 5-min candle has closed since the last health rescore.
    """
    latest_candle = last_completed_5min_open()
    if not last_health_ts_iso:
        return True
    try:
        last_scored = datetime.fromisoformat(last_health_ts_iso)
        if last_scored.tzinfo is None:
            last_scored = last_scored.replace(tzinfo=IST)
        return latest_candle > last_scored
    except ValueError:
        return True


def score(
    candles: pd.DataFrame,    # last 20 5-min NIFTY spot candles, oldest→newest
    direction: str,           # "CE" (bullish) | "PE" (bearish)
    prev_vwap_lost_consec: int,
) -> HealthResult:
    """
    Compute health score and veto signals.
    candles must have: date, open, high, low, close, volume  (≥ 5 rows minimum).
    """
    if len(candles) < 5:
        log.warning("health: only %d candles — returning neutral 50", len(candles))
        return HealthResult(50, 50, 0, False, False,
                            candles.iloc[-1]["date"].isoformat() if len(candles) else "")

    ce = direction == "CE"    # True = long NIFTY (bullish), False = short (bearish)

    vwap_series = compute_vwap(candles)
    rsi_series  = compute_rsi(candles["close"])
    pdi, ndi    = compute_dmi(candles)

    last   = candles.iloc[-1]
    prev   = candles.iloc[-2]
    candle_ts = pd.Timestamp(last["date"]).isoformat()

    close      = last["close"]
    prev_close = prev["close"]
    vwap_now   = vwap_series.iloc[-1]

    # ── Raw conditions ─────────────────────────────────────────────────────────
    # C1: momentum — close vs prev close, correct direction
    c1 = (close > prev_close) if ce else (close < prev_close)

    # C2: VWAP — price on correct side
    c2 = (close > vwap_now) if ce else (close < vwap_now)

    # C3: RSI slope correct over last 3 candles
    if len(rsi_series.dropna()) >= 3:
        r0 = rsi_series.iloc[-1]
        r1 = rsi_series.iloc[-2]
        r2 = rsi_series.iloc[-3]
        c3 = (r0 > r1 > r2) if ce else (r0 < r1 < r2)
    else:
        c3 = False

    # C4: DMI dominance — +DI/-DI > 25 on correct side
    pdi_now = pdi.iloc[-1]
    ndi_now = ndi.iloc[-1]
    if ce:
        c4 = (pdi_now > 25) and (pdi_now > ndi_now)
    else:
        c4 = (ndi_now > 25) and (ndi_now > pdi_now)

    # ── Raw score ──────────────────────────────────────────────────────────────
    w = config.HEALTH_WEIGHTS
    raw_score = (
        (w["C2_vwap"] if c2 else 0) +
        (w["C4_dmi"]  if c4 else 0) +
        (w["C3_rsi"]  if c3 else 0) +
        (w["C1_mom"]  if c1 else 0)
    )

    log.info(
        "health: dir=%s C1=%s C2=%s C3=%s C4=%s raw=%d  vwap=%.1f close=%.1f pdi=%.1f ndi=%.1f",
        direction, c1, c2, c3, c4, raw_score, vwap_now, close, pdi_now, ndi_now,
    )

    # ── VWAP veto ──────────────────────────────────────────────────────────────
    if c2:
        new_vwap_lost = 0
    else:
        new_vwap_lost = prev_vwap_lost_consec + 1

    exit_vwap = new_vwap_lost >= config.VWAP_LOST_EXIT

    effective_score = raw_score
    if not exit_vwap and new_vwap_lost == 1:
        # Drop one tier
        if raw_score >= config.HEALTH_HEALTHY:
            effective_score = config.HEALTH_CAUTION      # healthy → caution tier
        elif raw_score >= config.HEALTH_CAUTION:
            effective_score = config.HEALTH_CAUTION - 1  # caution → faded

    if exit_vwap:
        effective_score = 0
        log.warning("health: VWAP lost %d consecutive candles — VWAP EXIT signal", new_vwap_lost)

    # ── Reversal detection ─────────────────────────────────────────────────────
    # Opposite side's C1+C2 both fire → exit immediately
    c1_opposite = (close < prev_close) if ce else (close > prev_close)
    c2_opposite = (close < vwap_now)   if ce else (close > vwap_now)
    exit_reversal = c1_opposite and c2_opposite
    if exit_reversal:
        log.warning("health: reversal detected (opposite C1+C2) — REVERSAL EXIT signal")

    return HealthResult(
        raw_score=raw_score,
        effective_score=effective_score,
        vwap_lost_consec=new_vwap_lost,
        exit_vwap=exit_vwap,
        exit_reversal=exit_reversal,
        candle_ts=candle_ts,
    )
