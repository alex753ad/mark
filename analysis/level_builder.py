"""Simplified level building - find levels, let Claude judge strength."""

from data.collector import candles_15m, candles_1m
from logger import logger
from constants import ATR_PERIOD
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


def _calc_atr_1m(c1m: list[dict]) -> float:
    """Calculate ATR from 1M candles."""
    if len(c1m) < ATR_PERIOD:
        return 0.0
    recent = c1m[-ATR_PERIOD:]
    return sum(c["high"] - c["low"] for c in recent) / ATR_PERIOD


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


def _calculate_poc_simple(c15m: list[dict], range_low: float, range_high: float, atr: float) -> float | None:
    """
    Calculate Point of Control (POC) - price level with maximum volume.
    
    Simple approach: distribute volume across price bins and find max.
    """
    if not c15m or range_low >= range_high:
        return None
    
    # Create price bins (size = ATR * 0.2)
    bin_size = atr * 0.2
    if bin_size == 0:
        return None
    
    # Filter candles in range
    range_candles = [c for c in c15m if range_low <= c["low"] <= range_high or range_low <= c["high"] <= range_high]
    
    if not range_candles:
        return None
    
    # Accumulate volume by price bins
    volume_by_price = {}
    
    for candle in range_candles:
        candle_low = max(candle["low"], range_low)
        candle_high = min(candle["high"], range_high)
        candle_volume = candle["volume"]
        
        if candle_high == candle_low:
            # Point candle
            bin_price = round((candle_low - range_low) / bin_size) * bin_size + range_low
            volume_by_price[bin_price] = volume_by_price.get(bin_price, 0) + candle_volume
        else:
            # Distribute volume across bins
            candle_range = candle_high - candle_low
            
            # Calculate how many bins this candle spans
            num_bins = int((candle_high - candle_low) / bin_size) + 1
            
            for i in range(num_bins):
                bin_low = candle_low + i * bin_size
                bin_high = min(bin_low + bin_size, candle_high)
                bin_mid = (bin_low + bin_high) / 2
                
                if bin_mid > candle_high:
                    break
                
                # Volume proportional to overlap
                overlap = (bin_high - bin_low) / candle_range
                bin_volume = candle_volume * overlap
                
                volume_by_price[bin_mid] = volume_by_price.get(bin_mid, 0) + bin_volume
    
    if not volume_by_price:
        return None
    
    # Find price with maximum volume (POC)
    poc_price = max(volume_by_price.items(), key=lambda x: x[1])[0]
    
    return poc_price


