"""Level monitoring with breakout/rebound detection."""

import asyncio
import time
from data.collector import candles_15m, candles_1m, start_delta_tracking, stop_delta_tracking, get_delta, _stream_agg_trades
from bot.telegram import send_message
from constants import (
    VOLUME_BREAKOUT_RATIO,
    VOLUME_SPIKE_RATIO,
    VOLUME_SPIKE_RESET_RATIO,
    DISTANCE_RESET_ATR_MULTIPLIER,
    DISTANCE_PARTIAL_RESET_ATR_MULTIPLIER,
    WEAK_BREAKOUT_COOLDOWN_SECONDS,
    COLLECTOR_UPDATE_INTERVAL_SECONDS,
    PRESSURE_MIN_DIRECTIONAL_CANDLES,
    PRESSURE_ZONE_MIN_DISTANCE_PCT,
    PRESSURE_ZONE_MAX_DISTANCE_PCT,
    PRESSURE_VOLUME_MIN_RATIO,
    LEVEL_BROKEN_MIN_CANDLES,
)
from logger import logger


async def _handle_sweep(symbol: str, level: float, level_side: str, c1m: list[dict]):
    reclaim_vol = c1m[-1]["volume"]
    avg_vol = sum(c["volume"] for c in c1m[-20:]) / min(len(c1m), 20)
    ratio = round(reclaim_vol / avg_vol, 1) if avg_vol > 0 else 1.0
    if ratio >= 2.0:
        await send_message(f"🟡 {symbol} sweep + выкуп на {level}\n   Объём выкупа ×{ratio} — уровень держится")
    else:
        await send_message(f"🟡 {symbol} sweep на {level}\n   Объём слабый ×{ratio} — уровень ослаб")


