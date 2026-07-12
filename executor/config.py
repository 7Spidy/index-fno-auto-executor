# Frozen constants — v1.  Source of truth: fno-auto-executor-spec-v1.md §18.
# Do NOT change any value without updating the spec and the FROZEN comment.
# NOTE: DAILY_LOSS_PCT/DAILY_LOSS_LIMIT/PAPER_MODE are superseded by
# claude_change_spec_repo2.md (live-phase daily-loss breaker) — see §18 note.

import os

# Instrument
INSTRUMENT           = "NIFTY"

# Sizing
CAPITAL_RS           = 1_00_000      # Paper-mode fixed capital, mirrors Repo 1 DAILY_CAPITAL.
                                       # Live mode uses kite.get_margins() instead — see sizing.py.

# Signal inheritance
ATM_DELTA            = 0.50
TARGET_RR            = 1.5  # changed 3.0 → 1.5 (2026-06-10); base target only, runner mode unchanged

# Entry gate
VIX_MAX              = 22
COOLDOWN_CANDLES     = 3             # × 5 min = 15 min
INTENT_TTL_MIN       = 6

# Health score
HEALTH_WEIGHTS       = {"C2_vwap": 40, "C4_dmi": 25, "C3_rsi": 20, "C1_mom": 15}
HEALTH_HEALTHY       = 75
HEALTH_CAUTION       = 50
VWAP_LOST_EXIT       = 2            # consecutive 5-min candles

# Milestones
BREAKEVEN_AT         = 0.50         # fraction of T
LOCK_AT              = 0.90
LOCK_FRACTION        = 0.70         # lock 70% of T
RUNNER_GIVEBACK      = 0.10         # peak × 0.90
RUNNER_GIVEBACK_LATE = 0.05         # peak × 0.95 (past original target)

# Caution trailing
CAUTION_TRAIL_SWINGS = 3            # completed 5-min candles
CAUTION_ATR_BUFFER   = 0.10         # × ATR

# Theta time-stop
THETA_MINUTES        = 15
THETA_MIN_PROGRESS   = 0.25         # fraction of T

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
# DAILY_LOSS_LIMIT is no longer a static constant — live mode needs it computed
# against real-time margins. Use sizing.get_daily_loss_limit(paper_mode, kite) instead.

# Mode — sourced from GitHub Actions repo/environment variable PAPER_MODE
# ("true"/"false", case-insensitive). Defaults to True (safe) if unset.
# This is the fallback default only — executor/run.py resolves the effective
# mode per-tick as: Redis override (executor:paper_mode_override) > this env
# var > this default. See executor/state.py get_paper_mode_override().
PAPER_MODE           = os.environ.get("PAPER_MODE", "true").strip().lower() == "true"
