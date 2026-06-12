"""SQLite storage for live Bybit trades — live_trades.db.

Схема таблицы расширена полями Bybit: bybit_order_ids, bybit_sl_order_id,
bybit_position_qty, реальный fill по биржевым данным.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import aiosqlite

try:
    from config import RAILWAY_VOLUME_MOUNT_PATH
    DB_PATH = os.path.join(RAILWAY_VOLUME_MOUNT_PATH, "live_trades.db")
except Exception:
    DB_PATH = os.path.join(os.path.dirname(__file__), "..", "live_trades.db")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS live_trades (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                TEXT NOT NULL UNIQUE,
    paper_trade_id          TEXT,
    symbol                  TEXT NOT NULL,
    level                   REAL NOT NULL,
    level_type              TEXT,
    entry_price             REAL,
    entry_time              REAL NOT NULL,
    position_size_usdt      REAL NOT NULL,
    direction               TEXT NOT NULL DEFAULT 'long',

    -- Bybit ордера сетки
    grid_orders_json        TEXT DEFAULT '[]',
    grid_fill_count         INTEGER DEFAULT 0,

    -- Bybit IDs
    bybit_order_ids_json    TEXT DEFAULT '[]',
    bybit_sl_order_id       TEXT,
    bybit_position_qty      REAL DEFAULT 0,

    -- SL/TP параметры
    stop_loss               REAL,
    take_profit_1           REAL,
    take_profit_2           REAL,

    -- Выход
    exit_price              REAL,
    exit_time               REAL,
    exit_reason             TEXT,
    pnl_usdt                REAL,
    duration_minutes        REAL,
    status                  TEXT NOT NULL DEFAULT 'open',

    -- Служебные
    events_json             TEXT DEFAULT '[]',
    error_log_json          TEXT DEFAULT '[]',
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lt_symbol ON live_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_lt_status ON live_trades(status);
"""


async def init_live_trades_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_CREATE_TABLE)
        await db.commit()


async def open_live_trade(trade: dict) -> str:
    """Записать новую live-сделку. Возвращает trade_id."""
    now = time.time()
    trade_id = trade["trade_id"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO live_trades
               (trade_id, paper_trade_id, symbol, level, level_type,
                entry_price, entry_time, position_size_usdt, direction,
                grid_orders_json, grid_fill_count,
                bybit_order_ids_json, bybit_sl_order_id, bybit_position_qty,
                stop_loss, take_profit_1, take_profit_2,
                status, events_json, error_log_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_id,
                trade.get("paper_trade_id"),
                trade["symbol"],
                trade["level"],
                trade.get("level_type"),
                trade.get("entry_price"),
                trade.get("entry_time", now),
                trade["position_size_usdt"],
                trade.get("direction", "long"),
                json.dumps(trade.get("grid_orders", [])),
                0,
                json.dumps(trade.get("bybit_order_ids", [])),
                trade.get("bybit_sl_order_id"),
                0.0,
                trade.get("stop_loss"),
                trade.get("take_profit_1"),
                trade.get("take_profit_2"),
                "open",
                "[]",
                "[]",
                now,
                now,
            ),
        )
        await db.commit()
    return trade_id


async def add_live_event(trade_id: str, event_type: str, note: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT events_json FROM live_trades WHERE trade_id = ?", (trade_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        try:
            events = json.loads(row[0] or "[]")
        except Exception:
            events = []
        events.append({"time": time.time(), "type": event_type, "note": note})
        await db.execute(
            "UPDATE live_trades SET events_json = ?, updated_at = ? WHERE trade_id = ?",
            (json.dumps(events), time.time(), trade_id),
        )
        await db.commit()


async def log_live_error(trade_id: str, context: str, error: str) -> None:
    """Добавить запись об ошибке в error_log_json сделки."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT error_log_json FROM live_trades WHERE trade_id = ?", (trade_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        try:
            errors = json.loads(row[0] or "[]")
        except Exception:
            errors = []
        errors.append({"time": time.time(), "context": context, "error": error})
        await db.execute(
            "UPDATE live_trades SET error_log_json = ?, updated_at = ? WHERE trade_id = ?",
            (json.dumps(errors), time.time(), trade_id),
        )
        await db.commit()


async def update_live_trade(trade_id: str, **fields) -> None:
    """Обновить произвольные поля сделки."""
    if not fields:
        return
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [trade_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE live_trades SET {set_clause} WHERE trade_id = ?", values
        )
        await db.commit()


async def close_live_trade(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
    pnl_usdt: float,
) -> None:
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT entry_time FROM live_trades WHERE trade_id = ?", (trade_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        duration = (now - row[0]) / 60
        await db.execute(
            """UPDATE live_trades
               SET exit_price = ?, exit_time = ?, exit_reason = ?,
                   pnl_usdt = ?, duration_minutes = ?,
                   status = 'closed', updated_at = ?
               WHERE trade_id = ?""",
            (exit_price, now, exit_reason, round(pnl_usdt, 4),
             round(duration, 2), now, trade_id),
        )
        await db.commit()


async def get_open_live_trades() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM live_trades WHERE status = 'open' ORDER BY entry_time"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_live_trade_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pnl_usdt, duration_minutes FROM live_trades WHERE status = 'closed'"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_pnl_usdt": 0.0, "avg_duration_minutes": 0.0}
    total = len(rows)
    wins = [r[0] for r in rows if (r[0] or 0) > 0]
    losses = [r[0] for r in rows if (r[0] or 0) <= 0]
    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1),
        "total_pnl_usdt": round(sum(r[0] or 0 for r in rows), 4),
        "avg_duration_minutes": round(sum(r[1] or 0 for r in rows) / total, 1),
    }
