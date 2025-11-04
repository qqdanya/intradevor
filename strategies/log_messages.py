"""Utility helpers to build consistent strategy log messages."""
from __future__ import annotations

from typing import Optional


def start_processing(symbol: str, strategy_name: str) -> str:
    return f"[{symbol}] Начало обработки сигнала ({strategy_name})"


def series_already_active(symbol: str, timeframe: str) -> str:
    return f"[{symbol}] ⚠ Активная серия уже выполняется для {timeframe}. Сигнал отложен."


def repeat_count_empty(symbol: str, remaining: int) -> str:
    return f"[{symbol}] 🛑 repeat_count={remaining} — нечего выполнять."


def signal_not_actual(symbol: str, trade_type: str, reason: str) -> str:
    trade = trade_type.lower().strip()
    if trade == "classic":
        mode = "classic"
    elif trade == "sprint":
        mode = "sprint"
    else:
        mode = trade
    return f"[{symbol}] ❌ Сигнал неактуален для {mode}: {reason}"


def signal_not_actual_for_placement(symbol: str, reason: str) -> str:
    return f"[{symbol}] ❌ Сигнал неактуален для размещения: {reason}"


def trade_placement_failed(symbol: str, action: Optional[str] = None) -> str:
    suffix = f" {action}" if action else ""
    message = f"[{symbol}] ❌ Не удалось разместить сделку.{suffix}"
    return message.rstrip()


def payout_missing(symbol: str) -> str:
    return f"[{symbol}] ⚠ Не получили % выплаты. Пропускаем сигнал."


def payout_too_low(symbol: str, current_pct: int, min_pct: int) -> str:
    return (
        f"[{symbol}] ℹ Низкий payout {current_pct}% < {min_pct}% — пропускаем сигнал."
    )


def payout_resumed(symbol: str, current_pct: int) -> str:
    return f"[{symbol}] ℹ Работа продолжается (текущий payout = {current_pct}%)"


def stake_risk(
    symbol: str,
    stake: str,
    account_ccy: str,
    min_floor: str,
    current_balance: Optional[str] = None,
) -> str:
    extra = ""
    if current_balance is not None:
        extra = f" (текущий {current_balance} {account_ccy})"
    return (
        f"[{symbol}] 🛑 Сделка {stake} {account_ccy} может опустить баланс ниже "
        f"{min_floor} {account_ccy}{extra}. Пропускаем сигнал."
    )


def trade_summary(
    symbol: str,
    stake: str,
    minutes: int,
    direction: int,
    payout: int,
) -> str:
    side = "UP" if direction == 1 else "DOWN"
    return f"[{symbol}] stake={stake} min={minutes} side={side} payout={payout}%"


def result_unknown(symbol: str, treat_as_loss: bool = False) -> str:
    if treat_as_loss:
        return f"[{symbol}] ⚠ Результат неизвестен — считаем как LOSS."
    return f"[{symbol}] ⚠ Результат неизвестен"


def result_win(symbol: str, profit: str, extra: Optional[str] = None) -> str:
    suffix = f" {extra}" if extra else ""
    message = f"[{symbol}] ✅ {profit}.{suffix}"
    return message.rstrip(".")


def result_loss(symbol: str, profit: str, extra: Optional[str] = None) -> str:
    suffix = f" {extra}" if extra else ""
    message = f"[{symbol}] ❌ {profit}.{suffix}"
    return message.rstrip(".")

