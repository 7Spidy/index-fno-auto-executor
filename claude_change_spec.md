# Claude Change Spec — Repo 2 (index-fno-auto-executor) Live-Trading Sync

## Context & Goal

Repo 1 (`index-fno-signal-bot`) has diverged from Repo 2 (`index-fno-auto-executor`)
through several recent changes. Repo 2 is about to be switched from Paper Mode to
real-money live trading on the Vultr Mumbai VPS against a Zerodha account. This
spec brings Repo 2 to full logic parity with Repo 1 (condor engine excluded — it
is Repo 1-only, advisory/backtest scope, not part of the executor signal path),
and adds three new Repo 2-specific capabilities required for safe live-money
operation: dynamic capital-based lot sizing, a manual kill switch, and an
intra-minute trailing-SL sub-loop mirroring Repo 1's heartbeat cadence.

This is a real-money change. Every item below must be verifiably correct before
`PAPER_MODE` is flipped to `false` on the VPS.

## Scope

**Files to modify:**
- `executor/config.py` — add lot-multiplier threshold constants
- `executor/sizing.py` — dynamic lot-count decision + `compute_qty()` signature change
- `executor/state.py` — new Redis helpers: `get_lot_multiplier`/`set_lot_multiplier`,
  `get_kill_switch`/`set_kill_switch`
- `executor/gates.py` — kill-switch check added to `check_all()`
- `executor/run.py` — restructure `main()` to (a) run the once-per-day lot decision
  before the instrument loop, (b) wrap OPEN-position management in a 4×/15s
  budget-anchored sub-loop mirroring `position_tracker._loop_config()`
- `executor/manager.py` — no signature change expected, but must be re-verified
  against Repo 1's latest `position_tracker.py` for any drift beyond the ladder
  SL math already ported in `trailing.py`
- `.github/workflows/executor.yml` — add `concurrency` group
- `tests/test_sizing.py` — new tests for lot-multiplier logic
- `tests/test_state.py` — new tests for kill-switch + lot-multiplier Redis helpers
- New test file: `tests/test_run_subloop.py` (or extend an existing test file if
  more idiomatic) — sub-loop scheduling/budget logic

**Files to verify (parity check only, condor excluded):**
- `src/signals.py` vs any Repo 2 signal-consuming logic (Repo 2 does not
  re-evaluate signals — confirm it only consumes Redis intent, never recomputes;
  flag if this invariant has drifted)
- `src/indicators.py` vs `executor/utils/indicators.py`
- `src/position_tracker.py` (SL/target chain: `compute_ladder_sl` →
  `compute_ai_adjusted_sl` → `compute_final_sl`, and the new sub-loop pattern)
  vs `executor/trailing.py` + `executor/manager.py`
- `src/charges.py` vs `executor/charges.py`
- `src/trade_notifier.py` / `src/journal.py` (consolidated Discord embed,
  edit-in-place pattern) vs `executor/journal.py`
- `src/paper_engine.py` (capital/committed-premium logic) vs `executor/sizing.py`
  + `executor/state.py` (`committed_premium`)

**Explicitly out of scope:** `src/condor_engine.py`, `src/condor_config.py`,
`src/condor_notifier.py`, and anything under `src/dynamic_stock_universe.py` /
3PM momentum scanner (`src/momentum_scan/`) — these are Repo 1-only, no
Repo 2 execution counterpart exists or is wanted.

## Steps

### 1. Parity verification pass (do this first)

