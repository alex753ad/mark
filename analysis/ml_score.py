"""
ML scoring for support levels.

Два выхода:
  - p_bounce: float 0..1  — вероятность отбоя
  - expected_depth: float — ожидаемый прокол под уровень в %

Использование:
    from analysis.ml_score import ml_score

    result = ml_score(lvl)
    # result = {"p_bounce": 0.74, "expected_depth": 1.2, "ml_delta": 1}

    # В calculate_strength — применить ml_delta к итоговому strength:
    strength = max(1, min(5, strength + result["ml_delta"]))
    lvl["p_bounce"]       = result["p_bounce"]
    lvl["expected_depth"] = result["expected_depth"]
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


def _load():
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
    Возвращает dict с ключами:
        p_bounce       — вероятность отбоя (0..1)
        expected_depth — ожидаемый прокол % (только если p_bounce > 0.4)
        ml_delta       — поправка к strength: +1 / 0 / -1
    При ошибке загрузки возвращает нейтральный результат.
    """
    if not _load():
        return {"p_bounce": 0.5, "expected_depth": 1.5, "ml_delta": 0}

    try:
        ltype     = lvl.get("type", "body_level")
        strength  = float(lvl.get("strength", 3) or 3)
        vol       = float(lvl.get("vol_ratio", 1.0) or 1.0)
        touches   = float(lvl.get("touches_count") or lvl.get("approach", 1) or 1)
        atr_ratio = float(lvl.get("atr_ratio", 2.0) or 2.0)

        ltype_enc = _type_map.get(ltype, 1)
        x = np.array([[
            strength,
            ltype_enc,
            min(vol, 20.0),
            touches,
            min(atr_ratio, 20.0),
        ]])

        # Classifier
        proba     = _clf.predict_proba(x)[0]
        bounce_idx = list(_le.classes_).index("bounce")
        p_bounce  = float(proba[bounce_idx])

        # Regressor — всегда считаем, полезно для отображения
        expected_depth = float(_reg.predict(x)[0])
        expected_depth = max(0.1, round(expected_depth, 2))

        # Delta к strength
        if p_bounce >= 0.72:
            ml_delta = 1
        elif p_bounce <= 0.40:
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
    Применяет ml_delta к strength и добавляет p_bounce / expected_depth в lvl.

    Пример в trigger.py:
        calculate_strength(lvl)
        apply_ml_to_level(lvl)
    """
    result = ml_score(lvl)
    lvl["p_bounce"]       = result["p_bounce"]
    lvl["expected_depth"] = result["expected_depth"]

    # Применяем дельту, сохраняем pre-ML strength для логов
    lvl["strength_pre_ml"] = lvl.get("strength", 3)
    lvl["strength"] = max(1, min(5, lvl["strength"] + result["ml_delta"]))

    logger.debug(
        "ml_score applied level=%s p_bounce=%.2f depth=%.2f%% delta=%+d strength %d→%d",
        lvl.get("level"),
        result["p_bounce"],
        result["expected_depth"],
        result["ml_delta"],
        lvl["strength_pre_ml"],
        lvl["strength"],
    )
