# Change Spec: Sync Repo 2 (index-fno-auto-executor) Sizing/Capital Logic to Repo 1 (index-fno-signal-bot)

## Context & Goal

Repo 2's `compute_qty()` currently uses a **risk-based** sizing model (caps risk at `RISK_PCT` of `CAPITAL_RS`, scales `max_lots` up when SL distance is tight). Repo 1's `paper_engine.py` uses a **fixed-lot, capital-availability** model (always 1 lot per instrument, gated on whether remaining daily capital covers the entry cost).

Prior investigation confirmed:
- SL derivation (`prev_candle_low`/`prev_candle_high` via `executor_bridge.py` → shared Redis intent) is **already identical and correctly shared** between repos. No change needed here.
- Target computation (`target_pts`, variable per instrument) is **already shared** via the same intent. No change needed here.
- `TARGET_RR` and `ATM_DELTA` are already read from the same config values (1.5 / 0.50) in both repos.
- The **only** intended divergence (per explicit confirmation) is the capital figure used in the availability check:
  - **Repo 1**: always `DAILY_CAPITAL = ₹1,00,000` (fixed).
  - **Repo 2**: if `PAPER_MODE=true` → same fixed `₹1,00,000`; if `PAPER_MODE=false` (live) → real-time available capital from Kite Connect `margins()`.

Goal: make Repo 2's lot sizing and capital-gate logic byte-for-byte equivalent to Repo 1's `paper_engine.py`, except for that one capital-source divergence, and make lot size auto-sync from Repo 1's per-instrument lot table (so bumping lot count in Repo 1 requires no Repo 2 code change).

## Scope

### Files to modify
- `executor/sizing.py` — remove `compute_qty()`'s risk-based `max_lots` scaling; replace with fixed-lot lookup + capital-availability check. Add `get_daily_loss_limit()`.
- `executor/config.py` — remove `RISK_PCT` (no longer used) and the static `DAILY_LOSS_LIMIT` computation (becomes a function call instead).
- `executor/manager.py` — `try_enter()` and `check_exit_complete()` both gain a `kite` parameter; update their two call sites each in `run.py`.
- `executor/run.py` — thread `kite` through to `try_enter()` (1 call site) and `check_exit_complete()` (2 call sites).
- `executor/utils/kite_client.py` — add `get_margins()` wrapper method calling Kite Connect's `margins()` endpoint.
- `executor/state.py` — add `committed_premium()` helper, mirroring Repo 1's `_committed_premium()`.
- `fno-auto-executor-spec-v1.md` — update §18-area documentation to reflect fixed-lot sizing and the new `get_daily_loss_limit()` function, per the frozen-file convention the prior spec established.
- `tests/test_sizing.py`, `tests/test_state.py` (new files — no test suite currently exists in this repo).

### Files NOT to touch (frozen)
- `executor_bridge.py` (Repo 1) — SL/target/intent write logic already correct, do not modify.
- `tools/pnl_replay.py`
- Any signal generation core (`signals.py`, `stock_main.py`, `main.py`)
- `executor/manager.py` exit/trailing/health logic (lines beyond the entry flow) — out of scope.

## Conflicts, Gaps, and Corrections Found on Re-Review

Before finalizing, I pulled live code again and found several things the original spec draft got wrong or missed. These are corrected below.

### 1. Direct conflict with a prior, already-applied spec
`claude_change_spec_repo2.md` already exists in the repo root and was already executed — it's what produced the current `config.py`, `state.py`, `gates.py`, `manager.py` state we've been reading. That prior spec's verification checklist explicitly asserts:
```
- [ ] RISK_PCT, sizing.py, trailing.py, health.py are untouched
```
This spec **intentionally reverses that constraint** — we are now removing `RISK_PCT` and modifying `sizing.py`. That's expected given your explicit instructions, but Claude Code should not be surprised by a diff that contradicts the older spec file's checklist; both files will coexist in the repo, and the older one is now partially superseded. No action needed beyond awareness — not fixing retroactively, just flagging so Claude Code doesn't treat the old checklist as still binding.

