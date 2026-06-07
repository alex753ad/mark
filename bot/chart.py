"""Генерация графика 1M свечей для уведомлений о закрытии сделки."""

from __future__ import annotations

import io
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd


def generate_close_chart(
    symbol: str,
    candles: list[dict],          # список {"open","high","low","close","volume","time"}
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    level: Optional[float] = None,
    n_candles: int = 120,         # сколько свечей показывать
) -> bytes:
    """
    Вернуть PNG-байты графика в стиле образца:
    - чёрный фон
    - свечи (зелёные/красные)
    - объём внизу (те же цвета)
    - скользящая средняя 20 (пунктир жёлтый)
    - Volume Profile справа (горизонтальные бары)
    - POC линия + подпись
    - горизонтальные линии entry/exit/level если переданы
    """
    if not candles or len(candles) < 2:
        return b""

    data = candles[-n_candles:] if len(candles) > n_candles else candles

    # --- DataFrame ---
    df = pd.DataFrame(data)
    # time может быть timestamp в мс или с
    if df["time"].iloc[0] > 1e12:
        df["time"] = pd.to_datetime(df["time"], unit="ms")
    else:
        df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("time")
    df = df[["open", "high", "low", "close", "volume"]].astype(float)

    opens  = df["open"].values
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    vols   = df["volume"].values
    n      = len(df)
    x      = np.arange(n)

    # --- Volume Profile ---
    price_min = lows.min()
    price_max = highs.max()
    n_bins    = 40
    bins      = np.linspace(price_min, price_max, n_bins + 1)
    vp        = np.zeros(n_bins)
    for i in range(n):
        lo, hi, vol = lows[i], highs[i], vols[i]
        for b in range(n_bins):
            overlap = min(hi, bins[b + 1]) - max(lo, bins[b])
            if overlap > 0 and (hi - lo) > 0:
                vp[b] += vol * overlap / (hi - lo)

    poc_bin  = int(np.argmax(vp))
    poc_price = (bins[poc_bin] + bins[poc_bin + 1]) / 2

    # --- Layout ---
    fig = plt.figure(figsize=(13, 7), facecolor="#0d0d0d")
    # 2 rows: candles (70%) + volume (30%)
    # 2 cols: chart (88%) + volume profile (12%)
    gs = fig.add_gridspec(
        2, 2,
        height_ratios=[3, 1],
        width_ratios=[10, 1.4],
        hspace=0.04, wspace=0.02,
        left=0.06, right=0.97, top=0.93, bottom=0.06,
    )
    ax_c  = fig.add_subplot(gs[0, 0])  # candles
    ax_v  = fig.add_subplot(gs[1, 0], sharex=ax_c)  # volume
    ax_vp = fig.add_subplot(gs[0, 1], sharey=ax_c)  # volume profile

    for ax in (ax_c, ax_v, ax_vp):
        ax.set_facecolor("#0d0d0d")
        ax.tick_params(colors="#888888", labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor("#333333")

    # --- Candles ---
    bull_col = "#40e0c0"   # cyan-green как на образце
    bear_col = "#e05060"   # красный

    for i in range(n):
        is_bull = closes[i] >= opens[i]
        col     = bull_col if is_bull else bear_col
        body_lo = min(opens[i], closes[i])
        body_hi = max(opens[i], closes[i])
        body_h  = max(body_hi - body_lo, (price_max - price_min) * 0.001)
        ax_c.add_patch(mpatches.Rectangle(
            (i - 0.35, body_lo), 0.7, body_h,
            facecolor=col, edgecolor=col, linewidth=0,
        ))
        ax_c.plot([i, i], [lows[i], highs[i]], color=col, linewidth=0.8)

    # --- MA20 ---
    ma_len = 20
    if n >= ma_len:
        ma = pd.Series(closes).rolling(ma_len).mean().values
        ax_c.plot(x, ma, color="#f5a623", linewidth=1.0, linestyle="--", alpha=0.85, zorder=3)

    # --- Горизонтальные уровни ---
    def _hline(ax, price, color, label, style="-"):
        ax.axhline(price, color=color, linewidth=0.8, linestyle=style, alpha=0.75)
        ax.text(n - 1, price, f" {label} {price:.6g}",
                color=color, fontsize=6.5, va="center", ha="right", zorder=5)

    if level is not None:
        _hline(ax_c, level, "#ffe066", "LVL", style=":")
    if entry_price is not None:
        _hline(ax_c, entry_price, "#40c0e0", "ENTRY")
    if exit_price is not None:
        col_exit = "#40e080" if exit_price >= (entry_price or exit_price) else "#e05060"
        _hline(ax_c, exit_price, col_exit, "EXIT")

    # --- POC ---
    ax_c.axhline(poc_price, color="#44cc66", linewidth=0.7, linestyle=":", alpha=0.8)

    # --- Volume ---
    vol_colors = [bull_col if closes[i] >= opens[i] else bear_col for i in range(n)]
    ax_v.bar(x, vols, color=vol_colors, width=0.8, alpha=0.85)
    ax_v.set_ylim(0, vols.max() * 1.1)

    # --- Volume Profile bars ---
    bar_h = (bins[1] - bins[0]) * 0.85
    max_vp = vp.max() if vp.max() > 0 else 1
    for b in range(n_bins):
        bar_w = vp[b] / max_vp
        mid   = (bins[b] + bins[b + 1]) / 2
        color = "#44cc66" if b == poc_bin else "#c04040"
        ax_vp.add_patch(mpatches.Rectangle(
            (0, mid - bar_h / 2), bar_w, bar_h,
            facecolor=color, alpha=0.75, linewidth=0,
        ))
    ax_vp.set_xlim(0, 1.05)
    ax_vp.text(0.05, poc_price, f"POC {poc_price:.6g}",
               color="#44cc66", fontsize=6.5, va="center", ha="left",
               transform=ax_vp.get_yaxis_transform())
    ax_vp.set_xticks([])
    ax_vp.yaxis.set_visible(False)

    # --- X-axis ticks ---
    tick_step = max(1, n // 8)
    ax_v.set_xticks(x[::tick_step])
    ax_v.set_xticklabels(
        [df.index[i].strftime("%H:%M") for i in range(0, n, tick_step)],
        color="#888888", fontsize=7,
    )
    plt.setp(ax_c.get_xticklabels(), visible=False)

    # --- Y-axis ---
    ax_c.yaxis.tick_right()
    ax_c.yaxis.set_label_position("right")
    ax_v.yaxis.tick_right()
    ax_v.set_ylabel("Vol", color="#666666", fontsize=7, labelpad=2)
    ax_v.yaxis.set_label_position("right")

    # --- Title ---
    fig.text(0.07, 0.955, f"{symbol}  1M", color="#dddddd",
             fontsize=11, fontweight="bold", va="top")

    # --- Grid ---
    ax_c.grid(axis="y", color="#222222", linewidth=0.5, linestyle="-")
    ax_v.grid(axis="y", color="#222222", linewidth=0.5, linestyle="-")

    ax_c.set_xlim(-0.8, n + 0.2)
    ax_c.margins(y=0.04)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor="#0d0d0d")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
