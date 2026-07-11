# claude_change_spec.md — index-fno-auto-executor (Repo 2)

## Context & Goal
Repo 2 is moving from paper to **live phase**. This change ports the daily-loss
circuit breaker logic from Repo 1 (`src/paper_engine.py`) into Repo 2 exactly,
sets the loss limit to 15% of capital, and externalizes `PAPER_MODE` so it can
be toggled via a GitHub Actions variable without a code change or redeploy of
logic. It also closes a gap discovered during review: Repo 2 currently only
computes P&L for the paper gateway — live-gateway exits never populate
`pos["pnl"]`, so a live circuit breaker would silently never trip. This spec
fixes that too, since the breaker is meaningless without it.

**IMPORTANT — frozen file note:** `executor/config.py` is marked "Frozen
constants — v1. Do not change any value without updating the spec and the
FROZEN comment. Source of truth: fno-auto-executor-spec-v1.md §18." This spec
supersedes that note for the specific values below. If
`fno-auto-executor-spec-v1.md` exists in the repo, update §18 to reflect the
new values as part of this change. If it does not exist locally, note that in
your output and proceed — do not block on it.

## Scope
- `executor/config.py` — capital/loss-limit constants, `PAPER_MODE` sourced
  from env var, remove `USE_WEEKLY`, `STRIKE_STEP`, `MAX_RISK_POINTS`.
- `executor/state.py` — new daily P&L accumulator + entries-blocked flag
  (Redis-backed, mirrors Repo 1's `paper:` key pattern under an `executor:`
  namespace).
- `executor/gates.py` — new gate 0: entries-blocked check, run first, before
  VIX/cooldown/etc.
- `executor/manager.py` — compute P&L for **both** gateways on exit (not just
  `PaperGateway`), then run the same post-exit breaker logic Repo 1 uses.
- `executor/gateway/base.py` — remove unused `field` import.
- `executor/journal.py` — remove `notify_milestone()`.
- `executor/utils/calendar_nse.py` — remove `is_past_time()`.
- `executor/utils/kite_client.py` — remove `NFO`, `NSE` constants,
  `get_quote()`, `get_orders()` methods.
- Do **not** touch `executor/sizing.py`, `executor/trailing.py`,
  `executor/health.py` beyond what's listed above.

## Steps

### 0. Remove confirmed dead code

**`executor/config.py`** — delete `USE_WEEKLY` and `STRIKE_STEP`. Verified:
Repo 2 does not compute its own strike/expiry — `atm_strike` is inherited
verbatim from Repo 1's signal intent (`executor/state.py` reads
`intent["atm_strike"]` directly). Also delete `MAX_RISK_POINTS` — appears only
in its own inline comment, zero code references anywhere.

**`executor/gateway/base.py`** — remove the unused `field` import from
`from dataclasses import dataclass, field` (keep `dataclass`).

**`executor/journal.py`** — delete `notify_milestone()`. Zero call sites.

