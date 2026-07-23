# Claude Change Spec — Repo 2 (index-fno-auto-executor): Trade Dynamic Stocks

## Context & Goal

Repo 1 now writes live executor intent for the daily dynamic top-gainer
(CE-only) and top-loser (PE-only) stock picks, in addition to the static 17
instruments, with all option-metadata needed for execution attached inline
to the intent payload (`is_dynamic`, `lot_size`, `equity_token`,
`fno_exchange`, `strike_step`, `direction_restriction`) — see
`claude_change_spec_repo1_dynamic.md`. Repo 2 currently has two hard
dependencies that would silently prevent these from ever trading even if
intent is written:

1. `run.py`'s `main()` only loops `config.INDICES + config.STOCKS` — a fixed
   static list. Dynamic picks are never checked at all.
2. `gates._check_option_tradable` and `sizing.get_lot_size` (via
   `auth.get_lot_size`) both require the tradingsymbol to already exist in
   the shared static Redis cache (`kite:stock_option_tokens`). Confirmed:
   this cache is only ever populated for the static 14 stocks — a dynamic
   stock's tradingsymbol will never be found there.

This spec adds a self-contained path: Repo 2 reads the dynamic pick(s)
directly from Redis each tick, merges them into that tick's instrument loop,
and — for dynamic instruments only — uses the metadata already attached to
the intent/position instead of the shared static cache. This is a real-money
change to what Repo 2 will place live orders on. Treat every item below as
production-critical.

## Scope

**Files to modify:**
- `executor/instruments.py` — no static-list change, but add a helper to
  read/parse the dynamic universe payload
- `executor/state.py` — new helper: `get_dynamic_instruments(r, today_date_str) -> list[dict]`
- `executor/run.py` — merge dynamic instruments into the per-tick loop
- `executor/gates.py` — skip static-cache tradability lookup for dynamic
  intents (validate via live LTP only, already fetched); add direction-
  restriction safety check
- `executor/sizing.py` — `compute_qty()` accepts an optional
  `lot_size_override: int | None` param, bypassing `auth.get_lot_size()`
  when provided
- `executor/manager.py` — `try_enter()` passes the override through;
  `fresh_position_from_intent`-derived position snapshots dynamic metadata
- `executor/state.py` — `fresh_position_from_intent()` snapshots
  `is_dynamic`, `lot_size`, `equity_token`, `fno_exchange`,
  `direction_restriction` from intent into the position record
- `executor/run.py` — `_get_rsi_snapshot()` uses the position's snapshotted
  `equity_token` when available (dynamic), falling back to
  `auth.get_underlying_token()` for static instruments
- `tests/test_state.py`, `tests/test_sizing.py` — new tests

**Files NOT to modify:** anything condor-related (none exists in Repo 2 —
confirm it stays that way).

## Steps

### 1. Read the dynamic universe payload each tick

Add to `executor/state.py`:
```python
_KEY_DYNAMIC_UNIVERSE = "stock:dynamic_universe"

def get_dynamic_instruments(r: redis_lib.Redis, today_date_str: str) -> list[dict]:
    """
    Reads Repo 1's stock:dynamic_universe payload. Returns a list of 0-2
    instrument dicts (dynamic gainer/loser), each normalized to look like a
    static instrument-cfg dict for run.py's loop, or [] if:
      - the key is absent,
      - the payload fails to parse,
      - the payload's "date" field does not match today_date_str (stale —
        Repo 1 writes this once at EOD for the *next* trading day; if it's
        not today's date something is wrong upstream and dynamic picks
        should be skipped, not guessed at),
      - a pick is missing required fields (name, lot_size, equity_token,
        fno_exchange, direction_restriction).
    Never raises — any failure here must degrade to "no dynamic picks today",
    never block the static 17.
    """
```
Implementation must be fail-open exactly like Repo 1's own dynamic universe
computation is fail-open (per its own docstring) — a bad/missing payload
means zero dynamic picks that tick, never a blocked static-instrument run.
Log a warning on any skip reason, but do not raise.

### 2. Merge into the per-tick instrument loop