def build_levels(symbol: str, c1m_override: list[dict] = None, c15m_override: list[dict] = None) -> list[dict]:
    """
    Build support/resistance levels with SIMPLE logic.
    
    Philosophy:
    - Find ALL potential levels (pump bases, body levels, wicks)
    - Calculate Volume Profile POC (Point of Control)
    - Attach metadata (volume, timeframe alignment, round numbers)
    - Let trigger.py calculate strength based on metadata
    """
    c1m = c1m_override if c1m_override is not None else candles_1m.get(symbol, [])
    c15m = c15m_override if c15m_override is not None else candles_15m.get(symbol, [])
    
    if len(c1m) < 20 or len(c15m) < 5:
        return []

    atr = _calc_atr_1m(c1m)
    if atr == 0:
        return []

    current_price = c1m[-1]["close"] if c1m else 0

    # Find all impulse legs within the last pump
    pump_legs = _find_pump_legs(c15m)

    if not pump_legs:
        return []

    pump_low = min(leg[0] for leg in pump_legs)
    pump_high = max(leg[1] for leg in pump_legs)

    # Cluster radius: based on 15M ATR for adaptive scaling across all tokens
    if c15m:
        atr_15m = sum(c["high"] - c["low"] for c in c15m[-20:]) / min(len(c15m), 20)
        cluster_radius = atr_15m * 0.3  # 30% of 15M ATR
    else:
        cluster_radius = max(atr * 0.5, current_price * 0.003)

    logger.debug("Pump found",
                symbol=symbol,
                low=pump_low,
                high=pump_high,
                legs=len(pump_legs),
                move_pct=round((pump_high - pump_low) / pump_low * 100, 2))

    # Define support range: 20% below current price
    support_range_low = current_price * 0.80 if current_price > 0 else pump_low
    support_range_high = current_price * 1.05 if current_price > 0 else pump_high

    logger.debug("Support range",
                symbol=symbol,
                low=support_range_low,
                high=support_range_high)

    # Calculate Volume Profile POC for the support range
    poc_price = _calculate_poc_simple(c15m, support_range_low, support_range_high, atr)

    if poc_price:
        logger.info("POC calculated",
                   symbol=symbol,
                   poc=_round_level(poc_price))

    # Collect ALL potential levels
    all_levels = []

    # 1. Pump base levels — one per leg (each step of the staircase)
    seen_bases: set[float] = set()
    for leg_low, leg_high, _, _ in pump_legs:
        # Only add bases that fall within the support range
        if leg_low < support_range_low or leg_low > support_range_high:
            continue
        # Avoid near-duplicates across legs
        if any(abs(leg_low - s) <= cluster_radius for s in seen_bases):
            continue
        seen_bases.add(leg_low)
        pump_base_levels = _find_pump_base_simple(c15m, leg_low, atr)
        for price, candle_count, metadata in pump_base_levels:
            all_levels.append({
                "level": _round_level(price),
                "type": "pump_base",
                "candle_count": candle_count,
                "poc_aligned": False,
                **metadata
            })

    # Find pump peak time for post-pump candle counting
    pump_peak_time = 0
    for c in c15m:
        if c["high"] >= pump_high * 0.999:
            pump_peak_time = c["open_time"]
            break

    # 2. Body levels (15M candle bodies in the SUPPORT RANGE)
    body_levels = _find_body_levels_simple(c15m, support_range_low, support_range_high, atr, cluster_radius, pump_peak_time)
    for price, candle_count, metadata in body_levels:
        all_levels.append({
            "level": _round_level(price),
            "type": "body_level",
            "candle_count": candle_count,
            "poc_aligned": False,  # Will be set later
            **metadata
        })
    
    # 3. Wick levels (repeated lows after pump)
    wick_levels = _find_wick_levels_simple(c15m, pump_high, atr)
    for price, candle_count, metadata in wick_levels:
        all_levels.append({
            "level": _round_level(price),
            "type": "wick_level",
            "candle_count": candle_count,
            "poc_aligned": False,  # Will be set later
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
            "poc_aligned": False,  # Will be set later
            **metadata
        })
    
    # Deduplicate nearby levels (keep the one with more touches)
    levels = _deduplicate_simple(all_levels, cluster_radius)
    
    # Mark POC alignment - ONLY ONE level should be POC-aligned (the closest one)
    if poc_price:
        # Find the closest level to POC
        closest_level = None
        min_distance = float('inf')
        
        for lvl in levels:
            distance = abs(lvl["level"] - poc_price)
            if distance < min_distance:
                min_distance = distance
                closest_level = lvl
        
        # Mark only the closest level as POC-aligned
        if closest_level and min_distance <= cluster_radius:
            closest_level["poc_aligned"] = True
            logger.info("POC aligned to level",
                       symbol=symbol,
                       poc=_round_level(poc_price),
                       level=closest_level["level"],
                       distance=round(min_distance, 6))
        else:
            # POC doesn't match any existing level - add it separately
            if support_range_low <= poc_price <= support_range_high:
                # Count candles at POC
                candles_at_poc = [
                    c for c in c15m 
                    if (abs(c["close"] - poc_price) <= atr * 0.3 or 
                        abs(c["open"] - poc_price) <= atr * 0.3 or
                        abs(c["low"] - poc_price) <= atr * 0.3 or
                        abs(c["high"] - poc_price) <= atr * 0.3)
                ]
                
                if len(candles_at_poc) >= 2:  # Minimum 2 touches
                    total_volume = sum(c["volume"] for c in candles_at_poc)
                    hourly_bonus = max(_timeframe_bonus(c["open_time"]) for c in candles_at_poc) if candles_at_poc else 0
                    round_bonus = _round_number_bonus(poc_price)
                    
                    levels.append({
                        "level": _round_level(poc_price),
                        "type": "body_level",  # POC is usually a body level
                        "candle_count": len(candles_at_poc),
                        "poc_aligned": True,
                        "volume_at_level": total_volume,
                        "hourly_open_bonus": hourly_bonus,
                        "round_number_bonus": round_bonus,
                    })
                    
                    logger.info("POC added as separate level",
                               symbol=symbol,
                               poc=_round_level(poc_price),
                               candles=len(candles_at_poc))
                    
                    # Re-sort after adding POC
                    levels.sort(key=lambda x: x["level"])
    
    # Filter: only levels in support range
    levels = [lvl for lvl in levels if support_range_low <= lvl["level"] <= support_range_high]
    
    logger.debug("Levels before top-7 filter",
                symbol=symbol,
                count=len(levels),
                prices=[round(l["level"], 6) for l in sorted(levels, key=lambda x: x["level"])])
    
    # Limit to top 7 levels by quality (POC always included, then by quality)
    def level_quality(lvl):
        score = 0
        if lvl.get("poc_aligned"):
            score += 10000  # POC always survives the filter
        if lvl.get("type") == "pump_base":
            score += 5000   # pump_base always survives
        score += lvl.get("candle_count", 0) * 10
        score += lvl.get("hourly_open_bonus", 0) * 5
        score += lvl.get("round_number_bonus", 0) * 3
        return score
    
    levels.sort(key=level_quality, reverse=True)
    levels = levels[:7]
    
    # Re-sort by price
    levels.sort(key=lambda x: x["level"])
    
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
    """Find the last significant pump (>5% move). Returns overall pump_low and pump_high."""
    legs = _find_pump_legs(c15m)
    if not legs:
        return 0, 0
    pump_low = min(leg[0] for leg in legs)
    pump_high = max(leg[1] for leg in legs)
    return pump_low, pump_high
