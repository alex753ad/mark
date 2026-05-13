# Trading Bot

Telegram-бот для мониторинга уровней поддержки/сопротивления на фьючерсах Binance.
Автоматически строит уровни, оценивает их силу через Claude AI и отслеживает поведение цены.

## Стек

- Python 3.12+, asyncio
- Binance Futures API (python-binance)
- Claude API (Haiku 4.5 для генерации reason)
- Telegram Bot (aiogram 3)
- SQLite (aiosqlite) для истории

## Структура

```
trading_bot/
├── config.py              # Конфиг, TokenRegistry
├── main.py                # Координатор: циклы, фазы, задачи
├── tokens.json            # Список активных монет
├── bot.log                # Лог-файл
├── .env                   # Ключи API
├── data/
│   └── collector.py       # Сбор свечей 15М и 1М с Binance Futures
├── analysis/
│   ├── level_builder.py   # Построение уровней (pump_base, body_level, wick_level)
│   ├── trigger.py         # Триггер коррекции, ATR, zone_approaches, find_real_level
│   └── monitor.py         # Мониторинг уровня: пробой, отбой, осложнения, sweep
├── ai/
│   └── claude_client.py   # Haiku фильтр + Sonnet оценка с prompt caching
└── bot/
    └── telegram.py        # Команды Telegram, отправка сообщений
```

## Запуск

```bash
pip install python-binance anthropic aiogram python-dotenv
cd trading_bot
python main.py
```

## .env

```
CLAUDE_API_KEY=sk-ant-...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Команды Telegram

| Команда | Описание |
|---------|----------|
| `/add SYMBOL` | Добавить монету в мониторинг |
| `/remove SYMBOL` | Убрать монету и отменить все мониторинги |
| `/list` | Показать активные монеты |
| `/check SYMBOL LEVEL` | Оценить уровень через Claude + запустить мониторинг |
| `/monitors` | Список активных мониторингов с дистанцией до цены |
| `/stop SYMBOL` | Остановить все мониторинги монеты |
| `/analyze SYMBOL` | Запустить разовый анализ |

## Как работает

### Фаза 1 — Триггер коррекции

`_trigger_loop` каждые 5 сек проверяет: рост +3% на 15М + первая красная 1М свеча.
При срабатывании:

1. `level_builder` строит уровни (pump_base, body_level, wick_level)
2. `find_real_level` уточняет каждый уровень по кластеру касаний в 15М
3. Считаются: approach, vol_ratio, zone_approaches, история уровня
4. Haiku фильтрует слабые уровни (wick_level, body_level < 2 тел)
5. Sonnet оценивает прошедшие: strength 1-5, verdict hold/exit/exit_fast
6. Уровни с strength >= 4 → сообщение в Telegram + запуск мониторинга

### Фаза 2 — Мониторинг уровня

`start_monitor` следит за каждым уровнем до пробоя. Без таймаута.

Завершение мониторинга:
- Тело 1М свечи закрылось ниже уровня (support) или выше (resistance)
- Сильный пробой (объём ×2+) → мониторинг завершается
- Слабый пробой (объём < ×2) → сообщение, мониторинг продолжается

Алерты в процессе:
- Отбой от уровня (зелёная свеча + объём выше среднего)
- Объём при падении ×3+ от нормы
- 15М поглощение памп-свечи
- Промежуточный уровень пробит без отскока
- Sweep + reclaim → переоценка через Claude
- Нарастающий объём на подходе (1М + подтверждение 15М)

### Proximity алерт

`_proximity_loop` — отдельный цикл. Когда цена в 0.4% от мониторимого уровня →
алерт "готовь ордер". Не чаще раза в 60 секунд на уровень.

## Защита от спама

- Слабый пробой: одно сообщение, повтор только после возврата цены
- Отбой: одно сообщение, сброс после ухода цены на ATR
- Давление на подходе: одно сообщение, сброс после ухода на ATR
- Proximity: timestamp-based, минимум 60 сек между алертами

## Управление задачами

- `active_tasks` — словарь asyncio.Task по ключу `SYMBOL_LEVEL`
- `stop_flags` — asyncio.Event для гарантированной остановки
- `/stop` и `/remove` отменяют задачи + устанавливают stop_flags
- Фаза сбрасывается в "idle" когда все мониторы символа завершены

## Claude API

- Semaphore(2) — не более 2 параллельных запросов
- Системные промпты с `cache_control: ephemeral` (prompt caching)
- Кэш результатов в памяти по ключу `symbol:level`
- `skip_cache=True` при переоценке после sweep

### Параметры оценки

| Параметр | Влияние |
|----------|---------|
| pump_base + approach=1 + vol_ratio < 1 | strength 5 |
| approach=2 | strength 2, exit |
| origin + pump_base | +1 strength |
| cluster | -1 strength |
| pump_volume_ratio < 1.5 | -1 strength |
| zone_approaches 1/2/3+ | -1/-2/-3 strength |
| was_broken + not reclaimed | -2 strength |
| sweep_reclaimed | без снижения |

## Уточнение уровня

`find_real_level` ищет кластер касаний (low/high) в `candles_15m` в радиусе 1 ATR%.
Если >= 3 касаний и медиана смещена > 30% радиуса — используется скорректированный уровень.
Применяется и в `/check`, и в автоматическом режиме.

## Округление уровней

Уровни округляются по цене инструмента:
- \> 100 → 2 знака | 1–100 → 4 знака | 0.1–1 → 5 знаков | 0.01–0.1 → 6 знаков | < 0.01 → 8 знаков

## Логирование

`bot.log` + консоль. Формат: `время модуль сообщение`.
Все ошибки логируются с traceback + уведомление в Telegram.