async def start_monitor(
    symbol: str,
    level: float,
    level_side: str,
    stop_event: asyncio.Event = None,
    # profile fields for outcome saving
    approach_style: str = None,
    atr_ratio: float = None,
    vol_ratio: float = None,
) -> str | None:
    """Monitor a level until body of 1M candle breaks it.
    level_side: 'support' or 'resistance'
    Returns 'breakout' if level was broken with volume, None otherwise.
    """
    from analysis.trigger import calculate_atr
    atr = calculate_atr(symbol)

    touched = False
    approach_warned = False
    weak_breakout_sent = False
    weak_breakout_time = 0.0
    rebound_sent = False
    volume_spike_notified = False
    sweep_sent = False
    engulf_sent = False
    level_broken_sent = False
    classify_sent = False  # prevent duplicate _classify_and_log_level_event calls
    iteration = 0
    delta_stream_task = None
    delta_signal_sent = False
    touch_c1m_idx = 0  # index in c1m when touch happened
    touch_classify_at = 0  # c1m index when to classify (touch_idx + 5)

    # Track min price during monitoring for fill_depth_pct
    min_price_during = None
    max_price_during = None

    def _make_result(reason, _touched=False):
        """Build result dict with outcome info."""
        fdp = 0.0
        if level > 0:
            if level_side == "support" and min_price_during is not None:
                fdp = (level - min_price_during) / level * 100
            elif level_side == "resistance" and max_price_during is not None:
                fdp = (max_price_during - level) / level * 100
            fdp = max(fdp, 0.0)

        if reason == "breakout":
            outcome = "breakout"
        elif not _touched:
            outcome = "no_reach"
        elif fdp < 0.3:
            outcome = "partial"
        else:
            outcome = "bounce"

        return {
            "reason": reason,
            "outcome": outcome,
            "fill_depth_pct": round(fdp, 4),
            "approach_style": approach_style,
            "atr_ratio": atr_ratio,
            "vol_ratio_at_touch": vol_ratio,
        }

    while True:
        if stop_event and stop_event.is_set():
            return _make_result(None, touched)

        iteration += 1
        if iteration % 60 == 0:
            atr = calculate_atr(symbol)

        c1m = candles_1m.get(symbol, [])
        if c1m:
            last = c1m[-1]
            body_close = last["close"]
            body_open = last["open"]
            body_bottom = min(body_close, body_open)
            body_top = max(body_close, body_open)

            # Track extremes for fill_depth_pct
            if min_price_during is None or last["low"] < min_price_during:
                min_price_during = last["low"]
            if max_price_during is None or last["high"] > max_price_during:
                max_price_during = last["high"]

            if level_side == "support" and body_close < level:
                avg_vol = sum(c["volume"] for c in c1m[-20:]) / min(len(c1m), 20)
                breakout_vol_ratio = last["volume"] / avg_vol if avg_vol > 0 else 1.0
                # Confirm breakout: need 2 consecutive closes below level to avoid zakol
                prev_close_below = len(c1m) >= 2 and c1m[-2]["close"] < level
                if breakout_vol_ratio >= VOLUME_BREAKOUT_RATIO and prev_close_below:
                    await send_message(
                        f"💥 {symbol} пробой {level} с объёмом ×{breakout_vol_ratio:.1f} — настоящий, выход"
                    )
                    return _make_result("breakout", touched)
                elif breakout_vol_ratio >= VOLUME_BREAKOUT_RATIO and not prev_close_below:
                    # High volume but only 1 candle below — possible zakol, wait for confirmation
                    now = time.time()
                    if not weak_breakout_sent or (now - weak_breakout_time) > WEAK_BREAKOUT_COOLDOWN_SECONDS:
                        await send_message(
                            f"⚠️ {symbol} закол {level} с объёмом ×{breakout_vol_ratio:.1f} — ждём подтверждения"
                        )
                        weak_breakout_sent = True
                        weak_breakout_time = now
                else:
                    now = time.time()
                    if not weak_breakout_sent or (now - weak_breakout_time) > WEAK_BREAKOUT_COOLDOWN_SECONDS:
                        await send_message(
                            f"⚠️ {symbol} пробой {level} на слабом объёме (×{breakout_vol_ratio:.1f}) — возможен sweep, наблюдаем"
                        )
                        weak_breakout_sent = True
                        weak_breakout_time = now
            elif level_side == "support" and body_close >= level:
                weak_breakout_sent = False

            if level_side == "resistance" and body_close > level:
                avg_vol = sum(c["volume"] for c in c1m[-20:]) / min(len(c1m), 20)
                breakout_vol_ratio = last["volume"] / avg_vol if avg_vol > 0 else 1.0
                prev_close_above = len(c1m) >= 2 and c1m[-2]["close"] > level
                if breakout_vol_ratio >= VOLUME_BREAKOUT_RATIO and prev_close_above:
                    await send_message(
                        f"💥 {symbol} пробой {level} с объёмом ×{breakout_vol_ratio:.1f} — настоящий, выход"
                    )
                    return _make_result("breakout", touched)
                elif breakout_vol_ratio >= VOLUME_BREAKOUT_RATIO and not prev_close_above:
                    now = time.time()
                    if not weak_breakout_sent or (now - weak_breakout_time) > WEAK_BREAKOUT_COOLDOWN_SECONDS:
                        await send_message(
                            f"⚠️ {symbol} закол {level} с объёмом ×{breakout_vol_ratio:.1f} — ждём подтверждения"
                        )
                        weak_breakout_sent = True
                        weak_breakout_time = now
                else:
                    now = time.time()
                    if not weak_breakout_sent or (now - weak_breakout_time) > WEAK_BREAKOUT_COOLDOWN_SECONDS:
                        await send_message(
                            f"⚠️ {symbol} пробой {level} на слабом объёме (×{breakout_vol_ratio:.1f}) — возможен sweep, наблюдаем"
                        )
                        weak_breakout_sent = True
                        weak_breakout_time = now
            elif level_side == "resistance" and body_close <= level:
                weak_breakout_sent = False

            if level_side == "support" and last["low"] <= level * 1.002:
                if not touched:
                    # Start delta tracking on first touch
                    start_delta_tracking(symbol)
                    if delta_stream_task is None or delta_stream_task.done():
                        delta_stream_task = asyncio.create_task(_stream_agg_trades(symbol))
                    touch_c1m_idx = len(c1m) - 1
                    touch_classify_at = touch_c1m_idx + 5  # classify after 5 x 1M candles
                    logger.debug("Delta tracking started on touch", symbol=symbol, level=level)
                touched = True
            if level_side == "resistance" and last["high"] >= level * 0.998:
                if not touched:
                    start_delta_tracking(symbol)
                    if delta_stream_task is None or delta_stream_task.done():
                        delta_stream_task = asyncio.create_task(_stream_agg_trades(symbol))
                    touch_c1m_idx = len(c1m) - 1
                    touch_classify_at = touch_c1m_idx + 5
                touched = True

            # Classify touch event after 5 x 1M candles
            if touched and touch_classify_at > 0 and len(c1m) >= touch_classify_at and not rebound_sent:
                if not classify_sent:
                    asyncio.create_task(_classify_and_log_level_event(
                        symbol, level, c1m, candles_15m.get(symbol, []),
                        min_price_during or level, touch_c1m_idx
                    ))
                    classify_sent = True
                touch_classify_at = 0  # reset so we don't classify again

            # Delta signal: buy pressure absorbing sells at support
            if touched and not delta_signal_sent:
                d = get_delta(symbol, window_seconds=30)
                if d["trades"] >= 10:  # enough data
                    if level_side == "support" and d["delta"] > 0 and d["buy_vol"] > d["sell_vol"] * 1.5:
                        await send_message(
                            f"⚡ {symbol} дельта разворот у {level}\n"
                            f"   Buy {d['buy_vol']:.1f} vs Sell {d['sell_vol']:.1f} за 30с\n"
                            f"   Покупатели поглощают продажи — вход"
                        )
                        delta_signal_sent = True
                        logger.info("Delta reversal signal sent", symbol=symbol, level=level,
                                   buy=d["buy_vol"], sell=d["sell_vol"])
                    elif level_side == "resistance" and d["delta"] < 0 and d["sell_vol"] > d["buy_vol"] * 1.5:
                        await send_message(
                            f"⚡ {symbol} дельта разворот у {level}\n"
                            f"   Sell {d['sell_vol']:.1f} vs Buy {d['buy_vol']:.1f} за 30с\n"
                            f"   Продавцы давят — шорт"
                        )
                        delta_signal_sent = True

            if level_side == "support" and touched and body_close > body_open and body_close > level:
                avg_vol = sum(c["volume"] for c in c1m[-20:]) / min(len(c1m), 20)
                if not rebound_sent:
                    if not classify_sent:
                        asyncio.create_task(_classify_and_log_level_event(
                            symbol, level, c1m, candles_15m.get(symbol, []),
                            min_price_during or level, touch_c1m_idx
                        ))
                        classify_sent = True
                    rebound_sent = True
                    touched = False
                    delta_signal_sent = False
                    stop_delta_tracking(symbol)
                    if last["volume"] > avg_vol:
                        await send_message(
                            f"✅ {symbol} отбой от {level} подтверждён — цена выкупается"
                        )

            if level_side == "resistance" and touched and body_close < body_open and body_close < level:
                avg_vol = sum(c["volume"] for c in c1m[-20:]) / min(len(c1m), 20)
                if not rebound_sent:
                    if not classify_sent:
                        asyncio.create_task(_classify_and_log_level_event(
                            symbol, level, c1m, candles_15m.get(symbol, []),
                            max_price_during or level, touch_c1m_idx
                        ))
                        classify_sent = True
                    rebound_sent = True
                    touched = False
                    delta_signal_sent = False
                    stop_delta_tracking(symbol)
                    if last["volume"] > avg_vol:
                        await send_message(
                            f"✅ {symbol} отбой от {level} подтверждён — цена отбита вниз"
                        )

            current_price = last["close"]
            if atr > 0:
                distance = abs(current_price - level)
                if distance > atr * DISTANCE_RESET_ATR_MULTIPLIER:
                    # If touched but price moved away without confirmed rebound — classify
                    if touched and not rebound_sent and not classify_sent:
                        asyncio.create_task(_classify_and_log_level_event(
                            symbol, level, c1m, candles_15m.get(symbol, []),
                            min_price_during or level, touch_c1m_idx
                        ))
                        classify_sent = True
                    # Check near_miss: came within 0.5% but never touched
                    elif not touched and min_price_during is not None and not classify_sent:
                        dist_pct = (level - min_price_during) / level * 100 if level > min_price_during else 0
                        if 0 < dist_pct <= 0.5:
                            asyncio.create_task(_classify_and_log_level_event(
                                symbol, level, c1m, candles_15m.get(symbol, []),
                                min_price_during, touch_c1m_idx
                            ))
                            classify_sent = True
                    rebound_sent = False
                    approach_warned = False
                    touched = False
                    classify_sent = False  # reset for next touch
                    engulf_sent = False
                    level_broken_sent = False
                    delta_signal_sent = False
                    stop_delta_tracking(symbol)
                elif distance > atr * DISTANCE_PARTIAL_RESET_ATR_MULTIPLIER:
                    # Don't reset rebound_sent here — prevents spam
                    touched = False

            avg_vol_20 = sum(c["volume"] for c in c1m[-20:]) / min(len(c1m), 20)
            if volume_spike_notified and avg_vol_20 > 0 and last["volume"] / avg_vol_20 < VOLUME_SPIKE_RESET_RATIO:
                volume_spike_notified = False

            is_sweep = _check_sweep_reclaim(c1m, level, level_side)
            if is_sweep and not sweep_sent:
                await _handle_sweep(symbol, level, level_side, c1m)
                sweep_sent = True
            elif not is_sweep:
                sweep_sent = False

        alert, alert_type = _check_complications(symbol, level, level_side, approach_warned, volume_spike_notified, engulf_sent, level_broken_sent, weak_breakout_sent)
        if alert:
            await send_message(alert)
            if alert_type == "pressure":
                approach_warned = True
            elif alert_type == "volume_spike":
                volume_spike_notified = True
            elif alert_type == "engulf":
                engulf_sent = True
            elif alert_type == "level_broken":
                level_broken_sent = True

        await asyncio.sleep(COLLECTOR_UPDATE_INTERVAL_SECONDS)

    # Cleanup delta tracking when monitor exits
    stop_delta_tracking(symbol)
    if delta_stream_task and not delta_stream_task.done():
        delta_stream_task.cancel()


# Dedup guard: prevent multiple classify calls for the same touch event
_classify_last_sent: dict[str, float] = {}  # key: "symbol:level" -> timestamp


async def _classify_and_log_level_event(
    symbol: str,
    level: float,
    c1m: list[dict],
    c15m: list[dict],
    min_price: float,
    touch_time_idx: int,  # index in c1m when touch happened
):
    """
    Classify what happened at the level and log to history + send message.

    Categories:
    - near_miss: price came within 0.5% but didn't touch
    - bounce: touched and returned above on 1M
    - zakol: pierced but returned above within 1M
    - zakol_deep: pierced >1%, check retest within 5 x 1M
    - breakout: 15M candle closed below OR price moved to next level
    """
    import time as _time
    dedup_key = f"{symbol}:{level}"
    now = _time.time()
    if now - _classify_last_sent.get(dedup_key, 0) < 60:  # 60s cooldown per level
        return
    _classify_last_sent[dedup_key] = now
    from data.history import log_event

    if not c1m or level == 0:
        return

    current_price = c1m[-1]["close"]
    fill_depth_pct = (level - min_price) / level * 100 if min_price < level else 0.0

    # Get candles after touch
    post_touch = c1m[touch_time_idx:touch_time_idx + 20] if touch_time_idx < len(c1m) else []

    # --- NEAR MISS ---
    if fill_depth_pct < 0.1:  # didn't actually touch
        dist_pct = (current_price - level) / level * 100
        if 0 < dist_pct <= 0.5:
            details = f"level={level} min_price={min_price:.6f} dist={dist_pct:.2f}%"
            await log_event(symbol, "near_miss", details)
            await send_message(
                f"⚠️ {symbol} не дошёл до уровня {level}\n"
                f"   Минимум {min_price:.6f} — в {dist_pct:.2f}% от уровня\n"
                f"   Уровень возможно некорректный"
            )
        return

    # --- ZAKOL or BOUNCE ---
    # Check if price returned above level within 1M candles after touch
    returned_above = any(c["close"] > level for c in post_touch[:5])

    if returned_above:
        if fill_depth_pct >= 1.0:
            # Deep zakol — check retest within 5 x 1M
            retest_candles = post_touch[1:6]  # next 5 candles after return
            retest = any(
                abs(c["low"] - level) / level * 100 <= 0.3
                for c in retest_candles
            )
            if retest:
                details = f"level={level} depth={fill_depth_pct:.2f}% retest=yes"
                await log_event(symbol, "zakol_deep_retest", details)
                await send_message(
                    f"🟡 {symbol} глубокий закол у {level}\n"
                    f"   Глубина -{fill_depth_pct:.2f}% | Ретест снизу подтверждён\n"
                    f"   Уровень держится — вход актуален"
                )
            else:
                details = f"level={level} depth={fill_depth_pct:.2f}% retest=no"
                await log_event(symbol, "zakol_deep", details)
                await send_message(
                    f"🟡 {symbol} глубокий закол у {level}\n"
                    f"   Глубина -{fill_depth_pct:.2f}% | Ретест не подтверждён\n"
                    f"   Уровень ослаблен"
                )
        else:
            # Regular zakol
            details = f"level={level} depth={fill_depth_pct:.2f}%"
            await log_event(symbol, "zakol", details)
            await send_message(
                f"🟢 {symbol} закол у {level}\n"
                f"   Глубина -{fill_depth_pct:.2f}% | Возврат выше уровня\n"
                f"   Уровень держится"
            )
    else:
        # Bounce — only log, main loop sends the message
        details = f"level={level} fill_depth={fill_depth_pct:.2f}%"
        await log_event(symbol, "bounce", details)


def _check_complications(symbol: str, level: float, level_side: str, approach_warned: bool = False, volume_spike_notified: bool = False, engulf_sent: bool = False, level_broken_sent: bool = False, weak_breakout_active: bool = False) -> tuple[str | None, str | None]:
    """Check for various complication patterns during monitoring."""
    c1m = candles_1m.get(symbol, [])
    c15m = candles_15m.get(symbol, [])

    if len(c1m) < 10 or len(c15m) < 2:
        return None, None

    if not approach_warned:
        vol_trend = _check_volume_trend_approach(symbol, level, level_side)
        if vol_trend:
            return vol_trend, "pressure"

    if not level_broken_sent and not weak_breakout_active:
        broken = _check_level_broken(c1m, level)
        if broken:
            return (
                f"🔴 {symbol} осложнение\n"
                f"   Промежуточный уровень пробит без отскока\n"
                f"   → импульс сильный, твой уровень под угрозой"
            ), "level_broken"

    sweep = _check_sweep_reclaim(c1m, level, level_side)
    if sweep:
        return None, None

    return None, None


def _check_volume_spike(c1m: list[dict]) -> int | None:
    if len(c1m) < 10:
        return None

    current = c1m[-1]
    if current["close"] >= current["open"]:
        return None

    avg_vol = sum(c["volume"] for c in c1m[-60:]) / len(c1m[-60:]) if len(c1m) >= 60 else sum(c["volume"] for c in c1m) / len(c1m)

    if avg_vol == 0:
        return None

    ratio = current["volume"] / avg_vol
    if ratio >= VOLUME_SPIKE_RATIO:
        return int(ratio)
    return None


