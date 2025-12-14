# strategies/strategy_helpers.py

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from core.payout_provider import get_cached_payout
from core.time_utils import format_local_time
from strategies.timeframe_utils import minutes_from_timeframe
from strategies.constants import MOSCOW_TZ

MOSCOW_ZONE = ZoneInfo(MOSCOW_TZ)


@dataclass(slots=True)
class SignalContext:
    data: dict
    received_time: datetime
    direction: int
    signal_at_str: str
    symbol: str
    timeframe: str


def calc_next_candle_from_now(timeframe: str) -> datetime:
    now = datetime.now(MOSCOW_ZONE)
    tf_minutes = minutes_from_timeframe(timeframe)

    base = now.replace(second=0, microsecond=0)
    total_min = base.hour * 60 + base.minute
    next_total = (total_min // tf_minutes + 1) * tf_minutes

    days_add = next_total // (24 * 60)
    minutes_in_day = next_total % (24 * 60)
    hour = minutes_in_day // 60
    minute = minutes_in_day % 60

    return (base + timedelta(days=days_add)).replace(hour=hour, minute=minute)


def extract_next_expire_dt(signal: dict) -> Optional[datetime]:
    next_expire = signal.get("next_expire")
    if isinstance(next_expire, datetime):
        return next_expire.replace(tzinfo=MOSCOW_ZONE) if next_expire.tzinfo is None else next_expire.astimezone(MOSCOW_ZONE)

    meta = signal.get("meta") or {}
    ts = meta.get("next_timestamp")
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=MOSCOW_ZONE) if ts.tzinfo is None else ts.astimezone(MOSCOW_ZONE)

    return None


def refresh_signal_context(
    strategy,
    signal: dict,
    *,
    update_symbol: bool = False,
    update_timeframe: bool = False,
) -> SignalContext:
    """
    ЕДИНАЯ точка обновления контекста стратегии по сигналу.
    Возвращает структурированный SignalContext.
    """
    received_time = signal["timestamp"]
    if isinstance(received_time, datetime):
        if received_time.tzinfo is None:
            received_time = received_time.replace(tzinfo=MOSCOW_ZONE)
        else:
            received_time = received_time.astimezone(MOSCOW_ZONE)

    direction = int(signal["direction"])
    signal_at_str = signal.get("signal_time_str") or format_local_time(received_time)

    symbol = signal.get("symbol") or getattr(strategy, "symbol", "*")
    timeframe = (signal.get("timeframe") or getattr(strategy, "timeframe", "M1")).upper()

    # метаданные в стратегию
    strategy._last_signal_ver = signal.get("version", getattr(strategy, "_last_signal_ver", 0))
    strategy._last_indicator = signal.get("indicator", getattr(strategy, "_last_indicator", "-"))
    strategy._last_signal_at_str = signal_at_str
    strategy._next_expire_dt = extract_next_expire_dt(signal)

    if update_symbol:
        strategy.symbol = symbol

    if update_timeframe:
        strategy.timeframe = timeframe
        strategy.params["timeframe"] = timeframe

    return SignalContext(
        data=signal,
        received_time=received_time,
        direction=direction,
        signal_at_str=signal_at_str,
        symbol=symbol,
        timeframe=timeframe,
    )


# --- Backward-compat wrapper (чтобы не переписывать всё разом) ---
def update_signal_context(
    strategy,
    new_signal: dict,
    *,
    update_symbol: bool = False,
    update_timeframe: bool = False,
) -> tuple[dict, datetime, int, str]:
    ctx = refresh_signal_context(
        strategy,
        new_signal,
        update_symbol=update_symbol,
        update_timeframe=update_timeframe,
    )
    return ctx.data, ctx.received_time, ctx.direction, ctx.signal_at_str


async def wait_for_new_signal(strategy, trade_key: str, *, timeout: float, poll_interval: float = 0.5) -> Optional[dict]:
    common = getattr(strategy, "_common", None)
    if common is None:
        return None

    start = asyncio.get_event_loop().time()
    while strategy._running and (asyncio.get_event_loop().time() - start) < timeout:
        await strategy._pause_point()
        new_signal = common.pop_latest_signal(trade_key)
        if new_signal:
            return new_signal
        await asyncio.sleep(poll_interval)

    return None


async def is_payout_low_now(strategy, symbol: str) -> bool:
    min_pct = int(strategy.params.get("min_percent", 70))
    stake = float(strategy.params.get("base_investment", 100))
    account_ccy = strategy._anchor_ccy

    try:
        pct = await get_cached_payout(
            strategy.http_client,
            investment=stake,
            option=symbol,
            minutes=strategy._trade_minutes,
            account_ccy=account_ccy,
            trade_type=strategy._trade_type,
        )
    except Exception:
        pct = None

    if pct is None:
        strategy._status("ожидание процента")
        return True

    if pct < min_pct:
        strategy._status("ожидание высокого процента")
        return True

    return False
