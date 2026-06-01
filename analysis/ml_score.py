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

Признаки модели (6 штук):
    1. strength        — Python-сила уровня (1-5)
    2. ltype_enc       — тип уровня (из level_type_map.pkl)
    3. vol_ratio       — объём / среднее (обрезается до 20)
    4. touches         — число касаний / подходов
    5. atr_ratio       — расстояние до уровня в ATR (обрезается до 20)
    6. style_enc       — стиль подхода: flash=0, impulse=1, bleed=2, unknown=3
                         Доступен только в _monitored(); в _run_phase1 = "unknown"
"""

from __future__ import annotations
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

# Маппинг стилей подхода — константа, используется и при обучении, и при инференсе
STYLE_MAP: dict[str, int] = {
    "flash":   0,   # резкий удар ×2 объём → часто sweep перед разворотом → bounce вероятнее
    "impulse": 1,   # 3+ зелёных → красная → первый серьёзный тест уровня → нейтрально
    "bleed":   2,   # 4+ красных с растущим объёмом → методичное давление → bounce реже
    "unknown": 3,   # стиль не определён (триггерный путь, старт)
}


def _load() -> bool:
    global _clf, _reg, _le, _type_map
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
        return True
    except Exception as e:
        logger.warning("ml_score: models not loaded — %s", e)
        return False


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

        x = np.array([[
            strength,
            ltype_enc,
            min(vol, 20.0),
            touches,
            min(atr_ratio, 20.0),
            style_enc,             # признак 6: стиль подхода
        ]])

        # Classifier
        proba      = _clf.predict_proba(x)[0]
        bounce_idx = list(_le.classes_).index("bounce")
        p_bounce   = float(proba[bounce_idx])

        # Regressor — всегда считаем, полезно для отображения
        expected_depth = float(_reg.predict(x)[0])
        expected_depth = max(0.1, round(expected_depth, 2))

        # Пороги откалиброваны по квартилям реального bounce-rate
        # (верхний квартиль ~0.57, нижний ~0.32 на датасете 2026-05-21+)
        if p_bounce >= 0.57:
            ml_delta = 1
        elif p_bounce <= 0.32:
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

    CAP: пробитый и невосстановившийся уровень (was_broken && !sweep_reclaimed)
    — ML не корректирует вверх (только вниз или 0), чтобы не обходить штраф calculate_strength.

    Добавляет в lvl:
        p_bounce       — вероятность отбоя
        expected_depth — ожидаемый прокол %
        ml_delta       — применённая поправка
        strength_pre_ml — strength до ML (для логов и Claude cap)
    """
    result = ml_score(lvl)

    # CAP: не повышать strength для пробитого и не восстановившегося уровня
    was_broken = lvl.get("was_broken", False)
    sweep      = lvl.get("sweep_reclaimed", False)
    if was_broken and not sweep:
        result["ml_delta"] = min(result["ml_delta"], 0)  # только вниз или 0

    lvl["p_bounce"]       = result["p_bounce"]
    lvl["expected_depth"] = result["expected_depth"]
    lvl["ml_delta"]       = result["ml_delta"]

    # Сохраняем pre-ML strength для логов и Claude cap
    lvl["strength_pre_ml"] = lvl.get("strength", 3)
    lvl["strength"] = max(1, min(5, lvl["strength"] + result["ml_delta"]))

    logger.debug(
        "ml_score applied level=%s style=%s p_bounce=%.2f depth=%.2f%% delta=%+d strength %d→%d",
        lvl.get("level"),
        lvl.get("approach_style", "unknown"),
        result["p_bounce"],
        result["expected_depth"],
        result["ml_delta"],
        lvl["strength_pre_ml"],
        lvl["strength"],
    )