**`executor/utils/calendar_nse.py`** — delete `is_past_time()`. Zero call
sites. Also remove the now-unused `import time` if nothing else in the file
uses it (check first — don't remove blind).

**`executor/utils/kite_client.py`** — delete `NFO` and `NSE` module constants
(every call site in the repo uses raw `"NFO"`/`"NSE"` string literals
instead), and delete the `get_quote()` and `get_orders()` methods on
`KiteClient` — zero call sites for either.

**Leave as-is (deliberate stub, not dead code):** `MAX_TRADES_DAY = None` in
`config.py` — reserved for a future trade-count cap, same category
`DAILY_LOSS_LIMIT` was in before this change. Do not remove.

### 1. `executor/config.py` — capital, loss limit, paper-mode toggle

Before:
```python
# Sizing
CAPITAL_RS           = 1_00_000      # ₹1,00,000 paper capital
RISK_PCT             = 0.02          # 2% per trade → ₹2,000 max risk
...
# Daily limits — deferred to v2 (live phase)
MAX_TRADES_DAY       = None
DAILY_LOSS_LIMIT     = None

# Mode — flip to False for live
PAPER_MODE           = True
```

After:
```python
import os

# Sizing
CAPITAL_RS           = 1_00_000      # ₹1,00,000 capital (paper or live — see PAPER_MODE)
RISK_PCT             = 0.02          # 2% per trade → ₹2,000 max risk
...
# Daily limits — v2 (live phase), ported from Repo 1 (src/paper_engine.py)
MAX_TRADES_DAY       = None
DAILY_LOSS_PCT       = 0.15
DAILY_LOSS_LIMIT     = -(CAPITAL_RS * DAILY_LOSS_PCT)   # -15% of capital, computed

# Mode — sourced from GitHub Actions repo/environment variable PAPER_MODE
# ("true"/"false", case-insensitive). Defaults to True (safe) if unset.
# NOTE: this repo runs as a persistent process on the target VPS — changing
# the Actions variable requires a process restart to take effect, it is not
# a live mid-session toggle. See open question below.
PAPER_MODE           = os.environ.get("PAPER_MODE", "true").strip().lower() == "true"
```

Leave `RISK_PCT` and everything else in the file untouched — that's per-trade
risk sizing, a separate concept from the daily-loss circuit breaker, out of
scope here.

### 2. `executor/state.py` — daily P&L accumulator + block flag

Add new keys and functions, placed near the existing `_KEY_*` constants and
after `now_utc_iso()`. Mirror Repo 1's Redis pattern (`paper:pnl:{date}`,
`paper:no_more:{date}`) under the `executor:` namespace, with the same TTL
convention (24h, since these keys reset each trading day):

```python
_KEY_DAILY_PNL_PREFIX   = "executor:daily_pnl:"      # + date_str (YYYY-MM-DD)
_KEY_NO_MORE_PREFIX     = "executor:entries_blocked:" # + date_str
_DAILY_KEY_TTL_SECS     = 86400


def _daily_pnl_key(date_str: str) -> str:
    return f"{_KEY_DAILY_PNL_PREFIX}{date_str}"


def _no_more_key(date_str: str) -> str:
    return f"{_KEY_NO_MORE_PREFIX}{date_str}"


def get_daily_pnl(r: redis_lib.Redis, date_str: str) -> float:
    raw = r.get(_daily_pnl_key(date_str))
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def update_daily_pnl(r: redis_lib.Redis, date_str: str, delta: float) -> float:
    """Add delta to today's cumulative P&L and return the new total."""
    new_val = get_daily_pnl(r, date_str) + delta
    r.set(_daily_pnl_key(date_str), str(round(new_val, 2)), ex=_DAILY_KEY_TTL_SECS)
    return new_val


def entries_blocked(r: redis_lib.Redis, date_str: str) -> bool:
    """True if either the daily-loss breaker or the 1Trade flag has tripped today."""
    return r.exists(_no_more_key(date_str)) == 1


def block_entries(r: redis_lib.Redis, date_str: str, reason: str) -> None:
    r.set(_no_more_key(date_str), reason, ex=_DAILY_KEY_TTL_SECS)
    log.info("entries BLOCKED for %s: %s", date_str, reason)


def block_reason(r: redis_lib.Redis, date_str: str) -> Optional[str]:
    raw = r.get(_no_more_key(date_str))
    return raw if raw else None
```

Use whatever redis-bytes-vs-str decoding convention the rest of `state.py`
already uses (check `decode_responses` setting on the client before assuming
`raw` is a `str`) — match existing style, don't introduce a second convention.

### 3. `executor/gates.py` — gate 0: entries blocked

Add as the **first** check in `check_all`, before VIX:

```python
def check_all(
    intent: dict,
    r: redis_lib.Redis,
    kite: KiteClient,
) -> tuple[bool, str]:
    from executor.utils.calendar_nse import now_ist
    from executor.state import entries_blocked, block_reason

    # 0. Daily-loss / 1Trade circuit breaker — checked first, mirrors Repo 1
    date_str = now_ist().strftime("%Y-%m-%d")
    if entries_blocked(r, date_str):
        reason = block_reason(r, date_str) or "entries blocked"
        return False, reason

    # 1. VIX ≤ 22
    ...
```

Keep the rest of `check_all` and all existing `_check_*` helpers unchanged.

### 4. `executor/manager.py` — live P&L + post-exit breaker

**4a. Compute P&L for both gateways**, not just `PaperGateway`, in
`check_exit_complete`:

