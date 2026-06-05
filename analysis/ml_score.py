"""
ML scoring for support levels.

Два выхода:
  - p_bounce: float 0..1  — вероятность отбоя
  - expected_depth: float — ожидаемый прокол под уровень в %

Использование:
    from analysis.ml_score import ml_score, apply_ml_to_level

    # approach_style должен быть выставлен ДО вызова (из detect_approach_style):
    lvl["approach_style"] = detect_approach_style(symbol)

    calculate_strength(lvl)
    apply_ml_to_level(lvl)
    # lvl теперь содержит: p_bounce, expected_depth, ml_delta, strength_pre_ml

Признаки модели (7 штук):
    1. strength        — Python-сила уровня (1-5)
    2. ltype_enc       — тип уровня (из level_type_map.pkl)
    3. vol_ratio       — объём / среднее (обрезается до 20)
    4. touches         — число касаний / подходов (обрезается до 5)
    5. atr_ratio       — расстояние до уровня в ATR (обрезается до 20)
    6. style_enc       — стиль подхода: flash=0, impulse=1, bleed=2, unknown=3
                         Доступен только в _monitored(); в _run_phase1 = "unknown"
    7. age_capped      — время мониторинга в минутах (обрезается до 300)
                         bounce медианно ~5 мин, breakout ~131 мин (BUG-13)

Hard-filter (применяется в apply_ml_to_level ДО ML):
    touches >= 2 → ml_delta = -2, p_bounce = 0.0
    Данные: touches>=2 даёт 0% bounce из 113 случаев — детерминированное правило,
    ML не нужен.
"""

from __future__ import annotations
import asyncio
import os
import pickle
import logging
import numpy as np

logger = logging.getLogger(__name__)

# Paths — модели лежат рядом с этим файлом в папке ml/
_BASE = os.path.join(os.path.dirname(__file__), "ml")

_clf = None
_reg = None
_le  = None
_type_map = None

_model_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _model_lock
    if _model_lock is None:
        _model_lock = asyncio.Lock()
    return _model_lock

# Маппинг стилей подхода — константа, используется и при обучении, и при инференсе
STYLE_MAP: dict[str, int] = {
    "flash":   0,   # резкий удар ×2 объём → часто sweep перед разворотом → bounce вероятнее
    "impulse": 1,   # 3+ зелёных → красная → первый серьёзный тест уровня → нейтрально
    "bleed":   2,   # 4+ красных с растущим объёмом → методичное давление → bounce реже
    "unknown": 3,   # стиль не определён (триггерный путь, старт)
}

# Пороги p_bounce для ml_delta.
# Откалиброваны по P25/P75 на датасете outcome IN ('bounce','breakout'),
# strength!=0, created_at >= 2026-05-21 (partial исключён).
# Обновлять после каждого переобучения через train_ml.py (он печатает новые значения).
THRESHOLD_HIGH: float = 0.97   # p_bounce >= HIGH → ml_delta = +1
THRESHOLD_LOW:  float = 0.60   # p_bounce <= LOW  → ml_delta = -1

# Минимальное количество касаний для торговли.
# touches >= TOUCHES_BLOCK → hard block (0% bounce из 113 случаев в истории).
TOUCHES_BLOCK: int = 2


def _load() -> bool:
    global _clf, _reg, _le, _type_map, THRESHOLD_HIGH, THRESHOLD_LOW
    if _clf is not None:
        return True
    try:
        with open(os.path.join(_BASE, "clf.pkl"), "rb") as f:
            _clf = pickle.load(f)
        with open(os.path.join(_BASE, "reg.pkl"), "rb") as f:
            _reg = pickle.load(f)
        with open(os.path.join(_BASE, "label_encoder.pkl"), "rb") as f:
            _le = pickle.load(f)
        with open(os.path.join(_BASE, "level_type_map.pkl"), "rb") as f:
            _type_map = pickle.load(f)
        # Load thresholds saved by train_ml.py, if available.
        _thr_path = os.path.join(_BASE, "thresholds.json")
        if os.path.exists(_thr_path):
            import json as _json
            with open(_thr_path) as _f:
                _thr = _json.load(_f)
            THRESHOLD_HIGH = float(_thr.get("THRESHOLD_HIGH", THRESHOLD_HIGH))
            THRESHOLD_LOW  = float(_thr.get("THRESHOLD_LOW",  THRESHOLD_LOW))
            logger.debug("ml_score: thresholds loaded HIGH=%.4f LOW=%.4f", THRESHOLD_HIGH, THRESHOLD_LOW)
        return True
    except Exception as e:
        logger.warning("ml_score: models not loaded — %s", e)
        return False


async def reload_models() -> None:
    """Hot-reload models after train_ml.py finishes. Thread-safe via asyncio.Lock."""
    async with _get_lock():
        global _clf, _reg, _le, _type_map
        _clf = None
        _reg = None
        _le = None
        _type_map = None
        ok = _load()
        if ok:
            logger.info("ml_score: models reloaded successfully")
        else:
            logger.warning("ml_score: reload failed, models unavailable until next retry")


