# Архитектура проекта mark

Торговый бот для Binance Futures. Проект мониторит фьючерсные пары, строит уровни поддержки/сопротивления после импульсов, оценивает силу уровней через Python-эвристики, ML и Claude, отправляет сигналы в Telegram, ведет историю и paper trading по стратегиям.

Актуализировано: 2026-06-09.

## Общая схема

```text
Binance Futures
  |
  | REST klines + aggTrades websocket
  v
data.collector
  |
  +--> main.py trigger/proximity/stale loops
  |      |
  |      +--> analysis.level_builder
  |      +--> analysis.trigger
  |      +--> analysis.ml_score
  |      +--> analysis.claude_strength / ai.claude_client
  |      +--> analysis.monitor
  |              |
  |              +--> Telegram alerts
  |              +--> data.history
  |              +--> trading.event_bus
  |                       |
  |                       +--> trading.strategy_runner
  |                               +--> S1/S2/S3/S4 paper strategies
  |                               +--> trading.trade_log / trades.db
  |
  +--> web_server.py + dashboard.html
```

## Структура файлов

```text
mark/
  .env.example              # шаблон переменных окружения
  tokens.json               # активные символы
  blacklist.json            # символы, запрещенные для мониторинга/торговли
  trigger_times.json        # cooldown триггеров
  active_monitors.json      # мониторы для восстановления после рестарта
  history.db                # история уровней, исходов, событий
  trades.db                 # paper trading сделки и события

  main.py                   # основной оркестратор asyncio-задач
  config.py                 # env, пути, TokenRegistry, BlacklistRegistry
  constants.py              # пороги, интервалы, настройки ML/AI/стратегий
  models.py                 # SymbolState, LevelData, StateManager
  logger.py                 # loguru
  utils.py                  # вспомогательные функции форматирования/расчетов
  web_server.py             # aiohttp API и dashboard
  dashboard.html            # Vue 3 SPA для мониторинга состояния
  train_ml.py               # обучение sklearn-моделей по history.db
  ml_health_check.py        # проверка состояния ML-моделей
  export_to_csv.py          # экспорт history/trades в CSV

  data/
    collector.py            # свечи 1m/15m, aggTrades delta tracking
    history.py              # SQLite CRUD для событий, исходов и профилей

  analysis/
    level_builder.py        # построение уровней: pump_base/body/wick/order_block/etc.
    trigger.py              # ATR, триггеры, approaching levels, funding
    monitor.py              # live monitoring: bounce/breakout/sweep/pressure/spikes
    screener.py             # поиск монет по объему, росту и NATR
    pump_phase.py           # здоровье и фаза пампа
    ml_score.py             # применение обученных моделей к уровню
    claude_strength.py      # Claude-оценка силы уровней по ASCII-графику
    chart.py                # PNG-график уровня
    chart_ascii.py          # ASCII-график для AI-промпта
    ml/                     # clf/reg/label_encoder/thresholds/*.pkl,json

  ai/
    claude_client.py        # Anthropic client, JSON parsing, reasons/advice

  bot/
    telegram.py             # aiogram v3 команды, кнопки, FSM, экспорт, графики
    chart.py                # график закрытой сделки

  trading/
    event_bus.py            # async очередь событий monitor -> strategies
    base_strategy.py        # общий каркас paper strategies
    strategy1_bounce.py     # S1 Bounce
    strategy2_limit_grid.py # S2 Limit Grid
    strategy3_breakout.py   # S3 Breakout short/continuation
    strategy4_breakout_long.py # S4 Breakout Long
    strategy_runner.py      # запуск стратегий, price loop, timeout checker
    trade_log.py            # SQLite CRUD сделок и статистики
    price_tracker.py        # post-exit отслеживание цены
```

## Технологии

| Компонент | Используется |
| --- | --- |
| Runtime | Python 3.11+, asyncio |
| Биржа | python-binance 1.0.36, Binance Futures REST/WebSocket |
| Telegram | aiogram 3.27.0 |
| Web | aiohttp 3.13.3, Vue 3 в `dashboard.html` |
| AI | anthropic 0.86.0, модель `claude-haiku-4-5-20251001` |
| ML | scikit-learn RandomForest, pickle-модели в `analysis/ml/` |
| DB | SQLite + aiosqlite 0.22.1 |
| Графики | matplotlib, numpy, pillow |
| Логи | loguru |
| Деплой | Railway, `Procfile: worker: python main.py` |

## Конфигурация

`.env`:

```env
CLAUDE_API_KEY=sk-ant-...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
# TELEGRAM_PROXY=socks5://127.0.0.1:1080
```

`config.py` экспортирует:

- `CLAUDE_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_PROXY`;
- `TOKENS_FILE`, `BLACKLIST_FILE`, `TRIGGER_TIMES_FILE`, `ACTIVE_MONITORS_FILE`;
- `HISTORY_DB_FILE`, с учетом `RAILWAY_VOLUME_MOUNT_PATH`;
- `TokenRegistry` для `tokens.json`;
- `BlacklistRegistry` для `blacklist.json`;
- `validate_config()`.

## Основные состояния

`models.SymbolState` хранит состояние одного символа:

- текущую фазу `idle | phase1 | phase2`;
- активные monitor tasks и stop flags;
- cooldown последнего триггера;
- кэш proximity-уведомлений и уже проанализированных уровней;
- силу активных уровней;
- состояние слабых касаний;
- метрики pump phase: high/base/time, broken levels, last bounce, health, phase.

`models.LevelData` описывает уровень: цена, тип, сторона `support/resistance`, сила, verdict, причины, ATR/volume/approach признаки, кластерность, sweep/broken/history flags.

`StateManager` держит все `SymbolState` и умеет отменять задачи по символу или глобально.

## Жизненный цикл символа

1. Символ попадает в `tokens.json` вручную через Telegram или автоматически из screener.
2. `data.collector` поддерживает свежие 1m/15m свечи и delta по aggTrades.
3. `_trigger_loop` в `main.py` проверяет рост на 15m и cooldown.
4. При триггере `analysis.level_builder.build_levels()` строит уровни.
5. Уровни получают Python score, ML score и, если включено, Claude score.
6. Подходящие уровни запускаются в `analysis.monitor.start_monitor()`.
7. Мониторинг каждые несколько секунд проверяет proximity, pressure, volume spike, sweep, bounce и breakout.
8. События уходят в Telegram, `history.db` и `trading.event_bus`.
9. `trading.strategy_runner` читает события и открывает/закрывает paper trades в `trades.db`.
10. После рестарта `active_monitors.json` помогает восстановить активные мониторы.

## Построение уровней

`analysis.level_builder` использует 15m и 1m свечи:

- находит последний pump leg или строит уровни без явного пампа;
- выделяет `pump_base`, `body_level`, `wick_level`, `order_block`, `mid_impulse_pause`, consolidation zones;
- считает ATR, POC-like price, бонусы старшего таймфрейма и круглых чисел;
- дедуплицирует уровни по радиусу;
- назначает позицию уровня относительно импульса;
- помечает кластеры.

Ключевые настройки находятся в `constants.py`: `PUMP_MIN_GROWTH_PCT`, `BODY_CLUSTER_*`, `WICK_CLUSTER_MIN_TOUCHES`, `CONSOLIDATION_*`, `PRICE_ROUNDING_RULES`.

## Оценка уровня

Оценка объединяет несколько источников:

- Python: ATR, количество подходов, volume ratio, sweep/reclaim, положение уровня, история пробоев.
- ML: `analysis.ml_score.ml_score()` возвращает вероятность bounce и ожидаемую глубину пробоя по моделям из `analysis/ml/`.
- Claude: `analysis.claude_strength.calculate_strength_with_claude()` отправляет ASCII-график и summary уровней.
- История: `data.history.get_outcome_probs()` и профиль символа дают эмпирические вероятности.

Итоговые поля уровня: `strength`, `verdict`, `reason`, `p_bounce`, `expected_depth`, дополнительные признаки для стратегий.

## Pump Phase

`analysis.pump_phase` оценивает здоровье пампа:

- `detect_pump_peak()` определяет вершину, базу и время;
- `pump_health_score()` штрафует за возраст пампа, глубину коррекции, пробитые уровни и bleed structure;
- `get_pump_phase()` переводит score в `active`, `caution`, `degraded`, `dead`;
- `calc_correction_pct()` считает текущую коррекцию.

`main.py` использует это для фильтрации мониторинга: слабые уровни не запускаются, когда памп деградировал.

## Мониторинг

`analysis.monitor.start_monitor()` следит за уровнем и генерирует события:

- proximity alert при приближении цены;
- pressure в зоне уровня;
- volume spike;
- sweep/reclaim;
- bounce с записью исхода;
- weak/confirmed breakout;
- complications: engulfing, level broken, volume trend.

При bounce или breakout монитор может инициировать поиск следующего уровня через `main.py`: сопротивление после отскока или следующий уровень после пробоя.

## Trading subsystem

Paper trading работает отдельно от сигналов, через `trading.event_bus`.