def _find_pump_legs(c15m: list[dict]) -> list[tuple[float, float, int, int]]:
    """
    Find all impulse legs within the last significant pump.

    A leg is a sub-move of >=3% from a local low to a local high within
    the overall pump window. Returns list of (leg_low, leg_high, low_idx, high_idx).

    Example: price goes 0.016 → 0.019 → 0.017 → 0.021
    Returns two legs: (0.016, 0.019) and (0.017, 0.021)
    """
    if len(c15m) < 4:
        return []

    # Step 1: find overall pump window
    window_size = min(50, len(c15m))
    window = c15m[-window_size:]
    high_price = max(c["high"] for c in window)
    high_idx = None
    for i in range(len(c15m) - 1, max(0, len(c15m) - window_size), -1):
        if c15m[i]["high"] >= high_price * 0.999:
            high_idx = i
            break

    if high_idx is None:
        return []

    # Find the EARLIEST low that started the overall pump
    # (scan all candles before high_idx, take the furthest back that still gives >=5% move)
    pump_start_idx = None
    for i in range(max(0, high_idx - 60), high_idx):
        low_price = c15m[i]["low"]
        if low_price > 0 and (high_price - low_price) / low_price >= 0.05:
            pump_start_idx = i
            break  # first (earliest) qualifying low

    if pump_start_idx is None:
        return []

    # Step 2: slice the pump window candles
    pump_candles = c15m[pump_start_idx: high_idx + 1]
    if len(pump_candles) < 2:
        return []

    # Step 3: ZigZag — alternate between tracking highs and lows
    # Start by looking for the first significant reversal
    MIN_LEG_PCT = 0.03
    MIN_REVERSAL_PCT = 0.02  # pullback must be at least 2% to count as a new leg

    pivots = []  # list of (price, idx, 'low'|'high')

    # Always start with the first candle as a low
    pivots.append((pump_candles[0]["low"], pump_start_idx, "low"))

    looking_for = "high"
    running_high = pump_candles[0]["high"]
    running_high_idx = pump_start_idx
    running_low = pump_candles[0]["low"]
    running_low_idx = pump_start_idx

    for i, c in enumerate(pump_candles[1:], 1):
        orig_idx = pump_start_idx + i

        if looking_for == "high":
            if c["high"] > running_high:
                running_high = c["high"]
                running_high_idx = orig_idx
            # Check if we've had a significant pullback from the running high
            if running_high > 0 and (running_high - c["low"]) / running_high >= MIN_REVERSAL_PCT:
                # Commit the high pivot
                pivots.append((running_high, running_high_idx, "high"))
                running_low = c["low"]
                running_low_idx = orig_idx
                looking_for = "low"
        else:  # looking_for == "low"
            if c["low"] < running_low:
                running_low = c["low"]
                running_low_idx = orig_idx
            # Check if we've had a significant bounce from the running low
            if running_low > 0 and (c["high"] - running_low) / running_low >= MIN_REVERSAL_PCT:
                # Commit the low pivot
                pivots.append((running_low, running_low_idx, "low"))
                running_high = c["high"]
                running_high_idx = orig_idx
                looking_for = "high"

    # Commit the last running pivot
    if looking_for == "high":
        pivots.append((running_high, running_high_idx, "high"))
    else:
        pivots.append((running_low, running_low_idx, "low"))

    # Step 4: pair consecutive low→high pivots into legs
    legs = []
    for i in range(len(pivots) - 1):
        p1 = pivots[i]
        p2 = pivots[i + 1]
        if p1[2] == "low" and p2[2] == "high":
            leg_low, low_orig_idx = p1[0], p1[1]
            leg_high, high_orig_idx = p2[0], p2[1]
            if leg_low > 0 and (leg_high - leg_low) / leg_low >= MIN_LEG_PCT:
                legs.append((leg_low, leg_high, low_orig_idx, high_orig_idx))

    # For each unique low, keep only the leg with the highest high
    seen_lows: dict[float, tuple] = {}
    for leg in legs:
        key = round(leg[0], 8)
        if key not in seen_lows or leg[1] > seen_lows[key][1]:
            seen_lows[key] = leg

    # Keep only legs with move >= 5%
    legs = [leg for leg in seen_lows.values() if leg[1] > 0 and (leg[1] - leg[0]) / leg[0] >= 0.05]

    # Remove redundant legs: if leg A's low is within 4% of leg B's low, keep only the lower one
    legs.sort(key=lambda x: x[0])
    filtered: list[tuple] = []
    for leg in legs:
        if filtered and leg[0] > 0 and (leg[0] - filtered[-1][0]) / filtered[-1][0] < 0.04:
            # Too close to previous low — keep the lower one (already in filtered)
            continue
        filtered.append(leg)
    legs = filtered

    logger.debug("Pump legs found",
                count=len(legs),
                legs=[(round(l, 6), round(h, 6)) for l, h, _, _ in legs])

    return legs


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


