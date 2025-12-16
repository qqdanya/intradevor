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
    side = "ВВЕРХ" if direction == 1 else "ВНИЗ"
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


# === COMMON HELPERS ===
def params_updated(symbol: str, params: dict) -> str:
    return f"[{symbol}] ⚙ Параметры обновлены: {params}"


def signal_queue_error(symbol: str, exc: Exception) -> str:
    return f"[{symbol}] ⚠ Ошибка очереди сигналов: {exc}"


def minutes_invalid(symbol: str, requested: int, resolved: int, *, corrected: bool = False) -> str:
    if corrected:
        return f"[{symbol}] ⚠ Минуты {requested} недопустимы. Исправлено на {resolved}."
    return f"[{symbol}] ⚠ Минуты {requested} недопустимы. Использую {resolved}."


def classic_expire_missing(symbol: str) -> str:
    return f"[{symbol}] ❌ Нет времени экспирации для classic."


def trade_retry(symbol: str) -> str:
    return f"[{symbol}] ❌ Сделка не размещена. Пауза и повтор."


def classic_timeframe_unavailable(symbol: str, timeframe: str) -> str:
    return f"[{symbol}] ⚠ Таймфрейм {timeframe} недоступен для Classic — пропуск."


def account_mode(symbol: str, mode_text: str, strategy_name: str) -> str:
    return f"[{symbol}] Режим счёта: {mode_text} ({strategy_name})"


def account_mode_error(symbol: str, error: Exception) -> str:
    return f"[{symbol}] ⚠ Не удалось определить режим счёта: {error}"


def balance_info(symbol: str, display: str, amount_formatted: str, currency: str) -> str:
    return f"[{symbol}] Баланс: {display} ({amount_formatted}), валюта: {currency}"


def balance_error(symbol: str, error: Exception) -> str:
    return f"[{symbol}] ⚠ Не удалось получить баланс: {error}"


def strategy_shutdown(symbol: str, strategy_name: str) -> str:
    return f"[{symbol}] Завершение стратегии {strategy_name}"


def currency_change_ignored(symbol: str, current_ccy: str, requested_ccy: str) -> str:
    return f"[{symbol}] ⚠ Игнорирую попытку сменить валюту на лету {current_ccy} → {requested_ccy}."


def signal_listener_started(strategy_name: str) -> str:
    return f"[*] Запуск прослушивателя сигналов ({strategy_name})"


def signal_not_actual_generic(symbol: str, trade_type: str, reason: str) -> str:
    return f"[{symbol}] ⏰ Сигнал неактуален для {trade_type}: {reason} -> пропуск"


def removed_stale_signals(symbol: str, count: int) -> str:
    return f"[{symbol}] 🗑 Удалено устаревших сигналов в очереди: {count}"


def signal_enqueued(symbol: str, candle_time: str, next_time: str) -> str:
    return f"[{symbol}] Сигнал добавлен: свеча {candle_time} (до {next_time})"


def listener_error(error: Exception) -> str:
    return f"[*] Ошибка в прослушивателе: {error}"


def queue_processor_started(symbol: str, trade_key: str, allow_parallel: bool) -> str:
    return (
        f"[{symbol}] Запуск обработчика очереди {trade_key} (allow_parallel={allow_parallel})"
    )


def queue_signal_outdated(symbol: str, reason: str) -> str:
    return f"[{symbol}] ⏰ Сигнал устарел при обработке очереди: {reason} -> пропуск"


def open_trades_limit(symbol: str, max_trades: int, current: int, note: str = "") -> str:
    suffix = f" {note}" if note else ""
    return (
        f"[{symbol}] ⚠ Лимит {max_trades} сделок достигнут (факт: {current}).{suffix}"
    )


def global_lock_acquired(symbol: str) -> str:
    return f"[{symbol}] Получена глобальная блокировка, начало обработки"


def global_lock_released(symbol: str) -> str:
    return f"[{symbol}] Освобождение глобальной блокировки"


def handler_error(symbol: str, error: Exception) -> str:
    return f"[{symbol}] Ошибка в обработчике: {error}"


def handler_stopped(symbol: str, trade_key: str) -> str:
    return f"[{symbol}] Остановка обработчика {trade_key}"


def signal_deferred(symbol: str) -> str:
    return f"[{symbol}] Сигнал отложен (активная сделка)"


def deferred_signal_outdated(symbol: str, reason: str) -> str:
    return f"[{symbol}] ⏰ Отложенный сигнал устарел: {reason} -> пропуск"


def deferred_signal_start(symbol: str) -> str:
    return f"[{symbol}] Запуск отложенного сигнала"


def pending_signals_restart(symbol: str) -> str:
    return f"[{symbol}] Есть отложенные — перезапуск"


def strategy_limit_deferred(symbol: str, max_trades: int, current: int) -> str:
    return (
        f"[{symbol}] ⚠ Лимит {max_trades} сделок (факт: {current}) - отложенный сигнал оставлен в ожидании"
    )


def global_limit_before_start(symbol: str, max_trades: int, current: int) -> str:
    return (
        f"[{symbol}] ⚠ Достигнут лимит {max_trades} открытых сделок (факт: {current}). Сигнал отложен."
    )


def classic_limit_before_start(symbol: str, max_trades: int, current: int) -> str:
    return (
        f"[{symbol}] ⚠ Лимит {max_trades} сделок достигнут (факт: {current})."
    )