For each file pair listed under "Files to verify": diff the actual logic
(not just function names) between Repo 1's source and Repo 2's mirrored
module. Produce a short parity report as you go (which functions match,
which don't, and why) — do not silently assume parity. Any discrepancy found
that isn't one of the two sanctioned differences below must be flagged and
fixed to match Repo 1 exactly:

- **Sanctioned difference #1:** Live-mode capital source. Repo 1 always uses
  the fixed `DAILY_CAPITAL` constant (₹50,000). Repo 2 in paper mode uses the
  same fixed `CAPITAL_RS` (₹50,000); in live mode it uses
  `kite.get_margins()` real-time capital instead.
- **Sanctioned difference #2 (new, this spec):** Lot sizing. Repo 1 always
  trades 1 lot. Repo 2 in paper mode always trades 1 lot (unchanged). Repo 2
  in live mode uses **dynamic lot count** per the rule in Step 2 below.

Everything else — signal-consumption boundaries, SL/target math, charges
math, trailing ladder math, Discord/Notion journaling patterns, gate logic
(cooldown, no-open-position, time window, option-tradability) — must be
logically identical. Grep guard before finishing this step:

```
grep -rn "max_risk_pts\|MAX_RISK_POINTS\|_risk_ok\|VWAP_PROXIMITY_PTS" repo2/executor/*.py
```
This must return nothing (both are confirmed dead/removed in Repo 1 and must
never be reintroduced in Repo 2).

### 2. Dynamic lot sizing (live mode only)

**Rule:** Once per trading day, the first time the live-mode capital check
succeeds, decide a lot-multiplier for the entire day:
- Capital `< ₹50,000` → multiplier = 1
- Capital `>= ₹50,000` → multiplier = 2

This decision is made **exactly once per day**, cached in Redis, and reused
for every instrument and every remaining tick that day. It must NOT be
re-evaluated on every capital check or before every trade.

**Where the decision happens:** In `executor/run.py`'s `main()`, not inside
`_run_one_instrument()` or `sizing.compute_qty()`. Add a step immediately
after the paper/live mode is resolved and before the instrument loop:

```python
if not _PAPER_MODE:
    date_str = now_ist().date().isoformat()
    multiplier = state_module.get_lot_multiplier(r, date_str)
    if multiplier is None:
        try:
            capital = kite.get_margins(r)
        except Exception as exc:
            log.error("morning capital check failed: %s — no trading until resolved", exc)
            return  # do not proceed with the tick; retry next minute
        multiplier = 1 if capital < 50_000 else 2
        # atomic check-and-set so a slow/retried tick can't clobber an
        # already-decided value
        set_ok = state_module.set_lot_multiplier_if_absent(r, date_str, multiplier)
        if not set_ok:
            multiplier = state_module.get_lot_multiplier(r, date_str)
        log.info("lot multiplier for %s decided: capital=₹%.0f -> x%d", date_str, capital, multiplier)
else:
    multiplier = 1
```

- `state.get_lot_multiplier(r, date_str)` → reads `executor:lot_multiplier:<date_str>`,
  returns `int` or `None` if unset.
- `state.set_lot_multiplier_if_absent(r, date_str, value)` → Redis `SETNX`
  (or equivalent atomic check-and-set) on `executor:lot_multiplier:<date_str>`
  with a TTL through end-of-day (e.g. `ex=86400`). Returns `True` if this call
  set it, `False` if another process already had.
- If capital fetch fails: **no entries proceed this tick** (return early from
  `main()` before the instrument loop, or explicitly no-op every instrument's
  entry gate). Do not fall back to 1 lot. Retry naturally happens next minute's
  tick.
- **`compute_qty()` signature change:** add a `lot_multiplier: int = 1` parameter.
  Final `qty = lot_size * lot_multiplier`. In paper mode, callers must always
  pass `lot_multiplier=1` explicitly (do not rely on the default silently —
  make the call site explicit for auditability).
- **Downstream breaking-change check (mandatory, do not skip):** grep every
  caller of `compute_qty()` and confirm none of them assume qty == lot_size
  (i.e., == 1 lot) elsewhere — check `charges.py` net P&L calc, `journal.py`
  trade logging/Notion payload, Discord embed quantity display in
  `journal.update_consolidated_tracker`. Fix any hardcoded assumption found.

### 3. Kill switch

Add a Redis-backed manual kill switch, Repo 2-only (Repo 1 paper trading is
unaffected):

- Redis key: `executor:kill_switch` (string `"true"`/`"false"`, no TTL —
  persists until manually cleared).
- `state.get_kill_switch(r) -> bool` (default `False`/off if key absent).
- `state.set_kill_switch(r, value: bool) -> None` — for manual operator use
  (e.g., via `redis-cli` or a small ad-hoc script; no new Discord/workflow
  control surface in this spec).
- In `executor/gates.py`'s `check_all()`, add as the **first** check: if
  kill switch is on, fail the gate with reason `"kill_switch_active"` before
  any other check runs (cheap short-circuit, avoids unnecessary Kite API
  calls for cooldown/time/tradability checks).
- Kill switch blocks **new entries only**. Positions already OPEN continue
  through their normal SL/target/trailing/exit lifecycle untouched — do not
  force-squareoff on kill-switch activation.
- Document this key and its manual-set procedure at the top of `gates.py`
  and in this spec's own comments — do not bury it silently.

### 4. Intra-minute trailing-SL sub-loop

Mirror Repo 1's `position_tracker._loop_config()` pattern (4 sub-loops,
15-second spacing, job-start-anchored budget deadline) — but scoped to
**OPEN-position management only**, not the full per-tick instrument loop
(no re-running of entry gates or `try_enter` inside the sub-loop).

**Constants** (add to `executor/config.py`, mirroring Repo 1 naming):
```python
TRACKER_SUBLOOPS      = 4      # env override: EXEC_SUBLOOPS
TRACKER_SUBLOOP_SECS  = 15.0   # env override: EXEC_SUBLOOP_SECS
```
Keep the env-override pattern identical to Repo 1 (`_num()` helper reading
`os.environ`, falling back to these defaults) so `EXEC_SUBLOOPS=1` fully
reverts to today's single-pass behavior with no code change — same safety
property Repo 1 relies on.

