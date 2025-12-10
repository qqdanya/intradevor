from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ, ALL_SYMBOLS_LABEL, ALL_TF_LABEL, CLASSIC_ALLOWED_TFS
from core.money import format_amount
from core.intrade_api_async import is_demo_account, get_balance_info
from core.time_utils import format_local_time
from strategies.log_messages import (
    repeat_count_empty,
    series_already_active,
    signal_not_actual,
    signal_not_actual_for_placement,
    start_processing,
    trade_placement_failed,
    trade_summary,
    result_unknown,
    series_completed,
    steps_limit_reached,
    series_remaining,
    trade_timeout,
    fibonacci_win,
    fibonacci_push,
    fibonacci_loss,
)

FIBONACCI_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 5,
    "repeat_count": 10,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}

def _fib(n: int) -> int:
    """Возвращает n-е число Фибоначчи (1-indexed)."""
    if n <= 0:
        return 1
    seq = [1, 1]
    while len(seq) < n:
        seq.append(seq[-1] + seq[-2])
    return seq[n - 1]

class FibonacciStrategy(BaseTradingStrategy):
    """Стратегия Фибоначчи (управление ставками по последовательности Фибоначчи)"""

    def __init__(
        self,
        http_client,
        user_id: str,
        user_hash: str,
        symbol: str,
        log_callback=None,
        *,
        timeframe: str = "M1",
        params: Optional[dict] = None,
        **kwargs,
    ):
        # Объединяем параметры по умолчанию
        fibonacci_params = dict(FIBONACCI_DEFAULTS)
        if params:
            fibonacci_params.update(params)

        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=fibonacci_params,
            strategy_name="Fibonacci",
            **kwargs,
        )

        # Отслеживание активных серий по паре+таймфрейму
        self._active_series: dict[str, bool] = {}

    def is_series_active(self, trade_key: str) -> bool:
        """Показывает, выполняется ли серия для указанного ключа."""
        return self._active_series.get(trade_key, False)

    def should_request_fresh_signal_after_loss(self) -> bool:
        """Fibonacci требует обновления сигнала после убыточной сделки."""
        return True

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для стратегии Фибоначчи"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        trade_key = self.build_trade_key(symbol, timeframe)

        log = self.log or (lambda s: None)

        if self._active_series.get(trade_key):
            log(series_already_active(symbol, timeframe))
            if hasattr(self, '_common'):
                await self._common._handle_pending_signal(trade_key, signal_data)
            return

        # Обновляем информацию о сигнале
        self._last_signal_ver = signal_data['version']
        self._last_indicator = signal_data['indicator']
        self._last_signal_at_str = format_local_time(signal_data['timestamp'])

        ts = signal_data['meta'].get('next_timestamp') if signal_data['meta'] else None
        self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

        # Обновляем символ и таймфрейм если используются "все"
        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        # ПРОВЕРКА АКТУАЛЬНОСТИ СИГНАЛА (перед стартом серий)
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

        if self._trade_type == "classic":
            is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
            if not is_valid:
                log(signal_not_actual(symbol, "classic", reason))
                return
        else:
            is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
            if not is_valid:
                log(signal_not_actual(symbol, "sprint", reason))
                return

        series_left = self._get_series_left(trade_key)
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return

        series_started = False
        try:
            self._active_series[trade_key] = True
            series_started = True
            log(start_processing(symbol, "Фибоначчи"))

            # Запускаем серии Фибоначчи
            updated = await self._run_fibonacci_series(
                trade_key,
                symbol,
                timeframe,
                direction,
                log,
                series_left,
                signal_data['timestamp'],
                signal_data,
            )
            self._set_series_left(trade_key, updated)
        finally:
            if series_started:
                self._active_series.pop(trade_key, None)
                log(series_completed(symbol, timeframe, "Фибоначчи"))

    async def _run_fibonacci_series(
        self,
        trade_key: str,
        symbol: str,
        timeframe: str,
        initial_direction: int,
        log,
        series_left: int,
        signal_received_time: datetime,
        signal_data: dict,
    ) -> int:
        """Запускает серию ставок по последовательности Фибоначчи"""

        base_stake = float(self.params.get("base_investment", 100))
        max_steps = int(self.params.get("max_steps", 5))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))

        if max_steps <= 0:
            return series_left

        fib_index = 1  # Позиция в последовательности Фибоначчи (1-indexed)
        step_idx = 0
        did_place_any_trade = False
        needs_signal_validation = True
        requires_fresh_signal = self.should_request_fresh_signal_after_loss()
        need_new_signal = False
        series_direction = initial_direction
        signal_at_str = signal_data.get('signal_time_str') or format_local_time(signal_received_time)
        series_label = self.format_series_label(trade_key, series_left=series_left)

        def update_signal_context(new_signal: Optional[dict]) -> None:
            nonlocal signal_data, signal_received_time, series_direction, signal_at_str, needs_signal_validation
            if not new_signal:
                return

            signal_data = new_signal
            signal_received_time = new_signal['timestamp']
            series_direction = new_signal['direction']
            needs_signal_validation = True

            self._last_signal_ver = new_signal.get('version', self._last_signal_ver)
            self._last_indicator = new_signal.get('indicator', self._last_indicator)
            signal_at_str = new_signal.get('signal_time_str') or format_local_time(signal_received_time)
            self._last_signal_at_str = signal_at_str

            ts = new_signal.get('meta', {}).get('next_timestamp') if new_signal.get('meta') else None
            self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

        while self._running and step_idx < max_steps:
            await self._pause_point()

            if not await self.ensure_account_conditions():
                continue

            if needs_signal_validation:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(
                        signal_data,
                        current_time,
                        for_placement=True,
                    )
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        return series_left
                else:
                    sprint_payload = signal_data
                    if not sprint_payload.get('timestamp'):
                        sprint_payload = {'timestamp': signal_received_time}
                    is_valid, reason = self._is_signal_valid_for_sprint(
                        sprint_payload,
                        current_time,
                    )
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        return series_left

            # Рассчитываем ставку по числу Фибоначчи
            multiplier = _fib(fib_index)
            stake = base_stake * multiplier

            pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue

            log(
                trade_summary(
                    symbol,
                    format_amount(stake),
                    self._trade_minutes,
                    series_direction,
                    pct,
                ) + f" (Fibo #{fib_index})"
            )

            # Финальная проверка актуальности перед размещением сделки
            # Нужна на каждом шаге: при ожидании высокого payout можно
            # перепрыгнуть одну-две свечи и вернуться к ставке по
            # устаревшему сигналу или времени экспирации.
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(
                    signal_data,
                    current_time,
                    for_placement=True,
                )
            else:
                sprint_payload = signal_data
                if not sprint_payload.get('timestamp'):
                    sprint_payload = {'timestamp': signal_received_time}
                is_valid, reason = self._is_signal_valid_for_sprint(
                    sprint_payload,
                    current_time,
                )

            if not is_valid:
                log(signal_not_actual_for_placement(symbol, reason))
                return series_left

            needs_signal_validation = False

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, stake, self._anchor_ccy)

            if not trade_id:
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                return series_left

            did_place_any_trade = True

            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = self.params.get("result_wait_s")
            if wait_seconds is None:
                wait_seconds = trade_seconds
            else:
                wait_seconds = float(wait_seconds)

            self._notify_pending_trade(
                trade_id,
                symbol,
                timeframe,
                series_direction,
                stake,
                pct,
                trade_seconds,
                account_mode,
                expected_end_ts,
                signal_at=signal_at_str,
                series_label=series_label,
            )
            self._register_pending_trade(trade_id, symbol, timeframe)

            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
                wait_seconds=float(wait_seconds),
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=series_direction,
                stake=float(stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
                series_label=series_label,
            )

            step_idx += 1

            continue_series = True

            if profit is None:
                log(result_unknown(symbol, treat_as_loss=True))
                fib_index += 1
                if requires_fresh_signal:
                    need_new_signal = True
            elif profit > 0:
                fib_index = max(1, fib_index - 2)
                log(fibonacci_win(symbol, format_amount(profit), fib_index))
                if fib_index <= 1:
                    continue_series = False
            elif abs(profit) < 1e-9:
                log(fibonacci_push(symbol, fib_index))
            else:
                log(fibonacci_loss(symbol, format_amount(profit)))
                fib_index += 1
                if requires_fresh_signal:
                    need_new_signal = True

            await self.sleep(0.2)

            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(minutes=_minutes_from_timeframe(timeframe))

            if not continue_series:
                break

            if step_idx >= max_steps:
                break

            if requires_fresh_signal and need_new_signal:
                if hasattr(self, "_common") and self._common is not None:
                    timeout = float(self.params.get("signal_timeout_sec", 30.0))
                    new_signal = await self._wait_for_new_signal(
                        trade_key,
                        log,
                        symbol,
                        timeframe,
                        timeout=timeout,
                    )
                    if not new_signal:
                        break
                    update_signal_context(new_signal)
                    need_new_signal = False
                else:
                    break
            elif hasattr(self, "_common") and self._common is not None:
                new_signal = self._common.pop_latest_signal(trade_key)
                update_signal_context(new_signal)

        if did_place_any_trade:
            if step_idx >= max_steps:
                log(steps_limit_reached(symbol, max_steps))
            series_left = max(0, series_left - 1)
            log(series_remaining(symbol, series_left))

        return series_left

    async def _wait_for_new_signal(
        self,
        trade_key: str,
        log,
        symbol: str,
        timeframe: str,
        timeout: float = 30.0,
    ) -> Optional[dict]:
        """Ожидает новый сигнал в течение timeout секунд"""
        start_time = asyncio.get_event_loop().time()

        while self._running and (asyncio.get_event_loop().time() - start_time) < timeout:
            await self._pause_point()

            # Проверяем наличие нового сигнала
            if hasattr(self, "_common") and self._common is not None:
                new_signal = self._common.pop_latest_signal(trade_key)
                if new_signal:
                    return new_signal

            # Ждем немного перед следующей проверкой
            await asyncio.sleep(0.5)

        log(trade_timeout(symbol, timeout))
        return None

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        """Рассчитывает длительность сделки"""
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(
                0.0,
                (self._next_expire_dt - datetime.now(ZoneInfo(MOSCOW_TZ))).total_seconds(),
            )
            expected_end_ts = self._next_expire_dt.timestamp()
        else:
            trade_seconds = float(self._trade_minutes) * 60.0
            expected_end_ts = datetime.now().timestamp() + trade_seconds

        return trade_seconds, expected_end_ts

    def _notify_pending_trade(
        self,
        trade_id: str,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        trade_seconds: float,
        account_mode: str,
        expected_end_ts: float,
        *,
        signal_at: Optional[str] = None,
        series_label: Optional[str] = None,
    ):
        """Уведомляет о pending сделке"""
        placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        trade_key = self.build_trade_key(symbol, timeframe)
        if series_label is None:
            series_label = self.format_series_label(trade_key)
        self._set_planned_stake(trade_key, stake)
        if callable(self._on_trade_pending):
            try:
                self._on_trade_pending(
                    trade_id=trade_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    signal_at=signal_at or self._last_signal_at_str,
                    placed_at=placed_at_str,
                    direction=direction,
                    stake=float(stake),
                    percent=int(percent),
                    wait_seconds=float(trade_seconds),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                    expected_end_ts=expected_end_ts,
                    series=series_label,
                )
            except Exception:
                pass
