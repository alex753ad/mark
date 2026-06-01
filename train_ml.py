"""
Скрипт переобучения ML-моделей для ml_score.py.

Запуск (из корня проекта):
    python train_ml.py [--db history.db] [--out analysis/ml]

Фильтрация датасета:
    - Только строки с outcome IN ('bounce', 'breakout')  ← partial исключён
    - Исключаем strength_claude = 0 (уровни без Claude-оценки, bounce 18% vs 40%+)
    - Исключаем записи до 2026-05-21 (аномальный медвежий режим, breakout 35% vs 14-17%)

  ВАЖНО: partial — это артефакт прерванного мониторинга (duration=0), а не
  рыночный исход. Включение partial в обучение ухудшает качество модели, так как
  она начинает предсказывать его как легитимный класс и занижает p_bounce без причины.

  Hard-filter по touches делается в telegram.py ДО вызова ML:
    touches >= 2 → 0% bounce из 113 случаев → блокируем без ML.
  В обучении touches остаётся признаком — модель учится на полном диапазоне.

Признаки (6 штук, порядок должен совпадать с ml_score.py):
    1. strength_claude  — сила уровня (1-5)
    2. ltype_enc        — тип уровня (pump_base / body_level / wick_level / order_block)
    3. vol_capped       — vol_ratio_at_touch, обрезан до 20
    4. touches          — touches_count, обрезан до 5
    5. atr_capped       — atr_ratio, обрезан до 20
    6. style_enc        — approach_style (flash=0, impulse=1, bleed=2, unknown=3)

Выходные файлы (перезаписывают существующие):
    analysis/ml/clf.pkl            — RandomForestClassifier (bounce/breakout)
    analysis/ml/reg.pkl            — RandomForestRegressor (expected fill_depth_pct)
    analysis/ml/label_encoder.pkl  — LabelEncoder классов
    analysis/ml/level_type_map.pkl — маппинг level_type → int
"""

import argparse
import os
import pickle
import sqlite3

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import classification_report
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import LabelEncoder

# Импортируем константу STYLE_MAP из ml_score, чтобы не дублировать
import sys
sys.path.insert(0, os.path.dirname(__file__))
from analysis.ml_score import STYLE_MAP

FEATURES = ["strength_claude", "ltype_enc", "vol_capped", "touches", "atr_capped", "style_enc"]


