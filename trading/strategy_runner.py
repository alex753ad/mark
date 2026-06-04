"""Strategy runner — подписывается на event bus и прогоняет события через все стратегии."""

from __future__ import annotations

import asyncio

from trading.event_bus import subscribe
from trading.strategy1_bounce import Strategy1Bounce
from trading.strategy2_limit_grid import Strategy2LimitGrid
from trading.strategy3_breakout import Strategy3Breakout
from trading.trade_log import init_trades_db
from logger import logger


async def run_strategies() -> None:
    """
    Точка входа — запускается в asyncio.gather() из main.py.

    1. Инициализирует trades.db.
    2. Создаёт по одному экземпляру каждой стратегии.
    3. Запускает фоновый _timeout_checker (раз в 60 сек).
    4. Главный цикл: ждёт событие из event bus → on_event → _update_open_trades.
    """
    await init_trades_db()
    logger.info("trades.db initialized")

    strategies = [Strategy1Bounce(), Strategy2LimitGrid(), Strategy3Breakout()]
    asyncio.create_task(_timeout_checker(strategies))

    while True:
        event = await subscribe()
        symbol       = event.get("symbol")
        current_price = event.get("current_price")

        for strategy in strategies:
            try:
                await strategy.on_event(event)
            except Exception as e:
                logger.error(
                    "strategy on_event error",
                    strategy_id=strategy.strategy_id,
                    event_type=event.get("event_type"),
                    error=str(e),
                )

        if symbol and current_price:
            for strategy in strategies:
                try:
                    await strategy._update_open_trades(symbol, current_price)
                except Exception as e:
                    logger.error(
                        "strategy _update_open_trades error",
                        strategy_id=strategy.strategy_id,
                        symbol=symbol,
                        error=str(e),
                    )


async def _timeout_checker(strategies: list) -> None:
    """Каждые 60 сек проверяет таймауты по всем стратегиям."""
    while True:
        await asyncio.sleep(60)
        for strategy in strategies:
            try:
                await strategy._check_timeout()
            except Exception as e:
                logger.error(
                    "strategy _check_timeout error",
                    strategy_id=strategy.strategy_id,
                    error=str(e),
                )
