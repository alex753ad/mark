"""Strategy 2: Limit Grid — 5 лимитных ордеров в зоне уровня."""

from __future__ import annotations

import json
import time
import uuid

import aiosqlite

from trading.base_strategy import BaseStrategy
from trading.trade_log import open_trade, close_trade, add_trade_event, get_open_trades, DB_PATH
from bot.telegram import send_message
from constants import (
    S2_MIN_STRENGTH,
    S2_MIN_P_BOUNCE,
    S2_PRESSURE_COOLDOWN_SECONDS,
    S2_GRID_ORDERS,
)
from data.collector import candles_1m
from logger import logger


class Strategy2LimitGrid(BaseStrategy):
    strategy_id = 2
    strategy_name = "limit_grid"

    def __init__(self) -> None:
        # symbol → timestamp последнего события "pressure"
        self._recent_pressure: dict[str, float] = {}

    # ── Вход ──────────────────────────────────────────────────────────

    async def on_event(self, event: dict) -> None:
        event_type = event.get("event_type")

        if event_type == "pressure":
            self._recent_pressure[event["symbol"]] = time.time()
            return

        if event_type == "proximity":
            await self._try_open(event)
            return

        if event_type == "breakout":
            await self._handle_breakout(event)
            return

    async def _try_open(self, event: dict) -> None:
        symbol = event["symbol"]
        strength = event.get("strength", 0)
        p_bounce = event.get("p_bounce", 0.0)
        approach_style = event.get("approach_style", "unknown")

        if strength < S2_MIN_STRENGTH:
            return
        if p_bounce < S2_MIN_P_BOUNCE:
            return
        if approach_style == "bleed":
            return

        # Нет давления за последние N секунд
        last_pressure = self._recent_pressure.get(symbol, 0.0)
        if time.time() - last_pressure < S2_PRESSURE_COOLDOWN_SECONDS:
            return

        if not await self._can_open_trade(symbol):
            return

        level = event["level"]
        atr = event.get("atr", 0.0)
        expected_depth = event.get("expected_depth", 0.0)

        expected_depth_abs = level * (expected_depth / 100)
        if expected_depth < 0.1:
            expected_depth_abs = atr if atr > 0 else level * 0.005

        step = expected_depth_abs * 1.2 / (S2_GRID_ORDERS - 1)
        grid_anchor = level * 1.0015  # первый ордер на 0.15% выше уровня (front-run)
        grid_prices = [grid_anchor - step * i for i in range(S2_GRID_ORDERS)]
        order_size = round(self.POSITION_SIZE_USDT / S2_GRID_ORDERS, 4)

        grid_orders = [
            {
                "index": i + 1,
                "price": round(p, 8),
                "size": order_size,
                "filled": False,
                "fill_time": None,
                "cancelled": False,
            }
            for i, p in enumerate(grid_prices)
        ]

        bottom_price = grid_prices[-1]
        stop_loss = bottom_price - atr * 0.5

        # TP рассчитывается от entry_price (пока = level, уточнится при fill)
        # Сохраняем как функцию от grid bottom и entry_price — пересчитаем при каждом fill
        entry_price_initial = level  # до первого fill = верхний ордер

        trade_id = str(uuid.uuid4())
        trade = {
            "trade_id": trade_id,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "symbol": symbol,
            "level": level,
            "level_type": event.get("level_type", ""),
            "level_side": event.get("level_side", "support"),
            "entry_signal": "proximity",
            "strength_at_entry": strength,
            "p_bounce_at_entry": p_bounce,
            "expected_depth_at_entry": expected_depth,
            "approach_style": approach_style,
            "vol_ratio_at_entry": event.get("vol_ratio", 1.0),
            "atr_at_entry": atr,
            "entry_price": entry_price_initial,
            "entry_time": time.time(),
            "position_size": self.POSITION_SIZE_USDT,
            "direction": "long",
            "grid_orders_json": json.dumps(grid_orders),
            "grid_fill_count": 0,
        }

        await open_trade(trade)

        # Параметры сетки — первое событие
        tp1 = entry_price_initial + (entry_price_initial - bottom_price) * 1.0
        tp2 = entry_price_initial + (entry_price_initial - bottom_price) * 2.0
        params_note = json.dumps({
            "stop_loss": round(stop_loss, 8),
            "take_profit_1": round(tp1, 8),
            "take_profit_2": round(tp2, 8),
            "grid_bottom": round(bottom_price, 8),
            "atr": atr,
            "tp1_hit": False,
            "stop_moved_to_breakeven": False,
        })
        await add_trade_event(trade_id, "params_set", entry_price_initial, params_note)

        await self._send_open_message(trade, grid_orders, stop_loss, tp1, tp2)

        logger.info(
            "S2 grid opened",
            trade_id=trade_id,
            symbol=symbol,
            level=level,
            grid_count=S2_GRID_ORDERS,
            sl=round(stop_loss, 8),
        )

    # ── Сопровождение ─────────────────────────────────────────────────

    async def _check_exit(self, trade: dict, current_price: float) -> None:
        trade_id = trade["trade_id"]

        # Таймаут без единого fill
        if trade["grid_fill_count"] == 0:
            if time.time() - trade["entry_time"] > 3600:
                await close_trade(trade_id, trade["entry_price"], "timeout_no_fill")
                await self._send_close_message(trade, trade["entry_price"], "timeout_no_fill")
            return

        # Проверить заполнение ордеров
        await self._process_grid_fills(trade, current_price)

        # Перечитать trade из БД после возможного обновления
        updated = await self._reload_trade(trade_id)
        if updated is None or updated["status"] != "open":
            return

        params = self._extract_params(updated)
        if params is None:
            return

        stop_loss = params["stop_loss"]
        take_profit_1 = params["take_profit_1"]
        take_profit_2 = params["take_profit_2"]
        tp1_hit = params.get("tp1_hit", False)
        stop_moved = params.get("stop_moved_to_breakeven", False)
        entry_price = updated["entry_price"]

        effective_stop = entry_price if stop_moved else stop_loss

        # TP2
        if current_price >= take_profit_2:
            avg_exit = (take_profit_1 + take_profit_2) / 2 if tp1_hit else take_profit_2
            await close_trade(trade_id, avg_exit, "take_profit_2")
            await self._send_close_message(updated, avg_exit, "take_profit_2")
            return

        # TP1
        if not tp1_hit and current_price >= take_profit_1:
            params["tp1_hit"] = True
            params["stop_moved_to_breakeven"] = True
            await add_trade_event(
                trade_id, "tp1_hit", current_price,
                json.dumps({"partial_exit_price": current_price, "partial_exit_pct": 50})
            )
            await add_trade_event(trade_id, "params_updated", current_price, json.dumps(params))
            logger.info("S2 TP1 hit", trade_id=trade_id)
            return

        # Stop
        if current_price <= effective_stop:
            if tp1_hit:
                avg_exit = (take_profit_1 + entry_price) / 2
                await close_trade(trade_id, avg_exit, "stop_loss")
                await self._send_close_message(updated, avg_exit, "stop_loss")
            else:
                await close_trade(trade_id, current_price, "stop_loss")
                await self._send_close_message(updated, current_price, "stop_loss")

    async def _process_grid_fills(self, trade: dict, current_price: float) -> None:
        """Исполнить ордера сетки, до цены которых дошёл рынок.

        Проверяем не current_price, а low последних 2 свечей 1М — это решает
        проблему sweep: быстрое движение вниз с возвратом укладывается в 1–3 сек
        и не попадает в поллинг event bus (~5 сек), но всегда отражается в low свечи.
        fill_price = order["price"] — лимитный ордер исполняется по своей цене.
        """
        trade_id = trade["trade_id"]
        symbol = trade["symbol"]
        try:
            grid_orders = json.loads(trade["grid_orders_json"] or "[]")
        except Exception:
            return

        # Low последних 2 закрытых свечей 1М как прокси реального минимума цены.
        # default=current_price — фолбэк на старое поведение если свечей нет.
        _c1m = candles_1m.get(symbol, [])
        _last_low = min((c["low"] for c in _c1m[-2:]), default=current_price)

        changed = False
        for order in grid_orders:
            if order["filled"] or order.get("cancelled"):
                continue
            if _last_low <= order["price"]:
                order["filled"] = True
                order["fill_time"] = time.time()
                changed = True

                await add_trade_event(
                    trade_id, "order_filled", order["price"],
                    json.dumps({"order_index": order["index"], "price": order["price"]})
                )

                fill_count = sum(1 for o in grid_orders if o["filled"])
                weighted_entry = (
                    sum(o["price"] for o in grid_orders if o["filled"]) / fill_count
                )

                # Обновить grid в БД
                await self._update_grid_in_db(
                    trade_id, grid_orders, fill_count, weighted_entry, trade
                )

                await send_message(
                    f"🔵 [S2 Grid] {trade['symbol']} — ордер #{order['index']} исполнен на {order['price']}\n"
                    f"   Заполнено {fill_count}/{S2_GRID_ORDERS} | Ср. цена входа: {round(weighted_entry, 8)}"
                )

        if changed:
            # Пересчитать TP/SL от нового entry_price
            fill_count = sum(1 for o in grid_orders if o["filled"])
            weighted_entry = sum(o["price"] for o in grid_orders if o["filled"]) / fill_count
            await self._recalculate_params(trade_id, weighted_entry, trade)

    async def _update_grid_in_db(
        self,
        trade_id: str,
        grid_orders: list,
        fill_count: int,
        weighted_entry: float,
        trade: dict,
    ) -> None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE trades
                   SET grid_orders_json = ?, grid_fill_count = ?, entry_price = ?, updated_at = ?
                   WHERE trade_id = ?""",
                (
                    json.dumps(grid_orders),
                    fill_count,
                    round(weighted_entry, 8),
                    time.time(),
                    trade_id,
                ),
            )
            await db.commit()

    async def _recalculate_params(
        self, trade_id: str, weighted_entry: float, trade: dict
    ) -> None:
        """Пересчитать TP/SL после изменения средневзвешенного entry_price."""
        params = self._extract_params(trade)
        if params is None:
            return
        grid_bottom = params.get("grid_bottom", weighted_entry)
        atr = params.get("atr", trade.get("atr_at_entry", 0.0))
        stop_loss = grid_bottom - atr * 0.5
        tp1 = weighted_entry + (weighted_entry - grid_bottom) * 1.0
        tp2 = weighted_entry + (weighted_entry - grid_bottom) * 2.0
        params.update({
            "stop_loss": round(stop_loss, 8),
            "take_profit_1": round(tp1, 8),
            "take_profit_2": round(tp2, 8),
        })
        await add_trade_event(trade_id, "params_updated", weighted_entry, json.dumps(params))

    async def _handle_breakout(self, event: dict) -> None:
        trades = await get_open_trades(self.strategy_id)
        for trade in trades:
            if trade["symbol"] != event["symbol"]:
                continue
            if abs(trade["level"] - event["level"]) / max(trade["level"], 1) > 0.005:
                continue

            trade_id = trade["trade_id"]
            current_price = event["current_price"]
            fill_count = trade.get("grid_fill_count") or 0

            # Отменить неисполненные ордера
            try:
                grid_orders = json.loads(trade["grid_orders_json"] or "[]")
                for o in grid_orders:
                    if not o["filled"]:
                        o["cancelled"] = True
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE trades SET grid_orders_json = ?, updated_at = ? WHERE trade_id = ?",
                        (json.dumps(grid_orders), time.time(), trade_id),
                    )
                    await db.commit()
            except Exception as e:
                logger.error("S2 cancel grid orders failed", error=str(e))

            if fill_count == 0:
                # Позиции не было — ни один ордер сетки не исполнен.
                # Не вызываем close_trade: она считает pnl от entry_price=level,
                # хотя реальной позиции не существовало — это ложный убыток.
                # Закрываем запись напрямую с нулевым PnL.
                async with aiosqlite.connect(DB_PATH) as _db:
                    await _db.execute(
                        """UPDATE trades
                           SET exit_price = ?, exit_time = ?, exit_reason = ?,
                               pnl_pct = 0.0, pnl_usdt = 0.0, duration_minutes = ?,
                               status = 'closed', updated_at = ?
                           WHERE trade_id = ?""",
                        (
                            trade["entry_price"],
                            time.time(),
                            "cancelled_no_fill",
                            round((time.time() - trade["entry_time"]) / 60, 2),
                            time.time(),
                            trade_id,
                        ),
                    )
                    await _db.commit()
                await self._send_close_message(trade, trade["entry_price"], "cancelled_no_fill")
                logger.info("S2 grid cancelled (no fills), pnl=0", trade_id=trade_id)
            else:
                await close_trade(trade_id, current_price, "breakout_confirmed")
                await self._send_close_message(trade, current_price, "breakout_confirmed")

            logger.info("S2 grid closed on breakout", trade_id=trade_id, fill_count=fill_count)

    # ── Вспомогательные ───────────────────────────────────────────────

    def _extract_params(self, trade: dict) -> dict | None:
        try:
            events = json.loads(trade.get("events_json") or "[]")
        except Exception:
            return None
        for ev in reversed(events):
            if ev["type"] in ("params_updated", "params_set"):
                try:
                    return json.loads(ev["note"])
                except Exception:
                    return None
        return None

    async def _reload_trade(self, trade_id: str) -> dict | None:
        trades = await get_open_trades(self.strategy_id)
        for t in trades:
            if t["trade_id"] == trade_id:
                return t
        return None

    # ── Telegram ──────────────────────────────────────────────────────

    async def _send_open_message(
        self,
        trade: dict,
        grid_orders: list,
        stop_loss: float,
        tp1: float,
        tp2: float,
    ) -> None:
        order_size = round(self.POSITION_SIZE_USDT / S2_GRID_ORDERS, 2)
        prices_str = "  ".join(f"#{o['index']}: {o['price']}" for o in grid_orders)
        text = (
            f"🔵 [S2 Grid] {trade['symbol']} LONG — сетка выставлена\n"
            f"   Уровень: {trade['level']} ({trade['level_type']}, strength={trade['strength_at_entry']})"
            f" | p_bounce={trade['p_bounce_at_entry']:.2f}\n"
            f"   Ордера ({S2_GRID_ORDERS}×{order_size} USDT):\n"
            f"     {prices_str}\n"
            f"   SL: {round(stop_loss, 8)} | TP1: {round(tp1, 8)} | TP2: {round(tp2, 8)}"
        )
        try:
            await send_message(text)
        except Exception as e:
            logger.error("S2 send_open_message failed", error=str(e))

    async def _send_close_message(self, trade: dict, exit_price: float, reason: str) -> None:
        ep = trade["entry_price"]
        fill_count = trade.get("grid_fill_count") or 0
        pnl_pct = (exit_price - ep) / ep * 100 if ep > 0 else 0.0
        filled_size = self.POSITION_SIZE_USDT * fill_count / S2_GRID_ORDERS
        pnl_usdt = filled_size * pnl_pct / 100
        icon = "✅" if pnl_pct >= 0 else "🔴"
        max_fav  = trade.get("max_favorable_pct") or 0.0
        max_adv  = trade.get("max_adverse_pct") or 0.0
        max_profit_usdt = filled_size * max_fav / 100
        max_loss_usdt   = filled_size * max_adv / 100
        text = (
            f"{icon} [S2 Grid] {trade['symbol']} закрыт\n"
            f"   Заполнено ордеров: {fill_count}/{S2_GRID_ORDERS}"
            f" | Ср. вход: {ep} → Выход: {exit_price}\n"
            f"   Причина: {reason}\n"
            f"   PnL: {self._format_pct(pnl_pct)} ({self._format_pct(pnl_usdt, sign=True)} USDT)"
            f" | Время: {self._format_duration(trade['entry_time'])}\n"
            f"   📈 Max profit: +{max_fav:.2f}% (+{max_profit_usdt:.2f} USDT)\n"
            f"   📉 Max drawdown: -{max_adv:.2f}% (-{max_loss_usdt:.2f} USDT)"
        )
        try:
            await send_message(text)
        except Exception as e:
            logger.error("S2 send_close_message failed", error=str(e))
