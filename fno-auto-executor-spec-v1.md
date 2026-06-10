# F&O Auto-Executor — Spec v1 (FROZEN)

**Companion to:** `7Spidy/index-fno-signal-bot`
**Author:** Avi
**Status:** Rules frozen for v1. Paper mode first. One position at a time.
**Instrument:** NIFTY only (BANKNIFTY deferred to v2)
**Premise:** When the signal bot fires (all 4 conditions on one side), automatically place the trade with SL + target, then manage it every minute — ratcheting the stop, judging trade health, and exiting at the right moment — using **frozen deterministic rules. No LLM in the live loop.**

---

## 0. Execution model — stateless runs via cron-job.org

The signal bot uses `workflow_dispatch` triggered by cron-job.org (more reliable than GH Actions built-in cron). The executor uses the **exact same pattern.** No persistent daemon, no WebSocket.

```
cron-job.org (every 5 min, market hours)
  → POST github.com/repos/.../actions/workflows/signal.yml/dispatches
  → GH Actions runs signal.yml
  → If signal fires: write trade intent to Upstash Redis
  → Discord alert

cron-job.org (every 1 min, 09:40–15:10 IST)
  → POST github.com/repos/.../actions/workflows/executor.yml/dispatches
  → GH Actions runs executor.yml
  → Read state from Redis
  → Fetch NIFTY option LTP via Kite REST (quote API)
  → Apply management rules
  → Modify/cancel orders via Kite API
  → Write state back to Redis
  → Journal to Notion + Discord
```

**Why this works:** SL-M and target limit orders live on the exchange. If the executor misses a run (GH Actions delay, cold start overrun), the position is still exchange-protected. The executor only does trailing — it cannot cause an unprotected position by being absent.

**cron-job.org schedule for executor.yml:**
- Frequency: every 1 minute
- UTC window: `04:10–09:40 UTC, Mon–Fri` (= 09:40–15:10 IST)
- Trigger URL: `POST https://api.github.com/repos/7Spidy/{repo}/actions/workflows/executor.yml/dispatches`
- Auth: GitHub PAT in cron-job.org header (same pattern as signal bot)

**GH Actions public repo:** minutes are free. ~330 runs/day × 22 trading days = ~7,200 runs/month, no billing concern.

**Pip caching:** use `cache: 'pip'` in `actions/setup-python` so each run costs ~15-20s cold start + ~5-10s execution, well within 1-min cadence.

---

## 1. Scope (v1)

- **One open position at a time.** Multi-position deferred to v2.
- **Instrument:** NIFTY only. ATM weekly options (Tuesday expiry), long premium, MIS intraday.
- **Paper mode is the first deliverable.** Same code path as live; only the order-placement layer is swapped for a simulator. Live is a config flag flipped *after* paper validation.
- **No max trades/day or daily loss limit** — paper trade phase; both deferred to v2 live.

---

## 2. Two workflows

### `morning-login.yml` (inherited, unchanged)
Daily ~09:05 IST: TOTP login → Redis token + instrument cache + option token cache + dashboard reset.
The executor reuses the `kite:access_token` this job writes to Redis.

### `signal.yml` (extended from current)
Every 5 min, 09:40–14:45 IST. Same 4-condition logic.
**One change:** on signal fire, additionally write the trade intent (§3) to Redis key `executor:pending_intent`. If a position is already open (key `executor:position` exists in Redis), **suppress the new intent** (v1 single-position lock).

### `executor.yml` (new)
Every 1 min, 09:40–15:10 IST.
1. Read `executor:position` from Redis.
2. If empty → check `executor:pending_intent` → if present, run **entry gate** (§4) → if passes, execute entry (§5).
3. If position open → fetch option LTP via Kite quote API → run **manager** (§6–§12) → write updated state back.
4. At 15:10 IST → hard square-off regardless.

---

## 3. Handoff contract — signal → executor (Redis payload)

Signal bot writes on fire:

```json
{
  "ts": "2026-06-08T10:15:00+05:30",
  "instrument": "NIFTY",
  "direction": "CE",
  "atm_strike": 24500,
  "tradingsymbol": "NIFTY2560824500CE",
  "spot_close": 24512.4,
  "spot_sl": 24487.4,
  "spot_risk_pts": 25.0,
  "target_rr": 1.5,
  "atm_delta": 0.50,
  "conviction": "label_only",
  "health_inputs": {
    "vwap": 24505.1, "rsi": 61.2, "pdi": 28.4, "ndi": 17.9,
    "prev_close": 24500.0
  }
}
```

