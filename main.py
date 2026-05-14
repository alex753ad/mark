"""Main orchestrator for trading bot."""

import asyncio
import time
import json
import os
from logger import logger
from constants import (
    TRIGGER_COOLDOWN_SECONDS,
    SCREENER_DELAY_SECONDS,
    SCREENER_MIN_VOLUME_USD,
    SCREENER_MIN_GROWTH_PCT,
    SCREENER_MIN_NATR,
    SCREENER_AUTO_INTERVAL_SECONDS,
    CLAUDE_MAX_CONCURRENT_REQUESTS,
    COLLECTOR_UPDATE_INTERVAL_SECONDS,
    PROXIMITY_ALERT_DISTANCE_PCT,
    PROXIMITY_ALERT_COOLDOWN_SECONDS,
)
from models import state_manager
from data.collector import start_collector, candles_1m
from analysis.trigger import (
    check_trigger, get_approaching_levels, calculate_strength,
    detect_approach_style, calculate_atr_ratio, get_vol_ratio_current,
    get_btc_change_1m, get_funding_rate,
)
from analysis.monitor import start_monitor
from bot.telegram import send_message, start_bot
from config import token_registry, validate_config, TRIGGER_TIMES_FILE
from data.history import init_db, save_level_outcome, update_symbol_profile, get_outcome_probs, log_event


# Global semaphore for Claude API rate limiting
claude_semaphore = asyncio.Semaphore(CLAUDE_MAX_CONCURRENT_REQUESTS)


def load_trigger_times() -> dict[str, float]:
    """Load trigger cooldown timestamps from file."""
    if os.path.exists(TRIGGER_TIMES_FILE):
        try:
            with open(TRIGGER_TIMES_FILE) as f:
                data = json.load(f)
                logger.info("Loaded trigger times", count=len(data))
                return data
        except Exception as e:
            logger.error("Failed to load trigger times", error=str(e))
            return {}
    return {}


def save_trigger_times(data: dict[str, float]):
    """Save trigger cooldown timestamps to file."""
    try:
        with open(TRIGGER_TIMES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.exception("Failed to save trigger times", error=str(e))


from analysis.screener import run_screener as _run_screener, _format_vol


async def _send_startup_screener():
    """Send market screener on bot startup."""
    await asyncio.sleep(SCREENER_DELAY_SECONDS)
    try:
        from datetime import datetime, timezone
        rows = await _run_screener()
        if not rows:
            logger.info("No symbols passed screener filter")
            return

        now_str = datetime.now(timezone.utc).strftime("%d.%m %H:%M")
        lines = [f"📊 Рынок  {now_str} UTC\n"]
        lines.append(f"{'TICKER':<10} {'CHG%':>6}  {'NATR':>4}  {'VOL':>6}")
        lines.append("─" * 34)
        for ticker, chg, natr, vol, _ in rows:
            lines.append(f"{ticker:<10} {chg:>+5.1f}  {natr:>4.1f}  {_format_vol(vol):>6}")

        await send_message("```\n" + "\n".join(lines) + "\n```")
        logger.info("Startup screener sent", symbols_count=len(rows))
    except Exception as e:
        logger.exception("Error in startup screener", error=str(e))


async def _auto_screener_loop():
    """Every 30 minutes scan market, auto-add new symbols, build levels and start monitoring."""
    from datetime import datetime, timezone
    await asyncio.sleep(SCREENER_AUTO_INTERVAL_SECONDS)  # skip first run

    while True:
        known_symbols = set(token_registry.get_all())
        try:
            rows = await _run_screener()
            if not rows:
                await asyncio.sleep(SCREENER_AUTO_INTERVAL_SECONDS)
                continue

            now_str = datetime.now(timezone.utc).strftime("%d.%m %H:%M")
            new_symbols = []

            for ticker, chg, natr, vol, sym in rows:
                if sym not in known_symbols:
                    token_registry.add(sym)
                    known_symbols.add(sym)
                    new_symbols.append((ticker, chg, natr, vol, sym))
                    logger.info("Auto-added new symbol from screener", symbol=sym)

            # For each new symbol: fetch data, build levels, start monitoring
            if new_symbols:
                from binance import AsyncClient
                from data.collector import _parse_kline, candles_1m, candles_15m
                from analysis.level_builder import build_levels
                from analysis.trigger import calculate_atr, calculate_strength, get_level_history, _count_approaches
                from analysis.claude_strength import calculate_strength_with_claude
                import json as _json

                client = await AsyncClient.create()
                try:
                    for ticker, chg, natr, vol, sym in new_symbols:
                        try:
                            raw_15m = await client.futures_klines(symbol=sym, interval="15m", limit=500)
                            raw_1m = await client.futures_klines(symbol=sym, interval="1m", limit=300)
                            candles_15m[sym] = [_parse_kline(k) for k in raw_15m]
                            candles_1m[sym] = [_parse_kline(k) for k in raw_1m]

                            ext_c1m = candles_1m[sym]
                            ext_c15m = candles_15m[sym]
                            all_levels = build_levels(sym, c1m_override=ext_c1m, c15m_override=ext_c15m)

                            if not all_levels:
                                continue

                            current_price = ext_c1m[-1]["close"]
                            atr = calculate_atr(sym)
                            range_limit = current_price * 0.20

                            supports = [
                                lvl for lvl in all_levels
                                if lvl["level"] < current_price
                                and (current_price - lvl["level"]) <= range_limit
                                and (current_price - lvl["level"]) >= atr * 1.5
                            ]

                            if not supports:
                                continue

                            for lvl in supports:
                                lvl["symbol"] = sym
                                lvl["approach"] = _count_approaches(sym, lvl["level"], atr) if atr > 0 else 0
                                if atr > 0:
                                    lvl.update(get_level_history(sym, lvl["level"], atr))
                                calculate_strength(lvl)
                                lvl["python_strength"] = lvl["strength"]

                            poc_price = next((l["level"] for l in supports if l.get("poc_aligned")), None)
                            supports = await calculate_strength_with_claude(sym, ext_c15m, supports, poc_price)

                            for lvl in supports:
                                py = lvl.get("python_strength", lvl["strength"])
                                if lvl.get("approach", 0) >= 2 or (lvl.get("was_broken") and not lvl.get("sweep_reclaimed")):
                                    lvl["strength"] = min(lvl["strength"], py)

                            strong = [l for l in supports if l["strength"] >= 4]
                            if not strong:
                                continue

                            sym_state = state_manager.get_state(sym)
                            started = []
                            for lvl in strong:
                                task_key = sym_state.make_task_key(lvl["level"])
                                if task_key not in sym_state.tasks:
                                    task = asyncio.create_task(
                                        _monitored(sym, lvl["level"], "support",
                                                  level_type=lvl["type"],
                                                  strength=lvl["strength"])
                                    )
                                    sym_state.add_task(lvl["level"], task)
                                    sym_state.phase = "phase2"
                                    started.append(lvl)

                            if started:
                                lines = [f"🆕 {sym} добавлен автоматически",
                                         f"   {chg:+.1f}% | NATR {natr:.1f}%\n"]
                                for lvl in started:
                                    stars = "⭐️" * lvl["strength"]
                                    reason = lvl.get("claude_reason", "")
                                    lines.append(f"   {stars} {lvl['level']} — {lvl['type']}")
                                    if reason:
                                        lines.append(f"   💭 {reason}")
                                lines.append(f"\n👁 Мониторинг запущен ({len(started)} ур.)")
                                await send_message("\n".join(lines))

                                await log_event(sym, "added_screener", f"chg={chg:+.1f}% natr={natr:.1f}%")
                                levels_info = [{"level": l["level"], "type": l["type"], "strength": l["strength"]} for l in started]
                                await log_event(sym, "levels_built", _json.dumps(levels_info))
                                for lvl in started:
                                    await log_event(sym, "monitoring_start",
                                                   f"level={lvl['level']} strength={lvl['strength']} type={lvl['type']}")
                                logger.info("Auto monitoring started", symbol=sym, levels=len(started))

                        except Exception as e:
                            logger.exception("Error setting up new symbol", symbol=sym, error=str(e))
                finally:
                    await client.close_connection()

            logger.info("Auto screener completed", total=len(rows), new=len(new_symbols))

        except Exception as e:
            logger.exception("Error in auto screener loop", error=str(e))

        await asyncio.sleep(SCREENER_AUTO_INTERVAL_SECONDS)



# Global set of symbols currently being processed in phase1
_building_levels: set[str] = set()


async def _trigger_loop():
    """Main loop checking for correction triggers."""
    trigger_times = load_trigger_times()

    while True:
        try:
            tokens = token_registry.get_all()
            for symbol in tokens:
                state = state_manager.get_state(symbol)

                # Skip if already building levels
                if state.phase == "phase1" or symbol in _building_levels:
                    continue

                # Check cooldown
                last = trigger_times.get(symbol, 0)
                if time.time() - last < TRIGGER_COOLDOWN_SECONDS:
                    continue

                # Check trigger condition
                if check_trigger(symbol):
                    _building_levels.add(symbol)
                    trigger_times[symbol] = time.time()
                    save_trigger_times(trigger_times)
                    logger.info("Trigger activated", symbol=symbol)
                    await _run_phase1(symbol)
                    
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Error in trigger loop", error=str(e))
            try:
                await send_message("⚠️ Ошибка в trigger loop, см. логи")
            except Exception:
                pass
        
        await asyncio.sleep(COLLECTOR_UPDATE_INTERVAL_SECONDS)



async def _run_phase1(symbol: str):
    """Phase 1: Build levels, start monitoring. Claude NOT used here - only on manual analyze/check."""
    state = state_manager.get_state(symbol)
    was_in_phase2 = state.phase == "phase2"
    # Don't change phase to "phase1" if already monitoring — avoids race condition
    # where a finishing monitor resets phase to "idle" mid-build
    if not was_in_phase2:
        state.phase = "phase1"

    _building_levels.add(symbol)
    try:
        levels = await get_approaching_levels(symbol, use_claude=False)

        c1m = candles_1m.get(symbol, [])
        current_price = c1m[-1]["close"] if c1m else 0

        # --- Stop stale monitors (levels now outside -20% range) ---
        if was_in_phase2 and current_price > 0:
            range_low = current_price * 0.80
            stale_levels = []
            for task_key in list(state.tasks.keys()):
                parts = task_key.rsplit("_", 1)
                if len(parts) != 2:
                    continue
                try:
                    monitored_level = float(parts[1])
                except ValueError:
                    continue
                if monitored_level < range_low:
                    stop_ev = state.stop_flags.get(task_key)
                    if stop_ev:
                        stop_ev.set()
                    task = state.tasks.get(task_key)
                    if task:
                        task.cancel()
                    state.remove_task(task_key)
                    stale_levels.append(monitored_level)
                    logger.info("Stale monitor stopped (out of range)",
                               symbol=symbol, level=monitored_level,
                               current_price=current_price)
            if stale_levels:
                levels_str = ", ".join(str(l) for l in sorted(stale_levels))
                await send_message(
                    f"🔄 {symbol} уровни вышли из диапазона — мониторинг снят\n"
                    f"   {levels_str}"
                )
            # Clear analyzed cache so new pump levels aren't blocked by old entries
            state.clear_analyzed_levels()

        if not levels:
            logger.debug("No approaching levels found", symbol=symbol)
            state.phase = "phase2" if state.has_active_tasks() else "idle"
            return

        for lvl in levels:
            lvl["symbol"] = symbol
            lvl["level_side"] = "support" if current_price > lvl["level"] else "resistance"

        # Filter out already analyzed levels
        new_levels = [
            lvl for lvl in levels
            if not state.is_level_analyzed(lvl["level"])
        ]

        if not new_levels:
            logger.debug("All levels already analyzed", symbol=symbol)
            state.phase = "phase2" if state.has_active_tasks() else "idle"
            return

        # Calculate strength with Python only
        for lvl in new_levels:
            calculate_strength(lvl)

        # Filter weak levels
        strong = [lvl for lvl in new_levels if lvl["strength"] >= 4]

        # Mark all as analyzed
        for lvl in new_levels:
            state.mark_level_analyzed(lvl["level"])

        if not strong:
            logger.info("No strong levels found", symbol=symbol, total_levels=len(new_levels))
            state.phase = "phase2" if state.has_active_tasks() else "idle"
            return

        # Send notifications and start monitoring — only the NEAREST strong level
        # Others will be picked up after breakout via _start_next_level_after_breakout
        strong_sorted = sorted(strong, key=lambda l: abs(current_price - l["level"]))
        nearest = strong_sorted[0]

        level_side = nearest.get("level_side", "support")
        task_key = state.make_task_key(nearest["level"])

        if task_key in state.tasks:
            logger.debug("Nearest level already monitored", symbol=symbol)
            state.phase = "phase2"
            return

        zone_approaches = nearest.get("zone_approaches", 0)
        atr_pct = nearest.get("atr_pct", 0)
        stars = "⭐️" * nearest["strength"]

        if was_in_phase2:
            text = (
                f"📋 {symbol} новый уровень после пампа\n"
                f"   {stars} {nearest['level']} — {nearest.get('type', '')}\n"
            )
        else:
            text = (
                f"📋 {symbol} коррекция началась\n"
                f"   Уровень {nearest['level']} — {nearest.get('type', '')} {stars}\n"
            )
        if zone_approaches >= 1:
            text += f"   ⚠️ Зона тестировалась {zone_approaches} раз(а) в радиусе {atr_pct:.2f}%\n"
        text += f"\n   Жду цену на {nearest['level']}..."

        await send_message(text)
        logger.info("Level monitoring started",
                   symbol=symbol, level=nearest['level'], strength=nearest['strength'])

        task = asyncio.create_task(
            _monitored(symbol, nearest["level"], level_side,
                       level_type=nearest["type"],
                       strength=nearest["strength"])
        )
        state.add_task(nearest["level"], task)
        state.phase = "phase2"

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("Error in phase1", symbol=symbol, error=str(e))
        state.phase = "phase2" if was_in_phase2 else "idle"
        try:
            await send_message(f"⚠️ Ошибка в phase1({symbol}), см. логи")
        except Exception:
            pass
    finally:
        _building_levels.discard(symbol)



async def _monitored(symbol: str, level: float, level_side: str,
                     level_type: str = "body_level",
                     strength: int = 0,
                     approach_style: str = None, atr_ratio: float = None,
                     vol_ratio: float = None):
    """Monitor a level until breakout or manual stop."""
    state = state_manager.get_state(symbol)
    task_key = state.make_task_key(level)
    stop_event = state.stop_flags.get(task_key)

    start_time = time.time()
    reason = None  # ensure defined for finally block
    outcome = None
    fill_depth_pct = 0.0
    m_approach_style = approach_style
    m_atr_ratio = atr_ratio
    m_vol_ratio = vol_ratio

    try:
        monitor_result = await start_monitor(
            symbol, level, level_side, stop_event,
            approach_style=approach_style,
            atr_ratio=atr_ratio,
            vol_ratio=vol_ratio,
        )
        duration = int((time.time() - start_time) / 60)

        if isinstance(monitor_result, dict):
            reason = monitor_result.get("reason")
            outcome = monitor_result.get("outcome")
            fill_depth_pct = monitor_result.get("fill_depth_pct", 0.0)
            m_approach_style = monitor_result.get("approach_style")
            m_atr_ratio = monitor_result.get("atr_ratio")
            m_vol_ratio = monitor_result.get("vol_ratio_at_touch")
        else:
            reason = monitor_result
            outcome = "breakout" if reason == "breakout" else None

        result = "пробой" if reason == "breakout" else "отбой"

        # Clear analyzed cache on breakout so level can be re-evaluated
        if reason == "breakout":
            state.analyzed_levels.discard(f"{symbol}:{level}")

        btc_change = get_btc_change_1m()
        funding = await get_funding_rate(symbol)

        try:
            from analysis.trigger import _calc_vol_ratio, _count_approaches, calculate_atr
            atr = calculate_atr(symbol)
            vol_ratio_old = _calc_vol_ratio(symbol)
            touches = _count_approaches(symbol, level, atr) if atr > 0 else 1
            await save_level_outcome(
                symbol=symbol, level=level, level_type=level_type,
                strength=strength, approach_type=level_side,
                vol_ratio=vol_ratio_old, touches=touches,
                result=result, duration=duration,
                outcome=outcome,
                approach_style=m_approach_style,
                vol_ratio_at_touch=m_vol_ratio,
                atr_ratio=m_atr_ratio,
                fill_depth_pct=fill_depth_pct,
                btc_change_1m=btc_change,
                funding_rate=funding,
            )
            await update_symbol_profile(symbol)
            logger.info("Level outcome saved",
                       symbol=symbol, level=level, result=result,
                       outcome=outcome, duration=duration)
            if outcome == "bounce":
                await log_event(symbol, "bounce", f"level={level} fill_depth={fill_depth_pct:.2f}% duration={duration}m")
            elif outcome == "breakout":
                await log_event(symbol, "breakout", f"level={level} duration={duration}m")
        except Exception as e:
            logger.exception("Failed to save history", task_key=task_key, error=str(e))

    except asyncio.CancelledError:
        logger.info("Monitor cancelled", symbol=symbol, level=level)
    except Exception as e:
        logger.exception("Error in monitor", symbol=symbol, level=level, error=str(e))
    finally:
        state.remove_task(task_key)

        # On breakout — immediately look for next level below, regardless of other active monitors
        if reason == "breakout" and token_registry.contains(symbol):
            try:
                await _start_next_level_after_breakout(symbol, level)
            except Exception as e:
                logger.exception("Error finding next level after breakout", symbol=symbol, error=str(e))

        # Update phase
        if not state.has_active_tasks():
            state.phase = "idle"



async def _start_next_level_after_breakout(symbol: str, broken_level: float):
    """After a breakout, find and start monitoring the next level below.

    Priority:
    1. Levels from _last_analysis_cache (shown to user via /analyze)
    2. Rebuild levels from candle data
    If nothing found — check screener and possibly remove symbol.
    """
    from data.collector import candles_1m as _c1m, candles_15m as _c15m
    from analysis.trigger import calculate_atr, calculate_strength, get_level_history, _count_approaches
    from bot.telegram import _last_analysis_cache

    state = state_manager.get_state(symbol)
    ext_c1m = _c1m.get(symbol, [])
    current_price = ext_c1m[-1]["close"] if ext_c1m else 0
    atr = calculate_atr(symbol)

    def _in_range(lvl_price: float) -> bool:
        if current_price <= 0:
            return False
        dist = current_price - lvl_price
        return (
            lvl_price < broken_level
            and 0 < dist <= current_price * 0.20
            and (atr == 0 or dist >= atr * 1.5)
        )

    next_started = False

    # --- Priority 1: cached levels from last /analyze ---
    cached = _last_analysis_cache.get(symbol, [])
    candidates = [l for l in cached if _in_range(l["level"]) and l.get("strength", 0) >= 4]

    if candidates:
        nearest = min(candidates, key=lambda l: abs(current_price - l["level"]))
        task_key = state.make_task_key(nearest["level"])
        if task_key not in state.tasks:
            task = asyncio.create_task(
                _monitored(symbol, nearest["level"], "support",
                           level_type=nearest["type"],
                           strength=nearest["strength"])
            )
            state.add_task(nearest["level"], task)
            state.phase = "phase2"
            stars = "⭐️" * nearest["strength"]
            await send_message(
                f"📋 {symbol} следующий уровень\n"
                f"   {stars} {nearest['level']} — {nearest['type']}\n"
                f"👁 Мониторинг запущен"
            )
            await log_event(symbol, "monitoring_start",
                           f"level={nearest['level']} strength={nearest['strength']} (after breakout of {broken_level})")
            next_started = True
            logger.info("Next level from cache started", symbol=symbol, level=nearest["level"])

    # --- Priority 2: rebuild from candles ---
    if not next_started and ext_c1m:
        from analysis.level_builder import build_levels
        ext_c15m = _c15m.get(symbol, [])
        all_levels = build_levels(symbol, c1m_override=ext_c1m, c15m_override=ext_c15m)

        rebuild_candidates = [lvl for lvl in all_levels if _in_range(lvl["level"])]
        for lvl in rebuild_candidates:
            lvl["symbol"] = symbol
            lvl["approach"] = _count_approaches(symbol, lvl["level"], atr) if atr > 0 else 0
            if atr > 0:
                lvl.update(get_level_history(symbol, lvl["level"], atr))
            calculate_strength(lvl)

        strong = [l for l in rebuild_candidates if l["strength"] >= 4]
        if strong:
            nearest = min(strong, key=lambda l: abs(current_price - l["level"]))
            task_key = state.make_task_key(nearest["level"])
            if task_key not in state.tasks:
                task = asyncio.create_task(
                    _monitored(symbol, nearest["level"], "support",
                               level_type=nearest["type"],
                               strength=nearest["strength"])
                )
                state.add_task(nearest["level"], task)
                state.phase = "phase2"
                stars = "⭐️" * nearest["strength"]
                await send_message(
                    f"📋 {symbol} следующий уровень\n"
                    f"   {stars} {nearest['level']} — {nearest['type']}\n"
                    f"👁 Мониторинг запущен"
                )
                await log_event(symbol, "monitoring_start",
                               f"level={nearest['level']} strength={nearest['strength']} (rebuilt, after breakout of {broken_level})")
                next_started = True
                logger.info("Next level rebuilt and started", symbol=symbol, level=nearest["level"])

    # --- No level found ---
    if not next_started:
        try:
            rows = await _run_screener()
            screener_symbols = {sym for _, _, _, _, sym in rows}
            if symbol not in screener_symbols:
                token_registry.remove(symbol)
                await log_event(symbol, "removed",
                               f"breakout at level={broken_level} + dropped from screener")
                await send_message(
                    f"🗑 {symbol} удалён из списка\n"
                    f"   Все уровни пробиты и монета выпала из скринера"
                )
            else:
                await log_event(symbol, "breakout",
                               f"level={broken_level} — no next level, still in screener")
        except Exception as e:
            logger.error("Failed to check screener after breakout", symbol=symbol, error=str(e))


def cancel_tasks_for_symbol(symbol: str):
    """Cancel all monitoring tasks for a symbol."""
    state = state_manager.get_state(symbol)
    state.cancel_all_tasks()
    logger.info("All tasks cancelled", symbol=symbol)


def clear_analysis_cache(symbol: str):
    """Clear analyzed levels cache for a symbol."""
    state = state_manager.get_state(symbol)
    state.clear_analyzed_levels()
    logger.info("Analysis cache cleared", symbol=symbol)


async def _proximity_loop():
    """Loop checking for proximity alerts."""
    while True:
        try:
            all_tasks = state_manager.get_all_active_tasks()
            
            for task_key, task in list(all_tasks.items()):
                # Parse task_key: "SYMBOL_LEVEL"
                parts = task_key.rsplit("_", 1)
                if len(parts) != 2:
                    continue
                    
                symbol, level_str = parts
                try:
                    level = float(level_str)
                except ValueError:
                    continue
                
                state = state_manager.get_state(symbol)
                
                # Check if task is stopped
                stop_event = state.stop_flags.get(task_key)
                if stop_event and stop_event.is_set():
                    continue
                
                # Get current price
                c1m = candles_1m.get(symbol, [])
                if not c1m:
                    continue
                    
                current_price = c1m[-1]["close"]
                if level == 0:
                    continue
                    
                distance_pct = abs(current_price - level) / level * 100

                now = time.time()
                last_sent = state.proximity_notified.get(task_key, 0)

                # Only alert if price is approaching from above (for support)
                # and hasn't already bounced (price still near or below level)
                approaching = current_price > level  # price above support = approaching
                cooldown_ok = (now - last_sent) > PROXIMITY_ALERT_COOLDOWN_SECONDS

                if distance_pct <= PROXIMITY_ALERT_DISTANCE_PCT * 100 and approaching and cooldown_ok:
                    await send_message(
                        f"🎯 {symbol} цена в {distance_pct:.2f}% от уровня {level} — готовь ордер"
                    )
                    state.proximity_notified[task_key] = now
                    logger.info("Proximity alert sent", 
                               symbol=symbol, 
                               level=level, 
                               distance_pct=distance_pct)

            # Track touches on weak (unmonitored) levels from cache
            from bot.telegram import _last_analysis_cache
            for symbol, cached_levels in list(_last_analysis_cache.items()):
                c1m = candles_1m.get(symbol, [])
                if len(c1m) < 5:
                    continue
                current_price = c1m[-1]["close"]
                sym_state = state_manager.get_state(symbol)
                monitored = {float(k.rsplit("_", 1)[1]) for k in sym_state.tasks if "_" in k}

                for lvl_info in cached_levels:
                    lvl_price = lvl_info["level"]
                    strength = lvl_info.get("strength", 0)

                    if lvl_price in monitored:
                        continue  # already fully monitored
                    if strength >= 4:
                        continue  # strong levels are monitored separately
                    if lvl_price >= current_price:
                        continue  # only support levels below price

                    touch_key = f"weak_touch_{symbol}_{lvl_price}"
                    resolve_key = f"weak_resolve_{symbol}_{lvl_price}"

                    from analysis.trigger import calculate_atr
                    atr = calculate_atr(symbol)
                    touch_zone = atr * 0.5 if atr > 0 else lvl_price * 0.005

                    price_touched = current_price <= lvl_price + touch_zone

                    if price_touched:
                        if touch_key not in sym_state.proximity_notified:
                            # First touch — record candle index and min price
                            sym_state.proximity_notified[touch_key] = len(c1m) - 1
                            sym_state.proximity_notified[f"weak_min_{symbol}_{lvl_price}"] = c1m[-1]["low"]
                        else:
                            # Update min price during touch
                            prev_min = sym_state.proximity_notified.get(f"weak_min_{symbol}_{lvl_price}", lvl_price)
                            sym_state.proximity_notified[f"weak_min_{symbol}_{lvl_price}"] = min(prev_min, c1m[-1]["low"])
                    else:
                        # Price moved away — resolve if we had a touch
                        touch_idx = sym_state.proximity_notified.get(touch_key)
                        if touch_idx is not None and resolve_key not in sym_state.proximity_notified:
                            min_price = sym_state.proximity_notified.get(f"weak_min_{symbol}_{lvl_price}", lvl_price)
                            fill_depth = (lvl_price - min_price) / lvl_price * 100 if min_price < lvl_price else 0.0

                            # Determine outcome: bounce or breakout
                            # Check if price returned above level
                            post_touch = c1m[int(touch_idx):]
                            returned_above = any(c["close"] > lvl_price for c in post_touch[-10:])
                            stayed_below = all(c["close"] < lvl_price for c in post_touch[-5:]) if len(post_touch) >= 5 else False

                            if returned_above:
                                event = "zakol" if fill_depth >= 0.3 else "bounce"
                                await log_event(symbol, event,
                                               f"level={lvl_price} strength={strength} depth={fill_depth:.2f}% (weak, unmonitored)")
                                logger.info("Weak level touch resolved",
                                           symbol=symbol, level=lvl_price,
                                           event=event, fill_depth=fill_depth)
                            elif stayed_below:
                                await log_event(symbol, "breakout",
                                               f"level={lvl_price} strength={strength} (weak, unmonitored)")
                                logger.info("Weak level broken",
                                           symbol=symbol, level=lvl_price)

                            # Mark resolved, clean up touch state
                            sym_state.proximity_notified[resolve_key] = time.time()
                            sym_state.proximity_notified.pop(touch_key, None)
                            sym_state.proximity_notified.pop(f"weak_min_{symbol}_{lvl_price}", None)

                        # Reset resolve flag after price moves far away (> 2 ATR)
                        if resolve_key in sym_state.proximity_notified:
                            dist = current_price - lvl_price
                            if atr > 0 and dist > atr * 2:
                                sym_state.proximity_notified.pop(resolve_key, None)
                    
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Error in proximity loop", error=str(e))
            
        await asyncio.sleep(COLLECTOR_UPDATE_INTERVAL_SECONDS)



async def shutdown():
    """Graceful shutdown of all bot components."""
    logger.info("Shutting down gracefully...")
    
    # Save tasks before cancelling, then wait for them
    all_tasks = state_manager.get_all_active_tasks()
    state_manager.cancel_all_tasks()
    if all_tasks:
        await asyncio.gather(*all_tasks.values(), return_exceptions=True)
    
    try:
        await send_message("🛑 Бот остановлен")
    except Exception:
        pass
        
    logger.info("Shutdown complete")


async def _startup_monitoring():
    """On startup, build levels and start monitoring for all symbols in list."""
    # Wait for collector to load candle data
    await asyncio.sleep(30)

    tokens = token_registry.get_all()
    if not tokens:
        return

    logger.info("Starting monitoring for existing symbols", count=len(tokens))

    from binance import AsyncClient
    from data.collector import _parse_kline, candles_1m, candles_15m
    from analysis.level_builder import build_levels
    from analysis.trigger import calculate_atr, calculate_strength, get_level_history, _count_approaches
    import json as _json

    client = await AsyncClient.create()
    try:
        for symbol in tokens:
            try:
                raw_15m = await client.futures_klines(symbol=symbol, interval="15m", limit=500)
                raw_1m = await client.futures_klines(symbol=symbol, interval="1m", limit=300)
                candles_15m[symbol] = [_parse_kline(k) for k in raw_15m]
                candles_1m[symbol] = [_parse_kline(k) for k in raw_1m]

                ext_c1m = candles_1m[symbol]
                ext_c15m = candles_15m[symbol]
                all_levels = build_levels(symbol, c1m_override=ext_c1m, c15m_override=ext_c15m)

                if not all_levels:
                    logger.debug("No levels on startup", symbol=symbol)
                    continue

                current_price = ext_c1m[-1]["close"]
                atr = calculate_atr(symbol)
                range_limit = current_price * 0.20

                supports = [
                    lvl for lvl in all_levels
                    if lvl["level"] < current_price
                    and (current_price - lvl["level"]) <= range_limit
                    and (current_price - lvl["level"]) >= atr * 1.5
                ]

                if not supports:
                    continue

                for lvl in supports:
                    lvl["symbol"] = symbol
                    lvl["approach"] = _count_approaches(symbol, lvl["level"], atr) if atr > 0 else 0
                    if atr > 0:
                        lvl.update(get_level_history(symbol, lvl["level"], atr))
                    calculate_strength(lvl)
                    lvl["python_strength"] = lvl["strength"]

                # Startup: use Python only, no Claude (save tokens)
                strong = [l for l in supports if l["strength"] >= 4]
                if not strong:
                    logger.debug("No strong levels on startup", symbol=symbol)
                    continue

                # Monitor only the nearest level — others picked up after breakout
                nearest = min(strong, key=lambda l: abs(current_price - l["level"]))

                # Log levels built
                levels_info = [{"level": l["level"], "type": l["type"], "strength": l["strength"]} for l in strong]
                await log_event(symbol, "levels_built", _json.dumps(levels_info))

                sym_state = state_manager.get_state(symbol)
                task_key = sym_state.make_task_key(nearest["level"])
                if task_key not in sym_state.tasks:
                    task = asyncio.create_task(
                        _monitored(symbol, nearest["level"], "support",
                                  level_type=nearest["type"],
                                  strength=nearest["strength"])
                    )
                    sym_state.add_task(nearest["level"], task)
                    sym_state.phase = "phase2"
                    await log_event(symbol, "monitoring_start",
                                   f"level={nearest['level']} strength={nearest['strength']} type={nearest['type']} (startup)")
                    logger.info("Startup monitoring started", symbol=symbol, level=nearest["level"])

            except Exception as e:
                logger.exception("Error starting monitoring for symbol on startup",
                               symbol=symbol, error=str(e))
    finally:
        await client.close_connection()


async def main():
    """Main entry point."""
    # Validate configuration
    if not validate_config():
        logger.error("Configuration validation failed")
        return

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Setup signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    try:
        import signal
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(shutdown()))
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown()))
    except (NotImplementedError, AttributeError):
        # Windows doesn't support add_signal_handler
        logger.warning("Signal handlers not available on this platform")

    logger.info("Starting trading bot...")
    
    # Start all components
    await asyncio.gather(
        start_collector(),
        start_bot(),
        _trigger_loop(),
        _proximity_loop(),
        _auto_screener_loop(),
        _startup_monitoring(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down")