def load_data(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql(
        """
        SELECT *
        FROM level_outcomes
        WHERE outcome IN ('bounce', 'breakout')
          AND strength_claude != 0
          AND created_at >= '2026-05-21'
        """,
        conn,
    )
    conn.close()

    print(f"Строк после фильтрации: {len(df)}")
    if len(df) < 50:
        raise ValueError(f"Слишком мало данных для обучения: {len(df)} строк")

    print("Распределение исходов:")
    print(df["outcome"].value_counts(normalize=True).mul(100).round(1).to_string())
    print()

    # Диагностика touches — показываем что touches>=2 = 0% bounce
    print("bounce% по touches_count (первые 6 значений):")
    for tc, grp in list(df.groupby("touches_count"))[:6]:
        b = round((grp["outcome"] == "bounce").mean() * 100, 1)
        print(f"  touches={int(tc)}  bounce={b}%  n={len(grp)}")
    print()

    return df


def build_features(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Строит матрицу признаков, возвращает (X, level_type_map)."""
    level_type_map = {t: i for i, t in enumerate(sorted(df["level_type"].dropna().unique()))}

    df = df.copy()
    df["ltype_enc"]  = df["level_type"].map(level_type_map).fillna(1).astype(int)
    df["style_enc"]  = (
        df["approach_style"]
        .fillna("unknown")
        .map(STYLE_MAP)
        .fillna(STYLE_MAP["unknown"])
        .astype(int)
    )
    df["vol_capped"] = df["vol_ratio_at_touch"].clip(upper=20).fillna(1.0)
    df["atr_capped"] = df["atr_ratio"].clip(upper=20).fillna(1.0)
    df["touches"]    = df["touches_count"].fillna(0).clip(upper=5).astype(float)

    X = df[FEATURES].copy()

    print("Статистика признаков:")
    print(X.describe().round(2).to_string())
    print()

    # bounce% по approach_style
    print("bounce% по approach_style:")
    style_bounce = df.groupby("approach_style")["outcome"].apply(
        lambda s: (s == "bounce").mean()
    )
    counts = df["approach_style"].value_counts()
    for style in style_bounce.index:
        print(f"  {style:<10} bounce={style_bounce[style]:.1%}  n={counts.get(style, 0)}")
    print()

    # bounce% по level_type
    print("bounce% по level_type:")
    for lt, grp in df.groupby("level_type"):
        b = round((grp["outcome"] == "bounce").mean() * 100, 1)
        print(f"  {lt:<15} bounce={b}%  n={len(grp)}")
    print()

    return X, level_type_map


def train(db_path: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    df = load_data(db_path)
    X, level_type_map = build_features(df)

    # ── Классификатор ──────────────────────────────────────────────────
    le = LabelEncoder()
    y_clf = le.fit_transform(df["outcome"])

    print(f"Классы: {list(le.classes_)}")
    print()

    clf = RandomForestClassifier(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=5,
        random_state=42,
        class_weight="balanced",
        n_jobs=-1,
    )
    clf.fit(X, y_clf)

    bounce_idx = list(le.classes_).index("bounce")

    # Cross-val
    cv_scores = cross_val_score(clf, X, y_clf, cv=5, scoring="f1_macro")
    print(f"CV F1-macro: {cv_scores.mean():.3f} ± {cv_scores.std():.3f}")
    print()

    print("Классификация на трейне:")
    print(classification_report(y_clf, clf.predict(X), target_names=le.classes_))

    print("Важность признаков (классификатор):")
    for feat, imp in sorted(
        zip(FEATURES, clf.feature_importances_), key=lambda x: -x[1]
    ):
        print(f"  {feat:<20} {imp:.3f}")
    print()

    # ── Калибровка порогов по OOB / cross-val ────────────────────────
    # Используем predict_proba на трейне для оценки распределения.
    # Пороги выставляем по P25/P75 чтобы ~25% записей получали +1 и ~25% получали -1.
    all_proba = clf.predict_proba(X)[:, bounce_idx]
    p25 = np.percentile(all_proba, 25)
    p75 = np.percentile(all_proba, 75)
    print(f"p_bounce percentiles: P25={p25:.3f}  P75={p75:.3f}")
    print(f"  → пороги для ml_score.py: верхний={p75:.2f}  нижний={p25:.2f}")
    if not (0.50 <= p75 <= 0.99):
        print(f"  ⚠️  P75={p75:.3f} за пределами ожидаемого диапазона 0.50–0.99")
    print()

    # ── Регрессор ──────────────────────────────────────────────────────
    y_reg = df["fill_depth_pct"].fillna(0).clip(lower=0)
    reg = RandomForestRegressor(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=5,
        random_state=42,
        n_jobs=-1,
    )
    reg.fit(X, y_reg)
    preds = reg.predict(X)
    mae = np.mean(np.abs(preds - y_reg))
    print(f"Регрессор MAE на трейне: {mae:.3f}%")
    print()

    # ── Smoke-тест порогов ─────────────────────────────────────────────
    # Тест 1: body_level, touches=1 (типичный первый подход)
    print("Smoke-тест 1: body_level, touches=1 (strength=4, vol=1.5, atr=2.0):")
    body_enc = level_type_map.get("body_level", 0)
    for style_name, style_code in STYLE_MAP.items():
        x_test = pd.DataFrame(
            [[4, body_enc, 1.5, 1, 2.0, style_code]],
            columns=FEATURES,
        )
        proba  = clf.predict_proba(x_test)[0]
        p_b    = proba[bounce_idx]
        delta  = "+1" if p_b >= p75 else ("-1" if p_b <= p25 else " 0")
        print(f"  {style_name:<10} p_bounce={p_b:.3f}  ml_delta={delta}")
    print()

    # Тест 2: pump_base, touches=1 (второй по частоте тип)
    print("Smoke-тест 2: pump_base, touches=1 (strength=5, vol=1.0, atr=2.0):")
    pump_enc = level_type_map.get("pump_base", 1)
    for style_name, style_code in STYLE_MAP.items():
        x_test = pd.DataFrame(
            [[5, pump_enc, 1.0, 1, 2.0, style_code]],
            columns=FEATURES,
        )
        proba  = clf.predict_proba(x_test)[0]
        p_b    = proba[bounce_idx]
        delta  = "+1" if p_b >= p75 else ("-1" if p_b <= p25 else " 0")
        print(f"  {style_name:<10} p_bounce={p_b:.3f}  ml_delta={delta}")
    print()

    # Тест 3: touches=3 — должен давать -1 для всех стилей
    print("Smoke-тест 3: touches=3 → ожидается ml_delta=-1 для всех:")
    for style_name, style_code in STYLE_MAP.items():
        x_test = pd.DataFrame(
            [[4, body_enc, 1.5, 3, 2.0, style_code]],
            columns=FEATURES,
        )
        proba  = clf.predict_proba(x_test)[0]
        p_b    = proba[bounce_idx]
        delta  = "+1" if p_b >= p75 else ("-1" if p_b <= p25 else " 0")
        ok = "✅" if delta == "-1" else "❌"
        print(f"  {style_name:<10} p_bounce={p_b:.3f}  ml_delta={delta} {ok}")
    print()

    # ── Сохранение ─────────────────────────────────────────────────────
    pickle.dump(clf,            open(os.path.join(out_dir, "clf.pkl"),            "wb"))
    pickle.dump(reg,            open(os.path.join(out_dir, "reg.pkl"),            "wb"))
    pickle.dump(le,             open(os.path.join(out_dir, "label_encoder.pkl"),  "wb"))
    pickle.dump(level_type_map, open(os.path.join(out_dir, "level_type_map.pkl"), "wb"))

    print(f"✅ Модели сохранены в {out_dir}/")
    print(f"   Классы: {list(le.classes_)}")
    print(f"   level_type_map: {level_type_map}")
    print()
    print(f"📋 Обнови пороги в ml_score.py:")
    print(f"   THRESHOLD_HIGH = {p75:.2f}  # было 0.57")
    print(f"   THRESHOLD_LOW  = {p25:.2f}  # было 0.32")


def main() -> None:
    parser = argparse.ArgumentParser(description="Переобучение ML-моделей для ml_score.py")
    parser.add_argument("--db",  default="history.db",   help="Путь к history.db")
    parser.add_argument("--out", default="analysis/ml",  help="Папка для .pkl файлов")
    args = parser.parse_args()

    print(f"DB: {args.db}")
    print(f"Out: {args.out}")
    print()
    train(args.db, args.out)


if __name__ == "__main__":
    main()
