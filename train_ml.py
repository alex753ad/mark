"""
Скрипт переобучения ML-моделей для ml_score.py.

Запуск:
    python train_ml.py [--db history.db] [--out analysis/ml]

Фильтрация датасета:
    - Только строки с outcome IS NOT NULL
    - Исключаем strength_claude = 0 (уровни без Claude-оценки, bounce 18% vs 40%+)
    - Исключаем записи до 2026-05-21 (аномальный медвежий режим, breakout 35% vs 14-17%)

Признаки (6 штук, порядок должен совпадать с ml_score.py):
    1. strength_claude  — сила уровня (1-5)
    2. ltype_enc        — тип уровня (pump_base / body_level / wick_level / order_block)
    3. vol_capped       — vol_ratio_at_touch, обрезан до 20
    4. touches          — touches_count
    5. atr_capped       — atr_ratio, обрезан до 20
    6. style_enc        — approach_style (flash=0, impulse=1, bleed=2, unknown=3)

Выходные файлы (перезаписывают существующие):
    analysis/ml/clf.pkl            — RandomForestClassifier (bounce/breakout/partial)
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
        WHERE outcome IS NOT NULL
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

    # Показываем влияние approach_style на bounce
    print("bounce% по approach_style:")
    style_bounce = df.groupby("approach_style")["outcome"].apply(
        lambda s: (s == "bounce").mean()
    )
    counts = df["approach_style"].value_counts()
    for style in style_bounce.index:
        print(f"  {style:<10} bounce={style_bounce[style]:.1%}  n={counts.get(style, 0)}")
    print()

    return X, level_type_map


def train(db_path: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    df = load_data(db_path)
    X, level_type_map = build_features(df)

    # ── Классификатор ──────────────────────────────────────────────────
    le = LabelEncoder()
    y_clf = le.fit_transform(df["outcome"])

    clf = RandomForestClassifier(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=5,
        random_state=42,
        class_weight="balanced",
        n_jobs=-1,
    )
    clf.fit(X, y_clf)

    # Cross-val на bounce-class
    bounce_idx = list(le.classes_).index("bounce")
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
    print("Smoke-тест: p_bounce по стилям (strength=4, body_level, vol=1.5, atr=2.0):")
    body_enc = level_type_map.get("body_level", 1)
    for style_name, style_code in STYLE_MAP.items():
        x_test = pd.DataFrame(
            [[4, body_enc, 1.5, 1, 2.0, style_code]],
            columns=FEATURES,
        )
        proba  = clf.predict_proba(x_test)[0]
        p_b    = proba[bounce_idx]
        delta  = "+1" if p_b >= 0.57 else ("-1" if p_b <= 0.32 else " 0")
        print(f"  {style_name:<10} p_bounce={p_b:.3f}  ml_delta={delta}")
    print()

    # Проверяем что 75-й перцентиль ≈ 0.55-0.65
    all_proba = clf.predict_proba(X)[:, bounce_idx]
    p75 = np.percentile(all_proba, 75)
    p25 = np.percentile(all_proba, 25)
    print(f"p_bounce percentiles: P25={p25:.3f}  P75={p75:.3f}")
    if not (0.45 <= p75 <= 0.75):
        print(f"  ⚠️  P75={p75:.3f} за пределами ожидаемого диапазона 0.45–0.75")
    print()

    # ── Сохранение ─────────────────────────────────────────────────────
    pickle.dump(clf,            open(os.path.join(out_dir, "clf.pkl"),            "wb"))
    pickle.dump(reg,            open(os.path.join(out_dir, "reg.pkl"),            "wb"))
    pickle.dump(le,             open(os.path.join(out_dir, "label_encoder.pkl"),  "wb"))
    pickle.dump(level_type_map, open(os.path.join(out_dir, "level_type_map.pkl"), "wb"))

    print(f"✅ Модели сохранены в {out_dir}/")
    print(f"   Классы: {list(le.classes_)}")
    print(f"   level_type_map: {level_type_map}")


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