def series_completed(symbol: str, timeframe: str, strategy_name: str) -> str:
    return f"[{symbol}] Серия {strategy_name} завершена для {timeframe}"


def trade_step(symbol: str, step: int, stake: str, minutes: int, direction: int, payout: int) -> str:
    side = "ВВЕРХ" if direction == 1 else "ВНИЗ"
    return (
        f"[{symbol}] step={step} stake={stake} min={minutes} "
        f"side={side} payout={payout}%"
    )


def trade_step_with_label(
    symbol: str,
    step: int,
    stake: str,
    minutes: int,
    direction: int,
    payout: int,
    series_label: str,
    signal_time: Optional[str] = None,
) -> str:
    base = trade_step(symbol, step, stake, minutes, direction, payout)
    label = f" series={series_label}" if series_label else ""
    signal = f" signal={signal_time}" if signal_time else ""
    return f"{base}{label}{signal}"


def trade_result_removed(symbol: str, removed: int, outcome: str) -> str:
    return f"[{symbol}] 🗑 Удалено сигналов из очередей после {outcome}: {removed}"


def push_repeat(symbol: str) -> str:
    return f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без увеличения."


def push_repeat_same_stake(symbol: str) -> str:
    return f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без изменения ставки."


def win_with_series_finish(symbol: str, profit: str) -> str:
    return f"[{symbol}] ✅ WIN: profit={profit}. Серия завершена."


def win_with_parlay(symbol: str, profit: str) -> str:
    return (
        f"[{symbol}] ✅ WIN: profit={profit}. "
        "Увеличиваем ставку на размер выигрыша (парлей)."
    )


def loss_with_increase(symbol: str, profit: str) -> str:
    return f"[{symbol}] ❌ LOSS: profit={profit}. Увеличиваем ставку."


def loss_series_finish(symbol: str, profit: str) -> str:
    return f"[{symbol}] ❌ LOSS: profit={profit}. Серия завершается."


def loss_push_cleanup(symbol: str, removed: int, outcome: str) -> str:
    return f"[{symbol}] 🗑 Удалено сигналов из очередей после {outcome}: {removed}"


def steps_limit_reached(symbol: str, max_steps: int, *, flag: str = "🛑") -> str:
    return f"[{symbol}] {flag} Достигнут лимит шагов ({max_steps})."


def series_remaining(symbol: str, series_left: int) -> str:
    return f"[{symbol}] ▶ Осталось серий: {series_left}"


def balance_below_min(symbol: str, balance: str, min_balance: str) -> str:
    return (
        f"[{symbol}] ⛔ Баланс ниже минимума ({balance} < {min_balance}). Пропускаем сигнал."
    )


def trade_limit_reached(symbol: str, trades_done: int, max_trades: int) -> str:
    return (
        f"[{symbol}] 🛑 Достигнут лимит сделок ({trades_done}/{max_trades}). "
        "Ожидание завершения открытых сделок."
    )


def fixed_stake_stopped(symbol: str, trades_done: int) -> str:
    return f"[{symbol}] Fixed Stake остановлена. Выполнено сделок: {trades_done}"


def trade_timeout(symbol: str, timeout: float) -> str:
    return f"[{symbol}] ⏰ Таймаут ожидания нового сигнала ({timeout}с)"


def target_profit_reached(symbol: str, profit: str) -> str:
    return f"[{symbol}] Цель достигнута: {profit}"


def series_remaining_oscar(symbol: str, remaining: int) -> str:
    return f"[{symbol}] Осталось серий: {remaining}"


def series_paused(symbol: str, series_left: int) -> str:
    return f"[{symbol}] ▶ Осталось серий: {series_left}"


def fibonacci_win(symbol: str, profit: str, fib_index: int) -> str:
    return (
        f"[{symbol}] ✅ WIN: profit={profit}. "
        f"Шаг назад по Фибоначчи -> {fib_index}."
    )


def fibonacci_push(symbol: str, fib_index: int) -> str:
    return (
        f"[{symbol}] 🤝 PUSH: возврат ставки. "
        f"Остаемся на числе Фибоначчи {fib_index}."
    )


def fibonacci_loss(symbol: str, profit: str) -> str:
    return (
        f"[{symbol}] ❌ LOSS: profit={profit}. "
        "Следующее число Фибоначчи."
    )


def oscar_win_basic(
    symbol: str, profit: str, cum_profit: str, target: str, next_stake: str
) -> str:
    return (
        f"[{symbol}] ✅ WIN: profit={profit}. "
        f"Накоплено {cum_profit}/{target}. "
        f"Следующая ставка = stake+unit → {next_stake}"
    )


def oscar_win_with_requirements(
    symbol: str,
    profit: str,
    cum_profit: str,
    target: str,
    candidate: str,
    required: str,
    chosen: str,
) -> str:
    return (
        f"[{symbol}] ✅ WIN: profit={profit}. "
        f"Накоплено {cum_profit}/{target}. "
        f"Следующая ставка = min(stake+unit, req) → {candidate} / {required} = {chosen}"
    )


def oscar_refund(symbol: str, next_stake: str) -> str:
    return (
        f"[{symbol}] ↩️ REFUND: ставка возвращена. "
        f"Следующая ставка остаётся {next_stake}."
    )


def oscar_loss(symbol: str, profit: str, next_stake: str) -> str:
    return (
        f"[{symbol}] ❌ LOSS: profit={profit}. "
        f"Следующая ставка остаётся {next_stake}."
    )