### 2. `fno-auto-executor-spec-v1.md` also needs updating
This is the living source-of-truth doc (§18 area, lines ~200-385) and it still documents the old `RISK_PCT`/`max_lots` formula and `DAILY_LOSS_LIMIT` as a static constant. Per the frozen-file convention the prior spec established ("update §18 to reflect new values as part of this change"), this doc must be updated to reflect: fixed-lot sizing (no more `max_lots` formula), and `DAILY_LOSS_LIMIT` becoming a function of `paper_mode`. Added to scope below.

### 3. No test suite exists in Repo 2
There is no `tests/` directory in `index-fno-auto-executor` at all (confirmed via repo listing). The "Testing Requirements" section below assumes tests will be net-new, not modifications to existing tests — Claude Code should create `tests/test_sizing.py` and `tests/test_state.py` from scratch, matching whatever test framework Repo 1 uses for consistency (pytest, per Repo 1's `tests/` directory).

### 4. `kite` client is already threaded through `run.py` — no new gateway property needed
Earlier draft assumed a `gateway.kite_client` accessor would need adding to `KiteLiveGateway`. Not necessary: `run.py` already builds `kite: KiteClient` once at the top of the tick (`_build_kite(r)`) and passes it explicitly into `_run_idle(r, kite, gateway, ...)` where `try_enter` is called, and `kite` is in scope in the outer function where `check_exit_complete` is called too. Simpler fix: add `kite` as an explicit parameter to `manager.try_enter()` and `manager.check_exit_complete()` (and thread it through the two call sites in `run.py`), rather than reaching into the gateway object. This avoids adding surface area to `OrderGateway`/`KiteLiveGateway` for something `run.py` already has on hand.

### 5. `check_exit_complete` signature change ripples to both call sites in `run.py`
`check_exit_complete(gateway, pos, r)` is called twice in `run.py` (lines ~181 and ~185, both inside the main tick function where `kite` is already in scope from `_build_kite`). Both call sites need the new `kite` and `paper_mode` args threaded through when this function starts calling `get_daily_loss_limit()`.

### 6. `PAPER_MODE` restart caveat still applies here too
The prior spec flagged that `PAPER_MODE` is sourced from a GitHub Actions variable and requires a VPS process restart to take effect — not a live mid-session toggle. This still applies to the new capital-source logic in this spec: `paper_mode` will be read once at process start (via `config.PAPER_MODE`) and won't change mid-session even if the GitHub Actions variable is flipped. No new issue here, just confirming this spec's `paper_mode` param inherits the same restart-required behavior — not attempting to make it dynamic.



### 1. `executor/sizing.py`
Replace `compute_qty()` entirely:

```python
def compute_qty(
    r: redis_lib.Redis,
    tradingsymbol: str,
    entry_ltp: float,
    paper_mode: bool,
    kite: "KiteClient | None" = None,
) -> int:
    """
    Fixed-lot sizing, mirroring Repo 1's paper_engine.py exactly.
    Always 1 lot (lot_size from shared instrument cache — auto-syncs with
    Repo 1's INDEX_LOT_SIZES / stock_config lot tables since both write to
    the same Redis instrument cache).

    Capital-availability gate:
      - paper_mode=True  -> use fixed CAPITAL_RS (== Repo 1's DAILY_CAPITAL)
      - paper_mode=False -> use live available capital from kite.get_margins()

    Returns 0 (skip entry) if entry_cost > remaining capital.
    """
    lot_size = get_lot_size(r, tradingsymbol)

    if paper_mode:
        capital = config.CAPITAL_RS
    else:
        if kite is None:
            log.error("sizing: live mode requires a KiteClient for margins() — skip")
            return 0
        capital = kite.get_margins()

    committed = state.committed_premium(r)          # mirrors Repo 1 _committed_premium()
    remaining = capital - committed
    entry_cost = entry_ltp * lot_size

    if entry_cost > remaining:
        log.warning(
            "sizing: capital exhausted (need ₹%.0f, have ₹%.0f) — skip",
            entry_cost, remaining,
        )
        return 0

    log.info("sizing: lot_size=%d qty=%d entry_cost=₹%.0f remaining=₹%.0f",
              lot_size, lot_size, entry_cost, remaining)
    return lot_size
```

Remove `compute_levels()`'s dependency on nothing else — it stays unchanged (SL/target derivation from `premium_risk` is already correct and untouched).

### 2. `executor/config.py` and `executor/sizing.py` — loss limit becomes a function
- Delete `RISK_PCT = 0.02` line and its comment (in `config.py`).
- Keep `CAPITAL_RS = 1_00_000` in `config.py` — repurpose comment to: `# Paper-mode fixed capital, mirrors Repo 1 DAILY_CAPITAL. Live mode uses kite.get_margins() instead — see sizing.py.`
- `DAILY_LOSS_LIMIT` can no longer be a static module-level constant, since in live mode it must be computed against real-time margins. Remove the static computation from `config.py`:
  ```python
  # DELETE this line:
  # DAILY_LOSS_LIMIT = -(CAPITAL_RS * DAILY_LOSS_PCT)
  ```
  Add the replacement function to `executor/sizing.py` (alongside `compute_qty`, since both now share the same paper/live capital-source branching logic):
  ```python
  def get_daily_loss_limit(paper_mode: bool, kite: "KiteClient | None" = None) -> float:
      """
      -15% of capital. Paper mode: fixed CAPITAL_RS (mirrors Repo 1).
      Live mode: -15% of live available margins (kite.get_margins()).
      """
      if paper_mode:
          capital = config.CAPITAL_RS
      else:
          if kite is None:
              log.error("get_daily_loss_limit: live mode requires a KiteClient — falling back to CAPITAL_RS")
              capital = config.CAPITAL_RS
          else:
              capital = kite.get_margins()
      return -(capital * config.DAILY_LOSS_PCT)
  ```
- Every call site currently referencing `config.DAILY_LOSS_LIMIT` as a static value must be updated to call `get_daily_loss_limit(paper_mode, kite)` instead. **Claude Code: grep for `DAILY_LOSS_LIMIT` usages across `executor/*.py` and update each call site** — likely candidates are the circuit-breaker gate check (wherever daily P&L is compared against this limit) and any Discord/notifier message that reports the limit.
- Since margins are fetched live, this means `get_daily_loss_limit()` will make a Kite API call each time it's invoked in live mode — confirm with rate-limit-aware caching if this is checked on every tick (likely, since it's a pre-entry gate). Suggest caching the live margins value per-tick in Redis (short TTL, e.g. 60s) rather than calling `margins()` on every single gate evaluation, to avoid hammering the Kite API. **Flagging this caching approach for Claude Code to implement sensibly — not fully speccing exact TTL/key here, use judgment consistent with existing Redis caching patterns in the codebase (e.g. instrument cache pattern in `utils/auth.py`).**

