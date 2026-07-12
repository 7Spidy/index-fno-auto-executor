# index-fno-auto-executor

> Automated F&O position manager for NIFTY options — paper mode first, live when ready.

![Python](https://img.shields.io/badge/python-3.11-blue?style=flat-square)
![Mode](https://img.shields.io/badge/mode-PAPER-yellow?style=flat-square)
![Trigger](https://img.shields.io/badge/trigger-cron--job.org-lightgrey?style=flat-square)
![Exchange](https://img.shields.io/badge/exchange-NSE%20%2F%20NFO-orange?style=flat-square)

---

## What this does

When the companion signal bot ([index-fno-signal-bot](https://github.com/7Spidy/index-fno-signal-bot)) fires all 4 conditions on a side, this executor:

1. **Gates** the intent — VIX, risk points, cooldown, time window, option tradable
2. **Enters** — sizes the position, places entry + SL-M + target bracket on the exchange
3. **Manages every minute** — ratchets the stop, scores trade health on 5-min candles, rides winners into runner mode, exits at the right moment
4. **Journals** — logs every trade to Notion and posts P&L to Discord

All rules are frozen deterministic logic. **No LLM in the live loop.**

---

## Architecture

```
cron-job.org  (every 1 min, 09:40–15:10 IST)
     │
     ▼
GitHub Actions  executor.yml
     │
     ├── Read state ──────────────────── Upstash Redis
     │
     ├── Fetch LTP ───────────────────── Kite Connect REST
     │
     ├── Run manager
     │     ├── Entry gate (6 checks)
     │     ├── Milestone ladder  (breakeven → fork → lock → runner)
     │     ├── Health score      (C1–C4 on NIFTY spot 5-min candles)
     │     ├── Caution trailing  (swing lows/highs → premium via delta)
     │     └── Exit conditions   (9 priority-ordered rules)
     │
     ├── Modify / cancel orders ──────── Kite Connect REST
     │
     ├── Write state ─────────────────── Upstash Redis
     │
     └── Journal ─────────────────────── Notion + Discord
```

---

## State machine

```
IDLE ──► ENTERING ──► OPEN_FIXED ──► LOCKED ──► EXITING ──► COOLDOWN ──► IDLE
                            │                       ▲
                            └──────► RUNNER ────────┘
```

| Phase | What's happening |
|-------|-----------------|
| `IDLE` | Waiting for a signal intent from the signal bot |
| `ENTERING` | Entry order placed; waiting for fill confirmation |
| `OPEN_FIXED` | Position live; managing milestones and health score |
| `LOCKED` | Progress ≥ 90% of target; SL locked at 70% of move |
| `RUNNER` | Target cancelled; trailing at peak × 0.90 / 0.95 |
| `EXITING` | Exit order placed; waiting for flat confirmation |
| `COOLDOWN` | 15-min cooldown before accepting new signals |

---

## Module structure

```
executor/
├── run.py              ← GH Actions entrypoint (one tick per invocation)
├── config.py           ← All frozen constants — source of truth is the spec
├── manager.py          ← State machine driver
├── health.py           ← Weighted score (C1–C4), VWAP veto, reversal detection
├── trailing.py         ← Caution swing trail + monotonic ratchet invariant
├── sizing.py           ← Position sizing and level derivation
├── gates.py            ← Entry filters: VIX, cooldown, time, option tradable
├── state.py            ← Redis R/W, idempotency, startup reconcile, committed_premium()
├── journal.py          ← Notion trade log + Discord P&L summary
├── gateway/
│   ├── base.py         ← OrderGateway ABC
│   ├── paper.py        ← Simulated fills + honest cost model
│   └── kite_live.py    ← Real Kite Connect orders
└── utils/
    ├── kite_client.py  ← KiteConnect wrapper (market data + get_margins())
    ├── indicators.py   ← VWAP, RSI, ATR, DMI
    ├── auth.py         ← Redis-backed token helpers
    └── calendar_nse.py ← IST utilities

tests/
├── test_sizing.py      ← compute_qty() / get_daily_loss_limit() coverage
└── test_state.py       ← committed_premium() coverage
```

---

## Setup

### 1. Prerequisites

- `index-fno-signal-bot` running and writing `executor:pending_intent` to Redis on signal fire
- `morning-login.yml` (from signal bot repo) running daily ~09:05 IST — writes the Kite access token and instrument caches to Redis

### 2. GitHub Secrets

| Secret | Description |
|--------|-------------|
| `REDIS_URL` | Upstash Redis connection string |
| `KITE_API_KEY` | Kite Connect API key |
| `DISCORD_WEBHOOK_URL` | Discord webhook for trade alerts |
| `NOTION_TOKEN` | Notion integration token |
| `NOTION_TRADE_DB_ID` | Notion database ID for the trade log |

### 3. GitHub Variable

| Variable | Value |
|----------|-------|
| `PAPER_MODE` | `True` (paper) · `False` (live) |

### 4. cron-job.org trigger

| Field | Value |
|-------|-------|
| URL | `https://api.github.com/repos/7Spidy/index-fno-auto-executor/actions/workflows/executor.yml/dispatches` |
| Method | `POST` |
| Schedule | Every 1 min · Mon–Fri · 04:10–09:40 UTC |
| Header | `Authorization: Bearer {GITHUB_PAT}` |
| Header | `Accept: application/vnd.github+json` |
| Body | `{"ref": "main"}` |

---

## Sizing & risk parameters (v2 — fixed-lot, capital-availability)

Sizing is fixed-lot (always 1 lot, from the shared Kite instrument cache) gated on
capital availability, matching the companion signal bot's `paper_engine.py` exactly:

| Mode | Capital source | Daily loss limit |
|------|----------------|-------------------|
| Paper (`PAPER_MODE=True`) | Fixed ₹1,00,000 (`CAPITAL_RS`) | −15% of `CAPITAL_RS` |
| Live (`PAPER_MODE=False`) | Real-time available margins (`kite.get_margins()`, short-TTL Redis cached) | −15% of live available margins |

| Parameter | Value |
|-----------|-------|
| Lot size | 1 lot per instrument (auto-synced from the shared Redis instrument cache) |
| Instrument | NIFTY weekly options (Tuesday expiry) |
| Max positions | 1 at a time |
| Entry skipped if | `entry_ltp × lot_size` exceeds remaining capital |

---

## Paper → Live

When you've validated ≥ 1 week of paper trades:

1. Set the `PAPER_MODE` GitHub variable to `False`
2. Read spec §21 (SEBI retail algo framework) before placing real capital

No code changes needed.

---

## Spec

All rules, constants, and design decisions are documented in [`fno-auto-executor-spec-v1.md`](fno-auto-executor-spec-v1.md). The spec is frozen for v1. **Do not change any frozen constant without updating the spec first.**
