# Change Spec: Consolidated Discord Tracker for Repo 2 (index-fno-auto-executor)

## Context & Goal

Repo 1's `src/trade_notifier.py` posts ONE Discord message per day per channel
(`send_paper_consolidated`), edits it in place every cycle via PATCH, and only
opens a new message the next calendar day. Repo 2's `executor/journal.py`
currently fires a brand-new, disposable message on every entry and exit
(`notify_entry`, `notify_exit`) with no per-tick "still open" update and no
message-ID tracking — which is why Discord only shows EXIT lines.

Goal: port Repo 1's exact edit-in-place mechanism to Repo 2, scoped across all
17 instruments in one consolidated embed, updated unconditionally every tick
(all instruments processed → one PATCH), with PAPER/LIVE shown explicitly in
the embed. `notify_gate_fail` stays a separate one-off message (mirrors Repo
1's `send_trade_skipped`). `notify_entry`/`notify_exit` one-offs are removed —
the consolidated message fully replaces their function.

## Scope

Files to modify:
- `executor/state.py` — add closed-today list + discord message-ID persistence
- `executor/journal.py` — add `_post_new`, `_edit_existing`, embed builder,
  `update_consolidated_tracker()`; remove `notify_entry`, `notify_exit`
- `executor/run.py` — remove the 3 call sites for `notify_entry`/`notify_exit`;
  append to closed-today list at the COOLDOWN transition; call
  `update_consolidated_tracker()` once per tick after the instrument loop

Files NOT touched: `executor/manager.py`, `executor/gates.py`,
`executor/gateway/*`, `executor/config.py`, all of Repo 1. No schema change to
the position dict itself — only new *auxiliary* Redis keys.

## Steps

### 1. `executor/state.py` — new keys

Add near the existing `_KEY_*` constants:

```python
_KEY_CLOSED_TODAY_PREFIX = "executor:closed_today:"    # + date_str (YYYY-MM-DD), Redis list
_KEY_DISCORD_MSG_ID_PREFIX = "executor:discord_msg_id:" # + date_str (YYYY-MM-DD)
```

Add new functions (place after `block_reason`, before the paper-mode-override
block):

```python
def _closed_today_key(date_str: str) -> str:
    return f"{_KEY_CLOSED_TODAY_PREFIX}{date_str}"


def append_closed_today(r: redis_lib.Redis, date_str: str, record: dict) -> None:
    """Append one closed-trade record to today's list. TTL refreshed on every
    write so the list survives until end of day regardless of write order."""
    key = _closed_today_key(date_str)
    r.rpush(key, json.dumps(record))
    r.expire(key, _DAILY_KEY_TTL_SECS)


def get_closed_today(r: redis_lib.Redis, date_str: str) -> list[dict]:
    raw_list = r.lrange(_closed_today_key(date_str), 0, -1)
    out: list[dict] = []
    for raw in raw_list or []:
        try:
            out.append(_loads_tolerant(raw))
        except Exception:
            continue
    return out


def _discord_msg_id_key(date_str: str) -> str:
    return f"{_KEY_DISCORD_MSG_ID_PREFIX}{date_str}"


def get_discord_msg_id(r: redis_lib.Redis, date_str: str) -> Optional[str]:
    raw = r.get(_discord_msg_id_key(date_str))
    return raw.decode() if isinstance(raw, bytes) else raw


def set_discord_msg_id(r: redis_lib.Redis, date_str: str, msg_id: str) -> None:
    r.set(_discord_msg_id_key(date_str), msg_id, ex=_DAILY_KEY_TTL_SECS)
```

Note: `_loads_tolerant` and `_DAILY_KEY_TTL_SECS` already exist in this file
— reused as-is, no redefinition.

### 2. `executor/journal.py` — consolidated tracker

Add imports at top (alongside existing `datetime`/`ZoneInfo`):

```python
from executor import state as state_module
```

Add module-level color constants (mirrors Repo 1's `trade_notifier.py`):

```python
OPEN_COLOR = 0x4FC3F7
CLOSED_COLOR = 0x00C853
LOSS_COLOR = 0xF44336
```

Add `_post_new` / `_edit_existing`, ported verbatim from Repo 1's
`trade_notifier.py` but pointed at `DISCORD_WEBHOOK_URL` (Repo 2's existing
env var — no new secret needed):

```python
def _post_new(embed: dict) -> str | None:
    if not _DISCORD_WEBHOOK:
        return None
    try:
        resp = requests.post(
            _DISCORD_WEBHOOK + "?wait=true",
            json={"embeds": [embed]},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return str(resp.json().get("id", ""))
        log.warning("journal: consolidated POST returned %d: %s",
                    resp.status_code, resp.text[:200])
        return None
    except Exception as exc:
        log.warning("journal: consolidated POST failed: %s", exc)
        return None


def _edit_existing(msg_id: str, embed: dict) -> bool:
    if not _DISCORD_WEBHOOK:
        return False
    try:
        resp = requests.patch(
            f"{_DISCORD_WEBHOOK}/messages/{msg_id}",
            json={"embeds": [embed]},
            timeout=10,
        )
        ok = resp.status_code in (200, 204)
        if not ok:
            log.warning("journal: consolidated PATCH returned %d: %s",
                        resp.status_code, resp.text[:200])
        return ok
    except Exception as exc:
        log.warning("journal: consolidated PATCH failed: %s", exc)
        return False
```

Add embed builder — same field shape as Repo 1's
`_build_consolidated_embed`, plus explicit mode label (Repo 1 has none,
Repo 2 needs it since it serves both PAPER and LIVE):

```python
def _build_consolidated_embed(
    open_positions: list[dict],
    closed_positions: list[dict],
    date_str: str,
    mode_str: str,
) -> dict:
    fields = []

    for pos in open_positions:
        arrow = "↑" if pos.get("direction") == "CE" else "↓"
        ltp   = pos.get("entry_premium", 0)  # updated via reconcile each tick
        entry = pos.get("entry_premium", 0)
        sl    = pos.get("sl_premium", 0)
        qty   = pos.get("qty", 0)
        direction = pos.get("direction", "?")
        # Options always bought long — premium rising is always profit.
        # Do not branch on direction (Repo 1's PE sign-inversion bug).
        unreal = (ltp - entry) * qty
        sign = "+" if unreal >= 0 else ""
        fields.append({
            "name": f"{pos.get('tradingsymbol', pos.get('instrument'))} {direction} {arrow} [OPEN]",
            "value": (
                f"Entry ₹{entry:.2f} · SL ₹{sl:.2f}\n"
                f"Unrealized ≈ {sign}₹{unreal:.0f} (gross, est.)"
            ),
            "inline": False,
        })

    for rec in closed_positions:
        arrow = "↑" if rec.get("direction") == "CE" else "↓"
        pnl   = rec.get("pnl", 0) or 0
        sign  = "+" if pnl >= 0 else ""
        fields.append({
            "name": f"{rec.get('tradingsymbol', rec.get('instrument'))} {rec.get('direction')} {arrow} [CLOSED]",
            "value": (
                f"Entry ₹{rec.get('entry_premium', 0):.2f} · "
                f"Exit ₹{rec.get('exit_premium', 0):.2f} · "
                f"Net {sign}₹{pnl:.2f} · {rec.get('exit_reason', '')}"
            ),
            "inline": False,
        })

    if not fields:
        fields.append({
            "name": "No activity",
            "value": "No open or closed positions yet today.",
            "inline": False,
        })

    return {
        "title":     f"📊 Executor — {mode_str} — {date_str}",
        "color":     OPEN_COLOR,
        "fields":    fields,
        "footer":    {"text": f"{mode_str} mode · updated each cycle"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
```

Add the orchestrating function (called once per tick from `run.py`):

```python
def update_consolidated_tracker(r) -> bool:
    """Rebuild and PATCH (or POST, on first call of the day) the single
    consolidated Discord message covering all 17 instruments. Mirrors Repo
    1's send_paper_consolidated. Called unconditionally once per tick,
    regardless of whether anything changed this cycle."""
    if not _DISCORD_WEBHOOK:
        return False

    date_str = datetime.now(_IST).date().isoformat()
    mode_str = "PAPER" if os.getenv("PAPER_MODE", "true").lower() != "false" else "LIVE"

    open_positions = []
    for inst in state_module.list_open_instruments(r):
        pos = state_module.load_position(r, inst)
        if pos:
            open_positions.append(pos)

    closed_positions = state_module.get_closed_today(r, date_str)

    embed = _build_consolidated_embed(open_positions, closed_positions, date_str, mode_str)

    existing_id = state_module.get_discord_msg_id(r, date_str)
    if existing_id:
        return _edit_existing(existing_id, embed)

    msg_id = _post_new(embed)
    if msg_id:
        state_module.set_discord_msg_id(r, date_str, msg_id)
        log.info("journal: consolidated tracker message created (id=%s)", msg_id)
        return True
    return False
```

**Remove** `notify_entry()` and `notify_exit()` entirely (superseded by the
consolidated tracker). Keep `notify_gate_fail()` unchanged — it remains the
one-off skip-style notice, matching Repo 1's `send_trade_skipped` being
separate from the consolidated message.

### 3. `executor/run.py` — wire it in

- Delete the 3 call sites:
  - line 278: `journal.notify_entry(state_module.load_position(r, instrument) or {})`
  - line 294: `journal.notify_entry(pos)`
  - line 306: `journal.notify_exit(pos)`
- In `_journal_if_cooldown`, right before `state_module.save_position`, append
  the closing record to the daily list:

```python
def _journal_if_cooldown(r: redis_lib.Redis, instrument: str) -> None:
    pos = state_module.load_position(r, instrument)
    if pos and pos.get("phase") == "COOLDOWN" and not pos.get("notion_journaled"):
        journal.log_trade_to_notion(pos)
        date_str = datetime.now(IST).date().isoformat()
        state_module.append_closed_today(r, date_str, pos)
        pos["notion_journaled"] = True
        state_module.save_position(r, instrument, pos)
```

  (`datetime` and `IST` are already imported at the top of `run.py`.)

- In `main()`, after the instrument loop (after the `for inst_cfg in
  config.INDICES + config.STOCKS:` block, before `log.info("=== executor
  tick end ===")`), add the single unconditional update call:

```python
    # ── 4. Update consolidated Discord tracker — once per tick, always ─────────
    try:
        journal.update_consolidated_tracker(r)
    except Exception as exc:
        log.error("consolidated tracker update failed: %s", exc)

    log.info("=== executor tick end ===")
```

### Edge cases handled

- **Message ID lifecycle across squareoff / midnight**: date-keyed Redis key
  (`executor:discord_msg_id:{date}`) with `ex=_DAILY_KEY_TTL_SECS` (86400,
  already defined in `state.py`) — same mechanism as Repo 1. After 15:10
  squareoff the closed section fills in on the existing message; a new date
  → new key → new message next trading day. No manual rollover needed.
- **Empty tick (no positions at all yet)**: embed falls back to a "No
  activity" field, same as Repo 1.
- **PAPER/LIVE mode switch mid-day**: mode label re-evaluated from
  `PAPER_MODE` env var every call — reflects current mode even if it changed
  since the message was first created.
- **One instrument's failure isolation preserved**: the tracker update is
  wrapped in its own try/except in `main()`, so a Discord API failure never
  aborts the tick, matching the existing per-instrument isolation pattern.
- **Duplicate closed-today entries**: guarded the same way Repo 1 guards
  duplicate EOD rows — `notion_journaled` flag gates the append, so a
  position can only be appended to `closed_today` once (same tick it's
  journaled to Notion).

## Verification Checklist

```bash
# 1. Confirm removed functions are gone and nothing still references them
grep -n "notify_entry\|notify_exit" executor/journal.py executor/run.py
# Expected: no output

# 2. Confirm new functions exist
grep -n "_post_new\|_edit_existing\|_build_consolidated_embed\|update_consolidated_tracker" executor/journal.py
grep -n "append_closed_today\|get_closed_today\|get_discord_msg_id\|set_discord_msg_id" executor/state.py

# 3. Confirm run.py wiring
grep -n "update_consolidated_tracker\|append_closed_today" executor/run.py

# 4. Syntax / import sanity
python3 -m py_compile executor/journal.py executor/state.py executor/run.py

# 5. Run existing test suite — no regressions
pytest -q

# 6. If tests reference notify_entry/notify_exit, they must be updated/removed
grep -rn "notify_entry\|notify_exit" tests/
```

## Commit Message

```
feat(journal): consolidated edit-in-place Discord tracker for executor

- Port Repo 1's single-message-per-day pattern (POST once, PATCH every
  tick) to Repo 2, scoped across all 17 instruments in one embed.
- Add executor:closed_today:{date} and executor:discord_msg_id:{date}
  Redis keys (state.py) to support cross-tick message-ID persistence
  and same-day closed-trade aggregation.
- Remove notify_entry/notify_exit one-off posts — fully superseded by
  the consolidated tracker, called unconditionally once per tick from
  run.py's main().
- notify_gate_fail unchanged — remains a separate one-off notice,
  mirroring Repo 1's send_trade_skipped.
- Embed explicitly labels PAPER/LIVE mode (Repo 1 has no equivalent —
  paper-only there).
```

## Frozen Files (do not touch in this change)

- `executor/manager.py`
- `executor/gates.py`
- `executor/gateway/paper.py`, `executor/gateway/kite_live.py`
- `executor/config.py`
- `executor/utils/*`
- All of `index-fno-signal-bot` (Repo 1) — this change is Repo 2 only
