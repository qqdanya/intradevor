# strategies/antimartingale_strategy.py
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from strategies.base_trading_strategy import BaseTradingStrategy
from strategies.strategy_helpers import refresh_signal_context, wait_for_new_signal
from core.money import format_amount
from core.intrade_api_async import is_demo_account
from strategies.log_messages import (
    repeat_count_empty,
    series_already_active,
    signal_not_actual,
    signal_not_actual_for_placement,
    start_processing,
    trade_placement_failed,
    result_unknown,
    series_completed,
    trade_step,
    win_with_parlay,
    push_repeat_same_stake,
    loss_series_finish,
    steps_limit_reached,
    series_remaining,
)

ANTIMARTINGALE_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 3,
    "repeat_count": 10,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1.0,
    "signal_timeout_sec": 300,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}


class AntiMartingaleStrategy(BaseTradingStrategy):
    """
    AntiMartingale (Parlay) под новую архитектуру:

    - Увеличение ставки ТОЛЬКО после WIN
    - После WIN / PUSH — НЕ повторяем сделку на том же сигнале
      → всегда ждём новый сигнал или берём из очереди
    - После LOSS серия завершается
    - Устаревший сигнал НЕ завершает серию
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
        merged = dict(ANTIMARTINGALE_DEFAULTS)
        if params:
            merged.update(params)

        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=merged,
            strategy_name="AntiMartingale",
            **kwargs,
        )

        self._active_series: dict[str, bool] = {}

    # ======================================================================
    # Required by StrategyCommon
    # ======================================================================

    def is_series_active(self, trade_key: str) -> bool:
        return self._active_series.get(trade_key, False)

    # ======================================================================
    # Signal processing
    # ======================================================================

    async def _process_single_signal(self, signal_data: dict) -> None:
        log = self.log or (lambda s: None)

        # --- обновляем контекст сигнала ---
        ctx = refresh_signal_context(
            self,
            signal_data,
            update_symbol=self._use_any_symbol,
            update_timeframe=self._use_any_timeframe,
        )

        symbol = ctx.symbol
        timeframe = ctx.timeframe
        direction = ctx.direction
        trade_key = self.build_trade_key(symbol, timeframe)

        self._maybe_set_auto_minutes(timeframe)

        # --- если серия активна — сигнал уходит в pending ---
        if self._active_series.get(trade_key):
            log(series_already_active(symbol, timeframe))
            common = getattr(self, "_common", None)
            if common:
                await common._handle_pending_signal(trade_key, signal_data)
            return

        # --- лимит серий ---
        series_left = self._get_series_left(trade_key)
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return

        # --- старт серии ---
        self._active_series[trade_key] = True
        log(start_processing(symbol, "Антимартингейл"))

        try:
            updated_left = await self._run_antimartingale_series(
                trade_key=trade_key,
                symbol=symbol,
                timeframe=timeframe,
                initial_direction=direction,
                signal_data=signal_data,
                series_left=series_left,
                log=log,
            )
            self._set_series_left(trade_key, updated_left)

        finally:
            self._active_series.pop(trade_key, None)
            log(series_completed(symbol, timeframe, "Антимартингейл"))

    # ======================================================================
    # Series logic
    # ======================================================================

    async def _run_antimartingale_series(
        self,
        *,
        trade_key: str,
        symbol: str,
        timeframe: str,
        initial_direction: int,
        signal_data: dict,
        series_left: int,
        log,
    ) -> int:
        max_steps = int(self.params.get("max_steps", 3))
        base_stake = float(self.params.get("base_investment", 100))

        step = 0
        stake = base_stake
        direction = int(initial_direction)

        series_label = self.format_series_label(trade_key, series_left=series_left)

        while self._running and step < max_steps:
            await self._pause_point()

            # --- проверка аккаунта ---
            if not await self.ensure_account_conditions():
                continue

            # --- актуальность сигнала перед размещением ---
            now = self.now_moscow()
            if self._trade_type == "classic":
                ok, reason = self._is_signal_valid_for_classic(
                    signal_data, now, for_placement=True
                )
            else:
                ok, reason = self._is_signal_valid_for_sprint(signal_data, now)

            if not ok:
                log(signal_not_actual_for_placement(symbol, reason))
                timeout = float(self.params.get("signal_timeout_sec", 30))
                new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_signal:
                    return series_left

                ctx = refresh_signal_context(
                    self,
                    new_signal,
                    update_symbol=self._use_any_symbol,
                    update_timeframe=self._use_any_timeframe,
                )
                symbol = ctx.symbol
                timeframe = ctx.timeframe
                direction = ctx.direction
                signal_data = new_signal
                self._maybe_set_auto_minutes(timeframe)
                continue

            # --- payout + balance ---
            pct, _ = await self.check_payout_and_balance(
                symbol=symbol,
                stake=stake,
                min_pct=int(self.params.get("min_percent", 70)),
                wait_low=float(self.params.get("wait_on_low_percent", 1)),
            )
            if pct is None:
                continue

            log(
                trade_step(
                    symbol,
                    step,
                    format_amount(stake),
                    self._trade_minutes,
                    direction,
                    pct,
                )
            )

            # --- длительность сделки ---
            trade_seconds, expected_end_ts = self.trade_duration()

            # --- размещение ---
            trade_id = await self.place_trade_with_retry(
                symbol=symbol,
                direction=direction,
                stake=stake,
                account_ccy=self._anchor_ccy,
            )
            if not trade_id:
                log(trade_placement_failed(symbol, "Пропуск сигнала"))
                return series_left

            # --- pending ---
            self._register_pending_trade(trade_id, symbol, timeframe)

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            step_label = self.format_step_label(step, max_steps)

            self.notify_pending_trade(
                trade_id=str(trade_id),
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                stake=stake,
                percent=int(pct),
                trade_seconds=trade_seconds,
                account_mode=account_mode,
                expected_end_ts=expected_end_ts,
                signal_at=self._last_signal_at_str,
                series_label=series_label,
                step_label=step_label,
            )

            # --- результат ---
            profit = await self.wait_for_trade_result(
                trade_id=str(trade_id),
                wait_seconds=float(trade_seconds),
                placed_at=self.now_moscow().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=self._last_signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                stake=float(stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
                series_label=series_label,
                step_label=step_label,
            )

            if profit is None:
                log(result_unknown(symbol))
                return series_left

            if profit > 0:
                log(win_with_parlay(symbol, format_amount(profit)))
                stake += profit  # увеличиваем ставку
                step += 1

                # 🔴 ВАЖНО: ждём НОВЫЙ сигнал, а не повторяем
                timeout = float(self.params.get("signal_timeout_sec", 30))
                new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_signal:
                    return series_left

                ctx = refresh_signal_context(
                    self,
                    new_signal,
                    update_symbol=self._use_any_symbol,
                    update_timeframe=self._use_any_timeframe,
                )
                symbol = ctx.symbol
                timeframe = ctx.timeframe
                direction = ctx.direction
                signal_data = new_signal
                self._maybe_set_auto_minutes(timeframe)
                continue

            if abs(profit) < 1e-9:
                log(push_repeat_same_stake(symbol))

                timeout = float(self.params.get("signal_timeout_sec", 30))
                new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_signal:
                    return series_left

                ctx = refresh_signal_context(
                    self,
                    new_signal,
                    update_symbol=self._use_any_symbol,
                    update_timeframe=self._use_any_timeframe,
                )
                symbol = ctx.symbol
                timeframe = ctx.timeframe
                direction = ctx.direction
                signal_data = new_signal
                self._maybe_set_auto_minutes(timeframe)
                continue

            # LOSS
            log(loss_series_finish(symbol, format_amount(profit)))
            break

        # --- завершение серии ---
        if step >= max_steps:
            log(steps_limit_reached(symbol, max_steps))

        series_left = max(0, series_left - 1)
        log(series_remaining(symbol, series_left))
        return series_left

    # ======================================================================
    # Stop
    # ======================================================================

    def stop(self) -> None:
        super().stop()
        self._active_series.clear()
