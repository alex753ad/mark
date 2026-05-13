# Архитектура Trading Bot

## Структура проекта

```
trading_bot/
├── .env                     # API ключи (Binance, Claude, Telegram)
├── .env.example
├── tokens.json              # ["TONUSDT", "BUSDT", ...] — список монет
├── trigger_times.json       # cooldown таймстампы по символам
├── history.db               # SQLite — история исходов уровней
├── config.py                # Конфигурация + TokenRegistry
├── logger.py                # Loguru: консоль + ротация логов
├── main.py                  # Оркестратор: фазы, таски, shutdown
│
├── data/
│   ├── collector.py         # Сбор свечей 1М/15М с Binance (каждые 5 сек)
│   └── history.py           # SQLite: save/get level outcomes
│
├── analysis/
│   ├── level_builder.py     # Построение уровней поддержки
│   ├── trigger.py           # Триггер коррекции + расчёт strength
│   ├── monitor.py           # Мониторинг уровня (пробой/отскок)
│   └── chart.py             # PNG-график с VWAP и Volume Profile
│
├── ai/
│   └── claude_client.py     # Claude Haiku — генерация reason
│
├── bot/
│   └── telegram.py          # Telegram-бот (aiogram)
│
└── logs/
    └── bot_YYYY-MM-DD.log   # Дневные логи
```

---

## Поток данных

```
Binance Futures API
        │
        ▼
┌─────────────────┐
│  collector.py    │  каждые 5 сек обновляет candles_1m / candles_15m
│  300 свечей/ТФ   │  (глобальные dict'ы в памяти)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  main.py         │  _trigger_loop() — проверяет триггер каждые 5 сек
│  _trigger_loop   │
└────────┬────────┘
         │  триггер сработал (рост 3% на 15М + красная 1М)
         ▼
┌─────────────────┐
│  Фаза 1          │
│  _run_phase1()   │
│                  │
│  1. level_builder.build_levels()     → список уровней
│  2. trigger.get_approaching_levels() → фильтр по ATR×0.5
│  3. trigger.calculate_strength()     → strength (1-5), verdict
│  4. claude_client.analyze_levels()   → reason (текст)
│  5. telegram.send_message()          → уведомление (strength >= 4)
└────────┬────────┘
         │  для каждого уровня с strength >= 4
         ▼
┌─────────────────┐
│  Фаза 2          │
│  _monitored()    │
│                  │
│  monitor.start_monitor()   — каждые 5 сек проверяет:
│    - пробой (объём >= 2x)  → завершение
│    - слабый пробой         → сообщение, продолжает
│    - отскок                → сообщение (однократно)
│    - sweep + reclaim       → сообщение
│    - volume spike (>= 3x)  → сообщение
│    - engulfing 15М         → сообщение
│    - 5 свечей ниже уровня  → сообщение
│    - давление на подходе   → сообщение
│                  │
│  _proximity_loop()  — алерт при 0.4% от уровня
└─────────────────┘
```

---

## Система фаз

```
    idle ──── триггер (3% рост + красная 1М) ────► phase1
                                                      │
                              build_levels             │
                              get_approaching_levels   │
                              calculate_strength       │
                              analyze_levels (Claude)  │
                              telegram: уведомление    │
                                                      │
                    strength >= 4 ─────────────────► phase2
                                                      │
                              monitor (каждые 5 сек)   │
                              proximity_loop (0.4%)    │
                                                      │
                    пробой / stop ─────────────────► idle
```

---

## Модули детально

### config.py

| Что | Описание |
|-----|----------|
| `CLAUDE_API_KEY` | из `.env` |
| `TELEGRAM_TOKEN` | из `.env` |
| `TELEGRAM_CHAT_ID` | из `.env` |
| `TokenRegistry` | CRUD для `tokens.json` — `add()`, `remove()`, `get_all()`, `contains()` |
| `token_registry` | глобальный singleton |

---

### data/collector.py

| Что | Описание |
|-----|----------|
| `candles_15m[symbol]` | list[dict], до 300 свечей (~75 часов) |
| `candles_1m[symbol]` | list[dict], до 300 свечей (~5 часов) |
| `invalid_symbols` | set — символы, которые не прошли валидацию |
| `start_collector()` | async loop — загружает историю, обновляет каждые 5 сек |
| `_fetch_history()` | загружает 300 свечей 15М и 1М при старте |
| `_update()` | обновляет последние 2 свечи каждого ТФ |

**Формат свечи:**
```python
{"open": float, "high": float, "low": float, "close": float,
 "volume": float, "open_time": int, "close_time": int}
```