def ml_score(lvl: dict) -> dict:
    """
    Принимает lvl-словарь (тот же что в calculate_strength).
    Использует 6 признаков; approach_style читается из lvl["approach_style"]
    (fallback: "unknown").

    Возвращает dict с ключами:
        p_bounce       — вероятность отбоя (0..1)
        expected_depth — ожидаемый прокол под уровень в %
        ml_delta       — поправка к strength: +1 / 0 / -1
    При ошибке загрузки возвращает нейтральный результат.
    """
    if not _load():
        return {"p_bounce": 0.5, "expected_depth": 1.5, "ml_delta": 0}

    try:
        ltype     = lvl.get("type", "body_level")
        strength  = float(lvl.get("strength", 3) or 3)
        vol       = float(lvl.get("vol_ratio", 1.0) or 1.0)
        touches   = min(float(lvl.get("touches_count") or lvl.get("approach", 1) or 1), 5.0)
        atr_ratio = float(lvl.get("atr_ratio", 2.0) or 2.0)
        style     = lvl.get("approach_style", "unknown") or "unknown"

        ltype_enc = _type_map.get(ltype, 1)
        style_enc = STYLE_MAP.get(style, 3)
        age_min   = min(float(lvl.get("monitoring_age_minutes") or 0), 300.0)

        import time as _time
        monitoring_age_hours = (
            (_time.time() - lvl["monitoring_start_time"]) / 3600
            if lvl.get("monitoring_start_time")
            else 0.0
        )

        x = np.array([[
            strength,
            ltype_enc,
            min(vol, 20.0),
            touches,
            min(atr_ratio, 20.0),
            style_enc,
            age_min,
            monitoring_age_hours,
        ]])

        # Classifier
        try:
            proba      = _clf.predict_proba(x)[0]
            bounce_idx = list(_le.classes_).index("bounce")
            p_bounce   = float(proba[bounce_idx])

            # Regressor
            expected_depth = float(_reg.predict(x)[0])
        except Exception as e:
            logger.warning("ML predict failed (model mismatch?), using defaults", extra={"error": str(e)})
            p_bounce = 0.5
            expected_depth = 1.0
        expected_depth = max(0.1, round(expected_depth, 2))

        if p_bounce >= THRESHOLD_HIGH:
            ml_delta = 1
        elif p_bounce <= THRESHOLD_LOW:
            ml_delta = -1
        else:
            ml_delta = 0

        return {
            "p_bounce":       round(p_bounce, 3),
            "expected_depth": expected_depth,
            "ml_delta":       ml_delta,
        }

    except Exception as e:
        logger.warning("ml_score error: %s", e)
        return {"p_bounce": 0.5, "expected_depth": 1.5, "ml_delta": 0}


def apply_ml_to_level(lvl: dict) -> None:
    """
    Вызвать после calculate_strength(lvl).

    Требования к lvl перед вызовом:
      - lvl["approach_style"] должен быть уже выставлен, если стиль известен.
        В _monitored(): lvl["approach_style"] = detect_approach_style(symbol)
        В _run_phase1() / _startup_monitoring(): approach_style отсутствует → "unknown"

    Hard-filter: touches >= TOUCHES_BLOCK → p_bounce=0.0, ml_delta=-2, ML не вызывается.
    Данные показывают 0% bounce при touches>=2 из 113 случаев — детерминированное правило.

    CAP: пробитый и невосстановившийся уровень (was_broken && !sweep_reclaimed)
    — ML не корректирует вверх (только вниз или 0), чтобы не обходить штраф calculate_strength.

    Добавляет в lvl:
        p_bounce        — вероятность отбоя
        expected_depth  — ожидаемый прокол %
        ml_delta        — применённая поправка
        strength_pre_ml — strength до ML (для логов и Claude cap)
        ml_blocked      — True если сработал hard-filter по touches
    """
    lvl["strength_pre_ml"] = lvl.get("strength", 3)

    # ── Hard-filter: touches >= TOUCHES_BLOCK ──────────────────────────
    touches = int(lvl.get("touches_count") or lvl.get("approach", 1) or 1)
    if touches >= TOUCHES_BLOCK:
        lvl["p_bounce"]       = 0.0
        lvl["expected_depth"] = 0.0
        lvl["ml_delta"]       = -2
        lvl["ml_blocked"]     = True
        lvl["strength"]       = max(1, lvl["strength_pre_ml"] - 2)
        logger.debug(
            "ml_score BLOCKED touches=%d level=%s strength %d→%d",
            touches,
            lvl.get("level"),
            lvl["strength_pre_ml"],
            lvl["strength"],
        )
        return

    lvl["ml_blocked"] = False

    # ── ML scoring ────────────────────────────────────────────────────
    result = ml_score(lvl)

    # CAP: не повышать strength для пробитого и не восстановившегося уровня
    was_broken = lvl.get("was_broken", False)
    sweep      = lvl.get("sweep_reclaimed", False)
    if was_broken and not sweep:
        result["ml_delta"] = min(result["ml_delta"], 0)

    lvl["p_bounce"]       = result["p_bounce"]
    lvl["expected_depth"] = result["expected_depth"]
    lvl["ml_delta"]       = result["ml_delta"]
    lvl["strength"]       = max(1, min(5, lvl["strength_pre_ml"] + result["ml_delta"]))

    logger.debug(
        "ml_score applied level=%s style=%s touches=%d p_bounce=%.2f depth=%.2f%% delta=%+d strength %d→%d",
        lvl.get("level"),
        lvl.get("approach_style", "unknown"),
        touches,
        result["p_bounce"],
        result["expected_depth"],
        result["ml_delta"],
        lvl["strength_pre_ml"],
        lvl["strength"],
    )