### 3. `executor/manager.py` — `try_enter()` and `check_exit_complete()`

**`try_enter()`** — add `kite` as an explicit parameter (available at its `run.py` call site inside `_run_idle`, which already receives `kite`):

```python
def try_enter(
    intent: dict,
    gateway: OrderGateway,
    r: redis_lib.Redis,
    kite: "KiteClient",
    entry_ltp: float,
) -> None:
    ...
    qty = sizing.compute_qty(
        r, ts,
        entry_ltp=entry_ltp,
        paper_mode=config.PAPER_MODE,
        kite=kite if not config.PAPER_MODE else None,
    )
```
Update the call site in `run.py` (`_run_idle`, ~line 239) to pass `kite` and the already-available `entry_ltp` through.

**`check_exit_complete()`** — same pattern, add `kite` param, used for the new `get_daily_loss_limit()` call:
```python
def check_exit_complete(
    gateway: OrderGateway,
    pos: dict,
    r: redis_lib.Redis,
    kite: "KiteClient",
) -> None:
    ...
    loss_limit = sizing.get_daily_loss_limit(config.PAPER_MODE, kite if not config.PAPER_MODE else None)
    if new_pnl <= loss_limit and not state.entries_blocked(r, date_str):
        state.block_entries(r, date_str, f"daily_loss_breaker: pnl={new_pnl:.2f}")
```
Update **both** call sites in `run.py` (~lines 181 and 185, both within the scope where `kite` is already built via `_build_kite`) to pass `kite` through.

