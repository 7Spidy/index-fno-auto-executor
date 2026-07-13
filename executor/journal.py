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

log = logging.getLogger(__name__)

_DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
_NOTION_TOKEN    = os.getenv("NOTION_TOKEN", "")
_NOTION_DB_ID    = os.getenv("NOTION_TRADE_DB_ID", "")
_NOTION_API      = "https://api.notion.com/v1"
_NOTION_VERSION  = "2022-06-28"
_IST             = ZoneInfo("Asia/Kolkata")

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


def notify_entry(pos: dict) -> None:
    mode = "PAPER" if os.getenv("PAPER_MODE", "true").lower() != "false" else "LIVE"
    msg = (
        f"**[EXECUTOR] ENTRY** `{pos.get('tradingsymbol')}` "
        f"dir={pos.get('direction')} "
        f"entry=₹{pos.get('entry_premium', 0):.2f} "
        f"sl=₹{pos.get('sl_premium', 0):.2f} "
        f"qty={pos.get('qty')} "
        f"({mode})"
    )
    _discord(msg)


def notify_exit(pos: dict) -> None:
    pnl = pos.get("pnl")
    pnl_str = f"₹{pnl:+.2f}" if pnl is not None else "n/a"
    emoji = "✅" if (pnl or 0) >= 0 else "❌"
    msg = (
        f"**[EXECUTOR] EXIT {emoji}** `{pos.get('tradingsymbol')}` "
        f"reason={pos.get('exit_reason')} "
        f"exit=₹{pos.get('exit_premium', 0):.2f} "
        f"P&L={pnl_str}"
    )
    _discord(msg)


def notify_gate_fail(reason: str, intent: dict) -> None:
    msg = (
        f"**[EXECUTOR] GATE FAIL** `{intent.get('tradingsymbol')}` "
        f"reason: {reason}"
    )
    _discord(msg)


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
