"""Strategy 1: Bounce — вход по подтверждённому отбою (bounce / sweep)."""

from __future__ import annotations

import json
import time
import uuid

from trading.base_strategy import BaseStrategy
from trading.trade_log import open_trade, close_trade, add_trade_event, get_open_trades
from bot.telegram import send_message
from constants import S1_MIN_STRENGTH, S1_MIN_P_BOUNCE, S1_TP1_RR, S1_TP2_RR
from logger import logger


class Strategy1Bounce(BaseStrategy):
    strategy_id = 1
    strategy_name = "bounce"

    # ── Вход ──────────────────────────────────────────────────────────

    async def on_event(self, event: dict) -> None:
        event_type = event.get("event_type")

        # Реагируем на bounce и sweep как сигналы входа
        if event_type in ("bounce", "sweep"):
            await self._try_open(event)
            return

        # Breakout по открытой сделке — экстренный выход
        if event_type == "breakout":
            await self._handle_breakout(event)

    async def _try_open(self, event: dict) -> None:
        symbol = event["symbol"]
        strength = event.get("strength", 0)
        p_bounce = event.get("p_bounce", 0.0)
        approach_style = event.get("approach_style", "unknown")

        if strength < S1_MIN_STRENGTH:
            return
        if p_bounce < S1_MIN_P_BOUNCE:
            return
        if approach_style == "bleed":
            return
        if event.get("level_type") == "pump_base":
            return
        # Не входить при flash/impulse если объём ниже нормы — слабый сигнал.
        vol_ratio = event.get("vol_ratio", 1.0)
        if vol_ratio < 1.0 and approach_style in ("flash", "impulse"):
            logger.debug(
                "S1 skip: low vol_ratio on flash/impulse",
                symbol=symbol, vol_ratio=vol_ratio, style=approach_style,
            )
            return
        if not await self._can_open_trade(symbol):
            return

        entry_price = event["current_price"]
        atr = event.get("atr", 0.0)
        expected_depth = event.get("expected_depth", 0.0)

        # Если expected_depth слишком мал — использовать ATR как ориентир
        if expected_depth < 0.1:
            expected_depth = (atr / entry_price * 100) if entry_price > 0 else 0.5

        expected_depth_abs = entry_price * (expected_depth / 100)
        stop_loss = event["level"] - expected_depth_abs * 1.5
        risk = entry_price - stop_loss
        take_profit_1 = entry_price + risk * S1_TP1_RR
        take_profit_2 = entry_price + risk * S1_TP2_RR

        trade_id = str(uuid.uuid4())
        trade = {
            "trade_id": trade_id,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "symbol": symbol,
            "level": event["level"],
            "level_type": event.get("level_type", ""),
            "level_side": event.get("level_side", "support"),
            "entry_signal": event["event_type"],
            "strength_at_entry": strength,
            "p_bounce_at_entry": p_bounce,
            "expected_depth_at_entry": expected_depth,
            "approach_style": approach_style,
            "vol_ratio_at_entry": event.get("vol_ratio", 1.0),
            "atr_at_entry": atr,
            "entry_price": entry_price,
            "entry_time": time.time(),
            "position_size": self.POSITION_SIZE_USDT,
            "direction": "long",
            "grid_orders_json": None,
            "grid_fill_count": None,
            # Параметры выхода хранятся в extra-полях events_json
        }

        await open_trade(trade)

        # Сохранить параметры выхода как первое событие
        params_note = json.dumps({
            "stop_loss": round(stop_loss, 8),
            "take_profit_1": round(take_profit_1, 8),
            "take_profit_2": round(take_profit_2, 8),
            "tp1_hit": False,
            "stop_moved_to_breakeven": False,
        })
        await add_trade_event(trade_id, "params_set", entry_price, params_note)

        trade["entry_price"] = entry_price  # для сообщения
        await self._send_open_message(trade, stop_loss, take_profit_1, take_profit_2)

        logger.info(
            "S1 trade opened",
            trade_id=trade_id,
            symbol=symbol,
            entry=entry_price,
            sl=round(stop_loss, 8),
            tp1=round(take_profit_1, 8),
            tp2=round(take_profit_2, 8),
        )

    # ── Сопровождение ─────────────────────────────────────────────────

    async def _check_exit(self, trade: dict, current_price: float) -> None:
        trade_id = trade["trade_id"]
        entry_price = trade["entry_price"]

        # Восстановить параметры выхода из events_json
        params = self._extract_params(trade)
        if params is None:
            return

        stop_loss = params["stop_loss"]
        take_profit_1 = params["take_profit_1"]
        take_profit_2 = params["take_profit_2"]
        tp1_hit = params.get("tp1_hit", False)
        stop_moved = params.get("stop_moved_to_breakeven", False)

        # После TP1 стоп сдвинут на безубыток
        effective_stop = entry_price if stop_moved else stop_loss

        # TP2
        if current_price >= take_profit_2:
            # Финальный PnL: 50% по TP1 + 50% по TP2
            if tp1_hit:
                avg_exit = (take_profit_1 + take_profit_2) / 2
            else:
                avg_exit = take_profit_2
            await close_trade(trade_id, avg_exit, "take_profit_2")
            await self._send_close_message(trade, avg_exit, "take_profit_2")
            return

        # TP1 — частичная фиксация
        if not tp1_hit and current_price >= take_profit_1:
            params["tp1_hit"] = True
            params["stop_moved_to_breakeven"] = True
            await add_trade_event(
                trade_id, "tp1_hit", current_price,
                json.dumps({"partial_exit_price": current_price, "partial_exit_pct": 50})
            )
            # Обновить params_set с новыми флагами
            await add_trade_event(trade_id, "params_updated", current_price, json.dumps(params))
            logger.info("S1 TP1 hit, stop moved to breakeven", trade_id=trade_id)
            return

        # Stop loss
        if current_price <= effective_stop:
            if tp1_hit:
                # Половина уже зафиксирована по TP1, вторая половина по стопу (= безубыток)
                avg_exit = (take_profit_1 + entry_price) / 2
                await close_trade(trade_id, avg_exit, "stop_loss")
                await self._send_close_message(trade, avg_exit, "stop_loss")
            else:
                await close_trade(trade_id, current_price, "stop_loss")
                await self._send_close_message(trade, current_price, "stop_loss")

    async def _handle_breakout(self, event: dict) -> None:
        """Закрыть сделку при подтверждённом пробое того же уровня."""
        trades = await get_open_trades(self.strategy_id)
        for trade in trades:
            if trade["symbol"] != event["symbol"]:
                continue
            if abs(trade["level"] - event["level"]) / max(trade["level"], 1) > 0.005:
                continue
            current_price = event["current_price"]
            await close_trade(trade["trade_id"], current_price, "breakout_confirmed")
            await self._send_close_message(trade, current_price, "breakout_confirmed")
            logger.info("S1 trade closed on breakout", trade_id=trade["trade_id"])

    # ── Вспомогательные ───────────────────────────────────────────────

    def _extract_params(self, trade: dict) -> dict | None:
        """Достать последний params_set / params_updated из events_json."""
        try:
            events = json.loads(trade.get("events_json") or "[]")
        except Exception:
            return None
        # Берём последний params_updated или params_set
        for ev in reversed(events):
            if ev["type"] in ("params_updated", "params_set"):
                try:
                    return json.loads(ev["note"])
                except Exception:
                    return None
        return None

    # ── Telegram ──────────────────────────────────────────────────────

    async def _send_open_message(
        self, trade: dict, stop_loss: float, tp1: float, tp2: float
    ) -> None:
        ep = trade["entry_price"]
        sl_pct = self._format_pct((stop_loss - ep) / ep * 100)
        tp1_pct = self._format_pct((tp1 - ep) / ep * 100)
        tp2_pct = self._format_pct((tp2 - ep) / ep * 100)
        text = (
            f"📈 [S1 Bounce] {trade['symbol']} LONG\n"
            f"   Уровень: {trade['level']} ({trade['level_type']}, strength={trade['strength_at_entry']})\n"
            f"   Вход: {ep} | p_bounce={trade['p_bounce_at_entry']:.2f} | style={trade['approach_style']}\n"
            f"   SL: {round(stop_loss, 8)} ({sl_pct}) | TP1: {round(tp1, 8)} ({tp1_pct}) | TP2: {round(tp2, 8)} ({tp2_pct})\n"
            f"   Позиция: {int(self.POSITION_SIZE_USDT)} USDT"
        )
        try:
            await send_message(text)
        except Exception as e:
            logger.error("S1 send_open_message failed", error=str(e))

    async def _send_close_message(self, trade: dict, exit_price: float, reason: str) -> None:
        ep = trade["entry_price"]
        pnl_pct = (exit_price - ep) / ep * 100
        pnl_usdt = self.POSITION_SIZE_USDT * pnl_pct / 100
        icon = "✅" if pnl_pct >= 0 else "🔴"
        max_fav  = trade.get("max_favorable_pct") or 0.0
        max_adv  = trade.get("max_adverse_pct") or 0.0
        max_profit_usdt = self.POSITION_SIZE_USDT * max_fav / 100
        max_loss_usdt   = self.POSITION_SIZE_USDT * max_adv / 100
        text = (
            f"{icon} [S1 Bounce] {trade['symbol']} закрыт\n"
            f"   Причина: {reason}\n"
            f"   Вход: {ep} → Выход: {exit_price}\n"
            f"   PnL: {self._format_pct(pnl_pct)} ({self._format_pct(pnl_usdt, sign=True)} USDT)"
            f" | Время: {self._format_duration(trade['entry_time'])}\n"
            f"   📈 Max profit: +{max_fav:.2f}% (+{max_profit_usdt:.2f} USDT)\n"
            f"   📉 Max drawdown: -{max_adv:.2f}% (-{max_loss_usdt:.2f} USDT)"
        )
        try:
            await send_message(text)
        except Exception as e:
            logger.error("S1 send_close_message failed", error=str(e))
