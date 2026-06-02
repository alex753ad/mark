import asyncio
from binance import AsyncClient
from config import token_registry
from binance.exceptions import BinanceAPIException
from logger import logger
import time


invalid_symbols: set[str] = set()

candles_15m: dict[str, list[dict]] = {}
candles_1m:  dict[str, list[dict]] = {}

# aggTrades delta buffer: {symbol: list of {ts, qty, is_buy}}
# Only populated when symbol is being monitored.
agg_trades: dict[str, list[dict]] = {}
AGG_TRADES_WINDOW = 60  # keep last 60 seconds of trades

# Active stream tasks — one per symbol. Used to prevent duplicate streams
# and to cancel cleanly when stop_delta_tracking() is called (BUG-26).
_stream_tasks: dict[str, asyncio.Task] = {}

MAX_CANDLES = 300
_ALWAYS_COLLECT = ["BTCUSDT"]


def _parse_kline(kline) -> dict:
    return {
        "open_time": int(kline[0]),
        "open": float(kline[1]),
        "high": float(kline[2]),
        "low": float(kline[3]),
        "close": float(kline[4]),
        "volume": float(kline[5]),
        "close_time": int(kline[6]),
    }


async def _fetch_history(client: AsyncClient, symbol: str) -> bool:
    try:
        raw_15m = await client.futures_klines(symbol=symbol, interval="15m", limit=MAX_CANDLES)
        raw_1m = await client.futures_klines(symbol=symbol, interval="1m", limit=MAX_CANDLES)
    except BinanceAPIException:
        invalid_symbols.add(symbol)
        logger.warning("Invalid symbol: %s", symbol)
        return False
    except Exception:
        logger.exception("Failed to fetch history for %s", symbol)
        return False
    candles_15m[symbol] = [_parse_kline(k) for k in raw_15m]
    candles_1m[symbol] = [_parse_kline(k) for k in raw_1m]
    return True


async def _update(client: AsyncClient, symbol: str):
    try:
        raw_15m = await client.futures_klines(symbol=symbol, interval="15m", limit=2)
        raw_1m = await client.futures_klines(symbol=symbol, interval="1m", limit=2)
    except BinanceAPIException:
        # BUG-31: symbol was delisted / renamed after startup — stop polling it
        invalid_symbols.add(symbol)
        logger.warning("Symbol became invalid during update, skipping: %s", symbol)
        return
    except Exception:
        logger.warning("Failed to update candles for %s", symbol)
        return

    for kline in raw_15m:
        parsed = _parse_kline(kline)
        if candles_15m[symbol] and candles_15m[symbol][-1]["open_time"] == parsed["open_time"]:
            candles_15m[symbol][-1] = parsed
        else:
            candles_15m[symbol].append(parsed)
            if len(candles_15m[symbol]) > MAX_CANDLES:
                candles_15m[symbol] = candles_15m[symbol][-MAX_CANDLES:]

    for kline in raw_1m:
        parsed = _parse_kline(kline)
        if candles_1m[symbol] and candles_1m[symbol][-1]["open_time"] == parsed["open_time"]:
            candles_1m[symbol][-1] = parsed
        else:
            candles_1m[symbol].append(parsed)
            if len(candles_1m[symbol]) > MAX_CANDLES:
                candles_1m[symbol] = candles_1m[symbol][-MAX_CANDLES:]


def _all_symbols() -> list[str]:
    """Return user tokens + always-collected symbols (deduplicated)."""
    tokens = token_registry.get_all()
    seen = set(tokens)
    return tokens + [s for s in _ALWAYS_COLLECT if s not in seen]


def start_delta_tracking(symbol: str):
    """Start tracking aggTrades delta for a symbol.

    BUG-26: also spawns the self-healing stream task if not already running.
    Calling this multiple times for the same symbol is safe — duplicate tasks
    are suppressed via _stream_tasks.
    """
    if symbol not in agg_trades:
        agg_trades[symbol] = []
        logger.debug("Delta tracking started", symbol=symbol)

    # Spawn or reuse the stream task
    existing = _stream_tasks.get(symbol)
    if existing is None or existing.done():
        task = asyncio.create_task(_stream_agg_trades(symbol))
        _stream_tasks[symbol] = task
        logger.debug("aggTrades stream task spawned", symbol=symbol)


