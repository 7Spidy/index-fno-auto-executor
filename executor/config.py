# Frozen constants — v1.  Source of truth: fno-auto-executor-spec-v1.md §18.
# Do NOT change any value without updating the spec and the FROZEN comment.
# NOTE: DAILY_LOSS_PCT/DAILY_LOSS_LIMIT/PAPER_MODE are superseded by
# claude_change_spec_repo2.md (live-phase daily-loss breaker) — see §18 note.
# NOTE: instrument universe, ladder SL, and Repo 1 charges parity are per
# the 17-instrument claude_change_spec_repo2.md rewrite — see that file.

import os

from executor.instruments import INDICES, STOCKS, ALL_INSTRUMENT_NAMES  # noqa: F401 — re-exported for config.INDICES/STOCKS/ALL_INSTRUMENT_NAMES callers (e.g. run.py)

# Sizing
CAPITAL_RS           = 50_000        # Paper-mode fixed capital, mirrors Repo 1 DAILY_CAPITAL.
                                       # Live mode uses kite.get_margins() instead — see sizing.py.

# Signal inheritance
ATM_DELTA            = 0.50
TARGET_RR            = 1.5  # changed 3.0 → 1.5 (2026-06-10); base target only, runner mode unchanged

# Entry gate
COOLDOWN_CANDLES     = 3             # × 5 min = 15 min
INTENT_TTL_MIN       = 6

# Entry execution — marketable LIMIT, not MARKET. Buffer ensures near-certain
# fill (like a market order) while capping worst-case slippage. All entries
# are BUY (long option premium), so the buffer is always added above LTP.
ENTRY_LIMIT_BUFFER_PCT      = 0.01   # 1% above fetched LTP — tune if fills are missed or slippage is worse than expected
ENTRY_FILL_RETRY_ATTEMPTS   = 3
ENTRY_FILL_RETRY_DELAY_SECS = 2

# Paper fill model
PAPER_SPREAD         = 0.75         # ₹ bid-ask; half-spread applied per side

# Timing (IST, "HH:MM")
EVAL_WINDOW_START    = "09:40"
NO_NEW_ENTRY         = "14:45"
SQUAREOFF_IST        = "15:10"
COOLDOWN_AFTER_EXIT  = 15           # minutes in COOLDOWN phase before IDLE

# Daily limits — v2 (live phase), ported from Repo 1 (src/paper_engine.py)
MAX_TRADES_DAY       = None
DAILY_LOSS_PCT       = 0.15

# Dynamic lot sizing (live mode only) — decided once per day, see sizing.py /
# run.py's main(). Paper mode always uses multiplier=1 (unchanged behaviour).
LOT_MULTIPLIER_CAPITAL_THRESHOLD = 50_000   # capital >= this -> multiplier 2, else 1
# DAILY_LOSS_LIMIT is no longer a static constant — live mode needs it computed
# against real-time margins. Use sizing.get_daily_loss_limit(paper_mode, kite) instead.

def _num(env_name: str, default: float) -> float:
    """Read a numeric env-var override, falling back to `default` if unset
    or unparseable. Mirrors Repo 1's env-override convention."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


# Intra-minute trailing-SL sub-loop (OPEN-position management only — never
# entry gates). EXEC_SUBLOOPS=1 fully reverts to today's single-pass
# behaviour with no code change.
TRACKER_SUBLOOPS      = int(_num("EXEC_SUBLOOPS", 4))       # total passes per tick, incl. the first
TRACKER_SUBLOOP_SECS  = _num("EXEC_SUBLOOP_SECS", 15.0)      # seconds between sub-loop passes
# Wall-clock budget from job start (EXEC_JOB_START_EPOCH, exported as the
# workflow's first step) within which the sub-loop must complete — leaves
# headroom below the GitHub Actions `timeout-minutes: 2` (120s) job limit to
# account for checkout/pip-install cold start and processing time per pass.
TRACKER_JOB_BUDGET_SECS = _num("EXEC_JOB_BUDGET_SECS", 100.0)

# Mode — sourced from GitHub Actions repo/environment variable PAPER_MODE
# ("true"/"false", case-insensitive). Defaults to True (safe) if unset.
# This is the fallback default only — executor/run.py resolves the effective
# mode per-tick as: Redis override (executor:paper_mode_override) > this env
# var > this default. See executor/state.py get_paper_mode_override().
PAPER_MODE           = os.environ.get("PAPER_MODE", "true").strip().lower() == "true"
