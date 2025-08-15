# core/intrade_api_async.py
from __future__ import annotations
from typing import Any, Dict, Optional, Tuple

from core.http_async import HttpClient

# Если у вас в старом core/intrade_api.py были константы путей — скопируйте их значения сюда.
# Ниже — понятные имена, меняйте при необходимости.
PATH_BALANCE = "/api/balance"
PATH_PERCENT = "/api/current_percent"
PATH_PLACE_TRADE = "/api/place_trade"
PATH_CHECK_TRADE = "/api/check_trade"


async def get_balance_info(
    client: HttpClient, user_id: str, user_hash: str
) -> Dict[str, Any]:
    payload = {"user_id": user_id, "user_hash": user_hash}
    return await client.post(PATH_BALANCE, data=payload, expect_json=True)


async def get_current_percent(
    client: HttpClient, investment: float, symbol: str
) -> float:
    payload = {"investment": investment, "symbol": symbol}
    data = await client.post(PATH_PERCENT, data=payload, expect_json=True)
    return float(data.get("percent", 0.0))


async def place_trade(
    client: HttpClient,
    user_id: str,
    user_hash: str,
    symbol: str,
    direction: int,
    investment: float,
    timeframe_seconds: int,
) -> Dict[str, Any]:
    payload = {
        "user_id": user_id,
        "user_hash": user_hash,
        "symbol": symbol,
        "direction": direction,
        "investment": investment,
        "timeframe": timeframe_seconds,
    }
    return await client.post(PATH_PLACE_TRADE, data=payload, expect_json=True)


async def check_trade_result(
    client: HttpClient,
    user_id: str,
    user_hash: str,
    trade_id: str,
) -> Tuple[bool, Optional[float]]:
    payload = {"user_id": user_id, "user_hash": user_hash, "trade_id": trade_id}
    data = await client.post(PATH_CHECK_TRADE, data=payload, expect_json=True)
    # ожидаем {"ok": bool, "pnl": float | null}
    return bool(data.get("ok")), data.get("pnl")
