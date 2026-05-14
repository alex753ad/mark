"""Market screener — separated from main.py to avoid circular imports."""

from constants import (
    SCREENER_MIN_VOLUME_USD,
    SCREENER_MIN_GROWTH_PCT,
    SCREENER_MIN_NATR,
)
from logger import logger


def _format_vol(v: float) -> str:
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v/1_000_000:.0f}M"
    return f"{v/1_000:.0f}K"


async def run_screener() -> list[tuple]:
    """Run market screener and return list of (ticker, chg, natr, vol, symbol)."""
    from binance import AsyncClient
    client = await AsyncClient.create()
    rows = []
    try:
        tickers = await client.futures_ticker()
        candidates = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            vol = float(t["quoteVolume"])
            chg = float(t["priceChangePercent"])
            if vol > SCREENER_MIN_VOLUME_USD and chg > SCREENER_MIN_GROWTH_PCT:
                candidates.append((sym, chg, vol))

        for sym, chg, vol in candidates:
            try:
                klines = await client.futures_klines(symbol=sym, interval="5m", limit=14)
                if len(klines) < 2:
                    continue
                current_price = float(klines[-1][4])
                if current_price == 0:
                    continue
                tr_list = [float(k[2]) - float(k[3]) for k in klines]
                atr = sum(tr_list) / len(tr_list)
                natr = round(atr / current_price * 100, 1)
                if natr > SCREENER_MIN_NATR:
                    ticker = sym.replace("USDT", "")
                    rows.append((ticker, chg, natr, vol, sym))
            except Exception as e:
                logger.debug("Screener error", symbol=sym, error=str(e))
    finally:
        await client.close_connection()
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows
