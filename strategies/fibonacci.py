# strategies/fibonacci.py
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from strategies.base_trading_strategy import BaseTradingStrategy
from strategies.strategy_helpers import (
    calc_next_candle_from_now,
    is_payout_low_now,
    update_signal_context,
    wait_for_new_signal,
)
from core.money import format_amount
from core.intrade_api_async import is_demo_account
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
    "allow_parallel_trades": False,
}


def _fib(n: int) -> int:
    """n-е число Фибоначчи (1-indexed): 1,1,2,3,5..."""
    if n <= 1:
        return 1
    a, b = 1, 1
    for _ in range(3, n + 1):
        a, b = b, a + b
    return b


class FibonacciStrategy(BaseTradingStrategy):
    """
    Фибоначчи под новую архитектуру StrategyCommon:

    - Серия по одному trade_key (пара/ТФ)
    - LOW payout ДО старта серии: коротко ждём, подхватывая самый свежий pending сигнал
    - Если сигнал устарел перед размещением: НЕ завершаем серию, ждём новый сигнал и продолжаем
    - После LOSS/UNKNOWN (по умолчанию) требуется новый сигнал
    """

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
        fib_params = dict(FIBONACCI_DEFAULTS)
        if params:
            fib_params.update(params)

        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=fib_params,
            strategy_name="Fibonacci",
            **kwargs,
        )

        self._active_series: dict[str, bool] = {}

    # =====================================================================
    # Public / required by StrategyCommon
    # =====================================================================

    def is_series_active(self, trade_key: str) -> bool:
        return self._active_series.get(trade_key, False)

    def should_request_fresh_signal_after_loss(self) -> bool:
        return True

    async def _process_single_signal(self, signal_data: dict) -> None:
        log = self.log or (lambda s: None)

        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        direction = int(signal_data["direction"])

        self._maybe_set_auto_minutes(timeframe)
        trade_key = self.build_trade_key(symbol, timeframe)

        # активная серия -> pending
        if self._active_series.get(trade_key):
            log(series_already_active(symbol, timeframe))
            common = getattr(self, "_common", None)
            if common is not None:
                await common._handle_pending_signal(trade_key, signal_data)
            return

        # --- LOW PAYOUT ДО СТАРТА СЕРИИ ---
        # коротко ждём и по пути подхватываем самый свежий pending сигнал
        while self._running and await is_payout_low_now(self, symbol):
            await self._pause_point()

            common = getattr(self, "_common", None)
            if common is not None:
                newer = common.pop_latest_signal(trade_key)
                if newer:
                    signal_data = newer
                    symbol = signal_data["symbol"]
                    timeframe = signal_data["timeframe"]
                    direction = int(signal_data["direction"])
                    self._maybe_set_auto_minutes(timeframe)
                    trade_key = self.build_trade_key(symbol, timeframe)

            await asyncio.sleep(float(self.params.get("wait_on_low_percent", 1)))

        # обновляем стандартный контекст стратегии
        signal_data, signal_received_time, direction, signal_at_str = update_signal_context(
            self,
            signal_data,
            update_symbol=self._use_any_symbol,
            update_timeframe=self._use_any_timeframe,
        )
        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        trade_key = self.build_trade_key(symbol, timeframe)

        # стартовая проверка актуальности (если неактуален — просто выходим, серию не стартуем)
        now = self.now_moscow()
        if self._trade_type == "classic":
            ok, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
            if not ok:
                log(signal_not_actual(symbol, "classic", reason))
                return
        else:
            ok, reason = self._is_signal_valid_for_sprint(signal_data, now)
            if not ok:
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

            updated_left = await self._run_fibonacci_series(
                trade_key=trade_key,
                symbol=symbol,
                timeframe=timeframe,
                initial_direction=int(direction),
                log=log,
                series_left=series_left,
                signal_received_time=signal_received_time,
                signal_data=signal_data,
            )
            self._set_series_left(trade_key, updated_left)

        finally:
            if series_started:
                self._active_series.pop(trade_key, None)
                log(series_completed(symbol, timeframe, "Фибоначчи"))

    # =====================================================================
    # SERIES
    # =====================================================================

    async def _run_fibonacci_series(
        self,
        *,
        trade_key: str,
        symbol: str,
        timeframe: str,
        initial_direction: int,
        log,
        series_left: int,
        signal_received_time: datetime,
        signal_data: dict,
    ) -> int:
        base_stake = float(self.params.get("base_investment", 100))
        max_steps = int(self.params.get("max_steps", 5))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))

        if max_steps <= 0:
            return series_left

        fib_index = 1
        step_idx = 0
        did_place_any_trade = False
        needs_signal_validation = True
        requires_fresh_signal = self.should_request_fresh_signal_after_loss()
        need_new_signal = False
        series_direction = int(initial_direction)

        signal_at_str = signal_data.get("signal_time_str") or self._last_signal_at_str
        series_label = self.format_series_label(trade_key, series_left=series_left)

        def _refresh_from(new_sig: Optional[dict]) -> None:
            nonlocal signal_data, signal_received_time, series_direction, signal_at_str, needs_signal_validation
            if not new_sig:
                return
            signal_data, signal_received_time, series_direction, signal_at_str = update_signal_context(self, new_sig)
            needs_signal_validation = True

        while self._running and step_idx < max_steps:
            await self._pause_point()

            if not await self.ensure_account_conditions():
                continue

            # --- 0) если после лосса/unknown нужно обновить сигнал ---
            if requires_fresh_signal and need_new_signal:
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_sig = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_sig:
                    log(trade_timeout(symbol, timeout))
                    break
                _refresh_from(new_sig)
                need_new_signal = False

            # --- 1) предварительная валидация сигнала (если неактуален — ждём новый, серию НЕ завершаем) ---
            if needs_signal_validation:
                now = self.now_moscow()
                if self._trade_type == "classic":
                    ok, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
                else:
                    payload = signal_data if signal_data.get("timestamp") else {"timestamp": signal_received_time}
                    ok, reason = self._is_signal_valid_for_sprint(payload, now)

                if not ok:
                    log(signal_not_actual_for_placement(symbol, reason))
                    timeout = float(self.params.get("signal_timeout_sec", 30.0))
                    new_sig = await wait_for_new_signal(self, trade_key, timeout=timeout)
                    if not new_sig:
                        log(trade_timeout(symbol, timeout))
                        return series_left
                    _refresh_from(new_sig)
                    continue

            # --- 2) ставка по Фибо ---
            stake = float(base_stake) * float(_fib(fib_index))

            pct, _bal = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue

            log(
                trade_summary(
                    symbol,
                    format_amount(stake),
                    self._trade_minutes,
                    series_direction,
                    pct,
                )
                + f" (Fibo #{fib_index})"
            )

            # --- 3) финальная проверка перед размещением (если неактуален — ждём новый, серию НЕ завершаем) ---
            now = self.now_moscow()
            if self._trade_type == "classic":
                ok, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
            else:
                payload = signal_data if signal_data.get("timestamp") else {"timestamp": signal_received_time}
                ok, reason = self._is_signal_valid_for_sprint(payload, now)

            if not ok:
                log(signal_not_actual_for_placement(symbol, reason))
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_sig = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_sig:
                    log(trade_timeout(symbol, timeout))
                    return series_left
                _refresh_from(new_sig)
                continue

            needs_signal_validation = False

            # classic: экспирация = следующая свеча от "сейчас"
            if self._trade_type == "classic":
                self._next_expire_dt = calc_next_candle_from_now(timeframe)

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            # --- 4) размещение ---
            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, stake, self._anchor_ccy)
            if not trade_id:
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                self._status("ожидание сигнала")
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_sig = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_sig:
                    log(trade_timeout(symbol, timeout))
                    return series_left
                _refresh_from(new_sig)
                continue

            did_place_any_trade = True

            trade_seconds, expected_end_ts = self.trade_duration()
            wait_seconds_cfg = self.params.get("result_wait_s")
            wait_seconds = trade_seconds if wait_seconds_cfg is None else float(wait_seconds_cfg)

            step_label = self.format_step_label(step_idx, max_steps)

            self._register_pending_trade(trade_id, symbol, timeframe)

            # pending notify (унифицировано через BaseTradingStrategy)
            self.notify_pending_trade(
                trade_id=str(trade_id),
                symbol=symbol,
                timeframe=timeframe,
                direction=series_direction,
                stake=float(stake),
                percent=int(pct),
                trade_seconds=float(trade_seconds),
                account_mode=account_mode,
                expected_end_ts=float(expected_end_ts),
                signal_at=signal_at_str,
                series_label=series_label,
                step_label=step_label,
            )

            # --- 5) ожидание результата ---
            task = self._spawn_result_checker(
                trade_id=str(trade_id),
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
                step_label=step_label,
            )

            # --- результат и продолжение серии ---
            try:
                profit = await task
            except asyncio.CancelledError:
                return series_left

            step_idx += 1

            if profit is None:
                log(result_unknown(symbol, treat_as_loss=True))
                profit = -1.0

            if profit > 0:
                fib_index = max(1, fib_index - 2)
                log(fibonacci_win(symbol, format_amount(profit), fib_index))
                break

            if profit == 0:
                log(fibonacci_push(symbol, fib_index))
            else:
                fib_index += 1
                log(fibonacci_loss(symbol, format_amount(profit)))
                need_new_signal = requires_fresh_signal

            needs_signal_validation = True

            if step_idx >= max_steps:
                break

        # --- завершение серии ---
        if did_place_any_trade:
            if step_idx >= max_steps:
                log(steps_limit_reached(symbol, max_steps))
            series_left = max(0, int(series_left) - 1)
            log(series_remaining(symbol, series_left))

        return series_left

    # =====================================================================
    # Stop
    # =====================================================================

    def stop(self) -> None:
        super().stop()
        self._active_series.clear()
