"""Lightweight aiohttp web server for the trading bot dashboard.

Exposes:
  GET /             → serves dashboard.html
  GET /api/state    → full JSON snapshot for polling
  GET /api/events/<symbol>  → last 50 events for a symbol from history.db
"""

import json
import os
import time
from pathlib import Path

from aiohttp import web
from logger import logger


# ---------------------------------------------------------------------------
# Helpers — read shared in-memory state from other modules
# ---------------------------------------------------------------------------

def _get_state_snapshot() -> dict:
    """Build a JSON-serialisable snapshot of the current bot state."""
    from config import token_registry
    from models import state_manager
    from data.collector import candles_1m, candles_15m
    from bot.telegram import _last_analysis_cache

    symbols = token_registry.get_all()
    coins = []

    for sym in symbols:
        c1m = candles_1m.get(sym, [])
        c15m = candles_15m.get(sym, [])

        current_price = c1m[-1]["close"] if c1m else None
        price_24h_ago = None

        # Approximate 24h change from 15M candles (96 candles × 15m = 24h)
        pct_change = None
        if c15m and len(c15m) >= 2:
            old = c15m[-min(len(c15m), 96)]["open"]
            if old and current_price:
                pct_change = round((current_price - old) / old * 100, 2)

        # Last 15M candle volume (quote)
        vol_15m = None
        if c15m:
            last15 = c15m[-1]
            # quoteVolume = volume * close (approximation; exact only from REST)
            vol_15m = round(last15["volume"] * last15["close"])

        # Active monitors for this symbol
        state = state_manager.get_state(sym)
        monitored_levels = []
        for task_key in state.tasks:
            from models import SymbolState
            parsed = SymbolState.parse_task_key(task_key)
            if parsed:
                monitored_levels.append(parsed[1])

        # Levels from last /analyze cache
        cached_levels = _last_analysis_cache.get(sym, [])
        levels = []
        for lvl in cached_levels:
            price = lvl.get("level")
            dist_pct = None
            if current_price and price:
                dist_pct = round((price - current_price) / current_price * 100, 2)
            levels.append({
                "price":    price,
                "type":     lvl.get("type", ""),
                "strength": lvl.get("strength", 0),
                "monitored": price in monitored_levels,
                "dist_pct": dist_pct,
            })

        # Sort: closest to current price first
        if current_price:
            levels.sort(key=lambda l: abs(l["dist_pct"]) if l["dist_pct"] is not None else 9999)

        coins.append({
            "symbol":          sym,
            "price":           current_price,
            "pct_change":      pct_change,
            "vol_15m":         vol_15m,
            "phase":           state.phase,
            "monitor_count":   len(monitored_levels),
            "levels":          levels,
        })

    # Sort coins: monitoring first, then by abs pct_change desc
    coins.sort(key=lambda c: (
        -(c["monitor_count"] > 0),
        -(abs(c["pct_change"]) if c["pct_change"] is not None else 0),
    ))

    return {
        "ts":    int(time.time()),
        "coins": coins,
    }


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def handle_index(request: web.Request) -> web.Response:
    html_path = Path(__file__).parent / "dashboard.html"
    if not html_path.exists():
        return web.Response(text="dashboard.html not found", status=404)
    return web.FileResponse(html_path)


async def handle_state(request: web.Request) -> web.Response:
    try:
        snapshot = _get_state_snapshot()
        return web.Response(
            text=json.dumps(snapshot, ensure_ascii=False),
            content_type="application/json",
        )
    except Exception as e:
        logger.exception("Error building state snapshot")
        return web.Response(
            text=json.dumps({"error": str(e)}),
            content_type="application/json",
            status=500,
        )


async def handle_events(request: web.Request) -> web.Response:
    symbol = request.match_info.get("symbol", "").upper()
    if not symbol:
        return web.Response(text=json.dumps([]), content_type="application/json")
    try:
        from data.history import get_symbol_history
        events = await get_symbol_history(symbol, limit=50)
        return web.Response(
            text=json.dumps(events, ensure_ascii=False, default=str),
            content_type="application/json",
        )
    except Exception as e:
        logger.exception("Error fetching events", symbol=symbol)
        return web.Response(
            text=json.dumps({"error": str(e)}),
            content_type="application/json",
            status=500,
        )


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

async def start_web_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Start aiohttp server. Designed to run inside asyncio.gather()."""
    app = web.Application()
    app.router.add_get("/",                    handle_index)
    app.router.add_get("/api/state",           handle_state)
    app.router.add_get("/api/events/{symbol}", handle_events)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info(f"Dashboard available at http://{host}:{port}")

    # Keep running forever (asyncio.gather will hold it)
    import asyncio
    while True:
        await asyncio.sleep(3600)
