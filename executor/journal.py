"""
Journal — Notion trade log + Discord P&L summary.  Spec §17.
Fire-and-forget; errors are logged but never allowed to abort the executor run.

Notion DB: FnO Trade Log
DB ID: 26c9ff615ccf4f8181143be6417fbf7e
Collection: 63df2cd2-95a5-47fe-8736-c73f1dda1ade
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from executor import state as state_module

log = logging.getLogger(__name__)

_DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
_NOTION_TOKEN    = os.getenv("NOTION_TOKEN", "")
_NOTION_DB_ID    = os.getenv("NOTION_TRADE_DB_ID", "")
_NOTION_API      = "https://api.notion.com/v1"
_NOTION_VERSION  = "2022-06-28"
_IST             = ZoneInfo("Asia/Kolkata")

OPEN_COLOR   = 0x4FC3F7
CLOSED_COLOR = 0x00C853
LOSS_COLOR   = 0xF44336

# ── Exit reason mapping ────────────────────────────────────────────────────────
# Maps internal exit_reason codes → Notion select option names (must match DB exactly)

_EXIT_REASON_MAP: dict[str, str] = {
    "sl_hit":         "SL Hit",
    "hard_squareoff": "Square-off",
    "flat_external":  "Square-off",  # position closed outside the executor's own orders
}


def _map_exit_reason(raw: str) -> str:
    """Map internal exit_reason string to Notion select option. Falls back to '—'."""
    if not raw:
        return "—"
    return _EXIT_REASON_MAP.get(str(raw).lower().strip(), "—")


def _fmt_ist(iso_str: str) -> str:
    """Convert UTC ISO timestamp string to 'HH:MM IST'. Returns '' on failure."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        ist = dt.astimezone(_IST)
        return ist.strftime("%H:%M IST")
    except Exception:
        return str(iso_str)


# ── Notion property helpers ────────────────────────────────────────────────────

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {_NOTION_TOKEN}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _notion_text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": str(value)}}]}


def _notion_number(value) -> dict:
    return {"number": float(value) if value is not None else None}


def _notion_select(value: str) -> dict:
    return {"select": {"name": str(value)}}


def _notion_title(value: str) -> dict:
    return {"title": [{"text": {"content": str(value)}}]}


def _notion_date(iso_date: str) -> dict:
    """ISO date string (YYYY-MM-DD) → Notion date property."""
    return {"date": {"start": iso_date}}


# ── Discord ────────────────────────────────────────────────────────────────────

def _discord(msg: str) -> None:
    if not _DISCORD_WEBHOOK:
        log.debug("journal: DISCORD_WEBHOOK_URL not set — skipping Discord")
        return
    try:
        requests.post(_DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
    except Exception as exc:
        log.warning("journal: Discord post failed: %s", exc)


def notify_gate_fail(reason: str, intent: dict) -> None:
    msg = (
        f"**[EXECUTOR] GATE FAIL** `{intent.get('tradingsymbol')}` "
        f"reason: {reason}"
    )
    _discord(msg)


# ── Consolidated tracker (edit-in-place, one message per day) ──────────────────

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


# ── Notion ─────────────────────────────────────────────────────────────────────

def log_trade_to_notion(pos: dict) -> None:
    """
    Create a trade record in the Notion FnO Trade Log database.
    Called once on COOLDOWN transition (guarded by notion_journaled flag in run.py).
    Fire-and-forget — never raises; logs warnings on failure.

    Property names must match the Notion DB schema exactly (verified 2026-06-13):
      Day, Date, Direction, Entry Premium, Entry Time, Exit Premium,
      Exit Reason, Exit Time, Mode, Net P&L, SL Premium, Status, Target Premium
    """
    if not _NOTION_TOKEN or not _NOTION_DB_ID:
        log.debug("journal: NOTION_TOKEN or NOTION_TRADE_DB_ID not set — skipping Notion")
        return

    pnl = pos.get("pnl")
    mode_str = "Paper" if os.getenv("PAPER_MODE", "true").lower() != "false" else "Live"
    today_iso = datetime.now(timezone.utc).date().isoformat()
    exit_ts   = pos.get("cooldown_start_ts", "")   # set when trade enters COOLDOWN

    properties = {
        # Title — option tradingsymbol, e.g. "NIFTY25610724500CE"
        "Day":            _notion_title(pos.get("tradingsymbol", "")),
        # Date — trading date (UTC date; close enough for IST same-day)
        "Date":           _notion_date(today_iso),
        # Direction
        "Direction":      _notion_select(pos.get("direction", "—") or "—"),
        # Premiums
        "Entry Premium":  _notion_number(pos.get("entry_premium")),
        "Exit Premium":   _notion_number(pos.get("exit_premium")),
        "SL Premium":     _notion_number(pos.get("sl_premium")),
        "Target Premium": _notion_number(pos.get("target_premium")),
        # P&L
        "Net P&L":        _notion_number(pnl),
        # Exit
        "Exit Reason":    _notion_select(_map_exit_reason(pos.get("exit_reason", ""))),
        # Times — formatted as HH:MM IST
        "Entry Time":     _notion_text(_fmt_ist(pos.get("entry_ts", ""))),
        "Exit Time":      _notion_text(_fmt_ist(exit_ts)),
        # Meta
        "Mode":           _notion_select(mode_str),
        "Status":         _notion_select("Traded"),
    }

    body = {
        "parent": {"database_id": _NOTION_DB_ID},
        "properties": properties,
    }

    try:
        resp = requests.post(
            f"{_NOTION_API}/pages",
            headers=_notion_headers(),
            json=body,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            log.warning(
                "journal: Notion create page failed %d: %s",
                resp.status_code, resp.text[:300],
            )
        else:
            log.info(
                "journal: Notion trade logged  sym=%s  pnl=%s  page_id=%s",
                pos.get("tradingsymbol"), pnl, resp.json().get("id", "?"),
            )
    except Exception as exc:
        log.warning("journal: Notion post failed: %s", exc)