**Restructure `run.py`'s `main()`:**
1. Connect, resolve paper/live mode, resolve lot multiplier (Step 2) — single pass, as today.
2. Run gate/entry/exit logic for all 17 instruments — single pass per tick, as today
   (do NOT put this inside the sub-loop).
3. For the sub-loop: collect the set of instruments currently in `OPEN` phase
   after step 2. Then, budget-anchored exactly like Repo 1
   (`slot_base = time.monotonic()` at completion of the first pass; deadline
   = `TRACKER_JOB_START_EPOCH`-anchored budget minus elapsed), run up to
   `TRACKER_SUBLOOPS - 1` additional passes of **only**
   `manager.manage_position(...)` (trailing SL / target / exit-trigger check)
   for each OPEN instrument, spaced `TRACKER_SUBLOOP_SECS` apart.
4. Stop the sub-loop early if: no instruments are OPEN anymore (flat), or
   past squareoff time, or the remaining time budget is exhausted (log and
   skip remaining passes rather than overrunning the GitHub Actions timeout).
5. Run the consolidated Discord tracker update **once**, after the sub-loop
   completes (not once per sub-pass) — same as today's single call at the
   end of `main()`.
6. Set `TRACKER_JOB_START_EPOCH`-equivalent for Repo 2 — add an env var
   (e.g. `EXEC_JOB_START_EPOCH`) exported as the first step of the GitHub
   Actions job (`echo "EXEC_JOB_START_EPOCH=$(date +%s)" >> $GITHUB_ENV`) so
   the budget deadline correctly accounts for checkout + pip install cold-start
   time, exactly mirroring Repo 1's approach.
7. Each sub-pass must have the same per-instrument exception isolation as the
   main loop (`try/except` per instrument — one instrument's failure must
   never block the others' trailing SL updates).

**Do not** re-run `gates.check_all()`, `manager.try_enter()`, or the kill-switch
check inside the sub-loop — those remain once-per-tick. Only the OPEN-phase
management/trailing/exit-check path repeats.

### 5. Workflow changes

`.github/workflows/executor.yml`:
- Add a `concurrency` group so overlapping cron-job.org triggers are
  cancelled/queued rather than racing:
  ```yaml
  concurrency:
    group: executor-tick
    cancel-in-progress: true
  ```
- Add the job-start epoch export step (see Step 4.6 above) as the first step
  in the `tick` job, before `actions/checkout`.
- Re-check `timeout-minutes: 2` is still sufficient headroom: worst case is
  ~1 (first pass) + 3×15s (sub-loop sleeps) + processing time per pass ≈
  55-65s + cold start (~15-20s with pip cache) ≈ comfortably under 2 minutes,
  but confirm this empirically in paper mode before going live and flag if
  it's tight.

### 6. Testing (hard gate — do not proceed to "ready to commit" without these passing)

- `tests/test_sizing.py`: new tests for `compute_qty()` with `lot_multiplier`
  parameter (1x and 2x cases), and for the lot-multiplier decision function
  (capital < 50k → 1, capital >= 50k → 2, boundary at exactly 50k → 2).
- `tests/test_state.py`: new tests for `get_kill_switch`/`set_kill_switch`
  (default False, set True, set False again) and
  `get_lot_multiplier`/`set_lot_multiplier_if_absent` (unset → None, first
  set succeeds, second concurrent set is a no-op and returns existing value).
- New or extended test file for the sub-loop scheduling logic: verify it
  runs at most `TRACKER_SUBLOOPS` passes, stops early when no OPEN positions
  remain, stops early when budget is exhausted, and that
  `EXEC_SUBLOOPS=1` reverts to single-pass behavior.
- Run the full existing test suite (`test_charges.py`, `test_trailing.py`,
  `test_instruments.py`) to confirm no regression from the `compute_qty()`
  signature change or the parity-fix pass in Step 1.
- Run project linters if configured.
- All of the above must pass before moving to the manual commit/push gate.

## Verification checklist (grep guards — run all before declaring done)

```bash
# Dead code must never reappear:
grep -rn "max_risk_pts\|MAX_RISK_POINTS\|_risk_ok\|VWAP_PROXIMITY_PTS" executor/*.py
# should return nothing

# Confirm compute_qty() callers all pass lot_multiplier explicitly:
grep -rn "compute_qty(" executor/*.py tests/*.py

# Confirm condor logic was NOT ported (out of scope):
grep -rln "condor" executor/*.py
# should return nothing

# Confirm kill switch is checked first in gates.check_all():
grep -n "kill_switch" executor/gates.py

# Confirm sub-loop constants exist and are env-overridable:
grep -n "TRACKER_SUBLOOPS\|TRACKER_SUBLOOP_SECS\|EXEC_SUBLOOPS\|EXEC_SUBLOOP_SECS" executor/config.py
```

## Do not commit or push without manual confirmation

`git diff --name-only` must be reviewed and explicitly approved before
`git add` / `git commit` / `git push`. This is a real-money-trading system —
no automatic commit under any circumstance.
