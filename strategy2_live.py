"""Strategy 2 Live — зеркало S2 на Bybit Demo.

Слушает те же события что и Strategy2LimitGrid.
Открывает 10 лимитных ордеров на Bybit Demo, ставит стоп-лимит SL,
закрывает позицию рыночным ордером при тех же условиях (TP1/TP2/trailing/breakout/timeout).

Включение/отключение — через флаг S2_LIVE_ENABLED (устанавливается из Telegram).
"""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Optional

from trading.bybit_client import (
    cancel_all_orders,
    cancel_order,
    get_instrument_info,
    get_position,
    place_limit_order,
    place_market_order,
    place_stop_limit_order,
    set_leverage,
)
from trading.live_trade_log import (
    add_live_event,
    close_live_trade,
    get_open_live_trades,
    init_live_trades_db,
    log_live_error,
    open_live_trade,
    update_live_trade,
)
from constants import (
    S2_GRID_ORDERS,
    S2_MIN_P_BOUNCE,
    S2_MIN_STRENGTH,
    S2_POSITION_SIZE_USDT,
    S2_PRESSURE_COOLDOWN_SECONDS,
)
from data.collector import candles_1m
from bot.telegram import send_message
from logger import logger

# ── Trailing / TP параметры (идентичны S2 paper) ─────────────────────────────
S2_TRAILING_PCT = 0.005
S2_TP2_ATR_MULT = 5.0
S2_FULL_GRID_TP_PCT = 0.0015
LEVERAGE = 20

# ── Глобальный флаг включения ─────────────────────────────────────────────────
S2_LIVE_ENABLED: bool = False


def set_live_enabled(enabled: bool) -> None:
    global S2_LIVE_ENABLED
    S2_LIVE_ENABLED = enabled
    logger.info("S2 live trading", enabled=enabled)


def is_live_enabled() -> bool:
    return S2_LIVE_ENABLED


