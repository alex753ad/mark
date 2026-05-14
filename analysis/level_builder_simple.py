"""Simplified level building - find levels, let Claude judge strength."""

from data.collector import candles_15m, candles_1m
from analysis.trigger import calculate_atr
from logger import logger
from constants import ATR_PERIOD, WICK_CLUSTER_MIN_TOUCHES
import statistics


def _round_level(price: float) -> float:
    """Round price to appropriate precision."""
    if price > 100:
        return round(price, 2)
    elif price >= 1:
        return round(price, 4)
    elif price >= 0.1:
        return round(price, 5)
    elif price >= 0.01:
        return round(price, 6)
    else:
        return round(price, 8)


def _timeframe_bonus(open_time_ms: int) -> int:
    """Bonus if candle opens at significant timeframe (hourly, 4h)."""
    ts = open_time_ms // 1000
    minute = (ts % 3600) // 60
    hour = (ts % 86400) // 3600

    if minute == 0 and hour % 4 == 0:
        return 3  # 4-hour open
    if minute == 0:
        return 2  # Hourly open
    if minute == 30:
        return 1  # Half-hour
    return 0


def _round_number_bonus(price: float) -> int:
    """Bonus if price is near round number."""
    if price <= 0:
        return 0

    # Determine step size based on price magnitude
    if price >= 1000:
        step = 100
    elif price >= 100:
        step = 10
    elif price >= 10:
        step = 1
    elif price >= 1:
        step = 0.1
    elif price >= 0.1:
        step = 0.01
    elif price >= 0.01:
        step = 0.001
    else:
        step = 0.0001

    nearest = round(price / step) * step
    distance_pct = abs(price - nearest) / price * 100

    if distance_pct <= 0.3:
        return 2  # Very close
    elif distance_pct <= 0.8:
        return 1  # Close
    return 0


def build_levels(symbol: str, c1m_override: list[dict] = None, c15m_override: list[dict] = None) -> list[dict]:
    """
    Build support/resistance levels with SIMPLE logic.
    
    Philosophy:
    - Find ALL potential levels (pump bases, body levels, wicks)
    - Attach metadata (volume, timeframe alignment, round numbers)
    - Let Claude decide strength based on context
    """
    c1m = c1m_override if c1m_override is not None else candles_1m.get(symbol, [])
    c15m = c15m_override if c15m_override is not None else candles_15m.get(symbol, [])
    
    if len(c1m) < 20 or len(c15m) < 5:
        return []

    atr = calculate_atr(symbol, c1m)
    if atr == 0:
        return []

    # Find the LAST significant pump (simple approach)
    pump_low, pump_high = _find_last_pump(c15m)
    
    if pump_low == 0 or pump_high == 0:
        return []
    
    logger.debug("Pump found", 
                symbol=symbol, 
                low=pump_low, 
                high=pump_high,
                move_pct=round((pump_high - pump_low) / pump_low * 100, 2))

    current_price = c1m[-1]["close"] if c1m else 0
    
    # Collect ALL potential levels
    all_levels = []
    
    # 1. Pump base levels (lows that started the pump)
    pump_base_levels = _find_pump_base_simple(c15m, pump_low, atr)
    for price, candle_count, metadata in pump_base_levels:
        all_levels.append({
            "level": _round_level(price),
            "type": "pump_base",
            "candle_count": candle_count,
            **metadata
        })
    
    # 2. Body levels (15M candle bodies in the range)
    body_levels = _find_body_levels_simple(c15m, pump_low, pump_high, atr)
    for price, candle_count, metadata in body_levels:
        all_levels.append({
            "level": _round_level(price),
            "type": "body_level",
            "candle_count": candle_count,
            **metadata
        })
    
    # 3. Wick levels (repeated lows after pump)
    wick_levels = _find_wick_levels_simple(c15m, pump_high, atr)
    for price, candle_count, metadata in wick_levels:
        all_levels.append({
            "level": _round_level(price),
            "type": "wick_level",
            "candle_count": candle_count,
            **metadata
        })
    
    # 4. Order blocks (last bearish candle before pump)
    order_block = _find_order_block_simple(c15m, pump_low, pump_high)
    if order_block:
        price, metadata = order_block
        all_levels.append({
            "level": _round_level(price),
            "type": "order_block",
            "candle_count": 1,
            **metadata
        })
    
    # Deduplicate nearby levels (keep the one with more touches)
    levels = _deduplicate_simple(all_levels, atr * 0.5)
    
    # Filter: only levels below current price (supports)
    if current_price > 0:
        levels = [lvl for lvl in levels if lvl["level"] <= current_price * 1.05]
    
    # Assign position labels
    levels = _assign_positions(levels, pump_low, pump_high)
    
    # Mark clusters
    levels = _mark_clusters(levels)
    
    logger.debug("Levels built", 
                symbol=symbol, 
                count=len(levels),
                pump_base=sum(1 for l in levels if l["type"] == "pump_base"),
                body=sum(1 for l in levels if l["type"] == "body_level"))
    
    return levels


def _find_last_pump(c15m: list[dict]) -> tuple[float, float]:
    """Find the last significant pump (>5% move)."""
    if len(c15m) < 2:
        return 0, 0
    
    # Find high in recent candles
    start_idx = max(0, len(c15m) - 10)
    high_price = max(c["high"] for c in c15m[start_idx:])
    high_idx = start_idx
    for i in range(start_idx, len(c15m)):
        if c15m[i]["high"] >= high_price * 0.999:
            high_idx = i
            break
    
    # Look for low BEFORE the high (pump start must precede pump peak)
    search_start = max(0, high_idx - 20)
    for i in range(high_idx, search_start - 1, -1):
        low_price = c15m[i]["low"]
        if low_price > 0:
            move = (high_price - low_price) / low_price
            if move >= 0.05:  # 5% minimum
                return low_price, high_price
    
    return 0, 0


def _find_pump_base_simple(c15m: list[dict], pump_low: float, atr: float) -> list[tuple[float, int, dict]]:
    """Find pump base levels - where the pump started."""
    levels = []
    
    # Main pump base: the low itself
    candles_at_low = [c for c in c15m if abs(c["low"] - pump_low) <= atr * 0.3]
    
    if candles_at_low:
        total_volume = sum(c["volume"] for c in candles_at_low)
        hourly_bonus = max(_timeframe_bonus(c["open_time"]) for c in candles_at_low)
        round_bonus = _round_number_bonus(pump_low)
        
        levels.append((pump_low, len(candles_at_low), {
            "volume_at_level": total_volume,
            "hourly_open_bonus": hourly_bonus,
            "round_number_bonus": round_bonus,
        }))
    
    # Look for consolidation zones near pump_low (within 10%)
    consol_range = pump_low * 0.10
    consol_candles = [
        c for c in c15m 
        if pump_low <= min(c["open"], c["close"]) <= pump_low + consol_range
    ]
    
    if len(consol_candles) >= 3:
        median_price = statistics.median([c["close"] for c in consol_candles])
        
        if abs(median_price - pump_low) > atr * 0.5:  # Not duplicate
            candles_at_consol = [c for c in c15m if abs(c["close"] - median_price) <= atr * 0.3]
            
            if candles_at_consol:
                total_volume = sum(c["volume"] for c in candles_at_consol)
                hourly_bonus = max(_timeframe_bonus(c["open_time"]) for c in candles_at_consol)
                round_bonus = _round_number_bonus(median_price)
                
                levels.append((median_price, len(candles_at_consol), {
                    "volume_at_level": total_volume,
                    "hourly_open_bonus": hourly_bonus,
                    "round_number_bonus": round_bonus,
                }))
    
    return levels


def _find_body_levels_simple(c15m: list[dict], pump_low: float, pump_high: float, atr: float) -> list[tuple[float, int, dict]]:
    """Find body levels - 15M candle bodies in the pump range."""
    boundaries = []
    upper_bound = pump_high * 1.05
    
    avg_vol = sum(c["volume"] for c in c15m) / len(c15m) if c15m else 1
    
    for c in c15m:
        body_top = max(c["open"], c["close"])
        body_bot = min(c["open"], c["close"])
        
        if body_bot >= pump_low and body_top <= upper_bound:
            weight = 3
            if c["volume"] / avg_vol >= 2.0:
                weight = 5  # High volume candle
            
            tf_bonus = _timeframe_bonus(c["open_time"])
            weight += tf_bonus
            
            boundaries.append((body_top, weight, c["volume"], tf_bonus))
            boundaries.append((body_bot, weight, c["volume"], tf_bonus))
    
    # Cluster boundaries
    levels = []
    used = set()
    
    for i, (price, weight, volume, tf_bonus) in enumerate(boundaries):
        if i in used:
            continue
        
        cluster_prices = [price]
        cluster_weight = weight
        cluster_volume = volume
        cluster_tf_bonus = tf_bonus
        
        for j, (other_price, other_weight, other_volume, other_tf_bonus) in enumerate(boundaries):
            if j == i or j in used:
                continue
            
            if abs(other_price - price) <= atr * 0.5:
                cluster_prices.append(other_price)
                cluster_weight += other_weight
                cluster_volume += other_volume
                cluster_tf_bonus = max(cluster_tf_bonus, other_tf_bonus)
                used.add(j)
        
        avg_price = sum(cluster_prices) / len(cluster_prices)
        round_bonus = _round_number_bonus(avg_price)
        cluster_weight += round_bonus
        
        if cluster_weight >= 6:  # Minimum weight threshold
            levels.append((avg_price, len(cluster_prices), {
                "volume_at_level": cluster_volume,
                "hourly_open_bonus": cluster_tf_bonus,
                "round_number_bonus": round_bonus,
            }))
        
        used.add(i)
    
    return levels


def _find_wick_levels_simple(c15m: list[dict], pump_high: float, atr: float) -> list[tuple[float, int, dict]]:
    """Find wick levels - repeated lows after pump peak."""
    # Find pump peak time
    pump_peak_time = None
    for c in c15m:
        if c["high"] >= pump_high * 0.999:
            pump_peak_time = c["open_time"]
            break
    
    if not pump_peak_time:
        return []
    
    # Collect lows after pump peak
    wick_lows = []
    for c in c15m:
        if c["open_time"] > pump_peak_time:
            wick_lows.append((c["low"], c["volume"], c["open_time"]))
    
    # Cluster wicks
    levels = []
    used = set()
    
    for i, (price, volume, open_time) in enumerate(wick_lows):
        if i in used:
            continue
        
        cluster = [(price, volume, open_time)]
        
        for j, (other_price, other_volume, other_time) in enumerate(wick_lows):
            if j == i or j in used:
                continue
            
            if abs(other_price - price) <= atr * 0.3:
                cluster.append((other_price, other_volume, other_time))
                used.add(j)
        
        if len(cluster) >= WICK_CLUSTER_MIN_TOUCHES:
            avg_price = sum(p for p, v, t in cluster) / len(cluster)
            total_volume = sum(v for p, v, t in cluster)
            tf_bonus = max(_timeframe_bonus(t) for p, v, t in cluster)
            round_bonus = _round_number_bonus(avg_price)
            
            levels.append((avg_price, len(cluster), {
                "volume_at_level": total_volume,
                "hourly_open_bonus": tf_bonus,
                "round_number_bonus": round_bonus,
            }))
        
        used.add(i)
    
    return levels


def _find_order_block_simple(c15m: list[dict], pump_low: float, pump_high: float) -> tuple[float, dict] | None:
    """Find order block - last bearish candle before pump."""
    # Find pump start
    pump_start_idx = None
    for i, c in enumerate(c15m):
        if c["low"] <= pump_low * 1.001:
            pump_start_idx = i
            break
    
    if pump_start_idx is None or pump_start_idx < 2:
        return None
    
    # Look for last bearish candle before pump
    for i in range(pump_start_idx, max(0, pump_start_idx - 5), -1):
        c = c15m[i]
        if c["close"] < c["open"]:  # Bearish
            price = min(c["open"], c["close"])
            tf_bonus = _timeframe_bonus(c["open_time"])
            round_bonus = _round_number_bonus(price)
            
            return (price, {
                "volume_at_level": c["volume"],
                "hourly_open_bonus": tf_bonus,
                "round_number_bonus": round_bonus,
            })
    
    return None


def _deduplicate_simple(levels: list[dict], radius: float) -> list[dict]:
    """Deduplicate nearby levels - keep the one with more touches."""
    if not levels:
        return []
    
    sorted_levels = sorted(levels, key=lambda x: x["level"])
    result = [sorted_levels[0]]
    
    for lvl in sorted_levels[1:]:
        if abs(lvl["level"] - result[-1]["level"]) <= radius:
            # Keep the one with more candles
            if lvl["candle_count"] > result[-1]["candle_count"]:
                result[-1] = lvl
        else:
            result.append(lvl)
    
    return result


def _assign_positions(levels: list[dict], pump_low: float, pump_high: float) -> list[dict]:
    """Assign position labels (origin vs mid_move)."""
    origin_threshold = pump_low * 1.20
    
    for lvl in levels:
        if lvl["level"] <= origin_threshold:
            lvl["position"] = "origin"
        else:
            lvl["position"] = "mid_move"
    
    return levels


def _mark_clusters(levels: list[dict]) -> list[dict]:
    """Mark levels that are clustered together."""
    for i in range(len(levels) - 1):
        diff = abs(levels[i + 1]["level"] - levels[i]["level"])
        avg = (levels[i + 1]["level"] + levels[i]["level"]) / 2
        
        if avg > 0 and diff / avg < 0.01:  # Within 1%
            levels[i]["cluster"] = True
            levels[i + 1]["cluster"] = True
    
    for lvl in levels:
        if "cluster" not in lvl:
            lvl["cluster"] = False
    
    return levels