def _find_body_levels_simple(c15m: list[dict], range_low: float, range_high: float, atr: float, cluster_radius: float = 0, pump_peak_time: int = 0) -> list[tuple[float, int, dict]]:
    """Find body levels - 15M candle bodies in the given price range."""
    upper_bound = range_high * 1.05
    avg_vol = sum(c["volume"] for c in c15m) / len(c15m) if c15m else 1
    radius = cluster_radius if cluster_radius > 0 else atr * 0.5

    # Collect (price, candle_index, volume, tf_bonus) for each boundary
    boundaries = []
    for idx, c in enumerate(c15m):
        body_top = max(c["open"], c["close"])
        body_bot = min(c["open"], c["close"])
        if body_bot >= range_low and body_top <= upper_bound:
            tf_bonus = _timeframe_bonus(c["open_time"])
            vol_weight = 5 if c["volume"] / avg_vol >= 2.0 else 3
            boundaries.append((body_top, idx, c["volume"], tf_bonus, vol_weight))
            boundaries.append((body_bot, idx, c["volume"], tf_bonus, vol_weight))

    # Cluster boundaries
    levels = []
    used = set()

    for i, (price, candle_idx, volume, tf_bonus, vol_weight) in enumerate(boundaries):
        if i in used:
            continue

        cluster_prices = [price]
        cluster_candle_idxs = {candle_idx}
        cluster_max_tf_bonus = tf_bonus
        cluster_weight = vol_weight + tf_bonus

        for j, (other_price, other_idx, other_vol, other_tf, other_wt) in enumerate(boundaries):
            if j == i or j in used:
                continue
            if abs(other_price - price) <= radius:
                cluster_prices.append(other_price)
                cluster_candle_idxs.add(other_idx)
                cluster_max_tf_bonus = max(cluster_max_tf_bonus, other_tf)
                cluster_weight += other_wt + other_tf
                used.add(j)

        avg_price = sum(cluster_prices) / len(cluster_prices)
        round_bonus = _round_number_bonus(avg_price)
        cluster_weight += round_bonus

        # Volume = average per unique candle
        unique_candle_vols = [c15m[idx]["volume"] for idx in cluster_candle_idxs if idx < len(c15m)]
        avg_candle_volume = sum(unique_candle_vols) / len(unique_candle_vols) if unique_candle_vols else 0

        # candle_count = number of candles that actually TOUCHED the level (low/high within radius)
        # Not the full cluster size — that inflates to 40-70 for wide consolidation zones
        touch_idxs = {
            idx for idx in cluster_candle_idxs
            if idx < len(c15m) and (
                abs(c15m[idx]["low"] - avg_price) <= radius or
                abs(c15m[idx]["high"] - avg_price) <= radius
            )
        }
        # Apply post-pump filter if available
        if pump_peak_time > 0:
            post_pump_touch_idxs = {idx for idx in touch_idxs
                                    if c15m[idx]["open_time"] >= pump_peak_time}
            candle_count = len(post_pump_touch_idxs) if post_pump_touch_idxs else len(touch_idxs)
        else:
            candle_count = len(touch_idxs)

        if cluster_weight >= 3:
            levels.append((avg_price, candle_count, {
                "volume_at_level": avg_candle_volume,
                "hourly_open_bonus": cluster_max_tf_bonus,
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
        
        if len(cluster) >= 2:  # Minimum 2 touches
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
    """Deduplicate nearby levels - pump_base wins over body_level, else keep more touches."""
    if not levels:
        return []

    TYPE_PRIORITY = {"pump_base": 3, "order_block": 2, "body_level": 1, "wick_level": 0}

    sorted_levels = sorted(levels, key=lambda x: x["level"])
    result = [sorted_levels[0]]

    for lvl in sorted_levels[1:]:
        if abs(lvl["level"] - result[-1]["level"]) <= radius:
            prev = result[-1]
            prev_pri = TYPE_PRIORITY.get(prev["type"], 0)
            curr_pri = TYPE_PRIORITY.get(lvl["type"], 0)
            if curr_pri > prev_pri:
                result[-1] = lvl
            elif curr_pri == prev_pri and lvl["candle_count"] > prev["candle_count"]:
                result[-1] = lvl
        else:
            result.append(lvl)

    return result


def _assign_positions(levels: list[dict], pump_low: float, pump_high: float) -> list[dict]:
    """Assign position labels (origin / impulse / mid_move)."""
    pump_range = pump_high - pump_low
    origin_threshold = pump_low + pump_range * 0.30   # bottom 30%
    impulse_threshold = pump_low + pump_range * 0.70  # middle 30-70%

    for lvl in levels:
        if lvl["level"] <= origin_threshold:
            lvl["position"] = "origin"
        elif lvl["level"] <= impulse_threshold:
            lvl["position"] = "impulse"
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