def stop_delta_tracking(symbol: str):
    """Stop tracking aggTrades delta for a symbol.

    BUG-26: cancels the stream task so the connection is closed immediately.
    """
    agg_trades.pop(symbol, None)

    task = _stream_tasks.pop(symbol, None)
    if task and not task.done():
        task.cancel()
        logger.debug("aggTrades stream task cancelled", symbol=symbol)
    else:
        logger.debug("Delta tracking stopped", symbol=symbol)


def get_delta(symbol: str, window_seconds: int = 30) -> dict:
    """
    Calculate buy/sell delta for the last N seconds.
    
    Returns:
        {
            "buy_vol": float,   # taker buy volume
            "sell_vol": float,  # taker sell volume
            "delta": float,     # buy - sell
            "trades": int,      # number of trades
        }
    """
    trades = agg_trades.get(symbol, [])
    if not trades:
        return {"buy_vol": 0, "sell_vol": 0, "delta": 0, "trades": 0}

    cutoff = time.time() - window_seconds
    recent = [t for t in trades if t["ts"] >= cutoff]

    buy_vol = sum(t["qty"] for t in recent if t["is_buy"])
    sell_vol = sum(t["qty"] for t in recent if not t["is_buy"])

    return {
        "buy_vol": round(buy_vol, 4),
        "sell_vol": round(sell_vol, 4),
        "delta": round(buy_vol - sell_vol, 4),
        "trades": len(recent),
    }


async def _stream_agg_trades(symbol: str):
    """Stream aggTrades for a symbol with automatic reconnection (BUG-26).

    Changes vs old implementation:
    - Self-healing reconnect loop with exponential back-off (1s → 30s).
      Restarts on any error without waiting for monitor.py's 5-second tick.
    - Single AsyncClient per connection attempt — no client created per call.
    - Buffer cleared on disconnect so get_delta() returns zeros, not stale data.
    - Exits cleanly (no reconnect) on CancelledError or when stop_delta_tracking()
      removes the symbol from agg_trades.
    """
    _RECONNECT_DELAY_MIN = 1
    _RECONNECT_DELAY_MAX = 30
    delay = _RECONNECT_DELAY_MIN

    while symbol in agg_trades:
        client = None
        try:
            client = await AsyncClient.create()
            delay = _RECONNECT_DELAY_MIN  # reset back-off on successful connect

            bm = client.futures_multiplex_socket([f"{symbol.lower()}@aggTrade"])
            async with bm as stream:
                logger.debug("aggTrades stream connected", symbol=symbol)
                while symbol in agg_trades:
                    msg = await stream.recv()
                    if not msg or "data" not in msg:
                        continue
                    data = msg["data"]
                    if data.get("e") != "aggTrade":
                        continue

                    # m=True means buyer is maker → sell taker
                    # m=False means seller is maker → buy taker
                    is_buy = not data["m"]
                    qty = float(data["q"])
                    ts = data["T"] / 1000  # ms to seconds

                    buf = agg_trades.get(symbol)
                    if buf is None:
                        return  # stop_delta_tracking was called mid-stream

                    buf.append({"ts": ts, "qty": qty, "is_buy": is_buy})

                    # Trim entries older than the window
                    cutoff = time.time() - AGG_TRADES_WINDOW
                    agg_trades[symbol] = [t for t in buf if t["ts"] >= cutoff]

        except asyncio.CancelledError:
            logger.debug("aggTrades stream cancelled", symbol=symbol)
            return  # clean exit — do not reconnect
        except Exception as e:
            if symbol not in agg_trades:
                return  # tracking stopped while connecting

            logger.warning("aggTrades stream error — reconnecting in %ds: %s", delay, e)

            # Clear stale buffer so callers see zeros, not data from dead stream
            if symbol in agg_trades:
                agg_trades[symbol] = []

            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_DELAY_MAX)
        finally:
            if client is not None:
                try:
                    await client.close_connection()
                except Exception:
                    pass


async def start_collector():
    while True:
        try:
            client = await AsyncClient.create()
        except Exception:
            logger.exception("Failed to create Binance client")
            await asyncio.sleep(10)
            continue
        try:
            for symbol in _all_symbols():
                await _fetch_history(client, symbol)

            while True:
                for symbol in _all_symbols():
                    if symbol in invalid_symbols:
                        continue
                    if symbol not in candles_15m:
                        await _fetch_history(client, symbol)
                    else:
                        await _update(client, symbol)
                await asyncio.sleep(5)
        except Exception:
            logger.exception("Collector loop error, reconnecting")
            await asyncio.sleep(10)
        finally:
            try:
                await client.close_connection()
            except Exception:
                pass
