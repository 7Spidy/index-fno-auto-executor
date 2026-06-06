# Frozen constants — v1.  Source of truth: fno-auto-executor-spec-v1.md §18.
# Do NOT change any value without updating the spec and the FROZEN comment.

# Instrument
INSTRUMENT           = "NIFTY"
USE_WEEKLY           = True          # Tuesday expiry
STRIKE_STEP          = 50

# Sizing
CAPITAL_RS           = 1_00_000      # ₹1,00,000 paper capital
RISK_PCT             = 0.02          # 2% per trade → ₹2,000 max risk

# Signal inheritance
ATM_DELTA            = 0.50
TARGET_RR            = 3.0
MAX_RISK_POINTS      = 25

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

# Daily limits — deferred to v2 (live phase)
MAX_TRADES_DAY       = None
DAILY_LOSS_LIMIT     = None

# Mode — flip to False for live
PAPER_MODE           = True