def _check_engulfing(c15m: list[dict]) -> bool:
    if len(c15m) < 2:
        return False

    prev = c15m[-2]
    curr = c15m[-1]

    prev_is_green = prev["close"] > prev["open"]
    curr_is_red = curr["close"] < curr["open"]

    if not prev_is_green or not curr_is_red:
        return False

    return curr["open"] >= prev["close"] and curr["close"] <= prev["open"]


def _check_level_broken(c1m: list[dict], level: float) -> bool:
    """Check if level was broken by multiple candles."""
    recent = c1m[-LEVEL_BROKEN_MIN_CANDLES:]
    if not all(c["close"] < level for c in recent):
        return False
    avg_vol = sum(c["volume"] for c in c1m[-20:]) / min(len(c1m), 20)
    return recent[-1]["volume"] > avg_vol


def _check_sweep_reclaim(c1m: list[dict], level: float, level_side: str = "support") -> bool:
    if len(c1m) < 3:
        return False

    prev = c1m[-2]
    curr = c1m[-1]

    if level_side == "support":
        swept = prev["low"] < level and prev["close"] < level
        reclaimed = curr["close"] > level and curr["volume"] > prev["volume"]
    else:
        swept = prev["high"] > level and prev["close"] > level
        reclaimed = curr["close"] < level and curr["volume"] > prev["volume"]

    return swept and reclaimed


