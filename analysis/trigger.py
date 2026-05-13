"""Trigger detection and level strength calculation."""

from data.collector import candles_15m, candles_1m
from analysis.level_builder import build_levels
from constants import (
    TRIGGER_GROWTH_THRESHOLD,
    LEVEL_APPROACH_THRESHOLD,
    LEVEL_REAL_CLUSTER_MIN_TOUCHES,
    LEVEL_REAL_CLUSTER_SHIFT_THRESHOLD,
    ATR_PERIOD,
    STRENGTH_PUMP_VOLUME_LOW_THRESHOLD,
    STRENGTH_PUMP_VOLUME_HIGH_THRESHOLD,
    STRENGTH_APPROACH_EXIT_THRESHOLD,
)
from logger import logger
import statistics
import asyncio


def find_real_level(symbol: str, level: float) -> tuple[float, int]:
    """
    Find real cluster of touches near the given level using 15M candles.
    Counts touches only after the last pump peak to avoid inflated numbers.
    """
    c1m = candles_1m.get(symbol, [])
    c15m = candles_15m.get(symbol, [])
    if len(c1m) < ATR_PERIOD or not c15m:
        return level, 0

    current_price = c1m[-1]["close"]
    atr = calculate_atr(symbol)
    if atr == 0 or current_price == 0:
        return level, 0

    zone_radius = atr * 0.3  # Fixed radius: 0.3 ATR

    # Find pump peak time - count touches only after pump
    pump_high = max(c["high"] for c in c15m)
    pump_peak_time = next((c["open_time"] for c in c15m if c["high"] >= pump_high * 0.999), None)

    touches = []
    for c in c15m:
        # Only count after pump peak
        if pump_peak_time and c["open_time"] < pump_peak_time:
            continue
        # One touch per candle - use the closest extreme to the level
        dist_low = abs(c["low"] - level)
        dist_high = abs(c["high"] - level)
        if dist_low <= zone_radius and dist_low <= dist_high:
            touches.append(c["low"])
        elif dist_high <= zone_radius:
            touches.append(c["high"])

    if len(touches) < LEVEL_REAL_CLUSTER_MIN_TOUCHES:
        return level, 0

    real_level = statistics.median(touches)
    if abs(real_level - level) > zone_radius * LEVEL_REAL_CLUSTER_SHIFT_THRESHOLD:
        from analysis.level_builder import _round_level
        logger.debug("Level adjusted by cluster",
                    symbol=symbol,
                    original=level,
                    adjusted=real_level,
                    touches=len(touches))
        return _round_level(real_level), len(touches)

    return level, len(touches)


def calculate_atr(symbol: str) -> float:
    """Calculate Average True Range for symbol."""
    c1m = candles_1m.get(symbol, [])
    if len(c1m) < ATR_PERIOD:
        return 0.0
    recent = c1m[-ATR_PERIOD:]
    tr_sum = sum(c["high"] - c["low"] for c in recent)
    return tr_sum / ATR_PERIOD


def calculate_atr_pct(symbol: str) -> float:
    """Calculate ATR as percentage of current price."""
    c1m = candles_1m.get(symbol, [])
    if len(c1m) < ATR_PERIOD:
        return 0.0
    recent = c1m[-ATR_PERIOD:]
    tr_sum = sum(c["high"] - c["low"] for c in recent)
    atr = tr_sum / ATR_PERIOD
    current_price = c1m[-1]["close"]
    if current_price == 0:
        return 0.0
    return (atr / current_price) * 100


def check_trigger(symbol: str) -> bool:
    """
    Check if correction trigger is activated.
    
    Trigger conditions:
    - Growth >= TRIGGER_GROWTH_THRESHOLD on last 4x15M candles
    - Last 1M candle is red (close < open)
    """
    c15m = candles_15m.get(symbol, [])
    c1m = candles_1m.get(symbol, [])
    if len(c15m) < 4 or len(c1m) < 2:
        return False

    recent_15m = c15m[-4:]
    start_price = recent_15m[0]["open"]
    high_price = max(c["high"] for c in recent_15m)
    if start_price == 0:
        return False
    growth = (high_price - start_price) / start_price
    if growth < TRIGGER_GROWTH_THRESHOLD:
        return False

    last_1m = c1m[-1]
    if last_1m["close"] >= last_1m["open"]:
        return False

    logger.debug("Trigger conditions met", 
                symbol=symbol, 
                growth_pct=round(growth * 100, 2))
    return True


async def get_approaching_levels(symbol: str, use_claude: bool = True) -> list[dict]:
    """
    Get levels that are approaching current price.
    
    Args:
        symbol: Trading symbol
        use_claude: If True, use Claude Haiku for strength calculation
    
    Returns:
        List of level dicts with technical indicators calculated.
    """
    c1m = candles_1m.get(symbol, [])
    if not c1m:
        return []

    current_price = c1m[-1]["close"]
    atr = calculate_atr(symbol)
    if atr == 0:
        return []

    threshold = atr * LEVEL_APPROACH_THRESHOLD
    levels = build_levels(symbol)
    approaching = []

    for lvl in levels:
        real, _ = find_real_level(symbol, lvl["level"])
        lvl["level"] = real
        distance = abs(current_price - lvl["level"])
        if distance <= threshold:
            lvl["approach"] = _count_approaches(symbol, lvl["level"], atr)
            lvl["vol_ratio"] = _calc_vol_ratio(symbol)
            history = get_level_history(symbol, lvl["level"], atr)
            lvl.update(history)
            approaching.append(lvl)

    atr_pct = calculate_atr_pct(symbol)
    zone_radius = atr_pct / 100 * current_price if current_price > 0 else 0

    for lvl in approaching:
        nearby = [
            other for other in levels
            if other["level"] != lvl["level"]
            and abs(other["level"] - lvl["level"]) <= zone_radius
        ]
        zone_approaches = sum(
            _count_approaches(symbol, other["level"], atr)
            for other in nearby
        )
        lvl["zone_approaches"] = zone_approaches
        lvl["atr_pct"] = round(atr_pct, 3)

    # Calculate strength with Claude or fallback to Python
    if use_claude and approaching:
        try:
            c15m = candles_15m.get(symbol, [])
            
            # Extract POC from levels
            poc_price = None
            for lvl in levels:
                if lvl.get("poc_aligned"):
                    poc_price = lvl["level"]
                    break
            
            # Import here to avoid circular dependency
            from analysis.claude_strength import calculate_strength_with_claude
            
            # Call async function directly (we're already in async context)
            approaching = await calculate_strength_with_claude(symbol, c15m, approaching, poc_price)
            
            logger.info("Claude strength calculation completed",
                       symbol=symbol,
                       levels_count=len(approaching))
        except Exception as e:
            logger.error("Failed to use Claude, falling back to Python",
                        symbol=symbol,
                        error=str(e))
            # Fallback to Python calculation
            for lvl in approaching:
                calculate_strength(lvl)
    else:
        # Use Python calculation
        for lvl in approaching:
            calculate_strength(lvl)

    logger.debug("Approaching levels found", 
                symbol=symbol, 
                count=len(approaching), 
                threshold=round(threshold, 4))
    return approaching


def _count_approaches(symbol: str, level: float, atr: float) -> int:
    """Count number of times price approached the level after pump peak."""
    c1m = candles_1m.get(symbol, [])
    c15m = candles_15m.get(symbol, [])
    threshold = atr * LEVEL_APPROACH_THRESHOLD

    # Find pump peak time from 15M candles
    pump_high_time = None
    if c15m:
        pump_high = max(c["high"] for c in c15m)
        for c in c15m:
            if c["high"] >= pump_high * 0.999:
                pump_high_time = c["open_time"]
                break

    count = 0
    was_near = False

    for c in c1m:
        # Count approaches only after pump peak
        if pump_high_time and c["open_time"] < pump_high_time:
            was_near = False
            continue
        near = (
            abs(c["low"] - level) <= threshold or
            abs(c["close"] - level) <= threshold
        )
        if near and not was_near:
            count += 1
        was_near = near

    return count


def get_level_history(symbol: str, level: float, atr: float) -> dict:
    """Get historical behavior of a level."""
    c1m = candles_1m.get(symbol, [])
    threshold = atr * LEVEL_APPROACH_THRESHOLD

    was_broken = False
    sweep_reclaimed = False
    price_min = None
    max_vol_on_approach = 0.0

    for c in c1m:
        if price_min is None or c["low"] < price_min:
            price_min = c["low"]

        near = (
            abs(c["low"] - level) <= threshold or
            abs(c["close"] - level) <= threshold or
            c["low"] < level
        )
        if near:
            if c["volume"] > max_vol_on_approach:
                max_vol_on_approach = c["volume"]

        if c["close"] < level:
            was_broken = True

        if was_broken and c["close"] > level:
            sweep_reclaimed = True

    return {
        "was_broken": was_broken,
        "sweep_reclaimed": sweep_reclaimed,
        "price_min_since_level": price_min if price_min is not None else level,
        "max_vol_on_approach": round(max_vol_on_approach, 2),
    }


def get_breakout_info(symbol: str, level: float) -> dict:
    """Возвращает информацию о пробое уровня."""
    c15m = candles_15m.get(symbol, [])
    if not c15m:
        return {"type": "breakout", "zakol_pct": 0, "rebound_pct": 0}

    zakol_candles = [
        c for c in c15m
        if c["low"] < level and c["high"] >= level
    ]

    if zakol_candles:
        min_low = min(c["low"] for c in zakol_candles)
        zakol_pct = round((level - min_low) / level * 100, 2)

        after_zakol = [c for c in c15m if c["open_time"] >= zakol_candles[-1]["open_time"]]
        closes_above = [c["close"] for c in after_zakol if c["close"] > level]
        rebound_pct = round((max(closes_above) - level) / level * 100, 2) if closes_above else 0

        if rebound_pct > 0:
            return {"type": "zakol", "zakol_pct": zakol_pct, "rebound_pct": rebound_pct}

    return {"type": "breakout", "zakol_pct": 0, "rebound_pct": 0}


def detect_approach_style(symbol: str, n_candles: int = 5) -> str:
    """Classify how price is approaching a level based on recent 1M candles."""
    c1m = candles_1m.get(symbol, [])
    if len(c1m) < max(n_candles, 20):
        return "unknown"

    recent = c1m[-n_candles:]
    avg_vol_20 = sum(c["volume"] for c in c1m[-20:]) / 20

    # flash: one candle with volume >= 2x avg AND body >= 0.5%
    for c in recent:
        if avg_vol_20 > 0 and c["volume"] >= 2 * avg_vol_20:
            body_pct = abs(c["close"] - c["open"]) / c["open"] * 100 if c["open"] > 0 else 0
            if body_pct >= 0.5:
                return "flash"

    # impulse: 3+ green candles then red reversal
    if len(recent) >= 4:
        greens_before = sum(1 for c in recent[:-1] if c["close"] > c["open"])
        last_is_red = recent[-1]["close"] < recent[-1]["open"]
        if greens_before >= 3 and last_is_red:
            return "impulse"

    # bleed: 4+ red candles in a row, volume growing
    red_streak = []
    for c in recent:
        if c["close"] < c["open"]:
            red_streak.append(c)
        else:
            red_streak = []
    if len(red_streak) >= 4:
        vols = [c["volume"] for c in red_streak]
        if all(vols[i] <= vols[i + 1] for i in range(len(vols) - 1)):
            return "bleed"

    return "unknown"


def calculate_atr_ratio(symbol: str, level: float) -> float:
    """Distance from current price to level divided by ATR(14) on 1M."""
    c1m = candles_1m.get(symbol, [])
    if not c1m:
        return 0.0
    atr = calculate_atr(symbol)
    if atr == 0:
        return 0.0
    current_price = c1m[-1]["close"]
    return round(abs(current_price - level) / atr, 2)


def get_vol_ratio_current(symbol: str) -> float:
    """Volume of last 1M candle / MA20 volume of 1M candles."""
    c1m = candles_1m.get(symbol, [])
    if len(c1m) < 20:
        return 1.0
    avg_vol_20 = sum(c["volume"] for c in c1m[-20:]) / 20
    if avg_vol_20 == 0:
        return 1.0
    return round(c1m[-1]["volume"] / avg_vol_20, 2)


def get_btc_change_1m() -> float:
    """BTC % change over the last 1M candle."""
    btc = candles_1m.get("BTCUSDT", [])
    if len(btc) < 2:
        return 0.0
    prev_close = btc[-2]["close"]
    if prev_close == 0:
        return 0.0
    return round((btc[-1]["close"] - prev_close) / prev_close * 100, 4)


async def get_funding_rate(symbol: str) -> float | None:
    """Fetch latest funding rate from Binance. Returns None on error."""
    try:
        from binance import AsyncClient
        client = await AsyncClient.create()
        try:
            data = await client.futures_funding_rate(symbol=symbol, limit=1)
            if data:
                return float(data[-1]["fundingRate"])
        finally:
            await client.close_connection()
    except Exception:
        logger.debug("Failed to fetch funding rate", symbol=symbol)
    return None


def _calc_vol_ratio(symbol: str) -> float:
    c1m = candles_1m.get(symbol, [])
    if len(c1m) < 2:
        return 1.0

    current_vol = c1m[-1]["volume"]

    avg_24h = sum(c["volume"] for c in c1m) / len(c1m) if c1m else 1.0

    recent_60 = c1m[-60:] if len(c1m) >= 60 else c1m
    avg_1h = sum(c["volume"] for c in recent_60) / len(recent_60) if recent_60 else 1.0

    avg = (avg_24h + avg_1h) / 2
    if avg == 0:
        return 1.0
    return round(current_vol / avg, 2)


def calculate_strength(lvl: dict) -> dict:
    """
    Calculate strength (1-5) and verdict for a level.
    
    CRITICAL FACTORS (in order of importance):
    1. POC alignment (+2 strength)
    2. Level type (pump_base=5, consolidation_base=4, body_level=4, etc.)
    3. Hourly open alignment (+1 strength if >= 2 bonus)
    4. Round number proximity (+1 strength if bonus >= 2)
    5. Position (origin +1)
    6. Candle count (more candles = more reliable)
    
    Mutates and returns the same dict with added fields:
    - strength: int (1-5)
    - verdict: str ("hold" | "exit" | "exit_fast")
    """
    level_type = lvl.get("type", "body_level")
    position = lvl.get("position", "mid_move")
    approach = lvl.get("approach", 1)
    vol_ratio = lvl.get("vol_ratio", 1.0)
    cluster = lvl.get("cluster", False)
    pump_volume_ratio = lvl.get("pump_volume_ratio", 1.5)
    was_broken = lvl.get("was_broken", False)
    sweep_reclaimed = lvl.get("sweep_reclaimed", False)
    max_vol_on_approach = lvl.get("max_vol_on_approach", 0)
    zone_approaches = lvl.get("zone_approaches", 0)
    engulf_15m = lvl.get("engulf_15m", False)
    poc_aligned = lvl.get("poc_aligned", False)
    hourly_open_bonus = lvl.get("hourly_open_bonus", 0)
    round_number_bonus = lvl.get("round_number_bonus", 0)
    candle_count = lvl.get("candle_count", 0)

    # Base strength by level type
    if level_type == "pump_base":
        strength = 5
    elif level_type == "consolidation_base":
        strength = 4
    elif level_type == "body_level":
        strength = 4
    elif level_type == "order_block":
        strength = 4
    elif level_type == "consolidation":
        strength = 3
    else:
        strength = 2

    verdict = "hold"

    # POC alignment is CRITICAL - strongest factor
    if poc_aligned:
        strength += 2
        logger.debug("POC aligned bonus", level=lvl.get("level"), bonus=2)

    # Hourly open alignment — meaningful only for pump_base and order_block
    # For body_level it's noise (large clusters always contain an hourly candle)
    if hourly_open_bonus >= 2 and level_type in ("pump_base", "order_block", "consolidation_base"):
        strength += 1
        logger.debug("Hourly open bonus", level=lvl.get("level"), bonus=hourly_open_bonus)

    # Round number proximity
    if round_number_bonus >= 2:  # Very close to round number
        strength += 1
        logger.debug("Round number bonus", level=lvl.get("level"), bonus=round_number_bonus)

    # Candle count bonus (more touches = more reliable, but capped)
    if 5 <= candle_count <= 15:
        strength += 1
    elif candle_count > 15:
        strength += 0  # Too many = just a wide consolidation zone, no extra bonus
    elif candle_count <= 2:
        strength -= 1  # Too few candles = weak level

    # Approach count
    if approach >= STRENGTH_APPROACH_EXIT_THRESHOLD:
        strength = 2
        verdict = "exit"

    # Position bonus — only origin (pump base) gets a bonus
    if position == "origin":
        strength += 1

    # Cluster penalty
    if cluster:
        strength -= 1
        if strength < 4:
            verdict = "exit"

    # Pump volume ratio
    if pump_volume_ratio < STRENGTH_PUMP_VOLUME_LOW_THRESHOLD:
        strength -= 1
    elif pump_volume_ratio > STRENGTH_PUMP_VOLUME_HIGH_THRESHOLD:
        strength += 0  # Reliable, no adjustment

    # History penalties
    if was_broken and not sweep_reclaimed:
        strength -= 2
    if max_vol_on_approach > vol_ratio * 2:
        strength -= 1

    # Zone exhaustion
    if zone_approaches == 1:
        strength -= 1
    elif zone_approaches == 2:
        strength -= 2
    elif zone_approaches >= 3:
        strength -= 3
        verdict = "exit"

    # Engulfing pattern
    if engulf_15m and vol_ratio > 2:
        verdict = "exit_fast"

    # Clamp to valid range
    strength = max(1, min(5, strength))

    lvl["strength"] = strength
    lvl["verdict"] = verdict
    
    logger.debug("Strength calculated", 
                level=lvl.get("level"), 
                type=level_type, 
                strength=strength, 
                verdict=verdict,
                poc_aligned=poc_aligned,
                candle_count=candle_count)
    
    return lvl