Before:
```python
    from executor.gateway.paper import PaperGateway
    if isinstance(gateway, PaperGateway) and qty > 0:
        pnl = gateway.compute_pnl(entry, exit_p, qty)
        pos["pnl"] = pnl
        log.info("manager: paper P&L = ₹%.2f", pnl)
```

After:
```python
    if qty > 0:
        # Gross P&L (exit - entry) × qty. For live fills this is an
        # approximation — it does not include actual Zerodha brokerage/STT/
        # exchange charges (those come from the contract note, not
        # available synchronously here). Good enough for a circuit-breaker
        # trigger; do not treat this as the authoritative live P&L figure
        # for accounting/journal purposes.
        pnl = (exit_p - entry) * qty
        pos["pnl"] = round(pnl, 2)
        log.info("manager: P&L = ₹%.2f (paper=%s)", pnl, isinstance(gateway, PaperGateway))
```
(Keep the `from executor.gateway.paper import PaperGateway` import — still
needed for the `isinstance` check in the log line above.)

**4b. Post-exit breaker check** — add immediately after the P&L block, before
the transition to `COOLDOWN`:

```python
    from executor.utils.calendar_nse import now_ist
    date_str = now_ist().strftime("%Y-%m-%d")
    new_pnl = state.update_daily_pnl(r, date_str, pos.get("pnl", 0.0) or 0.0)

    if new_pnl <= config.DAILY_LOSS_LIMIT and not state.entries_blocked(r, date_str):
        state.block_entries(r, date_str, f"daily_loss_breaker: pnl={new_pnl:.2f}")
```

**No 1Trade rule.** Repo 1's 1Trade flag (block further entries once cumulative
P&L turns positive after any exit) is being removed from Repo 1 in this same
change round and must **not** be ported into Repo 2. Only the daily-loss
breaker applies. This is a pre-entry gate only — no effect on already-open
positions — and persists for the rest of the trading day via the 24h Redis
TTL.

## Open question to flag back to the user (do not silently resolve)
`PAPER_MODE` sourced from a GitHub Actions variable means toggling it requires
setting the variable **and** restarting the executor process on the VPS — it
is not a live, mid-session flip. If the intent was to flip mode without any
redeploy/restart at all, a Redis-backed toggle would be needed instead. Flag
this back rather than assuming either way.

## Verification Checklist (blocking — do not push until all pass)
- [ ] `grep -n "DAILY_LOSS_LIMIT\|DAILY_LOSS_PCT\|PAPER_MODE" executor/config.py`
      shows the computed loss limit and env-sourced `PAPER_MODE`.
- [ ] `python -c "from executor import config; assert config.DAILY_LOSS_LIMIT == -15000.0"`
      passes with no `PAPER_MODE` env var set (defaults true).
- [ ] `grep -n "def entries_blocked\|def block_entries\|def update_daily_pnl" executor/state.py`
      shows all three new functions.
- [ ] `grep -in "1trade" executor/` returns zero matches — the 1Trade rule is
      not ported into this repo.
- [ ] `grep -rn "USE_WEEKLY\|STRIKE_STEP\|MAX_RISK_POINTS\|notify_milestone\|is_past_time" executor/`
      returns zero matches.
- [ ] `grep -n "^NFO\|^NSE\|def get_quote\|def get_orders" executor/utils/kite_client.py`
      returns zero matches.
- [ ] `python -m pytest` (or repo's test runner) still passes with zero
      modifications to any test file.
- [ ] `check_all` in `gates.py` returns `(False, reason)` immediately when
      `entries_blocked` is true, without calling VIX/cooldown/etc (verify with
      a unit test or manual trace, not just visual read).
- [ ] `check_exit_complete` in `manager.py` computes `pos["pnl"]` for a live
      (`KiteLiveGateway`) exit, not just paper — confirm by reading the diff,
      no `isinstance(gateway, PaperGateway)` gate remains around the P&L math.
- [ ] Existing paper-mode test suite (if any) still passes — do not modify
      test files to make them pass; only production source files listed above.
- [ ] `RISK_PCT`, `sizing.py`, `trailing.py`, `health.py` are untouched
      (`git diff --stat` should show only `config.py`, `state.py`, `gates.py`,
      `manager.py`).

**Do not commit or push — wait for manual approval.**