### 4. `executor/utils/kite_client.py`
Add:
```python
def get_margins(self) -> float:
    """
    Return available equity cash for live capital-availability gate.
    Mirrors the capital figure semantics of Repo 1's DAILY_CAPITAL constant.
    """
    margins = self.kite.margins()
    return float(margins["equity"]["available"]["live_balance"])
```
Verify the exact JSON path against live Kite Connect API docs/response shape before finalizing — field names above are from Kite Connect's documented schema but must be confirmed against a live `margins()` call.

### 5. `executor/state.py`
Add:
```python
def committed_premium(r: redis_lib.Redis) -> float:
    """
    Sum of entry_price * qty for all currently-open executor positions.
    Mirrors Repo 1's paper_engine._committed_premium(). Given Repo 2's
    single-instrument, single-position-at-a-time gate (_check_no_open_position),
    this will be 0 whenever try_enter() is reachable — implemented for parity
    and to avoid silent divergence if multi-instrument support is added later.
    """
    pos = load_position(r)
    if not pos:
        return 0.0
    return pos.get("entry_price", 0.0) * pos.get("qty", 0)
```

## Testing Requirements
No test suite currently exists in this repo — create new files, don't assume any exist to modify.
- New `tests/test_sizing.py`: `compute_qty()` paper mode returns `lot_size` when `entry_cost <= CAPITAL_RS`, returns 0 when it exceeds; live mode calls `kite.get_margins()` and uses that value instead of `CAPITAL_RS`. `get_daily_loss_limit()` returns `-(live_margins * 0.15)` in live mode and `-(CAPITAL_RS * 0.15)` in paper mode.
- New `tests/test_state.py`: `committed_premium()` returns 0.0 when no position open, and `entry_price * qty` when one is.
- Mock `kite.get_margins()` against a plausible Kite Connect response shape — verify the exact JSON path (`margins()["equity"]["available"]["live_balance"]`) against live Kite Connect API docs before finalizing, since this is unverified against a real account response.
- Regression: confirm `compute_levels()` (SL/target) is untouched and still behaves as before.
- Run local backtester (`local_backtest/`) after changes and report P&L before pushing — per standing instruction, since this changes sizing math even though SL/target themselves are unchanged.

## Verification Checklist
- [ ] `grep -n "RISK_PCT" executor/*.py` returns no matches.
- [ ] `grep -n "max_lots" executor/sizing.py` returns no matches (removed).
- [ ] `compute_qty()` signature includes `paper_mode` and `entry_ltp` params.
- [ ] `get_margins()` added to `kite_client.py`, wired to both `compute_qty()` and `get_daily_loss_limit()`.
- [ ] `config.DAILY_LOSS_LIMIT` static constant removed; replaced by `get_daily_loss_limit(paper_mode, kite)` function in `sizing.py`.
- [ ] `try_enter()` and `check_exit_complete()` both take a `kite` param; all 3 call sites in `run.py` (1 for `try_enter`, 2 for `check_exit_complete`) updated accordingly.
- [ ] `fno-auto-executor-spec-v1.md` §18-area updated to match new sizing/loss-limit logic.
- [ ] Live-mode margins fetch for the loss limit is cached per-tick (not called on every gate evaluation) to avoid excessive Kite API calls.
- [ ] New `tests/test_sizing.py` and `tests/test_state.py` created and passing (no prior tests existed to modify).
- [ ] Local backtester run and P&L reported before any commit/push.
- [ ] Do NOT commit or push without manual confirmation.