- `event_bus.publish()` вызывается из мониторинга.
- `strategy_runner.run_strategies()` запускает стратегии, общий price loop и timeout checker.
- `BaseStrategy` задает общий контракт: обработка событий, вход, TP/SL, timeout, open trade limits.
- `trade_log.py` хранит сделки, события, экстремумы и статистику в `trades.db`.
- `price_tracker.py` отслеживает движение цены после выхода, чтобы оценивать качество закрытий.

Стратегии:

| Стратегия | Идея |
| --- | --- |
| S1 Bounce | вход от сильного уровня при тихом касании и высокой `p_bounce` |
| S2 Limit Grid | сетка лимитных ордеров у уровня при подходящем pressure/volume контексте |
| S3 Breakout | вход на подтвержденном пробое с объемом |
| S4 Breakout Long | long-сценарий пробоя/продолжения по отдельным правилам |

## Telegram

`bot.telegram` предоставляет:

- команды `/add`, `/remove`, `/list`, `/monitors`, `/stop`, `/stats`;
- `/analyze SYMBOL` для полного анализа;
- `/check SYMBOL LEVEL` для ручной проверки уровня;
- `/blacklist` и `/unblacklist`;
- `/export_db`;
- кнопки для рынка, истории, анализа, остановки мониторов;
- отправку PNG-графиков уровней и графиков закрытых сделок.

Доступ ограничен `TELEGRAM_CHAT_ID`.

## Web dashboard

`web_server.py` поднимает aiohttp на `127.0.0.1:8080` и отдает:

- `/` -> `dashboard.html`;
- `/api/state` -> состояние токенов, мониторов, collector и фаз;
- `/api/events` -> последние события из `history.db`;
- `/api/signals` -> сигналы/исходы;
- `/api/open-trades` -> открытые paper trades;
- `/api/trades-history` -> история сделок.

`dashboard.html` - одностраничный Vue 3 интерфейс с polling.

## Базы данных и файлы данных

`history.db`:

- события мониторинга;
- исходы уровней;
- профили символов;
- данные для обучения ML.

`trades.db`:

- открытые и закрытые paper trades;
- события сделок;
- экстремумы цены;
- post-exit статистика.

JSON-файлы:

- `tokens.json` - активный universe;
- `blacklist.json` - исключенные символы;
- `trigger_times.json` - защита от повторных триггеров;
- `active_monitors.json` - восстановление monitor tasks.

## ML

`train_ml.py` читает `history.db`, строит признаки и обучает модели:

- классификатор вероятности отскока;
- регрессор ожидаемой глубины;
- label encoder и map типов уровней;
- thresholds и размер последнего train dataset.

`analysis.ml_score` лениво загружает модели, защищает reload через lock и применяет score к каждому уровню. `main.py` содержит `_ml_retrain_loop`, который переобучает модели при накоплении новых данных.

## Важные интервалы и пороги

| Настройка | Значение |
| --- | --- |
| `TRIGGER_GROWTH_THRESHOLD` | 3% на 15m |
| `TRIGGER_COOLDOWN_SECONDS` | 3600 секунд |
| `COLLECTOR_UPDATE_INTERVAL_SECONDS` | 5 секунд |
| `SCREENER_AUTO_INTERVAL_SECONDS` | 600 секунд |
| `SCREENER_MIN_VOLUME_USD` | 40M USDT |
| `SCREENER_MIN_GROWTH_PCT` | 10% |
| `SCREENER_MIN_NATR` | 2.0 |
| `SCREENER_MIN_15M_VOLUME_USD` | 1M USDT |
| `PROXIMITY_ALERT_DISTANCE_PCT` | 2% |
| `VOLUME_BREAKOUT_RATIO` | 2.4x |
| `VOLUME_SPIKE_RATIO` | 3.0x |
| `STRATEGY_POSITION_SIZE_USDT` | 100 USDT |
| `STRATEGY_MAX_OPEN_TRADES` | 3 на стратегию |
| `STRATEGY_TRADE_TIMEOUT_MINUTES` | 60 минут |

## Запуск

```bash
pip install -r requirements.txt
python main.py
```

Для обучения ML:

```bash
python train_ml.py
```

Для проверки ML:

```bash
python ml_health_check.py
```

Для экспорта:

```bash
python export_to_csv.py
```

## Точки внимания

- В рабочем дереве есть сгенерированные файлы (`*.db`, `*.csv`, `__pycache__`, модели `*.pkl`), их не стоит смешивать с документационными изменениями в одном коммите.
- Название архитектурного файла сейчас `ARCHITECTURE (2).md`; README ссылается на `ARCHITECTURE.md`, поэтому ссылку стоит поправить или переименовать файл отдельным шагом.
- В части исходников комментарии уже содержат mojibake, но новый архитектурный документ сохранен как нормальный UTF-8.
