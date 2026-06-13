"""
run.py — GH Actions entrypoint.  One complete tick per invocation.  Spec §0, §2.

Flow:
  1. Connect Redis + build KiteClient (uses token from morning-login.yml).
  2. Build gateway (paper or live).
  3. Fetch option LTP + NIFTY spot.
  4. Advance paper orders to current LTP (process_tick).
  5. Load position from Redis; run startup reconcile.
  6. Hard square-off guard (15:10 IST).
  7. Route by phase:
       IDLE / no position   → check pending_intent → run entry gate → try_enter
       ENTERING             → check_entry_fill
       OPEN_FIXED/LOCKED/RUNNER → manage_position
       EXITING              → check_exit_complete
       COOLDOWN             → check_cooldown_elapsed
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta

import redis as redis_lib

from executor import config, gates, manager, journal
from executor import state as state_module
from executor.gateway.paper import PaperGateway
from executor.gateway.kite_live import KiteLiveGateway
from executor.utils import auth
from executor.utils.calendar_nse import now_ist, ist_hhmm, IST
from executor.utils.kite_client import KiteClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("run")

# ── Config overrides from environment ─────────────────────────────────────────
# Allow CI to assert paper mode via env var even if config.PAPER_MODE = False.
_PAPER_MODE = os.getenv("PAPER_MODE", str(config.PAPER_MODE)).lower() not in ("false", "0")


def _connect_redis() -> redis_lib.Redis:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        raise RuntimeError(
            "REDIS_URL is empty or unset. Set the REDIS_URL repository secret "
            "(GitHub → Settings → Secrets and variables → Actions) to your "
            "Upstash connection string, e.g. rediss://default:<password>@<host>:<port>"
        )
    if not url.startswith(("redis://", "rediss://", "unix://")):
        raise RuntimeError(
            f"REDIS_URL has an invalid scheme: {url.split('://', 1)[0]!r}. "
            "It must start with one of: redis://, rediss:// (Upstash uses rediss://), unix://"
        )
    r = redis_lib.from_url(url, decode_responses=False)
    r.ping()
    return r


def _build_kite(r: redis_lib.Redis) -> KiteClient:
    api_key      = os.environ["KITE_API_KEY"]
    access_token = auth.get_access_token(r)
    return KiteClient(api_key=api_key, access_token=access_token)


def _fetch_candles(kite: KiteClient, r: redis_lib.Redis) -> "pd.DataFrame":
    """Fetch last 20 5-min NIFTY spot candles ending now."""
    import pandas as pd
    from datetime import timedelta
    now = datetime.now(IST)
    from_dt = now - timedelta(hours=3)   # enough to cover 20 × 5-min candles
    token = auth.get_nifty_spot_token(r)
    try:
        df = kite.get_historical_candles(token, from_dt, now, interval="5minute")
        if len(df) > 20:
            df = df.iloc[-20:].reset_index(drop=True)
        return df
    except Exception as exc:
        log.warning("_fetch_candles: failed (%s) — returning empty DataFrame", exc)
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])


def main() -> None:
    log.info("=== executor tick start mode=%s ===", "PAPER" if _PAPER_MODE else "LIVE")

    # ── 1. Connect ─────────────────────────────────────────────────────────────
    try:
        r    = _connect_redis()
        kite = _build_kite(r)
    except Exception as exc:
        log.error("startup failed: %s", exc)
        sys.exit(1)

    # ── 2. Gateway ─────────────────────────────────────────────────────────────
    if _PAPER_MODE:
        lot_size = auth.get_lot_size(r, config.INSTRUMENT)
        gateway = PaperGateway(r, lot_size)
    else:
        gateway = KiteLiveGateway(kite)

    # ── 3. Market data ─────────────────────────────────────────────────────────
    pos = state_module.load_position(r)
    tradingsymbol = pos["tradingsymbol"] if pos else None

    option_ltp: float = 0.0
    spot_ltp: float   = 0.0

    try:
        nifty_ltp_map = kite.get_ltp(["NSE:NIFTY 50"])
        spot_ltp = nifty_ltp_map.get("NSE:NIFTY 50", 0.0)
    except Exception as exc:
        log.error("NIFTY spot LTP fetch failed: %s", exc)

    if tradingsymbol:
        try:
            opt_map    = kite.get_ltp([f"NFO:{tradingsymbol}"])
            option_ltp = opt_map.get(f"NFO:{tradingsymbol}", 0.0)
        except Exception as exc:
            log.error("option LTP fetch failed for %s: %s", tradingsymbol, exc)

    # ── 4. Advance paper orders ────────────────────────────────────────────────
    if _PAPER_MODE and isinstance(gateway, PaperGateway):
        if option_ltp > 0:
            gateway.set_current_ltp(option_ltp)
        filled = gateway.process_tick()
        if filled:
            log.info("paper tick: filled orders %s", filled)

    # ── 5. Reload position + reconcile ─────────────────────────────────────────
    pos = state_module.load_position(r)
    if pos and pos["phase"] in ("OPEN_FIXED", "LOCKED", "RUNNER", "ENTERING"):
        pos = gateway.reconcile(pos)
        state_module.save_position(r, pos)

    # ── 6. Hard square-off guard ───────────────────────────────────────────────
    now = now_ist()
    squareoff_time = ist_hhmm(config.SQUAREOFF_IST, now)
    if now >= squareoff_time:
        if pos and pos.get("phase") in ("OPEN_FIXED", "LOCKED", "RUNNER", "ENTERING"):
            log.warning("past %s — hard squareoff", config.SQUAREOFF_IST)
            manager.force_squareoff(gateway, pos, r, option_ltp=option_ltp)
            pos = state_module.load_position(r)
        else:
            log.info("past %s, no open position — nothing to do", config.SQUAREOFF_IST)
            return

    # ── 7. Phase routing ───────────────────────────────────────────────────────
    if pos is None or pos.get("phase") in (None, "IDLE"):
        _run_idle(r, kite, gateway, option_ltp, spot_ltp)

    elif pos["phase"] == "ENTERING":
        manager.check_entry_fill(gateway, pos, r, spot_ltp, option_ltp)

    elif pos["phase"] in ("OPEN_FIXED", "LOCKED", "RUNNER"):
        if option_ltp <= 0:
            log.error("option LTP unavailable — skipping management tick")
            return
        candles = _fetch_candles(kite, r)
        manager.manage_position(gateway, pos, r, option_ltp, spot_ltp, candles)
        # Check if manage_position transitioned to EXITING this tick
        pos = state_module.load_position(r)
        if pos and pos.get("phase") == "EXITING":
            manager.check_exit_complete(gateway, pos, r)
            _journal_if_cooldown(r, gateway)

    elif pos["phase"] == "EXITING":
        manager.check_exit_complete(gateway, pos, r)
        _journal_if_cooldown(r, gateway)

    elif pos["phase"] == "COOLDOWN":
        manager.check_cooldown_elapsed(pos, r)

    log.info("=== executor tick end ===")


def _run_idle(
    r: redis_lib.Redis,
    kite: KiteClient,
    gateway,
    option_ltp: float,
    spot_ltp: float,
) -> None:
    """Check for pending intent; run entry gate; enter if passes."""
    intent = state_module.load_intent(r)
    if not intent:
        log.info("idle: no pending intent")
        return

    log.info("idle: pending intent found ts=%s sym=%s",
             intent.get("ts"), intent.get("tradingsymbol"))

    ok, reason = gates.check_all(intent, r, kite)
    if not ok:
        log.info("gate FAIL: %s", reason)
        state_module.discard_intent(r, reason)
        journal.notify_gate_fail(reason, intent)
        return

    log.info("gate PASS — entering")
    consumed = state_module.consume_intent(r)
    if not consumed:
        log.warning("idle: intent vanished between gate check and consume — skipping")
        return

    # Fetch option LTP for the intent symbol (needed for MARKET fill price)
    ts = intent.get("tradingsymbol", "")
    try:
        ltp_map = kite.get_ltp([f"NFO:{ts}"])
        entry_ltp = ltp_map.get(f"NFO:{ts}", 0.0)
    except Exception as exc:
        log.error("entry LTP fetch failed for %s: %s", ts, exc)
        return

    if entry_ltp <= 0:
        log.error("entry LTP is 0 for %s — aborting", ts)
        return

    if _PAPER_MODE and isinstance(gateway, PaperGateway):
        gateway.set_current_ltp(entry_ltp)

    manager.try_enter(intent, gateway, r)
    journal.notify_entry(state_module.load_position(r) or {})

    # Immediately check fill (paper mode fills synchronously)
    pos = state_module.load_position(r)
    if pos and pos["phase"] == "ENTERING":
        manager.check_entry_fill(gateway, pos, r, spot_ltp, entry_ltp)
        pos = state_module.load_position(r)
        if pos and pos["phase"] == "OPEN_FIXED":
            journal.notify_entry(pos)


def _journal_if_cooldown(r: redis_lib.Redis, gateway) -> None:
    """
    Log trade to Notion + Discord once when we first enter COOLDOWN.
    The notion_journaled flag prevents duplicate rows on subsequent runs
    during the 15-minute cooldown window (executor runs every 1 minute).
    """
    pos = state_module.load_position(r)
    if pos and pos.get("phase") == "COOLDOWN" and not pos.get("notion_journaled"):
        journal.log_trade_to_notion(pos)
        journal.notify_exit(pos)
        pos["notion_journaled"] = True
        state_module.save_position(r, pos)


if __name__ == "__main__":
    main()
