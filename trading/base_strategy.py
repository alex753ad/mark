"""Base class for all trading strategies (paper trading)."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

from data.collector import candles_1m
from bot.telegram import send_message
from trading.trade_log import (
    get_open_trades,
    close_trade,
    update_trade_extremes,
    add_trade_event,
)
from constants import (
    STRATEGY_POSITION_SIZE_USDT,
    STRATEGY_MAX_OPEN_TRADES,
    STRATEGY_TRADE_TIMEOUT_MINUTES,
)
from logger import logger


class BaseStrategy(ABC):
    strategy_id: int       # задаётся в подклассе как атрибут класса
    strategy_name: str     # задаётся в подклассе как атрибут класса

    POSITION_SIZE_USDT: float = STRATEGY_POSITION_SIZE_USDT
    MAX_OPEN_TRADES: int = STRATEGY_MAX_OPEN_TRADES
    TRADE_TIMEOUT_MINUTES: float = STRATEGY_TRADE_TIMEOUT_MINUTES

    # ── Публичный интерфейс ───────────────────────────────────────────

    @abstractmethod
    async def on_event(self, event: dict) -> None:
        """Точка входа — вызывается из strategy_runner для каждого события."""

    @abstractmethod
    async def _send_open_message(self, trade: dict) -> None:
        """Telegram-сообщение об открытии сделки."""

    @abstractmethod
    async def _send_close_message(self, trade: dict, exit_price: float, reason: str) -> None:
        """Telegram-сообщение о закрытии сделки."""

    # ── Общая логика: таймаут ─────────────────────────────────────────

    async def _check_timeout(self) -> None:
        """
        Закрыть все открытые сделки этой стратегии, которые превысили
        TRADE_TIMEOUT_MINUTES. Вызывается каждые 60 сек из strategy_runner.
        """
        trades = await get_open_trades(self.strategy_id)
        now = time.time()
        for trade in trades:
            age_minutes = (now - trade["entry_time"]) / 60
            if age_minutes < self.TRADE_TIMEOUT_MINUTES:
                continue

            symbol = trade["symbol"]
            c1m = candles_1m.get(symbol, [])
            current_price = (
                c1m[-1]["close"] if c1m else trade["entry_price"]
            )

            try:
                await close_trade(trade["trade_id"], current_price, "timeout")
                await self._send_close_message(trade, current_price, "timeout")
                logger.info(
                    "Trade closed by timeout",
                    strategy=self.strategy_name,
                    trade_id=trade["trade_id"],
                    symbol=symbol,
                    age_minutes=round(age_minutes, 1),
                )
            except Exception as e:
                logger.error(
                    "Error closing timed-out trade",
                    strategy=self.strategy_name,
                    trade_id=trade["trade_id"],
                    error=str(e),
                )

    # ── Общая логика: обновление экстремумов и проверка TP/SL ─────────

    async def _update_open_trades(self, symbol: str, current_price: float) -> None:
        """
        Для всех открытых сделок этой стратегии по symbol:
        1. Обновить max_favorable_pct / max_adverse_pct.
        2. Проверить достижение stop_loss и take_profit.

        Логика TP/SL специфична для стратегии — делегируется _check_exit().
        """
        trades = await get_open_trades(self.strategy_id)
        for trade in trades:
            if trade["symbol"] != symbol:
                continue
            try:
                await update_trade_extremes(
                    trade["trade_id"],
                    current_price,
                    trade["entry_price"],
                    trade["direction"],
                )
                await self._check_exit(trade, current_price)
            except Exception as e:
                logger.error(
                    "Error updating open trade",
                    strategy=self.strategy_name,
                    trade_id=trade["trade_id"],
                    error=str(e),
                )

    @abstractmethod
    async def _check_exit(self, trade: dict, current_price: float) -> None:
        """
        Проверить условия выхода (SL / TP1 / TP2) для одной открытой сделки.
        Вызывается из _update_open_trades для каждой сделки символа.

        trade — полная строка из БД (все поля).
        current_price — цена последней 1М свечи.

        При срабатывании выхода:
          1. await close_trade(trade["trade_id"], exit_price, exit_reason)
          2. await self._send_close_message(trade, exit_price, exit_reason)
        """

    # ── Вспомогательные методы ────────────────────────────────────────

    async def _has_open_trade_for_symbol(self, symbol: str) -> bool:
        """True если по symbol уже есть открытая сделка этой стратегии."""
        trades = await get_open_trades(self.strategy_id)
        return any(t["symbol"] == symbol for t in trades)

    async def _open_trades_count(self) -> int:
        """Количество открытых сделок этой стратегии."""
        trades = await get_open_trades(self.strategy_id)
        return len(trades)

    async def _can_open_trade(self, symbol: str) -> bool:
        """
        True если можно открыть новую сделку:
        - нет открытой сделки по этому символу
        - не превышен MAX_OPEN_TRADES
        """
        if await self._has_open_trade_for_symbol(symbol):
            return False
        if await self._open_trades_count() >= self.MAX_OPEN_TRADES:
            return False
        return True

    @staticmethod
    def _format_pct(value: float, sign: bool = True) -> str:
        """Форматировать процент для Telegram: +1.23% или -0.45%."""
        prefix = "+" if sign and value >= 0 else ""
        return f"{prefix}{value:.2f}%"

    @staticmethod
    def _format_duration(entry_time: float) -> str:
        """Время с момента входа в виде '47 мин' или '2ч 13мин'."""
        minutes = int((time.time() - entry_time) / 60)
        if minutes < 60:
            return f"{minutes} мин"
        hours, mins = divmod(minutes, 60)
        return f"{hours}ч {mins}мин"