Redis key: `executor:pending_intent` (TTL: 6 minutes — if the executor hasn't consumed it by then, the signal candle is stale; discard).

---

## 4. Entry gate (all must pass, else skip, delete intent, log)

1. **VIX ≤ 22** — India VIX fetched via Kite quote at runtime. Hard block.
2. **MAX_RISK_POINTS ≤ 25** (NIFTY). Wide candle → SL too far → skip.
3. **Cooldown** — `COOLDOWN_CANDLES = 3` (15 min). Check last signal timestamp in Redis.
4. **No open position** — `executor:position` key must not exist.
5. **Time** — current IST time within 09:40–14:45; intent `ts` not older than 6 min.
6. **Option tradable** — instrument exists in cached option tokens and LTP is non-zero.

---

## 5. Sizing and level derivation

**Inherit from signal; do not recompute.**

From the signal payload:
```
premium_risk   = spot_risk_pts × ATM_DELTA         # e.g. 25 × 0.50 = ₹12.5
entry_premium  = option LTP at fill (first executor run after intent)
sl_premium     = entry_premium − premium_risk
target_premium = entry_premium + (premium_risk × TARGET_RR)   # 1.5:1
```

**Sizing:**
```
lot_size = from Kite instrument master (fetched at morning-login, cached in Redis)
max_lots = floor(CAPITAL_RS × RISK_PCT / (premium_risk × lot_size))
qty      = max_lots × lot_size   (minimum 1 lot; if even 1 lot exceeds, skip)
```

> **Target note:** flat 1.5:1 R:R (base target; changed 3.0 → 1.5 on 2026-06-10). Conviction is a display label only — does NOT scale target in v1. Conviction-scaled R:R is a documented v1.1 toggle, off by default.

---

## 6. The two clocks

- **Health clock — every 5-min candle close.** Favourability judged on entry timeframe. 1-min health-scoring whipsaws on noise. In stateless-run model: executor fetches last 20 candles of NIFTY spot via Kite historical API, recomputes indicators, re-scores. Store `last_health_ts` in Redis; only re-score when the 5-min candle has closed since last score.
- **Price / SL clock — every 1-min run.** Milestones and ratchet run every executor call using live LTP.

**All money measured on option premium** (SL, target, milestones, P&L).
**All favourability measured on NIFTY spot** (VWAP / RSI / DMI on the index).

---

## 7. Health score (0–100, recomputed per 5-min candle close)

| Cond | Description | Weight |
|------|-------------|-------:|
| C2 | Price on correct side of VWAP | **40** |
| C4 | DMI dominance (+DI/−DI > 25, correct side) | **25** |
| C3 | RSI slope correct direction over 3 candles | **20** |
| C1 | Close vs prior close, correct side | **15** |

**Tiers:** `≥75` healthy · `50–74` caution · `<50` faded.

**VWAP veto (overrides score):**
- VWAP lost for **1 candle** → drop one tier regardless of score.
- VWAP lost **2 consecutive candles** → exit at market.

**Reversal:** opposite side's C1+C2 both fire → exit at market immediately.

---

## 8. Milestone ladder + runner mode (premium, every 1-min run)

Let `T` = target premium − entry premium (= `premium_risk × 1.5`).

> Base target 1.5R as of 2026-06-10; runner mode still trails past T when health ≥ 75.
Let `progress` = current LTP − entry premium (long) / entry premium − current LTP (short).

| Milestone | Condition | Action |
|-----------|-----------|--------|
| Breakeven | `progress ≥ 0.50 × T` | Move SL to entry premium |
| Fork | Breakeven + health ≥ 75 | Enter **RUNNER MODE** |
| Lock | `progress ≥ 0.90 × T` (non-runner) | Move SL to lock ~70% of move |
| Target | LTP hits target premium (non-runner) | Exit; cancel SL order |

**RUNNER MODE** (cancel target, trail the stop):
1. Cancel target limit order.
2. Every run: `trail_sl = peak_premium_seen × 0.90` (give back 10%), ratcheted — never lower.
3. Once `progress > T` (past original target): tighten to `peak_premium_seen × 0.95`.
4. Runner exits only on: health `<50`, VWAP lost twice, or hard square-off.

---

## 9. Caution-tier trailing (health 50–74)

SL = lowest low of last **3 completed 5-min candles** (long) / highest high (short), converted to premium via delta, with `0.1 × ATR` buffer. Subject to ratchet invariant — never loosens.

---

## 10. Theta time-stop

If **15 minutes** since entry AND `progress < 0.25 × T` → trade isn't working, decay is bleeding → drop to caution handling, exit on next adverse 1-min run (any tick below current SL level).

---

## 11. Risk parameters (v1 — paper)

```
CAPITAL_RS          = 1_00_000        # ₹1,00,000 paper capital
RISK_PCT            = 0.02            # risk 2% of capital per trade = ₹2,000 max risk/trade
MAX_TRADES_DAY      = None            # no limit (paper trade)
DAILY_LOSS_LIMIT    = None            # no limit (paper trade)
```

Per-trade sizing is still capped by the `RISK_PCT × CAPITAL` formula — paper mode doesn't mean unlimited sizing. This keeps position sizing realistic for when you switch to live.

---

## 12. Exit conditions (consolidated, in priority order)

1. SL-M hit on exchange (broker fill reported in position check).
2. Target hit — non-runner (LTP at or beyond target premium).
3. Runner give-back (LTP ≤ `peak × 0.90` / `peak × 0.95`).
4. Health `<50` → exit at market.
5. VWAP lost 2 consecutive 5-min candles → exit at market.
6. Reversal (opposite C1+C2) → exit at market.
7. Theta time-stop (15 min, no progress) → exit on next adverse run.
8. **Hard square-off 15:10 IST** — unconditional, market order.
9. **No new entries after 14:45 IST** (intent with `ts` after 14:45 is discarded).

---

## 13. Hard invariants (no code path may violate)

- **Monotonic ratchet:** SL only ever moves toward profit. Centralise in `propose_sl(new_sl)` which rejects any call that loosens the stop.
- **One position** (v1) — enforced by `executor:position` key in Redis.
- **Mandatory SL on exchange at all times** — after entry fill, if SL order is found cancelled/rejected, immediately re-place or flat the position.
- **Idempotent entry** — intent keyed by `ts`; if the executor crashes and restarts, it checks Redis before acting, never double-enters.
- **Startup reconcile** — on each executor run: query Kite positions/orders, compare to Redis state; if mismatch (e.g. SL filled externally), update state accordingly before applying rules.

---

## 14. OCO implementation (no native bracket orders on Zerodha)

Zerodha discontinued bracket orders (2020). Build manual OCO:

1. Place **entry** (MIS market or limit).
2. On fill confirmed: place **SL-M order** at `sl_premium`, store `sl_order_id` in Redis.
3. Place **target limit order** at `target_premium`, store `target_order_id` in Redis.
4. Each run: check if either order filled → if SL filled, cancel target (and vice versa).
5. Trailing: `kite.modify_order(sl_order_id, trigger_price=new_sl)` — modifying the existing SL-M order, never cancelling and replacing (avoids a window of no protection).

---

## 15. Paper-fill model

- Entry fills at **LTP of first executor run after intent** (not signal candle close).
- Apply **bid-ask spread** = ₹0.75 (NIFTY ATM options typical). Buy at LTP + 0.375; sell at LTP − 0.375.
- Deduct per-trade costs:
  - Brokerage: ₹20 flat (Zerodha intraday)
  - STT: 0.025% of sell-side premium × qty
  - Exchange + SEBI: ~₹0.50 per lot
  - GST: 18% on brokerage + exchange fees
- SL/target paper fills only when LTP crosses the level in that run — no perfect tick fills.
- `OrderGateway.paper` implements same interface as `OrderGateway.live`; switching is a single config flag.

---

## 16. Manager state machine

```
IDLE
  ├─ pending_intent exists + gate passes → ENTERING
  └─ pending_intent exists + gate fails  → IDLE (log, delete intent)

ENTERING
  └─ entry fill confirmed → OPEN_FIXED (record entry_premium, sl, target, ts)

OPEN_FIXED
  └─ progress ≥ 50% T → BREAKEVEN
       ├─ health ≥ 75 → RUNNER
       └─ health < 75 → OPEN_FIXED (SL now at entry, target intact)

OPEN_FIXED / BREAKEVEN (non-runner)
  └─ progress ≥ 90% T → LOCKED (SL at ~70% lock level)

LOCKED
  └─ target hit → EXITING

RUNNER
  └─ give-back / health<50 / vwap×2 / squareoff → EXITING

any OPEN_*
  └─ SL hit / reversal / theta / squareoff / health<50 → EXITING

EXITING
  └─ flat confirmed (no open position in Kite) → COOLDOWN (log, journal)

COOLDOWN (15 min)
  └─ elapsed → IDLE
```

Redis key `executor:position` holds serialised state. Written at every transition. TTL: end of trading day.

---

## 17. Module structure

```
executor/
  run.py                   # GH Actions entrypoint — one complete tick per invocation
  gateway/
    base.py                # OrderGateway ABC (place / modify / cancel / get_positions)
    kite_live.py           # real Kite Connect orders
    paper.py               # simulated fills + cost model (§15)
  manager.py               # state machine driver, milestone ladder, runner, exits
  health.py                # weighted score, VWAP veto, reversal detection (§7)
  trailing.py              # caution swing trail + ratchet invariant (§13)
  sizing.py                # qty, risk cap, lot from Redis instrument cache (§5)
  gates.py                 # entry filters: VIX, cooldown, time, position lock (§4)
  state.py                 # Redis read/write, idempotency, startup reconcile (§13)
  journal.py               # Notion trade log + Discord P&L summary
  config.py                # all frozen constants below
```

Reuse from signal bot (copy, don't import cross-repo):
`kite_client`, `indicators`, `auth` (token refresh from Redis), `calendar_nse`.

---

## 18. Frozen constants (v1)

```python
# Instrument
INSTRUMENT          = "NIFTY"
USE_WEEKLY          = True             # Tuesday expiry
STRIKE_STEP         = 50

# Sizing
CAPITAL_RS          = 1_00_000         # paper
RISK_PCT            = 0.02             # 2% per trade → ₹2,000 max risk

# Signal inheritance
ATM_DELTA           = 0.50
TARGET_RR           = 1.5
MAX_RISK_POINTS     = 25

# Entry gate
VIX_MAX             = 22
COOLDOWN_CANDLES    = 3
INTENT_TTL_MIN      = 6

# Health score
HEALTH_WEIGHTS      = {"C2_vwap": 40, "C4_dmi": 25, "C3_rsi": 20, "C1_mom": 15}
HEALTH_HEALTHY      = 75
HEALTH_CAUTION      = 50
VWAP_LOST_EXIT      = 2               # consecutive 5-min candles

# Milestones
BREAKEVEN_AT        = 0.50            # of T
LOCK_AT             = 0.90
LOCK_FRACTION       = 0.70            # lock ~70% of move
RUNNER_GIVEBACK     = 0.10            # peak × 0.90
RUNNER_GIVEBACK_LATE = 0.05           # peak × 0.95 past original target

# Caution trailing
CAUTION_TRAIL_SWINGS = 3              # 5-min candles
CAUTION_ATR_BUFFER   = 0.10           # × ATR

# Theta
THETA_MINUTES       = 15
THETA_MIN_PROGRESS  = 0.25            # of T

# Paper
PAPER_SPREAD        = 0.75            # ₹ bid-ask on entry/exit

# Timing
EVAL_WINDOW_START   = "09:40"
NO_NEW_ENTRY        = "14:45"
SQUAREOFF_IST       = "15:10"
COOLDOWN_AFTER_EXIT = 15              # minutes before IDLE

# Daily limits (deferred to v2 live)
MAX_TRADES_DAY      = None
DAILY_LOSS_LIMIT    = None

# Mode
PAPER_MODE          = True            # flip to False for live
```

---

## 19. cron-job.org setup

| Job | Schedule | URL | Method |
|-----|----------|-----|--------|
| signal | every 5 min, Mon–Fri, 09:35–14:50 IST | `POST https://api.github.com/repos/7Spidy/{repo}/actions/workflows/signal.yml/dispatches` | POST |
| executor | every 1 min, Mon–Fri, 09:40–15:10 IST | `POST https://api.github.com/repos/7Spidy/{repo}/actions/workflows/executor.yml/dispatches` | POST |

Header for both: `Authorization: Bearer {GITHUB_PAT}`, `Accept: application/vnd.github+json`.
Body: `{"ref": "main"}`.

---

## 20. Build order

1. `OrderGateway` interface + **paper gateway** + cost model (§15).
2. Redis state + idempotency + startup reconcile (§13).
3. `run.py` entrypoint + `executor.yml` workflow (workflow_dispatch, pip cache, secrets).
4. Entry gate + sizing (§4, §5).
5. Manager state machine with frozen rules (§7–§13).
6. Health scorer + indicators reuse.
7. Journal (Notion + Discord).
8. Run paper mode live **≥ 1 week**, validate trailing against real market ticks.
9. Implement `kite_live` gateway; set `PAPER_MODE = False`; 1 lot, real capital small.

---

## 21. Deferred

- **BANKNIFTY** and multi-instrument → v2.
- **Multiple simultaneous positions** → v2.
- **MAX_TRADES_DAY + DAILY_LOSS_LIMIT** → v2 (live phase).
- **Conviction-scaled R:R** — toggle, off by default, v1.1.
- **LLM entry-gate** — slow, pre-entry regime check (5-min cadence, before any position). Not in the live management loop.

---

## ⚠️ Before going live

1. **SEBI retail algo-trading framework (2025)** and Zerodha API terms — confirm what applies to automated order placement at this order rate. Paper mode doesn't touch this; it's not a blocker for building.
2. The startup reconcile (§13) and mandatory-SL invariant exist for the failure modes real capital exposes: partial fills, stuck orders, crashed mid-position. Paper mode can't fully simulate this — allocate a week of paper time specifically to intentional failure testing (kill the run mid-execution, verify state reconciles correctly on next run).
