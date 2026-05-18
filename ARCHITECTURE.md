# Архитектура Trading Bot

Торговый бот для мониторинга уровней поддержки/сопротивления на фьючерсах Binance.
Стратегия: поиск уровней после пампов, оценка силы уровня, мониторинг подхода цены, алерты в Telegram.

---

## Оглавление

1. [Структура файлов](#структура-файлов)
2. [Стек технологий и зависимости](#стек-технологий-и-зависимости)
3. [Конфигурация и переменные окружения](#конфигурация-и-переменные-окружения)
4. [Модели данных](#модели-данных)
5. [Поток данных — общая схема](#поток-данных--общая-схема)
6. [Система фаз](#система-фаз)
7. [Модули детально](#модули-детально)
   - [config.py](#configpy)
   - [constants.py](#constantspy)
   - [logger.py](#loggerpy)
   - [models.py](#modelspy)
   - [data/collector.py](#datacollectorpy)
   - [data/history.py](#datahistorypy)
   - [analysis/level_builder.py](#analysislevel_builderpy)
   - [analysis/trigger.py](#analysistriggerpy)
   - [analysis/monitor.py](#analysismonitorpy)
   - [analysis/chart.py](#analysischartpy)
   - [analysis/chart_ascii.py](#analysischart_asciipy)
   - [analysis/claude_strength.py](#analysisclaude_strengthpy)
   - [ai/claude_client.py](#aiclaude_clientpy)
   - [bot/telegram.py](#bottelegrampy)
   - [main.py](#mainpy)
8. [Система оценки силы уровней](#система-оценки-силы-уровней)
9. [Алгоритм построения уровней](#алгоритм-построения-уровней)
10. [Мониторинг уровней — события и реакции](#мониторинг-уровней--события-и-реакции)
11. [Защита от спама](#защита-от-спама)
12. [Скринер рынка](#скринер-рынка)
13. [База данных — схема и назначение](#база-данных--схема-и-назначение)
14. [Telegram-бот — команды и интерфейс](#telegram-бот--команды-и-интерфейс)
15. [Жизненный цикл символа](#жизненный-цикл-символа)
16. [Запуск и остановка](#запуск-и-остановка)

---

## Структура файлов

```
trading_bot/
├── .env                          # API ключи (Binance, Claude, Telegram)
├── .env.example                  # Шаблон: CLAUDE_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
├── tokens.json                   # ["BUSDT", "TRUTHUSDT", ...] — список активных монет
├── trigger_times.json            # {symbol: unix_timestamp} — cooldown триггеров
├── history.db                    # SQLite — исходы уровней, профили, события
│
├── config.py                     # Загрузка .env, TokenRegistry (CRUD tokens.json)
├── constants.py                  # Все числовые пороги и настройки в одном месте
├── logger.py                     # Loguru: консоль (INFO) + файл (DEBUG, ротация)
├── models.py                     # Dataclass-модели: SymbolState, LevelData, StateManager
├── main.py                       # Оркестратор: запуск, фазы, скринер, proximity
│
├── data/
│   ├── collector.py              # Сбор свечей 1М/15М + aggTrades дельта
│   └── history.py                # SQLite CRUD: outcomes, profiles, events
│
├── analysis/
│   ├── level_builder.py          # Построение уровней (pump_base, body, wick, order_block)
│   ├── trigger.py                # Триггер коррекции + calculate_strength (Python)
│   ├── monitor.py                # Мониторинг уровня: пробой/отскок/sweep/давление
│   ├── chart.py                  # PNG-график: свечи + VWAP + Volume Profile + уровни
│   ├── chart_ascii.py            # ASCII-график для промпта Claude
│   └── claude_strength.py        # Claude Haiku: оценка силы уровней по ASCII-графику
│
├── ai/
│   └── claude_client.py          # Claude Haiku: reason + grid_advice + confidence
│
├── bot/
│   └── telegram.py               # Telegram-бот (aiogram v3): команды, кнопки, FSM
│
└── logs/
    └── bot_YYYY-MM-DD.log        # Дневные логи (ротация 1 день, хранение 7 дней)
```

---

## Стек технологий и зависимости

| Пакет | Версия | Назначение |
|-------|--------|------------|
| `python-binance` | — | Binance Futures API (REST + WebSocket) |
| `anthropic` | — | Claude API (Haiku 4.5) |
| `aiogram` | v3 | Telegram Bot Framework |
| `python-dotenv` | — | Загрузка `.env` |
| `aiosqlite` | — | Async SQLite |
| `loguru` | — | Структурированный логгинг |
| `numpy` | — | Расчёты для графиков |
| `matplotlib` | — | Генерация PNG-графиков |

Всё работает на **asyncio** — один event loop для всех компонентов.

---

## Конфигурация и переменные окружения

### .env

```
CLAUDE_API_KEY=sk-ant-...    # Ключ Anthropic API
TELEGRAM_TOKEN=...           # Telegram Bot Token
TELEGRAM_CHAT_ID=...         # Chat ID для авторизации (int)
```

### config.py

- Загружает `.env` через `python-dotenv`
- Экспортирует: `CLAUDE_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
- Пути к файлам: `TOKENS_FILE`, `TRIGGER_TIMES_FILE`, `HISTORY_DB_FILE`
- `TokenRegistry` — singleton для CRUD над `tokens.json`:
  - `get_all()` → `list[str]`
  - `add(symbol)` — добавляет и сохраняет
  - `remove(symbol)` — удаляет и сохраняет
  - `contains(symbol)` → `bool`
- `validate_config()` — проверяет наличие всех ключей

### constants.py — все пороговые значения

| Группа | Константа | Значение | Описание |
|--------|-----------|----------|----------|
| **Триггер** | `TRIGGER_GROWTH_THRESHOLD` | 0.03 (3%) | Рост на 15М для активации |
| | `TRIGGER_COOLDOWN_SECONDS` | 3600 (1 час) | Cooldown между триггерами одного символа |
| **Уровни** | `LEVEL_APPROACH_THRESHOLD` | 0.5 | ATR × 0.5 = "уровень близко" |
| | `LEVEL_CLUSTER_RADIUS_PCT` | 0.01 (1%) | Радиус кластера уровней |
| | `LEVEL_REAL_CLUSTER_MIN_TOUCHES` | 3 | Мин. касаний для уточнения уровня |
| | `LEVEL_REAL_CLUSTER_SHIFT_THRESHOLD` | 0.3 (30%) | Порог смещения медианы |
| **Мониторинг** | `PROXIMITY_ALERT_DISTANCE_PCT` | 0.004 (0.4%) | Расстояние для proximity alert |
| | `PROXIMITY_ALERT_COOLDOWN_SECONDS` | 600 (10 мин) | Cooldown proximity alerts |
| | `WEAK_BREAKOUT_COOLDOWN_SECONDS` | 300 (5 мин) | Cooldown слабого пробоя |
| **Объём** | `VOLUME_BREAKOUT_RATIO` | 2.0 | ×2 для подтверждения пробоя |
| | `VOLUME_SPIKE_RATIO` | 3.0 | ×3 для алерта спайка |
| | `VOLUME_SPIKE_RESET_RATIO` | 1.5 | < ×1.5 для сброса флага |
| | `VOLUME_REBOUND_MIN_RATIO` | 1.0 | Мин. объём для отбоя |
| **Дистанция** | `DISTANCE_RESET_ATR_MULTIPLIER` | 2.0 | > 2×ATR → полный сброс флагов |
| | `DISTANCE_PARTIAL_RESET_ATR_MULTIPLIER` | 1.0 | > 1×ATR → частичный сброс |
| | `PRESSURE_ZONE_MIN_DISTANCE_PCT` | 0.002 (0.2%) | Мин. расстояние для давления |
| | `PRESSURE_ZONE_MAX_DISTANCE_PCT` | 0.01 (1%) | Макс. расстояние для давления |
| **Давление** | `PRESSURE_MIN_DIRECTIONAL_CANDLES` | 3 | Мин. 3 направленных свечи |
| | `PRESSURE_VOLUME_MIN_RATIO` | 1.0 | Мин. объём 15М для подтверждения |
| **Сила уровня** | `STRENGTH_PUMP_VOLUME_LOW_THRESHOLD` | 1.5 | Порог низкого объёма пампа |
| | `STRENGTH_PUMP_VOLUME_HIGH_THRESHOLD` | 2.5 | Порог высокого объёма пампа |
| | `STRENGTH_APPROACH_EXIT_THRESHOLD` | 2 | Подходов для verdict=exit |
| **Сбор данных** | `CANDLES_HISTORY_LIMIT` | 300 | Свечей в памяти (1М ≈ 5ч, 15М ≈ 75ч) |
| | `COLLECTOR_UPDATE_INTERVAL_SECONDS` | 5 | Интервал обновления |
| **Скринер** | `SCREENER_MIN_VOLUME_USD` | 40M | Мин. объём |
| | `SCREENER_MIN_GROWTH_PCT` | 10% | Мин. рост |
| | `SCREENER_MIN_NATR` | 2.0 | Мин. NATR(5M) |
| | `SCREENER_DELAY_SECONDS` | 15 | Задержка перед первым скринером |
| | `SCREENER_AUTO_INTERVAL_SECONDS` | 1800 (30 мин) | Интервал автоскринера |
| **Claude** | `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Модель |
| | `CLAUDE_MAX_TOKENS` | 1024 | Макс. токенов в ответе |
| | `CLAUDE_MAX_CONCURRENT_REQUESTS` | 2 | Семафор параллелизма |
| | `CLAUDE_STRENGTH_ENABLED` | True | Флаг использования Claude |
| **ATR** | `ATR_PERIOD` | 14 | Период ATR на 1М свечах |
| **Уровни (build)** | `PUMP_MIN_GROWTH_PCT` | 0.05 (5%) | Мин. рост для определения пампа |
| | `BODY_CLUSTER_WEIGHT_15M` | 3 | Вес 15М свечи |
| | `BODY_CLUSTER_WEIGHT_1M` | 1 | Вес 1М свечи |
| | `BODY_CLUSTER_MIN_WEIGHT` | 6 | Мин. вес для body_level |
| | `WICK_CLUSTER_MIN_TOUCHES` | 4 | Мин. касаний для wick_level |
| | `CONSOLIDATION_MIN_CANDLES` | 3 | Мин. свечей для консолидации |
| | `CONSOLIDATION_RANGE_ATR_MULTIPLIER` | 4 | ATR множитель для узкого range |
| **Монитор** | `LEVEL_BROKEN_MIN_CANDLES` | 5 | Мин. свечей ниже уровня |

### Округление цены (`PRICE_ROUNDING_RULES`)

| Диапазон цены | Знаков после запятой |
|---------------|---------------------|
| > 100 | 2 |
| 1–100 | 4 |
| 0.1–1 | 5 |
| 0.01–0.1 | 6 |
| < 0.01 | 8 |

---

## Модели данных

### models.py

#### `SymbolState` — состояние одного символа

```python
@dataclass
class SymbolState:
    symbol: str
    phase: "idle" | "phase1" | "phase2" = "idle"
    tasks: dict[str, asyncio.Task]           # ключ: "SYMBOL_LEVEL"
    stop_flags: dict[str, asyncio.Event]     # флаги остановки мониторов
    last_trigger_time: float = 0.0
    proximity_notified: dict[str, float]     # timestamp последнего proximity alert
    analyzed_levels: set[str]                # "SYMBOL:LEVEL" — уже оценены
```

Методы:
- `make_task_key(level)` → `"SYMBOL_roundedLevel"`
- `add_task(level, task)` — добавляет task, **отменяет дубли** в радиусе 0.5%
- `remove_task(task_key)` — удаляет task, stop_flag, proximity_notified
- `cancel_all_tasks()` — отменяет все task'и + устанавливает все stop_flags
- `mark_level_analyzed(level)` / `is_level_analyzed(level)` — кэш оценённых уровней
- `clear_analyzed_levels()` — очистка кэша (при breakout, при re-trigger)

#### `LevelData` — структура данного уровня

```python
@dataclass
class LevelData:
    level: float
    type: str                    # pump_base, body_level, wick_level, order_block
    symbol: str
    level_side: "support" | "resistance"
    strength: int = 0            # 1-5
    verdict: "hold" | "exit" | "exit_fast" = "hold"
    reason: str = ""
    
    # Технические индикаторы
    approach: int = 0            # число подходов к уровню
    vol_ratio: float = 1.0       # текущий объём / среднее
    atr_pct: float = 0.0         # ATR как % от цены
    zone_approaches: int = 0     # подходы к соседним уровням в зоне
    
    # Характеристики уровня
    position: str = "mid_move"   # origin, impulse, mid_move
    cluster: bool = False        # есть соседний уровень < 1%
    pump_volume_ratio: float = 1.5
    
    # История
    was_broken: bool = False
    sweep_reclaimed: bool = False
    price_min_since_level: float = 0.0
    max_vol_on_approach: float = 0.0
    engulf_15m: bool = False
```

#### `StateManager` — глобальный менеджер состояний

Singleton: `state_manager = StateManager()`

- `get_state(symbol)` → `SymbolState` (создаёт если нет)
- `remove_state(symbol)` — отменяет все задачи и удаляет
- `get_all_active_tasks()` → `dict[str, Task]` (все символы)
- `cancel_all_tasks()` — всё остановить
- `get_active_monitors_count()` → `int`

---

## Поток данных — общая схема

```
                        Binance Futures API
                               │
                 ┌─────────────┼─────────────┐
                 │             │              │
          REST klines     REST klines    WebSocket aggTrade
          (15M, 1M)       (5M для        (по запросу при
                          скринера)       касании уровня)
                 │             │              │
                 ▼             ▼              ▼
        ┌────────────┐  ┌──────────┐  ┌─────────────┐
        │ collector   │  │ screener │  │ agg_trades  │
        │             │  │          │  │ delta buffer│
        │ candles_1m  │  │ rows[]   │  │ buy/sell    │
        │ candles_15m │  │          │  │ 60 сек      │
        └──────┬─────┘  └────┬─────┘  └──────┬──────┘
               │              │               │
               ▼              ▼               │
        ┌────────────┐  ┌──────────────┐      │
        │ trigger     │  │ auto_screener │     │
        │ _loop()     │  │ _loop()       │     │
        │ каждые 5с   │  │ каждые 30мин  │     │
        └──────┬─────┘  └──────┬───────┘      │
               │               │              │
     триггер   │   новый       │              │
     сработал  │   символ      │              │
               ▼               ▼              │
        ┌──────────────────────────┐          │
        │     _run_phase1()         │          │
        │                          │          │
        │  1. build_levels()       │          │
        │  2. get_approaching()    │          │
        │  3. calculate_strength() │          │
        │  4. send_message()       │          │
        └───────────┬──────────────┘          │
                    │                         │
          strength >= 4                       │
                    │                         │
                    ▼                         │
        ┌──────────────────────────┐          │
        │     _monitored()          │◄─────────┘
        │     start_monitor()       │
        │                          │
        │  каждые 5 сек:           │
        │  - пробой / закол        │
        │  - отскок                │
        │  - sweep + reclaim       │
        │  - volume spike          │
        │  - engulfing 15М         │
        │  - level broken          │
        │  - pressure              │
        │  - delta reversal        │
        │  - classify touch        │
        └───────────┬──────────────┘
                    │
                    ▼
        ┌──────────────────────────┐
        │  Завершение мониторинга   │
        │                          │
        │  → save_level_outcome()  │
        │  → update_symbol_profile │
        │  → log_event()           │
        │                          │
        │  Если breakout:           │
        │  → _start_next_level_    │
        │    after_breakout()      │
        │  → или remove символ     │
        └──────────────────────────┘
```

---

## Система фаз

Каждый символ проходит через фазы независимо:

```
                                    ┌──────────────────────────────────┐
                                    │                                  │
    idle ─── триггер (3% рост ──────▶ phase1                          │
             + красная 1М)          │                                  │
                                    │  build_levels()                  │
       ▲                            │  get_approaching_levels()        │
       │                            │  calculate_strength()            │
       │                            │  telegram: уведомление           │
       │                            │                                  │
       │                strength >= 4├─────────────────▶ phase2        │
       │                            │                   │              │
       │                            └────────────────── │ ─────────────┘
       │                                                │
       │                  start_monitor() каждые 5 сек  │
       │                  _proximity_loop() 0.4%        │
       │                                                │
       │         пробой                                 │
       │         ───────────── _start_next_level ───────┤
       │                      after_breakout()          │
       │                                                │
       │         нет уровней                            │
       └────────────────────────────────────────────────┘
```

**Переходы фаз:**
- `idle → phase1`: триггер сработал (рост ≥3% на 15М + красная 1М)
- `phase1 → phase2`: найден хотя бы 1 уровень с strength ≥ 4
- `phase1 → idle`: нет сильных уровней
- `phase2 → phase2`: при breakout — автоматический поиск следующего уровня
- `phase2 → idle`: все мониторы завершены, нет активных задач

**Важно:** если символ уже в `phase2` (есть мониторы), новый триггер не переводит в `phase1` — это защита от race condition. Вместо этого build_levels запускается, но фаза остаётся `phase2`, стухшие мониторы снимаются.

---

## Модули детально

### config.py

| Элемент | Описание |
|---------|----------|
| `CLAUDE_API_KEY` | Из `.env` |
| `TELEGRAM_TOKEN` | Из `.env` |
| `TELEGRAM_CHAT_ID` | Из `.env`, приводится к `int` |
| `TOKENS_FILE` | `"tokens.json"` |
| `TRIGGER_TIMES_FILE` | `"trigger_times.json"` |
| `HISTORY_DB_FILE` | `"history.db"` |
| `TokenRegistry` | Класс: `_load()`, `_save()`, `get_all()`, `add()`, `remove()`, `contains()` |
| `token_registry` | Глобальный singleton `TokenRegistry()` |
| `validate_config()` | Проверяет наличие всех ключей, возвращает `bool` |

---

### constants.py

Централизованное хранилище всех числовых порогов. Никакие magic numbers не размазаны по коду.
Полный список — см. таблицу в секции [Конфигурация](#constantspy--все-пороговые-значения).

---

### logger.py

Два обработчика через `loguru`:

1. **Консоль** (`stderr`): `HH:mm:ss {icon} {message}`, уровень `INFO`, с цветами
2. **Файл** (`logs/bot_YYYY-MM-DD.log`): полный формат с `{extra}`, уровень `DEBUG`, ротация 1 день, хранение 7 дней, UTF-8

Функция `log_with_context(level, message, **kwargs)` — логирование со структурированным контекстом через `logger.bind(**kwargs)`.

---

### models.py

См. раздел [Модели данных](#модели-данных).

---

### data/collector.py

**Глобальные хранилища в памяти:**

| Переменная | Тип | Описание |
|------------|-----|----------|
| `candles_15m[symbol]` | `list[dict]` | До 300 свечей 15М (~75 часов) |
| `candles_1m[symbol]` | `list[dict]` | До 300 свечей 1М (~5 часов) |
| `agg_trades[symbol]` | `list[dict]` | Буфер aggTrades за последние 60 сек |
| `invalid_symbols` | `set[str]` | Невалидные символы (не на Binance) |

**Формат свечи:**
```python
{
    "open_time": int,     # unix ms
    "open": float,
    "high": float,
    "low": float,
    "close": float,
    "volume": float,      # в базовой валюте
    "close_time": int,    # unix ms
}
```

**Формат aggTrade:**
```python
{
    "ts": float,      # unix seconds
    "qty": float,     # количество
    "is_buy": bool,   # taker buy (True) или sell (False)
}
```

**Функции:**

| Функция | Описание |
|---------|----------|
| `_parse_kline(kline)` | Преобразует сырые данные Binance в dict |
| `_fetch_history(client, symbol)` | Загружает 300 свечей 15М и 1М при старте |
| `_update(client, symbol)` | Обновляет последние 2 свечи каждого ТФ (upsert по `open_time`) |
| `_all_symbols()` | `token_registry.get_all()` + `["BTCUSDT"]` (всегда собирается) |
| `start_collector()` | Главный async loop: при старте `_fetch_history`, потом `_update` каждые 5 сек |
| `start_delta_tracking(symbol)` | Включает буферизацию aggTrades для символа |
| `stop_delta_tracking(symbol)` | Выключает и очищает буфер |
| `get_delta(symbol, window_seconds=30)` | Считает buy/sell delta за окно: `{buy_vol, sell_vol, delta, trades}` |
| `_stream_agg_trades(symbol)` | WebSocket стрим aggTrades, пишет в буфер, тримит > 60 сек |

**Особенности:**
- BTCUSDT всегда собирается (используется для BTC change в исходах)
- Reconnect при ошибке: sleep 10 сек, пересоздание клиента
- Новые символы из `tokens.json` подгружаются автоматически при следующем цикле
- Delta tracking включается только при касании уровня (экономия ресурсов)

---

### data/history.py

SQLite через `aiosqlite`. Файл: `history.db`.

**Таблицы:**

#### `level_outcomes` — результаты мониторинга уровней

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | INTEGER PK | автоинкремент |
| `symbol` | TEXT | символ |
| `level` | REAL | цена уровня |
| `level_type` | TEXT | pump_base, body_level, etc. |
| `strength_claude` | INTEGER | сила по Claude (1-5) |
| `approach_type` | TEXT | support / resistance |
| `vol_ratio_on_approach` | REAL | объём при подходе |
| `touches_count` | INTEGER | число касаний |
| `result` | TEXT | "пробой" / "отбой" |
| `duration_minutes` | INTEGER | длительность мониторинга |
| `outcome` | TEXT | breakout / bounce / partial / no_reach |
| `approach_style` | TEXT | flash / bleed / impulse / unknown |
| `vol_ratio_at_touch` | REAL | объём в момент касания |
| `atr_ratio` | REAL | дистанция до уровня в ATR |
| `fill_depth_pct` | REAL | глубина проникновения в % |
| `btc_change_1m` | REAL | изменение BTC за 1М |
| `funding_rate` | REAL | funding rate на момент завершения |
| `created_at` | TIMESTAMP | время записи |

#### `symbol_profiles` — агрегированная статистика по символу

| Колонка | Тип | Описание |
|---------|-----|----------|
| `symbol` | TEXT PK | символ |
| `best_level_type` | TEXT | тип с лучшим success rate |
| `wick_success_rate` | REAL | % отбоев для wick_level |
| `body_success_rate` | REAL | % отбоев для body_level |
| `base_success_rate` | REAL | % отбоев для pump_base |
| `total_signals` | INTEGER | всего сигналов |
| `updated_at` | TIMESTAMP | время обновления |

#### `symbol_events` — журнал событий символа

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | INTEGER PK | автоинкремент |
| `symbol` | TEXT | символ |
| `event_type` | TEXT | added_screener, bounce, breakout, etc. |
| `details` | TEXT | произвольный текст / JSON |
| `created_at` | TIMESTAMP | время |

**event_type значения:**
- `added_screener` — монета добавлена автоскринером
- `added_manual` — монета добавлена вручную
- `levels_built` — уровни построены (details: JSON список)
- `monitoring_start` — мониторинг запущен
- `bounce` — отскок от уровня
- `breakout` — пробой уровня
- `zakol` — закол (прокол с возвратом)
- `zakol_deep` — глубокий закол (> 1%)
- `zakol_deep_retest` — глубокий закол с ретестом
- `near_miss` — цена не дошла до уровня (< 0.5%)
- `removed` — монета удалена

**Функции:**

| Функция | Описание |
|---------|----------|
| `init_db()` | Создаёт таблицы + добавляет новые колонки через ALTER TABLE |
| `log_event(symbol, event_type, details)` | Запись в symbol_events |
| `get_symbol_history(symbol, limit=50)` | Последние N событий символа |
| `save_level_outcome(...)` | Полная запись исхода мониторинга (17 полей) |
| `get_symbol_profile(symbol)` | Читает из symbol_profiles |
| `update_symbol_profile(symbol)` | Пересчёт success_rate по последним 50 сигналам |
| `get_outcome_probs(symbol, level_type, approach_style, n=30)` | Распределение вероятностей: no_reach / partial / bounce / breakout + avg_fill_depth |

---

### analysis/level_builder.py

**Главная функция:** `build_levels(symbol, c1m_override=None, c15m_override=None) -> list[dict]`

Принимает опциональные override-данные (для расширенной истории при `/analyze`).

**Полный алгоритм:**

```
1. _find_pump_legs(c15m)
   └─ ZigZag: поиск всех импульсных ног ≥ 3% в окне 50 свечей 15М
   └─ Фильтр: только ноги с ростом ≥ 5%
   └─ Дедупликация: если low ног в пределах 4% — оставить нижнюю
   └─ Возвращает: [(leg_low, leg_high, low_idx, high_idx), ...]

2. Для каждой pump_leg:
   └─ _find_pump_base_simple(c15m, leg_low, atr)
      └─ Основной pump_base: свечи с low в пределах ATR×0.3 от leg_low
      └─ Зона консолидации: свечи в пределах 10% выше leg_low, ≥ 3 шт.

3. _find_body_levels_simple(c15m, range_low, range_high, atr, cluster_radius)
   └─ Собирает все границы тел 15М свечей (open/close)
   └─ Кластеризация: группировка в пределах cluster_radius
   └─ Подсчёт candle_count: только уникальные свечи с касанием (low/high в радиусе)
   └─ Фильтр post-pump: если есть pump_peak_time — только свечи после него
   └─ Вес кластера: vol_weight (×5 если объём ≥ 2x avg) + tf_bonus + round_bonus

4. _find_wick_levels_simple(c15m, pump_high, atr)
   └─ Только LOW'ы свечей ПОСЛЕ pump_peak
   └─ Кластеризация: ≤ ATR×0.3
   └─ Минимум 2 касания

5. _find_order_block_simple(c15m, pump_low, pump_high)
   └─ Последняя красная свеча перед началом пампа
   └─ Уровень = min(open, close)

6. _deduplicate_simple(levels, radius)
   └─ Приоритет: pump_base (3) > order_block (2) > body_level (1) > wick_level (0)
   └─ При равном приоритете: больше candle_count побеждает

7. POC (Point of Control):
   └─ _calculate_poc_simple(c15m, range_low, range_high, atr)
   └─ Распределяет объём свечей по ценовым бинам (размер = ATR×0.2)
   └─ Находит бин с максимальным объёмом
   └─ Привязывает POC к ближайшему уровню (в пределах cluster_radius)
   └─ Если нет совпадения — добавляет POC как отдельный body_level
   └─ ТОЛЬКО ОДИН уровень может быть poc_aligned=True

8. Фильтр: только уровни в support_range (цена ×0.80 — цена ×1.05)

9. Top-7: сортировка по quality score:
   └─ poc_aligned: +10000
   └─ pump_base: +5000
   └─ candle_count × 10
   └─ hourly_open_bonus × 5
   └─ round_number_bonus × 3

10. _assign_positions(levels, pump_low, pump_high)
    └─ origin: нижние 30% pump range
    └─ impulse: 30%-70% pump range
    └─ mid_move: верхние 30%

11. _mark_clusters(levels)
    └─ Два соседних уровня с разницей < 1% → cluster=True
```

**Типы уровней:**

| Тип | Описание | Как находится |
|-----|----------|---------------|
| `pump_base` | База импульсного движения | Low ноги пампа + консолидация рядом |
| `body_level` | Кластер тел 15М свечей | Группировка open/close с весами |
| `wick_level` | Повторные LOW'ы после пика | Кластер low'ов после pump_peak |
| `order_block` | Последняя красная перед пампом | Одна свеча перед pump_start |

**Бонусы для уровней:**

| Бонус | Описание | Значения |
|-------|----------|----------|
| `hourly_open_bonus` | Таймфрейм открытия | 3=4h, 2=1h, 1=30m, 0=нет |
| `round_number_bonus` | Близость к круглому числу | 2=≤0.3%, 1=≤0.8%, 0=далеко |
| `poc_aligned` | Совпадение с POC | True / False |
| `volume_at_level` | Объём на уровне | float |

---

### analysis/trigger.py

**Триггер коррекции:**

```python
def check_trigger(symbol) -> bool:
    # 1. Последние 4 свечи 15М: high - open[0] >= 3%
    # 2. Последняя 1М свеча: close < open (красная)
```

**Определение "близких" уровней:**

```python
async def get_approaching_levels(symbol, use_claude=True) -> list[dict]:
    # 1. build_levels(symbol)
    # 2. find_real_level() — уточнение по кластеру касаний 15М (медиана ≥ 3)
    # 3. Фильтр: distance <= ATR × 0.5
    # 4. Для каждого: approach, vol_ratio, history (was_broken, sweep_reclaimed)
    # 5. zone_approaches — подходы к соседним уровням в радиусе atr_pct
    # 6. calculate_strength (Python) или calculate_strength_with_claude
```

**Расчёт силы (Python):**

```python
def calculate_strength(lvl) -> dict:
    # Мутирует и возвращает dict с добавленными strength (1-5) и verdict
```

Алгоритм:

```
Базовый strength по типу:
  pump_base = 5, consolidation_base = 4, body_level = 4,
  order_block = 4, consolidation = 3, wick_level = 2

Бонусы:
  + poc_aligned            → +2
  + hourly_open ≥ 2 (только pump_base/order_block/consolidation_base) → +1
  + round_number ≥ 2       → +1
  + candle_count 5-15       → +1
  + position = origin       → +1

Штрафы:
  - candle_count ≤ 2        → -1
  - approach ≥ 2            → strength=2, verdict=exit
  - cluster = true          → -1, если strength < 4 → verdict=exit
  - pump_vol_ratio < 1.5    → -1
  - was_broken && !sweep    → -2
  - max_vol > vol_ratio × 2 → -1
  - zone_approaches 1/2/3+  → -1/-2/-3, если ≥ 3 → verdict=exit
  - engulf_15m + vol > 2    → verdict=exit_fast

Clamp: strength = max(1, min(5, strength))
```

**Другие функции trigger.py:**

| Функция | Описание |
|---------|----------|
| `find_real_level(symbol, level)` | Уточняет уровень по кластеру касаний 15М после pump_peak (медиана ≥ 3). Возвращает `(adjusted_level, touch_count)` |
| `calculate_atr(symbol)` | ATR(14) на 1М: `sum(high-low for 14 candles) / 14` |
| `calculate_atr_pct(symbol)` | ATR как % от текущей цены |
| `_count_approaches(symbol, level, atr)` | Число подходов к уровню на 1М **после pump_peak** (зона = ATR×0.5, с гистерезисом) |
| `get_level_history(symbol, level, atr)` | `{was_broken, sweep_reclaimed, price_min_since_level, max_vol_on_approach}` |
| `get_breakout_info(symbol, level)` | Определяет тип пробоя: `"zakol"` (с глубиной и отскоком) или `"breakout"` |
| `detect_approach_style(symbol, n=5)` | Классификация подхода: `flash` (1 свеча ×2 vol + ≥0.5%), `impulse` (3+ зелёных → красная), `bleed` (4+ красных с растущим vol), `unknown` |
| `calculate_atr_ratio(symbol, level)` | Расстояние до уровня / ATR(14) |
| `get_vol_ratio_current(symbol)` | Объём последней 1М / MA20 |
| `_calc_vol_ratio(symbol)` | Текущий объём / среднее (avg_24h + avg_1h) / 2 |
| `get_btc_change_1m()` | % изменение BTC за последнюю 1М свечу |
| `get_funding_rate(symbol)` | Текущий funding rate с Binance API |

---

### analysis/monitor.py

**Главная функция:** `start_monitor(symbol, level, level_side, stop_event, approach_style, atr_ratio, vol_ratio) -> dict | str | None`

Бесконечный цикл с проверкой каждые 5 сек. Возвращает dict с `{reason, outcome, fill_depth_pct, approach_style, atr_ratio, vol_ratio_at_touch}`.

**Локальные флаги (антиспам):**

| Флаг | Назначение |
|------|------------|
| `touched` | Цена коснулась уровня |
| `approach_warned` | Давление уже отправлено |
| `weak_breakout_sent` | Слабый пробой уже отправлен |
| `rebound_sent` | Отскок уже отправлен |
| `volume_spike_notified` | Спайк объёма отправлен |
| `sweep_sent` | Sweep уже отправлен |
| `engulf_sent` | Engulfing отправлен |
| `level_broken_sent` | Level broken отправлен |
| `delta_signal_sent` | Delta reversal отправлен |

**Таблица событий и реакций:**

| # | Событие | Условие | Действие |
|---|---------|---------|----------|
| 1 | **Пробой (support)** | body_close < level + vol ≥ 2x + prev_close тоже ниже | `💥 пробой — настоящий, выход` → завершение |
| 2 | **Закол** | body_close < level + vol ≥ 2x + но prev_close выше | `⚠️ закол — ждём подтверждения` (cooldown 5 мин) |
| 3 | **Слабый пробой** | body_close < level + vol < 2x | `⚠️ слабый пробой — возможен sweep` (cooldown 5 мин) |
| 4 | **Касание** | last.low ≤ level × 1.002 | Включить delta tracking, записать touch_c1m_idx |
| 5 | **Delta reversal** | touched + buy_vol > sell_vol × 1.5 (30с, ≥ 10 trades) | `⚡ дельта разворот — покупатели поглощают` (однократно) |
| 6 | **Отскок** | touched + green candle + close > level + vol > avg | `✅ отбой подтверждён` (однократно) |
| 7 | **Classify touch** | 5 свечей после касания | `_classify_and_log_level_event()` → near_miss / bounce / zakol / zakol_deep |
| 8 | **Distance reset** | distance > 2×ATR | Сброс ВСЕХ флагов, classify если было касание |
| 9 | **Partial reset** | distance > 1×ATR | Сброс только `touched` |
| 10 | **Volume spike reset** | vol < 1.5x avg20 | Сброс `volume_spike_notified` |
| 11 | **Sweep + reclaim** | prev: low < level + close < level; curr: close > level + vol растёт | `🟡 sweep + выкуп` (или `sweep — объём слабый`) |
| 12 | **Давление** | 3+ красных + vol растёт, зона 0.2-1% от уровня | `🔴 давление` (если подтверждено 15М) или `⚠️ давление на 1М` |
| 13 | **Level broken** | 5 свечей подряд close < level + vol > avg | `🔴 промежуточный уровень пробит` (однократно) |

**Классификация касаний** (`_classify_and_log_level_event`):

| Категория | Условие | Сообщение |
|-----------|---------|-----------|
| `near_miss` | fill_depth < 0.1%, dist ≤ 0.5% | `⚠️ не дошёл до уровня` |
| `zakol` | fill_depth < 1%, вернулся выше | `🟢 закол — уровень держится` |
| `zakol_deep` | fill_depth ≥ 1%, вернулся, без ретеста | `🟡 глубокий закол — ослаблен` |
| `zakol_deep_retest` | fill_depth ≥ 1%, вернулся, ретест ≤ 0.3% | `🟡 глубокий закол — ретест подтверждён` |
| `bounce` | touched, returned | только лог (сообщение шлёт основной цикл) |

**Вспомогательные функции:**

| Функция | Описание |
|---------|----------|
| `_check_complications(...)` | Проверяет pressure и level_broken |
| `_check_volume_spike(c1m)` | Красная свеча + vol ≥ 3x avg60 |
| `_check_engulfing(c15m)` | Зелёная 15М поглощена красной |
| `_check_level_broken(c1m, level)` | 5 closes < level + последний vol > avg |
| `_check_sweep_reclaim(c1m, level, side)` | prev: пробой; curr: возврат + vol выше |
| `_check_volume_trend_approach(symbol, level, side)` | 3+ направленных свечей с растущим vol в зоне 0.2-1% |

---

### analysis/chart.py

`generate_chart(symbol, levels, broken_levels=None, c15m_override=None) -> bytes | None`

Генерирует PNG-график с тёмной темой (`#0d0d0d`).

**Компоненты графика:**
- **GridSpec 2×2**: свечи + Volume Profile сверху; объём снизу
- **50 последних 15М свечей** (дедупликация по `open_time`)
- **Candlesticks**: зелёные (`#26a69a`) / красные (`#ef5350`)
- **VWAP**: оранжевая пунктирная линия (`#ff9800`)
- **Volume Profile**: 100 ценовых зон, горизонтальные бары (зелёные buy / красные sell)
- **POC**: жёлтая точечная линия (`#ffeb3b`)
- **Уровни поддержки**: зелёные горизонтальные линии (strength ≥ 4, в пределах ylim)
- **Пробитые уровни**: красные (пробой) или зелёные (закол) пунктирные линии
- **Объём в USD**: bar chart внизу, форматирование $K / $M
- **Ось X**: время HH:MM каждые 5 свечей (UTC)

Размер: 14×8 дюймов, 100 DPI.

---

### analysis/chart_ascii.py

Генерирует текстовое представление графика для промпта Claude.

**`generate_ascii_chart(c15m, levels, poc_price, width=60, height=20, symbol="")`**:
- Последние 50 свечей 15М
- ASCII свечи: `█` (bullish), `▓` (bearish)
- Маркеры уровней справа: `← 0.028968 (pump_base) 🎯`
- Volume Profile: 15 бинов, бары из `▓`

**`generate_levels_summary(levels, poc_price, avg_volume=0)`**:
- Текстовое описание каждого уровня для Claude:
  - Type, Position, Touches, Volume (relative: high/normal/low × avg)
  - Timeframe alignment (4h/1h/30m/none)
  - Round number proximity
  - POC aligned: YES/no

---

### analysis/claude_strength.py

**`calculate_strength_with_claude(symbol, c15m, levels, poc_price=None) -> list[dict]`**

Отправляет ASCII-график + summary уровней в Claude Haiku. Claude возвращает JSON с оценкой силы каждого уровня.

**Промпт включает:**
- ASCII-график 15М свечей
- Summary уровней с метаданными
- Критерии силы 1-5 (5 = POC + много касаний + 4h open + круглое число)
- Правила: POC = обязательно 5 звёзд, уникальные причины, русский язык, кратко

**Формат ответа Claude:**
```json
{
  "levels": [
    {"price": 0.028968, "strength": 5, "reason": "POC с максимальным объемом"},
    {"price": 0.032952, "strength": 4, "reason": "Больше всего касаний (6)"}
  ]
}
```

**Логика обновления:**
- Сопоставление по цене с толерантностью 0.5%
- Записывает `strength`, `claude_reason`, `verdict="hold"`
- Fallback при ошибке: `strength=3`, `reason="Ошибка Claude: ..."`

---

### ai/claude_client.py

**Назначение:** интерпретация торговой статистики — генерация `reason`, `grid_advice`, `confidence` для команды `/check`.

**Отличие от claude_strength.py:** claude_strength оценивает силу уровня (1-5); claude_client генерирует текстовое описание и рекомендации по сетке ордеров.

**Системный промпт** (с `cache_control: ephemeral` для prompt caching):
```
Ты — интерпретатор торговой статистики для крипто-бота.
Токен In-Play: высокая волатильность, рост 30-200% в сутки.
Стратегия: лимитки по Мартингейлу, цель отскок 1-3% от уровня 15М.
Отвечай строго JSON. Без markdown. Без пояснений вне JSON.
```

**Входные данные user prompt:**
- Массив уровней с strength, verdict, type, position, approach, vol_ratio, etc.
- Статистика исходов по токену (если sample_size ≥ 10): no_reach, partial, bounce, breakout, avg_fill_depth
- Контекст подхода: approach_style, atr_ratio, vol_ratio

**Формат ответа:**
```json
[{
    "level": 0.014007,
    "reason": "Pump base origin, первый подход, объём падает",
    "grid_advice": "narrow",     // narrow / normal / wide
    "confidence": 0.85           // 0.0-1.0
}]
```

**`_parse_json_response(text)`**: парсинг с regex fallback (`\[.*\]`).

---

### bot/telegram.py

Telegram-бот на **aiogram v3** с FSM, inline-кнопками и Reply-клавиатурой.

**Авторизация:** все обработчики проверяют `message.chat.id == TELEGRAM_CHAT_ID`.

**Reply-клавиатура (постоянная):**

```
┌──────────────────┬──────────────┐
│ 📜 История       │ ➖ Убрать    │
├──────────────────┼──────────────┤
│ 📋 Список        │ 👁 Мониторинги │
├──────────────────┼──────────────┤
│ 🔍 Проверить     │ 🛑 Стоп      │
├──────────────────┼──────────────┤
│ 📊 Анализ        │ 📊 Рынок     │
└──────────────────┴──────────────┘
```

**FSM-состояния:**

| State Group | State | Назначение |
|-------------|-------|------------|
| `CheckLevel` | `waiting_for_symbol` | Ожидание выбора символа |
| | `waiting_for_level` | Ожидание ввода цены уровня |
| `StopMonitor` | `waiting_for_choice` | Ожидание выбора символа для остановки |
| `AnalyzeSymbol` | `waiting_for_choice` | Ожидание выбора символа для анализа |

**Команды и обработчики:**

| Команда / Кнопка | Действие |
|-------------------|----------|
| `/add SYMBOL` | Добавить монету в tokens.json + log_event("added_manual") |
| `/remove SYMBOL` | Удалить монету + очистить свечи + отменить мониторы |
| `/list` | Список монет с inline-кнопками для быстрого анализа |
| `/check SYMBOL LEVEL` | Оценить уровень: find_real_level → strength → Claude reason → мониторинг |
| `/monitors` | Активные мониторы с расстоянием до цены |
| `/stop SYMBOL` | Остановить все мониторы символа |
| `/analyze SYMBOL` | Полный анализ: build_levels → strength → Claude → график → мониторинг |
| **📜 История** | Inline-кнопки: выбор монеты → 20 последних events из symbol_events |
| **➖ Убрать** | Inline-кнопки: выбор монеты для удаления |
| **📋 Список** | Монеты с inline-кнопками `📊 {SHORT}` для быстрого `/analyze` |
| **👁 Мониторинги** | Все активные мониторы: symbol @ level — цена (distance%) |
| **🔍 Проверить** | Inline-кнопки монет → inline-кнопки уровней из кэша → или ручной ввод |
| **🛑 Стоп** | Inline-кнопки: символы с активными мониторами + "Остановить все" |
| **📊 Анализ** | Inline-кнопки монет → полный `/analyze` |
| **📊 Рынок** | Скринер: таблица TICKER / CHG% / NATR / VOL + inline-кнопки для анализа |

**Кэш анализа:**
- `_last_analysis_cache: dict[str, list[dict]]` — результаты последнего `/analyze` по символу
- Используется в `/check` для быстрого выбора уровня из списка
- Используется в `_start_next_level_after_breakout` для поиска следующего уровня
- Используется в `_proximity_loop` для отслеживания слабых (немониторируемых) уровней

**Антидубль анализа:** `_analyzing: set[str]` — не позволяет запустить два `/analyze` одновременно.

**`_do_analyze(message, symbol)`** — полный процесс:
1. Загрузка расширенной истории: 1000 свечей 1М + 500 свечей 15М
2. `build_levels()` с расширенными данными
3. Фильтр: только supports (< текущей цены), в диапазоне 20%, дальше 1.5 ATR
4. Python `calculate_strength()` → сохранить как `python_strength`
5. Claude `calculate_strength_with_claude()` (если `CLAUDE_STRENGTH_ENABLED`)
6. **Cap:** Claude не может дать выше Python, если approach ≥ 2 или was_broken && !sweep
7. Разделение: strong (≥ 4) и weak (< 4)
8. Telegram: текст с уровнями + звёздами + claude_reason
9. PNG-график: `generate_chart()`
10. Auto-start мониторинга для ближайшего сильного уровня
11. Сохранение в `_last_analysis_cache`

**`_do_check(message, symbol, level)`** — оценка конкретного уровня:
1. `find_real_level()` — уточнение по кластеру
2. Подсказка ближайшего уровня (в пределах 2%)
3. Python `calculate_strength()`
4. Claude `calculate_strength_with_claude()` (с cap)
5. Claude `analyze_levels()` — reason + grid_advice + confidence
6. Telegram: полный отчёт + статистика исходов + сетка + уверенность
7. Если strength ≥ 4 → запуск мониторинга

**`send_message(text)`:** отправка с обрезкой до 4096 символов + reply_markup.

---

### main.py — оркестратор

**Глобальное состояние:**

| Переменная | Тип | Описание |
|------------|-----|----------|
| `claude_semaphore` | `Semaphore(2)` | Лимит параллельных запросов к Claude |
| `_building_levels` | `set[str]` | Символы, для которых сейчас строятся уровни |

**Запуск (`asyncio.gather`):**

```python
async def main():
    validate_config()
    init_db()
    
    await asyncio.gather(
        start_collector(),          # Сбор данных Binance (бесконечный)
        start_bot(),                # Telegram polling (бесконечный)
        _trigger_loop(),            # Проверка триггеров каждые 5 сек
        _proximity_loop(),          # Proximity alerts каждые 5 сек
        _startup_monitoring(),      # Однократно: build levels для всех монет при старте
    )
```

**`_trigger_loop()`** — каждые 5 сек:
1. Для каждого символа из tokens.json:
   - Пропустить если phase1 или _building_levels
   - Проверить cooldown (1 час)
   - `check_trigger(symbol)` → если True: сохранить время, `_run_phase1(symbol)`

**`_run_phase1(symbol)`**:
1. `get_approaching_levels(symbol, use_claude=False)` — только Python
2. Если был phase2: снять стухшие мониторы (уровни вышли из диапазона -20%)
3. Отфильтровать уже оценённые уровни
4. `calculate_strength()` для новых
5. Для strength ≥ 4: отправить Telegram, запустить `_monitored()`

**`_monitored(symbol, level, level_side, ...)`** — обёртка над `start_monitor()`:
1. Запускает `start_monitor()` до завершения
2. Записывает результат: `save_level_outcome()`, `update_symbol_profile()`, `log_event()`
3. При breakout: `state.analyzed_levels.discard()` (разрешить переоценку)
4. Получает BTC change и funding rate для записи
5. `finally`: `state.remove_task()`, если breakout — `_start_next_level_after_breakout()`

**`_start_next_level_after_breakout(symbol, broken_level)`**:
1. **Приоритет 1:** уровни из `_last_analysis_cache` (сохранённые при /analyze)
   - Фильтр: ниже пробитого, strength ≥ 4, в диапазоне 20%, дальше 1.5 ATR
2. **Приоритет 2:** rebuild levels из текущих свечей
3. **Нет уровней:** проверить скринер → если символ выпал → удалить из tokens.json

**`_proximity_loop()`** — каждые 5 сек:
1. Для всех активных мониторов: если distance ≤ 0.4% и approaching → `🎯 готовь ордер`
2. Для слабых уровней из кэша (strength < 4, не мониторятся):
   - Отслеживание касаний через `proximity_notified` dict
   - При уходе цены: classify → log_event (zakol / bounce / breakout)
   - Reset при distance > 2×ATR

**`_send_startup_screener()`** — через 15 сек после старта:
- Сканирует все фьючерсы Binance
- Фильтр: USDT, vol > 40M, рост > 10%, NATR(5M) > 2.0
- Отправляет таблицу в Telegram

**`_auto_screener_loop()`** — каждые 30 мин (пропуская первый раз):
- Сканирует рынок
- Новые символы: auto-add → fetch history → build_levels → Claude strength → мониторинг
- Telegram: `🆕 SYMBOL добавлен автоматически`

**`_startup_monitoring()`** — однократно через 30 сек после старта:
- Для каждого символа в tokens.json:
  - Загрузка расширенной истории (500 15М + 300 1М)
  - `build_levels()` → `calculate_strength()` (Python only, без Claude)
  - Для ближайшего strong (≥ 4): запуск мониторинга
  - Лог: `levels_built`, `monitoring_start (startup)`

**Вспомогательные:**

| Функция | Описание |
|---------|----------|
| `load_trigger_times()` | Читает `trigger_times.json` |
| `save_trigger_times(data)` | Пишет `trigger_times.json` |
| `_format_vol(v)` | `1234567 → "1M"`, `1234567890 → "1.2B"` |
| `_run_screener()` | Сканирование рынка → list[(ticker, chg, natr, vol, symbol)] |
| `cancel_tasks_for_symbol(symbol)` | Отменяет все задачи символа |
| `clear_analysis_cache(symbol)` | Очищает analyzed_levels |
| `shutdown()` | Graceful shutdown: cancel all → wait → send "🛑 Бот остановлен" |

---

## Система оценки силы уровней

Два независимых метода оценки:

### 1. Python (`trigger.py :: calculate_strength`)

Детерминистический расчёт по формуле (см. секцию [trigger.py](#analysistriggerpy)).
Используется при:
- Автоматических триггерах (_run_phase1)
- Стартовом мониторинге (_startup_monitoring)
- Fallback при ошибке Claude

### 2. Claude Haiku (`claude_strength.py :: calculate_strength_with_claude`)

Анализ ASCII-графика и метаданных уровней.
Используется при:
- `/analyze SYMBOL` (ручной анализ)
- `/check SYMBOL LEVEL` (ручная проверка)
- Auto-screener (новые символы)

### Взаимодействие Python и Claude

```
Python calculate_strength()
          │
          ▼
    python_strength (сохраняется)
          │
          ▼
Claude calculate_strength_with_claude()
          │
          ▼
    claude_strength (новое значение)
          │
          ▼
    CAP: если approach ≥ 2 или (was_broken && !sweep):
         strength = min(claude_strength, python_strength)
```

Python выступает как **верхняя граница** для проблемных уровней — Claude не может завысить оценку, если уровень уже тестировался или был пробит.

---

## Алгоритм построения уровней

Визуальная схема:

```
15М свечи (300 шт)
        │
        ▼
   _find_pump_legs()         ← ZigZag: все ноги ≥ 3%, оставить ≥ 5%
        │
   legs = [(low, high, idx_lo, idx_hi), ...]
        │
   ┌────┴────────────────────────────────────┐
   │    Для каждой ноги:                     │
   │    _find_pump_base_simple(leg_low)      │
   │        → pump_base уровни               │
   └─────────────────────────────────────────┘
        │
   _find_body_levels_simple(range -20%..+5%)
        │ → кластеризация тел 15М свечей
        │
   _find_wick_levels_simple(pump_high)
        │ → кластеризация low'ов после пика
        │
   _find_order_block_simple(pump_low, pump_high)
        │ → последняя красная перед пампом
        │
   _deduplicate_simple(radius)
        │ → pump_base > order_block > body_level > wick_level
        │
   _calculate_poc_simple()
        │ → Price с максимальным объёмом
        │ → Привязка к ближайшему уровню или добавление нового
        │
   Фильтр: support_range (цена ×0.80 .. ×1.05)
        │
   Top-7 по quality score
        │
   _assign_positions(origin / impulse / mid_move)
        │
   _mark_clusters(< 1% между уровнями)
        │
        ▼
   list[dict] — готовые уровни
```

---

## Мониторинг уровней — события и реакции

```
                        Цена приближается к уровню
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
              distance ≤ 0.4%               distance > 0.4%
              (proximity_loop)              (ждём)
                    │
              🎯 "готовь ордер"
                    │
              ┌─────┴─────┐
              │           │
         Касание        Нет касания
         touched=true       │
              │         (near_miss при 0.5%)
              │
    ┌─────────┼──────────────────┐
    │         │                  │
  Delta     Отскок            Пробой
  tracking  (green +          body < level
  start     close > level)         │
    │         │             ┌──────┼──────┐
    │    ✅ "отбой"        vol ≥ 2x    vol < 2x
    │         │              │          │
    │    classify:      2 closes    ⚠️ "слабый
    │    bounce/zakol    below?      пробой"
    │                      │
    │              ┌───────┼────────┐
    │            ДА: 💥            НЕТ:
    │            "настоящий        ⚠️ "закол —
    │             пробой"          ждём
    │             → EXIT           подтверждения"
    │
    ▼
  ⚡ delta reversal
  (buy_vol > sell_vol × 1.5)
```

---

## Защита от спама

| Механизм | Где применяется | Как работает |
|----------|-----------------|--------------|
| **Одноразовые флаги** | rebound, engulfing, level_broken, volume_spike, delta | `bool`, сбрасывается при distance > 2×ATR |
| **Timestamp cooldown** | weak_breakout (5 мин), proximity (10 мин) | `time.time()` сравнение |
| **ATR-based reset** | Все флаги монитора | distance > 2×ATR → полный сброс; > 1×ATR → частичный |
| **Volume normalization** | volume_spike | vol < ×1.5 avg → сброс |
| **analyzed_levels set** | Уровни в phase1 | `set[str]`, не переоценивает повторно. Очищается при breakout. |
| **claude_semaphore(2)** | Claude API | Не более 2 параллельных запросов |
| **trigger cooldown (1 ч)** | _trigger_loop | `trigger_times.json` |
| **_analyzing set** | /analyze | Не запускать два анализа для одного символа |
| **_building_levels set** | _run_phase1 | Не строить уровни параллельно для одного символа |
| **Task dedup (0.5%)** | SymbolState.add_task | Отменяет существующий монитор если новый уровень в пределах 0.5% |

---

## Скринер рынка

### Стартовый скринер (`_send_startup_screener`)
- Запускается через 15 сек после старта
- Однократный

### Автоматический скринер (`_auto_screener_loop`)
- Каждые 30 минут (первый запуск пропускается)
- Новые символы автоматически:
  1. Добавляются в tokens.json
  2. Загружают историю свечей
  3. Строят уровни
  4. Оценивают через Claude
  5. Запускают мониторинг ближайшего сильного
  6. Отправляют уведомление в Telegram

### Ручной скринер (кнопка 📊 Рынок)
- По запросу пользователя
- Таблица + inline-кнопки для анализа каждого символа

### Фильтры скринера

```python
# Все фьючерсы Binance
# Фильтр 1: symbol.endswith("USDT")
# Фильтр 2: quoteVolume > 40_000_000
# Фильтр 3: priceChangePercent > 10.0
# Фильтр 4: NATR(5M, 14) > 2.0
# Сортировка: по росту (descending)
```

---

## База данных — схема и назначение

```sql
-- Исходы мониторинга (для обучения и статистики)
level_outcomes (
    id, symbol, level, level_type, strength_claude,
    approach_type, vol_ratio_on_approach, touches_count,
    result, duration_minutes,
    outcome,            -- breakout/bounce/partial/no_reach
    approach_style,     -- flash/bleed/impulse/unknown
    vol_ratio_at_touch, atr_ratio, fill_depth_pct,
    btc_change_1m, funding_rate,
    created_at
)

-- Агрегаты по символу (пересчитываются после каждого исхода)
symbol_profiles (
    symbol PK, best_level_type,
    wick_success_rate, body_success_rate, base_success_rate,
    total_signals, updated_at
)

-- Журнал событий (для команды 📜 История)
symbol_events (
    id, symbol, event_type, details, created_at
)
```

**Связь с Claude:** `get_outcome_probs()` передаёт статистику в промпт Claude для `reason` и `confidence`.

---

## Telegram-бот — команды и интерфейс

### Полный список обработчиков

| Тип | Trigger | Handler | Описание |
|-----|---------|---------|----------|
| Button | 📜 История | `btn_history` | Inline выбор монеты → 20 events |
| Callback | `history:SYMBOL` | `cb_history` | Показ истории |
| Button | ➖ Убрать | `btn_remove` | Inline выбор → удаление |
| Callback | `remove:SYMBOL` | `cb_remove` | Удаление + cleanup |
| Button | 📋 Список | `btn_list` | Монеты + inline для анализа |
| Button | 👁 Мониторинги | `btn_monitors` | Все активные мониторы |
| Button | 🔍 Проверить | `btn_check` | Inline монеты → FSM level |
| Callback | `check:SYMBOL` | `cb_check_symbol` | Показ уровней из кэша |
| Callback | `checklvl:SYM:LEVEL` | `cb_check_level_from_cache` | Quick check из кэша |
| Callback | `checkmanual:SYMBOL` | `cb_check_manual` | Ручной ввод уровня |
| FSM | CheckLevel.waiting_for_level | `btn_check_level` | Парсинг float → _do_check |
| Button | 🛑 Стоп | `btn_stop` | Inline выбор + "Остановить все" |
| Callback | `stop:SYMBOL` | `cb_stop` | Остановка мониторов |
| Button | 📊 Анализ | `btn_analyze` | Inline выбор монеты |
| Callback | `analyze:SYMBOL` | `cb_analyze` | Полный _do_analyze |
| Button | 📊 Рынок | `btn_market` | Скринер + inline для анализа |
| Command | `/add SYMBOL` | `cmd_add` | Добавить монету |
| Command | `/remove SYMBOL` | `cmd_remove` | Удалить монету |
| Command | `/list` | `cmd_list` | Текстовый список |
| Command | `/check SYMBOL LEVEL` | `cmd_check` | _do_check напрямую |
| Command | `/monitors` | `cmd_monitors` | Текстовый список мониторов |
| Command | `/stop SYMBOL` | `cmd_stop` | Остановка |
| Command | `/analyze SYMBOL` | `cmd_analyze` | _do_analyze напрямую |

---

## Жизненный цикл символа

```
     /add SYMBOL          Скринер находит
     (ручной)             (автоматический)
          │                      │
          ▼                      ▼
    token_registry.add()    token_registry.add()
    log_event("added_manual")    log_event("added_screener")
          │                      │
          ▼                      ▼
    collector: _fetch_history()
    candles_1m + candles_15m заполнены
          │
          ├──── /analyze → build_levels → Claude → chart → мониторинг
          │
          ├──── trigger_loop → check_trigger → phase1 → мониторинг
          │
          ├──── auto_screener → build_levels → Claude → мониторинг
          │
          ▼
    Мониторинг (phase2)
          │
          ├──── breakout → save_outcome → next_level_after_breakout
          │                                    │
          │                              ┌─────┴────────┐
          │                           Нашёл           Не нашёл
          │                           следующий       следующий
          │                           уровень              │
          │                              │          ┌──────┴──────┐
          │                           phase2      В скринере?  Не в скринере
          │                                           │              │
          │                                        Ждём         remove
          │                                                    log("removed")
          │
          ├──── bounce → save_outcome → продолжить мониторинг
          │
          ├──── /stop → cancel_all_tasks → idle
          │
          └──── /remove → cancel_all + remove from tokens.json + cleanup
```

---

## Запуск и остановка

### Установка

```bash
pip install python-binance anthropic aiogram python-dotenv aiosqlite loguru numpy matplotlib
```

### .env

```
CLAUDE_API_KEY=sk-ant-...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Запуск

```bash
python main.py
```

### Что происходит при запуске

1. `validate_config()` — проверка ключей
2. `init_db()` — создание/миграция таблиц SQLite
3. Параллельный запуск 5 задач:
   - `start_collector()` — загрузка 300 свечей для каждого символа, потом update каждые 5 сек
   - `start_bot()` — Telegram polling + отправка "Бот запущен"
   - `_trigger_loop()` — проверка триггеров каждые 5 сек
   - `_proximity_loop()` — proximity alerts каждые 5 сек
   - `_startup_monitoring()` — через 30 сек: build_levels + мониторинг для всех монет

### Остановка

- **SIGINT / SIGTERM** (Linux/Mac): `shutdown()` → cancel all tasks → "🛑 Бот остановлен"
- **Ctrl+C** (Windows): `KeyboardInterrupt` → логирование
- Windows не поддерживает `add_signal_handler` — graceful shutdown через try/except

---

## Дополнительные файлы

| Файл | Назначение |
|------|------------|
| `trigger_times.json` | Персистентный cooldown триггеров: `{symbol: unix_timestamp}` |
| `tokens.json` | Активные монеты: `["BUSDT", "TRUTHUSDT", ...]` |
| `history.db` | SQLite: outcomes, profiles, events |
| `debug_gtc.py` | Отладочный скрипт |
| `test_claude_strength.py` | Тест Claude strength |
| `analysis/level_builder_backup.py` | Бэкап старого level_builder |
| `analysis/level_builder_simple.py` | Упрощённая версия level_builder |
