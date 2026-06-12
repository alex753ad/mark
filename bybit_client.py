"""Bybit Demo REST client — лимитные входы, рыночный выход, стоп-лимит SL.

Используется только strategy2_live.py. Все запросы идут на demo endpoint.

Подпись Bybit v5:
  GET:  param_str = timestamp + api_key + recv_window + query_string
  POST: param_str = timestamp + api_key + recv_window + json_body_string
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Optional

import aiohttp

from config import BYBIT_API_KEY, BYBIT_API_SECRET
from logger import logger

BYBIT_DEMO_BASE = "https://api-demo.bybit.com"
RECV_WINDOW = "5000"


def _sign(payload_str: str, timestamp: str) -> str:
    param_str = timestamp + BYBIT_API_KEY + RECV_WINDOW + payload_str
    return hmac.new(BYBIT_API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()


def _query_string(params: dict) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def _post_headers(body: dict) -> dict:
    """Заголовки для POST — подпись по JSON-строке тела."""
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(",", ":"))
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": _sign(body_str, ts),
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type": "application/json",
    }


def _get_headers(params: dict) -> dict:
    """Заголовки для GET — подпись по query string параметров."""
    ts = str(int(time.time() * 1000))
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": _sign(_query_string(params), ts),
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    url = BYBIT_DEMO_BASE + path
    async with aiohttp.ClientSession() as session:
        body_str = json.dumps(body, separators=(",", ":"))
        async with session.post(url, data=body_str, headers=_post_headers(body)) as resp:
            data = await resp.json()
    return data


async def _get(path: str, params: dict) -> dict:
    url = BYBIT_DEMO_BASE + path
    qs = "?" + _query_string(params) if params else ""
    async with aiohttp.ClientSession() as session:
        async with session.get(url + qs, headers=_get_headers(params)) as resp:
            data = await resp.json()
    return data


# ── Leverage ──────────────────────────────────────────────────────────────────

async def set_leverage(symbol: str, leverage: int = 20) -> None:
    body = {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }
    resp = await _post("/v5/position/set-leverage", body)
    if resp.get("retCode") not in (0, 110043):  # 110043 = already set
        logger.warning(
            "bybit set_leverage unexpected response",
            symbol=symbol,
            resp=resp,
        )


# ── Place orders ──────────────────────────────────────────────────────────────

async def place_limit_order(
    symbol: str,
    side: str,          # "Buy" | "Sell"
    qty: float,
    price: float,
    order_link_id: Optional[str] = None,
    reduce_only: bool = False,
) -> dict:
    """Разместить лимитный ордер. Возвращает {"orderId": ..., "orderLinkId": ...} или выбрасывает."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "qty": str(qty),
        "price": str(price),
        "timeInForce": "GTC",
        "orderLinkId": order_link_id or str(uuid.uuid4()),
    }
    if reduce_only:
        body["reduceOnly"] = True

    resp = await _post("/v5/order/create", body)
    if resp.get("retCode") != 0:
        raise RuntimeError(
            f"place_limit_order failed: retCode={resp.get('retCode')} "
            f"retMsg={resp.get('retMsg')} symbol={symbol} price={price} qty={qty}"
        )
    return resp["result"]


async def place_market_order(
    symbol: str,
    side: str,          # "Buy" | "Sell"
    qty: float,
    reduce_only: bool = True,
) -> dict:
    """Рыночное закрытие позиции."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "reduceOnly": reduce_only,
    }
    resp = await _post("/v5/order/create", body)
    if resp.get("retCode") != 0:
        raise RuntimeError(
            f"place_market_order failed: retCode={resp.get('retCode')} "
            f"retMsg={resp.get('retMsg')} symbol={symbol} qty={qty}"
        )
    return resp["result"]


async def place_stop_limit_order(
    symbol: str,
    side: str,
    qty: float,
    trigger_price: float,
    order_price: float,
    order_link_id: Optional[str] = None,
) -> dict:
    """Стоп-лимитный ордер (SL). trigger_price = уровень активации, order_price = цена ордера."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "qty": str(qty),
        "price": str(order_price),
        "triggerPrice": str(trigger_price),
        "triggerBy": "LastPrice",
        "triggerDirection": 2,   # 1=выше, 2=ниже (SL для лонга)
        "timeInForce": "GTC",
        "reduceOnly": True,
        "orderLinkId": order_link_id or str(uuid.uuid4()),
    }
    resp = await _post("/v5/order/create", body)
    if resp.get("retCode") != 0:
        raise RuntimeError(
            f"place_stop_limit_order failed: retCode={resp.get('retCode')} "
            f"retMsg={resp.get('retMsg')} symbol={symbol} trigger={trigger_price}"
        )
    return resp["result"]


async def cancel_order(symbol: str, order_id: str) -> bool:
    """Отменить ордер по orderId. Возвращает True если успешно."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "orderId": order_id,
    }
    resp = await _post("/v5/order/cancel", body)
    if resp.get("retCode") not in (0, 20001):   # 20001 = уже исполнен/отменён
        logger.warning(
            "bybit cancel_order unexpected response",
            symbol=symbol,
            order_id=order_id,
            resp=resp,
        )
        return False
    return True


async def cancel_all_orders(symbol: str) -> None:
    """Отменить все открытые ордера по символу."""
    body = {"category": "linear", "symbol": symbol}
    resp = await _post("/v5/order/cancel-all", body)
    if resp.get("retCode") != 0:
        logger.warning(
            "bybit cancel_all_orders failed",
            symbol=symbol,
            resp=resp,
        )


# ── Position info ─────────────────────────────────────────────────────────────

async def get_position(symbol: str) -> Optional[dict]:
    """Вернуть текущую позицию по символу или None."""
    params = {"category": "linear", "symbol": symbol}
    resp = await _get("/v5/position/list", params)
    if resp.get("retCode") != 0:
        logger.warning("bybit get_position failed", symbol=symbol, resp=resp)
        return None
    items = resp.get("result", {}).get("list", [])
    for item in items:
        if float(item.get("size", 0)) > 0:
            return item
    return None


async def get_order_status(symbol: str, order_id: str) -> Optional[str]:
    """Вернуть статус ордера: 'Filled', 'New', 'Cancelled', etc."""
    params = {
        "category": "linear",
        "symbol": symbol,
        "orderId": order_id,
    }
    resp = await _get("/v5/order/realtime", params)
    if resp.get("retCode") != 0:
        return None
    items = resp.get("result", {}).get("list", [])
    return items[0]["orderStatus"] if items else None


async def get_instrument_info(symbol: str) -> Optional[dict]:
    """Вернуть lotSizeFilter для расчёта минимального qty и шага."""
    params = {"category": "linear", "symbol": symbol}
    resp = await _get("/v5/market/instruments-info", params)
    if resp.get("retCode") != 0:
        return None
    items = resp.get("result", {}).get("list", [])
    return items[0] if items else None
