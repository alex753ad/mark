"""Trading bot configuration constants."""

# Trigger settings
TRIGGER_GROWTH_THRESHOLD = 0.03  # 3% рост на 15М для триггера
TRIGGER_COOLDOWN_SECONDS = 3600  # 1 час между триггерами одного символа

# Level detection
LEVEL_APPROACH_THRESHOLD = 0.5  # ATR × 0.5 для определения "близкого" уровня
LEVEL_CLUSTER_RADIUS_PCT = 0.01  # 1% для определения кластера уровней
LEVEL_REAL_CLUSTER_MIN_TOUCHES = 3  # Минимум касаний для уточнения уровня
LEVEL_REAL_CLUSTER_SHIFT_THRESHOLD = 0.3  # 30% смещения медианы для корректировки

# Monitoring
PROXIMITY_ALERT_DISTANCE_PCT = 0.004  # 0.4% от уровня для proximity alert
PROXIMITY_ALERT_COOLDOWN_SECONDS = 600  # 10 минут между proximity alerts
WEAK_BREAKOUT_COOLDOWN_SECONDS = 300  # 5 минут между сообщениями о слабом пробое

# Volume thresholds
VOLUME_BREAKOUT_RATIO = 2.0  # Объём ×2 для подтверждения пробоя
VOLUME_SPIKE_RATIO = 3.0  # Объём ×3 для алерта о спайке
VOLUME_SPIKE_RESET_RATIO = 1.5  # Объём < ×1.5 для сброса флага спайка
VOLUME_REBOUND_MIN_RATIO = 1.0  # Минимальный объём для подтверждения отбоя

# Distance thresholds
DISTANCE_RESET_ATR_MULTIPLIER = 2.0  # Удаление > 2×ATR для сброса всех флагов
DISTANCE_PARTIAL_RESET_ATR_MULTIPLIER = 1.0  # Удаление > 1×ATR для частичного сброса
PRESSURE_ZONE_MIN_DISTANCE_PCT = 0.002  # 0.2% минимальная дистанция для давления
PRESSURE_ZONE_MAX_DISTANCE_PCT = 0.01  # 1.0% максимальная дистанция для давления

# Pressure detection
PRESSURE_MIN_DIRECTIONAL_CANDLES = 3  # Минимум 3 направленных свечи для давления
PRESSURE_VOLUME_MIN_RATIO = 1.0  # Минимальный объём для подтверждения давления на 15М

# Level strength calculation
STRENGTH_PUMP_VOLUME_LOW_THRESHOLD = 1.5  # Порог низкого объёма пампа
STRENGTH_PUMP_VOLUME_HIGH_THRESHOLD = 2.5  # Порог высокого объёма пампа
STRENGTH_APPROACH_EXIT_THRESHOLD = 2  # Число подходов для автоматического exit

# Data collection
CANDLES_HISTORY_LIMIT = 300  # Количество свечей для хранения в памяти
COLLECTOR_UPDATE_INTERVAL_SECONDS = 5  # Интервал обновления данных с Binance

# Screener settings
SCREENER_MIN_VOLUME_USD = 40_000_000  # Минимальный объём для скринера
SCREENER_MIN_GROWTH_PCT = 10.0  # Минимальный рост для скринера
SCREENER_MIN_NATR = 2.0  # Минимальный NATR для скринера
SCREENER_DELAY_SECONDS = 15  # Задержка перед запуском скринера
SCREENER_AUTO_INTERVAL_SECONDS = 1800  # Интервал автоскринера (30 минут)

# API limits
CLAUDE_MAX_CONCURRENT_REQUESTS = 2  # Максимум параллельных запросов к Claude API
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # Модель Claude для анализа
CLAUDE_MAX_TOKENS = 1024  # Максимум токенов в ответе Claude
CLAUDE_STRENGTH_ENABLED = True  # Использовать Claude для определения силы уровней

# ATR settings
ATR_PERIOD = 14  # Период для расчёта ATR

# Price rounding rules (price range -> decimal places)
PRICE_ROUNDING_RULES = [
    (100, 2),      # > 100 → 2 знака
    (1, 4),        # 1-100 → 4 знака
    (0.1, 5),      # 0.1-1 → 5 знаков
    (0.01, 6),     # 0.01-0.1 → 6 знаков
    (0, 8),        # < 0.01 → 8 знаков
]

# Level building
PUMP_MIN_GROWTH_PCT = 0.05  # 5% минимальный рост для определения пампа
BODY_CLUSTER_WEIGHT_15M = 3  # Вес 15М свечи в кластеризации тел
BODY_CLUSTER_WEIGHT_1M = 1  # Вес 1М свечи в кластеризации тел
BODY_CLUSTER_MIN_WEIGHT = 6  # Минимальный вес для body_level
WICK_CLUSTER_MIN_TOUCHES = 4  # Минимум касаний для wick_level
CONSOLIDATION_MIN_CANDLES = 3  # Минимум свечей для consolidation
CONSOLIDATION_RANGE_ATR_MULTIPLIER = 4  # Множитель ATR для определения узкого range

# Monitoring events
LEVEL_BROKEN_MIN_CANDLES = 5  # Минимум свечей ниже уровня для алерта
