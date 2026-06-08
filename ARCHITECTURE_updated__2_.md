# Архитектура Trading Bot — Полная документация

Торговый бот для мониторинга уровней поддержки/сопротивления на фьючерсах Binance.
Стратегия: поиск уровней после пампов, многоуровневая оценка силы, мониторинг подхода цены,
автоматическое paper-trading через три стратегии, алерты в Telegram.

---

## Оглавление

1. [Структура файлов](#структура-файлов)
2. [Стек технологий и зависимости](#стек-технологий-и-зависимости)
3. [Конфигурация и переменные окружения](#конфигурация-и-переменные-окружения)
4. [Модели данных](#модели-данных)
5. [Поток данных — общая схема](#поток-данных--общая-схема)
6. [Система фаз](#система-фаз)
7. [Алгоритм построения уровней](#алгоритм-построения-уровней)
8. [Порядок калибровки и уточнения уровней](#порядок-калибровки-и-уточнения-уровней)
9. [Система оценки силы уровней](#система-оценки-силы-уровней)
10. [Торговые стратегии](#торговые-стратегии)
11. [Мониторинг уровней — события и реакции](#мониторинг-уровней--события-и-реакции)
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
├── .env.example                  # Шаблон переменных окружения
├── tokens.json                   # ["BUSDT", ...] — список активных монет
├── trigger_times.json            # {symbol: unix_timestamp} — cooldown триггеров
├── active_monitors.json          # [{symbol, level}, ...] — восстановление после рестарта
├── history.db                    # SQLite — исходы уровней, профили, события
├── trades.db                     # SQLite — paper-trading сделки всех стратегий
│
├── config.py                     # .env, TokenRegistry (CRUD tokens.json)
├── constants.py                  # Все числовые пороги (в т.ч. параметры стратегий)
├── logger.py                     # Loguru: консоль (INFO) + файл (DEBUG, ротация)
├── models.py                     # Dataclass-модели: SymbolState, LevelData, StateManager
├── main.py                       # Оркестратор: запуск, фазы, скринер, proximity
│
├── data/
│   ├── collector.py              # Свечи 1М/15М + aggTrades delta
│   └── history.py                # SQLite CRUD: outcomes, profiles, events
│
├── analysis/
│   ├── level_builder.py          # Построение уровней: pump_base, body, wick, order_block, breakout_level, mid_impulse_pause
│   ├── trigger.py                # Триггер коррекции + calculate_strength (Python)
│   ├── monitor.py                # Мониторинг: пробой/отскок/sweep/давление
│   ├── screener.py               # Скринер: run_screener() + фильтры
│   ├── chart.py                  # PNG-график: свечи + VWAP + Volume Profile
│   ├── chart_ascii.py            # ASCII-график для промпта Claude
│   ├── ml_score.py               # ML-оценка: p_bounce, expected_depth, ml_delta (±1 к strength)
│   ├── claude_strength.py        # Claude Haiku: оценка силы по ASCII-графику
│   └── ml/                       # Обученные sklearn-модели (pickle)
│       ├── clf.pkl               # RandomForest классификатор (bounce/breakout)
│       ├── reg.pkl               # Регрессор глубины пробоя
│       ├── label_encoder.pkl     # LabelEncoder классов исхода
│       ├── level_type_map.pkl    # Маппинг типов уровней → int
│       ├── last_train_size.txt   # Кол-во записей при последнем обучении
│       └── thresholds.json       # Пороги p_bounce из последнего обучения
│
├── ai/
│   └── claude_client.py          # Claude Haiku: reason + grid_advice + confidence
│
├── trading/
│   ├── base_strategy.py          # Базовый класс BaseStrategy
│   ├── strategy1_bounce.py       # Стратегия 1: Bounce — вход по подтверждённому отбою
│   ├── strategy2_limit_grid.py   # Стратегия 2: Limit Grid — 5 лимитных ордеров в зоне уровня
│   ├── strategy3_breakout.py     # Стратегия 3: Breakout Momentum — short при пробое
│   ├── strategy_runner.py        # Запускает все стратегии, слушает event bus
│   ├── event_bus.py              # asyncio.Queue: monitor → стратегии
│   └── trade_log.py              # SQLite CRUD для trades.db
│
├── bot/
│   └── telegram.py               # Telegram-бот (aiogram v3): команды, кнопки, FSM
│
└── logs/
    └── bot_YYYY-MM-DD.log        # Дневные логи (ротация 1 день, хранение 7 дней)
```

---

## Стек технологий и зависимости

| Пакет | Назначение |
|-------|------------|
| `python-binance` | Binance Futures API (REST + WebSocket) |
| `anthropic` | Claude API (Haiku 4.5) |
| `aiogram` v3 | Telegram Bot Framework |
| `python-dotenv` | Загрузка `.env` |
| `aiosqlite` | Async SQLite |
| `loguru` | Структурированный логгинг |
| `numpy` | Расчёты для графиков |
| `matplotlib` | Генерация PNG-графиков |
| `scikit-learn` | ML-модели (RandomForest + LabelEncoder) |
| `aiohttp` + `aiohttp-socks` | HTTP/SOCKS5 для proxy |

Всё работает на **asyncio** — один event loop для всех компонентов.

---

## Конфигурация и переменные окружения

### constants.py — все пороги стратегий

| Группа | Константа | Значение | Описание |
|--------|-----------|----------|----------|
| **Триггер** | `TRIGGER_GROWTH_THRESHOLD` | 0.03 (3%) | Рост на 15М для активации |
| | `TRIGGER_COOLDOWN_SECONDS` | 3600 | Cooldown между триггерами одного символа |
| **Уровни** | `LEVEL_APPROACH_THRESHOLD` | 0.5 | ATR × 0.5 = «уровень близко» |
| | `LEVEL_CLUSTER_RADIUS_PCT` | 0.01 | Радиус кластера уровней |
| **Мониторинг** | `PROXIMITY_ALERT_DISTANCE_PCT` | 0.02 | Расстояние для proximity alert |
| | `VOLUME_BREAKOUT_RATIO` | 2.4 | ×2.4 для подтверждения пробоя |
| **Стратегии** | `STRATEGY_POSITION_SIZE_USDT` | 100.0 | Размер позиции |
| | `STRATEGY_MAX_OPEN_TRADES` | 3 | Макс. одновременных сделок на стратегию |
| | `STRATEGY_TRADE_TIMEOUT_MINUTES` | 60.0 | Таймаут позиции |
| **S1 (Bounce)** | `S1_MIN_STRENGTH` | 4 | Мин. сила уровня для входа |
| | `S1_MIN_P_BOUNCE` | 0.70 | Мин. ML-вероятность отбоя |
| | `S1_MAX_VOL_RATIO` | 1.2 | Макс. объём при касании (только тихие отбои) |
| | `S1_TP1_RR` | 1.5 | Risk:Reward для первого TP |
| | `S1_TP2_RR` | 3.0 | Risk:Reward для второго TP |
| **S2 (Grid)** | `S2_MIN_STRENGTH` | 3 | Мин. сила уровня |
| | `S2_MIN_P_BOUNCE` | 0.60 | Мин. ML-вероятность отбоя |
| | `S2_PRESSURE_COOLDOWN_SECONDS` | 300 | Пауза после события давления |
| | `S2_GRID_ORDERS` | 5 | Число ордеров в сетке |
| **S3 (Breakout)** | `S3_MIN_BREAKOUT_VOL_RATIO` | 2.4 | Мин. объём для входа в шорт |
| | `S3_MIN_BREAKOUT_VOL_RATIO_STRONG` | 3.5 | Порог «сильного» пробоя |
| | `S3_SWEEP_COOLDOWN_SECONDS` | 120 | Пауза после sweep |
| | `S3_TP1_ATR_MULT` | 2.0 | TP1 в ATR для шорта |
| | `S3_TP2_ATR_MULT` | 4.0 | TP2 в ATR для шорта |
| | `S3_SL_ATR_MULT` | 0.5 | SL в ATR для шорта |
| | `S3_MIN_TRADE_DURATION_MINUTES` | 5.0 | Мин. время до SL/TP |

---

## Модели данных

### `SymbolState` — состояние одного символа

```python
@dataclass
class SymbolState:
    symbol: str
    phase: "idle" | "phase1" | "phase2" = "idle"
    tasks: dict[str, asyncio.Task]
    stop_flags: dict[str, asyncio.Event]
    last_trigger_time: float = 0.0
    proximity_notified: dict[str, float]
    analyzed_levels: set[str]
```

### `LevelData` — структура уровня

```python
@dataclass
class LevelData:
    level: float
    type: str          # pump_base, body_level, wick_level, order_block,
                       # breakout_level, mid_impulse_pause, consolidation_base
    symbol: str
    level_side: "support" | "resistance"
    strength: int = 0  # 1-5
    verdict: "hold" | "exit" | "exit_fast" = "hold"
    reason: str = ""
    approach: int = 0
    vol_ratio: float = 1.0
    atr_pct: float = 0.0
    zone_approaches: int = 0
    position: str = "mid_move"   # origin, impulse, mid_move
    cluster: bool = False
    pump_volume_ratio: float = 1.5
    was_broken: bool = False
    sweep_reclaimed: bool = False
    price_min_since_level: float = 0.0
    max_vol_on_approach: float = 0.0
    engulf_15m: bool = False
    p_bounce: float = 0.0        # ML: вероятность отбоя
    expected_depth: float = 0.0  # ML: ожидаемая глубина пробоя в %
```

---

## Алгоритм построения уровней

**Файл:** `analysis/level_builder.py`  
**Точка входа:** `build_levels(symbol, c1m_override=None, c15m_override=None) -> list[dict]`

### Шаг 1 — Поиск импульсных ног (_find_pump_legs)

```
1. Найти peak price в последних 72 свечах 15М
   Если peak > 30% выше текущей цены → расширить окно до 200 свечей
2. Назад от peak: найти low с движением ≥ 3% (2 прохода: ≥5%/100 и ≥3%/200 свечей)
3. Построить ZigZag между pump_start и peak:
   MIN_LEG_PCT = 3%, MIN_REVERSAL_PCT = 2%
4. Фильтр: оставить ноги с ростом ≥ 5%
5. Дедупликация: ноги с low в пределах 4% — оставить нижнюю
6. Возвращает: [(leg_low, leg_high, low_idx, high_idx), ...]
```

**Если ног не найдено** → Fallback `_build_levels_no_pump()`:
- Consolidation zones из 15М
- Body levels без фильтра post-pump
- 1М near-zone levels (20% зона вокруг цены)

### Шаг 2 — Pump Base (_find_pump_base_simple)

Для каждой ноги:
```
search_radius = atr_15m × 0.3   (или atr_1m × 1.5 если нет 15М ATR)

pump_base_1: свечи с low в пределах search_radius от leg_low
pump_base_2 (консолидация): свечи open/close в пределах 10% выше leg_low
  → если медиана > search_radius×1.5 от leg_low → добавить как отдельный уровень
```

### Шаг 3 — Breakout Level (_find_breakout_level)

```
Потолок консолидации ДО пампа = уровень, с которого цена запрыгнула.
После пампа это ГЛАВНАЯ поддержка.

1. От pump_start_idx сканируем вперёд пока не найдём «взрывную» свечу
   (тело ≥ 1.5 × ATR) — это начало самого пампа
2. ceiling = max(open, close) для всех свечей консолидации
3. Санити-чек: ceiling < pump_high × 0.97
```

### Шаг 4 — Body Levels (_find_body_levels_simple)

```
Диапазон: current_price × 0.60 .. current_price × 1.05

1. Собрать все границы тел 15М (open, close) в диапазоне
2. Веса:
   - Pre-pump свечи: vol_weight = 1
   - Post-pump свечи: vol_weight = 3 (или 5 если объём ≥ 2× avg)
   - + tf_bonus (4h=3, 1h=2, 30m=1)
3. Кластеризация в пределах cluster_radius
   cluster_radius = min(atr_15m × 0.3, current_price × 0.005)
4. Фильтр: cluster_weight ≥ 6
5. Post-pump фильтр: хотя бы одна свеча кластера должна быть после pump_peak
   Иначе — skip (чистые pre-pump body не имеют post-pump подтверждения)
```

### Шаг 5 — Wick Levels (_find_wick_levels_simple)

```
Только LOW свечей ПОСЛЕ pump_peak
Кластеризация: ≤ cluster_radius
Минимум 2 касания
```

### Шаг 6 — Order Block (_find_order_block_simple)

```
Последняя МЕДВЕЖЬЯ свеча в 5 свечах ДО pump_start
Уровень = min(open, close)
```

### Шаг 7 — Mid-Impulse Pauses (_find_mid_impulse_pauses)

```
Короткие паузы ВНУТРИ импульсных ног (2-4 свечи).
Ищем в средних 55% диапазона ноги (skip нижние 30% и верхние 15%).

Критерии окна (размер 2-4 свечи):
  - spread (max_body_top - min_body_bot) < atr_15m × 1.5
  - как минимум 2 close в пределах atr_15m × 0.5 друг от друга
  → пауза = median body_midpoints окна
```

### Шаг 8 — 1M Near-Zone Levels (_find_1m_near_zone_levels)

```
Зона: current_price ± 20%
radius = max(atr × 0.5, current_price × 0.001)

Собирает body touch points 1М свечей.
Минимум 2 уникальные свечи в кластере.
Используется как дополнение к 15М уровням — ловит паузы внутри одной большой 15М свечи.
```

### Шаг 9 — Консолидационные зоны (_find_consolidation_zones)

```
Окно 8 свечей, шаг 4 (50% перекрытие).
cluster_range < local_atr × 3  → это узкая консолидация
Минимум 4 свечи close в пределах price-relative radius (0.5% от цены)
```

### Шаг 10 — POC (_calculate_poc_simple)

```
Диапазон: post-pump зона (от pump_peak до конца или 48 свечей)
Бины: ATR × 0.2

Объём распределяется по телу свечи (open-close), не по всему wick-диапазону.
Для doji fallback: wick range.

POC = бин с максимальным объёмом.
```

### Шаг 11 — Дедупликация (_deduplicate_simple)

```
Сортировка по цене.
Если два соседних уровня в пределах cluster_radius:
  Приоритет: pump_base=3, breakout_level=3, order_block=2, consolidation_base=2,
             body_level=1, mid_impulse_pause=1, wick_level=0
  Равный приоритет → больше candle_count побеждает
  pump_base и breakout_level никогда не вытесняются более низким типом
```

### Шаг 12 — POC alignment

```
1. Если уровень в пределах cluster_radius от POC → poc_aligned=True
2. Если ничего → snap к ближайшему в пределах cluster_radius × 2
3. Если ничего → добавить POC как отдельный body_level (если ≥2 свечей рядом)
Только ОДИН уровень может быть poc_aligned=True
```

### Шаг 13 — Отбор Top-10 по quality score

```
Оценка каждого уровня:
  poc_aligned       → +10000
  pump_base (top-2 ближайших к цене) → +5000
  mid_impulse_pause → +1500
  proximity bonus   → до +100 (чем ближе к цене, тем больше)
  candle_count × 10
  hourly_open_bonus × 5
  round_number_bonus × 3

Сортировка по убыванию, берём top-10.
```

### Шаг 14 — Финальная разметка

```
_assign_positions: origin (нижние 30%), impulse (30-70%), mid_move (верхние 30%)
_mark_clusters: соседние уровни с разницей < 1% → cluster=True у слабейшего
```

### Типы уровней

| Тип | Описание | Откуда |
|-----|----------|--------|
| `pump_base` | База импульсного движения | Low ноги + консолидация рядом |
| `breakout_level` | Потолок консолидации до пампа = уровень запуска | Ceiling pre-pump range |
| `body_level` | Кластер тел 15М свечей | open/close кластеры с весами |
| `wick_level` | Повторные LOW'ы после пика | Кластер low после pump_peak |
| `order_block` | Последняя красная перед пампом | 1 свеча до pump_start |
| `mid_impulse_pause` | Пауза внутри импульсной ноги | 2-4 свечи с tight body cluster |
| `consolidation_base` | Зона консолидации | Окно 8 свечей с узким диапазоном |

---

## Порядок калибровки и уточнения уровней

### Этап 1 — Уточнение «реального» уровня (find_real_level)

```
После build_levels и ПЕРЕД расчётом силы:
  
1. Собрать LOW свечей 15М ПОСЛЕ pump_peak в радиусе ATR от уровня
2. Если касаний ≥ 3 (LEVEL_REAL_CLUSTER_MIN_TOUCHES):
   a. Считаем медиану LOW-точек
   b. Если медиана смещена от исходного уровня > 30% (LEVEL_REAL_CLUSTER_SHIFT_THRESHOLD):
      → adjusted_level = медиана, touch_count = N
   c. Иначе → уровень не меняется, возвращаем оригинальный + touch_count
3. Возвращает: (adjusted_level, touch_count)
```

Цель: сместить уровень туда, где цена реально разворачивалась, а не туда, где был расчётный low пампа.

### Этап 2 — Расчёт количества подходов (_count_approaches)

```
Подход = вход цены в зону ATR×0.5 от уровня с последующим выходом > ATR×0.5
Считается ТОЛЬКО после pump_peak.
Используется гистерезис: вход в зону → touched=True, выход > ATR×0.5 → touched=False, approach++

Если подходов ≥ 2 → уровень «избитый» → сила снижается, verdict=exit
```

### Этап 3 — История уровня (get_level_history)

```
Сканируем 1М свечи после pump_peak:

was_broken:
  - 3 подряд close ниже уровня с объёмом > avg → was_broken=True

sweep_reclaimed:
  - was_broken=True + затем close > уровня + объём растёт → sweep_reclaimed=True

price_min_since_level:
  - Минимальная цена после pump_peak (для оценки глубины просадки)

max_vol_on_approach:
  - Максимальный объём за последние 5 свечей при подходе к уровню
```

### Этап 4 — Бонусы уровня

| Бонус | Как считается |
|-------|---------------|
| `hourly_open_bonus` | open_time % 3600 == 0 && hour % 4 == 0 → 3 (4h), == 0 → 2 (1h), minute==30 → 1 |
| `round_number_bonus` | Расстояние до ближайшего круглого числа: ≤0.3% → 2, ≤0.8% → 1 |
| `poc_aligned` | Совпадение с бином максимального объёма (volume profile) |
| `cluster` | Есть другой уровень в пределах 1% → cluster=True (штраф к силе) |
| `position` | origin / impulse / mid_move (origin — бонус к силе) |

### Этап 5 — ML-калибровка (apply_ml_to_level)

```
Модели обучены на исходах из history.db (поле level_outcomes).

Признаки: strength, level_type_enc, vol_ratio, touches, atr_ratio

Классификатор (clf.pkl):
  → p_bounce: вероятность отбоя (0.0-1.0)
  → если p_bounce ≥ 0.72 → ml_delta = +1
  → если p_bounce ≤ 0.40 → ml_delta = -1
  → иначе → ml_delta = 0

Регрессор (reg.pkl):
  → expected_depth: ожидаемая глубина прострела в %

Обновляет уровень: strength += ml_delta, добавляет p_bounce, expected_depth
```

### Этап 6 — Claude-оценка (calculate_strength_with_claude)

```
Входные данные:
  - ASCII-график 50 последних 15М свечей
  - Summary уровней: type, position, touches, volume (relative), timeframe alignment, round number, POC

Claude возвращает JSON:
  {"levels": [{"price": 0.028968, "strength": 5, "reason": "POC + 4h open + 6 касаний"}, ...]}

Cap-правило (защита от галлюцинаций):
  Если approach ≥ 2 ИЛИ (was_broken && !sweep_reclaimed):
    strength = min(claude_strength, python_strength)
```

### Итоговый порядок pipeline

```
build_levels()
    ↓
find_real_level()          ← уточнение цены по кластеру касаний
    ↓
_count_approaches()        ← сколько раз цена уже подходила
    ↓
get_level_history()        ← was_broken, sweep_reclaimed, max_vol
    ↓
calculate_strength()       ← Python-оценка 1-5 по правилам
    ↓
apply_ml_to_level()        ← ML-корректировка ±1, добавляет p_bounce
    ↓
calculate_strength_with_claude()  ← (только при /analyze или screener)
    ↓
CAP применяется            ← проблемные уровни не могут получить выше Python-оценки
```

---

## Система оценки силы уровней

### Python-оценка (calculate_strength)

```
Базовый strength по типу уровня:
  pump_base         = 5
  breakout_level    = 5
  consolidation_base = 4
  body_level        = 4
  order_block       = 4
  consolidation     = 3
  wick_level        = 2

Бонусы:
  + poc_aligned                                   → +2
  + hourly_open ≥ 2 (только pump_base/order_block/consolidation) → +1
  + round_number ≥ 2                              → +1
  + candle_count в диапазоне 5-15                 → +1
  + position == "origin"                          → +1

Штрафы:
  - candle_count ≤ 2                              → -1
  - approach ≥ 2                                  → strength=2, verdict=exit
  - cluster == True                               → -1, если strength < 4 → verdict=exit
  - pump_vol_ratio < 1.5                          → -1
  - was_broken && !sweep_reclaimed                → -2
  - max_vol > vol_ratio × 2                       → -1
  - zone_approaches == 1                          → -1
  - zone_approaches == 2                          → -2
  - zone_approaches >= 3                          → -3, verdict=exit
  - engulf_15m == True && vol > 2×avg             → verdict=exit_fast

Clamp: strength = max(1, min(5, strength))
```

### Итоговая шкала strength

| Значение | Интерпретация | Действие |
|----------|---------------|----------|
| 5 | Максимальная сила: POC + много касаний + 4h open | Мониторинг + вход S1/S2 |
| 4 | Сильный уровень: pump_base или хорошо подтверждён | Мониторинг + вход S1/S2 |
| 3 | Средний: есть касания, нет POC | Мониторинг слабый / S2 с осторожностью |
| 2 | Слабый: избитый или пробитый без выкупа | verdict=exit, не мониторируется |
| 1 | Очень слабый | Игнорируется |

---

## Торговые стратегии

### Архитектура стратегий

```
monitor.py
    │
    │  publish(event)
    ↓
event_bus.py (asyncio.Queue)
    │
    │  subscribe()
    ↓
strategy_runner.py
    │
    ├── Strategy1Bounce.on_event(event)
    │   Strategy1Bounce._update_open_trades(symbol, price)
    │
    ├── Strategy2LimitGrid.on_event(event)
    │   Strategy2LimitGrid._update_open_trades(symbol, price)
    │
    └── Strategy3Breakout.on_event(event)
        Strategy3Breakout._update_open_trades(symbol, price)
```

**Общие правила для всех стратегий (BaseStrategy):**
- `POSITION_SIZE_USDT = 100.0` — размер одной позиции
- `MAX_OPEN_TRADES = 3` — лимит одновременных сделок
- `TRADE_TIMEOUT_MINUTES = 60.0` — принудительное закрытие
- `_check_timeout()` вызывается каждые 60 сек через отдельную task

### Event Bus — схема событий

| event_type | Триггер | Поля |
|------------|---------|------|
| `proximity` | Цена ≤ 0.4% от уровня | symbol, level, strength, p_bounce, vol_ratio, atr |
| `bounce` | Подтверждённый отбой (зелёная + close > level) | + approach_style, expected_depth |
| `sweep` | Прострел + возврат выше уровня | + sweep_vol_ratio |
| `pressure` | 3+ красных свечи с растущим объёмом в зоне | symbol, level |
| `breakout` | Тело закрылось ниже уровня + vol ≥ 2× | + breakout_vol_ratio |
| `weak_breakout` | Закрытие ниже + vol < 2× | + breakout_vol_ratio |
| `volume_spike` | Объём ≥ 3× avg + красная свеча | + spike_ratio |

---

### Strategy 1 — Bounce (Отбой)

**Файл:** `trading/strategy1_bounce.py`  
**Идея:** покупаем при подтверждённом отбое от сильного уровня поддержки.

#### Условия входа

| Условие | Значение |
|---------|----------|
| event_type | `bounce` или `sweep` |
| strength | ≥ 4 (`S1_MIN_STRENGTH`) |
| p_bounce | ≥ 0.70 (`S1_MIN_P_BOUNCE`) |
| approach_style | НЕ `bleed` (тающие продажи = слабость) |
| vol_ratio | ≤ 1.2 (`S1_MAX_VOL_RATIO`) — только тихие касания |
| Открытая сделка по символу | нет |
| Число открытых сделок стратегии | < 3 |

**Логика vol_ratio фильтра:**  
S1 входит только при тихом касании уровня — vol_ratio ≤ 1.2 (`S1_MAX_VOL_RATIO`).  
Высокий объём при касании означает активное движение (материал для S2/S3, не S1).  
Данные из 3223 исходов history.db показывают одинаковый bounce rate ~41% при vol 0.8–3.0×,  
т.е. объём не предсказывает отбой — но низкий объём снижает риск входа против импульса.

#### Расчёт уровней выхода

```
expected_depth_abs = entry_price × (expected_depth / 100)
  Если expected_depth < 0.1% → использовать ATR как ориентир

stop_loss     = level - expected_depth_abs × 1.5
risk          = entry_price - stop_loss
take_profit_1 = entry_price + risk × 1.5   (S1_TP1_RR)
take_profit_2 = entry_price + risk × 3.0   (S1_TP2_RR)
```

#### Управление сделкой

```
Каждые 5 сек через _update_open_trades → _check_exit:

TP2 hit (current_price ≥ take_profit_2):
  → Если TP1 уже был → avg_exit = (tp1 + tp2) / 2
  → Иначе → exit = take_profit_2
  → close_trade("take_profit_2")

TP1 hit (current_price ≥ take_profit_1, ещё не было):
  → Записать "tp1_hit", установить stop_moved_to_breakeven=True
  → Стоп перемещается на entry_price (безубыток)
  → НЕ закрывает позицию — продолжаем ждать TP2

Stop loss (current_price ≤ effective_stop):
  effective_stop = entry_price (если stop_moved) или stop_loss
  → Если TP1 уже было: avg_exit = (tp1 + entry_price) / 2
  → Иначе: exit = current_price
  → close_trade("stop_loss")
```

#### Аварийное закрытие по breakout

```
Событие "breakout" по тому же уровню (в пределах 0.5%):
  → close_trade(current_price, "breakout_confirmed")
```

#### Telegram-уведомления

```
Открытие:
  📈 [S1 Bounce] SYMBOL LONG
     Уровень: 0.028968 (pump_base, strength=4)
     Вход: 0.029100 | p_bounce=0.72 | style=flash
     SL: 0.028500 (-2.06%) | TP1: 0.030000 (+3.09%) | TP2: 0.031800 (+9.28%)
     Позиция: 100 USDT

Закрытие:
  ✅ [S1 Bounce] SYMBOL закрыт
     Причина: take_profit_2
     Вход: 0.029100 → Выход: 0.030900
     PnL: +6.19% (+6.19 USDT) | Время: 47 мин
     📈 Max profit: +6.30% (+6.30 USDT)
     📉 Max drawdown: -0.45% (-0.45 USDT)
```

---

### Strategy 2 — Limit Grid (Сетка лимиток)

**Файл:** `trading/strategy2_limit_grid.py`  
**Идея:** расставляем 5 лимитных ордеров в зоне уровня до касания. Мартингейл усредняет вход при sweep.

#### Условия входа

| Условие | Значение |
|---------|----------|
| event_type | `proximity` (цена ≤ 0.4% от уровня) |
| strength | ≥ 3 (`S2_MIN_STRENGTH`) |
| p_bounce | ≥ 0.60 (`S2_MIN_P_BOUNCE`) |
| approach_style | НЕ `bleed` |
| Давление недавно (< 300 сек) | НЕТ — не входить под давлением |
| Открытая сделка по символу | нет |

#### Построение сетки

```
expected_depth_abs = level × (expected_depth / 100)
  Если < 0.1% → atr (если есть) или level × 0.005

step = expected_depth_abs × 1.2 / (S2_GRID_ORDERS - 1)  = на 5 ордеров

grid_anchor = level × 1.0015   (первый ордер на 0.15% ВЫШЕ уровня — front-run)
grid_prices = [grid_anchor - step × i for i in range(5)]
order_size = 100 USDT / 5 = 20 USDT каждый

Пример для level=0.029000, expected_depth=1.5%:
  step = 0.000087
  ордера: 0.029044, 0.028957, 0.028870, 0.028783, 0.028696

bottom_price = grid_prices[-1]  (самый нижний ордер)
stop_loss = bottom_price - atr × 0.5
```

#### Исполнение ордеров

```
_process_grid_fills вызывается каждые 5 сек через _check_exit.
Использует low последних 2 закрытых 1М свечей (не current_price).
  → Ловит sweep, который укладывается в 1-3 сек и не виден в поллинге.

fill_price = order["price"]  (лимитный ордер)
При каждом fill:
  weighted_entry = avg(filled prices)
  Пересчёт TP/SL от нового средневзвешенного entry_price:
    tp1 = weighted_entry + (weighted_entry - bottom_price) × 1.0  (1R)
    tp2 = weighted_entry + (weighted_entry - bottom_price) × 2.0  (2R)
    stop_loss = bottom_price - atr × 0.5
```

#### Управление сделкой (после первого fill)

```
TP2 / TP1 / Stop: аналогично S1 (двухуровневый выход, безубыток после TP1)

Таймаут без fill: если ни один ордер не исполнился за 3600 сек → закрыть с pnl=0
```

#### Обработка breakout

```
При событии "breakout" по уровню:
  1. Отменить все неисполненные ордера (cancelled=True)
  2. Если fill_count == 0 → закрыть без убытка (pnl=0, pnl_usdt=0)
     Логика: позиции не было, считать убыток нет смысла
  3. Если fill_count > 0 → close_trade(current_price, "breakout_confirmed")
```

#### Telegram-уведомления

```
Открытие (сетка):
  🔵 [S2 Grid] SYMBOL LONG — сетка выставлена
     Уровень: 0.029000 (pump_base, strength=4) | p_bounce=0.65
     Ордера (5×20 USDT):
       #1: 0.029044  #2: 0.028957  #3: 0.028870  #4: 0.028783  #5: 0.028696
     SL: 0.028500 | TP1: 0.029400 | TP2: 0.029800

При каждом fill:
  🔵 [S2 Grid] SYMBOL — ордер #2 исполнен на 0.028957
     Заполнено 2/5 | Ср. цена входа: 0.029001
```

---

### Strategy 3 — Breakout Momentum (Шорт на пробой)

**Файл:** `trading/strategy3_breakout.py`  
**Идея:** при подтверждённом пробое поддержки открываем SHORT — momentum продолжится вниз.

#### Условия входа

| Условие | Значение |
|---------|----------|
| event_type | `breakout` |
| breakout_vol_ratio | ≥ 2.4 (`S3_MIN_BREAKOUT_VOL_RATIO`) |
| level_side | `support` (только пробой поддержки, не сопротивления) |
| level_type | НЕ `pump_base` (базы пампа — слишком сильные, часто ложный пробой) |
| btc_change_1m | ≤ +0.2% — не входить если BTC растёт (контртренд) |
| Sweep недавно (< 120 сек) | НЕТ — sweep перед пробоем = ловушка |
| Открытая сделка по символу | нет |

#### Расчёт уровней выхода (SHORT)

```
stop_loss     = level + atr × 0.5   (выше уровня — стоп для шорта)
take_profit_1 = entry_price - atr × 2.0  (S3_TP1_ATR_MULT)
take_profit_2 = entry_price - atr × 4.0  (S3_TP2_ATR_MULT)
```

#### Управление сделкой

```
Минимальное время до SL/TP: 5 мин (S3_MIN_TRADE_DURATION_MINUTES)
  → Защита от немедленного закрытия на шуме после входа

TP2 hit (current_price ≤ take_profit_2):
  → close_trade("take_profit_2")

TP1 hit (current_price ≤ take_profit_1):
  → Записать "tp1_hit", стоп на безубыток (entry_price)
  → Включить trailing stop:
      new_trailing_stop = current_price + atr × 1.5
      Обновлять только если new_stop < effective_stop (тянем стоп вниз)

Stop loss (current_price ≥ effective_stop):
  → Если TP1 уже было: avg_exit = (tp1 + entry_price) / 2
  → Иначе: exit = current_price
```

#### Специфичные обработчики

```
Bounce по тому же уровню (event "bounce"):
  → Пробой не подтвердился → close_trade("breakout_failed_bounce")
  → Немедленный выход — цена вернулась выше уровня

Sweep после открытия (event "sweep"):
  → Записать предупреждение "sweep_warning" в events_json
  → НЕ закрывать — sweep может быть частью движения
```

#### Фиксация контекста входа

```
При открытии записывается entry_context в events_json:
  delta_at_entry:       buy_vol - sell_vol за 30 сек (отрицательный = давление продавцов)
  candle_body_ratio:    тело последней 1М / диапазон (0=закол, 1=чистое тело)

Используется для последующего анализа ложных пробоев.
```

#### Telegram-уведомления

```
Открытие:
  🔴 [S3 Breakout] SYMBOL SHORT
     Пробой уровня: 0.029000 (body_level) | Объём: ×3.2
     Вход: 0.028800 | strength=4
     SL: 0.029290 (+1.70%) | TP1: 0.027400 (-4.86%) | TP2: 0.025600 (-11.11%)
     Позиция: 100 USDT
```

---

### Таблица сравнения стратегий

| Параметр | S1 Bounce | S2 Limit Grid | S3 Breakout |
|----------|-----------|---------------|-------------|
| Направление | LONG | LONG | SHORT |
| Триггер входа | bounce/sweep | proximity | breakout |
| Мин. strength | 4 | 3 | нет требования |
| Мин. p_bounce | 0.70 | 0.60 | нет (шорт) |
| Макс. vol_ratio | ≤ 1.2 при входе | — | ≥ 2.4 при пробое |
| Число ордеров | 1 | 5 (сетка) | 1 |
| Размер | 100 USDT | 100 USDT (5×20) | 100 USDT |
| TP1/TP2 | 1.5R / 3.0R | 1R / 2R от дна сетки | 2 ATR / 4 ATR |
| SL | expected_depth × 1.5 | bottom - ATR×0.5 | level + ATR×0.5 |
| После TP1 | стоп на BE | стоп на BE | стоп на BE + trailing |
| Аварийный выход | breakout | breakout | bounce (пробой провалился) |
| Таймаут | 60 мин | 60 мин (или 3600 без fill) | 60 мин |

---

## Мониторинг уровней — события и реакции

**Файл:** `analysis/monitor.py`  
**Функция:** `start_monitor(symbol, level, level_side, stop_event, ...) -> dict`

Бесконечный цикл каждые 5 сек. При каждом событии публикует в event_bus.

### Таблица событий мониторинга

| # | Событие | Условие | Алерт + event_bus |
|---|---------|---------|-------------------|
| 1 | Proximity | distance ≤ 0.4% + approaching | 🎯 готовь ордер + publish("proximity") |
| 2 | Касание | last.low ≤ level × 1.002 | touched=True + start_delta_tracking |
| 3 | Delta reversal | buy_vol > sell_vol × 1.5 (30с, ≥10 сделок) | ⚡ дельта разворот + publish("bounce") |
| 4 | Отскок | green candle + close > level + vol > avg | ✅ отбой + publish("bounce") |
| 5 | Sweep | prev: low < level; curr: close > level + vol | 🟡 sweep + publish("sweep") |
| 6 | Слабый пробой | body < level + vol < 2× | ⚠️ слабый пробой + publish("weak_breakout") |
| 7 | Закол | body < level + vol ≥ 2× + prev_close выше | ⚠️ закол + publish("breakout" c пометкой) |
| 8 | Настоящий пробой | body < level + vol ≥ 2× + 2 closes ниже | 💥 пробой → EXIT + publish("breakout") |
| 9 | Давление | 3+ красных + vol растёт, зона 0.2-1% | 🔴 давление + publish("pressure") |
| 10 | Level broken | 5 closes < level + vol > avg | 🔴 уровень пробит (однократно) |

### Классификация касаний (_classify_and_log_level_event)

| Результат | Условие |
|-----------|---------|
| `near_miss` | fill_depth < 0.1%, dist ≤ 0.5% — не дошёл |
| `zakol` | fill_depth < 1%, вернулся выше |
| `zakol_deep` | fill_depth ≥ 1%, вернулся, без ретеста |
| `zakol_deep_retest` | fill_depth ≥ 1%, вернулся, ретест ≤ 0.3% |
| `bounce` | touched, returned — фиксируется как стандартный отбой |

### Сброс флагов (антиспам)

```
distance > 2×ATR → ПОЛНЫЙ сброс всех флагов
distance > 1×ATR → ЧАСТИЧНЫЙ сброс (только touched)
vol < 1.5× avg  → сброс volume_spike_notified
```

---

## Скринер рынка

**Файл:** `analysis/screener.py`

Фильтры:
```
1. symbol.endswith("USDT")
2. quoteVolume > 40_000_000
3. priceChangePercent > 10.0
4. NATR(5M, 14) > 2.0
Сортировка: по росту descending
```

### Три режима скринера

| Режим | Запуск | Действие |
|-------|--------|----------|
| Стартовый | 15 сек после старта | Отправить таблицу в Telegram |
| Автоматический | Каждые 10 мин | Добавить новые монеты, запустить мониторинг |
| Ручной (📊 Рынок) | По запросу | Таблица + inline-кнопки для анализа |

---

## База данных — схема и назначение

### history.db

```sql
level_outcomes (
    id, symbol, level, level_type, strength_claude,
    approach_type, vol_ratio_on_approach, touches_count,
    result, duration_minutes,
    outcome,         -- breakout/bounce/partial/no_reach
    approach_style,  -- flash/bleed/impulse/unknown
    vol_ratio_at_touch, atr_ratio, fill_depth_pct,
    btc_change_1m, funding_rate, created_at
)

symbol_profiles (
    symbol PK, best_level_type,
    wick_success_rate, body_success_rate, base_success_rate,
    total_signals, updated_at
)

symbol_events (
    id, symbol, event_type, details, created_at
)
```

### trades.db (paper-trading)

```sql
trades (
    id, strategy_id, strategy_name, trade_id,
    symbol, level, level_type, level_side,
    entry_signal, strength_at_entry, p_bounce_at_entry,
    expected_depth_at_entry, approach_style,
    vol_ratio_at_entry, atr_at_entry,
    entry_price, entry_time, position_size, direction,
    grid_orders_json, grid_fill_count,
    events_json,      -- JSON-лог событий внутри сделки
    max_favorable_pct, max_adverse_pct,
    exit_price, exit_time, exit_reason,
    pnl_pct, pnl_usdt, duration_minutes,
    status,           -- open / closed
    created_at, updated_at
)
```

**`events_json` структура:**
```json
[
  {"time": 1234567890, "type": "params_set", "price": 0.029100,
   "note": "{"stop_loss": 0.028500, "take_profit_1": 0.030000, ...}"},
  {"time": 1234567950, "type": "tp1_hit", "price": 0.030005,
   "note": "{"partial_exit_price": 0.030005, "partial_exit_pct": 50}"}
]
```

---

## Telegram-бот — команды и интерфейс

### Команды

| Команда / Кнопка | Действие |
|-------------------|----------|
| `/add SYMBOL` | Добавить монету в tokens.json |
| `/remove SYMBOL` | Удалить + отменить мониторы |
| `/list` | Список монет + inline для анализа |
| `/check SYMBOL LEVEL` | Оценить уровень + запустить мониторинг |
| `/monitors` | Активные мониторы с расстоянием до цены |
| `/stop SYMBOL` | Остановить все мониторы символа |
| `/analyze SYMBOL` | Полный анализ + Claude + график + мониторинг |
| **📜 История** | 20 последних событий символа |
| **👁 Мониторинги** | Все активные мониторы |
| **🔍 Проверить** | Inline выбор уровня из кэша → /check |
| **🛑 Стоп** | Inline выбор символа для остановки |
| **📊 Анализ** | Inline выбор символа → /analyze |
| **📊 Рынок** | Скринер: таблица + inline-кнопки |

### _do_analyze — полный процесс

```
1. Загрузить расширенную историю: 1000 свечей 1М + 500 свечей 15М
2. build_levels() с расширенными данными
3. Фильтр: только supports (< текущей цены), дальше 1.5 ATR, в диапазоне 20%
4. Python calculate_strength()
5. apply_ml_to_level() — ML корректировка
6. Claude calculate_strength_with_claude()
7. CAP: Claude не выше Python на проблемных уровнях
8. Разделить: strong (≥4) и weak (<4)
9. Telegram: текст + звёзды + claude_reason
10. PNG-график: generate_chart()
11. Auto-start мониторинга ближайшего strong уровня
12. Сохранить в _last_analysis_cache
```

---

## Жизненный цикл символа

```
Добавление (ручное или скринер)
          ↓
collector: загрузить 300 свечей 1М/15М
          ↓
_startup_monitoring: build_levels → мониторинг ближайшего сильного
          ↓
trigger_loop: каждые 5 сек проверять рост ≥3% + красная 1М
          ↓ (триггер сработал)
phase1: build_levels → calculate_strength → notify Telegram
          ↓ (strength ≥ 4)
phase2: start_monitor → цикл каждые 5 сек
          ↓ (событие)
     ┌── bounce → save_outcome → продолжить мониторинг
     ├── breakout → save_outcome → _start_next_level_after_breakout
     │              ├── Нашёл следующий уровень → phase2 (новый монитор)
     │              └── Не нашёл → проверить скринер → (нет) remove
     └── /stop или /remove → cancel_all_tasks → idle
```

---

## Запуск и остановка

### Запуск

```bash
python main.py
```

**asyncio.gather запускает параллельно:**
1. `start_collector()` — свечи Binance
2. `start_bot()` — Telegram polling
3. `_trigger_loop()` — проверка триггеров каждые 5 сек
4. `_proximity_loop()` — proximity alerts каждые 5 сек
5. `_startup_monitoring()` — через 30 сек: build + мониторинг для всех монет
6. `run_strategies()` — strategy_runner слушает event_bus

### Graceful shutdown

SIGINT/SIGTERM → `shutdown()`:
1. Cancel all asyncio tasks
2. Wait for cleanup
3. Отправить «🛑 Бот остановлен» в Telegram

---
