"""
run.py — GH Actions entrypoint.  One complete tick per invocation, looping
all 17 instruments (3 indices + 14 stocks — see executor/instruments.py).

Flow:
  1. Connect Redis + build KiteClient (token from Repo 1's shared kite:* cache
     written by Repo 1's morning-login.yml — see executor/utils/auth.py).
  2. Build gateway (paper or live) — one instance shared across all 17
     instruments this tick.
  3. For each instrument:
       Fetch option LTP + underlying spot/futures LTP.
       Advance paper orders for this instrument's tradingsymbol only.
       Load position from Redis; run startup reconcile.
       Hard square-off guard (15:10 IST) — per instrument.
       Route by phase:
         IDLE / no position   → check pending_intent → run entry gate → try_enter
         ENTERING             → check_entry_fill (+ bounded same-tick retry)
         OPEN                 → manage_position
         EXITING              → check_exit_complete
         COOLDOWN             → check_cooldown_elapsed
     One instrument's failure must never block the other 16.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta

import redis as redis_lib

from executor import config, gates, manager, journal, sizing
from executor import state as state_module
from executor.gateway.paper import PaperGateway
from executor.gateway.kite_live import KiteLiveGateway
from executor.utils import auth
from executor.utils.calendar_nse import now_ist, ist_hhmm, IST
from executor.utils.indicators import compute_rsi
from executor.utils.kite_client import KiteClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("run")

# ── Config overrides from environment ─────────────────────────────────────────
# Allow CI to assert paper mode via env var even if config.PAPER_MODE = False.
# Resolution order (highest precedence first), re-evaluated every tick since
# each run is a fresh, stateless process:
#   1. Redis key executor:paper_mode_override (set via state.set_paper_mode_override)
#   2. PAPER_MODE env var / GitHub Actions repo variable
#   3. config.PAPER_MODE default
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


def _get_rsi_snapshot(
    kite: KiteClient, r: redis_lib.Redis, instrument: str, pos: dict | None = None,
) -> list[float] | None:
    """Port of Repo 1's position_tracker._get_rsi_snapshot: fetch OHLCV via
    the instrument's underlying token, compute RSI, take the last 3 values.

    Used only by the ladder's compute_ai_adjusted_sl — a fetch failure here
    just means the AI-adjusted tightening is skipped this tick (the ladder
    SL itself does not depend on RSI).

    For dynamic stock positions, `auth.get_underlying_token()` only checks
    the static caches and will raise — prefer the position's own snapshotted
    `equity_token` (set at entry, see state.fresh_position_from_intent)
    instead.
    """
    try:
        if pos and pos.get("is_dynamic") and pos.get("equity_token"):
            token = pos["equity_token"]
        else:
            token = auth.get_underlying_token(r, instrument)
        now = datetime.now(IST)
        from_dt = now - timedelta(days=5)   # warm-up for RSI(14) across weekends/holidays
        df = kite.get_historical_candles(token, from_dt, now, interval="5minute")
        if df.empty:
            return None
        rsi_series = compute_rsi(df["close"])
        last3 = rsi_series.dropna().iloc[-3:]
        if len(last3) < 3:
            return None
        return list(last3)
    except Exception as exc:
        log.warning("_get_rsi_snapshot(%s): %s", instrument, exc)
        return None


def main() -> None:
    global _PAPER_MODE

    # ── 1. Connect ─────────────────────────────────────────────────────────────
    try:
        r    = _connect_redis()
        kite = _build_kite(r)
    except Exception as exc:
        log.error("startup failed: %s", exc)
        sys.exit(1)

    # Redis override takes precedence over the env var, re-checked every tick.
    override = state_module.get_paper_mode_override(r)
    if override is not None:
        _PAPER_MODE = override

    log.info("=== executor tick start mode=%s ===", "PAPER" if _PAPER_MODE else "LIVE")

    # ── 2. Gateway — one shared instance for all 17 instruments this tick ──────
    if _PAPER_MODE:
        gateway = PaperGateway(r)
    else:
        gateway = KiteLiveGateway(kite)

    # ── 2b. Lot multiplier — decided once per day, live mode only. Never
    # re-evaluated per-instrument or per-trade. Paper mode always 1x. ─────────
    if not _PAPER_MODE:
        date_str = now_ist().date().isoformat()
        lot_multiplier = state_module.get_lot_multiplier(r, date_str)
        if lot_multiplier is None:
            try:
                capital = kite.get_margins(r)
            except Exception as exc:
                log.error("morning capital check failed: %s — no trading until resolved", exc)
                return  # do not proceed with the tick; retry next minute
            lot_multiplier = sizing.decide_lot_multiplier(capital)
            set_ok = state_module.set_lot_multiplier_if_absent(r, date_str, lot_multiplier)
            if not set_ok:
                lot_multiplier = state_module.get_lot_multiplier(r, date_str)
            log.info("lot multiplier for %s decided: capital=₹%.0f -> x%d",
                      date_str, capital, lot_multiplier)
    else:
        lot_multiplier = 1

    now = now_ist()
    squareoff_time = ist_hhmm(config.SQUAREOFF_IST, now)
    past_squareoff = now >= squareoff_time

    # ── 2c. Dynamic stock universe (Repo 1's daily top-gainer/top-loser
    # picks) — read fresh every tick, NEVER cached daily (unlike the lot
    # multiplier). Fail-open: state.get_dynamic_instruments() never raises;
    # a bad/missing/stale payload just means zero dynamic picks this tick. ──
    today_str = now.date().isoformat()
    dynamic_instruments = state_module.get_dynamic_instruments(r, today_str)
    if dynamic_instruments:
        log.info("dynamic instruments today: %s", [d["name"] for d in dynamic_instruments])

    # ── 3. Loop all instruments (static 17 + today's dynamic picks) —
    # single pass: gates/entry/exit + first OPEN-position management pass,
    # as today. Do NOT put this inside the sub-loop. ─────────────────────────
    for inst_cfg in config.INDICES + config.STOCKS + dynamic_instruments:
        instrument = inst_cfg["name"]
        exchange   = inst_cfg.get("fno_exchange", "NFO")
        try:
            _run_one_instrument(r, kite, gateway, instrument, exchange, past_squareoff, lot_multiplier)
        except Exception as exc:
            # One instrument's failure must never block the others —
            # mirrors Repo 1 main.py's per-instrument exception isolation.
            log.error("instrument %s tick failed: %s", instrument, exc)

    # ── 3b. Intra-minute trailing-SL sub-loop — OPEN-position management only.
    # Does NOT re-run gates.check_all(), manager.try_enter(), or the
    # kill-switch check; only manage_position()/exit-check repeats. ──────────
    _run_trailing_subloop(r, kite, gateway, past_squareoff, squareoff_time, dynamic_instruments)

    # ── 4. Update consolidated Discord tracker — once per tick, always ─────────
    try:
        journal.update_consolidated_tracker(r)
    except Exception as exc:
        log.error("consolidated tracker update failed: %s", exc)

    log.info("=== executor tick end ===")


def _run_trailing_subloop(
    r: redis_lib.Redis,
    kite: KiteClient,
    gateway,
    past_squareoff: bool,
    squareoff_time: datetime,
    dynamic_instruments: list[dict] | None = None,
) -> None:
    """Up to TRACKER_SUBLOOPS - 1 additional OPEN-position management passes,
    spaced TRACKER_SUBLOOP_SECS apart, budget-anchored to EXEC_JOB_START_EPOCH
    (exported by the workflow as its first step). EXEC_SUBLOOPS=1 reverts to
    today's single-pass behaviour (loop below never executes)."""
    if config.TRACKER_SUBLOOPS <= 1 or past_squareoff:
        return

    job_start_epoch = float(os.environ.get("EXEC_JOB_START_EPOCH", time.time()))

    open_instruments: list[tuple[str, str]] = []
    for inst_cfg in config.INDICES + config.STOCKS + list(dynamic_instruments or []):
        instrument = inst_cfg["name"]
        pos = state_module.load_position(r, instrument)
        if pos and pos.get("phase") == "OPEN":
            open_instruments.append((instrument, inst_cfg.get("fno_exchange", "NFO")))

    for pass_num in range(1, config.TRACKER_SUBLOOPS):
        if not open_instruments:
            log.info("subloop: no OPEN instruments remain — stopping early (pass %d)", pass_num)
            return
        if now_ist() >= squareoff_time:
            log.info("subloop: past squareoff — stopping early (pass %d)", pass_num)
            return
        elapsed = time.time() - job_start_epoch
        remaining = config.TRACKER_JOB_BUDGET_SECS - elapsed
        if remaining < config.TRACKER_SUBLOOP_SECS:
            log.info("subloop: budget exhausted (remaining=%.1fs) — stopping early (pass %d)",
                      remaining, pass_num)
            return

        time.sleep(config.TRACKER_SUBLOOP_SECS)

        log.info("subloop: pass %d/%d — managing %d OPEN instrument(s)",
                  pass_num + 1, config.TRACKER_SUBLOOPS, len(open_instruments))
        open_instruments = _manage_open_positions_pass(r, kite, gateway, open_instruments)


