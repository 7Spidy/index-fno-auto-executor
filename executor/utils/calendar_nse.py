"""Minimal NSE calendar helpers — IST time utilities and market-hours checks."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytz

IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def ist_hhmm(hhmm: str, base: datetime | None = None) -> datetime:
    """Convert "HH:MM" string to a timezone-aware IST datetime for today (or base)."""
    h, m = map(int, hhmm.split(":"))
    ref = (base or now_ist()).astimezone(IST)
    return ref.replace(hour=h, minute=m, second=0, microsecond=0)


def last_completed_5min_open(now: datetime | None = None) -> datetime:
    """
    Return the open-timestamp of the most recently completed 5-min candle.
    At 10:07 → 10:00.  At 10:04 → 09:55.
    """
    t = (now or now_ist()).astimezone(IST)
    floored = t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)
    return floored - timedelta(minutes=5)