In `run.py`'s `main()`, after resolving paper/live mode and the lot
multiplier, add:
```python
today_str = now_ist().date().isoformat()
dynamic_instruments = state_module.get_dynamic_instruments(r, today_str)
if dynamic_instruments:
    log.info("dynamic instruments today: %s", [d["name"] for d in dynamic_instruments])

for inst_cfg in config.INDICES + config.STOCKS + dynamic_instruments:
    ...
```
Each dynamic instrument dict must carry at least: `name`, `fno_exchange`
(from the pick, default `"NFO"`), `lot_size`, `equity_token`,
`direction_restriction`, `is_dynamic: True`. This is enough for
`_run_one_instrument`'s existing flow — it only ever reads `inst_cfg["name"]`
and `inst_cfg.get("fno_exchange", "NFO")` today; verify this remains true
after your parity check and doesn't reference anything else from
`config.STOCKS` entries (e.g. `sector`) that a dynamic dict lacks.

**Important:** do not cache this list once-per-day the way the lot
multiplier is cached — read it fresh every tick (cheap single Redis GET).
Unlike the lot decision, there's no "decide once, reuse all day" semantic
here; it's simply reading Repo 1's daily-stable value, and reading it fresh
every tick correctly picks up the case where Repo 1's EOD job for *today*
hadn't run yet at process start vs. later (defensive, low-cost either way).

### 3. Skip the static-cache tradability check for dynamic intents

In `gates.py`, `_check_option_tradable` currently requires
`ts in token_cache` (the shared static cache). Change so that when
`intent.get("is_dynamic")` is true, this specific cache-membership check is
skipped (the tradingsymbol legitimately won't be there), but the **live LTP
check remains mandatory and unchanged** — a dynamic-stock option must still
prove it has a non-zero live LTP before being considered tradable:

```python
def _check_option_tradable(intent, r, kite, exchange):
    ts = intent.get("tradingsymbol", "")
    if not ts:
        return False, "tradingsymbol missing from intent"

    if not intent.get("is_dynamic"):
        try:
            token_cache = auth.get_option_cache(r)
        except RuntimeError as exc:
            return False, str(exc)
        if ts not in token_cache:
            return False, f"{ts} not found in option token cache"

    # Live LTP check unchanged — mandatory for both static and dynamic
    ...
```

### 4. Direction-restriction safety check (defense in depth)

In `gates.check_all()`, add a check specific to dynamic intents: if
`intent.get("is_dynamic")` and `intent.get("direction_restriction")` is
`"CE_ONLY"` but `intent.get("direction") == "PE"` (or vice versa for
`"PE_ONLY"`), fail the gate with reason `"dynamic direction_restriction violated"`.
This should never actually fire in practice (Repo 1 only ever generates the
matching direction for a dynamic pick), but it is a cheap, valuable
safety net now that real money is involved and Repo 2 does not recompute
signals itself — it should not blindly trust a single upstream field
without a cross-check.

### 5. Bypass the shared lot-size cache for dynamic instruments

In `sizing.py`, `compute_qty()` gains a new parameter:
```python
def compute_qty(
    r, tradingsymbol, entry_ltp, paper_mode,
    kite=None,
    lot_multiplier: int = 1,
    lot_size_override: int | None = None,
) -> int:
    if lot_size_override is not None:
        lot_size = lot_size_override
    else:
        lot_size = get_lot_size(r, tradingsymbol)
    ...
    qty = lot_size * lot_multiplier
```
(Note: `lot_multiplier` here is the parameter added in the earlier
live-trading-sync spec — confirm it is already present from that prior
change; if not yet implemented, implement it here too per that spec.)

In `manager.try_enter()`, pass `lot_size_override=intent.get("lot_size") if intent.get("is_dynamic") else None`.

### 6. Snapshot dynamic metadata into the position record

In `state.fresh_position_from_intent()`, add:
```python
    "is_dynamic":       intent.get("is_dynamic", False),
    "equity_token":     intent.get("equity_token") if intent.get("is_dynamic") else None,
    "fno_exchange":     intent.get("fno_exchange") if intent.get("is_dynamic") else None,
    "direction_restriction": intent.get("direction_restriction") if intent.get("is_dynamic") else None,
```
This makes the position self-sufficient for its entire lifecycle (entry
through exit/cooldown) regardless of what `stock:dynamic_universe` contains
on later days — critical since Repo 1 overwrites that key at EOD for the
next trading day, potentially while today's dynamic-stock position from
*today* is still open into the next session (e.g., held past a squareoff
edge case, or a reconcile scenario). Verify `force_squareoff` and all
exit/cooldown paths in `manager.py` don't independently re-derive dynamic
metadata from the (possibly now-stale) `stock:dynamic_universe` key —
they must use the position record's own snapshotted fields.

### 7. RSI snapshot for trailing SL on dynamic positions

In `run.py`'s `_get_rsi_snapshot()`, it currently calls
`auth.get_underlying_token(r, instrument)`, which only checks static caches
and will raise for a dynamic stock's name. Change to prefer the position's
snapshotted `equity_token` when available:
```python
def _get_rsi_snapshot(kite, r, instrument, pos=None):
    try:
        if pos and pos.get("is_dynamic") and pos.get("equity_token"):
            token = pos["equity_token"]
        else:
            token = auth.get_underlying_token(r, instrument)
        ...
```
Update the call site in `_run_one_instrument` to pass the already-loaded
`pos` dict through.

### 8. Discord/journal labeling (verify, don't assume)

Check `journal.py` / the consolidated Discord tracker embed and Notion
logging for any assumption that `instrument` is always one of the static 17
(e.g. a lookup into a static sector/label table that would KeyError on an
unrecognized name). If found, make it degrade gracefully (e.g., omit the
sector tag, or label as "Dynamic"ic) rather than crashing that instrument's
tick. This is part of your parity/robustness pass — fix what you find.

## Verification checklist

```bash
# Confirm dynamic instruments are read fresh each tick, not cached daily:
grep -n "get_dynamic_instruments" executor/run.py

# Confirm the static-cache membership check is skipped only for dynamic intents,
# and the LTP check remains unconditional:
grep -n "is_dynamic" executor/gates.py

# Confirm direction_restriction safety check exists:
grep -n "direction_restriction" executor/gates.py

# Confirm lot_size_override wired through try_enter -> compute_qty:
grep -n "lot_size_override" executor/manager.py executor/sizing.py

# Confirm position snapshots dynamic metadata:
grep -n "is_dynamic\|equity_token\|direction_restriction" executor/state.py

# Confirm no condor code introduced:
grep -rln "condor" executor/*.py
# should return nothing
```

## Testing (hard gate)

- `tests/test_state.py`: `get_dynamic_instruments` — valid payload with 2
  picks, valid payload with 1 pick, missing key, stale date, malformed JSON,
  pick missing a required field (each should degrade to `[]` or drop the bad
  pick, never raise). Also test `fresh_position_from_intent` snapshots
  dynamic fields correctly for both dynamic and non-dynamic intents.
- `tests/test_sizing.py`: `compute_qty()` with `lot_size_override` set (bypasses
  `get_lot_size`) and unset (existing behavior unchanged).
- `tests/test_gates` (new or extended): dynamic intent skips static-cache
  check but still requires live LTP > 0; direction_restriction violation
  fails the gate; static intents unaffected.
- Run the full existing suite, confirm no regressions from the
  `compute_qty()` and `fresh_position_from_intent()` signature/shape changes.
- Paper-mode dry run recommended before going live with this: confirm in
  logs that on a day with an active dynamic pick, it appears in the
  per-tick instrument loop and (if signaled) enters correctly with the
  right lot size.

## Do not commit or push without manual confirmation

`git diff --name-only` must be reviewed and explicitly approved before
`git add` / `git commit` / `git push`. Sequencing reminder: this Repo 2
change must be deployed **before or simultaneously with** the matching Repo 1
change (`claude_change_spec_repo1_dynamic.md`) — never after, or Repo 1 will
begin writing dynamic-stock intents that an un-updated Repo 2 either ignores
(safe) or, if partially updated, could mishandle. Per standing rule: run the
local backtester and report P&L before any push, given this changes real
trading logic and touches the SL/target/sizing path indirectly (lot_size
override, position snapshot fields).