---

### data/history.py

| Таблица | Описание |
|---------|----------|
| `level_outcomes` | symbol, level, type, strength, result (breakout/rebound), duration, volume |
| `symbol_profiles` | агрегат по символу: success_rate по типу уровня |

| Функция | Описание |
|---------|----------|
| `init_db()` | создание таблиц |
| `save_level_outcome()` | записывает результат после завершения мониторинга |
| `get_symbol_profile()` | статистика по последним 50 сигналам |
| `update_symbol_profile()` | пересчёт success_rate |

---

### analysis/level_builder.py

**Главная функция:** `build_levels(symbol) -> list[dict]`

**Этапы:**
1. `_find_multiple_pump_zones(c15m)` — находит до 5 пампов >=5% на 15М → список (pump_low, pump_high, start_time, end_time)
2. Для каждой pump_zone:
   - `_calculate_volume_profile()` — распределение объёма по ценовым уровням
   - `_get_poc_from_profile()` — определение POC (Point of Control)
   - `_find_pump_base_levels()` — уровни от pump_low и близких консолидаций
   - `_find_consolidation_base_levels()` — горизонтальные зоны перед продолжением
   - `_find_body_levels()` — кластеризация тел свечей (вес: 15М=3, 1М=1, порог=6)
   - `_find_wick_levels()` — кластеризация теней после пика (>=4 касания)
   - `_find_order_blocks()` — последняя красная свеча перед пампом
3. `_deduplicate_with_priority()` — убирает дубли с приоритетом: pump_base > consolidation_base > body_level > order_block > wick_level
4. `_assign_positions(levels, ...)` — `origin` (ниже pump_low) или `mid_move`
5. `_mark_clusters(levels)` — помечает уровни ближе 1% друг к другу

**Типы уровней:**

| Тип | Условие | Base Strength |
|-----|---------|---------------|
| `pump_base` | База импульсного движения >=5%, POC-aligned уровни | 5 |
| `consolidation_base` | Зона консолидации перед продолжением (3+ свечи, range < 4×ATR) | 4 |
| `body_level` | Взвешенный кластер тел (порог >= 6) | 4 |
| `wick_level` | 4+ теней после пика пампа | 2 |
| `order_block` | Последняя красная свеча перед пампом | 4 |

**Округление цены:**

| Цена | Знаки после запятой |
|------|---------------------|
| > 100 | 2 |
| 1-100 | 4 |
| 0.1-1 | 5 |
| 0.01-0.1 | 6 |
| < 0.01 | 8 |

---

### analysis/trigger.py

| Функция | Описание |
|---------|----------|
| `check_trigger(symbol)` | рост >=3% на 15М + первая красная 1М → `True` |
| `get_approaching_levels(symbol)` | уровни в пределах ATR*0.5 от текущей цены |
| `find_real_level(symbol, level)` | уточняет уровень по кластеру касаний (медиана >=3) |
| `calculate_atr(symbol)` | ATR(14) на 1М свечах |
| `calculate_atr_pct(symbol)` | ATR как % от цены |
| `_count_approaches(symbol, level, atr)` | число подходов цены к уровню |
| `get_level_history(symbol, level, atr)` | `was_broken`, `sweep_reclaimed`, `max_vol` |
| `_calc_vol_ratio(symbol)` | текущий объём / среднее (24ч + 1ч) / 2 |
| `calculate_strength(lvl)` | strength (1-5) + verdict (`hold`/`exit`/`exit_fast`) |

**Логика strength:**
```
Базовый: pump_base=5, consolidation_base=4, body_level=4, order_block=4, wick_level=2

poc_aligned=true      → +1 strength
approach >= 2         → strength=2, verdict=exit
position=origin       → +1
cluster=true          → -1, verdict=exit если < 4
pump_vol_ratio < 1.5  → -1
was_broken && !sweep  → -2
zone_approaches 1/2/3+ → -1/-2/-3, verdict=exit если >= 3
engulf_15m + vol > 2  → verdict=exit_fast
```

---

### analysis/monitor.py

**Главная функция:** `start_monitor(symbol, level, level_side, stop_event)`

Каждые 5 секунд проверяет 8 событий:

| # | Событие | Условие | Действие |
|---|---------|---------|----------|
| 1 | Пробой | body ниже/выше + vol >= 2x avg | завершает мониторинг |
| 2 | Слабый пробой | body ниже/выше + vol < 2x | сообщение, cooldown 60 сек |
| 3 | Отскок | касание + зелёная + vol > avg | сообщение (однократно) |
| 4 | Sweep + reclaim | prev close ниже + curr close выше + vol растёт | сообщение |
| 5 | Volume spike | red + vol >= 3x avg60 | сообщение (однократно) |
| 6 | Engulfing 15М | зелёная 15М поглощена красной | сообщение (однократно) |
| 7 | Level broken | 5 свечей подряд ниже уровня | сообщение (однократно) |
| 8 | Pressure | 3+ красных + vol растёт, зона 0.2-1% | сообщение (однократно) |

**Антиспам:** все флаги сбрасываются при удалении цены > 1 ATR от уровня.

---

### analysis/chart.py

`generate_chart(symbol, levels, broken_levels) -> PNG bytes`

- 50 последних свечей 15М
- VWAP линия
- Volume Profile (100 зон) + POC
- Зелёные линии — уровни strength >= 4
- Красные/зелёные — пробитые уровни
- Объём в USD, тёмная тема (#0d0d0d)

---

### ai/claude_client.py

| Функция | Описание |
|---------|----------|
| `analyze_levels(levels)` | принимает уровни с рассчитанным strength/verdict, добавляет `reason` |
| `_get_reason(levels)` | один запрос к Claude Haiku 4.5, возвращает `[{"level": float, "reason": str}]` |

- Модель: `claude-haiku-4-5-20251001`
- Системный промпт с `ephemeral` cache control (prompt caching)
- Результат кэшируется в памяти по ключу `symbol:level`
- После Фазы 1 новых запросов к Claude не делает
- JSON-парсинг с regex fallback

---

### bot/telegram.py

| Команда | Описание |
|---------|----------|
| `/add SYMBOL` | добавить монету (валидация на Binance) |
| `/remove SYMBOL` | убрать монету + остановить все мониторы |
| `/list` | список активных монет |
| `/check SYMBOL LEVEL` | оценить уровень + запустить мониторинг |
| `/monitors` | активные мониторы + расстояние до цены |
| `/stop SYMBOL` | остановить все мониторы символа |
| `/analyze SYMBOL` | разовый анализ всех уровней в зоне -20% |

- Авторизация по `TELEGRAM_CHAT_ID`
- Inline-кнопки для быстрого доступа
- FSM-состояния для многошаговых команд
- Подсказка ближайшего уровня (в пределах 2%)
- Генерация графика при `/analyze`
- Лимит сообщения: 4096 символов

---

### main.py — оркестратор

**Глобальное состояние:**
```python
active_phases: dict[str, str]         # "idle" / "phase1" / "phase2"
active_tasks: dict[str, asyncio.Task] # ключ: "SYMBOL_LEVEL"
stop_flags: dict[str, asyncio.Event]  # гарантированная остановка
proximity_notified: dict[str, float]  # timestamp последнего алерта
claude_semaphore: Semaphore(2)        # лимит параллельных запросов к Claude
analyzed_levels: set[str]             # "SYMBOL:LEVEL" — уже оценены
last_trigger_time: dict[str, float]   # cooldown 1 час между триггерами
```

**Запуск (asyncio.gather):**
1. `start_collector()` — сбор данных с Binance
2. `start_bot()` — Telegram polling
3. `_trigger_loop()` — проверка триггеров каждые 5 сек
4. `_send_startup_screener()` — скринер через 15 сек после старта

**Скринер при старте:**
- Все фьючерсы Binance → фильтр: USDT, объём > 40M, рост > +10%, NATR(5M) > 2.0
- Сортировка по росту, вывод таблицей

---

## Защита от спама

| Механизм | Где применяется |
|----------|-----------------|
| Одноразовые флаги | отскок, engulfing, level_broken, volume_spike |
| Timestamp cooldown (60 сек) | weak breakout, proximity alert |
| ATR-based reset | все флаги — при уходе цены на > 1 ATR |
| Volume normalization | volume_spike сброс при vol < x1.5 |
| analyzed_levels set | уровень не переоценивается повторно |
| claude_semaphore(2) | не более 2 параллельных запросов к Claude |
| trigger cooldown (1 час) | один триггер на символ в час |

---

## Зависимости

```
python-binance     # Binance Futures API
anthropic          # Claude API
aiogram            # Telegram Bot (v3)
python-dotenv      # Environment variables
aiosqlite          # SQLite async
loguru             # Logging
numpy              # Chart calculations
matplotlib         # Chart generation
```

---

## Запуск

```bash
pip install python-binance anthropic aiogram python-dotenv aiosqlite loguru numpy matplotlib
python main.py
```

## .env

```
CLAUDE_API_KEY=sk-ant-...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```
