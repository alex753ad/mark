"""Strategy 3: Breakout Momentum — short при подтверждённом пробое поддержки."""

from __future__ import annotations

import json
import time
import uuid

from trading.base_strategy import BaseStrategy
from trading.trade_log import open_trade, close_trade, add_trade_event, get_open_trades
from bot.telegram import send_message
from constants import (
    S3_MIN_BREAKOUT_VOL_RATIO,
    S3_SWEEP_COOLDOWN_SECONDS,
    S3_TP1_ATR_MULT,
    S3_TP2_ATR_MULT,
    S3_SL_ATR_MULT,
)
from logger import logger


class Strategy3Breakout(BaseStrategy):
    strategy_id = 3
    strategy_name = "breakout"

    def __init__(self) -> None:
        # symbol → timestamp последнего события "sweep"
        self._recent_sweep: dict[str, float] = {}

    # ── Вход ──────────────────────────────────────────────────────────

    async def on_event(self, event: dict) -> None:
        event_type = event.get("event_type")

        if event_type == "sweep":
            self._recent_sweep[event["symbol"]] = time.time()
            return

        if event_type == "breakout":
            await self._try_open(event)
            return

        # Bounce по уровню — признак ложного пробоя, закрыть short
        if event_type == "bounce":
            await self._handle_bounce(event)
            return

        # Sweep пришёл уже после открытия — предупреждение
        if event_type == "sweep":
            await self._handle_sweep_warning(event)

    async def _try_open(self, event: dict) -> None:
        symbol = event["symbol"]
        breakout_vol_ratio = event.get("breakout_vol_ratio", 0.0)
        level_side = event.get("level_side", "")

        if breakout_vol_ratio < S3_MIN_BREAKOUT_VOL_RATIO:
            return
        if level_side != "support":
            return

        # Не торговать если был sweep незадолго до пробоя (ложный пробой)
        last_sweep = self._recent_sweep.get(symbol, 0.0)
        if time.time() - last_sweep < S3_SWEEP_COOLDOWN_SECONDS:
            return

        if not await self._can_open_trade(symbol):
            return

        entry_price = event["current_price"]
        level = event["level"]
        atr = event.get("atr", 0.0)

        stop_loss = level + atr * S3_SL_ATR_MULT
        take_profit_1 = entry_price - atr * S3_TP1_ATR_MULT
        take_profit_2 = entry_price - atr * S3_TP2_ATR_MULT

        trade_id = str(uuid.uuid4())
        trade = {
            "trade_id": trade_id,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "symbol": symbol,
            "level": level,
            "level_type": event.get("level_type", ""),
            "level_side": level_side,
            "entry_signal": "breakout",
            "strength_at_entry": event.get("strength", 0),
            "p_bounce_at_entry": event.get("p_bounce", 0.0),
            "expected_depth_at_entry": event.get("expected_depth", 0.0),
            "approach_style": event.get("approach_style", "unknown"),
            "vol_ratio_at_entry": breakout_vol_ratio,
            "atr_at_entry": atr,
            "entry_price": entry_price,
            "entry_time": time.time(),
            "position_size": self.POSITION_SIZE_USDT,
            "direction": "short",
            "grid_orders_json": None,
            "grid_fill_count": None,
        }

        await open_trade(trade)

        params_note = json.dumps({
            "stop_loss": round(stop_loss, 8),
            "take_profit_1": round(take_profit_1, 8),
            "take_profit_2": round(take_profit_2, 8),
            "tp1_hit": False,
            "stop_moved_to_breakeven": False,
            "breakout_vol_ratio": breakout_vol_ratio,
        })
        await add_trade_event(trade_id, "params_set", entry_price, params_note)

        await self._send_open_message(trade, stop_loss, take_profit_1, take_profit_2)

        logger.info(
            "S3 trade opened",
            trade_id=trade_id,
            symbol=symbol,
            entry=entry_price,
            sl=round(stop_loss, 8),
            tp1=round(take_profit_1, 8),
            tp2=round(take_profit_2, 8),
            vol_ratio=breakout_vol_ratio,
        )

    # ── Сопровождение ─────────────────────────────────────────────────

    async def _check_exit(self, trade: dict, current_price: float) -> None:
        trade_id = trade["trade_id"]
        entry_price = trade["entry_price"]

        params = self._extract_params(trade)
        if params is None:
            return

        stop_loss = params["stop_loss"]
        take_profit_1 = params["take_profit_1"]
        take_profit_2 = params["take_profit_2"]
        tp1_hit = params.get("tp1_hit", False)
        stop_moved = params.get("stop_moved_to_breakeven", False)

        # Short: стоп выше цены входа; после TP1 — на уровне безубытка
        effective_stop = entry_price if stop_moved else stop_loss

        # TP2 — цена ушла достаточно вниз
        if current_price <= take_profit_2:
            avg_exit = (take_profit_1 + take_profit_2) / 2 if tp1_hit else take_profit_2
            await close_trade(trade_id, avg_exit, "take_profit_2")
            await self._send_close_message(trade, avg_exit, "take_profit_2")
            return

        # TP1
        if not tp1_hit and current_price <= take_profit_1:
            params["tp1_hit"] = True
            params["stop_moved_to_breakeven"] = True
            await add_trade_event(
                trade_id, "tp1_hit", current_price,
                json.dumps({"partial_exit_price": current_price, "partial_exit_pct": 50})
            )
            await add_trade_event(trade_id, "params_updated", current_price, json.dumps(params))
            logger.info("S3 TP1 hit, stop moved to breakeven", trade_id=trade_id)
            return

        # Stop loss (для short: цена ушла вверх выше стопа)
        if current_price >= effective_stop:
            if tp1_hit:
                avg_exit = (take_profit_1 + entry_price) / 2
                await close_trade(trade_id, avg_exit, "stop_loss")
                await self._send_close_message(trade, avg_exit, "stop_loss")
            else:
                await close_trade(trade_id, current_price, "stop_loss")
                await self._send_close_message(trade, current_price, "stop_loss")

    async def _handle_bounce(self, event: dict) -> None:
        """Bounce по тому же уровню = пробой не подтвердился, закрыть short."""
        trades = await get_open_trades(self.strategy_id)
        for trade in trades:
            if trade["symbol"] != event["symbol"]:
                continue
            if abs(trade["level"] - event["level"]) / max(trade["level"], 1) > 0.005:
                continue
            current_price = event["current_price"]
            await close_trade(trade["trade_id"], current_price, "breakout_failed_bounce")
            await self._send_close_message(trade, current_price, "breakout_failed_bounce")
            logger.info(
                "S3 trade closed — breakout failed (bounce)",
                trade_id=trade["trade_id"],
            )

    async def _handle_sweep_warning(self, event: dict) -> None:
        """Sweep после открытия — записать предупреждение, не закрывать."""
        trades = await get_open_trades(self.strategy_id)
        for trade in trades:
            if trade["symbol"] != event["symbol"]:
                continue
            await add_trade_event(
                trade["trade_id"], "sweep_warning", event["current_price"],
                f"sweep detected after entry, vol_ratio={event.get('sweep_vol_ratio', 0)}"
            )
            logger.info("S3 sweep warning recorded", trade_id=trade["trade_id"])

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

    # ── Telegram ──────────────────────────────────────────────────────

    async def _send_open_message(
        self, trade: dict, stop_loss: float, tp1: float, tp2: float
    ) -> None:
        ep = trade["entry_price"]
        # Short: SL выше входа (+), TP ниже входа (-)
        sl_pct = self._format_pct((stop_loss - ep) / ep * 100)
        tp1_pct = self._format_pct((tp1 - ep) / ep * 100)
        tp2_pct = self._format_pct((tp2 - ep) / ep * 100)
        params = self._extract_params(trade) or {}
        vol_ratio = params.get("breakout_vol_ratio", trade.get("vol_ratio_at_entry", 0.0))
        text = (
            f"🔴 [S3 Breakout] {trade['symbol']} SHORT\n"
            f"   Пробой уровня: {trade['level']} ({trade['level_type']})"
            f" | Объём: ×{vol_ratio:.1f}\n"
            f"   Вход: {ep} | strength={trade['strength_at_entry']}\n"
            f"   SL: {round(stop_loss, 8)} ({sl_pct})"
            f" | TP1: {round(tp1, 8)} ({tp1_pct})"
            f" | TP2: {round(tp2, 8)} ({tp2_pct})\n"
            f"   Позиция: {int(self.POSITION_SIZE_USDT)} USDT"
        )
        try:
            await send_message(text)
        except Exception as e:
            logger.error("S3 send_open_message failed", error=str(e))

    async def _send_close_message(self, trade: dict, exit_price: float, reason: str) -> None:
        ep = trade["entry_price"]
        # Short: прибыль если цена упала
        pnl_pct = (ep - exit_price) / ep * 100
        pnl_usdt = self.POSITION_SIZE_USDT * pnl_pct / 100
        icon = "✅" if pnl_pct >= 0 else "🔴"
        text = (
            f"{icon} [S3 Breakout] {trade['symbol']} закрыт\n"
            f"   Причина: {reason}\n"
            f"   Вход: {ep} → Выход: {exit_price}\n"
            f"   PnL: {self._format_pct(pnl_pct)} ({self._format_pct(pnl_usdt, sign=True)} USDT)"
            f" | Время: {self._format_duration(trade['entry_time'])}\n"
            f"   Max drawdown: {self._format_pct(-(trade.get('max_adverse_pct') or 0.0))}"
        )
        try:
            await send_message(text)
        except Exception as e:
            logger.error("S3 send_close_message failed", error=str(e))