def _manage_open_positions_pass(
    r: redis_lib.Redis,
    kite: KiteClient,
    gateway,
    open_instruments: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """One sub-loop pass: trailing SL / exit-trigger check only for
    currently-OPEN instruments. No entry gates, no try_enter. Per-instrument
    exception isolation, same as the main tick. Returns the instruments still
    OPEN after this pass."""
    still_open: list[tuple[str, str]] = []
    for instrument, exchange in open_instruments:
        try:
            pos = state_module.load_position(r, instrument)
            if not pos or pos.get("phase") != "OPEN":
                continue
            tradingsymbol = pos["tradingsymbol"]

            try:
                opt_map = kite.get_ltp([f"{exchange}:{tradingsymbol}"])
                option_ltp = opt_map.get(f"{exchange}:{tradingsymbol}", 0.0)
            except Exception as exc:
                log.error("%s: subloop option LTP fetch failed: %s", instrument, exc)
                still_open.append((instrument, exchange))
                continue

            if isinstance(gateway, PaperGateway) and option_ltp > 0:
                gateway.set_current_ltp(option_ltp)
                gateway.process_tick(tradingsymbol)

            pos = state_module.load_position(r, instrument)
            if pos and pos.get("phase") == "OPEN":
                pos = gateway.reconcile(pos)
                state_module.save_position(r, instrument, pos)

            if option_ltp <= 0:
                log.error("%s: subloop option LTP unavailable — skipping this pass", instrument)
                still_open.append((instrument, exchange))
                continue

            rsi_last3 = _get_rsi_snapshot(kite, r, instrument, pos)
            manager.manage_position(gateway, pos, r, option_ltp, rsi_last3)

            pos = state_module.load_position(r, instrument)
            if pos and pos.get("phase") == "EXITING":
                manager.check_exit_complete(gateway, pos, r, kite)
                _journal_if_cooldown(r, instrument)
                continue

            if pos and pos.get("phase") == "OPEN":
                still_open.append((instrument, exchange))
        except Exception as exc:
            # One instrument's failure must never block the others' trailing
            # SL updates.
            log.error("subloop: instrument %s failed: %s", instrument, exc)
            still_open.append((instrument, exchange))
    return still_open


def _run_one_instrument(
    r: redis_lib.Redis,
    kite: KiteClient,
    gateway,
    instrument: str,
    exchange: str,
    past_squareoff: bool,
    lot_multiplier: int = 1,
) -> None:
    pos = state_module.load_position(r, instrument)
    tradingsymbol = pos["tradingsymbol"] if pos else None

    option_ltp: float = 0.0
    spot_ltp: float   = 0.0

    try:
        underlying_token = auth.get_underlying_token(r, instrument)
        spot_map = kite.get_ltp([str(underlying_token)])
        spot_ltp = spot_map.get(str(underlying_token), 0.0)
    except Exception as exc:
        log.error("%s: underlying LTP fetch failed: %s", instrument, exc)

    if tradingsymbol:
        try:
            opt_map    = kite.get_ltp([f"{exchange}:{tradingsymbol}"])
            option_ltp = opt_map.get(f"{exchange}:{tradingsymbol}", 0.0)
        except Exception as exc:
            log.error("%s: option LTP fetch failed for %s: %s", instrument, tradingsymbol, exc)

    # ── Advance paper orders for THIS instrument's tradingsymbol only ──────────
    if isinstance(gateway, PaperGateway):
        if option_ltp > 0:
            gateway.set_current_ltp(option_ltp)
            filled = gateway.process_tick(tradingsymbol)
            if filled:
                log.info("%s: paper tick filled orders %s", instrument, filled)

    # ── Reload position + reconcile ─────────────────────────────────────────
    pos = state_module.load_position(r, instrument)
    if pos and pos["phase"] in ("OPEN", "ENTERING"):
        pos = gateway.reconcile(pos)
        state_module.save_position(r, instrument, pos)

    # ── Hard square-off guard ───────────────────────────────────────────────
    if past_squareoff:
        if pos and pos.get("phase") in ("OPEN", "ENTERING"):
            log.warning("%s: past %s — hard squareoff", instrument, config.SQUAREOFF_IST)
            manager.force_squareoff(gateway, pos, r, option_ltp=option_ltp)
            pos = state_module.load_position(r, instrument)
        else:
            log.info("%s: past %s, no open position — nothing to do", instrument, config.SQUAREOFF_IST)
            return

    # ── Phase routing ───────────────────────────────────────────────────────
    if pos is None or pos.get("phase") in (None, "IDLE"):
        _run_idle(r, kite, gateway, instrument, exchange, option_ltp, spot_ltp, lot_multiplier)

    elif pos["phase"] == "ENTERING":
        manager.check_entry_fill(gateway, pos, r, spot_ltp, option_ltp)

    elif pos["phase"] == "OPEN":
        if option_ltp <= 0:
            log.error("%s: option LTP unavailable — skipping management tick", instrument)
            return
        rsi_last3 = _get_rsi_snapshot(kite, r, instrument, pos)
        manager.manage_position(gateway, pos, r, option_ltp, rsi_last3)
        # Check if manage_position transitioned to EXITING this tick
        pos = state_module.load_position(r, instrument)
        if pos and pos.get("phase") == "EXITING":
            manager.check_exit_complete(gateway, pos, r, kite)
            _journal_if_cooldown(r, instrument)

    elif pos["phase"] == "EXITING":
        manager.check_exit_complete(gateway, pos, r, kite)
        _journal_if_cooldown(r, instrument)

    elif pos["phase"] == "COOLDOWN":
        manager.check_cooldown_elapsed(pos, r)


def _run_idle(
    r: redis_lib.Redis,
    kite: KiteClient,
    gateway,
    instrument: str,
    exchange: str,
    option_ltp: float,
    spot_ltp: float,
    lot_multiplier: int = 1,
) -> None:
    """Check for pending intent; run entry gate; enter if passes."""
    intent = state_module.load_intent(r, instrument)
    if not intent:
        log.info("%s: idle: no pending intent", instrument)
        return

    log.info("%s: idle: pending intent found ts=%s sym=%s",
             instrument, intent.get("ts"), intent.get("tradingsymbol"))

    ok, reason = gates.check_all(intent, r, kite, exchange)
    if not ok:
        log.info("%s: gate FAIL: %s", instrument, reason)
        state_module.discard_intent(r, instrument, reason)
        journal.notify_gate_fail(reason, intent)
        return

    log.info("%s: gate PASS — entering", instrument)
    consumed = state_module.consume_intent(r, instrument)
    if not consumed:
        log.warning("%s: idle: intent vanished between gate check and consume — skipping", instrument)
        return

    # Fetch option LTP for the intent symbol (needed for marketable-LIMIT price)
    ts = intent.get("tradingsymbol", "")
    try:
        ltp_map = kite.get_ltp([f"{exchange}:{ts}"])
        entry_ltp = ltp_map.get(f"{exchange}:{ts}", 0.0)
    except Exception as exc:
        log.error("%s: entry LTP fetch failed for %s: %s", instrument, ts, exc)
        return

    if entry_ltp <= 0:
        log.error("%s: entry LTP is 0 for %s — aborting", instrument, ts)
        return

    if isinstance(gateway, PaperGateway):
        gateway.set_current_ltp(entry_ltp)

    manager.try_enter(intent, gateway, r, kite, entry_ltp, exchange, lot_multiplier=lot_multiplier)

    # Bounded same-tick retry on the fill check — the marketable-LIMIT entry
    # may not register as filled at the instant try_enter places it; without
    # this, the position would sit with no SL until the *next* cron tick (up
    # to ~1 minute later).
    pos = state_module.load_position(r, instrument)
    if pos and pos["phase"] == "ENTERING":
        for attempt in range(config.ENTRY_FILL_RETRY_ATTEMPTS):
            manager.check_entry_fill(gateway, pos, r, spot_ltp, entry_ltp)
            pos = state_module.load_position(r, instrument)
            if not pos or pos.get("phase") != "ENTERING":
                break
            if attempt < config.ENTRY_FILL_RETRY_ATTEMPTS - 1:
                time.sleep(config.ENTRY_FILL_RETRY_DELAY_SECS)


def _journal_if_cooldown(r: redis_lib.Redis, instrument: str) -> None:
    """
    Log trade to Notion once when we first enter COOLDOWN, and append it to
    today's closed-trade list for the consolidated Discord tracker.
    The notion_journaled flag prevents duplicate rows/entries on subsequent
    runs during the 15-minute cooldown window (executor runs every 1 minute).
    """
    pos = state_module.load_position(r, instrument)
    if pos and pos.get("phase") == "COOLDOWN" and not pos.get("notion_journaled"):
        journal.log_trade_to_notion(pos)
        date_str = datetime.now(IST).date().isoformat()
        state_module.append_closed_today(r, date_str, pos)
        pos["notion_journaled"] = True
        state_module.save_position(r, instrument, pos)


if __name__ == "__main__":
    main()
