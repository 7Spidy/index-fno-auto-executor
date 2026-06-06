"""
Journal — Notion trade log + Discord P&L summary.  Spec §17.
Fire-and-forget; errors are logged but never allowed to abort the executor run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

_DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
_NOTION_TOKEN    = os.getenv("NOTION_TOKEN", "")
_NOTION_DB_ID    = os.getenv("NOTION_TRADE_DB_ID", "")
_NOTION_API      = "https://api.notion.com/v1"
_NOTION_VERSION  = "2022-06-28"


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
    msg = (
        f"**[EXECUTOR] ENTRY** `{pos.get('tradingsymbol')}` "
        f"dir={pos.get('direction')} "
        f"entry=₹{pos.get('entry_premium', 0):.2f} "
        f"sl=₹{pos.get('sl_premium', 0):.2f} "
        f"target=₹{pos.get('target_premium', 0):.2f} "
        f"qty={pos.get('qty')} "
        f"({'PAPER' if os.getenv('PAPER_MODE', 'true').lower() != 'false' else 'LIVE'})"
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


def notify_milestone(pos: dict, milestone: str) -> None:
    msg = (
        f"**[EXECUTOR] {milestone}** `{pos.get('tradingsymbol')}` "
        f"sl=₹{pos.get('sl_premium', 0):.2f} "
        f"peak=₹{pos.get('peak_premium', 0):.2f}"
    )
    _discord(msg)


# ── Notion ─────────────────────────────────────────────────────────────────────

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


def log_trade_to_notion(pos: dict) -> None:
    """
    Create (or update) a trade record in the Notion trade database.
    Called on COOLDOWN transition (trade fully closed).
    """
    if not _NOTION_TOKEN or not _NOTION_DB_ID:
        log.debug("journal: NOTION_TOKEN or NOTION_TRADE_DB_ID not set — skipping Notion")
        return

    ts = datetime.now(timezone.utc).isoformat()
    pnl = pos.get("pnl")

    properties = {
        "Trade":       _notion_title(pos.get("tradingsymbol", "")),
        "Direction":   _notion_select(pos.get("direction", "")),
        "Entry ₹":     _notion_number(pos.get("entry_premium")),
        "Exit ₹":      _notion_number(pos.get("exit_premium")),
        "SL ₹":        _notion_number(pos.get("sl_premium")),
        "Target ₹":    _notion_number(pos.get("target_premium")),
        "Qty":         _notion_number(pos.get("qty")),
        "P&L ₹":       _notion_number(pnl),
        "Exit Reason": _notion_select(pos.get("exit_reason", "unknown")),
        "Entry TS":    _notion_text(pos.get("entry_ts", "")),
        "Intent TS":   _notion_text(pos.get("intent_ts", "")),
        "Mode":        _notion_select("PAPER" if os.getenv("PAPER_MODE", "true").lower() != "false" else "LIVE"),
        "Health Score": _notion_number(pos.get("last_health_score")),
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
            log.warning("journal: Notion create page failed %d: %s",
                        resp.status_code, resp.text[:200])
        else:
            log.info("journal: Notion trade logged page_id=%s",
                     resp.json().get("id", "?"))
    except Exception as exc:
        log.warning("journal: Notion post failed: %s", exc)