def _check_volume_trend_approach(symbol: str, level: float, level_side: str = "support") -> str | None:
    """Check for growing volume pressure as price approaches level."""
    c1m = candles_1m.get(symbol, [])
    c15m = candles_15m.get(symbol, [])
    if len(c1m) < 5 or not c15m:
        return None

    current_price = c1m[-1]["close"]
    if level == 0:
        return None
    distance_pct = abs(current_price - level) / level * 100
    if not (PRESSURE_ZONE_MIN_DISTANCE_PCT * 100 <= distance_pct <= PRESSURE_ZONE_MAX_DISTANCE_PCT * 100):
        return None

    recent = c1m[-5:]
    if level_side == "support":
        directional_candles = [c for c in recent if c["close"] < c["open"]]
    else:
        directional_candles = [c for c in recent if c["close"] > c["open"]]
    if len(directional_candles) < PRESSURE_MIN_DIRECTIONAL_CANDLES:
        return None

    volumes = [c["volume"] for c in directional_candles]
    growing = all(volumes[i] < volumes[i + 1] for i in range(len(volumes) - 1))
    if not growing:
        return None

    avg_vol = sum(c["volume"] for c in c1m[-20:]) / min(len(c1m), 20)
    last_vol_ratio = round(volumes[-1] / avg_vol, 1) if avg_vol > 0 else 1.0

    last_15m = c15m[-1]
    avg_15m = sum(c["volume"] for c in c15m[-20:]) / min(len(c15m), 20)
    if level_side == "support":
        confirmed_15m = last_15m["close"] < last_15m["open"] and last_15m["volume"] > avg_15m * PRESSURE_VOLUME_MIN_RATIO
    else:
        confirmed_15m = last_15m["close"] > last_15m["open"] and last_15m["volume"] > avg_15m * PRESSURE_VOLUME_MIN_RATIO

    if confirmed_15m:
        return (
            f"🔴 {symbol} продавец наращивает давление на подходе к {level}\n"
            f"   Объём 1М растёт ×{last_vol_ratio}, подтверждено 15М\n"
            f"   → вход рискованный, возможен пробой"
        )
    else:
        return None