class Strategy2Live:
    """Live-зеркало Strategy2LimitGrid на Bybit Demo."""

    def __init__(self) -> None:
        self._recent_pressure: dict[str, float] = {}
        self._recent_close: dict[str, float] = {}
        # Кэш инструментов: symbol → {"qty_step": float, "min_qty": float, "price_scale": int}
        self._instrument_cache: dict[str, dict] = {}

    # ── Инициализация ─────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        await init_live_trades_db()
        logger.info("Strategy2Live initialized")

    # ── Главный вход ──────────────────────────────────────────────────────────

    async def on_event(self, event: dict) -> None:
        if not S2_LIVE_ENABLED:
            return

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

    # ── Открытие позиции ──────────────────────────────────────────────────────

    async def _try_open(self, event: dict) -> None:
        symbol = event["symbol"]
        strength = event.get("strength", 0)
        p_bounce = event.get("p_bounce", 0.0)
        approach_style = event.get("approach_style", "unknown")

        # Те же фильтры что у S2 paper
        if strength < S2_MIN_STRENGTH:
            return
        if p_bounce < S2_MIN_P_BOUNCE:
            return
        if approach_style == "bleed":
            return
        if time.time() - self._recent_pressure.get(symbol, 0.0) < S2_PRESSURE_COOLDOWN_SECONDS:
            return

        # Проверить, нет ли уже открытой live-сделки по символу
        open_trades = await get_open_live_trades()
        open_symbols = {t["symbol"] for t in open_trades}
        if symbol in open_symbols:
            return
        if len(open_trades) >= 3:  # MAX_OPEN_TRADES
            return

        level = event["level"]
        close_key = f"{symbol}:{level}"
        if time.time() - self._recent_close.get(close_key, 0.0) < 300:
            return

        atr = event.get("atr", 0.0)

        grid_width = atr * 2.5
        step = grid_width / (S2_GRID_ORDERS - 1)
        grid_anchor = level * 1.0015

        c1m = candles_1m.get(symbol, [])
        current_price = c1m[-1]["close"] if c1m else grid_anchor

        if current_price > grid_anchor * 1.005:
            return
        grid_prices = [grid_anchor - step * i for i in range(S2_GRID_ORDERS)]
        grid_bottom = grid_prices[-1]
        if current_price < grid_bottom:
            return

        order_size_usdt = S2_POSITION_SIZE_USDT / S2_GRID_ORDERS
        stop_loss = grid_bottom - atr * 0.5
        tp1 = level + (level - grid_bottom) * 1.0
        tp2 = level + atr * S2_TP2_ATR_MULT

        trade_id = str(uuid.uuid4())
        paper_trade_id = event.get("paper_trade_id")  # опционально, если передаётся

        # Получить параметры инструмента (шаг qty, точность цены)
        try:
            instrument = await self._get_instrument(symbol)
        except Exception as e:
            logger.error(
                "S2Live: failed to get instrument info",
                symbol=symbol,
                error=str(e),
            )
            await send_message(
                f"⚠️ [S2 Live] {symbol} — не удалось получить параметры инструмента: {e}"
            )
            return

        # Установить плечо
        try:
            await set_leverage(symbol, LEVERAGE)
        except Exception as e:
            logger.error(
                "S2Live: failed to set leverage",
                symbol=symbol,
                leverage=LEVERAGE,
                error=str(e),
            )
            await send_message(
                f"⚠️ [S2 Live] {symbol} — не удалось выставить плечо x{LEVERAGE}: {e}"
            )
            return

        # Разместить 10 лимитных ордеров
        bybit_order_ids: list[str] = []
        grid_orders_placed: list[dict] = []
        failed_orders: list[int] = []  # индексы ордеров с ошибкой

        for i, price in enumerate(grid_prices):
            qty = self._calc_qty(order_size_usdt, price, instrument)
            if qty <= 0:
                err_msg = (
                    f"order_size_usdt={order_size_usdt:.2f} / price={price_rounded} → qty=0 "
                    f"(min_qty={instrument['min_qty']})"
                )
                logger.error(
                    "S2Live: qty=0 for grid order, skipping",
                    symbol=symbol,
                    price=price,
                    order_size_usdt=order_size_usdt,
                )
                await send_message(
                    f"⚠️ [S2 Live] {symbol} — ордер #{i+1} пропущен: qty=0\n"
                    f"   {err_msg}"
                )
                failed_orders.append(i)
                continue

            price_rounded = self._round_price(price, instrument)
            order_link_id = f"s2live_{trade_id[:8]}_{i}"

            try:
                result = await place_limit_order(
                    symbol=symbol,
                    side="Buy",
                    qty=qty,
                    price=price_rounded,
                    order_link_id=order_link_id,
                )
                bybit_order_ids.append(result["orderId"])
                grid_orders_placed.append({
                    "index": i + 1,
                    "price": price_rounded,
                    "qty": qty,
                    "order_id": result["orderId"],
                    "filled": False,
                    "fill_time": None,
                    "cancelled": False,
                })
                logger.info(
                    "S2Live: limit order placed",
                    symbol=symbol,
                    index=i + 1,
                    price=price_rounded,
                    qty=qty,
                    order_id=result["orderId"],
                )
            except Exception as e:
                logger.error(
                    "S2Live: failed to place limit order",
                    symbol=symbol,
                    index=i + 1,
                    price=price_rounded,
                    qty=qty,
                    error=str(e),
                )
                failed_orders.append(i)

        if not grid_orders_placed:
            await send_message(
                f"❌ [S2 Live] {symbol} — все {S2_GRID_ORDERS} лимитных ордеров не прошли, сетка не открыта"
            )
            logger.error(
                "S2Live: all limit orders failed, aborting",
                symbol=symbol,
                trade_id=trade_id,
            )
            return

        if failed_orders:
            logger.warning(
                "S2Live: some grid orders failed",
                symbol=symbol,
                failed_count=len(failed_orders),
                failed_indices=failed_orders,
            )
            await send_message(
                f"⚠️ [S2 Live] {symbol} — {len(failed_orders)}/{S2_GRID_ORDERS} ордеров не прошли\n"
                f"   Индексы: {failed_orders}\n"
                f"   Сетка открыта частично ({len(grid_orders_placed)} ордеров)"
            )

        # Разместить стоп-лимит SL
        # Считаем общий qty по размещённым ордерам
        total_qty = self._snap_qty(
            sum(o["qty"] for o in grid_orders_placed), instrument
        )
        sl_price_rounded = self._round_price(stop_loss, instrument)
        # Стоп-лимит: trigger = stop_loss, order price = stop_loss - 0.1% (слип)
        sl_order_price = self._round_price(stop_loss * 0.999, instrument)
        bybit_sl_order_id: Optional[str] = None

        try:
            sl_result = await place_stop_limit_order(
                symbol=symbol,
                side="Sell",
                qty=total_qty,
                trigger_price=sl_price_rounded,
                order_price=sl_order_price,
                order_link_id=f"s2live_sl_{trade_id[:8]}",
            )
            bybit_sl_order_id = sl_result["orderId"]
            logger.info(
                "S2Live: stop-limit SL placed",
                symbol=symbol,
                trigger=sl_price_rounded,
                order_price=sl_order_price,
                qty=total_qty,
                order_id=bybit_sl_order_id,
            )
        except Exception as e:
            logger.error(
                "S2Live: failed to place SL order",
                symbol=symbol,
                stop_loss=sl_price_rounded,
                qty=total_qty,
                error=str(e),
            )
            await send_message(
                f"⚠️ [S2 Live] {symbol} — ордера размещены, но SL НЕ ВЫСТАВЛЕН: {e}\n"
                f"   Закрой позицию вручную! SL должен быть: {sl_price_rounded}"
            )

        # Записать в live_trades.db
        await open_live_trade({
            "trade_id": trade_id,
            "paper_trade_id": paper_trade_id,
            "symbol": symbol,
            "level": level,
            "level_type": event.get("level_type", ""),
            "entry_price": level,
            "entry_time": time.time(),
            "position_size_usdt": S2_POSITION_SIZE_USDT,
            "direction": "long",
            "grid_orders": grid_orders_placed,
            "bybit_order_ids": bybit_order_ids,
            "bybit_sl_order_id": bybit_sl_order_id,
            "stop_loss": stop_loss,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
        })

        # Сохранить params в events
        await add_live_event(trade_id, "params_set", json.dumps({
            "stop_loss": round(stop_loss, 8),
            "take_profit_1": round(tp1, 8),
            "take_profit_2": round(tp2, 8),
            "grid_bottom": round(grid_bottom, 8),
            "atr": atr,
            "tp1_hit": False,
            "trailing_active": False,
            "trailing_peak": None,
            "trailing_stop": None,
            "full_grid_tp": None,
        }))

        prices_str = "  ".join(f"#{o['index']}: {o['price']}" for o in grid_orders_placed)
        failed_str = f"\n   ⚠️ Не размещено: {len(failed_orders)} ордеров" if failed_orders else ""
        sl_str = f"✅ {sl_price_rounded}" if bybit_sl_order_id else "❌ НЕ ВЫСТАВЛЕН"
        await send_message(
            f"🟢 [S2 Live] {symbol} — сетка на Bybit Demo\n"
            f"   Ордера ({len(grid_orders_placed)}/{S2_GRID_ORDERS}):\n"
            f"     {prices_str}{failed_str}\n"
            f"   SL: {sl_str} | TP1: {round(tp1, 8)} | TP2: {round(tp2, 8)}\n"
            f"   Плечо: x{LEVERAGE} | Размер: {S2_POSITION_SIZE_USDT} USDT"
        )

        logger.info(
            "S2Live grid opened",
            trade_id=trade_id,
            symbol=symbol,
            level=level,
            orders_placed=len(grid_orders_placed),
            sl=round(stop_loss, 8),
        )

    # ── Проверка выходов (вызывается из price loop стратегий) ─────────────────

    async def check_exits(self, symbol: str, current_price: float) -> None:
        """Проверить TP/SL/trailing для всех открытых live-сделок по символу."""
        if not S2_LIVE_ENABLED:
            return

        trades = await get_open_live_trades()
        for trade in trades:
            if trade["symbol"] != symbol:
                continue
            try:
                await self._check_exit(trade, current_price)
            except Exception as e:
                logger.error(
                    "S2Live: error in check_exits",
                    trade_id=trade["trade_id"],
                    symbol=symbol,
                    error=str(e),
                )
                await log_live_error(trade["trade_id"], "check_exits", str(e))

    async def _check_exit(self, trade: dict, current_price: float) -> None:
        trade_id = trade["trade_id"]
        symbol = trade["symbol"]

        # Проверить заполнение grid ордеров по биржевым данным
        await self._sync_grid_fills(trade, current_price)

        # Перечитать после возможного обновления fills
        trade = await self._reload_trade(trade_id)
        if trade is None or trade["status"] != "open":
            return

        fill_count = trade.get("grid_fill_count") or 0

        # 20 минут без единого fill — отменяем и закрываем
        if fill_count == 0:
            if time.time() - trade["entry_time"] > 1200:
                await self._cancel_and_close_no_fill(trade)
            return

        params = self._extract_params(trade)
        if not params:
            return

        stop_loss = params["stop_loss"]
        tp1 = params["take_profit_1"]
        tp2 = params["take_profit_2"]
        tp1_hit = params.get("tp1_hit", False)
        trailing_active = params.get("trailing_active", False)
        trailing_peak = params.get("trailing_peak")
        trailing_stop = params.get("trailing_stop")
        full_grid_tp = params.get("full_grid_tp")

        c1m = candles_1m.get(symbol, [])
        last_high = max((c["high"] for c in c1m[-2:]), default=current_price)
        last_low = min((c["low"] for c in c1m[-2:]), default=current_price)

        # full-grid TP
        if full_grid_tp is not None and not tp1_hit:
            if last_high >= full_grid_tp:
                await self._market_close(trade, full_grid_tp, "full_grid_tp")
                return
            if current_price <= stop_loss:
                await self._market_close(trade, current_price, "stop_loss")
            return

        # TP2 до TP1
        if not tp1_hit and last_high >= tp2:
            await self._market_close(trade, tp2, "take_profit_2")
            return

        # TP1 → активировать trailing
        if not tp1_hit and last_high >= tp1:
            peak = last_high
            t_stop = round(peak * (1.0 - S2_TRAILING_PCT), 8)
            params["tp1_hit"] = True
            params["trailing_active"] = True
            params["trailing_peak"] = peak
            params["trailing_stop"] = t_stop

            await add_live_event(trade_id, "tp1_hit", json.dumps({
                "price": tp1,
                "trailing_peak": peak,
                "trailing_stop": t_stop,
            }))
            # Сохранить обновлённые params
            await add_live_event(trade_id, "params_updated", json.dumps(params))
            logger.info("S2Live TP1 hit, trailing activated", trade_id=trade_id, peak=peak)
            return

        # Trailing
        if trailing_active and trailing_peak is not None:
            if last_high > trailing_peak:
                trailing_peak = last_high
                trailing_stop = round(trailing_peak * (1.0 - S2_TRAILING_PCT), 8)
                params["trailing_peak"] = trailing_peak
                params["trailing_stop"] = trailing_stop
                await add_live_event(trade_id, "params_updated", json.dumps(params))

            if trailing_stop is not None and last_low <= trailing_stop:
                avg_exit = (tp1 + trailing_stop) / 2
                await self._market_close(trade, avg_exit, "trailing_stop")
                return

        # Обычный SL до TP1
        if not tp1_hit and current_price <= stop_loss:
            await self._market_close(trade, current_price, "stop_loss")

    # ── Breakout — отменить сетку и закрыть позицию ───────────────────────────

    async def _handle_breakout(self, event: dict) -> None:
        if not S2_LIVE_ENABLED:
            return

        trades = await get_open_live_trades()
        for trade in trades:
            if trade["symbol"] != event["symbol"]:
                continue
            if abs(trade["level"] - event["level"]) / max(trade["level"], 1) > 0.005:
                continue

            symbol = trade["symbol"]
            fill_count = trade.get("grid_fill_count") or 0
            current_price = event["current_price"]

            # Отменить все открытые ордера
            try:
                await cancel_all_orders(symbol)
                logger.info("S2Live: cancelled all orders on breakout", symbol=symbol)
            except Exception as e:
                logger.error(
                    "S2Live: cancel_all_orders failed on breakout",
                    symbol=symbol,
                    error=str(e),
                )
                await log_live_error(trade["trade_id"], "handle_breakout/cancel_all", str(e))

            if fill_count == 0:
                await self._close_no_fill(trade, "cancelled_no_fill")
            else:
                params = self._extract_params(trade)
                tp1_hit = params.get("tp1_hit", False) if params else False
                trailing_stop = params.get("trailing_stop") if params else None
                tp1 = params.get("take_profit_1", trade["entry_price"]) if params else trade["entry_price"]

                if tp1_hit and trailing_stop is not None:
                    avg_exit = (tp1 + max(current_price, trailing_stop)) / 2
                    exit_reason = "breakout_after_tp1"
                else:
                    avg_exit = current_price
                    exit_reason = "breakout_confirmed"

                await self._market_close(trade, avg_exit, exit_reason)

    # ── Таймаут ───────────────────────────────────────────────────────────────

    async def check_timeout(self) -> None:
        """Вызывать из strategy_runner каждые 60 сек."""
        if not S2_LIVE_ENABLED:
            return

        trades = await get_open_live_trades()
        now = time.time()
        for trade in trades:
            age_minutes = (now - trade["entry_time"]) / 60
            if age_minutes < 60:
                continue

            symbol = trade["symbol"]
            fill_count = trade.get("grid_fill_count") or 0

            try:
                await cancel_all_orders(symbol)
            except Exception as e:
                logger.error(
                    "S2Live: cancel_all_orders failed on timeout",
                    trade_id=trade["trade_id"],
                    error=str(e),
                )
                await log_live_error(trade["trade_id"], "check_timeout/cancel_all", str(e))

            if fill_count == 0:
                # cancel_all_orders уже вызван выше — передаём в _close_no_fill напрямую
                await self._close_no_fill(trade, "timeout_no_fill")
            else:
                c1m = candles_1m.get(symbol, [])
                current_price = c1m[-1]["close"] if c1m else trade["entry_price"]
                await self._market_close(trade, current_price, "timeout")

    # ── Синхронизация fills с биржи ───────────────────────────────────────────

    async def _sync_grid_fills(self, trade: dict, current_price: float) -> None:
        """
        Определить заполненные ордера по low свечей (как в paper S2).
        При заполнении нового ордера — обновить entry_price, пересчитать params.
        """
        trade_id = trade["trade_id"]
        symbol = trade["symbol"]

        try:
            grid_orders = json.loads(trade.get("grid_orders_json") or "[]")
        except Exception:
            return

        c1m = candles_1m.get(symbol, [])
        last_low = min((c["low"] for c in c1m[-2:]), default=current_price)

        changed = False
        for order in grid_orders:
            if order.get("filled") or order.get("cancelled"):
                continue
            if last_low <= order["price"]:
                order["filled"] = True
                order["fill_time"] = time.time()
                changed = True
                logger.info(
                    "S2Live: grid order fill detected",
                    trade_id=trade_id,
                    symbol=symbol,
                    index=order["index"],
                    price=order["price"],
                )

        if not changed:
            return

        fill_count = sum(1 for o in grid_orders if o["filled"])
        weighted_entry = sum(o["price"] for o in grid_orders if o["filled"]) / fill_count

        await update_live_trade(
            trade_id,
            grid_orders_json=json.dumps(grid_orders),
            grid_fill_count=fill_count,
            entry_price=round(weighted_entry, 8),
        )
        await add_live_event(trade_id, "grid_fill", json.dumps({
            "fill_count": fill_count,
            "weighted_entry": round(weighted_entry, 8),
        }))

        # Пересчитать params (SL/TP)
        await self._recalculate_params(trade_id, weighted_entry, fill_count, trade)

    async def _recalculate_params(
        self,
        trade_id: str,
        weighted_entry: float,
        fill_count: int,
        trade: dict,
    ) -> None:
        params = self._extract_params(trade)
        if params is None:
            return

        grid_bottom = params.get("grid_bottom", weighted_entry)
        atr = params.get("atr", trade.get("atr_at_entry", 0.0))
        level = trade.get("level", weighted_entry)

        stop_loss = grid_bottom - atr * 0.5
        if fill_count >= S2_GRID_ORDERS:
            stop_loss = weighted_entry - atr * 0.2

        tp1 = weighted_entry + (weighted_entry - grid_bottom) * 1.0
        tp2 = weighted_entry + atr * S2_TP2_ATR_MULT

        full_grid_tp = None
        if fill_count >= S2_GRID_ORDERS:
            candidate = round(level * (1.0 - S2_FULL_GRID_TP_PCT), 8)
            full_grid_tp = candidate if candidate > weighted_entry else round(weighted_entry * 1.001, 8)

            # Обновить SL ордер на бирже при заполнении всей сетки
            symbol = trade["symbol"]
            try:
                # Отменить старый SL
                old_sl_id = trade.get("bybit_sl_order_id")
                if old_sl_id:
                    await cancel_order(symbol, old_sl_id)

                instrument = await self._get_instrument(symbol)
                # Перечитываем grid_orders из БД — trade может быть устаревшим снимком
                _fresh_trade = await self._reload_trade(trade_id)
                _filled_orders = json.loads(
                    (_fresh_trade or trade).get("grid_orders_json") or "[]"
                )
                total_qty = self._snap_qty(
                    sum(o["qty"] for o in _filled_orders if o.get("filled")),
                    instrument,
                )
                sl_price_rounded = self._round_price(stop_loss, instrument)
                sl_order_price = self._round_price(stop_loss * 0.999, instrument)
                sl_result = await place_stop_limit_order(
                    symbol=symbol,
                    side="Sell",
                    qty=total_qty,
                    trigger_price=sl_price_rounded,
                    order_price=sl_order_price,
                    order_link_id=f"s2live_sl2_{trade_id[:8]}",
                )
                await update_live_trade(trade_id, bybit_sl_order_id=sl_result["orderId"])
                logger.info(
                    "S2Live: SL updated after full grid",
                    trade_id=trade_id,
                    symbol=symbol,
                    new_sl=sl_price_rounded,
                )
            except Exception as e:
                logger.error(
                    "S2Live: failed to update SL after full grid",
                    trade_id=trade_id,
                    error=str(e),
                )
                await log_live_error(trade_id, "recalculate_params/update_sl", str(e))
                await send_message(
                    f"🚨 [S2 Live] {symbol} — не удалось обновить SL после заполнения всей сетки!\n"
                    f"   SL должен быть: {self._round_price(stop_loss, instrument)}\n"
                    f"   Ошибка: {e}\n"
                    f"   ВЫСТАВИ SL ВРУЧНУЮ!"
                )

        params.update({
            "stop_loss": round(stop_loss, 8),
            "take_profit_1": round(tp1, 8),
            "take_profit_2": round(tp2, 8),
            "full_grid_tp": full_grid_tp,
        })
        await add_live_event(trade_id, "params_updated", json.dumps(params))

    # ── Рыночное закрытие ─────────────────────────────────────────────────────

    async def _market_close(self, trade: dict, exit_price: float, reason: str) -> None:
        trade_id = trade["trade_id"]
        symbol = trade["symbol"]

        # Отменить все открытые ордера (незаполненные лимитные + SL)
        try:
            await cancel_all_orders(symbol)
        except Exception as e:
            logger.error(
                "S2Live: cancel_all_orders failed before market close",
                trade_id=trade_id,
                error=str(e),
            )
            await log_live_error(trade_id, f"market_close({reason})/cancel_all", str(e))
            await send_message(
                f"⚠️ [S2 Live] {symbol} — не удалось отменить ордера перед закрытием\n"
                f"   Причина закрытия: {reason} | Ошибка: {e}\n"
                f"   Проверь открытые ордера на бирже вручную!"
            )

        # Получить реальную позицию с биржи
        position = None
        try:
            position = await get_position(symbol)
        except Exception as e:
            logger.error(
                "S2Live: get_position failed before market close",
                trade_id=trade_id,
                error=str(e),
            )
            await log_live_error(trade_id, f"market_close({reason})/get_position", str(e))
            await send_message(
                f"🚨 [S2 Live] {symbol} — не удалось получить позицию с биржи!\n"
                f"   Причина закрытия: {reason} | Ошибка: {e}\n"
                f"   Закрытие в БД отменено — ПРОВЕРЬ ПОЗИЦИЮ ВРУЧНУЮ!"
            )
            return

        if position is None:
            # Позиции нет на бирже — закрываем только в БД
            logger.warning(
                "S2Live: no position found on exchange, closing in DB only",
                trade_id=trade_id,
                symbol=symbol,
            )
            await send_message(
                f"⚠️ [S2 Live] {symbol} — позиция не найдена на бирже\n"
                f"   Причина закрытия: {reason}\n"
                f"   Закрываем только в БД (возможно SL уже сработал на бирже)"
            )
            await self._close_no_fill(trade, reason)
            return

        qty = float(position.get("size", 0))
        if qty <= 0:
            logger.warning(
                "S2Live: position size=0 on exchange, closing in DB only",
                trade_id=trade_id,
                symbol=symbol,
            )
            await send_message(
                f"⚠️ [S2 Live] {symbol} — позиция на бирже size=0\n"
                f"   Причина закрытия: {reason}\n"
                f"   Закрываем только в БД (позиция уже закрыта на бирже)"
            )
            await self._close_no_fill(trade, reason)
            return

        try:
            await place_market_order(symbol=symbol, side="Sell", qty=qty, reduce_only=True)
            logger.info(
                "S2Live: market close executed",
                trade_id=trade_id,
                symbol=symbol,
                qty=qty,
                reason=reason,
            )
        except Exception as e:
            logger.error(
                "S2Live: place_market_order FAILED — position may be open on exchange!",
                trade_id=trade_id,
                symbol=symbol,
                qty=qty,
                reason=reason,
                error=str(e),
            )
            await log_live_error(trade_id, f"market_close({reason})/place_market_order", str(e))
            await send_message(
                f"🚨 [S2 Live] {symbol} — ОШИБКА рыночного закрытия!\n"
                f"   Причина закрытия: {reason}\n"
                f"   Ошибка: {e}\n"
                f"   Qty: {qty} — ЗАКРОЙ ПОЗИЦИЮ ВРУЧНУЮ!"
            )
            return

        # Рассчитать PnL
        entry_price = trade.get("entry_price") or trade.get("level") or exit_price
        fill_count = trade.get("grid_fill_count") or 0
        filled_usdt = S2_POSITION_SIZE_USDT * fill_count / S2_GRID_ORDERS if fill_count else 0
        pnl_usdt = filled_usdt * (exit_price - entry_price) / entry_price if entry_price > 0 else 0

        await close_live_trade(trade_id, exit_price, reason, pnl_usdt)
        self._recent_close[f"{symbol}:{trade['level']}"] = time.time()

        icon = "✅" if pnl_usdt >= 0 else "🔴"
        await send_message(
            f"{icon} [S2 Live] {symbol} закрыт\n"
            f"   Причина: {reason} | Выход: {exit_price}\n"
            f"   PnL: {pnl_usdt:+.2f} USDT | Fills: {fill_count}/{S2_GRID_ORDERS}"
        )

    async def _cancel_and_close_no_fill(self, trade: dict) -> None:
        """Отменить ордера и закрыть сделку без реальной позиции."""
        symbol = trade["symbol"]
        try:
            await cancel_all_orders(symbol)
        except Exception as e:
            logger.error(
                "S2Live: cancel_all_orders failed in no_fill close",
                trade_id=trade["trade_id"],
                error=str(e),
            )
            await log_live_error(trade["trade_id"], "cancel_and_close_no_fill/cancel_all", str(e))
        await self._close_no_fill(trade, "timeout_no_fill")

    async def _close_no_fill(self, trade: dict, reason: str) -> None:
        """Закрыть в БД без реального закрытия позиции (не было fills)."""
        await close_live_trade(trade["trade_id"], trade.get("entry_price", 0), reason, 0.0)
        await send_message(
            f"⚪ [S2 Live] {trade['symbol']} — {reason} (нет заполненных ордеров, PnL=0)"
        )

    # ── Инструмент ────────────────────────────────────────────────────────────

    async def _get_instrument(self, symbol: str) -> dict:
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        info = await get_instrument_info(symbol)
        if info is None:
            raise RuntimeError(f"instrument info not found for {symbol}")
        lot_filter = info.get("lotSizeFilter", {})
        price_filter = info.get("priceFilter", {})
        qty_step = float(lot_filter.get("qtyStep", "0.001"))
        min_qty = float(lot_filter.get("minOrderQty", "0.001"))
        tick_size = price_filter.get("tickSize", "0.01")
        # Точность цены из tickSize: "0.01" → 2 знака
        price_scale = len(tick_size.rstrip("0").split(".")[-1]) if "." in tick_size else 0
        parsed = {"qty_step": qty_step, "min_qty": min_qty, "price_scale": price_scale}
        self._instrument_cache[symbol] = parsed
        return parsed

    def _calc_qty(self, usdt: float, price: float, instrument: dict) -> float:
        if price <= 0:
            return 0.0
        raw = usdt / price
        qty = self._snap_qty(raw, instrument)
        return qty if qty >= instrument["min_qty"] else 0.0

    def _snap_qty(self, qty: float, instrument: dict) -> float:
        """Привести qty к ближайшему кратному qtyStep (floor) через Decimal для точности."""
        from decimal import Decimal, ROUND_DOWN
        step = instrument["qty_step"]
        step_d = Decimal(str(step))
        qty_d = Decimal(str(qty))
        snapped = (qty_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
        return float(snapped)

    def _round_price(self, price: float, instrument: dict) -> float:
        scale = instrument.get("price_scale", 2)
        return round(price, scale)

    # ── Вспомогательные ───────────────────────────────────────────────────────

    def _extract_params(self, trade: dict) -> Optional[dict]:
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

    async def _reload_trade(self, trade_id: str) -> Optional[dict]:
        trades = await get_open_live_trades()
        for t in trades:
            if t["trade_id"] == trade_id:
                return t
        return None
